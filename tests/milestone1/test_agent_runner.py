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
from orchestrator.agent_runner.runner import (
    CHUNK_MAX_CODE_CHARS,
    CHUNK_MIN_ASSEMBLED_CHARS,
    CHUNK_MIN_CODE_CHARS,
    DEFAULT_CODING_TIMEOUT_S,
    MAX_TOKENS,
    TEMPERATURE,
    ChunkBirthError,
    ChunkPromptContext,
    ChunkSpec,
    _build_messages,
    _collect_snippets,
    _extract_chunk,
    _validate_assembled_chunks,
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
_BASELINE_SNAPSHOT = r'''[{"role": "system", "content": "你是具身智能仿真工具链的排障 coding agent。只允许输出严格 JSON：{\"patches\":[{\"path\":\"相对运行工作目录的路径\",\"old\":\"原文（须唯一）\",\"new\":\"替换文\"}]}，或 {\"unified_diff\":\"git 风格 diff\"}。补丁只能改本次运行工作目录内的文件；禁止改配置、凭据、仓库源码。不要输出解释、不要输出 image。"}, {"role": "user", "content": "## 失败命令\n/mnt/venvs/simpraxis/bin/python /work/run_1/script.py --flag 值\n\n## traceback / 日志尾部\nTraceback (most recent call last):\n  File \"script.py\", line 12\nRuntimeError: fixture failure: boom\n\n## 相关源文件片段（仅限运行工作目录）\n### sim_main.py:12\n    7| x = 1\n    8| y = 2\n    9| z = x + y\n   10| raise RuntimeError('boom')\n   11| \n   12| -> raise RuntimeError('boom')\n   13| \n   14| def f():\n   15|     pass\n   16| \n   17| return\n\n## troubleshooting 知识库命中（namespaces=troubleshooting, top_k=6）\n[S1 来源: knowledge/projects/fixture/troubleshooting.md#s0 | evidence_refs: E0]\n夹具排障知识条目 0（字段名必须是 text）\n\n[S2 来源: knowledge/projects/fixture/troubleshooting.md#s1 | evidence_refs: E1]\n夹具排障知识条目 1（字段名必须是 text）\n\n[S3 来源: knowledge/projects/fixture/troubleshooting.md#s2 | evidence_refs: E2]\n夹具排障知识条目 2（字段名必须是 text）\n\n[S4 来源: knowledge/projects/fixture/troubleshooting.md#s3 | evidence_refs: E3]\n夹具排障知识条目 3（字段名必须是 text）\n\n[S5 来源: knowledge/projects/fixture/troubleshooting.md#s4 | evidence_refs: E4]\n夹具排障知识条目 4（字段名必须是 text）\n\n[S6 来源: knowledge/projects/fixture/troubleshooting.md#s5 | evidence_refs: E5]\n夹具排障知识条目 5（字段名必须是 text）"}]
[{"role": "system", "content": "你是具身智能仿真工具链的排障 coding agent。只允许输出严格 JSON：{\"patches\":[{\"path\":\"相对运行工作目录的路径\",\"old\":\"原文（须唯一）\",\"new\":\"替换文\"}]}，或 {\"unified_diff\":\"git 风格 diff\"}。补丁只能改本次运行工作目录内的文件；禁止改配置、凭据、仓库源码。不要输出解释、不要输出 image。"}, {"role": "user", "content": "## 人工评审意见（必须逐条响应，最高优先级）\n人工意见：提高分辨率\n## 失败命令\n/mnt/venvs/simpraxis/bin/python /work/run_1/script.py --flag 值\n\n## traceback / 日志尾部\nTraceback (most recent call last):\n  File \"script.py\", line 12\nRuntimeError: fixture failure: boom\n\n## 相关源文件片段（仅限运行工作目录）\n### sim_main.py:12\n    7| x = 1\n    8| y = 2\n    9| z = x + y\n   10| raise RuntimeError('boom')\n   11| \n   12| -> raise RuntimeError('boom')\n   13| \n   14| def f():\n   15|     pass\n   16| \n   17| return\n\n## troubleshooting 知识库命中（namespaces=troubleshooting, top_k=6）\n[S1 来源: knowledge/projects/fixture/troubleshooting.md#s0 | evidence_refs: E0]\n夹具排障知识条目 0（字段名必须是 text）\n\n[S2 来源: knowledge/projects/fixture/troubleshooting.md#s1 | evidence_refs: E1]\n夹具排障知识条目 1（字段名必须是 text）\n\n[S3 来源: knowledge/projects/fixture/troubleshooting.md#s2 | evidence_refs: E2]\n夹具排障知识条目 2（字段名必须是 text）\n\n[S4 来源: knowledge/projects/fixture/troubleshooting.md#s3 | evidence_refs: E3]\n夹具排障知识条目 3（字段名必须是 text）\n\n[S5 来源: knowledge/projects/fixture/troubleshooting.md#s4 | evidence_refs: E4]\n夹具排障知识条目 4（字段名必须是 text）\n\n[S6 来源: knowledge/projects/fixture/troubleshooting.md#s5 | evidence_refs: E5]\n夹具排障知识条目 5（字段名必须是 text）"}]'''
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
    # task7/8 与 V1 修补轮看到的消息必须与改前逐字一致（整条 JSON 快照）。
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
        with pytest.raises(ChunkBirthError, match=needle):
            _extract_chunk(raw, target=_CHUNK_TARGET, index=i, total=6)

    # 长度窗边界（本机逐字符计数）
    assert _extract_chunk(_chunk_fence("#" * 800, 0),
                          target=_CHUNK_TARGET, index=0, total=6) == "#" * 800
    expect(_chunk_fence("#" * 799, 0), 0, "code 长度 799 < 800")
    assert _extract_chunk(_chunk_fence("#" * 18000, 0),
                          target=_CHUNK_TARGET, index=0, total=6) == "#" * 18000
    expect(_chunk_fence("#" * 18001, 0), 0,
           "18001 > 18000 chars（机器窗上限）")
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
    # 拼接总长边界 29999/30000（字符口径）
    s29999 = "x = 1\n" + "#" * (29999 - len("x = 1\n"))
    s30000 = "x = 1\n" + "#" * (30000 - len("x = 1\n"))
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
    c5 = ("def fixture_main():\n"
          "    Path('telemetry.json').write_text("
          "json.dumps({'_fixture': True}), encoding='utf-8')\n\n\n"
          "if __name__ == '__main__':\n    fixture_main()\n"
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
        {"max_tokens": 32768, "temperature": 0.1, "timeout": 600}] * 6
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
        "timeout": DEFAULT_CODING_TIMEOUT_S}
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
    fp = {i: request_fingerprint(
        "same-prompt", None,
        {"max_tokens": 32768, "temperature": 0.1,
         "chunk_index": i, "chunk_total": 6},
        stage="coding_debug", model="fake-model") for i in range(6)}
    assert len(set(fp.values())) == 6
    assert request_fingerprint(
        "same-prompt", None,
        {"max_tokens": 32768, "temperature": 0.1,
         "chunk_index": 1, "chunk_total": 6},
        stage="coding_debug", model="fake-model") == fp[1]
    import re
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
