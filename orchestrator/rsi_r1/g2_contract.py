"""RSI-G2 版本化最小交付合同（ResultEnvelopeV2 + ScopeContractV1）。

Plan `~/文档/RSI_G2_Plan_detailed.md` §2/§2.4/§2.5/§2.6。**零模型 POST / 零物理**：
本模块只做纯投影、结构校验、只读分页与旁路捕获，不导入 sender/gateway/物理入口。

边界（Plan §2.1）：

* 原子交付单元 = ``primary + scope + analysis_receipt_sha256 + evidence/artifact
  handles + envelope_sha256``；任何必需字段缺失或超 16 KiB ⇒ ``RESULT_VIEW_INCOMPLETE``，
  绝不发送「看似成功但缺 primary」的部分对象；``summary`` 只是显示，可丢；
* legacy（R1/R1.2/R1.3）的 payload 与 4096 裁剪规则在本模块之外原样不动；
* scope 枚举/聚合/权重/删失/覆盖全部公开，expected 只能引用公开合同；
* 历史无原件只能 ``MISSING``/``UNKNOWN``——本模块不为历史生成「观测」。
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from orchestrator.common import research_transport as T
from orchestrator.common.clock import utc_now_iso_z
from orchestrator.common.fsutil import atomic_write_text, ensure_private_dir
from orchestrator.rsi_r1 import g1_trace as GT

# ---------------------------------------------------------------------------
# 常量（单一真值；transport 侧只做结构快速检查）
# ---------------------------------------------------------------------------

POLICY_LEGACY = T.POLICY_LEGACY
POLICY_G2_ATOMIC = T.POLICY_G2_ATOMIC
DELIVERY_POLICIES = T.DELIVERY_POLICIES

ENVELOPE_SCHEMA = "simpraxis.rsi_g2.result_envelope.v1"
SCOPE_CONTRACT_SCHEMA = "simpraxis.rsi_g2.scope_contract.v1"
SCIENCE_SCHEMA = "simpraxis.rsi_g2.science_assessment.v1"
CONTRACT_SNAPSHOT_SCHEMA = "simpraxis.rsi_g2.contract.v1"
CASE_SCHEMA = "simpraxis.rsi_g2.case.v1"

ENVELOPE_MAX_BYTES = T.G2_ENVELOPE_MAX_BYTES
PAGE_MAX_BYTES = T.G2_PAGE_MAX_BYTES
ARTIFACT_MAX_BYTES = T.G2_ARTIFACT_MAX_BYTES
SUMMARY_MAX_BYTES = 512
PROJECTION_VERSION = "g2_projection_v1"
CAPTURE_VERSION = "g2_capture_v1"

TARGET_POPULATIONS = ("fixed_point", "registered_distribution",
                      "registered_subgroup")
AGGREGATIONS = ("mean", "median", "paired_mean")
WEIGHTINGS = ("equal", "registered")
COVERAGES = ("observed_blocks", "registered_population", "fixed_point", "unknown")
SEMANTIC_LABELS = ("SUPPORTED", "REFUTED", "INCONCLUSIVE",
                   "INCONSISTENT_EVIDENCE")
SCIENCE_STATUSES = ("SUPPORTED", "REFUTED", "INCONCLUSIVE", "NOT_MEASURED")

#: 原子单元必需键（transport 的同名常量必须与此一致，见 tests/test_rsi_g2_contract）。
ENVELOPE_REQUIRED = (
    "schema", "schema_version", "study_id", "phase", "policy_id",
    "plan_sha256", "analysis_receipt_sha256", "analysis_output_sha256",
    "scope_contract_sha256", "scope", "required_contrast_ids", "contrasts",
    "primary", "diagnostics", "artifacts", "evidence", "summary_sha256",
    "envelope_sha256", "truncation",
)
ENVELOPE_OPTIONAL = ("summary", "scope_contract_text")
CONTRAST_FIELDS = ("contrast_id", "metric_id", "proposition", "delta",
                   "estimate", "lower", "upper", "level", "science_status",
                   "n_blocks", "dropped")
ARTIFACT_FIELDS = ("artifact_id", "relative_path", "sha256", "bytes",
                   "access_mode")
EVIDENCE_FIELDS = ("artifact_id", "json_pointer", "field_sha256")

#: 只允许候选自产的 analysis 前缀（与 G1 FileArtifactStore 同窄面）。
_ALLOWED_ARTIFACT_PREFIX = "analysis/"
_FORBIDDEN_PREFIXES = ("private/", "judge/", "observations/", "confirm/",
                       "raw/", "oracle/", "private_expected/")
_SAFE_RELPATH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,255}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class G2Error(RuntimeError):
    """G2 工程面错误：``code`` 可机读，不是科学结论。"""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"[{code}] {detail}" if detail else code)


# ---------------------------------------------------------------------------
# 规范序列化（Plan §2.6：固定 Python 3.10、拒重复键/NaN/Inf/bool-as-number）
# ---------------------------------------------------------------------------

def _reject_constant(value: str) -> Any:
    raise G2Error("NAN_OR_INF", f"JSON 常量 {value}")


def _no_duplicates(pairs: "list[tuple[str, Any]]") -> "dict[str, Any]":
    out: "dict[str, Any]" = {}
    for key, value in pairs:
        if key in out:
            raise G2Error("DUPLICATE_KEY", key)
        out[key] = value
    return out


def parse_json_strict(text: "str | bytes") -> Any:
    """严格 JSON：重复键 / NaN / Infinity 一律拒（与 g1_trace.canonical 同口径）。"""
    if isinstance(text, bytes):
        try:
            text = text.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise G2Error("BAD_UTF8", str(exc)) from None
    try:
        return json.loads(text, object_pairs_hook=_no_duplicates,
                          parse_constant=_reject_constant)
    except G2Error:
        raise
    except (ValueError, TypeError) as exc:
        raise G2Error("BAD_JSON", str(exc)) from None


def canonical(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def field_sha(value: Any) -> str:
    return sha_bytes(canonical(value))


def content_sha(doc: Mapping[str, Any],
                *, exclude: Sequence[str] = ()) -> str:
    """内容 hash：排除自身 hash 与外部签名字段（Plan §2.6）。"""
    drop = set(exclude)
    return field_sha({k: v for k, v in doc.items() if k not in drop})


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _require_finite(value: Any, *, where: str) -> float:
    if not _is_number(value) or not math.isfinite(float(value)):
        raise G2Error("NAN_OR_INF", where)
    return float(value)


def _require_hex64(value: Any, *, where: str) -> str:
    if not isinstance(value, str) or not _HEX64.match(value):
        raise G2Error("FIELD_INVALID", f"{where} 必须是 64 hex")
    return value


def _safe_relpath(relpath: Any, *, where: str = "relative_path") -> str:
    if not isinstance(relpath, str) or not _SAFE_RELPATH.match(relpath):
        raise G2Error("ARTIFACT_PATH", f"{where}={relpath!r}")
    parts = relpath.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise G2Error("ARTIFACT_PATH", f"{where}={relpath!r}")
    if relpath.startswith(_FORBIDDEN_PREFIXES):
        raise G2Error("ARTIFACT_FORBIDDEN", relpath)
    return relpath


# ---------------------------------------------------------------------------
# ScopeContractV1（独立文件，绑定原 TaskSpec sha；不改 TaskSpecV13/TASK_KEYS）
# ---------------------------------------------------------------------------

SCOPE_KEYS = frozenset({
    "schema", "schema_version", "contract_id", "task_id", "task_spec_sha256",
    "hypothesis_id", "estimand_id", "metric_id", "proposition", "delta",
    "unit", "target_population", "aggregation", "weighting",
    "block_to_contrast_formula", "formula_notes", "censoring_rule",
    "coverage", "interval_level", "allowed_semantic_labels", "notes",
    # 落盘件可带自身内容 hash；读回时逐字复核（不参与内容 hash）。
    "contract_sha256",
})


@dataclass(frozen=True)
class ScopeContractV1:
    """公开 scope 合同：枚举 + 公式 + 删失 + 覆盖，全部对候选可见。"""

    contract_id: str
    task_id: str
    task_spec_sha256: str
    hypothesis_id: str
    estimand_id: str
    metric_id: str
    proposition: str
    delta: float
    unit: str
    target_population: str
    aggregation: str
    weighting: str
    block_to_contrast_formula: str
    formula_notes: str
    censoring_rule: str
    coverage: str
    interval_level: float
    allowed_semantic_labels: "tuple[str, ...]" = SEMANTIC_LABELS
    notes: str = ""

    def validate(self) -> "ScopeContractV1":
        for name in ("contract_id", "task_id", "hypothesis_id", "estimand_id",
                     "metric_id", "proposition", "unit", "censoring_rule",
                     "formula_notes"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise G2Error("SCOPE_CONTRACT_UNDEFINED", name)
        _require_hex64(self.task_spec_sha256, where="task_spec_sha256")
        _require_finite(self.delta, where="delta")
        if self.target_population not in TARGET_POPULATIONS:
            raise G2Error("SCOPE_CONTRACT_UNDEFINED",
                          f"target_population={self.target_population!r}")
        if self.aggregation not in AGGREGATIONS:
            raise G2Error("SCOPE_CONTRACT_UNDEFINED",
                          f"aggregation={self.aggregation!r}")
        if self.weighting not in WEIGHTINGS:
            raise G2Error("SCOPE_CONTRACT_UNDEFINED",
                          f"weighting={self.weighting!r}")
        if self.block_to_contrast_formula not in AGGREGATIONS:
            raise G2Error("SCOPE_CONTRACT_UNDEFINED",
                          "block_to_contrast_formula 必须是登记枚举之一")
        if self.block_to_contrast_formula != self.aggregation:
            raise G2Error("SCOPE_CONTRACT_UNDEFINED",
                          "block_to_contrast_formula 与 aggregation 不一致")
        if self.coverage not in COVERAGES:
            raise G2Error("SCOPE_CONTRACT_UNDEFINED",
                          f"coverage={self.coverage!r}")
        level = _require_finite(self.interval_level, where="interval_level")
        if not 0.0 < level < 1.0:
            raise G2Error("SCOPE_CONTRACT_UNDEFINED", "interval_level")
        labels = tuple(self.allowed_semantic_labels)
        if not labels or any(label not in SEMANTIC_LABELS for label in labels):
            raise G2Error("SCOPE_CONTRACT_UNDEFINED", "allowed_semantic_labels")
        return self

    def to_dict(self) -> "dict[str, Any]":
        return {
            "schema": SCOPE_CONTRACT_SCHEMA, "schema_version": 1,
            "contract_id": self.contract_id, "task_id": self.task_id,
            "task_spec_sha256": self.task_spec_sha256,
            "hypothesis_id": self.hypothesis_id,
            "estimand_id": self.estimand_id, "metric_id": self.metric_id,
            "proposition": self.proposition, "delta": float(self.delta),
            "unit": self.unit,
            "target_population": self.target_population,
            "aggregation": self.aggregation, "weighting": self.weighting,
            "block_to_contrast_formula": self.block_to_contrast_formula,
            "formula_notes": self.formula_notes,
            "censoring_rule": self.censoring_rule, "coverage": self.coverage,
            "interval_level": float(self.interval_level),
            "allowed_semantic_labels": list(self.allowed_semantic_labels),
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, doc: Any) -> "ScopeContractV1":
        if not isinstance(doc, Mapping):
            raise G2Error("WRONG_TYPE", "scope contract 必须是对象")
        unknown = sorted(set(doc) - SCOPE_KEYS)
        missing = sorted(SCOPE_KEYS - set(doc)
                         - {"notes", "formula_notes", "contract_sha256"})
        if unknown or missing:
            raise G2Error("SCOPE_CONTRACT_UNDEFINED",
                          f"未知 {unknown} 缺 {missing}")
        if doc.get("schema") != SCOPE_CONTRACT_SCHEMA:
            raise G2Error("SCHEMA_MISMATCH", str(doc.get("schema")))
        if doc.get("schema_version") != 1:
            raise G2Error("SCHEMA_MISMATCH", "schema_version")
        contract = cls(
            contract_id=str(doc["contract_id"]),
            task_id=str(doc["task_id"]),
            task_spec_sha256=str(doc["task_spec_sha256"]),
            hypothesis_id=str(doc["hypothesis_id"]),
            estimand_id=str(doc["estimand_id"]),
            metric_id=str(doc["metric_id"]),
            proposition=str(doc["proposition"]),
            delta=doc["delta"],
            unit=str(doc["unit"]),
            target_population=str(doc["target_population"]),
            aggregation=str(doc["aggregation"]),
            weighting=str(doc["weighting"]),
            block_to_contrast_formula=str(doc["block_to_contrast_formula"]),
            formula_notes=str(doc.get("formula_notes") or ""),
            censoring_rule=str(doc["censoring_rule"]),
            coverage=str(doc["coverage"]),
            interval_level=doc["interval_level"],
            allowed_semantic_labels=tuple(doc["allowed_semantic_labels"]),
            notes=str(doc.get("notes") or ""),
        ).validate()
        recorded = doc.get("contract_sha256")
        if recorded is not None and str(recorded) != contract.sha256:
            raise G2Error("SCOPE_CONTRACT_MISMATCH", "contract_sha256 不符")
        return contract

    @property
    def sha256(self) -> str:
        return content_sha(self.to_dict())

    def render_public_text(self) -> str:
        """公开短文本（进入候选视图；renderer 只读本文件，不读 expected）。"""
        return "\n".join([
            f"scope_contract_id: {self.contract_id}",
            f"task_id: {self.task_id} (task_spec_sha256={self.task_spec_sha256})",
            f"hypothesis_id: {self.hypothesis_id}; estimand_id: {self.estimand_id}",
            f"metric_id: {self.metric_id}; proposition: {self.proposition}; "
            f"delta: {self.delta}; unit: {self.unit}",
            f"target_population: {self.target_population}",
            f"aggregation: {self.aggregation} (block_to_contrast_formula: "
            f"{self.block_to_contrast_formula}; {self.formula_notes})",
            f"weighting: {self.weighting}; coverage: {self.coverage}",
            f"censoring_rule: {self.censoring_rule}",
            f"interval_level: {self.interval_level}",
            f"allowed_semantic_labels: {', '.join(self.allowed_semantic_labels)}",
        ]) + "\n"


def load_scope_contract(path: "str | Path") -> ScopeContractV1:
    doc = parse_json_strict(Path(path).read_bytes())
    return ScopeContractV1.from_dict(doc)


def write_scope_contract(path: "str | Path", contract: ScopeContractV1) -> str:
    doc = contract.to_dict()
    doc["contract_sha256"] = contract.sha256
    target = Path(path)
    ensure_private_dir(target.parent)
    atomic_write_text(target, json.dumps(doc, ensure_ascii=False, sort_keys=True,
                                         indent=1) + "\n", mode=0o600)
    return contract.sha256


# ---------------------------------------------------------------------------
# ResultEnvelopeV2
# ---------------------------------------------------------------------------

def _contrast_record(raw: Mapping[str, Any]) -> "dict[str, Any]":
    missing = [k for k in CONTRAST_FIELDS if k not in raw]
    if missing:
        raise G2Error("RESULT_VIEW_INCOMPLETE", f"contrast 缺 {missing}")
    estimate = _require_finite(raw["estimate"], where="contrast.estimate")
    lower = _require_finite(raw["lower"], where="contrast.lower")
    upper = _require_finite(raw["upper"], where="contrast.upper")
    delta = _require_finite(raw["delta"], where="contrast.delta")
    level = _require_finite(raw["level"], where="contrast.level")
    if not 0.0 < level < 1.0:
        raise G2Error("ENVELOPE_INVALID", "contrast.level")
    if lower > upper:
        raise G2Error("ENVELOPE_INVALID", "contrast interval lower > upper")
    n_blocks = raw["n_blocks"]
    if not isinstance(n_blocks, int) or isinstance(n_blocks, bool) or n_blocks < 0:
        raise G2Error("ENVELOPE_INVALID", "contrast.n_blocks")
    dropped = raw["dropped"]
    if not isinstance(dropped, Mapping):
        raise G2Error("ENVELOPE_INVALID", "contrast.dropped")
    if raw["science_status"] not in SCIENCE_STATUSES:
        raise G2Error("ENVELOPE_INVALID", "contrast.science_status")
    for name in ("contrast_id", "metric_id", "proposition"):
        if not isinstance(raw[name], str) or not raw[name].strip():
            raise G2Error("ENVELOPE_INVALID", f"contrast.{name}")
    return {
        "contrast_id": raw["contrast_id"], "metric_id": raw["metric_id"],
        "proposition": raw["proposition"], "delta": delta,
        "estimate": estimate, "lower": lower, "upper": upper,
        "level": level, "science_status": raw["science_status"],
        "n_blocks": int(n_blocks),
        "dropped": {"count": int(dropped.get("count") or 0),
                    "ids": [str(x) for x in (dropped.get("ids") or [])][:64],
                    "ids_sha256": str(dropped.get("ids_sha256") or "")},
    }


def _artifact_record(raw: Mapping[str, Any]) -> "dict[str, Any]":
    missing = [k for k in ARTIFACT_FIELDS if k not in raw]
    if missing:
        raise G2Error("RESULT_VIEW_INCOMPLETE", f"artifact 缺 {missing}")
    rel = _safe_relpath(raw["relative_path"])
    if not rel.startswith(_ALLOWED_ARTIFACT_PREFIX):
        raise G2Error("ARTIFACT_FORBIDDEN", rel)
    sha = _require_hex64(raw["sha256"], where="artifact.sha256")
    n_bytes = raw["bytes"]
    if (not isinstance(n_bytes, int) or isinstance(n_bytes, bool)
            or not 0 <= n_bytes <= ARTIFACT_MAX_BYTES):
        raise G2Error("ARTIFACT_PATH", "artifact.bytes")
    if raw["access_mode"] != "read_result_artifact":
        raise G2Error("ENVELOPE_INVALID", "artifact.access_mode")
    artifact_id = raw["artifact_id"]
    if not isinstance(artifact_id, str) or not artifact_id.strip():
        raise G2Error("ENVELOPE_INVALID", "artifact.artifact_id")
    return {"artifact_id": artifact_id, "relative_path": rel, "sha256": sha,
            "bytes": int(n_bytes), "access_mode": "read_result_artifact"}


def _evidence_record(raw: Mapping[str, Any],
                     artifact_ids: "set[str]") -> "dict[str, Any]":
    missing = [k for k in EVIDENCE_FIELDS if k not in raw]
    if missing:
        raise G2Error("RESULT_VIEW_INCOMPLETE", f"evidence 缺 {missing}")
    if raw["artifact_id"] not in artifact_ids:
        raise G2Error("EVIDENCE_MISSING", str(raw["artifact_id"]))
    pointer = raw["json_pointer"]
    if not isinstance(pointer, str) or (pointer and not pointer.startswith("/")):
        raise G2Error("ENVELOPE_INVALID", "evidence.json_pointer")
    return {"artifact_id": raw["artifact_id"], "json_pointer": pointer,
            "field_sha256": _require_hex64(raw["field_sha256"],
                                           where="evidence.field_sha256")}


def build_envelope(*, study_id: str, phase: str, plan_sha256: str,
                   analysis_receipt_sha256: str, analysis_output_sha256: str,
                   scope_contract: ScopeContractV1,
                   required_contrast_ids: Sequence[str],
                   contrasts: Sequence[Mapping[str, Any]],
                   primary_contrast_id: str,
                   denominator: int, dropped: Mapping[str, Any],
                   diagnostics: Mapping[str, Any],
                   artifacts: Sequence[Mapping[str, Any]],
                   evidence: Sequence[Mapping[str, Any]],
                   summary: "Mapping[str, Any] | None" = None
                   ) -> "dict[str, Any]":
    """构造完整 envelope；任何必需项缺失/超帽即抛错（绝不部分交付）。"""
    contract = scope_contract.validate()
    _require_hex64(plan_sha256, where="plan_sha256")
    _require_hex64(analysis_receipt_sha256, where="analysis_receipt_sha256")
    _require_hex64(analysis_output_sha256, where="analysis_output_sha256")
    ids = [str(c) for c in required_contrast_ids]
    if not ids:
        raise G2Error("RESULT_VIEW_INCOMPLETE", "required_contrast_ids 为空")
    if len(set(ids)) != len(ids):
        raise G2Error("ENVELOPE_INVALID", "required_contrast_ids 重复")
    records = [_contrast_record(c) for c in contrasts]
    by_id = {r["contrast_id"]: r for r in records}
    missing = [cid for cid in ids if cid not in by_id]
    if missing:
        raise G2Error("RESULT_VIEW_INCOMPLETE",
                      f"必需对比未进入视图：{missing}")
    if primary_contrast_id not in by_id:
        raise G2Error("RESULT_VIEW_INCOMPLETE", "primary 不在 contrasts 中")
    if primary_contrast_id not in ids:
        raise G2Error("ENVELOPE_INVALID", "primary 不在 required_contrast_ids")
    primary = by_id[primary_contrast_id]
    arts = [_artifact_record(a) for a in artifacts]
    art_ids = {a["artifact_id"] for a in arts}
    if len(art_ids) != len(arts):
        raise G2Error("ENVELOPE_INVALID", "artifact_id 重复")
    ev = [_evidence_record(e, art_ids) for e in evidence]
    if not isinstance(denominator, int) or isinstance(denominator, bool) \
            or denominator < 0:
        raise G2Error("ENVELOPE_INVALID", "denominator")
    dropped_doc = {"count": int(dropped.get("count") or 0),
                   "ids": [str(x) for x in (dropped.get("ids") or [])][:64],
                   "ids_sha256": str(dropped.get("ids_sha256") or "")}
    scope = {
        "target_population": contract.target_population,
        "aggregation": contract.aggregation,
        "weighting": contract.weighting,
        "denominator": denominator,
        "dropped": dropped_doc,
        "coverage": contract.coverage,
    }
    doc: "dict[str, Any]" = {
        "schema": ENVELOPE_SCHEMA, "schema_version": 1,
        "study_id": str(study_id), "phase": str(phase),
        "policy_id": POLICY_G2_ATOMIC,
        "plan_sha256": plan_sha256,
        "analysis_receipt_sha256": analysis_receipt_sha256,
        "analysis_output_sha256": analysis_output_sha256,
        "scope_contract_sha256": contract.sha256,
        "scope_contract_text": contract.render_public_text()[:2048],
        "scope": scope,
        "required_contrast_ids": ids,
        "contrasts": records,
        "primary": {"estimand_id": contract.estimand_id,
                    "contrast_id": primary_contrast_id,
                    "metric_id": primary["metric_id"],
                    "estimate": primary["estimate"],
                    "interval": {"lower": primary["lower"],
                                 "upper": primary["upper"],
                                 "level": primary["level"]}},
        "diagnostics": dict(diagnostics),
        "artifacts": arts,
        "evidence": ev,
        "truncation": False,
    }
    summary_obj = dict(summary or {})
    if canonical(summary_obj) and len(canonical(summary_obj)) > SUMMARY_MAX_BYTES:
        raise G2Error("ENVELOPE_INVALID", "summary 超 512B（summary 只能作显示）")
    doc["summary"] = summary_obj
    doc["summary_sha256"] = field_sha(summary_obj)
    doc["envelope_sha256"] = content_sha(doc, exclude=("envelope_sha256",))
    measure = measure_envelope(doc)
    if not measure["ok"]:
        raise G2Error("CAPACITY_NOT_ESTABLISHED",
                      f"envelope {measure['n_bytes']}B > {ENVELOPE_MAX_BYTES}B")
    return doc


def measure_envelope(doc: Mapping[str, Any]) -> "dict[str, Any]":
    n_bytes = len(canonical(doc))
    return {"n_bytes": n_bytes, "max_bytes": ENVELOPE_MAX_BYTES,
            "ok": n_bytes <= ENVELOPE_MAX_BYTES}


def verify_envelope(doc: Any, *,
                    scope_contract: "ScopeContractV1 | None" = None
                    ) -> "dict[str, Any]":
    """完整结构校验（维护 checker 用）；失败抛 G2Error。"""
    if not isinstance(doc, Mapping):
        raise G2Error("ENVELOPE_INVALID", "必须是对象")
    unknown = sorted(set(doc) - set(ENVELOPE_REQUIRED) - set(ENVELOPE_OPTIONAL))
    missing = sorted(set(ENVELOPE_REQUIRED) - set(doc))
    if unknown or missing:
        raise G2Error("ENVELOPE_INVALID", f"未知 {unknown} 缺 {missing}")
    if doc["schema"] != ENVELOPE_SCHEMA or doc["schema_version"] != 1:
        raise G2Error("SCHEMA_MISMATCH", str(doc.get("schema")))
    if doc["policy_id"] != POLICY_G2_ATOMIC:
        raise G2Error("ENVELOPE_INVALID", "policy_id")
    if doc["truncation"] is not False:
        raise G2Error("ENVELOPE_INVALID", "truncation 必须为 false")
    for key in ("plan_sha256", "analysis_receipt_sha256",
                "analysis_output_sha256", "scope_contract_sha256"):
        _require_hex64(doc[key], where=key)
    ids = [str(c) for c in doc["required_contrast_ids"]]
    if not ids or len(set(ids)) != len(ids):
        raise G2Error("ENVELOPE_INVALID", "required_contrast_ids")
    records = [_contrast_record(c) for c in doc["contrasts"]]
    by_id = {r["contrast_id"]: r for r in records}
    missing = [cid for cid in ids if cid not in by_id]
    if missing:
        raise G2Error("RESULT_VIEW_INCOMPLETE", f"缺 {missing}")
    primary = doc["primary"]
    if not isinstance(primary, Mapping) or set(primary) != {
            "estimand_id", "contrast_id", "metric_id", "estimate", "interval"}:
        raise G2Error("RESULT_VIEW_INCOMPLETE", "primary 不完整")
    if primary["contrast_id"] not in by_id:
        raise G2Error("RESULT_VIEW_INCOMPLETE", "primary.contrast_id")
    if primary["contrast_id"] not in ids:
        raise G2Error("ENVELOPE_INVALID", "primary 不在必需集合")
    if not isinstance(primary["interval"], Mapping) or set(primary["interval"]) != {
            "lower", "upper", "level"}:
        raise G2Error("RESULT_VIEW_INCOMPLETE", "primary.interval")
    _require_finite(primary["estimate"], where="primary.estimate")
    for key in ("lower", "upper", "level"):
        _require_finite(primary["interval"][key], where=f"primary.{key}")
    scope = doc["scope"]
    if not isinstance(scope, Mapping) or set(scope) != {
            "target_population", "aggregation", "weighting", "denominator",
            "dropped", "coverage"}:
        raise G2Error("ENVELOPE_INVALID", "scope 字段集")
    if scope["target_population"] not in TARGET_POPULATIONS:
        raise G2Error("SCOPE_CONTRACT_UNDEFINED", "scope.target_population")
    if scope["aggregation"] not in AGGREGATIONS:
        raise G2Error("SCOPE_CONTRACT_UNDEFINED", "scope.aggregation")
    if scope["weighting"] not in WEIGHTINGS:
        raise G2Error("SCOPE_CONTRACT_UNDEFINED", "scope.weighting")
    if scope["coverage"] not in COVERAGES:
        raise G2Error("SCOPE_CONTRACT_UNDEFINED", "scope.coverage")
    if (not isinstance(scope["denominator"], int)
            or isinstance(scope["denominator"], bool)
            or scope["denominator"] < 0):
        raise G2Error("ENVELOPE_INVALID", "scope.denominator")
    arts = [_artifact_record(a) for a in doc["artifacts"]]
    art_ids = {a["artifact_id"] for a in arts}
    for item in doc["evidence"]:
        _evidence_record(item, art_ids)
    if not art_ids:
        raise G2Error("RESULT_VIEW_INCOMPLETE", "artifacts 为空")
    if not doc["evidence"]:
        raise G2Error("EVIDENCE_MISSING", "evidence 为空")
    if doc["summary_sha256"] != field_sha(doc.get("summary") or {}):
        raise G2Error("ENVELOPE_INVALID", "summary_sha256 不符")
    want = content_sha(doc, exclude=("envelope_sha256",))
    if doc["envelope_sha256"] != want:
        raise G2Error("ENVELOPE_INVALID", "envelope_sha256 不符")
    if scope_contract is not None:
        contract = scope_contract.validate()
        if doc["scope_contract_sha256"] != contract.sha256:
            raise G2Error("SCOPE_CONTRACT_MISMATCH", "contract sha 不符")
        for key in ("target_population", "aggregation", "weighting", "coverage"):
            if scope[key] != getattr(contract, key):
                raise G2Error("SCOPE_CONTRACT_MISMATCH", f"scope.{key}")
    measure = measure_envelope(doc)
    if not measure["ok"]:
        raise G2Error("CAPACITY_NOT_ESTABLISHED",
                      f"{measure['n_bytes']}B > {ENVELOPE_MAX_BYTES}B")
    return {"ok": True, "n_bytes": measure["n_bytes"],
            "n_contrasts": len(records), "envelope_sha256": doc["envelope_sha256"]}


def envelope_from_output(*, study_id: str, phase: str, plan_sha256: str,
                         output: Mapping[str, Any], output_bytes: bytes,
                         receipt_bytes: bytes,
                         scope_contract: ScopeContractV1,
                         required_contrast_ids: Sequence[str],
                         primary_contrast_id: str,
                         extra_artifacts: "Mapping[str, bytes] | None" = None
                         ) -> "dict[str, Any]":
    """从**候选自己的输出**确定性投影 envelope（不读 raw、不补宿主数值）。"""
    raw_contrasts = output.get("contrasts")
    if isinstance(raw_contrasts, Mapping):
        by_id = {str(k): dict(v) for k, v in raw_contrasts.items()
                 if isinstance(v, Mapping)}
    elif isinstance(raw_contrasts, list):
        by_id = {str(v.get("contrast_id")): dict(v) for v in raw_contrasts
                 if isinstance(v, Mapping)}
    else:
        raise G2Error("RESULT_VIEW_INCOMPLETE", "output.contrasts 形状不符")
    records: "list[dict[str, Any]]" = []
    evidence: "list[dict[str, Any]]" = []
    denominator = 0
    for cid in required_contrast_ids:
        src = by_id.get(str(cid))
        if src is None:
            raise G2Error("RESULT_VIEW_INCOMPLETE", f"输出缺 contrast {cid}")
        record = {
            "contrast_id": str(cid),
            "metric_id": src.get("metric_id"),
            "proposition": src.get("proposition"),
            "delta": scope_contract.delta,
            "estimate": src.get("estimate"),
            "lower": src.get("lower"),
            "upper": src.get("upper"),
            "level": scope_contract.interval_level,
            "science_status": _science_status_of(src.get("status"),
                                                 scope_contract),
            "n_blocks": src.get("n_blocks"),
            "dropped": src.get("dropped") or {"count": 0, "ids": [],
                                              "ids_sha256": ""},
        }
        records.append(record)
        denominator += int(src.get("n_blocks") or 0)
        for key in ("estimate", "lower", "upper"):
            evidence.append({
                "artifact_id": "analysis/output.json",
                "json_pointer": f"/contrasts/{_index_of(raw_contrasts, cid)}/{key}"
                if isinstance(raw_contrasts, list)
                else f"/contrasts/{cid}/{key}",
                "field_sha256": field_sha(src.get(key)),
            })
    artifacts = [{"artifact_id": "analysis/output.json",
                  "relative_path": "analysis/output.json",
                  "sha256": sha_bytes(output_bytes), "bytes": len(output_bytes),
                  "access_mode": "read_result_artifact"},
                 {"artifact_id": "analysis/receipt.json",
                  "relative_path": "analysis/receipt.json",
                  "sha256": sha_bytes(receipt_bytes), "bytes": len(receipt_bytes),
                  "access_mode": "read_result_artifact"}]
    for art_id, data in sorted((extra_artifacts or {}).items()):
        artifacts.append({"artifact_id": art_id, "relative_path": art_id,
                          "sha256": sha_bytes(data), "bytes": len(data),
                          "access_mode": "read_result_artifact"})
    diagnostics = output.get("diagnostics")
    diag_summary = ({"status": "OBSERVED",
                     "contrast_count": len(records),
                     "coverage": (diagnostics or {}).get("coverage")}
                    if isinstance(diagnostics, Mapping)
                    else {"status": "MISSING"})
    return build_envelope(
        study_id=study_id, phase=phase, plan_sha256=plan_sha256,
        analysis_receipt_sha256=sha_bytes(receipt_bytes),
        analysis_output_sha256=sha_bytes(output_bytes),
        scope_contract=scope_contract,
        required_contrast_ids=list(required_contrast_ids),
        contrasts=records, primary_contrast_id=primary_contrast_id,
        denominator=denominator,
        dropped={"count": 0, "ids": [], "ids_sha256": ""},
        diagnostics=diag_summary, artifacts=artifacts, evidence=evidence,
        summary={"n_required": len(records)})


def derive_required_ids(task: Any) -> "list[str]":
    """由**冻结 TaskSpec** 派生必需对比全集（主对比 + 阴性对照）。

    RSI-G2.1（Plan §3.0 第 2 行 / AC G21-22）：必需集合必须在分析验证之前由
    任务主张派生，**不得**从候选 output 现有项反推——否则「候选只算主对比」
    就能把阴性对照静默排除。上游分析过滤器也不得删减该集合。
    """
    primary = getattr(task, "primary", None)
    pid = str(getattr(primary, "id", "") or "")
    if not pid:
        raise G2Error("CONTRACT_INVALID", "task 缺 primary.id")
    ids: "list[str]" = [pid]
    nc = (getattr(task, "negative_control_spec", None) or {}).get("contrast") or {}
    nc_id = str(nc.get("contrast_id") or nc.get("id") or "")
    if nc_id and nc_id not in ids:
        ids.append(nc_id)
    return ids


def resolve_pointer(doc: Any, pointer: str) -> Any:
    """按 RFC6901 解析 JSON Pointer（空串表示整份文档）。"""
    if not isinstance(pointer, str) or (pointer and not pointer.startswith("/")):
        raise G2Error("POINTER_INVALID", repr(pointer))
    node = doc
    if pointer == "":
        return node
    for token in pointer.split("/")[1:]:
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(node, Mapping):
            if token not in node:
                raise G2Error("POINTER_INVALID", f"{pointer} 缺键 {token!r}")
            node = node[token]
        elif isinstance(node, list):
            try:
                index = int(token)
            except ValueError:
                raise G2Error("POINTER_INVALID", f"{pointer} 非列表下标") from None
            if not 0 <= index < len(node):
                raise G2Error("POINTER_INVALID", f"{pointer} 越界")
            node = node[index]
        else:
            raise G2Error("POINTER_INVALID", f"{pointer} 走到标量 {token!r}")
    return node


def _index_of(raw_contrasts: Any, cid: str) -> int:
    if isinstance(raw_contrasts, list):
        for i, item in enumerate(raw_contrasts):
            if isinstance(item, Mapping) and str(item.get("contrast_id")) == str(cid):
                return i
    return 0


def _science_status_of(label: Any, contract: ScopeContractV1) -> str:
    """fixture 预登记状态 → science_status；不因 MATCH 自动晋升 SUPPORTED。"""
    mapping = {"SUPPORTED": "SUPPORTED", "REFUTED": "REFUTED",
               "INCONCLUSIVE": "INCONCLUSIVE"}
    return mapping.get(str(label), "NOT_MEASURED")


# ---------------------------------------------------------------------------
# 只读分页读取入口（Plan §2.4：read_result_artifact）
# ---------------------------------------------------------------------------

class ArtifactRegistry:
    """当前 envelope 的 artifact 白名单（artifact_id → 登记条目）。"""

    def __init__(self, artifacts: Sequence[Mapping[str, Any]]) -> None:
        self._by_id: "dict[str, dict[str, Any]]" = {}
        for raw in artifacts:
            entry = _artifact_record(raw)
            self._by_id[entry["artifact_id"]] = entry

    def get(self, artifact_id: str) -> "dict[str, Any]":
        entry = self._by_id.get(str(artifact_id))
        if entry is None:
            raise G2Error("ARTIFACT_MISSING", str(artifact_id))
        return dict(entry)

    def ids(self) -> "list[str]":
        return sorted(self._by_id)

    @classmethod
    def from_envelope(cls, envelope: Mapping[str, Any]) -> "ArtifactRegistry":
        return cls(list(envelope.get("artifacts") or []))


def read_page(*, registry: ArtifactRegistry,
              read_bytes: Callable[[str], bytes], artifact_id: str,
              output_sha256: str, cursor: int = 0,
              page_bytes: int = PAGE_MAX_BYTES) -> "dict[str, Any]":
    """读取一个已登记 artifact 的字节页；任何越权/替换/越界都 fail-closed。"""
    if (not isinstance(cursor, int) or isinstance(cursor, bool) or cursor < 0):
        raise G2Error("CURSOR_INVALID", repr(cursor))
    if (not isinstance(page_bytes, int) or isinstance(page_bytes, bool)
            or not 1 <= page_bytes <= PAGE_MAX_BYTES):
        raise G2Error("PAGE_TOO_LARGE", repr(page_bytes))
    entry = registry.get(str(artifact_id))
    if str(output_sha256) != entry["sha256"]:
        raise G2Error("ARTIFACT_SHA_MISMATCH", str(artifact_id))
    rel = _safe_relpath(entry["relative_path"])
    if not rel.startswith(_ALLOWED_ARTIFACT_PREFIX):
        raise G2Error("ARTIFACT_FORBIDDEN", rel)
    try:
        data = read_bytes(rel)
    except OSError as exc:
        raise G2Error("ARTIFACT_MISSING", f"{rel}: {exc}") from None
    if sha_bytes(data) != entry["sha256"]:
        raise G2Error("ARTIFACT_SHA_MISMATCH", rel)
    if len(data) != entry["bytes"]:
        raise G2Error("ARTIFACT_SHA_MISMATCH", f"{rel} 字节数不符")
    if len(data) > ARTIFACT_MAX_BYTES:
        raise G2Error("PAGE_TOO_LARGE", f"{rel} > {ARTIFACT_MAX_BYTES}B")
    if cursor > len(data):
        raise G2Error("CURSOR_INVALID", f"{cursor} > {len(data)}")
    chunk = data[cursor:cursor + page_bytes]
    try:
        text = data.decode("utf-8")
        utf8 = True
    except UnicodeDecodeError:
        text, utf8 = "", False
    if utf8:
        # 起始 cursor 必须落在 UTF-8 边界；页尾回退到边界（续读不裂字符）。
        try:
            data[:cursor].decode("utf-8")
        except UnicodeDecodeError:
            raise G2Error("CURSOR_INVALID",
                          f"{cursor} 不在 UTF-8 字符边界") from None
        cut = len(chunk)
        while cut > 0 and cursor + cut < len(data):
            try:
                data[:cursor + cut].decode("utf-8")
                break
            except UnicodeDecodeError:
                cut -= 1
        chunk = data[cursor:cursor + cut]
    next_cursor = cursor + len(chunk) if cursor + len(chunk) < len(data) else None
    import base64
    payload = chunk.decode("utf-8") if utf8 else base64.b64encode(chunk).decode()
    out = {
        "artifact_id": str(artifact_id), "artifact_sha256": entry["sha256"],
        "offset": cursor, "n_bytes": len(chunk), "total_bytes": len(data),
        "page_sha256": sha_bytes(chunk),
        "encoding": "utf-8" if utf8 else "base64",
        "data": payload, "next_cursor": next_cursor,
    }
    if cursor == 0:
        out["read_rules"] = ("只读候选自产 analysis 产物；按 offset 续读，"
                             "next_cursor=null 表示读完；禁止用摘要代替字节")
    return out


# ---------------------------------------------------------------------------
# ScienceAssessmentV1（与 tuple/binding 分离；旧 closeout 不移植）
# ---------------------------------------------------------------------------

def science_assessment(*, binding_valid: bool, numeric_valid: bool,
                       tuple_match: bool, scope_valid: bool,
                       evidence_coverage_valid: bool,
                       semantic_label: str, target_population: str,
                       estimand_id: str, metric_id: str,
                       proposition_id: str, delta: "float | None",
                       contract_defined: bool = True) -> "dict[str, Any]":
    if not contract_defined:
        status = "NOT_MEASURED"
    else:
        status = {"SUPPORTED": "SUPPORTED", "REFUTED": "REFUTED",
                  "INCONCLUSIVE": "INCONCLUSIVE"}.get(semantic_label,
                                                      "NOT_MEASURED")
    return {
        "schema": SCIENCE_SCHEMA, "schema_version": 1,
        "binding_valid": bool(binding_valid),
        "numeric_valid": bool(numeric_valid),
        "tuple_match": bool(tuple_match),
        "scope_valid": bool(scope_valid),
        "evidence_coverage_valid": bool(evidence_coverage_valid),
        "science_status": status,
        "semantic_label": str(semantic_label),
        "target_population": str(target_population),
        "estimand_id": str(estimand_id),
        "metric_id": str(metric_id),
        "proposition_id": str(proposition_id),
        "delta": None if delta is None else float(delta),
    }


# ---------------------------------------------------------------------------
# 合同快照（Plan §2.5）
# ---------------------------------------------------------------------------

def implementation_closure(paths: Sequence["str | Path"]) -> "dict[str, Any]":
    """实现闭包 hash：相对路径 + 逐文件 sha 排序清单（不含输出/签名自身）。"""
    repo = Path(__file__).resolve().parents[2]

    def rel(path: Path) -> str:
        resolved = path.resolve()
        try:
            return resolved.relative_to(repo).as_posix()
        except ValueError:
            return resolved.name

    items: "list[dict[str, str]]" = []
    for raw in paths:
        path = Path(raw)
        items.append({"path": rel(path),
                      "sha256": sha_bytes(path.read_bytes())})
    items.sort(key=lambda item: item["path"])
    return {"files": items, "closure_sha256": field_sha(items)}


def build_contract_snapshot(*, policy_id: str, scope_contract: ScopeContractV1,
                            schema_files: Mapping[str, "str | Path"],
                            required_contrast_ids: Sequence[str],
                            source_commit: str,
                            implementation_files: Sequence["str | Path"],
                            tokenizer_id: str = "",
                            tokenizer_sha256: str = "",
                            capture_version: str = CAPTURE_VERSION
                            ) -> "dict[str, Any]":
    contract = scope_contract.validate()
    schema_shas = {label: sha_bytes(Path(path).read_bytes())
                   for label, path in sorted(schema_files.items())}
    doc: "dict[str, Any]" = {
        "schema": CONTRACT_SNAPSHOT_SCHEMA, "schema_version": 1,
        "policy_id": policy_id,
        "projection_version": PROJECTION_VERSION,
        "capture_version": capture_version,
        "scope_contract_sha256": contract.sha256,
        "schema_shas": schema_shas,
        "required_contrast_ids": [str(c) for c in required_contrast_ids],
        "limits": {"envelope_max_bytes": ENVELOPE_MAX_BYTES,
                   "page_max_bytes": PAGE_MAX_BYTES,
                   "artifact_max_bytes": ARTIFACT_MAX_BYTES,
                   "summary_max_bytes": SUMMARY_MAX_BYTES},
        "artifact_access": {"allowed_prefix": _ALLOWED_ARTIFACT_PREFIX,
                            "forbidden_prefixes": list(_FORBIDDEN_PREFIXES),
                            "action": "read_result_artifact"},
        "source_commit": str(source_commit),
        "implementation_closure": implementation_closure(implementation_files),
        "tokenizer": {"id": tokenizer_id, "sha256": tokenizer_sha256},
    }
    doc["contract_sha256"] = content_sha(doc, exclude=("contract_sha256",))
    return doc


def verify_contract_snapshot(doc: Any) -> "dict[str, Any]":
    if not isinstance(doc, Mapping):
        raise G2Error("CONTRACT_INVALID", "snapshot 必须是对象")
    if doc.get("schema") != CONTRACT_SNAPSHOT_SCHEMA:
        raise G2Error("SCHEMA_MISMATCH", str(doc.get("schema")))
    want = content_sha(doc, exclude=("contract_sha256",))
    if doc.get("contract_sha256") != want:
        raise G2Error("CONTRACT_INVALID", "contract_sha256 不符")
    limits = doc.get("limits") or {}
    if limits.get("envelope_max_bytes") != ENVELOPE_MAX_BYTES:
        raise G2Error("CONTRACT_INVALID", "envelope_max_bytes 漂移")
    if limits.get("page_max_bytes") != PAGE_MAX_BYTES:
        raise G2Error("CONTRACT_INVALID", "page_max_bytes 漂移")
    if not doc.get("required_contrast_ids"):
        raise G2Error("CONTRACT_INVALID", "required_contrast_ids 为空")
    return {"ok": True, "contract_sha256": doc["contract_sha256"]}


# ---------------------------------------------------------------------------
# 旁路捕获（复用 G1 事件/存储；只加 extra 元数据，不建第二套 trace）
# ---------------------------------------------------------------------------

class G2CaptureRecorder:
    """把 G2 边界的**实际对象**写成 observed 事件；缺网关 body 只记 MISSING。"""

    def __init__(self, root: Path, *, diagnostic_id: str,
                 policy_id: str = POLICY_G2_ATOMIC,
                 scope_contract_sha: str = "", study: str = "A",
                 source_commit: str = "", sub: str = "g2") -> None:
        self.root = Path(root)
        self.study = study
        self.policy_id = policy_id
        self.scope_contract_sha = scope_contract_sha
        self.writer = GT.TraceWriter(self.root / "trace" / sub,
                                     diagnostic_id=diagnostic_id,
                                     source_commit=source_commit)

    def _extra(self, **fields: Any) -> "dict[str, Any]":
        base = {"policy_id": self.policy_id,
                "scope_contract_sha": self.scope_contract_sha,
                "capture_version": CAPTURE_VERSION}
        base.update(fields)
        return base

    def _add(self, kind: str, data: bytes, *, phase: str,
             visibility: str = "observed", **fields: Any) -> str:
        if visibility == "observed":
            digest = sha_bytes(data)
            rel = GT.materialize(self.root, "g2", data)
            self.writer.add(GT.TraceEvent(
                diagnostic_id=self.writer.diagnostic_id, seq=0,
                event_kind=kind, visibility=visibility, study_id=self.study,
                phase=phase, artifact_ref=rel, bytes_sha256=digest,
                n_bytes=len(data), source_commit=self.writer.source_commit,
                request_sha256=fields.pop("request_sha256", None),
                extra=self._extra(**fields)))
            return rel
        self.writer.add(GT.TraceEvent(
            diagnostic_id=self.writer.diagnostic_id, seq=0,
            event_kind=kind, visibility="missing", study_id=self.study,
            phase=phase, artifact_ref=None, bytes_sha256=None, n_bytes=None,
            source_commit=self.writer.source_commit,
            request_sha256=fields.pop("request_sha256", None),
            missing_reason=fields.pop("missing_reason", "NOT_CAPTURED"),
            extra=self._extra(**fields)))
        return ""

    def record_projection(self, obj: Mapping[str, Any], *,
                          phase: str = "confirm") -> str:
        return self._add("clipped_view", canonical(dict(obj)), phase=phase,
                         view="projection")

    def record_envelope(self, envelope: Mapping[str, Any], *,
                        phase: str = "confirm") -> str:
        return self._add("clipped_view", canonical(dict(envelope)),
                         phase=phase, view="envelope",
                         envelope_sha256=str(envelope.get("envelope_sha256")))

    def record_request(self, request: "T.Request", *,
                       phase: str = "confirm") -> str:
        return self._add("request_serialized",
                         request.serialized.encode("utf-8"), phase=phase,
                         request_sha256=request.sha256)

    def record_reply(self, reply: Any, request: "T.Request", *,
                     phase: str = "confirm") -> str:
        return self._add("response_raw", str(reply.text).encode("utf-8"),
                         phase=phase, request_sha256=request.sha256,
                         response_id=getattr(reply, "response_id", ""))

    def record_dispatch(self, action: Any, result: Any, *,
                        phase: str = "confirm") -> str:
        self._add("action_parsed", canonical(action.to_doc()), phase=phase)
        return self._add("dispatch_return", canonical(dict(result)),
                         phase=phase)

    def record_clipped(self, turn: "T.Turn", *,
                       phase: str = "confirm") -> str:
        """记录**实际**下一请求会看到的视图（按 g2_atomic_v1 投影）。"""
        view = turn.clipped(policy=POLICY_G2_ATOMIC)
        return self._add("clipped_view", canonical(dict(view.result)),
                         phase=phase, view="clipped")

    def record_gateway_missing(self, *, phase: str = "confirm",
                               reason: str = "NO_HTTP_BODY_CAPTURED") -> None:
        self._add("gateway_capture", b"", phase=phase, visibility="missing",
                  missing_reason=reason)

    def write(self) -> "dict[str, Any]":
        return self.writer.write()


def first_divergence(envelope: "Mapping[str, Any] | None",
                     final_request_text: str) -> str:
    """最小交付链的第一个分歧点（与 G1 同名枚举保持兼容）。"""
    if envelope is None:
        return "CANDIDATE_OUTPUT"
    try:
        text = canonical(dict(envelope)).decode("utf-8")
    except (TypeError, ValueError):
        return "CANDIDATE_OUTPUT"
    if text not in final_request_text:
        return "RETURN_VIEW"
    return "NONE"


# ---------------------------------------------------------------------------
# 离线脚本化 sender（零网络；只有明确标 OFFLINE 的对象可用）
# ---------------------------------------------------------------------------

class RecordingScriptedSender:
    """记录请求并按脚本回放的 sender；**只用于离线工程门**。"""

    OFFLINE = True

    def __init__(self, replies: "Sequence[str] | Callable[[T.Request], str]",
                 *, response_prefix: str = "g2script") -> None:
        self._replies = replies
        self._index = 0
        self.response_prefix = response_prefix
        self.requests: "list[T.Request]" = []

    def send_once(self, request: "T.Request", reservation: Any) -> Any:
        from orchestrator.rsi_r1 import loop as L
        self.requests.append(request)
        if callable(self._replies):
            text = self._replies(request)
        else:
            if self._index >= len(self._replies):
                raise G2Error("SCRIPT_EXHAUSTED", "scripted 回复用尽")
            text = self._replies[self._index]
            self._index += 1
        return L.Reply(text=str(text),
                       response_id=f"{self.response_prefix}-{len(self.requests)}",
                       is_actionable=True)


def assert_offline_sender(sender: Any) -> None:
    """只有显式 OFFLINE 的 sender 可进 G2；真实 sender 构造即阻断。"""
    if not getattr(sender, "OFFLINE", False):
        raise G2Error("PAID_GATE", f"sender {type(sender).__name__} 非离线")


class FailIfCalled:
    """把真实网关/物理入口替换成「调用即失败」的守卫。"""

    def __init__(self, name: str = "gateway") -> None:
        self.name = name

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        raise G2Error("PAID_GATE", f"{self.name} 在 G2 零 POST 模式被调用")

    def send_once(self, *args: Any, **kwargs: Any) -> Any:
        raise G2Error("PAID_GATE", f"{self.name} 在 G2 零 POST 模式被调用")
