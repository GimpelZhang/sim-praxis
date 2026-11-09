"""T8 结案的零付费护栏测试（AC02 / AC10 / AC26 / AC33-证据指针 等）。

这些测试只跑纯逻辑，不碰战场目录、不发网、不计费；战役现场机判见
``scripts/verification/check_rsi_r0_closure.py``。
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def t8():
    return _load("rsi_r0_t8_closeout", "scripts/rsi_r0/t8_closeout.py")


@pytest.fixture(scope="module")
def judge():
    return _load("rsi_r0_closure_judge",
                 "scripts/verification/check_rsi_r0_closure.py")


# ---------------------------------------------------------------------------
# AC02：环境注入 rc != 0 必须把 G-M 打成 BLOCKED（此前无负测）
# ---------------------------------------------------------------------------
def test_env_injection_nonzero_blocks_gate_m():
    """``admission.build_gates(env_rc)``：rc!=0 ⇒ G-M 状态 BLOCKED、三项检查全假。

    注意口径：admission 写的是 ``G-M_model_provider_admission`` 全名键
    （战役 ``gates.json`` 里的 ``G-M`` 是 runner 侧的短键），本测钉的是
    admission 这条**准入车道**的拒绝行为。
    """
    adm = _load("rsi_r0_admission", "scripts/rsi_r0/admission.py")

    blocked = adm.build_gates(2)["G-M_model_provider_admission"]
    assert blocked["state"] == "BLOCKED"
    assert blocked["checks"]["env_injection_rc_zero"] is False
    assert blocked["checks"]["base_url_matches_stage_config"] is False

    ok = adm.build_gates(0)["G-M_model_provider_admission"]
    assert ok["state"] == "SATISFIED_PENDING_PROTOCOL_PROBE"
    assert ok["checks"]["env_injection_rc_zero"] is True


# ---------------------------------------------------------------------------
# AC10：输入帽越界必须在成本件里被**如实计数**，不吞不追认
# ---------------------------------------------------------------------------
def _contract(cap_in=20000, cap_out=8192):
    return {
        "currency": "USD",
        "pricing_basis": "test",
        "price_per_1m_input_usd": 1.0,
        "price_per_1m_output_usd": 4.0,
        "input_tokens_cap": cap_in,
        "output_tokens_cap": cap_out,
        "usd_ceiling": 100.0,
        "worst_case_per_request_usd": 0.052768,
    }


def _call(seq, tin, tout, batch="B", ok=True):
    return {"event": "call", "seq": seq, "ok": ok, "tokens_in": tin,
            "tokens_out": tout, "batch_id": batch, "batch_used_after": seq,
            "batch_approved": 24, "task": 1, "stage": "coding_debug",
            "model": "m"}


def test_ledger_stats_flags_input_cap_breaches(t8):
    facts = {
        "contract": _contract(),
        "ledger": [_call(1, 19999, 10), _call(2, 22071, 10), _call(3, -1, None, ok=False)],
        "attempts": [],
    }
    cost = t8.ledger_stats(facts)
    breaches = cost["requests_over_input_cap"]
    assert [b["seq"] for b in breaches] == [2]
    assert breaches[0]["over_by"] == 2071
    assert cost["requests_over_output_cap"] == []
    assert cost["max_tokens_in"] == 22071
    # 失败行 token 缺失不能被读成 0
    assert cost["calls"] == 3 and cost["fail"] == 1


def test_ledger_stats_attempts_counts_posts_by_distinct_request(t8):
    facts = {
        "contract": _contract(),
        "ledger": [_call(1, 100, 10)],
        "attempts": [
            {"request_id": "r1", "state": "PREPARED", "attempts_http": 0},
            {"request_id": "r1", "state": "SENT", "attempts_http": 1},
            {"request_id": "r1", "state": "SETTLED_OK", "attempts_http": 1},
        ],
    }
    cost = t8.ledger_stats(facts)
    assert cost["attempts"]["http_posts"] == 1  # 按去重 request_id，不按行数
    assert cost["attempts"]["settled_ok"] == 1
    assert cost["attempts"]["posts_match_ledger_calls"] is True


# ---------------------------------------------------------------------------
# 验收矩阵：AC01–AC34 全覆盖、状态合法、AC10 不得被判 PASS
# ---------------------------------------------------------------------------
def test_closure_matrix_covers_all_acs_and_never_passes_ac10(t8, tmp_path):
    facts = {"campaign": tmp_path}
    cost = {"requests_over_input_cap": [
        {"seq": 23, "tokens_in": 20807, "over_by": 807, "pct_over": 4.04}]}
    matrix = t8.ac_matrix(facts, {"evidence_pack": None}, cost)
    ids = [it["id"] for it in matrix["items"]]
    assert ids == [f"AC{i:02d}" for i in range(1, 35)]
    legal = {"PASS", "PARTIAL", "NOT_RUN_WITH_REASON"}
    assert all(it["status"] in legal for it in matrix["items"])
    ac10 = next(it for it in matrix["items"] if it["id"] == "AC10")
    assert ac10["status"] == "PARTIAL"
    assert "20807" in ac10["note"]  # 越界事实必须写进矩阵，不能沉默
    # 提前阻塞的验证项只能是 NOT_RUN_WITH_REASON
    for ac in ("AC28", "AC29"):
        it = next(x for x in matrix["items"] if x["id"] == ac)
        assert it["status"] == "NOT_RUN_WITH_REASON"
    # 自哈希可复算
    from orchestrator.rsi_r0.trusted import sha256_of
    assert matrix["matrix_sha256"] == sha256_of(
        {k: v for k, v in matrix.items() if k != "matrix_sha256"})


# ---------------------------------------------------------------------------
# 证据包：必需件缺失即 fail-closed；符号链接一律拒绝（H-11）
# ---------------------------------------------------------------------------
def test_pack_refuses_when_a_required_file_is_absent(t8, tmp_path):
    campaign = tmp_path / "campaign"
    campaign.mkdir()
    with pytest.raises(t8.CloseoutError):
        t8.build_pack(campaign, campaign / "closeout" / "evidence_pack")


def test_pack_walk_refuses_symlinked_evidence(t8, tmp_path):
    base = tmp_path / "runs"
    base.mkdir()
    target = tmp_path / "outside.txt"
    target.write_text("x", encoding="utf-8")
    (base / "link.txt").symlink_to(target)
    with pytest.raises(t8.CloseoutError):
        list(t8._iter_packable(base))


# ---------------------------------------------------------------------------
# 机判：证据指针四类与符号缺席必须为 False
# ---------------------------------------------------------------------------
def test_judge_pointer_kinds(judge, tmp_path):
    camp = tmp_path / "c"
    camp.mkdir()
    (camp / "doc.json").write_text(json.dumps(
        {"a": {"b": [{"ok": True}]}}), encoding="utf-8")
    (camp / "plain.txt").write_text("x", encoding="utf-8")

    assert judge.verify_pointer(camp, {"kind": "campaign_file",
                                       "rel": "plain.txt"})[0] is True
    assert judge.verify_pointer(camp, {"kind": "campaign_file",
                                       "rel": "nope.txt"})[0] is False
    assert judge.verify_pointer(camp, {"kind": "campaign_json_path",
                                       "rel": "doc.json",
                                       "pointer": "a/b/0/ok"})[0] is True
    assert judge.verify_pointer(camp, {"kind": "campaign_json_path",
                                       "rel": "doc.json",
                                       "pointer": "a/b/0/missing"})[0] is False
    assert judge.verify_pointer(camp, {"kind": "repo_file",
                                       "rel": "scripts/rsi_r0/t8_closeout.py",
                                       "symbol": "build_pack"})[0] is True
    # 缺席符号必须判 False（防止矩阵抄了一个不存在的测试名）
    assert judge.verify_pointer(camp, {"kind": "repo_file",
                                       "rel": "scripts/rsi_r0/t8_closeout.py",
                                       "symbol": "no_such_symbol_xyz"})[0] is False
    assert judge.verify_pointer(camp, {"kind": "unknown"})[0] is False


def test_judge_rejects_a_verdict_file_inside_the_pack(judge, tmp_path):
    """无循环的机判口径：包内出现判决件名即 problem（负例）。"""
    j = judge.Judge()
    result = {"evidence_pack": {"manifest_sha256": "deadbeef"}}
    for name in ("result.json", "human_report.md", "signs"):
        (tmp_path / "closeout" / "evidence_pack").mkdir(parents=True, exist_ok=True)
    pack = tmp_path / "closeout" / "evidence_pack"
    (pack / "result.json").write_text("{}", encoding="utf-8")
    (pack / "manifest.json").write_text(json.dumps({
        "files": [{"path": "result.json", "sha256": "x", "size": 2}],
        "n_files": 1, "total_bytes": 2, "git_commit": "",
    }), encoding="utf-8")
    judge.check_no_circularity(j, tmp_path, result, "")
    codes = {p["code"] for p in j.problems}
    assert "pack.contains_no_verdict_artifacts" in codes
    assert "pack.report_cites_manifest_sha" in codes
