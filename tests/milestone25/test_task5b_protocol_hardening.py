"""Phase 2.5 Task 5b：纯文本协议加固（0 付费 hermetic；冻结规格 task5b_spec.md）。

覆盖规格 §4 九项：
1-2 形态归因/反馈纯函数（含冻结 trace 逐字短样本）；
3   golden 双锚（上游 _step_stream sha256 + 库存 CodeAgent 行为锚）；
4   五空转剧本 E2E（空轮不免费/反馈逐轮在场/streak/工具执行一次）；
5   收敛剧本（见中文标签即合法形状，2 轮收敛）；
6   永散文触帽剧本（恰好帽次呼、0 工具执行）；
7   提示契约（旧冲突句移除/新锚点齐/T4 旧锚点保留）；
8   指纹诚实（新提示 ≠ 旧提示，保证真新出生）；
9   新件 import 白名单（re/typing/smolagents 三源）。
"""
from __future__ import annotations

import ast
import hashlib
import inspect
import itertools
from pathlib import Path

import pytest

from orchestrator.agentic.agent_code import ProtocolHardenedCodeAgent
from orchestrator.agentic.caps import KIND_FINISH, KIND_ROUND_CAP, BudgetTracker
from orchestrator.agentic.forensics import (
    TraceStore,
    agentic_cache_fingerprint,
)
from orchestrator.agentic.loop import run_agentic_inner_loop, system_contract
from orchestrator.agentic.policy import GrantTable
from smolagents.memory import ActionStep
from smolagents.memory import ToolCall as SmolToolCall
from smolagents.monitoring import Timing

from orchestrator.agentic.protocol import (
    LABEL_EMPTY,
    LABEL_NO_FENCE,
    LABEL_UNCLOSED,
    build_parse_feedback,
    classify_format_violation,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
FENCE = "```python"
MARKER = "[SimPraxis 协议精确提示]"

# --- 冻结 trace 逐字短样本（/mnt t5_freeze runs，0 新呼取证后嵌入） -------
WALLA_R2 = ("Thought: I need act with proper code block only per final "
            "instructions. First inspect workdir.")
WALLA_R4 = "Thought: I need emit actual code block."
WALLA_R5 = ("Thought: I will now call the directory listing tool in a valid "
            "Python code block.```")
WALLA_R6 = ('Thought: I will inspect the working directory.\n```python\n'
            'print(list_dir("."))\n```')
WALLB_R2 = ("Thought: I need inspect the work directory before creating the "
            "required module, then implement and independently exercise "
            "all APIs.")
WALLB_R4 = ('Thought: I will inspect the working directory before writing '
            'the solution.\n```python\nprint(list_dir("."))\n```')
OLD_CONFLICT_SENTENCE = "不要输出围栏以外的解释文本"


def _play(code: str) -> str:
    return f"{FENCE}\n{code}\n```"


class ScriptedGateway:
    """与 T4 同形：逐字回放；记录 messages 供反馈在场断言。"""

    def __init__(self, plays):
        self.plays = list(plays)
        self.calls = []

    def model_id(self, stage):
        return "unit-fake-model"

    def call(self, stage, messages, **kwargs):
        i = len(self.calls)
        self.calls.append({"stage": stage, "messages": messages,
                           "kwargs": kwargs})
        return self.plays[min(i, len(self.plays) - 1)], (1, 1)


class ReactiveGateway:
    """首轮发散文；一旦在上一轮观察见到中文精确标签即「学会」形状，
    此后永久发合法围栏（latch 固化，不因后续观察不含标签而退化）。"""

    def __init__(self, learned_play):
        self.calls = []
        self.learned = False
        self._learned_play = learned_play

    def model_id(self, stage):
        return "unit-fake-model"

    def call(self, stage, messages, **kwargs):
        self.calls.append({"messages": messages})
        prior = messages[-1]["content"][0]["text"] if messages else ""
        if MARKER in prior:
            self.learned = True
        if self.learned:
            return self._learned_play, (1, 1)
        return WALLA_R4, (1, 1)


@pytest.fixture()
def workdir(tmp_path):
    wd = tmp_path / "wd"
    wd.mkdir()
    return wd


@pytest.fixture()
def tracer(tmp_path):
    return TraceStore(tmp_path / "traces", "thr-5b")


def _grants():
    return GrantTable(tools=frozenset({"list_dir", "finish"}))


def _run(workdir, tracer, gateway, cap):
    seq = itertools.count(1)
    return run_agentic_inner_loop(
        workdir=workdir, task="t", grants=_grants(),
        gateway=gateway, budget=BudgetTracker(round_cap=cap),
        tracer=tracer, thread_id="thr-5b",
        seq_provider=lambda: next(seq)), gateway


# ---------------------------------------------------------------------------
# §4.1 分类器：冻结 trace 逐字样本
# ---------------------------------------------------------------------------

def test_classifier_frozen_prose_samples():
    # 散文与残缺裸栏在 parse 失败归因上合并（规格 §3.3 补记②-1）。
    for raw in (WALLA_R2, WALLA_R4, WALLB_R2, WALLA_R5):
        v = classify_format_violation(raw)
        assert v is not None and v["label"] == LABEL_NO_FENCE
        assert "自动补的结尾" in v["label_cn"]


def test_classifier_valid_hybrid_returns_none():
    # 合法 Thought+围栏（墙 A r6 / 墙 B r4 既证形状）绝不误伤。
    assert classify_format_violation(WALLA_R6) is None
    assert classify_format_violation(WALLB_R4) is None
    # 无围栏但本身合法 Python：框架 ast 裸 blob 分支会执行，不算空轮。
    assert classify_format_violation('print(list_dir("."))') is None


def test_classifier_empty_and_synthetic_shapes():
    assert classify_format_violation("")["label"] == LABEL_EMPTY
    assert classify_format_violation("   \n")["label"] == LABEL_EMPTY
    # 直接调用纯函数可归因（运行路径下框架 auto-close 会先补闭栏，见
    # test_stock_agent_autoclose_behavior_anchor 行为锚）。
    v = classify_format_violation('Thought: x\n```python\nprint(1)')
    assert v["label"] == LABEL_UNCLOSED


# ---------------------------------------------------------------------------
# §4.2 反馈：框架英文保留 + 纯常量、无任务信息
# ---------------------------------------------------------------------------

def test_feedback_preserves_framework_and_adds_precision():
    msg = build_parse_feedback(WALLA_R2,
                               "Error in code parsing:\nboom\n"
                               "Make sure to provide correct code blobs.", 3)
    assert msg.startswith("Error in code parsing:\nboom")  # 英文反馈逐字保留
    assert MARKER in msg
    assert "没有可执行的 ```python 代码围栏" in msg
    assert "连续第 3 次协议空转" in msg
    assert "计 1 次付费调用" in msg


def test_feedback_is_deterministic_and_task_free():
    a = build_parse_feedback(WALLA_R2, "frame", 1)
    b = build_parse_feedback(WALLA_R2, "frame", 1)
    assert a == b
    # 入参里没有任何任务/墙字段：不同任务同输入必须逐字节相同。
    assert "workdir" not in a and "wall" not in a


def test_feedback_streak_validation():
    with pytest.raises(ValueError):
        build_parse_feedback(WALLA_R2, "frame", 0)
    with pytest.raises(ValueError):
        build_parse_feedback(WALLA_R2, "frame", True)


# ---------------------------------------------------------------------------
# §4.3 golden 双锚
# ---------------------------------------------------------------------------

UPSTREAM_STEP_STREAM_SHA256 = (
    "e027f0eaa32a8d943f2fd8cb0a69298e03d2959a3f9792024a55c289dcf15cb7")


def test_golden_upstream_step_stream_hash():
    from smolagents.agents import CodeAgent
    import smolagents
    assert smolagents.__version__ == "1.26.0"
    src = inspect.getsource(CodeAgent._step_stream)
    assert hashlib.sha256(src.encode()).hexdigest() == \
        UPSTREAM_STEP_STREAM_SHA256


def test_stock_agent_behavior_anchor():
    """包装缝依赖的两个上游事实：parse 抛 AgentParsingError；
    且 model_output 在 parse 前已写入（含 auto-close 补的结尾 ```）。"""
    from smolagents.agents import AgentParsingError, CodeAgent

    gw = ScriptedGateway([WALLA_R2])
    model = _build_bare_model(gw, BudgetTracker(round_cap=4))
    agent = CodeAgent(tools=[], model=model, additional_authorized_imports=[],
                      max_steps=4, code_block_tags="markdown")
    step = ActionStep(step_number=1, timing=Timing(0.0))
    with pytest.raises(AgentParsingError):
        list(agent._step_stream(step))
    # 原文不以 ``` 结尾 → 框架补了一个结尾 ```；包装器即对该文本归因。
    assert step.model_output.startswith(WALLA_R2)
    assert step.model_output.rstrip().endswith("```")
    assert classify_format_violation(step.model_output)["label"] == \
        LABEL_NO_FENCE


def test_stock_agent_autoclose_behavior_anchor():
    """缺尾闭栏的合法围栏被 agents.py:1693 自动补栏后解析成功（ToolCall
    被 yield），证明 unclosed 归因只对纯函数直接调用可达。"""
    from smolagents.agents import CodeAgent

    unclosed = 'Thought: x\n```python\nprint(1)'
    gw = ScriptedGateway([unclosed])
    model = _build_bare_model(gw, BudgetTracker(round_cap=2))
    agent = CodeAgent(tools=[], model=model, additional_authorized_imports=[],
                      max_steps=2, code_block_tags="markdown")
    step = ActionStep(step_number=1, timing=Timing(0.0))
    # ToolCall 在执行前先 yield；tools=[] 时 print 被解释器拒（AgentExecution
    # Error），不影响本锚——本锚只钉「auto-close → parse 成功」这一事实。
    from smolagents.utils import AgentExecutionError
    gen = agent._step_stream(step)
    first = next(gen)
    assert isinstance(first, SmolToolCall)
    with pytest.raises(AgentExecutionError):
        list(gen)
    assert step.model_output.rstrip().endswith("```")


def _build_bare_model(gateway, budget):
    # 用产品模型缝（无 recorder/seq），仅服务行为锚；0 付费（fake gateway）。
    from orchestrator.agentic.model import build_gateway_model
    return build_gateway_model(gateway, budget=budget)


# ---------------------------------------------------------------------------
# §4.4 五空转剧本 E2E
# ---------------------------------------------------------------------------

def test_five_empty_rounds_then_action_and_finish(workdir, tracer):
    plays = [WALLA_R2, WALLA_R4, WALLB_R2, WALLA_R5, WALLA_R2,
             WALLA_R6, _play('finish(answer="done")')]
    outcome, gw = _run(workdir, tracer, ScriptedGateway(plays), cap=8)
    assert outcome.kind == KIND_FINISH
    # 空轮不免费：7 次实呼、7 轮预算、7 个 seq。
    assert len(gw.calls) == 7
    assert outcome.rounds_used == 7
    # 第 2..6 次呼的输入里都带精确反馈，标签对应前轮形态，streak 递增。
    payloads = [c["messages"][-1]["content"][0]["text"] for c in gw.calls]
    for i in range(1, 6):
        assert MARKER in payloads[i]
    assert "连续第 5 次协议空转" in payloads[5]
    # trace 七个模型轮行都带非 null seq；list_dir 恰好执行一次。
    import json
    lines = [json.loads(x) for x in
             Path(outcome.trace_path).read_text().splitlines()]
    model_rounds = [x for x in lines if x["tool"] == "__model_round__"]
    assert len(model_rounds) == 7
    assert [x["seq"] for x in model_rounds] == [1, 2, 3, 4, 5, 6, 7]
    assert outcome.steps_used == 1


# ---------------------------------------------------------------------------
# §4.5 收敛剧本
# ---------------------------------------------------------------------------

def test_feedback_converges_reactive_model(workdir, tracer):
    gw = ReactiveGateway(_play('finish(answer="done")'))
    seq = itertools.count(1)
    outcome = run_agentic_inner_loop(
        workdir=workdir, task="t",
        grants=GrantTable(tools=frozenset({"finish"})),
        gateway=gw, budget=BudgetTracker(round_cap=6),
        tracer=tracer, thread_id="thr-5b",
        seq_provider=lambda: next(seq))
    # 散文 1 轮 → 见中文精确标签即学会形状 → 次轮直接闭合围栏 finish，
    # 共 2 次模型呼、终局成立；关键是没有第三次空转。
    assert len(gw.calls) == 2
    assert MARKER in gw.calls[1]["messages"][-1]["content"][0]["text"]
    assert outcome.rounds_used == 2
    assert outcome.kind == KIND_FINISH
    # finish 终局不计工具执行步（见五空转 E2E：list_dir 才计 1 步）。
    assert outcome.steps_used == 0


# ---------------------------------------------------------------------------
# §4.6 永散文触帽：加固不放水
# ---------------------------------------------------------------------------

def test_always_prose_hits_round_cap(workdir, tracer):
    gw = ScriptedGateway([WALLA_R2])  # 末位回放
    outcome, gw = _run(workdir, tracer, gw, cap=5)
    assert outcome.kind == KIND_ROUND_CAP
    assert len(gw.calls) == 5
    assert outcome.steps_used == 0
    assert outcome.rounds_used == 5


# ---------------------------------------------------------------------------
# §4.7 提示契约
# ---------------------------------------------------------------------------

def test_system_contract_resolves_conflict_and_keeps_t4_anchors():
    p = system_contract()
    assert OLD_CONFLICT_SENTENCE not in p
    assert "Thought 与代码块成对" in p
    assert "正例" in p and "反例" in p
    assert "照样计 1 次付费调用" in p
    # T4 旧锚点一字不少（test_task4_context_dispatch 的同名断言继续绿）。
    for name in ("read_file", "list_dir", "grep", "write_file",
                 "apply_patch", "run_cmd", "run_probe", "recall",
                 "finish", "fail"):
        assert f"{name}(" in p
    for anchor in ("先读后改", "最小命令", "每轮自检",
                   "finish 不等于通过", "永不经过", "禁止 import", "dunder"):
        assert anchor in p
    # 装配的是包装子类。
    from smolagents.agents import CodeAgent
    assert issubclass(ProtocolHardenedCodeAgent, CodeAgent)


# ---------------------------------------------------------------------------
# §4.8 指纹诚实：提示变更必然产生新指纹（真新出生，防旧缓存命中）
# ---------------------------------------------------------------------------

def test_prompt_change_changes_fingerprint():
    kw = dict(stage="coding_debug", model="unit-fake-model", task="t",
              round_idx=1, prefix_hex="0" * 64, max_tokens=8192,
              temperature=0.6, thinking={"type": "disabled"})
    old_prompt = ("输出格式：每轮只输出一个 ```python 围栏代码块，块内是"
                  "调用工具的 Python 代码；不要输出围栏以外的解释文本。")
    fp_old = agentic_cache_fingerprint(system_prompt=old_prompt, **kw)
    fp_new = agentic_cache_fingerprint(system_prompt=system_contract(), **kw)
    assert fp_old != fp_new


# ---------------------------------------------------------------------------
# §7 R1：科学帽预注册修订（2026-10-30 用户逐字裁决）的活常量契约
# ---------------------------------------------------------------------------

def test_r1_cap_envelope_covers_g2_plus_5b_single_batch():
    """BLOCKER-1 关门钉：G2 已用 25（B2=16+B3=9，活账事实在付费批次前由
    门 1 三 lane 数过）+ 5b 唯一批次 B4(16)+B5(9)=25 + 污染储备 3 = 53。
    常量不足即红——防止常量被悄悄改回而 5b 批次重新不可执行。"""
    from orchestrator.gateway import quota_ledger as ql
    cap5 = ql.PHASE25_TASK_CAPS[5]
    g2_used = 25
    b5b = 16 + 9
    reserve = 3
    assert cap5 == g2_used + b5b + reserve == 53
    assert ql.PHASE25_HARD_CAP == 6 + 53 + 160 + 2 == 221
    assert ql.PHASE25_NORMAL_BUDGET == ql.PHASE25_HARD_CAP
    # 科学任务子集不变（改帽不改科学身份；再改仍须预注册）。
    assert 5 in ql.PHASE25_SCIENTIFIC_TASKS
    # 5b 批次全程不触任务帽的逐呼投影：B4 末呼 41、B5 末呼 50 均 < 53。
    assert g2_used + 16 == 41 < cap5 and g2_used + b5b == 50 < cap5


# ---------------------------------------------------------------------------
# §4.9 新件 import 白名单（零 os/sys/subprocess/socket/网络/model-id）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("relpath", [
    "orchestrator/agentic/protocol.py",
    "orchestrator/agentic/agent_code.py",
])
def test_new_modules_import_whitelist(relpath):
    tree = ast.parse((REPO_ROOT / relpath).read_text())
    allowed = {"__future__", "re", "typing", "smolagents",
               "orchestrator"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for n in node.names:
                root = n.name.split(".")[0]
                assert root in allowed, f"{relpath} 越白名单 import：{n.name}"
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.level and node.level >= 1:
                continue  # 包内相对导入（同目录产品件 .protocol）
            root = node.module.split(".")[0]
            assert root in allowed, f"{relpath} 越白名单 import：{node.module}"
    text = (REPO_ROOT / relpath).read_text()
    for banned in ("kimi", "glm-", "mimo", "deepseek", "http", "socket",
                   "subprocess", "open(", "eval(", "exec("):
        assert banned not in text, f"{relpath} 含禁项：{banned}"
