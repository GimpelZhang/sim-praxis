"""RSI-R1.2 T4：分析执行凭据（AnalysisReceipt）与输出结构校验。

本模块只做**纯验证**：不跑容器、不重算统计、不 import 候选代码。它把
「候选分析确实在精确公开观测视图上、由可信 sandbox 执行、退出 0、产物完整」
这件事固化成可机判的凭据，并给出字段级比较工具。

三条不可让步的口径（Plan §5.1）：

1. **身份由宿主验证/派生**：``source_sha256``（登记来源）、``input_view_sha256``
   与逐文件 ``inputs`` 全部由宿主从实际字节重算；候选自述的 relpath/phase/
   run 集合一律不作为证据。
2. **凭据与可信执行记录交叉验证**：``execution_id``/``exit_code``/``timed_out``/
   ``output_sha256`` 必须与 ``SandboxEvidence`` 一致；候选可写的 JSON 不算数。
3. **只返类别/字段**：任何数值不一致只报字段名与错误码，**不回显**维护侧
   正确值、不给出带方向的差值。
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from orchestrator.rsi_r1 import contracts as C
from orchestrator.rsi_r1 import rubric as R

SCHEMA = "simpraxis.rsi_r1.analysis_receipt.v1"
SCHEMA_VERSION = 1
OUTPUT_SCHEMA = "simpraxis.rsi_r1.analysis_output.v1"

PHASES = ("explore", "confirm", "replay")
PRODUCER_CANDIDATE = "candidate"
PRODUCER_MAINTAINER = "maintainer"
PRODUCERS = (PRODUCER_CANDIDATE, PRODUCER_MAINTAINER)

#: 输出里每条对比的必填字段（与冻结统计方法一一对应）。
CONTRAST_FIELDS = ("contrast_id", "metric_id", "proposition", "estimate",
                   "lower", "upper", "status", "n_blocks")
STATUSES = ("SUPPORTED", "REFUTED", "INCONCLUSIVE")

RECEIPT_REQUIRED = (
    "schema", "schema_version", "profile_id", "study_id", "phase",
    "plan_sha256", "confirm_manifest_sha256", "source_sha256",
    "registered_reply_id", "registered_action_id", "producer",
    "input_view_sha256", "inputs", "execution_id", "sandbox_status",
    "exit_code", "timed_out", "output_sha256", "output_schema",
    "output_run_ids", "validity", "reason_codes",
)


class ReceiptError(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class ReceiptExpectation:
    """宿主在验证时的期望值（全部来自可信侧事实，不读候选自述）。"""

    study_id: str
    phase: str
    plan_sha256: str
    source_sha256: str
    source_file_sha256: str
    producer: str
    registered_reply_id: "str | None"
    registered_action_id: "str | None"
    confirm_manifest_sha256: "str | None"
    allowed_run_ids: "tuple[str, ...]"
    expected_inputs: "tuple[dict[str, Any], ...]"
    output_bytes: bytes
    execution_id: str
    exit_code: int = 0
    timed_out: bool = False
    profile_id: str = "rsi-r1.2"
    extra_reason_codes: "tuple[str, ...]" = field(default=())


def _safe_component(value: Any) -> bool:
    text = str(value or "")
    return bool(text) and text not in (".", "..") and "/" not in text \
        and "\\" not in text and "\x00" not in text


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(bytes(data)).hexdigest()


def normalise_inputs(inputs: Sequence[Mapping[str, Any]]) -> "list[dict[str, Any]]":
    """把宿主构造的视图条目规范化并**按 run 身份去重**（不允许重复）。"""
    seen: "set[tuple[str, str]]" = set()
    out: "list[dict[str, Any]]" = []
    for item in inputs:
        run_id = str(item.get("run_id") or "")
        rel = str(item.get("relative_path") or "")
        sha = str(item.get("sha256") or "")
        key = (run_id, rel)
        if key in seen:
            raise ReceiptError("INPUTS_DUPLICATE", f"{run_id}:{rel}")
        seen.add(key)
        out.append({"run_id": run_id, "relative_path": rel, "sha256": sha})
    return sorted(out, key=lambda d: (d["run_id"], d["relative_path"]))


def input_view_sha256(inputs: Sequence[Mapping[str, Any]]) -> str:
    return C.canonical_sha256(normalise_inputs(inputs))


def build_receipt(*, profile_id: str, study_id: str, phase: str,
                  plan_sha256: str, confirm_manifest_sha256: "str | None",
                  source_sha256: str, registered_reply_id: "str | None",
                  registered_action_id: "str | None", producer: str,
                  inputs: Sequence[Mapping[str, Any]], execution_id: str,
                  sandbox_status: str, exit_code: int, timed_out: bool,
                  output_bytes: bytes, output_doc: "Mapping[str, Any] | None",
                  validity: bool, reason_codes: Sequence[str] = ()
                  ) -> "dict[str, Any]":
    """宿主侧构造凭据：所有 sha 都从**实际字节**推导。"""
    if phase not in PHASES:
        raise ReceiptError("PHASE_INVALID", repr(phase))
    if producer not in PRODUCERS:
        raise ReceiptError("PRODUCER_INVALID", repr(producer))
    if not _safe_component(study_id):
        raise ReceiptError("STUDY_INVALID", repr(study_id))
    norm = normalise_inputs(inputs)
    run_ids: "list[str]" = []
    if output_doc is not None:
        raw = output_doc.get("run_ids")
        if isinstance(raw, list):
            run_ids = [str(r) for r in raw]
    return {
        "schema": SCHEMA, "schema_version": SCHEMA_VERSION,
        "profile_id": str(profile_id), "study_id": str(study_id),
        "phase": str(phase), "plan_sha256": str(plan_sha256),
        "confirm_manifest_sha256": (None if confirm_manifest_sha256 is None
                                    else str(confirm_manifest_sha256)),
        "source_sha256": str(source_sha256),
        "registered_reply_id": (None if registered_reply_id is None
                                else str(registered_reply_id)),
        "registered_action_id": (None if registered_action_id is None
                                 else str(registered_action_id)),
        "producer": str(producer),
        "input_view_sha256": C.canonical_sha256(norm),
        "inputs": norm,
        "execution_id": str(execution_id),
        "sandbox_status": str(sandbox_status),
        "exit_code": int(exit_code), "timed_out": bool(timed_out),
        "output_sha256": sha256_bytes(output_bytes),
        "output_schema": ("" if output_doc is None
                          else str(output_doc.get("schema") or "")),
        "output_run_ids": sorted(set(run_ids)),
        "validity": bool(validity),
        "reason_codes": [str(c) for c in reason_codes],
    }


def receipt_sha256(receipt: Mapping[str, Any]) -> str:
    return C.canonical_sha256(dict(receipt))


def validate_structure(receipt: Any) -> "list[str]":
    """结构面：必需键、类型、取值域。返回错误码列表（空=结构合法）。"""
    if not isinstance(receipt, Mapping):
        return ["RECEIPT_NOT_OBJECT"]
    problems: "list[str]" = []
    missing = [k for k in RECEIPT_REQUIRED if k not in receipt]
    if missing:
        problems.append(f"RECEIPT_MISSING_KEYS:{','.join(missing)}")
        return problems
    if receipt.get("schema") != SCHEMA:
        problems.append("RECEIPT_SCHEMA_MISMATCH")
    if receipt.get("phase") not in PHASES:
        problems.append("RECEIPT_PHASE_INVALID")
    if receipt.get("producer") not in PRODUCERS:
        problems.append("RECEIPT_PRODUCER_INVALID")
    if not isinstance(receipt.get("inputs"), list):
        problems.append("RECEIPT_INPUTS_INVALID")
    for key in ("plan_sha256", "source_sha256", "input_view_sha256",
                "output_sha256"):
        if not isinstance(receipt.get(key), str) or len(receipt[key]) != 64:
            problems.append(f"RECEIPT_SHA_INVALID:{key}")
    return problems


def validate_receipt(receipt: "Mapping[str, Any]",
                     expect: ReceiptExpectation) -> "tuple[bool, list[str]]":
    """全量校验：身份/来源/视图/执行/产物。返回 ``(ok, reason_codes)``。

    不采信凭据自述的 ``validity``；每个字段都与宿主期望值或实际字节对照。
    """
    reasons = validate_structure(receipt)
    if reasons:
        return False, reasons

    def bad(code: str) -> None:
        reasons.append(code)

    if receipt.get("profile_id") != expect.profile_id:
        bad("PROFILE_MISMATCH")
    if receipt.get("study_id") != expect.study_id:
        bad("STUDY_MISMATCH")
    if receipt.get("phase") != expect.phase:
        bad("PHASE_MISMATCH")
    if receipt.get("plan_sha256") != expect.plan_sha256:
        bad("PLAN_SHA_MISMATCH")
    if receipt.get("confirm_manifest_sha256") != expect.confirm_manifest_sha256:
        bad("MANIFEST_SHA_MISMATCH")
    if receipt.get("source_sha256") != expect.source_sha256:
        bad("SOURCE_SHA_MISMATCH")
    if expect.source_file_sha256 != expect.source_sha256:
        bad("SOURCE_FILE_DRIFT")
    if receipt.get("producer") != expect.producer:
        bad("PRODUCER_MISMATCH")
    if receipt.get("registered_reply_id") != expect.registered_reply_id:
        bad("REPLY_BINDING_MISMATCH")
    if receipt.get("registered_action_id") != expect.registered_action_id:
        bad("ACTION_BINDING_MISMATCH")
    if receipt.get("execution_id") != expect.execution_id:
        bad("EXECUTION_MISMATCH")
    if int(receipt.get("exit_code", -1)) != int(expect.exit_code):
        bad("EXIT_CODE_MISMATCH")
    if bool(receipt.get("timed_out")) != bool(expect.timed_out):
        bad("TIMEOUT_MISMATCH")

    try:
        norm = normalise_inputs(receipt.get("inputs") or [])
    except ReceiptError as exc:
        bad(exc.code)
        norm = []
    got_view = C.canonical_sha256(norm) if norm else ""
    if got_view != receipt.get("input_view_sha256"):
        bad("INPUT_VIEW_SELF_INCONSISTENT")
    expected = normalise_inputs(list(expect.expected_inputs))
    if C.canonical_sha256(norm) != C.canonical_sha256(expected):
        got_runs = {d["run_id"] for d in norm}
        want_runs = {d["run_id"] for d in expected}
        if got_runs != want_runs:
            if want_runs - got_runs:
                bad("RUN_IDS_INCOMPLETE")
            if got_runs - want_runs:
                bad("RUN_IDS_EXTRA")
        if len(got_runs) != len({d["run_id"] for d in expected}):
            bad("RUN_IDS_DUPLICATE")
        if got_runs == want_runs:
            bad("INPUT_FILES_MISMATCH")
    allowed = set(str(r) for r in expect.allowed_run_ids)
    if not {d["run_id"] for d in norm}.issubset(allowed):
        bad("RUN_IDS_UNAUTHORIZED")

    if sha256_bytes(expect.output_bytes) != receipt.get("output_sha256"):
        bad("OUTPUT_SHA_MISMATCH")
    if receipt.get("output_schema") != OUTPUT_SCHEMA:
        bad("OUTPUT_SCHEMA_MISMATCH")

    reasons.extend(str(c) for c in expect.extra_reason_codes)
    # 去重且保序
    seen: "set[str]" = set()
    out: "list[str]" = []
    for code in reasons:
        if code not in seen:
            seen.add(code)
            out.append(code)
    return (not out), out


# ---------------------------------------------------------------------------
# 候选输出结构
# ---------------------------------------------------------------------------

def _finite(value: Any, *, where: str) -> float:
    if isinstance(value, bool):
        raise ReceiptError("OUTPUT_NONFINITE", where)
    try:
        out = float(value)
    except (TypeError, ValueError):
        raise ReceiptError("OUTPUT_FIELD_INVALID", where) from None
    if not math.isfinite(out):
        raise ReceiptError("OUTPUT_NONFINITE", where)
    return out


def parse_output(doc: Any, *, expected_run_ids: Sequence[str],
                 expected_contrasts: Sequence[str]) -> "dict[str, Any]":
    """校验候选输出结构与完整 run 身份；返回规范化副本。

    拒绝：缺文件/坏 JSON/NaN/缺字段/run 集合不符/未知对比/非法状态。
    """
    if not isinstance(doc, Mapping):
        raise ReceiptError("OUTPUT_NOT_OBJECT")
    if doc.get("schema") != OUTPUT_SCHEMA:
        raise ReceiptError("OUTPUT_SCHEMA_MISMATCH")
    raw_runs = doc.get("run_ids")
    if not isinstance(raw_runs, list):
        raise ReceiptError("OUTPUT_RUN_IDS_MISSING")
    runs = [str(r) for r in raw_runs]
    if len(runs) != len(set(runs)):
        raise ReceiptError("OUTPUT_RUN_IDS_DUPLICATE")
    want = sorted(str(r) for r in expected_run_ids)
    if sorted(runs) != want:
        if set(want) - set(runs):
            raise ReceiptError("OUTPUT_RUN_IDS_INCOMPLETE")
        raise ReceiptError("OUTPUT_RUN_IDS_EXTRA")
    raw_contrasts = doc.get("contrasts")
    if not isinstance(raw_contrasts, list):
        raise ReceiptError("OUTPUT_CONTRASTS_MISSING")
    by_id: "dict[str, dict[str, Any]]" = {}
    for i, item in enumerate(raw_contrasts):
        if not isinstance(item, Mapping):
            raise ReceiptError("OUTPUT_CONTRAST_INVALID", f"contrasts[{i}]")
        missing = [k for k in CONTRAST_FIELDS if k not in item]
        if missing:
            raise ReceiptError("OUTPUT_CONTRAST_FIELDS",
                               f"contrasts[{i}] 缺 {','.join(missing)}")
        cid = str(item["contrast_id"])
        if cid in by_id:
            raise ReceiptError("OUTPUT_CONTRAST_DUPLICATE", cid)
        if cid not in set(str(c) for c in expected_contrasts):
            raise ReceiptError("OUTPUT_CONTRAST_UNKNOWN", cid)
        status = str(item["status"])
        if status not in STATUSES:
            raise ReceiptError("OUTPUT_STATUS_INVALID", f"{cid}:{status}")
        by_id[cid] = {
            "contrast_id": cid,
            "metric_id": str(item["metric_id"]),
            "proposition": str(item["proposition"]),
            "estimate": _finite(item["estimate"], where=f"{cid}.estimate"),
            "lower": _finite(item["lower"], where=f"{cid}.lower"),
            "upper": _finite(item["upper"], where=f"{cid}.upper"),
            "status": status,
            "n_blocks": int(item["n_blocks"]),
        }
    missing_ids = sorted(set(str(c) for c in expected_contrasts) - set(by_id))
    if missing_ids:
        raise ReceiptError("OUTPUT_CONTRAST_INCOMPLETE", ",".join(missing_ids))
    return {"run_ids": sorted(runs), "contrasts": by_id}


# ---------------------------------------------------------------------------
# 字段级比较（只报字段名，不回显正确值）
# ---------------------------------------------------------------------------

def tolerance(reference: float) -> float:
    return float(R.CLAIM_ABS_TOL) + float(R.CLAIM_REL_TOL) * abs(float(reference))


def compare_values(got: Any, reference: Any, *, where: str,
                   resolve: "Mapping[str, Any] | None" = None
                   ) -> "list[str]":
    """比较三个数值字段；不一致只报 ``where``（字段名），不带数值。"""
    problems: "list[str]" = []
    for key in ("estimate", "lower", "upper"):
        try:
            value = _finite(got.get(key), where=f"{where}.{key}")
        except ReceiptError:
            problems.append(f"{where}.{key}")
            continue
        ref = float(reference[key])
        if abs(value - ref) > tolerance(ref):
            problems.append(f"{where}.{key}")
    return problems


def compare_contrast_identity(got: Mapping[str, Any],
                              reference: Mapping[str, Any],
                              *, where: str) -> "list[str]":
    problems: "list[str]" = []
    if str(got.get("metric_id")) != str(reference.get("metric_id")):
        problems.append(f"{where}.metric_id")
    if str(got.get("proposition")) != str(reference.get("proposition")):
        problems.append(f"{where}.proposition")
    if int(got.get("n_blocks", -1)) != int(reference.get("n_blocks", -2)):
        problems.append(f"{where}.n_blocks")
    return problems


def claim_consistency(claims: Sequence[Mapping[str, Any]],
                      output: Mapping[str, Any],
                      *, hypotheses: Mapping[str, Any]) -> "list[str]":
    """claim 与**候选自己的数值**及冻结假设核对（Plan §5.1 第三步）。

    ``hypotheses`` 形如 ``hypothesis_id → {contrast_id, metric_id, proposition,
    delta}``（来自冻结 plan）。任何不一致只报字段名。
    """
    problems: "list[str]" = []
    by_contrast = output.get("contrasts") or {}
    seen: "set[str]" = set()
    for i, claim in enumerate(claims):
        hid = str(claim.get("hypothesis_id"))
        hyp = hypotheses.get(hid)
        if hyp is None:
            problems.append(f"claims[{i}].hypothesis_id")
            continue
        cid = str(hyp["contrast_id"])
        seen.add(cid)
        cand = by_contrast.get(cid)
        if cand is None:
            problems.append(f"claims[{i}].contrast_id")
            continue
        if claim.get("metric_id") != hyp["metric_id"]:
            problems.append(f"claims[{i}].metric_id")
        if claim.get("proposition") != hyp["proposition"]:
            problems.append(f"claims[{i}].proposition")
        try:
            if abs(float(claim.get("estimate")) - float(cand["estimate"])) > \
                    tolerance(float(cand["estimate"])):
                problems.append(f"claims[{i}].estimate")
            if abs(float(claim.get("lower")) - float(cand["lower"])) > \
                    tolerance(float(cand["lower"])):
                problems.append(f"claims[{i}].lower")
            if abs(float(claim.get("upper")) - float(cand["upper"])) > \
                    tolerance(float(cand["upper"])):
                problems.append(f"claims[{i}].upper")
        except (TypeError, ValueError):
            problems.append(f"claims[{i}].nonfinite")
        if claim.get("n_blocks") is not None and \
                int(claim["n_blocks"]) != int(cand["n_blocks"]):
            problems.append(f"claims[{i}].n_blocks")
    for cid in sorted(set(str(h["contrast_id"]) for h in hypotheses.values())):
        if cid not in seen:
            problems.append(f"claims.missing:{cid}")
    return problems


# ---------------------------------------------------------------------------
# 公开候选输出 schema（§5.1「方法不是隐藏答案」）
# ---------------------------------------------------------------------------

OUTPUT_SCHEMA_DOC = "simpraxis.rsi_r1.analysis_output_schema.v1"


def public_output_schema() -> "dict[str, Any]":
    """公开候选分析输出的机器 schema 与字段含义（不含任何期望值）。"""
    return {
        "schema": OUTPUT_SCHEMA_DOC,
        "output_schema": OUTPUT_SCHEMA,
        "top_level": {
            "schema": f"必须逐字等于 {OUTPUT_SCHEMA!r}",
            "run_ids": "本 phase 精确 run 集合（字符串数组；无重复，不多不少）",
            "contrasts": "对比数组，每条覆盖一个已知 contrast_id，且全覆盖、无重复",
        },
        "run_ids_rule": {
            "identity": "集合相等：缺失 → OUTPUT_RUN_IDS_INCOMPLETE；多余 → OUTPUT_RUN_IDS_EXTRA",
            "duplicates": "同一 run 出现两次 → OUTPUT_RUN_IDS_DUPLICATE",
            "source": "期望集合来自宿主可信侧（观测视图/确认 manifest），候选不得自述",
        },
        "contrast_fields": {
            "contrast_id": "宿主分配的对比标识（未知 id 即拒绝）",
            "metric_id": "冻结指标目录中的 metric_id",
            "proposition": f"主张类型，∈ {list(R.PROPOSITIONS)}",
            "estimate": "配对 block 差均值（有限实数）",
            "lower": "区间下界（有限实数）",
            "upper": "区间上界（有限实数）",
            "status": f"分类结果，∈ {list(STATUSES)}",
            "n_blocks": "参与估计的 block 数（整数）",
        },
        "fields": list(CONTRAST_FIELDS),
        "statuses": list(STATUSES),
        "host_comparison": {
            "policy": "宿主用可信 raw 独立重算 effect/lower/upper/status，逐字段比较",
            "tolerance": "tol(ref) = abs_tol + rel_tol * abs(ref)（见公开统计方法）",
            "response": "只回字段名与机器码，从不回显宿主正确值",
        },
        "no_host_values": ("输出中不得出现宿主参考答案或期望值；任何 host 侧数字都不随"
                           "候选响应返回"),
        "rejects": ["OUTPUT_NOT_OBJECT", "OUTPUT_SCHEMA_MISMATCH",
                    "OUTPUT_RUN_IDS_MISSING", "OUTPUT_CONTRASTS_MISSING",
                    "OUTPUT_CONTRAST_FIELDS", "OUTPUT_CONTRAST_UNKNOWN",
                    "OUTPUT_STATUS_INVALID", "OUTPUT_CONTRAST_INCOMPLETE"],
    }


__all__ = [
    "CONTRAST_FIELDS", "OUTPUT_SCHEMA", "OUTPUT_SCHEMA_DOC", "PHASES",
    "PRODUCER_CANDIDATE", "PRODUCER_MAINTAINER", "RECEIPT_REQUIRED",
    "ReceiptError", "ReceiptExpectation", "SCHEMA", "SCHEMA_VERSION",
    "STATUSES", "build_receipt", "claim_consistency",
    "compare_contrast_identity", "compare_values", "input_view_sha256",
    "normalise_inputs", "parse_output", "public_output_schema",
    "receipt_sha256", "sha256_bytes", "tolerance", "validate_receipt",
    "validate_structure",
]
