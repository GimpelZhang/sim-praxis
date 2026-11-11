"""RSI-R1 T5：结案产物/证据包/独立复算（AC26 产物侧 + AC27 判据先存在）。

全部离线：临时 campaign 根；独立复算器以**外部进程**跑真实脚本
（``scripts/verification/check_rsi_r1_closure.py``），不 mock。
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from orchestrator.rsi_r1 import closure as CL
from orchestrator.rsi_r1 import contracts as C
from orchestrator.rsi_r1 import experiments as E
from orchestrator.rsi_r1 import human_events as HE
from orchestrator.rsi_r1 import judge as J
from orchestrator.rsi_r1 import replay as RP
from tests import rsi_r1_helpers as H

KEEPER = "b" * 64
LOCK_SHA = "c" * 64


# ---------------------------------------------------------------------------
# 合成「完整 campaign」：两 study 的合法确认批 + judge + replay + 账本
# ---------------------------------------------------------------------------

def _a_record(spec: E.ExperimentSpec, *, displacement: float) -> dict:
    times = [i * 0.001 for i in range(6)]
    return {"time_s": times,
            "qpos": [[displacement if i == len(times) - 1 else 0.0, 0.0]
                     for i in range(len(times))],
            "qvel": [[0.5, 0.0] for _ in times],
            "contact_dim_per_step": [3] * len(times),
            "run_id": spec.run_id, "spec_hash": spec.spec_hash,
            "block_id": spec.block_id}


def _b_record(spec: E.ExperimentSpec, *, rmse: float) -> dict:
    amp = float(spec.physical_params["target_amplitude_rad"])
    times = [i * 0.5 for i in range(9)]
    return {"time_s": times,
            "qpos": [[rmse * amp, 0.0] for _ in times],
            "target_q": [[0.0, 0.0] for _ in times],
            "instability_events": [],
            "run_id": spec.run_id, "spec_hash": spec.spec_hash,
            "block_id": spec.block_id}


def _meta(spec: E.ExperimentSpec, *, tag: str, root: str) -> dict:
    return {"run_id": spec.run_id, "spec_hash": spec.spec_hash,
            "condition_id": spec.condition_id, "initial_x_m": 0.0,
            "execution_id": f"exec-{tag}-{spec.run_id}",
            "container_id": f"ctr-{tag}-{spec.run_id}",
            "started_at_utc": f"2026-11-11T0{tag[-1]}:00:00Z",
            "raw_sha256": tag[-1] * 64, "root": root, "cache_hit": False}


def _plan(study: str):
    doc = H.plan_doc(study)
    if study == "A":
        doc["conditions"] = [c for c in doc["conditions"] if c["id"] != "heavy"]
    return H.doc_of(doc)


def _artifacts(plan, manifest) -> "dict[str, dict]":
    specs = J.build_specs(plan, manifest)
    out = {}
    for run_id, spec in specs.items():
        if plan.study_id == "A":
            disp = -0.01 if spec.condition_id == "low_mu" else 0.0
            record = _a_record(spec, displacement=disp)
        else:
            record = _b_record(spec, rmse=1.02 if spec.condition_id == "base" else 1.0)
        out[run_id] = {"record": record,
                       "meta": {**_meta(spec, tag="orig1", root="/mnt/r1/orig"),
                                "condition_id": spec.condition_id}}
    return out


def _submission(plan, manifest, report) -> dict:
    by_contrast = {c["id"]: c for c in report["layers"]["scientific"]}
    claims = []
    for hid, h in sorted({h.id: h for h in plan.hypotheses}.items()):
        c = by_contrast[h.contrast_id]
        claims.append({"hypothesis_id": hid, "proposition": c["proposition"],
                       "metric_id": c["metric_id"], "delta": c["delta"],
                       "estimate": c["effect"], "lower": c["lower"],
                       "upper": c["upper"]})
    return {"schema": J.SUBMISSION_SCHEMA, "study_id": plan.study_id,
            "plan_sha256": plan.sha256,
            "confirm_manifest_sha256": manifest.sha256,
            "primary_contrast_id": plan.primary.id, "claims": claims}


def full_campaign(tmp_path: Path) -> "tuple[object, dict]":
    """两 study 全绿（judge JUDGE_OK + replay ok）的完整 campaign 根。"""
    ctx = H.make_ctx(tmp_path / "campaign")
    for name in ("contract", "state", "confirm", "judge", "replay", "claim",
                 "deliverable", "quota", "human_events", "protocol"):
        (ctx.root / name).mkdir(parents=True, exist_ok=True, mode=0o700)
    budget = C.default_budget(H.ENVELOPE)
    ctx.write_json("contract/budget.json", budget.to_dict())
    ctx.write_json("contract/envelope.json", {
        "schema": "simpraxis.rsi_r1.envelope.v1", "schema_version": 1,
        "envelope_id": H.ENVELOPE, "budget_sha256": budget.sha256})
    CL.ensure_ledger_file(ctx)
    studies: "dict[str, dict]" = {}
    for study in ("A", "B"):
        plan = _plan(study)
        manifest = E.allocate_confirm(plan, KEEPER)
        arts = _artifacts(plan, manifest)
        first = J.judge_study(plan=plan, manifest=manifest, artifacts=arts)
        submission = _submission(plan, manifest, first)
        report = J.judge_study(plan=plan, manifest=manifest, artifacts=arts,
                               submission=submission)
        assert report["verdict"] == "JUDGE_OK"
        specs = J.build_specs(plan, manifest)
        replays = {rid: {"record": a["record"],
                         "meta": _meta(specs[rid], tag="repl2",
                                       root="/mnt/r1/replay")}
                   for rid, a in arts.items()}
        comparison = RP.compare_study(plan=plan, specs=specs,
                                      originals=arts, replays=replays)
        assert comparison["ok"] is True, comparison["problems"][:3]
        CL.write_confirm_manifest(ctx, study, manifest)
        CL.write_judge_report(ctx, study, report)
        CL.write_replay_comparison(ctx, study, comparison)
        CL.write_claim_submission(ctx, study, submission)
        studies[study] = CL.study_entry(judge_report=report,
                                        comparison=comparison)
    CL.write_deliverable(ctx, studies=studies, lock_sha256=LOCK_SHA)
    return ctx, studies


# ---------------------------------------------------------------------------
# 账本：零付费可自证（文件必须存在、绝不造假行）
# ---------------------------------------------------------------------------

def test_ensure_ledger_file_is_empty_and_private(tmp_path):
    ctx = H.make_ctx(tmp_path / "c")
    (ctx.root / "quota").mkdir(parents=True, mode=0o700)
    path = CL.ensure_ledger_file(ctx)
    assert path.exists() and path.stat().st_size == 0
    assert path.stat().st_mode & 0o777 == 0o600
    CL.ensure_ledger_file(ctx)                       # 幂等
    assert path.stat().st_size == 0
    assert CL.ledger_summary(ctx)["calls"] == 0
    assert CL.ledger_summary(ctx)["rows"] == 0


def test_ledger_summary_counts_calls_only(tmp_path):
    ctx = H.make_ctx(tmp_path / "c")
    (ctx.root / "quota").mkdir(parents=True, mode=0o700)
    path = CL.ensure_ledger_file(ctx)
    rows = [
        {"event": "selfcheck", "seq": 1, "ts_iso": "2026-11-11T00:00:00Z"},
        {"event": "call", "seq": 2, "ts_iso": "2026-11-11T00:01:00Z",
         "ok": True, "lane": "probe"},
        {"event": "call", "seq": 3, "ts_iso": "2026-11-11T00:02:00Z",
         "ok": False, "lane": "probe"},
    ]
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    s = CL.ledger_summary(ctx)
    assert s["rows"] == 3 and s["calls"] == 2 and s["selfcheck"] == 1
    assert s["ok"] == 1 and s["failed"] == 1
    assert s["calls_by_lane"] == {"probe": 2}


# ---------------------------------------------------------------------------
# 逐 study 产物
# ---------------------------------------------------------------------------

def test_study_writers_roundtrip_and_validate_study(tmp_path):
    ctx = H.make_ctx(tmp_path / "c")
    (ctx.root / "confirm").mkdir(parents=True, mode=0o700)
    doc = {"study_id": "A", "x": 1}
    assert CL.write_judge_report(ctx, "A", doc) == doc
    assert ctx.read_json("judge/report_A.json") == doc
    assert ctx.relative("judge/report_A.json").stat().st_mode & 0o777 == 0o600
    with pytest.raises(CL.ClosureError) as ei:
        CL.write_judge_report(ctx, "A", {"study_id": "B"})
    assert ei.value.code == "STUDY_MISMATCH"
    with pytest.raises(CL.ClosureError) as ei:
        CL.write_replay_comparison(ctx, "C", {"study_id": "C"})
    assert ei.value.code == "UNKNOWN_STUDY"


def test_study_entry_only_complete_when_both_axes_pass():
    ok_report = {"study_id": "A", "verdict": "JUDGE_OK",
                 "layers": {"evidence_valid": True, "claim_status": "MATCH",
                            "scientific": [{"id": "primary", "status": "SUPPORTED"}]}}
    bad_report = {"study_id": "A", "verdict": "JUDGE_REJECT",
                  "layers": {"evidence_valid": False, "scientific": []}}
    ok_cmp = {"study_id": "A", "ok": True, "n_replayed": 3, "problems": []}
    bad_cmp = {"study_id": "A", "ok": False, "problems": ["NOT_REPLAYED"]}
    assert CL.study_entry(judge_report=ok_report, comparison=ok_cmp)["claim"] == "COMPLETE"
    assert CL.study_entry(judge_report=bad_report, comparison=ok_cmp)["claim"] == "VALID_FAIL"
    assert CL.study_entry(judge_report=ok_report, comparison=bad_cmp)["claim"] == "VALID_FAIL"
    assert CL.study_entry(judge_report=None, comparison=ok_cmp)["claim"] == "BLOCKED"
    assert CL.study_entry(judge_report=ok_report, comparison=None)["claim"] == "BLOCKED"


# ---------------------------------------------------------------------------
# 交付件：不许无支撑 COMPLETE、不许把未知人工时间填 0
# ---------------------------------------------------------------------------

def test_deliverable_refuses_unsupported_complete(tmp_path):
    ctx = H.make_ctx(tmp_path / "c")
    with pytest.raises(CL.ClosureError) as ei:
        CL.write_deliverable(ctx, studies={"A": {"claim": "COMPLETE",
                                                 "judge_ok": True,
                                                 "replay_ok": False}},
                             lock_sha256=LOCK_SHA,
                             human_time={"complete": True})
    assert ei.value.code == "CLAIM_UNSUPPORTED"


def test_deliverable_refuses_unknown_human_time(tmp_path):
    ctx = H.make_ctx(tmp_path / "c")
    log = HE.HumanEventLog(ctx, clock=lambda: "2026-11-11T00:00:00Z")
    log.begin(kind="approval", actor="user", reason="未结束")
    with pytest.raises(HE.HumanEventError) as ei:
        CL.write_deliverable(ctx, studies={}, lock_sha256=LOCK_SHA)
    assert ei.value.code == "UNKNOWN_HUMAN_TIME"
    assert not ctx.exists(CL.DELIVERABLE_RELPATH)


def test_deliverable_records_ledger_cost(tmp_path):
    ctx = H.make_ctx(tmp_path / "c")
    CL.ensure_ledger_file(ctx)
    doc = CL.write_deliverable(ctx, studies={}, lock_sha256=LOCK_SHA,
                               human_time={"complete": True,
                                           "known_seconds_total": 0.0})
    assert doc["cost"]["calls"] == 0
    assert doc["schema"] == CL.DELIVERABLE_SCHEMA
    assert ctx.read_json(CL.DELIVERABLE_RELPATH)["lock_sha256"] == LOCK_SHA
    with pytest.raises(CL.ClosureError) as ei:
        CL.write_deliverable(ctx, studies={}, lock_sha256="short")
    assert ei.value.code == "LOCK_SHA_INVALID"


# ---------------------------------------------------------------------------
# 证据包
# ---------------------------------------------------------------------------

def test_pack_lists_hashes_and_refuses_extras(tmp_path):
    ctx = H.make_ctx(tmp_path / "c")
    (ctx.root / "contract").mkdir(parents=True, mode=0o700)
    (ctx.root / "state/replies").mkdir(parents=True, mode=0o700)
    ctx.write_json("contract/budget.json", {"x": 1})
    ctx.write_text("state/replies/r-1.txt", "正文")
    manifest = CL.build_evidence_pack(ctx, extras={})
    files = manifest["files"]
    assert set(files) == {"contract/budget.json", "state/replies/r-1.txt"}
    assert manifest["n_files"] == 2
    assert (ctx.root / "evidence_pack/contract/budget.json").read_bytes() == \
        ctx.relative("contract/budget.json").read_bytes()
    assert (ctx.root / "evidence_pack/evidence_manifest.json").stat().st_mode & 0o777 == 0o600
    assert CL.verify_pack(ctx.root / "evidence_pack")["ok"] is True
    with pytest.raises(CL.ClosureError) as ei:
        CL.build_evidence_pack(ctx, extras={})
    assert ei.value.code == "PACK_EXISTS"


def test_pack_includes_checker_by_default(tmp_path):
    ctx = H.make_ctx(tmp_path / "c")
    manifest = CL.build_evidence_pack(ctx)
    assert "checker/check_rsi_r1_closure.py" in manifest["files"]


def test_pack_refuses_symlink_and_oversize(tmp_path, monkeypatch):
    ctx = H.make_ctx(tmp_path / "c")
    (ctx.root / "contract").mkdir(parents=True, mode=0o700)
    target = tmp_path / "outside.txt"
    target.write_text("x", encoding="utf-8")
    (ctx.root / "contract/link.json").symlink_to(target)
    with pytest.raises(CL.ClosureError) as ei:
        CL.build_evidence_pack(ctx, extras={})
    assert ei.value.code == "PACK_SYMLINK"
    (ctx.root / "contract/link.json").unlink()
    ctx.write_text("contract/big.json", "yy")
    monkeypatch.setattr(CL, "MAX_PACK_FILE_BYTES", 1)
    with pytest.raises(CL.ClosureError) as ei:
        CL.build_evidence_pack(ctx, extras={})
    assert ei.value.code == "PACK_FILE_TOO_LARGE"


def test_verify_pack_detects_tamper_and_unlisted(tmp_path):
    ctx = H.make_ctx(tmp_path / "c")
    (ctx.root / "contract").mkdir(parents=True, mode=0o700)
    ctx.write_json("contract/budget.json", {"x": 1})
    CL.build_evidence_pack(ctx, extras={})
    pack = ctx.root / "evidence_pack"
    (pack / "contract/budget.json").write_text('{"x": 2}\n', encoding="utf-8")
    (pack / "contract/extra.json").write_text("{}\n", encoding="utf-8")
    out = CL.verify_pack(pack)
    assert out["ok"] is False
    joined = "\n".join(out["problems"])
    assert "contract/budget.json:sha" in joined
    assert "contract/extra.json:unlisted" in joined


# ---------------------------------------------------------------------------
# 独立复算器：真进程、真 PASS、真 FAIL
# ---------------------------------------------------------------------------

def test_closure_judge_passes_on_full_campaign(tmp_path):
    ctx, studies = full_campaign(tmp_path)
    assert studies["A"]["claim"] == "COMPLETE"
    assert studies["B"]["claim"] == "COMPLETE"
    CL.build_evidence_pack(ctx, extras={})
    judged = CL.run_closure_judge(ctx)
    assert judged["ok"] is True, judged["problems"][:5]
    assert judged["verdict"] == "PASS"
    assert judged["problems"] == []
    assert judged["returncode"] == 0
    assert len(judged["validator_source_sha256"]) == 64
    assert judged["run_at_utc"].endswith("Z")
    # 包外判决件：独立复算器自写，且与返回件同源
    on_disk = ctx.read_json(CL.JUDGE_REPORT_RELPATH)
    assert on_disk["verdict"] == "PASS"
    assert on_disk["validator_source_sha256"] == judged["validator_source_sha256"]
    # 零付费：账本 0 行、0 实呼
    assert CL.ledger_summary(ctx)["calls"] == 0


def test_closure_judge_fails_on_tampered_report(tmp_path):
    ctx, _ = full_campaign(tmp_path)
    report = ctx.read_json("judge/report_A.json")
    report["layers"]["evidence_valid"] = False        # 改一层，verdict 仍 JUDGE_OK
    ctx.write_json("judge/report_A.json", report)
    judged = CL.run_closure_judge(ctx, pack=False)
    assert judged["ok"] is False and judged["verdict"] == "FAIL"
    ids = [c["id"] for c in judged["checks"] if not c["ok"]]
    assert any("VERDICT-CONSISTENT" in i for i in ids)


def test_closure_judge_reports_missing_pack(tmp_path):
    ctx, _ = full_campaign(tmp_path)
    judged = CL.run_closure_judge(ctx, pack=tmp_path / "nope")
    assert judged["ok"] is False
    assert any("P-MANIFEST" in p for p in judged["problems"])


def test_checker_is_stdlib_only():
    """AC27「独立判据」：复算器不得 import orchestrator/第三方。"""
    src = CL.checker_path().read_text(encoding="utf-8")
    body = src.split("from __future__", 1)[1]
    imports = [ln.strip() for ln in body.splitlines()
               if ln.startswith(("import ", "from "))]
    assert imports, "checker 没有 import？"
    allowed = ("argparse", "hashlib", "json", "os", "sys", "datetime",
               "decimal", "pathlib", "__future__")
    for ln in imports:
        mod = ln.split()[1].split(".")[0]
        assert mod in allowed, f"非标准库依赖：{ln}"
