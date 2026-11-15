"""R1.3 任务规格（TaskSpecV13）与任务集清单。

Plan §2 的落地：任务、场景版本、公开问题、允许设计空间、估计对象、nuisance
分配责任、阴性控制、私有实例承诺与资源绑定全部 typed；``split`` 由宿主注册，
候选不能声明自己是 holdout。

开发/留出边界：
* 旧 R1.2 两个任务（round R 的 A/B）**一律 development**，其数值、9/32 分层与
  结论不得进入新候选的请求/挂载/缓存（AC08）；
* R1.3 两个 holdout 任务必须是**新的研究问题或新的干预比较**，不是换 seed
  （AC07）：机器判据 = 干预签名或估计量类型至少一项实质变化；
* 同一机制的两个任务不得互为近重复；A（含有效阴性控制）与 B（开放研究）
  必须是不同机制（Plan §0）。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping

from orchestrator.rsi_r1 import contracts as C
from orchestrator.rsi_r1 import contrasts as X
from orchestrator.rsi_r1 import profiles as P
from orchestrator.rsi_r1 import rubric as R
from orchestrator.rsi_r1 import scenes as S

TASK_KEYS = frozenset({
    "schema", "schema_version", "task_id", "mechanism_id", "split",
    "scene_version", "public_question", "allowed_design_space", "estimand",
    "nuisance_schema", "negative_control_spec", "private_instance_commitment",
    "metric_method_sha", "resource_spec_sha", "conditions", "primary",
    "fixed",
})

SPLIT_DEVELOPMENT = "development"
SPLIT_HOLDOUT = "holdout"
SPLITS = (SPLIT_DEVELOPMENT, SPLIT_HOLDOUT)

#: 旧 R1.2 任务（round R 的 48 呼确认设计）；仅作开发集与近重复对照。
OLD_A_TASK_ID = "r12-a-sliding-friction-main"
OLD_B_TASK_ID = "r12-b-tc-dt-main"

#: 新 R1.3 留出任务 id（机制沿 A/B，但研究问题与干预均已改变）。
NEW_A_TASK_ID = "r13-a-friction-speed-interaction"
NEW_B_TASK_ID = "r13-b-tc-inertia-interaction"


class TaskSpecError(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"[{code}] {detail}" if detail else code)


def _hex64(value: Any, *, where: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(
            c not in "0123456789abcdef" for c in value):
        raise TaskSpecError("SOURCE_SHA_INVALID", f"{where} 必须是 64 位小写 hex")
    return value


def _condition(cid: str, scene_id: str, params: Mapping[str, Any]) -> C.Condition:
    return C.Condition.from_dict(
        {"id": cid, "scene_id": scene_id, "params": dict(params)}, where=cid)


@dataclass(frozen=True)
class TaskSpecV13:
    task_id: str
    mechanism_id: str
    split: str
    scene_version: int
    public_question: str
    allowed_design_space: "dict[str, Any]"
    estimand: "dict[str, Any]"
    nuisance_schema: "dict[str, Any]"
    negative_control_spec: "dict[str, Any] | None"
    private_instance_commitment: str
    metric_method_sha: str
    resource_spec_sha: str
    conditions: "tuple[C.Condition, ...]"
    primary: X.ContrastV13
    fixed: "dict[str, str]"

    def to_dict(self) -> "dict[str, Any]":
        return {
            "schema": P.TASK_SPEC_SCHEMA_V13,
            "schema_version": 1,
            "task_id": self.task_id,
            "mechanism_id": self.mechanism_id,
            "split": self.split,
            "scene_version": self.scene_version,
            "public_question": self.public_question,
            "allowed_design_space": dict(self.allowed_design_space),
            "estimand": dict(self.estimand),
            "nuisance_schema": dict(self.nuisance_schema),
            "negative_control_spec": (None if self.negative_control_spec is None
                                      else dict(self.negative_control_spec)),
            "private_instance_commitment": self.private_instance_commitment,
            "metric_method_sha": self.metric_method_sha,
            "resource_spec_sha": self.resource_spec_sha,
            "conditions": [c.to_dict() for c in self.conditions],
            "primary": self.primary.to_dict(),
            "fixed": dict(self.fixed),
        }

    @property
    def sha256(self) -> str:
        return hashlib.sha256(json.dumps(
            self.to_dict(), sort_keys=True, separators=(",", ":"),
            ensure_ascii=False).encode("utf-8")).hexdigest()

    def public_document(self) -> "dict[str, Any]":
        """候选可见面：不含私有 block/seed；私有承诺只给摘要哈希。"""
        doc = self.to_dict()
        doc["private_instance_commitment"] = self.private_instance_commitment
        doc.pop("conditions", None)  # 具体确认条件由候选在允许空间内自选
        return doc

    @classmethod
    def from_dict(cls, doc: Any, *, where: str = "task") -> "TaskSpecV13":
        if not isinstance(doc, Mapping):
            raise TaskSpecError("WRONG_TYPE", f"{where} 必须是对象")
        unknown = sorted(set(doc) - TASK_KEYS)
        missing = sorted(TASK_KEYS - set(doc))
        if unknown or missing:
            raise TaskSpecError("KEY_MISMATCH", f"{where} 未知 {unknown} 缺 {missing}")
        if doc["schema"] != P.TASK_SPEC_SCHEMA_V13:
            raise TaskSpecError("SCHEMA_MISMATCH", f"{where}.schema")
        raw_conditions = doc["conditions"]
        if not isinstance(raw_conditions, (list, tuple)):
            raise TaskSpecError("WRONG_TYPE", f"{where}.conditions")
        conditions = tuple(
            C.Condition.from_dict(c, where=f"{where}.conditions[{i}]")
            for i, c in enumerate(raw_conditions))
        spec = cls(
            task_id=str(doc["task_id"]),
            mechanism_id=str(doc["mechanism_id"]),
            split=str(doc["split"]),
            scene_version=int(doc["scene_version"]),
            public_question=str(doc["public_question"]),
            allowed_design_space=dict(doc["allowed_design_space"]),
            estimand=dict(doc["estimand"]),
            nuisance_schema={k: dict(v) for k, v in doc["nuisance_schema"].items()},
            negative_control_spec=(None if doc["negative_control_spec"] is None
                                   else dict(doc["negative_control_spec"])),
            private_instance_commitment=_hex64(
                doc["private_instance_commitment"], where=f"{where}.commitment"),
            metric_method_sha=_hex64(doc["metric_method_sha"],
                                     where=f"{where}.metric_method_sha"),
            resource_spec_sha=_hex64(doc["resource_spec_sha"],
                                     where=f"{where}.resource_spec_sha"),
            conditions=conditions,
            primary=X.ContrastV13.from_dict(doc["primary"],
                                            where=f"{where}.primary"),
            fixed={str(k): str(v) for k, v in doc["fixed"].items()},
        )
        return spec.validate()

    # -- 验证 -------------------------------------------------------------

    def validate(self) -> "TaskSpecV13":
        if self.split not in SPLITS:
            raise TaskSpecError("SPLIT_INVALID", self.split)
        try:
            scene = S.scene_doc(self.mechanism_id)
        except S.SceneError as exc:
            raise TaskSpecError("MECHANISM_UNKNOWN", self.mechanism_id) from None
        if self.scene_version != scene.version:
            raise TaskSpecError("SCENE_VERSION",
                                f"{self.scene_version} != {scene.version}")
        if not self.public_question.strip():
            raise TaskSpecError("QUESTION_EMPTY", self.task_id)
        ids = [c.id for c in self.conditions]
        if not ids or len(set(ids)) != len(ids):
            raise TaskSpecError("CONDITIONS_INVALID", "条件为空或 id 重复")
        if len(ids) > P.MAX_CONFIRM_CONDITIONS:
            raise TaskSpecError("CONDITIONS_INVALID",
                                f"{len(ids)} > {P.MAX_CONFIRM_CONDITIONS}")
        if any(c.scene_id != self.mechanism_id for c in self.conditions):
            raise TaskSpecError("CONDITIONS_INVALID", "条件场景与任务机制不一致")
        self.primary.validate()
        if self.primary.metric != self.estimand.get("metric_id"):
            raise TaskSpecError("ESTIMAND_INVALID", "primary.metric 与 estimand 不一致")
        if self.primary.id not in self.estimand.get("contrast_ids", []):
            raise TaskSpecError("ESTIMAND_INVALID",
                                "estimand.contrast_ids 未包含 primary")
        missing = sorted(set(self.primary.condition_ids) - set(ids))
        if missing:
            raise TaskSpecError("CONDITIONS_INVALID",
                                f"primary 引用未登记条件 {missing}")
        if self.primary.kind == X.KIND_DID:
            self._validate_did_structure()
        elif self.primary.kind == X.KIND_PAIRWISE and len(self.conditions) < 2:
            raise TaskSpecError("CONDITIONS_INVALID", "pairwise 至少两条件")
        # 允许空间必须与场景域一致（不另造一套界）
        for pid, spec in self.allowed_design_space.items():
            try:
                scene.param(pid)
            except S.SceneError:
                raise TaskSpecError("DESIGN_SPACE", f"未知参数 {pid}") from None
            if not isinstance(spec, Mapping) or "domain" not in spec:
                raise TaskSpecError("DESIGN_SPACE", f"{pid} 缺 domain")
        # nuisance：mode/assignment_owner 必填，varying 的支撑域必须在场景域内
        for name, raw in self.nuisance_schema.items():
            if not isinstance(raw, Mapping):
                raise TaskSpecError("NUISANCE_INVALID", name)
            if raw.get("mode") not in ("fixed", "varying"):
                raise TaskSpecError("NUISANCE_INVALID", f"{name}.mode")
            if raw.get("assignment_owner") not in ("host", "candidate"):
                raise TaskSpecError("NUISANCE_INVALID", f"{name}.assignment_owner")
            if raw["mode"] == "varying":
                support = raw.get("support")
                if not isinstance(support, (list, tuple)) or len(support) != 2:
                    raise TaskSpecError("NUISANCE_INVALID", f"{name}.support")
                try:
                    param = scene.param(name)
                except S.SceneError:
                    raise TaskSpecError("NUISANCE_INVALID",
                                        f"未知 nuisance 参数 {name}") from None
                lo, hi = Decimal(str(support[0])), Decimal(str(support[1]))
                if lo > hi:
                    raise TaskSpecError("NUISANCE_INVALID", f"{name} 区间倒置")
                if param.lo is not None and (lo < param.lo or hi > param.hi):
                    raise TaskSpecError("NUISANCE_INVALID",
                                        f"{name} 支撑越出场景域")
        if self.negative_control_spec is not None:
            self._validate_negative_control()
        elif self.mechanism_id == S.SCENE_A:
            raise TaskSpecError("NEGATIVE_CONTROL_REQUIRED",
                                "A 机制任务必须声明有效阴性控制")
        return self

    def _validate_did_structure(self) -> None:
        """四条件必须构成 2×2 全因子，且权重与因子水平一致。"""
        by_id = {c.id: c for c in self.conditions}
        four = [by_id[cid] for cid in self.primary.condition_ids]
        keys = set(four[0].params)
        varying = sorted(k for k in keys
                         if len({c.params[k] for c in four}) > 1)
        if len(varying) != 2:
            raise TaskSpecError(
                "DID_NOT_FACTORIAL",
                f"四条件必须恰在两个参数上变化，实为 {varying}")
        levels = {k: sorted({c.params[k] for c in four}) for k in varying}
        if any(len(v) != 2 for v in levels.values()):
            raise TaskSpecError("DID_NOT_FACTORIAL", f"水平数 {levels}")
        combos = {(c.params[varying[0]], c.params[varying[1]]) for c in four}
        if len(combos) != 4:
            raise TaskSpecError("DID_NOT_FACTORIAL", "四条件不是全组合")
        weights = self.primary.weights
        # 2×2 交互项的权重模式：同号两项必须在**两个**因子上都不同（对角），
        # 且正负各两条。等价于 w ∝ (x−.5)(y−.5)：任何可加主效应模型下估计为 0。
        pos = [c for c in four if weights[c.id] > 0]
        neg = [c for c in four if weights[c.id] < 0]
        if len(pos) != 2 or len(neg) != 2:
            raise TaskSpecError("DID_NOT_FACTORIAL", "正负项各须两条")

        def diff(a: C.Condition, b: C.Condition) -> "list[str]":
            return sorted(k for k in keys if a.params.get(k) != b.params.get(k))

        if diff(pos[0], pos[1]) != varying or diff(neg[0], neg[1]) != varying:
            raise TaskSpecError(
                "DID_NOT_FACTORIAL",
                "同号两项必须在两个因子上都不同（对角），否则不是交互项")

    def _validate_negative_control(self) -> None:
        spec = self.negative_control_spec or {}
        contrast = spec.get("contrast")
        if not isinstance(contrast, Mapping):
            raise TaskSpecError("NEGATIVE_CONTROL_INVALID", "缺 contrast")
        nc = X.ContrastV13.from_dict(contrast,
                                     where=f"{self.task_id}.negative_control")
        nc.validate()
        if self.mechanism_id != S.SCENE_A:
            raise TaskSpecError("NEGATIVE_CONTROL_INVALID",
                                "只有 A 机制登记阴性控制")
        if nc.metric != "displacement_x_m_negative_equivalence":
            raise TaskSpecError("NEGATIVE_CONTROL_INVALID", "阴性指标不符")
        if nc.proposition != "equivalent":
            raise TaskSpecError("NEGATIVE_CONTROL_INVALID", "阴性主张必须是 equivalent")
        by_id = {c.id: c for c in self.conditions}
        missing = sorted(set(nc.condition_ids) - set(by_id))
        if missing:
            raise TaskSpecError("NEGATIVE_CONTROL_INVALID",
                                f"引用未登记条件 {missing}")
        a, b = (by_id[cid] for cid in nc.condition_ids)
        diff = sorted(k for k in a.params if a.params.get(k) != b.params.get(k))
        if diff != list(R.NEGATIVE_CONTROL_ALLOWED_DIFF):
            raise TaskSpecError(
                "NEGATIVE_CONTROL_INVALID",
                f"sham 只允许差 {list(R.NEGATIVE_CONTROL_ALLOWED_DIFF)}，实为 {diff}")

    # -- 比较 -------------------------------------------------------------

    def intervention_signature(self) -> "tuple[str, ...]":
        """不含 nuisance 的干预签名：条件之间实际变化的参数集。"""
        nuisance = {k for k, v in self.nuisance_schema.items()
                    if v.get("mode") == "varying"}
        keys = set(self.conditions[0].params)
        varying = {k for k in keys
                   if len({c.params[k] for c in self.conditions}) > 1}
        return tuple(sorted(varying - nuisance))


def assert_not_near_duplicate(old: TaskSpecV13, new: TaskSpecV13) -> None:
    """AC07：只有 seed/参数随机数变化的任务不是新留出任务。"""
    if new.mechanism_id != old.mechanism_id:
        raise TaskSpecError("MECHANISM_MISMATCH",
                            f"{new.task_id} 与 {old.task_id} 机制不同，不能互为近重复对照")
    same_sig = new.intervention_signature() == old.intervention_signature()
    same_estimand = (new.primary.kind == old.primary.kind
                     and new.primary.metric == old.primary.metric)
    if same_sig and same_estimand:
        raise TaskSpecError(
            "TASK_NEAR_DUPLICATE",
            (f"{new.task_id} 与 {old.task_id} 的干预签名 {list(new.intervention_signature())} "
             f"与估计量类型 ({new.primary.kind}, {new.primary.metric}) 均未变化；"
             "换 seed/参数随机数不算新任务"))


def _metric_method_sha() -> str:
    """量表 + 统计方法 + 对比 schema 的单一摘要（任务绑定的方法面）。"""
    body = {
        "rubric": R.rubric_sha256(),
        "statistics": {
            "schema": "simpraxis.rsi_r1.statistics.v1",
            "resamples": R.BOOTSTRAP_RESAMPLES,
            "unit": R.BOOTSTRAP_UNIT,
            "quantile": R.BOOTSTRAP_QUANTILE,
            "primary_level": str(R.CI_PRIMARY),
            "negative_level": str(R.CI_NEGATIVE),
            "seed": 20261111,
        },
        "contrast": X.public_schema()["rules"],
        "metrics": {k: {"delta": str(v["delta"]), "unit": v["unit"],
                        "study": v["study"], "censoring": v["censoring"]}
                    for k, v in R.METRICS.items()},
    }
    return hashlib.sha256(json.dumps(
        body, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 具体任务（T1 冻结；数值由场景合法域校准，不在 Plan 里猜）
# ---------------------------------------------------------------------------

def _allowed_space(scene_id: str) -> "dict[str, Any]":
    scene = S.scene_doc(scene_id)
    out: "dict[str, Any]" = {}
    for param in scene.params:
        entry: "dict[str, Any]" = {"unit": param.unit, "role": param.role}
        if param.kind == "enum":
            entry["domain"] = [str(v) for v in param.values]
        else:
            entry["domain"] = [str(param.lo), str(param.hi)]
        out[param.id] = entry
    return out


def _fixed_of(scene_id: str) -> "dict[str, str]":
    scene = S.scene_doc(scene_id)
    return {f.id: str(f.value) for f in scene.fixed}


def build_new_a(*, commitment: "str | None" = None,
                resource_sha: "str | None" = None) -> TaskSpecV13:
    scene_id = S.SCENE_A
    conditions = (
        _condition("hi_base", scene_id, {"mass_kg": "0.5", "v0_mps": "0.6",
                                         "sliding_friction": "0.4",
                                         "torsional_friction_m": "0.01"}),
        _condition("hi_low_mu", scene_id, {"mass_kg": "0.5", "v0_mps": "0.6",
                                           "sliding_friction": "0.2",
                                           "torsional_friction_m": "0.01"}),
        _condition("lo_base", scene_id, {"mass_kg": "0.5", "v0_mps": "0.2",
                                         "sliding_friction": "0.4",
                                         "torsional_friction_m": "0.01"}),
        _condition("lo_low_mu", scene_id, {"mass_kg": "0.5", "v0_mps": "0.2",
                                           "sliding_friction": "0.2",
                                           "torsional_friction_m": "0.01"}),
        _condition("sham", scene_id, {"mass_kg": "0.5", "v0_mps": "0.6",
                                      "sliding_friction": "0.4",
                                      "torsional_friction_m": "0.02"}),
    )
    primary = X.ContrastV13(
        id="friction_x_speed",
        kind=X.KIND_DID,
        metric="displacement_x_m",
        proposition="increase_gt",
        delta=R.metric_delta("displacement_x_m"),
        terms=X.did_terms(treatment_high="hi_low_mu", treatment_low="hi_base",
                          control_high="lo_low_mu", control_low="lo_base"),
    )
    nc = X.ContrastV13(
        id="negative_torsional",
        kind=X.KIND_PAIRWISE,
        metric="displacement_x_m_negative_equivalence",
        proposition="equivalent",
        delta=R.metric_delta("displacement_x_m_negative_equivalence"),
        terms=(("hi_base", Decimal("1")), ("sham", Decimal("-1"))),
    )
    spec = TaskSpecV13(
        task_id=NEW_A_TASK_ID,
        mechanism_id=scene_id,
        split=SPLIT_HOLDOUT,
        scene_version=S.scene_doc(scene_id).version,
        public_question=(
            "在水平面单刚体滑动中，滑动摩擦对 4 s 净位移的效应是否随初速度改变？"
            "主估计量 = 高低初速下（滑动摩擦 0.4→0.2 的位移增量）之差。"),
        allowed_design_space=_allowed_space(scene_id),
        estimand={
            "metric_id": "displacement_x_m",
            "contrast_ids": ["friction_x_speed", "negative_torsional"],
            "target_population": "32 个宿主分配的确认 block（质量 nuisance）",
            "aggregation": "paired_block_linear_contrast",
            "weighting": "equal_block_weight",
        },
        nuisance_schema={
            "mass_kg": {"mode": "varying", "support": ["0.2", "1.0"],
                        "assignment_owner": "host",
                        "note": "块内等质量；Coulomb 极限下位移与质量无关，用作无效应 nuisance"},
        },
        negative_control_spec={
            "contrast": nc.to_dict(),
            "expectation": "90% 区间严格落在 ±0.001 m 内（等效）",
            "note": "sham 只改 torsional_friction_m；condim 恒 3",
        },
        private_instance_commitment=commitment or ("0" * 64),
        metric_method_sha=_metric_method_sha(),
        resource_spec_sha=resource_sha or ("0" * 64),
        conditions=conditions,
        primary=primary,
        fixed={**_fixed_of(scene_id),
               "confirm_blocks": str(P.MAX_CONFIRM_BLOCKS),
               "host_nuisance": "mass_kg"},
    )
    return spec.validate()


def build_new_b(*, commitment: "str | None" = None,
                resource_sha: "str | None" = None) -> TaskSpecV13:
    scene_id = S.SCENE_B
    base = {"controller": "PD", "dt_s": "0.001", "kp": "5", "kd": "0.1",
            "target_amplitude_rad": "0.2", "target_freq_hz": "1.0"}
    conditions = (
        _condition("s_low", scene_id, {**base, "tc_s": "0.008",
                                       "inertia_ratio": "0.5"}),
        _condition("s_high", scene_id, {**base, "tc_s": "0.008",
                                        "inertia_ratio": "2.0"}),
        _condition("l_low", scene_id, {**base, "tc_s": "0.032",
                                       "inertia_ratio": "0.5"}),
        _condition("l_high", scene_id, {**base, "tc_s": "0.032",
                                        "inertia_ratio": "2.0"}),
    )
    primary = X.ContrastV13(
        id="tc_x_inertia",
        kind=X.KIND_DID,
        metric="normalized_tracking_rmse",
        proposition="increase_gt",
        delta=R.metric_delta("normalized_tracking_rmse"),
        terms=X.did_terms(treatment_high="l_high", treatment_low="s_high",
                          control_high="l_low", control_low="s_low"),
    )
    spec = TaskSpecV13(
        task_id=NEW_B_TASK_ID,
        mechanism_id=scene_id,
        split=SPLIT_HOLDOUT,
        scene_version=S.scene_doc(scene_id).version,
        public_question=(
            "单关节跟踪中，控制采样周期 Tc（0.008→0.032 s）对归一化跟踪 RMSE 的"
            "效应是否随惯量比（0.5→2.0）改变？主估计量 = 两惯量水平下 Tc 效应之差。"),
        allowed_design_space=_allowed_space(scene_id),
        estimand={
            "metric_id": "normalized_tracking_rmse",
            "contrast_ids": ["tc_x_inertia"],
            "target_population": "32 个宿主分配的确认 block（目标频率 nuisance）",
            "aggregation": "paired_block_linear_contrast",
            "weighting": "equal_block_weight",
        },
        nuisance_schema={
            "target_freq_hz": {"mode": "varying", "support": ["0.2", "2.0"],
                               "assignment_owner": "host",
                               "note": "块内同频；频率是跟踪难度的强工况变量"},
        },
        negative_control_spec=None,
        private_instance_commitment=commitment or ("0" * 64),
        metric_method_sha=_metric_method_sha(),
        resource_spec_sha=resource_sha or ("0" * 64),
        conditions=conditions,
        primary=primary,
        fixed={**_fixed_of(scene_id),
               "confirm_blocks": str(P.MAX_CONFIRM_BLOCKS),
               "host_nuisance": "target_freq_hz"},
    )
    return spec.validate()


def build_old_a() -> TaskSpecV13:
    """round R 的 A 任务：development，仅作近重复对照（数值结果不公开给新候选）。"""
    scene_id = S.SCENE_A
    conditions = (
        _condition("base", scene_id, {"mass_kg": "0.5", "v0_mps": "0.4",
                                      "sliding_friction": "0.4",
                                      "torsional_friction_m": "0.01"}),
        _condition("low_mu", scene_id, {"mass_kg": "0.5", "v0_mps": "0.4",
                                        "sliding_friction": "0.2",
                                        "torsional_friction_m": "0.01"}),
        _condition("sham", scene_id, {"mass_kg": "0.5", "v0_mps": "0.4",
                                      "sliding_friction": "0.4",
                                      "torsional_friction_m": "0.02"}),
    )
    primary = X.ContrastV13(
        id="friction_main", kind=X.KIND_PAIRWISE, metric="displacement_x_m",
        proposition="increase_gt", delta=R.metric_delta("displacement_x_m"),
        terms=(("low_mu", Decimal("1")), ("base", Decimal("-1"))))
    return TaskSpecV13(
        task_id=OLD_A_TASK_ID, mechanism_id=scene_id, split=SPLIT_DEVELOPMENT,
        scene_version=1,
        public_question="（开发集）滑动摩擦 0.4→0.2 对 4 s 净位移的主效应。",
        allowed_design_space=_allowed_space(scene_id),
        estimand={"metric_id": "displacement_x_m",
                  "contrast_ids": ["friction_main"],
                  "target_population": "round R 确认 block",
                  "aggregation": "paired_block_mean",
                  "weighting": "equal_block_weight"},
        nuisance_schema={"mass_kg": {"mode": "fixed", "support": ["0.5", "0.5"],
                                     "assignment_owner": "candidate"}},
        negative_control_spec={"contrast": X.ContrastV13(
            id="negative_torsional", kind=X.KIND_PAIRWISE,
            metric="displacement_x_m_negative_equivalence",
            proposition="equivalent",
            delta=R.metric_delta("displacement_x_m_negative_equivalence"),
            terms=(("base", Decimal("1")), ("sham", Decimal("-1")))).to_dict()},
        private_instance_commitment="1" * 64,
        metric_method_sha=_metric_method_sha(),
        resource_spec_sha="1" * 64,
        conditions=conditions, primary=primary,
        fixed=_fixed_of(scene_id)).validate()


def build_old_b() -> TaskSpecV13:
    scene_id = S.SCENE_B
    base = {"controller": "PD", "dt_s": "0.001", "kp": "5", "kd": "0.1",
            "target_amplitude_rad": "0.2", "target_freq_hz": "1.0",
            "inertia_ratio": "1.0"}
    conditions = (
        _condition("base", scene_id, {**base, "tc_s": "0.008"}),
        _condition("tc_only", scene_id, {**base, "tc_s": "0.016"}),
        _condition("dt_only", scene_id, {**base, "tc_s": "0.008",
                                         "dt_s": "0.0005"}),
    )
    primary = X.ContrastV13(
        id="tc_main", kind=X.KIND_PAIRWISE, metric="normalized_tracking_rmse",
        proposition="increase_gt", delta=R.metric_delta("normalized_tracking_rmse"),
        terms=(("tc_only", Decimal("1")), ("base", Decimal("-1"))))
    return TaskSpecV13(
        task_id=OLD_B_TASK_ID, mechanism_id=scene_id, split=SPLIT_DEVELOPMENT,
        scene_version=1,
        public_question="（开发集）控制周期 Tc 0.008→0.016 对跟踪 RMSE 的主效应。",
        allowed_design_space=_allowed_space(scene_id),
        estimand={"metric_id": "normalized_tracking_rmse",
                  "contrast_ids": ["tc_main"],
                  "target_population": "round R 确认 block",
                  "aggregation": "paired_block_mean",
                  "weighting": "equal_block_weight"},
        nuisance_schema={"inertia_ratio": {"mode": "varying",
                                           "support": ["0.5", "2.0"],
                                           "assignment_owner": "host"}},
        negative_control_spec=None,
        private_instance_commitment="1" * 64,
        metric_method_sha=_metric_method_sha(),
        resource_spec_sha="1" * 64,
        conditions=conditions, primary=primary,
        fixed=_fixed_of(scene_id)).validate()


def build_taskset(*, a_commitment: str, b_commitment: str,
                  a_resource_sha: str, b_resource_sha: str,
                  difference_table_sha: str) -> "dict[str, Any]":
    """T1 任务集清单：两留出 + 两开发，含近重复机判断言。"""
    old_a, old_b = build_old_a(), build_old_b()
    new_a = build_new_a(commitment=a_commitment, resource_sha=a_resource_sha)
    new_b = build_new_b(commitment=b_commitment, resource_sha=b_resource_sha)
    assert_not_near_duplicate(old_a, new_a)
    assert_not_near_duplicate(old_b, new_b)
    if new_a.mechanism_id == new_b.mechanism_id:
        raise TaskSpecError("MECHANISM_MISMATCH", "两留出任务必须是不同机制")
    if new_a.split != SPLIT_HOLDOUT or new_b.split != SPLIT_HOLDOUT:
        raise TaskSpecError("SPLIT_INVALID", "留出任务 split 必须是 holdout")
    return {
        "schema": P.TASKSET_MANIFEST_SCHEMA_V13,
        "schema_version": 1,
        "profile_id": P.PROFILE_R13,
        "split_policy": ("split 由宿主注册；旧 R1.2 A/B 一律 development，"
                         "不得向新候选提供其数值/分层/结论"),
        "difference_table_sha256": difference_table_sha,
        "near_duplicate_checks": [
            {"old": old_a.task_id, "new": new_a.task_id,
             "old_signature": list(old_a.intervention_signature()),
             "new_signature": list(new_a.intervention_signature()),
             "old_estimand": old_a.primary.kind,
             "new_estimand": new_a.primary.kind, "ok": True},
            {"old": old_b.task_id, "new": new_b.task_id,
             "old_signature": list(old_b.intervention_signature()),
             "new_signature": list(new_b.intervention_signature()),
             "old_estimand": old_b.primary.kind,
             "new_estimand": new_b.primary.kind, "ok": True},
        ],
        "tasks": [old_a.to_dict(), old_b.to_dict(), new_a.to_dict(),
                  new_b.to_dict()],
        "holdout_task_ids": [new_a.task_id, new_b.task_id],
        "development_task_ids": [old_a.task_id, old_b.task_id],
    }


def taskset_sha256(doc: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(
        doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")).hexdigest()
