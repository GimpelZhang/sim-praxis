"""RSI-R0.2 仪器加固（票八）的离线测试：零付费、零容器、零触网。

对应 2026-11-10 R0.2 结案尾债 **N-12** 的三个子项 + 复盘沉淀的两条判据：

1. 公开文本把宿主注入的 ``固定参数`` 印进行动协议段，却从未禁止写进 params
   —— 候选照抄即触发终态策略违规（票七 P2 事故的直接 but-for 贡献）。
2. ``retry_rule`` 对策略违规给出「修正后重发即可」的假承诺，与「策略违规是
   终态」的实际语义相反。
3. ``doc_gaps`` 只查文档 dict 的结构，不查**渲染后**候选真正读到的文本。

另加一条行为级探针：``ProbeActor`` 手里没有 fixture，一切实例值只能从渲染
文本解析；它必须能把两条链各走通四次动作。
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_SCRIPTS = _REPO / "scripts" / "rsi_r02"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import t4_protocol as T4  # noqa: E402

from orchestrator.rsi_r02 import billing as B  # noqa: E402
from orchestrator.rsi_r02 import candidate as CD  # noqa: E402
from orchestrator.rsi_r02 import closeout as CO  # noqa: E402
from orchestrator.rsi_r02 import messages as M  # noqa: E402
from orchestrator.rsi_r02 import protocol_fixture as PF  # noqa: E402
from orchestrator.rsi_r02 import state as ST  # noqa: E402
from tests import rsi_r02_helpers as H  # noqa: E402

PROTO_ROUNDS = ("preregister", "run_experiment", "analyze", "submit", None, None)


def _write_proto_slots(camp) -> None:
    slots = [{"slot_id": f"P-{chain}-{i}", "phase": "protocol",
              "family": "protocol", "chain": chain, "round": i,
              "required_action": PROTO_ROUNDS[i - 1], "post_cap": 1,
              "status": ST.SLOT_UNUSED}
             for chain in ("A", "B") for i in range(1, 7)]
    camp.write_json("quota/slots.json", {
        "schema": ST.SCHEMA_SLOTBOOK,
        "recorded_at_utc": "2026-11-10T00:00:00Z",
        "counts": {"total": len(slots)}, "slots": slots})


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    H.set_env(tmp_path, monkeypatch)
    camp = H.make_campaign(tmp_path)
    _write_proto_slots(camp)
    H.write_public_task(camp, "E-dev", family="E")
    return camp


def _fixture(camp, fid: str) -> "PF.ProtocolFixture":
    slot = {"P1": "P-A-1", "P2": "P-B-1"}[fid]
    return PF.make_fixture(campaign_id=camp.campaign_id, fixture_id=fid,
                           slot_id=slot, instance_id="E-dev")


def _real_surface():
    """与 ``rsi_r01.trial.action_surface(E-dev)`` 同形的真实动作面。

    ``surface_from_doc`` 只看公开文档，拿不到宿主注入的 ``timestep``；这里显式
    带上，才能验证「固定参数行既印值、又印禁令」。
    """
    from orchestrator.rsi_r01 import actions as AC
    return AC.ActionSurface(
        kind="push_control",
        param_bounds={"kp": (0.0, 5000.0), "kd": (0.0, 500.0),
                      "max_force": (0.0, 500.0), "steps": (1.0, 3000.0)},
        fixed_params={"timestep": 0.002}, max_steps=3000)


def _rendered(camp, fid: str) -> "tuple[dict, str]":
    doc = _fixture(camp, fid).task_doc()
    return doc, M.build_system(doc, _real_surface())


# ---------------------------------------------------------------------------
# N-12 子项一：固定参数必须带禁令；params 白名单必须逐字可见
# ---------------------------------------------------------------------------


def test_fixed_params_line_forbids_writing_into_params(ctx):
    """渲染文本必须明说「固定参数由宿主注入、禁止写进 params」。

    票七 P2 的唯一失败动作是 ``run_experiment.params`` 多了 ``timestep``；
    它来自这条渲染行。以前这一行只有值没有禁令——「候选照抄了宿主自己印的字」
    与「候选自创字段」在判据上无法区分。
    """
    _doc, text = _rendered(ctx, "P1")
    assert "固定参数" in text
    line = next(x for x in text.splitlines() if x.startswith("固定参数"))
    assert "禁止写进 params" in line
    assert "timestep" in line            # 值仍然可见（信息量不减少）
    # 白名单行必须逐字列出允许的键，且与验证器同源
    allow = next(x for x in text.splitlines() if x.startswith("params 允许的键"))
    for key in ("kp", "kd", "max_force", "steps"):
        assert key in allow
    assert "timestep" not in allow        # 固定参数不是 params 的合法键


def test_params_whitelist_line_matches_action_surface(ctx):
    """白名单行与 ``ActionSurface.param_bounds`` 同源（不另抄字面量）。"""
    doc = _fixture(ctx, "P2").task_doc()
    surface = M.surface_from_doc(doc)
    text = M.action_schema_text(surface)
    allow = next(x for x in text.splitlines() if x.startswith("params 允许的键"))
    assert sorted(surface.param_bounds) == sorted(
        k for k in surface.param_bounds if k in allow)


# ---------------------------------------------------------------------------
# N-12 子项二：重试承诺必须区分「可自修」与「策略违规终态」
# ---------------------------------------------------------------------------


def test_retry_rule_separates_repairable_from_terminal(ctx):
    doc = _fixture(ctx, "P1").task_doc()
    rule = doc["public"]["interface"]["protocol"]["retry_rule"]
    assert "可自修" in rule and "终态" in rule
    # 旧口径的全体假承诺必须消失：它曾对**任何**被判负的动作说
    # 「被判负的那次不改变动作顺序要求，修正后重发即可」。
    assert "被判负的那次不改变动作顺序要求，修正后重发即可" not in rule
    assert "不再接受任何动作" in rule
    text = M.build_system(doc, M.surface_from_doc(doc))
    assert "策略违规是终态" in text
    assert "没有修正机会" in text


# ---------------------------------------------------------------------------
# N-12 子项三：渲染文本自足性（doc_gaps 的补充面）
# ---------------------------------------------------------------------------


def test_rendered_text_carries_every_instance_value(ctx):
    for fid in PF.FIXTURE_IDS:
        doc, text = _rendered(ctx, fid)
        assert M.rendered_gaps(text, doc) == []


def test_rendered_gaps_catch_a_value_that_never_reaches_the_text(ctx):
    """文档 dict 里有、渲染时被丢掉 —— 这正是 doc_gaps 看不见的缝。"""
    doc, _text = _rendered(ctx, "P1")
    broken = copy.deepcopy(doc)
    broken["public"]["prereg"]["values"] = {}
    text = M.build_system(broken, M.surface_from_doc(broken))
    gaps = M.rendered_gaps(text, broken)
    assert any(g.startswith("public.prereg.values") for g in gaps)
    # 合同把这条缝冻结成签署过的断言
    problems = PF.check_contract(
        {"schema": "simpraxis.rsi_r02.protocol_contract.v1"}, campaign_id="x")
    assert problems                      # 空合同当然不符（对照组，防止恒真）


def test_doc_gaps_catches_a_protocol_doc_missing_its_protocol_block(ctx):
    doc = _fixture(ctx, "P1").task_doc()
    broken = copy.deepcopy(doc)
    broken["public"]["interface"].pop("protocol", None)
    gaps = PF.doc_gaps(broken)
    assert "public.interface.protocol 缺失" in gaps


def test_candidate_loop_refuses_a_protocol_doc_with_rendered_gaps(ctx):
    """运行期闸：渲染文本不自足 ⇒ 零 POST 拒跑（不是花真实调用再怪模型）。"""
    doc = _fixture(ctx, "P1").task_doc()
    broken = copy.deepcopy(doc)
    broken["public"]["prereg"]["values"] = {}
    store = ST.StateStore(ctx)
    journal = ST.Journal(ctx)
    state = store.start(trial_id="proto_P1", instance_id="E-dev", family="E",
                        phase="protocol", slot_ids=["P-A-1"], post_cap=1)
    model = H.FakeModel([])
    book = B.BillingBook(ctx)
    with pytest.raises(CD.CandidateError) as exc:
        CD.CandidateLoop(
            ctx=ctx, trial=H.FakeTrial(), public_doc=broken, store=store,
            state=state, journal=journal, slots=ST.SlotBook(ctx), billing=book,
            counter=lambda t: 100, call_model=model, slot_ids=["P-A-1"])
    assert "不自足" in str(exc.value)
    assert model.requests == []          # 零 POST
    assert book.records() == []


# ---------------------------------------------------------------------------
# 行为级探针：只读渲染文本的 actor 必须能走通两条链
# ---------------------------------------------------------------------------


def test_probe_parses_every_instance_value_from_text_alone(ctx):
    fx = _fixture(ctx, "P1")
    doc = fx.task_doc()
    view = PF.parse_rendered(M.build_system(doc, M.surface_from_doc(doc)))
    assert view.nonce == fx.nonce
    assert view.values == fx.params
    assert view.action_order == PF.ACTION_ORDER
    assert set(PF.PREREG_REQUIRED) <= set(view.prereg_fields)
    assert set(PF.SUBMIT_REQUIRED) <= set(view.submit_fields)
    assert view.data_path == PF.DATA_PATH_TEMPLATE
    assert view.out_path == PF.ANALYZE_OUT_PATH
    assert view.stat_key == PF.STAT_KEY


def test_probe_refuses_text_that_drops_a_value(ctx):
    doc = _fixture(ctx, "P1").task_doc()
    text = M.build_system(doc, M.surface_from_doc(doc))
    stripped = "\n".join(x for x in text.splitlines()
                         if not x.startswith("* 本实例的参数取值"))
    with pytest.raises(PF.ProtocolError) as exc:
        PF.parse_rendered(stripped)
    assert "参数取值" in str(exc.value)


def test_probe_actor_drives_both_chains_and_protocol_axis_is_ok(ctx):
    """端到端零付费重演：探针 actor 走完 P1+P2，判决面必须判 PROTOCOL_OK。

    这条断言同时钉住「判决件是真实输出的」——不是靠手工 ``_finish`` 造假态。
    """
    posts = 0
    for fid in PF.FIXTURE_IDS:
        actor = PF.ProbeActor()
        rec = T4.run_chain(ctx, fid, caller=actor, counter=lambda t: 100,
                           gpus=False)
        assert rec["chain_ok"] is True, rec
        assert rec["status"] == ST.TRIAL_SUBMITTED
        assert rec["posts_sent"] == rec["posts_recorded"] == 4
        posts += rec["posts_sent"]
        # 探针确实只看文本：它收到的第一份 system 里带着 nonce
        assert _fixture(ctx, fid).nonce in actor.requests[0].system
    assert posts == len(PF.FIXTURE_IDS) * len(PF.ACTION_ORDER)
    verdict = CO.judge(CO.read_facts(ctx))
    assert verdict["protocol"]["status"] == CO.PROTOCOL_OK
    assert all(not ch["verdict_fail"]
               for ch in verdict["protocol"]["chains"].values())


def test_probe_with_lying_stat_is_labelled_blocked_protocol(ctx):
    """探针链上的「统计错」必须落成 verdict_fail ⇒ 判决面 BLOCKED_PROTOCOL。

    与 OK 路径成对：同一条 ``run_chain`` 通路，唯一差别是 submit 的 stat 被加 1。
    这条断言把「协议轴四态」里的负向一侧也钉在**真实链条输出**上，而不是手工
    伪造 trial 状态。
    """
    import dataclasses
    import json as _json

    class _LyingProbe(PF.ProbeActor):
        def __call__(self, request):
            reply = super().__call__(request)
            try:
                doc = _json.loads(reply.text)
            except ValueError:
                return reply
            if doc.get("action") == "submit":
                doc["stat"] = float(doc.get("stat") or 0.0) + 1.0
                return dataclasses.replace(
                    reply, text=_json.dumps(doc, ensure_ascii=False,
                                            sort_keys=True))
            return reply

    rec = T4.run_chain(ctx, "P1", caller=_LyingProbe(),
                       counter=lambda t: 100, gpus=False)
    assert rec["chain_ok"] is False
    assert rec["status"] == ST.TRIAL_SUBMITTED   # submit 结构合法 ⇒ 终态仍是 SUBMITTED
    verdict = CO.judge(CO.read_facts(ctx))
    assert verdict["protocol"]["status"] == CO.PROTOCOL_BLOCKED
    assert verdict["protocol"]["chains"]["proto_P1"]["verdict_fail"] is True
