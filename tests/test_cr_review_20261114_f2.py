"""2026-11-14 全仓评审 fix-2 批次的回归测试（runner / graph / agentic）。

覆盖 finding：CR-02 / CR-15 / CR-16 / CR-17 / CR-18 / CR-19 / CR-20 /
CR-21 / CR-23 / CR-42 / CR-43 / CR-44 / CR-45。

纪律：全部本地合成（fake 网关/向量，真实子进程仅用 sys.executable 的 stdlib
片段），0 付费、0 联网、0 容器。
"""
from __future__ import annotations

import argparse
import inspect
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.agent_runner import runner as runner_mod
from orchestrator.agent_runner.runner import AgentRunner, RunSpec, agent_run_seam
from orchestrator.agentic import loop as loop_mod
from orchestrator.agentic import wiring as wiring_mod
from orchestrator.agentic.caps import BudgetTracker
from orchestrator.agentic.forensics import ANSWER_TOOL, TraceStore
from orchestrator.agentic.loop import AgenticOutcome, Condenser
from orchestrator.common.callcache import CallReplay
from orchestrator.graph import cli as graph_cli
from orchestrator.graph import nodes as nodes_mod
from orchestrator.graph.nodes import (
    GATE_APPROVE,
    GATE_REJECT,
    GraphDeps,
    node_gate1,
    node_gate2,
    node_stage_frame,
    route_after_gate2,
)
from orchestrator.graph.roadmap import (
    Stage,
    narrow_spec_grants,
    stage_to_mapping,
    stage_view,
)

GATE_CFG = {"decision": GATE_REJECT, "human_notes": "请修"}


def _stage(sid: str, *, max_rounds: int = 8) -> Stage:
    return Stage(stage_id=sid, title=f"T {sid}", objective="obj",
                 exit_ac=("AC1",), tool_grants=("read_file",),
                 max_rounds=max_rounds)


# ---------------------------------------------------------------------------
# CR-02：子进程环境白名单（硬规则 4 边界）
# ---------------------------------------------------------------------------

def test_cr02_child_env_excludes_secrets_and_keeps_allowlist(
        monkeypatch, tmp_path):
    monkeypatch.setenv("SIMPRAXIS_LLM_TOKEN", "tok-" + "A" * 32)
    monkeypatch.setenv("HF_TOKEN", "hf_" + "a" * 32)
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_" + "b" * 32)
    monkeypatch.setenv("ARK_API_KEY", "ark-" + "c" * 24)
    monkeypatch.setenv("SIMPRAXIS_USE_PROXY", "0")
    monkeypatch.setenv("MY_PLAIN_HOST_VAR", "hello")
    out = runner_mod._default_subprocess(
        [sys.executable, "-c",
         "import os,json;print(json.dumps(dict(os.environ)))"],
        tmp_path, 30, {"SCENARIO_VAR": "1"})
    assert out.rc == 0
    env = json.loads(out.stdout.strip().splitlines()[-1])
    for leaked in ("SIMPRAXIS_LLM_TOKEN", "HF_TOKEN", "GITHUB_TOKEN",
                   "ARK_API_KEY", "MY_PLAIN_HOST_VAR"):
        assert leaked not in env, f"凭据/凭据形状变量泄漏进子进程：{leaked}"
    # 白名单与场景变量仍必须到位（不能把子进程环境清空到跑不起来）。
    assert env.get("PATH") == os.environ.get("PATH")
    assert env.get("SIMPRAXIS_USE_PROXY") == "0"
    assert env.get("SCENARIO_VAR") == "1"


# ---------------------------------------------------------------------------
# CR-21：超时整组杀 + 输出上限
# ---------------------------------------------------------------------------

def test_cr21_timeout_kills_grandchildren(tmp_path):
    pidfile = tmp_path / "grandchild.pid"
    code = (
        "import pathlib, subprocess, sys, time\n"
        "p = subprocess.Popen([sys.executable, '-c', "
        "'import time; time.sleep(60)'])\n"
        f"pathlib.Path({str(pidfile)!r}).write_text(str(p.pid))\n"
        "time.sleep(60)\n")
    started = time.time()
    with pytest.raises(subprocess.TimeoutExpired):
        runner_mod._default_subprocess(
            [sys.executable, "-c", code], tmp_path, 1.5, {})
    assert time.time() - started < 30
    assert pidfile.is_file(), "子进程未写出孙进程 pid"
    pid = int(pidfile.read_text())
    deadline = time.time() + 15
    alive = True
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            alive = False
            break
        time.sleep(0.1)
    if alive:  # 清理，避免测试自身泄漏进程
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        pytest.fail("超时后孙进程仍存活（killpg 未覆盖整组）")


def test_cr21_output_tail_is_capped():
    with tempfile.TemporaryFile() as fh:
        fh.write(b"A" * 100)
        fh.write(b"B" * (runner_mod._OUTPUT_TAIL_CAP + 50))
        text = runner_mod._read_tail(fh)
    assert text.startswith("[…输出前")
    assert text.endswith("B")
    assert len(text) <= runner_mod._OUTPUT_TAIL_CAP + 100


# ---------------------------------------------------------------------------
# CR-42：patch-safety 失败路径必须记 coding_round
# ---------------------------------------------------------------------------

class _FakeGateway:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def model_id(self, stage):  # noqa: ARG002
        return "fake-coding"

    def call(self, stage, messages, **kw):  # noqa: ARG002
        self.calls += 1
        assert self.responses, "已付出超过预期轮数的 coding 调用"
        return self.responses.pop(0), (None, None)


class _FakeVector:
    def query_kb(self, query, *, namespace, top_k):  # noqa: ARG002
        return []


def test_cr42_patch_safety_failure_records_coding_round(tmp_path):
    wd = tmp_path / "work"
    wd.mkdir()
    gw = _FakeGateway([json.dumps({"patches": [
        {"path": "../evil.py", "old": "", "new": "x = 1\n"}]})])
    runner = AgentRunner(gw, _FakeVector(), evidence_root=tmp_path / "ev")
    spec = RunSpec(thread_id="cr42", workdir=wd,
                   cmd=[sys.executable, "-c", "raise SystemExit(1)"],
                   telemetry_relpath="telemetry.json", batch_id="CR42")
    rec = runner.run(spec)
    assert rec["status"] == "failed"
    assert "补丁安全拒绝" in rec["reason"]
    assert gw.calls == 1
    assert rec["timeline"][0]["coding_round"] == 1
    doc = json.loads((wd / "FAILURE.json").read_text(encoding="utf-8"))
    assert doc["coding_round"] == 1, "patch-safety 失败轮未计入 FAILURE.json"


# ---------------------------------------------------------------------------
# CR-15 / CR-19：递归预算与节点外异常收口集合
# ---------------------------------------------------------------------------

def test_cr15_recursion_limit_scales_with_stage_count():
    assert graph_cli._recursion_limit(0) == graph_cli.RECURSION_LIMIT
    # 6 Stage：3 前缀 + 4*6 + 4*2*6（每次 Gate2 打回 4 步，D10 上限 2）+ 2
    assert graph_cli._recursion_limit(6) == 3 + 24 + 48 + 2
    assert graph_cli._recursion_limit(6) > 29  # 评审给出的超限场景


def test_cr19_closure_exc_covers_direction_gateway_and_recursion():
    assert issubclass(graph_cli.DirectionError, graph_cli._CLOSURE_EXC)
    assert issubclass(graph_cli.DirectionSchemaError, graph_cli._CLOSURE_EXC)
    assert issubclass(graph_cli.GatewayError, graph_cli._CLOSURE_EXC)
    assert issubclass(graph_cli.GraphRecursionError, graph_cli._CLOSURE_EXC)
    src = inspect.getsource(graph_cli._invoke)
    # GatewayPausedError 是 GatewayError 的子类：熔断必须由先行子句原样上抛。
    pause_at = src.index("except GatewayPausedError")
    closure_at = src.index("except _CLOSURE_EXC")
    assert pause_at < closure_at


# ---------------------------------------------------------------------------
# CR-18：节点外异常结案后拒绝重放
# ---------------------------------------------------------------------------

def test_cr18_closure_marker_blocks_reinvoke(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMPRAXIS_PHASE1_ROOT", str(tmp_path))
    from orchestrator.graph.state import run_dir_for
    marker = run_dir_for("tt_cr18") / graph_cli.CLOSURE_MARKER
    marker.write_text(json.dumps({"status": "closed"}), encoding="utf-8")
    called = []
    monkeypatch.setattr(graph_cli, "_production_deps",
                        lambda args: called.append(args))  # noqa: ARG005
    args = argparse.Namespace(thread="tt_cr18")
    assert graph_cli._invoke(args, resume=True) == 3
    assert called == [], "有终态标记时不得再构网关/重放图"


def test_cr18_closure_path_writes_marker(tmp_path, monkeypatch):
    run_dir = _invoke_with_boom(tmp_path, monkeypatch,
                                nodes_mod.GraphStateError("boom"),
                                thread="tt_cr18b")
    assert (run_dir / "FAILURE.json").is_file()
    marker = run_dir / graph_cli.CLOSURE_MARKER
    assert marker.is_file(), "节点外异常收口必须落线程级终态标记"
    doc = json.loads(marker.read_text(encoding="utf-8"))
    assert doc["error_type"] == "GraphStateError"


def _invoke_with_boom(tmp_path, monkeypatch, exc, *, thread):
    """跑一次 _invoke，让图在节点外抛 exc；返回该线程的 run 目录。"""
    import contextlib

    monkeypatch.setenv("SIMPRAXIS_PHASE1_ROOT", str(tmp_path))
    from orchestrator.graph.state import run_dir_for

    class _BoomGraph:
        def invoke(self, payload, config=None, **kwargs):  # noqa: ARG002
            raise exc

        def get_state(self, config):  # noqa: ARG002
            return SimpleNamespace(
                values={"thread_id": thread, "track": "track1",
                        "events": []}, next=())

    class _Closeable:
        def close(self):
            return None

    monkeypatch.setattr(
        graph_cli, "_production_deps",
        lambda args: (GraphDeps(), {"name": "q"}, _Closeable()))  # noqa: ARG005

    def _compiled(deps, db_path=None):  # noqa: ARG001
        return contextlib.nullcontext(_BoomGraph())

    monkeypatch.setattr(graph_cli, "_compiled", _compiled)
    args = argparse.Namespace(
        thread=thread, track="track1", task=2, batch_id="B",
        index=None, question="q", variant=None, stages_file=None)
    rc = graph_cli._invoke(args, resume=False)
    assert rc == 3
    # 同一标记也会拒绝再次重放。
    assert graph_cli._invoke(args, resume=True) == 3
    return run_dir_for(thread)


@pytest.mark.parametrize("case", ("direction", "recursion", "gateway"))
def test_cr19_closure_path_covers_new_exception_families(tmp_path, monkeypatch,
                                                         case):
    exc = {
        "direction": graph_cli.DirectionError("KB 零行"),
        "recursion": graph_cli.GraphRecursionError("步数超限"),
        "gateway": graph_cli.GatewayError("HTTP 500"),
    }[case]
    run_dir = _invoke_with_boom(tmp_path, monkeypatch, exc,
                                thread=f"tt_cr19_{case}")
    assert (run_dir / "FAILURE.json").is_file()
    doc = json.loads((run_dir / "FAILURE.json").read_text(encoding="utf-8"))
    assert doc["status"] == "human_failure"
    assert doc["error_type"] == type(exc).__name__


def test_cr19_gateway_pause_still_propagates(tmp_path, monkeypatch):
    """GatewayPausedError（预算熔断）不得被包成 human_failure。"""
    import contextlib

    monkeypatch.setenv("SIMPRAXIS_PHASE1_ROOT", str(tmp_path))
    from orchestrator.graph.state import run_dir_for

    pause = graph_cli.GatewayPausedError({"stage": "coding_debug"})

    class _BoomGraph:
        def invoke(self, payload, config=None, **kwargs):  # noqa: ARG002
            raise pause

        def get_state(self, config):  # noqa: ARG002
            return SimpleNamespace(values={}, next=())

    class _Closeable:
        def close(self):
            return None

    monkeypatch.setattr(
        graph_cli, "_production_deps",
        lambda args: (GraphDeps(), {"name": "q"}, _Closeable()))  # noqa: ARG005
    monkeypatch.setattr(graph_cli, "_compiled",
                        lambda deps, db_path=None: contextlib.nullcontext(
                            _BoomGraph()))  # noqa: ARG005
    args = argparse.Namespace(
        thread="tt_cr19_pause", track="track1", task=2, batch_id="B",
        index=None, question="q", variant=None, stages_file=None)
    with pytest.raises(graph_cli.GatewayPausedError):
        graph_cli._invoke(args, resume=False)
    run_dir = run_dir_for("tt_cr19_pause")
    assert not (run_dir / "FAILURE.json").exists()
    assert not (run_dir / graph_cli.CLOSURE_MARKER).exists()


# ---------------------------------------------------------------------------
# CR-16：Stage.max_rounds 真正执行
# ---------------------------------------------------------------------------

def test_cr16_narrow_spec_grants_enforces_remaining_rounds():
    stage = _stage("s1", max_rounds=8)
    spec = RunSpec(thread_id="t", workdir=Path("/tmp"), cmd=["true"],
                   telemetry_relpath="x.json", batch_id="B",
                   max_coding_rounds=5, tool_grants=("read_file",))
    state = {
        "stages": [stage_to_mapping(stage)],
        "stage_index": 0,
        "stage_frame": stage_view(stage, ("read_file",)),
        "stage_rounds": {"s1": 3},
    }
    narrowed = narrow_spec_grants(spec, state)
    assert narrowed.max_coding_rounds == 5  # min(工厂帽 5, 剩余 5)
    state["stage_rounds"] = {"s1": 6}
    assert narrow_spec_grants(spec, state).max_coding_rounds == 2
    state["stage_rounds"] = {"s1": 8}
    assert narrow_spec_grants(spec, state).max_coding_rounds == 0


def test_cr16_seam_refuses_when_stage_rounds_exhausted():
    stage = _stage("s1", max_rounds=8)
    state = {
        "stages": [stage_to_mapping(stage)],
        "stage_index": 0,
        "stage_frame": stage_view(stage, ("read_file",)),
        "stage_rounds": {"s1": 8},
    }
    called = []

    class _Runner:
        def run(self, spec):  # noqa: ARG002
            called.append(spec)
            raise AssertionError("剩余轮帽为 0 时不得发起付费调用")

    def factory(state):  # noqa: ARG001
        return RunSpec(thread_id="t", workdir=Path("/tmp"), cmd=["true"],
                       telemetry_relpath="x.json", batch_id="B",
                       max_coding_rounds=5, tool_grants=("read_file",))

    rec = agent_run_seam(_Runner(), factory)(state)
    assert rec["status"] == "failed" and called == []
    assert "轮帽" in rec["reason"]


# ---------------------------------------------------------------------------
# CR-20：Gate2 打回计数按 Stage 分键
# ---------------------------------------------------------------------------

def test_cr20_gate2_reject_count_is_per_stage():
    s1, s2 = _stage("s1"), _stage("s2")
    state = {
        "stages": [stage_to_mapping(s1), stage_to_mapping(s2)],
        "stage_index": 0,
        "stage_status": {},
        "stage_frame": stage_view(s1, ("read_file",)),
        "events": [],
        "gate2_rejects": 0,
    }
    deps = GraphDeps(gate2=lambda s: dict(GATE_CFG))
    for _ in range(2):
        out = node_gate2(state, deps)
        state = {**state, **{k: v for k, v in out.items() if k != "events"}}
        state["events"] = list(state["events"]) + list(out["events"])
    assert state["gate2_rejects"] == 2
    assert state["stage_rejects"] == {"s1": 2}
    assert route_after_gate2(state) == nodes_mod.TO_HUMAN_FAILURE

    # 换到 Stage 2：全局计数已是 2，但它自己只被拒一次 → 不得结案。
    state2 = {
        "stages": [stage_to_mapping(s1), stage_to_mapping(s2)],
        "stage_index": 1,
        "stage_status": {"s1": "approved"},
        "stage_frame": stage_view(s2, ("read_file",)),
        "events": [],
        "gate2_rejects": 2,
        "stage_rejects": {"s1": 2},
    }
    out2 = node_gate2(state2, deps)
    assert out2["stage_rejects"] == {"s1": 2, "s2": 1}
    merged = {**state2, **{k: v for k, v in out2.items() if k != "events"}}
    merged["events"] = list(state2["events"]) + list(out2["events"])
    assert route_after_gate2(merged) == nodes_mod.TO_STAGE_FRAME


# ---------------------------------------------------------------------------
# CR-43 / CR-44：Gate1 默认 choice 留痕 / stage_status 不被死写覆盖
# ---------------------------------------------------------------------------

def test_cr43_gate1_defaulted_choice_is_recorded():
    proposals = [{"name": "a"}, {"name": "b"}]
    out = node_gate1(
        {"proposals": proposals},
        GraphDeps(gate1=lambda s: {"decision": GATE_APPROVE}))  # noqa: ARG005
    ev = out["events"][0]
    assert ev["choice"] == 0 and ev["choice_defaulted"] is True
    out2 = node_gate1(
        {"proposals": proposals},
        GraphDeps(gate1=lambda s: {"decision": GATE_APPROVE, "choice": 1}))
    ev2 = out2["events"][0]
    assert ev2["choice"] == 1
    assert "choice_defaulted" not in ev2  # 真选 0/1 时事件形状不变


def test_cr44_stage_frame_retry_sets_running_not_rejected():
    stage = _stage("s1")
    out = node_stage_frame({
        "stages": [stage_to_mapping(stage)],
        "stage_index": 0,
        "stage_status": {"s1": "rejected"},
        "stage_frame": {},
        "events": [{"type": "gate2_rejected", "stage_id": "s1"}],
    })
    assert out["stage_status"]["s1"] == "running"
    assert out["stage_frame"]["stage_id"] == "s1"


# ---------------------------------------------------------------------------
# CR-17：断点续跑原语只在“最后一次尝试被打断”时恢复
# ---------------------------------------------------------------------------

class _FakeModel:
    def __init__(self):
        self.prefix = "0" * 64
        self.round_no = 0
        self.steps = 0

    def note_step(self, *, step, tool, args, normalized_obs_sha256):
        self.steps += 1
        self.prefix = f"{self.prefix[:32]}{self.steps:032x}"

    def restore_round(self, round_idx):
        self.round_no = round_idx


def _trace_row(tracer, *, round_idx, step, tool, observation):
    tracer.record(seq=None, round_idx=round_idx, step=step, tool=tool,
                  args={"k": step}, observation=observation,
                  policy={"decision": "allow", "reason": "",
                          "grant_source": "test"}, latency_ms=0)


def test_cr17_resume_helper_restores_only_interrupted_attempt(tmp_path):
    tracer = TraceStore(tmp_path / "tr", "t17")
    _trace_row(tracer, round_idx=1, step=1, tool="read_file",
               observation="hello")
    model = _FakeModel()
    budget = BudgetTracker(round_cap=5)
    info = loop_mod._resume_prefix_from_trace(
        model, tracer, workdir=tmp_path, budget=budget)
    assert info == {"steps_replayed": 1, "round": 1, "verified": True}
    assert model.steps == 1 and model.round_no == 1
    assert budget.steps_used == 1 and budget.rounds_used == 1

    # 上次尝试已自终局（__answer__）：新一次尝试不得灌回旧轨迹。
    _trace_row(tracer, round_idx=1, step=0, tool=ANSWER_TOOL,
               observation="done")
    model2, budget2 = _FakeModel(), BudgetTracker(round_cap=5)
    assert loop_mod._resume_prefix_from_trace(
        model2, tracer, workdir=tmp_path, budget=budget2) is None
    assert model2.steps == 0 and budget2.steps_used == 0


def test_cr17_resume_helper_tampered_trace_returns_none(tmp_path):
    tracer = TraceStore(tmp_path / "tr", "t17b")
    _trace_row(tracer, round_idx=1, step=1, tool="read_file",
               observation="hello")
    line = next(ln for ln in tracer.load_lines()
                if not str(ln["tool"]).startswith("__"))
    (tracer.root / line["obs_full_relpath"]).unlink()
    model = _FakeModel()
    assert loop_mod._resume_prefix_from_trace(
        model, tracer, workdir=tmp_path,
        budget=BudgetTracker(round_cap=5)) is None


class _ScriptedGateway:
    def __init__(self, text):
        self.text = text
        self.calls = 0

    def model_id(self, stage):  # noqa: ARG002
        return "fake-agentic"

    def call(self, stage, messages, **kw):  # noqa: ARG002
        self.calls += 1
        return self.text, (1, 1)


def test_cr17_inner_loop_invokes_resume_primitive(tmp_path, monkeypatch):
    """接缝真的调用了续写原语（不是只把函数留在模块里）。"""
    from orchestrator.agentic.wiring import parse_grant_strings
    seen = []
    monkeypatch.setattr(
        loop_mod, "_resume_prefix_from_trace",
        lambda *a, **k: seen.append(k) or None)
    gw = _ScriptedGateway('```python\nfinish(answer="done")\n```')
    out = loop_mod.run_agentic_inner_loop(
        workdir=tmp_path, task="do it",
        grants=parse_grant_strings(("finish",)), gateway=gw,
        budget=BudgetTracker(round_cap=1),
        tracer=TraceStore(tmp_path / "agentic_traces", "t17c"),
        thread_id="t17c", nonprod=True)
    assert out.kind == "finish"
    assert len(seen) == 1 and seen[0]["workdir"] == tmp_path


# ---------------------------------------------------------------------------
# CR-23：condenser 每次压缩只读一次 trace
# ---------------------------------------------------------------------------

class _CountingTracer:
    def __init__(self, lines):
        self.lines = lines
        self.loads = 0

    def load_lines(self):
        self.loads += 1
        return list(self.lines)

    def record(self, **kw):  # noqa: ARG002
        return None


class ActionStep:  # 与 smolagents 的 ActionStep 同类名即可（Condenser 按名筛）
    def __init__(self, step_number):
        self.step_number = step_number
        self.tool_calls = []
        self.observations = f"obs-{step_number}"
        self.model_output = ""


def test_cr23_condenser_loads_trace_once_per_condense():
    lines = [{"tool": "read_file", "round": r, "obs_sha256": f"{r:064d}",
              "policy_decision": {"decision": "allow"}} for r in (1, 2)]
    tracer = _CountingTracer(lines)
    condenser = Condenser(tracer, keep_full=1)
    agent = SimpleNamespace(memory=SimpleNamespace(
        steps=[ActionStep(1), ActionStep(2), ActionStep(3)]))
    condenser(None, agent)
    assert condenser.condensed_rounds == 2
    assert tracer.loads == 1, "压缩热路径不得逐轮重解析整份 trace"
    assert "obs-1" not in agent.memory.steps[0].observations
    assert "obs-2" not in agent.memory.steps[1].observations
    assert agent.memory.steps[2].observations == "obs-3"


# ---------------------------------------------------------------------------
# CR-45：agentic 失败件重放保留首写 ts_iso
# ---------------------------------------------------------------------------

def test_cr45_agentic_failure_keeps_first_ts(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMPRAXIS_PHASE1_ROOT", str(tmp_path / "phase1"))
    (tmp_path / "FAILURE.json").write_text(
        json.dumps({"status": "failed",
                    "ts_iso": "2020-01-01T00:00:00Z"}),
        encoding="utf-8")
    spec = SimpleNamespace(
        agentic=True, workdir=tmp_path, thread_id="t45", tool_grants=(),
        step_cap=0, token_cap=0, max_coding_rounds=1,
        agentic_trace_relpath=None, agentic_config=None,
        telemetry_relpath="telemetry.json", cmd=["true"],
        task_title="", gate2_notes=None)
    monkeypatch.setattr(
        wiring_mod, "run_agentic_inner_loop",
        lambda **kw: AgenticOutcome(kind="round_cap", reason="帽"))
    rec = wiring_mod.run_agentic_for_runspec(
        spec, gateway=object(), call_cache=CallReplay(tmp_path / "cache"))
    assert rec["status"] == "failed"
    doc = json.loads((tmp_path / "FAILURE.json").read_text(encoding="utf-8"))
    assert doc["ts_iso"] == "2020-01-01T00:00:00Z"
