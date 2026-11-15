"""R1.3 版本化合同：BudgetV13 / ConsentV13 / EnvelopeV13。

R1.2 的 ``BudgetSpecV2`` 在 ``__post_init__`` 里把 schema/profile 写死
（``simpraxis.rsi_r12.budget.v1`` / ``rsi-r1.2``），不能给 R1.3 复用；本模块
提供**独立**的 v13 三件套，并保持旧解析路径逐字节不变：

* ``BudgetV13``：六 lane（候选 124 + 审查 8 = 132）、资源帽取自 T1
  ``resource_plan.json`` 的硬帽（11,900,000）、无 repair_reserve；
* ``ConsentV13``：新 scope 费用件，绑定 plan/budget/族帽/132 POST 上限与
  用户 2026-11-15 执行授权；美元单价无硬保证时保持 UNKNOWN；
* ``EnvelopeV13``：contract/envelope.json 的 R1.3 版本（含 taskset/resource
  plan 摘要）。

本模块只做解析/一致性；不触网、不写盘。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Mapping

from orchestrator.rsi_r1 import profiles as P

BUDGET_KEYS = frozenset({"schema", "schema_version", "envelope_id",
                         "profile_id", "posts_by_lane", "max_usd",
                         "max_runs_by_lane", "max_steps", "aux_subenvelopes"})
CONSENT_KEYS = frozenset({"schema", "schema_version", "decision",
                          "paid_authorized", "envelope_id", "profile_id",
                          "plan_path", "plan_sha256", "budget_sha256",
                          "scope", "max_posts", "review_calibration_posts",
                          "review_final_posts", "family_cap", "exposure_note",
                          "authorized_at_utc", "authorization", "risk"})
ENVELOPE_KEYS = frozenset({"schema", "schema_version", "profile_id",
                           "envelope_id", "mode", "created_at_utc", "plan_path",
                           "plan_sha256", "budget_path", "budget_sha256",
                           "profile_sha256", "ledger_namespace", "hard_cap",
                           "normal_budget", "lane_caps", "family_cap",
                           "history_posts", "family_permit_path",
                           "family_permit_sha256", "cost_gate",
                           "taskset_sha256", "resource_plan_sha256",
                           "public_taskset_sha256"})

COST_AUTHORIZED = "AUTHORIZED"
COST_BLOCKED = "BLOCKED"


class Contract13Error(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"[{code}] {detail}" if detail else code)


def _hex64(value: Any, *, where: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(
            c not in "0123456789abcdef" for c in value):
        raise Contract13Error("SOURCE_SHA_INVALID", where)
    return value


def _sha(doc: Any) -> str:
    return hashlib.sha256(json.dumps(
        doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class BudgetV13:
    schema: str
    envelope_id: str
    profile_id: str
    posts_by_lane: "Mapping[str, int]"
    max_usd: Decimal
    max_runs_by_lane: "Mapping[str, int]"
    max_steps: int
    aux_subenvelopes: "Mapping[str, int]" = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.schema != P.BUDGET_SCHEMA_V13:
            raise Contract13Error("SCHEMA_MISMATCH", str(self.schema))
        if self.profile_id != P.PROFILE_R13:
            raise Contract13Error("PROFILE_MISMATCH", str(self.profile_id))
        if dict(self.posts_by_lane) != dict(P.LANE_POST_CAPS_R13):
            raise Contract13Error("LANE_TABLE", "posts_by_lane 与 profile 不一致")
        if sum(self.posts_by_lane.values()) != P.RSI_R13_HARD_CAP:
            raise Contract13Error("BUDGET_TOTAL", "lane 合计 != 132")
        if int(self.max_steps) <= 0:
            raise Contract13Error("MAX_STEPS", str(self.max_steps))
        if not isinstance(self.max_usd, Decimal):
            raise Contract13Error("USD_INVALID", "max_usd 必须是 Decimal")

    @property
    def total_posts(self) -> int:
        return sum(int(v) for v in self.posts_by_lane.values())

    def to_dict(self) -> "dict[str, Any]":
        return {
            "schema": self.schema, "schema_version": 1,
            "envelope_id": self.envelope_id, "profile_id": self.profile_id,
            "posts_by_lane": dict(self.posts_by_lane),
            "max_usd": str(self.max_usd),
            "max_runs_by_lane": dict(self.max_runs_by_lane),
            "max_steps": int(self.max_steps),
            "aux_subenvelopes": dict(self.aux_subenvelopes),
        }

    @property
    def sha256(self) -> str:
        return _sha(self.to_dict())

    @classmethod
    def from_dict(cls, doc: Any) -> "BudgetV13":
        if not isinstance(doc, Mapping):
            raise Contract13Error("WRONG_TYPE", "budget 必须是对象")
        if set(doc) != BUDGET_KEYS:
            raise Contract13Error("KEY_MISMATCH",
                                  f"未知/缺键 {sorted(set(doc) ^ BUDGET_KEYS)}")
        return cls(
            schema=str(doc["schema"]),
            envelope_id=str(doc["envelope_id"]),
            profile_id=str(doc["profile_id"]),
            posts_by_lane={k: int(v) for k, v in doc["posts_by_lane"].items()},
            max_usd=Decimal(str(doc["max_usd"])),
            max_runs_by_lane={k: int(v) for k, v in
                              doc["max_runs_by_lane"].items()},
            max_steps=int(doc["max_steps"]),
            aux_subenvelopes={k: int(v) for k, v in
                              (doc.get("aux_subenvelopes") or {}).items()})


def default_budget_v13(envelope_id: str, *, hard_cap_steps: int,
                       aux_subenvelopes: "Mapping[str, int] | None" = None
                       ) -> BudgetV13:
    return BudgetV13(
        schema=P.BUDGET_SCHEMA_V13, envelope_id=envelope_id,
        profile_id=P.PROFILE_R13,
        posts_by_lane={lane: int(P.LANE_POST_CAPS_R13[lane])
                       for lane in P.LANE_ORDER_R13},
        max_usd=Decimal(P.MAX_USD_LITERAL_DECIMAL),
        max_runs_by_lane={
            "explore": int(P.MAX_EXPLORE_RUNS_PER_STUDY),
            "confirm": int(P.MAX_CONFIRM_RUNS_PER_STUDY),
            "replay": int(P.MAX_REPLAY_RUNS_PER_STUDY),
        },
        max_steps=int(hard_cap_steps),
        aux_subenvelopes=dict(aux_subenvelopes or {}))


@dataclass(frozen=True)
class ConsentV13:
    decision: str
    paid_authorized: bool
    envelope_id: str
    profile_id: str
    plan_path: str
    plan_sha256: str
    budget_sha256: str
    scope: str
    max_posts: int
    review_calibration_posts: int
    review_final_posts: int
    family_cap: int
    exposure_note: str
    authorized_at_utc: str
    authorization: "Mapping[str, Any]"
    risk: "Mapping[str, Any]"

    def to_dict(self) -> "dict[str, Any]":
        return {"schema": P.CONSENT_SCHEMA_V13, "schema_version": 1,
                "decision": self.decision,
                "paid_authorized": bool(self.paid_authorized),
                "envelope_id": self.envelope_id, "profile_id": self.profile_id,
                "plan_path": self.plan_path,
                "plan_sha256": self.plan_sha256,
                "budget_sha256": self.budget_sha256, "scope": self.scope,
                "max_posts": int(self.max_posts),
                "review_calibration_posts": int(self.review_calibration_posts),
                "review_final_posts": int(self.review_final_posts),
                "family_cap": int(self.family_cap),
                "exposure_note": self.exposure_note,
                "authorized_at_utc": self.authorized_at_utc,
                "authorization": dict(self.authorization),
                "risk": dict(self.risk)}

    @classmethod
    def from_dict(cls, doc: Any) -> "ConsentV13":
        if not isinstance(doc, Mapping):
            raise Contract13Error("WRONG_TYPE", "consent 必须是对象")
        if set(doc) != CONSENT_KEYS:
            raise Contract13Error("KEY_MISMATCH",
                                  f"未知/缺键 {sorted(set(doc) ^ CONSENT_KEYS)}")
        for key in ("plan_sha256", "budget_sha256"):
            _hex64(doc[key], where=key)
        if doc["schema"] != P.CONSENT_SCHEMA_V13:
            raise Contract13Error("SCHEMA_MISMATCH", str(doc["schema"]))
        if doc["profile_id"] != P.PROFILE_R13:
            raise Contract13Error("PROFILE_MISMATCH", str(doc["profile_id"]))
        if int(doc["max_posts"]) != P.RSI_R13_HARD_CAP:
            raise Contract13Error("MAX_POSTS", "consent.max_posts 必须为 132")
        if int(doc["family_cap"]) != P.R13_FAMILY_CAP:
            raise Contract13Error("FAMILY_CAP", f"consent.family_cap 必须为 {P.R13_FAMILY_CAP}")
        return cls(
            decision=str(doc["decision"]),
            paid_authorized=bool(doc["paid_authorized"]),
            envelope_id=str(doc["envelope_id"]),
            profile_id=str(doc["profile_id"]),
            plan_path=str(doc["plan_path"]),
            plan_sha256=str(doc["plan_sha256"]),
            budget_sha256=str(doc["budget_sha256"]),
            scope=str(doc["scope"]),
            max_posts=int(doc["max_posts"]),
            review_calibration_posts=int(doc["review_calibration_posts"]),
            review_final_posts=int(doc["review_final_posts"]),
            family_cap=int(doc["family_cap"]),
            exposure_note=str(doc["exposure_note"]),
            authorized_at_utc=str(doc["authorized_at_utc"]),
            authorization=dict(doc["authorization"]),
            risk=dict(doc["risk"]))


def assert_paid_authorized_v13(consent: Any) -> None:
    if not isinstance(consent, ConsentV13):
        raise Contract13Error("CONSENT_STALE", "R1.3 只接受 ConsentV13")
    if consent.decision != COST_AUTHORIZED or not consent.paid_authorized:
        raise Contract13Error("COST_GATE_BLOCKED", "费用门未授权")
    if int(consent.review_calibration_posts) + int(
            consent.review_final_posts) != P.RSI_R13_REVIEW_POSTS:
        raise Contract13Error("REVIEW_POSTS", "审查 POST 合计必须为 8")


def validate_envelope_v13(doc: Any, *, envelope_id: "str | None" = None
                          ) -> "dict[str, Any]":
    if not isinstance(doc, Mapping):
        raise Contract13Error("WRONG_TYPE", "envelope 必须是对象")
    if set(doc) != ENVELOPE_KEYS:
        raise Contract13Error("KEY_MISMATCH",
                              f"未知/缺键 {sorted(set(doc) ^ ENVELOPE_KEYS)}")
    if doc["schema"] != P.ENVELOPE_SCHEMA_V13:
        raise Contract13Error("SCHEMA_MISMATCH", str(doc["schema"]))
    if doc["profile_id"] != P.PROFILE_R13:
        raise Contract13Error("PROFILE_MISMATCH", str(doc["profile_id"]))
    if envelope_id is not None and doc["envelope_id"] != envelope_id:
        raise Contract13Error("ENVELOPE_ID", "envelope_id 不一致")
    for key in ("budget_sha256", "plan_sha256", "taskset_sha256",
                "resource_plan_sha256", "public_taskset_sha256"):
        _hex64(doc[key], where=key)
    return dict(doc)
