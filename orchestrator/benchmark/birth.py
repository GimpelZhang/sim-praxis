"""Task 3 Functional Birth Validator：B0→B4 五层出生验证（Plan T3.5）。

**Birth ≠ Task Success**：本模块只回答「交付物是否达到可执行的最低程度」，
``task_success`` 在结果里**恒为 None**；任务成功由 Task 4 的独立评测器判定。

五层语义（D3-1，固定）：

===========  ============================================================  ==============
层           检查什么                                                      必需性来源
===========  ============================================================  ==============
B0 静态      入口存在 / 普通文件 / 非符号链接 / 不越出 workdir / 体积下限 /  恒必需
             ``ast.parse``（.py）或 YAML 解析（.yaml）
B1 真实      按 ``task.yaml.entrypoint.kind`` 真加载候选（``python_script``  ``birth_contract.import_required``
import       → ``runpy.run_path``；``module`` → ``importlib``）；文档包不适用
B2 构造      调契约的构造入口（``build_scene`` / ``build_env``），对象非 None   ``construction_required``
B3 契约      契约结构检查（动作维度/观测键/可调用性；文档包查四件产物）       ``contract_required``
B4 最小步进  一次真实 reset（若契约要求）+ 一次 step：必须推进且全 finite    ``minimal_step_required``
===========  ============================================================  ==============

隔离纪律（T3.1 不变量 2 / D3-2 / D3-4 / D3-5 / D3-6）：

- **每层一个独立子进程**（``python -I -c``），层间不共享解释器状态；候选调用
  ``os._exit()`` 只杀子进程，``sys.modules`` 污染不跨层；
- ``preexec_fn`` 设 ``RLIMIT_CPU`` / ``RLIMIT_AS``；``subprocess`` 超时另设上限；
  两个上限哪个先到就按 ``timeout`` / ``memory`` 归类；
- 路径 jail：入口 ``resolve()`` 后必须仍在 ``workdir`` 内，符号链接入口与软链
  逃逸一律 ``path_escape``（D3-5，``is_relative_to`` 而非字符串前缀）；
- 断网**分级**：容器路径 ``--network none`` 记 ``enforced``；宿主路径无特权断网
  能力，记 ``audited_only`` 并附 AST 静态扫描命中（D3-6，不假装断网）。

失败 ``reason`` 取值固定（D3-7）：``missing / too_small / symlink / ast_error /
compile_error / import_error / construction_error / contract_error / step_error /
non_finite / timeout / memory / path_escape / blocked_infra``。
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from orchestrator.benchmark import contracts as C
from orchestrator.benchmark import task_spec as ts
from orchestrator.benchmark.runtime_fingerprint import sanitized_env
from orchestrator.common.fsutil import atomic_write_text, ensure_private_dir

__all__ = [
    "BIRTH_SCHEMA", "LEVELS", "LEVEL_STATUSES", "FAILURE_REASONS",
    "CONTRACT_BY_TASK", "BirthError", "BirthSpec", "Limits", "LevelResult",
    "BirthResult", "BirthValidator", "RUNNER_MARKER", "parse_runner_output",
]

BIRTH_SCHEMA = "simpraxis.functional_birth.v1"
BIRTH_NAME = "functional_birth.json"
BIRTH_SHA_NAME = "birth.sha256"
LEVELS = ("B0", "B1", "B2", "B3", "B4")
LEVEL_STATUSES = ("PASS", "FAIL", "NOT_APPLICABLE", "BLOCKED_INFRA")
FAILURE_REASONS = ("missing", "too_small", "symlink", "ast_error",
                   "compile_error", "import_error", "construction_error",
                   "contract_error", "step_error", "non_finite", "timeout",
                   "memory", "path_escape", "blocked_infra")
RUNNER_MARKER = "__SIMPRAXIS_BIRTH__"
#: D3-4：stdout/stderr 各截断 64 KiB 落盘（meta 里仍记全量字节数）
MAX_CAPTURE_BYTES = 64 * 1024

#: 任务 → 契约名（Task 4/6 可直接复用；未知任务必须显式给 contract）
CONTRACT_BY_TASK = {
    "mujoco_contact_push_v1": "mujoco_push",
    "libero_env_adapter_v1": "libero_env_adapter",
    "icy_ground_dr_v1": "research_doc",
}

#: 每层的「兜底 reason」（子进程没吐出标记行时用；仍是固定枚举，不是自由文本）
_GENERIC_REASON = {
    "B0": "compile_error",
    "B1": "import_error",
    "B2": "construction_error",
    "B3": "contract_error",
    "B4": "step_error",
}

#: 断网审计的 AST 扫描目标（D3-6 的 audited 部分）
_NETWORK_MODULES = ("socket", "requests", "urllib", "urllib3", "http",
                    "ftplib", "telnetlib", "aiohttp", "httpx")


class BirthError(RuntimeError):
    """出生验证自身不可继续（配置错误 / 输出目录不合规）。

    注意：**候选**的问题一律落成 FAIL/BLOCKED_INFRA 的结果件，不抛本异常。
    """


# --------------------------------------------------------------------------
# 候选子进程内的运行器（单文件、无本地 import，避免被候选目录劫持）
# --------------------------------------------------------------------------

_RUNNER_SRC = r'''
import ast
import json
import math
import sys
import traceback

MARKER = "__SIMPRAXIS_BIRTH__"


def emit(status, reason, detail="", data=None):
    sys.stdout.write(MARKER + json.dumps(
        {"status": status, "reason": reason, "detail": str(detail)[:4000],
         "data": data or {}}, sort_keys=True, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    sys.exit(0)


cfg = json.loads(open(sys.argv[1], encoding="utf-8").read())
LEVEL = cfg["level"]
WORKDIR = cfg["workdir"]
ENTRY_KIND = cfg["entry"]["kind"]
ENTRY_PATH = cfg["entry"]["path"]
sys.path.insert(0, cfg["repo_root"])
from orchestrator.benchmark import contracts as C  # noqa: E402


def _fail(reason, detail):
    emit("FAIL", reason, detail)


def _jail(rel):
    from pathlib import Path
    wd = Path(WORKDIR).resolve(strict=True)
    p = wd / rel
    if p.is_symlink():
        target = p.resolve(strict=False)
        if target != wd and wd not in target.parents:
            _fail("path_escape", "%s 是符号链接且逃出 workdir（-> %s）"
                  % (rel, target))
        _fail("symlink", "%s 是符号链接（出生验证拒绝链接入口）" % rel)
    if not p.exists():
        _fail("missing", "%s 不存在" % rel)
    rp = p.resolve(strict=True)
    if rp != wd and wd not in rp.parents:
        _fail("path_escape", "%s 解析到 %s，越过 workdir %s" % (rel, rp, wd))
    return wd, rp


def _load():
    wd, rp = _jail(ENTRY_PATH)
    sys.path.insert(0, str(wd))
    try:
        if ENTRY_KIND == "module":
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                "simpraxis_candidate", str(rp))
            if spec is None or spec.loader is None:
                raise ImportError("无法为 %s 构造 import spec" % rp)
            mod = importlib.util.module_from_spec(spec)
            sys.modules["simpraxis_candidate"] = mod
            spec.loader.exec_module(mod)
            return mod
        import runpy
        return runpy.run_path(str(rp), run_name="__simpraxis_candidate__")
    finally:
        if sys.path and sys.path[0] == str(wd):
            sys.path.pop(0)


def _load_or_fail():
    try:
        return _load()
    except SystemExit:
        raise
    except SyntaxError as exc:
        _fail("ast_error", "%s: %s" % (type(exc).__name__, exc))
    except ImportError as exc:
        _fail("import_error", "%s: %s" % (type(exc).__name__, exc))
    except MemoryError as exc:
        _fail("memory", "加载候选时内存超限（RLIMIT_AS）：%s" % (exc,))
    except BaseException as exc:
        _fail(cfg["generic_reason"],
              "%s: %s\n%s" % (type(exc).__name__, exc,
                              traceback.format_exc()[-4096:]))


def _contract():
    try:
        return C.contract_by_name(cfg["contract"])
    except BaseException as exc:
        emit("BLOCKED_INFRA", "blocked_infra", "契约不可用：%s" % exc)


def level_b0():
    wd, rp = _jail(ENTRY_PATH)
    size = rp.stat().st_size
    if size < int(cfg["min_bytes"]):
        _fail("too_small", "%s 只有 %d 字节（下限 %d）"
              % (ENTRY_PATH, size, int(cfg["min_bytes"])))
    if rp.suffix == ".py":
        try:
            ast.parse(rp.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            _fail("ast_error", "%s: %s" % (type(exc).__name__, exc))
    elif rp.suffix in (".yaml", ".yml"):
        try:
            import yaml
            doc = yaml.safe_load(rp.read_text(encoding="utf-8"))
        except BaseException as exc:
            _fail("compile_error", "YAML 解析失败：%s: %s"
                  % (type(exc).__name__, exc))
        if not isinstance(doc, (dict, list)):
            _fail("compile_error", "YAML 顶层必须是 mapping/sequence")
    emit("PASS", None, "", {"bytes": size, "suffix": rp.suffix,
                            "resolved": str(rp)})


def level_b1():
    if ENTRY_KIND == "document_bundle":
        emit("NOT_APPLICABLE", None, "document_bundle 无 import 语义")
    mod = _load_or_fail()
    contract = _contract()
    data = {}
    if contract.builder:
        data["builder_present"] = C.module_get(mod, contract.builder) is not None
    emit("PASS", None, "", data)


def level_b2():
    if ENTRY_KIND == "document_bundle":
        emit("NOT_APPLICABLE", None, "document_bundle 无构造语义")
    contract = _contract()
    mod = _load_or_fail()
    try:
        obj = contract.build(mod)
    except SystemExit:
        raise
    except MemoryError as exc:
        _fail("memory", "构造时内存超限（RLIMIT_AS）：%s" % (exc,))
    except BaseException as exc:
        _fail("construction_error", "%s: %s" % (type(exc).__name__, exc))
    keys = getattr(obj, "observation_keys", None)
    try:
        keys = list(keys) if keys is not None else None
    except BaseException:
        keys = repr(keys)
    emit("PASS", None, "", {"object_type": type(obj).__name__,
                            "action_dim": getattr(obj, "action_dim", None),
                            "observation_keys": keys})


def level_b3():
    contract = _contract()
    obj = None
    if contract.needs_object:
        mod = _load_or_fail()
        try:
            obj = contract.build(mod)
        except SystemExit:
            raise
        except BaseException as exc:
            _fail("construction_error", "%s: %s" % (type(exc).__name__, exc))
    try:
        violations = contract.check(obj, cfg.get("ctx") or {})
    except SystemExit:
        raise
    except BaseException as exc:
        _fail("contract_error", "契约检查抛异常：%s: %s"
              % (type(exc).__name__, exc))
    if violations:
        _fail("contract_error",
              "; ".join("%s: %s" % (v.code, v.detail) for v in violations))
    emit("PASS", None, "", {"violations": 0})


def level_b4():
    if ENTRY_KIND == "document_bundle":
        emit("NOT_APPLICABLE", None, "document_bundle 无步进语义")
    contract = _contract()
    ctx = cfg.get("ctx") or {}
    mod = _load_or_fail()
    try:
        obj = contract.build(mod)
    except SystemExit:
        raise
    except MemoryError as exc:
        _fail("memory", "构造时内存超限（RLIMIT_AS）：%s" % (exc,))
    except BaseException as exc:
        _fail("construction_error", "%s: %s" % (type(exc).__name__, exc))
    if contract.reset_required:
        try:
            contract.reset(obj, ctx)
        except SystemExit:
            raise
        except MemoryError as exc:
            _fail("memory", "reset 时内存超限（RLIMIT_AS）：%s" % (exc,))
        except BaseException as exc:
            _fail("step_error", "reset 失败：%s: %s"
                  % (type(exc).__name__, exc))
    try:
        action = contract.action(obj, ctx)
    except SystemExit:
        raise
    except BaseException as exc:
        _fail("step_error", "最小动作构造失败：%s: %s"
              % (type(exc).__name__, exc))
    try:
        result = contract.step(obj, action, ctx)
    except SystemExit:
        raise
    except MemoryError as exc:
        _fail("memory", "step 时内存超限（RLIMIT_AS）：%s" % (exc,))
    except BaseException as exc:
        _fail("step_error", "step 失败：%s: %s"
              % (type(exc).__name__, exc))
    numbers = C.flatten_numbers(result)
    if not numbers:
        _fail("step_error", "step 未返回任何数值状态：%r" % (result,))
    bad = sorted(key for key, value in numbers.items() if not math.isfinite(value))
    if bad:
        _fail("non_finite", "步进后存在非有限值：%s" % (bad[:8],))
    if not any(value != 0.0 for value in numbers.values()):
        _fail("step_error", "步进后状态毫无变化（未推进）")
    sample = dict(sorted(numbers.items())[:8])
    emit("PASS", None, "", {"advanced": True, "n_values": len(numbers),
                            "sample": sample})


LEVELS_IMPL = {"B0": level_b0, "B1": level_b1, "B2": level_b2,
               "B3": level_b3, "B4": level_b4}

if __name__ == "__main__":
    fn = LEVELS_IMPL.get(LEVEL)
    if fn is None:
        emit("BLOCKED_INFRA", "blocked_infra", "未知层 %r" % (LEVEL,))
    try:
        fn()
    except SystemExit:
        raise
    except BaseException as exc:
        emit("BLOCKED_INFRA", "blocked_infra",
             "运行器异常：%s: %s\n%s" % (type(exc).__name__, exc,
                                        traceback.format_exc()[-2048:]))
'''


# --------------------------------------------------------------------------
# 描述与结果
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class BirthSpec:
    """出生验证所需的**最小**任务视图（不依赖 registry，测试可直接构造）。"""

    task_id: str
    entry_kind: str
    entry_path: str
    birth_contract: Mapping[str, bool]
    contract: str
    outputs: "tuple[str, ...]" = ()
    min_bytes: int = 16

    @classmethod
    def from_task_spec(cls, spec: ts.TaskSpec) -> "BirthSpec":
        try:
            contract = CONTRACT_BY_TASK[spec.task_id]
        except KeyError:
            raise BirthError(
                f"{spec.task_id} 没有登记契约；请在 CONTRACT_BY_TASK 显式登记"
            ) from None
        entry = dict(spec.entrypoint)
        return cls(
            task_id=spec.task_id,
            entry_kind=str(entry.get("kind")),
            entry_path=str(entry.get("path")),
            birth_contract=dict(spec.birth_contract),
            contract=contract,
            outputs=tuple(spec.outputs),
        )

    def required_levels(self) -> "dict[str, bool]":
        bc = dict(self.birth_contract)
        return {
            "B0": True,
            "B1": bool(bc.get("import_required")),
            "B2": bool(bc.get("construction_required")),
            "B3": bool(bc.get("contract_required")),
            "B4": bool(bc.get("minimal_step_required")),
        }

    def to_json(self) -> dict:
        return {
            "task_id": self.task_id,
            "entry": {"kind": self.entry_kind, "path": self.entry_path},
            "birth_contract": dict(self.birth_contract),
            "contract": self.contract,
            "outputs": list(self.outputs),
            "min_bytes": self.min_bytes,
            "required": self.required_levels(),
        }


@dataclass(frozen=True)
class Limits:
    """子进程资源上限（D3-4）；改值必须在 Complete 登记（阈值属判据）。"""

    timeout_s: float = 120.0
    cpu_s: int = 180
    mem_bytes: int = 4 * 1024 ** 3

    def to_json(self) -> dict:
        return {"timeout_s": self.timeout_s, "cpu_s": self.cpu_s,
                "mem_bytes": self.mem_bytes}


@dataclass
class LevelResult:
    level: str
    status: str
    reason: "str | None" = None
    detail: str = ""
    duration_s: float = 0.0
    exit_code: "int | None" = None
    pid: "int | None" = None
    data: dict = field(default_factory=dict)
    stdout_bytes: int = 0
    stderr_bytes: int = 0

    def __post_init__(self) -> None:
        if self.status not in LEVEL_STATUSES:
            raise BirthError(f"{self.level}: 非法状态 {self.status!r}")
        if self.reason is not None and self.reason not in FAILURE_REASONS:
            raise BirthError(f"{self.level}: 非法 reason {self.reason!r}")

    def to_json(self) -> dict:
        return {
            "status": self.status,
            "reason": self.reason,
            "detail": self.detail,
            "duration_s": round(self.duration_s, 6),
            "exit_code": self.exit_code,
            "pid": self.pid,
            "data": dict(self.data),
            "stdout_bytes": self.stdout_bytes,
            "stderr_bytes": self.stderr_bytes,
            "evidence": {
                "stdout": f"levels/{self.level}/stdout.txt",
                "stderr": f"levels/{self.level}/stderr.txt",
                "meta": f"levels/{self.level}/meta.json",
            },
        }


@dataclass
class BirthResult:
    spec: BirthSpec
    levels: "dict[str, LevelResult]"
    birth_status: str
    problems: "list[str]"
    limits: Limits
    network_audit: dict
    workdir: str
    ts_iso: str
    schema: str = BIRTH_SCHEMA
    task_success: "None" = None

    def to_json(self) -> dict:
        return {
            "schema": self.schema,
            "task_id": self.spec.task_id,
            "task_success": None,
            "birth_status": self.birth_status,
            "entry": {"kind": self.spec.entry_kind,
                      "path": self.spec.entry_path},
            "contract": C.contract_by_name(self.spec.contract).describe(),
            "required": self.spec.required_levels(),
            "levels": {level: self.levels[level].to_json() for level in LEVELS},
            "problems": list(self.problems),
            "limits": self.limits.to_json(),
            "network_audit": dict(self.network_audit),
            "workdir": self.workdir,
            "ts_iso": self.ts_iso,
        }

    def canonical_json(self) -> str:
        return ts.canonical_json(self.to_json())


# --------------------------------------------------------------------------
# 运行器输出解析
# --------------------------------------------------------------------------

def parse_runner_output(stdout: str) -> "dict | None":
    """取最后一行标记 JSON；没有标记行即 ``None``（子进程自杀/崩溃）。"""
    for line in reversed(stdout.splitlines()):
        if line.startswith(RUNNER_MARKER):
            try:
                return json.loads(line[len(RUNNER_MARKER):])
            except json.JSONDecodeError:
                return None
    return None


def _scan_network(path: Path) -> "list[dict]":
    """AST 扫候选入口的 import（D3-6 audited 部分；解析失败返回空并由 B0 报）。"""
    hits: "list[dict]" = []
    import ast
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:                                       # noqa: BLE001
        return hits
    for node in ast.walk(tree):
        names: "list[str]" = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        for name in names:
            root = name.split(".")[0]
            if root in _NETWORK_MODULES:
                hits.append({"module": name, "lineno": getattr(node, "lineno", 0)})
    return hits


# --------------------------------------------------------------------------
# 验证器
# --------------------------------------------------------------------------

class BirthValidator:
    """在一个 ``workdir`` 上跑完 B0→B4，落盘五层原始输出与聚合结果。"""

    def __init__(self, spec: BirthSpec, *, workdir: "str | Path",
                 python: "str | None" = None,
                 limits: "Limits | None" = None,
                 network_mode: str = "audited_only",
                 ctx: "Mapping[str, Any] | None" = None) -> None:
        self.spec = spec
        self.workdir = Path(workdir).resolve()
        if not self.workdir.is_dir():
            raise BirthError(f"workdir 不存在或不是目录：{self.workdir}")
        self.python = python or sys.executable
        self.limits = limits or Limits()
        if network_mode not in ("none", "audited_only"):
            raise BirthError(f"network_mode 非法：{network_mode!r}")
        self.network_mode = network_mode
        self.repo_root = Path(__file__).resolve().parents[2]
        self.ctx = dict(ctx or {})
        self.ctx.setdefault("workdir", str(self.workdir))

    # -- 子进程 -------------------------------------------------------------
    def _preexec(self):                                     # pragma: no cover - 子进程内生效
        import resource
        resource.setrlimit(resource.RLIMIT_CPU,
                           (self.limits.cpu_s, self.limits.cpu_s + 1))
        resource.setrlimit(resource.RLIMIT_AS,
                           (self.limits.mem_bytes, self.limits.mem_bytes))

    def _payload(self, level: str) -> dict:
        return {
            "level": level,
            "workdir": str(self.workdir),
            "entry": {"kind": self.spec.entry_kind,
                      "path": self.spec.entry_path},
            "contract": self.spec.contract,
            "repo_root": str(self.repo_root),
            "min_bytes": self.spec.min_bytes,
            "generic_reason": _GENERIC_REASON[level],
            "ctx": dict(self.ctx),
        }

    def _not_applicable(self, level: str, level_dir: Path) -> LevelResult:
        """``birth_contract`` 未要求的层：直接记 NOT_APPLICABLE，不起子进程。

        D3-3：四个布尔决定哪些层必需；不适用层**不得算 PASS**。证据三件照写
        （空 stdout/stderr + meta.json），保证五层证据结构一致（T3.7）。
        """
        result = LevelResult(
            level=level, status="NOT_APPLICABLE",
            detail="birth_contract 未要求该层（不适用层不计入 PASS）")
        atomic_write_text(level_dir / "stdout.txt", "", 0o600)
        atomic_write_text(level_dir / "stderr.txt", "", 0o600)
        atomic_write_text(level_dir / "meta.json",
                          json.dumps(result.to_json(), ensure_ascii=False,
                                     indent=2, sort_keys=True) + "\n", 0o600)
        return result

    def _run_restricted(self, level: str, level_dir: Path) -> LevelResult:
        if not self.spec.required_levels()[level]:
            return self._not_applicable(level, level_dir)
        payload_path = level_dir / "payload.json"
        atomic_write_text(payload_path,
                          json.dumps(self._payload(level), ensure_ascii=False,
                                     indent=2, sort_keys=True) + "\n", 0o600)
        started = time.monotonic()
        timed_out = False
        stdout = stderr = ""
        rc: "int | None" = None
        pid: "int | None" = None
        try:
            proc = subprocess.Popen(
                [self.python, "-I", "-c", _RUNNER_SRC, str(payload_path)],
                cwd=str(self.workdir), env=sanitized_env(),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                preexec_fn=self._preexec)
        except (OSError, subprocess.SubprocessError) as exc:
            # 计划 T3.9：setrlimit 不可用必须显式 BLOCKED_INFRA，不得静默跳过限制
            result = LevelResult(
                level=level, status="BLOCKED_INFRA", reason="blocked_infra",
                detail=f"受限子进程无法启动（setrlimit/preexec 失败）：{exc}")
            atomic_write_text(level_dir / "stdout.txt", "", 0o600)
            atomic_write_text(level_dir / "stderr.txt", "", 0o600)
            atomic_write_text(level_dir / "meta.json",
                              json.dumps(result.to_json(), ensure_ascii=False,
                                         indent=2, sort_keys=True) + "\n", 0o600)
            return result
        pid = proc.pid
        try:
            stdout, stderr = proc.communicate(timeout=self.limits.timeout_s)
            rc = proc.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            proc.kill()
            stdout, stderr = proc.communicate()
            rc = proc.returncode
        duration = time.monotonic() - started

        (level_dir / "stdout.txt").write_text(
            _truncate_capture(stdout), encoding="utf-8")
        (level_dir / "stderr.txt").write_text(
            _truncate_capture(stderr), encoding="utf-8")

        parsed = parse_runner_output(stdout)
        if timed_out:
            status, reason, detail, data = "FAIL", "timeout", (
                f"超过 {self.limits.timeout_s}s 上限"), {}
        elif parsed is not None:
            status = parsed.get("status")
            reason = parsed.get("reason")
            detail = parsed.get("detail") or ""
            data = parsed.get("data") or {}
        else:
            status = "FAIL"
            reason = _GENERIC_REASON[level]
            if rc in (-9, -11):
                reason = "memory"
            elif rc in (-24, -25):
                reason = "timeout"
            detail = (f"子进程未产出标记行（rc={rc}）："
                      f"{stderr.strip().splitlines()[-1] if stderr.strip() else '无 stderr'}")
            data = {}

        result = LevelResult(
            level=level, status=status, reason=reason, detail=detail,
            duration_s=duration, exit_code=rc, pid=pid, data=dict(data),
            stdout_bytes=len(stdout.encode("utf-8")),
            stderr_bytes=len(stderr.encode("utf-8")))
        atomic_write_text(level_dir / "meta.json",
                          json.dumps(result.to_json(), ensure_ascii=False,
                                     indent=2, sort_keys=True) + "\n", 0o600)
        return result

    # -- 聚合 ---------------------------------------------------------------
    def _aggregate(self, levels: "dict[str, LevelResult]") -> "tuple[str, list[str]]":
        required = self.spec.required_levels()
        problems: "list[str]" = []
        failing = [L for L in LEVELS if required[L] and levels[L].status == "FAIL"]
        not_applicable = [L for L in LEVELS
                          if required[L] and levels[L].status == "NOT_APPLICABLE"]
        infra = [L for L in LEVELS if levels[L].status == "BLOCKED_INFRA"]
        for level in LEVELS:
            if not required[level] and levels[level].status == "FAIL":
                problems.append(
                    f"{level} 非必需层却 FAIL（{levels[level].reason}）——不影响结论，需人看")
        if failing:
            return "NOT_BORN", problems + [f"必需层失败：{failing}"]
        if not_applicable:
            return "NOT_BORN", problems + [
                f"必需层被标 NOT_APPLICABLE（契约与 birth_contract 自相矛盾）：{not_applicable}"]
        if infra:
            return "BLOCKED_INFRA", problems + [f"基础设施阻塞层：{infra}"]
        return "BORN", problems

    # -- 入口 ---------------------------------------------------------------
    def validate(self, out_dir: "str | Path | None" = None) -> BirthResult:
        target = Path(out_dir) if out_dir is not None else (
            Path(tempfile.mkdtemp(prefix="simpraxis-birth-")))
        ensure_private_dir(target)
        levels_dir = ensure_private_dir(target / "levels")
        levels: "dict[str, LevelResult]" = {}
        for level in LEVELS:
            level_dir = ensure_private_dir(levels_dir / level)
            levels[level] = self._run_restricted(level, level_dir)

        entry = self.workdir / self.spec.entry_path
        hits = _scan_network(entry) if entry.is_file() else []
        audit = {
            "mode": "enforced" if self.network_mode == "none" else "audited_only",
            "network_mode": self.network_mode,
            "static_import_hits": hits,
            "note": ("容器 --network none：断网被强制执行"
                     if self.network_mode == "none"
                     else "宿主无特权断网：仅做 AST 静态审计，如实标注不假装断网"),
        }
        status, problems = self._aggregate(levels)
        result = BirthResult(
            spec=self.spec, levels=levels, birth_status=status,
            problems=problems, limits=self.limits, network_audit=audit,
            workdir=str(self.workdir), ts_iso=_now())
        return result


def _truncate_capture(text: str) -> str:
    """按 UTF-8 字节截到 64 KiB（末尾留一行截断标记）；全量字节数记在 meta。"""
    raw = text.encode("utf-8")
    if len(raw) <= MAX_CAPTURE_BYTES:
        return text
    head = raw[:MAX_CAPTURE_BYTES].decode("utf-8", errors="replace")
    return head + f"\n[simpraxis] 输出已截断：全量 {len(raw)} 字节\n"


def _now() -> str:
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def write_birth(out_dir: "str | Path", result: BirthResult) -> dict:
    """原子写 ``functional_birth.json`` + ``birth.sha256``（0600）。"""
    directory = ensure_private_dir(Path(out_dir))
    body = json.dumps(result.to_json(), ensure_ascii=False, indent=2,
                      sort_keys=True) + "\n"
    path = atomic_write_text(directory / BIRTH_NAME, body, 0o600)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    sha_path = atomic_write_text(directory / BIRTH_SHA_NAME,
                                 f"{digest}  {BIRTH_NAME}\n", 0o600)
    return {"birth_path": str(path), "sha256_path": str(sha_path),
            "birth_sha256": digest, "birth_status": result.birth_status}


def load_birth(path: "str | Path") -> dict:
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    if doc.get("schema") != BIRTH_SCHEMA:
        raise BirthError(f"{path}: schema={doc.get('schema')!r} 非 {BIRTH_SCHEMA}")
    if doc.get("task_success") is not None:
        raise BirthError(f"{path}: task_success 必须恒为 None（Birth ≠ Task Success）")
    if doc.get("birth_status") not in ("BORN", "NOT_BORN", "BLOCKED_INFRA"):
        raise BirthError(f"{path}: 非法 birth_status {doc.get('birth_status')!r}")
    for level in LEVELS:
        entry = doc.get("levels", {}).get(level)
        if not isinstance(entry, dict):
            raise BirthError(f"{path}: 缺层 {level}")
        if entry.get("status") not in LEVEL_STATUSES:
            raise BirthError(f"{path}: {level} 非法状态 {entry.get('status')!r}")
    return doc
