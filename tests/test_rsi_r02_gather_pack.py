"""RSI-R0.2 gather / pack 测试（AC20/AC31/AC35 的实现面）。

断言：未开跑一律 NOT_RUN_WITH_REASON（不填默认）；run 索引逐件 sha；claim 来自
journal 原样裁决；证据包白名单/判决在包外/manifest 不自指/独立复验能发现篡改。
"""
from __future__ import annotations

import json
import os

import pytest

from orchestrator.rsi_r02 import campaign as C
from orchestrator.rsi_r02 import gather as G
from orchestrator.rsi_r02 import pack as P
from orchestrator.rsi_r02 import stages as SG
from orchestrator.rsi_r02 import state as ST
from tests import rsi_r02_helpers as H


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    H.set_env(tmp_path, monkeypatch)
    c = H.make_campaign(tmp_path)
    H.write_trial_slots(c, (*SG.dev_specs(), *SG.validation_specs()))
    return c


def _trial(ctx, spec, *, status=ST.TRIAL_SUBMITTED, success=True):
    store = ST.StateStore(ctx)
    st = store.start(trial_id=spec.trial_id, instance_id=spec.instance_id,
                     family=spec.family, phase=spec.phase,
                     slot_ids=[], post_cap=spec.post_cap)
    st.independent_success = success
    st.posts_sent = 1
    store.save(st)
    return store.transition(st, status=status)


def _obs(ctx, instance_id="E-dev", run_id="run_abc"):
    base = f"observations/trusted/{instance_id}"
    ctx.write_text(f"{base}/{run_id}.json", '{"x": [0, 1]}\n')
    ctx.write_json(f"{base}/{run_id}.manifest.json",
                   {"trial_id": instance_id, "cell": "c1"})


def test_unstarted_trials_are_not_run_with_reason(ctx):
    doc = G.collect(ctx)
    # 9 个能力 trial + 2 条协议链（D-T4-3：协议 trial 也必须有事实行）
    assert len(doc["trials"]) == 11
    assert {t["phase"] for t in doc["trials"]} == {"dev", "validation", "protocol"}
    assert all(t["effective_status"] == G.NOT_RUN for t in doc["trials"])
    assert doc["denominators"] == {
        "assigned_by_family": {"E": 3, "D": 3, "R": 3},
        "independent_success_by_family": {}}


def test_gather_counts_success_and_indexes_runs(ctx):
    _trial(ctx, SG.dev_specs()[0])
    _obs(ctx)
    doc = G.collect(ctx)
    by_id = {t["trial_id"]: t for t in doc["trials"]}
    assert by_id["E-dev"]["effective_status"] == ST.TRIAL_SUBMITTED
    assert doc["denominators"]["independent_success_by_family"] == {"E": 1}
    assert len(doc["runs"]) == 1
    run = doc["runs"][0]
    assert run["run_id"] == "run_abc" and run["trial_id"] == "E-dev"
    assert len(run["observation_sha256"]) == 64


def test_gather_claims_come_from_judge_events_verbatim(ctx):
    _trial(ctx, SG.dev_specs()[0])
    verdict = {"family": "E", "layer": "L4", "pass": True,
               "verdict": {"claim_status": "SUPPORTS", "claim_value": 1.0}}
    ST.Journal(ctx).append(kind="decision", trial_id="E-dev",
                           action="judge", ok=True, payload={"verdict": verdict})
    claims = G.collect(ctx)["claims"]
    assert claims == [{"trial_id": "E-dev", "family": "E", "layer": "L4",
                       "pass": True, "claim_status": "SUPPORTS",
                       "claim_value": 1.0,
                       "verdict_sha256": claims[0]["verdict_sha256"]}]
    assert len(claims[0]["verdict_sha256"]) == 64


def test_gather_write_manifest_covers_only_gather_files(ctx):
    G.write(ctx)
    manifest = json.loads(
        ctx.relative("gather/manifest.json").read_text(encoding="utf-8"))
    assert set(manifest["files"]) == {
        "gather/facts.json", "gather/runs.jsonl", "gather/claims.json"}


def test_pack_includes_whitelist_and_excludes_private_and_ledger(ctx):
    ctx.write_json("private/truth.json", {"secret_truth": 1})
    ctx.write_text("quota/ledger.jsonl", '{"seq": 1}\n')
    ctx.write_json("closeout/judgement.json", {"process": "COMPLETE"})
    ctx.write_json("quota/approvals/R02-B1.json",
                   {"gate": "G-P", "authorized_posts": 8})
    manifest = P.build(ctx)
    files = set(manifest["files"])
    assert "contract/campaign_context.json" in files
    assert "quota/slots.json" in files
    assert "quota/approvals/R02-B1.json" in files
    assert not any(f.startswith("private/") for f in files)
    assert "quota/ledger.jsonl" not in files
    assert "closeout/judgement.json" not in files
    assert "manifest.json" not in files
    assert P.verify(ctx)["ok"] is True


def test_pack_verify_catches_tamper_and_unlisted(ctx):
    P.build(ctx)
    target = ctx.relative("evidence_pack/quota/slots.json")
    target.write_text("{}", encoding="utf-8")
    out = P.verify(ctx)
    assert out["ok"] is False
    assert any(p.startswith("SHA_MISMATCH: quota/slots.json") for p in out["problems"])
    P.build(ctx)
    extra = ctx.relative("evidence_pack/extra.txt")
    extra.write_text("x\n", encoding="utf-8")
    assert any("UNLISTED: extra.txt" in p for p in P.verify(ctx)["problems"])


def test_gather_missing_observation_is_an_error_not_a_row(ctx):
    """评审 m4：只有 manifest 没有观测原件的 run 不得入索引。

    带 ``observation_sha256: null`` 入表等于把缺件洗成「有这件 run」，下游按
    条数算分母会凭空多一个。无收集器时必须炸；有收集器时逐条记问题。
    """
    ctx.write_json("observations/trusted/E-dev/run_gone.manifest.json",
                   {"trial_id": "E-dev", "cell": "c1"})
    with pytest.raises(FileNotFoundError, match="观测原件缺失"):
        G.runs_index(ctx)
    problems: "list[str]" = []
    runs = G.runs_index(ctx, problems)
    assert runs == []
    assert any("MISSING_OBSERVATION: observations/trusted/E-dev/run_gone.json"
               in p for p in problems)
    doc = G.collect(ctx)
    assert doc["runs"] == [] and doc["problems"]


def test_pack_verify_detects_live_added_and_removed(ctx):
    """评审 m3：包建完后 live 侧增删件，逐件比对看不出来——集合差必须报。"""
    P.build(ctx)
    assert P.verify(ctx)["ok"] is True
    ctx.write_json("analysis/after_build.json", {"late": True})
    out = P.verify(ctx)
    assert out["ok"] is False
    assert "LIVE_ADDED: analysis/after_build.json" in out["problems"]
    P.build(ctx)
    ctx.relative("analysis/after_build.json").unlink()
    out = P.verify(ctx)
    assert out["ok"] is False
    assert "LIVE_REMOVED: analysis/after_build.json" in out["problems"]


def test_pack_refuses_secret_file(ctx):
    # 拼接构造，避免仓库内出现字面密钥（scan_secrets 会对字面量判 HIT）
    token = "AKIA" + "0" * 16
    ctx.write_text("analysis/leak.json", '{"k": "%s"}\n' % token)
    with pytest.raises(P.PackError) as ei:
        P.build(ctx)
    assert "SECRET" in str(ei.value)


def test_pack_refuses_symlink(ctx, tmp_path):
    """评审 m2：符号链接**拒绝**（不再静默跳过），有收集器时逐条记账。"""
    target = tmp_path / "outside.txt"
    target.write_text("x\n", encoding="utf-8")
    link = ctx.relative("analysis/link.txt")
    os.symlink(target, link)
    with pytest.raises(P.PackError, match="符号链接"):
        P.select(ctx)
    problems: "list[str]" = []
    files = P.select(ctx, problems=problems)
    assert "analysis/link.txt" not in files
    assert any("SYMLINK_IN_LIVE: analysis/link.txt" in p for p in problems)
