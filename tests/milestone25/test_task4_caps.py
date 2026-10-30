"""Phase 2.5 Task4 AC-T4-10：三门预算算术 + 到帽优雅终局（0 付费）。

三门：每 run 工具步数帽 / 每 run 累计 token 帽 / coding 轮帽。到帽语义是
``CapExceeded``（BaseException：模型代码的 except Exception 与 CodeAgent
的包装都接不住）经硬化执行器映射为终局 CodeOutput，loop 读出 kind 后写
FAILURE 件——任何帽都不允许向环外抛裸异常。批次票/task cap 仍走 Task 0
phase25 命名空间，本件只锁运行时计数与终局分类，不新建配额文件格式。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.agentic.caps import (
    FAILURE_KINDS,
    KIND_FAIL,
    KIND_FINISH,
    KIND_ROUND_CAP,
    KIND_STEP_CAP,
    KIND_TOKEN_CAP,
    BudgetTracker,
    CapExceeded,
)
from orchestrator.agentic.forensics import TraceStore
from orchestrator.agentic.loop import (
    KIND_MAX_STEPS,
    AgenticOutcome,
    run_agentic_inner_loop,
)
from orchestrator.agentic.policy import GrantTable

_FENCE = "```python"


class ScriptedGateway:
    """按调用序号回放代码块；calls 数即账行代理。"""

    def __init__(self, plays, usage=(10, 20)):
        self.plays = list(plays)
        self.calls = []
        self._usage = usage

    def model_id(self, stage):
        return "unit-fake-model"

    def call(self, stage, messages, **kwargs):
        i = len(self.calls)
        self.calls.append({"stage": stage, "kwargs": kwargs})
        text = self.plays[min(i, len(self.plays) - 1)]
        return text, (self._usage[0] + i, self._usage[1] + i)


def _play(code):
    return f"{_FENCE}\n{code}\n```"


@pytest.fixture()
def workdir(tmp_path):
    wd = tmp_path / "wd"
    wd.mkdir()
    (wd / "a.txt").write_text("alpha\n")
    return wd


@pytest.fixture()
def tracer(tmp_path):
    return TraceStore(tmp_path / "agentic_traces", "thr-caps")


def _grants():
    return GrantTable(tools=frozenset({"read_file"}))


def _run(workdir, tracer, budget, plays, *, max_steps=None):
    return run_agentic_inner_loop(
        workdir=workdir, task="t", grants=_grants(),
        gateway=ScriptedGateway(plays), budget=budget, tracer=tracer,
        thread_id="thr-caps", max_steps=max_steps)


# ---------------------------------------------------------------------------
# 预算算术（纯计数，无 IO）
# ---------------------------------------------------------------------------

def test_budget_tracker_defaults_and_validation():
    b = BudgetTracker()
    # 默认：步/token 帽不显式限（0），轮帽 5；计数归零。
    assert (b.step_cap, b.token_cap, b.round_cap) == (0, 0, 5)
    assert (b.steps_used, b.tokens_used, b.rounds_used) == (0, 0, 0)
    for kwargs in (
        {"step_cap": -1}, {"token_cap": -1}, {"round_cap": -1},
        {"step_cap": True}, {"token_cap": 1.5}, {"round_cap": 0},
    ):
        with pytest.raises(ValueError):
            BudgetTracker(**kwargs)


def test_step_cap_arithmetic_boundary():
    b = BudgetTracker(step_cap=2)
    b.check_tool_step()            # 0/2 放行
    b.note_tool_step()             # →1
    b.check_tool_step()            # 1/2 放行（下一次调用后才到 2）
    b.note_tool_step()             # →2
    with pytest.raises(CapExceeded) as ei:
        b.check_tool_step()        # 2/2：下一次调用会超帽，终局
    assert ei.value.kind == KIND_STEP_CAP and "2/2" in ei.value.reason


def test_token_cap_arithmetic_no_partial_accounting():
    b = BudgetTracker(token_cap=20)
    b.add_usage(5, 5)               # 累计 10
    assert b.tokens_used == 10
    with pytest.raises(CapExceeded) as ei:
        b.add_usage(8, 8)           # 投影 26 > 20
    assert ei.value.kind == KIND_TOKEN_CAP
    # 到帽不做部分记账：累计值保持 10（终局 run 不允许半截计数）。
    assert b.tokens_used == 10
    b = BudgetTracker(token_cap=20)
    for bad in ((-1, 1), (1, -1), (True, 1), (1, 2.0)):
        with pytest.raises(ValueError):
            b.add_usage(*bad)


def test_round_cap_arithmetic_boundary():
    b = BudgetTracker(round_cap=2)
    b.add_round()                   # →1
    b.add_round()                   # →2（恰好到帽允许）
    with pytest.raises(CapExceeded) as ei:
        b.add_round()               # 第 3 轮：超 coding 轮帽
    assert ei.value.kind == KIND_ROUND_CAP and "2/2" in ei.value.reason


def test_cap_exceeded_is_base_exception():
    # 模型生成代码里的 except Exception 与 CodeAgent 的 Exception 包装
    # 都必须接不住终局信号（同 smolagents FinalAnswerException 设计）。
    assert issubclass(CapExceeded, BaseException)
    assert not issubclass(CapExceeded, Exception)
    with pytest.raises(CapExceeded):
        try:
            raise CapExceeded(KIND_STEP_CAP, "x")
        except Exception:           # noqa: BLE001 - 反证：接不住
            pytest.fail("CapExceeded 被 except Exception 捕获")


# ---------------------------------------------------------------------------
# 到帽端到端：优雅 FAILURE 件，不裸穿（fake gateway，0 付费）
# ---------------------------------------------------------------------------

def test_step_cap_produces_failure_artifact(workdir, tracer):
    budget = BudgetTracker(step_cap=1, round_cap=5)
    outcome = _run(workdir, tracer, budget,
                   [_play('read_file(path="a.txt")'),
                    _play('read_file(path="a.txt")')])
    assert outcome.kind == KIND_STEP_CAP
    assert outcome.is_failure_artifact
    assert outcome.steps_used == 1
    # 终局有 trace 在案，环外无裸异常（本测试能正常返回即证明）。
    assert Path(outcome.trace_path).exists()


def test_token_cap_produces_failure_artifact(workdir, tracer):
    # 首次 usage=(10,20)：投影 30 > 帽 5，generate 内即终局。
    budget = BudgetTracker(step_cap=0, token_cap=5, round_cap=5)
    outcome = _run(workdir, tracer, budget,
                   [_play('read_file(path="a.txt")')])
    assert outcome.kind == KIND_TOKEN_CAP
    assert outcome.is_failure_artifact
    assert "5" in outcome.reason


def test_round_cap_produces_failure_artifact_distinct_from_max_steps(
        workdir, tracer):
    # max_steps 显式放宽到 10：终局必须来自 coding 轮帽而非 smolagents
    # max_steps_error（两类终局不得互相冒充）。
    budget = BudgetTracker(step_cap=0, round_cap=2)
    outcome = _run(workdir, tracer, budget,
                   [_play('read_file(path="a.txt")')], max_steps=10)
    assert outcome.kind == KIND_ROUND_CAP
    assert outcome.is_failure_artifact
    assert outcome.rounds_used == 2


def test_max_steps_state_is_its_own_failure_kind(workdir, tracer):
    # 轮帽放宽、smolagents max_steps 先到：KIND_MAX_STEPS 也是 FAILURE 件。
    budget = BudgetTracker(step_cap=0, round_cap=10)
    outcome = _run(workdir, tracer, budget,
                   [_play('read_file(path="a.txt")')], max_steps=1)
    assert outcome.kind == KIND_MAX_STEPS
    assert outcome.is_failure_artifact


def test_control_primitives_still_terminate_at_step_cap(workdir, tracer):
    # 到帽时 finish/fail 必须仍能收尾（控制原语不占工具步帽）。
    budget = BudgetTracker(step_cap=1, round_cap=5)
    outcome = _run(workdir, tracer, budget,
                   [_play('read_file(path="a.txt")'),
                    _play('finish(answer="done", reason="r")')])
    assert outcome.kind == KIND_FINISH
    assert not outcome.is_failure_artifact
    assert outcome.value == "done"


def test_fail_primitive_is_failure_artifact(workdir, tracer):
    budget = BudgetTracker(round_cap=5)
    outcome = _run(workdir, tracer, budget,
                   [_play('fail(reason="不可恢复")')])
    assert outcome.kind == KIND_FAIL
    assert outcome.is_failure_artifact
    assert "不可恢复" in outcome.reason


def test_failure_kind_set_completeness():
    # loop 的失败件分类必须覆盖四帽 + fail；finish 永不在内。
    assert FAILURE_KINDS == frozenset(
        {KIND_FAIL, KIND_STEP_CAP, KIND_TOKEN_CAP, KIND_ROUND_CAP})
    for kind in FAILURE_KINDS:
        assert AgenticOutcome(kind=kind).is_failure_artifact
    assert not AgenticOutcome(kind=KIND_FINISH).is_failure_artifact
