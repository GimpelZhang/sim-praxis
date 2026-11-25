"""RSI-S1 逐点 host 重仿真（S0 残尾②）。

口径（计划 S1-1 第 2 条）：

* ``recompute(confirm_plan, fresh_root=...)``：在**全新根**上按冻结确认计划
  （条件 × 私有 block）重跑受信物理服务，产出逐点 ``(t, qpos, qvel)`` 序列与
  逐对比重算值；
* ``compare(raw, tol)``：与原始 raw 记录逐点比较——确定性物理要求逐点一致
  （默认 tol=0.0 位级；跨进程复算可用 1e-12），不一致**逐点报 Diff**，定位到
  run / block / step / 字段，绝不"重跑求解直到吻合"；
* 接线：K7 ``default_confirmer`` 与 S1-7 独立复跑共用本模块；输出喂
  ``GuardCtx.host_recomputed``（G-CLAIM 的 host 侧真值）。

纪律：比对失败即停止码（不补考、不换种子、不改界）；本模块不 import mujoco
（物理一律经 ``physics_service`` 受信面执行）。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from orchestrator.common.clock import utc_now_iso_z
from orchestrator.common.fsutil import atomic_write_text, ensure_private_dir
from orchestrator.rsi_r1 import experiments as E
from orchestrator.rsi_r1 import observations as O
from orchestrator.rsi_r1 import statistics as ST

from .physics_service import PhysicsService, PhysicsServiceError

__all__ = ["SCHEMA", "HostRecomputeError", "Diff", "HostRecompute",
           "point_diffs", "recompute"]

SCHEMA = "simpraxis.rsi_s1.host_recompute.v1"

#: 逐点比较的字段（确定性物理的全部状态通道）。
POINT_FIELDS = ("time_s", "qpos", "qvel")

#: 指标别名：阴性等效指标是**同一位移量**在不同 δ/区间语义下的登记名
#: （rubric：「displacement_x_m_negative_equivalence」= 校准阴性控制专用）。
#: 分析/复算按基础指标取值，禁止为它另造一份数值通道。
METRIC_ALIASES = {"displacement_x_m_negative_equivalence": "displacement_x_m",
                  # S2-1：B 机制的亚阈阴性对照（计划 §2.1 第 3 行）
                  "normalized_tracking_rmse_negative_equivalence":
                      "normalized_tracking_rmse",
                  # S2b：C_N/D_N 亚阈阴性对照
                  "rebound_height_m_negative_equivalence":
                      "rebound_height_m",
                  "steady_state_amplitude_rad_negative_equivalence":
                      "steady_state_amplitude_rad"}


class HostRecomputeError(RuntimeError):
    """复算失败/不一致（带机器码；停止码语义）。"""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


@dataclass(frozen=True)
class Diff:
    run_id: str
    condition_id: str
    block_id: str
    step: int
    field: str
    baseline: Any
    replay: Any

    def to_dict(self) -> dict[str, Any]:
        return {"run_id": self.run_id, "condition_id": self.condition_id,
                "block_id": self.block_id, "step": self.step,
                "field": self.field, "baseline": self.baseline,
                "replay": self.replay}


def _flatten(value: Any) -> list[float]:
    if isinstance(value, (list, tuple)):
        out: list[float] = []
        for item in value:
            out.extend(_flatten(item))
        return out
    return [float(value)]


def _record_diffs(run_row: Mapping[str, Any], baseline: Mapping[str, Any],
                  replay: Mapping[str, Any], tol: float) -> list[Diff]:
    diffs: list[Diff] = []
    run_id = str(run_row.get("run_id") or baseline.get("run_id") or "?")
    cond = str(run_row.get("condition_id") or "?")
    block = str(run_row.get("block_id") or baseline.get("block_id") or "?")
    for name in POINT_FIELDS:
        if name not in baseline or name not in replay:
            diffs.append(Diff(run_id, cond, block, -1, name,
                              baseline.get(name), replay.get(name)))
            continue
        base_series = baseline[name]
        rep_series = replay[name]
        if not isinstance(base_series, list) or not isinstance(rep_series, list):
            diffs.append(Diff(run_id, cond, block, -1, name,
                              type(base_series).__name__,
                              type(rep_series).__name__))
            continue
        if len(base_series) != len(rep_series):
            diffs.append(Diff(run_id, cond, block, -1, f"{name}.length",
                              len(base_series), len(rep_series)))
            continue
        for step in range(len(base_series)):
            left = _flatten(base_series[step])
            right = _flatten(rep_series[step])
            if len(left) != len(right):
                diffs.append(Diff(run_id, cond, block, step, f"{name}.width",
                                  len(left), len(right)))
                continue
            for idx, (a, b) in enumerate(zip(left, right)):
                if abs(a - b) > tol:
                    suffix = f"{name}[{idx}]" if len(left) > 1 else name
                    diffs.append(Diff(run_id, cond, block, step, suffix, a, b))
    return diffs


def point_diffs(run_row: Mapping[str, Any], baseline: Mapping[str, Any],
                replay: Mapping[str, Any], *, tol: float = 0.0) -> list[Diff]:
    """逐点比较一条 run 的 (t, qpos, qvel)；任何超出 tol 的点都是 Diff。"""
    if tol < 0:
        raise HostRecomputeError("TOL_INVALID", f"tol={tol}")
    return _record_diffs(run_row, baseline, replay, tol)


@dataclass
class HostRecompute:
    """一次 host 重算的句柄（原始/重放记录路径 + 逐点差异 + 重算统计）。"""

    study: str
    plan_sha256: str
    fresh_root: Path
    tolerance: float
    runs: list[dict[str, Any]] = field(default_factory=list)
    diffs: list[Diff] = field(default_factory=list)
    confirm_result: dict[str, Any] = field(default_factory=dict)
    generated_at_utc: str = ""

    def compare(self, raw: "Mapping[str, Any] | None" = None,
                tol: float | None = None) -> list[Diff]:
        """与 ``raw``（默认本句柄的原始记录集）逐点比较并返回 Diff 列表。

        传入 ``raw`` 时按 run_id 索引其 ``runs`` 行（每行须含 ``record_path``）；
        缺 run、缺记录文件都是 Diff（``step=-1``），不允许"少比一条"。
        """
        tolerance = self.tolerance if tol is None else float(tol)
        if raw is None:
            rows = {str(r["run_id"]): r for r in self.runs}
        else:
            source = raw.get("runs") if isinstance(raw, Mapping) else None
            if not isinstance(source, list):
                raise HostRecomputeError("RAW_SHAPE", "raw.runs 必须是列表")
            rows = {str(r["run_id"]): r for r in source}
        out: list[Diff] = []
        for replay in self.runs:
            run_id = str(replay["run_id"])
            row = rows.get(run_id)
            if row is None:
                out.append(Diff(run_id, replay.get("condition_id", "?"),
                                replay.get("block_id", "?"), -1, "run",
                                "present", "missing"))
                continue
            baseline = self._load(row.get("record_path"))
            replayed = self._load(replay.get("replay_record_path")
                                  or replay.get("record_path"))
            if baseline is None or replayed is None:
                out.append(Diff(run_id, replay.get("condition_id", "?"),
                                replay.get("block_id", "?"), -1, "record",
                                "present" if baseline is not None else "missing",
                                "present" if replayed is not None else "missing"))
                continue
            out.extend(point_diffs(row, baseline, replayed, tol=tolerance))
        return out

    @staticmethod
    def _load(path: Any) -> "dict[str, Any] | None":
        if not isinstance(path, str) or not path:
            return None
        target = Path(path)
        if not target.is_file():
            return None
        return O.read_record_jsonl(target)


def _linear_contrast_values(rows: Sequence[Mapping[str, Any]],
                            contrast: Mapping[str, Any]) -> dict[str, float]:
    """逐 block 的线性对比值：Σ weight × metric(condition)。

    DiD（四项 ±1）与 pairwise（两项 ±1）同式；缺项/重复即拒。
    """
    terms = contrast.get("terms")
    if not isinstance(terms, list) or len(terms) < 2:
        raise HostRecomputeError("CONTRAST_INVALID", f"{contrast.get('id')!r}")
    weights: dict[str, float] = {}
    for term in terms:
        if isinstance(term, Mapping):
            cid = str(term.get("condition_id"))
            weight = float(term.get("weight"))
        else:
            cid, weight = str(term[0]), float(term[1])
        weights[cid] = weight
    metric_id = str(contrast.get("metric_id") or contrast.get("metric"))
    metric_id = METRIC_ALIASES.get(metric_id, metric_id)
    by_block: dict[str, dict[str, float]] = {}
    for row in rows:
        if str(row.get("metric_id")) != metric_id:
            continue
        cid = str(row.get("condition_id"))
        if cid not in weights:
            continue
        block = str(row.get("block_id"))
        slot = by_block.setdefault(block, {})
        if cid in slot:
            raise HostRecomputeError(
                "DUPLICATE_BLOCK", f"{block}/{cid} 出现多次（分母偷换）")
        slot[cid] = float(row["value"])
    values: dict[str, float] = {}
    for block, slot in by_block.items():
        missing = sorted(set(weights) - set(slot))
        if missing:
            raise HostRecomputeError(
                "UNPAIRED_BLOCK", f"{block} 缺条件 {missing}")
        values[block] = sum(weights[cid] * slot[cid] for cid in weights)
    if not values:
        raise HostRecomputeError("EMPTY", f"对比 {contrast.get('id')!r} 无数据")
    return values


def recompute(confirm_plan: Mapping[str, Any], *, fresh_root: Path | str,
              tol: float = 0.0, service: PhysicsService | None = None,
              batch_id: str = "confirm") -> HostRecompute:
    """在全新根上按冻结确认计划重跑，并与原始记录逐点比较。

    ``confirm_plan`` 必须已冻结且携带 ``study`` / ``plan_sha256`` /
    ``conditions`` / ``blocks`` / ``runs``（每条含 ``record_path``）。
    返回句柄：``.diffs`` 为空才允许放行；非空即调用方按停止码处理
    （本函数默认不吞错，但也不"自动重跑救场"）。
    """
    if confirm_plan.get("frozen") is not True:
        raise HostRecomputeError("NOT_FROZEN", "confirm_plan 未冻结：拒绝复跑")
    study = str(confirm_plan.get("study") or "")
    plan_sha = str(confirm_plan.get("plan_sha256") or "")
    runs = confirm_plan.get("runs")
    conditions = confirm_plan.get("conditions")
    blocks = confirm_plan.get("blocks")
    if not study or len(plan_sha) != 64:
        raise HostRecomputeError("CONFIRM_INVALID", "缺 study/plan_sha256")
    if not isinstance(runs, list) or not runs:
        raise HostRecomputeError("CONFIRM_INVALID", "缺原始 runs")
    if not isinstance(conditions, list) or not conditions:
        raise HostRecomputeError("CONFIRM_INVALID", "缺 conditions")
    if not isinstance(blocks, list) or not blocks:
        raise HostRecomputeError("CONFIRM_INVALID", "缺 blocks")
    root = ensure_private_dir(Path(fresh_root))
    service = service or PhysicsService(observations_root=root / "obs",
                                        evidence_root=root / "evidence")
    by_key = {(str(c["id"]), str(b["block_id"])): (c, b)
              for c in conditions for b in blocks}
    replay_rows: list[dict[str, Any]] = []
    for row in runs:
        key = (str(row.get("condition_id")), str(row.get("block_id")))
        if key not in by_key:
            raise HostRecomputeError("RUN_UNKNOWN", f"{key} 不在冻结分配表")
        cond, block = by_key[key]
        params = {**dict(cond.get("params") or {}),
                  **dict(block.get("nuisance") or {})}
        spec = E.build_host_spec(
            plan_sha256=plan_sha,
            study_id={"A_N": "A", "B_N": "B", "C_N": "C", "D_N": "D"}.get(study, study),
            condition_id=key[0], block_id=key[1],
            scene_id=str(cond["scene_id"]), params=params)
        try:
            result = service.run_one(spec=spec, batch_id=batch_id)
        except PhysicsServiceError as exc:
            raise HostRecomputeError(f"REPLAY_{exc.code}", exc.detail) from None
        replay_rows.append({**row, "replay_record_path": str(result.record_path),
                            "replay_record_sha256": result.record_sha256,
                            "replay_value": result.metric_value})
    handle = HostRecompute(study=study, plan_sha256=plan_sha,
                           fresh_root=root, tolerance=float(tol),
                           runs=replay_rows, generated_at_utc=utc_now_iso_z())
    handle.diffs = handle.compare(confirm_plan, tol=tol)
    # 逐对比重算（host 独立值；与 K5 的估计逐字段比较在 G-CLAIM / 独立复跑）
    contrasts = confirm_plan.get("contrasts") or []
    if not isinstance(contrasts, list):
        raise HostRecomputeError("CONFIRM_INVALID", "contrasts 必须是列表")
    results: list[dict[str, Any]] = []
    # 评审 MEDIUM：对比值必须用**重放**指标（replay_value），否则"独立复算"是
    # 拿原始值的重算（同义反复）。逐点零 Diff 时二者应逐位相等。
    replay_stat_rows = [{**row, "value": row.get("replay_value")}
                        for row in replay_rows]
    for contrast in contrasts:
        values = _linear_contrast_values(replay_stat_rows, contrast)
        estimated = ST.estimate(values, level=contrast.get("level", "0.95"))
        # S2 词表 v2（五值；与 default_analyzer 同一分类真值）
        from orchestrator.hk import verdicts as V

        results.append({
            "id": contrast.get("id"),
            "metric_id": contrast.get("metric_id"),
            "proposition": contrast.get("proposition"),
            "delta": contrast.get("delta"),
            "level": contrast.get("level", "0.95"),
            "estimate": estimated["effect"],
            "ci_low": estimated["lower"],
            "ci_high": estimated["upper"],
            "n_blocks": estimated["n_blocks"],
            "claim": V.classify_v2(contrast.get("proposition"),
                                   contrast.get("delta"),
                                   estimated["lower"], estimated["upper"],
                                   n_blocks=estimated["n_blocks"]),
        })
    handle.confirm_result = {
        "schema": f"{SCHEMA}.confirm_result",
        "study": study,
        "plan_sha256": plan_sha,
        "confirmed_at_utc": handle.generated_at_utc,
        "point_tolerance": float(tol),
        "n_runs": len(replay_rows),
        "diffs": [d.to_dict() for d in handle.diffs],
        "contrasts": results,
        "result_sha256": "",
    }
    handle.confirm_result["result_sha256"] = hashlib.sha256(json.dumps(
        {k: v for k, v in handle.confirm_result.items() if k != "result_sha256"},
        ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")).hexdigest()
    return handle


def write_report(handle: HostRecompute, path: Path) -> Path:
    """把复算句柄（含逐点 Diff）原子落盘（证据件）。"""
    doc = {
        "schema": SCHEMA,
        "study": handle.study,
        "plan_sha256": handle.plan_sha256,
        "fresh_root": str(handle.fresh_root),
        "tolerance": handle.tolerance,
        "generated_at_utc": handle.generated_at_utc,
        "runs": handle.runs,
        "n_diffs": len(handle.diffs),
        "diffs": [d.to_dict() for d in handle.diffs],
        "confirm_result": handle.confirm_result,
    }
    target = Path(path)
    atomic_write_text(target,
                      json.dumps(doc, ensure_ascii=False, sort_keys=True,
                                 indent=2) + "\n", mode=0o600)
    return target
