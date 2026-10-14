#!/usr/bin/env python3
"""Task3 单测：LangGraph 双轨状态机（0 付费；gateway/feishu 全 fake）。

六个契约场景（计划 L492）：
  ① 批准主路径；② Gate1 打回 1 次后批准（direction 2 次、human_notes 入请求）；
  ③ Gate2 打回带意见 → agent_run 再跑且意见入 prompt；
  ④ 两次打回 → human_failure + FAILURE.json；
  ⑤ 真实 kill -9 重启演练（真实 pending_gates + 真实 sqlite 检查点）；
  ⑥ 图结构文本拓扑导出（不装系统包）。

No Fake Physics：agent 假件写出的 telemetry 数值全部由公式生成并带 _fixture
标记；evaluate 节点只从文件读数（file_backed_evaluate），节点代码无指标字面量。
"""
from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
from langgraph.checkpoint.sqlite import SqliteSaver  # noqa: E402

from orchestrator.feishu_gateway.store import PendingStore  # noqa: E402
from orchestrator.graph import (  # noqa: E402
    FAILURE_JSON,
    GraphDeps,
    TaskSeamError,
    build_graph,
    file_backed_evaluate,
    initial_state,
)
from orchestrator.graph.nodes import route_track  # noqa: E402
from orchestrator.graph.topology import export_text  # noqa: E402

EVIDENCE_DIR = Path("/mnt/artifacts/simpraxis/phase1/task3_graph")


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeDirection:
    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, state: dict) -> dict:
        notes = ""
        for ev in reversed(state.get("events", [])):
            if ev.get("type") == "gate1_rejected":
                notes = ev["human_notes"]
                break
        self.calls.append({
            "rejects": state.get("gate1_rejects", 0),
            "notes": notes,
            "prev": [p["name"] for p in state.get("proposals", [])],
        })
        return {"proposals": [{"name": "P0"}, {"name": "P1"}],
                "question": "q",
                "gate_round": state.get("gate1_rejects", 0)}


class ScriptedGate:
    def __init__(self, script: list[dict]):
        self.script = list(script)
        self.calls: list[dict] = []

    def __call__(self, state: dict) -> dict:
        self.calls.append({
            "n_proposals": len(state.get("proposals", [])),
            "has_metrics": bool(state.get("metrics")),
        })
        return dict(self.script.pop(0))


class FakeAgent:
    """写公式生成的 telemetry 夹具文件，并记录 Gate2 意见是否到达 prompt 侧。"""

    def __init__(self, root: Path):
        self.root = root
        self.calls: list[dict] = []

    def __call__(self, state: dict) -> dict:
        notes = ""
        for ev in reversed(state.get("events", [])):
            if ev.get("type") == "gate2_rejected":
                notes = ev["human_notes"]
                break
        n = len(self.calls) + 1
        t = 8
        tele = {
            "_fixture": True,
            "_formula": "penetration_m=0.001*m; force_k=1.0+0.1*sin(k)",
            "engine": "fixture-engine", "run_id": f"fixture-run-{n}",
            "dt": 0.016,
            "penetrations": [0.001 * m for m in range(t)],
            "contact_forces": [1.0 + 0.1 * (((k - 3) % 3) - 1)
                               for k in range(t)],
        }
        path = self.root / f"telemetry_{n}.json"
        path.write_text(json.dumps(tele), encoding="utf-8")
        self.calls.append({"notes": notes, "uri": str(path)})
        return {"telemetry_uri": str(path), "coding_round": 0}


class FakePersist:
    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, state: dict) -> dict:
        self.calls.append({"thread_id": state["thread_id"],
                           "chosen": state.get("chosen", {}).get("name")})
        return {"written": 1}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMPRAXIS_PHASE1_ROOT", str(tmp_path))
    return tmp_path


def _deps(tmp_path, gate1_script, gate2_script=None):
    fd, fa, fp = FakeDirection(), FakeAgent(tmp_path), FakePersist()
    deps = GraphDeps(
        direction=fd,
        gate1=ScriptedGate(gate1_script),
        agent_run=fa,
        evaluate=file_backed_evaluate,
        gate2=ScriptedGate(gate2_script or
                           [{"decision": "approve", "choice": None,
                             "human_notes": ""}]),
        persist=fp,
        run_dir=tmp_path / "runs",
    )
    return deps, fd, fa, fp


def _invoke(deps, thread="t1", track="track1"):
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    saver = SqliteSaver(conn)
    graph = build_graph(deps).compile(checkpointer=saver)
    cfg = {"configurable": {"thread_id": thread}, "recursion_limit": 25}
    return graph.invoke(
        initial_state(thread_id=thread, track=track, question="q"), config=cfg)


# ---------------------------------------------------------------------------
# ① 批准主路径
# ---------------------------------------------------------------------------

def test_01_approve_main_path(env):
    deps, fd, fa, fp = _deps(
        env, [{"decision": "approve", "choice": 1, "human_notes": ""}])
    final = _invoke(deps)
    kinds = [ev["type"] for ev in final["events"]]
    assert kinds == ["direction", "gate1_approved", "agent_run", "evaluate",
                     "gate2_accepted", "persist_learning", "done"]
    assert final["chosen"]["name"] == "P1"          # choice=1 生效
    assert final["metrics"]["provenance"] == "file:telemetry_uri"
    assert final["metrics"]["fixture"] is True       # 夹具自标
    assert final["metrics"]["data"]["penetrations"][3] == pytest.approx(0.003)
    assert len(fd.calls) == 1 and len(fa.calls) == 1 and len(fp.calls) == 1
    assert (env / "runs" / "DONE.json").is_file()
    assert not (env / "runs" / FAILURE_JSON).exists()


def test_01b_track2_also_enters_direction(env):
    assert route_track({"track": "track2"}) == "track2"
    deps, _, _, _ = _deps(
        env, [{"decision": "approve", "choice": 0, "human_notes": ""}])
    final = _invoke(deps, thread="t2", track="track2")
    assert final["track"] == "track2"
    assert any(ev["type"] == "done" for ev in final["events"])


def test_01c_seam_missing_raises_not_fake(env):
    """agent_run 接缝未注入时，真实走到必须硬失败（禁止伪造成功）。"""
    fd = FakeDirection()
    deps = GraphDeps(
        direction=fd,
        gate1=ScriptedGate([{"decision": "approve", "choice": 0,
                             "human_notes": ""}]),
        agent_run=None,
        run_dir=env / "runs")
    with pytest.raises(TaskSeamError, match="agent_run"):
        _invoke(deps)


# ---------------------------------------------------------------------------
# ② Gate1 打回 1 次后批准
# ---------------------------------------------------------------------------

def test_02_gate1_reject_once_then_approve(env):
    deps, fd, fa, _ = _deps(env, [
        {"decision": "reject_with_comments", "choice": None,
         "human_notes": "请逐条补充 CCD 证据"},
        {"decision": "approve", "choice": 0, "human_notes": ""},
    ])
    final = _invoke(deps)
    kinds = [ev["type"] for ev in final["events"]]
    assert kinds.count("direction") == 2
    assert kinds == ["direction", "gate1_rejected", "direction",
                     "gate1_approved", "agent_run", "evaluate",
                     "gate2_accepted", "persist_learning", "done"]
    assert len(fd.calls) == 2
    assert fd.calls[0]["notes"] == ""
    assert fd.calls[1]["notes"] == "请逐条补充 CCD 证据"  # 意见进下一轮请求
    assert fd.calls[1]["prev"] == ["P0", "P1"]            # 上一轮方案随附
    assert final["gate1_rejects"] == 1
    assert len(fa.calls) == 1


def test_02b_illegal_gate1_verdict_hard_errors(env):
    """未知决策值不得被默默当 reject（M2）：直接 GraphStateError，不重跑。"""
    from orchestrator.graph.nodes import GraphStateError
    deps, fd, _, _ = _deps(env, [
        {"decision": "approve_with_caveat", "choice": 0, "human_notes": ""}])
    with pytest.raises(GraphStateError, match="决策值非法"):
        _invoke(deps)
    assert len(fd.calls) == 1


# ---------------------------------------------------------------------------
# ③ Gate2 打回带意见 → agent_run 再跑，意见入 prompt
# ---------------------------------------------------------------------------

def test_03_gate2_reject_reruns_agent_with_notes(env):
    deps, fd, fa, _ = _deps(env, [
        {"decision": "approve", "choice": 0, "human_notes": ""},
    ], gate2_script=[
        {"decision": "reject_with_comments", "choice": None,
         "human_notes": "接触不稳，需提高 timestep 分辨率"},
        {"decision": "approve", "choice": None, "human_notes": ""},
    ])
    final = _invoke(deps)
    kinds = [ev["type"] for ev in final["events"]]
    assert kinds.count("agent_run") == 2 and kinds.count("evaluate") == 2
    assert kinds == ["direction", "gate1_approved",
                     "agent_run", "evaluate", "gate2_rejected",
                     "agent_run", "evaluate", "gate2_accepted",
                     "persist_learning", "done"]
    assert len(fa.calls) == 2
    assert fa.calls[0]["notes"] == ""
    assert fa.calls[1]["notes"] == "接触不稳，需提高 timestep 分辨率"
    assert final["gate2_rejects"] == 1
    assert final["gate1_rejects"] == 0
    assert (env / "runs" / "DONE.json").is_file()


# ---------------------------------------------------------------------------
# ④ 两次打回 → human_failure + FAILURE.json
# ---------------------------------------------------------------------------

def test_04_gate1_double_reject_human_failure(env):
    deps, fd, fa, _ = _deps(env, [
        {"decision": "reject_with_comments", "choice": None,
         "human_notes": "第一次打回"},
        {"decision": "reject_with_comments", "choice": None,
         "human_notes": "第二次打回"},
    ])
    final = _invoke(deps)
    kinds = [ev["type"] for ev in final["events"]]
    assert kinds == ["direction", "gate1_rejected", "direction",
                     "gate1_rejected", "human_failure"]
    assert len(fd.calls) == 2 and len(fa.calls) == 0
    failure = json.loads((env / "runs" / FAILURE_JSON).read_text())
    assert failure["status"] == "human_failure"
    assert failure["stage"] == "gate1"
    assert failure["gate1_rejects"] == 2
    assert not (env / "runs" / "DONE.json").exists()


def test_04b_gate2_double_reject_human_failure(env):
    deps, _, fa, _ = _deps(env, [
        {"decision": "approve", "choice": 0, "human_notes": ""},
    ], gate2_script=[
        {"decision": "reject_with_comments", "choice": None,
         "human_notes": "gate2 打回 A"},
        {"decision": "reject_with_comments", "choice": None,
         "human_notes": "gate2 打回 B"},
    ])
    final = _invoke(deps)
    kinds = [ev["type"] for ev in final["events"]]
    assert kinds[-1] == "human_failure"
    assert kinds.count("agent_run") == 2
    failure = json.loads((env / "runs" / FAILURE_JSON).read_text())
    assert failure["stage"] == "gate2" and failure["gate2_rejects"] == 2
    assert len(fa.calls) == 2


def test_04c_reject_without_notes_hard_failure(env):
    """空意见打回不白烧 macro：第一次即 human_failure（Task2 M2 守卫）。"""
    deps, fd, _, _ = _deps(env, [
        {"decision": "reject_with_comments", "choice": None,
         "human_notes": "   "},
    ])
    final = _invoke(deps)
    assert [ev["type"] for ev in final["events"]][-1] == "human_failure"
    assert len(fd.calls) == 1
    assert json.loads((env / "runs" / FAILURE_JSON).read_text())["stage"] == "gate1"


# ---------------------------------------------------------------------------
# ⑤ kill -9 重启演练（真实子进程 + 真实 sqlite）
# ---------------------------------------------------------------------------

WORKER = Path(__file__).parent / "graph_drill_worker.py"
DRILL_THREAD = "t-drill-kill9"


def _wait_token(proc, lines, token, timeout=40.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        line = proc.stdout.readline()
        if line:
            lines.append(line)
            if token in line:
                return line
        elif proc.poll() is not None:
            rest = proc.stdout.read()
            if rest:
                lines.append(rest)
            raise AssertionError(
                f"子进程在等到 {token!r} 前退出 rc={proc.returncode}\n"
                + "".join(lines[-20:]))
        else:
            time.sleep(0.02)
    raise TimeoutError(f"等待 {token!r} 超时\n" + "".join(lines[-20:]))


def test_05_kill9_resume_drill(env):
    db = env / "state" / "langgraph_checkpoints.db"
    pending_db = env / "state" / "pending_gates.db"
    run_dir = env / "runs" / DRILL_THREAD
    child_env = dict(os.environ, SIMPRAXIS_PHASE1_ROOT=str(env))
    # 子进程 cwd 是 tmp 根（夹具落盘地），必须显式把仓库根放进 PYTHONPATH
    child_env["PYTHONPATH"] = str(REPO_ROOT) + (
        os.pathsep + child_env["PYTHONPATH"] if child_env.get("PYTHONPATH") else "")
    lines: list[str] = []

    def spawn(phase):
        return subprocess.Popen(
            [sys.executable, str(WORKER), "--phase", phase],
            cwd=str(env), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, env=child_env)

    # --- run 阶段：发 Gate1 卡片后阻塞，直接 SIGKILL ---
    proc = spawn("run")
    try:
        _wait_token(proc, lines, "DIRECTION_CALLED")
        _wait_token(proc, lines, "GATE1_CARD_SENT")
        time.sleep(0.5)
        assert proc.poll() is None, "run 子进程应仍在 gate1 阻塞"
        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=10)
        assert proc.returncode == -9
    finally:
        if proc.poll() is None:
            proc.kill()

    blob_run = "".join(lines)

    # 持久层取证：检查点与 PENDING 卡片行都活着
    assert db.is_file(), "SqliteSaver 检查点未落盘"
    with sqlite3.connect(str(db)) as conn:
        n = conn.execute(
            "select count(*) from checkpoints where thread_id=?",
            (DRILL_THREAD,)).fetchone()[0]
        assert n >= 1, "检查点中无本 thread 记录"
    store = PendingStore(pending_db)
    try:
        row = store.get_pending(DRILL_THREAD, 1)
        assert row is not None and row["status"] == "PENDING"
        assert row["card_message_id"] == "drill-card-g1"
    finally:
        store.close()

    # --- resume 阶段：invoke(None) 重放，不重发卡片；重挂后再注入真人点按 ---
    proc2 = spawn("resume")
    resume_lines: list[str] = []
    try:
        _wait_token(proc2, resume_lines, "GATE1_REATTACH")
        # 真人在 resume 重挂成功后点按（WS 回调只做 mark_decided 一件事）
        store = PendingStore(pending_db)
        try:
            assert store.mark_decided(
                DRILL_THREAD, 1, decision="approve", choice=0, source="cli")
        finally:
            store.close()
        _wait_token(proc2, resume_lines, "AGENT_RAN")
        _wait_token(proc2, resume_lines, "GATE2_CARD_SENT")
        store = PendingStore(pending_db)
        try:
            assert store.mark_decided(
                DRILL_THREAD, 2, decision="approve", source="cli")
        finally:
            store.close()
        _wait_token(proc2, resume_lines, "GRAPH_DONE", timeout=30)
        rc = proc2.wait(timeout=10)
        assert rc == 0, "".join(resume_lines)
    finally:
        if proc2.poll() is None:
            proc2.kill()

    blob_resume = "".join(resume_lines)
    assert blob_run.count("GATE1_CARD_SENT") == 1
    assert "GATE1_REATTACH" in blob_resume
    assert "GATE1_CARD_SENT" not in blob_resume, "resume 严禁重发 Gate1 卡片"
    assert blob_resume.count("DIRECTION_CALLED") == 0, "重放点应在 gate1 而非 direction"
    assert blob_resume.count("AGENT_RAN") == 1
    assert (run_dir / "DONE.json").is_file()

    # 证据落 /mnt（0o600）
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(EVIDENCE_DIR, 0o700)
    log = EVIDENCE_DIR / "resume_drill.log"
    header = [
        "Task3 ⑤ kill -9 重启演练证据（0 付费；feishu/gateway fake，",
        "pending_gates 与 SqliteSaver 为真实 sqlite）",
        f"ts={time.strftime('%Y-%m-%dT%H:%M:%S%z')} thread={DRILL_THREAD}",
        "断言：run 阶段 GATE1_CARD_SENT×1 后 SIGKILL(rc=-9)；",
        "检查点>=1 行且 PENDING 卡片行存活；mark_decided 注入真人决策；",
        "resume 阶段 GATE1_REATTACH（不重发）→ AGENT_RAN → GATE2 → GRAPH_DONE。",
        "=" * 70,
        "----- run 阶段 stdout -----",
    ]
    body = header + [blob_run, "----- resume 阶段 stdout -----", blob_resume]
    log.write_text("\n".join(body), encoding="utf-8")
    os.chmod(log, 0o600)


# ---------------------------------------------------------------------------
# ⑥ 图结构导出（文本拓扑，无系统包）
# ---------------------------------------------------------------------------

def test_06_topology_export(env):
    compiled = build_graph(None).compile()
    txt = export_text(compiled)
    for node in ("direction", "gate1", "agent_run", "evaluate", "gate2",
                 "persist_learning", "human_failure", "done"):
        assert f"  - {node}:" in txt
    assert "route_after_gate1" in txt and "route_after_gate2" in txt
    assert "route_track" in txt
    assert "gate1 -> direction" in txt          # 打回回环
    assert "gate2 -> agent_run" in txt
    assert "persist_learning -> done" in txt
    assert "human_failure -> __end__" in txt
    assert "[conditional]" in txt
    # 契约：不为此装系统包；探测不到 pygraphviz 时 svg 导出返回 None
    from orchestrator.graph.topology import export_svg
    assert export_svg(compiled, env / "topology.svg") is None
    assert "draw_ascii" in txt

    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(EVIDENCE_DIR, 0o700)
    out = EVIDENCE_DIR / "topology.txt"
    out.write_text(txt, encoding="utf-8")
    os.chmod(out, 0o600)
