"""RSI-G2.1 E2E：合成封存件 → 真实 loop/read/submit → judge（fixture_only）。"""
from __future__ import annotations

import json
from pathlib import Path

from orchestrator.rsi_r1 import g2_cli as CLI
from orchestrator.rsi_r1 import g21_bridge as G21
from tests.fixtures import g21_synthetic as SYN

TRACE_KINDS = {"request_serialized", "response_raw", "action_parsed",
               "dispatch_return", "clipped_view", "claim_persisted"}


def _prepare(tmp_path: Path) -> tuple[Path, Path]:
    doc = SYN.replay_manifest_doc(tmp_path)
    manifest = tmp_path / "replay_manifest.json"
    manifest.write_text(json.dumps(doc, ensure_ascii=False, sort_keys=True),
                        encoding="utf-8")
    return manifest, tmp_path / "diag"


def test_synthetic_complete_study_reaches_verified(tmp_path):
    """完整 NC 集合的合成源：envelope→Request→read（多页）→submit 全通。"""
    manifest, root = _prepare(tmp_path)
    assert CLI.main(["bridge-init", "--root", str(root),
                     "--inputs", str(manifest)]) == 0
    assert CLI.main(["bridge-run", "--root", str(root), "--study", "A"]) == 0
    report = json.loads((root / "reports/A-bridge.json").read_text())
    assert report["ok"] is True
    assert report["study_state"] == "VERIFIED"
    assert report["source"]["status"] == G21.SOURCE_READY
    assert report["envelope_bytes"] <= G2_LIMIT()
    # judge 各层
    judge = json.loads((root / "judge/report_A.json").read_text())
    assert judge["verdict"] == "JUDGE_OK"
    assert all(judge["layers"].values())
    # 五面
    faces = json.loads((root / "submit/A/five_faces.json").read_text())
    assert faces["ok"] is True
    shas = [faces["faces"][name]["sha256"] for name in faces["faces"]]
    assert len(set(shas)) == len(shas)
    assert faces["checks"]["render_consistent"] is True
    assert faces["checks"]["origin_present"] is True
    # trace 事件族 + envelope 在最终 request 中逐字在场
    rows = [json.loads(line) for line in
            (root / "trace/A/events.jsonl").read_text().splitlines()
            if line.strip()]
    kinds = {row["event_kind"] for row in rows}
    assert TRACE_KINDS <= kinds
    requests = [row for row in rows if row["event_kind"] == "request_serialized"]
    assert len(requests) >= 3
    envelope = json.loads((root / "public/A/envelope.json").read_text())
    text = G21.G2.canonical(envelope).decode("utf-8")
    payload = json.loads((root / requests[-1]["artifact_ref"]).read_text())
    joined = "\n".join(message["content"] for message in payload["messages"])
    assert text in joined
    # 多页读取：>=2 个 read 动作
    actions = [row for row in rows if row["event_kind"] == "action_parsed"]
    assert sum(1 for row in actions
               if b"read_result_artifact" in
               (root / row["artifact_ref"]).read_bytes()) >= 2
    # registry 指向真实存在且逐字节相等的候选副本
    registry = json.loads((root / "state/g2_artifacts.json").read_text())
    for artifact in registry["artifacts"]:
        path = root / artifact["relative_path"]
        assert path.is_file()
        assert G21.sha_file(path) == artifact["sha256"]
        assert artifact["relative_path"].startswith("analysis/")
    # 零 POST：消费者环境只有 PATH，回合日志逐条记录
    turns = [json.loads(line) for line in
             (root / "logs/A-turns.jsonl").read_text().splitlines() if line.strip()]
    assert turns and all(turn["env_keys"] == ["PATH"] for turn in turns)
    assert [turn["action"] for turn in turns] == [
        "read_result_artifact", "read_result_artifact", "submit_claim"]


def test_five_faces_detect_tampered_reply_bytes(tmp_path):
    """原件（reply 文本）被改写 ⇒ reply 面与事件 sha 不符。"""
    manifest, root = _prepare(tmp_path)
    assert CLI.main(["bridge-init", "--root", str(root),
                     "--inputs", str(manifest)]) == 0
    assert CLI.main(["bridge-run", "--root", str(root), "--study", "A"]) == 0
    faces = json.loads((root / "submit/A/five_faces.json").read_text())
    reply_rel = faces["faces"]["raw_reply"]["relpath"]
    path = root / reply_rel
    path.write_text(path.read_text(encoding="utf-8") + " ")
    again = G21.five_faces(G21.G21Context(root), "A")
    assert again["ok"] is False
    assert again["checks"]["reply_binding"] is False


def G2_LIMIT() -> int:
    return 16 * 1024


def test_closure_checker_rejects_self_asserted_ready(tmp_path):
    """独立复核器不信 closeout 自述：伪造 READY 必须被原件复核否掉。"""
    import subprocess
    import sys
    manifest, root = _prepare(tmp_path)
    assert CLI.main(["bridge-init", "--root", str(root),
                     "--inputs", str(manifest)]) == 0
    assert CLI.main(["bridge-run", "--root", str(root), "--study", "A"]) == 0
    assert CLI.main(["bridge-raw-check", "--root", str(root)]) == 0
    assert CLI.main(["bridge-capacity", "--root", str(root),
                     "--write-matrix"]) == 0
    # 伪造一个「全 PASS / READY」的 closeout（缺 isolation 与签名新鲜度）
    fake = {"schema": "simpraxis.rsi_g21.closeout.v1", "schema_version": 1,
            "overall": "G2.1_ENGINEERING_READY_FOR_NEW_DESIGN",
            "axes": {"content_integrity": "PASS",
                     "visibility_observability": "PASS",
                     "submit_e2e": "PASS", "raw_checker": "PASS",
                     "tokenizer_capacity": "PASS",
                     "negative_required_set": "PASS", "isolation": "PASS",
                     "legacy_compat": "PASS", "signature_freshness": "PASS"},
            "gaps": [],
            "scientific_claim": "NONE（只出工程轴；不晋升 R1/R2）"}
    (root / "decision").mkdir(exist_ok=True)
    (root / "decision" / "g21_closeout.json").write_text(json.dumps(fake))
    proc = subprocess.run(
        [sys.executable, "scripts/verification/check_rsi_g21_closure.py",
         "--root", str(root), "--repo", str(Path(__file__).resolve().parents[1])],
        capture_output=True, text=True,
        cwd=str(Path(__file__).resolve().parents[1]))
    assert proc.returncode == 1
    report = json.loads(proc.stdout)
    assert report["problems"]
    assert report["consistent"] is False
    text = " ".join(report["problems"])
    assert "isolation.report 缺失" in text or "isolation" in text
    assert any(name.startswith("closeout 轴") for name in report["problems"])


def test_bridge_records_consumer_label_and_resources(tmp_path):
    manifest, root = _prepare(tmp_path)
    assert CLI.main(["bridge-init", "--root", str(root),
                     "--inputs", str(manifest)]) == 0
    assert CLI.main(["bridge-run", "--root", str(root), "--study", "A"]) == 0
    faces = json.loads((root / "submit/A/five_faces.json").read_text())
    assert faces["consumer_label"] == "maintainer_scripted_consumer"
    assert faces["claim_producer"] == "maintainer_scripted_consumer"
    assert faces["counts_as_candidate_success"] is False
    resources = json.loads(
        (root / "logs" / "bridge-run-A-resources.json").read_text())
    assert resources["command"] == "bridge-run-A"
    assert resources["wall_s"] > 0
    assert resources["problems_enforced"] == []
    # 嵌入 pytest 时宿主峰值只记录不判（Plan §3.3）
    assert set(resources["problems_observed"]) - set(
        resources["problems_enforced"]) == {
        p for p in resources["problems_observed"] if p.startswith("peak_rss=")}
    assert resources["limits"]["wall_s"] > 0
