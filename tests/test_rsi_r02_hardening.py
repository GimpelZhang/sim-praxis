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
import json
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

    ``surface_from_doc`` 自 2026-11-10 起也能从 ``public.scene_params`` 还原
    ``timestep``；这里仍显式带上，以便「从文档还原」这条路被单独断言
    （``test_surface_from_doc_mirrors_runtime_fixed_params``）。
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


def test_rendered_gaps_numbers_match_as_whole_tokens(ctx):
    """数值必须按整段数字比对：``300`` 不许拿 ``3000`` 里的子串冒充在场。

    票八评审（2026-11-10，reviewer 车道）实测：删掉「本实例的参数取值」整行后，
    ``rendered_gaps`` 只报了 kp/kd/max_force，``steps=300`` 因为 ``"300" in "3000"``
    被判为在场——一个缺值被判成自足的假阴性。这条把「四个值全删后四个全报」钉死。
    """
    doc, text = _rendered(ctx, "P1")
    values = doc["public"]["prereg"]["values"]
    stripped = "\n".join(x for x in text.splitlines()
                         if not x.startswith("* 本实例的参数取值"))
    gaps = set(M.rendered_gaps(stripped, doc))
    expected = {f"public.prereg.values.{k}.值" for k in values}
    assert expected <= gaps, (expected - gaps, gaps)
    # 反面对照：完整文本里一个都不报（数值比对没有把正常渲染误伤成缺口）
    assert not any(g.startswith("public.prereg.values.") and g.endswith(".值")
                   for g in M.rendered_gaps(text, doc))
    # 直接钉 token 语义
    assert M._present("单次实验步数上限：3000", "300") is False
    assert M._present('"steps": 300}', "300") is True


def test_order_and_duplicate_are_not_promised_as_terminal(ctx):
    """跳步/重排/重复登记在实现里**不终止**（只回 ok=False 并计一次 POST）。

    票八评审的阻塞项：加固文本曾把它们与「未授权键/越界」并列写成终态，而
    ``candidate.POLICY_VIOLATION_CODES`` 只含 UNAUTHORIZED_KEY 与 OUT_OF_BOUNDS。
    公开文本必须与宿主语义一致：终态句只列这两类；跳步/重复另起一句说清
    「被拒但已计 POST，可在余量内改正，成功序列仍须恰为 ACTION_ORDER」。
    """
    doc, text = _rendered(ctx, "P1")
    # 只看边界段的两条 bullet：实例值/合同的 JSON 转储行里也常含这些词，
    # 用整行 text 会误伤（评审回放时就是这样红的）。
    bullets = [x for x in text.splitlines() if x.startswith("* ")]
    terminal = next(x for x in bullets if "策略违规是终态" in x)
    assert "白名单之外的键" in terminal and "越界" in terminal
    assert "跳步" not in terminal and "重复登记" not in terminal
    repairable = next(x for x in bullets if "动作跳步或重排" in x)
    assert "不是终态" in repairable
    assert "POST" in repairable           # 明确告知代价：已经计掉一次
    # 2026-11-10 root E reviewer E-HIGH-1：submit 是例外——它在槽位错配时仍被宿主
    # 接受并终态（实测：跳过 analyze 提前 submit ⇒ 3 POST、status=SUBMITTED）。
    assert "submit 除外" in repairable
    # 合同侧 retry_rule 同口径
    rule = doc["public"]["interface"]["protocol"]["retry_rule"]
    head, _, tail = rule.partition("**策略违规是终态**")
    assert "动作跳步或重排、重复登记" in head and "动作跳步" not in tail
    assert "恰好等于动作顺序" in rule


def test_sequence_once_promise_only_renders_for_protocol_docs(ctx):
    """「动作序列必须各一次」只是**协议链**判据，dev/validation 文档不得承诺。

    票八评审（2026-11-10，reviewer 车道）为 root C 的 LOCK 出具 REJECT 的证据：
    E-dev 的公开文档没有 ``public.interface.protocol`` 块，system 文本里却印着
    「必须恰好是 … 各一次——多一次或少一次都判整条链失败」；而宿主对 dev trial
    允许同一实例多次实验（计划 §7：≤12 次实验 / ≤8 次分析），root C 自己的 T3
    就成功跑了 D-dev 6 次 ``run_experiment`` 且 ``judge_pass=true``。承诺与语义
    相反 ⇒ 候选按文本办事会被判整条链失败。这条把「只在带 protocol 块的文档里
    渲染该句」钉死，正反两面都断言。
    """
    # 正面：协议文档（P1 fixture 自带 protocol 块）必须渲染
    _doc, proto_text = _rendered(ctx, "P1")
    assert "各一次" in proto_text and "整条链失败" in proto_text
    # 反面：dev 文档（write_public_task 无 protocol 块）一个词都不许出现
    dev_doc = H.write_public_task(ctx, "E-dev", family="E")
    assert "protocol" not in dev_doc["public"]["interface"]
    dev_text = M.build_system(dev_doc, _real_surface())
    for phrase in ("各一次", "整条链失败", "动作跳步或重排"):
        assert phrase not in dev_text, phrase
    # 直接调渲染函数时 protocol 缺省即不渲染（默认面向非协议 trial）
    assert "各一次" not in M.action_schema_text(_real_surface())
    assert "各一次" in M.action_schema_text(_real_surface(), protocol=True)


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


def test_local_producer_names_cover_every_actor(ctx):
    """零付费判据的白名单必须等于三个本地 actor **实际**上报的名字。

    票八 T3 首跑真被这条打红过：预演 actor 从 ``ProtocolStub`` 换成
    ``ProbeActor`` 后，它如实上报 ``protocol_probe``，而
    ``t3_control.zero_paid.only_stub_producers`` 还只认两个旧名字——容器里
    跑了 9 实例 + 两链之后才在报告里判 red。把「调用一次、取回包里的
    provider_model」做成断言，改名在单测就红，不必等容器。
    """
    import t3_control as T3C

    from orchestrator.rsi_r02 import model_stub as MS

    doc = _fixture(ctx, "P1").task_doc()
    request = M.build_request(
        system=M.build_system(doc, M.surface_from_doc(doc)),
        task_brief="开始", turns=(), counter=lambda _t: 1)
    policy = MS.StubPolicy(ladder=({"kp": 60.0, "kd": 8.0,
                                    "max_force": 30.0, "steps": 300},))
    actors = {
        "model_stub": MS.ModelStub(public_doc=H.write_public_task(
            ctx, "E-dev", family="E"), policy=policy),
        "protocol_stub": PF.ProtocolStub(fixture=_fixture(ctx, "P1")),
        "protocol_probe": PF.ProbeActor(),
    }
    for expected, actor in actors.items():
        reply = actor(request)
        assert reply.capture["provider_model"] == expected
        assert expected in T3C.LOCAL_PRODUCERS
    assert T3C.LOCAL_PRODUCERS == frozenset(actors)


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


# ---------------------------------------------------------------------------
# 2026-11-10 root D reviewer 车道：文本↔语义矛盾 / 指向不存在章节 / 合同期≡运行期
# ---------------------------------------------------------------------------


def test_repair_promise_is_not_falsely_exclusive(ctx):
    """HIGH：同一份 system 不许既说「可自修——但仅限这一类」，又说跳步/重排可改。

    宿主真语义（``rsi_r01/trial.py:152-158``）：除 ``candidate.
    POLICY_VIOLATION_CODES``（UNAUTHORIZED_KEY/OUT_OF_BOUNDS）终止外，
    PREREG_INVALID/CELL_DUPLICATED/RUN_ID_UNKNOWN 等一律 ``ok=False`` 非终态。
    「仅限这一类」是假陈述，且与紧邻的协议链 bullet 自相矛盾——root D reviewer
    即以此为 REJECT 依据。另钉死「空回复才是致命的」（candidate EMPTY_RESPONSE）。
    """
    from orchestrator.rsi_r01 import task_contract as TC
    from orchestrator.rsi_r01 import trial as TR

    texts = [_rendered(ctx, fid)[1] for fid in ("P1", "P2")]
    for iid in ("E-dev", "D-dev", "R-dev"):
        doc = H.write_public_task(ctx, iid, family=iid[0])
        texts.append(M.build_system(doc, TR.action_surface(TC.BY_ID[iid])))
    for text in texts:
        assert "仅限这一类" not in text
        assert "空回复会直接终止本 trial" in text
        assert "策略违规是终态" in text
    # 可改正类必须逐字点名（与宿主实现一致）
    for phrase in ("重复登记", "重复跑同一格", "run_id / analysis_id"):
        assert phrase in texts[0], phrase


def test_dev_docs_do_not_point_at_sections_they_lack(ctx):
    """MEDIUM：dev/validation 文档没有 public.prereg/submit，文本不得指向它们。"""
    doc = H.write_public_task(ctx, "E-dev", family="E")
    dev = M.build_system(doc, M.surface_from_doc(doc))
    for phrase in ("public.prereg.required_fields",
                   "public.submit.required_fields",
                   "required_fields 是字段要求的"):
        assert phrase not in dev, phrase
    proto = _rendered(ctx, "P1")[1]
    assert "public.prereg.required_fields" in proto
    assert "public.submit.required_fields" in proto
    assert "required_fields 是字段要求的" in proto


def test_surface_from_doc_mirrors_runtime_fixed_params(ctx):
    """MEDIUM-3：合同期渲染用的动作面必须与运行期 ``trial.surface`` 逐字段同形。

    root D reviewer 实测旧实现：合同期固定参数行印 ``{}``、运行期印
    ``{"timestep": 0.002}``——冻结的自足性证据与候选读到的文本不是同一份。
    """
    from orchestrator.rsi_r01 import schemas as S
    from orchestrator.rsi_r01 import task_contract as TC
    from orchestrator.rsi_r01 import trial as TR

    doc = _fixture(ctx, "P1").task_doc()
    sd, rt = M.surface_from_doc(doc), TR.action_surface(TC.BY_ID["E-dev"])
    assert sd.kind == rt.kind
    assert dict(sd.fixed_params) == dict(rt.fixed_params) == {"timestep": 0.002}
    assert dict(sd.param_bounds) == dict(rt.param_bounds)
    assert sd.max_steps == rt.max_steps
    # push_control 取公开 scene_params；pendulum 恒取 S.DT_S（与运行期同口径）
    dev = H.write_public_task(ctx, "E-dev", family="E")
    dev["public"]["scene_params"] = {"timestep": 0.002}
    assert dict(M.surface_from_doc(dev).fixed_params) == {"timestep": 0.002}
    dev["public"]["interface"]["kind"] = "pendulum_run"
    assert dict(M.surface_from_doc(dev).fixed_params) == {"timestep": float(S.DT_S)}


def test_rendered_gaps_catch_a_missing_fixed_param(ctx):
    """固定参数行丢了名字或数值都必须报 gap（合同期与运行期都用同一条断言）。"""
    doc, text = _rendered(ctx, "P1")
    assert M.rendered_gaps(text, doc) == []
    stripped = "\n".join(x for x in text.splitlines()
                         if not x.startswith("固定参数（"))
    gaps = set(M.rendered_gaps(stripped, doc))
    assert {"fixed_params.timestep", "fixed_params.timestep.值"} <= gaps


def test_retry_rule_counts_only_sent_posts(ctx):
    """LOW：发网前被拒的请求零 POST（candidate 预发阶段），重试口径必须说清楚。"""
    rule = _fixture(ctx, "P1").task_doc()["public"]["interface"]["protocol"]["retry_rule"]
    assert "已发网" in rule
    assert "发网前被拒的请求不占 POST" in rule
    assert "每次动作计一次 POST" not in rule


# ---------------------------------------------------------------------------
# 2026-11-10 root E reviewer 车道：submit 的终局语义 / 非协议文档不得印 nonce
# ---------------------------------------------------------------------------


def test_submit_is_never_described_as_repairable(ctx):
    """E-HIGH-1：submit 一旦被接受就结束 trial，文本必须**明说**，不许并入可改正类。

    实测（零付费重放，见 ``test_submit_without_nonce_terminates_the_trial``）：
    ``actions._parse_submit`` 不查 nonce/stat/stat_key，``trial._submit`` 只查引用
    是否存在就 ``ok=True``，``candidate`` 随即终态——与「缺字段…都会以 ok=false
    返回、可以改正」相反。修复前 root E 的候选读到的正是那句不实承诺。
    """
    proto = _rendered(ctx, "P1")[1]
    bullets = [x for x in proto.splitlines() if x.startswith("* ")]
    terminal_submit = next(x for x in bullets if "submit 是终局动作" in x)
    assert "立刻结束本 trial" in terminal_submit
    assert "没有任何改正机会" in terminal_submit
    assert "提前提交" in terminal_submit
    # 协议链专属的结链核验必须逐字告知（否则候选以为缺 nonce 会被当场拒）
    assert "nonce" in terminal_submit and "stat_key" in terminal_submit
    assert "整条链直接判失败" in terminal_submit
    # required_fields bullet 不得再声称 submit 缺字段会被当场拒绝
    req = next(x for x in bullets if "唯一权威" in x)
    assert "submit 缺字段**不会**在接收时被拒" in req
    # 非协议文档同样必须说清终局语义（只是不谈结链核验）
    dev_doc = H.write_public_task(ctx, "E-dev", family="E")
    dev = M.build_system(dev_doc, M.surface_from_doc(dev_doc))
    dev_bullets = [x for x in dev.splitlines() if x.startswith("* ")]
    assert any("submit 是终局动作" in x for x in dev_bullets)
    assert "整条链" not in dev                        # 不把协议判据说给 dev


def test_dev_docs_do_not_print_nonce_in_the_action_shape(ctx):
    """E-LOW-1：dev/validation 文档没有 nonce，动作形状不得印它。"""
    dev_doc = H.write_public_task(ctx, "E-dev", family="E")
    dev = M.build_system(dev_doc, M.surface_from_doc(dev_doc))
    shape = next(x for x in dev.splitlines() if x.startswith('4) {"schema"'))
    assert "nonce" not in shape and "stat_key" not in shape
    proto_shape = next(x for x in _rendered(ctx, "P1")[1].splitlines()
                       if x.startswith('4) {"schema"'))
    assert '"nonce":"<逐字>"' in proto_shape and '"stat_key":"<逐字>"' in proto_shape


def test_submit_without_nonce_terminates_the_trial(ctx):
    """行为级钉子：文本承诺的终局语义必须与真实链条一致（不是单测自说自话）。

    两条重放都零付费：(1) submit 去掉 nonce/stat；(2) 跳过 analyze 提前 submit。
    两者都必须是 4/3 POST 被收下、status=SUBMITTED（终态）、判决面 BLOCKED_PROTOCOL
    ——这正是文本现在写明的事实。若哪天宿主改成「拒绝缺字段并给改正机会」，
    这条测试会红，逼着文本一起改。
    """
    import dataclasses
    import json as _json

    class _DropNonce(PF.ProbeActor):
        def __call__(self, request):
            reply = super().__call__(request)
            try:
                doc = _json.loads(reply.text)
            except ValueError:
                return reply
            if doc.get("action") == "submit":
                doc.pop("nonce", None)
                doc.pop("stat", None)
                return dataclasses.replace(
                    reply, text=_json.dumps(doc, ensure_ascii=False))
            return reply

    class _EarlySubmit(PF.ProbeActor):
        def __call__(self, request):
            reply = super().__call__(request)
            try:
                doc = _json.loads(reply.text)
            except ValueError:
                return reply
            if doc.get("action") == "analyze":
                return dataclasses.replace(reply, text=_json.dumps({
                    "schema": doc.get("schema"), "action": "submit",
                    "claim": "SUPPORTED", "run_ids": doc.get("run_ids") or [],
                    "analysis_ids": [], "nonce": "x", "stat": 0.0},
                    ensure_ascii=False))
            return reply

    for fid, actor, expected_posts in (("P1", _DropNonce, 4),
                                       ("P2", _EarlySubmit, 3)):
        rec = T4.run_chain(ctx, fid, caller=actor(), counter=lambda t: 100,
                           gpus=False)
        assert rec["chain_ok"] is False, rec
        assert rec["status"] == ST.TRIAL_SUBMITTED, rec
        assert rec["posts_sent"] == expected_posts, rec
    verdict = CO.judge(CO.read_facts(ctx))
    assert verdict["protocol"]["status"] == CO.PROTOCOL_BLOCKED


def test_blanket_repair_promise_carries_the_submit_exclusion(ctx):
    """E-HIGH-1 的另一半：通用句不得再无条件覆盖 submit。"""
    text = _rendered(ctx, "P1")[1]
    bullets = [x for x in text.splitlines() if x.startswith("* ")]
    general = next(x for x in bullets if "空回复会直接终止本 trial" in x)
    assert "除 submit 以外" in general
    assert "不终止**本 trial" in general
    dev_doc = H.write_public_task(ctx, "E-dev", family="E")
    dev = M.build_system(dev_doc, M.surface_from_doc(dev_doc))
    assert "除 submit 以外" in next(x for x in dev.splitlines()
                                   if "空回复会直接终止本 trial" in x)


def test_research_block_reaches_the_text_verbatim(ctx):
    """E-MED-1：R 族公开文档的 research 整块必须落进文本。

    ``evaluator.check_prereg`` 对 R 族按 ``research.damping_levels ×
    initial_angles`` 做四格集合相等，旧渲染只印 h/band/window，把这些判据轴丢在
    纸面之外；``rendered_gaps`` 又在非协议文档上直接返回空，两道闸一起漏。现在
    整块 canonical JSON 渲染 + 逐字在场断言（非协议文档也查）。
    """
    doc = H.write_public_task(ctx, "R-dev", family="R")
    doc["public"].pop("goal", None)
    doc["public"]["research"] = {
        "h": 1, "question": "阻尼摆问题", "window_s": 4.0,
        "band": {"theta_rad": 0.05, "omega_rad_s": 0.05, "strict": True},
        "damping_levels": [0.15, 0.45], "initial_angles": [0.8, -0.8], "delta": 0.1,
    }
    text = M.build_system(doc, M.surface_from_doc(doc))
    assert json.dumps(doc["public"]["research"], ensure_ascii=False,
                      sort_keys=True) in text
    assert "目标：阻尼摆问题" in text           # 没有 goal 时用问题，不印 None
    assert M.rendered_gaps(text, doc) == []
    stripped = "\n".join(x for x in text.splitlines()
                         if not x.startswith("公开研究设定："))
    assert "public.research" in M.rendered_gaps(stripped, doc)
    # goal 是对象时同样整块渲染（E/D 族）
    goal_doc = H.write_public_task(ctx, "E-dev", family="E")
    goal_doc["public"]["goal"] = {"type": "x", "target": [0.5, 0.0]}
    gt = M.build_system(goal_doc, M.surface_from_doc(goal_doc))
    assert json.dumps(goal_doc["public"]["goal"], ensure_ascii=False,
                      sort_keys=True) in gt
    assert M.rendered_gaps(gt, goal_doc) == []
