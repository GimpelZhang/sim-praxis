"""RSI-R0.2 协议 fixture 测试（T3/AC18，零付费、无容器）。

夹具替身模拟「容器执行候选源码」这一件事：从源码里取 NONCE 常量、读本轮 run
的 x 序列、打印 PROTO_STAT。其余（动作分派/槽账/journal/计费）全走候选 loop
本体。检查器侧独立重算统计，并拒绝静态复读与错序。
"""
from __future__ import annotations

import copy
import json

import pytest

from orchestrator.rsi_r01 import actions as AC
from orchestrator.rsi_r02 import candidate as CD
from orchestrator.rsi_r02 import messages as M
from orchestrator.rsi_r02 import protocol_fixture as PF
from tests import rsi_r02_helpers as H

X_BY_RUN = {"r1": [0.0, 0.4, -0.9, 0.2], "r2": [0.0, 0.1]}


class _Rec:
    def __init__(self, run_id: str):
        self.run_id = run_id
        self.manifest = {"run_id": run_id}


class _Store:
    def __init__(self, docs):
        self.docs = docs

    def load_trusted(self, run_id, manifest, *, expected_trial=None,
                     expected_instance=None):
        return self.docs[run_id]


class _ProtocolTrial:
    """模拟受限容器：真跑候选写的 analyze 源码里那两件事。"""

    def __init__(self, *, lie_factor: float = 1.0):
        self.surface = AC.ActionSurface(
            kind="push_control",
            param_bounds={"kp": (0, 5000), "kd": (0, 500), "max_force": (0, 500),
                          "steps": (1, 3000)},
            fixed_params={}, max_steps=3000)
        self.calls: "list[AC.Action]" = []
        self.runs: "list[_Rec]" = []
        self.docs: "dict[str, dict]" = {}
        self.events: "list[dict]" = []
        self.store = _Store(self.docs)
        self.lie_factor = lie_factor

    def dispatch(self, action: AC.Action) -> "dict[str, Any]":
        self.calls.append(action)
        if isinstance(action, AC.Preregister):
            return {"ok": True, "action": "preregister", "prereg_sha256": "a" * 64}
        if isinstance(action, AC.RunExperiment):
            rid = f"r{len(self.runs) + 1}"
            self.docs[rid] = {"run_id": rid, "x": list(X_BY_RUN[rid]),
                              "params": dict(action.params)}
            self.runs.append(_Rec(rid))
            return {"ok": True, "action": "run_experiment", "run_id": rid,
                    "summary": {"distance": 0.01, "final_speed": 0.001,
                                "final_distance": 0.01}}
        if isinstance(action, AC.Analyze):
            nonce = _nonce_of(action.source)
            stat = max(max(abs(v) for v in self.docs[r]["x"])
                       for r in action.run_ids) * self.lie_factor
            payload = {"nonce": nonce, "stat": stat,
                       "run_ids": sorted(action.run_ids)}
            ev = {"analysis_id": f"a{len(self.events) + 1}",
                  "stdout_preview": PF.PROTO_LINE
                  + json.dumps(payload, sort_keys=True)}
            self.events.append({"action": "analyze", "ok": True, "analysis": ev})
            return {"ok": True, "action": "analyze", "analysis": ev,
                    "path": "analysis/a.json", "sha256": "0" * 64}
        if isinstance(action, AC.Submit):
            return {"ok": True, "action": "submit", "claim": action.claim}
        raise AssertionError(f"未知动作 {action!r}")

    def judge(self) -> "dict[str, Any]":
        return {"pass": True, "layer": "L4", "family": "E"}


def _nonce_of(source: str) -> str:
    for line in source.splitlines():
        if line.startswith("NONCE = "):
            return str(eval(line[len("NONCE = "):]))   # 测试内只在自家常量上用
    raise AssertionError("源码里没有 NONCE")


def _run_chain(ctx, *, fixture_id: str, lie_factor: float = 1.0):
    inst = f"E-{fixture_id.lower()}"
    fixture = PF.make_fixture(campaign_id=ctx.campaign_id, fixture_id=fixture_id,
                              slot_id=f"{inst}-01", instance_id=inst)
    slot_ids = [f"{inst}-0{i}" for i in range(1, 9)]
    H.write_slots(ctx, slot_ids, family="E")
    trial = _ProtocolTrial(lie_factor=lie_factor)
    loop, model, store, journal, slots, book = H.build_loop(
        ctx, slot_ids=slot_ids, replies=[], trial=trial,
        family="E", instance_id=inst)
    stub = PF.ProtocolStub(fixture=fixture)
    loop.call_model = stub
    loop.public_doc = fixture.task_doc()
    loop.system = M.build_system(loop.public_doc, trial.surface)
    out = loop.run()
    scoped = [ev for ev in journal.events
              if ev.get("trial_id") == f"{inst}-dev"]
    return fixture, out, trial, scoped


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    H.set_env(tmp_path, monkeypatch)
    return H.make_campaign(tmp_path)


# ---- nonce / 公开文档 ----------------------------------------------------


def test_nonce_is_deterministic_and_fixture_specific():
    a = PF.derive_nonce(campaign_id="c1", fixture_id="P1", slot_id="s1")
    b = PF.derive_nonce(campaign_id="c1", fixture_id="P1", slot_id="s1")
    other = PF.derive_nonce(campaign_id="c1", fixture_id="P2", slot_id="s1")
    assert a == b and a != other and len(a) == 16


def test_task_doc_carries_protocol_but_no_private_fields():
    fx = PF.make_fixture(campaign_id="c1", fixture_id="P1", slot_id="s1")
    doc = json.dumps(fx.task_doc(), ensure_ascii=False)
    assert fx.nonce in doc and "PROTO_STAT" in doc
    for leaked in ("tolerance", "fault", "reference_gains", "run_5"):
        assert leaked not in doc


def test_analyze_source_binds_nonce_and_prints_line():
    fx = PF.make_fixture(campaign_id="c1", fixture_id="P2", slot_id="s9")
    src = PF.analyze_source(fx)
    assert fx.nonce in src
    assert PF.PROTO_LINE.strip() in src
    assert "/data/run_*.json" in src and "/out/analysis.json" in src


# ---- 四动作链 ------------------------------------------------------------


def test_stub_drives_four_action_chain_and_checker_passes(ctx):
    fixture, out, trial, events = _run_chain(ctx, fixture_id="P1")
    assert out["outcome"] == "submitted"
    assert [CD.action_name(a) for a in trial.calls] == list(PF.ACTION_ORDER)
    verdict = PF.verify_run(fixture, events=events, trial=trial)
    assert verdict["ok"], verdict["problems"]
    assert verdict["evidence"]["n_posts"] == 4
    assert verdict["evidence"]["host_stat"] == pytest.approx(0.9)


def test_second_chain_uses_different_nonce_and_stat(ctx):
    """两链并列存在：P2 的 nonce/统计与 P1 不同，静态复读不可能同时过。"""
    fx1, _o1, t1, e1 = _run_chain(ctx, fixture_id="P1")
    fx2, _o2, t2, e2 = _run_chain(ctx, fixture_id="P2")
    assert fx1.nonce != fx2.nonce
    assert PF.verify_run(fx1, events=e1, trial=t1)["ok"]
    assert PF.verify_run(fx2, events=e2, trial=t2)["ok"]


def test_replayed_submit_text_fails_other_chain(ctx):
    fx1, _o1, t1, e1 = _run_chain(ctx, fixture_id="P1")
    fx2, _o2, t2, e2 = _run_chain(ctx, fixture_id="P2")
    # 把 P1 的 submit 原文塞进 P2 的链：nonce/stat 都对不上
    replay = [ev["payload"]["assistant_text"] for ev in e1
              if ev.get("action") == "post"
              and (ev.get("payload") or {}).get("assistant_text")]
    submit_text = [t for t in replay if '"submit"' in t][0]
    tampered = copy.deepcopy(e2)
    for ev in tampered:
        if ev.get("kind") == "result" and ev.get("action") == "post" and \
                '"submit"' in str((ev.get("payload") or {}).get("assistant_text")):
            ev["payload"]["assistant_text"] = submit_text
    verdict = PF.verify_run(fx2, events=tampered, trial=t2)
    assert not verdict["ok"]
    assert any("nonce" in p for p in verdict["problems"])


def test_host_recompute_catches_lying_stat(ctx):
    """候选把 STDOUT 统计报错（与提交自洽）：宿主独立重算必须抓到。"""
    fixture, out, trial, events = _run_chain(ctx, fixture_id="P1", lie_factor=0.5)
    assert out["outcome"] == "submitted"
    verdict = PF.verify_run(fixture, events=events, trial=trial)
    assert not verdict["ok"]
    assert any("统计" in p for p in verdict["problems"])


def test_missing_post_breaks_chain(ctx):
    fixture, _out, trial, events = _run_chain(ctx, fixture_id="P1")
    trimmed = [ev for ev in events
               if not (ev.get("kind") == "result" and ev.get("action") == "post"
                       and '"submit"' in str((ev.get("payload") or {})
                                             .get("assistant_text")))]
    verdict = PF.verify_run(fixture, events=trimmed, trial=trial)
    assert not verdict["ok"]
    assert any("POST 数" in p for p in verdict["problems"])


def test_wrong_action_order_is_rejected(ctx):
    fixture, _out, trial, events = _run_chain(ctx, fixture_id="P1")
    swapped = [ev for ev in events]
    acts = [ev for ev in swapped if ev.get("action") in ("analyze", "submit")
            and ev.get("kind") == "result"]
    assert len(acts) == 2
    acts[0]["action"], acts[1]["action"] = acts[1]["action"], acts[0]["action"]
    verdict = PF.verify_run(fixture, events=swapped, trial=trial)
    assert not verdict["ok"]
    assert any("顺序" in p for p in verdict["problems"])


def test_proto_line_parser_is_strict():
    with pytest.raises(PF.ProtocolError):
        PF._parse_proto_line("hello\n")
    with pytest.raises(PF.ProtocolError):
        PF._parse_proto_line('PROTO_STAT not-json')
    assert PF._parse_proto_line('PROTO_STAT {"nonce": "x"}') == {"nonce": "x"}


def test_recovered_runs_reads_only_this_trials_manifests(tmp_path):
    """已终态 trial 的 run 记录从可信 manifest 复原；别的 trial 不收。

    重跑幂等（评审 H4）：恢复语义下 runner.runs 为空，若不复原，宿主就拿不到
    可信观测、协议检查器只能判「缺可信观测」——那是把「没重跑」误报成失败。

    坏 manifest **不再静默跳过**（评审 N3）：不给收集器就直接抛，给了就逐条
    记 ``MANIFEST_UNREADABLE``，坏件绝不混进复原结果。
    """
    good = {"schema": "x", "trial_id": "proto_P1", "run_id": "run_ok",
            "cell": {"kp": 60.0}}
    other = {"schema": "x", "trial_id": "E-dev", "run_id": "run_other"}
    (tmp_path / "run_ok.manifest.json").write_text(json.dumps(good),
                                                   encoding="utf-8")
    (tmp_path / "run_other.manifest.json").write_text(json.dumps(other),
                                                      encoding="utf-8")
    (tmp_path / "run_bad.manifest.json").write_text("{not json",
                                                    encoding="utf-8")
    with pytest.raises(PF.ProtocolError, match="清单不可读"):
        PF.recovered_runs(tmp_path, "proto_P1")
    problems: "list[str]" = []
    runs = PF.recovered_runs(tmp_path, "proto_P1", problems)
    assert [r.run_id for r in runs] == ["run_ok"]
    assert runs[0].manifest == good
    # 评审：cell 从受信 manifest 原样取回（置 None 会让 R 族重跑无法复算）
    assert runs[0].cell == {"kp": 60.0}
    assert any("MANIFEST_UNREADABLE: run_bad.manifest.json" in p
               for p in problems)
    # 坏件在场时即使 trial_id 对不上也要炸（fail-closed，先读后筛）
    with pytest.raises(PF.ProtocolError):
        PF.recovered_runs(tmp_path, "no_such_trial")
    empty = tmp_path / "empty"
    empty.mkdir()
    assert PF.recovered_runs(empty, "no_such_trial") == []


def test_recovered_cell_is_rederived_from_manifest_params(tmp_path):
    """评审 LOW-1：manifest 没写 cell 时必须按 params 复原到冻结四格。

    以前复原出来的 RunRecord 恒为 cell=None，而调用方对 None 直接跳过 ⇒
    R 族独立 oracle 在重跑时整族不复算，看着像复原、实际空转。复原必须走
    与真实分派同一条归类逻辑（trial.cell_of），否则可能归错格。
    """
    good = {"schema": "x", "trial_id": "R-dev", "run_id": "run_r1",
            "instance_id": "R-dev",
            "params": {"damping": 0.45, "initial_angle": -0.8,
                       "timestep": 0.002, "steps": 2000}}
    (tmp_path / "run_r1.manifest.json").write_text(json.dumps(good),
                                                   encoding="utf-8")
    runs = PF.recovered_runs(tmp_path, "R-dev")
    assert [r.cell for r in runs] == ["high/minus"]
    # 不在冻结四格上的参数不得猜格：无收集器即抛，有收集器逐条记账
    good["params"] = {"damping": 0.3, "initial_angle": 0.0}
    (tmp_path / "run_r1.manifest.json").write_text(json.dumps(good),
                                                   encoding="utf-8")
    with pytest.raises(PF.ProtocolError, match="cell 不可复原"):
        PF.recovered_runs(tmp_path, "R-dev")
    problems: "list[str]" = []
    runs = PF.recovered_runs(tmp_path, "R-dev", problems)
    assert runs[0].cell is None
    assert any(p.startswith("CELL_UNRESOLVED: run_r1.manifest.json")
               for p in problems)
