"""Phase 2.8.1 T4：预算单点执行 + 三源遥测 + 泄漏检测（计划 §8.5.5 / §8.5.8 / §8.3.1）。

三件事，一个模块，因为它们是同一件事的三个面：**「这次 Trial 到底花了多少」
只能有一个权威答案**。

1. :class:`TrialBudget` + :class:`CapGuardGateway` —— 调用帽的**代码面**执行者。
   先查账本、后发请求，第 11 次调用**不触网**。为什么读账本而不是内存计数器：
   内存计数器在进程崩溃/重启后归零，会放行第 11 次；账本是唯一计费事实。
2. :func:`observed` —— 把「账本 / 追踪 / 运行器证据」三个源摊开，逐字段标注
   **来源与权威级**。``claimed``（引擎自述）永远是最低一级，只用来对账。
3. :func:`leak_scan` —— §8.3.1 的文件系统泄漏通道检测器。本阶段**没有 OS 级
   沙箱**（候选在宿主进程/兄弟容器里跑，能读盘），所以「没查到泄漏」只能
   说成 **未检出**，不能写成「已证明无泄漏」——该措辞限制由调用方（报告与
   P281-7 的汇报）逐字承担，本模块的 :data:`LEAK_SCAN_LIMITATION` 给出原文。
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from orchestrator.common.fsutil import atomic_write_text, ensure_private_dir

from . import constants

__all__ = (
    "CapGuardGateway",
    "LEAK_SCAN_LIMITATION",
    "LeakScanError",
    "TrialBudget",
    "TrialCapReached",
    "ledger_call_rows",
    "leak_scan",
    "observed",
    "read_summary",
    "summarize_observed",
    "write_summary",
)

#: 报告与汇报**必须逐字**带上的限制声明（§8.3.1 末段）
LEAK_SCAN_LIMITATION = (
    "本阶段的隔离是**约定 + 检测**，不是 OS 级沙箱：候选进程与宿主同权限。"
    "泄漏扫描只能给出「未检出」，不构成「已证明无泄漏」。")

#: 追踪件里的控制行人标记（与 ``agentic/forensics.py`` 同字面量，不 import 内环包）
MODEL_ROUND_TOOL = "__model_round__"
SUMMARY_EVENT = "summary"

#: 数值字面量 token：前后都不得紧贴词字符或小数点（防止 ``0.0014`` 命中 ``0.001``）
_NUMBER_RE = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)(?![\w.])")
#: 单个文件文本扫描上限（超过就不做文本正则，只做哈希）
TEXT_SCAN_LIMIT = 2 * 1024 * 1024
#: 单文件哈希上限（与仓规「>10MB 不入」同量级）
HASH_LIMIT = 10 * 1024 * 1024


class TrialCapReached(RuntimeError):
    """本 Trial 的模型调用帽已用尽——**在发出请求之前**抛出（不记账、不触网）。"""

    def __init__(self, trial_id: str, cap: int, used: int) -> None:
        super().__init__(
            f"Trial {trial_id} 调用帽已满：已用 {used} / 帽 {cap}"
            "（本次调用在联网之前被拦，未记账）")
        self.trial_id = trial_id
        self.cap = cap
        self.used = used


class LeakScanError(RuntimeError):
    """泄漏扫描**自身**失败（根不可读等）——与「扫到泄漏」严格区分。"""


# ---------------------------------------------------------------------------
# 账本读取（阶段账的唯一口径）
# ---------------------------------------------------------------------------

def _ledger_path_of(ledger: Any) -> Path:
    """接受 ``QuotaLedger`` / 命名空间对象 / ``Path`` 三种形态。"""
    if isinstance(ledger, (str, Path)):
        return Path(ledger)
    ns = getattr(ledger, "ns", None)
    if ns is not None and hasattr(ns, "ledger_path_fn"):
        return Path(ns.ledger_path_fn())
    for name in ("ledger_path", "path"):
        fn = getattr(ledger, name, None)
        if fn is not None:
            return Path(fn() if callable(fn) else fn)
    raise TypeError(
        f"无法从 {type(ledger).__name__} 解析账本路径（需要 QuotaLedger 或 Path）")


def _read_rows(path: Path) -> "list[dict]":
    """只读账本行；文件不在场 = 空账（**不**顺手建目录/建文件）。"""
    if not path.is_file():
        return []
    rows: "list[dict]" = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            row = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def ledger_call_rows(ledger: Any, trial_id: str, *,
                     batch_id: "str | None" = None) -> "list[dict]":
    """本 Trial 的**实呼行**（``event=="call"`` 且 ``thread_id==trial_id``）。

    为什么按 ``thread_id`` 而不按 ``batch_id``：``thread_id`` 是 ``trial_id``
    的单点派生（``constants.trial_id``），而 ``batch_id`` 是批准凭据的键——
    两者在本阶段同值，但凭据键将来可能被合并成「一批多 Trial」，那时按
    ``batch_id`` 数就把别人的调用算进来了。

    ``batch_id``（**A7-10，2026-11-07**）：显式切窗＝「本 Trial **在本票下**的
    实呼行」。重跑批（P281-B1b-*）按同一 ``trial_id`` 重开票时，只按 ``thread_id``
    数会把**上一轮**的同名 Trial 调用算进本轮帽——2026-11-07 的历史事故正是
    如此：三个 agentic Trial 首轮各花满 10 呼，重跑时 ``used()==10 >= cap``
    直接 ``TrialCapReached``，**一次网都没发**（账本 0 行），而报告却把首轮的
    10 呼/111529 tokens 当作本轮观测值。切窗后这类「旧轮数据冒充本轮」不可能
    再发生——本轮的帽与本轮的观测都只看本票的行。
    """
    out: "list[dict]" = []
    for row in _read_rows(_ledger_path_of(ledger)):
        if row.get("event") != "call":
            continue
        if row.get("thread_id") != trial_id:
            continue
        if batch_id is not None and row.get("batch_id") != batch_id:
            continue
        seq = row.get("seq")
        if not isinstance(seq, int) or isinstance(seq, bool):
            continue                      # 非实呼行（理论上 event 已过滤掉）
        out.append(row)
    return out


# ---------------------------------------------------------------------------
# 预算：先查上限、后发请求
# ---------------------------------------------------------------------------

@dataclass
class TrialBudget:
    """一次 Trial 的调用帽执行者。**每一次判定都回账本取权威值**。"""

    cap: int
    ledger: Any
    trial_id: str
    #: A7-10：切窗到本票（``None`` = 旧口径，按 ``thread_id`` 数全历史）
    batch_id: "str | None" = None
    _checks: int = field(default=0, init=False, repr=False)
    _last_used: int = field(default=0, init=False, repr=False)

    def __post_init__(self) -> None:
        if type(self.cap) is not int or self.cap < 1:
            raise ValueError(f"TrialBudget.cap 必须是正整数：{self.cap!r}")
        if not isinstance(self.trial_id, str) or not self.trial_id:
            raise ValueError("TrialBudget.trial_id 必须是非空字符串")

    def used(self) -> int:
        """账本里本 Trial 的实呼行数（**权威值**，不读内存计数器）。

        ``batch_id`` 非空时切窗到本票（A7-10）：重跑批的帽只数本轮的调用。
        """
        self._checks += 1
        self._last_used = len(ledger_call_rows(
            self.ledger, self.trial_id, batch_id=self.batch_id))
        return self._last_used

    def before_call(self) -> None:
        """超帽即抛——本函数之前**没有任何**网络动作。"""
        used = self.used()
        if used >= self.cap:
            raise TrialCapReached(self.trial_id, self.cap, used)

    def after_call(self) -> int:
        """记账后的权威用量刷新（调用方据此判断是否触帽）。"""
        return self.used()

    def exhausted(self) -> bool:
        return self.used() >= self.cap


class CapGuardGateway:
    """把 :class:`TrialBudget` 套在网关外层的**透明代理**（不改 router）。

    只拦 ``call`` / ``call_visual`` 两个付费入口；其余属性原样转给内层网关，
    因此引擎代码对「网关」的其它用法（``model_id`` / ``config`` 等）零改动。
    """

    def __init__(self, inner: Any, budget: TrialBudget) -> None:
        self.inner = inner
        self.budget = budget

    #: 便于 logger/证据件识别真实类型（不是伪装成 ModelGateway）
    guard_of = "phase281.CapGuardGateway"

    def _guarded(self, fn_name: str, args: tuple, kwargs: dict) -> Any:
        self.budget.before_call()
        fn = getattr(self.inner, fn_name)
        try:
            return fn(*args, **kwargs)
        finally:
            # 失败（含供应侧错误）也已占 seq/记账，必须刷新——否则下一次
            # before_call 会用过期计数放行。
            self.budget.after_call()

    def call(self, *args: Any, **kwargs: Any) -> Any:
        return self._guarded("call", args, kwargs)

    def call_visual(self, *args: Any, **kwargs: Any) -> Any:
        return self._guarded("call_visual", args, kwargs)

    def __getattr__(self, name: str) -> Any:
        # __getattr__ 只在正常查找失败时调用 → 不会遮蔽上面的 call/call_visual
        return getattr(self.inner, name)

    def __repr__(self) -> str:  # pragma: no cover - 调试友好
        return f"CapGuardGateway({self.inner!r}, cap={self.budget.cap})"


# ---------------------------------------------------------------------------
# 三源遥测
# ---------------------------------------------------------------------------

def _sum_tokens(rows: Iterable[Mapping]) -> "tuple[int, int]":
    tin = tout = 0
    for row in rows:
        value = row.get("tokens_in")
        if isinstance(value, int) and not isinstance(value, bool):
            tin += value
        value = row.get("tokens_out")
        if isinstance(value, int) and not isinstance(value, bool):
            tout += value
    return tin, tout


def read_summary(trial_dir: "str | Path") -> "dict | None":
    """读 Trial 根 ``telemetry.jsonl`` 里 ``event=="summary"`` 的那一行。"""
    path = Path(trial_dir) / "telemetry.jsonl"
    if not path.is_file():
        return None
    found: "dict | None" = None
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and row.get("event") == SUMMARY_EVENT:
            found = row
    return found


def _trace_rows(trial_dir: Path) -> "list[dict]":
    """``trace/`` 下的全部 JSONL 行（harness 运行后从 workdir 拷来）。"""
    out: "list[dict]" = []
    base = trial_dir / "trace"
    if not base.is_dir():
        return out
    for path in sorted(base.rglob("*.jsonl")):
        if path.is_symlink() or not path.is_file():
            continue
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not raw.strip():
                continue
            try:
                row = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                out.append(row)
    return out


def _trace_counts(rows: Iterable[Mapping]) -> "tuple[int, int]":
    """``(tool_steps, coding_rounds)``：控制行（``__xxx__``）不算工具步。"""
    steps = rounds = 0
    for row in rows:
        tool = str(row.get("tool") or "")
        if tool == MODEL_ROUND_TOOL:
            rounds += 1
        elif tool and not tool.startswith("__"):
            steps += 1
    return steps, rounds


def observed(trial_dir: "str | Path", ledger: Any, *,
             trial_id: "str | None" = None,
             batch_id: "str | None" = None) -> dict:
    """三源合成的「实际发生了什么」（计划 §8.5.8）。

    返回键：``calls`` / ``seqs`` / ``stages`` / ``tokens_in`` / ``tokens_out``
    / ``tool_steps`` / ``coding_rounds`` / ``first_birth_round``，外加
    ``sources``（逐字段来源）与 ``claimed``（**不参与判定**的自述快照）。

    权威级（高→低）：账本 > 追踪 > 运行器证据（``telemetry.jsonl``）> 自述。
    ``tool_steps`` / ``coding_rounds`` 优先取追踪重算值；追踪不在场（legacy
    臂没有追踪件）才退回运行器总结行。``first_birth_round`` **只有**运行器
    知道（它是引擎内部语义），故一律取总结行。
    """
    base = Path(trial_dir)
    summary = read_summary(base)
    if trial_id is None:
        trial_id = None
        trial_json = base / "trial.json"
        if trial_json.is_file():
            try:
                doc = json.loads(trial_json.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                doc = {}
            if isinstance(doc, dict):
                trial_id = doc.get("trial_id")
        if not trial_id and summary is not None:
            trial_id = summary.get("trial_id")
    if not isinstance(trial_id, str) or not trial_id:
        raise ValueError(f"无法确定 trial_id（{base}）：需显式传参")

    # A7-10：切窗到本票 —— 观测值必须是**本轮**的实呼（重跑批不能把旧轮
    # 10 呼/111529 tokens 冒充成「这一次观测到了什么」）。
    rows = ledger_call_rows(ledger, trial_id, batch_id=batch_id)
    tokens_in, tokens_out = _sum_tokens(rows)
    stages = [str(row.get("stage") or "") for row in rows]

    trace = _trace_rows(base)
    tool_steps = coding_rounds = None
    sources = {"calls": "ledger", "seqs": "ledger", "stages": "ledger",
               "tokens_in": "ledger", "tokens_out": "ledger"}
    if trace:
        tool_steps, coding_rounds = _trace_counts(trace)
        sources["tool_steps"] = "trace"
        sources["coding_rounds"] = "trace"
    else:
        # 追踪不在场：退回运行器总结行（权威级低一级，来源如实标注）
        if summary is not None:
            tool_steps = summary.get("tool_steps")
            coding_rounds = summary.get("coding_rounds")
            sources["tool_steps"] = "runner_telemetry"
            sources["coding_rounds"] = "runner_telemetry"
        else:
            sources["tool_steps"] = "absent"
            sources["coding_rounds"] = "absent"

    first_birth_round = None
    if summary is not None:
        first_birth_round = summary.get("first_birth_round")
        sources["first_birth_round"] = "runner_telemetry"
    else:
        sources["first_birth_round"] = "absent"

    claimed = None
    manifest = base / "candidate_trial_manifest.json"
    if manifest.is_file():
        try:
            doc = json.loads(manifest.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            doc = None
        if isinstance(doc, dict):
            claimed = dict(doc.get("claimed") or {})

    return {
        "trial_id": trial_id,
        "calls": len(rows),
        "seqs": sorted(int(row["seq"]) for row in rows),
        "stages": stages,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "tool_steps": tool_steps,
        "coding_rounds": coding_rounds,
        "first_birth_round": first_birth_round,
        "sources": sources,
        "claimed": claimed,
    }


def summarize_observed(obs: Mapping) -> dict:
    """把 :func:`observed` 压成 ``event=="summary"`` 行（运行器遥测的写入口）。"""
    return {
        "event": SUMMARY_EVENT,
        "trial_id": obs["trial_id"],
        "calls": obs["calls"],
        "coding_rounds": obs.get("coding_rounds"),
        "tool_steps": obs.get("tool_steps"),
        "first_birth_round": obs.get("first_birth_round"),
        "tokens_in": obs.get("tokens_in"),
        "tokens_out": obs.get("tokens_out"),
    }


def write_summary(trial_dir: "str | Path", obs: Mapping) -> Path:
    """原子写 ``<trial_dir>/telemetry.jsonl``（0600，单行，覆盖写）。"""
    base = Path(trial_dir)
    ensure_private_dir(base)
    line = summarize_observed(obs)
    path = base / "telemetry.jsonl"
    atomic_write_text(path, json.dumps(line, ensure_ascii=False,
                                       sort_keys=True) + "\n", mode=0o600)
    return path


# ---------------------------------------------------------------------------
# 泄漏检测（§8.3.1 三条通道）
# ---------------------------------------------------------------------------

def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _iter_files(base: Path) -> "Iterable[Path]":
    """常规文件迭代器：**不跟符号链接**（软链本身就是可达性逃逸的载体）。"""
    if not base.is_dir():
        return
    for path in sorted(base.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        yield path


def _scan_roots(root: "str | Path", kb_root: "str | Path | None"
                ) -> "tuple[list[Path], list[str]]":
    """返回 ``(受保护根目录, 绝对路径字面针)``。缺件目录跳过但不静默——由
    调用方的报告登记「扫描了哪些根」。"""
    r = Path(root)
    dirs = [r / "private" / "criteria", r / "private" / "runtime",
            r / "references"]
    needles = [str(r / "private"), str(r / "references"),
               "/private/criteria", "/private/runtime"]
    if kb_root is not None:
        kb = Path(kb_root)
        dirs += [kb / "solutions" / "track1_tools" / "03_libero",
                 kb / "solutions" / "track1_tools" / "04_libero_vla"]
        needles += [str(kb / "solutions"), "/solutions/track1_tools/"]
    return [d for d in dirs if d.is_dir()], [n for n in needles if n]


def _threshold_values(criteria: Mapping) -> "list[float]":
    out: "list[float]" = []
    thresholds = criteria.get("thresholds") or {}
    if not isinstance(thresholds, Mapping):
        return out
    for entry in thresholds.values():
        value = entry.get("value") if isinstance(entry, Mapping) else None
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            out.append(float(value))
    return out


def leak_scan(trial_dir: "str | Path", *, task_id: "str | None" = None,
              root: "str | Path | None" = None,
              kb_root: "str | Path | None" = None,
              criteria: "Mapping | None" = None,
              scan_dirs: "tuple[str, ...]" = ("delivered", "workdir"),
              extra_needles: "Iterable[str]" = ()) -> "list[dict]":
    """三条通道的泄漏检测器；**干净即返回 ``[]``**（命中即 STOP-11）。

    通道：

    ⓐ ``absolute_path`` —— 候选可见面出现私有根/参考根/KB 方案根的**绝对路径
       字面**（说明它拿到了「去哪读」的坐标）；
    ⓑ ``sha256_match`` —— 候选产出（``delivered/`` + ``workdir/``）里存在与
       **判据件 / 参考件 / KB 方案脚本**逐字节相同的文件（说明它读到了内容）；
    ⓒ ``threshold_constant`` —— 候选可见面出现判据阈值的数值常量（说明它
       猜到了判据，或直接抄了）。

    **为什么 ``input/`` 不在 ⓑ 的扫描面**：``input/`` 是运行器自己 staging 的
    只读面，里面的技能件本来就来自私有根，逐字节相同是**正常**的——把它算成
    泄漏等于让检测器对每个 Trial 恒红。ⓑ 只看候选**产出**。

    **为什么 ``private/skills_stage`` 不在 ⓑ 的对照面**：同上，技能件是授权输入，
    不是判据。对照面只留判据件、参考件与方案脚本。

    返回值元素的键：``kind`` / ``path`` / ``detail``。
    """
    base = Path(trial_dir)
    if not base.is_dir():
        raise LeakScanError(f"Trial 目录不在场：{base}")

    dirs, needles = _scan_roots(root, kb_root) if root is not None else ([], [])
    needles = list(needles) + [n for n in extra_needles if n]

    # 受保护内容的 sha256 集（漏掉大文件是有意的：判据/脚本都在 KB 级，不会 >10MB）
    protected: "dict[str, str]" = {}
    for directory in dirs:
        for path in _iter_files(directory):
            try:
                if path.stat().st_size > HASH_LIMIT:
                    continue
                protected.setdefault(_sha256(path), str(path))
            except OSError:
                continue

    values: "list[float]" = []
    if criteria is not None:
        values = _threshold_values(criteria)

    hits: "list[dict]" = []
    for name in scan_dirs:
        area = base / name
        for path in _iter_files(area):
            rel = path.relative_to(base).as_posix()
            try:
                size = path.stat().st_size
            except OSError:
                continue
            # ⓑ 内容哈希交集
            if protected and size <= HASH_LIMIT:
                try:
                    digest = _sha256(path)
                except OSError:
                    digest = None
                if digest is not None and digest in protected:
                    hits.append({
                        "kind": "sha256_match", "path": rel,
                        "detail": f"与受保护件逐字节相同：{protected[digest]}",
                    })
            # ⓐ / ⓒ 文本面
            if size > TEXT_SCAN_LIMIT:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for needle in needles:
                if needle and needle in text:
                    hits.append({"kind": "absolute_path", "path": rel,
                                 "detail": f"出现私有/参考路径字面：{needle}"})
                    break
            if values:
                for token in _NUMBER_RE.findall(text):
                    try:
                        number = float(token)
                    except ValueError:  # pragma: no cover - 正则已保证可解析
                        continue
                    if any(number == value for value in values):
                        hits.append({
                            "kind": "threshold_constant", "path": rel,
                            "detail": f"出现判据阈值常量：{token}",
                        })
                        break
    return hits
