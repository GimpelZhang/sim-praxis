"""RSI-R1 单源合同：类型、公开 schema、参数/单位/指标/错误规则的唯一来源。

Plan §5.1 的三条硬性要求：
  * 拒 unknown keys、NaN/Inf、bool 当数值、重复 id、路径穿越/链接；
  * canonical JSON 固定排序/分隔/UTF-8，hash 包含 ``schema_version``；
  * 不用 ``dict.get(..., default)`` 替缺必填字段。

本阶段的代码全部是**纯函数 + 不可变 dataclass**：不触网、不调用模型、不访问
仿真器、不写任何旧根。预算表的数值唯一真值在 ``orchestrator/rsi_r1/lanes.py``，
本模块只做**结构校验与派生**，绝不另写一份 lane 字面量（AC02 机判比对处）。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from orchestrator.rsi_r1 import profiles as _p12
from orchestrator.rsi_r1.lanes import (
    LANE_ORDER,
    LANE_POST_CAPS,
    LANE_TASK_ID,
    MAX_AUX_STEPS,
    MAX_CONFIRM_RUNS_PER_STUDY,
    MAX_EXPLORE_RUNS_PER_STUDY,
    MAX_REPLAY_RUNS_PER_STUDY,
    MAX_STUDY_STEPS,
    MAX_TOTAL_RUNS,
    MAX_TOTAL_STEPS,
    RSI_R1_HARD_CAP,
    RSI_R1_NORMAL_BUDGET,
    RSI_R1_TASK_CAPS,
    assert_lane_table_consistent,
)

SCHEMA_PREFIX = "simpraxis.rsi_r1"
SCHEMA_VERSION = 1

BUDGET_SCHEMA = f"{SCHEMA_PREFIX}.budget.v1"
ENVELOPE_SCHEMA = f"{SCHEMA_PREFIX}.envelope.v1"

#: 预算的「整轮帽」是 100 USD 等值（Plan §8.3「费用帽建议100 USD等值」）。
MAX_USD_LITERAL = Decimal("100")

#: envelope / campaign id 的 ASCII 白名单（与 runtime_paths 同口径）。
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

#: 拒绝码 → 公开条款 id（Plan §5.1「每个拒绝码有公开条款id」）。
#: 这是**唯一**映射表；公开渲染与错误路径都从这里取，不各写一份。
REFUSAL_CLAUSES: "Mapping[str, str]" = {
    "UNKNOWN_KEY": "R1-S1-01",
    "MISSING_KEY": "R1-S1-02",
    "WRONG_TYPE": "R1-S1-03",
    "NAN_OR_INF": "R1-S1-04",
    "BOOL_AS_NUMBER": "R1-S1-05",
    "DUPLICATE_ID": "R1-S1-06",
    "PATH_TRAVERSAL": "R1-S1-07",
    "SCHEMA_MISMATCH": "R1-S1-08",
    "BUDGET_DRIFT": "R1-S1-09",
    "LANE_UNKNOWN": "R1-S1-10",
    "CAP_EXCEEDED": "R1-S1-11",
    "ENVELOPE_ID_INVALID": "R1-S1-12",
    "USD_INVALID": "R1-S1-13",
    # §8.3 费用/授权门（AC01）
    "PAID_NOT_AUTHORIZED": "R1-S8-01",
    "CONSENT_STALE": "R1-S8-02",
    # §1.1 读数/参数形状（AC05）
    "FLOAT_INPUT": "R1-S1-14",
    "NON_NUMERIC": "R1-S1-15",
    "NONFINITE_PARAM": "R1-S1-16",
    "NONFINITE": "R1-S1-16",
    # §3 场景/条件/分布（AC05/AC08）
    "UNKNOWN_SCENE": "R1-S3-01",
    "UNKNOWN_PARAM": "R1-S3-02",
    "MISSING_PARAM": "R1-S3-03",
    "DOMAIN": "R1-S3-04",
    "ENUM_INVALID": "R1-S3-05",
    "TICK_NONDIVISIBLE": "R1-S3-06",
    "STEP_LIMIT": "R1-S3-07",
    "CONDITION_LIMIT": "R1-S3-08",
    "DISTRIBUTION_DEGENERATE": "R1-S3-09",
    "DISTRIBUTION": "R1-S3-10",
    "DESIGN_NOT_SEPARABLE": "R1-S3-11",
    "HYPOTHESIS_INSUFFICIENT": "R1-S3-12",
    # §4.1 主张/量表（AC09）
    "METRIC_UNREGISTERED": "R1-S4-01",
    "DELTA_DRIFT": "R1-S4-02",
    "NEGATIVE_CONTROL_MISSING": "R1-S4-03",
    "PLAN_PHASE_INVALID": "R1-S4-04",
    "SOURCE_SHA_INVALID": "R1-S4-05",
    # §5.3/§5.4 候选产物与批执行（AC15–AC18；T3 起）
    "MALFORMED_JSON": "R1-S1-17",
    "STUDY_MISMATCH": "R1-S5-01",
    "PLAN_MISMATCH": "R1-S5-02",
    "CONDITION_UNKNOWN": "R1-S5-03",
    "RESOURCE_EXCEEDED": "R1-S5-04",
    "ENUM_NOT_SAMPLEABLE": "R1-S5-05",
    "SPEC_TAMPERED": "R1-S5-06",
    "CONFIRM_BLOCK_COUNT": "R1-S5-07",
    "MAX_CONDITIONS": "R1-S5-08",
    "SEED_INVALID": "R1-S5-09",
    "SCENE_MISMATCH": "R1-S5-10",
    # ---- RSI-R1.2（Plan §1/§1.1/§2；新 profile 段）----
    "PROFILE_UNKNOWN": "R12-S1-01",
    "PROFILE_MISMATCH": "R12-S1-02",
    "FAMILY_CAP_EXCEEDED": "R12-S1-03",
    "FAMILY_RESERVATION_MISMATCH": "R12-S1-04",
    "FAMILY_ROUND_OPEN": "R12-S1-05",
    "FAMILY_PERMIT_INVALID": "R12-S1-06",
    "HISTORY_DRIFT": "R12-S1-07",
    "HISTORY_UNAVAILABLE": "R12-S1-08",
    "COST_GATE_BLOCKED": "R12-S1-09",
    "OFFLINE_AUTH_REQUIRED": "R12-S1-10",
    # 族账自身完整性（family.py 可抛；注册即公开条款，不留给运行时裸码）
    "FAMILY_USAGE_INVALID": "R12-S1-11",
    "FAMILY_CORRUPT": "R12-S1-12",
    "FAMILY_ABSENT": "R12-S1-13",
    "FAMILY_CAP_MISMATCH": "R12-S1-14",
    "FAMILY_PROFILE_MISMATCH": "R12-S1-15",
    "PROFILE_NOT_ELIGIBLE": "R12-S1-16",
    "PUBLIC_CONTRACT_INVALID": "R12-S2-01",
    # ---- RSI-R1.2 T1（Plan §2.1/§2.2；资源账与最终请求审计）----
    "RESOURCE_LEDGER_TAMPERED": "R12-S2-02",
    "RESOURCE_LEDGER_CORRUPT": "R12-S2-03",
    "BUDGET_UNREACHABLE": "R12-S2-04",
    "PUBLIC_CONTRACT_MISSING": "R12-S2-05",
    "PUBLIC_CONTRACT_DRIFT": "R12-S2-06",
    "PUBLIC_CONTRACT_PRIVATE": "R12-S2-07",
    "DYNAMIC_REF_INVALID": "R12-S2-08",
}


class ContractError(ValueError):
    """RSI-R1 合同拒绝（结构化：``code`` 指向公开条款 id）。"""

    def __init__(self, code: str, detail: str) -> None:
        if code not in REFUSAL_CLAUSES:
            raise KeyError(f"未登记的拒绝码：{code!r}")
        self.code = code
        self.clause = REFUSAL_CLAUSES[code]
        self.detail = detail
        super().__init__(f"[{code}/{self.clause}] {detail}")


# ---------------------------------------------------------------------------
# canonical JSON（Plan §5.1）
# ---------------------------------------------------------------------------

def canonical_json(obj: Any) -> str:
    """固定排序/分隔/UTF-8 的 canonical JSON；NaN/Inf 一律拒。"""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def canonical_bytes(obj: Any) -> bytes:
    return canonical_json(obj).encode("utf-8")


def canonical_sha256(obj: Any) -> str:
    return hashlib.sha256(canonical_bytes(obj)).hexdigest()


def document_hash(doc: Mapping[str, Any]) -> str:
    """文档 hash：**包含** schema_version（Plan §5.1 逐字要求）。"""
    payload = dict(doc)
    if "schema_version" not in payload:
        raise ContractError("MISSING_KEY", "文档缺 schema_version，不可计算 hash")
    return canonical_sha256(payload)


# ---------------------------------------------------------------------------
# 严格字段读取（缺必填即拒，绝不 .get 兜底）
# ---------------------------------------------------------------------------

def require_object(value: Any, *, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractError("WRONG_TYPE", f"{where} 必须是对象，实为 {type(value).__name__}")
    return value


def require_exact_keys(obj: Mapping[str, Any], expected: "set[str] | frozenset[str]",
                       *, where: str) -> None:
    got = set(obj)
    unknown = got - set(expected)
    if unknown:
        raise ContractError("UNKNOWN_KEY", f"{where} 含未登记键：{sorted(unknown)}")
    missing = set(expected) - got
    if missing:
        raise ContractError("MISSING_KEY", f"{where} 缺必填键：{sorted(missing)}")


def require_int(value: Any, *, where: str, min_value: int | None = None,
                max_value: int | None = None) -> int:
    if isinstance(value, bool):
        # bool 是 int 的子类：单独拒绝，避免 True 被当成 1 混进预算表。
        raise ContractError("BOOL_AS_NUMBER", f"{where} 不得是 bool")
    if not isinstance(value, int):
        raise ContractError("WRONG_TYPE", f"{where} 必须是整数，实为 {type(value).__name__}")
    if min_value is not None and value < min_value:
        raise ContractError("CAP_EXCEEDED", f"{where}={value} < 下界 {min_value}")
    if max_value is not None and value > max_value:
        raise ContractError("CAP_EXCEEDED", f"{where}={value} > 上界 {max_value}")
    return value


def require_str(value: Any, *, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise ContractError("WRONG_TYPE", f"{where} 必须是非空字符串")
    return value


def require_safe_id(value: Any, *, where: str) -> str:
    s = require_str(value, where=where)
    if not _ID_RE.match(s):
        raise ContractError("ENVELOPE_ID_INVALID",
                            f"{where}={s!r} 不是安全 id（ASCII 字母数字 . _ -，≤128）")
    return s


def require_no_nan(value: Any, *, where: str) -> None:
    """递归检查文档内无 NaN/Inf，且浮点数不是 bool。"""
    if isinstance(value, bool):
        return
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ContractError("NAN_OR_INF", f"{where} 含 NaN/Inf")
        return
    if isinstance(value, Mapping):
        for k, v in value.items():
            require_no_nan(v, where=f"{where}.{k}")
        return
    if isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            require_no_nan(v, where=f"{where}[{i}]")


def require_decimal(value: Any, *, where: str) -> Decimal:
    """小数只接受字符串或 int（**不接受 float**，避免二进制舍入漂移）。"""
    if isinstance(value, bool):
        raise ContractError("BOOL_AS_NUMBER", f"{where} 不得是 bool")
    if isinstance(value, float):
        raise ContractError("USD_INVALID",
                            f"{where} 不得用 float 表示金额（用十进制字符串）")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        try:
            d = Decimal(value)
        except InvalidOperation as exc:
            raise ContractError("USD_INVALID", f"{where}={value!r} 不是十进制数") from exc
        if not d.is_finite():
            raise ContractError("USD_INVALID", f"{where}={value!r} 非有限")
        return d
    raise ContractError("WRONG_TYPE", f"{where} 必须是十进制字符串或整数")


# ---------------------------------------------------------------------------
# BudgetSpec（Plan §5.1；AC02 的唯一真值）
# ---------------------------------------------------------------------------

BUDGET_KEYS = frozenset({
    "schema", "schema_version", "envelope_id",
    "posts_by_lane", "max_usd", "max_runs_by_lane", "max_steps",
})


@dataclass(frozen=True)
class BudgetSpec:
    """整轮预算信封：五条 lane 的 POST 帽 + 运行/步数帽 + 美元帽。

    ``posts_by_lane`` 必须与 ``lanes.LANE_POST_CAPS`` **逐 lane 相等**——
    这是 AC02「单真值 144 与分 lane 完全一致」的实现点，任何漂移都当场拒。
    ``max_runs_by_lane`` 是 Plan §8.2 的**每 study 阶段帽**（explore 64 /
    confirm 192 / replay 192）；两 study 合计由 ``max_total_runs`` 派生。
    """

    schema: str
    envelope_id: str
    posts_by_lane: Mapping[str, int]
    max_usd: Decimal
    max_runs_by_lane: Mapping[str, int]
    max_steps: int

    # ---- 构造与校验 ----

    def __post_init__(self) -> None:  # dataclass(frozen) 仍可就地校验
        assert_lane_table_consistent()
        if self.schema != BUDGET_SCHEMA:
            raise ContractError("SCHEMA_MISMATCH",
                                f"schema 必须是 {BUDGET_SCHEMA}，实为 {self.schema!r}")
        require_safe_id(self.envelope_id, where="envelope_id")
        if not isinstance(self.max_usd, Decimal):
            raise ContractError("USD_INVALID", "max_usd 必须是 Decimal")
        if not self.max_usd.is_finite() or self.max_usd <= 0:
            raise ContractError("USD_INVALID", f"max_usd={self.max_usd} 必须为正有限值")
        if self.max_usd > MAX_USD_LITERAL:
            raise ContractError("CAP_EXCEEDED",
                                f"max_usd={self.max_usd} 超过整轮费用帽 {MAX_USD_LITERAL}")
        got_lanes = tuple(self.posts_by_lane)
        if set(got_lanes) != set(LANE_ORDER):
            raise ContractError("LANE_UNKNOWN",
                                f"posts_by_lane 的 lane 集合必须是 {LANE_ORDER}，"
                                f"实为 {got_lanes}")
        for lane in LANE_ORDER:
            cap = require_int(self.posts_by_lane[lane], where=f"posts_by_lane.{lane}",
                              min_value=0)
            if cap != LANE_POST_CAPS[lane]:
                raise ContractError(
                    "BUDGET_DRIFT",
                    f"posts_by_lane.{lane}={cap} 与 lane 表 {LANE_POST_CAPS[lane]} 不符"
                    "（Plan §8.1 单真值，禁止本地改写）")
        total = sum(self.posts_by_lane[lane] for lane in LANE_ORDER)
        if total != RSI_R1_HARD_CAP:
            raise ContractError("BUDGET_DRIFT",
                                f"lane 帽合计 {total} != {RSI_R1_HARD_CAP}")
        expected_runs = {
            "explore": MAX_EXPLORE_RUNS_PER_STUDY,
            "confirm": MAX_CONFIRM_RUNS_PER_STUDY,
            "replay": MAX_REPLAY_RUNS_PER_STUDY,
        }
        if dict(self.max_runs_by_lane) != expected_runs:
            raise ContractError(
                "BUDGET_DRIFT",
                f"max_runs_by_lane={dict(self.max_runs_by_lane)} 与 Plan §8.2 "
                f"{expected_runs} 不符")
        if self.max_steps != MAX_TOTAL_STEPS:
            raise ContractError(
                "BUDGET_DRIFT",
                f"max_steps={self.max_steps} 与 Plan §8.2 {MAX_TOTAL_STEPS} 不符")
        require_int(self.max_steps, where="max_steps", min_value=1)

    # ---- 派生量 ----

    @property
    def total_posts(self) -> int:
        return sum(self.posts_by_lane[lane] for lane in LANE_ORDER)

    @property
    def normal_posts(self) -> int:
        return self.total_posts - self.posts_by_lane["repair_reserve"]

    @property
    def max_total_runs(self) -> int:
        return 2 * sum(self.max_runs_by_lane.values())

    def slots(self) -> "list[dict[str, Any]]":
        """展开成逐槽表（Plan §8.1「预算数据唯一源…导出槽表」）。

        每个 POST 槽一个 dict；槽号从 1 起，顺序固定为 lane 表序。
        """
        out: "list[dict[str, Any]]" = []
        n = 0
        for lane in LANE_ORDER:
            for i in range(self.posts_by_lane[lane]):
                n += 1
                out.append({"slot": n, "lane": lane, "lane_task_id": LANE_TASK_ID[lane],
                            "lane_index": i + 1,
                            "reserve": lane == "repair_reserve"})
        return out

    def to_dict(self) -> "dict[str, Any]":
        return {
            "schema": self.schema,
            "schema_version": SCHEMA_VERSION,
            "envelope_id": self.envelope_id,
            "posts_by_lane": {lane: self.posts_by_lane[lane] for lane in LANE_ORDER},
            "max_usd": str(self.max_usd),
            "max_runs_by_lane": dict(self.max_runs_by_lane),
            "max_steps": self.max_steps,
        }

    @property
    def sha256(self) -> str:
        return document_hash(self.to_dict())

    @classmethod
    def from_dict(cls, obj: Any) -> "BudgetSpec":
        obj = require_object(obj, where="budget")
        require_no_nan(obj, where="budget")
        require_exact_keys(obj, BUDGET_KEYS, where="budget")
        if obj["schema_version"] != SCHEMA_VERSION:
            raise ContractError("SCHEMA_MISMATCH",
                                f"schema_version={obj['schema_version']!r} != {SCHEMA_VERSION}")
        posts = require_object(obj["posts_by_lane"], where="budget.posts_by_lane")
        runs = require_object(obj["max_runs_by_lane"], where="budget.max_runs_by_lane")
        require_no_nan(runs, where="budget.max_runs_by_lane")
        return cls(
            schema=require_str(obj["schema"], where="budget.schema"),
            envelope_id=require_safe_id(obj["envelope_id"], where="budget.envelope_id"),
            posts_by_lane={lane: require_int(posts[lane], where=f"posts_by_lane.{lane}")
                           for lane in LANE_ORDER},
            max_usd=require_decimal(obj["max_usd"], where="budget.max_usd"),
            max_runs_by_lane={k: require_int(v, where=f"max_runs_by_lane.{k}")
                              for k, v in runs.items()},
            max_steps=require_int(obj["max_steps"], where="budget.max_steps"),
        )

    def to_json(self) -> str:
        return canonical_json(self.to_dict())


def default_budget(envelope_id: str) -> BudgetSpec:
    """按 Plan §8.1/§8.2 的字面值构造**唯一**默认预算。

    这是唯一允许生成 BudgetSpec 的工厂；任何调用方都必须显式给 envelope_id，
    不存在「缺省 envelope」。
    """
    return BudgetSpec(
        schema=BUDGET_SCHEMA,
        envelope_id=require_safe_id(envelope_id, where="envelope_id"),
        posts_by_lane={lane: LANE_POST_CAPS[lane] for lane in LANE_ORDER},
        max_usd=MAX_USD_LITERAL,
        max_runs_by_lane={
            "explore": MAX_EXPLORE_RUNS_PER_STUDY,
            "confirm": MAX_CONFIRM_RUNS_PER_STUDY,
            "replay": MAX_REPLAY_RUNS_PER_STUDY,
        },
        max_steps=MAX_TOTAL_STEPS,
    )


# ---------------------------------------------------------------------------
# AC02：单真值对账（预算 ↔ 账本命名空间 ↔ 派生文本）
# ---------------------------------------------------------------------------

def assert_single_truth(budget: BudgetSpec, *,
                        ledger_task_caps: Mapping[int, int],
                        ledger_hard_cap: int,
                        ledger_normal_budget: int) -> None:
    """AC02 机判核心：四条独立来源必须逐项相等，否则拒。

    ``ledger_*`` 由调用方从 ``QuotaLedger`` 命名空间取（不经默认参数，
    避免「表自己加起来对」式的自我证明）。
    """
    if dict(ledger_task_caps) != dict(RSI_R1_TASK_CAPS):
        raise ContractError(
            "BUDGET_DRIFT",
            f"账本 task_caps={dict(ledger_task_caps)} != lanes.RSI_R1_TASK_CAPS="
            f"{dict(RSI_R1_TASK_CAPS)}")
    if ledger_hard_cap != RSI_R1_HARD_CAP:
        raise ContractError("BUDGET_DRIFT",
                            f"账本硬帽 {ledger_hard_cap} != {RSI_R1_HARD_CAP}")
    if ledger_normal_budget != RSI_R1_NORMAL_BUDGET:
        raise ContractError("BUDGET_DRIFT",
                            f"账本正常预算 {ledger_normal_budget} != {RSI_R1_NORMAL_BUDGET}")
    if budget.total_posts != ledger_hard_cap:
        raise ContractError("BUDGET_DRIFT",
                            f"预算槽数 {budget.total_posts} != 账本硬帽 {ledger_hard_cap}")
    if budget.normal_posts != ledger_normal_budget:
        raise ContractError("BUDGET_DRIFT",
                            f"预算正常槽 {budget.normal_posts} != {ledger_normal_budget}")
    # R0.2 的历史双合同（224 vs 228）绝不能被混进来当 R1 的真值。
    if budget.total_posts in (224, 228):
        raise ContractError("BUDGET_DRIFT",
                            f"检测到 RSI-R0.x 旧合同数值 {budget.total_posts} 混入 R1 预算")


def assert_run_budget_consistent() -> None:
    """Plan §8.2 的步数不变量（AC02 附项）。"""
    assert_lane_table_consistent()
    if MAX_STUDY_STEPS + MAX_AUX_STEPS > MAX_TOTAL_STEPS:
        raise ContractError("BUDGET_DRIFT", "study+aux 步数超过总上限")
    if MAX_TOTAL_RUNS != 2 * (MAX_EXPLORE_RUNS_PER_STUDY
                              + MAX_CONFIRM_RUNS_PER_STUDY
                              + MAX_REPLAY_RUNS_PER_STUDY):
        raise ContractError("BUDGET_DRIFT", "两 study 运行数不等于 2×(64+192+192)")


# ---------------------------------------------------------------------------
# BudgetSpecV2 / ConsentV2（RSI-R1.2 profile；T0）
#
# 旧 schema（budget.v1 / consent.v1）逐字节不变：旧 golden、旧 LOCK、旧判器继续
# 走 ``BudgetSpec``/``Consent``。新 profile 的解析必须**显式声明** schema，选择器
# ``budget_from_dict`` / ``consent_from_dict`` 按 schema 分派，未知 schema 拒绝，
# 不做「猜一个最像的」回退。
#
# 数值单一真值在 ``orchestrator.rsi_r1.profiles``（版本化 profile）；
# 本段只做形状/绑定校验，不再写第二份字面量表（AC04）。
# ---------------------------------------------------------------------------

BUDGET_KEYS_V2 = frozenset({
    "schema", "schema_version", "envelope_id", "profile_id",
    "posts_by_lane", "max_usd", "max_runs_by_lane", "max_steps",
    "aux_subenvelopes",
})


@dataclass(frozen=True)
class BudgetSpecV2:
    """R1.2 预算信封：四条 lane（无 repair_reserve）+ 资源帽 + aux 子信封。

    与 ``BudgetSpec`` 的差异（Plan §1.1）：
    * lane 表 12/16/48/48=124，**无** ``repair_reserve``；
    * 正常预算 == 硬帽（付费后无本轮修复入口）；
    * 新增 ``aux_subenvelopes``（五子信封合计恰 ``profiles.AUX_SUBENVELOPE_TOTAL``）；
    * 新增 ``profile_id``（必须显式 ``rsi-r1.2``）。
    """

    schema: str
    envelope_id: str
    profile_id: str
    posts_by_lane: Mapping[str, int]
    max_usd: Decimal
    max_runs_by_lane: Mapping[str, int]
    max_steps: int
    aux_subenvelopes: Mapping[str, int]

    def __post_init__(self) -> None:
        _p12.assert_r12_table_consistent()
        if self.schema != _p12.BUDGET_SCHEMA_V2:
            raise ContractError("SCHEMA_MISMATCH",
                                f"schema 必须是 {_p12.BUDGET_SCHEMA_V2}，实为 {self.schema!r}")
        if self.profile_id != _p12.PROFILE_R12:
            raise ContractError("PROFILE_MISMATCH",
                                f"profile_id 必须是 {_p12.PROFILE_R12}，实为 "
                                f"{self.profile_id!r}")
        require_safe_id(self.envelope_id, where="envelope_id")
        if not isinstance(self.max_usd, Decimal):
            raise ContractError("USD_INVALID", "max_usd 必须是 Decimal")
        if not self.max_usd.is_finite() or self.max_usd <= 0:
            raise ContractError("USD_INVALID", f"max_usd={self.max_usd} 必须为正有限值")
        if self.max_usd > MAX_USD_LITERAL:
            raise ContractError("CAP_EXCEEDED",
                                f"max_usd={self.max_usd} 超过整轮费用帽 {MAX_USD_LITERAL}")
        got_lanes = tuple(self.posts_by_lane)
        if set(got_lanes) != set(_p12.LANE_ORDER_R12):
            raise ContractError("LANE_UNKNOWN",
                                f"posts_by_lane 的 lane 集合必须是 "
                                f"{_p12.LANE_ORDER_R12}，实为 {got_lanes}")
        if "repair_reserve" in self.posts_by_lane:
            raise ContractError("BUDGET_DRIFT",
                                "R1.2 无 repair_reserve；登记该 lane 即为旧合同混入")
        for lane in _p12.LANE_ORDER_R12:
            cap = require_int(self.posts_by_lane[lane], where=f"posts_by_lane.{lane}",
                              min_value=0)
            if cap != _p12.LANE_POST_CAPS_R12[lane]:
                raise ContractError(
                    "BUDGET_DRIFT",
                    f"posts_by_lane.{lane}={cap} 与 profile 表 "
                    f"{_p12.LANE_POST_CAPS_R12[lane]} 不符（禁止本地改写）")
        total = sum(self.posts_by_lane[lane] for lane in _p12.LANE_ORDER_R12)
        if total != _p12.RSI_R12_HARD_CAP:
            raise ContractError("BUDGET_DRIFT",
                                f"lane 帽合计 {total} != {_p12.RSI_R12_HARD_CAP}")
        expected_runs = {
            "explore": _p12.MAX_EXPLORE_RUNS_PER_STUDY,
            "confirm": _p12.MAX_CONFIRM_RUNS_PER_STUDY,
            "replay": _p12.MAX_REPLAY_RUNS_PER_STUDY,
        }
        if dict(self.max_runs_by_lane) != expected_runs:
            raise ContractError(
                "BUDGET_DRIFT",
                f"max_runs_by_lane={dict(self.max_runs_by_lane)} 与 R1.2 "
                f"{expected_runs} 不符")
        if self.max_steps != _p12.MAX_TOTAL_STEPS:
            raise ContractError(
                "BUDGET_DRIFT",
                f"max_steps={self.max_steps} 与 R1.2 {_p12.MAX_TOTAL_STEPS} 不符")
        require_int(self.max_steps, where="max_steps", min_value=1)
        expected_aux = dict(_p12.AUX_SUBENVELOPES)
        if dict(self.aux_subenvelopes) != expected_aux:
            raise ContractError(
                "BUDGET_DRIFT",
                f"aux_subenvelopes={dict(self.aux_subenvelopes)} 与 R1.2 "
                f"{expected_aux} 不符")
        if sum(self.aux_subenvelopes.values()) != _p12.AUX_SUBENVELOPE_TOTAL:
            raise ContractError(
                "BUDGET_DRIFT",
                f"aux 子信封合计不等于 {_p12.AUX_SUBENVELOPE_TOTAL}")

    @property
    def total_posts(self) -> int:
        return sum(self.posts_by_lane[lane] for lane in _p12.LANE_ORDER_R12)

    @property
    def normal_posts(self) -> int:
        # 无 repair_reserve：正常预算即硬帽。
        return self.total_posts

    @property
    def max_total_runs(self) -> int:
        return 2 * sum(self.max_runs_by_lane.values())

    @property
    def aux_total_steps(self) -> int:
        return sum(self.aux_subenvelopes.values())

    def slots(self) -> "list[dict[str, Any]]":
        out: "list[dict[str, Any]]" = []
        n = 0
        for lane in _p12.LANE_ORDER_R12:
            for i in range(self.posts_by_lane[lane]):
                n += 1
                out.append({"slot": n, "lane": lane,
                            "lane_task_id": _p12.LANE_TASK_ID_R12[lane],
                            "lane_index": i + 1, "reserve": False})
        return out

    def to_dict(self) -> "dict[str, Any]":
        return {
            "schema": self.schema,
            "schema_version": _p12.SCHEMA_VERSION,
            "envelope_id": self.envelope_id,
            "profile_id": self.profile_id,
            "posts_by_lane": {lane: self.posts_by_lane[lane]
                              for lane in _p12.LANE_ORDER_R12},
            "max_usd": str(self.max_usd),
            "max_runs_by_lane": dict(self.max_runs_by_lane),
            "max_steps": self.max_steps,
            "aux_subenvelopes": dict(self.aux_subenvelopes),
        }

    @property
    def sha256(self) -> str:
        return document_hash(self.to_dict())

    @classmethod
    def from_dict(cls, obj: Any) -> "BudgetSpecV2":
        obj = require_object(obj, where="budget")
        require_no_nan(obj, where="budget")
        require_exact_keys(obj, BUDGET_KEYS_V2, where="budget")
        if obj["schema_version"] != _p12.SCHEMA_VERSION:
            raise ContractError("SCHEMA_MISMATCH",
                                f"schema_version={obj['schema_version']!r} != "
                                f"{_p12.SCHEMA_VERSION}")
        posts = require_object(obj["posts_by_lane"], where="budget.posts_by_lane")
        runs = require_object(obj["max_runs_by_lane"], where="budget.max_runs_by_lane")
        require_no_nan(runs, where="budget.max_runs_by_lane")
        aux = require_object(obj["aux_subenvelopes"], where="budget.aux_subenvelopes")
        require_no_nan(aux, where="budget.aux_subenvelopes")
        return cls(
            schema=require_str(obj["schema"], where="budget.schema"),
            envelope_id=require_safe_id(obj["envelope_id"], where="budget.envelope_id"),
            profile_id=require_str(obj["profile_id"], where="budget.profile_id"),
            posts_by_lane={lane: require_int(posts[lane], where=f"posts_by_lane.{lane}")
                           for lane in _p12.LANE_ORDER_R12},
            max_usd=require_decimal(obj["max_usd"], where="budget.max_usd"),
            max_runs_by_lane={k: require_int(v, where=f"max_runs_by_lane.{k}")
                              for k, v in runs.items()},
            max_steps=require_int(obj["max_steps"], where="budget.max_steps"),
            aux_subenvelopes={k: require_int(v, where=f"aux_subenvelopes.{k}")
                              for k, v in aux.items()},
        )

    def to_json(self) -> str:
        return canonical_json(self.to_dict())


def default_budget_v2(envelope_id: str) -> BudgetSpecV2:
    """按 R1.2 profile 的字面值构造**唯一**默认预算；无缺省 envelope。"""
    return BudgetSpecV2(
        schema=_p12.BUDGET_SCHEMA_V2,
        envelope_id=require_safe_id(envelope_id, where="envelope_id"),
        profile_id=_p12.PROFILE_R12,
        posts_by_lane={lane: _p12.LANE_POST_CAPS_R12[lane]
                       for lane in _p12.LANE_ORDER_R12},
        max_usd=Decimal(_p12.MAX_USD_LITERAL_DECIMAL),
        max_runs_by_lane={
            "explore": _p12.MAX_EXPLORE_RUNS_PER_STUDY,
            "confirm": _p12.MAX_CONFIRM_RUNS_PER_STUDY,
            "replay": _p12.MAX_REPLAY_RUNS_PER_STUDY,
        },
        max_steps=_p12.MAX_TOTAL_STEPS,
        aux_subenvelopes=dict(_p12.AUX_SUBENVELOPES),
    )


def budget_from_dict(obj: Any) -> "BudgetSpec | BudgetSpecV2":
    """按 ``schema`` 分派预算解析；未知/缺失 schema 一律拒绝。"""
    obj = require_object(obj, where="budget")
    schema = obj.get("schema")
    if schema == BUDGET_SCHEMA:
        return BudgetSpec.from_dict(obj)
    if schema == _p12.BUDGET_SCHEMA_V2:
        return BudgetSpecV2.from_dict(obj)
    raise ContractError("SCHEMA_MISMATCH",
                        f"budget.schema={schema!r} 未登记（合法：{BUDGET_SCHEMA!r} / "
                        f"{_p12.BUDGET_SCHEMA_V2!r}）")


def assert_single_truth_v2(budget: BudgetSpecV2, *,
                           ledger_task_caps: Mapping[int, int],
                           ledger_hard_cap: int,
                           ledger_normal_budget: int,
                           family_cap: int) -> None:
    """AC04/AC07 机判：预算 ↔ 账本命名空间 ↔ profile ↔ 族帽 四源逐项相等。"""
    if dict(ledger_task_caps) != dict(_p12.RSI_R12_TASK_CAPS):
        raise ContractError(
            "BUDGET_DRIFT",
            f"账本 task_caps={dict(ledger_task_caps)} != profile "
            f"{dict(_p12.RSI_R12_TASK_CAPS)}")
    if ledger_hard_cap != _p12.RSI_R12_HARD_CAP:
        raise ContractError("BUDGET_DRIFT",
                            f"账本硬帽 {ledger_hard_cap} != {_p12.RSI_R12_HARD_CAP}")
    if ledger_normal_budget != _p12.RSI_R12_NORMAL_BUDGET:
        raise ContractError("BUDGET_DRIFT",
                            f"账本正常预算 {ledger_normal_budget} != "
                            f"{_p12.RSI_R12_NORMAL_BUDGET}")
    if budget.total_posts != ledger_hard_cap:
        raise ContractError("BUDGET_DRIFT",
                            f"预算槽数 {budget.total_posts} != 账本硬帽 {ledger_hard_cap}")
    if budget.normal_posts != ledger_normal_budget:
        raise ContractError("BUDGET_DRIFT",
                            f"预算正常槽 {budget.normal_posts} != {ledger_normal_budget}")
    if family_cap != _p12.FAMILY_CAP:
        raise ContractError("BUDGET_DRIFT",
                            f"族帽 {family_cap} != {_p12.FAMILY_CAP}")
    if family_cap != _p12.FAMILY_HISTORY_POSTS + budget.total_posts:
        raise ContractError(
            "BUDGET_DRIFT",
            f"族帽 {family_cap} != 历史 {_p12.FAMILY_HISTORY_POSTS} + 本轮 "
            f"{budget.total_posts}")
    # 旧合同数值（144/224/228）绝不能被混进来当 R1.2 真值。
    if budget.total_posts in (144, 224, 228):
        raise ContractError("BUDGET_DRIFT",
                            f"检测到旧合同数值 {budget.total_posts} 混入 R1.2 预算")


# ---------------------------------------------------------------------------
# 费用/授权决定（Plan §8.3；AC01）
# ---------------------------------------------------------------------------
#
# §8.3 原文：「T0费用决策只有两类：取得当前端点适用且含在途费用的供应硬保证；
# 或用户对 **R1 范围** 明确风险豁免。旧 R0.2 豁免不承继。本 Plan 批准只批准设计，
# 不自动豁免费用门。」
#
# 因此本模块把「决定」做成显式枚举 + 显式 scope 字段：任何缺少 R1 范围声明的
# 文档都拒收，从而 R0.2 的旧豁免书（schema 不同、scope 不同）不可能被误当授权。

CONSENT_SCHEMA = f"{SCHEMA_PREFIX}.consent.v1"
COST_DECISION_SUPPLY = "SUPPLY_HARD_GUARANTEE"
COST_DECISION_WAIVER = "R1_SCOPE_RISK_WAIVER"
COST_DECISION_BLOCKED = "BLOCKED"
COST_DECISIONS = (COST_DECISION_SUPPLY, COST_DECISION_WAIVER, COST_DECISION_BLOCKED)

CONSENT_KEYS = frozenset({
    "schema", "schema_version", "envelope_id", "decision", "scope",
    "granted_at_utc", "granted_by", "verbatim", "plan_path", "plan_sha256",
    "supersedes", "notes",
})


@dataclass(frozen=True)
class Consent:
    """费用门决定书：供应硬保证 / R1 范围风险豁免 / 阻断 三者之一。

    ``verbatim`` 必须是授权方原话（逐字），不得由执行方改写；``plan_sha256``
    把决定绑定到**这一版** R1 Plan 文件，防止用旧授权套新计划。
    """

    schema: str
    envelope_id: str
    decision: str
    scope: str
    granted_at_utc: str
    granted_by: str
    verbatim: str
    plan_path: str
    plan_sha256: str
    supersedes: "tuple[str, ...]"
    notes: str

    def __post_init__(self) -> None:
        if self.schema != CONSENT_SCHEMA:
            raise ContractError("SCHEMA_MISMATCH",
                                f"consent schema 必须是 {CONSENT_SCHEMA}，实为 {self.schema!r}")
        if self.decision not in COST_DECISIONS:
            raise ContractError("PAID_NOT_AUTHORIZED",
                                f"decision={self.decision!r} 不在 {COST_DECISIONS}")
        if self.scope != "R1":
            # 旧 R0.2 豁免的 scope 是 "R0.2"：这里逐字拒收，落实「旧豁免不承继」。
            raise ContractError("CONSENT_STALE",
                                f"scope={self.scope!r} != 'R1'：旧 R0.x 豁免不承继（§8.3）")
        require_safe_id(self.envelope_id, where="consent.envelope_id")
        for name in ("granted_at_utc", "granted_by", "plan_path", "plan_sha256"):
            require_str(getattr(self, name), where=f"consent.{name}")
        if len(self.plan_sha256) != 64 or any(
                c not in "0123456789abcdef" for c in self.plan_sha256):
            raise ContractError("CONSENT_STALE",
                                "consent.plan_sha256 必须是 64 位小写十六进制")
        if self.decision == COST_DECISION_WAIVER:
            # 豁免必须带原话；空白原话等于没有授权。
            if not self.verbatim.strip():
                raise ContractError("PAID_NOT_AUTHORIZED",
                                    "R1 范围风险豁免必须携带授权方原话 verbatim")
        if not isinstance(self.supersedes, tuple):
            raise ContractError("WRONG_TYPE", "consent.supersedes 必须是字符串数组")

    def to_dict(self) -> "dict[str, Any]":
        return {
            "schema": self.schema,
            "schema_version": SCHEMA_VERSION,
            "envelope_id": self.envelope_id,
            "decision": self.decision,
            "scope": self.scope,
            "granted_at_utc": self.granted_at_utc,
            "granted_by": self.granted_by,
            "verbatim": self.verbatim,
            "plan_path": self.plan_path,
            "plan_sha256": self.plan_sha256,
            "supersedes": list(self.supersedes),
            "notes": self.notes,
        }

    @property
    def sha256(self) -> str:
        return document_hash(self.to_dict())

    @property
    def paid_authorized(self) -> bool:
        return self.decision in (COST_DECISION_SUPPLY, COST_DECISION_WAIVER)

    @classmethod
    def from_dict(cls, obj: Any) -> "Consent":
        obj = require_object(obj, where="consent")
        require_no_nan(obj, where="consent")
        require_exact_keys(obj, CONSENT_KEYS, where="consent")
        if obj["schema_version"] != SCHEMA_VERSION:
            raise ContractError("SCHEMA_MISMATCH",
                                f"consent.schema_version={obj['schema_version']!r} != {SCHEMA_VERSION}")
        sup = obj["supersedes"]
        if not isinstance(sup, (list, tuple)) or not all(isinstance(s, str) for s in sup):
            raise ContractError("WRONG_TYPE", "consent.supersedes 必须是字符串数组")
        return cls(
            schema=require_str(obj["schema"], where="consent.schema"),
            envelope_id=require_safe_id(obj["envelope_id"], where="consent.envelope_id"),
            decision=require_str(obj["decision"], where="consent.decision"),
            scope=require_str(obj["scope"], where="consent.scope"),
            granted_at_utc=require_str(obj["granted_at_utc"], where="consent.granted_at_utc"),
            granted_by=require_str(obj["granted_by"], where="consent.granted_by"),
            verbatim=str(obj["verbatim"] or ""),
            plan_path=require_str(obj["plan_path"], where="consent.plan_path"),
            plan_sha256=require_str(obj["plan_sha256"], where="consent.plan_sha256"),
            supersedes=tuple(sup),
            notes=str(obj["notes"] or ""),
        )


def assert_paid_authorized(consent: Consent) -> None:
    """AC01：无有效授权即 0 POST（调用点在 sender 内，不只在 CLI 入口）。"""
    if not consent.paid_authorized:
        raise ContractError(
            "PAID_NOT_AUTHORIZED",
            "费用门 BLOCKED：Plan §8.3 要求供应硬保证或 R1 范围风险豁免；"
            "零付费规划可做，但不得发网")


def verify_consent_binds_plan(consent: Consent, *, plan_path: str,
                              plan_sha256: str) -> None:
    """授权书必须绑定**当前**这一版 Plan 文件（防旧授权套新计划）。"""
    if consent.plan_sha256 != plan_sha256:
        raise ContractError(
            "CONSENT_STALE",
            f"consent.plan_sha256={consent.plan_sha256[:12]}… 与当前 Plan "
            f"{plan_sha256[:12]}… 不符")


# ---- ConsentV2（R1.2 费用门；Plan §1.1「新 scope R1.2」）----
#
# 旧 ``Consent``（scope "R1"）逐字节保留；R1.2 的豁免/硬保证必须用新 schema、
# 新 scope，并**显式**绑定：
#   * 新 Plan 字节 sha256；
#   * 新 profile id；
#   * 族帽 155 与历史两本源账的冻结 sha256（防「历史又是 31」口头承诺）；
#   * 费用风险处置（每 POST 预留/总暴露），旧轮豁免不承继。

CONSENT_SCHEMA_V2 = _p12.CONSENT_SCHEMA_V2
COST_DECISION_SUPPLY_V2 = "SUPPLY_HARD_GUARANTEE"
COST_DECISION_WAIVER_V2 = "R1_2_SCOPE_RISK_WAIVER"
COST_DECISION_BLOCKED_V2 = "BLOCKED"
COST_DECISIONS_V2 = (COST_DECISION_SUPPLY_V2, COST_DECISION_WAIVER_V2,
                     COST_DECISION_BLOCKED_V2)

CONSENT_KEYS_V2 = frozenset({
    "schema", "schema_version", "envelope_id", "profile_id", "decision", "scope",
    "granted_at_utc", "granted_by", "verbatim", "plan_path", "plan_sha256",
    "family_cap", "history_posts", "history_ledger_sha256",
    "risk_reserve_usd", "exposure_note", "supersedes", "notes",
})


@dataclass(frozen=True)
class ConsentV2:
    """R1.2 费用门决定书（供应硬保证 / R1.2 范围风险豁免 / 阻断）。

    ``scope`` 必须是 ``"R1.2"``；``history_ledger_sha256`` 必须恰为两本历史
    源账的冻结字节 sha256（顺序固定），任何「换个说法」的历史口径都拒收。
    """

    schema: str
    envelope_id: str
    profile_id: str
    decision: str
    scope: str
    granted_at_utc: str
    granted_by: str
    verbatim: str
    plan_path: str
    plan_sha256: str
    family_cap: int
    history_posts: int
    history_ledger_sha256: "tuple[str, ...]"
    risk_reserve_usd: Decimal
    exposure_note: str
    supersedes: "tuple[str, ...]"
    notes: str

    def __post_init__(self) -> None:
        if self.schema != CONSENT_SCHEMA_V2:
            raise ContractError("SCHEMA_MISMATCH",
                                f"consent schema 必须是 {CONSENT_SCHEMA_V2}，实为 "
                                f"{self.schema!r}")
        if self.profile_id != _p12.PROFILE_R12:
            raise ContractError("PROFILE_MISMATCH",
                                f"profile_id 必须是 {_p12.PROFILE_R12}，实为 "
                                f"{self.profile_id!r}")
        if self.decision not in COST_DECISIONS_V2:
            raise ContractError("PAID_NOT_AUTHORIZED",
                                f"decision={self.decision!r} 不在 {COST_DECISIONS_V2}")
        if self.scope != "R1.2":
            raise ContractError(
                "CONSENT_STALE",
                f"scope={self.scope!r} != 'R1.2'：旧 R1/R0.x 豁免不承继（Plan §1.1）")
        require_safe_id(self.envelope_id, where="consent.envelope_id")
        for name in ("granted_at_utc", "granted_by", "plan_path", "plan_sha256"):
            require_str(getattr(self, name), where=f"consent.{name}")
        if len(self.plan_sha256) != 64 or any(
                c not in "0123456789abcdef" for c in self.plan_sha256):
            raise ContractError("CONSENT_STALE",
                                "consent.plan_sha256 必须是 64 位小写十六进制")
        if self.family_cap != _p12.FAMILY_CAP:
            raise ContractError("FAMILY_CAP_EXCEEDED",
                                f"family_cap={self.family_cap} != {_p12.FAMILY_CAP}")
        if self.history_posts != _p12.FAMILY_HISTORY_POSTS:
            raise ContractError("HISTORY_DRIFT",
                                f"history_posts={self.history_posts} != "
                                f"{_p12.FAMILY_HISTORY_POSTS}")
        expected_ledgers = tuple(str(s["ledger_sha256"])
                                 for s in _p12.HISTORY_SOURCES)
        if tuple(self.history_ledger_sha256) != expected_ledgers:
            raise ContractError(
                "HISTORY_DRIFT",
                "history_ledger_sha256 必须恰为两本历史源账的冻结字节 sha256")
        if not isinstance(self.risk_reserve_usd, Decimal):
            raise ContractError("USD_INVALID", "risk_reserve_usd 必须是 Decimal")
        if not self.risk_reserve_usd.is_finite() or self.risk_reserve_usd < 0:
            raise ContractError("USD_INVALID",
                                "risk_reserve_usd 必须是非负有限值")
        if self.risk_reserve_usd > MAX_USD_LITERAL:
            raise ContractError("CAP_EXCEEDED",
                                "risk_reserve_usd 超过整轮费用帽 100 USD")
        if self.decision == COST_DECISION_WAIVER_V2:
            if not self.verbatim.strip():
                raise ContractError("PAID_NOT_AUTHORIZED",
                                    "R1.2 范围风险豁免必须携带授权方原话 verbatim")
            if not self.exposure_note.strip():
                raise ContractError(
                    "PAID_NOT_AUTHORIZED",
                    "风险豁免必须写明每个 POST 的预留与总暴露处置（exposure_note）")
            if self.risk_reserve_usd <= 0:
                raise ContractError(
                    "PAID_NOT_AUTHORIZED",
                    "风险豁免必须给出正的每轮风险预留 risk_reserve_usd")
        if not isinstance(self.supersedes, tuple):
            raise ContractError("WRONG_TYPE", "consent.supersedes 必须是字符串数组")

    def to_dict(self) -> "dict[str, Any]":
        return {
            "schema": self.schema,
            "schema_version": _p12.SCHEMA_VERSION,
            "envelope_id": self.envelope_id,
            "profile_id": self.profile_id,
            "decision": self.decision,
            "scope": self.scope,
            "granted_at_utc": self.granted_at_utc,
            "granted_by": self.granted_by,
            "verbatim": self.verbatim,
            "plan_path": self.plan_path,
            "plan_sha256": self.plan_sha256,
            "family_cap": self.family_cap,
            "history_posts": self.history_posts,
            "history_ledger_sha256": list(self.history_ledger_sha256),
            "risk_reserve_usd": str(self.risk_reserve_usd),
            "exposure_note": self.exposure_note,
            "supersedes": list(self.supersedes),
            "notes": self.notes,
        }

    @property
    def sha256(self) -> str:
        return document_hash(self.to_dict())

    @property
    def paid_authorized(self) -> bool:
        return self.decision in (COST_DECISION_SUPPLY_V2,
                                 COST_DECISION_WAIVER_V2)

    @classmethod
    def from_dict(cls, obj: Any) -> "ConsentV2":
        obj = require_object(obj, where="consent")
        require_no_nan(obj, where="consent")
        require_exact_keys(obj, CONSENT_KEYS_V2, where="consent")
        if obj["schema_version"] != _p12.SCHEMA_VERSION:
            raise ContractError("SCHEMA_MISMATCH",
                                f"consent.schema_version={obj['schema_version']!r} != "
                                f"{_p12.SCHEMA_VERSION}")
        sup = obj["supersedes"]
        if not isinstance(sup, (list, tuple)) or not all(isinstance(s, str) for s in sup):
            raise ContractError("WRONG_TYPE", "consent.supersedes 必须是字符串数组")
        ledgers = obj["history_ledger_sha256"]
        if not isinstance(ledgers, (list, tuple)) or not all(
                isinstance(s, str) for s in ledgers):
            raise ContractError("WRONG_TYPE",
                                "consent.history_ledger_sha256 必须是字符串数组")
        return cls(
            schema=require_str(obj["schema"], where="consent.schema"),
            envelope_id=require_safe_id(obj["envelope_id"], where="consent.envelope_id"),
            profile_id=require_str(obj["profile_id"], where="consent.profile_id"),
            decision=require_str(obj["decision"], where="consent.decision"),
            scope=require_str(obj["scope"], where="consent.scope"),
            granted_at_utc=require_str(obj["granted_at_utc"], where="consent.granted_at_utc"),
            granted_by=require_str(obj["granted_by"], where="consent.granted_by"),
            verbatim=str(obj["verbatim"] or ""),
            plan_path=require_str(obj["plan_path"], where="consent.plan_path"),
            plan_sha256=require_str(obj["plan_sha256"], where="consent.plan_sha256"),
            family_cap=require_int(obj["family_cap"], where="consent.family_cap"),
            history_posts=require_int(obj["history_posts"],
                                      where="consent.history_posts"),
            history_ledger_sha256=tuple(str(s) for s in ledgers),
            risk_reserve_usd=require_decimal(obj["risk_reserve_usd"],
                                             where="consent.risk_reserve_usd"),
            exposure_note=str(obj["exposure_note"] or ""),
            supersedes=tuple(sup),
            notes=str(obj["notes"] or ""),
        )


def consent_from_dict(obj: Any) -> "Consent | ConsentV2":
    """按 ``schema`` 分派费用门决定书；未知/缺失 schema 一律拒绝。"""
    obj = require_object(obj, where="consent")
    schema = obj.get("schema")
    if schema in (CONSENT_SCHEMA, f"{SCHEMA_PREFIX}.consent.v1"):
        return Consent.from_dict(obj)
    if schema == CONSENT_SCHEMA_V2:
        return ConsentV2.from_dict(obj)
    raise ContractError("SCHEMA_MISMATCH",
                        f"consent.schema={schema!r} 未登记")


def assert_paid_authorized_v2(consent: ConsentV2) -> None:
    """AC08：R1.2 付费门——无 R1.2 范围有效授权即 0 POST。"""
    if not isinstance(consent, ConsentV2):
        raise ContractError("CONSENT_STALE", "R1.2 付费门只接受 ConsentV2")
    if consent.decision == COST_DECISION_BLOCKED_V2:
        raise ContractError(
            "COST_GATE_BLOCKED",
            "费用门 BLOCKED：T0–T5 离线工作可继续，T6 付费探针不得发网")
    if not consent.paid_authorized:
        raise ContractError("PAID_NOT_AUTHORIZED",
                            "R1.2 费用门无有效授权（互斥三选一未选供应硬保证/风险豁免）")


# ---- R1.2 envelope 文档（contract/envelope.json）----

ENVELOPE_KEYS_V2 = frozenset({
    "schema", "schema_version", "profile_id", "envelope_id", "mode",
    "created_at_utc", "plan_path", "plan_sha256",
    "budget_path", "budget_sha256", "profile_sha256",
    "ledger_namespace", "hard_cap", "normal_budget", "lane_caps",
    "family_cap", "history_posts",
    "family_permit_path", "family_permit_sha256",
    "cost_gate", "public_science_scope_sha256", "public_contract_sha256",
})

ENVELOPE_MODES = ("offline_validation", "paid")


def validate_envelope_v2(doc: Any, *, envelope_id: "str | None" = None) -> "dict[str, Any]":
    """校验 R1.2 envelope 文档；任何未登记键/漂移值都拒（T0 冻结面）。

    返回原文档的浅拷贝（不就地改）。``envelope_id`` 给出时额外核对一致。
    """
    doc = require_object(doc, where="envelope")
    require_no_nan(doc, where="envelope")
    require_exact_keys(doc, ENVELOPE_KEYS_V2, where="envelope")
    if doc["schema"] != _p12.ENVELOPE_SCHEMA_V2:
        raise ContractError("SCHEMA_MISMATCH",
                            f"envelope.schema={doc['schema']!r} != "
                            f"{_p12.ENVELOPE_SCHEMA_V2}")
    if doc["schema_version"] != _p12.SCHEMA_VERSION:
        raise ContractError("SCHEMA_MISMATCH",
                            f"envelope.schema_version={doc['schema_version']!r}")
    if doc["profile_id"] != _p12.PROFILE_R12:
        raise ContractError("PROFILE_MISMATCH",
                            f"envelope.profile_id={doc['profile_id']!r}")
    env_id = require_safe_id(doc["envelope_id"], where="envelope.envelope_id")
    if envelope_id is not None and env_id != envelope_id:
        raise ContractError("PROFILE_MISMATCH",
                            f"envelope.id={env_id!r} 与请求 {envelope_id!r} 不符")
    if doc["mode"] not in ENVELOPE_MODES:
        raise ContractError("WRONG_TYPE",
                            f"envelope.mode={doc['mode']!r} 不在 {ENVELOPE_MODES}")
    if require_int(doc["ledger_namespace"], where="envelope.ledger_namespace") \
            != _p12.PROFILE_R12_DEF.namespace:
        raise ContractError("BUDGET_DRIFT",
                            f"envelope.ledger_namespace={doc['ledger_namespace']} "
                            f"!= {_p12.PROFILE_R12_DEF.namespace}")
    if require_int(doc["hard_cap"], where="envelope.hard_cap") \
            != _p12.RSI_R12_HARD_CAP:
        raise ContractError("BUDGET_DRIFT", "envelope.hard_cap 不等于 124")
    if require_int(doc["normal_budget"], where="envelope.normal_budget") \
            != _p12.RSI_R12_NORMAL_BUDGET:
        raise ContractError("BUDGET_DRIFT", "envelope.normal_budget 不等于 124")
    lanes = require_object(doc["lane_caps"], where="envelope.lane_caps")
    if {k: require_int(v, where=f"envelope.lane_caps.{k}") for k, v in lanes.items()} \
            != dict(_p12.LANE_POST_CAPS_R12):
        raise ContractError("BUDGET_DRIFT", "envelope.lane_caps 与 profile 表不符")
    if require_int(doc["family_cap"], where="envelope.family_cap") != _p12.FAMILY_CAP:
        raise ContractError("FAMILY_CAP_EXCEEDED", "envelope.family_cap 不等于 155")
    if require_int(doc["history_posts"], where="envelope.history_posts") \
            != _p12.FAMILY_HISTORY_POSTS:
        raise ContractError("HISTORY_DRIFT", "envelope.history_posts 不等于 31")
    for name in ("plan_sha256", "budget_sha256", "profile_sha256",
                 "family_permit_sha256", "public_science_scope_sha256",
                 "public_contract_sha256"):
        value = require_str(doc[name], where=f"envelope.{name}")
        if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            raise ContractError("SCHEMA_MISMATCH",
                                f"envelope.{name} 必须是 64 位小写十六进制")
    for name in ("plan_path", "budget_path", "family_permit_path"):
        require_str(doc[name], where=f"envelope.{name}")
    created = require_str(doc["created_at_utc"], where="envelope.created_at_utc")
    if not created.endswith("Z"):
        raise ContractError("SCHEMA_MISMATCH",
                            "envelope.created_at_utc 必须是严格 UTC Z（ts_iso 口径）")
    gate = require_object(doc["cost_gate"], where="envelope.cost_gate")
    decision = gate.get("decision")
    if decision not in COST_DECISIONS_V2:
        raise ContractError("PAID_NOT_AUTHORIZED",
                            f"envelope.cost_gate.decision={decision!r} 未登记")
    paid = bool(gate.get("paid_authorized"))
    if paid != (decision in (COST_DECISION_SUPPLY_V2, COST_DECISION_WAIVER_V2)):
        raise ContractError("PAID_NOT_AUTHORIZED",
                            "envelope.cost_gate.paid_authorized 与 decision 不一致")
    if doc["mode"] == "offline_validation" and paid:
        raise ContractError(
            "OFFLINE_AUTH_REQUIRED",
            "offline_validation 模式不得携带付费授权（离线演练禁用 sender）")
    return dict(doc)


# ---------------------------------------------------------------------------
# 账户暴露视图（Plan §8.3：旧 root H 的 UNKNOWN 必须可见，且不改旧账）
# ---------------------------------------------------------------------------

ACCOUNT_EXPOSURE_SCHEMA = f"{SCHEMA_PREFIX}.account_exposure.v1"


def account_exposure_doc(*, envelope_id: str, pending_unknown: "list[dict[str, Any]]",
                         note: str) -> "dict[str, Any]":
    """构造只读账户暴露视图（不修改任何旧账行）。"""
    total = Decimal("0")
    for row in pending_unknown:
        total += require_decimal(row["reserved_usd"], where="pending_unknown.reserved_usd")
    return {
        "schema": ACCOUNT_EXPOSURE_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "envelope_id": require_safe_id(envelope_id, where="envelope_id"),
        "pending_unknown": pending_unknown,
        "pending_unknown_usd": str(total),
        "count": len(pending_unknown),
        "note": note,
    }


# ---------------------------------------------------------------------------
# 研究计划合同（Plan §5.1；AC05/AC08/AC09 的结构面）
#
# 这里只做**机械检查**：结构、id 唯一、参数在公开域、δ 与量表一致、分布非退化、
# 以及 Study B 的「Tc 与 dt 可分离」设计门。机制是否科学合理交独立人工审查
# （§4.2），代码不替候选设计、不按预期结果打分。
# ---------------------------------------------------------------------------

PLAN_SCHEMA = f"{SCHEMA_PREFIX}.plan.v1"
PLAN_PHASES = ("explore", "confirm")
CONTRAST_KINDS = ("primary", "negative", "exploratory")

CONDITION_KEYS = frozenset({"id", "scene_id", "params"})
CONTRAST_KEYS = frozenset({"id", "kind", "metric", "proposition", "delta",
                           "condition_ids"})
DISTRIBUTION_KEYS = frozenset({"generator", "block_count", "varying"})
HYPOTHESIS_KEYS = frozenset({"id", "mechanism", "predicted", "contrast_id",
                             "falsification"})
PLAN_KEYS = frozenset({
    "schema", "schema_version", "study_id", "revision", "parent_sha",
    "hypotheses", "conditions", "nuisance_distribution", "primary",
    "negative_control", "analysis_source_sha", "design_source_sha", "phase",
})

#: 预测方向除了三类结构化主张，还允许纯描述（不判 SUPPORTED/REFUTED）。
DESCRIPTIVE_PREDICTION = "descriptive"


def _require_sha256_hex(value: Any, *, where: str, allow_none: bool = False):
    if value is None:
        if allow_none:
            return None
        raise ContractError("SOURCE_SHA_INVALID", f"{where} 不得为空（confirm 必须绑定源码）")
    s = require_str(value, where=where)
    if not re.fullmatch(r"[0-9a-f]{64}", s):
        raise ContractError("SOURCE_SHA_INVALID", f"{where} 必须是 64 位小写 hex")
    return s


@dataclass(frozen=True)
class Condition:
    """一个公开 typed 条件：``params`` 必须过场景域校验后规范化。"""

    id: str
    scene_id: str
    params: "Mapping[str, str]"

    @classmethod
    def from_dict(cls, doc: Any, *, where: str) -> "Condition":
        from orchestrator.rsi_r1 import scenes as _scenes

        obj = require_object(doc, where=where)
        require_exact_keys(obj, CONDITION_KEYS, where=where)
        cid = require_safe_id(obj["id"], where=f"{where}.id")
        scene_id = require_str(obj["scene_id"], where=f"{where}.scene_id")
        raw_params = require_object(obj["params"], where=f"{where}.params")
        try:
            params = _scenes.normalise_params(scene_id, raw_params)
        except _scenes.SceneError as exc:
            raise ContractError(exc.code, f"{where}.params: {exc.detail or exc.code}") from None
        return cls(id=cid, scene_id=scene_id, params=params)

    def to_dict(self) -> "dict[str, Any]":
        return {"id": self.id, "scene_id": self.scene_id,
                "params": dict(self.params)}


@dataclass(frozen=True)
class ContrastSpec:
    id: str
    kind: str
    metric: str
    proposition: str
    delta: Decimal
    condition_ids: "tuple[str, str]"

    @classmethod
    def from_dict(cls, doc: Any, *, where: str) -> "ContrastSpec":
        from orchestrator.rsi_r1.rubric import PROPOSITIONS, metric_delta

        obj = require_object(doc, where=where)
        require_exact_keys(obj, CONTRAST_KEYS, where=where)
        cid = require_safe_id(obj["id"], where=f"{where}.id")
        kind = require_str(obj["kind"], where=f"{where}.kind")
        if kind not in CONTRAST_KINDS:
            raise ContractError("WRONG_TYPE", f"{where}.kind={kind!r} 不在 {CONTRAST_KINDS}")
        metric = require_str(obj["metric"], where=f"{where}.metric")
        proposition = require_str(obj["proposition"], where=f"{where}.proposition")
        if proposition not in PROPOSITIONS:
            raise ContractError("WRONG_TYPE",
                                f"{where}.proposition={proposition!r} 不在 {PROPOSITIONS}")
        delta = require_decimal(obj["delta"], where=f"{where}.delta")
        try:
            fixed = metric_delta(metric)
        except KeyError:
            raise ContractError("METRIC_UNREGISTERED",
                                f"{where}.metric={metric!r} 未在量表注册") from None
        if delta != fixed:
            raise ContractError(
                "DELTA_DRIFT",
                f"{where}.delta={delta} != 量表固定 δ={fixed}（候选不能扩界）")
        ids = obj["condition_ids"]
        if not isinstance(ids, (list, tuple)) or len(ids) != 2:
            raise ContractError("WRONG_TYPE", f"{where}.condition_ids 必须恰 2 个条件 id")
        a = require_safe_id(ids[0], where=f"{where}.condition_ids[0]")
        b = require_safe_id(ids[1], where=f"{where}.condition_ids[1]")
        if a == b:
            raise ContractError("DUPLICATE_ID",
                                f"{where}.condition_ids 两端同为 {a!r}（配对差需要两个条件）")
        return cls(id=cid, kind=kind, metric=metric, proposition=proposition,
                   delta=delta, condition_ids=(a, b))

    def to_dict(self) -> "dict[str, Any]":
        return {"id": self.id, "kind": self.kind, "metric": self.metric,
                "proposition": self.proposition, "delta": str(self.delta),
                "condition_ids": list(self.condition_ids)}


@dataclass(frozen=True)
class DistributionSpec:
    generator: str
    block_count: int
    varying: "Mapping[str, tuple[str, str]]"   # param_id → (lo, hi)

    @classmethod
    def from_dict(cls, doc: Any, *, where: str) -> "DistributionSpec":
        obj = require_object(doc, where=where)
        require_exact_keys(obj, DISTRIBUTION_KEYS, where=where)
        gen = require_safe_id(obj["generator"], where=f"{where}.generator")
        count = require_int(obj["block_count"], where=f"{where}.block_count",
                            min_value=1, max_value=100_000)
        raw_varying = require_object(obj["varying"], where=f"{where}.varying")
        varying: "dict[str, tuple[str, str]]" = {}
        for pid, rng in raw_varying.items():
            require_safe_id(pid, where=f"{where}.varying key")
            if not isinstance(rng, (list, tuple)) or len(rng) != 2:
                raise ContractError("WRONG_TYPE",
                                    f"{where}.varying.{pid} 必须是 [lo, hi]")
            lo = require_decimal(rng[0], where=f"{where}.varying.{pid}[0]")
            hi = require_decimal(rng[1], where=f"{where}.varying.{pid}[1]")
            if lo > hi:
                raise ContractError("DISTRIBUTION",
                                    f"{where}.varying.{pid} 区间倒置 [{lo},{hi}]")
            varying[pid] = (str(lo), str(hi))
        if not varying:
            raise ContractError("DISTRIBUTION_DEGENERATE",
                                f"{where}.varying 为空：确认分布不能是点质量")
        return cls(generator=gen, block_count=count, varying=varying)

    def to_dict(self) -> "dict[str, Any]":
        return {"generator": self.generator, "block_count": self.block_count,
                "varying": {k: list(v) for k, v in self.varying.items()}}


@dataclass(frozen=True)
class Hypothesis:
    id: str
    mechanism: str
    predicted: str
    contrast_id: str
    falsification: str

    @classmethod
    def from_dict(cls, doc: Any, *, where: str) -> "Hypothesis":
        from orchestrator.rsi_r1.rubric import PROPOSITIONS

        obj = require_object(doc, where=where)
        require_exact_keys(obj, HYPOTHESIS_KEYS, where=where)
        hid = require_safe_id(obj["id"], where=f"{where}.id")
        mechanism = require_str(obj["mechanism"], where=f"{where}.mechanism")
        predicted = require_str(obj["predicted"], where=f"{where}.predicted")
        if predicted not in PROPOSITIONS + (DESCRIPTIVE_PREDICTION,):
            raise ContractError(
                "WRONG_TYPE",
                f"{where}.predicted={predicted!r} 不在 {PROPOSITIONS + (DESCRIPTIVE_PREDICTION,)}")
        contrast_id = require_safe_id(obj["contrast_id"], where=f"{where}.contrast_id")
        falsification = require_str(obj["falsification"], where=f"{where}.falsification")
        return cls(id=hid, mechanism=mechanism, predicted=predicted,
                   contrast_id=contrast_id, falsification=falsification)

    def to_dict(self) -> "dict[str, Any]":
        return {"id": self.id, "mechanism": self.mechanism,
                "predicted": self.predicted, "contrast_id": self.contrast_id,
                "falsification": self.falsification}


@dataclass(frozen=True)
class PlanCheck:
    """机械检查结果（全部为 True 才接受；细节留档供审查引用）。"""

    study_id: str
    phase: str
    checks: "tuple[tuple[str, bool, str], ...]"

    @property
    def ok(self) -> bool:
        return all(ok for _, ok, _ in self.checks)

    def to_dict(self) -> "dict[str, Any]":
        return {"study_id": self.study_id, "phase": self.phase, "ok": self.ok,
                "checks": [{"id": c, "ok": o, "detail": d} for c, o, d in self.checks]}


@dataclass(frozen=True)
class ResearchPlan:
    study_id: str
    revision: int
    parent_sha: "str | None"
    hypotheses: "tuple[Hypothesis, ...]"
    conditions: "tuple[Condition, ...]"
    nuisance_distribution: DistributionSpec
    primary: ContrastSpec
    negative_control: "ContrastSpec | None"
    analysis_source_sha: "str | None"
    design_source_sha: "str | None"
    phase: str

    @classmethod
    def from_dict(cls, doc: Any, *, where: str = "plan") -> "ResearchPlan":
        obj = require_object(doc, where=where)
        require_exact_keys(obj, PLAN_KEYS, where=where)
        if obj["schema"] != PLAN_SCHEMA:
            raise ContractError("SCHEMA_MISMATCH",
                                f"{where}.schema={obj['schema']!r} != {PLAN_SCHEMA}")
        require_int(obj["schema_version"], where=f"{where}.schema_version")
        study_id = require_str(obj["study_id"], where=f"{where}.study_id")
        if study_id not in ("A", "B"):
            raise ContractError("UNKNOWN_SCENE", f"{where}.study_id={study_id!r}")
        revision = require_int(obj["revision"], where=f"{where}.revision", min_value=1)
        parent_sha = _require_sha256_hex(obj["parent_sha"], where=f"{where}.parent_sha",
                                         allow_none=True)
        phase = require_str(obj["phase"], where=f"{where}.phase")
        if phase not in PLAN_PHASES:
            raise ContractError("PLAN_PHASE_INVALID",
                                f"{where}.phase={phase!r} 不在 {PLAN_PHASES}")

        raw_h = obj["hypotheses"]
        if not isinstance(raw_h, list):
            raise ContractError("WRONG_TYPE", f"{where}.hypotheses 必须是数组")
        hypotheses = tuple(
            Hypothesis.from_dict(h, where=f"{where}.hypotheses[{i}]")
            for i, h in enumerate(raw_h))
        raw_c = obj["conditions"]
        if not isinstance(raw_c, list):
            raise ContractError("WRONG_TYPE", f"{where}.conditions 必须是数组")
        conditions = tuple(
            Condition.from_dict(c, where=f"{where}.conditions[{i}]")
            for i, c in enumerate(raw_c))
        neg_raw = obj["negative_control"]
        negative = (None if neg_raw is None
                    else ContrastSpec.from_dict(neg_raw, where=f"{where}.negative_control"))
        return cls(
            study_id=study_id, revision=revision, parent_sha=parent_sha,
            hypotheses=hypotheses, conditions=conditions,
            nuisance_distribution=DistributionSpec.from_dict(
                obj["nuisance_distribution"], where=f"{where}.nuisance_distribution"),
            primary=ContrastSpec.from_dict(obj["primary"], where=f"{where}.primary"),
            negative_control=negative,
            analysis_source_sha=_require_sha256_hex(
                obj["analysis_source_sha"], where=f"{where}.analysis_source_sha",
                allow_none=(phase == "explore")),
            design_source_sha=_require_sha256_hex(
                obj["design_source_sha"], where=f"{where}.design_source_sha",
                allow_none=(phase == "explore")),
            phase=phase,
        )

    def to_dict(self) -> "dict[str, Any]":
        return {
            "schema": PLAN_SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "study_id": self.study_id,
            "revision": self.revision,
            "parent_sha": self.parent_sha,
            "hypotheses": [h.to_dict() for h in self.hypotheses],
            "conditions": [c.to_dict() for c in self.conditions],
            "nuisance_distribution": self.nuisance_distribution.to_dict(),
            "primary": self.primary.to_dict(),
            "negative_control": (None if self.negative_control is None
                                 else self.negative_control.to_dict()),
            "analysis_source_sha": self.analysis_source_sha,
            "design_source_sha": self.design_source_sha,
            "phase": self.phase,
        }

    @property
    def sha256(self) -> str:
        return document_hash(self.to_dict())


def _condition_diff(a: Condition, b: Condition) -> "list[str]":
    keys = set(a.params) | set(b.params)
    return sorted(k for k in keys if a.params.get(k) != b.params.get(k))


def _check_tc_dt_separable(conditions: "tuple[Condition, ...]") -> "tuple[str, str, str] | None":
    """Study B 设计门：存在共享基准的两个单变量干预（一个只改 Tc、一个只改 dt）。"""
    for base in conditions:
        tc_only = [c for c in conditions
                   if c is not base and _condition_diff(base, c) == ["tc_s"]]
        dt_only = [c for c in conditions
                   if c is not base and _condition_diff(base, c) == ["dt_s"]]
        if tc_only and dt_only:
            return base.id, tc_only[0].id, dt_only[0].id
    return None


def validate_plan(plan: ResearchPlan, *,
                  contract: "Mapping[str, Any] | None" = None,
                  state: "str | None" = None) -> PlanCheck:
    """机械设计检查；不合法即抛 ``ContractError``（带公开条款 id）。

    ``contract``/``state`` 保留给调用方传公开合同与当前状态机状态；本函数的
    判定不依赖候选的预期结果，也不看任何确认集数据。
    """
    from orchestrator.rsi_r1 import scenes as _scenes
    from orchestrator.rsi_r1.rubric import (
        CONFIRM_BLOCKS,
        MAX_CONDITIONS,
        METHOD_RUBRIC_IDS,
        NEGATIVE_CONTROL_METRIC,
        PROPOSITIONS,
    )

    checks: "list[tuple[str, bool, str]]" = []

    # --- 1. id 唯一 ---
    hyp_ids = [h.id for h in plan.hypotheses]
    con_ids = [c.id for c in plan.conditions]
    if len(set(hyp_ids)) != len(hyp_ids):
        raise ContractError("DUPLICATE_ID", f"hypotheses 重复 id：{hyp_ids}")
    if len(set(con_ids)) != len(con_ids):
        raise ContractError("DUPLICATE_ID", f"conditions 重复 id：{con_ids}")
    contrast_ids = {plan.primary.id}
    if plan.negative_control is not None:
        contrast_ids.add(plan.negative_control.id)
    for h in plan.hypotheses:
        if h.contrast_id not in contrast_ids:
            raise ContractError(
                "MISSING_KEY",
                f"hypothesis {h.id} 指向未登记的 contrast {h.contrast_id!r}")
    checks.append(("ids", True, f"{len(con_ids)} 条件 / {len(contrast_ids)} 主对比"))

    # --- 2. 两个可区分解释（§4.2 M2）---
    if len(plan.hypotheses) < 2:
        raise ContractError("HYPOTHESIS_INSUFFICIENT",
                            f"只有 {len(plan.hypotheses)} 个解释；至少需要 2 个")
    mechs = [h.mechanism.strip() for h in plan.hypotheses]
    if len(set(mechs)) != len(mechs):
        raise ContractError("HYPOTHESIS_INSUFFICIENT", "两个解释机制陈述逐字相同")
    for h in plan.hypotheses:
        if len(h.falsification.strip()) < 4:
            raise ContractError("HYPOTHESIS_INSUFFICIENT",
                                f"hypothesis {h.id} 的反证条件过于空泛")
    checks.append(("hypotheses", True, f"{len(plan.hypotheses)} 个互异解释且各有反证"))

    # --- 3. 条件与场景 ---
    if plan.phase == "confirm" and len(plan.conditions) > MAX_CONDITIONS:
        raise ContractError("CONDITION_LIMIT",
                            f"{len(plan.conditions)} 个条件超过确认上限 {MAX_CONDITIONS}")
    if len(plan.conditions) < 2:
        raise ContractError("CONDITION_LIMIT", "至少需要 2 个条件才能构成配对")
    by_id = {c.id: c for c in plan.conditions}
    study_scene = _scenes.scene_doc(
        _scenes.SCENE_A if plan.study_id == "A" else _scenes.SCENE_B)
    for c in plan.conditions:
        if c.scene_id != study_scene.scene_id:
            raise ContractError("UNKNOWN_SCENE",
                                f"condition {c.id} 的 scene_id={c.scene_id!r} 与 study "
                                f"{plan.study_id} 不符")
    checks.append(("conditions", True, f"{len(plan.conditions)} 条件全部过公开域校验"))

    # --- 4. 主对比 ---
    if plan.primary.kind != "primary":
        raise ContractError("WRONG_TYPE", "primary.kind 必须是 'primary'")
    for cid in plan.primary.condition_ids:
        if cid not in by_id:
            raise ContractError("MISSING_KEY", f"primary 引用未知条件 {cid!r}")
    if plan.primary.metric not in study_scene.metrics:        raise ContractError("METRIC_UNREGISTERED",
                            f"主对比指标 {plan.primary.metric!r} 不属于 study {plan.study_id}")
    if plan.primary.proposition not in PROPOSITIONS:
        raise ContractError("WRONG_TYPE", "primary.proposition 非法")
    checks.append(("primary", True,
                   f"{plan.primary.metric} {plan.primary.proposition}({plan.primary.delta})"))

    # --- 5. 校准阴性控制（A 必需）---
    if plan.study_id == "A":
        if plan.negative_control is None:
            raise ContractError("NEGATIVE_CONTROL_MISSING",
                                "Study A 必须登记校准阴性控制（基准 vs sham）")
        neg = plan.negative_control
        if neg.kind != "negative":
            raise ContractError("WRONG_TYPE", "negative_control.kind 必须是 'negative'")
        if neg.metric != NEGATIVE_CONTROL_METRIC:
            raise ContractError("METRIC_UNREGISTERED",
                                f"阴性控制指标必须是 {NEGATIVE_CONTROL_METRIC}")
        if neg.proposition != "equivalent":
            raise ContractError("WRONG_TYPE", "阴性控制必须用 equivalent 主张")
        for cid in neg.condition_ids:
            if cid not in by_id:
                raise ContractError("MISSING_KEY", f"negative_control 引用未知条件 {cid!r}")
        a, b = (by_id[cid] for cid in neg.condition_ids)
        diff = _condition_diff(a, b)
        if diff != ["torsional_friction_m"]:
            raise ContractError(
                "DESIGN_NOT_SEPARABLE",
                f"阴性控制两条件只允许差 torsional_friction_m，实际差 {diff}")
        checks.append(("negative_control", True,
                       f"{a.id} vs {b.id} 仅差 torsional friction（condim 恒 3）"))

    # --- 6. Study B 的 Tc/dt 可分离设计门 ---
    if plan.study_id == "B":
        found = _check_tc_dt_separable(plan.conditions)
        if found is None:
            raise ContractError(
                "DESIGN_NOT_SEPARABLE",
                "未找到共享基准的单变量对比（一个只改 tc_s、一个只改 dt_s）；"
                "只同时改两者不能宣称单因素因果归因")
        base, tc_c, dt_c = found
        checks.append(("tc_dt_separable", True, f"基准 {base}：Tc-only→{tc_c}，dt-only→{dt_c}"))

    # --- 7. 分布非退化 ---
    dist = plan.nuisance_distribution
    ranges = {pid: (lo, hi) for pid, (lo, hi) in dist.varying.items()}
    try:
        nd = _scenes.check_distribution_non_degenerate(study_scene.scene_id, ranges)
    except _scenes.SceneError as exc:
        raise ContractError(exc.code, f"nuisance_distribution: {exc.detail or exc.code}") from None
    if plan.phase == "confirm" and dist.block_count != CONFIRM_BLOCKS:
        raise ContractError("DISTRIBUTION",
                            f"确认 block_count={dist.block_count} != {CONFIRM_BLOCKS}")
    checks.append(("distribution", True,
                   f"{dist.block_count} blocks，非退化参数 {nd['param']}"
                   f"（域宽 {nd['fraction']}）"))

    # --- 8. 冻结源码 hash（confirm 必填）---
    if plan.phase == "confirm":
        if plan.analysis_source_sha is None or plan.design_source_sha is None:
            raise ContractError("SOURCE_SHA_INVALID",
                                "confirm 计划必须绑定 analysis/design 源码 sha256")
        checks.append(("sources", True,
                       f"design={plan.design_source_sha[:12]}… "
                       f"analysis={plan.analysis_source_sha[:12]}…"))

    # --- 9. 量表五条（结构占位；科学判定交人工审查）---
    checks.append(("rubric_ids", True, "/".join(METHOD_RUBRIC_IDS)))
    return PlanCheck(study_id=plan.study_id, phase=plan.phase,
                     checks=tuple(checks))


# ---------------------------------------------------------------------------
# 公开合同渲染（AC05）：单位、域、错误条款与可自修规则全部来自单源
# ---------------------------------------------------------------------------

DEFAULT_INPUT_LIMITS: "Mapping[str, Any]" = {
    "max_local_tokens": 20_000,
    "max_series_bytes": 131_072,
    "max_output_tokens": 8_192,
    "max_tool_return_bytes": 4_096,
}


def public_contract_document(*, scene_ids: "tuple[str, ...]",
                             limits: "Mapping[str, Any] | None" = None
                             ) -> "dict[str, Any]":
    from orchestrator.rsi_r1 import observations as _obs
    from orchestrator.rsi_r1 import scenes as _scenes
    from orchestrator.rsi_r1.rubric import METRICS, rubric_sha256

    lim = dict(DEFAULT_INPUT_LIMITS if limits is None else limits)
    docs = []
    for sid in scene_ids:
        doc = _scenes.scene_doc(sid)
        docs.append({
            **doc.to_dict(),
            "raw_fields": [
                {"id": f, "unit": _raw_field_unit(f),
                 "description": _raw_field_description(f)}
                for f in _obs.required_fields(sid)
            ],
        })
    return {
        "schema": f"{SCHEMA_PREFIX}.public_contract.v1",
        "schema_version": SCHEMA_VERSION,
        "limits": lim,
        "scenes": docs,
        "metrics": {k: {"unit": v["unit"], "delta": str(v["delta"]),
                        "study": v["study"], "censoring": v["censoring"],
                        "description": v["description"]}
                    for k, v in sorted(METRICS.items())},
        # v1 公开合同是冻结的 R1 面：只暴露 R1-S 条款；R12-S 条款属于
        # v2 资源合同（profiles.public_contract_document），不得混入旧文档。
        "refusals": {k: v for k, v in sorted(REFUSAL_CLAUSES.items())
                     if v.startswith("R1-S")},
        "rubric_sha256": rubric_sha256(),
    }


def _raw_field_unit(field: str) -> str:
    table = {
        "time_s": "s", "qpos": "SI", "qvel": "SI/s", "params_applied": "mixed",
        "engine": "1", "run_id": "1", "spec_hash": "1", "block_id": "1",
        "nsteps": "1", "nonfinite_count": "1", "solver_warnings": "1",
        "contact_pair": "1", "contact_dim_per_step": "1",
        "friction_sliding_per_step": "1", "friction_torsional_per_step": "m",
        "contact_force_n": "N", "target_q": "rad", "command_torque": "Nm",
        "applied_torque": "Nm", "control_update_index": "1",
        "saturation_count": "1", "instability_events": "1",
    }
    if field not in table:
        raise ContractError("MISSING_KEY", f"raw 字段 {field!r} 没有单位登记")
    return table[field]


def _raw_field_description(field: str) -> str:
    table = {
        "time_s": "物理时间，由 trusted host 记录并按物理时间积分",
        "qpos": "广义位置，与 time_s 等长",
        "qvel": "广义速度，与 time_s 等长",
        "params_applied": "实际应用的物理/控制参数（不是候选请求值）",
        "engine": "引擎名与固定版本",
        "run_id": "root+plan+condition+block 派生的稳定 id",
        "spec_hash": "由可信 host 重建的 spec 指纹",
        "block_id": "确认 block id",
        "nsteps": "采样点总数",
        "nonfinite_count": "宿主统计的非有限采样数，必须为 0",
        "solver_warnings": "solver 警告列表；空表示无",
        "contact_pair": "显式接触对标识",
        "contact_dim_per_step": "每步实际 contact 维度，必须恒为 3",
        "friction_sliding_per_step": "每步实际滑动摩擦系数",
        "friction_torsional_per_step": "每步实际扭转摩擦半径",
        "contact_force_n": "每步接触力（N）",
        "target_q": "目标关节角（rad）",
        "command_torque": "控制器输出（限幅前，Nm）",
        "applied_torque": "实际施加扭矩（限幅后，Nm）",
        "control_update_index": "每个时点所属的控制更新序号",
        "saturation_count": "限幅发生的次数",
        "instability_events": "失稳事件列表 {time_s, reason}",
    }
    if field not in table:
        raise ContractError("MISSING_KEY", f"raw 字段 {field!r} 没有说明登记")
    return table[field]


def assert_public_contract_complete(doc: "Mapping[str, Any]") -> None:
    """渲染前置检查：每个参数/字段/指标都必须有单位与非空说明。"""
    for scene in doc["scenes"]:
        if not scene["params"]:
            raise ContractError("MISSING_KEY", f"{scene['scene_id']} 没有公开参数")
        for p in scene["params"]:
            if not p["unit"] or not p["description"]:
                raise ContractError("MISSING_KEY",
                                    f"{scene['scene_id']}.{p['id']} 缺单位或说明")
        for f in scene["fixed"]:
            if not f["unit"] or not f["description"]:
                raise ContractError("MISSING_KEY",
                                    f"{scene['scene_id']} 固定量 {f['id']} 缺单位或说明")
        for rf in scene["raw_fields"]:
            if not rf["unit"] or not rf["description"]:
                raise ContractError("MISSING_KEY",
                                    f"{scene['scene_id']} raw 字段 {rf['id']} 缺单位或说明")
        for mid in scene["metrics"]:
            if mid not in doc["metrics"]:
                raise ContractError("METRIC_UNREGISTERED",
                                    f"{scene['scene_id']} 指标 {mid} 未在量表注册")
            m = doc["metrics"][mid]
            if not m["unit"] or not m["description"]:
                raise ContractError("MISSING_KEY", f"指标 {mid} 缺单位或说明")
    for code, clause in doc["refusals"].items():
        if not clause.startswith("R1-S"):
            raise ContractError("MISSING_KEY", f"拒绝码 {code} 缺公开条款 id")


def render_public_contract(*, scene_ids: "tuple[str, ...]",
                           limits: "Mapping[str, Any] | None" = None) -> str:
    """把单源合同渲染成确定性的公开文本（同输入 ⇒ 逐字节相同）。"""
    doc = public_contract_document(scene_ids=scene_ids, limits=limits)
    assert_public_contract_complete(doc)
    lines: "list[str]" = []
    lines.append("RSI-R1 公开研究合同 v1")
    lines.append(f"量表 sha256: {doc['rubric_sha256']}")
    lines.append("")
    lines.append("## 输入上限")
    for k, v in sorted(doc["limits"].items()):
        lines.append(f"- {k} = {v}")
    lines.append("")
    for scene in doc["scenes"]:
        lines.append(f"## 场景 {scene['scene_id']}（Study {scene['study']}）")
        lines.append(scene["description"])
        lines.append("")
        lines.append("可设计参数（id｜单位｜域｜角色｜语义）：")
        for p in scene["params"]:
            dom = "/".join(p["domain"])
            lines.append(f"- {p['id']}｜{p['unit']}｜{dom}｜{p['role']}｜{p['description']}")
        lines.append("")
        lines.append("固定量（不可设计；覆盖即拒）：")
        for f in scene["fixed"]:
            lines.append(f"- {f['id']} = {f['value']} {f['unit']}｜{f['description']}")
        lines.append("")
        lines.append("raw 字段（id｜单位｜语义）：")
        for rf in scene["raw_fields"]:
            lines.append(f"- {rf['id']}｜{rf['unit']}｜{rf['description']}")
        lines.append("")
        lines.append("主指标（id｜单位｜δ｜删失｜语义）：")
        for mid in scene["metrics"]:
            m = doc["metrics"][mid]
            lines.append(f"- {mid}｜{m['unit']}｜δ={m['delta']}｜"
                         f"{m['censoring']}｜{m['description']}")
        lines.append("")
    lines.append("## 拒绝条款（code → 公开条款 id）")
    for code, clause in sorted(doc["refusals"].items()):
        lines.append(f"- {code} → {clause}")
    lines.append("")
    lines.append("## 可自修规则")
    lines.append("- 参数缺/多/越域/类型错：按上面的域与单位改正后重提，等同一条新动作。")
    lines.append("- δ 由指标目录固定，不接受候选自定义界值（DELTA_DRIFT）。")
    lines.append("- Tc 与 dt 用整数微秒判整除（TICK_NONDIVISIBLE），不接受近似。")
    return "\n".join(lines) + "\n"
