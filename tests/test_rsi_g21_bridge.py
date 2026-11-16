"""RSI-G2.1 证据桥：清单/冻结/导入/材料化/pointer/scope provenance（零 POST）。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator.rsi_r1 import g2_contract as G2
from orchestrator.rsi_r1 import g21_bridge as G21
from orchestrator.rsi_r1 import state as S
from orchestrator.rsi_r1 import task_specs as TS
from tests.fixtures import g21_synthetic as SYN


def _manifest(root: Path) -> Path:
    info = SYN.build_sealed_study(root / "source")
    study = {"task": info["task"], "plan": info["plan"],
             "source_design": info["design"], "receipt": info["receipt"],
             "output_raw": info["output_raw"],
             "output_pointer": info["output_pointer"],
             "confirm_manifest": info["manifest"],
             "observations_batch": info["observations"],
             "scope_contract": info["sealed_scope"],
             "journal": info["journal"]}
    doc = {"schema": G21.MANIFEST_SCHEMA, "schema_version": 1,
           "source_campaign": "fixture_only", "source_commit": "0" * 40,
           "g2": {"root": str(root / "source"),
                  "contract_snapshot": info["sealed_scope"]},
           "studies": {"A": dict(study), "B": dict(study)}}
    path = root / "replay_manifest.json"
    path.write_text(json.dumps(doc, ensure_ascii=False, sort_keys=True),
                    encoding="utf-8")
    return path


def test_manifest_rejects_bad_schema_and_missing_paths(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"schema": "nope", "studies": {}}))
    with pytest.raises(G21.G21Error):
        G21.load_manifest(bad)
    doc = json.loads(_manifest(tmp_path).read_text())
    doc["studies"]["A"].pop("receipt")
    missing = tmp_path / "missing.json"
    missing.write_text(json.dumps(doc))
    with pytest.raises(G21.G21Error):
        G21.load_manifest(missing)


def test_source_readiness_detects_missing_required_contrast(tmp_path):
    manifest = G21.load_manifest(_manifest(tmp_path))
    task = TS.TaskSpecV13.from_dict(
        G21.read_json(manifest["studies"]["A"]["task"]))
    ready = G21.source_readiness(manifest["studies"]["A"], task)
    assert ready["status"] == G21.SOURCE_READY
    assert ready["required_contrast_ids"] == ["friction_x_speed",
                                              "negative_torsional"]
    # 去掉 NC 的候选 bytes ⇒ SOURCE_EVIDENCE_INCOMPLETE（不 host 回填）
    raw_path = Path(manifest["studies"]["A"]["output_raw"])
    doc = json.loads(raw_path.read_text())
    doc["contrasts"] = [c for c in doc["contrasts"]
                        if c["contrast_id"] == "friction_x_speed"]
    raw_path.write_text(json.dumps(doc, ensure_ascii=False, sort_keys=True))
    partial = G21.source_readiness(manifest["studies"]["A"], task)
    assert partial["status"] == G21.SOURCE_INCOMPLETE
    assert partial["missing_contrast_ids"] == ["negative_torsional"]


def test_import_is_typed_and_stops_at_results_released(tmp_path):
    manifest = G21.load_manifest(_manifest(tmp_path))
    journal = manifest["studies"]["A"]["journal"]
    plan = G21.plan_import(journal, "A")
    names = [event["event"] for event in plan["events"]]
    assert names[-1] == "study_transition"
    assert plan["events"][-1]["payload"]["to"] == S.ST_RESULTS_RELEASED
    assert "claim" not in names
    # 截断点之后的事件（claim）不得导入
    assert all(not (spec["event"] == "claim"
                    and spec["payload"].get("study") == "A")
               for spec in plan["events"])


def test_import_rejects_unregistered_event_and_tampered_journal(tmp_path):
    manifest = G21.load_manifest(_manifest(tmp_path))
    journal = Path(manifest["studies"]["A"]["journal"])
    lines = journal.read_text(encoding="utf-8").splitlines()
    extra = json.loads(lines[-1])
    extra.update({"seq": extra["seq"] + 1, "event": "mystery",
                  "payload": {"x": 1}, "prev_sha256": extra["sha256"]})
    journal.write_text("\n".join(lines + [json.dumps(extra)]) + "\n",
                       encoding="utf-8")
    with pytest.raises(G21.G21Error) as exc:
        G21.plan_import(journal, "A")
    assert exc.value.code in ("JOURNAL_TAMPERED", "IMPORT_EVENT_FORBIDDEN")
    # 篡改已登记事件 payload ⇒ sha 链断
    doc = json.loads(lines[0])
    doc["payload"]["reason"] = "tampered"
    journal.write_text(json.dumps(doc) + "\n" + "\n".join(lines[1:]) + "\n",
                       encoding="utf-8")
    with pytest.raises(G21.G21Error) as exc:
        G21.plan_import(journal, "A")
    assert exc.value.code == "JOURNAL_TAMPERED"


def test_materialize_copies_are_byte_identical(tmp_path):
    manifest = G21.load_manifest(_manifest(tmp_path))
    root = tmp_path / "diag"
    G21.freeze_inputs(root, manifest)
    G21.build_scopes(root, manifest)
    material = G21.materialize_study(root, manifest, "A")
    for item in material["copies"]:
        assert G21.sha_file(item["dst"]) == item["sha256"]
    pointer = G21.read_json(root / "private/analysis/A-confirm.json")
    assert G21.sha_file(root / pointer["raw_relpath"]) == pointer["raw_sha256"]
    registry_path = Path(material["candidate_output_rel"])
    assert root / registry_path == (root / "analysis/A/"
                                    f"{material['plan12']}/output.json")


def test_envelope_pointers_survive_contrast_permutation(tmp_path):
    manifest = G21.load_manifest(_manifest(tmp_path))
    task = TS.TaskSpecV13.from_dict(
        G21.read_json(manifest["studies"]["A"]["task"]))
    scope = G2.load_scope_contract(manifest["studies"]["A"]["scope_contract"])
    raw = Path(manifest["studies"]["A"]["output_raw"]).read_bytes()
    receipt = Path(manifest["studies"]["A"]["receipt"]).read_bytes()
    numbers = sorted(c["contrast_id"] for c in json.loads(raw)["contrasts"])
    task_numbers = [task.primary.id]
    for cid in numbers:
        if cid not in task_numbers:
            task_numbers.append(cid)
    reversed_doc = json.loads(raw)
    reversed_doc["contrasts"] = list(reversed(reversed_doc["contrasts"]))
    reversed_bytes = json.dumps(reversed_doc, ensure_ascii=False,
                                sort_keys=True).encode()
    for payload in (raw, reversed_bytes):
        envelope = G21.build_envelope_from_original(
            study="A", plan_sha256="a" * 64, output_bytes=payload,
            receipt_bytes=receipt,
            output_rel="analysis/A/x/output.json",
            receipt_rel="analysis/A/x/receipt.json", scope=scope, task=task)
        parsed = json.loads(payload)
        for item in envelope["evidence"]:
            value = G2.resolve_pointer(parsed, item["json_pointer"])
            assert G2.field_sha(value) == item["field_sha256"]


def test_scope_provenance_records_sealed_divergence(tmp_path):
    manifest = G21.load_manifest(_manifest(tmp_path))
    root = tmp_path / "diag"
    provenance = G21.build_scopes(root, manifest)
    entry = provenance["studies"]["A"]
    assert entry["divergence"]["interval_level"] == {"sealed": 0.9,
                                                    "g21": 0.95}
    scope = G2.load_scope_contract(root / entry["path"])
    assert scope.interval_level == 0.95
    task = G21.read_json(manifest["studies"]["A"]["task"])
    assert scope.task_spec_sha256 == G21.sha_bytes(G2.canonical(task))


def test_context_refuses_path_traversal(tmp_path):
    ctx = G21.G21Context(tmp_path)
    with pytest.raises(G21.G21Error):
        ctx.relative("../escape.txt")
    with pytest.raises(G21.G21Error):
        ctx.assert_write_allowed(Path("/tmp/outside.txt"))


def test_placeholder_source_sha_is_refused(tmp_path):
    manifest_path = _manifest(tmp_path)
    doc = json.loads(manifest_path.read_text())
    doc["studies"]["A"]["source_design"] = doc["studies"]["A"]["receipt"]
    manifest_path.write_text(json.dumps(doc))
    manifest = G21.load_manifest(manifest_path)
    root = tmp_path / "diag2"
    G21.freeze_inputs(root, manifest)
    G21.build_scopes(root, manifest)
    with pytest.raises(G21.G21Error) as exc:
        G21.materialize_study(root, manifest, "A")
    assert exc.value.code == "SOURCE_FILE_DRIFT"


def test_signature_freshness_detects_implementation_drift(tmp_path, monkeypatch):
    manifest = G21.load_manifest(_manifest(tmp_path))
    root = tmp_path / "diag3"
    G21.freeze_inputs(root, manifest)
    G21.snapshot_files(root)
    assert G21.verify_signature_freshness(root)["ok"] is True
    original = G21.implementation_closure

    def shifted():
        closure = original()
        files = [dict(item) for item in closure["files"]]
        files[0]["sha256"] = "0" * 64
        return {"files": files,
                "closure_sha256": G21.G2.field_sha(files)}

    monkeypatch.setattr(G21, "implementation_closure", shifted)
    with pytest.raises(G21.G21Error) as exc:
        G21.verify_signature_freshness(root)
    assert exc.value.code == "STALE_SIGNATURE"


def test_import_rejects_unbound_artifact_reply(tmp_path):
    """Plan §3.1：导入必须核 plan/state/reply/artifact 关联，不只看事件名。"""
    manifest_path = _manifest(tmp_path)
    manifest = G21.load_manifest(manifest_path)
    journal = Path(manifest["studies"]["A"]["journal"])
    lines = journal.read_text(encoding="utf-8").splitlines()
    # 去掉唯一的 reply 事件后重算整条 sha 链（模拟「artifact 无对应 reply」）
    keep = [line for line in lines
            if json.loads(line).get("event") != "reply"]
    rebuilt: "list[str]" = []
    prev = None
    for index, line in enumerate(keep, 1):
        doc = json.loads(line)
        doc["seq"] = index
        doc["prev_sha256"] = prev
        doc["sha256"] = S._event_sha(index, doc["ts_iso"], doc["event"],
                                     doc.get("payload") or {}, prev)
        prev = doc["sha256"]
        rebuilt.append(json.dumps(doc, ensure_ascii=False, sort_keys=True))
    journal.write_text("\n".join(rebuilt) + "\n", encoding="utf-8")
    with pytest.raises(G21.G21Error) as exc:
        G21.plan_import(journal, "A")
    assert exc.value.code == "IMPORT_UNBOUND"
