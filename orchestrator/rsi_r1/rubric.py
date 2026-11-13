"""RSI-R1 公开量表：所有数值界/方向语义的唯一源（AC09）。

「量表先于校准固定」指：δ、等效界、灵敏度门、失稳定义、复跑容差、重抽样参数
都在第一次真实校准之前写死在本文件，并由 ``rubric_sha256()`` 进 prereg/LOCK。
任何改动都会改变该 hash ⇒ 冻结件失配（AC09 的「改 δ/容差使 LOCK 失效」）。

本模块零 import（只依赖 lanes 的纯字面量），``contracts``/``judge``/``replay``
都从这里取数，禁止第二份字面量。
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Any, Mapping

from orchestrator.rsi_r1.lanes import (
    MAX_CONFIRM_RUNS_PER_STUDY,
    MAX_EXPLORE_RUNS_PER_STUDY,
    MAX_REPLAY_RUNS_PER_STUDY,
)

# ---------------------------------------------------------------------------
# 统计：配对差 + bootstrap percentile 区间（§4.1）
# ---------------------------------------------------------------------------

BOOTSTRAP_RESAMPLES = 10_000
BOOTSTRAP_UNIT = "block"          # 32 个 block 为抽样单位，绝不按 run 抽
BOOTSTRAP_QUANTILE = "linear"
BOOTSTRAP_DTYPE = "float64"
CI_PRIMARY = Decimal("0.95")
CI_NEGATIVE = Decimal("0.90")     # 阴性等效用 90% 区间；不得拿 p>0.05 当阴性

# 候选主张 ↔ judge 独立重算的穷尽比较容差
CLAIM_ABS_TOL = Decimal("1e-8")
CLAIM_REL_TOL = Decimal("1e-6")

PROPOSITIONS = ("decrease_gt", "increase_gt", "equivalent")

# ---------------------------------------------------------------------------
# 指标目录：δ 由本目录固定，候选不能扩界（§4.1 末段）
# ---------------------------------------------------------------------------

#: metric_id → {unit, delta(Decimal 字面量), study, censoring, description}
METRICS: "Mapping[str, Mapping[str, Any]]" = {
    "displacement_x_m": {
        "unit": "m",
        "delta": Decimal("0.001"),
        "study": "A",
        "censoring": "none",
        "description": ("4 s 内刚体沿 x 方向的净位移：x(t_end) − 声明的初始位置 "
                        "meta.initial_x_m（t=0；**不是**首个采样 qpos[0]）"),
    },
    "stop_time_s": {
        "unit": "s",
        "delta": Decimal("0.01"),
        "study": "A",
        "censoring": "right",
        "description": ("首次满足 |vx|<0.01 m/s 且随后 0.2 s 内保持的最早时刻；"
                        "4 s 内未停即右删失，不得当 0"),
    },
    "displacement_x_m_negative_equivalence": {
        "unit": "m",
        "delta": Decimal("0.001"),
        "study": "A",
        "censoring": "none",
        "description": ("校准阴性控制专用：基准 vs sham 位移差的等效界；"
                        "90% 区间须严格落 ±0.001 m 内"),
    },
    "normalized_tracking_rmse": {
        "unit": "1",
        "delta": Decimal("0.01"),
        "study": "B",
        "censoring": "none",
        "description": ("按实际 dt 积分的跟踪误差，除以目标幅值 A；"
                        "时间窗 0.5–4 s；硬失稳 run 记 loss=10"),
    },
}

#: A 的阴性控制必须用 equivalent 主张 + 90% 区间 + 该界。
NEGATIVE_CONTROL_METRIC = "displacement_x_m_negative_equivalence"

# ---------------------------------------------------------------------------
# Study A：校准控制（§3.2）
# ---------------------------------------------------------------------------

#: 独立灵敏度控制：固定 μ=0.15 vs 0.6、m=0.5 kg、v0=0.4 m/s，
#: 位移差绝对值必须 > 0.01 m，否则 DESIGN_NOT_READY（不换 seed/阈值救场）。
SENSITIVITY_MU_LOW = Decimal("0.15")
SENSITIVITY_MU_HIGH = Decimal("0.60")
SENSITIVITY_MASS_KG = Decimal("0.5")
SENSITIVITY_V0_MPS = Decimal("0.4")
SENSITIVITY_MIN_DISPLACEMENT_DIFF_M = Decimal("0.01")

#: 阴性 sham 与基准**只**差 torsional friction；实际 contact dim 两边恒为 3。
NEGATIVE_CONTROL_ALLOWED_DIFF = ("torsional_friction_m",)
NEGATIVE_CONTROL_CONTACT_DIM = 3

#: stop_time 指标的定义常量（§3.2）。
STOP_VX_EPS_MPS = Decimal("0.01")
STOP_HOLD_S = Decimal("0.2")
RUN_DURATION_S = Decimal("4.0")

# ---------------------------------------------------------------------------
# Study B：失稳与损失（§3.3）
# ---------------------------------------------------------------------------

INSTABILITY_Q_ABS_MAX_RAD = Decimal("4.0")
INSTABILITY_LOSS = Decimal("10.0")
LOSS_CAP = Decimal("10.0")
TRACKING_WINDOW_START_S = Decimal("0.5")
TRACKING_WINDOW_END_S = Decimal("4.0")

# ---------------------------------------------------------------------------
# 复跑容差（§5.6，首校准前注册）
# ---------------------------------------------------------------------------

#: metric_id → (绝对容差, 相对补项)；判据 abs+rel 合并。
REPLAY_TOLERANCES: "Mapping[str, tuple[Decimal, Decimal]]" = {
    "displacement_x_m": (Decimal("1e-5"), Decimal("1e-4")),
    "stop_time_s": (Decimal("0.002"), Decimal("1e-4")),
    "normalized_tracking_rmse": (Decimal("1e-5"), Decimal("1e-4")),
}
REPLAY_DEFAULT_TOLERANCE = (Decimal("1e-5"), Decimal("1e-4"))

# ---------------------------------------------------------------------------
# 数据划分（§3.4）
# ---------------------------------------------------------------------------

CONFIRM_BLOCKS = 32
MAX_CONDITIONS = 6
EXPLORE_BATCHES = 8
EXPLORE_RUNS_PER_BATCH = 8
MAX_EXPLORE_RUNS = MAX_EXPLORE_RUNS_PER_STUDY     # 64
MAX_CONFIRM_RUNS = MAX_CONFIRM_RUNS_PER_STUDY     # 192
MAX_REPLAY_RUNS = MAX_REPLAY_RUNS_PER_STUDY       # 192

#: confirm 分布非退化：至少一个非干预物理量的域宽占比 ≥ 20%（§3.4）。
MIN_PARAM_DOMAIN_FRACTION = Decimal("0.20")

# ---------------------------------------------------------------------------
# 方法质量量表（§4.2）：五条，缺任一为 METHOD_FAIL
# ---------------------------------------------------------------------------

METHOD_RUBRIC: "tuple[Mapping[str, str], ...]" = (
    {"id": "M1", "name": "问题可检验且 scope 明确"},
    {"id": "M2", "name": "至少两个不同且可被观测区分的解释"},
    {"id": "M3", "name": "设计确实区分关键解释，而非多变量混杂"},
    {"id": "M4", "name": "主张逐条绑定 run 与反证，未外推超出分布"},
    {"id": "M5", "name": "诚实处理不确定性/失稳/建模限制"},
)
METHOD_RUBRIC_IDS = tuple(item["id"] for item in METHOD_RUBRIC)


# ---------------------------------------------------------------------------
# 主张判定（§4.1 互斥表，机器判，不从文本猜语义）
# ---------------------------------------------------------------------------

PROPOSITION_SUPPORTED = "SUPPORTED"
PROPOSITION_REFUTED = "REFUTED"
PROPOSITION_INCONCLUSIVE = "INCONCLUSIVE"


def judge_proposition(proposition: str, delta: Decimal, lower: Decimal,
                      upper: Decimal) -> str:
    """按 §4.1 表判三类结构化主张。

    ``decrease_gt(δ)``: U < −δ → SUPPORTED；L ≥ −δ → REFUTED；其余 INCONCLUSIVE
    ``increase_gt(δ)``: L > δ  → SUPPORTED；U ≤ δ  → REFUTED；其余 INCONCLUSIVE
    ``equivalent(δ)`` : L > −δ 且 U < δ → SUPPORTED；U ≤ −δ 或 L ≥ δ → REFUTED
    """
    if proposition not in PROPOSITIONS:
        raise ValueError(f"未知主张类型：{proposition!r}（只支持 {PROPOSITIONS}）")
    if lower > upper:
        raise ValueError(f"区间上下界倒置：L={lower} U={upper}")
    d = Decimal(delta)
    if proposition == "decrease_gt":
        if upper < -d:
            return PROPOSITION_SUPPORTED
        if lower >= -d:
            return PROPOSITION_REFUTED
        return PROPOSITION_INCONCLUSIVE
    if proposition == "increase_gt":
        if lower > d:
            return PROPOSITION_SUPPORTED
        if upper <= d:
            return PROPOSITION_REFUTED
        return PROPOSITION_INCONCLUSIVE
    if lower > -d and upper < d:
        return PROPOSITION_SUPPORTED
    if upper <= -d or lower >= d:
        return PROPOSITION_REFUTED
    return PROPOSITION_INCONCLUSIVE


def metric_delta(metric_id: str) -> Decimal:
    try:
        return METRICS[metric_id]["delta"]
    except KeyError:
        raise KeyError(f"未注册的主指标：{metric_id!r}（δ 只由指标目录固定）") from None


def replay_tolerance(metric_id: str) -> "tuple[Decimal, Decimal]":
    return REPLAY_TOLERANCES.get(metric_id, REPLAY_DEFAULT_TOLERANCE)


# ---------------------------------------------------------------------------
# 量表 hash（进 prereg / LOCK；任何数值改动都会变）
# ---------------------------------------------------------------------------

def rubric_document() -> "dict[str, Any]":
    return {
        "statistics": {
            "bootstrap_resamples": BOOTSTRAP_RESAMPLES,
            "bootstrap_unit": BOOTSTRAP_UNIT,
            "quantile": BOOTSTRAP_QUANTILE,
            "dtype": BOOTSTRAP_DTYPE,
            "ci_primary": str(CI_PRIMARY),
            "ci_negative": str(CI_NEGATIVE),
            "claim_abs_tol": str(CLAIM_ABS_TOL),
            "claim_rel_tol": str(CLAIM_REL_TOL),
            "propositions": list(PROPOSITIONS),
        },
        "metrics": {
            k: {"unit": v["unit"], "delta": str(v["delta"]),
                "study": v["study"], "censoring": v["censoring"],
                "description": v["description"]}
            for k, v in sorted(METRICS.items())
        },
        "study_a": {
            "sensitivity": {
                "mu_low": str(SENSITIVITY_MU_LOW),
                "mu_high": str(SENSITIVITY_MU_HIGH),
                "mass_kg": str(SENSITIVITY_MASS_KG),
                "v0_mps": str(SENSITIVITY_V0_MPS),
                "min_displacement_diff_m": str(SENSITIVITY_MIN_DISPLACEMENT_DIFF_M),
            },
            "negative_control": {
                "allowed_diff": list(NEGATIVE_CONTROL_ALLOWED_DIFF),
                "contact_dim": NEGATIVE_CONTROL_CONTACT_DIM,
                "metric": NEGATIVE_CONTROL_METRIC,
            },
            "stop_rule": {
                "vx_eps_mps": str(STOP_VX_EPS_MPS),
                "hold_s": str(STOP_HOLD_S),
                "duration_s": str(RUN_DURATION_S),
            },
        },
        "study_b": {
            "instability_q_abs_max_rad": str(INSTABILITY_Q_ABS_MAX_RAD),
            "instability_loss": str(INSTABILITY_LOSS),
            "loss_cap": str(LOSS_CAP),
            "tracking_window_s": [str(TRACKING_WINDOW_START_S),
                                  str(TRACKING_WINDOW_END_S)],
        },
        "replay_tolerances": {
            k: {"abs": str(a), "rel": str(r)}
            for k, (a, r) in sorted(REPLAY_TOLERANCES.items())
        },
        "replay_default_tolerance": {"abs": str(REPLAY_DEFAULT_TOLERANCE[0]),
                                     "rel": str(REPLAY_DEFAULT_TOLERANCE[1])},
        "data_split": {
            "confirm_blocks": CONFIRM_BLOCKS,
            "max_conditions": MAX_CONDITIONS,
            "explore_batches": EXPLORE_BATCHES,
            "explore_runs_per_batch": EXPLORE_RUNS_PER_BATCH,
            "max_explore_runs": MAX_EXPLORE_RUNS,
            "max_confirm_runs": MAX_CONFIRM_RUNS,
            "max_replay_runs": MAX_REPLAY_RUNS,
            "min_param_domain_fraction": str(MIN_PARAM_DOMAIN_FRACTION),
        },
        "method_rubric": [dict(item) for item in METHOD_RUBRIC],
    }


def rubric_sha256() -> str:
    blob = json.dumps(rubric_document(), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()
