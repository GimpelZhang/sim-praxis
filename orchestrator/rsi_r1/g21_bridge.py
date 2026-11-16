"""RSI-G2.1 生产证据桥（Plan v1.0；零模型 POST、零新仿真）。

本模块把**已封存的真实结果**搬进一个新的隔离离线根，用**生产代码路径**
（``ResearchLoop.turn`` / ``EventStore`` / ``parse_action`` / ``CampaignDispatcher``
/ ``judge_v13``）走通 ``envelope → 最终 Request → read_result_artifact → 动作 →
submit → report/claim``，并在真实事件边界落原件。

纪律（Plan §0/§2/§3）：

* **源根只读**：所有封存件按字节复制进新根并逐件复核 sha；原件不被改写。
* **零 POST / 零仿真**：``run_confirm``/``ModelGateway``/物理 backend 全部
  ``FailIfCalled``；sender 只能是离线独立消费者（子进程）或脚本回放。
* **不补旧候选缺失产物**（§3.1）：T0 先由冻结 TaskSpec 派生必需对比全集；
  原始候选 bytes 缺项 ⇒ ``SOURCE_EVIDENCE_INCOMPLETE``，不以宿主 raw 回填。
* **事件等级**：新 turn 写 OBSERVED 原字节；无 HTTP body 记 MISSING；
  历史回放标 ``RECONSTRUCTED``，不把新运行回溯成轮 O。
* **五面分立**：raw reply / action / report doc / rendered / claim 各存各的 sha，
  互不替代。
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Mapping, Sequence

from orchestrator.common import research_transport as RT
from orchestrator.common.fsutil import atomic_write_text, ensure_private_dir
from orchestrator.rsi_r1 import assembly as A
from orchestrator.rsi_r1 import contracts as C
from orchestrator.rsi_r1 import g1_trace as GT
from orchestrator.rsi_r1 import g2_contract as G2
from orchestrator.rsi_r1 import loop as L
from orchestrator.rsi_r1 import profiles as P
from orchestrator.rsi_r1 import protocol as PT
from orchestrator.rsi_r1 import research_report as RR
from orchestrator.rsi_r1 import state as S
from orchestrator.rsi_r1 import task_specs as TS

MANIFEST_SCHEMA = "simpraxis.rsi_g21.replay_manifest.v1"
IMPORT_SCHEMA = "simpraxis.rsi_g21.replay_import.v1"
CLOSURE_SCHEMA = "simpraxis.rsi_g21.source_closure.v1"
SNAPSHOT_SCHEMA = "simpraxis.rsi_g21.contract_snapshot.v1"
CAPACITY_SCHEMA = "simpraxis.rsi_g21.capacity_matrix.v1"
TOKENIZER_SCHEMA = "simpraxis.rsi_g21.tokenizer_contract.v1"
ISOLATION_SCHEMA = "simpraxis.rsi_g21.isolation_report.v1"
CLOSEOUT_SCHEMA = "simpraxis.rsi_g21.closeout.v1"
FIVEFACE_SCHEMA = "simpraxis.rsi_g21.five_faces.v1"
RAW_SCHEMA = "simpraxis.rsi_g21.raw_check_meta.v1"

MODE_OFFLINE_BRIDGE = "offline_evidence_bridge"
CONSUMER_LABEL = "maintainer_scripted_consumer"
SOURCE_READY = "SOURCE_READY"
SOURCE_INCOMPLETE = "SOURCE_EVIDENCE_INCOMPLETE"

STUDIES = ("A", "B")

#: 允许从封存 journal 导入的事件名与 payload 键白名单（typed loader）。
IMPORT_EVENTS: "Mapping[str, frozenset[str]]" = {
    "root_transition": frozenset({"from", "to", "reason"}),
    "study_transition": frozenset({"study", "from", "to", "reason"}),
    "plan_revision": frozenset({"study", "doc", "parent_sha256",
                                "plan_sha256", "revision"}),
    "artifact": frozenset({"study", "kind", "source_sha256", "response_id",
                           "action_sha256", "producer", "original_producer",
                           "assisted"}),
    "reply": frozenset({"response_id", "text_sha256", "n_bytes", "phase",
                        "nonce", "relpath", "study"}),
    "action": frozenset({"response_id", "action_sha256", "name", "payload",
                         "study"}),
    "action_result": frozenset({"response_id", "result", "ok", "code",
                                "action", "study"}),
    "confirm_lock": frozenset({"study", "token", "analysis_source_sha256",
                               "analysis_reply_id", "analysis_action_id",
                               "analysis_receipt_sha256", "design_source_sha256",
                               "design_reply_id", "design_action_id",
                               "distribution_sha256", "metric_id", "primary_id",
                               "plan_sha256", "block_count", "checks"}),
    "confirm_intent": frozenset({"intent_id", "specs_sha256", "study", "token"}),
    "confirm_result": frozenset({"intent_id", "outcome", "result_sha256", "study"}),
    "confirm_publish": frozenset({"intent_id", "manifest_sha256", "study"}),
    # 另一 study 的终态事件可以随历史导入（它是当时之前的事实）；**本 study**
    # 的 claim/conclusion 必须落在 cutoff 之后，否则视为越过 RESULTS_RELEASED。
    "claim": frozenset({"claims", "producer", "report_artifact_id",
                        "report_bytes", "report_text_sha256", "response_id",
                        "study"}),
    "study_conclusion": frozenset({"reason", "study", "verdict"}),
}

#: 本 study 不得越过的终态事件（submit 之后的面）。
_POST_RELEASE_EVENTS = frozenset({"claim", "study_conclusion"})


class G21Error(RuntimeError):
    """G2.1 阻断（fail-closed）；``code`` 公开可机判。"""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"[{code}] {detail}")


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha_file(path: "str | Path") -> str:
    return sha_bytes(Path(path).read_bytes())


def read_json(path: "str | Path") -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json_private(path: "str | Path", obj: Any) -> str:
    target = Path(path)
    ensure_private_dir(target.parent)
    text = (json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=1,
                       allow_nan=False) + "\n")
    atomic_write_text(target, text, mode=0o600)
    return sha_bytes(text.encode("utf-8"))


def _rel_within(root: Path, rel: str) -> Path:
    if not isinstance(rel, str) or not rel or rel.startswith("/"):
        raise G21Error("PATH_INVALID", repr(rel))
    parts = Path(rel).parts
    if ".." in parts:
        raise G21Error("PATH_INVALID", f"越界路径 {rel!r}")
    target = root / rel
    resolved = target.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError:
        raise G21Error("PATH_INVALID", f"解析后越过根：{rel!r}") from None
    return resolved


class G21Context:
    """新 G2.1 根的写守卫上下文（与生产 ``CampaignContext`` 同协议面）。"""

    def __init__(self, root: "str | Path") -> None:
        self._root = Path(root).resolve(strict=False)

    @property
    def root(self) -> Path:
        return self._root

    def subdir(self, name: str) -> Path:
        path = _rel_within(self._root, name)
        ensure_private_dir(path)
        return path

    def relative(self, rel: str) -> Path:
        return _rel_within(self._root, rel)

    def assert_write_allowed(self, target: "str | Path") -> Path:
        resolved = Path(target).resolve(strict=False)
        try:
            resolved.relative_to(self._root)
        except ValueError:
            raise G21Error("WRITE_REFUSED",
                           f"写路径不在 G2.1 根内：{resolved}") from None
        return resolved

    def exists(self, rel: str) -> bool:
        return self.relative(rel).exists()

    def read_json(self, rel: str) -> Any:
        return read_json(self.relative(rel))

    def write_json(self, rel: str, obj: Any) -> Path:
        target = self.assert_write_allowed(self.relative(rel))
        write_json_private(target, obj)
        return target

    def write_text(self, rel: str, text: str) -> Path:
        target = self.assert_write_allowed(self.relative(rel))
        ensure_private_dir(target.parent)
        atomic_write_text(target, text, mode=0o600)
        return target


def _copy_verified(src: "str | Path", dst: "str | Path") -> dict[str, Any]:
    """逐字节复制并核对 sha；目标已存在且相同则跳过（幂等）。"""
    source = Path(src)
    target = Path(dst)
    data = source.read_bytes()
    digest = sha_bytes(data)
    ensure_private_dir(target.parent)
    if target.is_file():
        if sha_file(target) != digest:
            raise G21Error("COPY_DRIFT", f"目标已存在且不同：{target}")
        return {"src": str(source), "dst": str(target), "sha256": digest,
                "bytes": len(data), "copied": False}
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_bytes(data)
    os.chmod(tmp, 0o600)
    os.replace(tmp, target)
    if sha_file(target) != digest:
        raise G21Error("COPY_DRIFT", f"复制后 sha 不符：{target}")
    return {"src": str(source), "dst": str(target), "sha256": digest,
            "bytes": len(data), "copied": True}


# ---------------------------------------------------------------------------
# 输入清单（ReplayManifestV1）
# ---------------------------------------------------------------------------

_STUDY_KEYS = ("task", "plan", "source_design", "receipt", "output_raw",
               "output_pointer", "confirm_manifest", "observations_batch",
               "scope_contract")


def load_manifest(path: "str | Path") -> dict[str, Any]:
    return load_manifest_dict(read_json(path))


def load_manifest_dict(doc: Any) -> dict[str, Any]:
    if not isinstance(doc, Mapping):
        raise G21Error("MANIFEST_INVALID", "manifest 必须是对象")
    if doc.get("schema") != MANIFEST_SCHEMA:
        raise G21Error("MANIFEST_INVALID", f"schema={doc.get('schema')!r}")
    studies = doc.get("studies")
    if not isinstance(studies, Mapping):
        raise G21Error("MANIFEST_INVALID", "缺 studies")
    out: "dict[str, Any]" = {"schema": MANIFEST_SCHEMA, "schema_version": 1,
                             "source_campaign": str(doc.get("source_campaign")
                                                    or ""),
                             "source_commit": str(doc.get("source_commit") or ""),
                             "g2": dict(doc.get("g2") or {}),
                             "studies": {}}
    for study in STUDIES:
        entry = studies.get(study)
        if not isinstance(entry, Mapping):
            raise G21Error("MANIFEST_INVALID", f"缺 study {study}")
        item: "dict[str, Any]" = {}
        for key in _STUDY_KEYS:
            raw = entry.get(key)
            if not isinstance(raw, str) or not raw:
                raise G21Error("MANIFEST_INVALID", f"{study}.{key} 缺路径")
            path = Path(raw)
            if not path.is_absolute():
                raise G21Error("MANIFEST_INVALID", f"{study}.{key} 必须是绝对路径")
            item[key] = str(path)
        journal = entry.get("journal")
        if not isinstance(journal, str) or not journal:
            raise G21Error("MANIFEST_INVALID", f"{study}.journal 缺路径")
        item["journal"] = str(Path(journal))
        out["studies"][study] = item
    g2 = out["g2"]
    for key in ("root", "contract_snapshot"):
        if not isinstance(g2.get(key), str) or not g2.get(key):
            raise G21Error("MANIFEST_INVALID", f"g2.{key} 缺路径")
    return out


def _manifest_inputs(manifest: Mapping[str, Any]) -> "list[tuple[str, Path, str]]":
    """(role, path, study) 的封闭输入列表（不含 observations 目录内文件）。"""
    items: "list[tuple[str, Path, str]]" = [
        ("g2.contract_snapshot", Path(manifest["g2"]["contract_snapshot"]), "")]
    if manifest["g2"].get("root"):
        items.append(("g2.root", Path(manifest["g2"]["root"]), ""))
    for study in STUDIES:
        entry = manifest["studies"][study]
        for key in _STUDY_KEYS:
            items.append((f"{study}.{key}", Path(entry[key]), study))
        items.append((f"{study}.journal", Path(entry["journal"]), study))
    return items


def freeze_inputs(root: "str | Path", manifest: Mapping[str, Any]) -> dict[str, Any]:
    """G21-0：逐件登记输入与 sha；不写旧根。"""
    ctx = G21Context(root)
    entries: "list[dict[str, Any]]" = []
    for role, path, study in _manifest_inputs(manifest):
        if not path.exists():
            raise G21Error(SOURCE_INCOMPLETE, f"输入缺失：{role}={path}")
        if path.is_dir():
            files = sorted(p for p in path.rglob("*") if p.is_file())
            digest = hashlib.sha256()
            for item in files:
                digest.update(item.relative_to(path).as_posix().encode())
                digest.update(b"\0")
                digest.update(item.read_bytes())
            entries.append({"role": role, "study": study, "path": str(path),
                            "kind": "dir", "n_files": len(files),
                            "sha256": digest.hexdigest(),
                            "bytes": sum(p.stat().st_size for p in files)})
            continue
        entries.append({"role": role, "study": study, "path": str(path),
                        "kind": "file", "sha256": sha_file(path),
                        "bytes": path.stat().st_size})
    for study in STUDIES:
        batch = Path(manifest["studies"][study]["observations_batch"])
        if not batch.is_dir():
            raise G21Error(SOURCE_INCOMPLETE, f"观测批缺失：{batch}")
    closure = {"schema": CLOSURE_SCHEMA, "schema_version": 1,
               "source_campaign": manifest.get("source_campaign", ""),
               "source_commit": manifest.get("source_commit", ""),
               "entries": entries,
               "closure_sha256": G2.field_sha(entries)}
    write_json_private(ctx.relative("freeze/source_closure.json"), closure)
    freeze = {"schema": MANIFEST_SCHEMA, "schema_version": 1,
              "source_campaign": manifest.get("source_campaign", ""),
              "source_commit": manifest.get("source_commit", ""),
              "mode": MODE_OFFLINE_BRIDGE,
              "inputs": {role: {"path": str(path), "sha256": (
                  sha_file(path) if path.is_file() else "")}
                  for role, path, _ in _manifest_inputs(manifest)},
              "replay_manifest": dict(manifest),
              "created_at_utc": _utc_now()}
    write_json_private(ctx.relative("freeze/g21_manifest.json"), freeze)
    return {"ok": True, "entries": len(entries),
            "closure_sha256": closure["closure_sha256"]}


def verify_source_closure(root: "str | Path",
                          manifest: Mapping[str, Any]) -> dict[str, Any]:
    """复核冻结件与磁盘一致；任何漂移 ⇒ ``INPUT_DRIFT``。"""
    ctx = G21Context(root)
    frozen = ctx.read_json("freeze/source_closure.json")
    have = {item["role"]: item for item in frozen.get("entries") or []}
    problems: "list[str]" = []
    for role, path, study in _manifest_inputs(manifest):
        item = have.get(role)
        if item is None:
            problems.append(f"{role}: 未登记")
            continue
        if item.get("kind") == "dir":
            if not path.is_dir():
                problems.append(f"{role}: 目录消失")
                continue
            files = sorted(p for p in path.rglob("*") if p.is_file())
            digest = hashlib.sha256()
            for entry in files:
                digest.update(entry.relative_to(path).as_posix().encode())
                digest.update(b"\0")
                digest.update(entry.read_bytes())
            if digest.hexdigest() != item["sha256"]:
                problems.append(f"{role}: 目录内容漂移")
            continue
        if not path.is_file():
            problems.append(f"{role}: 文件消失")
        elif sha_file(path) != item.get("sha256"):
            problems.append(f"{role}: sha 漂移")
    if problems:
        raise G21Error("INPUT_DRIFT", "; ".join(problems[:8]))
    return {"ok": True, "closure_sha256": frozen["closure_sha256"],
            "n_entries": len(have)}


# ---------------------------------------------------------------------------
# 源就绪判定（§3.1：不得为闭合门补旧候选缺失产物）
# ---------------------------------------------------------------------------

def required_contrast_ids(task: TS.TaskSpecV13) -> "list[str]":
    """冻结 TaskSpec → 必需对比全集（主对比 + 阴性对照）。"""
    return G2.derive_required_ids(task)


def _output_contrast_ids(output_bytes: bytes) -> "set[str]":
    try:
        doc = G2.parse_json_strict(output_bytes)
    except G2.G2Error as exc:
        raise G21Error("CANDIDATE_OUTPUT_INVALID", exc.detail) from None
    raw = doc.get("contrasts") if isinstance(doc, Mapping) else None
    if isinstance(raw, list):
        return {str(item.get("contrast_id")) for item in raw
                if isinstance(item, Mapping)}
    if isinstance(raw, Mapping):
        return {str(key) for key in raw}
    return set()


def source_readiness(manifest_entry: Mapping[str, Any],
                     task: TS.TaskSpecV13) -> dict[str, Any]:
    """T0：原始候选 bytes 是否含 task 冻结的必需全集。"""
    output_bytes = Path(manifest_entry["output_raw"]).read_bytes()
    present = _output_contrast_ids(output_bytes)
    required = required_contrast_ids(task)
    missing = [cid for cid in required if cid not in present]
    return {"status": SOURCE_INCOMPLETE if missing else SOURCE_READY,
            "required_contrast_ids": required,
            "present_contrast_ids": sorted(present),
            "missing_contrast_ids": missing,
            "output_sha256": sha_bytes(output_bytes),
            "output_bytes": len(output_bytes),
            "policy": "原件缺项 ⇒ SOURCE_EVIDENCE_INCOMPLETE；不以宿主 raw 回填"}


# ---------------------------------------------------------------------------
# 状态导入（typed loader；截止 RESULTS_RELEASED）
# ---------------------------------------------------------------------------

def plan_import(journal_path: "str | Path", study: str) -> dict[str, Any]:
    """读取源 journal，验证 sha 链，返回截止到 ``study`` RESULTS_RELEASED 的事件。"""
    if study not in STUDIES:
        raise G21Error("STUDY_UNKNOWN", study)
    lines = Path(journal_path).read_text(encoding="utf-8").splitlines()
    events: "list[dict[str, Any]]" = []
    prev: "str | None" = None
    for index, line in enumerate(lines, 1):
        if not line.strip():
            continue
        doc = json.loads(line)
        if doc.get("schema") != S.EVENT_SCHEMA:
            raise G21Error("JOURNAL_TAMPERED", f"第 {index} 行 schema")
        if doc.get("seq") != len(events) + 1:
            raise G21Error("JOURNAL_TAMPERED", f"第 {index} 行 seq")
        if doc.get("prev_sha256") != prev:
            raise G21Error("JOURNAL_TAMPERED", f"第 {index} 行 prev")
        want = S._event_sha(int(doc["seq"]), str(doc["ts_iso"]),
                            str(doc["event"]), doc.get("payload") or {}, prev)
        if doc.get("sha256") != want:
            raise G21Error("JOURNAL_TAMPERED", f"第 {index} 行 sha")
        events.append(doc)
        prev = str(doc["sha256"])
    cutoff = None
    for index, doc in enumerate(events):
        payload = doc.get("payload") or {}
        if (doc.get("event") == "study_transition"
                and payload.get("study") == study
                and payload.get("to") == S.ST_RESULTS_RELEASED):
            cutoff = index
            break
    if cutoff is None:
        raise G21Error(SOURCE_INCOMPLETE,
                       f"源 journal 无 study {study} 的 RESULTS_RELEASED")
    imported = events[: cutoff + 1]
    typed: "list[dict[str, Any]]" = []
    for doc in imported:
        name = str(doc["event"])
        allowed = IMPORT_EVENTS.get(name)
        if allowed is None:
            raise G21Error("IMPORT_EVENT_FORBIDDEN", name)
        payload = doc.get("payload") or {}
        if name in _POST_RELEASE_EVENTS and payload.get("study") == study:
            raise G21Error("IMPORT_EVENT_FORBIDDEN",
                           f"本 study 的 {name} 落在 cutoff 之前")
        unknown = sorted(set(payload) - allowed)
        if unknown:
            raise G21Error("IMPORT_EVENT_FORBIDDEN",
                           f"{name} 含未登记键 {unknown}")
        typed.append({"event": name, "payload": dict(payload),
                      "source_seq": int(doc["seq"]),
                      "source_sha256": str(doc["sha256"]),
                      "source_ts_iso": str(doc["ts_iso"])})
    return {"study": study, "events": typed,
            "cutoff_source_seq": imported[-1]["seq"],
            "source_journal_sha256": sha_file(journal_path),
            "source_events_total": len(events),
            "imported_events": len(typed)}


def write_import(root: "str | Path", study: str,
                 plan: Mapping[str, Any]) -> dict[str, Any]:
    """把 typed 事件写进新根 journal（幂等：已导入则复核后跳过）。"""
    ctx = G21Context(root)
    journal = S.Journal(ctx)
    existing = journal.events()
    if existing:
        if len(existing) != len(plan["events"]):
            raise G21Error("IMPORT_CONFLICT",
                           f"新根 journal 已有 {len(existing)} 条事件")
        for new_ev, spec in zip(existing, plan["events"]):
            if new_ev.event != spec["event"] or new_ev.payload != spec["payload"]:
                raise G21Error("IMPORT_CONFLICT", f"seq={new_ev.seq} 内容不符")
        mapping = [{"source_seq": spec["source_seq"],
                    "source_sha256": spec["source_sha256"],
                    "event": spec["event"],
                    "new_seq": ev.seq, "new_sha256": ev.sha256}
                   for spec, ev in zip(plan["events"], existing)]
    else:
        mapping = []
        for spec in plan["events"]:
            ev = journal.append(spec["event"], spec["payload"])
            mapping.append({"source_seq": spec["source_seq"],
                            "source_sha256": spec["source_sha256"],
                            "event": spec["event"],
                            "new_seq": ev.seq, "new_sha256": ev.sha256})
    doc = {"schema": IMPORT_SCHEMA, "schema_version": 1,
           "mode": MODE_OFFLINE_BRIDGE,
           "study": study,
           "source_campaign": "",
           "source_journal_sha256": plan["source_journal_sha256"],
           "cutoff_source_seq": plan["cutoff_source_seq"],
           "source_events_total": plan["source_events_total"],
           "imported_events": len(mapping),
           "mapping": mapping,
           "created_at_utc": _utc_now()}
    write_json_private(ctx.relative(f"import/{study}/replay_import.json"), doc)
    return {"ok": True, "imported": len(mapping),
            "import_sha256": G2.field_sha(mapping)}


# ---------------------------------------------------------------------------
# 原件材料化（候选只读副本 + 私有凭据 + 计划/manifest/设计/观测）
# ---------------------------------------------------------------------------

def build_scopes(root: "str | Path",
                 manifest: Mapping[str, Any]) -> dict[str, Any]:
    """G21-0：从冻结任务 + 登记统计口径重建 G2.1 scope 合同并登记差异。

    锁内适配（2026-11-16 实测）：G2 封存 scope 的 ``interval_level=0.9`` 与
    候选原始 bytes 的实际区间口径不符——把封存 A 的 block 效应用登记 bootstrap
    重算，``[0.040860710191191175, 0.04087292938493192]`` 是 **0.95** 区间
    （0.9 区间为 ``[0.04086192806134932, 0.0408721873536706]``），与候选输出
    逐位相同；生产统计口径 ``rubric.CI_PRIMARY=0.95``。G2.1 因此按**证据**重建
    scope（level=0.95），并把与 G2 封存件的差异逐项写进
    ``contract/scope_provenance.json``（不静默改任一侧、不改旧件）。
    """
    from orchestrator.rsi_r1 import rubric as RUL
    ctx = G21Context(root)
    stats = stat_contract(root)
    provenance: "dict[str, Any]" = {"schema": "simpraxis.rsi_g21.scope_provenance.v1",
                                    "schema_version": 1,
                                    "basis": ("frozen TaskSpec bytes + "
                                              "rubric.CI_PRIMARY/statistics "
                                              "registered parameters"),
                                    "studies": {}}
    for study in STUDIES:
        entry = manifest["studies"][study]
        task_doc = read_json(entry["task"])
        primary = task_doc["primary"]
        metric = str(primary["metric"])
        task_sha = sha_bytes(G2.canonical(task_doc))
        doc = {
            "schema": G2.SCOPE_CONTRACT_SCHEMA, "schema_version": 1,
            "contract_id": f"g21-{study}-{task_doc['task_id']}",
            "task_id": str(task_doc["task_id"]),
            "task_spec_sha256": task_sha,
            "hypothesis_id": f"{task_doc['task_id']}:main",
            "estimand_id": str(primary["id"]), "metric_id": metric,
            "proposition": str(primary["proposition"]),
            "delta": float(primary["delta"]),
            "unit": str(RUL.METRICS[metric]["unit"]),
            "target_population": "registered_distribution",
            "aggregation": "mean", "weighting": "equal",
            "block_to_contrast_formula": "mean",
            "formula_notes": ("每配对 block 效应按冻结 TaskSpec 登记系数；"
                              "等权 mean=sum(x)/n"),
            "censoring_rule": "非有限/缺失 block 记 dropped，不补值",
            "coverage": "observed_blocks",
            "interval_level": float(stats["ci_level"]),
            "allowed_semantic_labels": list(G2.SEMANTIC_LABELS),
            "notes": ("RSI-G2.1 重建：绑定冻结任务文档规范 sha；"
                      "与 G2 封存 scope 的差异见 contract/scope_provenance.json"),
        }
        scope = G2.ScopeContractV1.from_dict(doc).validate()
        rel = f"contract/scope_{study}.json"
        G2.write_scope_contract(ctx.relative(rel), scope)
        sealed = read_json(entry["scope_contract"])
        divergence = {}
        for key in ("interval_level", "delta", "unit", "target_population",
                    "aggregation", "weighting", "coverage", "estimand_id",
                    "task_spec_sha256"):
            if sealed.get(key) != doc.get(key):
                divergence[key] = {"sealed": sealed.get(key),
                                   "g21": doc.get(key)}
        provenance["studies"][study] = {
            "path": rel, "scope_sha256": scope.sha256,
            "sealed_scope_path": str(entry["scope_contract"]),
            "sealed_scope_sha256": str(sealed.get("contract_sha256") or ""),
            "divergence": divergence}
    write_json_private(ctx.relative("contract/scope_provenance.json"),
                       provenance)
    return provenance


def materialize_study(root: "str | Path", manifest: Mapping[str, Any],
                      study: str) -> dict[str, Any]:
    """把该 study 的运行所需原件按字节复制进新根；返回路径表。"""
    ctx = G21Context(root)
    entry = manifest["studies"][study]
    plan_doc = read_json(entry["plan"])
    plan = C.ResearchPlan.from_dict(plan_doc)
    plan12 = plan.sha256[:12]
    source_sha = str(plan_doc.get("analysis_source_sha") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", source_sha):
        raise G21Error(SOURCE_INCOMPLETE, f"{study}: plan 缺真实 analysis_source_sha")
    task_doc = read_json(entry["task"])
    # 锁内适配（2026-11-16 G2.1 T0 实测）：G2 封存 scope 绑的是**冻结任务文档的
    # 规范 JSON sha**（保留文件里的 terms 顺序）。``TaskSpecV13.to_dict()`` 会把
    # primary.terms 按 id 归一化重排，其 ``sha256`` 与签名 scope 恒不相等
    # （A: 98ed73db… vs 944cc4c9…；B: 16ca1898… vs 08e4849b…）。绑定按冻结字节
    # 的规范 sha 判，并把两种算子的差异如实写进报告（不静默改任一侧）。
    task_sha = sha_bytes(G2.canonical(task_doc))
    model_sha = TS.TaskSpecV13.from_dict(task_doc).sha256
    scope_rel = f"contract/scope_{study}.json"
    if not ctx.exists(scope_rel):
        raise G21Error("SCOPE_MISSING", f"{scope_rel}（先 build_scopes）")
    scope_doc = ctx.read_json(scope_rel)
    if str(scope_doc.get("task_spec_sha256") or "") != task_sha:
        raise G21Error("SCOPE_TASK_MISMATCH",
                       f"{study}: scope.task_spec_sha256 != 冻结任务文档规范 sha")
    copies: "list[dict[str, Any]]" = []

    def copy(src: str, rel: str) -> Path:
        info = _copy_verified(src, ctx.relative(rel))
        copies.append({**info, "rel": rel})
        return Path(info["dst"])

    # 观测批（逐件复制；manifest.json 一并，供 O.verify_manifest 复核）
    batch_src = Path(entry["observations_batch"])
    batch_name = batch_src.name
    batch_dst = ctx.subdir(f"observations/{batch_name}")
    for item in sorted(batch_src.rglob("*")):
        if item.is_file():
            rel = f"observations/{batch_name}/{item.relative_to(batch_src).as_posix()}"
            copy(str(item), rel)
    # 设计 / 计划 / 确认 manifest / 任务 / scope
    if sha_file(entry["source_design"]) != source_sha:
        # 占位/伪造 source sha：design 文件字节必须真的等于登记 sha（AC G21-06）。
        raise G21Error("SOURCE_FILE_DRIFT",
                       f"{study}: designs 文件 sha != plan.analysis_source_sha")
    copy(entry["source_design"], f"designs/{source_sha}.py")
    copy(entry["plan"], f"confirm/plan_{study}.json")
    copy(entry["confirm_manifest"], f"confirm/manifest_{study}.json")
    copy(entry["task"], f"public/{study}/task.json")
    copy(entry["scope_contract"], f"public/{study}/scope_contract.json")
    # 私有凭据与候选 output 指针（原 pointer 的 raw 路径指向旧根，必须重写）
    receipt_bytes = Path(entry["receipt"]).read_bytes()
    output_bytes = Path(entry["output_raw"]).read_bytes()
    copy(entry["receipt"], f"private/receipts/{study}-confirm.json")
    raw_rel = f"private/analysis/{study}-confirm.raw.json"
    copy(entry["output_raw"], raw_rel)
    # 候选只读副本（Plan §3.0 第 1 行：registry 引用实际存在件）
    cand_out = f"analysis/{study}/{plan12}/output.json"
    cand_receipt = f"analysis/{study}/{plan12}/receipt.json"
    copy(entry["output_raw"], cand_out)
    copy(entry["receipt"], cand_receipt)
    pointer = {"schema": "simpraxis.rsi_r1.analysis_pointer.v1",
               "schema_version": 1, "study_id": study, "phase": "confirm",
               "relpath": cand_out, "raw_relpath": raw_rel,
               "sha256": sha_bytes(output_bytes),
               "raw_sha256": sha_bytes(output_bytes)}
    write_json_private(ctx.relative(f"private/analysis/{study}-confirm.json"),
                       pointer)
    # 历史回复原文（EventStore.history 按 relpath 读回）
    import_plan = plan_import(entry["journal"], study)
    replies: "list[str]" = []
    for spec in import_plan["events"]:
        if spec["event"] != "reply":
            continue
        rel = str(spec["payload"].get("relpath") or "")
        src = Path(entry["journal"]).parent.parent / rel
        if not src.is_file():
            raise G21Error(SOURCE_INCOMPLETE, f"回复原文缺失：{rel}")
        copy(str(src), rel)
        replies.append(rel)
    return {"plan": plan, "plan_doc": plan_doc, "task": TS.TaskSpecV13.from_dict(task_doc),
            "task_doc": task_doc, "source_sha": source_sha, "plan12": plan12,
            "task_spec_sha": {"frozen_doc_canonical": task_sha,
                              "model_to_dict": model_sha,
                              "agree": task_sha == model_sha},
            "scope_path": scope_rel,
            "candidate_output_rel": cand_out, "candidate_receipt_rel": cand_receipt,
            "output_bytes": output_bytes, "receipt_bytes": receipt_bytes,
            "observations_batch": batch_name, "replies": replies,
            "copies": copies, "import_plan": import_plan}


# ---------------------------------------------------------------------------
# envelope 投影（原件定位；AC G21-21）
# ---------------------------------------------------------------------------

_CONTRAST_FIELDS = ("estimate", "lower", "upper")


def build_envelope_from_original(*, study: str, plan_sha256: str,
                                 output_bytes: bytes, receipt_bytes: bytes,
                                 output_rel: str, receipt_rel: str,
                                 scope: G2.ScopeContractV1,
                                 task: TS.TaskSpecV13) -> dict[str, Any]:
    """从**原始候选字节**投影 envelope；pointer 按原对象下标生成并逐值复核。"""
    doc = G2.parse_json_strict(output_bytes)
    if not isinstance(doc, Mapping):
        raise G21Error("CANDIDATE_OUTPUT_INVALID", "output 必须是对象")
    raw_contrasts = doc.get("contrasts")
    if isinstance(raw_contrasts, Mapping):
        by_id = {str(k): dict(v) for k, v in raw_contrasts.items()
                 if isinstance(v, Mapping)}
        pointer_of = lambda cid: f"/contrasts/{cid}"  # noqa: E731
    elif isinstance(raw_contrasts, list):
        by_id = {str(v.get("contrast_id")): dict(v) for v in raw_contrasts
                 if isinstance(v, Mapping)}
        index_of = {str(v.get("contrast_id")): i
                    for i, v in enumerate(raw_contrasts)
                    if isinstance(v, Mapping)}
        pointer_of = lambda cid: f"/contrasts/{index_of[cid]}"  # noqa: E731
    else:
        raise G21Error("CANDIDATE_OUTPUT_INVALID", "output.contrasts 形状不符")
    required = required_contrast_ids(task)
    missing = [cid for cid in required if cid not in by_id]
    if missing:
        raise G21Error(SOURCE_INCOMPLETE,
                       f"候选原始 bytes 缺必需对比 {missing}")
    records: "list[dict[str, Any]]" = []
    evidence: "list[dict[str, Any]]" = []
    denominator = 0
    for cid in required:
        src = by_id[cid]
        record = {"contrast_id": cid, "metric_id": src.get("metric_id"),
                  "proposition": src.get("proposition"),
                  "delta": scope.delta, "estimate": src.get("estimate"),
                  "lower": src.get("lower"), "upper": src.get("upper"),
                  "level": scope.interval_level,
                  "science_status": _science_status(src.get("status")),
                  "n_blocks": src.get("n_blocks"),
                  "dropped": src.get("dropped") or {"count": 0, "ids": [],
                                                    "ids_sha256": ""}}
        records.append(record)
        denominator += int(src.get("n_blocks") or 0)
        for key in _CONTRAST_FIELDS:
            pointer = f"{pointer_of(cid)}/{key}"
            # AC G21-21：pointer 必须解析回**同一原对象**的同一值。
            resolved = G2.resolve_pointer(doc, pointer)
            if G2.field_sha(resolved) != G2.field_sha(src.get(key)):
                raise G21Error("POINTER_MISMATCH", pointer)
            evidence.append({"artifact_id": "analysis/output.json",
                             "json_pointer": pointer,
                             "field_sha256": G2.field_sha(resolved)})
    artifacts = [
        {"artifact_id": "analysis/output.json", "relative_path": output_rel,
         "sha256": sha_bytes(output_bytes), "bytes": len(output_bytes),
         "access_mode": "read_result_artifact"},
        {"artifact_id": "analysis/receipt.json", "relative_path": receipt_rel,
         "sha256": sha_bytes(receipt_bytes), "bytes": len(receipt_bytes),
         "access_mode": "read_result_artifact"},
    ]
    diagnostics = doc.get("diagnostics")
    diag_summary = ({"status": "OBSERVED", "contrast_count": len(records),
                     "coverage": (diagnostics or {}).get("coverage")}
                    if isinstance(diagnostics, Mapping)
                    else {"status": "MISSING"})
    envelope = G2.build_envelope(
        study_id=study, phase="confirm", plan_sha256=plan_sha256,
        analysis_receipt_sha256=sha_bytes(receipt_bytes),
        analysis_output_sha256=sha_bytes(output_bytes),
        scope_contract=scope, required_contrast_ids=required,
        contrasts=records, primary_contrast_id=task.primary.id,
        denominator=denominator,
        dropped={"count": 0, "ids": [], "ids_sha256": ""},
        diagnostics=diag_summary, artifacts=artifacts, evidence=evidence,
        summary={"n_required": len(records), "origin_bytes": "sealed_candidate",
                 "pointer_basis": "original_bytes"})
    G2.verify_envelope(envelope, scope_contract=scope)
    return envelope


def _science_status(label: Any) -> str:
    return {"SUPPORTED": "SUPPORTED", "REFUTED": "REFUTED",
            "INCONCLUSIVE": "INCONCLUSIVE"}.get(str(label), "NOT_MEASURED")


def write_artifact_registry(ctx: G21Context, study: str, plan_sha256: str,
                            envelope: Mapping[str, Any]) -> dict[str, Any]:
    doc = {"study": study, "plan_sha256": plan_sha256,
           "envelope_sha256": envelope["envelope_sha256"],
           "artifacts": list(envelope["artifacts"])}
    write_json_private(ctx.relative("state/g2_artifacts.json"), doc)
    return doc


# ---------------------------------------------------------------------------
# 捕获（复用 G1 TraceEvent/TraceWriter；原件落 trace/<study>/）
# ---------------------------------------------------------------------------

class G21Capture(G2.G2CaptureRecorder):
    """G2.1 捕获：与 G2 同事件 socket，但原件落 ``trace/<study>/artifacts``。"""

    def __init__(self, root: "str | Path", *, study: str, diagnostic_id: str,
                 scope_contract_sha: str, source_commit: str) -> None:
        # 直接构造 TraceWriter 而不用父类 ``sub=``：G2.1 要求事件链落
        # ``trace/<study>/events.jsonl``（与原件 ``trace/<study>/artifacts`` 同根）。
        self.root = Path(root)
        self.study = study
        self.policy_id = G2.POLICY_G2_ATOMIC
        self.scope_contract_sha = scope_contract_sha
        self.writer = GT.TraceWriter(self.root / "trace" / study,
                                     diagnostic_id=diagnostic_id,
                                     source_commit=source_commit)

    def _add(self, kind: str, data: bytes, *, phase: str,
             visibility: str = "observed", **fields: Any) -> str:
        extra = dict(fields)
        request_sha = extra.pop("request_sha256", None)
        if visibility != "observed":
            self.writer.add(GT.TraceEvent(
                diagnostic_id=self.writer.diagnostic_id, seq=0,
                event_kind=kind, visibility="missing", study_id=self.study,
                phase=phase, artifact_ref=None, bytes_sha256=None, n_bytes=None,
                source_commit=self.writer.source_commit,
                request_sha256=request_sha,
                missing_reason=extra.pop("missing_reason", "NOT_CAPTURED"),
                extra=self._extra(**extra)))
            return ""
        digest = sha_bytes(data)
        rel = f"trace/{self.study}/artifacts/{digest}.bin"
        target = Path(self.root) / rel
        ensure_private_dir(target.parent)
        if not target.is_file() or sha_file(target) != digest:
            target.write_bytes(data)
            os.chmod(target, 0o600)
        self.writer.add(GT.TraceEvent(
            diagnostic_id=self.writer.diagnostic_id, seq=0,
            event_kind=kind, visibility="observed", study_id=self.study,
            phase=phase, artifact_ref=rel, bytes_sha256=digest,
            n_bytes=len(data), source_commit=self.writer.source_commit,
            request_sha256=request_sha, extra=self._extra(**extra)))
        return rel

    def record_claim(self, claim_doc: Mapping[str, Any], *, phase: str,
                     response_id: str = "") -> str:
        return self._add("claim_persisted", G2.canonical(dict(claim_doc)),
                         phase=phase, response_id=response_id)


# ---------------------------------------------------------------------------
# 离线准入/预算（零 POST；真实 loop 只要求同协议面）
# ---------------------------------------------------------------------------

class OfflineAdmission:
    """离线准入：不认识付费门，只记录本任务回合数（POST 恒 0）。"""

    def __init__(self, *, study: str, envelope_id: str) -> None:
        self.study = study
        self.lane = f"study_{study}"
        self.envelope_id = envelope_id
        self.turns = 0

    def check_all(self, **kwargs: Any) -> Any:
        self.turns += 1
        return SimpleNamespace(envelope_id=self.envelope_id,
                               post_index=self.turns)

    def posts_used(self, lane: str) -> int:
        return 0


class OfflineBudget:
    """离线预算：每次 sender 调用记一个**离线回合**，永不产生 POST 行。"""

    def __init__(self) -> None:
        self.posts_by_lane = {"study_A": 1_000_000, "study_B": 1_000_000}
        self.offline_turns = 0
        self.paused = False

    def reserve_atomic(self, *, call_id: str, permit: Any) -> Any:
        return SimpleNamespace(call_id=call_id, offline=True)

    def resolve_or_pause(self, reply: Any) -> None:
        self.offline_turns += 1


def _fail_backend_factory(name: str) -> Callable[[str], Any]:
    def build(batch_id: str) -> Any:
        raise G21Error("PAID_GATE", f"物理 backend 在 G2.1 零仿真模式被调用：{name}")
    return build


def _utc_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# 独立消费者 transport（子进程 python -I；只给公开 Request）
# ---------------------------------------------------------------------------

class ConsumerTransport:
    """把最终 Request 的公开投影喂给独立消费者进程，收单条动作 JSON。

    ``OFFLINE = True`` 是准入标记：``assert_offline_sender`` 只认该标记。
    """

    OFFLINE = True

    def __init__(self, consumer_path: "str | Path", *, python: str,
                 work_dir: "str | Path", turn_log: "str | Path",
                 canaries: "Sequence[str]" = (), timeout_s: int = 120,
                 study: str = "") -> None:
        self.consumer_path = str(consumer_path)
        self.python = str(python)
        self.work_dir = Path(work_dir)
        self.turn_log = Path(turn_log)
        self.canaries = [str(c) for c in canaries if c]
        self.timeout_s = int(timeout_s)
        self.study = study
        self.turns = 0
        self.requests: "list[RT.Request]" = []

    def send_once(self, request: RT.Request,
                  reservation: Any) -> L.Reply:
        from orchestrator.rsi_r1 import loop as LOOP
        self.turns += 1
        payload = json.dumps({"system": request.system,
                              "messages": list(request.messages),
                              "max_tokens": int(request.max_tokens)},
                             ensure_ascii=False, sort_keys=True)
        cwd = tempfile.mkdtemp(prefix=f"g21c-{self.study or 'x'}-",
                               dir=str(self.work_dir))
        os.chmod(cwd, 0o700)
        env = {"PATH": "/usr/bin:/bin"}
        started = time.time()
        proc = subprocess.Popen(
            [self.python, "-I", self.consumer_path],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, cwd=cwd, env=env)
        try:
            out, err = proc.communicate(payload.encode("utf-8"),
                                        timeout=self.timeout_s)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = proc.communicate()
            self._append({"n": self.turns, "study": self.study,
                          "error": "CONSUMER_TIMEOUT",
                          "request_sha256": request.sha256})
            raise G21Error("CONSUMER_FAILED", "消费者超时") from None
        elapsed_ms = int((time.time() - started) * 1000)
        text = out.decode("utf-8", errors="strict")
        self.requests.append(request)
        entry = {"n": self.turns, "study": self.study,
                 "request_sha256": request.sha256,
                 "request_bytes": request.n_bytes,
                 "request_local_tokens": request.local_tokens,
                 "reply_sha256": sha_bytes(out),
                 "reply_bytes": len(out),
                 "stderr_sha256": sha_bytes(err),
                 "stderr_tail": err.decode("utf-8", "replace")[-400:],
                 "stdout_tail": out.decode("utf-8", "replace")[-400:],
                 "pid": proc.pid, "rc": proc.returncode, "cwd": cwd,
                 "env_keys": sorted(env), "elapsed_ms": elapsed_ms}
        for canary in self.canaries:
            if canary in text:
                entry["canary_hit"] = True
                self._append(entry)
                raise G21Error("CONSUMER_CANARY", "回复含禁止的宿主私有值")
        if proc.returncode != 0:
            detail = ""
            try:
                detail = str(json.loads(text).get("error") or "")
            except (json.JSONDecodeError, AttributeError):
                detail = ""
            entry["error"] = f"CONSUMER_RC:{detail or 'UNPARSED'}"
            self._append(entry)
            raise G21Error("CONSUMER_FAILED",
                           f"rc={proc.returncode} detail={detail} "
                           f"stderr={entry['stderr_tail']!r}")
        try:
            doc = json.loads(text)
        except json.JSONDecodeError:
            entry["error"] = "CONSUMER_BAD_JSON"
            self._append(entry)
            raise G21Error("CONSUMER_FAILED", "stdout 不是单条 JSON") from None
        if not isinstance(doc, dict) or not str(doc.get("action") or ""):
            entry["error"] = "CONSUMER_NO_ACTION"
            self._append(entry)
            raise G21Error("CONSUMER_FAILED", "回复缺 action")
        entry["action"] = str(doc["action"])
        self._append(entry)
        return LOOP.Reply(text=text,
                          response_id=f"g21-{self.study or 'x'}-{self.turns:03d}-"
                                      f"{sha_bytes(out)[:12]}",
                          is_actionable=True)

    def _append(self, entry: Mapping[str, Any]) -> None:
        ensure_private_dir(self.turn_log.parent)
        with self.turn_log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(dict(entry), ensure_ascii=False,
                                    sort_keys=True) + "\n")


# ---------------------------------------------------------------------------
# 离线装配（真实 loop/state/dispatcher；不碰 run_confirm/physics/gateway）
# ---------------------------------------------------------------------------

@dataclass
class OfflineParts:
    study: str
    state: Any
    store: Any
    dispatcher: Any
    admission: OfflineAdmission
    budget: OfflineBudget
    loop: Any
    envelope: dict[str, Any]
    capture: Any
    counter: Callable[[str], int]


def tokenizer_counter(tokenizer: Any) -> Callable[[str], int]:
    def count(text: str) -> int:
        return len(tokenizer.encode(str(text)).ids)
    return count


def build_offline_parts(*, root: "str | Path", study: str,
                        task: TS.TaskSpecV13, scope: G2.ScopeContractV1,
                        envelope: Mapping[str, Any], sender: Any,
                        capture: G21Capture,
                        counter: Callable[[str], int]) -> OfflineParts:
    """用生产装配件构造只读证据桥链（G2 policy + 离线 sender）。"""
    ctx = G21Context(root)
    state = S.ResearchState(ctx)
    store = L.EventStore(state, study=study)
    admission = OfflineAdmission(study=study, envelope_id=f"g21-{study}")
    budget = OfflineBudget()
    dispatcher = A.CampaignDispatcher(
        ctx=ctx, state=state, admission=admission, budget=budget,
        study=study, runner=G2.FailIfCalled("sandbox_runner"), image="",
        backend_factory=_fail_backend_factory("physics"),
        profile=P.PROFILE_R13_DEF, task=task,
        delivery_policy=RT.POLICY_G2_ATOMIC, scope_contract=scope,
        require_task_contrasts=True)
    system = L.render_system(extra_rules=(
        "### G2.1 证据桥（只读回放，零 POST）\n"
        "study 已在 RESULTS_RELEASED：确认批结果由封存原件重建，"
        "**不重跑** run_confirm / 物理 / 网关。\n"
        "结果视图（envelope）在独立消息中逐字给出；可用 "
        "read_result_artifact(artifact_id, output_sha256, cursor, page_bytes) "
        "分页读取 analysis/ 下的候选产物，读完再 submit_claim。\n\n"
        + PT.render_study_brief(study, profile=P.PROFILE_R13_DEF, task=task)))

    def messages() -> RT.Request:
        return RT.build_request(
            system=system,
            task_brief=L.render_task_brief(dispatcher.visible_state()),
            turns=store.history(), counter=counter,
            max_tokens=int(getattr(P.PROFILE_R13_DEF, "output_tokens",
                                   RT.MAX_OUTPUT_TOKENS)),
            delivery_policy=RT.POLICY_G2_ATOMIC,
            result_context=dict(envelope))

    loop = L.ResearchLoop(
        sender=sender, parser=L.parse_action, dispatcher=dispatcher, store=store,
        messages=messages, counter=counter, admission=admission, budget=budget,
        envelope=SimpleNamespace(envelope_id=f"g21-{study}"),
        root_lock=None, spend_contract=None, capture=capture)
    return OfflineParts(study=study, state=state, store=store,
                        dispatcher=dispatcher, admission=admission,
                        budget=budget, loop=loop, envelope=dict(envelope),
                        capture=capture, counter=counter)


def run_turns(parts: OfflineParts, *, max_turns: int = 12) -> dict[str, Any]:
    """驱动真实 turn 到 study 终态或回合上限；记录每步公开结果。"""
    outcome_rows: "list[dict[str, Any]]" = []
    stopped = ""
    for index in range(1, max_turns + 1):
        state_now = parts.state.study_state(parts.study)
        if state_now in S.STUDY_TERMINAL:
            stopped = "STUDY_TERMINAL"
            break
        try:
            outcome = parts.loop.turn()
        except L.LoopError as exc:
            parts.store.stop_or_block(code=exc.code, detail=exc.detail)
            outcome_rows.append({"n": index, "ok": False, "code": exc.code,
                                 "detail": exc.detail, "raised": True,
                                 "terminal": True})
            stopped = exc.code
            break
        row = outcome.as_dict()
        row["n"] = index
        outcome_rows.append(row)
        if outcome.terminal:
            stopped = outcome.code or "TERMINAL"
            break
        if not outcome.ok and outcome.code == "CLAIMS_INVALID":
            stopped = "CLAIMS_INVALID"
            break
    state_after = parts.state.study_state(parts.study)
    return {"study": parts.study, "turns": outcome_rows,
            "stopped": stopped or "MAX_TURNS", "study_state": state_after,
            "offline_turns": parts.budget.offline_turns,
            "model_posts": 0}


# ---------------------------------------------------------------------------
# 五面来源绑定（reply/action/report/rendered/claim）
# ---------------------------------------------------------------------------

def five_faces(ctx: G21Context, study: str) -> dict[str, Any]:
    """从落盘事件独立重算五面 sha 并校验互不混用。"""
    journal = S.Journal(ctx)
    events = journal.events()
    submit_action = None
    for ev in reversed(events):
        if (ev.event == "action" and ev.payload.get("name") == "submit_claim"
                and ev.payload.get("study") in (None, study)):
            submit_action = ev
            break
    if submit_action is None:
        raise G21Error("SUBMIT_MISSING", f"study {study} 没有 submit_claim 动作事件")
    response_id = str(submit_action.payload.get("response_id"))
    action_doc = dict(submit_action.payload.get("payload") or {})
    action_sha = G2.field_sha(action_doc)
    reply_ev = None
    for ev in events:
        if ev.event == "reply" and ev.payload.get("response_id") == response_id:
            reply_ev = ev
    if reply_ev is None:
        raise G21Error("REPLY_MISSING", response_id)
    reply_rel = str(reply_ev.payload.get("relpath") or "")
    reply_bytes = ctx.relative(reply_rel).read_bytes() if reply_rel else b""
    claim_ev = None
    for ev in events:
        if ev.event == "claim" and ev.payload.get("study") == study:
            claim_ev = ev
    if claim_ev is None:
        raise G21Error("CLAIM_MISSING", study)
    claims = list(claim_ev.payload.get("claims") or [])
    claim_sha = G2.field_sha(claims)
    report_doc = action_doc.get("research_report")
    report_sha = G2.field_sha(report_doc) if report_doc is not None else ""
    rendered = RR.render_report(report_doc) if report_doc is not None else ""
    rendered_sha = sha_bytes(rendered.encode("utf-8"))
    report_text = str(action_doc.get("report_text") or "")
    faces = {
        "raw_reply": {"sha256": sha_bytes(reply_bytes),
                      "event_sha256": str(reply_ev.payload.get("text_sha256")),
                      "relpath": reply_rel, "bytes": len(reply_bytes)},
        "action": {"sha256": action_sha,
                   "event_sha256": str(submit_action.payload.get("action_sha256")),
                   "name": "submit_claim",
                   "event_seq": submit_action.seq},
        "report_doc": {"sha256": report_sha,
                       "keys": sorted(report_doc) if isinstance(report_doc, Mapping) else []},
        "rendered": {"sha256": rendered_sha, "bytes": len(rendered.encode("utf-8"))},
        "claim": {"sha256": claim_sha, "n_claims": len(claims),
                  "event_seq": claim_ev.seq,
                  "report_text_sha256": str(claim_ev.payload.get(
                      "report_text_sha256"))},
    }
    checks = {
        "reply_binding": faces["raw_reply"]["sha256"]
        == faces["raw_reply"]["event_sha256"],
        "action_binding": faces["action"]["sha256"]
        == faces["action"]["event_sha256"],
        "render_consistent": report_text == rendered,
        "report_text_sha": sha_bytes(report_text.encode("utf-8"))
        == faces["claim"]["report_text_sha256"],
        "origin_present": isinstance(claim_ev.payload.get("origin"), Mapping),
    }
    origin = dict(claim_ev.payload.get("origin") or {})
    for key, face in (("raw_reply_sha256", "raw_reply"),
                      ("action_sha256", "action"),
                      ("report_doc_sha256", "report_doc"),
                      ("rendered_sha256", "rendered")):
        checks[f"origin_{key}"] = (str(origin.get(key) or "")
                                   == faces[face]["sha256"])
    distinct = [faces[name]["sha256"] for name in faces]
    checks["all_distinct"] = len(set(distinct)) == len(distinct)
    doc = {"schema": FIVEFACE_SCHEMA, "schema_version": 1, "study": study,
           "faces": faces, "origin": origin, "checks": checks,
           "ok": all(checks.values()),
           "response_id": response_id,
           "created_at_utc": _utc_now()}
    write_json_private(ctx.relative(f"submit/{study}/five_faces.json"), doc)
    if rendered:
        ctx.write_text(f"submit/{study}/rendered.txt", rendered)
    ctx.write_json(f"submit/{study}/claim.json",
                   {"claims": claims, "claim_sha256": claim_sha})
    if report_doc is not None:
        ctx.write_json(f"submit/{study}/report.json", report_doc)
    return doc


# ---------------------------------------------------------------------------
# 研究链路总入口
# ---------------------------------------------------------------------------

def run_study(root: "str | Path", manifest: Mapping[str, Any], study: str, *,
              consumer_path: "str | Path | None" = None,
              python: "str | None" = None, max_turns: int = 12,
              tokenizer: Any = None) -> dict[str, Any]:
    """bridge-run 的实体：导入状态 → 材料化 → 真实 turn（读+提交）。"""
    if study not in STUDIES:
        raise G21Error("STUDY_UNKNOWN", study)
    ctx = G21Context(root)
    entry = manifest["studies"][study]
    if not (Path(root) / "freeze" / "source_closure.json").is_file():
        raise G21Error("FREEZE_MISSING", "先 bridge-init")
    verify_source_closure(root, manifest)
    task_doc = read_json(entry["task"])
    task = TS.TaskSpecV13.from_dict(task_doc)
    readiness = source_readiness(entry, task)
    result: "dict[str, Any]" = {"study": study, "source": readiness}
    if readiness["status"] != SOURCE_READY:
        # §3.1：真实源缺必需对比 ⇒ 阻止真实 O 链，不补写
        result.update({"ok": False, "status": SOURCE_INCOMPLETE,
                       "blocked_before_build": True})
        write_json_private(ctx.relative(f"reports/{study}-source.json"), result)
        # closeout 的 source/submit 轴按 <study>-bridge.json 读：阻断路径也必须留面。
        write_json_private(ctx.relative(f"reports/{study}-bridge.json"), result)
        return result
    material = materialize_study(root, manifest, study)
    scope = G2.load_scope_contract(Path(root) / material["scope_path"])
    plan = material["plan"]
    plan_doc = material["plan_doc"]
    import_plan = material["import_plan"]
    write_import(root, study, import_plan)
    envelope = build_envelope_from_original(
        study=study, plan_sha256=plan.sha256,
        output_bytes=material["output_bytes"],
        receipt_bytes=material["receipt_bytes"],
        output_rel=material["candidate_output_rel"],
        receipt_rel=material["candidate_receipt_rel"],
        scope=scope, task=material["task"])
    write_artifact_registry(ctx, study, plan.sha256, envelope)
    write_json_private(ctx.relative(f"public/{study}/envelope.json"), envelope)
    diagnostic_id = f"g21-{study}"
    capture = G21Capture(root, study=study, diagnostic_id=diagnostic_id,
                         scope_contract_sha=scope.sha256,
                         source_commit=str(manifest.get("source_commit") or ""))
    capture.record_envelope(envelope, phase="confirm")
    capture.record_gateway_missing(phase="confirm",
                                   reason="NO_HTTP_BODY_CAPTURED_OFFLINE_BRIDGE")
    counter = tokenizer_counter(tokenizer) if tokenizer is not None else (
        lambda text: max(1, len(str(text)) // 4))
    if consumer_path is None:
        raise G21Error("CONSUMER_MISSING", "需要独立消费者脚本")
    sender = ConsumerTransport(
        consumer_path, python=python or sys.executable,
        work_dir=ctx.subdir("logs/consumer"),
        turn_log=ctx.relative(f"logs/{study}-turns.jsonl"),
        canaries=_canaries(entry), study=study)
    G2.assert_offline_sender(sender)
    parts = build_offline_parts(root=root, study=study, task=material["task"],
                                scope=scope, envelope=envelope, sender=sender,
                                capture=capture, counter=counter)
    outcome = run_turns(parts, max_turns=max_turns)
    claim_events = [ev for ev in parts.state.journal.events()
                    if ev.event == "claim"
                    and ev.payload.get("study") == study]
    if claim_events:
        capture.record_claim(dict(claim_events[-1].payload), phase="claim",
                             response_id=str(claim_events[-1].payload.get(
                                 "response_id") or ""))
    trace = capture.write()
    claim = {}
    try:
        claim = five_faces(ctx, study)
    except G21Error as exc:
        claim = {"ok": False, "code": exc.code, "detail": exc.detail}
    result.update({"ok": bool(claim.get("ok")) and
                   outcome["study_state"] in (S.ST_VERIFIED, S.ST_VALID_FAIL),
                   "plan_sha256": plan.sha256,
                   "task_spec_sha": material["task_spec_sha"],
                   "envelope_sha256": envelope["envelope_sha256"],
                   "envelope_bytes": len(G2.canonical(envelope)),
                   "import": {"imported": len(import_plan["events"]),
                              "cutoff_source_seq": import_plan["cutoff_source_seq"]},
                   "turns": outcome, "trace": trace, "five_faces": claim,
                   "study_state": outcome["study_state"]})
    write_json_private(ctx.relative(f"reports/{study}-bridge.json"), result)
    return result


def _canaries(entry: Mapping[str, Any]) -> "list[str]":
    """消费者回复不得出现的宿主私有值（run_id 与 raw 路径片段）。"""
    out: "list[str]" = []
    batch = Path(entry["observations_batch"])
    names = sorted(p.name for p in batch.glob("run-*.jsonl"))[:2]
    names += sorted(p.name for p in batch.glob("run-*.jsonl"))[-1:]
    out.extend(name.split(".")[0] for name in names)
    out.append(str(Path(entry["output_raw"]).parent))
    return [item for item in out if item]


# ---------------------------------------------------------------------------
# 合同快照 / 实现闭包 / 工程签新鲜度（G21-0, G21-5）
# ---------------------------------------------------------------------------

_IMPLEMENTATION_FILES = (
    "orchestrator/rsi_r1/assembly.py",
    "orchestrator/rsi_r1/loop.py",
    "orchestrator/rsi_r1/state.py",
    "orchestrator/rsi_r1/judge.py",
    "orchestrator/rsi_r1/judge_v13.py",
    "orchestrator/rsi_r1/research_report.py",
    "orchestrator/rsi_r1/g2_contract.py",
    "orchestrator/rsi_r1/g2_validation.py",
    "orchestrator/rsi_r1/g21_bridge.py",
    "orchestrator/rsi_r1/g21_raw_checker.py",
    "orchestrator/rsi_r1/sandbox.py",
    "orchestrator/rsi_r1/task_specs.py",
    "orchestrator/rsi_r1/contracts.py",
    "orchestrator/rsi_r1/experiments.py",
    "orchestrator/rsi_r1/metrics.py",
    "orchestrator/rsi_r1/contrasts.py",
    "orchestrator/rsi_r1/statistics.py",
    "orchestrator/rsi_r1/diagnostics.py",
    "orchestrator/rsi_r1/analysis_receipt.py",
    "orchestrator/common/research_transport.py",
    "orchestrator/common/runtime_paths.py",
    "orchestrator/rsi_r1/review_capacity.py",
    "orchestrator/rsi_r1/profiles.py",
    "tests/fixtures/g21_consumer/consumer.py",
    "scripts/verification/check_rsi_g21_closure.py",
)


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def implementation_closure() -> dict[str, Any]:
    return G2.implementation_closure(
        [repo_root() / rel for rel in _IMPLEMENTATION_FILES])


def snapshot_files(root: "str | Path") -> dict[str, Any]:
    """G21-0 合同快照：schema/实现/tokenizer/限额/必需对比。"""
    ctx = G21Context(root)
    frozen = ctx.read_json("freeze/source_closure.json")
    from orchestrator.rsi_r1 import review_capacity as RC
    try:
        tokenizer_version = importlib.metadata.version("tokenizers")
    except importlib.metadata.PackageNotFoundError:
        tokenizer_version = "UNKNOWN"
    snapshot = {
        "schema": SNAPSHOT_SCHEMA, "schema_version": 1,
        "policy_id": RT.POLICY_G2_ATOMIC,
        "scope_contract_sha256": "",
        "input_closure_sha256": frozen["closure_sha256"],
        "implementation_closure": implementation_closure(),
        "limits": {"envelope_max_bytes": G2.ENVELOPE_MAX_BYTES,
                   "page_max_bytes": G2.PAGE_MAX_BYTES,
                   "artifact_max_bytes": G2.ARTIFACT_MAX_BYTES},
        # Plan §3.3：tokenizer 身份/版本/冻结 sha 进合同快照（不在运行期再猜）。
        "tokenizer": {"id": str(Path(RC.DEFAULT_TOKENIZER_PATH).name),
                      "path": RC.DEFAULT_TOKENIZER_PATH,
                      "sha256": RC.DEFAULT_TOKENIZER_SHA256,
                      "library": "tokenizers",
                      "library_version": tokenizer_version,
                      "input_token_cap": int(P.REVIEW_INPUT_TOKENS_R13),
                      "framing": "json.dumps({system,messages,max_tokens}, sort_keys=True)"},
        "fixtures": {"capacity_matrix_sha256": (
            sha_file(ctx.relative("freeze/capacity_matrix.json"))
            if ctx.exists("freeze/capacity_matrix.json") else "")},
        "created_at_utc": _utc_now()}
    snapshot["snapshot_sha256"] = G2.content_sha(snapshot,
                                                exclude=("snapshot_sha256",))
    write_json_private(ctx.relative("contract/g21_contract.json"), snapshot)
    return snapshot


def verify_signature_freshness(root: "str | Path",
                               manifest: "Mapping[str, Any] | None" = None
                               ) -> dict[str, Any]:
    """比对快照登记的实现闭包与磁盘现状；漂移 ⇒ ``STALE_SIGNATURE``。"""
    ctx = G21Context(root)
    path = ctx.relative("contract/g21_contract.json")
    if not path.is_file():
        raise G21Error("SIGNATURE_MISSING", "缺 contract/g21_contract.json")
    doc = ctx.read_json("contract/g21_contract.json")
    want = G2.content_sha(doc, exclude=("snapshot_sha256",))
    if doc.get("snapshot_sha256") != want:
        raise G21Error("SIGNATURE_INVALID", "snapshot_sha256 不符")
    current = implementation_closure()
    problems: "list[str]" = []
    old = {item["path"]: item["sha256"]
           for item in (doc.get("implementation_closure") or {}).get("files") or []}
    new = {item["path"]: item["sha256"] for item in current["files"]}
    for rel in sorted(set(old) | set(new)):
        if old.get(rel) != new.get(rel):
            problems.append(rel)
    if manifest is not None:
        frozen = ctx.read_json("freeze/source_closure.json")
        for role, path_, _study in _manifest_inputs(manifest):
            item = next((e for e in frozen.get("entries") or []
                         if e["role"] == role), None)
            if item is None:
                problems.append(f"input:{role}")
    if problems:
        raise G21Error("STALE_SIGNATURE",
                       f"实现/输入闭包漂移：{problems[:8]}")
    return {"ok": True, "files": len(new),
            "closure_sha256": current["closure_sha256"]}


# ---------------------------------------------------------------------------
# 批准 tokenizer（G21-4）
# ---------------------------------------------------------------------------

def tokenizer_contract(root: "str | Path") -> dict[str, Any]:
    """核实本地批准 tokenizer 身份；缺件/不符即阻断，不下载替代。"""
    from orchestrator.rsi_r1 import review_capacity as RC
    ctx = G21Context(root)
    path = Path(RC.DEFAULT_TOKENIZER_PATH)
    doc: "dict[str, Any]" = {
        "schema": TOKENIZER_SCHEMA, "schema_version": 1,
        "path": str(path), "expected_sha256": RC.DEFAULT_TOKENIZER_SHA256,
        "framing": ("system 1 条 + messages 逐条；请求序列化 = "
                    "json.dumps({system,messages,max_tokens}, sort_keys=True)"),
        "input_token_cap": int(P.REVIEW_INPUT_TOKENS_R13),
        "output_token_cap": int(P.REVIEW_OUTPUT_TOKENS_R13),
        "approval_ref": ("orchestrator/rsi_r1/review_capacity.py:"
                         "DEFAULT_TOKENIZER_SHA256 + profiles."
                         "REVIEW_INPUT_TOKENS_R13（R1.3 冻结测量口径）"),
        "supplier_token_count": "UNKNOWN_NOT_CLAIMED",
        "created_at_utc": _utc_now()}
    if not path.is_file():
        doc.update(status="TOKENIZER_UNAVAILABLE", detail="本地 tokenizer 缺失")
        write_json_private(ctx.relative("freeze/tokenizer_contract.json"), doc)
        raise G21Error("TOKENIZER_UNAVAILABLE", str(path))
    got = sha_file(path)
    doc["sha256"] = got
    try:
        doc["library_version"] = importlib.metadata.version("tokenizers")
    except importlib.metadata.PackageNotFoundError:
        doc["library_version"] = "UNKNOWN"
    if got != RC.DEFAULT_TOKENIZER_SHA256:
        doc.update(status="TOKENIZER_APPROVAL_UNRESOLVED",
                   detail="artifact sha 与冻结批准值不符")
        write_json_private(ctx.relative("freeze/tokenizer_contract.json"), doc)
        raise G21Error("TOKENIZER_APPROVAL_UNRESOLVED", f"sha={got}")
    try:
        tokenizer = RC.load_tokenizer(path, RC.DEFAULT_TOKENIZER_SHA256)
    except RC.ReviewCapacityError as exc:
        doc.update(status="TOKENIZER_UNAVAILABLE", detail=f"{exc.code}: {exc.detail}")
        write_json_private(ctx.relative("freeze/tokenizer_contract.json"), doc)
        raise G21Error("TOKENIZER_UNAVAILABLE", exc.detail) from None
    doc.update(status="TOKENIZER_READY",
               config_sha256={"review_capacity.py": sha_file(
                   repo_root() / "orchestrator/rsi_r1/review_capacity.py"),
                   "profiles.py": sha_file(
                       repo_root() / "orchestrator/rsi_r1/profiles.py")})
    write_json_private(ctx.relative("freeze/tokenizer_contract.json"), doc)
    return {"ok": True, "status": doc["status"], "tokenizer": tokenizer,
            "contract": doc}


# ---------------------------------------------------------------------------
# 独立 raw 复算（G21-3）
# ---------------------------------------------------------------------------

_FORBIDDEN_CHECKER_IMPORTS = ("orchestrator", "g2_contract", "g2_validation",
                              "metrics", "statistics", "contrasts")


def stat_contract(root: "str | Path") -> dict[str, Any]:
    """登记正式统计口径（参数是事实；独立实现不复用生产代码）。"""
    from orchestrator.rsi_r1 import rubric as RUL
    from orchestrator.rsi_r1 import statistics as ST
    ctx = G21Context(root)
    doc = {"schema": "simpraxis.rsi_g21.stat_contract.v1", "schema_version": 1,
           "source": ("orchestrator/rsi_r1/rubric.py + statistics.py"
                      "（参数登记；复算实现见 g21_raw_checker.py）"),
           "seed": int(ST.STATISTICS_SEED),
           "bootstrap_resamples": int(RUL.BOOTSTRAP_RESAMPLES),
           "bootstrap_quantile": str(RUL.BOOTSTRAP_QUANTILE),
           "bootstrap_dtype": str(RUL.BOOTSTRAP_DTYPE),
           "ci_level": float(RUL.CI_PRIMARY),
           "claim_abs_tol": float(RUL.CLAIM_ABS_TOL),
           "claim_rel_tol": float(RUL.CLAIM_REL_TOL),
           "metrics": {
               "displacement_x_m": {
                   "study": "A", "unit": "m", "formula":
                   "x(t_end)-meta.initial_x_m（qpos[-1][0] − 声明初值）",
                   "censoring": "none"},
               "normalized_tracking_rmse": {
                   "study": "B", "unit": "1",
                   "formula": ("sqrt(mean((qpos[:,0]-target_q[:,0])^2))/"
                               "target_amplitude_rad"),
                   "window_s": [float(RUL.TRACKING_WINDOW_START_S),
                                float(RUL.TRACKING_WINDOW_END_S)],
                   "instability_events": f"LOSS_CAP={float(RUL.LOSS_CAP)}"},
           },
           "file_shas": {"rubric.py": sha_file(
               repo_root() / "orchestrator/rsi_r1/rubric.py"),
               "statistics.py": sha_file(
                   repo_root() / "orchestrator/rsi_r1/statistics.py")}}
    write_json_private(ctx.relative("freeze/stat_contract.json"), doc)
    return doc


def _checker_independent(checker: Path) -> dict[str, Any]:
    text = checker.read_text(encoding="utf-8")
    hits = [name for name in _FORBIDDEN_CHECKER_IMPORTS
            if re.search(rf"^\s*(from|import)\s+{re.escape(name)}\b", text,
                         re.MULTILINE)]
    return {"independent": not hits, "forbidden_imports": hits,
            "checker_sha256": sha_bytes(text.encode("utf-8"))}


def run_raw_check(root: "str | Path", manifest: Mapping[str, Any], *,
                  python: "str | None" = None) -> dict[str, Any]:
    """子进程独立复算 + 预登记变异矩阵；不信报告布尔。"""
    ctx = G21Context(root)
    G21Context(root).subdir("raw_check")
    stats = stat_contract(root)
    checker = repo_root() / "orchestrator/rsi_r1/g21_raw_checker.py"
    independent = _checker_independent(checker)
    if not independent["independent"]:
        raise G21Error("RAW_CHECKER_NOT_INDEPENDENT",
                       f"禁用 import：{independent['forbidden_imports']}")
    python = python or sys.executable
    stat_path = ctx.relative("freeze/stat_contract.json")
    studies: "dict[str, Any]" = {}
    for study in STUDIES:
        entry = manifest["studies"][study]
        plan12 = _plan12(entry)
        task_path = ctx.relative(f"public/{study}/task.json")
        scope_path = ctx.relative(f"public/{study}/scope_contract.json")
        candidate = ctx.relative(f"analysis/{study}/{plan12}/output.json")
        batch = Path(entry["observations_batch"])
        if ctx.exists(f"observations/{batch.name}"):
            raw_dir = ctx.relative(f"observations/{batch.name}")
        else:
            raw_dir = batch
        if not task_path.is_file():
            task_path = Path(entry["task"])
        if not scope_path.is_file():
            scope_path = Path(entry["scope_contract"])
        rebuilt = ctx.relative(f"contract/scope_{study}.json")
        if rebuilt.is_file():
            scope_path = rebuilt
        if not candidate.is_file():
            candidate = Path(entry["output_raw"])
        out = ctx.relative(f"raw_check/{study}.json")
        proc = subprocess.run(
            [python, "-I", str(checker), "--study", study,
             "--task", str(task_path), "--scope", str(scope_path),
             "--raw", str(raw_dir), "--candidate", str(candidate),
             "--stat-contract", str(stat_path), "--out", str(out)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=1800)
        report = read_json(out) if out.is_file() else {
            "ok": False, "error": proc.stderr.decode("utf-8", "replace")[-800:]}
        report["checker_rc"] = proc.returncode
        task = TS.TaskSpecV13.from_dict(
            read_json(manifest["studies"][study]["task"]))
        ready = source_readiness(manifest["studies"][study], task)
        report["source_status"] = ready["status"]
        if ready["status"] != SOURCE_READY:
            # 源本身缺必需对比：checker 的 REQUIRED_CONTRAST_MISSING 是
            # 预期的来源阻断，不是复算失败；其余轴（binding/scope/数值主对比）
            # 仍须成立。
            problems = list(report.get("problems") or [])
            expected_gap = "REQUIRED_CONTRAST_MISSING" in problems
            axes = report.get("axes") or {}
            report["raw_ok"] = bool(expected_gap and axes.get("binding_valid")
                                    and axes.get("scope_valid"))
        else:
            report["raw_ok"] = bool(report.get("ok"))
        studies[study] = report
    mutations: "dict[str, Any]" = {}
    mutation_study = _mutation_study(manifest)
    for name in _mutation_names():
        entry = manifest["studies"][mutation_study]
        out = ctx.relative(f"raw_check/mutation-{name}.json")
        task_path = ctx.relative(f"public/{mutation_study}/task.json")
        scope_path = ctx.relative(f"contract/scope_{mutation_study}.json")
        plan12 = _plan12(entry)
        candidate = ctx.relative(f"analysis/{mutation_study}/{plan12}/output.json")
        batch = Path(entry["observations_batch"])
        raw_dir = (ctx.relative(f"observations/{batch.name}")
                   if ctx.exists(f"observations/{batch.name}") else batch)
        if not task_path.is_file():
            task_path = Path(entry["task"])
        if not scope_path.is_file():
            scope_path = Path(entry["scope_contract"])
        if not candidate.is_file():
            candidate = Path(entry["output_raw"])
        proc = subprocess.run(
            [python, "-I", str(checker), "--study", mutation_study,
             "--task", str(task_path), "--scope", str(scope_path),
             "--raw", str(raw_dir), "--candidate", str(candidate),
             "--stat-contract", str(stat_path), "--out", str(out),
             "--mutation", name],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=1800)
        mutations[name] = (read_json(out) if out.is_file() else
                           {"ok": False,
                            "error": proc.stderr.decode("utf-8", "replace")[-400:]})
    report = {"schema": "simpraxis.rsi_g21.raw_check_report.v1",
              "schema_version": 1, "independent": independent["independent"],
              "checker_sha256": independent["checker_sha256"],
              "stat_contract_sha256": sha_file(stat_path),
              "studies": studies,
              "mutations": mutations,
              "ok": bool(independent["independent"])
              and all(doc.get("raw_ok") for doc in studies.values())
              and all(doc.get("ok") for doc in mutations.values()),
              "scope_ok": all(doc.get("axes", {}).get("scope_valid")
                              for doc in studies.values() if doc.get("axes")),
              "created_at_utc": _utc_now()}
    write_json_private(ctx.relative("raw_check/report.json"), report)
    return report


def _mutation_study(manifest: Mapping[str, Any]) -> str:
    """变异基线选**源就绪**且 task/study 匹配的 study（B 优先）。"""
    for study in ("B", "A"):
        try:
            task = read_json(manifest["studies"][study]["task"])
        except (OSError, ValueError):
            continue
        prefix = str(task.get("mechanism_id") or "").split("_")[0]
        if prefix == study:
            ready = source_readiness(manifest["studies"][study],
                                     TS.TaskSpecV13.from_dict(task))
            if ready["status"] == SOURCE_READY:
                return study
    return "B"


def _plan12(entry: Mapping[str, Any]) -> str:
    """候选产物副本目录名：与 ``materialize_study`` 的 plan.sha256[:12] 同源。"""
    return C.ResearchPlan.from_dict(read_json(entry["plan"])).sha256[:12]


def _mutation_names() -> "list[str]":
    import orchestrator.rsi_r1.g21_raw_checker as CHK
    return list(CHK.MUTATIONS)


# ---------------------------------------------------------------------------
# 容量矩阵（G21-4）
# ---------------------------------------------------------------------------

def _filler_contrast(index: int, *, n_blocks: int = 1) -> dict[str, Any]:
    return {"contrast_id": f"c{index}", "metric_id": "displacement_x_m",
            "proposition": "increase_gt", "delta": 0.001,
            "estimate": 0.001 * (index + 1), "lower": 0.0, "upper": 0.002,
            "level": 0.95, "science_status": "INCONCLUSIVE",
            "n_blocks": n_blocks,
            "dropped": {"count": 0, "ids": [], "ids_sha256": ""}}


def _envelope_with(n_contrasts: int, *, n_runs: int,
                   scope: G2.ScopeContractV1) -> dict[str, Any]:
    contrasts = [_filler_contrast(i) for i in range(max(1, n_contrasts))]
    evidence = [{"artifact_id": "analysis/output.json",
                 "json_pointer": f"/contrasts/{i}/estimate",
                 "field_sha256": G2.field_sha(c["estimate"])}
                for i, c in enumerate(contrasts)]
    artifacts = [{"artifact_id": "analysis/output.json",
                  "relative_path": "analysis/output.json",
                  "sha256": "e" * 64, "bytes": max(1, n_runs * 100),
                  "access_mode": "read_result_artifact"}]
    return G2.build_envelope(
        study_id="X", phase="confirm", plan_sha256="a" * 64,
        analysis_receipt_sha256="b" * 64, analysis_output_sha256="c" * 64,
        scope_contract=scope,
        required_contrast_ids=[c["contrast_id"] for c in contrasts],
        contrasts=contrasts, primary_contrast_id=contrasts[0]["contrast_id"],
        denominator=n_runs, dropped={"count": 0, "ids": [], "ids_sha256": ""},
        diagnostics={"status": "OBSERVED"}, artifacts=artifacts,
        evidence=evidence,
        summary={"n_required": len(contrasts), "shape": "capacity_fixture"})


def _synthetic_envelope(n_runs: int, *, contrast_count: int = 2,
                        scope: G2.ScopeContractV1) -> dict[str, Any]:
    return _envelope_with(contrast_count, n_runs=n_runs, scope=scope)


def _boundary_envelopes(scope: G2.ScopeContractV1
                        ) -> "tuple[dict[str, Any], dict[str, Any]]":
    """真实 16 KiB 边界：逐步加对比直到刚好 ≤ 帽，再加一条即超。"""
    low, high = 1, 512
    best = None
    while low <= high:
        mid = (low + high) // 2
        try:
            doc = _envelope_with(mid, n_runs=192, scope=scope)
        except G2.G2Error:
            high = mid - 1
            continue
        size = len(G2.canonical(doc))
        if size <= G2.ENVELOPE_MAX_BYTES:
            best = (mid, doc, size)
            low = mid + 1
        else:
            high = mid - 1
    if best is None:
        raise G21Error("CAPACITY_NOT_ESTABLISHED", "无法构造 ≤16KiB 的合法 envelope")
    ok_doc = best[1]
    overflow = dict(ok_doc)
    extra = _filler_contrast(best[0])
    overflow["contrasts"] = list(ok_doc["contrasts"]) + [extra]
    overflow["required_contrast_ids"] = list(ok_doc["required_contrast_ids"]) + [
        extra["contrast_id"]]
    overflow["evidence"] = list(ok_doc["evidence"]) + [{
        "artifact_id": "analysis/output.json",
        "json_pointer": f"/contrasts/{best[0]}/estimate",
        "field_sha256": G2.field_sha(extra["estimate"])}]
    overflow["envelope_sha256"] = G2.content_sha(overflow,
                                                exclude=("envelope_sha256",))
    return ok_doc, overflow


def capacity_matrix(scope: G2.ScopeContractV1) -> dict[str, Any]:
    """冻结容量 fixture 矩阵（实现前写死；运行后不改预期凑绿）。"""
    entries = [
        {"fixture_id": "envelope_boundary_ok", "source_type": "synthetic",
         "runs": 192, "expected_outcome": "NORMAL_SUCCESS"},
        {"fixture_id": "envelope_16384_ok", "source_type": "synthetic",
         "runs": 160, "expected_outcome": "NORMAL_SUCCESS"},
        {"fixture_id": "envelope_16385_block", "source_type": "synthetic",
         "runs": 192, "expected_outcome": "BLOCK_BEFORE_SEND"},
        {"fixture_id": "page_8192_ok", "source_type": "synthetic",
         "runs": 128, "expected_outcome": "NORMAL_SUCCESS"},
        {"fixture_id": "page_8193_block", "source_type": "synthetic",
         "runs": 128, "expected_outcome": "BLOCK_BEFORE_SEND"},
        {"fixture_id": "artifact_over_8mib_block", "source_type": "synthetic",
         "runs": 128, "expected_outcome": "BLOCK_BEFORE_SEND"},
        {"fixture_id": "legacy_4096_boundary", "source_type": "synthetic",
         "runs": 160, "expected_outcome": "NORMAL_SUCCESS"},
        {"fixture_id": "sealed_A_160", "source_type": "sealed",
         "runs": 160, "expected_outcome": "SOURCE_OR_SUCCESS"},
        {"fixture_id": "sealed_B_128", "source_type": "sealed",
         "runs": 128, "expected_outcome": "SOURCE_OR_SUCCESS"},
        {"fixture_id": "required_negative_control_set", "source_type": "synthetic",
         "runs": 128, "expected_outcome": "NORMAL_SUCCESS"},
        {"fixture_id": "long_ids_multi_page", "source_type": "synthetic",
         "runs": 128, "expected_outcome": "NORMAL_SUCCESS"},
    ]
    return {"schema": CAPACITY_SCHEMA, "schema_version": 1,
            "entries": entries,
            "limits": {"envelope_max_bytes": G2.ENVELOPE_MAX_BYTES,
                       "page_max_bytes": G2.PAGE_MAX_BYTES,
                       "artifact_max_bytes": G2.ARTIFACT_MAX_BYTES,
                       "legacy_reply_max_bytes": RT.TOOL_REPLY_MAX_BYTES,
                       "request_token_cap": int(P.REVIEW_INPUT_TOKENS_R13)},
            "note": "正常形状必须消费并 submit；超限负例必须在 sender 前阻断"}


def _build_request_for(envelope: Mapping[str, Any],
                       counter: Callable[[str], int]) -> RT.Request:
    return RT.build_request(system="g21 capacity", task_brief="shape",
                            turns=[], counter=counter,
                            delivery_policy=RT.POLICY_G2_ATOMIC,
                            result_context=dict(envelope))


def run_capacity(root: "str | Path", manifest: Mapping[str, Any], *,
                 tokenizer: Any) -> dict[str, Any]:
    """跑冻结矩阵；正常形状完整 envelope→Request，超限在 sender 前阻断。"""
    ctx = G21Context(root)
    G21Context(root).subdir("capacity")
    frozen_path = ctx.relative("freeze/capacity_matrix.json")
    if not frozen_path.is_file():
        raise G21Error("CAPACITY_MATRIX_MISSING", "先写冻结矩阵")
    matrix = ctx.read_json("freeze/capacity_matrix.json")
    counter = tokenizer_counter(tokenizer)
    scope_path = Path(root) / "contract" / "scope_A.json"
    if not scope_path.is_file():
        scope_path = Path(manifest["studies"]["A"]["scope_contract"])
    scope = G2.load_scope_contract(scope_path)
    results: "list[dict[str, Any]]" = []
    for entry in matrix["entries"]:
        fid = entry["fixture_id"]
        observed: "dict[str, Any]" = {"fixture_id": fid,
                                      "expected": entry["expected_outcome"]}
        try:
            if fid.startswith("sealed_"):
                study = "A" if fid.endswith("160") else "B"
                task = TS.TaskSpecV13.from_dict(
                    read_json(manifest["studies"][study]["task"]))
                ready = source_readiness(manifest["studies"][study], task)
                observed["source_status"] = ready["status"]
                observed["missing_contrast_ids"] = ready["missing_contrast_ids"]
                observed["outcome"] = ("BLOCKED_SOURCE"
                                       if ready["status"] != SOURCE_READY
                                       else "NORMAL_SUCCESS")
            elif fid in ("envelope_16384_ok", "envelope_16385_block"):
                ok_doc, overflow = _boundary_envelopes(scope)
                if fid == "envelope_16384_ok":
                    req = _build_request_for(ok_doc, counter)
                    observed.update({"envelope_bytes": len(G2.canonical(ok_doc)),
                                     "request_bytes": req.n_bytes,
                                     "request_tokens": req.local_tokens})
                    observed["outcome"] = (
                        "NORMAL_SUCCESS"
                        if len(G2.canonical(ok_doc)) <= G2.ENVELOPE_MAX_BYTES
                        else "UNEXPECTED_SUCCESS")
                else:
                    observed["envelope_bytes"] = len(G2.canonical(overflow))
                    try:
                        _build_request_for(overflow, counter)
                        observed["outcome"] = "UNEXPECTED_SUCCESS"
                    except RT.ResultViewIncomplete:
                        observed["outcome"] = "BLOCK_BEFORE_SEND"
            elif fid in ("page_8192_ok", "page_8193_block"):
                probe = _page_probe(G2.PAGE_MAX_BYTES if fid.endswith("ok")
                                    else G2.PAGE_MAX_BYTES + 1)
                observed.update(probe)
                observed["outcome"] = (
                    "NORMAL_SUCCESS" if probe.get("page_ok")
                    else "BLOCK_BEFORE_SEND" if probe.get("code")
                    in ("PAGE_TOO_LARGE", "ARTIFACT_MISSING")
                    else "UNEXPECTED_SUCCESS")
            elif fid == "artifact_over_8mib_block":
                observed["outcome"] = ("BLOCK_BEFORE_SEND"
                                       if _artifact_blocked(
                                           G2.ARTIFACT_MAX_BYTES + 1)
                                       else "UNEXPECTED_SUCCESS")
            elif fid == "legacy_4096_boundary":
                small = RT.Turn(model_text="m", result={
                    "ok": True, "action": "analyze", "note": "y" * 100})
                big = RT.Turn(model_text="m", result={
                    "ok": True, "action": "analyze", "blob": "y" * 6000})
                small_size = len(json.dumps(dict(small.clipped(
                    policy=RT.POLICY_LEGACY).result), ensure_ascii=False,
                    sort_keys=True).encode("utf-8"))
                big_size = len(json.dumps(dict(big.clipped(
                    policy=RT.POLICY_LEGACY).result), ensure_ascii=False,
                    sort_keys=True).encode("utf-8"))
                observed.update({"small_bytes": small_size,
                                 "big_bytes": big_size,
                                 "cap": RT.TOOL_REPLY_MAX_BYTES})
                observed["outcome"] = (
                    "NORMAL_SUCCESS"
                    if small_size <= RT.TOOL_REPLY_MAX_BYTES
                    and big_size <= RT.TOOL_REPLY_MAX_BYTES
                    else "UNEXPECTED_SUCCESS")
            elif fid == "long_ids_multi_page":
                payload = _long_ids_page_probe()
                observed.update(payload)
                observed["outcome"] = ("NORMAL_SUCCESS"
                                       if payload["page_ok"] and payload["sha_ok"]
                                       else "UNEXPECTED_SUCCESS")
            elif fid == "required_negative_control_set":
                envelope = _synthetic_envelope(128, contrast_count=2,
                                               scope=scope)
                req = _build_request_for(envelope, counter)
                observed.update({"envelope_bytes": len(G2.canonical(envelope)),
                                 "request_bytes": req.n_bytes,
                                 "request_tokens": req.local_tokens,
                                 "required_ids": list(
                                     envelope["required_contrast_ids"])})
                observed["outcome"] = ("NORMAL_SUCCESS"
                                       if req.local_tokens
                                       <= int(P.REVIEW_INPUT_TOKENS_R13)
                                       else "BLOCK_BEFORE_SEND")
            else:
                runs = int(entry["runs"])
                envelope = _synthetic_envelope(runs, scope=scope)
                req = _build_request_for(envelope, counter)
                observed.update({"envelope_bytes": len(G2.canonical(envelope)),
                                 "request_bytes": req.n_bytes,
                                 "request_tokens": req.local_tokens,
                                 "n_runs": runs})
                ok = (len(G2.canonical(envelope)) <= G2.ENVELOPE_MAX_BYTES
                      and req.local_tokens <= int(P.REVIEW_INPUT_TOKENS_R13))
                observed["outcome"] = "NORMAL_SUCCESS" if ok else "BLOCK_BEFORE_SEND"
        except RT.ResultViewIncomplete as exc:
            observed["outcome"] = "BLOCK_BEFORE_SEND"
            observed["block_detail"] = exc.detail
        except G2.G2Error as exc:
            observed["outcome"] = "BLOCK_BEFORE_SEND"
            observed["block_detail"] = f"{exc.code}: {exc.detail}"
        expected = entry["expected_outcome"]
        if expected == "SOURCE_OR_SUCCESS":
            observed["ok"] = observed["outcome"] in ("NORMAL_SUCCESS",
                                                     "BLOCKED_SOURCE")
        else:
            observed["ok"] = observed["outcome"] == expected
        results.append(observed)
    report = {"schema": CAPACITY_SCHEMA, "schema_version": 1,
              "results": results,
              "ok": all(row["ok"] for row in results),
              "n_ok": sum(1 for row in results if row["ok"]),
              "n_total": len(results),
              "tokenizer_sha256": str(
                  (ctx.read_json("freeze/tokenizer_contract.json")
                   if ctx.exists("freeze/tokenizer_contract.json") else {})
                  .get("sha256") or ""),
              "created_at_utc": _utc_now()}
    write_json_private(ctx.relative("capacity/report.json"), report)
    return report


def _page_probe(page_bytes: int) -> dict[str, Any]:
    """真实分页探针：同一个 9000B artifact，页帽 8192 成功、8193 发网前拒绝。"""
    data = ("x" * 9000).encode("utf-8")
    digest = sha_bytes(data)
    registry = G2.ArtifactRegistry([{
        "artifact_id": "analysis/output.json",
        "relative_path": "analysis/output.json", "sha256": digest,
        "bytes": len(data), "access_mode": "read_result_artifact"}])
    try:
        page = G2.read_page(registry=registry, read_bytes=lambda rel: data,
                            artifact_id="analysis/output.json",
                            output_sha256=digest, cursor=0,
                            page_bytes=page_bytes)
    except G2.G2Error as exc:
        return {"page_ok": False, "code": exc.code}
    ok = (page["n_bytes"] <= G2.PAGE_MAX_BYTES
          and page["data"] == data[: page["n_bytes"]].decode("utf-8")
          and page["page_sha256"] == sha_bytes(
              data[: page["n_bytes"]]))
    return {"page_ok": bool(ok), "n_bytes": page["n_bytes"],
            "next_cursor": page["next_cursor"]}


def _artifact_blocked(n_bytes: int) -> bool:
    try:
        G2._artifact_record({"artifact_id": "a", "relative_path":
                             "analysis/x.json", "sha256": "0" * 64,
                             "bytes": n_bytes,
                             "access_mode": "read_result_artifact"})
    except G2.G2Error:
        return True
    return False


def _long_ids_page_probe() -> dict[str, Any]:
    """长 artifact_id/长 run_id 的多页读取（含 UTF-8 边界）。"""
    text = ("run-{" + "a" * 120 + "}: 结果 " * 700)
    data = text.encode("utf-8")
    sha = sha_bytes(data)
    registry = G2.ArtifactRegistry([{
        "artifact_id": "analysis/output.json", "relative_path":
        "analysis/output.json", "sha256": sha, "bytes": len(data),
        "access_mode": "read_result_artifact"}])
    pages: "list[str]" = []
    cursor = 0
    while cursor is not None:
        page = G2.read_page(registry=registry,
                            read_bytes=lambda rel, d=data: d,
                            artifact_id="analysis/output.json",
                            output_sha256=sha, cursor=cursor,
                            page_bytes=G2.PAGE_MAX_BYTES)
        pages.append(page["data"])
        cursor = page["next_cursor"]
    rebuilt = "".join(pages).encode("utf-8")
    return {"page_ok": len(pages) >= 1, "sha_ok": sha_bytes(rebuilt) == sha,
            "n_pages": len(pages), "bytes": len(data)}


# ---------------------------------------------------------------------------
# OS 级隔离探针（G21-5；能力检测不是证据）
# ---------------------------------------------------------------------------

_PROBE_SOURCE = r'''
import hashlib, json, os, socket, sys
DATA, OUT = sys.argv[1], sys.argv[2]
def attempt(fn):
    try:
        return {"ok": True, "value": fn()}
    except OSError as exc:
        return {"ok": False, "errno": exc.errno, "strerror": exc.strerror}
result = {
  "uid": os.getuid(), "gid": os.getgid(),
  "allowed_read": attempt(lambda: hashlib.sha256(open(DATA + "/canary.txt", "rb").read()).hexdigest()),
  "forbidden_read": {name: attempt(lambda p=name: open(DATA + "/" + p, "rb").read()[:8].decode("utf-8", "replace"))
                     for name in os.environ.get("G21_FORBIDDEN", "").split(",") if name},
  "positive_read": {name: attempt(lambda p=name: open(DATA + "/" + p, "rb").read()[:8].decode("utf-8", "replace"))
                    for name in os.environ.get("G21_READ_OK", "").split(",") if name},
  "host_env_leak": sorted(k for k in os.environ if k.startswith("G21_HOST_")),
  "write_data": attempt(lambda: open(DATA + "/write_probe", "wb").write(b"x")),
  "write_out": attempt(lambda: open(OUT + "/probe.txt", "wb").write(b"ok")),
  "network": attempt(lambda: socket.create_connection(("1.1.1.1", 80), timeout=2).close()),
  "routes": open("/proc/net/route").read().strip(),
  "env_keys": sorted(os.environ),
  "fd_count": len(os.listdir("/proc/self/fd")),
  "pid1": os.getpid(),
}
print(json.dumps(result, sort_keys=True))
'''


def _docker_argv(*, image: str, container: str, view: Path, out: Path,
                 uid: str, forbidden: Sequence[str],
                 read_ok: Sequence[str] = ()) -> "list[str]":
    return [
        "docker", "run", "--rm", "--name", container,
        "--network", "none",
        "--user", f"{uid}:{uid}",
        "--read-only",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--pids-limit", "64",
        "--memory", "256m",
        "--memory-swap", "256m",
        "--cpus", "1.0",
        "--tmpfs", "/tmp:rw,noexec,nosuid,size=16m",
        "-e", "PYTHONDONTWRITEBYTECODE=1",
        "-e", "HOME=/tmp",
        "-e", "G21_FORBIDDEN=" + ",".join(forbidden),
        "-e", "G21_READ_OK=" + ",".join(read_ok),
        "-v", f"{view}:/data:ro",
        "-v", f"{out}:/out:rw",
        image, "python3", "/data/probe.py", "/data", "/out",
    ]


def _iso_view(root: Path, name: str, *, canary: str,
              extra: "Mapping[str, str] | None" = None,
              forbid_private: bool = True) -> tuple[Path, Path]:
    view = root / "isolation" / name / "view"
    out = root / "isolation" / name / "out"
    ensure_private_dir(view)
    ensure_private_dir(out)
    (view / "canary.txt").write_text(canary, encoding="utf-8")
    (view / "probe.py").write_text(_PROBE_SOURCE, encoding="utf-8")
    for rel, text in (extra or {}).items():
        target = view / rel
        ensure_private_dir(target.parent)
        target.write_text(text, encoding="utf-8")
    os.chmod(view, 0o755)
    for child in view.iterdir():
        if child.is_file():
            os.chmod(child, 0o644)
    if forbid_private:
        priv = view / "private" / "expected.json"
        ensure_private_dir(priv.parent)
        priv.write_text('{"expected":"FORBIDDEN_CANARY"}', encoding="utf-8")
        os.chmod(priv, 0o600)
    os.chmod(out, 0o777)
    return view, out


def run_isolation(root: "str | Path", *, image_digest: str,
                  timeout_s: int = 120) -> dict[str, Any]:
    """每主体实际文件探针：允许读/禁止读/写 ro/写 out/网络，全部留证。"""
    ctx = G21Context(root)
    G21Context(root).subdir("isolation")
    root_path = ctx.root
    principals = {
        "renderer": {"uid": "10001", "canary": "renderer-canary",
                     "forbid": False},
        "candidate": {"uid": "10002", "canary": "candidate-canary",
                      "forbid": True},
        "compiler": {"uid": "10003", "canary": "compiler-canary",
                     "forbid": True},
        "checker": {"uid": "10004", "canary": "checker-canary",
                    "forbid": True,
                    "extra": {"raw": "raw-canary-content",
                              "expected": "expected-canary-content"}},
    }
    rows: "list[dict[str, Any]]" = []
    ok = True
    for name, spec in principals.items():
        view, out = _iso_view(root_path, name, canary=spec["canary"],
                              extra=spec.get("extra"),
                              forbid_private=bool(spec["forbid"]))
        forbidden = ["private/expected.json"] if spec["forbid"] else []
        container = f"g21iso-{name}-{int(time.time() * 1000) % 100000}"
        read_ok = sorted((spec.get("extra") or {}))
        argv = _docker_argv(image=image_digest, container=container, view=view,
                            out=out, uid=spec["uid"], forbidden=forbidden,
                            read_ok=read_ok)
        started = time.time()
        proc = subprocess.run(argv, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=timeout_s,
                              env={**os.environ,
                                   "G21_HOST_CANARY": "host-only-canary"})
        elapsed = int((time.time() - started) * 1000)
        log = proc.stdout + b"\n--- stderr ---\n" + proc.stderr
        log_sha = sha_bytes(log)
        log_path = root_path / "isolation" / f"{name}.log"
        log_path.write_bytes(log)
        os.chmod(log_path, 0o600)
        parsed: "dict[str, Any]" = {}
        try:
            parsed = json.loads(proc.stdout.decode("utf-8").strip().splitlines()[-1])
        except (ValueError, IndexError):
            parsed = {"parse_error": True}
        checks = _isolation_checks(name, spec, parsed, forbidden)
        ok = ok and all(checks.values())
        rows.append({"principal": name, "container": container, "argv": argv,
                     "image": image_digest, "uid": spec["uid"],
                     "elapsed_ms": elapsed, "rc": proc.returncode,
                     "log_sha256": log_sha, "probe": parsed,
                     "checks": checks,
                     "mounts": [f"{view}:/data:ro", f"{out}:/out:rw"]})
    # AC G21-23 负控：故意把私件挂成可读 ⇒ 必须被检出
    view, out = _iso_view(root_path, "negative_control",
                          canary="neg-canary",
                          extra={"private/expected.json":
                                 '{"expected":"NEG_CONTROL_READABLE"}'},
                          forbid_private=False)
    os.chmod(view / "private", 0o755)  # 故意可穿越/可读：负控
    os.chmod(view / "private" / "expected.json", 0o644)
    container = f"g21iso-neg-{int(time.time() * 1000) % 100000}"
    argv = _docker_argv(image=image_digest, container=container, view=view,
                        out=out, uid="10005",
                        forbidden=["private/expected.json"])
    proc = subprocess.run(argv, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, timeout=timeout_s)
    neg_log = proc.stdout + b"\n--- stderr ---\n" + proc.stderr
    (root_path / "isolation" / "negative_control.log").write_bytes(neg_log)
    os.chmod(root_path / "isolation" / "negative_control.log", 0o600)
    neg_parsed: "dict[str, Any]" = {}
    try:
        neg_parsed = json.loads(proc.stdout.decode("utf-8").strip().splitlines()[-1])
    except (ValueError, IndexError):
        neg_parsed = {"parse_error": True}
    neg_read = (neg_parsed.get("forbidden_read") or {}).get(
        "private/expected.json") or {}
    neg_detected = bool(neg_read.get("ok")) is True
    report = {"schema": ISOLATION_SCHEMA, "schema_version": 1,
              "status": "ISOLATION_OK" if ok and neg_detected else
                        ("ISOLATION_UNVERIFIED" if not ok else "NEGATIVE_CONTROL_FAILED"),
              "image": image_digest, "principals": rows,
              "negative_control": {"readable_private": neg_detected,
                                   "violation_detected": neg_detected,
                                   "probe": neg_parsed, "argv": argv},
              "note": ("能力检测不算证据；只有实际探针 rc/errno/uid/mount 成立才 PASS。"
                       "本矩阵证明四类主体在独立 uid + 只读挂载视图下的文件访问边界；"
                       "证据桥的消费者是宿主子进程（-I、临时 cwd、env 仅 PATH，"
                       "逐回合记在 logs/<study>-turns.jsonl），未在容器内运行——"
                       "该差异如实保留，不当作候选进程已沙盒化。"),
              "created_at_utc": _utc_now()}
    write_json_private(ctx.relative("isolation/report.json"), report)
    return report


def _isolation_checks(name: str, spec: Mapping[str, Any],
                      parsed: Mapping[str, Any],
                      forbidden: Sequence[str]) -> dict[str, bool]:
    canary_sha = sha_bytes(str(spec["canary"]).encode("utf-8"))
    allowed = (parsed.get("allowed_read") or {})
    checks = {
        "probe_parsed": not parsed.get("parse_error", False),
        "uid_applied": parsed.get("uid") == int(spec["uid"]),
        "allowed_read": bool(allowed.get("ok")) and
        str(allowed.get("value")) == canary_sha,
        "write_data_blocked": not (parsed.get("write_data") or {}).get("ok", False),
        "write_out_ok": bool((parsed.get("write_out") or {}).get("ok")),
        "network_blocked": not (parsed.get("network") or {}).get("ok", False),
        # 镜像自带 ENV 允许存在；**宿主**环境不得泄入（用 canary 判）。
        "host_env_not_leaked": not parsed.get("host_env_leak"),
        "no_default_route": not any(
            len(line.split()) > 2 and line.split()[1] == "00000000"
            for line in str(parsed.get("routes") or "").splitlines()[1:]),
    }
    for rel in forbidden:
        item = (parsed.get("forbidden_read") or {}).get(rel) or {}
        checks[f"forbidden_blocked:{rel}"] = (
            not item.get("ok", False)
            and item.get("errno") in (2, 13))  # ENOENT / EACCES
    for rel in ("raw", "expected"):
        if rel in (spec.get("extra") or {}):
            item = (parsed.get("positive_read") or {}).get(rel) or {}
            checks[f"positive_control:{rel}"] = bool(item.get("ok"))
    return checks


# ---------------------------------------------------------------------------
# 终态（G21-6）
# ---------------------------------------------------------------------------

def closeout(root: "str | Path") -> dict[str, Any]:
    """多轴 AND；缺项/UNKNOWN 不 vacuous pass；只出工程轴。"""
    ctx = G21Context(root)
    G21Context(root).subdir("decision")

    def load(rel: str) -> "dict[str, Any] | None":
        path = ctx.relative(rel)
        return ctx.read_json(rel) if path.is_file() else None

    freeze = load("freeze/source_closure.json")
    snapshot = load("contract/g21_contract.json")
    tokenizer = load("freeze/tokenizer_contract.json")
    capacity = load("capacity/report.json")
    isolation = load("isolation/report.json")
    raw_check = load("raw_check/report.json")
    bridge_a = load("reports/A-bridge.json")
    bridge_b = load("reports/B-bridge.json")
    gaps: "list[str]" = []
    axes: "dict[str, str]" = {}

    axes["content_integrity"] = "PASS" if freeze and snapshot else "UNKNOWN"
    axes["visibility_observability"] = _trace_axis(ctx, gaps)
    axes["submit_e2e"] = _submit_axis(bridge_a, bridge_b, gaps)
    axes["source_binding"] = _source_axis(bridge_a, bridge_b, gaps)
    axes["raw_checker"] = ("PASS" if raw_check and raw_check.get("ok")
                           else "FAIL" if raw_check else "UNKNOWN")
    if raw_check and not raw_check.get("independent"):
        axes["raw_checker"] = "FAIL"
        gaps.append("RAW_CHECKER_NOT_INDEPENDENT")
    axes["scope_contract"] = ("PASS" if raw_check and raw_check.get("scope_ok")
                              else "UNKNOWN")
    axes["tokenizer_capacity"] = (
        "PASS" if tokenizer and tokenizer.get("status") == "TOKENIZER_READY"
        and capacity and capacity.get("ok") else
        "FAIL" if (tokenizer and tokenizer.get("status") != "TOKENIZER_READY")
        or (capacity and not capacity.get("ok")) else "UNKNOWN")
    axes["negative_required_set"] = (
        "PASS" if capacity and all(
            row["ok"] for row in capacity.get("results") or []
            if row["fixture_id"] in ("required_negative_control_set",
                                     "sealed_A_160", "sealed_B_128")) else "FAIL")
    axes["isolation"] = (isolation or {}).get("status", "UNKNOWN")
    axes["legacy_compat"] = _legacy_axis(ctx, gaps)
    axes["signature_freshness"] = "PASS"
    try:
        verify_signature_freshness(root)
    except G21Error as exc:
        axes["signature_freshness"] = "FAIL"
        gaps.append(exc.code)
    for name, value in axes.items():
        if value == "UNKNOWN":
            gaps.append(f"UNKNOWN:{name}")
        elif value == "ISOLATION_UNVERIFIED":
            gaps.append("ISOLATION_UNVERIFIED")
        elif value == "FAIL":
            gaps.append(f"FAIL:{name}")
    blocked = [g for g in gaps if g.startswith("FAIL:") or g in (
        "RAW_CHECKER_NOT_INDEPENDENT", "STALE_SIGNATURE", "ISOLATION_UNVERIFIED",
        "SOURCE_EVIDENCE_INCOMPLETE", "TOKENIZER_UNAVAILABLE")]
    if any(bridge and bridge.get("source", {}).get("status") == SOURCE_INCOMPLETE
           for bridge in (bridge_a, bridge_b)):
        overall = "G2.1_BLOCKED_PRODUCTION_EVIDENCE"
        gaps.append(SOURCE_INCOMPLETE)
    elif "STALE_SIGNATURE" in gaps:
        overall = "G2.1_BLOCKED_PRODUCTION_EVIDENCE"
    elif axes["negative_required_set"] == "FAIL" or \
            axes["tokenizer_capacity"] == "FAIL":
        overall = "G2.1_BLOCKED_CAPACITY"
    elif "ISOLATION_UNVERIFIED" in gaps:
        overall = "G2.1_BLOCKED_ISOLATION"
    elif blocked or any(value in ("UNKNOWN", "FAIL") for value in axes.values()):
        overall = "G2.1_BLOCKED_PRODUCTION_EVIDENCE"
    else:
        overall = "G2.1_ENGINEERING_READY_FOR_NEW_DESIGN"
    signature = {}
    for key, rel in (("implementation_manifest_sha", "contract/g21_contract.json"),
                     ("input_manifest_sha", "freeze/source_closure.json"),
                     ("tokenizer_contract_sha", "freeze/tokenizer_contract.json"),
                     ("test_reports_sha", "reports/ac_report.json"),
                     ("raw_check_report_sha", "raw_check/report.json"),
                     ("capacity_report_sha", "capacity/report.json"),
                     ("isolation_report_sha", "isolation/report.json")):
        path = ctx.relative(rel)
        signature[key] = sha_file(path) if path.is_file() else ""
    report = {"schema": CLOSEOUT_SCHEMA, "schema_version": 1,
              "overall": overall, "axes": axes, "gaps": sorted(set(gaps)),
              "signature": signature,
              "scientific_claim": "NONE（只出工程轴；不晋升 R1/R2）",
              "future_paid_ready": overall ==
              "G2.1_ENGINEERING_READY_FOR_NEW_DESIGN",
              "created_at_utc": _utc_now()}
    write_json_private(ctx.relative("decision/g21_closeout.json"), report)
    return report


def _trace_axis(ctx: G21Context, gaps: "list[str]") -> str:
    total = 0
    kinds: "set[str]" = set()
    for study in STUDIES:
        path = ctx.relative(f"trace/{study}/events.jsonl")
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            doc = json.loads(line)
            total += 1
            kinds.add(str(doc.get("event_kind")))
    need = {"request_serialized", "response_raw", "action_parsed",
            "dispatch_return", "clipped_view", "claim_persisted"}
    if need <= kinds and total > 0:
        return "PASS"
    if total == 0:
        return "UNKNOWN"
    gaps.append("TRACE_EVENTS_MISSING")
    return "FAIL"


def _submit_axis(a: "Mapping[str, Any] | None",
                 b: "Mapping[str, Any] | None",
                 gaps: "list[str]") -> str:
    ready = [doc for doc in (a, b) if doc and
             doc.get("source", {}).get("status") == SOURCE_READY]
    if not ready:
        return "BLOCKED_SOURCE"
    if all((doc.get("five_faces") or {}).get("ok") for doc in ready):
        return "PASS"
    gaps.append("SUBMIT_E2E_INCOMPLETE")
    return "FAIL"


def _source_axis(a: "Mapping[str, Any] | None",
                 b: "Mapping[str, Any] | None",
                 gaps: "list[str]") -> str:
    docs = [doc for doc in (a, b) if doc]
    incomplete = [doc["study"] for doc in docs
                  if doc.get("source", {}).get("status") == SOURCE_INCOMPLETE]
    if incomplete:
        gaps.append(SOURCE_INCOMPLETE)
        return SOURCE_INCOMPLETE
    if all(doc and doc.get("plan_sha256") for doc in (a, b)):
        return "PASS"
    return "UNKNOWN"


def _legacy_axis(ctx: G21Context, gaps: "list[str]") -> str:
    """legacy 兼容轴来自 AC 报告里 legacy/G2 golden 的记录（rc 全 0）。"""
    path = ctx.relative("reports/ac_report.json")
    if not path.is_file():
        return "UNKNOWN"
    doc = ctx.read_json("reports/ac_report.json")
    ids = ("G21-04", "G21-16")
    records = doc.get("records") or {}
    chosen = [records[aid] for aid in ids if aid in records]
    if not chosen:
        return "UNKNOWN"
    if all(int(rec.get("rc", 1)) == 0 for rec in chosen):
        return "PASS"
    gaps.append("LEGACY_DRIFT")
    return "FAIL"
