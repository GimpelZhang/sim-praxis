"""RSI-R0.2 T3 控制脚本的纯逻辑单测（零付费、零容器）。

目前只锁一件事：协议四动作链的**槽位第二重绑定**。槽要求的是模型在该轮
POST 里请求的动作名，不是 journal 的 ``action="post"``；这层对照错了会把
正确链整条误报成坏链（真实控制跑里四槽全判 mismatch 的回归）。
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_SCRIPTS = _REPO / "scripts" / "rsi_r02"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import t3_control as T3C  # noqa: E402

from orchestrator.rsi_r02 import state as ST  # noqa: E402


class _Book:
    def __init__(self, mapping):
        self._m = mapping

    def get(self, slot_id):
        if slot_id not in self._m:
            raise ST.StateError(f"未知槽：{slot_id}")
        return self._m[slot_id]


def _post(slot_id):
    return {"kind": "result", "action": "post", "slot_id": slot_id, "ok": True}


def _dispatched(action, slot_id):
    return {"kind": "result", "action": action, "slot_id": slot_id, "ok": True}


def test_slot_binding_pairs_dispatch_not_the_post_event():
    events = [
        _post("P-A-1"), _dispatched("preregister", "P-A-1"),
        _post("P-A-2"), _dispatched("run_experiment", "P-A-2"),
        _post("P-A-3"), _dispatched("analyze", "P-A-3"),
        _post("P-A-4"), _dispatched("submit", "P-A-4"),
    ]
    book = _Book({f"P-A-{i}": {"required_action": name} for i, name in
                  enumerate(("preregister", "run_experiment", "analyze",
                             "submit"), start=1)})
    rows = T3C.slot_binding(events, book)
    assert [r["actual_action"] for r in rows] == [
        "preregister", "run_experiment", "analyze", "submit"]
    assert all(r["ok"] for r in rows)


def test_slot_binding_flags_wrong_action_and_unknown_slot():
    events = [_post("P-B-1"), _dispatched("submit", "P-B-1"),
              _post("P-B-9")]                       # 第二个槽无分派、也不在书里
    book = _Book({"P-B-1": {"required_action": "preregister"}})
    rows = T3C.slot_binding(events, book)
    assert rows[0]["ok"] is False
    assert rows[0]["actual_action"] == "submit"
    assert rows[1]["required_action"] is None       # 未知槽不猜要求，如实留空
    assert rows[1]["actual_action"] == ""
    assert rows[1]["ok"] is True                    # 无可对照时不制造假失败


def test_slot_binding_ignores_intent_events():
    """intent 事件也叫 post，但不是结果，不参与槽对照。"""
    events = [{"kind": "intent", "action": "post", "slot_id": "P-A-1"},
              _post("P-A-1"), _dispatched("preregister", "P-A-1")]
    book = _Book({"P-A-1": {"required_action": "preregister"}})
    rows = T3C.slot_binding(events, book)
    assert len(rows) == 1 and rows[0]["ok"] is True


def test_protocol_ok_requires_submitted_terminal_state():
    """评审 LOW-2：verdict.ok 之外还必须终态 SUBMITTED。

    协议 trial 在跑完整条四动作链后中断（ABORTED）时，恢复出来的 run 仍能
    让检查器判 ok；只看 verdict 等于把「没跑完」洗成「跑通」。
    """
    good = {"verdict": {"ok": True, "problems": []},
            "status": ST.TRIAL_SUBMITTED, "slot_problems": []}
    assert T3C.protocol_ok([good, dict(good)]) is True
    assert T3C.protocol_ok([dict(good, status=ST.TRIAL_ABORTED)]) is False
    assert T3C.protocol_ok([dict(good, verdict={"ok": False})]) is False
    assert T3C.protocol_ok([dict(good, slot_problems=["X"])]) is False


# ---- 结论件发布（t3_publish_evidence）------------------------------------

def _pair(tmp_path):
    import subprocess

    from orchestrator.rsi_r02 import campaign as C

    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "x.txt").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "x.txt"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=Junchuan Zhang",
                    "-c", "user.email=zjunchuan@gmail.com",
                    "commit", "-q", "-m", "init"], cwd=repo, check=True)
    plan = tmp_path / "plan.md"
    plan.write_text("# plan\n", encoding="utf-8")
    consent = tmp_path / "consent.txt"
    consent.write_text("authorized\n", encoding="utf-8")
    root = tmp_path / "rsi_r02"
    ctrl = C.create_campaign(campaign_id="r02_t3_control_TEST", plan_path=plan,
                             consent_path=consent, repo_root=repo, root=root)
    prod = C.create_campaign(campaign_id="r02_prod_TEST", plan_path=plan,
                             consent_path=consent, repo_root=repo, root=root)
    return ctrl, prod


def test_publish_copies_and_records_provenance(tmp_path):
    import t3_publish_evidence as T3P

    ctrl, prod = _pair(tmp_path)
    payloads = {"report.json": b'{"ok": true}\n',
                "closeout_rehearsal.json": b'{"ok": true}\n',
                "records/proto_P1.json": b'{"ok": true}\n',
                "records/proto_P2.json": b'{"ok": false}\n'}
    for rel, data in payloads.items():
        ctrl.write_text(f"t3_control/{rel}", data.decode("utf-8"))
    out = T3P.publish(control=ctrl, prod=prod, note="t")
    assert out["schema"] == T3P.SCHEMA
    assert out["source_campaign"] == "r02_t3_control_TEST"
    for rel, data in payloads.items():
        assert prod.relative(f"t3_control/{rel}").read_bytes() == data
        assert out["files"][rel]["identical"] is True
        assert out["files"][rel]["bytes"] == len(data)
    prov = prod.relative("t3_control/provenance.json")
    import json
    assert json.loads(prov.read_text(encoding="utf-8")) == out


def test_publish_refuses_missing_evidence(tmp_path):
    import t3_publish_evidence as T3P

    ctrl, prod = _pair(tmp_path)
    ctrl.write_text("t3_control/report.json", "{}\n")
    import pytest
    with pytest.raises(T3P.PublishError, match="closeout_rehearsal.json"):
        T3P.publish(control=ctrl, prod=prod)
