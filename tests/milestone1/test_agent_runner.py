#!/usr/bin/env python3
"""Task4 单测：agent_runner 多轮补救（0 付费，gateway/vector 全 fake）。

契约场景（计划 L505）：
  ① 假脚本第 1 次失败、补丁后第 2 次成功 → 两轮收敛，恰好 1 次 coding 调用；
  ② 越界补丁（企图改 workdir 外 stage_models.yaml）被拒、不发起重试、写 FAILURE；
  ③ 5 轮全败：恰好 5 次 coding_debug、FAILURE.json、图进 human_failure；
     失败 run 再次进入在计数处硬停（GatewayPausedError 子类），零网络；
  ④ Gate2 打回后的 coding 轮次按 run 独立，与 gate2_rejects 各算各的；
  另：runner 全部消息纯文本（硬规则1 视觉禁用）；troubleshooting top_k=6、
  字段 text + 引用列注。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from langgraph.checkpoint.sqlite import SqliteSaver

from memory_flywheel.vector_engine import VectorEngine  # noqa: F401 （类型契约）
from orchestrator.agent_runner import (
    MAX_CODING_ROUNDS,
    AgentRunner,
    CodingLimitError,
    RunSpec,
    agent_run_seam,
)
from orchestrator.agent_runner.runner import _collect_snippets
from orchestrator.common.callcache import SCHEMA as REPLAY_SCHEMA, CallReplay
from orchestrator.gateway.router import GatewayPausedError
from orchestrator.graph import GraphDeps, build_graph, file_backed_evaluate, initial_state
from orchestrator.graph.nodes import write_graph_failure

# 图级复用件（定义在 test_graph，同包导入）
from tests.milestone1.test_graph import (  # noqa: E402
    FakeDirection,
    FakePersist,
    ScriptedGate,
)


CONVERGING_SCRIPT = '''
from pathlib import Path
wd = Path(__file__).parent
log = wd / "runs.log"
log.write_text((log.read_text() + "x") if log.exists() else "x")
fix = (wd / "fix.txt").read_text().strip()
if fix != "PATCHED":
    raise RuntimeError("fixture failure: waiting for patch")
(wd / "telemetry.json").write_text(
    '{"_fixture": true, "_formula": "constant", "engine": "fixture-engine"}')
'''

ALWAYS_FAIL_SCRIPT = '''
from pathlib import Path
wd = Path(__file__).parent
log = wd / "runs.log"
log.write_text((log.read_text() + "x") if log.exists() else "x")
raise RuntimeError("fixture failure: always fails")
'''


class FakeGateway:
    def __init__(self, responses: list[str], usage=(None, None)):
        self.responses = list(responses)
        self.usage = usage
        self.calls: list[dict] = []

    def model_id(self, stage):
        return "fake-coding-redacted-0001"

    def call(self, stage, messages, **kw):
        # 硬规则1：任何 image block 都不该出现在 runner 请求里
        for mm in messages:
            assert isinstance(mm.get("content"), str), \
                "runner 请求含非文本 content（视觉禁用被破坏）"
            assert '"type": "image"' not in mm["content"] \
                and '"type":"image"' not in mm["content"]
        self.calls.append({"stage": stage, "messages": messages, "kw": kw})
        if not self.responses:
            raise AssertionError("第 6 次 coding 调用：硬上限被突破")
        return self.responses.pop(0), self.usage


class FakeVector:
    def __init__(self):
        self.queries: list[dict] = []

    def query_kb(self, query, *, namespace, top_k):
        self.queries.append({"q": query, "namespace": namespace, "top_k": top_k})
        assert namespace == "troubleshooting" and top_k == 6
        return [{"text": f"夹具排障知识条目 {i}（字段名必须是 text）",
                 "evidence_refs": [f"E{i}"],
                 "kb_relpath": f"knowledge/projects/fixture/troubleshooting.md",
                 "section_path": f"s{i}"} for i in range(6)]


def _write_workdir(root: Path, script_text: str, *, fix="NEEDS_FIX") -> Path:
    wd = root / "work"
    wd.mkdir(parents=True, exist_ok=True)
    (wd / "script.py").write_text(script_text, encoding="utf-8")
    (wd / "fix.txt").write_text(fix, encoding="utf-8")
    return wd


def _runner(root: Path, gw: FakeGateway) -> AgentRunner:
    return AgentRunner(gw, FakeVector(), evidence_root=root / "ev")


def _real_cmd_spec(thread, wd, *, notes=None, billing_task=4,
                   batch="T4-FIXTURE", run_idx=None):
    import sys
    return RunSpec(thread_id=thread, workdir=wd,
                   cmd=[sys.executable, str(wd / "script.py")],
                   telemetry_relpath="telemetry.json",
                   batch_id=batch, billing_task=billing_task,
                   gate2_notes=notes, run_idx=run_idx)


# ---------------------------------------------------------------------------
# ⓪ traceback 取证边界：只读 workdir 内帧（stdlib/site-packages/仓库外跳过）
# ---------------------------------------------------------------------------

def test_00_snippets_only_read_inside_workdir(tmp_path):
    wd = tmp_path / "work"
    wd.mkdir()
    inside = wd / "sim_main.py"
    inside.write_text("\n".join(f"line {i}" for i in range(1, 30)),
                      encoding="utf-8")
    outside = tmp_path / "other_repo" / "evil.py"
    outside.parent.mkdir(parents=True)
    outside.write_text("SECRET_CONTENT_MUST_NOT_BE_READ\n", encoding="utf-8")
    blob = (
        'Traceback (most recent call last):\n'
        f'  File "{outside}", line 1, in <module>\n'
        '    SECRET_CONTENT_MUST_NOT_BE_READ\n'
        '  File "/usr/lib/python3.10/traceback.py", line 100, in format\n'
        '    stdlib frame\n'
        f'  File "{inside}", line 12, in run\n'
        '    line 12\n'
        'RuntimeError: fixture failure\n')
    snippets = _collect_snippets(wd, blob)
    assert len(snippets) == 1
    assert snippets[0]["file"] == "sim_main.py" and snippets[0]["line"] == 12
    joined = json.dumps(snippets, ensure_ascii=False)
    assert "SECRET_CONTENT_MUST_NOT_BE_READ" not in joined
    assert "traceback.py" not in joined


# ---------------------------------------------------------------------------
# ① 两轮收敛
# ---------------------------------------------------------------------------

def test_01_converges_after_one_patch(tmp_path):
    wd = _write_workdir(tmp_path, CONVERGING_SCRIPT)
    gw = FakeGateway([json.dumps(
        {"patches": [{"path": "fix.txt", "old": "NEEDS_FIX",
                      "new": "PATCHED"}]})])
    runner = _runner(tmp_path, gw)
    rec = runner.run(_real_cmd_spec("t4-1", wd))

    assert rec["status"] == "ok"
    assert rec["coding_round"] == 1 and rec["executions"] == 2
    assert Path(rec["telemetry_uri"]).is_file()
    assert len(gw.calls) == 1 and gw.calls[0]["stage"] == "coding_debug"
    assert (wd / "runs.log").read_text() == "xx"       # 确实执行了两次
    prompt = gw.calls[0]["messages"][1]["content"]
    assert "fixture failure" in prompt                  # traceback 尾部
    assert "最高优先级" not in prompt                   # 本轮无 Gate2 意见
    assert "[S1 来源:" in prompt                        # KB 列注
    assert "evidence_refs: E0" in prompt                # text + evidence_refs 双字段
    assert "字段名必须是 text" in prompt
    assert all(isinstance(m["content"], str) for m in gw.calls[0]["messages"])


def test_01b_gate2_notes_marked_highest_priority(tmp_path):
    wd = _write_workdir(tmp_path, CONVERGING_SCRIPT)
    gw = FakeGateway([json.dumps(
        {"patches": [{"path": "fix.txt", "old": "NEEDS_FIX",
                      "new": "PATCHED"}]})])
    rec = _runner(tmp_path, gw).run(
        _real_cmd_spec("t4-1b", wd, notes="接触不稳，提高 timestep 分辨率"))
    assert rec["status"] == "ok"
    prompt = gw.calls[0]["messages"][1]["content"]
    assert "人工评审意见（必须逐条响应，最高优先级）" in prompt
    assert "提高 timestep 分辨率" in prompt


# ---------------------------------------------------------------------------
# ② 越界补丁拒绝、不重试
# ---------------------------------------------------------------------------

def test_02_out_of_bounds_patch_rejected_no_retry(tmp_path):
    wd = _write_workdir(tmp_path, CONVERGING_SCRIPT)
    outside = tmp_path / "stage_models.yaml"
    outside.write_text("anchors: keep\n", encoding="utf-8")
    gw = FakeGateway([json.dumps(
        {"patches": [{"path": str(outside), "old": "anchors: keep",
                      "new": "anchors: HACKED"}]})])
    runner = _runner(tmp_path, gw)
    rec = runner.run(_real_cmd_spec("t4-2", wd))

    assert rec["status"] == "failed"
    assert "补丁安全拒绝" in rec["reason"]
    assert len(gw.calls) == 1                  # 拒绝后不发起第二次调用
    assert outside.read_text() == "anchors: keep\n"
    failure = json.loads((wd / "FAILURE.json").read_text())
    assert failure["status"] == "failed"

    # 同名禁改文件即便位于 workdir 内也拒
    gw2 = FakeGateway([json.dumps(
        {"patches": [{"path": "stage_models.yaml", "old": "a", "new": "b"}]})])
    wd2 = _write_workdir(tmp_path / "again", CONVERGING_SCRIPT)
    (wd2 / "stage_models.yaml").write_text("a")
    rec2 = _runner(tmp_path, gw2).run(_real_cmd_spec("t4-2b", wd2))
    assert rec2["status"] == "failed" and len(gw2.calls) == 1
    assert (wd2 / "stage_models.yaml").read_text() == "a"


# ---------------------------------------------------------------------------
# ③ 5 轮全败 + 硬停 + 图进 human_failure
# ---------------------------------------------------------------------------

def _rotating_patches(n):
    return [json.dumps({"patches": [
        {"path": "fix.txt", "old": f"x={i}", "new": f"x={i+1}"}]})
        for i in range(n)]


def test_03_five_rounds_then_hard_stop(tmp_path):
    wd = _write_workdir(tmp_path, ALWAYS_FAIL_SCRIPT, fix="x=0")
    gw = FakeGateway(_rotating_patches(5))
    runner = _runner(tmp_path, gw)
    rec = runner.run(_real_cmd_spec("t4-3", wd))

    assert rec["status"] == "failed"
    assert len(gw.calls) == MAX_CODING_ROUNDS == 5
    assert rec["coding_round"] == 5 and rec["executions"] == 6
    assert (wd / "fix.txt").read_text() == "x=5"
    failure = json.loads((wd / "FAILURE.json").read_text())
    assert failure["coding_round"] == 5
    # 五次 req/resp/decision 证据齐全
    ev = tmp_path / "ev" / "t4-3"
    for n in range(1, 6):
        assert (ev / f"coding_round{n}.req.json").is_file()
        assert (ev / f"coding_round{n}.resp.json").is_file()
        assert (ev / f"coding_round{n}.decision.json").is_file()

    # 失败 run 再次进入：计数处硬停（GatewayPausedError 等价硬停），零网络零执行
    runs_before = (wd / "runs.log").read_text()
    gw2 = FakeGateway(_rotating_patches(5))
    with pytest.raises(CodingLimitError) as ei:
        _runner(tmp_path, gw2).run(_real_cmd_spec("t4-3", wd))
    assert isinstance(ei.value, GatewayPausedError)
    assert gw2.calls == []
    assert (wd / "runs.log").read_text() == runs_before


def test_03b_failed_run_routes_graph_to_human_failure(tmp_path):
    wd = _write_workdir(tmp_path / "g", ALWAYS_FAIL_SCRIPT, fix="x=0")
    gw = FakeGateway(_rotating_patches(5))
    runner = _runner(tmp_path, gw)
    thread = "t4-3g"

    def spec_factory(state):
        import sys
        notes = ""
        for ev in reversed(state.get("events", [])):
            if ev.get("type") == "gate2_rejected":
                notes = ev["human_notes"]
                break
        return RunSpec(thread_id=thread, workdir=wd,
                       cmd=[sys.executable, str(wd / "script.py")],
                       telemetry_relpath="telemetry.json",
                       batch_id="T4-FIXTURE", gate2_notes=notes or None)

    deps = GraphDeps(
        direction=FakeDirection(),
        gate1=ScriptedGate([{"decision": "approve", "choice": 0, "human_notes": ""}]),
        agent_run=agent_run_seam(runner, spec_factory),
        evaluate=file_backed_evaluate,
        gate2=ScriptedGate([{"decision": "approve", "choice": None, "human_notes": ""}]),
        persist=FakePersist(),
        run_dir=tmp_path / "g" / "runs",
    )
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    graph = build_graph(deps).compile(checkpointer=SqliteSaver(conn))
    final = graph.invoke(
        initial_state(thread_id=thread, track="track1", question="q"),
        config={"configurable": {"thread_id": thread}, "recursion_limit": 25})
    kinds = [ev["type"] for ev in final["events"]]
    assert "agent_run_failed" in kinds
    assert kinds[-1] == "human_failure"
    assert "evaluate" not in kinds                  # 失败不进 evaluate
    graph_failure_p = tmp_path / "g" / "runs" / "FAILURE.json"
    assert graph_failure_p.is_file()
    assert len(gw.calls) == 5

    # L-4：图级 / workdir 级 FAILURE.json 双向交叉引用，workdir 既有字段保留
    gf = json.loads(graph_failure_p.read_text(encoding="utf-8"))
    assert gf["status"] == "human_failure" and gf["stage"] == "agent_run"
    assert gf["agent_failure_json"] == str(wd / "FAILURE.json")
    wf = json.loads((wd / "FAILURE.json").read_text(encoding="utf-8"))
    assert wf["graph_failure_json"] == str(graph_failure_p)
    assert wf["coding_round"] == 5 and wf["status"] == "failed"

    # L-4：结案被整节点重放时图级 ts_iso 不滚动（预置旧时间戳必须保留）
    old_ts = "2020-01-01T00:00:00+0000"
    gf["ts_iso"] = old_ts
    (graph_failure_p).write_text(
        json.dumps(gf, ensure_ascii=False), encoding="utf-8")
    write_graph_failure(final, deps, stage="agent_run",
                        reason=wf.get("reason", "x"))
    gf2 = json.loads(graph_failure_p.read_text(encoding="utf-8"))
    assert gf2["ts_iso"] == old_ts
    assert gf2["agent_failure_json"] == str(wd / "FAILURE.json")


def test_03c_runner_fail_preserves_first_ts_on_node_replay(tmp_path):
    """L-4：runner _fail 重入时保留首写 ts_iso（结案时间不被重放刷新）。"""
    from orchestrator.agent_runner.runner import _ExecResult

    wd = _write_workdir(tmp_path / "h", ALWAYS_FAIL_SCRIPT, fix="x=0")
    runner = _runner(tmp_path, FakeGateway([]))
    spec = _real_cmd_spec("t4-ts", wd)
    (wd / "FAILURE.json").write_text(json.dumps({
        "status": "failed", "ts_iso": "2020-01-01T00:00:00+0000",
        "reason": "stale"}), encoding="utf-8")
    root = runner._root(spec)
    rec = runner._fail(root, spec, [{"exec": 1}],
                       _ExecResult(1, "", "boom"), reason="再次结案")
    assert rec["status"] == "failed"
    payload = json.loads((wd / "FAILURE.json").read_text(encoding="utf-8"))
    assert payload["ts_iso"] == "2020-01-01T00:00:00+0000"
    assert payload["reason"] == "再次结案"


# ---------------------------------------------------------------------------
# ④ coding_round 与 gate2_rejects 计数独立
# ---------------------------------------------------------------------------

def test_04_counters_independent_across_gate2_reject(tmp_path):
    thread = "t4-4"
    # 两个 run 目录各自「失败一次后收敛」
    wds = []
    for k in (1, 2):
        wd = _write_workdir(tmp_path / f"run{k}", CONVERGING_SCRIPT)
        wds.append(wd)
    gw = FakeGateway([
        json.dumps({"patches": [{"path": "fix.txt", "old": "NEEDS_FIX",
                                 "new": "PATCHED"}]}),
        json.dumps({"patches": [{"path": "fix.txt", "old": "NEEDS_FIX",
                                 "new": "PATCHED"}]}),
    ])
    runner = _runner(tmp_path, gw)

    def spec_factory(state):
        import sys
        notes = None
        for ev in reversed(state.get("events", [])):
            if ev.get("type") == "gate2_rejected":
                notes = ev["human_notes"]
                break
        k = len(state.get("agent_runs", [])) + 1
        wd = wds[k - 1]
        return RunSpec(thread_id=thread, workdir=wd,
                       cmd=[sys.executable, str(wd / "script.py")],
                       telemetry_relpath="telemetry.json",
                       batch_id="T4-FIXTURE", gate2_notes=notes)

    deps = GraphDeps(
        direction=FakeDirection(),
        gate1=ScriptedGate([{"decision": "approve", "choice": 0, "human_notes": ""}]),
        agent_run=agent_run_seam(runner, spec_factory),
        evaluate=file_backed_evaluate,
        gate2=ScriptedGate([
            {"decision": "reject_with_comments", "choice": None,
             "human_notes": "Gate2 意见：参数重选"},
            {"decision": "approve", "choice": None, "human_notes": ""},
        ]),
        persist=FakePersist(),
        run_dir=tmp_path / "runs",
    )
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    graph = build_graph(deps).compile(checkpointer=SqliteSaver(conn))
    final = graph.invoke(
        initial_state(thread_id=thread, track="track1", question="q"),
        config={"configurable": {"thread_id": thread}, "recursion_limit": 25})

    kinds = [ev["type"] for ev in final["events"]]
    assert kinds.count("agent_run") == 2
    assert final["gate2_rejects"] == 1
    assert final["coding_round"] == 1             # 第二个 run 仍从 1 计
    assert all(r.get("coding_round") == 1 for r in final["agent_runs"])
    assert kinds[-1] == "done"
    # 第二次 run 的补救 prompt 里带 Gate2 意见
    second_prompt = gw.calls[1]["messages"][1]["content"]
    assert "Gate2 意见：参数重选" in second_prompt
    assert gw.calls[0]["messages"][1]["content"] != second_prompt


# ---------------------------------------------------------------------------
# L-1 调用级幂等：kill-9 落在实呼返回后、节点返回前 → 整节点重放 0 二次付费
# ---------------------------------------------------------------------------

def test_05_call_replay_node_replay_zero_rebill(tmp_path, monkeypatch):
    import contextlib

    thread = "t4-replay"
    wd = _write_workdir(tmp_path, CONVERGING_SCRIPT)
    patch_json = json.dumps(
        {"patches": [{"path": "fix.txt", "old": "NEEDS_FIX",
                      "new": "PATCHED"}]})
    # 备 2 条响应：重放命中不消耗；末段指纹失配按 miss 正常重呼才消耗第 2 条。
    gw = FakeGateway([patch_json, patch_json], usage=(123, 456))
    # L-1/C2：命中不得进 billing_context（ledger preflight/record 只可能在
    # 该上下文内发生）；spy 计数上下文进入次数。
    import orchestrator.agent_runner.runner as runner_mod
    billing_entries: list[dict] = []
    real_billing = runner_mod.billing_context

    @contextlib.contextmanager
    def _spy_billing(**kw):
        billing_entries.append(kw)
        with real_billing(**kw) as ctx:
            yield ctx

    monkeypatch.setattr(runner_mod, "billing_context", _spy_billing)
    cache_dir = tmp_path / "cache"
    cache = CallReplay(cache_dir)
    ev_root = tmp_path / "ev"
    runner = AgentRunner(gw, FakeVector(), evidence_root=ev_root,
                         call_cache=cache)
    spec = _real_cmd_spec(thread, wd, run_idx=1)

    rec = runner.run(spec)
    assert rec["status"] == "ok" and rec["coding_round"] == 1
    assert len(gw.calls) == 1
    assert len(billing_entries) == 1        # miss 实呼恰进一次计费上下文

    cache_file = cache_dir / "coding_t4-replay_r1_round1.json"
    assert cache_file.is_file()
    cached = json.loads(cache_file.read_text(encoding="utf-8"))
    assert cached["schema"] == REPLAY_SCHEMA
    assert cached["kind"] == "coding_debug" and cached["usage"] == [123, 456]
    assert cached["raw"] == patch_json
    assert stat_mode(cache_dir) == 0o700 and stat_mode(cache_file) == 0o600

    ev_dir = ev_root / thread
    req_p = ev_dir / "coding_round1.req.json"
    resp_p = ev_dir / "coding_round1.resp.json"
    frozen = {p: (p.read_bytes(), p.stat().st_mtime_ns)
              for p in (req_p, resp_p)}

    # 模拟 kill-9 落在「实呼已返回、补丁/产物落盘与节点返回之前」：
    # 清掉补丁效果与成功产物，保留缓存与证据（新进程从检查点重放整个节点）。
    (wd / "telemetry.json").unlink()
    (wd / "fix.txt").write_text("NEEDS_FIX", encoding="utf-8")

    runner2 = AgentRunner(gw, FakeVector(), evidence_root=ev_root,
                          call_cache=cache)
    rec2 = runner2.run(_real_cmd_spec(thread, wd, run_idx=1))

    # 重放：0 次新增发网；补丁从缓存 raw 重放应用并再次收敛
    assert rec2["status"] == "ok" and rec2["coding_round"] == 1
    assert rec2["executions"] == 2
    assert len(gw.calls) == 1
    assert len(billing_entries) == 1       # 命中：不进 billing_context
    assert (wd / "telemetry.json").is_file()
    assert (wd / "fix.txt").read_text() == "PATCHED"
    # req/resp 证据字节与 mtime 均不得被重写（decision 允许重写，不在断言内）
    for p, (blob, mt) in frozen.items():
        assert p.read_bytes() == blob, f"{p.name} 内容被重放重写"
        assert p.stat().st_mtime_ns == mt, f"{p.name} mtime 被重放刷新"
    assert not (ev_dir / "coding_round2.req.json").is_file()

    # 指纹失配（等价于重放产生了不同请求）：必须按 miss 正常计费重呼，
    # 禁止把旧补丁/旧判定复用到不同请求——第 2 条响应此时才被消耗。
    rec_json = json.loads(cache_file.read_text(encoding="utf-8"))
    rec_json["req_sha256"] = "0" * 64
    cache_file.write_text(json.dumps(rec_json), encoding="utf-8")
    (wd / "telemetry.json").unlink()
    (wd / "fix.txt").write_text("NEEDS_FIX", encoding="utf-8")
    runner3 = AgentRunner(gw, FakeVector(), evidence_root=ev_root,
                          call_cache=cache)
    rec3 = runner3.run(_real_cmd_spec(thread, wd, run_idx=1))
    assert rec3["status"] == "ok"
    assert len(gw.calls) == 2 and len(billing_entries) == 2
    # miss 重呼重写 req/resp 证据（mtime 前进），随后以新指纹重新落缓存
    for p, (blob, mt) in frozen.items():
        assert p.stat().st_mtime_ns > mt, f"{p.name} miss 重呼未重写证据"
    rec_after = json.loads(cache_file.read_text(encoding="utf-8"))
    assert rec_after["req_sha256"] != "0" * 64


def stat_mode(p: Path) -> int:
    return p.stat().st_mode & 0o777
