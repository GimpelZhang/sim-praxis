"""RSI-R0.1 判据实现：删失/沉降/效应/假设判定与工程族 L4 复算。

科学合同（计划 §4.3）的唯一实现处。独立性要求（§4.3 末段 / AC17）：本模块的
沉降与效应函数**不得**被 :mod:`scripts.rsi_r01.independent_oracle` import；
oracle 用另一套算法（后缀真假归约 vs 反向找最后越界索引）独立复算，两者输出
按预锁界比对。

边界语义逐条落地：

* ``inside[i] = |θ[i]| < 0.05 AND |ω[i]| < 0.05``——严格小于，等于阈值**不在**带内；
* 沉降索引 = 最早 i 使所有 j≥i 均 inside；末样本不在带内即**删失**；
* 仅末点满足时按定义可为 T，但必须公开 ``only_last_point=True``（有限窗口
  「至末尾保持」，不暗称无限时稳定）；
* ``e_a = (t_low,a − t_high,a)/t_low,a``；``mean_effect`` 是两个角度 e_a 的
  算术平均，**不可**先平均时间再比值；
* 任一必要时间删失、``t_low=0`` 或数据不完整 ⇒ ``mean_effect=null``，同时给出
  原因与可计算的部分统计（不拿 T 代替删失、不把 null 强转 float）；
* ``z = h × mean_effect``；``z ≥ δ`` SUPPORTED，``z ≤ −δ`` REFUTED，中间
  INCONCLUSIVE；δ=0.10 预先规定，不随付费结果移动。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from orchestrator.rsi_r01 import schemas as S

#: 计算容差只针对**同一份完整离散观测**（binary64 运算/序列化界导出并冻结）。
#: 沉降时间按采样索引比对（容差 0）；效应按相对差比。
EFFECT_TOLERANCE = 1e-12
#: dt 与 dt/2 的**物理**收敛界（T2 校准用，不是 C5 计算容差）。
CONVERGENCE_SETTLE_TOL_S = 0.02
CONVERGENCE_EFFECT_TOL = 0.01


class EvaluationError(S.ContractError):
    """内部合同不成立（不是候选的错，须落 INVALID_INSTRUMENT）。"""


@dataclass(frozen=True)
class Layer:
    L1 = "L1"
    L2 = "L2"
    L3 = "L3"
    L4 = "L4"
    L5 = "L5"

    ALL = (L1, L2, L3, L4, L5)


@dataclass(frozen=True)
class SettleResult:
    """一次观测的沉降判定（全部字段来自数据本身）。"""

    index: int | None
    time_s: float | None
    censored: bool
    only_last_point: bool
    hold_to_end_s: float | None
    rows: int

    def as_dict(self) -> dict[str, Any]:
        return {"index": self.index, "time_s": self.time_s,
                "censored": self.censored, "only_last_point": self.only_last_point,
                "hold_to_end_s": self.hold_to_end_s, "rows": self.rows}


def inside_band(theta: float, omega: float) -> bool:
    """带内判定：严格小于（等于阈值不在带内）。"""
    return abs(theta) < S.THETA_BAND and abs(omega) < S.OMEGA_BAND


def settling(theta: Sequence[float], omega: Sequence[float], t: Sequence[float]
             ) -> SettleResult:
    """最早 i 使 ∀j≥i inside；末点不在带内即删失。

    算法：从尾部向前找**最后一个**越界索引，其下一索引即沉降点。这样做而不是
    逐点记忆布尔数组，是为了让 oracle 能用相反方向的算法（后缀归约）复算。
    """
    n = len(theta)
    if n == 0 or len(omega) != n or len(t) != n:
        raise EvaluationError("序列长度不一致或为空")
    last_out = -1
    for i in range(n - 1, -1, -1):
        if not inside_band(theta[i], omega[i]):
            last_out = i
            break
    if last_out == n - 1:
        return SettleResult(index=None, time_s=None, censored=True,
                            only_last_point=False, hold_to_end_s=None, rows=n)
    idx = last_out + 1
    settle_time = float(t[idx])
    only_last = idx == n - 1
    hold = float(t[-1]) - settle_time
    return SettleResult(index=idx, time_s=settle_time, censored=False,
                        only_last_point=only_last, hold_to_end_s=hold, rows=n)


def effect(t_low: float | None, t_high: float | None) -> dict[str, Any]:
    """e = (t_low − t_high)/t_low；任一缺失或 t_low=0 则不可计算（null + 原因）。"""
    if t_low is None or t_high is None:
        return {"value": None, "reason": "censored_or_missing"}
    if t_low == 0.0:
        return {"value": None, "reason": "t_low_zero"}
    value = (float(t_low) - float(t_high)) / float(t_low)
    if value != value or value in (float("inf"), float("-inf")):
        return {"value": None, "reason": "non_finite"}
    return {"value": value, "reason": None}


def mean_effect(per_angle: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """两角度 e_a 的算术平均；任一缺失 ⇒ null（不给只算一半的平均冒充全量）。"""
    values = []
    missing = []
    for angle, res in per_angle.items():
        if res.get("value") is None:
            missing.append(angle)
        else:
            values.append(float(res["value"]))
    if missing:
        return {"value": None, "reason": "partial_censored",
                "missing_angles": sorted(missing),
                "computable_values": dict(sorted((a, r["value"]) for a, r in per_angle.items()
                                                 if r.get("value") is not None))}
    if not values:
        raise EvaluationError("没有任何角度可计算效应")
    return {"value": sum(values) / len(values), "reason": None,
            "computable_values": dict(sorted((a, r["value"]) for a, r in per_angle.items()))}


def claim_of(mean: float | None, h: int) -> tuple[str, float | None]:
    """z = h × mean_effect；δ 双侧界。"""
    if mean is None:
        return S.ClaimStatus.INCONCLUSIVE, None
    if h not in (1, -1):
        raise EvaluationError(f"公开假设方向必须是 +1/−1，收到 {h!r}")
    z = float(h) * float(mean)
    if z >= S.DELTA:
        return S.ClaimStatus.SUPPORTED, z
    if z <= -S.DELTA:
        return S.ClaimStatus.REFUTED, z
    return S.ClaimStatus.INCONCLUSIVE, z


# ---------------------------------------------------------------------------
# R 族：四格（damping × 初角）+ 假设判定
# ---------------------------------------------------------------------------

#: 公开初角键（四格合同的一部分；计划 §4.1）。
ANGLE_KEYS = ("plus", "minus")


@dataclass
class ResearchJudgement:
    observation_status: str
    claim_status: str
    analysis_valid: bool
    informative: bool
    mean_effect: float | None
    z: float | None
    h: int
    cells: dict[str, Any] = field(default_factory=dict)
    reason_codes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": S.SCHEMA_VERDICT,
            "observation_status": self.observation_status,
            "claim_status": self.claim_status,
            "analysis_valid": self.analysis_valid,
            "informative": self.informative,
            "mean_effect": self.mean_effect,
            "z": self.z,
            "h": self.h,
            "delta": S.DELTA,
            "cells": self.cells,
            "reason_codes": list(self.reason_codes),
        }


def judge_research(*, observations: Mapping[str, Mapping[str, Any]], h: int,
                   prereg: Mapping[str, Any] | None = None) -> ResearchJudgement:
    """对四个 run（低/高阻尼 × 两个初角）判定公开假设。

    ``observations`` 键形如 ``"low/plus"``、``"high/minus"``，值为完整观测文档。
    任何文档不合法 ⇒ observation_status=INVALID、analysis_valid=False；
    合法但删失 ⇒ CENSORED、analysis_valid 仍可为 True（诚实报告删失不是伪造），
    而 ``informative`` 为 False（本轮已校准的信息任务要求 informative 才 L5 通过）。
    """
    cells: dict[str, Any] = {}
    reasons: list[str] = []
    any_invalid = False
    any_censored = False
    settle: dict[tuple[str, str], SettleResult] = {}

    expected = [f"{d}/{a}" for d in ("low", "high") for a in ANGLE_KEYS]
    missing = [k for k in expected if k not in observations]
    if missing:
        any_invalid = True
        reasons.append(S.ReasonCode.OBSERVATION_INCOMPLETE)
    for key in expected:
        doc = observations.get(key)
        if doc is None:
            continue
        problems = S.check_observation_doc(doc)
        if problems:
            any_invalid = True
            reasons.append(S.ReasonCode.OBSERVATION_INCOMPLETE)
            cells[key] = {"status": S.ObservationStatus.INVALID,
                          "problems": problems[:8]}
            continue
        res = settling(doc["theta"], doc["omega"], doc["t"])
        settle[(key.split("/")[0], key.split("/")[1])] = res
        if res.censored:
            any_censored = True
        cells[key] = {"status": (S.ObservationStatus.CENSORED if res.censored
                                 else S.ObservationStatus.COMPLETE),
                      "settle": res.as_dict()}

    per_angle: dict[str, dict[str, Any]] = {}
    for angle in ANGLE_KEYS:
        low = settle.get(("low", angle))
        high = settle.get(("high", angle))
        t_low = None if low is None or low.censored else low.time_s
        t_high = None if high is None or high.censored else high.time_s
        per_angle[angle] = effect(t_low, t_high)
        per_angle[angle]["t_low"] = t_low
        per_angle[angle]["t_high"] = t_high

    if any_invalid:
        obs_status = S.ObservationStatus.INVALID
        mean = {"value": None, "reason": "invalid_observation"}
        claim, z = S.ClaimStatus.INCONCLUSIVE, None
        analysis_valid = False
    else:
        mean = mean_effect(per_angle)
        claim, z = claim_of(mean["value"], h)
        obs_status = S.ObservationStatus.CENSORED if any_censored else S.ObservationStatus.COMPLETE
        analysis_valid = True

    if any_censored:
        reasons.append(S.ReasonCode.CENSORED)
    informative = bool(claim != S.ClaimStatus.INCONCLUSIVE and obs_status == S.ObservationStatus.COMPLETE)
    if prereg is not None:
        problems = check_prereg(prereg)
        if problems:
            reasons.append(S.ReasonCode.PREREG_MISMATCH)
            analysis_valid = False
            informative = False

    out_cells = dict(cells)
    for angle, res in per_angle.items():
        out_cells[f"effect/{angle}"] = res
    out_cells["mean_effect"] = mean
    return ResearchJudgement(
        observation_status=obs_status, claim_status=claim,
        analysis_valid=analysis_valid, informative=informative,
        mean_effect=mean["value"], z=z, h=int(h), cells=out_cells,
        reason_codes=sorted(set(reasons)),
    )


# ---------------------------------------------------------------------------
# 预注册语义（§4.4 / AC12）
# ---------------------------------------------------------------------------

PREREG_REQUIRED_FIELDS = (
    "question", "h", "cells", "metric", "formula_version", "delta",
    "censoring_rule", "analysis_procedure", "stop_rule",
)


def check_prereg(prereg: Mapping[str, Any], *,
                 expected_cells: Iterable[tuple[float, float]] | None = None
                 ) -> list[str]:
    """结构一致性：字段齐全、四格参数矩阵按**集合相等**校验。

    ``expected_cells`` 是该实例冻结的四格 (damping, theta0) 集合；给出时必须
    集合相等（不能只比去重数，也不能少格/多格/换参数）。
    """
    problems: list[str] = []
    if not isinstance(prereg, Mapping):
        return ["prereg 不是对象"]
    for key in PREREG_REQUIRED_FIELDS:
        if key not in prereg:
            problems.append(f"缺字段 {key}")
    if problems:
        return problems
    if prereg.get("h") not in (1, -1):
        problems.append("h 必须是 +1/−1")
    if prereg.get("delta") != S.DELTA:
        problems.append(f"delta 必须等于预规定 {S.DELTA}")
    cells = prereg.get("cells")
    if not isinstance(cells, list):
        return problems + ["cells 必须是数组"]
    if expected_cells is None:
        # 工程族（E/D）：没有四格参数矩阵，登记只要求非空的计划格集合。
        if not cells:
            problems.append("cells 不能为空")
        if not all(isinstance(c, Mapping) for c in cells):
            problems.append("cells 每项必须是对象")
        return problems
    try:
        got = [(float(c["damping"]), round(float(c["theta0"]), 12)) for c in cells]
    except (TypeError, KeyError, ValueError) as exc:
        return problems + [f"cells 结构不合法：{exc}"]
    if len(cells) != 4:
        problems.append(f"cells 必须恰好 4 格，收到 {len(cells)}")
    if len(set(got)) != len(got):
        problems.append("cells 有重复格（重复同格顶数）")
    want = {(float(d), round(float(a), 12)) for d, a in expected_cells}
    if set(got) != want:
        problems.append(f"四格参数集合不符：got={sorted(set(got))} want={sorted(want)}")
    return problems


# ---------------------------------------------------------------------------
# 工程族（E/D）：L4 复算只看可信事件
# ---------------------------------------------------------------------------


def lower_bound_success(*, distance: float, final_speed: float,
                        tolerance: Mapping[str, float]) -> bool:
    dist_tol = float(tolerance.get("distance", 0.06))
    speed_tol = float(tolerance.get("final_speed", 0.02))
    return bool(distance <= dist_tol and final_speed <= speed_tol)


def evaluate_engineering(*, runs: Sequence[Mapping[str, Any]],
                         tolerance: Mapping[str, float]) -> dict[str, Any]:
    """E/D 族 L4：末次可信 run 的 distance/final_speed 在容差内即通过。

    ``runs`` 元素形如 ``{"run_id":..., "distance":..., "final_speed":...}``，
    全部来自可信服务回传，候选自报一律不参与（AC09/AC16）。
    """
    if not runs:
        return {"layer": Layer.L4, "status": "FAIL", "reason": "no_trusted_run"}
    last = runs[-1]
    ok = lower_bound_success(distance=float(last["distance"]),
                             final_speed=float(last["final_speed"]),
                             tolerance=tolerance)
    return {"layer": Layer.L4, "status": "PASS" if ok else "FAIL",
            "run_id": last.get("run_id"),
            "distance": float(last["distance"]),
            "final_speed": float(last["final_speed"]),
            "tolerance": dict(tolerance)}
