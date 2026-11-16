"""RSI-G1 证据链旁路：输入清单、跳点事件与历史回放（**零模型 POST**）。

Plan `~/文档/RSI_G1_Plan_detailed.md` §2/§2.1/§5.1 的最小实现：

* ``inventory_evidence``：按**显式 allowlist**（root + relpath 列表）登记输入，
  lstat 拒符号链接、realpath 必须在根内、按根/总量上限 fail-closed；
  缺件记 ``missing`` 且 sha/n_bytes 为 null（MISSING 不是空字节 hash）。
* ``G1TraceEventV1``：每跳一个事件，``parent_event_sha256`` 连成链；
  整体 manifest 记 ``event_id → event_sha256`` 与 ``trace_sha256``。
* ``trace_study``：从**同期归档原件**回放一个 study 的跳点：
  analysis 原字节 / confirm 返回 / 模型可见裁剪视图（RECONSTRUCTED）/
  请求（历史未落盘 ⇒ MISSING）/ 回复原文 / 解析动作 / 落盘 claim。
  只有直接原件记 ``observed``；按固定旧代码重建的记 ``reconstructed``；
  缺任一必要输入 ⇒ 该段归因 ``UNOBSERVABLE``（不猜模型错误）。

本模块只读旧根、只写 G1 根；不导入 dispatcher/sender/物理入口。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from orchestrator.common.clock import utc_now_iso_z
from orchestrator.common.fsutil import (atomic_write_bytes, atomic_write_text,
                                        ensure_private_dir)
from orchestrator.common import research_transport as T

SCHEMA_EVENT = "simpraxis.rsi_g1.trace_event.v1"
SCHEMA_TRACE = "simpraxis.rsi_g1.trace.v1"
SCHEMA_MANIFEST = "simpraxis.rsi_g1.input_manifest.v1"

EVENT_KINDS: "tuple[str, ...]" = (
    "analysis_output", "dispatch_return", "clipped_view", "request_serialized",
    "gateway_capture", "response_raw", "action_parsed", "claim_persisted",
)
VISIBILITIES: "tuple[str, ...]" = ("observed", "reconstructed", "missing")
FIRST_DIVERGENCE: "tuple[str, ...]" = (
    "CANDIDATE_OUTPUT", "RETURN_VIEW", "MODEL_VISIBLE_VIEW", "SUBMIT_REQUEST",
    "PERSISTED_CLAIM", "UNOBSERVABLE",
)

#: 回放上限（Plan §6：只读输入 ≤2 GiB、输出 ≤100 MiB、单次墙钟 30 min 由 CLI 计时）。
MAX_INPUT_BYTES = 2 * 1024 ** 3
MAX_OUTPUT_BYTES = 100 * 1024 ** 2
DEFAULT_MAX_FILES = 4096

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class G1TraceError(RuntimeError):
    """G1 取证面错误（``code`` 公开；不是科学结论）。"""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"[{code}] {detail}" if detail else code)


# ---------------------------------------------------------------------------
# 规范字节与摘要
# ---------------------------------------------------------------------------

def canonical(obj: Any) -> bytes:
    """固定规范形式（Plan §2.1：sort_keys/紧凑分隔/非 ASCII 原样/禁 NaN）。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def field_sha(value: Any) -> str:
    """字段级摘要：与原件字节无关的**规范对象** hash（Plan §2.1 明确区分）。"""
    return sha_bytes(canonical(value))


def materialize(root: Path, sub: str, data: bytes) -> str:
    """把原字节写进 G1 根（0600，原子），返回根内相对路径（artifact_ref 唯一合法面）。"""
    digest = sha_bytes(data)
    rel = f"trace/{sub}/artifacts/{digest}.bin"
    target = Path(root) / rel
    ensure_private_dir(target.parent)
    atomic_write_bytes(target, data, mode=0o600)
    if sha_bytes(target.read_bytes()) != digest:  # 源/目标 hash 必须相等
        raise G1TraceError("ARTIFACT_HASH", rel)
    return rel


def _is_json_value(value: Any) -> bool:
    try:
        canonical(value)
    except (TypeError, ValueError):
        return False
    return True


def _resolve_json_pointer(doc: Any, pointer: str) -> Any:
    """RFC6901 解析；不存在/重复键/非法指针一律拒。"""
    if pointer == "":
        return doc
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise G1TraceError("POINTER_INVALID", pointer)
    node = doc
    for raw in pointer.split("/")[1:]:
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(node, Mapping):
            if token not in node:
                raise G1TraceError("POINTER_MISSING", pointer)
            node = node[token]
        elif isinstance(node, list):
            try:
                node = node[int(token)]
            except (ValueError, IndexError):
                raise G1TraceError("POINTER_MISSING", pointer) from None
        else:
            raise G1TraceError("POINTER_MISSING", pointer)
    return node


# ---------------------------------------------------------------------------
# 事件
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TraceEvent:
    diagnostic_id: str
    seq: int
    event_kind: str
    visibility: str
    study_id: str = ""
    phase: str = ""
    attempt_id: str = ""
    action_id: str = ""
    response_id: str = ""
    state_sha256: "str | None" = None
    artifact_ref: "str | None" = None
    bytes_sha256: "str | None" = None
    n_bytes: "int | None" = None
    source_commit: str = ""
    request_sha256: "str | None" = None
    recorded_at: str = field(default_factory=utc_now_iso_z)
    status: str = "OK"
    error_code: "str | None" = None
    missing_reason: "str | None" = None
    parent_event_id: "str | None" = None
    parent_event_sha256: "str | None" = None
    extra: "Mapping[str, Any]" = field(default_factory=dict)

    def body(self) -> "dict[str, Any]":
        if self.event_kind not in EVENT_KINDS:
            raise G1TraceError("EVENT_KIND", self.event_kind)
        if self.visibility not in VISIBILITIES:
            raise G1TraceError("VISIBILITY", self.visibility)
        body = {
            "schema": SCHEMA_EVENT, "diagnostic_id": self.diagnostic_id,
            "seq": self.seq, "event_kind": self.event_kind,
            "visibility": self.visibility, "study_id": self.study_id,
            "phase": self.phase,
            "attempt_id": self.attempt_id, "action_id": self.action_id,
            "response_id": self.response_id,
            "state_sha256": self.state_sha256,
            "artifact_ref": self.artifact_ref,
            "bytes_sha256": self.bytes_sha256, "n_bytes": self.n_bytes,
            "source_commit": self.source_commit,
            "request_sha256": self.request_sha256,
            "recorded_at": self.recorded_at, "status": self.status,
            "error_code": self.error_code,
            "missing_reason": self.missing_reason,
            "parent_event_id": self.parent_event_id,
            "parent_event_sha256": self.parent_event_sha256,
        }
        if self.extra:
            body["extra"] = dict(self.extra)
        # MISSING 的字节面必须是 null（不是空字节 hash）
        if self.visibility == "missing" and (self.bytes_sha256 or self.n_bytes):
            raise G1TraceError("MISSING_HAS_BYTES", self.event_kind)
        return body

    def event_id(self) -> str:
        return sha_bytes(canonical(self.body()))

    def event_sha256(self) -> str:
        body = dict(self.body())
        body["event_id"] = self.event_id()
        return sha_bytes(canonical(body))


class TraceWriter:
    """按 seq 追加事件、自动串 parent；manifest 最后提交。"""

    def __init__(self, out_dir: Path, *, diagnostic_id: str,
                 source_commit: str = "") -> None:
        ensure_private_dir(out_dir)
        self.out_dir = Path(out_dir)
        self.diagnostic_id = diagnostic_id
        self.source_commit = source_commit
        self.events: "list[TraceEvent]" = []
        self._ids: "set[str]" = set()
        self._sealed = False

    def add(self, event: TraceEvent) -> TraceEvent:
        if self._sealed:
            raise G1TraceError("TRACE_SEALED", self.out_dir.as_posix())
        if event.diagnostic_id != self.diagnostic_id:
            raise G1TraceError("DIAGNOSTIC_MISMATCH", event.diagnostic_id)
        seq = len(self.events) + 1
        parent = self.events[-1] if self.events else None
        event = TraceEvent(**{**event.__dict__,
                              "seq": seq,
                              "parent_event_id": None if parent is None else parent.event_id(),
                              "parent_event_sha256": None if parent is None else parent.event_sha256()})
        eid = event.event_id()
        if eid in self._ids:
            raise G1TraceError("EVENT_DUPLICATE", eid)
        self._ids.add(eid)
        self.events.append(event)
        return event

    def write(self, *, now: "str | None" = None) -> "dict[str, Any]":
        if self._sealed:
            raise G1TraceError("TRACE_SEALED", self.out_dir.as_posix())
        lines = [json.dumps({**event.body(), "event_id": event.event_id(),
                             "event_sha256": event.event_sha256()},
                            ensure_ascii=False, sort_keys=True)
                 for event in self.events]
        payload = "\n".join(lines) + "\n" if lines else ""
        atomic_write_text(self.out_dir / "events.jsonl", payload, mode=0o600)
        manifest = {
            "schema": SCHEMA_TRACE, "schema_version": 1,
            "diagnostic_id": self.diagnostic_id,
            "source_commit": self.source_commit,
            "n_events": len(self.events),
            "events": {event.event_id(): event.event_sha256()
                       for event in self.events},
            "kinds": [event.event_kind for event in self.events],
            "visibilities": [event.visibility for event in self.events],
            "created_at_utc": now or utc_now_iso_z(),
        }
        manifest["trace_sha256"] = sha_bytes(canonical(
            {k: v for k, v in manifest.items() if k != "created_at_utc"}))
        atomic_write_text(self.out_dir / "trace.json",
                          json.dumps(manifest, ensure_ascii=False,
                                     sort_keys=True, indent=1) + "\n",
                          mode=0o600)
        self._sealed = True
        return manifest


def verify_trace(trace_dir: Path) -> "dict[str, Any]":
    """复核 trace：逐事件重算 event_sha、父链连续、manifest 计数一致。"""
    trace_dir = Path(trace_dir)
    problems: "list[str]" = []
    try:
        manifest = json.loads((trace_dir / "trace.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "problems": [f"trace.json: {exc}"]}
    lines = [line for line in
             (trace_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
             if line.strip()] if (trace_dir / "events.jsonl").is_file() else []
    if len(lines) != int(manifest.get("n_events", -1)):
        problems.append("事件数与 manifest 不符")
    prev: "dict[str, Any] | None" = None
    for i, line in enumerate(lines, start=1):
        doc = json.loads(line)
        eid, esha = doc.pop("event_id"), doc.pop("event_sha256")
        body = {k: v for k, v in doc.items()}
        body.pop("seq", None)
        body["seq"] = i
        if sha_bytes(canonical(body)) != eid:
            problems.append(f"seq{i}:event_id")
        check = dict(body, event_id=eid)
        if sha_bytes(canonical(check)) != esha:
            problems.append(f"seq{i}:event_sha")
        if manifest.get("events", {}).get(eid) != esha:
            problems.append(f"seq{i}:manifest")
        if prev is None and doc.get("parent_event_id") is not None:
            problems.append("首事件不应有 parent")
        if prev is not None and doc.get("parent_event_id") != prev["event_id"]:
            problems.append(f"seq{i}:parent")
        prev = {"event_id": eid, "event_sha256": esha}
    return {"ok": not problems, "n_events": len(lines), "problems": problems}


# ---------------------------------------------------------------------------
# 输入清单
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RootSpec:
    root_id: str
    path: Path
    max_files: int = DEFAULT_MAX_FILES
    max_total_bytes: int = MAX_INPUT_BYTES


@dataclass(frozen=True)
class FileSpec:
    root_id: str
    relpath: str
    role: str
    sensitivity: str = "private"


def _safe_relpath(relpath: str) -> Path:
    rel = Path(relpath)
    if rel.is_absolute() or ".." in rel.parts or rel.as_posix() != relpath:
        raise G1TraceError("ALLOWLIST_PATH", relpath)
    return rel


def inventory_evidence(roots: Sequence[RootSpec], files: Sequence[FileSpec], *,
                       out_root: Path, diagnostic_id: str,
                       source_commit: str = "",
                       now: "str | None" = None) -> "dict[str, Any]":
    """按显式清单登记输入：lstat 拒 symlink、realpath 必须在根内。"""
    if not _SAFE_ID.match(diagnostic_id):
        raise G1TraceError("DIAGNOSTIC_ID", diagnostic_id)
    by_id = {root.root_id: root for root in roots}
    if len(by_id) != len(roots):
        raise G1TraceError("ROOT_DUPLICATE", "root_id 重复")
    entries: "list[dict[str, Any]]" = []
    per_root: "dict[str, list[int]]" = {rid: [0, 0] for rid in by_id}
    total = 0
    for spec in files:
        root = by_id.get(spec.root_id)
        if root is None:
            raise G1TraceError("ROOT_UNKNOWN", spec.root_id)
        rel = _safe_relpath(spec.relpath)
        base = Path(root.path)
        if not base.is_dir():
            raise G1TraceError("ROOT_MISSING", base.as_posix())
        path = base / rel
        # 逐级 lstat：任何一级是符号链接即拒（不跟随）
        cur = base
        for part in rel.parts:
            cur = cur / part
            try:
                st = os.lstat(cur)
            except FileNotFoundError:
                entries.append({"root_id": spec.root_id, "relpath": rel.as_posix(),
                                "role": spec.role, "sensitivity": spec.sensitivity,
                                "state": "missing", "sha256": None,
                                "n_bytes": None,
                                "missing_reason": "FILE_ABSENT"})
                break
            if os.path.islink(cur):
                raise G1TraceError("ALLOWLIST_SYMLINK", cur.as_posix())
        else:
            data = path.read_bytes()
            per_root[spec.root_id][0] += 1
            per_root[spec.root_id][1] += len(data)
            total += len(data)
            if per_root[spec.root_id][0] > int(root.max_files):
                raise G1TraceError("FILE_CAP", root.root_id)
            if per_root[spec.root_id][1] > int(root.max_total_bytes):
                raise G1TraceError("ROOT_BYTES_CAP", root.root_id)
            if total > MAX_INPUT_BYTES:
                raise G1TraceError("TOTAL_BYTES_CAP", str(total))
            entries.append({"root_id": spec.root_id, "relpath": rel.as_posix(),
                            "role": spec.role, "sensitivity": spec.sensitivity,
                            "state": "observed", "sha256": sha_bytes(data),
                            "n_bytes": len(data), "missing_reason": None,
                            "abs_path": str(path)})
    manifest = {
        "schema": SCHEMA_MANIFEST, "schema_version": 1,
        "diagnostic_id": diagnostic_id, "source_commit": source_commit,
        "roots": [{"root_id": r.root_id, "path": str(r.path),
                   "max_files": int(r.max_files),
                   "max_total_bytes": int(r.max_total_bytes)} for r in roots],
        "files": entries, "n_files": len(entries),
        "total_bytes": total,
        "created_at_utc": now or utc_now_iso_z(),
    }
    out = Path(out_root) / "manifest"
    ensure_private_dir(out)
    atomic_write_text(out / "input_manifest.json",
                      json.dumps(manifest, ensure_ascii=False, sort_keys=True,
                                 indent=1) + "\n", mode=0o600)
    return manifest


# ---------------------------------------------------------------------------
# 历史回放
# ---------------------------------------------------------------------------

def _read_json(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _events_of(rows: Iterable[Mapping[str, Any]], kind: str) -> "list[Mapping[str, Any]]":
    return [row for row in rows if row.get("event") == kind]


def _study_responses(rows: Sequence[Mapping[str, Any]], study: str) -> "list[str]":
    """该 study 的 response_id 前缀（``r1-study_A-…`` / ``…study_B…``）。"""
    tag = f"study_{study}"
    out: "list[str]" = []
    for row in rows:
        if row.get("event") != "action":
            continue
        rid = str((row.get("payload") or {}).get("response_id") or "")
        if tag in rid and rid not in out:
            out.append(rid)
    return out


def trace_study(*, source_root: Path, study: str, diagnostic_id: str,
                out_root: Path, source_commit: str = "",
                now: "str | None" = None) -> "dict[str, Any]":
    """回放一个 study 的跳点；返回 trace 摘要（含首分歧判定）。"""
    if not _SAFE_ID.match(diagnostic_id):
        raise G1TraceError("DIAGNOSTIC_ID", diagnostic_id)
    if study not in ("A", "B"):
        raise G1TraceError("STUDY", study)
    root = Path(source_root)
    journal_path = root / "state" / "journal.jsonl"
    if not journal_path.is_file():
        raise G1TraceError("JOURNAL_MISSING", journal_path.as_posix())
    rows = [json.loads(line) for line in
            journal_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    writer = TraceWriter(Path(out_root) / "trace" / study,
                         diagnostic_id=diagnostic_id,
                         source_commit=source_commit)
    gaps: "list[str]" = []
    refs: "dict[str, Any]" = {}

    # 1) 已封存 analysis 原字节（confirm 阶段）
    raw_rel = f"private/analysis/{study}-confirm.raw.json"
    raw_path = root / raw_rel
    if raw_path.is_file() and not raw_path.is_symlink():
        data = raw_path.read_bytes()
        refs["analysis_output"] = data
        ref_rel = materialize(Path(out_root), study, data)
        writer.add(TraceEvent(diagnostic_id=diagnostic_id, seq=0,
                              event_kind="analysis_output", visibility="observed",
                              study_id=study, artifact_ref=ref_rel,
                              bytes_sha256=sha_bytes(data), n_bytes=len(data),
                              source_commit=source_commit,
                              extra={"source_relpath": raw_rel}))
    else:
        gaps.append("analysis_output")
        writer.add(TraceEvent(diagnostic_id=diagnostic_id, seq=0,
                              event_kind="analysis_output", visibility="missing",
                              study_id=study, status="MISSING",
                              error_code="MISSING",
                              missing_reason="analysis 原始字节未归档",
                              source_commit=source_commit))

    # 2) confirm 返回（dispatcher 边界，同期归档在 journal 的 action_result）
    confirm_results = [row["payload"]["result"] for row in rows
                       if row.get("event") == "action_result"
                       and (row.get("payload") or {}).get("action") == "run_confirm"
                       and (row.get("payload") or {}).get("result", {}).get("ok")]
    # 归属：用紧邻的 action 事件的 response_id 前缀
    confirm_result = None
    for row in rows:
        if row.get("event") != "action":
            continue
        rid = str((row.get("payload") or {}).get("response_id") or "")
        if f"study_{study}" not in rid:
            continue
        name = (row.get("payload") or {}).get("name")
        if name != "run_confirm":
            continue
        # 找同一 response_id 的 action_result
        for res in rows:
            if (res.get("event") == "action_result"
                    and (res.get("payload") or {}).get("response_id") == rid):
                doc = (res.get("payload") or {}).get("result") or {}
                if doc.get("ok"):
                    confirm_result = doc
                break
    if confirm_result is not None:
        cb = canonical(confirm_result)
        cb_rel = materialize(Path(out_root), study, cb)
        writer.add(TraceEvent(diagnostic_id=diagnostic_id, seq=0,
                              event_kind="dispatch_return", visibility="observed",
                              study_id=study, artifact_ref=cb_rel,
                              bytes_sha256=sha_bytes(cb),
                              n_bytes=len(cb), source_commit=source_commit))
    else:
        gaps.append("dispatch_return")
        writer.add(TraceEvent(diagnostic_id=diagnostic_id, seq=0,
                              event_kind="dispatch_return", visibility="missing",
                              study_id=study, status="MISSING", error_code="MISSING",
                              missing_reason="confirm 返回未在 journal 归档",
                              source_commit=source_commit))

    # 3) 模型可见视图：用固定旧代码重建（RECONSTRUCTED，不冒充历史字节）
    visible_keys: "list[str] | None" = None
    if confirm_result is not None:
        clipped = T.Turn(model_text="", result=confirm_result).clipped()
        view = dict(clipped.result)
        vb = canonical(view)
        visible_keys = sorted(view)
        vb_rel = materialize(Path(out_root), study, vb)
        writer.add(TraceEvent(diagnostic_id=diagnostic_id, seq=0,
                              event_kind="clipped_view", visibility="reconstructed",
                              study_id=study, artifact_ref=vb_rel,
                              bytes_sha256=sha_bytes(vb),
                              n_bytes=len(vb), source_commit=source_commit,
                              extra={"retained_keys": visible_keys,
                                     "clip_limits": {
                                         "tool_reply_max_bytes": T.TOOL_REPLY_MAX_BYTES,
                                         "error_reply_max_bytes": T.ERROR_REPLY_MAX_BYTES}}))
    else:
        gaps.append("clipped_view")

    # 4) 请求原字节：历史未落盘 ⇒ MISSING（不得用当前代码重建冒充）
    gaps.append("request_serialized")
    writer.add(TraceEvent(diagnostic_id=diagnostic_id, seq=0,
                          event_kind="request_serialized", visibility="missing",
                          study_id=study, status="MISSING", error_code="MISSING",
                          missing_reason="历史请求字节未归档（只存回复与结果）",
                          source_commit=source_commit))
    # Plan §2.1：gateway capture 不存在时必须记 MISSING，不补造
    gaps.append("gateway_capture")
    writer.add(TraceEvent(diagnostic_id=diagnostic_id, seq=0,
                          event_kind="gateway_capture", visibility="missing",
                          study_id=study, status="MISSING", error_code="MISSING",
                          missing_reason="历史无网关发送适配器捕获",
                          source_commit=source_commit))

    # 5) 提交回复原文（submit_claim 那一轮）
    submit_rid = None
    for row in rows:
        if row.get("event") != "action":
            continue
        rid = str((row.get("payload") or {}).get("response_id") or "")
        if f"study_{study}" in rid and (row.get("payload") or {}).get("name") == "submit_claim":
            submit_rid = rid
    reply_rel = None
    for row in rows:
        if row.get("event") == "reply" and (row.get("payload") or {}).get("response_id") == submit_rid:
            reply_rel = (row.get("payload") or {}).get("relpath")
    if reply_rel and (root / reply_rel).is_file():
        data = (root / reply_rel).read_bytes()
        refs["response_raw"] = data
        reply_ref = materialize(Path(out_root), study, data)
        writer.add(TraceEvent(diagnostic_id=diagnostic_id, seq=0,
                              event_kind="response_raw", visibility="observed",
                              study_id=study, response_id=submit_rid or "",
                              artifact_ref=reply_ref, bytes_sha256=sha_bytes(data),
                              n_bytes=len(data), source_commit=source_commit,
                              extra={"source_relpath": reply_rel}))
    else:
        gaps.append("response_raw")
        writer.add(TraceEvent(diagnostic_id=diagnostic_id, seq=0,
                              event_kind="response_raw", visibility="missing",
                              study_id=study, status="MISSING", error_code="MISSING",
                              missing_reason="submit 回复原文缺失",
                              source_commit=source_commit))

    # 6) 解析动作（journal action 事件）
    action_doc = None
    for row in rows:
        if row.get("event") == "action" and (row.get("payload") or {}).get("response_id") == submit_rid:
            action_doc = (row.get("payload") or {}).get("payload")
            break
    if action_doc is not None:
        ab = canonical(action_doc)
        refs["action_parsed"] = action_doc
        writer.add(TraceEvent(diagnostic_id=diagnostic_id, seq=0,
                              event_kind="action_parsed", visibility="observed",
                              study_id=study, response_id=submit_rid or "",
                              bytes_sha256=sha_bytes(ab), n_bytes=len(ab),
                              source_commit=source_commit))
    else:
        gaps.append("action_parsed")

    # 7) 落盘 claim
    claim_rel = f"claim/submission_{study}.json"
    claim_path = root / claim_rel
    claim_doc = None
    if claim_path.is_file() and not claim_path.is_symlink():
        data = claim_path.read_bytes()
        claim_doc = json.loads(data)
        refs["claim_persisted"] = claim_doc
        claim_ref = materialize(Path(out_root), study, data)
        writer.add(TraceEvent(diagnostic_id=diagnostic_id, seq=0,
                              event_kind="claim_persisted", visibility="observed",
                              study_id=study, artifact_ref=claim_ref,
                              bytes_sha256=sha_bytes(data), n_bytes=len(data),
                              source_commit=source_commit,
                              extra={"source_relpath": claim_rel}))
    else:
        gaps.append("claim_persisted")

    # -- R4：receipt 交叉核验（output_sha256 必须等于 analysis 原字节 hash） --
    receipt_check = {"present": False, "output_sha_ok": None,
                     "manifest_sha_ok": None, "problems": []}
    rec_path = root / f"private/receipts/{study}-confirm.json"
    man_path = root / f"confirm/manifest_{study}.json"
    if rec_path.is_file():
        receipt_check["present"] = True
        rec = json.loads(rec_path.read_text(encoding="utf-8"))
        raw_bytes = refs.get("analysis_output")
        if raw_bytes is None:
            receipt_check["problems"].append("analysis 原字节缺失")
        else:
            receipt_check["output_sha_ok"] = (
                str(rec.get("output_sha256") or "") == sha_bytes(raw_bytes))
            if not receipt_check["output_sha_ok"]:
                receipt_check["problems"].append("output_sha256 不符")
        if man_path.is_file():
            # 凭据记的是 manifest 文档内的规范 sha（不含该字段本身），
            # 不是文件字节 sha（轮 P 复盘实测）。
            man_doc = json.loads(man_path.read_text(encoding="utf-8"))
            receipt_check["manifest_sha_ok"] = (
                str(rec.get("confirm_manifest_sha256") or "")
                == str(man_doc.get("manifest_sha256") or ""))
            if not receipt_check["manifest_sha_ok"]:
                receipt_check["problems"].append("confirm_manifest sha 不符")
    else:
        receipt_check["problems"].append("凭据件缺失")

    # -- R3：submit 五面信封（response/action/report/rendered/claim 各自 hash）--
    envelope = {}
    if refs.get("response_raw") is not None:
        envelope["response_sha"] = sha_bytes(refs["response_raw"])
    if action_doc is not None:
        envelope["action_sha"] = sha_bytes(canonical(action_doc))
        report_doc = action_doc.get("research_report")
        if isinstance(report_doc, Mapping):
            envelope["research_report_sha"] = sha_bytes(canonical(report_doc))
        rendered = action_doc.get("report_text")
        if isinstance(rendered, str):
            envelope["rendered_sha"] = sha_bytes(rendered.encode("utf-8"))
        claims = action_doc.get("claims")
        if isinstance(claims, list) and claims:
            envelope["claim_sha"] = sha_bytes(canonical(claims[0]))
    if claim_doc is not None and "claim_sha" not in envelope:
        claims = claim_doc.get("claims")
        if isinstance(claims, list) and claims:
            envelope["claim_sha"] = sha_bytes(canonical(claims[0]))
    envelope_doc = {"schema": "simpraxis.rsi_g1.submit_envelope.v1",
                    "schema_version": 1, "study": study, "shas": envelope}
    ensure_private_dir(Path(out_root) / "trace" / study)
    atomic_write_text(Path(out_root) / "trace" / study / "submit_envelope.json",
                      json.dumps(envelope_doc, ensure_ascii=False,
                                 sort_keys=True, indent=1) + "\n", mode=0o600)

    # -- 数值对照：candidate_output → (可见视图) → claim ------------------
    def _cand_estimate(doc: Mapping[str, Any]) -> "float | None":
        try:
            return float((doc["candidate_output"]["contrasts"][0])["estimate"])
        except (KeyError, IndexError, TypeError, ValueError):
            return None

    def _claim_estimate(doc: Mapping[str, Any]) -> "float | None":
        try:
            return float((doc["claims"][0])["estimate"])
        except (KeyError, IndexError, TypeError, ValueError):
            return None

    cand = _cand_estimate(confirm_result or {})
    claim = _claim_estimate(claim_doc or {})
    visible = None
    if confirm_result is not None and visible_keys is not None:
        clipped = T.Turn(model_text="", result=confirm_result).clipped().result
        try:
            if "candidate_output" in clipped:
                visible = float(clipped["candidate_output"]["contrasts"][0]["estimate"])
        except (KeyError, IndexError, TypeError, ValueError):
            visible = None
    tol = 1e-8 + 1e-6 * abs(cand) if cand is not None else None
    mismatch = (cand is not None and claim is not None and tol is not None
                and abs(cand - claim) > tol)
    suspected = None
    if mismatch:
        suspected = ("RETURN_VIEW" if visible is None
                     else "PERSISTED_CLAIM" if abs(visible - claim) <= tol
                     else "MODEL_VISIBLE_VIEW")
    # 归因等级：视图是 RECONSTRUCTED、请求 MISSING ⇒ 只能 UNOBSERVABLE
    first = "UNOBSERVABLE" if (mismatch or gaps) else "PERSISTED_CLAIM"
    summary = {
        "study": study, "diagnostic_id": diagnostic_id,
        "observed": sorted(k for k, v in refs.items() if isinstance(v, (bytes, dict))),
        "gaps": gaps,
        "comparisons": {
            "candidate_output_estimate": cand,
            "model_visible_estimate": visible,
            "persisted_claim_estimate": claim,
            "tolerance": tol,
            "numeric_mismatch": bool(mismatch),
            "visible_keys": visible_keys,
        },
        "first_divergence": first,
        "suspected": suspected,
        "receipt_check": receipt_check,
        "submit_envelope": envelope,
        "kind": "history",
        "attribution_level": "RECONSTRUCTED" if suspected else None,
        "note": ("历史请求字节未归档：视图只能按固定旧代码重建，"
                 "按 Plan §5.1 不得据此给候选归因"),
    }
    manifest = writer.write(now=now)
    atomic_write_text(Path(out_root) / "trace" / f"{study}_summary.json",
                      json.dumps(summary, ensure_ascii=False, sort_keys=True,
                                 indent=1) + "\n", mode=0o600)
    return {"manifest": manifest, "summary": summary}


# ---------------------------------------------------------------------------
# A 层状态机（Plan §3.1：REGISTERED→INVENTORIED→TRACED→AUDITED→TESTED→CLOSED）
# ---------------------------------------------------------------------------

STEPS: "tuple[str, ...]" = ("REGISTERED", "INVENTORIED", "TRACED", "AUDITED",
                            "TESTED", "CLOSED")
STATE_REL = "state.json"


class G1State:
    """按序推进的步骤记录；跳步/回退/UUID 缺失一律 fail-closed。"""

    def __init__(self, root: Path, *, diagnostic_id: str) -> None:
        if not _SAFE_ID.match(diagnostic_id):
            raise G1TraceError("DIAGNOSTIC_ID", diagnostic_id)
        self.root = Path(root)
        self.diagnostic_id = diagnostic_id
        self.path = self.root / STATE_REL

    def load(self) -> "dict[str, Any]":
        if not self.path.is_file():
            return {"schema": "simpraxis.rsi_g1.state.v1", "schema_version": 1,
                    "diagnostic_id": self.diagnostic_id, "step": "REGISTERED",
                    "history": [{"step": "REGISTERED", "at_utc": None}]}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def advance(self, step: str, *, now: "str | None" = None,
                strict: bool = False) -> "dict[str, Any]":
        """推进步骤；``strict=False`` 时乱序只记 warning（命令仍可跑）。"""
        if step not in STEPS:
            raise G1TraceError("STEP_UNKNOWN", step)
        doc = self.load()
        current = str(doc.get("step"))
        if step == current:
            return doc
        idx_now, idx_next = STEPS.index(current), STEPS.index(step)
        if idx_next != idx_now + 1:
            if strict:
                raise G1TraceError("STEP_ORDER", f"{current} → {step}")
            warnings = list(doc.get("warnings") or [])
            warnings.append({"step": step, "at_utc": now or utc_now_iso_z(),
                             "out_of_order_from": current})
            out = dict(doc, warnings=warnings)
            ensure_private_dir(self.root)
            atomic_write_text(self.path, json.dumps(out, ensure_ascii=False,
                                                    sort_keys=True, indent=1)
                              + "\n", mode=0o600)
            return out
        if step == "CLOSED" and any(s not in [h["step"] for h in doc["history"]]
                                    for s in STEPS[:-1]):
            if strict:
                raise G1TraceError("STEP_SKIP", "CLOSED 前有未完成步骤")
            warnings = list(doc.get("warnings") or [])
            warnings.append({"step": "CLOSED", "at_utc": now or utc_now_iso_z(),
                             "incomplete": True})
            doc = dict(doc, warnings=warnings)
        history = list(doc.get("history") or [])
        history.append({"step": step, "at_utc": now or utc_now_iso_z()})
        out = dict(doc, step=step, history=history)
        ensure_private_dir(self.root)
        atomic_write_text(self.path,
                          json.dumps(out, ensure_ascii=False, sort_keys=True,
                                     indent=1) + "\n", mode=0o600)
        return out


def verify_inputs_unchanged(manifest_path: Path) -> "dict[str, Any]":
    """事后复核：逐件重算 sha，与 inventory 时的清单比对（窗口内未变）。"""
    doc = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    roots = {r["root_id"]: Path(r["path"]) for r in doc.get("roots") or []}
    changed: "list[str]" = []
    missing: "list[str]" = []
    checked = 0
    for entry in doc.get("files") or []:
        if entry.get("state") != "observed":
            continue
        base = roots.get(entry["root_id"])
        if base is None:
            changed.append(f"{entry['root_id']}:UNKNOWN_ROOT")
            continue
        path = base / entry["relpath"]
        if not path.is_file() or path.is_symlink():
            missing.append(f"{entry['root_id']}:{entry['relpath']}")
            continue
        checked += 1
        if sha_bytes(path.read_bytes()) != entry.get("sha256"):
            changed.append(f"{entry['root_id']}:{entry['relpath']}")
    return {"ok": not changed and not missing, "checked": checked,
            "changed": changed, "missing": missing,
            "note": "只证明 inventory→verify 窗口内容未变；窗口之前无基线"}
