"""Phase 2.8.1 T2：出生隔离 v2 的**子进程入口**。

职责边界（与计划 §6.3.1 逐条对应）：

- 只读 ``cfg.json``（``level`` / ``levels`` / ``workdir`` / ``entry`` /
  ``contract`` / ``limits`` / ``ctx`` / ``min_bytes``）——**没有**一次性挑战值，
  也**没有**结果文件路径；这两样在 v2 里根本不存在（父进程不生成，见
  ``birth_protocol`` 的模块注释）；
- 导入候选、跑层探针、把**结构性证据事件**（``imported`` / ``constructed`` /
  ``contract`` / ``step`` / ``advance``）与**诊断事件**（``*_error``）逐行写到
  ``EVENT_FD``；
- **不判层结果**：层结果由父进程按内核可观测量（退出码/信号/rusage/墙钟/
  文件 stat/事件计数）自己判定。本进程自报的 ``level`` 事件只是一条**弱证据**，
  父进程与自己的事实核对，不一致就记 ``event_conflict``；
- 退出码语义：**恒 0**（层失败不是崩溃）——判定完全归父进程。落到非 0 只可能是
  本进程自己崩了（导入失败/解释器缺失），父进程按 ``blocked_infra`` 或对应层的
  兜底 reason 处理。

为什么本文件的源码里**不出现** AC-P281-2-15 列出的那四类字面量（本文档也不写出来，
否则自己就撞扫描面）：v2 的子进程一侧确实没有这些东西——协议字段名与探针实现在
``birth_protocol``，父进程的挑战值生成/下发/校验（v1 那套）在 v2 里整条被拆掉了。
扫描因此是**真的在验架构**，不是文字游戏。

与 v1 运行器（``orchestrator/benchmark/birth.py::_RUNNER_SRC``）的语义对齐：
每层用**独立进程**跑、层内步骤与 v1 逐步同义（B0 静态 → B1 导入 → B2 构造 →
B3 契约 → B4 reset+最小步进）；差别只在**输出通道**（写 fd 3 的事件流而不是
带挑战值的结果文件）与**判定归属**（父进程判，本进程只报事实）。
"""
from __future__ import annotations

import importlib.util
import json
import math
import os
import runpy
import sys
import time
import traceback
from pathlib import Path

from orchestrator.benchmark import contracts as C
from orchestrator.benchmark.phase281 import birth_protocol as P

__all__ = ("Cfg", "LEVELS", "main")

LEVELS: "tuple[str, ...]" = ("B0", "B1", "B2", "B3", "B4")


class _LevelFail(RuntimeError):
    """一层失败：``reason`` 取自 ``candidate_birth.REASON_CODES`` 闭集。"""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


class Cfg:
    """``cfg.json`` 的只读视图（键集由父进程单点决定，见 ``candidate_birth``）。"""

    def __init__(self, doc: dict) -> None:
        self.doc = doc
        self.workdir = Path(str(doc["workdir"])).resolve()
        self.entry = dict(doc["entry"])
        self.contract_name = str(doc["contract"])
        self.min_bytes = int(doc.get("min_bytes") or 16)
        self.ctx = dict(doc.get("ctx") or {})
        self.steps = int(dict(doc.get("limits") or {}).get("steps") or 1)
        self.required = dict(doc.get("required") or {})
        self.entry_kind = str(self.entry.get("kind"))
        self.entry_path = str(self.entry.get("path"))


def _out(record: dict) -> None:
    P.emit(P.EVENT_FD, record)


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _jail(cfg: Cfg) -> Path:
    """入口必须是 workdir 内的常规文件（``resolve()`` 后判定，不用字符串前缀）。"""
    wd = cfg.workdir.resolve(strict=True)
    p = wd / cfg.entry_path
    if p.is_symlink():
        target = p.resolve(strict=False)
        raise _LevelFail(
            "path_escape" if not _inside(target, wd) else "symlink",
            f"{cfg.entry_path} 是符号链接（出生验证拒绝链接入口）-> {target}")
    if not p.exists():
        raise _LevelFail("missing", f"{cfg.entry_path} 不存在")
    if not p.is_file():
        raise _LevelFail("missing", f"{cfg.entry_path} 不是常规文件（is_file=False）")
    rp = p.resolve(strict=True)
    if not _inside(rp, wd):
        raise _LevelFail("path_escape", f"{cfg.entry_path} 解析到 {rp}，越过 workdir {wd}")
    return rp


def _load(cfg: Cfg):
    rp = _jail(cfg)
    sys.path.insert(0, str(cfg.workdir))
    try:
        if cfg.entry_kind == "module":
            spec = importlib.util.spec_from_file_location("simpraxis_candidate", str(rp))
            if spec is None or spec.loader is None:
                raise ImportError(f"无法为 {rp} 构造 import spec")
            mod = importlib.util.module_from_spec(spec)
            sys.modules["simpraxis_candidate"] = mod
            spec.loader.exec_module(mod)
            return mod
        return runpy.run_path(str(rp), run_name="__simpraxis_candidate__")
    finally:
        if sys.path and sys.path[0] == str(cfg.workdir):
            sys.path.pop(0)


def _contract(cfg: Cfg):
    try:
        return C.contract_by_name(cfg.contract_name)
    except BaseException as exc:                        # noqa: BLE001 - 基础设施
        raise _LevelFail("blocked_infra",
                         f"契约不可用：{type(exc).__name__}: {exc}") from None


def _load_or_fail(cfg: Cfg) -> object:
    try:
        mod = _load(cfg)
    except _LevelFail:
        raise
    except SyntaxError as exc:
        _out({"ev": P.EV_IMPORT_ERROR, "reason": "ast_error",
              "detail": f"SyntaxError: {exc}"})
        raise _LevelFail("ast_error", f"SyntaxError: {exc}") from None
    except ImportError as exc:
        _out({"ev": P.EV_IMPORT_ERROR, "reason": "import_error",
              "detail": f"ImportError: {exc}"})
        raise _LevelFail("import_error", f"ImportError: {exc}") from None
    except MemoryError as exc:
        _out({"ev": P.EV_IMPORT_ERROR, "reason": "memory",
              "detail": f"加载候选时内存超限：{exc}"})
        raise _LevelFail("memory", f"加载候选时内存超限：{exc}") from None
    except BaseException as exc:                        # noqa: BLE001 - 候选的错
        _out({"ev": P.EV_IMPORT_ERROR, "reason": "import_error",
              "detail": f"{type(exc).__name__}: {exc}"})
        raise _LevelFail("import_error",
                         f"{type(exc).__name__}: {exc}") from None
    _out({"ev": P.EV_IMPORTED, "module": "simpraxis_candidate"})
    return mod


def _build(cfg: Cfg, mod) -> object:
    contract = _contract(cfg)
    try:
        obj = contract.build(mod)
    except MemoryError as exc:
        _out({"ev": P.EV_CONSTRUCTION_ERROR, "reason": "memory",
              "detail": f"MemoryError: {exc}"})
        raise _LevelFail("memory", f"构造时内存超限：{exc}") from None
    except BaseException as exc:                        # noqa: BLE001 - 候选的错
        _out({"ev": P.EV_CONSTRUCTION_ERROR, "reason": "construction_error",
              "detail": f"{type(exc).__name__}: {exc}"})
        raise _LevelFail("construction_error",
                         f"{type(exc).__name__}: {exc}") from None
    _out({"ev": P.EV_CONSTRUCTED, "object_type": type(obj).__name__})
    return obj


def _level_b0(cfg: Cfg) -> dict:
    """**不由本进程判定**：B0 只需要 ``stat`` + ``ast``，父进程自己就能做。

    起一个子进程去收集「父进程本来就能独立测得的事实」只会把判据送到候选手边，
    故 v2 把 B0 整体留在父进程（``candidate_birth.CandidateBirthValidator._run_b0``）。
    本函数保留为**显式占位**：真被调用到就如实报 ``blocked_infra``，而不是
    悄悄给出一个「看着像 B0」的结果。
    """
    return {"outcome": "blocked_infra", "reason": "blocked_infra",
            "detail": "B0 由父进程判定（父进程 stat + AST，不交给子进程）",
            "facts": {}}



def _level_b1(cfg: Cfg) -> dict:
    mod = _load_or_fail(cfg)
    contract = _contract(cfg)
    data = {}
    if contract.builder:
        data["builder_present"] = C.module_get(mod, contract.builder) is not None
    return {"outcome": "pass", "reason": None, "detail": "候选已导入", "facts": data}


def _level_b2(cfg: Cfg) -> dict:
    mod = _load_or_fail(cfg)
    obj = _build(cfg, mod)
    keys = getattr(obj, "observation_keys", None)
    try:
        keys = list(keys) if keys is not None else None
    except BaseException:                               # noqa: BLE001 - 只做呈现
        keys = repr(keys)
    return {"outcome": "pass", "reason": None, "detail": "已构造",
            "facts": {"object_type": type(obj).__name__,
                      "action_dim": getattr(obj, "action_dim", None),
                      "observation_keys": keys}}


def _level_b3(cfg: Cfg) -> dict:
    contract = _contract(cfg)
    obj = None
    if contract.needs_object:
        mod = _load_or_fail(cfg)
        obj = _build(cfg, mod)
    try:
        violations = contract.check(obj, cfg.ctx)
    except SystemExit:
        raise
    except BaseException as exc:                        # noqa: BLE001 - 候选的错
        _out({"ev": P.EV_CONTRACT_ERROR, "reason": "contract_error",
              "detail": f"契约检查抛异常：{type(exc).__name__}: {exc}"})
        raise _LevelFail(
            "contract_error",
            f"契约检查抛异常：{type(exc).__name__}: {exc}") from None
    if violations:
        detail = "; ".join(f"{v.code}: {v.detail}" for v in violations)
        _out({"ev": P.EV_CONTRACT_ERROR, "reason": "contract_error",
              "detail": detail})
        raise _LevelFail("contract_error", detail)
    _out({"ev": P.EV_CONTRACT, "violations": 0})
    return {"outcome": "pass", "reason": None, "detail": "契约检查通过",
            "facts": {"violations": 0}}


def _level_b4(cfg: Cfg) -> dict:
    contract = _contract(cfg)
    mod = _load_or_fail(cfg)
    try:
        obj = contract.build(mod)
    except MemoryError as exc:
        _out({"ev": P.EV_CONSTRUCTION_ERROR, "reason": "memory",
              "detail": f"MemoryError: {exc}"})
        raise _LevelFail("memory", f"构造时内存超限：{exc}") from None
    except BaseException as exc:                        # noqa: BLE001 - 候选的错
        _out({"ev": P.EV_CONSTRUCTION_ERROR, "reason": "construction_error",
              "detail": f"{type(exc).__name__}: {exc}"})
        raise _LevelFail("construction_error",
                         f"{type(exc).__name__}: {exc}") from None
    _out({"ev": P.EV_CONSTRUCTED, "object_type": type(obj).__name__})
    if contract.reset_required:
        try:
            contract.reset(obj, cfg.ctx)
        except MemoryError as exc:
            _out({"ev": P.EV_STEP_ERROR, "reason": "memory",
                  "detail": f"reset 时内存超限：{exc}"})
            raise _LevelFail("memory", f"reset 时内存超限：{exc}") from None
        except BaseException as exc:                    # noqa: BLE001 - 候选的错
            _out({"ev": P.EV_STEP_ERROR, "reason": "step_error",
                  "detail": f"reset 失败：{type(exc).__name__}: {exc}"})
            raise _LevelFail("step_error",
                             f"reset 失败：{type(exc).__name__}: {exc}") from None
    try:
        action = contract.action(obj, cfg.ctx)
    except BaseException as exc:                        # noqa: BLE001 - 候选的错
        _out({"ev": P.EV_STEP_ERROR, "reason": "step_error",
              "detail": f"动作构造失败：{type(exc).__name__}: {exc}"})
        raise _LevelFail("step_error",
                         f"最小动作构造失败：{type(exc).__name__}: {exc}") from None

    advanced = False
    sample: dict = {}
    for index in range(max(1, cfg.steps)):
        try:
            result = contract.step(obj, action, cfg.ctx)
        except MemoryError as exc:
            _out({"ev": P.EV_STEP_ERROR, "reason": "memory",
                  "detail": f"MemoryError: {exc}", "i": index})
            raise _LevelFail("memory", f"step 时内存超限：{exc}") from None
        except BaseException as exc:                    # noqa: BLE001 - 候选的错
            _out({"ev": P.EV_STEP_ERROR, "reason": "step_error",
                  "detail": f"{type(exc).__name__}: {exc}", "i": index})
            raise _LevelFail("step_error",
                             f"step 失败：{type(exc).__name__}: {exc}") from None
        numbers = C.flatten_numbers(result)
        if not numbers:
            _out({"ev": P.EV_STEP, "level": "B4", "i": index,
                  "status": "no_numbers"})
            raise _LevelFail("step_error",
                             f"step 未返回任何数值状态：{result!r}")
        bad = sorted(key for key, value in numbers.items() if not math.isfinite(value))
        if bad:
            _out({"ev": P.EV_STEP, "level": "B4", "i": index,
                  "status": "nonfinite"})
            raise _LevelFail("non_finite", f"步进后存在非有限值：{bad[:8]}")
        if any(value != 0.0 for value in numbers.values()):
            advanced = True
        sample = dict(sorted(numbers.items())[:8])
        _out({"ev": P.EV_STEP, "level": "B4", "i": index, "status": "ok"})
    _out({"ev": P.EV_ADVANCE, "advanced": bool(advanced), "n_values": len(sample)})
    if not advanced:
        raise _LevelFail("step_error", "步进后状态毫无变化（未推进）")
    return {"outcome": "pass", "reason": None, "detail": "最小步进成功",
            "facts": {"advanced": True, "n_values": len(sample), "sample": sample}}


_IMPL = {"B0": _level_b0, "B1": _level_b1, "B2": _level_b2,
         "B3": _level_b3, "B4": _level_b4}


def _run_level(cfg: Cfg, level: str) -> dict:
    if not cfg.required.get(level, True):
        return {"outcome": "not_applicable", "reason": None,
                "detail": "birth_contract 未要求该层（不适用层不计入 PASS）",
                "facts": {}}
    fn = _IMPL.get(level)
    if fn is None:
        return {"outcome": "blocked_infra", "reason": "blocked_infra",
                "detail": f"未知层 {level!r}", "facts": {}}
    return fn(cfg)


def main(argv: "list[str]") -> int:
    if len(argv) < 2:
        return 2
    cfg = Cfg(json.loads(Path(argv[1]).read_text(encoding="utf-8")))
    levels = [str(item) for item in (cfg.doc.get("levels") or [cfg.doc.get("level")])]
    _out({"ev": P.EV_START, "pid": os.getpid(), "ts": time.monotonic(),
          "wall": P.now_wall(), P.FIELD_ABSENT: P.probe_absent(),
          "mode": str(cfg.doc.get("level")), "levels": levels})
    for level in levels:
        try:
            payload = dict(_run_level(cfg, level))
        except _LevelFail as exc:
            payload = {"outcome": "fail", "reason": exc.reason,
                       "detail": exc.detail, "facts": {}}
        except SystemExit:
            raise
        except BaseException as exc:                    # noqa: BLE001 - 自身异常
            payload = {"outcome": "blocked_infra", "reason": "blocked_infra",
                       "detail": f"{type(exc).__name__}: {exc}\n"
                                 f"{traceback.format_exc()[-2048:]}", "facts": {}}
        payload.update({"ev": P.EV_LEVEL, "level": level})
        _out(payload)
    return 0


if __name__ == "__main__":                              # pragma: no cover - 子进程入口
    sys.exit(main(sys.argv))
