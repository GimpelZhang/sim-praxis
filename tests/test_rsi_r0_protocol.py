"""RSI-R0 T5：协议六次探针、成本合同、产物化门事实、LOCK 取代（AC25 + G-P 原料）。

全部零发网零容器：假网关自己往本 campaign 的账本里写 ``seq`` 行（形状与真账本
一致：整数 seq + ok），假工具在宿主侧读标记文件冒充容器内 ``read_file``。真实
容器与真实供应商只在正式六次探针里出现（CLI ``protocol``）。
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from orchestrator.common import runtime_paths as rp
from orchestrator.rsi_r0 import budget_contract, gates, protocol_probe as pp, seals
from orchestrator.rsi_r0 import runner as rn
from orchestrator.rsi_r0.cli import EXIT_OK, EXIT_REFUSED, main as cli_main
from orchestrator.rsi_r0.contracts import sha256_bytes, sha256_of

NONCE_RE = re.compile(r"必须原样填 ([0-9a-f]{16})")
MARKER_IN_PROMPT = re.compile(r"非秘密测试数据）：\n([0-9a-f]{32})")


@pytest.fixture()
def campaign(tmp_path, monkeypatch):
    saved = {k: os.environ.get(k) for k in
             ("SIMPRAXIS_RSI_R0_ROOT", "SIMPRAXIS_RSI_R0_CAMPAIGN")}
    path = tmp_path / "rsi_r0" / "t5"
    yield path
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def _append_ledger(row: dict) -> None:
    path = rp.rsi_r0_ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


class FakeGateway:
    """最小网关替身：只写账本行、回固定答案，不触网。"""

    def __init__(self, *, attempts_http: int = 1, block: bool = False,
                 fail_on_call: int | None = None, answer_override=None):
        self.attempts_http = attempts_http
        self.block = block
        self.fail_on_call = fail_on_call
        self.answer_override = answer_override
        self.calls: list[str] = []
        self.next_seq = 1

    def model_id(self, stage: str) -> str:
        return "fake-model-t5"

    def answer(self, prompt: str) -> str:
        if self.answer_override is not None:
            return self.answer_override(prompt)
        m = NONCE_RE.search(prompt)
        if m:
            return json.dumps({"kind": "ready", "nonce": m.group(1)})
        if "全 部 非 法" in prompt or "一律非法" in prompt:
            return json.dumps({"action": "read_file", "path": "/work/marker.txt"})
        m = MARKER_IN_PROMPT.search(prompt)
        if m:
            return json.dumps({"kind": "marker", "value": m.group(1)})
        raise AssertionError(f"未预期的提示词：{prompt[:80]!r}")

    def call(self, stage, messages, *, max_tokens=512, temperature=0.2,
             timeout=120, thinking=None, capture=None):
        prompt = messages[-1]["content"]
        self.calls.append(prompt)
        if self.fail_on_call is not None and len(self.calls) == self.fail_on_call:
            if self.block:
                _append_ledger({"event": "preflight_blocked", "seq": None, "ok": False})
                raise RuntimeError("quota preflight blocked")
            _append_ledger({"seq": self.next_seq, "ok": False, "stage": stage})
            self.next_seq += 1
            raise RuntimeError("HTTP 500")
        seq = self.next_seq
        self.next_seq += 1
        _append_ledger({"seq": seq, "ok": True, "stage": stage, "batch_id": "R0-P"})
        if capture is not None:
            capture.update({"stop_reason": "end_turn", "provider_model": "fake-model-t5",
                            "provider_request_id": f"req-{seq}",
                            "attempts_http": self.attempts_http,
                            "content_bytes": 10, "body_sha256": "0" * 64,
                            "usage": {"in": 12, "out": 34}})
        return self.answer(prompt), (12, 34)


def fake_tool(*, work: Path) -> dict:
    data = (Path(work) / "marker.txt").read_text(encoding="utf-8")
    return {"argv_sha256": "a" * 64, "rc": 0, "wall_s": 0.01,
            "payload": {"ok": True, "content": data,
                        "content_sha256": sha256_bytes(data.encode("utf-8")), "bytes": len(data)}}


def _arm(campaign, *, contract: bool = True, ticket: bool = True) -> rn.R0Runner:
    """把 G-P 的每条条件都做成**真产物**（零网络零容器），用于流程测试。"""
    from orchestrator.rsi_r0.instances import PLANS
    from orchestrator.rsi_r0.matrix import freeze_criteria

    runner = rn.R0Runner(campaign)
    runner.ensure_campaign()
    protocol = rp.rsi_r0_protocol_dir()
    freeze_criteria(protocol, PLANS, allow_refreeze=False)
    if contract:
        budget_contract.write_contract(protocol / "pricing_contract.json")
    runner.write_model_approval(
        decided_at_utc="2026-11-02T00:00:00Z",
        basis="用户 2026-11-02 逐字裁决三阶段模型（T5 单测夹具，非新裁决）")
    old_dir = campaign / "old_assets"
    old_dir.mkdir(parents=True, exist_ok=True)
    (old_dir / "a.json").write_text("{}", encoding="utf-8")
    old_ledger = campaign / "old_ledger.jsonl"
    old_ledger.write_text('{"seq": 1}\n', encoding="utf-8")
    manifest = {
        "schema": seals.MANIFEST_SCHEMA,
        "captured_at_utc": "2026-11-08T00:00:00Z",
        "ledgers": {"phase1": {"path": str(old_ledger), "exists": True, "lines": 1,
                               "sha256": seals.sha256_file(old_ledger)}},
        "approvals": {"phase1": seals.dir_digest(old_dir)},
        "gateway_state": {},
    }
    (protocol / "baseline_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    runner.write_lock()
    if ticket:
        _write_ticket(campaign)
    gates.write_approval(runner.gate_approval_path("R0-P"), gates.approval_body(
        batch_id="R0-P", task_cap=6, hard_cap=222, currency="USD", usd_ceiling=100.0,
        approved_by="user", approved_at_utc="2026-11-08T00:00:00Z", scope="protocol"))
    return runner


def _prepare(campaign, *, gateway=None, tool=None, armed: bool = True,
             contract: bool = True) -> tuple[rn.R0Runner, pp.ProtocolProbe]:
    if armed:
        runner = _arm(campaign, contract=contract)
    else:
        runner = rn.R0Runner(campaign)
        runner.ensure_campaign()
    if contract and not (rp.rsi_r0_protocol_dir() / "pricing_contract.json").exists():
        budget_contract.write_contract(rp.rsi_r0_protocol_dir() / "pricing_contract.json")
    return runner, pp.ProtocolProbe(runner, gateway or FakeGateway(),
                                    tool_runner=tool or fake_tool)


def _write_ticket(campaign) -> None:
    from orchestrator.gateway import quota_ledger as ql
    ql.write_approval(batch_id="R0-P", task=0, stage="coding_debug", approved=6,
                      beyond_normal=False, note="T5 unit fixture",
                      namespace=ql.RSI_R0_NAMESPACE)


# ---------------------------------------------------------------- 六次探针
def test_six_probes_pass_end_to_end_without_touching_the_network(campaign):
    runner, probe = _prepare(campaign)
    report = probe.run()
    assert report["verdict"] == "PROTOCOL_OK"
    assert report["requests"]["used"] == 6
    per_trial = {t["trial_id"]: t["requests_used"] for t in report["trials"]}
    assert per_trial == {"P1": 1, "P2": 1, "P3": 2, "P4": 2}
    assert report["requests"]["ledger_seqs"] == [1, 2, 3, 4, 5, 6]
    assert all(t["state"] == "PASS" for t in report["trials"])
    assert report["protocol_spec_sha256"] == pp.protocol_spec_hash()
    assert report["model"]["resolved_model_id"] == "fake-model-t5"
    # 工具往返必须留下"实际执行过"的事件，而不是只留模型自述
    for trial in report["trials"]:
        if trial["trial_id"] in ("P3", "P4"):
            ev = trial["tool_event"]
            assert ev["executed_in_container"] and ev["content_matches_marker"]
            assert ev["argv_sha256"] and ev["container_seen_sha256"] == ev["host_marker_sha256"]
    # 发网事实账：6 条已结算，全部 seq 落定
    latest = probe.journal.latest_by_request()
    assert len(latest) == 6
    assert all(r["state"] == "SETTLED_OK" and isinstance(r["seq"], int)
               for r in latest.values())


def test_report_is_written_and_hashed(campaign):
    runner, probe = _prepare(campaign)
    report = probe.run()
    path = pp.write_report(report)
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["report_sha256"] == report["report_sha256"]
    assert sha256_of({k: v for k, v in doc.items() if k != "report_sha256"}) == \
        doc["report_sha256"]


def test_a_failed_trial_blocks_the_batch_and_cancels_the_rest(campaign):
    bad = FakeGateway(answer_override=lambda prompt: "这不是 JSON")
    runner, probe = _prepare(campaign, gateway=bad)
    report = probe.run()
    assert report["verdict"] == "PROTOCOL_BLOCKED"
    states = {t["trial_id"]: t["state"] for t in report["trials"]}
    assert states == {"P1": "FAIL", "P2": "CANCELLED_BY_GATE",
                      "P3": "CANCELLED_BY_GATE", "P4": "CANCELLED_BY_GATE"}
    assert report["requests"]["used"] == 1          # 不补抽到通过
    assert "P1" in report["blocked_reason"]


def test_transport_failure_blocks_and_settles_the_attempt(campaign):
    gw = FakeGateway(fail_on_call=1)
    runner, probe = _prepare(campaign, gateway=gw)
    report = probe.run()
    assert report["verdict"] == "PROTOCOL_BLOCKED"
    assert report["trials"][0]["state"] == "PROTOCOL_BLOCKED"
    latest = list(probe.journal.latest_by_request().values())
    assert len(latest) == 1 and latest[0]["state"] == "SETTLED_FAIL"
    assert latest[0]["seq"] == 1 and latest[0]["attempts_http"] == 1


def test_gateway_preflight_block_is_recorded_as_never_sent(campaign):
    gw = FakeGateway(fail_on_call=1, block=True)
    runner, probe = _prepare(campaign, gateway=gw)
    report = probe.run()
    assert report["verdict"] == "PROTOCOL_BLOCKED"
    latest = list(probe.journal.latest_by_request().values())
    assert latest[0]["state"] == "BLOCKED_PREFLIGHT"
    assert latest[0]["attempts_http"] == 0          # 可证实没发出去，不记成发过


def test_hidden_retry_is_a_hard_violation(campaign):
    gw = FakeGateway(attempts_http=2)
    runner, probe = _prepare(campaign, gateway=gw)
    report = probe.run()
    assert report["verdict"] == "PROTOCOL_BLOCKED"
    assert "AC07" in report["blocked_reason"] or "重试" in report["blocked_reason"]
    latest = list(probe.journal.latest_by_request().values())
    assert latest[0]["error_class"] == "HiddenRetryDetected"


def test_request_caps_are_enforced_before_sending(campaign):
    runner, probe = _prepare(campaign)
    probe._used = pp.TOTAL_CAP
    with pytest.raises(pp.ProbeError, match="请求帽"):
        probe._request(trial_id="P1", index=1, prompt="x", max_tokens=8)
    probe._used = 0
    with pytest.raises(pp.ProbeError, match="本试验请求帽"):
        probe._request(trial_id="P1", index=3, prompt="x", max_tokens=8)


def test_missing_contract_is_not_a_protocol_result(campaign):
    """没有最坏成本上界 ⇒ 没资格发网（不是"协议失败"，是参保失败）。"""
    runner, probe = _prepare(campaign, contract=False)
    with pytest.raises(pp.ProbeError, match="BLOCKED_COST"):
        probe.run()


def test_missing_ticket_is_not_a_protocol_result(campaign):
    """门过了但账本侧没有批次票 ⇒ 同样不许发网。"""
    runner = _arm(campaign, ticket=False)
    probe = pp.ProtocolProbe(runner, FakeGateway(), tool_runner=fake_tool)
    with pytest.raises(pp.ProbeError, match="批次票据"):
        probe.run()


# ---------------------------------------------------------------- 解析与白名单
@pytest.mark.parametrize("text", [
    "```json\n{\"kind\":\"ready\"}\n```",
    "{\"kind\":\"ready\"}\n多余的第二行",
    "[1, 2, 3]",
    "",
])
def test_single_line_json_is_parsed_strictly(text):
    assert pp.parse_single_json(text) is None


def test_action_whitelist_and_path_scope():
    work = Path("/tmp/rsi_r0_action_scope")
    work.mkdir(exist_ok=True)
    ok = pp.validate_action({"action": "read_file", "path": "/work/marker.txt"}, work=work)
    assert ok == "marker.txt"
    for bad in ({"action": "write_file", "path": "/work/marker.txt"},
                {"action": "read_file"},
                {"action": "read_file", "path": "/etc/passwd"},
                {"action": "read_file", "path": "/work/sub/../../etc/passwd"},
                None):
        with pytest.raises(pp.ProbeBlocked):
            pp.validate_action(bad, work=work)


def test_tool_that_reads_something_else_blocks_the_trial(campaign):
    def lying_tool(*, work):
        return {"argv_sha256": "b" * 64, "rc": 0, "wall_s": 0.01,
                "payload": {"ok": True, "content": "deadbeef\n",
                            "content_sha256": "deadbeef"}}

    runner, probe = _prepare(campaign, tool=lying_tool)
    report = probe.run()
    assert report["verdict"] == "PROTOCOL_BLOCKED"
    assert report["trials"][2]["state"] == "PROTOCOL_BLOCKED"


def test_docker_argv_is_sandboxed():
    argv = pp._docker_argv(Path("/tmp/work"))
    joined = " ".join(argv)
    assert "--network=none" in argv
    assert "--read-only" in argv and "--user=1000:1000" in argv
    assert "--cap-drop=ALL" in argv and "no-new-privileges" in joined
    assert pp.IMAGE_REF in argv
    assert "-v" in argv and "/tmp/work:/work:ro" in argv


# ---------------------------------------------------------------- 成本合同
def test_pricing_contract_is_a_conservative_ceiling(tmp_path):
    path = tmp_path / "pricing_contract.json"
    doc = budget_contract.write_contract(path)
    assert doc["pricing_basis"] == "conservative_ceiling_not_vendor_quote"
    assert doc["worst_case_per_request_usd"] > 0
    assert doc["worst_case_total_usd"] == pytest.approx(
        doc["worst_case_per_request_usd"] * 222)
    assert doc["worst_case_total_usd"] <= doc["usd_ceiling"]
    ok, why = budget_contract.contract_ok(budget_contract.read_contract(path))
    assert ok, why


def test_pricing_contract_tampering_is_rejected(tmp_path):
    path = tmp_path / "pricing_contract.json"
    doc = budget_contract.write_contract(path)
    doc["price_per_1m_output_usd"] = 0.01        # 偷偷压单价
    path.write_text(json.dumps(doc))
    ok, why = budget_contract.contract_ok(budget_contract.read_contract(path))
    assert not ok and "自哈希" in why


def test_pricing_contract_over_ceiling_is_rejected(tmp_path):
    path = tmp_path / "pricing_contract.json"
    doc = budget_contract.write_contract(path, usd_ceiling=1.0)
    ok, why = budget_contract.contract_ok(doc)
    assert not ok and "货币帽" in why


# ---------------------------------------------------------------- 产物化门事实
def test_model_approval_tracks_the_config_hash(campaign, tmp_path, monkeypatch):
    runner = rn.R0Runner(campaign)
    runner.ensure_campaign()
    fake_cfg = tmp_path / "stage_models.yaml"
    fake_cfg.write_text("stage_models:\n  coding_debug:\n    model_id: m-1\n",
                        encoding="utf-8")
    monkeypatch.setattr(rn.R0Runner, "stage_models_path", lambda self: fake_cfg)
    with pytest.raises(rn.RunnerError, match="basis 太短"):
        runner.write_model_approval(decided_at_utc="2026-11-08T00:00:00Z", basis="短")
    doc = runner.write_model_approval(decided_at_utc="2026-11-08T00:00:00Z",
                                      basis="用户 2026-11-02 逐字裁决三阶段模型（测试）")
    assert doc["stage_models"] == {"coding_debug": "m-1"}
    ok, detail = runner.model_ids_approved()
    assert ok, detail
    # 幂等重入
    assert runner.write_model_approval(
        decided_at_utc="2026-11-08T00:00:00Z",
        basis="用户 2026-11-02 逐字裁决三阶段模型（测试）")["approval_sha256"] == \
        doc["approval_sha256"]
    # 配置一变（等价于偷偷换模型）⇒ 批准立刻失效
    fake_cfg.write_text("stage_models:\n  coding_debug:\n    model_id: m-2\n",
                        encoding="utf-8")
    ok, detail = runner.model_ids_approved()
    assert not ok and "已改" in detail["why"]
    with pytest.raises(rn.RunnerError, match="拒绝覆盖"):
        runner.write_model_approval(decided_at_utc="2026-11-08T00:00:00Z",
                                    basis="另一条足够长但不同的理由文本，用于触发拒绝覆盖分支")


def test_seal_verification_detects_any_old_asset_change(tmp_path):
    old = tmp_path / "old"
    old.mkdir()
    (old / "a.json").write_text("{}", encoding="utf-8")
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text('{"seq": 1}\n', encoding="utf-8")
    manifest = {
        "schema": seals.MANIFEST_SCHEMA,
        "captured_at_utc": "2026-11-08T00:00:00Z",
        "ledgers": {"phase1": {"path": str(ledger), "exists": True, "lines": 1,
                               "sha256": seals.sha256_file(ledger)}},
        "approvals": {"phase1": seals.dir_digest(old)},
        "gateway_state": {},
    }
    path = tmp_path / "baseline_manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    verdict = seals.verify_baseline(path)
    assert verdict["ok"] and verdict["checked"] == 2
    (old / "b.json").write_text("{}", encoding="utf-8")
    verdict = seals.verify_baseline(path)
    assert not verdict["ok"]
    assert "approvals:phase1.files" in verdict["drift"]


def test_missing_baseline_manifest_is_not_ok(tmp_path):
    verdict = seals.verify_baseline(tmp_path / "nope.json")
    assert not verdict["ok"] and "缺基线清单" in verdict["why"]


def test_gate_facts_come_from_artifacts_not_env(campaign, monkeypatch):
    monkeypatch.setenv("SIMPRAXIS_MODEL_IDS_APPROVED", "1")
    monkeypatch.setenv("SIMPRAXIS_COST_UPPER_BOUND_USD", "100")
    monkeypatch.setenv("SIMPRAXIS_SEALS_VERIFIED", "1")
    runner = rn.R0Runner(campaign)
    runner.ensure_campaign()
    # 只有环境变量、没有任何产物 ⇒ G-M 不成立
    facts = runner.gate_facts("G-M")
    assert facts["gate_m_passed"] is False
    assert {"budget_contract_approved", "model_ids_approved",
            "cost_upper_bound_known", "seals_verified"} <= set(facts["detail"]) | \
        set(gates.evaluate_gate("G-M", facts)["missing"])


# ---------------------------------------------------------------- LOCK
def test_lock_covers_protocol_spec_and_pricing(campaign, monkeypatch):
    runner = rn.R0Runner(campaign)
    runner.ensure_campaign()
    snap = runner.lock_snapshot()
    assert snap["protocol_spec_sha256"] == pp.protocol_spec_hash()
    assert snap["protocol_spec"]["total_request_cap"] == 6
    assert snap["pricing_contract_sha256"] is None
    budget_contract.write_contract(rp.rsi_r0_protocol_dir() / "pricing_contract.json")
    snap2 = runner.lock_snapshot()
    assert snap2["pricing_contract_sha256"]
    monkeypatch.setitem(pp.PROTOCOL_SPEC, "temperature", 0.9)
    assert runner.lock_snapshot()["protocol_spec_sha256"] != snap["protocol_spec_sha256"]


def test_supersede_lock_archives_the_old_file(campaign):
    runner = rn.R0Runner(campaign)
    runner.ensure_campaign()
    old = runner.write_lock()
    with pytest.raises(rn.RunnerError, match="理由太短"):
        runner.supersede_lock("短")
    out = runner.supersede_lock("T4 首版锁的 stage_models_sha256 为 null（路径缺陷），"
                                "补记后需重写并归档旧件")
    assert out["superseded"] == old["lock_sha256"]
    archived = Path(out["archive"])
    assert archived.exists()
    assert json.loads(archived.read_text())["lock_sha256"] == old["lock_sha256"]
    hist = rp.rsi_r0_protocol_dir() / "lock_history.jsonl"
    line = json.loads(hist.read_text(encoding="utf-8").splitlines()[-1])
    assert line["old_lock_sha256"] == old["lock_sha256"]
    assert line["new_lock_sha256"] == out["new_lock_sha256"]
    assert runner.verify_lock()["ok"] is True


def test_supersede_lock_refuses_once_anything_was_sent(campaign):
    from orchestrator.rsi_r0.attempts import AttemptJournal, BudgetReservations
    from orchestrator.rsi_r0.contracts import ResourceCaps

    runner = rn.R0Runner(campaign)
    runner.ensure_campaign()
    runner.write_lock()
    journal = AttemptJournal(rp.rsi_r0_attempts_path(),
                             BudgetReservations(ResourceCaps(),
                                                rp.rsi_r0_reservations_dir()))
    rec = journal.reserve(slot_id="P1", batch_id="R0-P", task_id=0,
                          stage="coding_debug", tokens_in=10, tokens_out=10, usd=0.01)
    journal.mark_sent(rec, seq=1)
    with pytest.raises(rn.RunnerError, match="禁止取代"):
        runner.supersede_lock("试图在发网之后换掉实验定义，这种取代必须被拒绝并留下原件")


# ---------------------------------------------------------------- CLI
def test_cli_pricing_approve_and_model_approval(campaign, capsys):
    assert cli_main(["pricing", "--campaign", str(campaign), "--write"]) == EXIT_OK
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["ok"] is True
    assert cli_main(["approve", "--campaign", str(campaign), "--batch", "R0-P",
                     "--task", "0", "--requests", "6"]) == EXIT_OK
    approve = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert approve["ticket_created"] is True and approve["requests"] == 6
    # 同内容重入幂等
    assert cli_main(["approve", "--campaign", str(campaign), "--batch", "R0-P",
                     "--task", "0", "--requests", "6"]) == EXIT_OK
    # 不同内容拒覆盖
    assert cli_main(["approve", "--campaign", str(campaign), "--batch", "R0-P",
                     "--task", "0", "--requests", "5"]) == EXIT_REFUSED
    capsys.readouterr()
    assert cli_main(["model-approval", "--campaign", str(campaign),
                     "--decided-at", "2026-11-02T00:00:00Z",
                     "--basis", "用户 2026-11-02 逐字裁决三阶段模型（CLI 测试）"]) == EXIT_OK
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["ok"] is True and out["stage_models"]["coding_debug"]


def test_cli_protocol_still_refuses_without_paid_guard(campaign, capsys, monkeypatch):
    monkeypatch.delenv("SIMPRAXIS_ALLOW_PAID", raising=False)
    assert cli_main(["protocol", "--campaign", str(campaign)]) == EXIT_REFUSED
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert "SIMPRAXIS_ALLOW_PAID" in payload["refused"]


def test_cli_protocol_refuses_when_the_gate_is_not_met(campaign, capsys, monkeypatch):
    monkeypatch.setenv("SIMPRAXIS_ALLOW_PAID", "1")
    assert cli_main(["protocol", "--campaign", str(campaign)]) == EXIT_REFUSED
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert "G-P" in payload["gate"]


def test_cli_lock_supersede_path(campaign, capsys):
    assert cli_main(["lock", "--campaign", str(campaign)]) == EXIT_OK
    assert cli_main(["lock", "--campaign", str(campaign),
                     "--supersede-reason",
                     "测试：旧锁需要按新快照重写并保留归档与历史"]) == EXIT_OK
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["superseded"]["new_lock_sha256"]
    assert out["verify"]["ok"] is True
