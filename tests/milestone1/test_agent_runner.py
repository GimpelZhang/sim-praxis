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

import ast
import hashlib
import json
import random
import re
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
from orchestrator.agent_runner.runner import (
    CHUNK_CLOSED_SPLIT_MAX_CHARS,
    CHUNK_MAX_CODE_CHARS,
    CHUNK_MIN_ASSEMBLED_CHARS,
    CHUNK_MIN_CODE_CHARS,
    CODING_THINKING,
    DEFAULT_CODING_TIMEOUT_S,
    MAX_TOKENS,
    TEMPERATURE,
    ChunkBirthError,
    ChunkPromptContext,
    ChunkSpec,
    CodingRecallSpec,
    FULL_SOURCE_PER_FILE_CHARS,
    FULL_SOURCE_TOTAL_CHARS,
    PRIOR_REF_PER_FILE_CHARS,
    PRIOR_REF_TOTAL_CHARS,
    _clean_coding_hint,
    _collect_full_source,
    _collect_prior_refs,
    _ENTRY_GUARD_REASON,
    _FRAME_RE,
    _RC0_MISSING_ARTIFACT_FMT,
    _append_rc0_missing_artifact_note,
    _build_messages,
    _classify_closed_body,
    _collect_snippets,
    _extract_chunk,
    _validate_assembled_chunks,
    _validate_entry_guard,
)
from orchestrator.common.callcache import (
    SCHEMA as REPLAY_SCHEMA,
    CallReplay,
    request_fingerprint,
)
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


def test_03c_failure_evidence_mirror_single_suffix(tmp_path):
    # 回归：失败镜像件名须为单后缀 failure.json，不再双后缀 failure.json.json
    # （历史卷 failure.json.json 为冻结证据不改名；本断言只约束今后新写件）。
    wd = _write_workdir(tmp_path / "m", ALWAYS_FAIL_SCRIPT, fix="x=0")
    gw = FakeGateway(_rotating_patches(5))
    runner = _runner(tmp_path, gw)
    thread = "t4-3c"
    rec = runner.run(_real_cmd_spec(thread, wd))
    assert rec["status"] == "failed"

    ev = tmp_path / "ev" / thread
    mirror = ev / "failure.json"
    assert mirror.is_file()
    assert not (ev / "failure.json.json").exists()
    doubled = sorted(p.name for p in ev.iterdir()
                     if p.name.endswith(".json.json"))
    assert doubled == []

    # 镜像与 workdir FAILURE.json 同一 payload 对象两次序列化：字节一致
    work_failure = wd / "FAILURE.json"
    assert mirror.read_text(encoding="utf-8") == \
        work_failure.read_text(encoding="utf-8")
    mirror_doc = json.loads(mirror.read_text(encoding="utf-8"))
    work_doc = json.loads(work_failure.read_text(encoding="utf-8"))
    for key in ("status", "reason", "coding_round", "executions"):
        assert mirror_doc[key] == work_doc[key]

    # 常规逐轮件名中间段不受影响（防过度改名）
    assert (ev / "coding_round1.decision.json").is_file()


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


# ===========================================================================
# ⑤ Task6 V2 拆块出生（chunked birth，锁内适配 seq42-47 / 用户 2026-10-20
#    裁决「拆块交付」；run4 2026-10-21 起 sentinel 围栏纯文本定界 + n=6，
#    设计终稿 .omc/handoffs/task6-sentinel-seam-design.md）：默认输出字符级
#    快照回归、拆块段渲染、ChunkSpec 校验、逐块/拼接机器校验、6 块顺序出生
#    端到端、坏块整轮失败无半成品、反馈机制零改动。
#    全部 0 付费：FakeGateway + 全合成 chunk 文本（非解题脚本，硬规则 3）。
#    [chunked-birth-tests-v2]
# ===========================================================================

import sys  # noqa: E402 （拆块测试段局部导入，与既有测试函数内 import 同风格）

_CHUNK_TARGET = "v2.py"
_CHUNK_TITLES = ("头部/合成常量", "场景/合成注入", "步态/合成控制",
                 "采集/合成判定", "batch/合成数据产物", "媒体/合成产物")
_CHUNK_BOUNDARIES = tuple(
    "行数预算：约 180 行。"
    f"第 {i} 块边界（夹具）：只允许写本块合成章节，不得写其他块章节。"
    for i in range(6))

# 字符级快照：改前 _build_messages 两 case（无意见/有人工意见）的逐字输出，
# 由一次性 /tmp/task6_capture_baseline.py 在改前模板上捕获（3857 B）。任何对
# 默认模板的无意改动都会在此逐字比对下失败；如确需改模板，必须重捕并评审。
# v1.4 协议变更登记件（设计 §2.5 必改清单①，.omc/handoffs/
# task6-ide-repair-protocol-design.md §3 行为代差登记节）：修复轮 system 首行
# 更新为双通道文案（JSON 语义保留 + replace 围栏格式 + 「输出前先确认选了
# 哪种通道」句）；user 内容两行逐字不变。出生轮 system 逐字=旧原文由
# test_v14_birth_system_verbatim_old_text 钉死（必改清单②）。
_BASELINE_SNAPSHOT = r'''[{"role": "system", "content": "你是具身智能仿真工具链的排障 coding agent。输出前先确认选了哪种通道。通道一（JSON，只允许输出严格 JSON）：{\"patches\":[{\"path\":\"相对运行工作目录的路径\",\"old\":\"原文（须唯一）\",\"new\":\"替换文\"}]}，或 {\"unified_diff\":\"git 风格 diff\"}。通道二（simpraxis.replace.v1 围栏整替函数，优先）示例：\n<<<<<<< simpraxis.replace.v1 path=脚本文件.py func=函数名 [class=类名]\n<该函数含装饰器行的完整新源码>\n=======\n>>>>>>> simpraxis.replace.v1.end\n首行 path/func 逐字给相对路径与函数名，class= 可省略（函数名在文件中多处定义时用它消歧）；中间是完整新源码；=======是可选分隔行；末行结束围栏；三行各独占一行。补丁只能改本次运行工作目录内的文件；禁止改配置、凭据、仓库源码。不要输出解释、不要输出 image。"}, {"role": "user", "content": "## 失败命令\n/mnt/venvs/simpraxis/bin/python /work/run_1/script.py --flag 值\n\n## traceback / 日志尾部\nTraceback (most recent call last):\n  File \"script.py\", line 12\nRuntimeError: fixture failure: boom\n\n## 相关源文件片段（仅限运行工作目录）\n### sim_main.py:12\n    7| x = 1\n    8| y = 2\n    9| z = x + y\n   10| raise RuntimeError('boom')\n   11| \n   12| -> raise RuntimeError('boom')\n   13| \n   14| def f():\n   15|     pass\n   16| \n   17| return\n\n## troubleshooting 知识库命中（namespaces=troubleshooting, top_k=6）\n[S1 来源: knowledge/projects/fixture/troubleshooting.md#s0 | evidence_refs: E0]\n夹具排障知识条目 0（字段名必须是 text）\n\n[S2 来源: knowledge/projects/fixture/troubleshooting.md#s1 | evidence_refs: E1]\n夹具排障知识条目 1（字段名必须是 text）\n\n[S3 来源: knowledge/projects/fixture/troubleshooting.md#s2 | evidence_refs: E2]\n夹具排障知识条目 2（字段名必须是 text）\n\n[S4 来源: knowledge/projects/fixture/troubleshooting.md#s3 | evidence_refs: E3]\n夹具排障知识条目 3（字段名必须是 text）\n\n[S5 来源: knowledge/projects/fixture/troubleshooting.md#s4 | evidence_refs: E4]\n夹具排障知识条目 4（字段名必须是 text）\n\n[S6 来源: knowledge/projects/fixture/troubleshooting.md#s5 | evidence_refs: E5]\n夹具排障知识条目 5（字段名必须是 text）"}]
[{"role": "system", "content": "你是具身智能仿真工具链的排障 coding agent。输出前先确认选了哪种通道。通道一（JSON，只允许输出严格 JSON）：{\"patches\":[{\"path\":\"相对运行工作目录的路径\",\"old\":\"原文（须唯一）\",\"new\":\"替换文\"}]}，或 {\"unified_diff\":\"git 风格 diff\"}。通道二（simpraxis.replace.v1 围栏整替函数，优先）示例：\n<<<<<<< simpraxis.replace.v1 path=脚本文件.py func=函数名 [class=类名]\n<该函数含装饰器行的完整新源码>\n=======\n>>>>>>> simpraxis.replace.v1.end\n首行 path/func 逐字给相对路径与函数名，class= 可省略（函数名在文件中多处定义时用它消歧）；中间是完整新源码；=======是可选分隔行；末行结束围栏；三行各独占一行。补丁只能改本次运行工作目录内的文件；禁止改配置、凭据、仓库源码。不要输出解释、不要输出 image。"}, {"role": "user", "content": "## 人工评审意见（必须逐条响应，最高优先级）\n人工意见：提高分辨率\n## 失败命令\n/mnt/venvs/simpraxis/bin/python /work/run_1/script.py --flag 值\n\n## traceback / 日志尾部\nTraceback (most recent call last):\n  File \"script.py\", line 12\nRuntimeError: fixture failure: boom\n\n## 相关源文件片段（仅限运行工作目录）\n### sim_main.py:12\n    7| x = 1\n    8| y = 2\n    9| z = x + y\n   10| raise RuntimeError('boom')\n   11| \n   12| -> raise RuntimeError('boom')\n   13| \n   14| def f():\n   15|     pass\n   16| \n   17| return\n\n## troubleshooting 知识库命中（namespaces=troubleshooting, top_k=6）\n[S1 来源: knowledge/projects/fixture/troubleshooting.md#s0 | evidence_refs: E0]\n夹具排障知识条目 0（字段名必须是 text）\n\n[S2 来源: knowledge/projects/fixture/troubleshooting.md#s1 | evidence_refs: E1]\n夹具排障知识条目 1（字段名必须是 text）\n\n[S3 来源: knowledge/projects/fixture/troubleshooting.md#s2 | evidence_refs: E2]\n夹具排障知识条目 2（字段名必须是 text）\n\n[S4 来源: knowledge/projects/fixture/troubleshooting.md#s3 | evidence_refs: E3]\n夹具排障知识条目 3（字段名必须是 text）\n\n[S5 来源: knowledge/projects/fixture/troubleshooting.md#s4 | evidence_refs: E4]\n夹具排障知识条目 4（字段名必须是 text）\n\n[S6 来源: knowledge/projects/fixture/troubleshooting.md#s5 | evidence_refs: E5]\n夹具排障知识条目 5（字段名必须是 text）"}]'''
_SNAPSHOT_MSGS = [json.loads(line) for line in _BASELINE_SNAPSHOT.splitlines()]
_SNAP_SNIPPETS = [{'file': 'sim_main.py', 'line': 12, 'snippet': "    7| x = 1\n    8| y = 2\n    9| z = x + y\n   10| raise RuntimeError('boom')\n   11| \n   12| -> raise RuntimeError('boom')\n   13| \n   14| def f():\n   15|     pass\n   16| \n   17| return"}]
_SNAP_HITS = [{'text': '夹具排障知识条目 0（字段名必须是 text）', 'evidence_refs': ['E0'], 'kb_relpath': 'knowledge/projects/fixture/troubleshooting.md', 'section_path': 's0'}, {'text': '夹具排障知识条目 1（字段名必须是 text）', 'evidence_refs': ['E1'], 'kb_relpath': 'knowledge/projects/fixture/troubleshooting.md', 'section_path': 's1'}, {'text': '夹具排障知识条目 2（字段名必须是 text）', 'evidence_refs': ['E2'], 'kb_relpath': 'knowledge/projects/fixture/troubleshooting.md', 'section_path': 's2'}, {'text': '夹具排障知识条目 3（字段名必须是 text）', 'evidence_refs': ['E3'], 'kb_relpath': 'knowledge/projects/fixture/troubleshooting.md', 'section_path': 's3'}, {'text': '夹具排障知识条目 4（字段名必须是 text）', 'evidence_refs': ['E4'], 'kb_relpath': 'knowledge/projects/fixture/troubleshooting.md', 'section_path': 's4'}, {'text': '夹具排障知识条目 5（字段名必须是 text）', 'evidence_refs': ['E5'], 'kb_relpath': 'knowledge/projects/fixture/troubleshooting.md', 'section_path': 's5'}]
_SNAP_CMD = ['/mnt/venvs/simpraxis/bin/python', '/work/run_1/script.py', '--flag', '值']
_SNAP_TB = 'Traceback (most recent call last):\n  File "script.py", line 12\nRuntimeError: fixture failure: boom'


def _snap_kwargs(**over):
    kw = dict(traceback_tail=_SNAP_TB, snippets=_SNAP_SNIPPETS,
              hits=_SNAP_HITS, cmd=_SNAP_CMD)
    kw.update(over)
    return kw


def test_06_build_messages_default_byte_snapshot():
    # 锁内适配（计划 v1.2 外接缝）的回归锚点：extra/chunk 两新参默认 None 时，
    # 修补轮（chunk_context=None）看到的消息必须与登记快照逐字一致（整条
    # JSON 快照）。v1.4 代差（设计 §3 行为代差登记节）：system 首行是修复轮
    # 双通道文案（必改清单①，协议变更登记件）；user 内容与改前逐字一致。
    msgs_none = _build_messages(gate2_notes=None, **_snap_kwargs())
    msgs_note = _build_messages(
        gate2_notes="人工意见：提高分辨率", **_snap_kwargs())
    assert msgs_none == _SNAPSHOT_MSGS[0]
    assert msgs_note == _SNAPSHOT_MSGS[1]
    assert json.dumps(msgs_none, ensure_ascii=False) \
        == _BASELINE_SNAPSHOT.splitlines()[0]
    user = msgs_none[1]["content"]
    assert "拆块交付" not in user and "升阶任务合同" not in user


def test_07_extra_sections_render_position_order():
    user = _build_messages(
        gate2_notes=None,
        extra_sections=(("标题甲", "正文甲"), ("标题乙", "正文乙")),
        **_snap_kwargs())[1]["content"]
    # 无意见时 extra 段是 user 开头；顺序=传入顺序；紧邻失败命令段。
    assert user.startswith(
        "## 标题甲\n正文甲\n\n## 标题乙\n正文乙\n\n## 失败命令\n")
    user2 = _build_messages(
        gate2_notes="人工意见：看这里",
        extra_sections=(("标题甲", "正文甲"),),
        **_snap_kwargs())[1]["content"]
    p_notes = user2.index("## 人工评审意见")
    p_extra = user2.index("## 标题甲")
    p_fail = user2.index("## 失败命令")
    assert p_notes < p_extra < p_fail


def test_08_chunk_section_render_fence_format():
    ctx0 = ChunkPromptContext(
        0, 6, _CHUNK_TARGET, _CHUNK_TITLES, _CHUNK_BOUNDARIES,
        round_feedback="块 2 path 不符：x")
    u0 = _build_messages(
        gate2_notes=None, chunk_context=ctx0, **_snap_kwargs())[1]["content"]
    assert "## 拆块交付（第 1/6 块）" in u0
    assert "1. 《头部/合成常量》" in u0 and "6. 《媒体/合成产物》" in u0
    assert "你本次只能产出第 1/6 块《头部/合成常量》" in u0
    assert "### 前序已定稿块" not in u0          # 块 0 无前序
    assert "上一轮拆块交付失败反馈" in u0 and "块 2 path 不符" in u0
    # run4：围栏字面串与字段逐字口径
    assert "### 交付格式（围栏纯文本，禁止 JSON）" in u0
    assert (f"<<<<<<< simpraxis.chunk.v1 path={_CHUNK_TARGET} "
            "index=0 total=6") in u0
    assert ">>>>>>> simpraxis.chunk.v1.end" in u0
    assert "index 逐字等于 0" in u0
    assert "total 逐字等于 6" in u0
    assert "「缺结束围栏」" in u0
    # 旧 JSON 协议文案反向断言
    assert '"index":0' not in u0
    assert "index 必须是 0" not in u0
    assert "严格 JSON" not in u0
    assert str(CHUNK_MIN_CODE_CHARS) in u0
    assert str(CHUNK_MAX_CODE_CHARS) in u0
    assert str(CHUNK_MIN_ASSEMBLED_CHARS) in u0
    # run5（规格 §15.3-H1）：模型面只改这一行 bullet——逐字钉死渲染串；
    # 18001–36000 多行件机器二拆是内部机制，不得在模型面披露。
    run5_bullet = (
        f"- code 只含本块 Python 原文，请控制在 {CHUNK_MAX_CODE_CHARS} 字符以内"
        "（本机逐字符计数）；少于 "
        f"{CHUNK_MIN_CODE_CHARS} 字符、超过 {CHUNK_CLOSED_SPLIT_MAX_CHARS} 字符"
        "或缺结束围栏，仍机器校验失败；")
    assert run5_bullet in u0
    assert "超限即机器校验失败" not in u0       # run4 旧 bullet 反向钉
    assert str(CHUNK_CLOSED_SPLIT_MAX_CHARS) in u0

    prior = "PRIOR0\nPRIOR1\n"
    ctx5 = ChunkPromptContext(
        5, 6, _CHUNK_TARGET, _CHUNK_TITLES, _CHUNK_BOUNDARIES,
        previous_code=prior)
    u5 = _build_messages(
        gate2_notes=None, extra_sections=(("S", "B"),),
        chunk_context=ctx5, **_snap_kwargs())[1]["content"]
    assert "## 拆块交付（第 6/6 块）" in u5
    p_sec, p_chunk, p_fail = (
        u5.index("## S\nB"), u5.index("## 拆块交付"),
        u5.index("## 失败命令"))
    assert p_sec < p_chunk < p_fail            # extra 段在 chunk 段之前
    assert "```python\n" + prior + "\n```" in u5
    assert "以下是前 5 块拼接后的逐字定稿" in u5
    assert "上一轮拆块交付失败反馈" not in u5   # 反馈只进每轮块 0

    # run5（规格 §15.3-H1/§15.5-LOW1）：分层词族扫描。中文机制词族
    # （分片/拆分/二拆/机器分片/切分）对六份**渲染后** prompt 全扫——
    # 「拆块」是 run4 既有标题用词，不在禁族；代码注释里的「二拆/切分」
    # 不渲染，天然不可见。latin split/frag 只对三条 run5 增量字面量断言
    # （H1 bullet + 两条真值外串），**禁止**对整份 prompt 全扫：冻结图
    # prompt 合法含 split_normal_vs_ice…、splits/cover 等 frozen words，
    # 全扫是规格点名的错误实现（§15.5-LOW2）。
    cn_family = ("分片", "拆分", "二拆", "机器分片", "切分")
    rendered = []
    for i in range(6):
        ctx_i = ChunkPromptContext(
            i, 6, _CHUNK_TARGET, _CHUNK_TITLES, _CHUNK_BOUNDARIES,
            round_feedback="块 2 path 不符：x" if i == 0 else "")
        rendered.append(_build_messages(
            gate2_notes=None, chunk_context=ctx_i,
            **_snap_kwargs())[1]["content"])
    for i, ui in enumerate(rendered):
        for w in cn_family:
            assert w not in ui, (i, w)
    cempty_external = (
        "块 2 code 长度 18802 > 18000 chars，请将该块整体缩短至"
        " 18000 字符以内后整轮重发（本机逐字符计数）")
    oversize_external = "块 0 code 长度 40000 > 36000 chars（机器窗上限）"
    for w in cn_family:
        assert w not in cempty_external
        assert w not in oversize_external
    # frozen-word 例外登记（不在三条增量字面量内，仅注释存证，禁止扩成白名单）：
    # orchestrator/graph/task6.py 的 split_normal_vs_ice…、
    # task6_v2_prompts.py 的 splits/cover 是 run4 冻结图内容，保持原样。
    for lit in (run5_bullet, cempty_external, oversize_external):
        assert "split" not in lit.lower() and "frag" not in lit.lower()


def test_09_chunk_spec_construction_validation():
    kw = dict(target_path=_CHUNK_TARGET, chunk_count=6,
              chunk_titles=_CHUNK_TITLES,
              per_call_sections=(("a", "b"),),
              chunk_boundaries=_CHUNK_BOUNDARIES)
    assert ChunkSpec(**kw).chunk_count == 6
    with pytest.raises(ValueError, match="target_path"):
        ChunkSpec(**{**kw, "target_path": "  "})
    with pytest.raises(ValueError, match="chunk_count"):
        ChunkSpec(**{**kw, "chunk_count": 0})
    with pytest.raises(ValueError, match="chunk_count"):
        ChunkSpec(**{**kw, "chunk_count": True})   # bool 不是 int
    with pytest.raises(ValueError):
        ChunkSpec(**{**kw, "chunk_count": 3,
                     "chunk_titles": _CHUNK_TITLES[:3],
                     "chunk_boundaries": _CHUNK_BOUNDARIES[:3],
                     "per_call_sections": ("a", "b")})  # 非 (str,str) 元组
    with pytest.raises(ValueError):
        ChunkSpec(**{**kw, "chunk_titles": _CHUNK_TITLES[:5]})


def _chunk_fence(code, index, *, path=_CHUNK_TARGET, total=6,
                 pre="", post="", begin=None, omit_end=False):
    """按 §2.1 拼 sentinel 围栏响应（整体重建，非旧 _chunk_envelope 改名；
    begin/omit_end 仅供畸形矩阵构造不合规围栏行）。"""
    head = begin if begin is not None else (
        f"<<<<<<< simpraxis.chunk.v1 path={path} "
        f"index={index} total={total}")
    tail = "" if omit_end else "\n>>>>>>> simpraxis.chunk.v1.end"
    return f"{pre}{head}\n{code}{tail}{post}"


def test_10_extract_chunk_fence_machine_checks():
    good = "x = 1\n" + _fixture_pad("m10", 13)
    assert CHUNK_MIN_CODE_CHARS <= len(good) <= CHUNK_MAX_CODE_CHARS
    assert _extract_chunk(_chunk_fence(good, 0),
                          target=_CHUNK_TARGET, index=0, total=6) == good

    def expect(raw, i, needle):
        # run5：长度分类已移出 _extract_chunk；15+ 形仍在提取器抛，长度两路径
        # 由调用点之后的模块级纯函数 _classify_closed_body 抛（规格 §15.1-A）。
        # 组合调用与生产 _run_chunked_birth_round 同序：extract→classify。
        with pytest.raises(ChunkBirthError, match=needle):
            body = _extract_chunk(raw, target=_CHUNK_TARGET, index=i, total=6)
            _classify_closed_body(body, i)

    # 长度窗边界（本机逐字符计数）
    assert _extract_chunk(_chunk_fence("#" * 800, 0),
                          target=_CHUNK_TARGET, index=0, total=6) == "#" * 800
    expect(_chunk_fence("#" * 799, 0), 0, "code 长度 799 < 800")
    assert _extract_chunk(_chunk_fence("#" * 18000, 0),
                          target=_CHUNK_TARGET, index=0, total=6) == "#" * 18000
    # run5（§15.3-H2/§15.4-MAJORB）：单行 18001 无可行 LF（C 空）→ 真值外串
    # 「缩短后整轮重发」，旧「> 18000 chars（机器窗上限）」随长度硬拒删除。
    expect(_chunk_fence("#" * 18001, 0), 0,
           "18001 > 18000 chars，请将该块整体缩短至"
           " 18000 字符以内后整轮重发")
    with pytest.raises(ChunkBirthError) as ei_18001:
        b_18001 = _extract_chunk(_chunk_fence("#" * 18001, 0),
                                 target=_CHUNK_TARGET, index=0, total=6)
        _classify_closed_body(b_18001, 0)
    assert "机器窗上限" not in str(ei_18001.value)
    # 可行窗 I(L)=[max(800,L−18001), min(18000,L−801)]，L=18001 → [800,17200]
    assert ei_18001.value.detail == {
        "schema": "simpraxis.chunk_split_reject.v1",
        "chunk_index": 0,
        "closed_chars": 18001,
        "class": "no_line_boundary",
        "window": [800, 17200],
    }
    assert list(ei_18001.value.detail) == [
        "schema", "chunk_index", "closed_chars", "class", "window"]
    # path/index/total 不符
    expect(_chunk_fence(good, 1, path="evil.py"), 1,
           "块 1 path 不符：收到 evil.py")
    expect(_chunk_fence(good, 0, begin=(
        "<<<<<<< simpraxis.chunk.v1 path='a\"b' index=0 total=6")), 0,
        "path 含非白名单字符")
    expect(_chunk_fence(good, 0, begin=(
        "<<<<<<< simpraxis.chunk.v1 path=" + "a" * 129
        + " index=0 total=6")), 0, "非白名单字符（长度 129")
    expect(_chunk_fence(good, 0, total=5), 0, "total 不符：收到 5")
    expect(_chunk_fence(good, 1), 0, "index 不符：收到 1，应为 0")
    # 不合规整数字段：负号/前导零/全角，一律不回显
    for bad in ("-1", "01", "＋1", "１"):
        expect(_chunk_fence(good, 0, begin=(
            f"<<<<<<< simpraxis.chunk.v1 path={_CHUNK_TARGET} "
            f"index={bad} total=6")), 0, "index 不合规整数字段（不回显）")
    # 短码 / markdown
    expect(_chunk_fence("x = 1\n", 0), 0, "code 长度")
    expect(_chunk_fence("```python\n" + good, 0), 0, "markdown 围栏")
    # 裸控制字符（TAB 允许；CR 只允许行尾 CRLF）
    expect(_chunk_fence("x = 1\0bad\n" + _fixture_pad("m10", 13), 0),
           0, r"裸控制字符 U\+0000")
    expect(_chunk_fence("a = 1\rb\n" + _fixture_pad("m10", 13), 0),
           0, r"裸控制字符 U\+000D")
    expect(_chunk_fence("x = 1\v\n" + _fixture_pad("m10", 13), 0),
           0, r"裸控制字符 U\+000B")
    # 零宽字符（正文）
    expect(_chunk_fence("x​ = 1\n" + _fixture_pad("m10", 13), 0),
           0, r"零宽字符 U\+200B")
    # 全角漂移：围栏行不合规（只拒，不回显）
    expect(_chunk_fence(good, 0, begin=(
        "＜＜＜＜＜＜＜ simpraxis.chunk.v1 path=v2.py index=0 total=6")),
        0, "起始围栏行不合规")
    expect(_chunk_fence(good, 0, begin=(
        "<<<<<<< simpraxis.chunk.v1 path＝v2.py index＝0 total＝6")),
        0, "起始围栏行不合规")
    # BOM
    expect("﻿" + _chunk_fence(good, 0), 0, r"U\+FEFF BOM")
    # 围栏外正文（起前/尾后），原因不摘录内容
    expect(_chunk_fence(good, 0, pre="不好意思先寒暄一句\n"), 0,
           "围栏外存在非空白正文")
    expect(_chunk_fence(good, 0, post="\n尾后解释文字"), 0,
           "围栏外存在非空白正文")
    expect(_chunk_fence(good, 0, pre="　\n"), 0, "围栏外存在非空白正文")
    expect(_chunk_fence(good, 0, pre="​\n"), 0, "围栏外存在非空白正文")
    # 多起始/多结束/body 内核记/缺尾
    b2 = (f"<<<<<<< simpraxis.chunk.v1 path={_CHUNK_TARGET} "
          "index=0 total=6")
    expect(b2 + "\n" + _fixture_pad("m10", 13) + "\n" + b2
           + "\n>>>>>>> simpraxis.chunk.v1.end", 0, "起始围栏出现 2 次")
    expect(_chunk_fence(good + "\n>>>>>>> simpraxis.chunk.v1.end\ntail", 0),
           0, "结束围栏出现 2 次")
    expect(_chunk_fence(
        "x = 1  # simpraxis.chunk.v1 drift\n" + _fixture_pad("m10", 13), 0),
        0, "代码体内第 2 行出现围栏核记")
    expect(_chunk_fence(good, 0, omit_end=True), 0, "缺结束围栏行")
    # end 在 begin 之前且其后再无 end：拒（情形 4，begin 后 0 个 end）
    b_head = (f"<<<<<<< simpraxis.chunk.v1 path={_CHUNK_TARGET} "
              "index=0 total=6")
    expect(">>>>>>> simpraxis.chunk.v1.end\n" + b_head + "\n" + good, 0,
           "缺结束围栏行")
    # 七连 < 但无核记的普通代码行：过；行首 SP/TAB 缩进：过
    near = "z = a <<<<<<< b\n" + _fixture_pad("m10", 13)
    assert _extract_chunk(_chunk_fence(near, 0),
                          target=_CHUNK_TARGET, index=0, total=6) == near
    crlf = _chunk_fence(good, 0).replace("\n", "\r\n")
    assert _extract_chunk(crlf, target=_CHUNK_TARGET,
                          index=0, total=6) == good
    # 纯空白 body
    expect(_chunk_fence("   \n\t", 0), 0, "code 为空或纯空白")
    # 历史 char139 等价形：首行三引号中文 docstring + 裸 LF + 引号反斜杠
    hist = ('"""合成 docstring。\n他说"启动"：路径 C:\\Users\\test\n'
            '三引号结束。"""\n' + _fixture_pad("m10", 13))
    got = _extract_chunk(_chunk_fence(hist, 0),
                         target=_CHUNK_TARGET, index=0, total=6)
    assert got == hist and '他说"启动"' in got


def test_11_validate_assembled_machine_checks():
    codes = _happy_chunk_codes()
    _validate_assembled_chunks("".join(codes))       # 合法拼接不抛
    with pytest.raises(ChunkBirthError, match="顶层重复 def/class"):
        _validate_assembled_chunks(
            codes[0] + codes[1] + codes[1] + codes[3] + codes[4] + codes[5])
    with pytest.raises(ChunkBirthError, match="ast.parse 失败"):
        _validate_assembled_chunks("".join(codes[:5]) + "def broken(:\n")
    # ast 原因只含类型+行/列，不附模型源码片段（MAJOR-1）
    try:
        _validate_assembled_chunks("".join(codes[:5]) + "def broken(:\n")
    except ChunkBirthError as exc:
        msg = str(exc)
        assert "def broken(" not in msg and "SyntaxError" in msg
    # 拼接总长边界 29999/30000（字符口径）。run6 §15.8：30000 侧过地板后还须
    # 过入口守卫——以等长植入把尾部 # 换为 def main()+A_v1 双引号守卫（逐字），
    # 长度口径一字不动（:len 双断言、29999 raises 均原样）。
    _guard_v1_program = (
        'def main():\n    return 0\n\n\n'
        'if __name__ == "__main__":\n'
        '    raise SystemExit(main())\n')
    s29999 = "x = 1\n" + "#" * (29999 - len("x = 1\n"))
    s30000 = _guard_v1_program + "#" * (30000 - len(_guard_v1_program))
    assert len(s29999) == 29999 and len(s30000) == 30000
    with pytest.raises(ChunkBirthError, match="拼接总长 29999 < 30000"):
        _validate_assembled_chunks(s29999)
    _validate_assembled_chunks(s30000)
    # 逃单块组合：一块堆满 18000 + 五块 801 桩 = 22005，逐块过、拼接必拒
    # （设计 §2.4 的 22000=18000+5×800 同形反例；801 桩同样远超 800 下限）
    big = "#" * 18000
    small = ["#" * 801 for _ in range(5)]
    pieces = [big] + small
    for j, pc in enumerate(pieces):
        assert _extract_chunk(_chunk_fence(pc, j, total=6),
                              target=_CHUNK_TARGET, index=j, total=6) == pc
    assert len("".join(pieces)) == 22005
    with pytest.raises(ChunkBirthError, match="拼接总长 22005 < 30000"):
        _validate_assembled_chunks("".join(pieces))
    # run5（规格 §15.1-A）：逃单块组合登记到新行政顶 36000——一块 36000
    # 多行闭合件（LF@17999，纯注释 ast/重名中立）经分类器原样接受出 record，
    # 五块 800 桩拼接 40000+ 过三闸（语义变化：run4 前此形在长度硬拒，run5
    # 起 18001–36000 多行件不是失败轴；单行件仍 C 空拒，见 run5 专项块）。
    big36 = "#" * 17999 + "\n" + "#" * 18000
    assert len(big36) == 36000
    assert _extract_chunk(_chunk_fence(big36, 0, total=6),
                          target=_CHUNK_TARGET, index=0, total=6) == big36
    rec36 = _classify_closed_body(big36, 0)
    assert list(rec36) == ["schema", "chunk_index", "closed_chars",
                           "cut_offset", "cut_line", "frag0_chars",
                           "frag1_chars", "boundary"]
    assert rec36 == {"schema": "simpraxis.chunk_split.v1", "chunk_index": 0,
                     "closed_chars": 36000, "cut_offset": 17999,
                     "cut_line": 0, "frag0_chars": 17999,
                     "frag1_chars": 18000, "boundary": "line"}
    # run6 §15.8：pads[0] 等长植入同一 A_v1 守卫（恰 800 字符，# 补足），
    # 其余四垫仍为纯 # 800；join 总长恒等 40005（新增 len 断言钉死）。
    pad0 = _guard_v1_program + "#" * (800 - len(_guard_v1_program))
    assert len(pad0) == 800
    pads = [pad0] + ["#" * 800 for _ in range(4)]
    joined = "\n".join([big36] + pads)
    assert len(joined) == 40005
    _validate_assembled_chunks(joined)  # 40005 ≥ 30000
    # 40000 单行闭合件：L>36000 → 类3 over_split_max（即使想切也越过 2×18000）。
    one40 = "#" * 40000
    with pytest.raises(ChunkBirthError) as ei40:
        b40 = _extract_chunk(_chunk_fence(one40, 0, total=6),
                             target=_CHUNK_TARGET, index=0, total=6)
        _classify_closed_body(b40, 0)
    msg40 = str(ei40.value)
    assert "40000 > 36000 chars（机器窗上限）" in msg40
    assert "> 18000" not in msg40                    # 旧长度拒串反向钉
    assert ei40.value.detail == {
        "schema": "simpraxis.chunk_split_reject.v1",
        "chunk_index": 0,
        "closed_chars": 40000,
        "class": "over_split_max",
        "window": None,
    }


def _fixture_pad(tag, lines):
    # 合成填充注释（唯一可搜前缀，防跨块重名误判；非任何解题脚本内容）。
    return "\n".join(
        f"# 夹具填充 {tag} {k:04d} 合成文本非解题脚本 " + "z" * 40
        for k in range(lines))


def _happy_chunk_codes():
    """六块合成 happy fixture：每块 800-18000 字符、拼接 ≥30000 且 ast 合法、
    顶层名唯一。禁止为过测试反向调长度常量（评审 L2）。"""
    c0 = ("'''合成夹具 c0：拆块出生端到端，非解题脚本'''\n"
          "from __future__ import annotations\nimport json\n"
          "from pathlib import Path\n\nFIXTURE_CONSTANT_A = 1\n\n"
          + _fixture_pad("c0", 120) + "\n")
    c1 = ("def fixture_alpha(value):\n"
          "    # 合成夹具函数 alpha\n"
          "    return value + FIXTURE_CONSTANT_A + 1\n\n"
          + _fixture_pad("c1", 120) + "\n")
    c2 = ("def fixture_beta(value):\n"
          "    # 合成夹具函数 beta（步态/控制族占位，非解题脚本）\n"
          "    return value * 2\n\n"
          + _fixture_pad("c2", 120) + "\n")
    c3 = ("def fixture_gamma(value):\n"
          "    # 合成夹具函数 gamma（采集/判定族占位，非解题脚本）\n"
          "    return max(value, 0)\n\n"
          + _fixture_pad("c3", 120) + "\n")
    c4 = ("def fixture_delta(value):\n"
          "    # 合成夹具函数 delta（数据产物族占位，非解题脚本）\n"
          "    return {'v': value}\n\n"
          + _fixture_pad("c4", 120) + "\n")
    # run6 §15.8：c5 入口须与 V1 同形——def main() + 逐字双引号 A_v1 守卫
    # （raise SystemExit(main())）；写 telemetry.json 行为不变，仍为合成夹具。
    c5 = ("def main():\n"
          "    Path('telemetry.json').write_text("
          "json.dumps({'_fixture': True}), encoding='utf-8')\n\n\n"
          'if __name__ == "__main__":\n'
          "    raise SystemExit(main())\n"
          + _fixture_pad("c5", 120) + "\n")
    return [c0, c1, c2, c3, c4, c5]


def _chunked_runner(tmp_path, responses, *, thread="t6chunk", cap=3,
                    # 默认对齐 task6 生产 CODING_MAX_TOKENS_V2（2026-10-21 升 32768）
                    tokens=32768, timeout=600, usage=((50, 60))):
    wd = tmp_path / "work"
    wd.mkdir(parents=True, exist_ok=True)
    gw = FakeGateway(responses, usage=usage)
    cache = CallReplay(tmp_path / "cache")
    runner = AgentRunner(gw, FakeVector(), evidence_root=tmp_path / "ev",
                         call_cache=cache)
    spec = RunSpec(
        thread_id=thread, workdir=wd,
        cmd=[sys.executable, str(wd / _CHUNK_TARGET)],
        telemetry_relpath="telemetry.json", batch_id="T6-FIXTURE",
        billing_task=6, run_idx=1, max_coding_rounds=cap,
        coding_max_tokens=tokens, coding_timeout_s=timeout,
        chunked_birth=ChunkSpec(
            target_path=_CHUNK_TARGET, chunk_count=6,
            chunk_titles=_CHUNK_TITLES,
            per_call_sections=(
                ("V2 升阶任务合同（夹具）",
                 "合同正文夹具：genesis_humanoid_dr_v2.py ζ/Hunt-Crossley"),
                ("已批准 macro 提案（夹具）",
                 json.dumps({"name": "fixture-zeta"}, ensure_ascii=False)),
                ("V1 结构骨架（夹具）", "V1 sha256=0123456789abcdef 骨架行")),
            chunk_boundaries=_CHUNK_BOUNDARIES))
    return runner, spec, gw, wd, cache


def test_12_chunked_birth_six_calls_assemble_and_birth(tmp_path):
    codes = _happy_chunk_codes()
    assert [len(c) >= CHUNK_MIN_CODE_CHARS for c in codes] == [True] * 6
    assert [len(c) <= CHUNK_MAX_CODE_CHARS for c in codes] == [True] * 6
    assert len("".join(codes)) >= CHUNK_MIN_ASSEMBLED_CHARS
    runner, spec, gw, wd, cache = _chunked_runner(
        tmp_path, [_chunk_fence(c, i) for i, c in enumerate(codes)])
    rec = runner.run(spec)

    assert rec["status"] == "ok"
    assert rec["coding_round"] == 1 and rec["executions"] == 2
    assert len(gw.calls) == 6                       # 恰好 6 次顺序调用
    assert [c["kw"] for c in gw.calls] == [
        # 攢批92：kimi 强制思考契约 temperature=0.6 + thinking disabled（T8-D2）。
        {"max_tokens": 32768, "temperature": TEMPERATURE, "timeout": 600,
         "thinking": CODING_THINKING}] * 6
    target = wd / _CHUNK_TARGET
    assert target.is_file()
    # run4：块间补恰好一个 LF（设计 §2.2 EBNF）；happy body 自带尾换行，
    # 接缝处多一空行即模型原文（见 test_18 自然形态）。
    assert target.read_text(encoding="utf-8") == "\n".join(codes)
    assert (wd / "telemetry.json").is_file()       # 出生后重跑收敛

    for i, call in enumerate(gw.calls):
        user = call["messages"][1]["content"]
        assert f"## 拆块交付（第 {i + 1}/6 块）" in user
        assert f"index 逐字等于 {i}" in user
        assert "## V2 升阶任务合同（夹具）" in user  # 合同每 call 都带
        assert "已批准 macro 提案（夹具）" in user
        assert "V1 结构骨架（夹具）" in user
        if i == 0:
            assert "### 前序已定稿块" not in user
        else:
            assert codes[i - 1] in user            # 前一块逐字进围栏
            assert f"以下是前 {i} 块拼接后的逐字定稿" in user

    cache_dir = tmp_path / "cache"
    keys = [f"coding_t6chunk_r1_round1_chunk{i}.json" for i in range(6)]
    for name in keys:
        assert (cache_dir / name).is_file()
    assert not (cache_dir / "coding_t6chunk_r1_round1.json").exists()
    sha = [json.loads((cache_dir / n).read_text())["req_sha256"] for n in keys]
    assert len(set(sha)) == 6                      # 指纹逐块不同（含 chunk_index）

    ev = tmp_path / "ev" / "t6chunk"
    for i in range(6):
        req_p = ev / f"coding_round1.chunk{i}.req.json"
        resp_p = ev / f"coding_round1.chunk{i}.resp.json"
        assert req_p.is_file() and resp_p.is_file()
        req = json.loads(req_p.read_text())
        assert req["chunk_index"] == i and req["chunk_total"] == 6
        resp = json.loads(resp_p.read_text())
        assert resp["ok"] is True
        assert resp["chunk"] == {"path": _CHUNK_TARGET, "index": i,
                                 "total": 6}
        assert len(resp["code"]) >= CHUNK_MIN_CODE_CHARS
    decision = json.loads(
        (ev / "coding_round1.decision.json").read_text())
    assert decision["applied"] == [_CHUNK_TARGET] and decision["rejected"] is None
    # run5 m3（§15.2-m3）：成功 chunked 出生 decision 必带长度恰 6 的 records，
    # 六块均 ≤18000 → 全 null；键序 applied/rejected/chunk_split_records。
    assert list(decision) == ["applied", "rejected", "chunk_split_records"]
    assert decision["chunk_split_records"] == [None] * 6
    # observe-only 时间线计数（§15.1-D）：0 二拆；正常出生不产生 fragments 件。
    tl1 = next(t for t in rec["timeline"] if t.get("coding_round") == 1)
    assert tl1["split_accepted"] == 0 and tl1["split_rejected"] == 0
    assert tl1["closed_oversize_blocks"] == 0
    assert not list(ev.glob("*.fragments.json"))
    assert not (wd / "FAILURE.json").exists()


def test_18_chunked_birth_natural_body_lf_seams(tmp_path):
    """阻断回归（设计 §2.2 EBNF）：自然上线形态 body 无尾换行——begin 行后与
    end 行前的 LF 都是语法分隔符，提取器逐字保留 body，故拼接侧（L980 出生
    拼接 + L1139 前序上下文）必须在块间补回恰好一个 LF；"".join 会让相邻块
    行粘连（出生 ast IndentationError；i≥2 模型看到的前序参考码被污染）。
    本测在旧 "".join 下必红，"\n".join 下绿。"""
    codes = [c.rstrip("\n") for c in _happy_chunk_codes()]
    assert all(CHUNK_MIN_CODE_CHARS <= len(c) <= CHUNK_MAX_CODE_CHARS
               for c in codes)
    assert len("\n".join(codes)) >= CHUNK_MIN_ASSEMBLED_CHARS
    # 真实提取器往返：恢复 code 逐字且均不带尾换行（自然形态先钉死）。
    for i, code in enumerate(codes):
        got = _extract_chunk(_chunk_fence(code, i), target=_CHUNK_TARGET,
                             index=i, total=6)
        assert got == code and not got.endswith("\n")

    runner, spec, gw, wd, _ = _chunked_runner(
        tmp_path, [_chunk_fence(c, i) for i, c in enumerate(codes)],
        thread="t6lf", cap=1)
    rec = runner.run(spec)

    assert rec["status"] == "ok", rec.get("reason")
    assert len(gw.calls) == 6
    born = (wd / _CHUNK_TARGET).read_text(encoding="utf-8")
    ast.parse(born)                             # 出生内容 ast 通过
    _validate_assembled_chunks(born)           # 含重名/总长/ast 全闸
    # 出生文件五处接缝：上块末行 + 恰好一个 LF + 下块首行，粘连子串绝不出现。
    for k in range(5):
        last_line = codes[k].rsplit("\n", 1)[-1]
        first_line = codes[k + 1].split("\n", 1)[0]
        assert last_line + "\n" + first_line in born
        assert last_line + first_line not in born
    # L1139 现场：i≥1 请求里的前序定稿（```python 围栏内）每一处真实块间接缝
    # 都有 LF、不粘连（真正的 c0|c1 接缝首次出现在 i=2 的请求里；i=1 只有 c0，
    # 其末行也必须独立成行，不与围栏闭合 ``` 粘连）。
    for i in range(1, 6):
        prompt = gw.calls[i]["messages"][1]["content"]
        assert "\n".join(codes[:i]) in prompt
        for k in range(i - 1):
            last_line = codes[k].rsplit("\n", 1)[-1]
            first_line = codes[k + 1].split("\n", 1)[0]
            assert last_line + "\n" + first_line in prompt
            assert last_line + first_line not in prompt
    assert codes[0].rsplit("\n", 1)[-1] + "\n```" in \
        gw.calls[1]["messages"][1]["content"]

    # 另一形态：body 自带尾换行（现有 happy 夹具）仍绿——多空行即模型原文。
    happy = _happy_chunk_codes()
    runner2, spec2, gw2, wd2, _ = _chunked_runner(
        tmp_path / "form2",
        [_chunk_fence(c, i) for i, c in enumerate(happy)],
        thread="t6lf2", cap=1)
    rec2 = runner2.run(spec2)
    assert rec2["status"] == "ok" and len(gw2.calls) == 6
    born2 = (wd2 / _CHUNK_TARGET).read_text(encoding="utf-8")
    assert born2 == "\n".join(happy)
    _validate_assembled_chunks(born2)


def test_13_default_path_call_kwargs_8192_120(tmp_path):
    wd = _write_workdir(tmp_path / "d", CONVERGING_SCRIPT)
    gw = FakeGateway([json.dumps(
        {"patches": [{"path": "fix.txt", "old": "NEEDS_FIX",
                      "new": "PATCHED"}]})])
    runner = _runner(tmp_path, gw)
    rec = runner.run(_real_cmd_spec("t4-kw", wd))
    assert rec["status"] == "ok" and len(gw.calls) == 1
    assert gw.calls[0]["kw"] == {
        "max_tokens": MAX_TOKENS, "temperature": TEMPERATURE,
        "timeout": DEFAULT_CODING_TIMEOUT_S, "thinking": CODING_THINKING}
    assert gw.calls[0]["kw"]["max_tokens"] == 8192
    assert gw.calls[0]["kw"]["timeout"] == 120
    assert "拆块交付" not in gw.calls[0]["messages"][1]["content"]


def test_14_chunked_spec_after_birth_uses_single_patch_path(tmp_path):
    # 目标已存在（出生后的修补轮）：ChunkSpec 在挂载状态但 runner 按磁盘现状
    # 选路，回到既有单补丁调用，消息不含拆块段/合同段。
    wd = _write_workdir(tmp_path / "repair", CONVERGING_SCRIPT)
    (wd / "script.py").unlink()
    (wd / _CHUNK_TARGET).write_text(CONVERGING_SCRIPT, encoding="utf-8")
    gw = FakeGateway([json.dumps(
        {"patches": [{"path": "fix.txt", "old": "NEEDS_FIX",
                      "new": "PATCHED"}]})], usage=(50, 60))
    runner, spec, _, _, cache = _chunked_runner_with(
        tmp_path, gw, thread="t6repair", wd=wd)
    rec = runner.run(spec)
    assert rec["status"] == "ok" and len(gw.calls) == 1
    prompt = gw.calls[0]["messages"][1]["content"]
    assert "拆块交付" not in prompt and "升阶任务合同" not in prompt
    assert (cache.dir / "coding_t6repair_r1_round1.json").is_file()
    assert not (cache.dir / "coding_t6repair_r1_round1_chunk0.json").exists()
    # run5 m3（§15.2-m3）：非出生单补丁轮 decision 不带 chunk_split_records，
    # 时间线也不写 observe-only 三计数。
    decision = json.loads(
        (tmp_path / "ev" / "t6repair" / "coding_round1.decision.json").read_text())
    assert "chunk_split_records" not in decision
    assert "rejected_detail" not in decision
    for t in rec["timeline"]:
        assert "split_accepted" not in t and "split_rejected" not in t


def _chunked_runner_with(tmp_path, gw, *, thread, wd):
    cache = CallReplay(tmp_path / "cache")
    runner = AgentRunner(gw, FakeVector(), evidence_root=tmp_path / "ev",
                         call_cache=cache)
    spec = RunSpec(
        thread_id=thread, workdir=wd,
        cmd=[sys.executable, str(wd / _CHUNK_TARGET)],
        telemetry_relpath="telemetry.json", batch_id="T6-FIXTURE",
        billing_task=6, run_idx=1, max_coding_rounds=3,
        coding_max_tokens=32768, coding_timeout_s=600,
        chunked_birth=ChunkSpec(
            target_path=_CHUNK_TARGET, chunk_count=6,
            chunk_titles=_CHUNK_TITLES,
            per_call_sections=(("V2 升阶任务合同（夹具）", "合同夹具"),),
            chunk_boundaries=_CHUNK_BOUNDARIES))
    return runner, spec, gw, wd, cache


@pytest.mark.parametrize("scenario,calls_expected,needle", [
    ("missing_index", 1, "起始围栏行不合规"),
    ("bad_path", 2, "块 1 path 不符"),
    ("too_short", 1, "块 0 code 长度"),
    ("dup_names", 6, "顶层重复 def/class"),
    ("ast_error", 6, "ast.parse 失败"),
    ("total_short", 6, "拼接总长"),
    ("missing_end", 1, "缺结束围栏行"),
    ("outside_text", 1, "围栏外存在非空白正文"),
    ("fenced_body_nucleus", 1, "出现围栏核记"),
    ("fullwidth_drift", 1, "起始围栏行不合规"),
])
def test_15_bad_chunk_fails_whole_round_without_half_file(
        tmp_path, scenario, calls_expected, needle):
    codes = _happy_chunk_codes()
    if scenario == "missing_index":
        bad_begin = (f"<<<<<<< simpraxis.chunk.v1 path={_CHUNK_TARGET} "
                     "total=6")  # 缺 index 字段：begin 正则 0 命中
        responses = [_chunk_fence(codes[0], 0, begin=bad_begin)]
    elif scenario == "bad_path":
        responses = [_chunk_fence(codes[0], 0),
                     _chunk_fence(codes[1], 1, path="evil.py")]
    elif scenario == "too_short":
        responses = [_chunk_fence("x = 1\n", 0)]
    elif scenario == "dup_names":
        dup2 = ("def fixture_alpha(value):\n    return value\n\n"
                + _fixture_pad("dup", 120) + "\n")
        responses = [_chunk_fence(c, i)
                     for i, c in enumerate([codes[0], codes[1], dup2,
                                            codes[3], codes[4], codes[5]])]
    elif scenario == "ast_error":
        bad5 = "def fixture_broken(:\n    pass\n" + _fixture_pad("ast", 120)
        responses = [_chunk_fence(c, i)
                     for i, c in enumerate([*codes[:5], bad5])]
    elif scenario == "missing_end":
        responses = [_chunk_fence(codes[0], 0, omit_end=True)]
    elif scenario == "outside_text":
        responses = [_chunk_fence(codes[0], 0, pre="模型寒暄一句\n")]
    elif scenario == "fenced_body_nucleus":
        bad = "x = 1\n# simpraxis.chunk.v1\n" + _fixture_pad("nb", 120)
        responses = [_chunk_fence(bad, 0)]
    elif scenario == "fullwidth_drift":
        bad_begin = (f"＜＜＜＜＜＜＜ simpraxis.chunk.v1 path={_CHUNK_TARGET}"
                     " index=0 total=6")
        responses = [_chunk_fence(codes[0], 0, begin=bad_begin)]
    else:  # total_short：六块都过 800，拼接总长 < 30000
        short = ["#" * 801 for _ in range(6)]
        responses = [_chunk_fence(c, i) for i, c in enumerate(short)]

    runner, spec, gw, wd, _ = _chunked_runner(
        tmp_path, responses, thread="t6bad", cap=1)
    rec = runner.run(spec)

    assert rec["status"] == "failed"
    assert needle in rec["reason"]
    assert "末次拆块出生机器原因" in rec["reason"]
    assert len(gw.calls) == calls_expected
    target = wd / _CHUNK_TARGET
    assert not target.exists()                  # 任何坏块都不半成品落盘
    # workdir 内只允许结案 FAILURE.json，无 V2 目标/脚本/半成品残留
    assert sorted(p.name for p in wd.iterdir()) == ["FAILURE.json"]
    ev = tmp_path / "ev" / "t6bad"
    decision = json.loads(
        (ev / "coding_round1.decision.json").read_text())
    assert decision["applied"] == [] and needle in decision["rejected"]
    # run5（§15.3-H2）：矩阵全是 15+ 旧形/短码，detail 恒 None →
    # rejected_detail/chunk_split_records 条件键缺席；0 fragments 件。
    assert "rejected_detail" not in decision
    assert "chunk_split_records" not in decision
    assert not list(ev.glob("*.fragments.json"))
    # observe-only 计数落时间线：0 accept/0 reject（非长度两路径）。
    tl = next(t for t in rec["timeline"] if "chunk_birth_rejected" in t)
    assert tl["split_accepted"] == 0 and tl["split_rejected"] == 0
    assert tl["closed_oversize_blocks"] == 0
    failure = json.loads((spec.workdir / "FAILURE.json").read_text())
    assert failure["coding_round"] == 1
    assert any("chunk_birth_rejected" in t for t in failure["timeline"])
    # 逐块类失败：失败块 resp 证据自带 ok=false + parse_rejected，且不入缓存
    if calls_expected <= 2:
        i = calls_expected - 1
        resp = json.loads(
            (ev / f"coding_round1.chunk{i}.resp.json").read_text())
        assert resp["ok"] is False and needle in resp["parse_rejected"]
        assert not (tmp_path / "cache"
                    / f"coding_t6bad_r1_round1_chunk{i}.json").exists()


def test_16_chunk_fingerprint_includes_index_and_keys_safe():
    from orchestrator.common.callcache import request_fingerprint
    # 攢批92：TEMPERATURE=0.6 + thinking disabled 成对（kimi-k3.1-pro 供应侧
    # 硬约束矩阵，T8-D2 账外探针 C1/C4/E）；thinking 入指纹，改约自动 miss。
    fp = {i: request_fingerprint(
        "same-prompt", None,
        {"max_tokens": 32768, "temperature": TEMPERATURE,
         "thinking": CODING_THINKING,
         "chunk_index": i, "chunk_total": 6},
        stage="coding_debug", model="fake-model") for i in range(6)}
    assert len(set(fp.values())) == 6
    assert request_fingerprint(
        "same-prompt", None,
        {"max_tokens": 32768, "temperature": TEMPERATURE,
         "thinking": CODING_THINKING,
         "chunk_index": 1, "chunk_total": 6},
        stage="coding_debug", model="fake-model") == fp[1]
    # thinking 键缺席/变约必须是不同指纹（防透传回归把 kwarg 静默丢掉）。
    assert request_fingerprint(
        "same-prompt", None,
        {"max_tokens": 32768, "temperature": TEMPERATURE,
         "chunk_index": 1, "chunk_total": 6},
        stage="coding_debug", model="fake-model") != fp[1]
    for i in range(6):
        assert re.fullmatch(
            r"[A-Za-z0-9_.-]{1,160}",
            f"coding_t6chunk_r1_round1_chunk{i}")


def test_17_feedback_zero_change_only_block0_round2(tmp_path):
    # 评审 MAJOR-2 + §10.4 终裁：反馈机制零改动。round1 块 3 失败 →
    # round2 仅块 0 prompt 含机器原因段，块 1-5 不含；缓存键含 round 分量，
    # 跨轮命中结构上不可能（round2 六块全重付）；ChunkBirthError 无 chunk_index。
    assert not hasattr(ChunkBirthError, "chunk_index")
    codes = _happy_chunk_codes()
    bad_body = "x = 1\n# simpraxis.chunk.v1 drift-line\n" \
        + _fixture_pad("fb", 120)
    round1 = [_chunk_fence(codes[0], 0), _chunk_fence(codes[1], 1),
              _chunk_fence(codes[2], 2), _chunk_fence(bad_body, 3)]
    round2 = [_chunk_fence(c, i) for i, c in enumerate(codes)]
    runner, spec, gw, wd, _ = _chunked_runner(
        tmp_path, round1 + round2, thread="t6fb", cap=2)
    rec = runner.run(spec)

    assert rec["status"] == "ok"
    assert rec["coding_round"] == 2 and len(gw.calls) == 10
    feedback_title = "### 上一轮拆块交付失败反馈"
    # round2 = calls[4:10]
    u0 = gw.calls[4]["messages"][1]["content"]
    assert feedback_title in u0 and "出现围栏核记" in u0
    for call in gw.calls[5:10]:
        assert feedback_title not in call["messages"][1]["content"]
    # round1 块 0 无反馈段（首轮无历史）
    assert feedback_title not in gw.calls[0]["messages"][1]["content"]
    # round1 失败轮不半成品出生，round2 成功后文件方出生
    assert (wd / _CHUNK_TARGET).is_file()


# ===========================================================================
# Task6 run5：闭合件机器二拆（唯一依据 .omc/handoffs/task6-sentinel-seam-
# design.md §15.1–§15.6；0 付费、全 hermetic；全部夹具为合成 LF/CRLF 布局，
# 严禁拷贝 /mnt 真实响应文本，硬规则 3）
# ===========================================================================

_RUN5_RECORD_KEYS = ["schema", "chunk_index", "closed_chars", "cut_offset",
                     "cut_line", "frag0_chars", "frag1_chars", "boundary"]
_RUN5_DETAIL_KEYS = ["schema", "chunk_index", "closed_chars", "class",
                     "window"]
_RUN5_CEMPTY_EXT = (
    "块 0 code 长度 {L} > 18000 chars，请将该块整体缩短至"
    " 18000 字符以内后整轮重发（本机逐字符计数）")
_RUN5_OVERSIZE_EXT = "块 0 code 长度 {L} > 36000 chars（机器窗上限）"


def _run5_dense_body(tag, target):
    """合成纯注释多行闭合件（非解题脚本）：LF 密（每行约 50 字符）、
    总长恰 target、首行/末行均为 '#'；ast 合法、无顶层名（重名中立）。"""
    lines: list[str] = []
    used = 0
    k = 0
    while True:
        line = f"# run5合成夹具 {tag} {k:05d} " + "z" * 24
        if used + len(line) + 1 > target:
            break
        lines.append(line)
        used += len(line) + 1
        k += 1
    body = "\n".join(lines)
    if len(body) < target:          # 整除边界（如 18500=370×50）：末行直接补齐
        body += "#" * (target - len(body))
    assert len(body) == target
    ast.parse(body)
    return body


def test_run5_classifier_unit_math_three_classes():
    """§15.1-A/B/E、§15.2-m2：三分类边界、可行窗、切点整数键（等距取小）、
    m2 四条等式、record/detail 白名单与键序、两条真值外串逐字。"""
    # 类 1：L≤18000 → None（18000 边界，单行/多行各一）
    assert _classify_closed_body("#" * 18000, 0) is None
    assert _classify_closed_body("#" * 800 + "\n" + "#" * 999, 0) is None

    # run4 三次真实失败点的合成 LF 布局：19492/18029/20141 全部 class2 接受。
    for L, tag in ((19492, "r4a"), (18029, "r4b"), (20141, "r4c")):
        body = _run5_dense_body(tag, L)
        rec = _classify_closed_body(body, 2)
        assert rec is not None
        assert list(rec) == _RUN5_RECORD_KEYS
        c = rec["cut_offset"]
        assert rec["schema"] == "simpraxis.chunk_split.v1"
        assert rec["chunk_index"] == 2 and rec["closed_chars"] == L
        assert rec["frag0_chars"] == c
        assert rec["frag1_chars"] == L - c - 1
        assert rec["frag0_chars"] + rec["frag1_chars"] + 1 == L
        assert rec["cut_line"] == body[:c].count("\n")
        assert body[:c] + "\n" + body[c + 1:] == body
        assert CHUNK_MIN_CODE_CHARS <= rec["frag0_chars"] <= CHUNK_MAX_CODE_CHARS
        assert CHUNK_MIN_CODE_CHARS <= rec["frag1_chars"] <= CHUNK_MAX_CODE_CHARS
        assert rec["boundary"] == "line"

    # §15.2 四构造：L18801 端点 800/18000 双向可行；L18802 坏位置 C 空。
    rec = _classify_closed_body("#" * 800 + "\n" + "#" * 18000, 0)
    assert (rec["frag0_chars"], rec["frag1_chars"]) == (800, 18000)
    rec = _classify_closed_body("#" * 18000 + "\n" + "#" * 800, 0)
    assert (rec["frag0_chars"], rec["frag1_chars"]) == (18000, 800)
    for body in ("#" * 800 + "\n" + "#" * 18001,
                 "#" * 18001 + "\n" + "#" * 800):
        with pytest.raises(ChunkBirthError) as ei:
            _classify_closed_body(body, 3)
        assert list(ei.value.detail) == _RUN5_DETAIL_KEYS
        assert ei.value.detail["class"] == "no_line_boundary"
        assert ei.value.detail["window"] == [801, 18000]
        assert ei.value.detail["closed_chars"] == 18802

    # 36000 两形切点 + 单行 C 空（窗 [17999,18000]）
    rec = _classify_closed_body("#" * 17999 + "\n" + "#" * 18000, 0)
    assert (rec["cut_offset"], rec["frag0_chars"],
            rec["frag1_chars"]) == (17999, 17999, 18000)
    rec = _classify_closed_body("#" * 18000 + "\n" + "#" * 17999, 0)
    assert (rec["cut_offset"], rec["frag0_chars"],
            rec["frag1_chars"]) == (18000, 18000, 17999)
    with pytest.raises(ChunkBirthError) as ei:
        _classify_closed_body("#" * 36000, 0)
    assert ei.value.detail["window"] == [17999, 18000]
    assert ei.value.detail["class"] == "no_line_boundary"

    # 36001：算术定义域上界唯一点 → 类3（行政顶 36000=2×18000）
    body36001 = "#" * 18000 + "\n" + "#" * 18000
    assert len(body36001) == 36001
    with pytest.raises(ChunkBirthError) as ei:
        _classify_closed_body(body36001, 0)
    assert str(ei.value) == _RUN5_OVERSIZE_EXT.format(L=36001)
    assert ei.value.detail == {
        "schema": "simpraxis.chunk_split_reject.v1", "chunk_index": 0,
        "closed_chars": 36001, "class": "over_split_max", "window": None}

    # 等距切点：L=18002，LFs@8999/9000/9001，c*=9000（9000/9001 等距取小；
    # 第三邻 8999 专杀「取首个」变异）
    tie = "#" * 8999 + "\n\n\n" + "#" * 9000
    assert len(tie) == 18002
    rec = _classify_closed_body(tie, 0)
    assert rec["cut_offset"] == 9000 and rec["cut_line"] == 1

    # 窗外形 LF 不入 C，但计入 cut_line：L18202 LFs@100/@201/@9000
    body = ("#" * 100 + "\n" + "#" * 100 + "\n" + "#" * 8798 + "\n"
            + "#" * 9201)
    assert len(body) == 18202
    rec = _classify_closed_body(body, 1)
    assert rec["cut_offset"] == 9000 and rec["cut_line"] == 2

    # C 空 detail 逐字等值（两窗端点规格）+ 真值外串
    body = "x" * 100 + "\n" + "y" * 17900
    assert len(body) == 18001
    with pytest.raises(ChunkBirthError) as ei:
        _classify_closed_body(body, 0)
    assert str(ei.value) == _RUN5_CEMPTY_EXT.format(L=18001)
    assert ei.value.detail == {
        "schema": "simpraxis.chunk_split_reject.v1", "chunk_index": 0,
        "closed_chars": 18001, "class": "no_line_boundary",
        "window": [800, 17200]}
    assert "机器窗上限" not in str(ei.value)
    body = "a" * 17000 + "\n" + "b" * 18998
    assert len(body) == 35999
    with pytest.raises(ChunkBirthError) as ei:
        _classify_closed_body(body, 0)
    assert ei.value.detail["window"] == [17998, 18000]
    assert ei.value.detail["closed_chars"] == 35999
    assert "35999" in str(ei.value) and "机器窗上限" not in str(ei.value)


def test_run5_birth_multiline_oversize_accepted_record_and_file(tmp_path):
    """端到端：18001 多行闭合件原样接受为第 4 个逻辑块；6 调用、fragments
    逐 record 落盘且先于拼接闸、decision records 长度 6、下一块 prompt 只含
    逻辑块原文而不含任何 record 字段。"""
    codes = _happy_chunk_codes()
    big = _run5_dense_body("e2e18001", 18001)
    codes[3] = big
    runner, spec, gw, wd, _ = _chunked_runner(
        tmp_path, [_chunk_fence(c, i) for i, c in enumerate(codes)],
        thread="t6r5a", cap=1)
    rec = runner.run(spec)

    assert rec["status"] == "ok", rec.get("reason")
    assert len(gw.calls) == 6
    born = (wd / _CHUNK_TARGET).read_text(encoding="utf-8")
    assert born == "\n".join(codes)          # codes 恒为 6 个逻辑块
    assert big in born

    ev = tmp_path / "ev" / "t6r5a"
    decision = json.loads(
        (ev / "coding_round1.decision.json").read_text())
    records = decision["chunk_split_records"]
    assert len(records) == 6
    assert [r is None for r in records] == [True, True, True, False, True, True]
    expect_rec = _classify_closed_body(big, 3)
    assert records[3] == expect_rec
    frag_p = ev / "coding_round1.chunk3.fragments.json"
    on_disk = json.loads(frag_p.read_text())
    assert on_disk == expect_rec
    assert list(on_disk) == _RUN5_RECORD_KEYS
    # 落盘 JSON 不允许 sort_keys（键序即白名单序，m2 机器可对账）
    raw = frag_p.read_text(encoding="utf-8")
    assert raw.index("cut_offset") < raw.index("frag0_chars")
    assert len(list(ev.glob("*.fragments.json"))) == 1

    # 块 4 的 prompt：前序含 big 逐字，零 record 机制字段
    u4 = gw.calls[4]["messages"][1]["content"]
    assert big in u4
    for k in ("simpraxis.chunk_split.v1", "cut_offset", "cut_line",
              "frag0_chars", "frag1_chars", "closed_chars"):
        assert k not in u4

    t1 = next(t for t in rec["timeline"] if t.get("coding_round") == 1)
    assert t1["split_accepted"] == 1 and t1["split_rejected"] == 0
    assert t1["closed_oversize_blocks"] == 1


def test_run5_two_oversize_blocks_records_aligned(tmp_path):
    """两块 class2：records 按下标对齐长度 6、两件 fragments、计数 2。"""
    codes = _happy_chunk_codes()
    b2 = _run5_dense_body("twoA", 18500)
    b4 = _run5_dense_body("twoB", 21000)
    codes[2], codes[4] = b2, b4
    runner, spec, gw, wd, _ = _chunked_runner(
        tmp_path, [_chunk_fence(c, i) for i, c in enumerate(codes)],
        thread="t6r5b", cap=1)
    rec = runner.run(spec)
    assert rec["status"] == "ok", rec.get("reason") and len(gw.calls) == 6

    ev = tmp_path / "ev" / "t6r5b"
    decision = json.loads(
        (ev / "coding_round1.decision.json").read_text())
    records = decision["chunk_split_records"]
    assert len(records) == 6
    assert [r is None for r in records] == [True, True, False, True, False, True]
    assert records[2] == _classify_closed_body(b2, 2)
    assert records[4] == _classify_closed_body(b4, 4)
    names = sorted(p.name for p in ev.glob("*.fragments.json"))
    assert names == ["coding_round1.chunk2.fragments.json",
                     "coding_round1.chunk4.fragments.json"]
    # 后续块 prompt 见到的是逐字逻辑块（不是碎片）
    assert b2 in gw.calls[3]["messages"][1]["content"]
    assert b4 in gw.calls[5]["messages"][1]["content"]
    t1 = next(t for t in rec["timeline"] if t.get("coding_round") == 1)
    assert t1["split_accepted"] == 2 and t1["closed_oversize_blocks"] == 2


@pytest.mark.parametrize("kind,body,cls,window,ext", [
    ("over", "#" * 40000, "over_split_max", None,
     "> 36000 chars（机器窗上限）"),
    ("cempty", "#" * 18001, "no_line_boundary", [800, 17200],
     "请将该块整体缩短至 18000 字符以内后整轮重发"),
])
def test_run5_length_reject_short_circuit(tmp_path, kind, body, cls,
                                          window, ext):
    """类3/C 空：块 0 即短路——后续块零调用零 req 证据；decision 带条件键
    rejected_detail（恰三键序），计数 0/1/1，无半成品。"""
    runner, spec, gw, wd, _ = _chunked_runner(
        tmp_path, [_chunk_fence(body, 0)], thread="t6r5" + kind, cap=1)
    rec = runner.run(spec)
    assert rec["status"] == "failed" and len(gw.calls) == 1
    assert ext in rec["reason"]
    ev = tmp_path / "ev" / ("t6r5" + kind)
    decision = json.loads(
        (ev / "coding_round1.decision.json").read_text())
    assert list(decision) == ["applied", "rejected", "rejected_detail"]
    assert decision["applied"] == [] and ext in decision["rejected"]
    detail = decision["rejected_detail"]
    assert list(detail) == _RUN5_DETAIL_KEYS
    assert detail["class"] == cls and detail["window"] == window
    for i in range(1, 6):
        assert not (ev / f"coding_round1.chunk{i}.req.json").exists()
    assert not list(ev.glob("*.fragments.json"))
    t1 = next(t for t in rec["timeline"] if "chunk_birth_rejected" in t)
    assert t1["split_accepted"] == 0 and t1["split_rejected"] == 1
    assert t1["closed_oversize_blocks"] == 1
    assert t1["chunk_birth_rejected_detail"] == detail
    assert not (wd / _CHUNK_TARGET).exists()


def test_run5_oversize_bare_cr_rejected_pre_classify(tmp_path):
    """brief §4 CRLF (a)：>18000 体中部一个非行尾 U+000D 必须死在情形 12
    （分类前）——无 detail、无 fragments、后续块零调用、计数器 0/0/0。
    同一基件去 CR 后分类器本应 class2 接受，证明拦截点先于长度分类。"""
    base = _run5_dense_body("barecr", 20199)
    assert len(base) == 20199
    assert _classify_closed_body(base, 0) is not None  # 无 CR：本应 class2
    pos = 10  # 行中非行尾落点（前后字符皆非 LF）
    assert base[pos] != "\n" and base[pos + 1] != "\n"
    body = base[:pos] + "\r" + base[pos:]
    runner, spec, gw, wd, _ = _chunked_runner(
        tmp_path, [_chunk_fence(body, 0)], thread="t6r5cr", cap=1)
    rec = runner.run(spec)
    assert rec["status"] == "failed" and len(gw.calls) == 1
    assert ("含裸控制字符 U+000D（TAB 允许；CR 只允许行尾 CRLF）"
            in rec["reason"])
    # 15+ 旧形 detail=None：decision 恰两键，timeline 无 detail 同义键
    ev = tmp_path / "ev" / "t6r5cr"
    decision = json.loads(
        (ev / "coding_round1.decision.json").read_text())
    assert list(decision) == ["applied", "rejected"]
    assert "rejected_detail" not in decision
    assert decision["applied"] == []
    for i in range(1, 6):
        assert not (ev / f"coding_round1.chunk{i}.req.json").exists()
    assert not list(ev.glob("*.fragments.json"))
    t1 = next(t for t in rec["timeline"] if "chunk_birth_rejected" in t)
    assert "chunk_birth_rejected_detail" not in t1
    assert t1["split_accepted"] == 0 and t1["split_rejected"] == 0
    assert t1["closed_oversize_blocks"] == 0
    assert not (wd / _CHUNK_TARGET).exists()


def test_run5_fragments_survive_assembled_floor_reject(tmp_path):
    """record 先于拼接三闸写盘：块0 class2 接受、五块 801 桩 → 六调用齐后
    拼接撞 30000 地板，fragments 件仍存活；失败 decision 不带 records。"""
    big = _run5_dense_body("survive", 18500)
    stubs = ["#" * 801 for _ in range(5)]
    responses = [_chunk_fence(big, 0)] + [
        _chunk_fence(s, i + 1) for i, s in enumerate(stubs)]
    runner, spec, gw, wd, _ = _chunked_runner(
        tmp_path, responses, thread="t6r5s", cap=1)
    rec = runner.run(spec)
    assert rec["status"] == "failed" and len(gw.calls) == 6
    assert "拼接总长" in rec["reason"]

    ev = tmp_path / "ev" / "t6r5s"
    frag = ev / "coding_round1.chunk0.fragments.json"
    assert frag.is_file()
    assert json.loads(frag.read_text()) == _classify_closed_body(big, 0)
    decision = json.loads(
        (ev / "coding_round1.decision.json").read_text())
    assert "chunk_split_records" not in decision
    assert "rejected_detail" not in decision       # 拼接失败 detail=None
    t1 = next(t for t in rec["timeline"] if "chunk_birth_rejected" in t)
    assert t1["split_accepted"] == 1               # 以存活 fragments 件计数
    assert t1["split_rejected"] == 0
    assert t1["closed_oversize_blocks"] == 1
    assert not (wd / _CHUNK_TARGET).exists()


def test_run5_external_strings_only_in_u0_next_round(tmp_path):
    """§15.6 结构隔离：长度失败后重生轮，真值外串只进块0反馈段，块1-5 零命中。"""
    happy = _happy_chunk_codes()
    cempty_ext = _RUN5_CEMPTY_EXT.format(L=18001)
    over_ext = "> 36000 chars（机器窗上限）"
    round1 = [_chunk_fence("#" * 18001, 0)]
    round2 = [_chunk_fence(c, i) for i, c in enumerate(happy)]
    runner, spec, gw, _, _ = _chunked_runner(
        tmp_path, round1 + round2, thread="t6r5fb", cap=2)
    rec = runner.run(spec)
    assert rec["status"] == "ok" and len(gw.calls) == 7

    u0 = gw.calls[1]["messages"][1]["content"]
    assert "### 上一轮拆块交付失败反馈" in u0
    assert cempty_ext in u0
    assert over_ext not in u0                      # C 空轮不泄露类3 串
    for call in gw.calls[2:7]:
        u = call["messages"][1]["content"]
        assert "上一轮拆块交付失败反馈" not in u
        assert cempty_ext not in u and over_ext not in u


def test_run5_cache_replay_zero_calls_identical_artifacts(tmp_path):
    """kill-9 重放：class2 原文已在调用点缓存，分类确定性重算 0 重付；
    fragments 幂等重写 sha256 相等、records 相等、出生产物逐字相等。"""
    codes = _happy_chunk_codes()
    big = _run5_dense_body("replay", 19000)
    codes[2] = big
    runner, spec, gw, wd, cache = _chunked_runner(
        tmp_path, [_chunk_fence(c, i) for i, c in enumerate(codes)],
        thread="t6r5rp", cap=1)
    rec = runner.run(spec)
    assert rec["status"] == "ok" and len(gw.calls) == 6

    ev = tmp_path / "ev" / "t6r5rp"
    frag = ev / "coding_round1.chunk2.fragments.json"
    sha_before = hashlib.sha256(frag.read_bytes()).hexdigest()
    decision_before = json.loads(
        (ev / "coding_round1.decision.json").read_text())
    born_before = (wd / _CHUNK_TARGET).read_bytes()

    # 重放前情：出生文件被 kill-9 抹掉（但缓存/证据根在），出生路径重选；
    # 新网关任何调用即炸（FakeGateway 空响应池）。
    (wd / _CHUNK_TARGET).unlink()
    (wd / "telemetry.json").unlink()
    gw2 = FakeGateway([])
    runner2 = AgentRunner(gw2, FakeVector(), evidence_root=tmp_path / "ev",
                          call_cache=cache)
    rec2 = runner2.run(spec)
    assert rec2["status"] == "ok", rec2.get("reason")
    assert gw2.calls == []
    assert (wd / _CHUNK_TARGET).read_bytes() == born_before
    assert hashlib.sha256(frag.read_bytes()).hexdigest() == sha_before
    decision_after = json.loads(
        (ev / "coding_round1.decision.json").read_text())
    assert (decision_after["chunk_split_records"]
            == decision_before["chunk_split_records"])


def test_run5_oversize_reason_blocks_canary_and_mechanism_words(tmp_path):
    """m4：模型正文 canary 绝不进机器原因/反馈段；中文机制词族对真值外串
    与重生轮块0 prompt 零命中（块0 整份 prompt 也无 canary）。"""
    canary = "canary-run5-m4-77e1c3"
    head = f"# {canary} "
    body = "#" * 700 + "\n" + head + "#" * (18001 - 701 - len(head))
    assert len(body) == 18001 and body.count("\n") == 1 and body[700] == "\n"
    happy = _happy_chunk_codes()
    responses = [_chunk_fence(body, 0)] + [
        _chunk_fence(c, i) for i, c in enumerate(happy)]
    runner, spec, gw, _, _ = _chunked_runner(
        tmp_path, responses, thread="t6r5m4", cap=2)
    rec = runner.run(spec)
    assert rec["status"] == "ok" and len(gw.calls) == 7

    ev = tmp_path / "ev" / "t6r5m4"
    d1 = json.loads((ev / "coding_round1.decision.json").read_text())
    assert canary not in d1["rejected"]
    assert canary not in json.dumps(d1["rejected_detail"], ensure_ascii=False)
    failure_entries = [t for t in rec["timeline"]
                       if "chunk_birth_rejected" in t]
    assert failure_entries and canary not in json.dumps(
        failure_entries, ensure_ascii=False)
    # 重生轮块0：反馈段是真值外串，整份 prompt 无 canary、无机制词族
    u0 = gw.calls[1]["messages"][1]["content"]
    assert canary not in u0
    family = ("分片", "拆分", "二拆", "机器分片", "切分")
    for w in family:
        assert w not in u0
        assert w not in _RUN5_CEMPTY_EXT.format(L=18001)
        assert w not in _RUN5_OVERSIZE_EXT.format(L=40000)


def test_run5_classifier_pure_no_fs_double_run(monkeypatch):
    """§15.2-m5：纯函数双跑等值且零文件 IO（builtins.open 探针）。"""
    body = _run5_dense_body("pure", 20261)

    def boom(*a, **k):
        raise AssertionError("分类器触碰文件 IO")

    monkeypatch.setattr("builtins.open", boom)
    r1 = _classify_closed_body(body, 4)
    r2 = _classify_closed_body(body, 4)
    assert r1 == r2
    with pytest.raises(ChunkBirthError):
        _classify_closed_body("#" * 40000, 0)
    with pytest.raises(ChunkBirthError):
        _classify_closed_body("#" * 18001, 0)


def test_run5_crlf_full_body_class2_and_deep_registration():
    """CRLF 两条款：(b) 全响应 CRLF 经扫描器归一为 19899 LF 正文后 class2；
    (c) 手工 CRLF body 直送分类器——CR 是普通字符，LF 切点恒等式仍成立。
    夹具纪律：只在纯 LF 基件上单次 .replace（双替会造 \\r\\r\\n 假裸 CR）。"""
    base = "\n".join("#" * 198 for _ in range(100))
    assert len(base) == 19899 and "\r" not in base
    raw = _chunk_fence(base, 0).replace("\n", "\r\n")
    assert "\r\r\n" not in raw
    b = _extract_chunk(raw, target=_CHUNK_TARGET, index=0, total=6)
    assert b == base and len(b) == 19899 and "\r" not in b
    rec = _classify_closed_body(b, 0)
    assert list(rec) == _RUN5_RECORD_KEYS
    assert rec["closed_chars"] == 19899
    assert rec["frag0_chars"] + rec["frag1_chars"] + 1 == 19899
    assert rec["cut_line"] == b[:rec["cut_offset"]].count("\n")

    crlf_body = base.replace("\n", "\r\n")
    assert len(crlf_body) == 19998 and "\r\r\n" not in crlf_body
    rec2 = _classify_closed_body(crlf_body, 0)
    c = rec2["cut_offset"]
    assert crlf_body[c] == "\n"
    assert crlf_body[:c] + "\n" + crlf_body[c + 1:] == crlf_body
    assert "\r" in crlf_body[:c]                # CR 保留在碎片 int 口径内
    assert rec2["frag0_chars"] + rec2["frag1_chars"] + 1 == 19998


def test_run5_fuzz_cut_oracle_fixed_seed():
    """固定种子 fuzz 200 例：独立 oracle 计算 I(L)/C/c*，与生产逐字段对账；
    含单行（C 空）、密 LF（class2）、疏 LF、随机伯努利、类3、类1。"""
    rng = random.Random(20261022)
    n_none = n_accept = n_ce = n_over = 0
    for n in range(200):
        L = rng.randint(800, 40000)
        chars = ["#"] * L
        style = n % 4
        if style == 1:                              # 密 LF
            p = rng.randint(40, 120)
            j = p
            while j < L - 1:
                chars[j] = "\n"
                j += p + rng.randint(0, 40)
        elif style == 2:                            # 疏 LF
            for _ in range(rng.randint(1, 4)):
                chars[rng.randint(1, L - 2)] = "\n"
        elif style == 3:                            # 伯努利稀疏
            for j in range(1, L - 1):
                if rng.random() < 0.01:
                    chars[j] = "\n"
        body = "".join(chars)
        assert len(body) == L

        lo = max(800, L - 18001)
        hi = min(18000, L - 801)
        if L <= CHUNK_MAX_CODE_CHARS:
            assert _classify_closed_body(body, 0) is None
            n_none += 1
        elif L > CHUNK_CLOSED_SPLIT_MAX_CHARS:
            with pytest.raises(ChunkBirthError) as ei:
                _classify_closed_body(body, 0)
            d = ei.value.detail
            assert d["class"] == "over_split_max" and d["window"] is None
            n_over += 1
        else:
            cands = [j for j in range(lo, hi + 1) if body[j] == "\n"]
            if not cands:
                with pytest.raises(ChunkBirthError) as ei:
                    _classify_closed_body(body, 0)
                d = ei.value.detail
                assert list(d) == _RUN5_DETAIL_KEYS
                assert d["class"] == "no_line_boundary"
                assert d["window"] == [lo, hi] and d["closed_chars"] == L
                n_ce += 1
            else:
                cstar = min(cands,
                            key=lambda j: (abs(2 * j - (L - 1)), j))
                r = _classify_closed_body(body, 0)
                assert list(r) == _RUN5_RECORD_KEYS
                assert r["cut_offset"] == cstar
                assert r["frag0_chars"] == cstar
                assert r["frag1_chars"] == L - cstar - 1
                assert r["frag0_chars"] + r["frag1_chars"] + 1 == L
                assert r["cut_line"] == body[:cstar].count("\n")
                assert body[:cstar] + "\n" + body[cstar + 1:] == body
                assert (CHUNK_MIN_CODE_CHARS <= r["frag0_chars"]
                        <= CHUNK_MAX_CODE_CHARS)
                assert (CHUNK_MIN_CODE_CHARS <= r["frag1_chars"]
                        <= CHUNK_MAX_CODE_CHARS)
                n_accept += 1
    # 四类在固定种子下都必须实际发生
    assert n_none > 0 and n_accept > 0 and n_ce > 0 and n_over > 0


# ===========================================================================
# ⑥ run6（设计 §15.7/§15.8 v3.2）：可执行入口守卫 AST 谓词 + rc0 缺产物
#    机器核验（D2）。全部 0 付费、全合成形态——禁止读 /mnt、禁止真 V2 响应
#    （硬规则 3）。形态逐字转写自 LEAD 仲裁件/探针（均不入仓依赖）：
#    .omc/handoffs/lead_h5_predicate_prototype.py（46 实例）、
#    /tmp/lead_v31_lowcheck.py（H5.3 async 三形 + R15 七实例）。
# [run6-entry-guard-tests]
# ===========================================================================


_RUN6_DEF_MAIN = "def main(argv=None):\n    return 0\n"
_RUN6_GUARD_V1 = ('if __name__ == "__main__":\n'
                  '    raise SystemExit(main())\n')


def _run6_h54_forms():
    """H5.4 正式矩阵：41 标签/46 实例/45 独立代码体（E8=E17 同码）。

    返回 (label, should_accept, code) 三元组；接受 8（A1-A7+A_v1）、
    拒绝 19 实例/14 标签（R1-R14）、外延 E1-E19（E18 拒/E19 受）。
    """
    d = _RUN6_DEF_MAIN
    g = _RUN6_GUARD_V1
    accept = {
        "A1": d + 'if __name__ == "__main__":\n    main()\n',
        "A2": d + 'if __name__ == "__main__":\n'
                  '    import sys; sys.exit(main())\n',
        "A3": d + 'x = 1\nprint("hi")\n' + g,
        "A_v1": d + g,
        "A4": (d + 'if __name__ == "__main__":\n'
               '    if True:\n        with open("x") as f:\n'
               '            if f:\n                main()\n'),
        "A5": (d + 'if __name__ == "__main__":\n'
               '    try:\n        raise SystemExit(main())\n'
               '    finally:\n        pass\n'),
        "A6": ('if __name__ == "__main__":\n    pass\n' + d + g),
        "A7": d + "if __name__ == '__main__':\n    main([])\n",
    }
    r1_body = "\n".join(
        f"try:\n    x{j} = 1\nexcept Exception as exc:\n"
        "    _ = type(exc).__name__"
        for j in range(4))
    reject = {
        "R1": "def main():\n    pass\n" + r1_body
              + '\nraise SystemExit("batch 编排未定义，请通过子命令调用")\n',
        "R2": d + 'if "__main__" == __name__:\n    main()\n',
        "R3a": d + 'if __name__ == "__main":\n    main()\n',
        "R3b": d + "if __name__ == 'main':\n    main()\n",
        "R3c": d + 'if __name__ == b"__main__":\n    main()\n',
        "R4a": d + 'if __name__ is "__main__":\n    main()\n',
        "R4b": d + 'if __name__ in ("__main__",):\n    main()\n',
        "R4c": d + 'if __name__ != "x":\n    main()\n',
        "R5": ("def wrapper():\n"
               '    if __name__ == "__main__":\n        main()\n'
               "def main():\n    pass\n"),
        "R6": ("class C:\n    def run(self):\n"
               '        if __name__ == "__main__":\n            main()\n'
               "def main():\n    pass\n"),
        "R7": (d + 'if __name__ == "__main__":\n    pass\n'
               "else:\n    main()\n"),
        "R8": d + 'if __name__ == "__main__":\n    pass\n',
        "R9": d + 'raise SystemExit("batch 编排未定义")\n',
        "R10": (d + 'if __name__ == "__main__":\n'
               "    def inner():\n        main()\n"),
        "R11": (d + 'import sys\nif __name__ == "__main__":\n'
               '    getattr(sys.modules[__name__], "main")()\n'),
        "R12": d + 'if __name__ == "__main__" and True:\n    main()\n',
        "R13": (d + 'if type(exc).__name__ == "__main__":\n'
               "    main()\n"),
        "R14a": ("def fixture_main():\n    pass\n"
                 "if __name__ == '__main__':\n    fixture_main()\n"),
        "R14b": d + 'if __name__ == "__main__":\n    run()\n',
    }
    external = {
        "E1_ternary_false_branch": (False, d + 'if __name__ == "__main__":\n    x = 0 if True else main()\n'),
        "E2_ternary_true_branch": (True, d + 'if __name__ == "__main__":\n    x = main() if True else 0\n'),
        "E3_ternary_test_call": (False, d + 'if __name__ == "__main__":\n    x = 1 if main() else 2\n'),
        "E4_listcomp": (False, d + 'if __name__ == "__main__":\n    [main() for _ in (1,)]\n'),
        "E5_match_case": (True, d + 'if __name__ == "__main__":\n    match 1:\n        case 1:\n            main()\n'),
        "E6_bare_name": (False, d + 'if __name__ == "__main__":\n    main\n'),
        "E7_module_call_no_guard": (False, d + "main()\n"),
        "E8_nested_if_orelse_trap": (False, d + 'if __name__ == "__main__":\n    if False:\n        pass\n    else:\n        main()\n'),
        "E9_ifexp_comparator": (False, d + 'if __name__ == ("__main__" if True else "x"):\n    main()\n'),
        "E10_lambda": (False, d + 'if __name__ == "__main__":\n    f = lambda: main()\n'),
        "E11_boolop_dead_and": (False, d + 'if __name__ == "__main__":\n    False and main()\n'),
        "E12_boolop_live_and": (False, d + 'if __name__ == "__main__":\n    True and main()\n'),
        "E13_boolop_live_or": (False, d + 'if __name__ == "__main__":\n    0 or main()\n'),
        "E14_plain_after_boolop": (True, d + 'if __name__ == "__main__":\n    False and x\n    main()\n'),
        "E15_try_finally_only": (False, d + 'if __name__ == "__main__":\n    try:\n        pass\n    finally:\n        main()\n'),
        "E16_try_except_only": (False, d + 'if __name__ == "__main__":\n    try:\n        pass\n    except Exception:\n        main()\n'),
        "E17_nested_if_orelse_trap": (False, d + 'if __name__ == "__main__":\n    if False:\n        pass\n    else:\n        main()\n'),
        "E18_match_class_pattern": (False, d + 'if __name__ == "__main__":\n    match 1:\n        case main():\n            pass\n'),
        "E19_match_guard_call": (True, d + 'if __name__ == "__main__":\n    match 1:\n        case 1 if main():\n            pass\n'),
    }
    forms = [(name, True, code) for name, code in accept.items()]
    forms += [(name, False, code) for name, code in reject.items()]
    forms += [(name, expect, code) for name, (expect, code) in external.items()]
    return forms, accept, reject, external


_RUN6_H54_FORMS, _RUN6_ACCEPT, _RUN6_REJECT, _RUN6_EXTERNAL = _run6_h54_forms()
_RUN6_H54_IDS = [f[0] for f in _RUN6_H54_FORMS]


def test_run6_h54_matrix_counts_41_labels_46_instances_45_bodies():
    # v3.2 口径：核心 22（8 接受 + 14 拒签）+ 外延 19 = 41 标签；
    # R3/R4/R14 展 a/b/c 后 46 实例；E8=E17 同码 → 45 独立体。
    assert len(_RUN6_H54_FORMS) == 46
    assert len(_RUN6_H54_IDS) == 46
    # 标签口径：R3a/b/c、R4a/b/c、R14a/b 各归一标签（实例 19、标签 14）。
    families = {re.sub(r"^(R\d+)[abc]$", r"\1", x) for x in _RUN6_H54_IDS}
    assert len(families) == 41
    bodies = {code for _, _, code in _RUN6_H54_FORMS}
    assert len(bodies) == 45
    assert _RUN6_EXTERNAL["E8_nested_if_orelse_trap"][1] \
        == _RUN6_EXTERNAL["E17_nested_if_orelse_trap"][1]
    assert _RUN6_EXTERNAL["E8_nested_if_orelse_trap"][0] is False


@pytest.mark.parametrize("label,should_accept,code", _RUN6_H54_FORMS,
                         ids=_RUN6_H54_IDS)
def test_run6_h54_entry_guard_predicate_46_forms(
        label, should_accept, code):
    # 全部形态 ast 可解析（非法编译期形态是 H5.3 async 单列，不在本矩阵）。
    tree = ast.parse(code)
    if should_accept:
        _validate_entry_guard(tree)               # 不抛即接受
        _validate_assembled_chunks(
            code + "#" * max(0, CHUNK_MIN_ASSEMBLED_CHARS - len(code))
            + "\n")
        return
    with pytest.raises(ChunkBirthError) as ei:
        _validate_entry_guard(tree)
    # 拒绝串逐字冻结、detail 恒 None（守卫拒不带 simpraxis.chunk_split_reject）。
    assert str(ei.value) == _ENTRY_GUARD_REASON
    assert ei.value.detail is None


@pytest.mark.parametrize("name,code", [
    ("await", _RUN6_DEF_MAIN + 'if __name__ == "__main__":\n    await main()\n'),
    ("async_for", _RUN6_DEF_MAIN + 'if __name__ == "__main__":\n'
                   '    async for x in main():\n        pass\n'),
    ("async_with", _RUN6_DEF_MAIN + 'if __name__ == "__main__":\n'
                    '    async with main():\n        pass\n'),
])
def test_run6_h53_module_level_async_forms_parse_ok_compile_syntaxerror(
        name, code):
    # H5.3 唯一登记的过接受残余：谓词经泛下降可触达叶子 main（受），
    # 但模块级 compile() 响亮 SyntaxError（编译期失败，非静默 rc0）。
    ast.parse(code)
    _validate_entry_guard(ast.parse(code))
    with pytest.raises(SyntaxError):
        compile(code, f"<run6-{name}>", "exec")


def test_run6_guard_reject_reason_only_into_next_round_block0(tmp_path):
    """M1②/④：守卫拒绝串只进下一轮块 0 反馈段；detail=None 三条件键缺席、
    split_rejected=0、0 fragments 件；i>0 块反馈段为空（test_17 同源断言保留）。"""
    codes = _happy_chunk_codes()
    c5_noguard = ("def main():\n    pass  # run6 合成：无入口守卫\n\n"
                  + _fixture_pad("c5nog", 120) + "\n")
    assert CHUNK_MIN_CODE_CHARS <= len(c5_noguard) <= CHUNK_MAX_CODE_CHARS
    round1 = [_chunk_fence(c, i)
              for i, c in enumerate(codes[:5] + [c5_noguard])]
    round2 = [_chunk_fence(c, i) for i, c in enumerate(codes)]
    runner, spec, gw, wd, _ = _chunked_runner(
        tmp_path, round1 + round2, thread="t6guard", cap=2)
    rec = runner.run(spec)

    assert rec["status"] == "ok" and rec["coding_round"] == 2
    assert len(gw.calls) == 12
    feedback_title = "### 上一轮拆块交付失败反馈"
    u0r2 = gw.calls[6]["messages"][1]["content"]
    assert feedback_title in u0r2 and _ENTRY_GUARD_REASON in u0r2
    for call in gw.calls[7:12]:
        assert feedback_title not in call["messages"][1]["content"]
    # round1 六块均无反馈段（首轮无历史）
    for call in gw.calls[:6]:
        assert feedback_title not in call["messages"][1]["content"]
    # round1 decision：rejected 逐字串、rejected_detail 键缺席。
    decision = json.loads(
        (tmp_path / "ev" / "t6guard" / "coding_round1.decision.json")
        .read_text())
    assert decision["applied"] == []
    assert decision["rejected"] == _ENTRY_GUARD_REASON
    assert "rejected_detail" not in decision
    assert "chunk_split_records" not in decision
    assert not list((tmp_path / "ev" / "t6guard").glob("*.fragments.json"))
    # 时间线（exec1 条目，成功返回仍携首轮拒绝注释）：三条件键缺席 + 计数 0。
    t1 = next(t for t in rec["timeline"]
              if "chunk_birth_rejected" in t)
    assert t1["chunk_birth_rejected"] == _ENTRY_GUARD_REASON
    assert "chunk_birth_rejected_detail" not in t1
    assert t1["split_accepted"] == 0 and t1["split_rejected"] == 0
    assert t1["closed_oversize_blocks"] == 0


def test_run6_total_chunkbirth_error_raise_sites_pin_23():
    # M1⑤：全文件 raise ChunkBirthError 恰 23（run6 前 22 + 入口守卫 1）。
    import orchestrator.agent_runner.runner as runner_mod
    src = Path(runner_mod.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    sites = [n for n in ast.walk(tree)
             if isinstance(n, ast.Raise) and isinstance(n.exc, ast.Call)
             and isinstance(n.exc.func, ast.Name)
             and n.exc.func.id == "ChunkBirthError"]
    assert len(sites) == 23
    # 守卫那一枚必须是冻结常量直填构造（无 f-string/拼接/ detail）。
    guard_fn = next(n for n in tree.body
                    if isinstance(n, ast.FunctionDef)
                    and n.name == "_validate_entry_guard")
    raises = [n for n in ast.walk(guard_fn) if isinstance(n, ast.Raise)]
    assert len(raises) == 1
    exc = raises[0].exc
    assert isinstance(exc, ast.Call)
    assert isinstance(exc.func, ast.Name) and exc.func.id == "ChunkBirthError"
    assert len(exc.args) == 1 and not exc.keywords
    assert isinstance(exc.args[0], ast.Name)
    assert exc.args[0].id == "_ENTRY_GUARD_REASON"


# ---- M1⑥：D2 rc=0 缺约定产物机器核验句 -------------------------------------

def test_run6_d2_note_verbatim_idempotent_no_dquote_no_frame():
    note = _RC0_MISSING_ARTIFACT_FMT.format(relpath="summary.json")
    assert '"' not in note                       # 全句无 ASCII 双引号
    assert not _FRAME_RE.search(note)           # 永不被 traceback 帧正则命中
    once = _append_rc0_missing_artifact_note("\n", "summary.json")
    assert once == "\n\n\n" + note and once.count(note) == 1
    twice = _append_rc0_missing_artifact_note(once, "summary.json")
    assert twice == once                        # 幂等：节点重放不二次注入
    empty = _append_rc0_missing_artifact_note("", "summary.json")
    assert empty == "\n\n" + note and empty.count(note) == 1
    # relpath 只由入参代入（两个不同产物各成一句）。
    note2 = _RC0_MISSING_ARTIFACT_FMT.format(relpath="telemetry.json")
    assert note != note2 and "summary.json" not in note2


class _Run6ScriptedRecall:
    """只记录查询串的合成 recall（0 触网；retrieve 双路均空）。"""

    def __init__(self):
        self.queries: list[str] = []

    def retrieve(self, query, *, site):
        assert site == "coding"
        self.queries.append(query)
        return {"kb_hits": [], "learning_hits": []}

    def record_adoption(self, **kwargs):
        pass


def _run6_rc0_missing_spec(tmp_path, *, telemetry_relpath, script, cap,
                           thread, responses):
    wd = tmp_path / "work"
    wd.mkdir(parents=True, exist_ok=True)
    (wd / "script.py").write_text(script, encoding="utf-8")
    gw = FakeGateway(responses)
    recall = _Run6ScriptedRecall()
    runner = AgentRunner(gw, FakeVector(), evidence_root=tmp_path / "ev",
                         call_cache=CallReplay(tmp_path / "cache"),
                         recall=recall)
    spec = RunSpec(
        thread_id=thread, workdir=wd,
        cmd=[sys.executable, str(wd / "script.py")],
        telemetry_relpath=telemetry_relpath, batch_id="T6-FIXTURE",
        billing_task=6, run_idx=1, max_coding_rounds=cap)
    return runner, spec, gw, wd, recall


def test_run6_d2_rc0_missing_artifact_note_single_patch_path(tmp_path):
    """rc=0 但 summary.json 缺席：机器核验句逐字进当轮模型消息（单补丁路），
    timeline/FAILURE/recall 查询/snippets 四处不见句。"""
    script = "import sys\nsys.exit(0)\n"
    patch = json.dumps({"patches": [{
        "path": "script.py", "old": "sys.exit(0)",
        "new": "sys.exit(0)  # run6 D2 round1"}]})
    runner, spec, gw, wd, recall = _run6_rc0_missing_spec(
        tmp_path, telemetry_relpath="summary.json", script=script, cap=1,
        thread="t6d2", responses=[patch])
    rec = runner.run(spec)

    assert rec["status"] == "failed" and len(gw.calls) == 1
    note = _RC0_MISSING_ARTIFACT_FMT.format(relpath="summary.json")
    user_msg = gw.calls[0]["messages"][1]["content"]
    assert user_msg.count(note) == 1
    p_tb = user_msg.index("## traceback / 日志尾部")
    p_note = user_msg.index(note)
    p_snip = user_msg.index("## 相关源文件片段")
    assert p_tb < p_note < p_snip              # 只落在模型侧 tail 段
    # 四处反查：timeline stderr_tail
    assert all(note not in t["stderr_tail"] for t in rec["timeline"])
    # _fail 产物（reason 与整个 FAILURE.json 正文）
    failure = json.loads((wd / "FAILURE.json").read_text(encoding="utf-8"))
    assert note not in failure["reason"]
    assert note not in json.dumps(failure, ensure_ascii=False)
    assert note not in rec["reason"]
    # recall 查询串：raw tail（rc0 空 stderr/stdout → "\n"），无 note
    assert recall.queries == ["仿真失败 \n"]
    # snippets：采集发生在注入之前；note 自身无帧，真实帧片段也不含 note
    assert _collect_snippets(wd, "\n") == []
    framed = f'File "{wd / "script.py"}", line 1\n' + note
    snips = _collect_snippets(wd, framed)
    assert snips and all(note not in s["snippet"] for s in snips)


def test_run6_d2_rc_nonzero_does_not_inject_note(tmp_path):
    """rc≠0（真失败）路径永不注入 D2 句。"""
    script = "import sys\nsys.exit(3)\n"
    patch = json.dumps({"patches": [{
        "path": "script.py", "old": "sys.exit(3)",
        "new": "sys.exit(3)  # run6 rc3 round1"}]})
    runner, spec, gw, wd, _ = _run6_rc0_missing_spec(
        tmp_path, telemetry_relpath="summary.json", script=script, cap=1,
        thread="t6d2rc", responses=[patch])
    rec = runner.run(spec)

    assert rec["status"] == "failed" and len(gw.calls) == 1
    note = _RC0_MISSING_ARTIFACT_FMT.format(relpath="summary.json")
    assert note not in gw.calls[0]["messages"][1]["content"]
    assert "机器核验（非模型输出）" not in gw.calls[0]["messages"][1]["content"]


def test_run6_d2_note_chunked_path_same_source(tmp_path, monkeypatch):
    """拆块出生路同源：run() 把同一 repair_tail 实参透传进出生轮（白盒拦取，
    0 网关调用）；与单补丁路共用同一 helper、同一冻结句。"""
    runner, spec, gw, wd, _ = _chunked_runner(
        tmp_path, [], thread="t6d2chunk", cap=1)
    spec.cmd = [sys.executable, "-c", ""]     # rc=0 空跑，目标 v2.py 缺席
    captured = {}

    def _capture_birth_round(self, **kwargs):
        captured["tail"] = kwargs["tail"]
        raise ChunkBirthError("run6 合成：出生轮白盒拦取，不产生网关调用")

    monkeypatch.setattr(type(runner), "_run_chunked_birth_round",
                        _capture_birth_round)
    rec = runner.run(spec)

    assert rec["status"] == "failed" and gw.calls == []
    note = _RC0_MISSING_ARTIFACT_FMT.format(relpath="telemetry.json")
    expected = _append_rc0_missing_artifact_note("\n", "telemetry.json")
    assert captured["tail"] == expected
    assert captured["tail"].count(note) == 1


def test_run6_empty_patches_feedback_continue_cap_fail(tmp_path):
    """M1⑦ 反转（v1.4 必改清单③；设计 §3 行为代差登记节 J7）：空 patches
    由 instant-death 收窄为帽内 transport 反馈续跑——两轮均拒后帽尽 _fail，
    reason 携末次传输机器原因；「补丁安全拒绝（不重试）」仅保留 P1 路径
    越界（test_02 不动）。"""
    script = "import sys\nsys.exit(3)\n"
    empty = json.dumps({"patches": []})
    runner, spec, gw, wd, _ = _run6_rc0_missing_spec(
        tmp_path, telemetry_relpath="summary.json", script=script, cap=2,
        thread="t6empty", responses=[empty, empty])
    rec = runner.run(spec)

    assert rec["status"] == "failed"
    assert len(gw.calls) == 2 and rec["executions"] == 2
    # 帽尽走 transport _fail（非 P1 instant-death 文案）
    assert "补丁安全拒绝（不重试）" not in rec["reason"]
    assert "末次补丁传输机器原因" in rec["reason"]
    assert "返回中既无 unified_diff 也无 patches[]" in rec["reason"]
    assert (wd / "FAILURE.json").is_file()
    # timeline：两轮 transport 拒绝键 + coding_round 同位落键（§2.4 MINOR-2）
    tr = [t for t in rec["timeline"] if "patch_transport_rejected" in t]
    assert len(tr) == 2
    assert [t["coding_round"] for t in tr] == [1, 2]
    # decision 证据：transport True、applied 空、rejected 为机器原因
    ev = tmp_path / "ev" / "t6empty"
    for n in (1, 2):
        doc = json.loads(
            (ev / f"coding_round{n}.decision.json").read_text(encoding="utf-8"))
        assert doc["applied"] == [] and doc["rejected"] \
            == "返回中既无 unified_diff 也无 patches[]"
        assert doc["transport"] is True


# ---- M1⑧：rc2 拆块六消息逐字快照（固定 tail，含 snippets/cmd 段） ----------

_RUN6_RC2_SNAPSHOT = r'''"你是具身智能仿真工具链的排障 coding agent。只允许输出严格 JSON：{\"patches\":[{\"path\":\"相对运行工作目录的路径\",\"old\":\"原文（须唯一）\",\"new\":\"替换文\"}]}，或 {\"unified_diff\":\"git 风格 diff\"}。补丁只能改本次运行工作目录内的文件；禁止改配置、凭据、仓库源码。不要输出解释、不要输出 image。"
"## V2 升阶任务合同（快照夹具）\n合同正文快照 ζ\n\n## 拆块交付（第 1/6 块）\n\n目标文件分 6 块顺序出生，你正在交付第 1/6 块；本次只允许产出这一块。\n\n### 交付块清单（编号、标题、行数预算与边界）\n1. 《合成头》\n第 0 块只允许写合成章节 0。\n2. 《合成注入》\n第 1 块只允许写合成章节 1。\n3. 《合成控制》\n第 2 块只允许写合成章节 2。\n4. 《合成判定》\n第 3 块只允许写合成章节 3。\n5. 《合成产物》\n第 4 块只允许写合成章节 4。\n6. 《合成媒体》\n第 5 块只允许写合成章节 5。\n\n### 本块边界（只允许写的章节）\n你本次只能产出第 1/6 块《合成头》：第 0 块只允许写合成章节 0。\n严禁在本块 code 中写入任何其他块的章节，也不得重复前序已定稿内容。\n\n### 上一轮拆块交付失败反馈（机器原因，本轮必须避免）\n合成机器原因（快照夹具）：上一轮拆块交付失败\n\n### 交付格式（围栏纯文本，禁止 JSON）\n本块 Python 原文直接书写，**不要**放入 JSON、不要做任何转义；反斜杠、引号、三引号、中文与换行都按 Python 源码原样写。你的整条响应必须且只能是下面三个部分：首行为起始围栏（独占一行）、中间为本块 Python 原文（若干行）、末行为结束围栏（独占一行）；字段逐字照抄、顺序固定、ASCII 字符：\n<<<<<<< simpraxis.chunk.v1 path=v2.py index=0 total=6\n<本块 Python 原文，逐字；不要 markdown 围栏、不要解释>\n>>>>>>> simpraxis.chunk.v1.end\n- path 逐字等于 v2.py；index 逐字等于 0；total 逐字等于 6；三个字段顺序不得调换、不加引号；\n- 起始围栏之前、结束围栏之后不允许任何字符（空白与换行除外）；代码体内不允许出现围栏行或 simpraxis.chunk.v1 字样；\n- 若响应在结束围栏之前结束（例如输出被截断），本机记录为「缺结束围栏」，本块机器校验失败；\n- code 只含本块 Python 原文，请控制在 18000 字符以内（本机逐字符计数）；少于 800 字符、超过 36000 字符或缺结束围栏，仍机器校验失败；\n- 全部 6 块拼接后须为完整可运行文件（总长不少于 30000 字符），块边界不得重叠。\n\n## 失败命令\n/mnt/venvs/simpraxis/bin/python /work/t6/v2.py\n\n## traceback / 日志尾部\nTraceback (most recent call last):\n  File \"v2.py\", line 7\nRuntimeError: 快照夹具失败\n\n## 相关源文件片段（仅限运行工作目录）\n### v2.py:7\n     5| x = 1\n     6| y = 2\n     7| -> raise RuntimeError('快照夹具失败')\n\n## troubleshooting 知识库命中（namespaces=troubleshooting, top_k=6）\n[S1 来源: knowledge/projects/fixture/t.md#s0 | evidence_refs: E0]\n合成排障条目 0"
"## V2 升阶任务合同（快照夹具）\n合同正文快照 ζ\n\n## 拆块交付（第 2/6 块）\n\n目标文件分 6 块顺序出生，你正在交付第 2/6 块；本次只允许产出这一块。\n\n### 交付块清单（编号、标题、行数预算与边界）\n1. 《合成头》\n第 0 块只允许写合成章节 0。\n2. 《合成注入》\n第 1 块只允许写合成章节 1。\n3. 《合成控制》\n第 2 块只允许写合成章节 2。\n4. 《合成判定》\n第 3 块只允许写合成章节 3。\n5. 《合成产物》\n第 4 块只允许写合成章节 4。\n6. 《合成媒体》\n第 5 块只允许写合成章节 5。\n\n### 本块边界（只允许写的章节）\n你本次只能产出第 2/6 块《合成注入》：第 1 块只允许写合成章节 1。\n严禁在本块 code 中写入任何其他块的章节，也不得重复前序已定稿内容。\n\n### 前序已定稿块（逐字拼接文本，已最终定稿，禁止重复/改动）\n以下是前 1 块拼接后的逐字定稿，你的 code 会被直接拼在其后；拼接结果必须是合法 Python：不得重复其中 import/常量/函数，不得改动或重排其中任何字符。\n```python\n# 快照前序定稿块 0\n```\n\n### 交付格式（围栏纯文本，禁止 JSON）\n本块 Python 原文直接书写，**不要**放入 JSON、不要做任何转义；反斜杠、引号、三引号、中文与换行都按 Python 源码原样写。你的整条响应必须且只能是下面三个部分：首行为起始围栏（独占一行）、中间为本块 Python 原文（若干行）、末行为结束围栏（独占一行）；字段逐字照抄、顺序固定、ASCII 字符：\n<<<<<<< simpraxis.chunk.v1 path=v2.py index=1 total=6\n<本块 Python 原文，逐字；不要 markdown 围栏、不要解释>\n>>>>>>> simpraxis.chunk.v1.end\n- path 逐字等于 v2.py；index 逐字等于 1；total 逐字等于 6；三个字段顺序不得调换、不加引号；\n- 起始围栏之前、结束围栏之后不允许任何字符（空白与换行除外）；代码体内不允许出现围栏行或 simpraxis.chunk.v1 字样；\n- 若响应在结束围栏之前结束（例如输出被截断），本机记录为「缺结束围栏」，本块机器校验失败；\n- code 只含本块 Python 原文，请控制在 18000 字符以内（本机逐字符计数）；少于 800 字符、超过 36000 字符或缺结束围栏，仍机器校验失败；\n- 全部 6 块拼接后须为完整可运行文件（总长不少于 30000 字符），块边界不得重叠。\n\n## 失败命令\n/mnt/venvs/simpraxis/bin/python /work/t6/v2.py\n\n## traceback / 日志尾部\nTraceback (most recent call last):\n  File \"v2.py\", line 7\nRuntimeError: 快照夹具失败\n\n## 相关源文件片段（仅限运行工作目录）\n### v2.py:7\n     5| x = 1\n     6| y = 2\n     7| -> raise RuntimeError('快照夹具失败')\n\n## troubleshooting 知识库命中（namespaces=troubleshooting, top_k=6）\n[S1 来源: knowledge/projects/fixture/t.md#s0 | evidence_refs: E0]\n合成排障条目 0"
"## V2 升阶任务合同（快照夹具）\n合同正文快照 ζ\n\n## 拆块交付（第 3/6 块）\n\n目标文件分 6 块顺序出生，你正在交付第 3/6 块；本次只允许产出这一块。\n\n### 交付块清单（编号、标题、行数预算与边界）\n1. 《合成头》\n第 0 块只允许写合成章节 0。\n2. 《合成注入》\n第 1 块只允许写合成章节 1。\n3. 《合成控制》\n第 2 块只允许写合成章节 2。\n4. 《合成判定》\n第 3 块只允许写合成章节 3。\n5. 《合成产物》\n第 4 块只允许写合成章节 4。\n6. 《合成媒体》\n第 5 块只允许写合成章节 5。\n\n### 本块边界（只允许写的章节）\n你本次只能产出第 3/6 块《合成控制》：第 2 块只允许写合成章节 2。\n严禁在本块 code 中写入任何其他块的章节，也不得重复前序已定稿内容。\n\n### 前序已定稿块（逐字拼接文本，已最终定稿，禁止重复/改动）\n以下是前 2 块拼接后的逐字定稿，你的 code 会被直接拼在其后；拼接结果必须是合法 Python：不得重复其中 import/常量/函数，不得改动或重排其中任何字符。\n```python\n# 快照前序定稿块 0\n# 快照前序定稿块 1\n```\n\n### 交付格式（围栏纯文本，禁止 JSON）\n本块 Python 原文直接书写，**不要**放入 JSON、不要做任何转义；反斜杠、引号、三引号、中文与换行都按 Python 源码原样写。你的整条响应必须且只能是下面三个部分：首行为起始围栏（独占一行）、中间为本块 Python 原文（若干行）、末行为结束围栏（独占一行）；字段逐字照抄、顺序固定、ASCII 字符：\n<<<<<<< simpraxis.chunk.v1 path=v2.py index=2 total=6\n<本块 Python 原文，逐字；不要 markdown 围栏、不要解释>\n>>>>>>> simpraxis.chunk.v1.end\n- path 逐字等于 v2.py；index 逐字等于 2；total 逐字等于 6；三个字段顺序不得调换、不加引号；\n- 起始围栏之前、结束围栏之后不允许任何字符（空白与换行除外）；代码体内不允许出现围栏行或 simpraxis.chunk.v1 字样；\n- 若响应在结束围栏之前结束（例如输出被截断），本机记录为「缺结束围栏」，本块机器校验失败；\n- code 只含本块 Python 原文，请控制在 18000 字符以内（本机逐字符计数）；少于 800 字符、超过 36000 字符或缺结束围栏，仍机器校验失败；\n- 全部 6 块拼接后须为完整可运行文件（总长不少于 30000 字符），块边界不得重叠。\n\n## 失败命令\n/mnt/venvs/simpraxis/bin/python /work/t6/v2.py\n\n## traceback / 日志尾部\nTraceback (most recent call last):\n  File \"v2.py\", line 7\nRuntimeError: 快照夹具失败\n\n## 相关源文件片段（仅限运行工作目录）\n### v2.py:7\n     5| x = 1\n     6| y = 2\n     7| -> raise RuntimeError('快照夹具失败')\n\n## troubleshooting 知识库命中（namespaces=troubleshooting, top_k=6）\n[S1 来源: knowledge/projects/fixture/t.md#s0 | evidence_refs: E0]\n合成排障条目 0"
"## V2 升阶任务合同（快照夹具）\n合同正文快照 ζ\n\n## 拆块交付（第 4/6 块）\n\n目标文件分 6 块顺序出生，你正在交付第 4/6 块；本次只允许产出这一块。\n\n### 交付块清单（编号、标题、行数预算与边界）\n1. 《合成头》\n第 0 块只允许写合成章节 0。\n2. 《合成注入》\n第 1 块只允许写合成章节 1。\n3. 《合成控制》\n第 2 块只允许写合成章节 2。\n4. 《合成判定》\n第 3 块只允许写合成章节 3。\n5. 《合成产物》\n第 4 块只允许写合成章节 4。\n6. 《合成媒体》\n第 5 块只允许写合成章节 5。\n\n### 本块边界（只允许写的章节）\n你本次只能产出第 4/6 块《合成判定》：第 3 块只允许写合成章节 3。\n严禁在本块 code 中写入任何其他块的章节，也不得重复前序已定稿内容。\n\n### 前序已定稿块（逐字拼接文本，已最终定稿，禁止重复/改动）\n以下是前 3 块拼接后的逐字定稿，你的 code 会被直接拼在其后；拼接结果必须是合法 Python：不得重复其中 import/常量/函数，不得改动或重排其中任何字符。\n```python\n# 快照前序定稿块 0\n# 快照前序定稿块 1\n# 快照前序定稿块 2\n```\n\n### 交付格式（围栏纯文本，禁止 JSON）\n本块 Python 原文直接书写，**不要**放入 JSON、不要做任何转义；反斜杠、引号、三引号、中文与换行都按 Python 源码原样写。你的整条响应必须且只能是下面三个部分：首行为起始围栏（独占一行）、中间为本块 Python 原文（若干行）、末行为结束围栏（独占一行）；字段逐字照抄、顺序固定、ASCII 字符：\n<<<<<<< simpraxis.chunk.v1 path=v2.py index=3 total=6\n<本块 Python 原文，逐字；不要 markdown 围栏、不要解释>\n>>>>>>> simpraxis.chunk.v1.end\n- path 逐字等于 v2.py；index 逐字等于 3；total 逐字等于 6；三个字段顺序不得调换、不加引号；\n- 起始围栏之前、结束围栏之后不允许任何字符（空白与换行除外）；代码体内不允许出现围栏行或 simpraxis.chunk.v1 字样；\n- 若响应在结束围栏之前结束（例如输出被截断），本机记录为「缺结束围栏」，本块机器校验失败；\n- code 只含本块 Python 原文，请控制在 18000 字符以内（本机逐字符计数）；少于 800 字符、超过 36000 字符或缺结束围栏，仍机器校验失败；\n- 全部 6 块拼接后须为完整可运行文件（总长不少于 30000 字符），块边界不得重叠。\n\n## 失败命令\n/mnt/venvs/simpraxis/bin/python /work/t6/v2.py\n\n## traceback / 日志尾部\nTraceback (most recent call last):\n  File \"v2.py\", line 7\nRuntimeError: 快照夹具失败\n\n## 相关源文件片段（仅限运行工作目录）\n### v2.py:7\n     5| x = 1\n     6| y = 2\n     7| -> raise RuntimeError('快照夹具失败')\n\n## troubleshooting 知识库命中（namespaces=troubleshooting, top_k=6）\n[S1 来源: knowledge/projects/fixture/t.md#s0 | evidence_refs: E0]\n合成排障条目 0"
"## V2 升阶任务合同（快照夹具）\n合同正文快照 ζ\n\n## 拆块交付（第 5/6 块）\n\n目标文件分 6 块顺序出生，你正在交付第 5/6 块；本次只允许产出这一块。\n\n### 交付块清单（编号、标题、行数预算与边界）\n1. 《合成头》\n第 0 块只允许写合成章节 0。\n2. 《合成注入》\n第 1 块只允许写合成章节 1。\n3. 《合成控制》\n第 2 块只允许写合成章节 2。\n4. 《合成判定》\n第 3 块只允许写合成章节 3。\n5. 《合成产物》\n第 4 块只允许写合成章节 4。\n6. 《合成媒体》\n第 5 块只允许写合成章节 5。\n\n### 本块边界（只允许写的章节）\n你本次只能产出第 5/6 块《合成产物》：第 4 块只允许写合成章节 4。\n严禁在本块 code 中写入任何其他块的章节，也不得重复前序已定稿内容。\n\n### 前序已定稿块（逐字拼接文本，已最终定稿，禁止重复/改动）\n以下是前 4 块拼接后的逐字定稿，你的 code 会被直接拼在其后；拼接结果必须是合法 Python：不得重复其中 import/常量/函数，不得改动或重排其中任何字符。\n```python\n# 快照前序定稿块 0\n# 快照前序定稿块 1\n# 快照前序定稿块 2\n# 快照前序定稿块 3\n```\n\n### 交付格式（围栏纯文本，禁止 JSON）\n本块 Python 原文直接书写，**不要**放入 JSON、不要做任何转义；反斜杠、引号、三引号、中文与换行都按 Python 源码原样写。你的整条响应必须且只能是下面三个部分：首行为起始围栏（独占一行）、中间为本块 Python 原文（若干行）、末行为结束围栏（独占一行）；字段逐字照抄、顺序固定、ASCII 字符：\n<<<<<<< simpraxis.chunk.v1 path=v2.py index=4 total=6\n<本块 Python 原文，逐字；不要 markdown 围栏、不要解释>\n>>>>>>> simpraxis.chunk.v1.end\n- path 逐字等于 v2.py；index 逐字等于 4；total 逐字等于 6；三个字段顺序不得调换、不加引号；\n- 起始围栏之前、结束围栏之后不允许任何字符（空白与换行除外）；代码体内不允许出现围栏行或 simpraxis.chunk.v1 字样；\n- 若响应在结束围栏之前结束（例如输出被截断），本机记录为「缺结束围栏」，本块机器校验失败；\n- code 只含本块 Python 原文，请控制在 18000 字符以内（本机逐字符计数）；少于 800 字符、超过 36000 字符或缺结束围栏，仍机器校验失败；\n- 全部 6 块拼接后须为完整可运行文件（总长不少于 30000 字符），块边界不得重叠。\n\n## 失败命令\n/mnt/venvs/simpraxis/bin/python /work/t6/v2.py\n\n## traceback / 日志尾部\nTraceback (most recent call last):\n  File \"v2.py\", line 7\nRuntimeError: 快照夹具失败\n\n## 相关源文件片段（仅限运行工作目录）\n### v2.py:7\n     5| x = 1\n     6| y = 2\n     7| -> raise RuntimeError('快照夹具失败')\n\n## troubleshooting 知识库命中（namespaces=troubleshooting, top_k=6）\n[S1 来源: knowledge/projects/fixture/t.md#s0 | evidence_refs: E0]\n合成排障条目 0"
"## V2 升阶任务合同（快照夹具）\n合同正文快照 ζ\n\n## 拆块交付（第 6/6 块）\n\n目标文件分 6 块顺序出生，你正在交付第 6/6 块；本次只允许产出这一块。\n\n### 交付块清单（编号、标题、行数预算与边界）\n1. 《合成头》\n第 0 块只允许写合成章节 0。\n2. 《合成注入》\n第 1 块只允许写合成章节 1。\n3. 《合成控制》\n第 2 块只允许写合成章节 2。\n4. 《合成判定》\n第 3 块只允许写合成章节 3。\n5. 《合成产物》\n第 4 块只允许写合成章节 4。\n6. 《合成媒体》\n第 5 块只允许写合成章节 5。\n\n### 本块边界（只允许写的章节）\n你本次只能产出第 6/6 块《合成媒体》：第 5 块只允许写合成章节 5。\n严禁在本块 code 中写入任何其他块的章节，也不得重复前序已定稿内容。\n\n### 前序已定稿块（逐字拼接文本，已最终定稿，禁止重复/改动）\n以下是前 5 块拼接后的逐字定稿，你的 code 会被直接拼在其后；拼接结果必须是合法 Python：不得重复其中 import/常量/函数，不得改动或重排其中任何字符。\n```python\n# 快照前序定稿块 0\n# 快照前序定稿块 1\n# 快照前序定稿块 2\n# 快照前序定稿块 3\n# 快照前序定稿块 4\n```\n\n### 交付格式（围栏纯文本，禁止 JSON）\n本块 Python 原文直接书写，**不要**放入 JSON、不要做任何转义；反斜杠、引号、三引号、中文与换行都按 Python 源码原样写。你的整条响应必须且只能是下面三个部分：首行为起始围栏（独占一行）、中间为本块 Python 原文（若干行）、末行为结束围栏（独占一行）；字段逐字照抄、顺序固定、ASCII 字符：\n<<<<<<< simpraxis.chunk.v1 path=v2.py index=5 total=6\n<本块 Python 原文，逐字；不要 markdown 围栏、不要解释>\n>>>>>>> simpraxis.chunk.v1.end\n- path 逐字等于 v2.py；index 逐字等于 5；total 逐字等于 6；三个字段顺序不得调换、不加引号；\n- 起始围栏之前、结束围栏之后不允许任何字符（空白与换行除外）；代码体内不允许出现围栏行或 simpraxis.chunk.v1 字样；\n- 若响应在结束围栏之前结束（例如输出被截断），本机记录为「缺结束围栏」，本块机器校验失败；\n- code 只含本块 Python 原文，请控制在 18000 字符以内（本机逐字符计数）；少于 800 字符、超过 36000 字符或缺结束围栏，仍机器校验失败；\n- 全部 6 块拼接后须为完整可运行文件（总长不少于 30000 字符），块边界不得重叠。\n\n## 失败命令\n/mnt/venvs/simpraxis/bin/python /work/t6/v2.py\n\n## traceback / 日志尾部\nTraceback (most recent call last):\n  File \"v2.py\", line 7\nRuntimeError: 快照夹具失败\n\n## 相关源文件片段（仅限运行工作目录）\n### v2.py:7\n     5| x = 1\n     6| y = 2\n     7| -> raise RuntimeError('快照夹具失败')\n\n## troubleshooting 知识库命中（namespaces=troubleshooting, top_k=6）\n[S1 来源: knowledge/projects/fixture/t.md#s0 | evidence_refs: E0]\n合成排障条目 0"'''
_RUN6_RC2_LINES = [json.loads(line) for line in _RUN6_RC2_SNAPSHOT.splitlines()]


def test_run6_rc2_chunked_six_messages_byte_snapshot():
    # 与 task6 出生轮完全同形的渲染输入（紧凑合成版）：固定 rc2 tail、1 片段、
    # 1 KB 命中、固定 cmd、1 合同段；块 0 带机器反馈、块 i 前序逐字定稿。
    titles = ("合成头", "合成注入", "合成控制", "合成判定", "合成产物", "合成媒体")
    boundaries = tuple(f"第 {i} 块只允许写合成章节 {i}。" for i in range(6))
    tail = ('Traceback (most recent call last):\n  File "v2.py", line 7\n'
            "RuntimeError: 快照夹具失败")
    snips = [{"file": "v2.py", "line": 7,
              "snippet": "     5| x = 1\n     6| y = 2\n"
                         "     7| -> raise RuntimeError('快照夹具失败')"}]
    hits = [{"text": "合成排障条目 0", "evidence_refs": ["E0"],
             "kb_relpath": "knowledge/projects/fixture/t.md",
             "section_path": "s0"}]
    cmd = ["/mnt/venvs/simpraxis/bin/python", "/work/t6/v2.py"]
    sections = (("V2 升阶任务合同（快照夹具）", "合同正文快照 ζ"),)
    feedback = "合成机器原因（快照夹具）：上一轮拆块交付失败"
    assert len(_RUN6_RC2_LINES) == 7          # system ×1 + user ×6
    for i in range(6):
        ctx = ChunkPromptContext(
            i, 6, "v2.py", titles, boundaries,
            previous_code="\n".join(f"# 快照前序定稿块 {j}"
                                    for j in range(i)),
            round_feedback=feedback if i == 0 else "")
        msgs = _build_messages(
            gate2_notes=None, traceback_tail=tail, snippets=snips,
            hits=hits, cmd=cmd, extra_sections=sections, chunk_context=ctx)
        assert msgs[0]["content"] == _RUN6_RC2_LINES[0]
        assert msgs[1]["content"] == _RUN6_RC2_LINES[i + 1]
        # T18：snippets/cmd 段在六块消息里逐字在场
        assert "## 失败命令\n" + " ".join(cmd) in msgs[1]["content"]
        assert "### v2.py:7" in msgs[1]["content"]


# ---- M1⑨：R15 严格形态有意假阴性（谓词拒，运行时 main 恰执行一次） ---------

_RUN6_R15_MARKER = r'''
class _CM:
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def __iter__(self): return iter(())
_state = {"n": 0}
class _Callable:
    def __call__(self):
        _state["n"] += 1
        return _CM()
main = _Callable()
'''

_RUN6_R15_FORMS = {
    "F1_with_item": 'if __name__ == "__main__":\n    with main():\n        pass\n',
    "F2_for_iter": 'if __name__ == "__main__":\n    for x in main():\n        pass\n',
    "F3_while_test": 'if __name__ == "__main__":\n    while main():\n        break\n',
    "F4a_try_wrap_guard": ('try:\n'
                           '    if __name__ == "__main__":\n'
                           '        main()\n'
                           'except Exception:\n    pass\n'),
    "F4b_with_wrap_guard": ('with _CM():\n'
                            '    if __name__ == "__main__":\n'
                            '        main()\n'),
    "F5_not_neq": 'if not __name__ != "__main__":\n    main()\n',
    "F6_finally": ('if __name__ == "__main__":\n'
                   '    try:\n        pass\n'
                   '    finally:\n        main()\n'),
}


@pytest.mark.parametrize("name", sorted(_RUN6_R15_FORMS),
                         ids=sorted(_RUN6_R15_FORMS))
def test_run6_r15_strict_forms_reject_but_runtime_executes_once(name):
    # 严格形态合同（v3.1 R15，有意假阴性）：保守拒绝防未来无意放宽；
    # python 直跑语义下 main 确执行——代价至多一轮重生。
    code = _RUN6_R15_MARKER + "\n" + _RUN6_R15_FORMS[name]
    with pytest.raises(ChunkBirthError) as ei:
        _validate_entry_guard(ast.parse(code))
    assert str(ei.value) == _ENTRY_GUARD_REASON
    g = {"__name__": "__main__"}
    exec(compile(code, f"<run6-r15-{name}>", "exec"), g)
    assert g["_state"]["n"] == 1


def test_run6_r15_canonical_control_accept_and_executes_once():
    # 同探针环境对照：规范形必须仍受且 main 恰一次（防探针环境自身误判）。
    code = _RUN6_R15_MARKER + '\nif __name__ == "__main__":\n    main()\n'
    _validate_entry_guard(ast.parse(code))
    g = {"__name__": "__main__"}
    exec(compile(code, "<run6-r15-control>", "exec"), g)
    assert g["_state"]["n"] == 1


# ---- M1⑩：E18/E19 match 两形态运行时双坐实 -------------------------------

def test_run6_e18_match_class_pattern_runtime_typeerror():
    # E18：case main() 是类模式，运行时自身即 TypeError（与谓词拒双坐实）。
    code = _RUN6_EXTERNAL["E18_match_class_pattern"][1]
    with pytest.raises(TypeError, match="called match pattern must be a type"):
        exec(compile(code, "<run6-e18>", "exec"), {"__name__": "__main__"})


def test_run6_e19_match_guard_runtime_executes_main_once():
    # E19：match 守卫位 main() 运行时恰执行一次（计数器内联，无 fixture 依赖）。
    state = {"n": 0}

    class _GuardCallable:
        def __call__(self):
            state["n"] += 1
            return 1

    guard = ('if __name__ == "__main__":\n'
             '    match 1:\n        case 1 if main():\n            pass\n')
    g = {"__name__": "__main__", "main": _GuardCallable()}
    exec(compile(guard, "<run6-e19>", "exec"), g)
    assert state["n"] == 1
    # 同一守卫体即矩阵 E19 源码尾部（与 46 矩阵同源防漂移）。
    assert _RUN6_EXTERNAL["E19_match_guard_call"][1].endswith(guard)


# ===========================================================================
# ⑧ Task6 run7b：非唯一/未命中锚点从「立即结案」改为「机器反馈 + 5 轮帽内
#    续跑」（冻结规格 .omc/handoffs/task6-run7b-anchor-feedback-design.md
#    v1.0.1 §8 A1-A15；2026-10-24）。全 hermetic：FakeGateway/真
#    CallReplay(tmp)/真 tmp 文件系统，0 联网 0 付费、无 paid marker、不读
#    /mnt。本段仅追加，旧测试一字未改；局部导入镜像 :542 段风格。
#    E8（v1.0.1 勘误）：成功返回只在轮顶 telemetry_ok，N 次 coding 收敛
#    ⇒ N+1 次 executions（A1/A2/A14=3、A7=4；全歧义 A4=5 末次 except 直
#    接 _fail；test_01 单补丁=2）。
# [run7b-anchor-feedback-tests]
# ===========================================================================

import os  # noqa: E402 （run7b 段局部导入）
import inspect  # noqa: E402
from orchestrator.agent_runner.runner import (  # noqa: E402
    PatchAnchorError,
    PatchSafetyError,
    _ANCHOR_HINT_MULTI,
    _ANCHOR_HINT_ZERO,
    _ANCHOR_LINE_SEPS,
    _ANCHOR_REJECT_FMT,
    _anchor_note,
    _append_anchor_feedback,
    _apply_structured_patch,
    _apply_unified_diff,
    _coding_cap_reason,
    _commit_staged,
    _neutralize_for_model,
    _parse_and_apply,
    _stage_structured,
    _stage_unified_section,
)
from orchestrator.common.host_shim.shim_core import redact_for_model  # noqa: E402

_RUN7B_FORBIDDEN = (
    "SIMPRAXIS_HOST_SHIM", "simpraxis-host-shim", "host_shim",
    "平台托底", "sitecustomize", "shim_core")
_RUN7B_MARK = "机器核验（非模型输出）：上一轮返回的补丁已被整体拒绝"
_RUN7B_SEPS8 = ("\n", "\r", "\v", "\f", "\x1d", "\x85", " ", " ")
_RUN7B_REASON_MULTI = "old 块命中 2 次（需恰好 1 次）：fix.txt"


def _run7b_patch(path, old, new):
    return {"path": path, "old": old, "new": new}


def _run7b_structured(*patches):
    return json.dumps({"patches": list(patches)}, ensure_ascii=False)


def _run7b_setup(tmp_path, responses, *, script=CONVERGING_SCRIPT,
                 cap=5, thread="t7b", fix="NEEDS_FIX",
                 usage=(None, None), recall=None, files=None, gw=None):
    wd = tmp_path / "work"
    wd.mkdir(parents=True, exist_ok=True)
    (wd / "script.py").write_text(script, encoding="utf-8")
    (wd / "fix.txt").write_text(fix, encoding="utf-8")
    for rel, content in (files or {}).items():
        p = wd / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    if gw is None:
        gw = FakeGateway(responses, usage=usage)
    cache = CallReplay(tmp_path / "cache")
    runner = AgentRunner(gw, FakeVector(), evidence_root=tmp_path / "ev",
                         call_cache=cache, recall=recall)
    spec = RunSpec(
        thread_id=thread, workdir=wd,
        cmd=[sys.executable, str(wd / "script.py")],
        telemetry_relpath="telemetry.json", batch_id="T6-FIXTURE",
        billing_task=6, run_idx=1, max_coding_rounds=cap)
    return runner, spec, gw, wd, cache


def _run7b_decision(tmp_path, thread, n):
    p = tmp_path / "ev" / thread / f"coding_round{n}.decision.json"
    return json.loads(p.read_text(encoding="utf-8"))


def _run7b_anchor_entries(rec):
    return [t for t in rec["timeline"] if "patch_anchor_rejected" in t]


class _Run7bSnapshotGateway(FakeGateway):
    """每次调用瞬间快照指定文件（mtime_ns+bytes）：钉拒绝轮到下一调用间零写。"""

    def __init__(self, responses, wd, rels, **kw):
        super().__init__(responses, **kw)
        self.snaps = []
        self._wd, self._rels = wd, rels

    def call(self, stage, messages, **kw):
        self.snaps.append({
            rel: (os.stat(self._wd / rel).st_mtime_ns,
                  (self._wd / rel).read_bytes())
            for rel in self._rels})
        return super().call(stage, messages, **kw)


# ---- A1：structured 歧义→机器反馈→第二轮唯一锚→ok（executions=3，E8） ------

def test_run7b_a1_structured_ambiguous_feedback_then_unique_ok(tmp_path):
    responses = [
        _run7b_structured(_run7b_patch("fix.txt", "NEEDS_FIX", "PATCHED")),
        _run7b_structured(
            _run7b_patch("fix.txt", "NEEDS_FIX NEEDS_FIX", "PATCHED"))]
    gw = _Run7bSnapshotGateway(responses, tmp_path / "work", ["fix.txt"],
                               usage=(10, 20))
    runner, spec, gw, wd, cache = _run7b_setup(
        tmp_path, responses, fix="NEEDS_FIX NEEDS_FIX",
        thread="t7b-a1", gw=gw)
    rec = runner.run(spec)

    assert rec["status"] == "ok"
    assert rec["coding_round"] == 2 and rec["executions"] == 3
    assert len(gw.calls) == 2
    # round1 decision 恰两键、rejected 原文
    assert _run7b_decision(tmp_path, "t7b-a1", 1) == {
        "applied": [], "rejected": _RUN7B_REASON_MULTI}
    # r1 拒绝后、r2 调用前目标字节/mtime 不变（两次调用瞬间快照相等）
    assert gw.snaps[0] == gw.snaps[1]
    assert gw.snaps[0]["fix.txt"][1] == "NEEDS_FIX NEEDS_FIX".encode()
    assert (wd / "fix.txt").read_bytes() == b"PATCHED"
    # round2 模型消息：标记句 + 多命中 hint 全文，0 六禁词
    msg2 = gw.calls[1]["messages"][1]["content"]
    assert _RUN7B_MARK in msg2 and _ANCHOR_HINT_MULTI in msg2
    assert all(w not in msg2 for w in _RUN7B_FORBIDDEN)
    # timeline r1 条三键
    entries = _run7b_anchor_entries(rec)
    assert len(entries) == 1
    assert entries[0]["coding_round"] == 1
    assert entries[0]["patch_anchor_rejected"] == _RUN7B_REASON_MULTI
    assert entries[0]["patch_anchor_hits"] == 2
    # sidecar 实跑恰 3 次（两脚本夹具每次 exec 追加一个 x）
    assert (wd / "runs.log").read_text() == "xxx"
    # 歧义 raw 在调用成功后即入缓存（先 put 后 apply 既有顺序不变）
    assert len(list((tmp_path / "cache").glob("*.json"))) == 2


# ---- A2：unified hunk 歧义同路径→反馈→r2 唯一→ok（executions=3） ----------

def test_run7b_a2_unified_hunk_ambiguous_feedback_then_unique_ok(tmp_path):
    r1 = ("--- a/fix.txt\n+++ b/fix.txt\n"
          "@@ -1,1 +1,1 @@\n-DUP\n+PATCHED\n")
    r2 = ("--- a/fix.txt\n+++ b/fix.txt\n"
          "@@ -1,2 +1,2 @@\n-DUP\n-DUP\n+PATCHED\n")
    responses = [json.dumps({"unified_diff": r1}, ensure_ascii=False),
                 json.dumps({"unified_diff": r2}, ensure_ascii=False)]
    gw = _Run7bSnapshotGateway(responses, tmp_path / "work", ["fix.txt"])
    runner, spec, gw, wd, _ = _run7b_setup(
        tmp_path, responses, fix="DUP\nDUP\n", thread="t7b-a2", gw=gw)
    rec = runner.run(spec)

    assert rec["status"] == "ok" and len(gw.calls) == 2
    assert rec["coding_round"] == 2 and rec["executions"] == 3
    assert _run7b_decision(tmp_path, "t7b-a2", 1) == {
        "applied": [],
        "rejected": "hunk 上下文命中 2 次（需恰好 1 次）：fix.txt"}
    assert gw.snaps[0] == gw.snaps[1]
    msg2 = gw.calls[1]["messages"][1]["content"]
    assert _RUN7B_MARK in msg2 and "hunk 上下文命中 2 次" in msg2
    entries = _run7b_anchor_entries(rec)
    assert len(entries) == 1 and entries[0]["patch_anchor_hits"] == 2
    assert (wd / "runs.log").read_text() == "xxx"
    assert (wd / "fix.txt").read_bytes() == b"PATCHED"


# ---- A3：0 命中 → ZERO hint 进反馈，续跑收敛 ------------------------------

def test_run7b_a3_zero_hits_feedback_continue(tmp_path):
    runner, spec, gw, wd, _ = _run7b_setup(
        tmp_path,
        [_run7b_structured(_run7b_patch("fix.txt", "NO_SUCH_ANCHOR", "x")),
         _run7b_structured(_run7b_patch("fix.txt", "NEEDS_FIX", "PATCHED"))],
        thread="t7b-a3")
    rec = runner.run(spec)

    assert rec["status"] == "ok" and len(gw.calls) == 2
    assert rec["executions"] == 3
    entries = _run7b_anchor_entries(rec)
    assert len(entries) == 1
    assert entries[0]["patch_anchor_hits"] == 0
    assert entries[0]["patch_anchor_rejected"] == (
        "old 块命中 0 次（需恰好 1 次）：fix.txt")
    msg2 = gw.calls[1]["messages"][1]["content"]
    assert _RUN7B_MARK in msg2 and _ANCHOR_HINT_ZERO in msg2
    assert _ANCHOR_HINT_MULTI not in msg2


# ---- A4：五轮全歧义 → 末次 except 内直接 _fail（executions==5） ------------

def test_run7b_a4_all_ambiguous_cap_direct_fail(tmp_path):
    ambiguous = _run7b_structured(
        _run7b_patch("fix.txt", "NEEDS_FIX", "PATCHED"))
    runner, spec, gw, wd, _ = _run7b_setup(
        tmp_path, [ambiguous] * 5, script=ALWAYS_FAIL_SCRIPT,
        fix="NEEDS_FIX NEEDS_FIX", thread="t7b-a4")
    rec = runner.run(spec)

    assert rec["status"] == "failed"
    assert len(gw.calls) == 5 and rec["executions"] == 5
    assert (wd / "runs.log").read_text() == "xxxxx"
    assert not (wd / "telemetry.json").exists()
    assert rec["coding_round"] == 5
    expected_reason = (
        "5 轮 coding_debug 后仍失败（RunSpec 上限）；末次锚点机器原因："
        + _RUN7B_REASON_MULTI)
    assert rec["reason"] == expected_reason
    assert "（RunSpec 上限）" in rec["reason"]
    assert "补丁安全拒绝（不重试）" not in rec["reason"]
    failure = json.loads((wd / "FAILURE.json").read_text(encoding="utf-8"))
    assert failure["coding_round"] == 5 and failure["executions"] == 5
    assert failure["reason"] == expected_reason
    entries = _run7b_anchor_entries(rec)
    assert [e["coding_round"] for e in entries] == [1, 2, 3, 4, 5]
    assert all(e["patch_anchor_hits"] == 2 for e in entries)
    for n in range(1, 6):
        assert _run7b_decision(tmp_path, "t7b-a4", n)["rejected"] == (
            _RUN7B_REASON_MULTI)


# ---- A5：普通「补丁已应用仍失败」5 轮帽路径不回归（executions==6） ---------

def test_run7b_a5_normal_cap_path_executions_six(tmp_path):
    runner, spec, gw, wd, _ = _run7b_setup(
        tmp_path, _rotating_patches(5), script=ALWAYS_FAIL_SCRIPT,
        fix="x=0", thread="t7b-a5")
    rec = runner.run(spec)

    assert rec["status"] == "failed"
    assert len(gw.calls) == 5 and rec["executions"] == 6
    assert (wd / "fix.txt").read_text() == "x=5"
    assert rec["reason"] == "5 轮 coding_debug 后仍失败（RunSpec 上限）"
    assert _run7b_anchor_entries(rec) == []


# ---- A6：整轮原子性六形（§3.3 ①-⑥；全零落盘/零目录/mtime 不变） ------------

def test_run7b_a6_whole_round_atomicity_six_forms(tmp_path):
    wd = tmp_path / "work"
    wd.mkdir()
    (wd / "fix.txt").write_text("DUP\nDUP\n", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("keep", encoding="utf-8")
    outside_mtime = os.stat(outside).st_mtime_ns

    def _stat(rel):
        return os.stat(wd / rel).st_mtime_ns, (wd / rel).read_bytes()

    # ① [合法出生补丁（会新建目录）, 歧义补丁] ⇒ PatchAnchorError，目录不建
    s_before = _stat("fix.txt")
    with pytest.raises(PatchAnchorError) as ei:
        _parse_and_apply(wd, _run7b_structured(
            _run7b_patch("sub/new.py", "", "birth"),
            _run7b_patch("fix.txt", "DUP", "X")))
    assert ei.value.hits == 2
    assert not (wd / "sub").exists()
    assert _stat("fix.txt") == s_before

    # ② [歧义, 越界]：歧义先抛 PatchAnchorError；越界补丁零落盘
    with pytest.raises(PatchAnchorError) as ei:
        _parse_and_apply(wd, _run7b_structured(
            _run7b_patch("fix.txt", "DUP", "X"),
            _run7b_patch(str(outside), "keep", "HACK")))
    assert ei.value.hits == 2
    assert outside.read_text() == "keep"
    assert os.stat(outside).st_mtime_ns == outside_mtime
    assert _stat("fix.txt") == s_before

    # ③ [越界] 单独：父类 PatchSafetyError（非锚点子类），1 调用结案语义
    with pytest.raises(PatchSafetyError) as ei:
        _parse_and_apply(wd, _run7b_structured(
            _run7b_patch(str(outside), "keep", "HACK")))
    assert not isinstance(ei.value, PatchAnchorError)

    # ④ unified 两 section（diff --git 分隔）第二 hunk 歧义 ⇒ 第一目标不变
    (wd / "a.txt").write_text("AAA\n", encoding="utf-8")
    (wd / "b.txt").write_text("DUP\nDUP\n", encoding="utf-8")
    a_before = _stat("a.txt")
    b_before = _stat("b.txt")
    diff = ("--- a/a.txt\n+++ b/a.txt\n@@ -1,1 +1,1 @@\n-AAA\n+BBB\n"
            "diff --git a/b.txt b/b.txt\n"
            "--- a/b.txt\n+++ b/b.txt\n@@ -1,1 +1,1 @@\n-DUP\n+X\n")
    with pytest.raises(PatchAnchorError) as ei:
        _parse_and_apply(wd, json.dumps({"unified_diff": diff}))
    assert ei.value.hits == 2
    assert _stat("a.txt") == a_before
    assert _stat("b.txt") == b_before

    # ⑤ 结构性坏响应（old/new 非 str、元素非对象）：任何写之前父类硬停
    with pytest.raises(PatchSafetyError) as ei:
        _parse_and_apply(wd, _run7b_structured(
            _run7b_patch("fix.txt", 1, "X")))
    assert not isinstance(ei.value, PatchAnchorError)
    with pytest.raises(PatchSafetyError):
        _parse_and_apply(wd, json.dumps({"patches": [1]}))
    assert _stat("fix.txt") == s_before

    # ⑥ MINOR-2：已存在非空文件 old="" ⇒ hits=len+1 的 PatchAnchorError，
    #    零落盘（旧实现是父类硬停；新码走反馈通道）
    (wd / "c.txt").write_text("abc", encoding="utf-8")
    c_before = _stat("c.txt")
    with pytest.raises(PatchAnchorError) as ei:
        _apply_structured_patch(wd, _run7b_patch("c.txt", "", "X"))
    assert ei.value.hits == 4  # "abc".count("") == len+1
    assert _stat("c.txt") == c_before

    # ⑥续（端到端）：该代差进入反馈通道而非结案——r1 old="" 被拒、r2 收敛
    life = tmp_path / "life"
    runner, spec, gw, lwd, _ = _run7b_setup(
        life,
        [_run7b_structured(_run7b_patch("fix.txt", "", "X")),
         _run7b_structured(_run7b_patch("fix.txt", "abc", "PATCHED"))],
        fix="abc", thread="t7b-a6")
    rec = runner.run(spec)
    assert rec["status"] == "ok" and len(gw.calls) == 2
    assert _run7b_anchor_entries(rec)[0]["patch_anchor_hits"] == 4


# ---- A7：成功应用即清零反馈，后继轮消息不带陈旧标记（executions=4） -------

_RUN7B_STAGE_SCRIPT = '''
from pathlib import Path
wd = Path(__file__).parent
log = wd / "runs.log"
log.write_text((log.read_text() + "x") if log.exists() else "x")
fix = (wd / "fix.txt").read_text().strip()
if fix != "PATCHED":
    raise RuntimeError("fixture failure: stage=" + fix)
(wd / "telemetry.json").write_text('{"_fixture": true}')
'''


def test_run7b_a7_feedback_cleared_after_success(tmp_path):
    runner, spec, gw, wd, _ = _run7b_setup(
        tmp_path,
        [_run7b_structured(_run7b_patch("fix.txt", "NEEDS_FIX", "STAGE2")),
         _run7b_structured(
             _run7b_patch("fix.txt", "NEEDS_FIX NEEDS_FIX", "STAGE2")),
         _run7b_structured(_run7b_patch("fix.txt", "STAGE2", "PATCHED"))],
        script=_RUN7B_STAGE_SCRIPT, fix="NEEDS_FIX NEEDS_FIX",
        thread="t7b-a7")
    rec = runner.run(spec)

    assert rec["status"] == "ok"
    assert len(gw.calls) == 3 and rec["executions"] == 4
    m1 = gw.calls[0]["messages"][1]["content"]
    m2 = gw.calls[1]["messages"][1]["content"]
    m3 = gw.calls[2]["messages"][1]["content"]
    assert _RUN7B_MARK not in m1                      # 首轮无反馈
    assert _RUN7B_MARK in m2 and _ANCHOR_HINT_MULTI in m2
    assert _RUN7B_MARK not in m3                      # r2 成功后已清零
    assert "机器判定原因" not in m3
    assert (wd / "runs.log").read_text() == "xxxx"
    assert _run7b_anchor_entries(rec)[0]["coding_round"] == 1


# ---- A8：常量/helper 单测（戒律 + 54 形矩阵 + 10 分隔符 + RAW + 幂等） -----

def test_run7b_a8a_constants_discipline():
    rendered = _ANCHOR_REJECT_FMT.format(reason="r", hint="h")
    assert len(rendered.splitlines()) == 8           # 显式 8 物理行
    assert '"' not in rendered                       # MINOR-9：常量无 ASCII 双引号
    assert "{reason}" not in rendered and "{hint}" not in rendered
    for blob in (rendered, _ANCHOR_HINT_MULTI, _ANCHOR_HINT_ZERO):
        for w in _RUN7B_FORBIDDEN:
            assert w not in blob
    assert _ANCHOR_HINT_MULTI != _ANCHOR_HINT_ZERO


_RUN7B_MATRIX = []
for _w in _RUN7B_FORBIDDEN:
    for _s in _RUN7B_SEPS8:
        _RUN7B_MATRIX.append((_w, f"x{_s}{_w}.py{_s}z"))
    _RUN7B_MATRIX.append((_w, f'rel 是 "x/{_w}.py"'))
_RUN7B_MATRIX_IDS = [f"case{i:02d}" for i in range(len(_RUN7B_MATRIX))]


@pytest.mark.parametrize("word,reason", _RUN7B_MATRIX,
                         ids=_RUN7B_MATRIX_IDS)
def test_run7b_a8b_redaction_matrix_54(word, reason):
    assert len(_RUN7B_MATRIX) == 54                  # 6×8 + 6 = 54
    exc = PatchAnchorError(
        f"old 块命中 2 次（需恰好 1 次）：{reason}", hits=2)
    note = _anchor_note(exc)
    out = redact_for_model(note)
    lines = note.splitlines()
    assert len(lines) == 8
    # 除原因行（index 2）外 7 行编译期常量逐字存活
    survivors = [ln for ln in lines if word not in ln]
    assert len(survivors) == 7
    for ln in survivors:
        assert ln in out
    assert _RUN7B_MARK in out
    # 模型面 0 六禁词（任一形）
    for w in _RUN7B_FORBIDDEN:
        assert w not in out


def test_run7b_a8c_neutralize_all_ten_separators():
    seps10 = ("\n", "\r", "\v", "\f",
              "\x1c", "\x1d", "\x1e", "\x85", " ", " ")
    assert len(_ANCHOR_LINE_SEPS) == 10
    assert _ANCHOR_LINE_SEPS == seps10
    for c in seps10:
        assert _neutralize_for_model("x" + c + "y") == "x y"
        assert _neutralize_for_model("a" + c + c + "b") == "a b"
    plain = "机器判定原因：old 块命中 2 次（需恰好 1 次）：fix.txt"
    assert _neutralize_for_model(plain) == plain


def test_run7b_a8d_raw_triad_never_neutralized():
    raw = "old 块命中 2 次（需恰好 1 次）：x\x85sitecustomize.py"
    exc = PatchAnchorError(raw, hits=2)
    assert str(exc) == raw                           # 人侧 RAW
    assert "\x85" in str(exc) and "sitecustomize" in str(exc)
    note = _anchor_note(exc)
    assert "\x85" not in note                        # 模型面经中和
    assert "x sitecustomize.py" in note


def test_run7b_a8e_append_idempotent():
    note = _anchor_note(PatchAnchorError(_RUN7B_REASON_MULTI, hits=2))
    once = _append_anchor_feedback("tail", note)
    twice = _append_anchor_feedback(once, note)
    assert once == "tail\n\n" + note and twice == once
    assert once.count(_RUN7B_MARK) == 1
    empty = _append_anchor_feedback("", note)
    assert empty == "\n\n" + note and empty.count(note) == 1


def test_run7b_a8f_hint_dispatch():
    n0 = _anchor_note(PatchAnchorError("zero", hits=0))
    n2 = _anchor_note(PatchAnchorError("multi", hits=2))
    assert _ANCHOR_HINT_ZERO in n0 and _ANCHOR_HINT_MULTI not in n0
    assert _ANCHOR_HINT_MULTI in n2 and _ANCHOR_HINT_ZERO not in n2


# ---- A9：消息快照（rc0 note 在前、anchor note 在后；非注入轮不含） --------

def test_run7b_a9_anchor_note_message_snapshot_order(tmp_path):
    tb = ("Traceback (most recent call last):\n"
          '  File "script.py", line 6\n'
          "RuntimeError: fixture failure: waiting for patch")
    tail_rc0 = _append_rc0_missing_artifact_note(tb, "telemetry.json")
    note = _anchor_note(PatchAnchorError(_RUN7B_REASON_MULTI, hits=2))
    tail_both = _append_anchor_feedback(tail_rc0, note)
    # rc0 与 anchor 可共存、次序固定 rc0 在前 anchor 在最后
    assert tail_both == tail_rc0 + "\n\n" + note
    msgs = _build_messages(
        gate2_notes=None, traceback_tail=redact_for_model(tail_both),
        snippets=[], hits=[],
        cmd=[sys.executable, str(tmp_path / "work" / "script.py")])
    content = msgs[1]["content"]
    rc0_note = _RC0_MISSING_ARTIFACT_FMT.format(relpath="telemetry.json")
    p_tb = content.index("## traceback / 日志尾部")
    p_rc0 = content.index(rc0_note)
    p_anchor = content.index(note)
    assert p_tb < p_rc0 < p_anchor
    assert content.count(note) == 1 and content.count(rc0_note) == 1
    # 非注入轮：两种机器注记都不在
    plain = _build_messages(
        gate2_notes=None, traceback_tail=tb, snippets=[], hits=[],
        cmd=[sys.executable, str(tmp_path / "work" / "script.py")])
    plain_text = plain[1]["content"]
    assert _RUN7B_MARK not in plain_text
    assert "机器核验（非模型输出）" not in plain_text


# ---- A10：脱敏分界端到端（rel 呈 sitecustomize.py 奇异形；RAW vs 模型面） --

@pytest.mark.parametrize("sep", ["\x85", " "], ids=["nel", "u2028"])
def test_run7b_a10_redaction_boundary_end_to_end(tmp_path, sep):
    weird = f"x{sep}sitecustomize.py"
    ambiguous = _run7b_structured(_run7b_patch(weird, "DUP", "X"))
    recall = _Run6ScriptedRecall()
    runner, spec, gw, wd, _ = _run7b_setup(
        tmp_path, [ambiguous] * 5, script=ALWAYS_FAIL_SCRIPT,
        fix="NEEDS_FIX", thread="t7b-a10", files={weird: "DUP\nDUP\n"},
        recall=recall)
    rec = runner.run(spec)

    assert rec["status"] == "failed" and len(gw.calls) == 5
    raw_reason = f"old 块命中 2 次（需恰好 1 次）：{weird}"
    # 模型面：round2 起消息经真实 redact——标记/指令句存活、0 六禁词
    msg2 = gw.calls[1]["messages"][1]["content"]
    assert _RUN7B_MARK in msg2
    assert "必须是当前文件中恰好出现一次的连续原文" in msg2
    for w in _RUN7B_FORBIDDEN:
        assert w not in msg2
    assert sep not in msg2
    # 人侧 RAW 三元：decision / timeline / FAILURE 保原 rel 原分隔符
    assert _run7b_decision(tmp_path, "t7b-a10", 1) == {
        "applied": [], "rejected": raw_reason}
    entries = _run7b_anchor_entries(rec)
    assert entries[0]["patch_anchor_rejected"] == raw_reason
    failure = json.loads((wd / "FAILURE.json").read_text(encoding="utf-8"))
    assert weird in failure["reason"] and sep in failure["reason"]
    assert "sitecustomize" in failure["reason"]
    # recall 查询串/snippets 采集在注入之前，不被污染
    assert recall.queries and all(
        _RUN7B_MARK not in q and "sitecustomize" not in q
        for q in recall.queries)
    snips = _collect_snippets(wd, rec["timeline"][-1]["stderr_tail"])
    assert all("sitecustomize" not in s["snippet"] for s in snips)


# ---- A11：成功路径逐字节等价（reference 为父提交 1323143 旧算法零共享照抄） -

class _Run7bRefPatchError(RuntimeError):
    """A11 reference 本地异常（MINOR-4：禁止 import 生产私有件）。"""


_RUN7B_REF_FORBIDDEN = frozenset({"stage_models.yaml", "00_env.sh"})
_RUN7B_REF_HUNK_RE = re.compile(
    r"^@@ -(?P<o>\d+)(?:,\d+)? \+(?P<n>\d+)(?:,\d+)? @@", re.M)


def _run7b_ref_chmod(target: Path) -> None:
    # 父提交 chmod_keep_user_exec 语义内联：0o600 | (old & 0o700)
    os.chmod(target, 0o600 | (target.stat().st_mode & 0o700))


def _run7b_ref_validate_target(path: Path, workdir: Path) -> Path:
    name = path.name
    if name in _RUN7B_REF_FORBIDDEN:
        raise _Run7bRefPatchError(f"补丁企图修改禁改文件：{name}")
    if "access" in path.parts:
        raise _Run7bRefPatchError(f"补丁企图进入 ~/access 区域：{path}")
    workdir_r = workdir.resolve()
    target = path.resolve(strict=False) if path.is_absolute() \
        else (workdir_r / path).resolve(strict=False)
    try:
        target.relative_to(workdir_r)
    except ValueError:
        raise _Run7bRefPatchError(
            f"补丁目标越出 workdir：{path} -> {target}（workdir={workdir_r}）")
    cur = workdir_r
    for part in target.relative_to(workdir_r).parts:
        cur = cur / part
        if cur.is_symlink():
            real = cur.resolve(strict=False)
            try:
                real.relative_to(workdir_r)
            except ValueError:
                raise _Run7bRefPatchError(
                    f"补丁经符号链接逃逸 workdir：{cur} -> {real}")
    return target


def _run7b_ref_apply_unified_diff(workdir: Path, diff_text: str) -> list[str]:
    """照抄父提交 1323143 runner.py 旧 _apply_unified_diff（立即写盘版）。"""
    files = re.split(r"^diff --git .+$", diff_text, flags=re.M)
    applied = []
    for section in files:
        if not section.strip():
            continue
        m_new = re.search(r"^\+\+\+ (?:b/)?(.+)$", section, re.M)
        if not m_new:
            raise _Run7bRefPatchError("unified diff 缺 +++ 头")
        rel = m_new.group(1).strip()
        target = _run7b_ref_validate_target(Path(rel), workdir)
        body_start = section.find("\n@@")
        body = section[body_start + 1:] if body_start >= 0 else ""
        old_lines = target.read_text(encoding="utf-8").splitlines(keepends=True) \
            if target.exists() else []
        result = []
        cursor = 0
        hunks = list(_RUN7B_REF_HUNK_RE.finditer(body))
        if not hunks:
            raise _Run7bRefPatchError("unified diff 无 hunk")
        for hi, hm in enumerate(hunks):
            hunk_end = hunks[hi + 1].start() if hi + 1 < len(hunks) else len(body)
            hunk_lines = body[hm.start():hunk_end].splitlines()
            old_block, new_block = [], []
            for hl in hunk_lines[1:]:
                if hl.startswith("\\"):
                    continue
                if hl.startswith("-"):
                    old_block.append(hl[1:])
                elif hl.startswith("+"):
                    new_block.append(hl[1:])
                else:
                    ctx = hl[1:] if hl[:1] in (" ", "") else hl
                    old_block.append(ctx)
                    new_block.append(ctx)
            window = [l.rstrip("\n") for l in old_lines[cursor:]]
            old_s = [l.rstrip("\n") for l in old_block]
            hits = [i for i in range(len(window) - len(old_s) + 1)
                    if window[i:i + len(old_s)] == old_s]
            if len(hits) != 1:
                raise _Run7bRefPatchError(
                    f"hunk 上下文命中 {len(hits)} 次（需恰好 1 次）：{rel}")
            at = cursor + hits[0]
            result.extend(old_lines[cursor:at])
            result.extend("\n".join(new_block).splitlines(keepends=True))
            cursor = at + len(old_s)
        result.extend(old_lines[cursor:])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("".join(result), encoding="utf-8")
        _run7b_ref_chmod(target)
        applied.append(str(target.relative_to(workdir.resolve())))
    return applied


def _run7b_ref_apply_structured_patch(workdir: Path, patch: dict) -> str:
    """照抄父提交 1323143 runner.py 旧 _apply_structured_patch（立即写盘版）。"""
    rel = patch.get("path")
    if not isinstance(rel, str) or not rel:
        raise _Run7bRefPatchError("补丁缺 path 字段")
    target = _run7b_ref_validate_target(Path(rel), workdir)
    old, new = patch.get("old"), patch.get("new")
    if not isinstance(old, str) or not isinstance(new, str):
        raise _Run7bRefPatchError("补丁 old/new 必须为字符串")
    text = target.read_text(encoding="utf-8") if target.exists() else ""
    if text.count(old) != 1:
        raise _Run7bRefPatchError(
            f"old 块命中 {text.count(old)} 次（需恰好 1 次）：{rel}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text.replace(old, new, 1), encoding="utf-8")
    _run7b_ref_chmod(target)
    return str(target.relative_to(workdir.resolve()))


def _run7b_snapshot_tree(wd: Path) -> dict:
    out = {}
    for p in sorted(wd.rglob("*")):
        rel = str(p.relative_to(wd))
        if p.is_symlink():
            out[rel] = ("symlink", os.readlink(p))
        elif p.is_dir():
            out[rel] = ("dir", p.stat().st_mode & 0o777)
        else:
            out[rel] = ("file", p.read_bytes(), p.stat().st_mode & 0o777)
    return out


def _run7b_seed(wd: Path, scenario: str):
    if scenario == "same_file_two":
        (wd / "f.txt").write_text("aXbYc", encoding="utf-8")
    elif scenario == "two_section":
        # 本仓最小 unified applier 约定：hunk 覆到 EOF 时文件尾换行随最后
        # 一条 new_block 落盘（无 tail 行携带）；两 section 同指此文件，
        # 终态以参考实现逐字节对照（不手写理想 git 语义）。
        (wd / "f.txt").write_text("l1\nl2\nl3\n", encoding="utf-8")
    elif scenario == "crlf":
        # read_text 通用换行把 CRLF 翻成 LF（两侧算法同形，同归一即等价）。
        (wd / "f.txt").write_bytes(b"a\r\nb\r\n")
    elif scenario in ("mode700", "mode600"):
        p = wd / "script_x.py"
        p.write_text("print(1)\n", encoding="utf-8")
        os.chmod(p, 0o700 if scenario == "mode700" else 0o600)
    elif scenario == "symlink":
        (wd / "real.py").write_text("abc\n", encoding="utf-8")
        os.symlink("real.py", wd / "lnk.py")
    # birth / multi_file / nested_dirs 无预置文件


_RUN7B_TWO_SECTION_DIFF = (
    "--- a/f.txt\n+++ b/f.txt\n@@ -1,3 +1,3 @@\n-l1\n+L1\n l2\n l3\n"
    "diff --git a/f.txt b/f.txt\n"
    "--- a/f.txt\n+++ b/f.txt\n@@ -2,2 +2,2 @@\n l2\n-l3\n+L3\n")


def _run7b_scenario(scenario: str):
    if scenario == "birth_old_empty":
        return ("structured_one",
                _run7b_patch("new.py", "", "born\n"))
    if scenario == "same_file_two":
        return ("structured_many", [
            _run7b_patch("f.txt", "X", "Q"),      # 第二锚点落第一补丁结果
            _run7b_patch("f.txt", "aQb", "ZZZ")])
    if scenario == "two_section":
        return ("unified", _RUN7B_TWO_SECTION_DIFF)
    if scenario == "multi_file":
        return ("structured_many", [
            _run7b_patch("a.txt", "", "A\n"),
            _run7b_patch("b.txt", "", "B\n")])
    if scenario == "crlf":
        # 磁盘 CRLF 经 read_text 为 LF；锚点用读入形，两侧同归一即等价。
        return ("structured_one",
                _run7b_patch("f.txt", "a\nb", "x\ny"))
    if scenario in ("mode700", "mode600"):
        return ("structured_one",
                _run7b_patch("script_x.py", "print(1)", "print(2)"))
    if scenario == "nested_dirs":
        return ("structured_one",
                _run7b_patch("d1/d2/d3/new.py", "", "deep\n"))
    if scenario == "symlink":
        return ("structured_one",
                _run7b_patch("lnk.py", "abc", "XYZ"))
    raise KeyError(scenario)


def _run7b_run_side(wd: Path, side: str, kind: str, payload):
    if side == "prod":
        if kind == "structured_one":
            return [_apply_structured_patch(wd, payload)]
        if kind == "structured_many":
            return _parse_and_apply(wd, _run7b_structured(*payload))
        return _apply_unified_diff(wd, payload)
    if kind == "structured_one":
        return [_run7b_ref_apply_structured_patch(wd, payload)]
    if kind == "structured_many":
        return [_run7b_ref_apply_structured_patch(wd, p) for p in payload]
    return _run7b_ref_apply_unified_diff(wd, payload)


_RUN7B_SCENARIOS = [
    "birth_old_empty", "same_file_two", "two_section", "multi_file",
    "crlf", "mode700", "mode600", "nested_dirs", "symlink"]


@pytest.mark.parametrize("scenario", _RUN7B_SCENARIOS, ids=_RUN7B_SCENARIOS)
def test_run7b_a11_success_path_byte_equivalent(tmp_path, scenario):
    kind, payload = _run7b_scenario(scenario)
    wd_ref = tmp_path / "ref"
    wd_new = tmp_path / "new"
    wd_ref.mkdir()
    wd_new.mkdir()
    _run7b_seed(wd_ref, scenario)
    _run7b_seed(wd_new, scenario)
    # MINOR-3：钉新树自身补丁前后 inode 不变（两树互为独立 tmp，禁跨树比 inode）
    ino_before = (wd_new / "real.py").stat().st_ino if scenario == "symlink" \
        else None

    applied_ref = _run7b_run_side(wd_ref, "ref", kind, payload)
    applied_new = _run7b_run_side(wd_new, "prod", kind, payload)

    assert applied_new == applied_ref
    if scenario == "two_section":
        # MINOR-1：一个 diff 两 section 同指一文件 ⇒ [rel, rel] 不塌缩；
        # 第二 section 的 cursor 跑在第一 section 投影行上（L1 已在投影里）。
        assert applied_new == ["f.txt", "f.txt"]
        assert (wd_new / "f.txt").read_text() == "L1\nl2\nL3"
    if scenario == "same_file_two":
        assert applied_new == ["f.txt", "f.txt"]
        assert (wd_new / "f.txt").read_text() == "ZZZYc"
    if scenario == "crlf":
        # CRLF 两侧同归一成 LF 字节。
        assert (wd_new / "f.txt").read_bytes() == b"x\ny\n"
    assert _run7b_snapshot_tree(wd_new) == _run7b_snapshot_tree(wd_ref)
    if scenario == "symlink":
        # MINOR-3：write_text 跟随链接、不换 inode
        assert (wd_new / "real.py").stat().st_ino == ino_before
        assert (wd_new / "real.py").read_text() == "XYZ\n"


def test_run7b_a11_staging_functions_grep_clean():
    # §3.3：投影函数体禁出现任何落盘原语；commit 段恰三件 rename-free 原语
    for fn in (_stage_structured, _stage_unified_section):
        src = inspect.getsource(fn)
        for bad in ("mkdir", "write_text", "chmod", "os.replace",
                    "atomic_write", "os.rename"):
            assert bad not in src, (fn.__name__, bad)
    csrc = inspect.getsource(_commit_staged)
    assert "mkdir(parents=True, exist_ok=True)" in csrc
    assert 'write_text(' in csrc and 'encoding="utf-8"' in csrc
    assert "chmod_keep_user_exec(" in csrc
    for bad in ("os.replace", "atomic_write", "os.rename"):
        assert bad not in csrc


# ---- A12：kill-9 重放两形（真 CallReplay(tmp)；FS 回滚/FS 已修补） ---------

def test_run7b_a12a_replay_fs_rolled_back_zero_new_rows(tmp_path):
    P = _run7b_structured(_run7b_patch("fix.txt", "NEEDS_FIX", "PATCHED"))
    runner, spec, gw, wd, cache = _run7b_setup(
        tmp_path, [P], thread="t7b-a12", usage=(10, 20))
    rec = runner.run(spec)
    assert rec["status"] == "ok" and len(gw.calls) == 1
    rows1 = list((tmp_path / "cache").glob("*.json"))
    assert len(rows1) == 1

    # kill-9 模拟：节点重放时 FS 与 telemetry 已回滚（FAILURE 从不存在）
    (wd / "telemetry.json").unlink()
    (wd / "fix.txt").write_text("NEEDS_FIX", encoding="utf-8")
    gw2 = FakeGateway([], usage=(10, 20))
    runner2 = AgentRunner(gw2, FakeVector(), evidence_root=tmp_path / "ev",
                          call_cache=CallReplay(tmp_path / "cache"))
    rec2 = runner2.run(spec)
    assert rec2["status"] == "ok"
    assert len(gw2.calls) == 0                      # 命中重放，0 新账行
    assert (wd / "fix.txt").read_text() == "PATCHED"
    assert (wd / "telemetry.json").is_file()
    assert list((tmp_path / "cache").glob("*.json")) == rows1


def test_run7b_a12b_replay_fs_already_patched_zero_hits_feedback(tmp_path):
    P1 = _run7b_structured(_run7b_patch("fix.txt", "x=0", "x=1"))
    runner, spec, gw, wd, _ = _run7b_setup(
        tmp_path, [P1], script=ALWAYS_FAIL_SCRIPT, cap=1, fix="x=0",
        thread="t7b-a12b", usage=(10, 20))
    rec = runner.run(spec)
    assert rec["status"] == "failed" and rec["executions"] == 2
    assert (wd / "fix.txt").read_text() == "x=1"

    # kill-9 模拟：FS 已修补（留 x=1），仅撤 FAILURE；脚本确定性仍真失败
    (wd / "FAILURE.json").unlink()
    P2 = _run7b_structured(_run7b_patch("fix.txt", "x=1", "x=2"))
    gw2 = FakeGateway([P2], usage=(10, 20))
    runner2 = AgentRunner(gw2, FakeVector(),
                          evidence_root=tmp_path / "ev2",
                          call_cache=CallReplay(tmp_path / "cache"))
    spec2 = RunSpec(
        thread_id="t7b-a12b", workdir=wd,
        cmd=[sys.executable, str(wd / "script.py")],
        telemetry_relpath="telemetry.json", batch_id="T6-FIXTURE",
        billing_task=6, run_idx=1, max_coding_rounds=2)
    rec2 = runner2.run(spec2)

    assert rec2["status"] == "failed"
    assert len(gw2.calls) == 1                      # round1 命中缓存 0 账行
    assert rec2["executions"] == 3
    entries = _run7b_anchor_entries(rec2)
    assert len(entries) == 1
    assert entries[0]["coding_round"] == 1
    assert entries[0]["patch_anchor_hits"] == 0
    assert entries[0]["patch_anchor_rejected"] == (
        "old 块命中 0 次（需恰好 1 次）：fix.txt")
    # 真实新付费轮（指纹因反馈必变而 miss）消息含标记+ZERO hint
    msg = gw2.calls[0]["messages"][1]["content"]
    assert _RUN7B_MARK in msg and _ANCHOR_HINT_ZERO in msg
    # 普通帽尽（非末次锚点帽），无锚点后缀
    assert rec2["reason"] == "2 轮 coding_debug 后仍失败（RunSpec 上限）"
    assert (wd / "fix.txt").read_text() == "x=2"
    assert len(list((tmp_path / "cache").glob("*.json"))) == 2


# ---- A13：六形 transport 反馈续跑（v1.4 必改清单④反转） --------------------

_RUN7B_HARD_CASES = {
    "empty_patches": json.dumps({"patches": []}),
    "json_scalar": "42",
    "json_array": '["a"]',
    "old_new_nonstr": _run7b_structured(_run7b_patch("fix.txt", 1, "x")),
    "unified_no_plus": json.dumps(
        {"unified_diff": "--- a/f.txt\n@@ -1,1 +1,1 @@\n-a\n+b\n"},
        ensure_ascii=False),
    "unified_no_hunk": json.dumps(
        {"unified_diff": "--- a/f.txt\n+++ b/f.txt\n"},
        ensure_ascii=False),
}


@pytest.mark.parametrize("name", sorted(_RUN7B_HARD_CASES),
                         ids=sorted(_RUN7B_HARD_CASES))
def test_run7b_a13_transport_forms_feedback_continue(tmp_path, name):
    """A13 反转（v1.4 必改清单④；设计 §3 行为代差登记节 J5/J6/J7/J10 等）：
    六类 JSON 通道坏响应由 instant-death 收窄为帽内 transport 反馈续跑——
    坏形→好形两件套，第 2 轮好补丁收敛 ok；「恰 1 调用硬停」语义仅保留
    P1 路径越界（test_02）。"""
    good = _run7b_structured(_run7b_patch("fix.txt", "NEEDS_FIX", "PATCHED"))
    runner, spec, gw, wd, _ = _run7b_setup(
        tmp_path, [_RUN7B_HARD_CASES[name], good],
        thread="t7b-a13-" + name)
    rec = runner.run(spec)

    assert rec["status"] == "ok"
    assert len(gw.calls) == 2 and rec["executions"] == 3
    assert rec["coding_round"] == 2
    assert (wd / "fix.txt").read_text(encoding="utf-8") == "PATCHED"
    # round1 decision：transport True、applied 空（六形全走 JSON 通道 transport）
    doc1 = _run7b_decision(tmp_path, "t7b-a13-" + name, 1)
    assert doc1["applied"] == [] and isinstance(doc1["rejected"], str)
    assert doc1["transport"] is True
    # timeline r1：transport 拒绝键 + coding_round 同位落键
    tr = [t for t in rec["timeline"] if "patch_transport_rejected" in t]
    assert len(tr) == 1 and tr[0]["coding_round"] == 1
    # 锚点通道零介入（六形均非 F2/F3/F4）
    assert _run7b_anchor_entries(rec) == []


# ---- A14：run7 真实响应缩形回归（两补丁同指 V2，patch0 old 命中 2 次） -----

def test_run7b_a14_run7_real_response_synthetic_shrink(tmp_path):
    v2_name = "genesis_humanoid_dr_v2.py"
    v2 = ("# synthetic shrink of run7 V2（合成夹具，禁读 /mnt）\n"
          "def step_a():\n"
          "    target.update(self._side_step(a=1))\n"
          "    target.update(self._side_step(b=2))\n")
    r1 = _run7b_structured(
        _run7b_patch(v2_name, "target.update(self._side_step(",
                     "target.update(self._side_step_v2("),
        _run7b_patch(v2_name, "def control_due(subscript_index):",
                     "def control_due(subscript_index, *, extra=0):"))
    r2 = _run7b_structured(_run7b_patch("fix.txt", "NEEDS_FIX", "PATCHED"))
    runner, spec, gw, wd, _ = _run7b_setup(
        tmp_path, [r1, r2], thread="t7b-a14", files={v2_name: v2})
    rec = runner.run(spec)

    assert rec["status"] == "ok" and len(gw.calls) == 2
    assert rec["coding_round"] == 2 and rec["executions"] == 3
    # run7 现场 105B decision 的 rejected 串逐字
    assert _run7b_decision(tmp_path, "t7b-a14", 1) == {
        "applied": [],
        "rejected": f"old 块命中 2 次（需恰好 1 次）：{v2_name}"}
    assert (wd / v2_name).read_text(encoding="utf-8") == v2
    assert _run7b_anchor_entries(rec)[0]["patch_anchor_hits"] == 2


# ---- A15：chunked 出生互操作（old="" 经 staging 出生，AST 闸在） ------------

def test_run7b_a15_chunked_birth_through_staging(tmp_path):
    codes = _happy_chunk_codes()
    runner, spec, gw, wd, _ = _chunked_runner(
        tmp_path, [_chunk_fence(c, i) for i, c in enumerate(codes)],
        thread="t7b-a15")
    rec = runner.run(spec)

    assert rec["status"] == "ok"
    assert rec["coding_round"] == 1 and rec["executions"] == 2
    assert len(gw.calls) == 6
    assert (wd / _CHUNK_TARGET).read_text(encoding="utf-8") == "\n".join(codes)
    _validate_assembled_chunks("\n".join(codes))      # AST 入口守卫在


# ===========================================================================
# ⑨ Task6 run7b 提交 B：rc0 批次结构闸（冻结规格
#    .omc/handoffs/task6-schema-gate-design.md v1.0，2026-10-24，sha256
#    a9c1394cc1b3…7c7；简报 /tmp/commit_b_executor_brief.md §3a）。
#    全 hermetic：tmp 合成批（常量一律 import gait_rungs，禁止硬编码）、
#    真实 subprocess 脚本实跑写出批次、FakeGateway/真 CallReplay(tmp)，
#    0 联网 0 付费、不读 /mnt。本段仅追加，旧测试一字未改。
# [run7b-schema-gate-tests]
# ===========================================================================

import csv as _csv_sg  # noqa: E402 （提交 B 段局部导入）
import subprocess as _sp_sg  # noqa: E402
import dataclasses as _dc_sg  # noqa: E402

from orchestrator.judge import gait_rungs as _gr  # noqa: E402
from orchestrator.agent_runner.runner import (  # noqa: E402
    MODEL_SCHEMA_FACE_CAP,
    _append_batch_schema_note,
    _build_schema_face,
    _dedup_schema_face,
    _sanitize_schema_face,
    _schema_face_normalize,
    _scrub_workdir,
)

_SG_HEAD = "机器核验（非模型输出）：上一轮脚本 rc=0，但批次结构闸未通过"
_SG_PRODUCT_LINE_PREFIX = "批次产物无法读取（机器核验）："

# 合成批 subprocess 脚本：不 import gait_rungs，口径常量从测试侧写入的
# _sg_const.json 读取（值本身 import 自 gait_rungs）。首行 # sg-tag0 供 G3
# 逐轮锚点补丁推进；shape=_bare 时只写 telemetry.json 标记件后退出
# （G5a/G5b 用预铺产物）。
_SG_BATCH_SCRIPT = r'''# sg-tag0
import csv, json
from pathlib import Path
wd = Path(__file__).parent
const = json.loads((wd / "_sg_const.json").read_text(encoding="utf-8"))
(wd / "telemetry.json").write_text('{"ok": true}', encoding="utf-8")
shape = (wd / "shape.txt").read_text(encoding="utf-8").strip()
if (wd / "echo.txt").exists():
    print((wd / "echo.txt").read_text(encoding="utf-8"))
if shape == "_bare":
    raise SystemExit(0)
(wd / "summary.json").write_text(json.dumps({"seeds": const["seeds"]}),
                                 encoding="utf-8")
td = wd / "telemetry"
td.mkdir(exist_ok=True)
flat = shape not in ("nested", "rungbad")
for arm in const["arms"]:
    for s in range(const["seeds"]):
        feet = 0.0 if flat else {"R": 0.0, "L": 0.0}
        cf = 0 if flat else {"R": 0, "L": 0}
        data = {
            "schema": const["schema"], "engine": const["engine"],
            "engine_version": const["engine_version"],
            "sim": {"use_deterministic_algorithms": True,
                    "gs_enable_zerocopy": False,
                    "telem_hz": const["telem_hz"],
                    "dur_s": const["dur_s"]},
            "series": {k: [0.0] * const["frames"]
                       for k in const["series_keys"]},
            "params": {"arm": arm},
            "outcomes": {"fell": False, "fall_frame": None,
                         "torso_z_min": 1.0, "tilt_max_deg": 0.0,
                         "slip_dist_m": feet, "slip_v_max_mps": feet,
                         "slip_v_max_postpush_mps": feet,
                         "contact_force_peak_N": feet,
                         "contact_frames": cf}}
        if shape == "rungbad":
            if arm == "normal" and s == 0:
                data["outcomes"]["fell"] = True
            if arm == "ice_nominal" and s == 1:
                data["series"][const["series_keys"][0]][0] = float("inf")
            if arm == "ice_dr" and s == 2:
                data["series"][const["series_keys"][0]][1] = float("nan")
        (td / (arm + "_s" + str(s) + ".json")).write_text(
            json.dumps(data), encoding="utf-8")
with open(wd / "dr_controls_vs_experimental.csv", "w", newline="",
          encoding="utf-8") as fh:
    w = csv.DictWriter(fh, fieldnames=const["csv_fields"])
    w.writeheader()
    for arm in const["arms"]:
        for s in range(const["seeds"]):
            row = {c: "0" for c in const["csv_fields"]}
            row["run_id"] = arm + "_s" + str(s)
            w.writerow(row)
'''

_SG_FIXED_FAIL_SCRIPT = (
    'import sys\n'
    'sys.stderr.write("sg fixed failure line\\n")\n'
    'raise SystemExit(1)\n')


def _sg_const_doc():
    # 单一口径：测试侧与子进程脚本共用，值全部 import 自 gait_rungs。
    return {"seeds": _gr.MIN_SEEDS, "arms": list(_gr.ARMS),
            "series_keys": list(_gr.SERIES_KEYS),
            "csv_fields": list(_gr.CSV_REQUIRED_FIELDS),
            "telem_hz": _gr.TELEM_HZ, "dur_s": _gr.DUR_S,
            "frames": _gr.EXPECTED_FRAMES, "schema": _gr.TELEMETRY_SCHEMA,
            "engine": _gr.ENGINE, "engine_version": _gr.ENGINE_VERSION}


def _sg_telemetry_dict(arm, s, *, flat, mut=None):
    feet = 0.0 if flat else {"R": 0.0, "L": 0.0}
    cf = 0 if flat else {"R": 0, "L": 0}
    data = {
        "schema": _gr.TELEMETRY_SCHEMA,
        "engine": _gr.ENGINE, "engine_version": _gr.ENGINE_VERSION,
        "sim": {"use_deterministic_algorithms": True,
                "gs_enable_zerocopy": False,
                "telem_hz": _gr.TELEM_HZ, "dur_s": _gr.DUR_S},
        "series": {k: [0.0] * _gr.EXPECTED_FRAMES for k in _gr.SERIES_KEYS},
        "params": {"arm": arm},
        "outcomes": {"fell": False, "fall_frame": None,
                     "torso_z_min": 1.0, "tilt_max_deg": 0.0,
                     "slip_dist_m": feet, "slip_v_max_mps": feet,
                     "slip_v_max_postpush_mps": feet,
                     "contact_force_peak_N": feet, "contact_frames": cf}}
    if mut is not None:
        mut(arm, s, data)
    return data


def _sg_materialize(wd, *, flat=True, seeds=None, summary_obj=None,
                    summary_bytes=None, bad_telem=None, omit_telem=(),
                    csv_cols=None, telem_mut=None):
    """测试侧直接铺批次文件（G5a/G5b；不走子进程生成）。"""
    seeds = _gr.MIN_SEEDS if seeds is None else seeds
    wd.mkdir(parents=True, exist_ok=True)
    if summary_bytes is not None:
        (wd / "summary.json").write_bytes(summary_bytes)
    else:
        (wd / "summary.json").write_text(
            json.dumps(summary_obj if summary_obj is not None
                       else {"seeds": seeds}),
            encoding="utf-8")
    td = wd / "telemetry"
    td.mkdir(exist_ok=True)
    for arm in _gr.ARMS:
        for s in range(seeds):
            rid = f"{arm}_s{s}"
            if rid in omit_telem:
                continue
            p = td / f"{rid}.json"
            if bad_telem == rid:
                p.write_bytes(b'{"broken":')
                continue
            p.write_text(json.dumps(_sg_telemetry_dict(
                arm, s, flat=flat, mut=telem_mut)), encoding="utf-8")
    cols = csv_cols if csv_cols is not None else list(_gr.CSV_REQUIRED_FIELDS)
    with open(wd / "dr_controls_vs_experimental.csv", "w", newline="",
              encoding="utf-8") as fh:
        w = _csv_sg.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for arm in _gr.ARMS:
            for s in range(seeds):
                w.writerow({c: "0" for c in cols}
                           | {"run_id": f"{arm}_s{s}"})


class _SgCapGateway(FakeGateway):
    """帽守卫：第 cap+1 次调用即 AssertionError（FakeGateway 空响应之外双保险）。"""

    def __init__(self, responses, cap, **kw):
        super().__init__(responses, **kw)
        self._sg_cap = cap

    def call(self, stage, messages, **kw):
        assert len(self.calls) < self._sg_cap, \
            f"第 {self._sg_cap + 1} 次 coding 调用：硬上限被突破"
        return super().call(stage, messages, **kw)


def _sg_setup(tmp_path, responses, *, shape="flat", cap=5,
              thread="t7b-sg", echo=None, gw=None, usage=(None, None),
              bare=False, files=None, script=_SG_BATCH_SCRIPT,
              flag=True):
    wd = tmp_path / "work"
    wd.mkdir(parents=True, exist_ok=True)
    (wd / "script.py").write_text(script, encoding="utf-8")
    (wd / "shape.txt").write_text("_bare" if bare else shape,
                                  encoding="utf-8")
    (wd / "_sg_const.json").write_text(
        json.dumps(_sg_const_doc()), encoding="utf-8")
    if echo is not None:
        (wd / "echo.txt").write_text(echo, encoding="utf-8")
    for rel, content in (files or {}).items():
        p = wd / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    if gw is None:
        gw = FakeGateway(responses, usage=usage)
    cache = CallReplay(tmp_path / "cache")
    runner = AgentRunner(gw, FakeVector(), evidence_root=tmp_path / "ev",
                         call_cache=cache, recall=None)
    spec = RunSpec(
        thread_id=thread, workdir=wd,
        cmd=[sys.executable, str(wd / "script.py")],
        telemetry_relpath="telemetry.json", batch_id="T6-SG-FIXTURE",
        billing_task=6, run_idx=1, max_coding_rounds=cap,
        batch_schema_check=flag)
    return runner, spec, gw, wd, cache


def _sg_tag_patches(n):
    # G3：每轮恰好一次命中的脚本注释锚点，结构失败轮轮可应用。
    return [_run7b_structured(_run7b_patch(
        "script.py", f"# sg-tag{i - 1}", f"# sg-tag{i}"))
        for i in range(1, n + 1)]


def _sg_schema_entries(rec):
    return [t for t in rec["timeline"] if "batch_schema_errors" in t]


def _sg_user_text(call):
    return call["messages"][1]["content"]


def _sg_note_segment(user):
    """结构注记段：从 head 到下一个 ## 节标题为止（cmd 节合法携带绝对路径，
    脱敏断言只覆盖注记段，不覆盖整封消息）。"""
    i0 = user.index(_SG_HEAD)
    seg = user[i0:]
    j = seg.find("\n## ", 1)
    return seg if j == -1 else seg[:j]


def _sg_norm(text, *roots):
    for r in roots:
        text = text.replace(str(r), "<WD>")
    return text


def _sg_evidence_files(tmp_path, thread):
    d = tmp_path / "ev" / thread
    return sorted(p.name for p in d.glob("coding_round*")) if d.is_dir() else []


def _sg_req_blob(path, *roots):
    """req 证据归一化比较：路径折叠 + 剔除逐秒变化的 ts_iso。"""
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    doc.pop("ts_iso", None)
    return _sg_norm(json.dumps(doc, ensure_ascii=False, sort_keys=True), *roots)


# ---- G1：结构违约 → 1 轮结构修复 → 收敛（gw=1/exec=2） ---------------------

def test_sg_g1_structural_then_converge(tmp_path):
    responses = [_run7b_structured(
        _run7b_patch("shape.txt", "flat", "nested"))]
    runner, spec, gw, wd, _ = _sg_setup(
        tmp_path, responses, thread="t7b-sg-g1")
    rec = runner.run(spec)

    assert rec["status"] == "ok"
    assert rec["coding_round"] == 1 and rec["executions"] == 2
    assert len(gw.calls) == 1
    raw0 = rec["timeline"][0]["batch_schema_errors"]
    assert len(raw0) == 45                      # 9 run × 5 模板，全量 RAW
    assert "batch_schema_errors" not in rec["timeline"][1]   # G13c
    # m5：坏批先证 load_errs==[]（非缺件/缺列假绿）
    _t, _r, _s, le = _gr.load_batch(wd)
    assert le == []
    user = _sg_user_text(gw.calls[0])
    assert _SG_HEAD in user
    assert "共 45 条，去重 5 类" in user
    seg = _sg_note_segment(user)
    templates = sorted({ln.split(": ", 1)[1] for ln in raw0})
    assert len(templates) == 5
    for tpl in templates:                      # 5 模板句取自实际发射，逐句在
        assert tpl in seg
    assert str(wd) not in seg                 # 注记段模型面无绝对路径


# ---- G2：rc0 干净批 0 coding、SUCCESS、扫描恰一次 --------------------------

def test_sg_g2_clean_scans_once(tmp_path, monkeypatch):
    calls = []
    orig = _gr.scan_batch_schema_gate

    def spy(workdir):
        calls.append(str(workdir))
        return orig(workdir)

    monkeypatch.setattr(_gr, "scan_batch_schema_gate", spy)
    runner, spec, gw, wd, _ = _sg_setup(
        tmp_path, [], shape="nested", thread="t7b-sg-g2")
    rec = runner.run(spec)

    assert rec["status"] == "ok"
    assert rec["coding_round"] == 0 and rec["executions"] == 1
    assert gw.calls == [] and len(calls) == 1
    assert all("batch_schema_errors" not in t for t in rec["timeline"])


# ---- G2b：结构干净但 rung 必败（fell/inf/nan）仍 SUCCESS；四 rung spy 零呼 -

def test_sg_g2b_structural_clean_rung_hostile_no_rung_calls(tmp_path,
                                                            monkeypatch):
    spied = {}
    for name in ("rung1_check", "rung2_check", "rung3_check",
                 "_check_calibration"):
        def make(n):
            def fake(*a, **k):
                spied[n] = spied.get(n, 0) + 1
                raise AssertionError(f"runner 路径不得调用 {n}")
            return fake
        monkeypatch.setattr(_gr, name, make(name))
    scan_calls = []
    orig = _gr.scan_batch_schema_gate
    monkeypatch.setattr(_gr, "scan_batch_schema_gate",
                        lambda w: (scan_calls.append(w) or orig(w)))
    runner, spec, gw, wd, _ = _sg_setup(
        tmp_path, [], shape="rungbad",
        gw=FakeGateway([]), thread="t7b-sg-g2b")
    rec = runner.run(spec)

    assert rec["status"] == "ok" and rec["coding_round"] == 0
    assert gw.calls == [] and len(scan_calls) == 1
    assert spied == {}
    assert _sg_schema_entries(rec) == []


# ---- G3：全结构失败，cap=2→exec3 / cap=5→exec6，每失败 exec 全量 RAW -------

@pytest.mark.parametrize("cap", [2, 5])
def test_sg_g3_structural_fail_to_cap(tmp_path, cap):
    gw = _SgCapGateway(_sg_tag_patches(cap), cap)
    runner, spec, _, wd, _ = _sg_setup(
        tmp_path, [], cap=cap, gw=gw, thread=f"t7b-sg-g3-c{cap}")
    rec = runner.run(spec)

    assert rec["status"] == "failed"
    assert rec["coding_round"] == cap
    assert rec["executions"] == cap + 1
    assert len(gw.calls) == cap                    # s2 口径：网关恰 cap 次
    # 第 cap+1 次调用假件本身即炸（控制流帽尽在先，此为显式双保险留痕）
    with pytest.raises(AssertionError, match="硬上限被突破"):
        gw.call("coding_debug", [])
    entries = _sg_schema_entries(rec)
    assert len(entries) == cap + 1            # 含帽尽末次 exec
    for t in entries:
        assert len(t["batch_schema_errors"]) == 45
    assert "共 45 条，去重 5 类" in rec["reason"]
    assert (wd / "FAILURE.json").is_file()


# ---- G4：默认关零行为变化（子进程 -I 证 judge 不导入 + req 字节相等） ------

def test_sg_g4_flag_off_isolated_subprocess_and_req_equality(tmp_path):
    repo = Path(__file__).resolve().parents[2]

    # 同形 flag on/off（rc!=0 轮）：独立 workdir/root，messages/req/缓存键
    # 路径归一后逐字节相等（缓存实写需非 None usage）。
    def one_run(flag, slot):
        root = tmp_path / slot
        wd = root / "work"
        wd.mkdir(parents=True)
        gw = FakeGateway([_run7b_structured(_run7b_patch(
            "fix.txt", "NEEDS_FIX", "PATCHED"))], usage=(10, 20))
        cache = CallReplay(root / "cache")
        runner = AgentRunner(gw, FakeVector(), evidence_root=root / "ev",
                             call_cache=cache, recall=None)
        (wd / "script.py").write_text(_SG_FIXED_FAIL_SCRIPT, encoding="utf-8")
        (wd / "fix.txt").write_text("NEEDS_FIX", encoding="utf-8")
        spec = RunSpec(
            thread_id="t7b-sg-g4", workdir=wd,
            cmd=[sys.executable, str(wd / "script.py")],
            telemetry_relpath="telemetry.json", batch_id="T6",
            billing_task=6, run_idx=1, max_coding_rounds=1,
            batch_schema_check=flag)
        return runner.run(spec), gw, root, wd

    rec_on, gw_on, root_on, wd_on = one_run(True, "on")
    rec_off, gw_off, root_off, wd_off = one_run(False, "off")
    assert rec_on["status"] == rec_off["status"] == "failed"
    msg_on = _sg_norm(json.dumps(gw_on.calls[0]["messages"],
                                 ensure_ascii=False, sort_keys=True),
                      root_on, wd_on)
    msg_off = _sg_norm(json.dumps(gw_off.calls[0]["messages"],
                                  ensure_ascii=False, sort_keys=True),
                       root_off, wd_off)
    assert msg_on == msg_off
    assert "批次结构闸" not in msg_off
    for r in (root_on, root_off):
        keys = sorted(p.name for p in (r / "cache").glob("*.json"))
        assert keys == ["coding_t7b-sg-g4_r1_round1.json"]
    req_on = (root_on / "ev" / "t7b-sg-g4" / "coding_round1.req.json")
    req_off = (root_off / "ev" / "t7b-sg-g4" / "coding_round1.req.json")
    assert _sg_req_blob(req_on, root_on, wd_on) == \
        _sg_req_blob(req_off, root_off, wd_off)
    # 缺字段构造（RunSpec 不传 batch_schema_check）同形
    spec_missing = RunSpec(
        thread_id="t7b-sg-g4", workdir=wd_on,
        cmd=[sys.executable, str(wd_on / "script.py")],
        telemetry_relpath="telemetry.json", batch_id="T6",
        billing_task=6, run_idx=1, max_coding_rounds=1)
    assert spec_missing.batch_schema_check is False

    # 隔离子进程：flag-off 坏批也 SUCCESS，且 orchestrator.judge 不进 sys.modules。
    child_wd = tmp_path / "child"
    const_p = tmp_path / "_child_const.json"
    const_p.write_text(json.dumps(_sg_const_doc()), encoding="utf-8")
    code = f"""
import json, sys, glob
sys.path.insert(0, {str(repo)!r})
from pathlib import Path
from orchestrator.agent_runner.runner import AgentRunner, RunSpec
from orchestrator.common.callcache import CallReplay

class G:
    def model_id(self, s): return "m"
    def call(self, *a, **k): raise AssertionError("flag-off 零调用")
class V:
    def query_kb(self, *a, **k): return []

wd = Path({str(child_wd)!r}); wd.mkdir(exist_ok=True)
const = json.loads(Path({str(const_p)!r}).read_text(encoding="utf-8"))
(wd / "summary.json").write_text(json.dumps({{"seeds": const["seeds"]}}),
                                 encoding="utf-8")
td = wd / "telemetry"; td.mkdir(exist_ok=True)
for arm in const["arms"]:
    for s in range(const["seeds"]):
        data = {{"schema": const["schema"], "engine": const["engine"],
                "engine_version": const["engine_version"],
                "sim": {{"use_deterministic_algorithms": True,
                        "gs_enable_zerocopy": False,
                        "telem_hz": const["telem_hz"],
                        "dur_s": const["dur_s"]}},
                "series": {{k: [0.0] * const["frames"]
                           for k in const["series_keys"]}},
                "params": {{"arm": arm}},
                "outcomes": {{"fell": False, "fall_frame": None,
                             "torso_z_min": 1.0, "tilt_max_deg": 0.0,
                             "slip_dist_m": 0.0, "slip_v_max_mps": 0.0,
                             "slip_v_max_postpush_mps": 0.0,
                             "contact_force_peak_N": 0.0,
                             "contact_frames": 0}}}}
        (td / (arm + "_s" + str(s) + ".json")).write_text(
            json.dumps(data), encoding="utf-8")
import csv
with open(wd / "dr_controls_vs_experimental.csv", "w", newline="",
          encoding="utf-8") as fh:
    w = csv.DictWriter(fh, fieldnames=const["csv_fields"])
    w.writeheader()
    for arm in const["arms"]:
        for s in range(const["seeds"]):
            row = {{c: "0" for c in const["csv_fields"]}}
            row["run_id"] = arm + "_s" + str(s)
            w.writerow(row)
(wd / "telemetry.json").write_text('{{"ok": true}}', encoding="utf-8")
(wd / "script.py").write_text("raise SystemExit(0)\\n", encoding="utf-8")
spec = RunSpec(thread_id="child", workdir=wd,
               cmd=[sys.executable, str(wd / "script.py")],
               telemetry_relpath="telemetry.json", batch_id="T6",
               billing_task=6, run_idx=1)
runner = AgentRunner(G(), V(), evidence_root=wd / "ev",
                     call_cache=CallReplay(wd / "cache"), recall=None)
rec = runner.run(spec)
ev_files = sorted(glob.glob(str(wd / "ev" / "child" / "coding_round*")))
print("STATUS", rec["status"], rec["coding_round"])
print("JUDGE_PRESENT", "orchestrator.judge" in sys.modules)
print("GAIT_PRESENT", any("gait_rungs" in m for m in sys.modules))
print("EVIDENCE_EMPTY", ev_files == [])
"""
    proc = _sp_sg.run([sys.executable, "-I", "-c", code],
                      capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert "STATUS ok 0" in proc.stdout
    assert "JUDGE_PRESENT False" in proc.stdout
    assert "GAIT_PRESENT False" in proc.stdout
    assert "EVIDENCE_EMPTY True" in proc.stdout


# ---- G5a：读盘产品类五形态（真实扫描，不 monkeypatch 谓词） ----------------

def _sg_readfault_round(tmp_path, thread, *, response_old="flat"):
    """cap=1 单轮：返回产品/结构注记与 FAILURE 记录。"""
    responses = [_run7b_structured(
        _run7b_patch("shape.txt", response_old, response_old + "x"))]
    runner, spec, gw, wd, _ = _sg_setup(
        tmp_path, responses, cap=1, bare=True, thread=thread)
    return runner.run(spec), gw, wd


def test_sg_g5a1_nameless_jsondecode_fallback(tmp_path, monkeypatch):
    def fake_load(_wd):
        raise json.JSONDecodeError("msg", "doc", 0)

    monkeypatch.setattr(_gr, "load_batch", fake_load)
    rec, gw, wd = _sg_readfault_round(tmp_path, "t7b-sg-g5a1")
    assert rec["status"] == "failed" and len(gw.calls) == 1
    raw = rec["timeline"][0]["batch_schema_errors"]
    assert raw == [_SG_PRODUCT_LINE_PREFIX + "JSONDecodeError: 批次产物"]
    user = _sg_user_text(gw.calls[0])
    assert "共 1 条，去重 1 类" in user
    seg = _sg_note_segment(user)
    assert "仅产物修复" in seg and str(wd) not in seg
    assert redact_for_model(user) == user


def test_sg_g5a2_truncated_summary_json(tmp_path):
    def setup(wd):
        _sg_materialize(wd, summary_bytes=b'{"seeds":')

    runner, spec, gw, wd, _ = _sg_setup(
        tmp_path, _sg_tag_patches(1), cap=1, bare=True,
        thread="t7b-sg-g5a2")
    _sg_materialize(wd, summary_bytes=b'{"seeds":')
    rec = runner.run(spec)
    raw = rec["timeline"][0]["batch_schema_errors"]
    assert raw == [_SG_PRODUCT_LINE_PREFIX + "JSONDecodeError: summary.json"]
    assert str(wd) not in raw[0]


def test_sg_g5a3_broken_telemetry_json(tmp_path):
    runner, spec, gw, wd, _ = _sg_setup(
        tmp_path, _sg_tag_patches(1), cap=1, bare=True,
        thread="t7b-sg-g5a3")
    _sg_materialize(wd, bad_telem="normal_s0")
    rec = runner.run(spec)
    raw = rec["timeline"][0]["batch_schema_errors"]
    assert raw == [_SG_PRODUCT_LINE_PREFIX +
                   "JSONDecodeError: normal_s0.json"]


@pytest.mark.parametrize("summary_obj,exc_name", [
    ({}, "KeyError"),
    ({"seeds": None}, "TypeError"),
    ([], "TypeError"),
    ({"seeds": "abc"}, "ValueError"),
])
def test_sg_g5a4_seeds_four_forms(tmp_path, summary_obj, exc_name):
    runner, spec, gw, wd, _ = _sg_setup(
        tmp_path, _sg_tag_patches(1), cap=1, bare=True,
        thread=f"t7b-sg-g5a4-{exc_name}")
    _sg_materialize(wd, summary_obj=summary_obj)
    rec = runner.run(spec)
    raw = rec["timeline"][0]["batch_schema_errors"]
    assert raw == [f"{_SG_PRODUCT_LINE_PREFIX}{exc_name}: summary.json"]


def test_sg_g5a5_missing_telem_and_csv_col_are_structural(tmp_path):
    runner, spec, gw, wd, _ = _sg_setup(
        tmp_path, _sg_tag_patches(1), cap=1, bare=True,
        thread="t7b-sg-g5a5")
    _sg_materialize(wd, flat=False, omit_telem=("normal_s0",),
                    csv_cols=["run_id"])
    rec = runner.run(spec)
    entry = _sg_schema_entries(rec)[0]
    raws = entry["batch_schema_errors"]
    assert len(raws) == 2
    # 人侧 RAW：缺件行绝对路径；缺列行按校验器发射口径用相对文件名
    gap = [r for r in raws if r.startswith("缺 ")]
    col = [r for r in raws if "缺列" in r]
    assert len(gap) == len(col) == 1
    assert str(wd) in gap[0] and str(wd) not in col[0]
    user = _sg_user_text(gw.calls[0])
    seg = _sg_note_segment(user)
    assert "共 2 条，去重 2 类" in seg
    assert "仅结构修正" in seg                     # 结构形，非读盘形
    assert _SG_PRODUCT_LINE_PREFIX not in seg
    assert "缺 telemetry/normal_s0.json" in seg    # 模型面 scrub 相对形
    assert "缺列" in seg
    assert str(wd) not in seg


# ---- G5b：引擎类四例 × run()/seam() 两入口（0 付费、零 side effect） ------

@pytest.mark.parametrize("where,kind", [
    ("collector_nameerror", "collector"),
    ("read_permission", "read"),
    ("validate_keyerror", "collector"),
    ("validate_typeerror", "collector"),
])
def test_sg_g5b_engine_crashes_both_entrances(tmp_path, monkeypatch,
                                              where, kind):
    wd = tmp_path / "work"
    # 真实铺一个扁平批（无 monkeypatch 时为 45 条结构错）；脚本只负责 rc0 +
    # 标记件。各例 monkeypatch 在读取/校验点制造引擎类异常。
    _sg_materialize(wd, flat=True)
    (wd / "script.py").write_text("raise SystemExit(0)\n")
    (wd / "telemetry.json").write_text('{"ok": true}')

    def build_runner():
        gw = FakeGateway([])
        cache = CallReplay(tmp_path / f"cache-{where}")
        runner = AgentRunner(gw, FakeVector(),
                             evidence_root=tmp_path / "ev",
                             call_cache=cache, recall=None)
        spec = RunSpec(
            thread_id=f"t7b-sg-g5b-{where}", workdir=wd,
            cmd=[sys.executable, str(wd / "script.py")],
            telemetry_relpath="telemetry.json", batch_id="T6",
            billing_task=6, run_idx=1, max_coding_rounds=5,
            batch_schema_check=True)
        return runner, spec, cache, gw

    if where == "collector_nameerror":
        def boom_collector(_t, _s, _l):
            raise NameError("future_validator_bug")
        monkeypatch.setattr(_gr, "collect_batch_schema_errors",
                            boom_collector)
    elif where == "read_permission":
        def boom_load(_wd):
            raise PermissionError(13, "Permission denied",
                                  str(wd / "summary.json"))
        monkeypatch.setattr(_gr, "load_batch", boom_load)
    elif where == "validate_keyerror":
        def boom_validate_key(_d, _r):
            raise KeyError("future_validator_bug")
        monkeypatch.setattr(_gr, "validate_telemetry_structure",
                            boom_validate_key)
    else:
        def boom_validate_type(_d, _r):
            raise TypeError("future_validator_bug")
        monkeypatch.setattr(_gr, "validate_telemetry_structure",
                            boom_validate_type)

    # 入口 1：runner.run
    runner, spec, cache, gw = build_runner()
    with pytest.raises(_gr.BatchSchemaGateError) as ei:
        runner.run(spec)
    assert ei.value.phase == kind
    assert isinstance(ei.value, RuntimeError)
    assert ei.value.__cause__ is not None
    if kind == "read":
        assert str(ei.value) == "批次产物读取引擎异常：PermissionError"
        assert str(wd) not in str(ei.value)
    else:
        assert str(ei.value).startswith("批次结构校验器引擎异常：")

    # 入口 2：agent_run_seam
    runner2, spec2, cache2, gw2 = build_runner()
    seam = agent_run_seam(runner2, lambda state: spec2)
    with pytest.raises(_gr.BatchSchemaGateError) as ei2:
        seam({"gate2_rejected": []})
    assert ei2.value.phase == kind and ei2.value.__cause__ is not None

    # 可观测集：0 付费、无 FAILURE、无 req/decision、无缓存写
    assert gw.calls == [] and gw2.calls == []
    assert not (wd / "FAILURE.json").exists()
    assert _sg_evidence_files(tmp_path, f"t7b-sg-g5b-{where}") == []
    assert list(cache.dir.glob("*.json")) == []
    assert list(cache2.dir.glob("*.json")) == []


def test_sg_g5b_phase_kwonly_and_typeerror():
    with pytest.raises(TypeError):
        _gr.BatchSchemaGateError("x", "read")
    assert issubclass(_gr.BatchSchemaGateError, RuntimeError)


# ---- G6：人/模分道 + 端到端 redact（真实发射 45 + 注入缺件行） -------------

def test_sg_g6_human_raw_vs_model_face_redact(tmp_path):
    runner, spec, gw, wd, _ = _sg_setup(
        tmp_path, _sg_tag_patches(5), cap=5,
        thread="t6_genesis-ascent-20261016a_1")
    rec = runner.run(spec)
    raw = _sg_schema_entries(rec)[0]["batch_schema_errors"]
    assert len(raw) == 45
    # 另注入一行真实 load_err 形态（人侧绝对路径，RAW 不脱敏）
    injected = f"缺 {wd}/telemetry/normal_s0.json"
    raw_with_gap = list(raw) + [injected]
    assert any(str(wd) in ln for ln in raw_with_gap)
    faces, m_total = _build_schema_face(raw_with_gap, workdir=wd)
    assert m_total == 6                         # 5 模板类 + 1 缺件类
    assert "缺 telemetry/normal_s0.json" in faces
    assert all(str(wd) not in f for f in faces)
    note_struct = _append_batch_schema_note(
        "", raw_with_gap, len(raw_with_gap), read_fault=False, workdir=wd)
    assert f"共 {len(raw_with_gap)} 条，去重 6 类" in note_struct   # N/M 写真值
    assert '"' not in note_struct
    note_read = _append_batch_schema_note(
        "", [_SG_PRODUCT_LINE_PREFIX + "JSONDecodeError: summary.json"], 1,
        read_fault=True, workdir=wd)
    # 两形注记过真实 redact 逐字存活；54 形毒行只杀毒行自身
    for note in (note_struct, note_read):
        assert redact_for_model(note) == note
        for word, poison in _RUN7B_MATRIX:
            out = redact_for_model(note + "\n" + poison)
            assert note in out and word not in out
    # 真实一轮模型面（活跑 M=5 帽不绑定）
    user = _sg_user_text(gw.calls[0])
    seg = _sg_note_segment(user)
    assert "共 45 条，去重 5 类" in seg
    assert str(wd) not in seg
    templates = sorted({ln.split(": ", 1)[1] for ln in raw})
    for tpl in templates:
        assert tpl in seg


# ---- G7：RunSpec 默认关 -----------------------------------------------------

def test_sg_g7_runspec_default_off():
    f = next(f for f in _dc_sg.fields(RunSpec)
             if f.name == "batch_schema_check")
    assert f.default is False


# ---- G8：纯函数 -------------------------------------------------------------

def test_sg_g8_pure_face_functions():
    flat = []
    for arm in ("normal", "ice_nominal", "ice_dr"):
        for s in range(3):
            for tpl in ("slip_dist_m", "slip_v_max_mps",
                        "slip_v_max_postpush_mps", "contact_force_peak_N",
                        "contact_frames"):
                flat.append(
                    f"{arm}_s{s}: outcomes.{tpl} 必须是 {{R,L}} 对象")
    faces, m_total = _build_schema_face(flat, workdir="/tmp/wd8")
    assert (m_total, len(faces)) == (5, 5)
    # 无 ": " 行各自成类、保序
    uniq, m2 = _dedup_schema_face(["缺件行甲", "缺件行甲", "缺件行乙",
                                   "summary.seeds=2 < 3（计划 L567 N≥3）"])
    assert uniq == ["缺件行甲", "缺件行乙",
                    "summary.seeds=2 < 3（计划 L567 N≥3）"] and m2 == 3
    # scrub 三形
    wd = "/tmp/wd8x"
    assert _scrub_workdir(f"缺 {wd}/telemetry/x.json", wd) == \
        "缺 telemetry/x.json"
    assert _scrub_workdir("normal_s0: tpl", wd) == "normal_s0: tpl"
    assert _scrub_workdir(f"pad {wd} pad", wd) == "pad . pad"
    # normalize 两形 + passthrough（seed 后缀有意不进面文）
    s_line = next(
        ln for ln in _gr.validate_telemetry_structure(
            {"schema": "bad", "engine": _gr.ENGINE,
             "engine_version": _gr.ENGINE_VERSION, "sim": {}, "series": {},
             "params": {}, "outcomes": {}}, "normal_s2")
        if ln.startswith("normal_s2: schema="))
    assert _schema_face_normalize(s_line) == (
        f"normal: schema 不等于约定值（要求 {_gr.TELEMETRY_SCHEMA}）")
    e_line = next(
        ln for ln in _gr.validate_telemetry_structure(
            {"schema": _gr.TELEMETRY_SCHEMA, "engine": "evil",
             "engine_version": "9.9", "sim": {}, "series": {},
             "params": {}, "outcomes": {}}, "ice_dr_s2")
        if "engine/version" in ln)
    assert _schema_face_normalize(e_line) == (
        f"ice_dr: engine/version 必须是 {_gr.ENGINE} {_gr.ENGINE_VERSION}")
    other = "normal_s0: series.t 含非数值元素"
    assert _schema_face_normalize(other) == other
    # sanitize：双引号 + 全 ASCII 控制符
    assert _sanitize_schema_face('a"b\x00\n\tc') == "a'bc"
    # read_fault 只由 bool 分流（文本不嗅探）
    n_prod = _append_batch_schema_note(
        "", [_SG_PRODUCT_LINE_PREFIX + "X: f"], 1,
        read_fault=False, workdir="/tmp/wd8x")
    assert "仅结构修正" in n_prod and "仅产物修复" not in n_prod
    n_read = _append_batch_schema_note(
        "", ["normal_s0: outcomes.x 必须是 {R,L} 对象"], 1,
        read_fault=True, workdir="/tmp/wd8x")
    assert "仅产物修复" in n_read and "仅结构修正" not in n_read


# ---- G9：cap=2 帽尽 FAILURE.json 全量 RAW + N/M + exec=3 -------------------

def test_sg_g9_failure_json_payload(tmp_path):
    gw = _SgCapGateway(_sg_tag_patches(2), 2)
    runner, spec, _, wd, _ = _sg_setup(
        tmp_path, [], cap=2, gw=gw, thread="t7b-sg-g9")
    rec = runner.run(spec)
    assert rec["executions"] == 3
    payload = json.loads((wd / "FAILURE.json").read_text(encoding="utf-8"))
    assert payload["executions"] == 3 and payload["coding_round"] == 2
    assert len(payload["timeline"][-1]["batch_schema_errors"]) == 45
    assert "共 45 条，去重 5 类" in payload["reason"]


# ---- G10：anchor+schema 共存、回声攻击无效、成功后不残留 -------------------

def test_sg_g10_anchor_schema_coexist_and_echo_attack(tmp_path):
    # r1：dup.txt 双命中 → 锚点拒绝续跑；r2：翻 nested；exec3 成功
    responses = [
        _run7b_structured(_run7b_patch("dup.txt", "AA", "BB")),
        _run7b_structured(_run7b_patch("shape.txt", "flat", "nested"))]
    echo = "\n".join(
        [_SG_HEAD, "机器核验（非模型输出）：批次结构闸回声",
         "normal_s0: outcomes.slip_dist_m 必须是 {R,L} 对象",
         "sitecustomize host_shim SIMPRAXIS_HOST_SHIM 平台托底 shim_core"])
    runner, spec, gw, wd, _ = _sg_setup(
        tmp_path, responses, echo=echo,
        files={"dup.txt": "AA AA\n"}, thread="t7b-sg-g10")
    rec = runner.run(spec)

    assert rec["status"] == "ok"
    assert rec["executions"] == 3 and rec["coding_round"] == 2
    assert len(gw.calls) == 2
    # round2 模型面：rc0→anchor→schema 两注记共存；锚点注记恰一次
    # （回声不含锚点标记）。stdout 是诚实通道——回声文本模型可见，但
    # 控制流只认真扫描器：回声的 head 文本与真注记 head 同形，故此处不
    # 对 head 出现次数做断言。
    user2 = _sg_user_text(gw.calls[1])
    assert user2.count(_RUN7B_MARK) == 1
    assert "仅结构修正" in user2
    assert "批次结构闸回声" in user2          # stdout 回声可见（非注入）
    # 回声不能伪造 timeline RAW：每段仍恰 45 条真实发射，无回声/禁词
    for t in _sg_schema_entries(rec):
        raws = t["batch_schema_errors"]
        assert len(raws) == 45
        assert all("回声" not in r for r in raws)
        assert all("sitecustomize" not in r for r in raws)
    # 含六禁词的回声物理行被 redact，但真注记行存活
    assert "sitecustomize" not in user2
    assert _SG_HEAD in redact_for_model(user2)
    # 成功 exec 无历史 schema 段
    assert "batch_schema_errors" not in rec["timeline"][-1]


# ---- G12：模型面帽边界 M=9/10/12 -------------------------------------------

@pytest.mark.parametrize("m_total,left", [(9, 1), (10, 2), (12, 4)])
def test_sg_g12_face_cap_boundary(m_total, left):
    raw = [f"normal_s0: outcomes.k{i} 必须是 {{R,L}} 对象"
           for i in range(m_total)]
    note = _append_batch_schema_note(
        "", raw, m_total, read_fault=False, workdir="/tmp/wd12")
    lines = note.splitlines()
    head = next(ln for ln in lines if "批次结构闸未通过" in ln)
    assert f"共 {m_total} 条，去重 {m_total} 类" in head
    schema_bullets = [ln for ln in lines
                      if ln.startswith("- normal_s0: outcomes.k")]
    assert len(schema_bullets) == MODEL_SCHEMA_FACE_CAP
    omit = f"- ……（其余 {left} 类略，完整机器证据见 timeline/FAILURE）"
    assert omit in lines
    assert len(raw) == m_total                    # RAW 全量不截断


# ---- G13a/G17：rc!=0 不扫描；flag on/off messages+req 逐字节相等 -----------

def test_sg_g13a_g17_rc_nonzero_flag_byte_equality(tmp_path, monkeypatch):
    calls = []
    orig = _gr.scan_batch_schema_gate
    monkeypatch.setattr(_gr, "scan_batch_schema_gate",
                        lambda w: (calls.append(w) or orig(w)))

    def run_once(flag, slot, thread):
        root = tmp_path / slot
        wd = root / "work"
        wd.mkdir(parents=True)
        (wd / "script.py").write_text(_SG_FIXED_FAIL_SCRIPT, encoding="utf-8")
        gw = FakeGateway([_run7b_structured(_run7b_patch(
            "fix.txt", "NEEDS_FIX", "PATCHED"))])
        cache = CallReplay(root / "cache")
        runner = AgentRunner(gw, FakeVector(), evidence_root=root / "ev",
                             call_cache=cache, recall=None)
        spec = RunSpec(
            thread_id=thread, workdir=wd,
            cmd=[sys.executable, str(wd / "script.py")],
            telemetry_relpath="telemetry.json", batch_id="T6",
            billing_task=6, run_idx=1, max_coding_rounds=1,
            batch_schema_check=flag)
        return runner.run(spec), gw, root, wd

    rec_on, gw_on, root_on, wd_on = run_once(True, "on", "t7b-sg-g13")
    rec_off, gw_off, root_off, wd_off = run_once(False, "off", "t7b-sg-g13")
    assert calls == []                               # rc!=0 spy=0
    assert _sg_schema_entries(rec_on) == [] and _sg_schema_entries(
        rec_off) == []
    m_on = _sg_norm(json.dumps(gw_on.calls[0]["messages"],
                               ensure_ascii=False, sort_keys=True),
                    root_on, wd_on)
    m_off = _sg_norm(json.dumps(gw_off.calls[0]["messages"],
                                ensure_ascii=False, sort_keys=True),
                     root_off, wd_off)
    assert m_on == m_off
    assert "批次结构闸" not in m_on
    req_on = (root_on / "ev" / "t7b-sg-g13" / "coding_round1.req.json")
    req_off = (root_off / "ev" / "t7b-sg-g13" / "coding_round1.req.json")
    assert _sg_req_blob(req_on, root_on, wd_on) == \
        _sg_req_blob(req_off, root_off, wd_off)


# ---- G13b：第 5 轮锚点拒绝帽尽（reason 只带锚点形，RAW 留存） --------------

def test_sg_g13b_fifth_round_anchor_cap_reason(tmp_path):
    responses = _sg_tag_patches(4) + [
        _run7b_structured(_run7b_patch("dup.txt", "AA", "BB"))]
    gw = _SgCapGateway(responses, 5)
    runner, spec, _, wd, _ = _sg_setup(
        tmp_path, responses, cap=5, gw=gw,
        files={"dup.txt": "AA AA\n"}, thread="t7b-sg-g13b")
    rec = runner.run(spec)

    assert rec["status"] == "failed"
    assert rec["executions"] == 5 and rec["coding_round"] == 5
    assert len(gw.calls) == 5                        # 被拒轮不重 exec
    assert "末次锚点机器原因" in rec["reason"]
    assert "批次结构闸" not in rec["reason"]
    assert "去重" not in rec["reason"]
    entries = _sg_schema_entries(rec)
    assert len(entries) == 5 and all(
        len(t["batch_schema_errors"]) == 45 for t in entries)


# ---- G14：flag 开 + 干净轮：键不存在（非空 list） --------------------------

def test_sg_g14_clean_key_absent(tmp_path):
    runner, spec, gw, _, _ = _sg_setup(
        tmp_path, [], shape="nested", thread="t7b-sg-g14")
    rec = runner.run(spec)
    assert rec["status"] == "ok"
    assert all("batch_schema_errors" not in t for t in rec["timeline"])


# ---- G15：敌意值矩阵（模型可控值被两形归一化整段丢弃） ---------------------

_SG_POISONS = [
    "a'b",                                   # 逼 repr 用 ASCII 双引号
    "host_shim",                             # 六禁词之一
    '": " evil',                             # 含 ": "
    "tab\there",                             # tab
    "nul\x00here",                           # NUL
    "line1\nline2",                          # 真实换行
    "x" * 500,                               # 超长
]


@pytest.mark.parametrize("poison", _SG_POISONS)
def test_sg_g15_hostile_value_matrix(poison):
    # schema 形：9 run 同毒值
    raws = []
    for arm in ("normal", "ice_nominal", "ice_dr"):
        for s in range(3):
            data = {"schema": poison, "engine": _gr.ENGINE,
                    "engine_version": _gr.ENGINE_VERSION,
                    "sim": {}, "series": {}, "params": {}, "outcomes": {}}
            rid = f"{arm}_s{s}"
            line = next(x for x in _gr.validate_telemetry_structure(
                data, rid) if x.startswith(f"{rid}: schema="))
            raws.append(line)
            assert poison in line or repr(poison) in line   # RAW 逐字保留
    faces, m_total = _build_schema_face(raws, workdir="/tmp/wd15")
    assert m_total == 1 and len(faces) == 1               # 毒值不能伪造 M
    face = faces[0]
    assert poison not in face and "evil" not in face
    assert face == f"normal: schema 不等于约定值（要求 {_gr.TELEMETRY_SCHEMA}）"
    assert '"' not in face
    assert redact_for_model(face) == face
    # engine 形
    eraws = []
    for s in range(9):
        data = {"schema": _gr.TELEMETRY_SCHEMA,
                "engine": "evil" + poison,
                "engine_version": "9.9" + poison,
                "sim": {}, "series": {}, "params": {}, "outcomes": {}}
        rid = f"normal_s{s if s < 3 else 0}"
        eraws.append(next(x for x in _gr.validate_telemetry_structure(
            data, rid) if "engine/version" in x))
    efaces, em = _build_schema_face(eraws, workdir="/tmp/wd15")
    assert em == 1
    assert efaces[0] == (f"normal: engine/version 必须是 {_gr.ENGINE} "
                         f"{_gr.ENGINE_VERSION}")
    assert redact_for_model(efaces[0]) == efaces[0]
    # 非两形行不受归一化影响
    passthrough = f"normal_s0: series.t 含非数值元素 {poison}"
    out = _sanitize_schema_face(_schema_face_normalize(passthrough))
    assert '"' not in out
    if poison not in ("host_shim",):                       # 禁词行由 redact 负责
        assert "normal_s0: series.t 含非数值元素" in out


# ---- G18：成功分支视频四字段键集/值不变 -------------------------------------

def test_sg_g18_success_video_fields_unchanged(tmp_path):
    runner, spec, gw, _, _ = _sg_setup(
        tmp_path, [], shape="nested", thread="t7b-sg-g18")
    rec = runner.run(spec)
    assert rec["status"] == "ok"
    assert set(rec) == {
        "status", "telemetry_uri", "coding_round", "executions", "timeline",
        "video_path", "duration_ms", "cover_png", "intranet_url"}
    assert rec["video_path"] is None and rec["duration_ms"] is None
    assert rec["cover_png"] is None and rec["intranet_url"] is None


# ===========================================================================
# v1.4「IDE 式多次小合成」修复协议测试（.omc/handoffs/
# task6-ide-repair-protocol-design.md 唯一规范件；授权链见 runner.
# PatchTransportError docstring；§3 行为代差登记节为必改③④注释引用源）。
# 必改清单② + §4 新增 hermetic 面：围栏扫描纪律、AST 锚三态与 class 消歧、
# 定位/重 parse 闸零落盘、M1/M2 混出证据登记、J7/J9 反馈续跑与帽尽 _fail、
# P1 仍 instant-death、PatchTransportError 分类学、transport 注记/帽文案、
# 函数源码启发与 8192 预算、transport probe 纯函数面。全 0 付费：FakeGateway
# + tmp 文件系统，无 paid marker、不联网、不读 /mnt、探针永不走 main。
# [v1.4-ide-repair-tests]
# ===========================================================================

from orchestrator.agent_runner.runner import (  # noqa: E402 （v1.4 段局部导入）
    FUNCTION_SOURCE_TOTAL_CHARS,
    REPLACE_FENCE_BEGIN,
    REPLACE_FENCE_END,
    PatchTransportError,
    _append_transport_feedback,
    _scan_replace_fences,
    _parse_replace_and_apply,
    _transport_note,
    collect_repair_functions,
)

_V14_B = REPLACE_FENCE_BEGIN   # "<<<<<<< simpraxis.replace.v1"（7 个 <）
_V14_E = REPLACE_FENCE_END     # ">>>>>>> simpraxis.replace.v1.end"（7 个 >）


def _v14_wd(tmp_path, text, name="t.py"):
    wd = tmp_path / "wd"
    wd.mkdir(exist_ok=True)
    (wd / name).write_text(text, encoding="utf-8")
    return wd


def _v14_fence(path, func, body, cls=None):
    head = f"{_V14_B} path={path} func={func}"
    if cls is not None:
        head += f" class={cls}"
    return f"{head}\n{body}\n{_V14_E}"


# ---- 必改②：出生轮 system 逐字=旧原文（条件拼装 §2.5） --------------------

def test_v14_birth_system_verbatim_old_text():
    """必改清单②（设计 §2.5）：chunk_context 非 None → system 逐字保持旧
    原文（出生环连续四跑成功路径零变化）；修复轮（None）为双通道文案，由
    _build_messages 条件分派钉死。旧原文取 rc2 快照首行（Option A 保留件）。"""
    old_system = _RUN6_RC2_LINES[0]
    ctx = ChunkPromptContext(
        0, 6, "v2.py", _CHUNK_TITLES, _CHUNK_BOUNDARIES,
        previous_code="", round_feedback="")
    birth = _build_messages(
        gate2_notes=None, traceback_tail="T", snippets=[], hits=[],
        cmd=["x"], chunk_context=ctx)[0]["content"]
    assert birth == old_system
    assert "simpraxis.replace.v1" not in birth
    assert "输出前先确认选了哪种通道" not in birth
    repair = _build_messages(
        gate2_notes=None, traceback_tail="T", snippets=[], hits=[],
        cmd=["x"])[0]["content"]
    assert repair != old_system
    assert "simpraxis.replace.v1" in repair
    assert "输出前先确认选了哪种通道" in repair


# ---- 围栏扫描纪律（F1 各形 + 正形；§2.1） ----------------------------------

def test_v14_fence_scanner_positive_variants():
    body = "def add(a, b):\n    return a + b"
    raw = _v14_fence("t.py", "add", body) + "\n"
    blocks = _scan_replace_fences(raw)
    assert len(blocks) == 1
    b = blocks[0]
    assert (b.path, b.func, b.cls) == ("t.py", "add", None)
    assert b.body == body and b.line == 1
    # 可选 ======= 分隔行（紧贴 end 前剥除）
    raw_sep = (f"{_V14_B} path=t.py func=add\n{body}\n=======\n{_V14_E}\n")
    assert _scan_replace_fences(raw_sep)[0].body == body
    # CRLF 逐行归一（剥恰好一个尾 \r）
    assert _scan_replace_fences(raw.replace("\n", "\r\n"))[0].body == body
    # 围栏外非核记散文/JSON 件容忍（M1 同轮混出共存前提）
    raw_mixed = f"修复如下：\n{raw}{{\"patches\": []}}\n"
    assert len(_scan_replace_fences(raw_mixed)) == 1
    # class= 字段捕获
    assert _scan_replace_fences(
        _v14_fence("t.py", "add", body, cls="Calc"))[0].cls == "Calc"
    # 无核记 → 空列表（JSON-only 响应直走既有通道）
    assert _scan_replace_fences('{"patches": []}') == []


def test_v14_fence_scanner_structure_rejects():
    # 半打（begin 无配对 end）
    with pytest.raises(PatchTransportError, match="无配对 end"):
        _scan_replace_fences(
            f"{_V14_B} path=t.py func=add\ndef add():\n    return 1")
    # 全角 begin 不命中 RE 但含核记 → begin/end 行不合规
    with pytest.raises(PatchTransportError, match="begin/end 行不合规"):
        _scan_replace_fences(
            "＜＜＜ simpraxis.replace.v1 path=t.py func=add\n"
            f"def add():\n    return 1\n{_V14_E}\n")
    # 正文行含围栏核记（同出生纪律）
    with pytest.raises(PatchTransportError, match="含围栏核记"):
        _scan_replace_fences(
            f"{_V14_B} path=t.py func=add\n"
            "def add():\n"
            "    # 正文禁写 simpraxis.replace.v1 字样\n"
            f"{_V14_E}\n")
    # 迷走 end 行（无配对 begin）
    with pytest.raises(PatchTransportError, match="无配对 begin"):
        _scan_replace_fences(
            f"{_V14_E}\n{_V14_B} path=t.py func=add\n"
            f"def add():\n    return 1\n{_V14_E}\n")
    # 空/纯空白正文（格式契约底线）
    with pytest.raises(PatchTransportError, match="正文为空"):
        _scan_replace_fences(
            f"{_V14_B} path=t.py func=add\n=======\n{_V14_E}\n")


# ---- AST 锚三态与 class 消歧（F2/F3/F4；§2.1） -----------------------------

_TWO_ADD = (
    "class Calc:\n"
    "    @staticmethod\n"
    "    def add(a, b):\n"
    "        return a - b\n"
    "\n"
    "\n"
    "class Calc2:\n"
    "    @staticmethod\n"
    "    def add(a, b):\n"
    "        return a + b\n")


def test_v14_replace_anchor_states(tmp_path):
    wd = _v14_wd(tmp_path, _TWO_ADD)
    raw0 = _v14_fence("t.py", "missing", "def missing():\n    return 0")
    # F2 锚 0 命中 → 锚点通道（hits=0）
    with pytest.raises(PatchAnchorError, match="0 命中") as ei0:
        _parse_replace_and_apply(wd, raw0)
    assert ei0.value.hits == 0
    # F3 锚 ≥2 无 class= → 候选列表（AST 派生标签可回显）
    raw3 = _v14_fence("t.py", "add", "def add():\n    return 0")
    with pytest.raises(
            PatchAnchorError, match=r"候选 Calc\.add、Calc2\.add") as ei3:
        _parse_replace_and_apply(wd, raw3)
    assert ei3.value.hits == 2
    # F4a class 不存在 → class_not_found
    with pytest.raises(PatchAnchorError, match="class_not_found"):
        _parse_replace_and_apply(
            wd, _v14_fence("t.py", "add", "def add():\n    return 0",
                           cls="Nope"))
    # F4b 类内无该函数 → func_not_in_class
    with pytest.raises(PatchAnchorError, match="func_not_in_class"):
        _parse_replace_and_apply(
            wd, _v14_fence("t.py", "other", "def other():\n    return 0",
                           cls="Calc"))
    # class= 消歧恰 1 → 应用；齐左正文经缩进重定基贴回类缩进（语法完好）
    good = _v14_fence(
        "t.py", "add",
        "@staticmethod\ndef add(a, b):\n    return a + b", cls="Calc")
    applied, extras = _parse_replace_and_apply(wd, good)
    assert applied == ["t.py"] and extras == {}
    text = (wd / "t.py").read_text(encoding="utf-8")
    ast.parse(text)  # 重 parse 闸同款：语法必须完好
    assert "        return a + b" in text
    assert "        return a - b" not in text


# ---- 三闸：F5 定位闸 / F6 重 parse 闸 / F7 非 .py（零落盘） -----------------

def test_v14_replace_gates_f5_f6_f7_zero_write(tmp_path):
    # F5：目标文件 SyntaxError → transport 指引 JSON，文件字节不动
    wd = _v14_wd(tmp_path, "def broken(:\n")
    raw5 = _v14_fence("t.py", "x", "def x():\n    return 1")
    with pytest.raises(PatchTransportError, match="语法不可解析"):
        _parse_replace_and_apply(wd, raw5)
    assert (wd / "t.py").read_text(encoding="utf-8") == "def broken(:\n"
    # F6：投影重 parse 失败（body 坏 Python）→ transport 携行号，零落盘
    wd2 = _v14_wd(tmp_path, "def ok():\n    return 1\n", name="ok.py")
    raw6 = _v14_fence("ok.py", "ok", "def ok(:")
    with pytest.raises(PatchTransportError, match="重 parse 闸失败"):
        _parse_replace_and_apply(wd2, raw6)
    assert (wd2 / "ok.py").read_text(encoding="utf-8") == \
        "def ok():\n    return 1\n"
    # F7：非 .py path → transport 指引 JSON，不落盘
    (wd2 / "note.txt").write_text("orig\n", encoding="utf-8")
    raw7 = _v14_fence("note.txt", "x", "def x():\n    return 1")
    with pytest.raises(PatchTransportError, match=r"非 \.py"):
        _parse_replace_and_apply(wd2, raw7)
    assert (wd2 / "note.txt").read_text(encoding="utf-8") == "orig\n"


# ---- M1/M2 混出证据登记（§2.1） --------------------------------------------

def test_v14_m1_m2_mixed_channel_extras(tmp_path):
    # M1：围栏合法应用成功 + 同轮 JSON 件 → 应用围栏，json_part_ignored
    wd = _v14_wd(tmp_path, "def add(a, b):\n    return a - b\n")
    raw_m1 = (
        _v14_fence("t.py", "add",
                   "def add(a, b):\n    return a + b") + "\n"
        '{"patches": [{"path": "t.py", "old": "x", "new": "y"}]}')
    applied, extras = _parse_replace_and_apply(wd, raw_m1)
    assert applied == ["t.py"] and extras == {"json_part_ignored": True}
    assert "return a + b" in (wd / "t.py").read_text(encoding="utf-8")
    # M2：围栏非法（F2 锚 0）+ 同轮 JSON 合法 → 应用 JSON + 机器原因登记
    wd2 = _v14_wd(tmp_path, "def real():\n    return 1\n", name="t.py")
    raw_m2 = (
        _v14_fence("t.py", "ghost", "def ghost():\n    return 0") + "\n"
        '{"patches": [{"path": "t.py", "old": "return 1", "new": "return 2"}]}')
    applied2, extras2 = _parse_replace_and_apply(wd2, raw_m2)
    assert applied2 == ["t.py"]
    assert "fence_invalid_json_applied" in extras2
    assert "0 命中" in extras2["fence_invalid_json_applied"]
    assert (wd2 / "t.py").read_text(encoding="utf-8") == \
        "def real():\n    return 2\n"
    # 组合反馈：围栏 F2 + 同轮 JSON 也败（J7 空列表）→ 保持锚点键族（hits 保留）
    wd3 = _v14_wd(tmp_path, "def real():\n    return 1\n", name="t.py")
    raw_c = (
        _v14_fence("t.py", "ghost", "def ghost():\n    return 0") + "\n"
        '{"patches": []}')
    with pytest.raises(PatchAnchorError) as ei_c:
        _parse_replace_and_apply(wd3, raw_c)
    assert "同轮 JSON 通道同时失败" in str(ei_c.value)
    assert ei_c.value.hits == 0
    assert (wd3 / "t.py").read_text(encoding="utf-8") == \
        "def real():\n    return 1\n"


def test_v14_mixed_channel_brace_body_json_candidate(tmp_path):
    # 三签 MAJOR-1：围栏体含 dict 字面量时，JSON 侧抽取必须走剥除围栏
    # 行段后的候选文本——raw 整段 first-`{`/last-`}` 切片会切进围栏内容
    # 成垃圾：M1 键静默蒸发 / M2 合法 JSON 被误标 JSONDecodeError。
    # M1：fence 体含 {'sum': a + b} → fence 应用 + json_part_ignored 登记
    wd = _v14_wd(tmp_path, "def add(a, b):\n    return a - b\n")
    raw_m1 = (
        _v14_fence("t.py", "add",
                   "def add(a, b):\n    return {'sum': a + b}") + "\n"
        '{"patches": [{"path": "t.py", "old": "x", "new": "y"}]}')
    applied, extras = _parse_replace_and_apply(wd, raw_m1)
    assert applied == ["t.py"]
    assert extras == {"json_part_ignored": True}
    assert "'sum'" in (wd / "t.py").read_text(encoding="utf-8")
    # M2：fence 体含 dict 字面量且 F2 锚 0 + 同轮尾部合法 JSON →
    # JSON 被应用 + fence_invalid_json_applied 键（机器原因 0 命中）
    wd2 = _v14_wd(tmp_path, "def real():\n    return 1\n", name="t.py")
    raw_m2 = (
        _v14_fence("t.py", "ghost",
                   "def ghost():\n    return {'k': 0}") + "\n"
        '{"patches": [{"path": "t.py", "old": "return 1", "new": "return 2"}]}')
    applied2, extras2 = _parse_replace_and_apply(wd2, raw_m2)
    assert applied2 == ["t.py"]
    assert "fence_invalid_json_applied" in extras2
    assert "0 命中" in extras2["fence_invalid_json_applied"]
    assert (wd2 / "t.py").read_text(encoding="utf-8") == \
        "def real():\n    return 2\n"


def test_v14_mixed_channel_p1_path_escape_instant_death(tmp_path):
    # 三签 MAJOR-2：混出通道 JSON 侧 P1 路径越界仍是唯一 instant-death——
    # except 首行 type-is 精确匹配裸 PatchSafetyError 族直接 re-raise，
    # 不得洗成围栏+JSON 组合反馈轮（§3 P1「围栏或 JSON…不变」）。
    wd = _v14_wd(tmp_path, "def real():\n    return 1\n")
    raw = (
        _v14_fence("t.py", "ghost", "def ghost():\n    return 0") + "\n"
        '{"patches": [{"path": "../evil.py", "old": "a", "new": "b"}]}')
    with pytest.raises(PatchSafetyError) as ei:
        _parse_replace_and_apply(wd, raw)
    # 裸安全异常本体：绝不包装成 transport/anchor（type-is 透传依赖此形）
    assert type(ei.value) is PatchSafetyError
    assert not isinstance(ei.value, PatchTransportError)
    assert not isinstance(ei.value, PatchAnchorError)
    assert (wd / "t.py").read_text(encoding="utf-8") == \
        "def real():\n    return 1\n"


# ---- P1 路径越界：唯一 instant-death（test_02 run() 面不动） ----------------

def test_v14_p1_path_escape_instant_death(tmp_path):
    wd = _v14_wd(tmp_path, "def add():\n    return 1\n")
    raw = _v14_fence("../evil.py", "add", "def add():\n    return 2")
    with pytest.raises(PatchSafetyError) as ei:
        _parse_replace_and_apply(wd, raw)
    # 裸安全异常：绝不包装成 transport/anchor（run() 裸 except 硬停依赖此形）
    assert not isinstance(ei.value, PatchTransportError)
    assert not isinstance(ei.value, PatchAnchorError)


# ---- 异常分类学（§4 N4 钉死） ----------------------------------------------

def test_v14_transport_error_taxonomy():
    assert issubclass(PatchTransportError, PatchSafetyError)
    assert not issubclass(PatchTransportError, PatchAnchorError)
    assert not issubclass(PatchAnchorError, PatchTransportError)
    exc = PatchTransportError("x")
    assert isinstance(exc, PatchSafetyError)
    assert not isinstance(exc, PatchAnchorError)  # a6⑤ 同款：平级非子类


# ---- transport 注记/幂等/帽文案（§2.4） ------------------------------------

def test_v14_transport_note_and_cap_reason():
    # 注记 3 物理行；reason 经换行中和独占首行；{"patches":[...]} 字面保留
    note = _transport_note(PatchTransportError("行甲\n行乙"))
    lines = note.splitlines()
    assert len(lines) == 3
    assert lines[0] == \
        "机器核验（非模型输出）：上一轮未产出可应用补丁（机器原因：行甲 行乙）。"
    assert '{"patches":[...]}' in note
    assert "simpraxis.replace.v1 围栏整替函数" in note
    # 幂等注入（节点重放不重复）；append 恒加两个 LF 分隔（tail 原样保留）
    assert _append_transport_feedback(note, note) == note
    assert _append_transport_feedback("tail\n", note) == "tail\n" + "\n\n" + note
    # 帽文案：基串逐字；锚点后缀优先互斥；transport 后缀仅无 anchor 时
    spec = RunSpec(
        thread_id="t", workdir=Path("/tmp/x"), cmd=["x"],
        telemetry_relpath="t.json", batch_id="B", billing_task=6,
        run_idx=1, max_coding_rounds=2)
    base = "2 轮 coding_debug 后仍失败（RunSpec 上限）"
    assert _coding_cap_reason(spec) == base
    anchor = PatchAnchorError("锚甲", hits=0)
    transport = PatchTransportError("传甲")
    assert _coding_cap_reason(spec, anchor, transport_exc=transport) \
        == f"{base}；末次锚点机器原因：锚甲"
    assert _coding_cap_reason(spec, transport_exc=transport) \
        == f"{base}；末次补丁传输机器原因：传甲"


# ---- J9：零花括号散文反馈续跑 + 帽尽 transport _fail（§3/J9） --------------

def test_v14_j9_prose_feedback_continue(tmp_path):
    # J9 三形态之一（零花括号散文；旧实现图层逃逸不经 decision/不耗帽）
    good = _run7b_structured(_run7b_patch("fix.txt", "NEEDS_FIX", "PATCHED"))
    runner, spec, gw, wd, _ = _run7b_setup(
        tmp_path, ["这不是 JSON 也不是围栏，抱歉。", good],
        thread="t7b-v14j9")
    rec = runner.run(spec)

    assert rec["status"] == "ok" and len(gw.calls) == 2
    doc1 = _run7b_decision(tmp_path, "t7b-v14j9", 1)
    assert doc1["transport"] is True and doc1["applied"] == []
    assert "JSONDecodeError" in doc1["rejected"]
    tr = [t for t in rec["timeline"] if "patch_transport_rejected" in t]
    assert len(tr) == 1 and tr[0]["coding_round"] == 1
    # 次轮模型消息：transport 标记 + 围栏通道指引；0 帧命中不注入函数源码段
    msg2 = gw.calls[1]["messages"][1]["content"]
    assert "上一轮未产出可应用补丁" in msg2
    assert "simpraxis.replace.v1" in msg2
    assert "涉案函数当前源码" not in msg2
    assert (wd / "fix.txt").read_text(encoding="utf-8") == "PATCHED"


def test_v14_j9_cap_exhaust_transport_suffix(tmp_path):
    # 帽尽（cap=1 单坏轮）：_fail 携末次传输机器原因，非 P1「补丁安全拒绝」
    runner, spec, gw, wd, _ = _run7b_setup(
        tmp_path, ["抱歉，我不能完成。"], thread="t7b-v14j9cap", cap=1)
    rec = runner.run(spec)

    assert rec["status"] == "failed" and len(gw.calls) == 1
    assert rec["executions"] == 1
    assert "末次补丁传输机器原因" in rec["reason"]
    assert "补丁安全拒绝（不重试）" not in rec["reason"]
    assert "JSONDecodeError" in rec["reason"]
    assert (wd / "FAILURE.json").is_file()
    assert _run7b_decision(tmp_path, "t7b-v14j9cap", 1)["transport"] is True


# ---- §2.3 函数源码注入：启发 + 8192 预算（纯函数） -------------------------

def test_v14_collect_repair_functions(tmp_path):
    wd = tmp_path / "src"
    wd.mkdir()
    (wd / "sim.py").write_text(
        "def calc_slip(params):\n"
        "    return params['slip_dist_m_R']\n"
        "\n"
        "def unrelated(a):\n"
        "    return a\n", encoding="utf-8")
    # traceback 帧函数名命中（<module> 不匹配帧形）
    seg = collect_repair_functions(
        wd, traceback_tail='File "sim.py", line 2, in calc_slip')
    assert "## 涉案函数当前源码" in seg
    assert "### sim.py :: calc_slip" in seg and "def calc_slip" in seg
    assert "unrelated" not in seg
    # Constant 字符串前后缀变体命中（字段 slip_dist_m_R ⊂ 常量，len≥3 双端）
    seg2 = collect_repair_functions(
        wd, schema_lines=["normal: slip_dist_m_R 必须为数值"])
    assert "calc_slip" in seg2 and "unrelated" not in seg2
    # 0 命中 → 空串（不新增报错面）
    assert collect_repair_functions(wd) == ""
    assert collect_repair_functions(
        wd, schema_lines=["normal: other_field 必须为数值"]) == ""


def test_v14_collect_repair_functions_budget(tmp_path):
    wd = tmp_path / "src2"
    wd.mkdir()
    big = ("def big_fn():\n"
           + "".join(f"    v{i} = {i}\n" for i in range(900))
           + "    return 0\n")
    (wd / "big.py").write_text(big, encoding="utf-8")
    assert len(big) > FUNCTION_SOURCE_TOTAL_CHARS   # 夹具自证超预算
    (wd / "small.py").write_text(
        "def small_fn():\n    return 1\n", encoding="utf-8")
    # 恰 1 命中且超预算：跳过 + 注记（整函数注入禁止截断）
    seg_big = collect_repair_functions(
        wd, traceback_tail='File "big.py", line 1, in big_fn')
    assert "函数过大未注入" in seg_big and "v0 = 0" not in seg_big
    # 两命中合计超预算：只注入较小者（行数升序，小者优先；无注记）
    seg_both = collect_repair_functions(
        wd, traceback_tail=('File "big.py", line 1, in big_fn\n'
                            'File "small.py", line 1, in small_fn'))
    assert "### small.py :: small_fn" in seg_both
    assert "big_fn" not in seg_both
    assert "函数过大未注入" not in seg_both
    # 两小者合计在预算内：双双注入（行数并列时 relpath 稳定序）
    (wd / "a.py").write_text("def fa():\n    return 1\n", encoding="utf-8")
    (wd / "b.py").write_text("def fb():\n    return 2\n", encoding="utf-8")
    seg_ab = collect_repair_functions(
        wd, traceback_tail=('File "a.py", line 1, in fa\n'
                            'File "b.py", line 1, in fb'))
    assert "### a.py :: fa" in seg_ab and "### b.py :: fb" in seg_ab


def test_v14_function_source_injection_e2e(tmp_path):
    # run() 装配点（Option B gate：rc0→anchor→schema→transport→函数源码→
    # redact）：锚点反馈轮 gate 开 → 帧命中函数源码段进次轮模型消息；
    # 无反馈的 round1 gate 关 → 不注入。
    v2 = "def broken_fn():\n    raise RuntimeError('boom')\n"
    r1 = _run7b_structured(
        _run7b_patch("fix.txt", "NO_SUCH_ANCHOR_XYZ", "y"))
    r2 = _run7b_structured(_run7b_patch("fix.txt", "NEEDS_FIX", "PATCHED"))
    # 收敛脚本：恒写帧行（供启发命中），fix.txt==PATCHED 时写产物 rc=0
    script = ('import sys\n'
              'from pathlib import Path\n'
              'sys.stderr.write(\'File "sim.py", line 2, in broken_fn\\n\')\n'
              'wd = Path(__file__).parent\n'
              'if (wd / "fix.txt").read_text().strip() == "PATCHED":\n'
              '    (wd / "telemetry.json").write_text(\'{"_fixture": true}\')\n'
              '    sys.exit(0)\n'
              'sys.exit(3)\n')
    runner, spec, gw, wd, _ = _run7b_setup(
        tmp_path, [r1, r2], thread="t7b-v14inj",
        files={"sim.py": v2}, script=script)
    rec = runner.run(spec)

    assert rec["status"] == "ok" and len(gw.calls) == 2
    msg1 = gw.calls[0]["messages"][1]["content"]
    assert "涉案函数当前源码" not in msg1
    msg2 = gw.calls[1]["messages"][1]["content"]
    assert "## 涉案函数当前源码（机器注入，供整替参考）" in msg2
    assert "### sim.py :: broken_fn" in msg2
    assert "def broken_fn():" in msg2
    assert (wd / "fix.txt").read_text(encoding="utf-8") == "PATCHED"


# ---- transport probe 纯函数面（§2.6；永不走 main/真实 gateway） -----------

def test_v14_transport_probe_pure(tmp_path):
    from orchestrator.agent_runner import transport_probe as tp
    # build_probe_messages：两件套；system 强制 class=；user 带玩具契约。
    # 仪器保真钉（seq=121 FAIL 后）：system 必须逐字含字面 begin 前缀
    # `<<<<<<< simpraxis.replace.v1 `（原措辞缺前缀致模型拆行、fence_parse
    # 假阴——生产修复轮示例同款前缀），防止措辞回退。
    msgs = tp.build_probe_messages()
    assert [m["role"] for m in msgs] == ["system", "user"]
    assert "<<<<<<< simpraxis.replace.v1 path=" in msgs[0]["content"]
    assert "class=" in msgs[0]["content"] and "class=" in msgs[1]["content"]
    assert tp.TOY_FILENAME in msgs[1]["content"]
    # 加固：示例 begin 行本体须过生产 _REPLACE_BEGIN_RE（三组捕获齐备）——
    # 字面子串钉不住措辞漂移出解析器口径（critic 复审 Open Q3）。
    from orchestrator.agent_runner.runner import _REPLACE_BEGIN_RE
    begin_line = next(ln for ln in tp.PROBE_SYSTEM.split("\n")
                      if ln.startswith("<<<<<<< "))
    bm = _REPLACE_BEGIN_RE.match(begin_line)
    assert bm is not None and bm.group(1) and bm.group(2) and bm.group(3)
    # _cache_key：前缀 + 键正则 ^[A-Za-z0-9_.-]{1,160}$（模型名白名单化）
    key = tp._cache_key("mimo-v2.6-flash/坏 字符", "[]")
    assert key.startswith("transport_probe_")
    assert re.fullmatch(r"[A-Za-z0-9_.-]{1,160}", key)
    # judge_probe 四阶段（tmp workdir 放玩具源；不触网不计费）
    wd = tmp_path / "probe"
    wd.mkdir()
    (wd / tp.TOY_FILENAME).write_text(tp.TOY_SOURCE, encoding="utf-8")
    good = _v14_fence(
        tp.TOY_FILENAME, "add",
        "@staticmethod\ndef add(a, b):\n    return a + b", cls="Calc")
    v = tp.judge_probe(good, wd)
    assert v["pass"] is True and v["stage"] == "ok"
    # fence_parse：无围栏
    v2 = tp.judge_probe("抱歉", wd)
    assert v2["pass"] is False and v2["stage"] == "fence_parse"
    # class_usage：缺 class=（玩具契约强制；发生在 apply 之前）
    v3 = tp.judge_probe(good.replace(" class=Calc", ""), wd)
    assert v3["pass"] is False and v3["stage"] == "class_usage"
    # anchor_or_apply：func 锚不在（class=Calc 给定 → func_not_in_class）
    v4 = tp.judge_probe(good.replace("func=add", "func=ghost"), wd)
    assert v4["pass"] is False and v4["stage"] == "anchor_or_apply"
    # toy_check：应用成功但 bug 未修 → check() 断言败
    wd2 = tmp_path / "probe2"
    wd2.mkdir()
    (wd2 / tp.TOY_FILENAME).write_text(tp.TOY_SOURCE, encoding="utf-8")
    noop = _v14_fence(
        tp.TOY_FILENAME, "add",
        "@staticmethod\ndef add(a, b):\n    return a - b", cls="Calc")
    v5 = tp.judge_probe(noop, wd2)
    assert v5["pass"] is False and v5["stage"] == "toy_check"


def test_v14_transport_probe_run_hermetic(tmp_path):
    from orchestrator.agent_runner import transport_probe as tp
    good = _v14_fence(
        tp.TOY_FILENAME, "add",
        "@staticmethod\ndef add(a, b):\n    return a + b", cls="Calc")
    gw = FakeGateway([good], usage=(10, 5))
    cache = CallReplay(tmp_path / "cache")
    ev = tmp_path / "ev"
    r1 = tp.run_probe(
        gateway=gw, batch_id="T6-PROBE-FIX", thread_id="hermetic",
        evidence_dir=ev, workdir=tmp_path / "wd", call_cache=cache)
    assert r1["status"] == "pass" and r1["judge"]["stage"] == "ok"
    assert r1["cache_hit"] is False and len(gw.calls) == 1
    assert Path(r1["evidence"]).is_file()
    # 模型未变重跑：缓存命中 0 触网 0 记账（FakeGateway 响应已耗尽仍 pass）
    r2 = tp.run_probe(
        gateway=gw, batch_id="T6-PROBE-FIX", thread_id="hermetic",
        evidence_dir=ev, workdir=tmp_path / "wd2", call_cache=cache)
    assert r2["status"] == "pass" and r2["cache_hit"] is True
    assert len(gw.calls) == 1
    # FAIL：坏响应 → fail + 判定阶段件 + 诚实声明在场（不升格引用）
    gw_bad = FakeGateway(["不是围栏"], usage=(1, 1))
    r3 = tp.run_probe(
        gateway=gw_bad, batch_id="T6-PROBE-FIX", thread_id="hermetic-fail",
        evidence_dir=ev, workdir=tmp_path / "wd3",
        call_cache=CallReplay(tmp_path / "cache3"))
    assert r3["status"] == "fail" and r3["judge"]["stage"] == "fence_parse"
    assert "玩具契约" in r3["honesty"]


# ---------------------------------------------------------------------------
# 攢批94（2026-10-27 Task8 流体锁内适配）：coding 站增强召回
# ---------------------------------------------------------------------------

class _Z94Vector:
    """按 namespace 应答的合成向量库（0 触网），记录全部检索参数。"""

    def __init__(self):
        self.queries: list[dict] = []

    def query_kb(self, query, *, namespace, top_k):
        self.queries.append({"q": query, "namespace": namespace,
                             "top_k": top_k})
        if namespace == "capabilities":
            return [{"text": "夹具 API 金块：gs.materials.SPH.Liquid(...)",
                     "evidence_refs": ["Z94"],
                     "kb_relpath":
                         "knowledge/projects/genesis_world/code_knowledge.md",
                     "section_path": "3.5"} for _ in range(top_k)]
        return []


def test_z94_clean_coding_hint_signature_and_noise():
    """错误签名清洗：torch/litellm 警告与 Traceback 帧行剥离，异常行保留。"""
    tail = (
        '  warnings.warn("torch<2.8.0 is not supported. Please upgrade '
        'pytorch manually")\n'
        "LiteLLM:WARNING register_model: model=x not in built-in cost map\n"
        "Traceback (most recent call last):\n"
        '  File "/workspace/genesis_fluid_pour.py", line 210, in <module>\n'
        "genesis.utils.exceptions.GenesisException: Scene is already built. "
        "Entities must be added before scene.build()\n")
    hint = _clean_coding_hint(tail)
    assert hint.startswith("仿真失败 ")
    assert "Scene is already built" in hint
    assert "torch" not in hint and "LiteLLM" not in hint
    assert "Traceback" not in hint and "File " not in hint
    assert len(hint) <= 300


def test_z94_clean_coding_hint_unwraps_quoted_message():
    """GenesisException 元组形：取引号内真实错误消息。"""
    tail = ("FAILURE: artifact_or_initialization_failure: "
            "('liquid entity was already created', \"context: {'oom': False}\")\n")
    hint = _clean_coding_hint(tail)
    assert hint == "仿真失败 liquid entity was already created"


def test_z94_clean_coding_hint_fallback_when_only_noise():
    """全是噪声/空 tail：退回旧形态切片（fail-safe，永不返回空串）。"""
    assert _clean_coding_hint("\n") == "仿真失败 \n"
    hint = _clean_coding_hint("LiteLLM:WARNING cost map fields default to 0")
    assert hint.startswith("仿真失败 ") and hint


def _z94_runner_spec(tmp_path, *, recall):
    wd = tmp_path / "work"
    wd.mkdir(parents=True, exist_ok=True)
    # stderr 先吐 torch 警告再吐真实异常行（镜像 run7 真实 tail 结构）。
    (wd / "script.py").write_text(
        "import sys\n"
        "sys.stderr.write('  warnings.warn(\"torch<2.8.0 please upgrade "
        "pytorch manually\")\\n')\n"
        "sys.stderr.write('GenesisException: Scene is already built.\\n')\n"
        "sys.exit(1)\n", encoding="utf-8")
    patch = json.dumps({"patches": [{
        "path": "script.py", "old": "sys.exit(1)",
        "new": "sys.exit(1)  # z94 round1"}]})
    gw = FakeGateway([patch])
    vec = _Z94Vector()
    runner = AgentRunner(gw, vec, evidence_root=tmp_path / "ev")
    spec = RunSpec(
        thread_id="z94fluid", workdir=wd,
        cmd=[sys.executable, str(wd / "script.py")],
        telemetry_relpath="results.json", batch_id="T8-Z94",
        billing_task=8, run_idx=1, max_coding_rounds=1,
        coding_recall=recall)
    return runner, spec, gw, vec


def test_z94_enhanced_recall_queries_and_prompt_section(tmp_path):
    """CodingRecallSpec 开启：troubleshooting 吃清洗串 + capabilities 额外路，
    金块以独立段进 prompt（C 编号），troubleshooting 旧段保留。"""
    runner, spec, gw, vec = _z94_runner_spec(
        tmp_path,
        recall=CodingRecallSpec(
            clean_troubleshooting_hint=True,
            extra_kb=(("capabilities", 3, "z94 冻结查询串 SPH.Liquid"),)))
    rec = runner.run(spec)
    assert rec["status"] == "failed" and len(gw.calls) == 1

    ns_queries = {(q["namespace"], q["top_k"]): q["q"] for q in vec.queries}
    ts_q = ns_queries[("troubleshooting", 6)]
    assert ts_q.startswith("仿真失败 ")
    assert "Scene is already built" in ts_q
    assert "torch" not in ts_q                      # 警告噪声已剥离
    assert ns_queries[("capabilities", 3)] == "z94 冻结查询串 SPH.Liquid"

    user_msg = gw.calls[0]["messages"][1]["content"]
    assert "## troubleshooting 知识库命中（namespaces=troubleshooting, " \
        "top_k=6）" in user_msg
    assert "## capabilities 知识库命中（API 参考；namespace=capabilities, " \
        "top_k=3）" in user_msg
    assert "[C1 来源:" in user_msg
    assert "夹具 API 金块：gs.materials.SPH.Liquid" in user_msg
    p_ts = user_msg.index("## troubleshooting")
    p_cap = user_msg.index("## capabilities")
    assert p_ts < p_cap                            # 段次序钉死


def test_z94_default_off_byte_identical_path(tmp_path):
    """coding_recall=None（Task6/7 与全部既有任务）：只有一条 troubleshooting
    旧查询、无 capabilities 调用、prompt 无新段。"""
    runner, spec, gw, vec = _z94_runner_spec(tmp_path, recall=None)
    rec = runner.run(spec)
    assert rec["status"] == "failed"
    assert [q["namespace"] for q in vec.queries] == ["troubleshooting"]
    assert vec.queries[0]["q"] == "仿真失败 " + \
        rec["timeline"][0]["stderr_tail"][-500:]
    user_msg = gw.calls[0]["messages"][1]["content"]
    assert "capabilities 知识库命中" not in user_msg


def test_z94_failed_seam_record_carries_timeline(tmp_path):
    """失败 seam 记录回灌 timeline/executions（learning._fix_actions 不再恒无）。"""
    runner, spec, gw, _ = _z94_runner_spec(tmp_path, recall=None)
    seam = agent_run_seam(runner, lambda state: spec)
    record = seam({})
    assert record["status"] == "failed"
    assert isinstance(record["timeline"], list) and record["timeline"]
    # 初始 exec + 补丁后再 exec（帽 1）→ 2 条时间线
    assert record["executions"] == len(record["timeline"]) == 2
    assert record["failure_json"] and record["reason"]


# ---------------------------------------------------------------------------
# 攢批95：修复轮全文可见性（Task8 流体 run8 实测墙；锁内适配 2026-10-27）
# ---------------------------------------------------------------------------

def test_z95_collect_full_source_dedup_order_and_listing(tmp_path):
    """涉案 .py 按 snippet 首现序去重、逐行号全文、非 .py 不枚举。"""
    wd = tmp_path / "work"
    wd.mkdir()
    (wd / "a.py").write_text("a1\na2\na3\n", encoding="utf-8")
    (wd / "b.py").write_text("b1\nb2\n", encoding="utf-8")
    (wd / "notes.txt").write_text("x\n", encoding="utf-8")
    snippets = [
        {"file": "a.py", "line": 2, "snippet": "..."},
        {"file": "a.py", "line": 3, "snippet": "..."},   # 同件去重
        {"file": "notes.txt", "line": 1, "snippet": "..."},  # 非 py 跳过
        {"file": "b.py", "line": 1, "snippet": "..."},
    ]
    files = _collect_full_source(wd, snippets)
    assert [f["file"] for f in files] == ["a.py", "b.py"]
    assert files[0]["n_lines"] == 3 and files[1]["n_lines"] == 2
    assert files[0]["listing"].splitlines()[1].startswith("    2| a2")
    assert all(f["skipped"] is None for f in files)


def test_z95_collect_full_source_budget_skips_with_never_half_file(tmp_path,
                                                                   monkeypatch):
    """超单文件预算跳过并注记（绝不注入半文件）；超总预算截断后续。"""
    wd = tmp_path / "work"
    wd.mkdir()
    (wd / "big.py").write_text("x = 1\n" * 20, encoding="utf-8")
    monkeypatch.setattr(
        "orchestrator.agent_runner.runner.FULL_SOURCE_PER_FILE_CHARS", 30)
    files = _collect_full_source(wd, [{"file": "big.py", "line": 1}])
    assert files[0]["listing"] is None and "预算" in files[0]["skipped"]

    (wd / "small.py").write_text("s\n", encoding="utf-8")
    monkeypatch.setattr(
        "orchestrator.agent_runner.runner.FULL_SOURCE_PER_FILE_CHARS",
        FULL_SOURCE_PER_FILE_CHARS)
    monkeypatch.setattr(
        "orchestrator.agent_runner.runner.FULL_SOURCE_TOTAL_CHARS", 20)
    files = _collect_full_source(
        wd, [{"file": "small.py", "line": 1},
             {"file": "big.py", "line": 1}])
    # small（9 字符）完整注入后总预算耗尽，big 必须整条跳过而非半截。
    assert files[0]["file"] == "small.py" and files[0]["listing"] == "    1| s"
    assert files[1]["file"] == "big.py" and files[1]["listing"] is None
    assert "总预算" in files[1]["skipped"]


def test_z95_build_messages_section_position_and_default_absent():
    """全文段在帧片段之后、troubleshooting 之前；默认 None 时段不存在。"""
    base = dict(gate2_notes=None, traceback_tail="boom",
                snippets=[{"file": "s.py", "line": 1, "snippet": "1| x"}],
                hits=[], cmd=["python", "s.py"])
    msgs_off = _build_messages(**base)
    assert "目标脚本全文" not in msgs_off[1]["content"]

    files = [{"file": "s.py", "n_lines": 3,
              "listing": "    1| a\n    2| HIDDEN_LINE_Z95\n    3| c",
              "skipped": None}]
    msgs_on = _build_messages(**base, full_source_files=files)
    content = msgs_on[1]["content"]
    assert "## 运行工作目录目标脚本全文" in content
    assert "HIDDEN_LINE_Z95" in content
    p_snip = content.index("## 相关源文件片段")
    p_full = content.index("## 运行工作目录目标脚本全文")
    p_ts = content.index("## troubleshooting")
    assert p_snip < p_full < p_ts
    # None 与默认调用逐字节一致。
    assert _build_messages(**base, full_source_files=None)[1]["content"] == \
        msgs_off[1]["content"]


def test_z95_runner_injects_full_source_when_flag_on(tmp_path):
    """端到端：repair_full_source=True 时帧窗口外的行也进 prompt；False 不进。"""
    def _make_runner(flag: bool):
        wd = tmp_path / f"work_{flag}"
        wd.mkdir(parents=True)
        body = ["import sys",
                "MARKER_Z95 = 'z95-line-beyond-frame-window'",  # 行 2
                "sys.stderr.write('noise\\n')"]
        body += [f"_pad_{i} = {i}" for i in range(20)]      # 行 4..23
        body.append("raise RuntimeError('z95 boom')")       # 行 24
        (wd / "script.py").write_text("\n".join(body) + "\n", encoding="utf-8")
        patch = json.dumps({"patches": [{
            "path": "script.py",
            "old": "raise RuntimeError('z95 boom')",
            "new": "raise RuntimeError('z95 boom round1')"}]})
        gw = FakeGateway([patch])
        runner = AgentRunner(gw, _Z94Vector(), evidence_root=tmp_path / "ev")
        spec = RunSpec(
            thread_id=f"z95{flag}", workdir=wd,
            cmd=[sys.executable, str(wd / "script.py")],
            telemetry_relpath="results.json", batch_id="T8-Z95",
            billing_task=8, run_idx=1, max_coding_rounds=1,
            repair_full_source=flag)
        return runner.run(spec), gw

    rec_on, gw_on = _make_runner(True)
    assert rec_on["status"] == "failed" and len(gw_on.calls) == 1
    user_on = gw_on.calls[0]["messages"][1]["content"]
    assert "## 运行工作目录目标脚本全文（修复前当前版本" in user_on
    assert "z95-line-beyond-frame-window" in user_on       # 帧窗外的行可见

    rec_off, gw_off = _make_runner(False)
    user_off = gw_off.calls[0]["messages"][1]["content"]
    assert "目标脚本全文" not in user_off
    assert "z95-line-beyond-frame-window" not in user_off


# ---------------------------------------------------------------------------
# 攢批97：跨 workdir 上一 run 只读参考注入（Task8 流体 run_2 信息墙）
# ---------------------------------------------------------------------------

def test_z97_collect_prior_refs_renders_skips_and_bad_paths(tmp_path):
    prior = tmp_path / "run_1"
    prior.mkdir()
    (prior / "genesis_fluid_pour.py").write_text("p1\np2\n", encoding="utf-8")
    refs = [
        ("上一 run 最终脚本 genesis_fluid_pour.py", prior / "genesis_fluid_pour.py"),
        ("缺失件", prior / "missing.json"),
        ("相对路径件", Path("relative.py")),
    ]
    out = _collect_prior_refs(tuple(refs))
    assert [r["file"] for r in out] == [refs[0][0], refs[1][0], refs[2][0]]
    assert out[0]["n_lines"] == 2
    assert out[0]["listing"].splitlines()[1].startswith("    2| p2")
    assert out[1]["listing"] is None and "不存在" in out[1]["skipped"]
    assert out[2]["listing"] is None and "非绝对路径" in out[2]["skipped"]


def test_z97_collect_prior_refs_budget_never_half_file(tmp_path, monkeypatch):
    big = tmp_path / "big.py"
    small = tmp_path / "small.json"
    big.write_text("x = 1\n" * 20, encoding="utf-8")
    small.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(
        "orchestrator.agent_runner.runner.PRIOR_REF_TOTAL_CHARS", 10)
    refs = (("小遥测", small), ("大脚本", big))
    out = _collect_prior_refs(refs)
    # small 完整注入（4 字符）后总预算耗尽，big 整条跳过带注记而非半截。
    assert out[0]["listing"] is not None
    assert out[1]["listing"] is None and "总预算" in out[1]["skipped"]
    # 常量存在且与全文预算同量级（防误改成 0）。
    assert PRIOR_REF_PER_FILE_CHARS == FULL_SOURCE_PER_FILE_CHARS
    assert PRIOR_REF_TOTAL_CHARS == FULL_SOURCE_TOTAL_CHARS


def test_z97_build_messages_section_position_and_default_absent():
    """参考段在当前 workdir 全文之后、troubleshooting 之前；默认段不存在。"""
    base = dict(gate2_notes=None, traceback_tail="boom",
                snippets=[{"file": "s.py", "line": 1, "snippet": "1| x"}],
                hits=[], cmd=["python", "s.py"])
    msgs_off = _build_messages(**base)
    assert "同线程上一 run 参考产物" not in msgs_off[1]["content"]

    cur = [{"file": "s.py", "n_lines": 1, "listing": "    1| x",
            "skipped": None}]
    refs = [{"file": "上一 run 最终脚本 genesis_fluid_pour.py（5 行）",
             "n_lines": 5,
             "listing": "    1| PRIOR_LINE_Z97", "skipped": None}]
    content = _build_messages(**base, full_source_files=cur,
                              prior_refs=refs)[1]["content"]
    p_snip = content.index("## 相关源文件片段")
    p_full = content.index("## 运行工作目录目标脚本全文")
    p_prior = content.index("## 同线程上一 run 参考产物")
    p_ts = content.index("## troubleshooting")
    assert p_snip < p_full < p_prior < p_ts
    assert "PRIOR_LINE_Z97" in content
    assert "补丁" in content and "严禁指向这些路径" in content
    # 显式 None 与默认调用逐字节一致。
    assert _build_messages(**base, prior_refs=None)[1]["content"] == \
        msgs_off[1]["content"]

# ---------------------------------------------------------------------------
# 攢批100（Task8 fluid run_4，2026-10-28）：replace 围栏现存锚点白名单
# ---------------------------------------------------------------------------

def test_z100_replace_anchor_manifest_module_and_methods():
    """ast 抽取模块级函数与类方法；无定义返回 None。"""
    from orchestrator.agent_runner.runner import _replace_anchor_manifest
    src = (
        "import os\n"
        "def alpha():\n    return 1\n"
        "async def beta(x):\n    return x\n"
        "class Calc:\n"
        "    def m1(self):\n        return 1\n"
        "    def m2(self):\n        return 2\n"
        "top = 3\n")
    out = _replace_anchor_manifest(src)
    assert "模块级函数 alpha、beta" in out
    assert "类 Calc 的方法 m1/m2（func=方法名 class=Calc）" in out
    assert _replace_anchor_manifest("x = 1\n") is None


def test_z100_replace_anchor_manifest_syntax_error_fallback():
    """ast 不可解析时退回顶层正则，只抓模块级 def。"""
    from orchestrator.agent_runner.runner import _replace_anchor_manifest
    out = _replace_anchor_manifest(
        "def real_one():\n    pass\n"
        "def broken(:\n"
        "    class Inner:\n        def hidden(self):\n            pass\n")
    assert "real_one" in out and "broken" in out
    assert "hidden" not in out  # 退回正则不猜类方法


def test_z100_collect_full_source_attaches_anchor_whitelist(tmp_path):
    """端到端：collector 给成功注入件带 anchors；渲染进全文段且指向异名警告。"""
    wd = tmp_path / "work"
    wd.mkdir()
    (wd / "a.py").write_text(
        "def build_kitchen_scene():\n    return 1\n"
        "def vessel_pose_at(t):\n    return t\n", encoding="utf-8")
    files = _collect_full_source(wd, [{"file": "a.py", "line": 1}])
    assert files[0]["anchors"] is not None
    assert "build_kitchen_scene" in files[0]["anchors"]
    base = dict(gate2_notes=None, traceback_tail="boom",
                snippets=[{"file": "a.py", "line": 1, "snippet": "1| x"}],
                hits=[], cmd=["python", "a.py"])
    content = _build_messages(**base, full_source_files=files)[1]["content"]
    assert "replace 围栏锚点白名单" in content
    assert "build_kitchen_scene" in content and "vessel_pose_at" in content
    # run_4 实测墙：上一 run 参考脚本异名不是锚点。
    assert "上一 run 只读参考脚本里的函数名不是本文件锚点" in content
    # 手工构造无 anchors 的旧形态 dict：不渲染白名单句（向后兼容）。
    legacy = [{"file": "a.py", "n_lines": 2,
               "listing": "    1| x", "skipped": None}]
    legacy_text = _build_messages(**base, full_source_files=legacy)[1]["content"]
    assert "replace 围栏锚点白名单" not in legacy_text
