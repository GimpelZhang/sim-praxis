"""R1.3 审查运行收据（ReviewRunReceipt）：来源与隔离的过程证据。

Plan §5：身份不同不够，``other_review_visible=false`` 的**自述**也不够——
调度器必须证明第二通道启动时输入中无第一审结果（隔离目录/FD/env/缓存），
原始请求与输出独立封存。

本模块只做：
* ``build_receipt``：由**调度器**在发车前后的客观事实构造收据（文件/请求/
  响应的逐字节哈希、隔离目录、输入白名单、访问日志摘要）；
* ``verify_receipt``：结构/哈希/绑定/隔离自洽检查；无法证明 → 拒绝
  （``REVIEW_PROVENANCE_UNVERIFIED``）。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from orchestrator.rsi_r1 import profiles as P

RECEIPT_SCHEMA = P.REVIEW_RECEIPT_SCHEMA_V13

_KEYS = frozenset({
    "schema", "schema_version", "review_id", "actor_kind", "reviewer_id",
    "session_id", "process_id", "prompt_sha256", "schema_sha256",
    "source_sha256", "packet_sha256", "input_allowlist", "manifest_sha256",
    "started_at_utc", "ended_at_utc", "request_sha256", "response_sha256",
    "access_log_sha256", "other_review_visible", "isolation",
})
_HEX = re.compile(r"^[0-9a-f]{64}$")
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


class ReceiptError(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"[{code}] {detail}" if detail else code)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class IsolationEvidence:
    """调度器提供的隔离事实（不是模型自述）。"""

    fresh_context: bool
    fresh_process: bool
    env_scrubbed: bool
    cache_dir: str
    other_review_inputs_present: bool
    created_by: str
    evidence: "tuple[dict[str, Any], ...]"

    def to_dict(self) -> "dict[str, Any]":
        return {
            "fresh_context": self.fresh_context,
            "fresh_process": self.fresh_process,
            "env_scrubbed": self.env_scrubbed,
            "cache_dir": self.cache_dir,
            "other_review_inputs_present": self.other_review_inputs_present,
            "created_by": self.created_by,
            "evidence": [dict(e) for e in self.evidence],
        }

    @classmethod
    def from_dict(cls, doc: Any) -> "IsolationEvidence":
        if not isinstance(doc, Mapping) or set(doc) != {
                "fresh_context", "fresh_process", "env_scrubbed", "cache_dir",
                "other_review_inputs_present", "created_by", "evidence"}:
            raise ReceiptError("ISOLATION_INVALID", "隔离事实字段集不符")
        evidence = doc["evidence"]
        if not isinstance(evidence, (list, tuple)) or not evidence:
            raise ReceiptError("ISOLATION_INVALID", "隔离证据为空（自述不算证据）")
        rows = []
        for i, row in enumerate(evidence):
            if not isinstance(row, Mapping) or set(row) != {"kind", "sha256"}:
                raise ReceiptError("ISOLATION_INVALID", f"evidence[{i}]")
            if not _HEX.fullmatch(str(row["sha256"])):
                raise ReceiptError("ISOLATION_INVALID", f"evidence[{i}].sha256")
            rows.append({"kind": str(row["kind"]), "sha256": str(row["sha256"])})
        return cls(fresh_context=bool(doc["fresh_context"]),
                   fresh_process=bool(doc["fresh_process"]),
                   env_scrubbed=bool(doc["env_scrubbed"]),
                   cache_dir=str(doc["cache_dir"]),
                   other_review_inputs_present=bool(
                       doc["other_review_inputs_present"]),
                   created_by=str(doc["created_by"]), evidence=tuple(rows))


def build_receipt(*, review_id: str, reviewer_id: str, session_id: str,
                  process_id: int, prompt: bytes, schema: bytes,
                  source: bytes, packet: bytes, request: bytes,
                  response: bytes, access_log: bytes,
                  input_allowlist: Sequence[str], started_at_utc: str,
                  ended_at_utc: str, isolation: IsolationEvidence,
                  actor_kind: str = "model_reviewer") -> "dict[str, Any]":
    return {
        "schema": RECEIPT_SCHEMA,
        "schema_version": 1,
        "review_id": str(review_id),
        "actor_kind": str(actor_kind),
        "reviewer_id": str(reviewer_id),
        "session_id": str(session_id),
        "process_id": int(process_id),
        "prompt_sha256": sha256_bytes(prompt),
        "schema_sha256": sha256_bytes(schema),
        "source_sha256": sha256_bytes(source),
        "packet_sha256": sha256_bytes(packet),
        "input_allowlist": list(input_allowlist),
        "manifest_sha256": sha256_bytes("\n".join(input_allowlist).encode()),
        "started_at_utc": str(started_at_utc),
        "ended_at_utc": str(ended_at_utc),
        "request_sha256": sha256_bytes(request),
        "response_sha256": sha256_bytes(response),
        "access_log_sha256": sha256_bytes(access_log),
        "other_review_visible": False,
        "isolation": isolation.to_dict(),
    }


def verify_receipt(receipt: Any, *, review_id: str, reviewer_id: str,
                   packet_sha256: str) -> "dict[str, Any]":
    if not isinstance(receipt, Mapping):
        raise ReceiptError("REVIEW_PROVENANCE_UNVERIFIED", "收据不是对象")
    unknown = sorted(set(receipt) - _KEYS)
    missing = sorted(_KEYS - set(receipt))
    if unknown or missing:
        raise ReceiptError("REVIEW_PROVENANCE_UNVERIFIED",
                           f"收据未知 {unknown} 缺 {missing}")
    if receipt["schema"] != RECEIPT_SCHEMA:
        raise ReceiptError("REVIEW_PROVENANCE_UNVERIFIED", "收据 schema")
    if receipt["review_id"] != review_id:
        raise ReceiptError("REVIEW_PROVENANCE_UNVERIFIED", "review_id 不一致")
    if receipt["reviewer_id"] != reviewer_id:
        raise ReceiptError("REVIEW_PROVENANCE_UNVERIFIED", "reviewer_id 不一致")
    if receipt["packet_sha256"] != packet_sha256:
        raise ReceiptError("REVIEW_PROVENANCE_UNVERIFIED", "packet sha 不一致")
    for key in ("prompt_sha256", "schema_sha256", "source_sha256",
                "packet_sha256", "manifest_sha256", "request_sha256",
                "response_sha256", "access_log_sha256"):
        if not _HEX.fullmatch(str(receipt[key])):
            raise ReceiptError("REVIEW_PROVENANCE_UNVERIFIED", f"{key} 非 sha256")
    for key in ("started_at_utc", "ended_at_utc"):
        if not _ISO.fullmatch(str(receipt[key])):
            raise ReceiptError("REVIEW_PROVENANCE_UNVERIFIED", f"{key} 非 UTC Z")
    if receipt["started_at_utc"] > receipt["ended_at_utc"]:
        raise ReceiptError("REVIEW_PROVENANCE_UNVERIFIED", "时间倒序")
    if receipt["other_review_visible"] is not False:
        raise ReceiptError("REVIEW_PROVENANCE_UNVERIFIED",
                           "other_review_visible 必须为 false")
    isolation = IsolationEvidence.from_dict(receipt["isolation"])
    if not (isolation.fresh_context and isolation.fresh_process
            and isolation.env_scrubbed):
        raise ReceiptError("REVIEW_PROVENANCE_UNVERIFIED",
                           "调度器未证明全新上下文/进程/环境清洗")
    if isolation.other_review_inputs_present:
        raise ReceiptError("REVIEW_PROVENANCE_UNVERIFIED",
                           "第二通道输入中存在第一审结果")
    if isolation.created_by != "scheduler":
        raise ReceiptError("REVIEW_PROVENANCE_UNVERIFIED",
                           "隔离证据必须由调度器创建")
    allow = receipt["input_allowlist"]
    if not isinstance(allow, list) or not allow:
        raise ReceiptError("REVIEW_PROVENANCE_UNVERIFIED", "输入白名单为空")
    return dict(receipt)


def public_schema() -> "dict[str, Any]":
    return {
        "schema": RECEIPT_SCHEMA,
        "keys": sorted(_KEYS),
        "isolation": {
            "required": ["fresh_context", "fresh_process", "env_scrubbed",
                         "cache_dir", "other_review_inputs_present",
                         "created_by", "evidence"],
            "rule": ("身份不同/自述 false 都不够；调度器须给出隔离目录、"
                     "输入白名单与请求/响应逐字节哈希"),
        },
    }
