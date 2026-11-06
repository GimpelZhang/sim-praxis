"""P281-4 两臂参数面测试（AC-P281-4-01 / -02 / -03，计划 §8.7）。

三件事：

1. **参数等价性**（AC-01，双向）：两臂在「花多少」上逐值相等、在「是什么」
   上逐值不同。少一条，比较就变成两组不同预算或同一条臂的自我对比。
2. **零 KB 召回**（AC-02）：``AGENTIC_GRANTS`` 不含 ``recall``；legacy 臂的
   ``AgentRunner.recall`` 保持 ``None``，第二位置（``vector_engine``）拿到的是
   :class:`NullRecallEngine`；真实 ``VectorEngine.query_kb`` 在两臂分派路径上
   一次都不被调用。
3. **替身面**（AC-03）：``NullRecallEngine`` 四个入口恒空、永不抛——它是
   「问过、但问的是一个恒空的替身」的实验设计物，不是省事的 mock。

AC-01 的一条**锁内适配**（登记在 ``Phase_2.8.1_Plan_Complete.md`` T4 段）：
计划把它写成「``{engine, runspec_agentic, tool_grants, step_cap, token_cap,
agentic_config}`` 六项逐值不同」，但 ``agentic_config`` 在两臂上**必须**同为
``None``——AC-15 要求 ``cache_epoch_for(None) == cache_epoch_for(AgenticConfig())``，
即两臂同一缓存纪元；若让它们不同，就等于给 agentic 臂开了修复开关，实验的
自变量会从「引擎」变成「引擎+增强」。故本测试断言：五项真自变量逐值不同、
``agentic_config`` 两臂同为 ``None``（并把「不可由它区分」写成显式断言而不是
默默跳过），且 ``arm_config_digest`` 两臂不等（配置面整体可分）。
"""
from __future__ import annotations

import dataclasses
import inspect
from types import SimpleNamespace

import pytest

from orchestrator.benchmark.phase281 import arm_adapter, constants

#: 计划 §8.5.1 的「必须逐值相同」面
SAME_FIELDS = ("max_coding_rounds", "call_cap")
#: 计划 §8.5.1 的「必须逐值不同」面
DIFF_FIELDS = ("engine", "runspec_agentic", "tool_grants", "step_cap",
               "token_cap", "agentic_config")

LEGACY = "legacy_v1"
AGENTIC = "agentic_v1"


# --------------------------------------------------------------------------
# AC-P281-4-01：两臂参数等价（双向）
# --------------------------------------------------------------------------

def test_ac_p281_4_01_arm_parity():
    """同帽、异引擎；帽值必须等于常量表，不许某臂偷偷调小（STOP-15）。"""
    assert set(arm_adapter.ARMS) == set(constants.ARMS) == {LEGACY, AGENTIC}
    legacy = arm_adapter.ARMS[LEGACY].to_json()
    agentic = arm_adapter.ARMS[AGENTIC].to_json()

    # ① 「花多少」逐值相等
    for name in SAME_FIELDS:
        assert legacy[name] == agentic[name], name
    assert legacy["max_coding_rounds"] == constants.MAX_CODING_ROUNDS
    assert agentic["max_coding_rounds"] == constants.MAX_CODING_ROUNDS
    assert legacy["call_cap"] == constants.CALL_CAP_PER_TRIAL
    assert agentic["call_cap"] == constants.CALL_CAP_PER_TRIAL

    # ② 「是什么」逐值不同（agentic_config 见模块 docstring 的锁内适配）
    differs = {name for name in DIFF_FIELDS if legacy[name] != agentic[name]}
    assert differs == {"engine", "runspec_agentic", "tool_grants",
                       "step_cap", "token_cap"}, differs
    # agentic_config 两臂同为 None 是**契约**（AC-15 同一缓存纪元），不是遗漏：
    assert legacy["agentic_config"] is None
    assert agentic["agentic_config"] is None
    # …因此两臂的可分性必须由别的面承担：规范化配置摘要不等
    assert (arm_adapter.arm_config_digest(LEGACY)
            != arm_adapter.arm_config_digest(AGENTIC))

    # ③ 各自的自变量取值
    assert legacy["engine"] == "agent_runner"
    assert agentic["engine"] == "agentic_loop"
    assert legacy["runspec_agentic"] is False
    assert agentic["runspec_agentic"] is True
    assert legacy["tool_grants"] == []
    assert tuple(agentic["tool_grants"]) == arm_adapter.AGENTIC_GRANTS
    assert legacy["step_cap"] == 0 and legacy["token_cap"] == 0
    assert agentic["step_cap"] == constants.STEP_CAP_PER_TRIAL
    assert agentic["token_cap"] == constants.TOKEN_CAP_PER_TRIAL

    # ④ 表不可变：冻结表 + 冻结数据类（改只能改源码并重跑 AC）
    with pytest.raises(TypeError):
        arm_adapter.ARMS[LEGACY] = arm_adapter.ARMS[AGENTIC]   # type: ignore[index]
    with pytest.raises(dataclasses.FrozenInstanceError):
        arm_adapter.ARMS[AGENTIC].call_cap = 1                 # type: ignore[misc]

    # ⑤ 参数面与 digest 同源：digest 是 to_json 面的规范化 sha256
    assert arm_adapter.arm_config_digest(LEGACY) == arm_adapter.arm_config_digest(
        arm_adapter.ARMS[LEGACY])
    assert (arm_adapter.canonical_json(legacy)
            != arm_adapter.canonical_json(agentic))


def test_ac_p281_4_01b_runspec_default_stays_frozen():
    """``RunSpec.agentic`` 的缺省值保持 ``False``（解冻只在臂分支上显式给）。"""
    from orchestrator.agent_runner.runner import RunSpec

    by_name = {f.name: f for f in dataclasses.fields(RunSpec)}
    assert by_name["agentic"].default is False
    assert by_name["tool_grants"].default == ()
    assert by_name["step_cap"].default == 0
    assert by_name["token_cap"].default == 0
    assert by_name["agentic_config"].default is None


def test_ac_p281_4_01c_armspec_rejects_table_lies():
    """表里不一的四种构造一律拒（不靠人眼，靠构造期）。"""
    base = dict(max_coding_rounds=10, call_cap=10)
    with pytest.raises(arm_adapter.ArmSpecError):     # 非 agentic 臂带授予串
        arm_adapter.ArmSpec(arm_id=LEGACY, engine="agent_runner",
                            runspec_agentic=False, tool_grants=("read_file",),
                            step_cap=0, token_cap=0, **base)
    with pytest.raises(arm_adapter.ArmSpecError):     # engine 与布尔不同向
        arm_adapter.ArmSpec(arm_id=AGENTIC, engine="agent_runner",
                            runspec_agentic=True, tool_grants=(),
                            step_cap=0, token_cap=0, **base)
    with pytest.raises(arm_adapter.ArmSpecError):     # 未知臂
        arm_adapter.ArmSpec(arm_id="nope_v1", engine="agent_runner",
                            runspec_agentic=False, tool_grants=(),
                            step_cap=0, token_cap=0, **base)
    with pytest.raises(arm_adapter.ArmSpecError):     # 非 agentic 臂带 config
        arm_adapter.ArmSpec(arm_id=LEGACY, engine="agent_runner",
                            runspec_agentic=False, tool_grants=(),
                            step_cap=0, token_cap=0, agentic_config=object(),
                            **base)


# --------------------------------------------------------------------------
# AC-P281-4-02：零 KB 召回（静态面 + 分派面 + 运行面三处）
# --------------------------------------------------------------------------

def test_ac_p281_4_02_no_kb_recall_both_arms(monkeypatch):
    """两臂都不许看 KB：授予串里没有、分派里没有、真实入口一次不被调用。"""
    # ① 静态：授予串不含 recall，也不含任何以 recall 起头的形态
    assert "recall" not in arm_adapter.AGENTIC_GRANTS
    assert not [g for g in arm_adapter.AGENTIC_GRANTS
                if g == "recall" or g.startswith("recall:")]
    # arm_adapter 自身不构造真实向量引擎（依赖方向：引擎层不认识 KB 存储）
    source = inspect.getsource(arm_adapter)
    assert "VectorEngine(" not in source

    # ② 分派面：legacy 走 AgentRunner，第二位置（vector_engine）是零召回替身、
    #    recall 注入通路保持 None；agentic 走 wiring，kwargs 里没有 recall
    calls: "dict[str, object]" = {}

    class _Runner:
        def __init__(self, *args, **kwargs):
            calls["legacy_args"] = args
            calls["legacy_kwargs"] = kwargs

        def run(self, spec):
            return {"status": "ok", "engine": "agent_runner"}

    def _agentic(spec, **kwargs):
        calls["agentic_args"] = (spec,)
        calls["agentic_kwargs"] = kwargs
        return {"status": "ok", "engine": "agentic_loop"}

    monkeypatch.setattr("orchestrator.agent_runner.runner.AgentRunner", _Runner)
    monkeypatch.setattr("orchestrator.agentic.wiring.run_agentic_for_runspec",
                        _agentic)

    # ③ 真实向量引擎：一次调用即失败（计数器比「断言没发生」更难糊弄）
    real_queries: "list[tuple]" = []

    def _boom(self, *args, **kwargs):
        real_queries.append((self, args, kwargs))
        raise AssertionError("两臂都不许调用真实 VectorEngine（AC-P281-4-02）")

    monkeypatch.setattr("memory_flywheel.vector_engine.VectorEngine.query_kb", _boom)
    monkeypatch.setattr("memory_flywheel.vector_engine.VectorEngine.query_solutions",
                        _boom)

    recall = arm_adapter.NullRecallEngine()
    ctx = SimpleNamespace(
        runspec=object(), gateway=object(), call_cache=object(),
        ledger=object(), evidence_dir="/tmp/evidence",
        sandbox=SimpleNamespace(subprocess_runner=lambda: object(),
                                run_backend=lambda: object()),
        recall=recall)
    # 只保留同一份 ctx：两臂拿到的是**同一个**零召回替身（实验设计：平等地不给）
    assert arm_adapter.run_arm(LEGACY, ctx)["engine"] == "agent_runner"
    assert arm_adapter.run_arm(AGENTIC, ctx)["engine"] == "agentic_loop"

    legacy_args, legacy_kwargs = calls["legacy_args"], calls["legacy_kwargs"]
    assert legacy_args[0] is ctx.gateway
    assert legacy_args[1] is recall, "legacy 臂的 vector_engine 必须是零召回替身"
    assert legacy_kwargs.get("recall") is None, "legacy 臂不得注入生产召回"
    assert legacy_kwargs.get("call_cache") is ctx.call_cache

    agentic_kwargs = calls["agentic_kwargs"]
    assert "recall" not in agentic_kwargs
    assert agentic_kwargs.get("gateway") is ctx.gateway
    assert agentic_kwargs.get("ledger") is ctx.ledger
    assert agentic_kwargs.get("call_cache") is ctx.call_cache

    assert real_queries == [], real_queries


# --------------------------------------------------------------------------
# AC-P281-4-03：零召回替身的四个入口恒空、永不抛
# --------------------------------------------------------------------------

def test_ac_p281_4_03_null_recall_engine(monkeypatch):
    """``query_kb``/``query_solutions``/``retrieve``/``record_adoption`` 全恒空。"""
    # 真实引擎一旦被这个替身碰到就炸（替身不许转调真引擎）
    monkeypatch.setattr(
        "memory_flywheel.vector_engine.VectorEngine.query_kb",
        lambda self, *a, **k: (_ for _ in ()).throw(AssertionError("碰了真引擎")))
    monkeypatch.setattr(
        "memory_flywheel.vector_engine.VectorEngine.query_solutions",
        lambda self, *a, **k: (_ for _ in ()).throw(AssertionError("碰了真引擎")))

    recall = arm_adapter.NullRecallEngine()
    assert recall.calls == 0

    for namespace in ("coding", "direction", "troubleshooting", "capabilities"):
        assert recall.query_kb("出口在哪里", namespace=namespace, top_k=3) == []
    assert recall.query_solutions("track1_tools/03_libero") == []
    assert recall.query_solutions() == []          # 全自由参数也不抛
    hit = recall.retrieve("sim.py rc!=0", site="coding")
    assert hit == {"kb_hits": [], "learning_hits": []}
    assert recall.record_adoption(thread_id="t", site="coding",
                                  learning_id="l", prompt_text="p",
                                  learning_text="x") is None

    # 记账面：问过 4+2+1 次，全部落在替身上；采纳记录是**空操作**
    # （只记三元组，不落 prompt/learning 正文——替身绝不写任何存储）
    assert recall.calls == 7, recall.calls
    assert recall.adoptions == [{"thread_id": "t", "site": "coding",
                                 "learning_id": "l"}]
    assert all(q["top_k"] in (0, 3) for q in recall.queries)
    assert sum(1 for q in recall.queries if q["namespace"] == "retrieve") == 1

    # 边界输入（None/空串）也不许抛：旧引擎在失败链上会传空 hint
    assert recall.query_kb("") == []
    assert recall.retrieve("") == {"kb_hits": [], "learning_hits": []}
