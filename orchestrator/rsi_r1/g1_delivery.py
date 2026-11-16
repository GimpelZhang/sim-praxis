"""RSI-G1 受控交付：``DeliveryClaimV1`` 与 ``claim_compiler``（零 POST）。

Plan §2/§5 的最小实现：

* 两种候选提交形式 ``verbatim``（显式数值 + 来源引用）与 ``by_ref``（只给
  ``result_ref`` + 语义主张）**规范化为同一** ``DeliveryClaimV1``。
* 编译器只做**机械复制**：从候选自己已封存的 analysis 输出解引用，把
  estimate/CI 原样搬进交付件；不访问 raw/oracle/judge，不重估、不改方向、
  不补写局限（Plan G1-06）。
* 显式数值与引用值不一致 ⇒ ``CLAIM_NUMERIC_MISMATCH``（拒绝而非静默覆盖）；
  缺字段 ⇒ ``CLAIM_INCOMPLETE``；跨 study/旧版本/未封存/哈希不符 ⇒
  ``CLAIM_BINDING_MISMATCH``。
* 审计分列 ``parse_valid / binding_valid / numeric_fidelity / semantic_valid``；
  语义面恒为 ``None``（由独立 evaluator 判，编译器不越位）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Sequence

from orchestrator.rsi_r1 import g1_trace as GT

CLAIM_SCHEMA = "simpraxis.rsi_g1.delivery_claim.v1"
SUB_SCHEMA = "simpraxis.rsi_g1.candidate_submission.v1"
INTERFACES: "tuple[str, ...]" = ("verbatim", "by_ref")

#: 独立科学 evaluator 的固定容差（Plan §2.1：不放宽）。
ABS_TOL = 1e-8
REL_TOL = 1e-6

#: 候选可引用的 artifact_id 前缀（analysis store 只暴露候选自己的分析面）。
ALLOWED_ARTIFACT_PREFIXES: "tuple[str, ...]" = ("analysis/",)
FORBIDDEN_PREFIXES: "tuple[str, ...]" = ("private/", "judge/", "observations/",
                                         "confirm/", "raw/", "oracle/")


class DeliveryError(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"[{code}] {detail}" if detail else code)


def assert_store_isolation(store: Any) -> None:
    """S3：编译器只接受**候选**store；维护侧 store 一进入即拒。"""
    if getattr(store, "maintenance", False) is True:
        raise DeliveryError("STORE_ISOLATION",
                            "编译器不得接收维护侧 store（raw/judge/oracle）")
    if getattr(store, "store_kind", "candidate") != "candidate":
        raise DeliveryError("STORE_ISOLATION", str(getattr(store, "store_kind")))


def tolerance(reference: float) -> float:
    return ABS_TOL + REL_TOL * abs(float(reference))


def close(a: Any, b: Any) -> bool:
    try:
        fa, fb = float(a), float(b)
    except (TypeError, ValueError):
        return False
    return abs(fa - fb) <= tolerance(fb)


# ---------------------------------------------------------------------------
# 候选 analysis store（隔离面）
# ---------------------------------------------------------------------------

class CandidateArtifactStore(Protocol):
    def read(self, artifact_id: str) -> bytes: ...


@dataclass(frozen=True)
class FileArtifactStore:
    """只读、只暴露候选 analysis 面的 artifact store。

    ``root`` 是候选可见的 campaign 根；``referenced`` 记录每件读过的字节 sha，
    供审计（不缓存 judge/raw，因为根本不在允许前缀内）。
    """

    root: Any
    allowed_prefixes: "tuple[str, ...]" = ALLOWED_ARTIFACT_PREFIXES
    _read: "list[dict[str, Any]]" = field(default_factory=list, compare=False)

    def read(self, artifact_id: str) -> bytes:
        rel = str(artifact_id)
        if rel.startswith("/") or ".." in rel.split("/"):
            raise DeliveryError("ARTIFACT_PATH", rel)
        if any(rel.startswith(p) for p in FORBIDDEN_PREFIXES):
            raise DeliveryError("ARTIFACT_FORBIDDEN", rel)
        if not any(rel.startswith(p) for p in self.allowed_prefixes):
            raise DeliveryError("ARTIFACT_OUTSIDE_ALLOWLIST", rel)
        path = self.root / rel
        if path.is_symlink() or not path.is_file():
            raise DeliveryError("ARTIFACT_MISSING", rel)
        data = path.read_bytes()
        self._read.append({"artifact_id": rel, "sha256": GT.sha_bytes(data),
                           "n_bytes": len(data)})
        return data

    def reads(self) -> "list[dict[str, Any]]":
        return list(self._read)


@dataclass(frozen=True)
class MemoryArtifactStore:
    """测试/离线 case 用：显式 dict，键仍是 artifact_id。"""

    blobs: Mapping[str, bytes]
    _read: "list[dict[str, Any]]" = field(default_factory=list, compare=False)

    def read(self, artifact_id: str) -> bytes:
        rel = str(artifact_id)
        if rel.startswith("/") or ".." in rel.split("/"):
            raise DeliveryError("ARTIFACT_PATH", rel)
        if any(rel.startswith(p) for p in FORBIDDEN_PREFIXES):
            raise DeliveryError("ARTIFACT_FORBIDDEN", rel)
        if not any(rel.startswith(p) for p in ALLOWED_ARTIFACT_PREFIXES):
            raise DeliveryError("ARTIFACT_OUTSIDE_ALLOWLIST", rel)
        if rel not in self.blobs:
            raise DeliveryError("ARTIFACT_MISSING", rel)
        data = bytes(self.blobs[rel])
        self._read.append({"artifact_id": rel, "sha256": GT.sha_bytes(data),
                           "n_bytes": len(data)})
        return data


# ---------------------------------------------------------------------------
# 提交解析
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Submission:
    interface: str
    study_id: str
    plan_sha: str
    analysis_receipt_sha: str
    claim: "dict[str, Any]"
    report: "dict[str, Any]"


def _require(doc: Mapping[str, Any], keys: Sequence[str], *, where: str) -> None:
    missing = [k for k in keys if doc.get(k) in (None, "")]
    if missing:
        raise DeliveryError("CLAIM_INCOMPLETE", f"{where} 缺 {missing}")


def _ref_of(claim: Mapping[str, Any]) -> "dict[str, Any]":
    ref = claim.get("result_ref")
    if not isinstance(ref, Mapping):
        raise DeliveryError("CLAIM_INCOMPLETE", "缺 result_ref")
    _require(ref, ("artifact_id", "artifact_sha256", "json_pointer"), where="result_ref")
    return dict(ref)


def parse_submission(doc: Mapping[str, Any], *, interface: str) -> Submission:
    """把两种接口的提交解析成统一结构；只做结构面，不碰数值。"""
    if interface not in INTERFACES:
        raise DeliveryError("INTERFACE_INVALID", interface)
    if not isinstance(doc, Mapping):
        raise DeliveryError("SUBMISSION_INVALID", "顶层必须是对象")
    claims = doc.get("claims")
    if not isinstance(claims, list) or len(claims) != 1:
        raise DeliveryError("CLAIM_INCOMPLETE", "claims 必须恰 1 条（主对比）")
    claim = dict(claims[0])
    _require(claim, ("hypothesis_id", "proposition", "metric_id", "delta"),
             where="claim")
    ref = _ref_of(claim)
    if interface == "verbatim":
        _require(claim, ("estimate", "lower", "upper"), where="claim(verbatim)")
        for key in ("estimate", "lower", "upper"):
            value = claim[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise DeliveryError("CLAIM_INCOMPLETE", f"{key} 非数值")
    report = doc.get("research_report")
    if not isinstance(report, Mapping):
        raise DeliveryError("CLAIM_INCOMPLETE", "缺 research_report")
    study = str(doc.get("study_id") or "")
    plan_sha = str(doc.get("plan_sha256") or "")
    receipt = str(doc.get("analysis_receipt_sha") or "")
    if not study or not plan_sha or not receipt:
        raise DeliveryError("CLAIM_INCOMPLETE",
                            "缺 study_id/plan_sha256/analysis_receipt_sha")
    claim["result_ref"] = ref
    return Submission(interface=interface, study_id=study, plan_sha=plan_sha,
                      analysis_receipt_sha=receipt, claim=claim,
                      report=dict(report))


# ---------------------------------------------------------------------------
# 编译
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CompiledClaim:
    claim: "dict[str, Any]"
    provenance: "dict[str, Any]"

    def audit(self) -> "dict[str, Any]":
        return {
            "parse_valid": True, "binding_valid": True,
            "numeric_fidelity": True, "semantic_valid": None,
            "note": "语义面由独立 evaluator 判（编译器不越位）",
        }


def _resolve_ref(ref: Mapping[str, Any], store: CandidateArtifactStore,
                 *, study_id: str, plan_sha: str,
                 receipt_sha: str) -> "tuple[dict[str, Any], bytes]":
    data = store.read(str(ref["artifact_id"]))
    if GT.sha_bytes(data) != str(ref["artifact_sha256"]):
        raise DeliveryError("CLAIM_BINDING_MISMATCH", "artifact sha 不符")
    try:
        doc = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise DeliveryError("CLAIM_BINDING_MISMATCH", "artifact 不是 JSON") from None
    # 绑定：study/plan/receipt 必须与提交声明一致（防跨 study/旧版本）
    for key, want in (("study_id", study_id), ("plan_sha256", plan_sha),
                      ("analysis_receipt_sha", receipt_sha)):
        got = doc.get(key)
        if got is not None and str(got) != str(want):
            raise DeliveryError("CLAIM_BINDING_MISMATCH", f"{key} 不符")
    try:
        node = GT._resolve_json_pointer(doc, str(ref["json_pointer"]))
    except GT.G1TraceError as exc:
        raise DeliveryError("CLAIM_BINDING_MISMATCH", exc.detail or exc.code) from None
    if not isinstance(node, Mapping):
        raise DeliveryError("CLAIM_BINDING_MISMATCH", "pointer 未指向对象")
    missing = [k for k in ("estimate", "lower", "upper") if k not in node]
    if missing:
        raise DeliveryError("CLAIM_BINDING_MISMATCH", f"引用节点缺 {missing}")
    want_field = ref.get("field_sha256")
    if want_field is not None and GT.field_sha(node) != str(want_field):
        raise DeliveryError("CLAIM_BINDING_MISMATCH", "field_sha256 不符")
    return dict(node), data


def compile_claim(submission: Submission,
                  store: CandidateArtifactStore, *,
                  claim_id: "str | None" = None,
                  direction: "str | None" = None,
                  population_scope: "str | None" = None,
                  sample_scope: "Mapping[str, Any] | None" = None,
                  evidence: Sequence[Mapping[str, Any]] = (),
                  limitations: Sequence[str] = (),
                  alternative_explanations: Sequence[str] = (),
                  next_steps: Sequence[str] = (),
                  ) -> CompiledClaim:
    """机械复制候选引用值；不做任何科学判断。"""
    assert_store_isolation(store)
    claim = submission.claim
    ref = claim["result_ref"]
    node, artifact_bytes = _resolve_ref(
        ref, store, study_id=submission.study_id,
        plan_sha=submission.plan_sha,
        receipt_sha=submission.analysis_receipt_sha)
    values = {"estimate": float(node["estimate"]),
              "lower": float(node["lower"]), "upper": float(node["upper"])}
    fidelity = True
    if submission.interface == "verbatim":
        for key in ("estimate", "lower", "upper"):
            if not close(claim.get(key), values[key]):
                raise DeliveryError(
                    "CLAIM_NUMERIC_MISMATCH",
                    f"{key}: 提交 {claim.get(key)!r} ≠ 引用 {values[key]!r}")
    # C2：sample_scope 与 typed evidence 由编译器从**候选工件**机械补全
    # （Plan §2 的 DeliveryClaimV1 字段），调用方可显式覆盖但不得引入新数值。
    artifact_doc = json.loads(artifact_bytes.decode("utf-8"))
    run_ids = artifact_doc.get("run_ids") or []
    blocks = []
    diag = artifact_doc.get("diagnostics") or {}
    cov = ((diag.get("coverage") if isinstance(diag, Mapping) else None)
           or artifact_doc.get("coverage") or {})
    if isinstance(cov, Mapping) and cov.get("blocks"):
        blocks = list(cov["blocks"])
    elif artifact_doc.get("block_table"):
        blocks = [str(row.get("block_id")) for row in artifact_doc["block_table"]]
    auto_sample = {
        "run_ids_sha": GT.sha_bytes(GT.canonical(sorted(str(r) for r in run_ids)))
        if run_ids else None,
        "block_ids_sha": GT.sha_bytes(GT.canonical(sorted(str(b) for b in blocks)))
        if blocks else None,
        "denominator": len(run_ids) if run_ids else None,
        "dropped": [],
    }
    merged_scope = {**auto_sample, **dict(sample_scope or {})}
    typed_locator = {"artifact_id": str(ref["artifact_id"]),
                     "artifact_sha256": GT.sha_bytes(artifact_bytes),
                     "json_pointer": str(ref["json_pointer"]),
                     "field_sha256": GT.field_sha(node)}
    evidence_out = [dict(item) for item in evidence] or [typed_locator]
    interval = claim.get("interval") or {}
    level = interval.get("level")
    if level is None:
        level = node.get("level", 0.95)
    out_claim = {
        "schema": CLAIM_SCHEMA, "schema_version": 1,
        "claim_id": claim_id or str(claim.get("hypothesis_id")),
        "producer": "candidate",
        "study_id": submission.study_id,
        "plan_sha": submission.plan_sha,
        "analysis_receipt_sha": submission.analysis_receipt_sha,
        "estimand_id": str(claim.get("hypothesis_id")),
        "contrast_id": str(claim.get("contrast_id") or claim.get("hypothesis_id")),
        "metric_id": str(claim["metric_id"]),
        "population_scope": population_scope or "unspecified",
        "estimate": values["estimate"],
        "interval": {"level": float(level),
                     "lower": values["lower"], "upper": values["upper"]},
        "direction": direction or str(claim.get("direction")
                                      or claim.get("proposition")),
        # 状态优先取候选显式声明，缺省时用其所引用节点的状态（仍是候选产物）
        "status": str(claim.get("status") or node.get("status") or "reported"),
        "sample_scope": merged_scope,
        "evidence": evidence_out,
        "result_ref": dict(ref),
        "limitations": list(limitations),
        "alternative_explanations": list(alternative_explanations),
        "next_steps": list(next_steps),
        "value_origin": {k: "candidate_analysis" for k in values},
    }
    provenance = {
        "interface": submission.interface,
        "artifact_id": str(ref["artifact_id"]),
        "artifact_sha256": GT.sha_bytes(artifact_bytes),
        "json_pointer": str(ref["json_pointer"]),
        "referenced_field_sha256": GT.field_sha(node),
        "explicit_values_checked": submission.interface == "verbatim",
        "numeric_fidelity": fidelity,
    }
    return CompiledClaim(claim=out_claim, provenance=provenance)


def render_submission(claim: CompiledClaim) -> str:
    """把交付件渲染成报告正文片段（只排版，不补结论）。"""
    c = claim.claim
    return ("## 主对比\n"
            f"- contrast: {c['contrast_id']}（metric {c['metric_id']}）\n"
            f"- estimate: {c['estimate']}\n"
            f"- interval: [{c['interval']['lower']}, {c['interval']['upper']}]"
            f"（{c['interval']['level']}）\n"
            f"- 方向: {c['direction']}\n"
            f"- 来源: {claim.provenance['artifact_id']}"
            f"#{claim.provenance['json_pointer']}\n")


# ---------------------------------------------------------------------------
# 离线 case（G3）：同一判据跑 verbatim / by_ref
# ---------------------------------------------------------------------------

def _artifact_bytes(value: Any) -> bytes:
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    return GT.canonical(value)


def seal_submission(doc: Mapping[str, Any],
                    artifacts: Mapping[str, Any]) -> "tuple[dict[str, Any], dict[str, bytes]]":
    """把引用里的 sha 按给定 artifacts 填好（fixture 生成用；编译器仍逐项复核）。"""
    out = json.loads(json.dumps(doc, ensure_ascii=False))
    blobs: "dict[str, bytes]" = {}
    for aid, value in artifacts.items():
        blobs[str(aid)] = _artifact_bytes(value)
    for claim in out.get("claims") or []:
        ref = claim.get("result_ref") or {}
        aid = str(ref.get("artifact_id") or "")
        if aid not in blobs:
            # 越权/越出允许前缀的引用：故意不填真实 sha，让编译器在 store
            # 边界以稳定错误码拒绝（ARTIFACT_FORBIDDEN / OUTSIDE_ALLOWLIST）。
            if (any(aid.startswith(p) for p in FORBIDDEN_PREFIXES)
                    or not any(aid.startswith(p) for p in ALLOWED_ARTIFACT_PREFIXES)):
                ref.setdefault("artifact_sha256", "0" * 64)
                claim["result_ref"] = ref
                continue
            raise DeliveryError("ARTIFACT_MISSING", aid)
        data = blobs[aid]
        if not ref.get("artifact_sha256"):
            ref["artifact_sha256"] = GT.sha_bytes(data)
        if not ref.get("field_sha256"):
            node = GT._resolve_json_pointer(json.loads(data.decode("utf-8")),
                                            str(ref.get("json_pointer")))
            ref["field_sha256"] = GT.field_sha(node)
        claim["result_ref"] = ref
    return out, blobs


def run_case(case: Mapping[str, Any]) -> "dict[str, Any]":
    """跑一个 G3 case：返回实际结果、期望与一致性（不写盘）。"""
    case_id = str(case.get("case_id"))
    interface = str(case.get("interface"))
    expect = dict(case.get("expect") or {})
    artifacts = case.get("artifacts") or {}
    submission_doc = case.get("submission") or {}
    try:
        sealed, blobs = seal_submission(submission_doc, artifacts)
        submission = parse_submission(sealed, interface=interface)
        compiled = compile_claim(submission, MemoryArtifactStore(blobs=blobs),
                                 **dict(case.get("compile_options") or {}))
        result = {"ok": True, "code": None,
                  "claim": compiled.claim, "provenance": compiled.provenance}
    except DeliveryError as exc:
        result = {"ok": False, "code": exc.code, "detail": exc.detail}
    want_ok = bool(expect.get("ok", True))
    want_code = expect.get("code")
    matched = (result["ok"] == want_ok
               and (want_ok or result.get("code") == want_code))
    return {"case_id": case_id, "interface": interface,
            "ok": matched, "actual": result, "expect": expect}
