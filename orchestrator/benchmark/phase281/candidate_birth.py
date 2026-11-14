"""Phase 2.8.1 T2：出生隔离 v2 的**父进程侧**（判定与落盘）。

设计一句话（计划 §6.1）：把「导入并执行候选」的进程与「判定层结果」的进程
**分开**。子进程（``birth_child``）只能往 fd 3 写**无签名的原始事件**；本模块
（父进程）自己用**内核可观测量**（``os.wait4`` 的退出码/信号/rusage、父进程单调
墙钟、管道字节与行数、父进程自己 ``stat`` 的工作目录、父进程自己解析的 AST）
加结构性事件计数来判定层结果，``birth.json`` 只由本模块写。

**消除了什么**（可机判）：v1 的同进程残余——候选在运行器读走 stdin 之后从
``__main__`` 全局变量里取出挑战值与结果路径，写出一条合法记录再 ``os._exit(0)``。
v2 的子进程**根本不持有**这两样东西（父进程不生成），所以「读全局变量」这条路
在地址空间层面不存在；AC-P281-2-01/03/15 分别从 cfg 键集、行为负例与源码扫描
三个角度机判这一点。

**没消除什么**（如实登记的固有上限，不得在报告里写强）：

1. **本设计不提供沙箱**。子进程仍可 fork、可写自己的文件、可伪造事件流；
2. ``nonce_in_child`` / ``nonce_absent`` 是**子进程自报、父进程校验格式**——
   父进程只能保证「子进程说了什么、格式对不对」，不能保证它没说谎；
3. 事件流的内容（步数、非有限值）是父进程唯一无法独立测得的量：一个**刻意
   构造一致事件流**的恶意子进程仍可能骗过判定。这是进程模型的固有上限，
   属 Phase 3 backlog（计划 §6.1 末段）。

失败 ``reason`` 是**闭集**（:data:`REASON_CODES`，13 个；计划 §6.4 表）。计划
写它「定义在 ``birth.py``」，但计划同时要求 ``birth.py 一字不动``（§6.9 commit
骨架）——两者不可同时成立，故**定义在本模块**并在 Complete 登记：
``birth.py`` 的 v1 枚举（14 个，含 ``too_small``/``compile_error``）是已结案阶段
的判据面，不得改动；v2 的语义面（``event_conflict`` 取代 ``too_small``）也不该
反过来污染 v1。
"""
from __future__ import annotations

import hashlib
import json
import os
import resource
import select
import shutil
import signal
import stat as statmod
import sys
import tempfile
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping

from orchestrator.benchmark import birth as v1
from orchestrator.benchmark import contracts as C
from orchestrator.benchmark import task_spec as ts
from orchestrator.benchmark.phase281 import birth_protocol as P
from orchestrator.benchmark.phase281.paths import assert_no_benchmark_root_env
from orchestrator.common.fsutil import atomic_write_text, ensure_private_dir

__all__ = (
    "BIRTH_NAME", "BIRTH_SCHEMA", "CHILD_BOOTSTRAP", "EVENTS_NAME",
    "LEVEL_STATUSES", "LEVELS", "LIMITS_DEFAULT", "NETWORK_MODULES",
    "REASON_CODES", "RUNNER_MARKER", "STATUSES",
    "BirthError", "BirthResultV2", "BirthSpecV2", "CandidateBirthValidator",
    "LimitsV2", "LevelOutcome", "child_env", "load_birth", "spec_from_task",
)

BIRTH_SCHEMA = "simpraxis.candidate_birth.v2"
BIRTH_NAME = "birth.json"
EVENTS_NAME = "birth.events.jsonl"
LEVELS = ("B0", "B1", "B2", "B3", "B4")
STATUSES = ("BORN", "NOT_BORN", "BLOCKED_INFRA")
LEVEL_STATUSES = ("PASS", "FAIL", "NOT_APPLICABLE", "BLOCKED_INFRA")

#: 闭集 13 个（计划 §6.4 表，顺序无关；单测断言「不多不少」）
REASON_CODES = (
    "missing", "symlink", "ast_error", "import_error", "construction_error",
    "contract_error", "step_error", "non_finite", "timeout", "memory",
    "path_escape", "event_conflict", "blocked_infra",
)

#: v1 的 stdout 标记：v2 的运行器**不再**写它，出现即候选伪造（AC-P281-2-02）
RUNNER_MARKER = v1.RUNNER_MARKER

#: 断网审计的 AST 扫描目标（与 v1 ``birth._NETWORK_MODULES`` 由单测钉死相等）
NETWORK_MODULES = ("socket", "requests", "urllib", "urllib3", "http",
                   "ftplib", "telnetlib", "aiohttp", "httpx")

#: 父/子 bootstrap：``-I`` 忽略 ``PYTHONPATH``/用户 site，引擎仓根由父进程作为
#: 位置参数传入（同 ``phase281_evaluate.py`` 的先例）。计划 §6.3.1 写的是
#: ``-I -m …birth_child``——``-I`` 下 ``-m`` 找不到未 pip 安装的包，故按同一
#: 先例落地（锁内适配，见 Plan_Complete）。
CHILD_BOOTSTRAP = (
    "import sys;"
    "sys.path.insert(0, sys.argv[1]);"
    "from orchestrator.benchmark.phase281.birth_child import main;"
    "raise SystemExit(main([sys.argv[0]] + sys.argv[2:]))"
)

#: 子进程回收后再给「持有管道的孙进程」的宽限（AC-P281-2-07）
GRANDCHILD_GRACE_S = 0.5
#: 击杀进程组后等管道排空的上限（超时即收紧，不让超时无上界）
KILL_GRACE_S = 5.0
#: 单管道读取块大小
_READ_CHUNK = 65536
#: stdout/stderr 落盘上限（v1 同口径 64 KiB；全量字节数仍如实记）
MAX_CAPTURE_BYTES = 64 * 1024

#: 把信号折算成 reason（父进程自己能观测到的类别，覆盖子进程的诊断）
_SIGNAL_REASON = {signal.SIGKILL: "memory", signal.SIGSEGV: "memory",
                  signal.SIGXFSZ: "memory", signal.SIGXCPU: "timeout"}
#: 每层「没有诊断事件」时的兜底 reason（v1 ``_GENERIC_REASON`` 的 v2 同义表）
_GENERIC_REASON = {"B0": "ast_error", "B1": "import_error",
                   "B2": "construction_error", "B3": "contract_error",
                   "B4": "step_error"}


class BirthError(RuntimeError):
    """出生验证自身不可继续（配置错误 / 输出目录不合规 / 环境越界）。

    **候选**的问题一律落成 FAIL/BLOCKED_INFRA 的结果件，不抛本异常
    （与 v1 ``birth.BirthError`` 同一口径）。
    """


# --------------------------------------------------------------------------
# 规格
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class LimitsV2:
    """子进程资源上限（改值必须在 Complete 登记——阈值属判据）。"""

    wall_s: float = 120.0
    cpu_s: int = 180
    mem_bytes: int = 4 * 1024 ** 3
    fsize_bytes: int = 64 * 1024 * 1024
    max_rss_mb: int = 4 * 1024
    disk_cap_bytes: int = 256 * 1024 * 1024
    steps: int = 1

    def to_json(self) -> dict:
        return {"wall_s": self.wall_s, "cpu_s": self.cpu_s,
                "mem_bytes": self.mem_bytes, "fsize_bytes": self.fsize_bytes,
                "max_rss_mb": self.max_rss_mb,
                "disk_cap_bytes": self.disk_cap_bytes, "steps": self.steps}


LIMITS_DEFAULT = LimitsV2()


@dataclass(frozen=True)
class BirthSpecV2:
    """一次出生检查的**最小**输入视图（不依赖 registry，测试可直接构造）。

    ``level`` 取 ``B0``..``B4`` 之一，或 ``"ALL"``（本阶段扩展：一次跑完五层，
    用于 AC-P281-2-10 的逐层 parity 与 CLI 的 ``--level ALL``）。
    """

    level: str
    entry_relpath: str
    limits: LimitsV2 = LIMITS_DEFAULT
    workdir: Path = Path(".")
    contract: str = "mujoco_push"
    entry_kind: str = "python_script"
    birth_contract: Mapping[str, bool] = field(default_factory=dict)
    min_bytes: int = 16
    ctx: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.level not in LEVELS + ("ALL",):
            raise BirthError(f"level 非法：{self.level!r}（合法值 {LEVELS + ('ALL',)}）")
        if self.entry_kind not in ("python_script", "module", "document_bundle"):
            raise BirthError(f"entry_kind 非法：{self.entry_kind!r}")

    def required_levels(self) -> "dict[str, bool]":
        bc = dict(self.birth_contract)
        return {"B0": True,
                "B1": bool(bc.get("import_required")),
                "B2": bool(bc.get("construction_required")),
                "B3": bool(bc.get("contract_required")),
                "B4": bool(bc.get("minimal_step_required"))}

    def levels_to_run(self) -> "tuple[str, ...]":
        return LEVELS if self.level == "ALL" else (self.level,)

    def to_json(self) -> dict:
        return {"level": self.level, "entry_relpath": self.entry_relpath,
                "limits": self.limits.to_json(), "workdir": str(self.workdir),
                "contract": self.contract, "entry_kind": self.entry_kind,
                "birth_contract": dict(self.birth_contract),
                "min_bytes": self.min_bytes, "ctx": dict(self.ctx),
                "required": self.required_levels()}


def spec_from_task(task_id: str, *, level: str = "ALL",
                   limits: LimitsV2 = LIMITS_DEFAULT,
                   workdir: "str | Path | None" = None,
                   ctx: "Mapping[str, Any] | None" = None) -> BirthSpecV2:
    """从冻结的任务定义直接造规格（CLI 与 parity 用；判据不来自候选）。

    ``task_id`` 是**登记名**（``mujoco_contact_push_v1``），不是目录名——
    两者在 2.8.0 起就不相等，故一律经 registry 解析，不拼路径。
    """
    return spec_from_birth_spec(v1.BirthSpec.from_task_spec(load_task(task_id)),
                                level=level, limits=limits, workdir=workdir,
                                ctx=ctx)


def load_task(task_id: str) -> "ts.TaskSpec":
    """经 registry 取已登记任务（顺带做一次清单与磁盘一致性校验）。"""
    from orchestrator.benchmark.registry import load_registry
    return load_registry().get(task_id)


def benchmark_task_path(task_id: str) -> Path:
    """任务定义的**实际**路径（由 registry 给出；目录名与 task_id 未必相同）。"""
    return Path(load_task(task_id).task_file)


def spec_from_birth_spec(birth_spec: "v1.BirthSpec", *, level: str = "ALL",
                         limits: LimitsV2 = LIMITS_DEFAULT,
                         workdir: "str | Path | None" = None,
                         ctx: "Mapping[str, Any] | None" = None) -> BirthSpecV2:
    """v1 ``BirthSpec`` → v2 规格（parity 测试的唯一转换点）。"""
    return BirthSpecV2(
        level=level,
        entry_relpath=birth_spec.entry_path,
        limits=limits,
        workdir=Path(workdir) if workdir is not None else Path("."),
        contract=birth_spec.contract,
        entry_kind=birth_spec.entry_kind,
        birth_contract=dict(birth_spec.birth_contract),
        min_bytes=birth_spec.min_bytes,
        ctx=dict(ctx or {}),
    )


# --------------------------------------------------------------------------
# 子进程环境与限制
# --------------------------------------------------------------------------

def child_env(base: "Mapping[str, str] | None" = None) -> "dict[str, str]":
    """父环境 → 子进程白名单环境（凭据变量一定不在其中）。

    fail-closed 的位置在这里而不是「父进程有没有凭据」：编排进程本身跑在带
    ``ANTHROPIC_*`` 的 shell 里是**正常**的（凭据按 export 名读取，硬规则 4），
    真正的不变量是**白名单里混进了凭据键就拒绝开工**——所以本函数自己再验一遍，
    不依赖 ``CHILD_ENV_ALLOWLIST`` 将来不被改坏。
    """
    source = dict(os.environ if base is None else base)
    env = {key: source[key] for key in P.CHILD_ENV_ALLOWLIST if key in source}
    leaked = sorted(key for key in env if key.startswith(P.REFUSED_ENV_PREFIXES))
    if leaked:
        raise BirthError(
            f"子进程环境白名单混入凭据变量 {leaked}：拒绝开工"
            "（硬规则 4；这不是父环境的错，是白名单被改坏了）")
    return env


def _refused_env() -> "list[str]":
    return sorted(key for key in os.environ
                  if key.startswith(P.REFUSED_ENV_PREFIXES))


#: cfg 里**允许**出现的键（闭集；多一个就说明有人把 v1 的挑战通道搬回来了）
CFG_KEYS = ("contract", "ctx", "entry", "level", "levels", "limits",
            "min_bytes", "required", "workdir")
#: 一旦在 cfg 里出现就拒绝写盘的词根（AC-P281-2-01 的机判面）
_CFG_FORBIDDEN = ("nonce", "challenge", "result_path", "result_file",
                  "token", "secret")


def _assert_cfg_clean(cfg: Mapping[str, Any]) -> None:
    """写盘前机判：cfg 的键集**恰是**闭集，且键名不含任何挑战/结果通道词根。

    这不是防御性编程——它是 AC-P281-2-01「子进程的输入里不存在挑战值与结果
    路径」的**唯一可机判面**：父进程若真的把这两样下发了，本函数立刻抛。
    """
    keys = set(cfg)
    if keys != set(CFG_KEYS):
        raise BirthError(
            f"cfg 键集越界：多出 {sorted(keys - set(CFG_KEYS))}、"
            f"缺少 {sorted(set(CFG_KEYS) - keys)}")
    for key in cfg:
        lowered = key.lower()
        for word in _CFG_FORBIDDEN:
            if word in lowered:
                raise BirthError(f"cfg 键 {key!r} 含禁止词根 {word!r}")


def _apply_limits(limits: LimitsV2) -> None:            # pragma: no cover - 子进程内生效
    resource.setrlimit(resource.RLIMIT_CPU,
                       (int(limits.cpu_s), int(limits.cpu_s) + 1))
    resource.setrlimit(resource.RLIMIT_AS,
                       (int(limits.mem_bytes), int(limits.mem_bytes)))
    resource.setrlimit(resource.RLIMIT_FSIZE,
                       (int(limits.fsize_bytes), int(limits.fsize_bytes)))


# --------------------------------------------------------------------------
# 结果
# --------------------------------------------------------------------------

@dataclass
class LevelOutcome:
    level: str
    status: str
    reason: "str | None" = None
    detail: str = ""
    kernel: dict = field(default_factory=dict)
    facts: dict = field(default_factory=dict)
    conflicts: "list[str]" = field(default_factory=list)
    events: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in LEVEL_STATUSES:
            raise BirthError(f"{self.level}: 非法状态 {self.status!r}")
        if self.reason is not None and self.reason not in REASON_CODES:
            raise BirthError(f"{self.level}: 非法 reason {self.reason!r}")

    def to_json(self) -> dict:
        return {"status": self.status, "reason": self.reason,
                "detail": self.detail, "kernel": dict(self.kernel),
                "facts": dict(self.facts), "conflicts": list(self.conflicts),
                "events": dict(self.events)}


@dataclass
class BirthResultV2:
    trial_id: str
    level: str
    status: str
    reason: "str | None"
    levels: dict
    kernel: dict
    events: dict
    workdir_facts: dict
    diagnostics: dict
    checked_at: str
    problems: "list[str]" = field(default_factory=list)
    limits: dict = field(default_factory=dict)
    schema: str = BIRTH_SCHEMA

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise BirthError(f"非法 status {self.status!r}")
        if self.reason is not None and self.reason not in REASON_CODES:
            raise BirthError(f"非法 reason {self.reason!r}")

    def to_json(self) -> dict:
        return {"schema": self.schema, "trial_id": self.trial_id,
                "level": self.level, "status": self.status,
                "reason": self.reason,
                "levels": {k: dict(v) for k, v in self.levels.items()},
                "kernel": dict(self.kernel), "events": dict(self.events),
                "workdir_facts": dict(self.workdir_facts),
                "diagnostics": dict(self.diagnostics),
                "problems": list(self.problems),
                "limits": dict(self.limits),
                "checked_at": self.checked_at}


# --------------------------------------------------------------------------
# 聚合（与 v1 ``BirthValidator._aggregate`` 同义）
# --------------------------------------------------------------------------

def _aggregate(levels: Mapping[str, Mapping], required: Mapping[str, bool]
               ) -> "tuple[str, str | None, list[str]]":
    """把已判的层汇总成 Trial 级状态；**只汇总在场的层**（缺席不算失败）。

    与 v1 的三条规则逐条同义：必需层失败 → ``NOT_BORN``；必需层被标
    NOT_APPLICABLE（契约自相矛盾）→ ``NOT_BORN``；有 BLOCKED_INFRA → ``BLOCKED_INFRA``。
    差别只在「缺席的层」：v1 五层恒在场，v2 允许单层运行，缺席层如实记为未跑，
    由 ``diagnostics.scope_complete`` 披露。
    """
    problems: "list[str]" = []
    present = [level for level in LEVELS if level in levels]
    failing = [level for level in present
               if required.get(level, True) and levels[level].get("status") == "FAIL"]
    na = [level for level in present
          if required.get(level, True)
          and levels[level].get("status") == "NOT_APPLICABLE"]
    infra = [level for level in present
             if levels[level].get("status") == "BLOCKED_INFRA"]
    for level in present:
        if not required.get(level, True) and levels[level].get("status") == "FAIL":
            problems.append(f"{level} 非必需层却 FAIL（{levels[level].get('reason')}）"
                            "——不影响结论，需人看")
    if failing:
        first = failing[0]
        reason = levels[first].get("reason") or _GENERIC_REASON[first]
        return "NOT_BORN", reason, problems + [f"必需层失败：{failing}"]
    if na:
        return "NOT_BORN", None, problems + [
            f"必需层被标 NOT_APPLICABLE（契约与 birth_contract 自相矛盾）：{na}"]
    if infra:
        return "BLOCKED_INFRA", "blocked_infra", problems + [
            f"基础设施阻塞层：{infra}"]
    return "BORN", None, problems


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------

def _close(fd: "int | None") -> None:
    if fd is None or fd < 0:
        return
    try:
        os.close(fd)
    except OSError:
        pass


def _write_all(fd: int, data: "bytes | bytearray") -> None:
    view = memoryview(bytes(data))
    while view:
        try:
            written = os.write(fd, view)
        except (BrokenPipeError, OSError):
            return
        if written <= 0:                                # pragma: no cover - 防御
            return
        view = view[written:]


def _first(events: "list[Mapping[str, Any]]", name: str) -> "Mapping[str, Any] | None":
    for event in events:
        if event.get("ev") == name:
            return event
    return None


def _first_error_reason(events: "list[Mapping[str, Any]]") -> "str | None":
    """第一条**诊断事件**携带的 reason（只接受闭集内的值，其余忽略）。"""
    for event in events:
        name = str(event.get("ev") or "")
        if not name.endswith("_error"):
            continue
        reason = event.get("reason")
        if reason in REASON_CODES:
            return str(reason)
    return None


def _forged_marker(stdout: str, stderr: str) -> "str | None":
    """stdout/stderr 出现 ``RUNNER_MARKER`` 行 ⇒ 候选伪造出生标记（v1 同判据）。"""
    for text in (stdout, stderr):
        for line in (text or "").splitlines():
            if line.startswith(RUNNER_MARKER):
                return line[:200]
    return None


def _count_events(events: "list[Mapping[str, Any]]") -> "dict[str, int]":
    counts: "dict[str, int]" = {}
    for event in events:
        name = str(event.get("ev") or "")
        counts[name] = counts.get(name, 0) + 1
    return counts


def _scan_network(path: Path) -> "list[dict]":
    """AST 扫一个文件的 import（与 v1 ``_scan_network`` 同口径）。"""
    import ast
    hits: "list[dict]" = []
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:                                   # noqa: BLE001 - 解析失败由 B0 报
        return hits
    for node in ast.walk(tree):
        names: "list[str]" = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        for name in names:
            root = name.split(".")[0]
            if root in NETWORK_MODULES:
                hits.append({"module": name, "lineno": getattr(node, "lineno", 0)})
    return hits


def _scan_network_tree(root: Path, limit: int = 200) -> "tuple[list[dict], int]":
    """扫 ``workdir`` 下全部 ``.py``（上限件数，触顶如实登记）。"""
    if not root.is_dir():
        return [], 0
    files = [p for p in sorted(root.rglob("*.py"))
             if p.is_file() and not p.is_symlink()][:limit]
    hits: "list[dict]" = []
    for path in files:
        rel = path.relative_to(root).as_posix()
        for hit in _scan_network(path):
            hits.append({"file": rel, "module": hit["module"],
                         "lineno": hit["lineno"]})
    return hits, len(files)


def _entry_path(workdir: Path, relpath: str) -> Path:
    """父进程侧的入口 jail（与子进程 ``birth_child._jail`` 同一判据）。"""
    if relpath.startswith("/") or ".." in Path(relpath).parts:
        raise _EntryProblem("path_escape", f"入口路径必须是 workdir 内的相对路径：{relpath!r}")
    wd = workdir.resolve(strict=True)
    p = wd / relpath
    if p.is_symlink():
        target = p.resolve(strict=False)
        inside = target == wd or wd in target.parents
        raise _EntryProblem("path_escape" if not inside else "symlink",
                            f"{relpath} 是符号链接（出生验证拒绝链接入口）-> {target}")
    if not p.exists():
        raise _EntryProblem("missing", f"{relpath} 不存在")
    if not p.is_file():
        raise _EntryProblem("missing", f"{relpath} 不是常规文件（is_file=False）")
    rp = p.resolve(strict=True)
    if rp != wd and wd not in rp.parents:
        raise _EntryProblem("path_escape", f"{relpath} 解析到 {rp}，越过 workdir {wd}")
    return rp


class _EntryProblem(RuntimeError):
    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


def _disk_bytes(workdir: Path) -> int:
    """父进程自己 ``stat`` 出来的工作目录字节数（与 ``_path_facts.bytes`` 同口径）。

    评审 CR-58：``RLIMIT_FSIZE`` 只是**单文件**上限，候选写 10×60 MB 文件就
    绕开 64 MiB 的 fsize 帽；``disk_cap_bytes`` 是总量帽，必须在层判定里执行，
    不能只写进 ``workdir_facts`` 当摆设。
    """
    total = 0
    for root, _dirnames, filenames in os.walk(workdir, followlinks=False):
        base = Path(root)
        for name in filenames:
            path = base / name
            try:
                info = path.lstat()
            except OSError:
                continue
            if not statmod.S_ISLNK(info.st_mode):
                total += info.st_size
            else:
                # 复核 2026-11-14：符号链接本身 0 字节，若直接跳过，指向
                # workdir 外大文件（/mnt 上预置件）的链接会完全绕开总量帽。
                # 按解析后的目标尺寸计入（悬空链接计 0），保证「工作目录
                # 可触达字节」口径真实。
                try:
                    total += path.stat().st_size
                except OSError:
                    continue
    return total


def _path_facts(workdir: Path, limits: LimitsV2) -> dict:
    """父进程自己 ``stat`` 出来的工作目录事实（候选无法伪造这一份）。"""
    files = 0
    total = 0
    links: "list[str]" = []
    for root, dirnames, filenames in os.walk(workdir, followlinks=False):
        base = Path(root)
        for name in sorted(filenames):
            path = base / name
            rel = path.relative_to(workdir).as_posix()
            try:
                info = path.lstat()
            except OSError:
                continue
            files += 1
            if statmod.S_ISLNK(info.st_mode):
                if len(links) < 64:
                    links.append(rel)
            else:
                total += info.st_size
        for name in sorted(dirnames):
            path = base / name
            if path.is_symlink() and len(links) < 64:
                links.append(path.relative_to(workdir).as_posix())
    hits, scanned = _scan_network_tree(workdir)
    return {"files": files, "bytes": total, "symlinks": links,
            "network_import_hits": hits, "scanned_files": scanned,
            "over_disk_cap": total > limits.disk_cap_bytes}


# --------------------------------------------------------------------------
# 验证器
# --------------------------------------------------------------------------

class CandidateBirthValidator:
    """父进程侧的出生验证器：spawn 子进程 → 收事件 → 按内核事实判定 → 写 birth.json。"""

    def __init__(self, *, trial_dir: "str | Path", trial_id: str,
                 python: "str | None" = None,
                 repo_root: "str | Path | None" = None) -> None:
        assert_no_benchmark_root_env()
        # 父进程**允许**持有凭据（编排进程就在这样的 shell 里跑）；这里只把变量
        # **名**记进诊断，用来证明「父进程有、子进程没有」——值一个字节都不留。
        self.parent_credential_env = _refused_env()
        self.trial_dir = Path(trial_dir)
        self.trial_id = str(trial_id)
        self.python = python or sys.executable
        self.repo_root = (Path(repo_root) if repo_root is not None
                          else Path(__file__).resolve().parents[3])

    # -- 单层 ---------------------------------------------------------------
    def _run_b0(self, spec: BirthSpecV2) -> LevelOutcome:
        import ast
        workdir = spec.workdir
        try:
            entry = _entry_path(workdir, spec.entry_relpath)
        except _EntryProblem as exc:
            return LevelOutcome(level="B0", status="FAIL", reason=exc.reason,
                                detail=exc.detail, kernel={"spawned": False})
        size = entry.stat().st_size
        if size < spec.min_bytes:
            return LevelOutcome(
                level="B0", status="FAIL", reason="missing",
                detail=f"{spec.entry_relpath} 只有 {size} 字节（下限 {spec.min_bytes}）",
                kernel={"spawned": False})
        if entry.suffix == ".py":
            try:
                ast.parse(entry.read_text(encoding="utf-8"))
            except (SyntaxError, ValueError, UnicodeDecodeError) as exc:
                return LevelOutcome(level="B0", status="FAIL", reason="ast_error",
                                    detail=f"{type(exc).__name__}: {exc}",
                                    kernel={"spawned": False})
        elif entry.suffix in (".yaml", ".yml"):
            import yaml
            try:
                doc = yaml.safe_load(entry.read_text(encoding="utf-8"))
            except (ValueError, UnicodeDecodeError) as exc:
                return LevelOutcome(level="B0", status="FAIL", reason="ast_error",
                                    detail=f"YAML 读取失败：{type(exc).__name__}: {exc}",
                                    kernel={"spawned": False})
            except BaseException as exc:                # noqa: BLE001 - 归 ast_error
                return LevelOutcome(level="B0", status="FAIL", reason="ast_error",
                                    detail=f"YAML 解析失败：{type(exc).__name__}: {exc}",
                                    kernel={"spawned": False})
            if not isinstance(doc, (dict, list)):
                return LevelOutcome(level="B0", status="FAIL", reason="ast_error",
                                    detail="YAML 顶层必须是 mapping/sequence",
                                    kernel={"spawned": False})
        hits, scanned = _scan_network_tree(workdir)
        return LevelOutcome(
            level="B0", status="PASS",
            detail=f"{spec.entry_relpath} {size} 字节（父进程 stat+AST）",
            kernel={"spawned": False},
            facts={"bytes": size, "suffix": entry.suffix,
                   "scanned_files": scanned, "network_import_hits": hits})

    def _cfg(self, spec: BirthSpecV2, level: str) -> dict:
        """子进程 ``cfg.json``：**只有**层/工作目录/入口/契约/上限/必需表。

        AC-P281-2-01 的机判面就在这个函数的返回值上：键集里没有挑战值、也没有
        结果路径（:func:`_assert_cfg_clean` 逐键机判一次，写盘前）。
        """
        return {"level": level,
                "levels": [level],
                "workdir": str(spec.workdir),
                "entry": {"kind": spec.entry_kind, "path": spec.entry_relpath},
                "contract": spec.contract,
                "min_bytes": spec.min_bytes,
                "required": spec.required_levels(),
                "ctx": dict(spec.ctx),
                "limits": {"steps": int(spec.limits.steps),
                           "wall_s": float(spec.limits.wall_s)}}

    def _run_child_level(self, spec: BirthSpecV2, level: str) -> LevelOutcome:
        level_dir = ensure_private_dir(self.trial_dir / "birth_levels" / level)
        if spec.level == "ALL":
            events_path = self.trial_dir / f"birth.events.{level}.jsonl"
        else:
            events_path = self.trial_dir / EVENTS_NAME
        cfg = self._cfg(spec, level)
        _assert_cfg_clean(cfg)
        cfg_path = atomic_write_text(
            level_dir / "cfg.json",
            json.dumps(cfg, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            0o600)
        scratch_root = ensure_private_dir(self.trial_dir / ".birth_tmp")
        cwd = Path(tempfile.mkdtemp(prefix=f"birth-{level}-", dir=str(scratch_root)))
        try:
            run = self._spawn(spec, cfg_path, events_path, cwd)
        finally:
            shutil.rmtree(cwd, ignore_errors=True)
        # 评审 CR-58：子进程退出后立刻量一次工作根总量（父进程 stat，候选
        # 无法伪造）；_judge 里按 disk_cap_bytes 判负。
        run["disk_bytes"] = _disk_bytes(spec.workdir)
        events, ev_problems = P.read_events(events_path)
        return self._judge(spec, level, run, events, ev_problems,
                           str(events_path), len(cfg["levels"]))

    # -- 生成与回收 ---------------------------------------------------------
    def _spawn(self, spec: BirthSpecV2, cfg_path: Path, events_path: Path,
               cwd: Path) -> dict:
        ev_r, ev_w = os.pipe()
        out_r, out_w = os.pipe()
        err_r, err_w = os.pipe()
        opened = [ev_r, ev_w, out_r, out_w, err_r, err_w]
        if min(opened) < 3:
            for fd in opened:
                _close(fd)
            raise BirthError("管道 fd 低于 3：无法保证 fd 3 事件通道")
        devnull = os.open(os.devnull, os.O_RDONLY)
        ev_fd = os.open(events_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        argv = [self.python, "-I", "-c", CHILD_BOOTSTRAP,
                str(self.repo_root), str(cfg_path)]
        env = child_env()
        started = time.monotonic()
        try:
            pid = os.fork()
        except OSError as exc:
            for fd in opened + [devnull, ev_fd]:
                _close(fd)
            return {"spawn_error": f"{type(exc).__name__}: {exc}",
                    "kernel": {"spawned": False, "spawned_error": str(exc)},
                    "stdout": "", "stderr": "", "events_bytes": 0,
                    "timed_out": False, "reaped": False}
        if pid == 0:                                    # pragma: no cover - 子进程
            try:
                os.setsid()
                _apply_limits(spec.limits)
                os.dup2(devnull, 0)
                os.dup2(out_w, 1)
                os.dup2(err_w, 2)
                if ev_w != P.EVENT_FD:
                    os.dup2(ev_w, P.EVENT_FD)
                os.closerange(4, 4096)
                os.chdir(str(cwd))
                os.execve(self.python, argv, env)
            except BaseException:                       # noqa: BLE001 - 子进程自毁
                pass
            os._exit(127)
        for fd in (ev_w, out_w, err_w, devnull):
            _close(fd)
        try:
            return self._drain(pid, {"ev": ev_r, "out": out_r, "err": err_r},
                               ev_fd, spec.limits, started, argv, env)
        finally:
            for fd in (ev_r, out_r, err_r, ev_fd):
                _close(fd)

    def _drain(self, pid: int, fds: "dict[str, int]", ev_fd: int,
               limits: LimitsV2, started: float, argv: "list[str]",
               env: "dict[str, str]") -> dict:
        open_fds = dict(fds)
        out_buf = bytearray()
        err_buf = bytearray()
        out_total = err_total = 0
        ev_bytes = 0
        status = None
        rusage = None
        reaped = False
        timed_out = False
        reaped_at = 0.0

        def consume(fd: int) -> None:
            """读一次 fd：事件字节直写事件文件，stdout/stderr 有上限地攒下来。"""
            nonlocal out_total, err_total, ev_bytes
            name = next((k for k, v in open_fds.items() if v == fd), None)
            if name is None:
                return
            try:
                chunk = os.read(fd, _READ_CHUNK)
            except OSError:
                chunk = b""
            if not chunk:                               # EOF（或读失败）即摘除
                _close(fd)
                open_fds.pop(name, None)
                return
            if name == "ev":
                _write_all(ev_fd, chunk)
                ev_bytes += len(chunk)
            elif name == "out":
                out_total += len(chunk)
                if len(out_buf) < MAX_CAPTURE_BYTES:
                    out_buf.extend(chunk)
            else:
                err_total += len(chunk)
                if len(err_buf) < MAX_CAPTURE_BYTES:
                    err_buf.extend(chunk)

        while True:
            if not reaped:
                try:
                    wpid, st, ru = os.wait4(pid, os.WNOHANG)
                except ChildProcessError:               # 已被别处回收
                    wpid, st, ru = pid, None, None
                if wpid == pid:
                    reaped = True
                    status, rusage = st, ru
                    reaped_at = time.monotonic()
            now = time.monotonic()
            if not reaped and (now - started) > limits.wall_s:
                timed_out = True
                break
            if reaped and not open_fds:
                break
            if reaped and (now - reaped_at) > GRANDCHILD_GRACE_S:
                break
            if not open_fds:
                time.sleep(0.01)
                continue
            try:
                ready = select.select(sorted(open_fds.values()), [], [], 0.05)[0]
            except (InterruptedError, OSError):
                continue
            for fd in ready:
                consume(fd)

        # 三条都要整组清理：①子进程没回收；②超时；③**子进程已退出但管道仍被
        # 持有**——后者正是「候选 fork 后父进程退出、孙进程继续」的形态
        # （AC-P281-2-07）：组里还有活着的成员，不杀就会留下无主进程。
        if open_fds or not reaped or timed_out:
            self._terminate_group(pid)
            grace_end = time.monotonic() + KILL_GRACE_S
            while open_fds and time.monotonic() < grace_end:
                try:
                    ready = select.select(sorted(open_fds.values()), [], [], 0.05)[0]
                except (InterruptedError, OSError):
                    break
                for fd in ready:
                    consume(fd)
            if not reaped:
                reaped, status, rusage = self._wait_blocking(pid)
        for fd in list(open_fds.values()):
            _close(fd)

        wall_s = time.monotonic() - started
        kernel = {"spawned": True, "reaped": bool(reaped),
                  "timed_out": bool(timed_out),
                  "exit_code": None, "signal": None, "utime_s": None,
                  "stime_s": None, "maxrss_kb": None,
                  "wall_s": round(wall_s, 6),
                  "argv_isolated": "-I" in argv,
                  "env_keys": sorted(env)}
        if status is not None:
            if os.WIFEXITED(status):
                kernel["exit_code"] = os.WEXITSTATUS(status)
            if os.WIFSIGNALED(status):
                kernel["signal"] = int(os.WTERMSIG(status))
                kernel["exit_code"] = -int(os.WTERMSIG(status))
            if os.WIFSTOPPED(status):                   # pragma: no cover - 防御
                kernel["stopped"] = True
        if rusage is not None:
            kernel["utime_s"] = round(rusage.ru_utime, 6)
            kernel["stime_s"] = round(rusage.ru_stime, 6)
            kernel["maxrss_kb"] = int(rusage.ru_maxrss)
        return {"kernel": kernel,
                "stdout": out_buf.decode("utf-8", errors="replace"),
                "stderr": err_buf.decode("utf-8", errors="replace"),
                "stdout_bytes": out_total, "stderr_bytes": err_total,
                "events_bytes": ev_bytes,
                "timed_out": bool(timed_out), "reaped": bool(reaped)}

    def _terminate_group(self, pid: int) -> None:
        """整组击杀（``setsid`` 让子进程自成进程组，孙进程默认仍在组内）。

        ``getpgid`` 在组长**已被回收**后必然失败，而 ``setsid`` 保证了
        「进程组号 == 组长 pid」，所以此时退回 ``pid`` 当组号仍然能清掉整组
        ——退回 ``os.kill(pid)`` 只会得到一次 ``ProcessLookupError``，把
        fork 逃逸的孙进程留在系统里（AC-P281-2-07 的机判面）。
        """
        try:
            pgid = os.getpgid(pid)
        except (ProcessLookupError, PermissionError, OSError):
            pgid = pid
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                os.killpg(pid, signal.SIGKILL)
            except OSError:
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass

    def _wait_blocking(self, pid: int) -> "tuple[bool, int | None, object | None]":
        end = time.monotonic() + KILL_GRACE_S
        while time.monotonic() < end:
            try:
                wpid, st, ru = os.wait4(pid, os.WNOHANG)
            except ChildProcessError:
                return True, None, None
            if wpid == pid:
                return True, st, ru
            time.sleep(0.01)
        return False, None, None

    # -- 判定 ---------------------------------------------------------------
    def _judge(self, spec: BirthSpecV2, level: str, run: dict,
               events: "list[Mapping[str, Any]]", ev_problems: "list[str]",
               events_path: str, n_levels: int) -> LevelOutcome:
        kernel = dict(run.get("kernel") or {})
        counts = _count_events(events)
        steps = [e for e in events if e.get("ev") == P.EV_STEP]
        conflicts: "list[str]" = [f"事件流：{item}" for item in ev_problems]
        violations: "list[str]" = []

        start = _first(events, P.EV_START)
        if start is None:
            violations.append("缺少 start 事件（子进程未按协议开工）")
        elif P.FIELD_ABSENT not in start:
            violations.append(
                f"start 事件缺少自证字段 {P.FIELD_ABSENT}（格式不符）")
        elif start.get(P.FIELD_ABSENT) is not True:
            violations.append(
                f"start 事件自报 {P.FIELD_ABSENT}={start.get(P.FIELD_ABSENT)!r}（应为 true）")
        forged = _forged_marker(run.get("stdout") or "", run.get("stderr") or "")
        if forged is not None:
            violations.append(f"候选伪造出生标记行（不采信）：{forged}")

        verdict, verdict_detail = self._level_verdict(spec, level, events, kernel)
        claim = None
        for event in events:
            if event.get("ev") == P.EV_LEVEL and event.get("level") == level:
                claim = event
        if claim is not None:
            want = "pass" if verdict else "fail"
            if claim.get("outcome") != want:
                conflicts.append(
                    f"{level}: 子进程自报 outcome={claim.get('outcome')!r}，"
                    f"父进程判定 {want!r}（判决以父进程为准）")

        facts = {"events": counts, "steps": len(steps),
                 "events_bytes": int(run.get("events_bytes") or 0),
                 "stdout_bytes": int(run.get("stdout_bytes") or 0),
                 "stderr_bytes": int(run.get("stderr_bytes") or 0),
                 "parent_verdict": "pass" if verdict else "fail",
                 "verdict_detail": verdict_detail,
                 "start_absent": (None if start is None
                                  else start.get(P.FIELD_ABSENT) is True)}
        events_doc = {"count": len(events), "steps": len(steps),
                      "bytes": int(run.get("events_bytes") or 0),
                      "path": events_path, "levels": n_levels}

        def _fail(reason: str, detail: str) -> LevelOutcome:
            return LevelOutcome(level=level, status="FAIL", reason=reason,
                                detail=detail, kernel=kernel, facts=facts,
                                conflicts=list(conflicts), events=events_doc)

        if run.get("spawn_error"):
            return LevelOutcome(level=level, status="BLOCKED_INFRA",
                                reason="blocked_infra",
                                detail=f"受限子进程无法启动：{run['spawn_error']}",
                                kernel=kernel, facts=facts, conflicts=list(conflicts),
                                events=events_doc)
        if not kernel.get("reaped", False):
            return LevelOutcome(level=level, status="BLOCKED_INFRA",
                                reason="blocked_infra",
                                detail="子进程无法回收（进程组清理失败）",
                                kernel=kernel, facts=facts, conflicts=list(conflicts),
                                events=events_doc)
        if run.get("timed_out"):
            return _fail("timeout",
                         f"超过 {spec.limits.wall_s}s 上限（进程组已整组击杀）")
        if kernel.get("signal") in _SIGNAL_REASON:
            return _fail(_SIGNAL_REASON[kernel["signal"]],
                         f"子进程被信号 {kernel['signal']} 终止"
                         f"（maxrss={kernel.get('maxrss_kb')} KiB）")
        max_rss_kb = kernel.get("maxrss_kb") or 0
        if max_rss_kb > spec.limits.max_rss_mb * 1024:
            return _fail("memory",
                         f"maxrss {max_rss_kb} KiB 超过上限 "
                         f"{spec.limits.max_rss_mb} MiB")
        #: 评审 CR-58：总量磁盘帽（``RLIMIT_FSIZE`` 只管单文件）。AC-P281-2-08
        #: 允许的资源类 reason 是 ``memory``/``blocked_infra``，这里与
        #: ``SIGXFSZ → memory`` 同口径，统一落 ``memory``。
        disk_bytes = int(run.get("disk_bytes") or 0)
        if disk_bytes > spec.limits.disk_cap_bytes:
            return _fail("memory",
                         f"workdir 写入 {disk_bytes} B 超过磁盘帽 "
                         f"{spec.limits.disk_cap_bytes} B")
        if violations:
            return _fail("contract_error", "; ".join(violations))
        if ev_problems:
            return _fail("event_conflict", "事件流被截断/损坏：" + "; ".join(ev_problems))
        reason = _first_error_reason(events)
        if reason is not None:
            detail = ""
            for event in events:
                if str(event.get("ev") or "").endswith("_error"):
                    detail = str(event.get("detail") or "")
                    break
            return _fail(reason, detail or f"子进程报告 {reason}")
        if not verdict:
            return _fail(_GENERIC_REASON[level], verdict_detail)
        return LevelOutcome(level=level, status="PASS", detail=verdict_detail,
                            kernel=kernel, facts=facts, conflicts=list(conflicts),
                            events=events_doc)

    def _level_verdict(self, spec: BirthSpecV2, level: str,
                       events: "list[Mapping[str, Any]]", kernel: Mapping,
                       ) -> "tuple[bool, str]":
        """**父进程侧**的层判定（只用手上的内核事实与结构性事件计数）。"""
        counts = _count_events(events)
        steps = [e for e in events if e.get("ev") == P.EV_STEP]
        spawned = bool(kernel.get("spawned"))
        exit_code = kernel.get("exit_code")
        base_ok = spawned and exit_code == 0 and kernel.get("signal") is None
        if not spawned:
            return False, "父进程未起子进程（该层由父进程自己判定）"
        if level == "B1":
            ok = (base_ok and counts.get(P.EV_IMPORTED, 0) >= 1
                  and counts.get(P.EV_IMPORT_ERROR, 0) == 0)
            return ok, ("退出码 0 且有 imported 事件且无 import_error"
                        if ok else
                        f"B1 未达成：rc={exit_code} imported="
                        f"{counts.get(P.EV_IMPORTED, 0)} "
                        f"import_error={counts.get(P.EV_IMPORT_ERROR, 0)}")
        if level == "B2":
            ok = (base_ok and counts.get(P.EV_IMPORTED, 0) >= 1
                  and counts.get(P.EV_CONSTRUCTED, 0) >= 1
                  and counts.get(P.EV_IMPORT_ERROR, 0) == 0
                  and counts.get(P.EV_CONSTRUCTION_ERROR, 0) == 0)
            return ok, ("有 imported+constructed 事件且无 *_error"
                        if ok else "B2 未达成：缺 imported/constructed 事件或出现 *_error")
        if level == "B3":
            ok = (base_ok and counts.get(P.EV_CONTRACT, 0) >= 1
                  and counts.get(P.EV_CONTRACT_ERROR, 0) == 0)
            if ok:
                ok = all(int(event.get("violations") or 0) == 0 for event in events
                         if event.get("ev") == P.EV_CONTRACT)
            try:
                needs_object = C.contract_by_name(spec.contract).needs_object
            except BaseException:                       # noqa: BLE001 - 契约不可用
                return False, f"契约 {spec.contract!r} 不可用（基础设施）"
            if ok and needs_object:
                ok = (counts.get(P.EV_IMPORTED, 0) >= 1
                      and counts.get(P.EV_CONSTRUCTED, 0) >= 1)
            return ok, ("contract 事件在场且 violations=0" if ok
                        else "B3 未达成：缺 contract 事件、violations>0 或出现 contract_error")
        if level == "B4":
            cpu_s = (float(kernel.get("utime_s") or 0.0)
                     + float(kernel.get("stime_s") or 0.0))
            ok = (base_ok and len(steps) >= 1
                  and all(step.get("status") == "ok" for step in steps)
                  and counts.get(P.EV_STEP_ERROR, 0) == 0
                  and counts.get(P.EV_IMPORTED, 0) >= 1
                  and counts.get(P.EV_CONSTRUCTED, 0) >= 1
                  and cpu_s > 0.0)
            advance = _first(events, P.EV_ADVANCE)
            ok = ok and advance is not None and advance.get("advanced") is True
            return ok, (f"{len(steps)} 个 step 事件全 ok、advance=true、cpu={cpu_s:.6f}s"
                        f" 且无 step_error" if ok else
                        f"B4 未达成：steps={len(steps)} "
                        f"advance={None if advance is None else advance.get('advanced')} "
                        f"cpu={cpu_s:.6f}s")
        return False, f"未知层 {level!r}"

    # -- 入口 ---------------------------------------------------------------
    def run(self, spec: BirthSpecV2) -> BirthResultV2:
        assert_no_benchmark_root_env()
        workdir = Path(spec.workdir)
        if not workdir.is_dir():
            raise BirthError(f"workdir 不存在或不是目录：{workdir}")
        # 与 v1 ``BirthValidator.__init__`` 同义：契约检查要能拿到 workdir
        resolved = workdir.resolve()
        ctx = dict(spec.ctx)
        ctx.setdefault("workdir", str(resolved))
        spec = replace(spec, workdir=resolved, ctx=ctx)
        prior = self._load_prior()
        outcomes: "dict[str, LevelOutcome]" = {}
        for level in spec.levels_to_run():
            if not spec.required_levels()[level]:
                outcomes[level] = LevelOutcome(
                    level=level, status="NOT_APPLICABLE",
                    detail="birth_contract 未要求该层（不跑、不计入 PASS）",
                    kernel={"spawned": False})
            elif level == "B0":
                # B0 由**父进程**自己判：它只需要 stat + AST，起子进程只会把
                # 「父进程本可独立测得的事实」交给候选去说。
                outcomes[level] = self._run_b0(spec)
            else:
                outcomes[level] = self._run_child_level(spec, level)
        return self._assemble(spec, prior, outcomes)

    def _load_prior(self) -> dict:
        """读回本 Trial 既有的 ``birth.json``（**只读 trial_dir，绝不读 workdir**）。

        AC-P281-2-04：候选可以把 ``status=BORN`` 写进自己的工作目录，父进程
        **不读**那里——唯一读点是 ``<trial_dir>/birth.json``，而该文件只由
        :meth:`write` 写。
        """
        target = self.trial_dir / BIRTH_NAME
        if target.is_symlink() or not target.is_file():
            return {}
        try:
            doc = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(doc, dict) or doc.get("schema") != BIRTH_SCHEMA:
            return {}
        if doc.get("trial_id") != self.trial_id:
            return {}
        return doc

    def _assemble(self, spec: BirthSpecV2, prior: Mapping,
                  outcomes: Mapping[str, LevelOutcome]) -> BirthResultV2:
        levels = dict(prior.get("levels") or {})
        levels.update({level: outcome.to_json()
                       for level, outcome in outcomes.items()})
        required = spec.required_levels()
        status, reason, problems = _aggregate(levels, required)
        conflicts = list((prior.get("diagnostics") or {}).get("event_conflicts") or [])
        for level in spec.levels_to_run():
            conflicts.extend(f"{level}: {item}" for item in outcomes[level].conflicts)
        paths = dict((prior.get("events") or {}).get("paths") or {})
        for level in spec.levels_to_run():
            paths[level] = outcomes[level].events.get("path")
        events = {
            "count": sum(int(o.events.get("count") or 0) for o in outcomes.values()),
            "steps": sum(int(o.events.get("steps") or 0) for o in outcomes.values()),
            "bytes": sum(int(o.events.get("bytes") or 0) for o in outcomes.values()),
            "paths": {k: v for k, v in sorted(paths.items()) if v},
        }
        events["path"] = (events["paths"].get(spec.levels_to_run()[0])
                          if len(spec.levels_to_run()) == 1 else None)
        last = outcomes[spec.levels_to_run()[-1]]
        spawned = [o for o in outcomes.values() if o.kernel.get("spawned")]
        self_reports = {o.level: (levels.get(o.level) or {}).get("facts", {})
                        .get("start_absent") for o in spawned}
        diagnostics = {
            # 负向事实：子进程侧**不存在**挑战值（计划 §6.4 表的取值就是 false）
            "nonce_in_child": False,
            # 子进程自报（start 事件的 nonce_absent 字段），父进程只校验格式。
            # 没有起过子进程（例如只跑 B0）时为空真：这一位只说明「没有反例」，
            # 不说明「验过了」——后者看 nonce_absent_verified。
            P.FIELD_ABSENT: all(value is True for value in self_reports.values()),
            "nonce_absent_verified": bool(self_reports) and all(
                value is True for value in self_reports.values()),
            "nonce_absent_self_report": {k: v for k, v in sorted(self_reports.items())},
            # 父进程环境**有**哪些凭据变量名（证明「父有子无」；只记名不记值）
            "parent_credential_env": list(self.parent_credential_env),
            "event_conflicts": conflicts,
            "problems": list(problems),
            "required": dict(required),
            "scope_complete": all(level in levels for level in LEVELS
                                  if required[level]),
            "runner_marker": RUNNER_MARKER,
            "protocol": P.PROTOCOL_VERSION,
        }
        return BirthResultV2(
            trial_id=self.trial_id, level=spec.level, status=status,
            reason=reason, levels=levels, kernel=dict(last.kernel),
            events=events,
            workdir_facts=_path_facts(spec.workdir, spec.limits),
            diagnostics=diagnostics, checked_at=P.now_wall(),
            problems=problems, limits=spec.limits.to_json())

    # -- 落盘 ---------------------------------------------------------------
    def write(self, result: BirthResultV2) -> dict:
        """原子写 ``<trial_dir>/birth.json``（0600；目录 0700；不落在符号链接上）。"""
        assert_no_benchmark_root_env()
        directory = ensure_private_dir(self.trial_dir)
        target = directory / BIRTH_NAME
        if target.is_symlink():
            raise BirthError(
                f"{target} 是符号链接：拒绝写入（出生件不得落在链接上）")
        body = json.dumps(result.to_json(), ensure_ascii=False, indent=2,
                          sort_keys=True) + "\n"
        path = atomic_write_text(target, body, 0o600)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        return {"birth_path": str(path), "birth_sha256": digest,
                "status": result.status, "reason": result.reason,
                "scope_complete": result.diagnostics.get("scope_complete")}


def load_birth(path: "str | Path") -> dict:
    """读一份 v2 出生件并 fail-closed 校验（schema/状态/reason/层状态）。"""
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    if doc.get("schema") != BIRTH_SCHEMA:
        raise BirthError(f"{path}: schema={doc.get('schema')!r} 非 {BIRTH_SCHEMA}")
    if doc.get("status") not in STATUSES:
        raise BirthError(f"{path}: 非法 status {doc.get('status')!r}")
    if doc.get("reason") is not None and doc.get("reason") not in REASON_CODES:
        raise BirthError(f"{path}: 非法 reason {doc.get('reason')!r}")
    levels = doc.get("levels")
    if not isinstance(levels, dict) or not levels:
        raise BirthError(f"{path}: levels 缺失或为空")
    for level, entry in levels.items():
        if level not in LEVELS:
            raise BirthError(f"{path}: 未知层 {level!r}")
        if not isinstance(entry, dict) or entry.get("status") not in LEVEL_STATUSES:
            raise BirthError(f"{path}: {level} 非法状态 {entry!r}")
    return doc
