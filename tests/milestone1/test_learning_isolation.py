#!/usr/bin/env python3
"""Task6 学习回写隔离测试（计划 L535-538，0 付费、零联网）。

四件必断言（计划逐字）：
  ① 写入 2 条夹具笔记 → count 增加 → query 可召回；
  ② kb_chunks 仍 1542 / --expect 104 不变；
  ③ Q1Q2Q3 召回有序 id 与 Phase0/Task2 基线完全一致；
  ④ 混维度守卫仍对 kb_chunks 生效，且 phase1_learnings 有自己的维度守卫。

夹具一律写临时 lancedb（SIMPRAXIS_LANCEDB_URI 指向 tmp_path），生产库只做
只读快照前后比对——测试永不写真实 phase1_learnings、永不碰 kb_chunks。
"""
from __future__ import annotations

import inspect
import json
import subprocess
import sys
from pathlib import Path

import pytest
import pyarrow as pa

import lancedb

from memory_flywheel.phase1 import learning_store as ls_mod
from memory_flywheel.phase1.learning_store import (
    LAYER,
    NAMESPACE,
    TABLE_NAME,
    LearningStore,
)
from memory_flywheel.vector_engine import TABLE_NAME as KB_TABLE, VectorEngine
# Task0b：读活数据层（$KB_ROOT/data/lancedb）的 02/03/04/05 打 kb_integration 门
from tests.kb_support import require_kb_integration

REPO_ROOT = Path(__file__).resolve().parents[2]

# Phase0/Task2 基线（/mnt/artifacts/.../task2_direction/recall_baseline_q1q2q3.json
# 逐行转录；mpnet 768 重算必须逐位同序，不允许只断「命中」）
Q1_IDS = ["a5ac6af02544fc330202e3a512198d12",
          "69bc86bd5f56599c09754864816a5aa7",
          "36d88099b89eb2d7bc95cb8b63ae1b20"]
Q2_IDS = ["398ef19f41b0901b04c3a7f3984e8ece",
          "a4a1083844306f192273f41763fa7020",
          "5f3dd7341b0553129fcf87746a5a4aac",
          "5c9058abbc25c15702dc9249e813aab6",
          "667621d5b55057536b0658d9e7801593"]
Q3_IDS = ["8fd71136657b7b29cf551ddb17224fa8",
          "65b9c51cd155fe49adbd9ac0609b7696",
          "3936c514758ed8d5d33f1235533c57a0",
          "33b9cd6b58aa0b00c06bb433b61e2c9a",
          "c8fe70ddd727296a11a80cfb59e90c50"]
Q1_TEXT = ("YAML 配置文件为什么会静默覆盖命令行参数？我在命令行传 --device cuda:0 "
           "为什么还是跑在 CPU 上？")
Q2_TEXT = "headless 无显示器，EGL 离屏渲染初始化失败黑屏怎么排查？"
Q3_TEXT = "Isaac Sim 里 cudaErrorIllegalAddress 非法显存地址在什么情况下发生？"


@pytest.fixture
def roots(tmp_path):
    uri = tmp_path / "lance"
    ev_root = tmp_path / "phase1"
    (ev_root / "runs" / "t1").mkdir(parents=True)
    (ev_root / "runs" / "t2").mkdir(parents=True)
    return uri, ev_root


@pytest.fixture
def runs_root(tmp_path):
    """Task7 §1.8 第二证据根 $SIMPRAXIS_RUN_DIR（真实 rollout 产物）。"""
    p = tmp_path / "runsroot"
    p.mkdir(parents=True)
    return p


@pytest.fixture
def store(roots, runs_root):
    uri, ev_root = roots
    return LearningStore(uri=str(uri), evidence_root=ev_root,
                         runs_evidence_root=runs_root)


def _evidence(ev_root: Path, thread: str, name: str = "telemetry.json") -> str:
    p = ev_root / "runs" / thread / name
    p.write_text(json.dumps({"_fixture": True}), encoding="utf-8")
    return str(p)


# ---------------------------------------------------------------------------
# ① 2 条夹具笔记：count ↑、query 可召回、固定列值
# ---------------------------------------------------------------------------

def test_01_two_fixture_notes_count_and_recall(store, roots):
    _, ev_root = roots
    assert store.count() == 0 and store.table_exists() is False

    e1 = _evidence(ev_root, "t1")
    r1 = store.add_learning(
        run_id="t1", track="track1", rung=3, sim_engine="mujoco",
        gate_source="gate2_accepted",
        evidence_relpath="runs/t1/telemetry.json",
        failure_symptom="接触判定窗口太短导致抓取被误判失败",
        fix_action="把接触持续阈值对齐 1.0s 并提高子步分辨率",
        human_opinion="需要给出逐子步接触深度证据", effective=True)
    e2 = _evidence(ev_root, "t2")
    r2 = store.add_learning(
        run_id="t2", track="track1", rung=0, sim_engine="physx_isaac",
        gate_source="human_failure_gate1",
        evidence_relpath="runs/t2/telemetry.json",
        failure_symptom="Gate1 两次打回：方案与目标任务无关",
        fix_action="", human_opinion="方向不合要求", effective=False)

    assert store.count() == 2
    assert r1["id"] != r2["id"]

    # 同库只允许出现 phase1_learnings 一张表（kb_chunks 未被创建/触碰）
    assert store.db.table_names() == [TABLE_NAME]

    hits = store.query("穿模 接触深度 子步 接触持续阈值", top_k=2)
    assert hits[0]["run_id"] == "t1"
    assert "vector" not in hits[0]            # 检索行不回传向量
    assert hits[0]["namespace"] == NAMESPACE == "phase1_learnings"
    assert hits[0]["layer"] == LAYER == "phase1_run_note"
    assert hits[0]["track"] == "track1" and hits[0]["rung"] == 3

    hits2 = store.query("Gate1 方向打回 方案无关", top_k=1)
    assert hits2[0]["run_id"] == "t2" and hits2[0]["rung"] == 0
    # 证据列存相对路径（不存 /mnt 绝对路径）
    assert all(not h["evidence_relpath"].startswith("/") for h in hits + hits2)


def test_01b_replay_is_idempotent(store, roots):
    """kill -9 resume 重放同一结案：确定性主键先删后写，不产生重复行。"""
    _, ev_root = roots
    _evidence(ev_root, "t1")
    kw = dict(run_id="t1", track="track1", rung=3, sim_engine="mujoco",
              gate_source="gate2_accepted",
              evidence_relpath="runs/t1/telemetry.json",
              failure_symptom="同一失败现象", fix_action="同一修复",
              human_opinion="同一意见", effective=True)
    a = store.add_learning(**kw)
    b = store.add_learning(**kw)
    assert a["id"] == b["id"] and store.count() == 1


# ---------------------------------------------------------------------------
# ②③ 生产基线只读不变：1542 行 / 104 Q 块 / Q1Q2Q3 有序 id
# ---------------------------------------------------------------------------

@pytest.mark.kb_integration
def test_02_real_kb_table_still_1542_dim768():
    require_kb_integration(data=True)
    eng = VectorEngine()
    tbl = eng.table()
    assert tbl.count_rows() == 1542, "kb_chunks 被污染（学习只能写新表）"
    assert tbl.schema.field("vector").type.list_size == 768


@pytest.mark.kb_integration
def test_03_qxx_atomic_chunks_still_104():
    require_kb_integration(data=True)
    proc = subprocess.run(
        [sys.executable, "-m", "memory_flywheel.scripts.check_chunk_integrity",
         "--expect", "104"],
        cwd=str(REPO_ROOT), capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout[-1500:] + proc.stderr[-1500:]


@pytest.mark.kb_integration
def test_04_q1q2q3_ordered_ids_match_phase0_baseline():
    require_kb_integration(data=True)
    eng = VectorEngine()
    q1 = [h["id"] for h in eng.query_kb(
        Q1_TEXT, namespace="capabilities", project="lw_benchhub", top_k=3)]
    q2 = [h["id"] for h in eng.query_kb(
        Q2_TEXT, namespace="troubleshooting", top_k=5)]
    q3 = [h["id"] for h in eng.query_kb(
        Q3_TEXT, namespace="capabilities", top_k=5)]
    assert q1 == Q1_IDS
    assert q2 == Q2_IDS
    assert q3 == Q3_IDS
    # 与 /mnt Task2 基线文件交叉核对（防止转录漂移）
    base = json.loads(Path(
        "/mnt/artifacts/simpraxis/phase1/task2_direction/"
        "recall_baseline_q1q2q3.json").read_text(encoding="utf-8"))
    assert [h["id"] for h in base["Q1"]["hits"]] == Q1_IDS
    assert [h["id"] for h in base["Q2"]["hits"]] == Q2_IDS
    assert [h["id"] for h in base["Q3"]["hits"]] == Q3_IDS


@pytest.mark.kb_integration
def test_05_real_learning_table_unpolluted(roots):
    """测试全程只写临时库：真实 phase1_learnings 行数快照前后不变。"""
    require_kb_integration(data=True)
    uri, ev_root = roots
    before = LearningStore().count()
    s = LearningStore(uri=str(uri), evidence_root=ev_root)
    _evidence(ev_root, "t1")
    s.add_learning(run_id="t1", track="track1", rung=3, sim_engine="x",
                   gate_source="gate2_accepted",
                   evidence_relpath="runs/t1/telemetry.json",
                   failure_symptom="临时库夹具笔记", fix_action="",
                   human_opinion="", effective=True)
    after = LearningStore().count()
    assert before == after, f"真实学习表被测试污染：{before} → {after}"


# ---------------------------------------------------------------------------
# ④ 守卫：无证据不写 / 维度守卫（两表各自）/ 非法字段
# ---------------------------------------------------------------------------

def test_06_no_evidence_no_write(store, roots):
    _, ev_root = roots
    with pytest.raises(FileNotFoundError):
        store.add_learning(
            run_id="t1", track="track1", rung=3, sim_engine="x",
            gate_source="gate2_accepted",
            evidence_relpath="runs/t1/missing.json",
            failure_symptom="证据不存在", fix_action="", human_opinion="",
            effective=True)
    assert store.count() == 0
    with pytest.raises(ValueError, match="绝对路径"):
        store.add_learning(
            run_id="t1", track="track1", rung=3, sim_engine="x",
            gate_source="gate2_accepted",
            evidence_relpath=str(ev_root / "runs" / "t1" / "telemetry.json"),
            failure_symptom="绝对路径", fix_action="", human_opinion="",
            effective=True)
    traversal = _evidence(ev_root, "t1")
    with pytest.raises(ValueError, match="越界"):
        store.add_learning(
            run_id="t1", track="track1", rung=3, sim_engine="x",
            gate_source="gate2_accepted",
            evidence_relpath="../../" + Path(traversal).parent.name +
            "/escape.json",
            failure_symptom="越界", fix_action="", human_opinion="",
            effective=True)


def test_07_field_validation(store, roots):
    _, ev_root = roots
    _evidence(ev_root, "t1")
    base = dict(run_id="t1", track="track1", rung=3, sim_engine="x",
                gate_source="gate2_accepted",
                evidence_relpath="runs/t1/telemetry.json",
                failure_symptom="s", fix_action="f", human_opinion="h",
                effective=True)
    with pytest.raises(ValueError, match="track"):
        store.add_learning(**{**base, "track": "track3"})
    for bad_rung in (True, -1, 5, 1.0, "3"):
        with pytest.raises(ValueError, match="rung"):
            store.add_learning(**{**base, "rung": bad_rung})
    with pytest.raises(ValueError, match="rung 必须为 0"):
        store.add_learning(**{**base, "gate_source": "human_failure_gate1",
                              "rung": 3, "effective": False,
                              "failure_symptom": "失败但 rung 非 0"})
    with pytest.raises(ValueError, match="rung 必须 >=1"):
        store.add_learning(**{**base, "rung": 0})
    with pytest.raises(ValueError, match="gate_source"):
        store.add_learning(**{**base, "gate_source": "phase1_sync"})
    # Task7 锁内适配：空三项的 gate2_accepted（一次通过的干净 run）允许写入
    # （计划 L534 要求验收通过即写；rung/engine/证据原件即内容）；空三项的
    # 失败结案仍必须拒——失败却无任何现象/动作/意见才是无内容行。
    with pytest.raises(ValueError, match="全空"):
        store.add_learning(**{**base, "gate_source": "human_failure_gate1",
                              "rung": 0, "effective": False,
                              "failure_symptom": "",
                              "fix_action": "", "human_opinion": ""})
    store.add_learning(**{**base, "run_id": "clean-accept",
                          "failure_symptom": "",
                          "fix_action": "", "human_opinion": ""})
    store.table().delete("run_id = 'clean-accept'")  # 保持本用例末尾 0 行
    with pytest.raises(ValueError, match="effective"):
        store.add_learning(**{**base, "effective": "yes"})
    with pytest.raises(ValueError, match="失败结案 effective"):
        store.add_learning(**{**base, "gate_source": "human_failure_gate2",
                              "rung": 0, "effective": True,
                              "failure_symptom": "失败却宣称生效"})
    with pytest.raises(ValueError, match="run_id"):
        store.add_learning(**{**base, "run_id": "  "})
    assert store.count() == 0


@pytest.mark.parametrize("source", [
    "gate2_accepted",
    "gate2_accepted_supplementary",   # Task0a 纪律 12 图外补沉淀新值
    "human_failure_agent_run",
    "human_failure_gate1",
    "human_failure_gate2",
])
def test_07b_gate_source_whitelist_enum(store, roots, source):
    """Task0a：新 gate_source 进白名单枚举（非 failure、rung>=1），老 4 值语义
    不变；任意他串仍在 test_07 被 phase1_sync 断言拒绝（不放宽任意字符串）。"""
    from memory_flywheel.phase1.learning_store import _FAILURE_RE
    _, ev_root = roots
    w1 = ev_root / "runs" / "w1"
    w1.mkdir(parents=True, exist_ok=True)
    (w1 / "telemetry.json").write_text(
        json.dumps({"_fixture": True}), encoding="utf-8")
    is_failure = bool(_FAILURE_RE.match(source))
    summary = store.add_learning(
        run_id="w1", track="track1",
        rung=0 if is_failure else 1, sim_engine="x", gate_source=source,
        evidence_relpath="runs/w1/telemetry.json",
        failure_symptom="s" if is_failure else "",
        fix_action="f" if is_failure else "",
        human_opinion="h" if is_failure else "",
        effective=False if is_failure else True)
    assert summary["gate_source"] == source
    assert store.count() == 1


def test_08_own_table_dim_guard(roots):
    uri, ev_root = roots
    db = lancedb.connect(str(uri))
    db.create_table(TABLE_NAME, schema=pa.schema([
        pa.field("id", pa.string()),
        pa.field("vector", pa.list_(pa.float32(), 384))]), mode="create")
    with pytest.raises(RuntimeError, match=TABLE_NAME):
        LearningStore(uri=str(uri), evidence_root=ev_root)


def test_09_kb_table_dim_guard_still_armed(roots):
    """混维度守卫对 kb_chunks 恒开（Task6 新表不得削弱基线防护）。"""
    uri, _ = roots
    db = lancedb.connect(str(uri))
    db.create_table(KB_TABLE, schema=pa.schema([
        pa.field("id", pa.string()),
        pa.field("vector", pa.list_(pa.float32(), 384))]), mode="create")
    with pytest.raises(RuntimeError, match=KB_TABLE):
        VectorEngine(uri=str(uri))


def test_10_no_main_write_action():
    """计划 L537：CLI 无默认 __main__ 写动作；不提供删表脚本；不导入基线引擎。

    （docstring 可以提及 kb_chunks 以说明隔离意图；代码路径零引用——
    行为侧隔离由 test_01 的 table_names 断言兜底。）
    """
    src = inspect.getsource(ls_mod)
    # 文档字符串允许解释为什么没有 __main__；真正拦截的是可执行守卫
    assert "if __name__" not in src
    assert "drop_table" not in src, "Task6 不提供删表脚本（删除需书面授权）"
    imports = [ln.strip() for ln in src.splitlines()]
    assert not any(
        ln.startswith("from memory_flywheel.vector_engine")
        or ln.startswith("import memory_flywheel.vector_engine")
        for ln in imports), "学习通道不得导入 VectorEngine 写路径"
    assert ls_mod.TABLE_NAME == "phase1_learnings"


# ---------------------------------------------------------------------------
# 图接缝：persist_learning（accept）与 human_failure（rung=0）
# ---------------------------------------------------------------------------

def _accept_state(ev_root: Path) -> dict:
    tele = _evidence(ev_root, "t1")
    return {
        "thread_id": "t1", "track": "track1",
        "chosen": {"name": "PhysX 方案", "sim_engine": "physx_isaac"},
        "agent_runs": [{
            "status": "ok", "telemetry_uri": tele, "coding_round": 1,
            "timeline": [{"coding_round": 1,
                          "applied": ["scripts/contact_fix.py"]}]}],
        "metrics": {"rungs": {"rung1": {"pass": True},
                              "rung2": {"pass": True},
                              "rung3": {"pass": True},
                              "rung4": None},
                    "rung4": None, "rules_pass": True},
        "events": [
            {"type": "gate2_rejected",
             "human_notes": "接触不稳，提高 timestep 分辨率"},
            {"type": "gate2_accepted"}],
    }


def test_11_seam_accept_writes_structured_entry(store, roots):
    from orchestrator.graph.learning import make_learning_seam
    _, ev_root = roots
    seam = make_learning_seam(store)
    state = _accept_state(ev_root)
    summary = seam({**state, "_learning": {"kind": "accept"}})
    assert summary["gate_source"] == "gate2_accepted" and summary["rung"] == 3
    assert store.count() == 1
    row = store.query("接触不稳 timestep 修复", top_k=1)[0]
    assert row["sim_engine"] == "physx_isaac"
    assert row["evidence_relpath"] == "runs/t1/telemetry.json"
    assert "接触不稳" in row["text"] and "contact_fix.py" in row["text"]
    assert "生效与否：是" in row["text"]


def test_12_seam_rung4_activated_bumps_rung(store, roots):
    """L-5：仅 mode=="active" 且 status ok 的 Rung4 记 rung=4。"""
    from orchestrator.graph.learning import make_learning_seam
    _, ev_root = roots
    state = _accept_state(ev_root)
    state["metrics"]["rungs"]["rung4"] = {"status": "ok", "mode": "active",
                                          "n_defects": 0}
    state["metrics"]["rung4"] = {"status": "ok", "mode": "active"}
    summary = make_learning_seam(store)({**state, "_learning":
                                         {"kind": "accept"}})
    assert summary["rung"] == 4
    # active 模式不需要封顶注记
    assert "rung4_mode" not in summary


@pytest.mark.parametrize("mode", ["record_only", "uncalibrated"])
def test_12b_seam_rung4_non_active_caps_at_3(store, roots, mode):
    """L-5：record_only/uncalibrated 的 Rung4 无自动升级效力，封顶 3，
    返回摘要带 rung4_mode/rung4_note 注记（learning_store 12 列冻结，
    注记只在返回值上，不写表）。"""
    from orchestrator.graph.learning import make_learning_seam
    _, ev_root = roots
    state = _accept_state(ev_root)
    r4 = {"status": "ok", "mode": mode}
    state["metrics"]["rungs"]["rung4"] = dict(r4)
    state["metrics"]["rung4"] = dict(r4)
    summary = make_learning_seam(store)({**state, "_learning":
                                         {"kind": "accept"}})
    assert summary["rung"] == 3
    assert summary["rung4_mode"] == mode
    assert mode in summary["rung4_note"]
    # 落库行本身仍是普通 accept 行，gate_source 不被污染
    assert summary["gate_source"] == "gate2_accepted"


def test_12c_seam_rung4_status_ok_without_mode_caps_at_3(store, roots):
    """L-5 防御：缺 mode 字段的旧形态 rung4 不得升到 4（按非 active 处理）。"""
    from orchestrator.graph.learning import make_learning_seam
    _, ev_root = roots
    state = _accept_state(ev_root)
    state["metrics"]["rungs"]["rung4"] = {"status": "ok"}
    state["metrics"]["rung4"] = {"status": "ok"}
    summary = make_learning_seam(store)({**state, "_learning":
                                         {"kind": "accept"}})
    assert summary["rung"] == 3
    assert "rung4_mode" not in summary


def test_13_human_failure_node_writes_rung0(store, roots):
    from orchestrator.graph.nodes import (
        GraphDeps, node_human_failure,
    )
    from orchestrator.graph.learning import make_learning_seam
    _, ev_root = roots
    run_dir = ev_root / "runs" / "t9"
    run_dir.mkdir(parents=True)
    deps = GraphDeps(persist=make_learning_seam(store), run_dir=run_dir)
    state = {"thread_id": "t9", "track": "track2",
             "gate1_rejects": 0, "gate2_rejects": 2,
             "events": [
                 {"type": "gate2_rejected", "human_notes": "打回 A"},
                 {"type": "gate2_rejected", "human_notes": "打回 B"}]}
    update = node_human_failure(state, deps)
    ev = update["events"][-1]
    assert ev["type"] == "human_failure" and ev["stage"] == "gate2"
    assert ev["learning"]["rung"] == 0
    assert (run_dir / "FAILURE.json").is_file()
    rows = store.query("打回", top_k=5)
    assert len(rows) == 1 and rows[0]["rung"] == 0
    assert rows[0]["gate_source"] == "human_failure_gate2"
    assert rows[0]["evidence_relpath"].endswith("FAILURE.json")
    assert "打回 A" in rows[0]["text"] and "生效与否：否" in rows[0]["text"]


def test_14_human_failure_without_persist_skips_learning(roots):
    """Task3 风格调用方不注入 persist：FAILURE.json 照写，学习不回写（不抛异常）。"""
    from orchestrator.graph.nodes import GraphDeps, node_human_failure
    _, ev_root = roots
    run_dir = ev_root / "runs" / "t8"
    run_dir.mkdir(parents=True)
    deps = GraphDeps(run_dir=run_dir)
    state = {"thread_id": "t8", "track": "track1",
             "gate1_rejects": 0, "gate2_rejects": 2, "events": []}
    update = node_human_failure(state, deps)
    assert "learning" not in update["events"][-1]
    assert (run_dir / "FAILURE.json").is_file()


def test_15_seam_guardrails(store, roots):
    from orchestrator.graph.learning import make_learning_seam
    from orchestrator.graph.nodes import GraphStateError
    _, ev_root = roots
    seam = make_learning_seam(store)

    # 无 metrics / 无成功 run：拒绝伪造
    bad = {"thread_id": "t1", "track": "track1", "agent_runs": [],
           "events": [{"type": "gate2_accepted"}], "chosen": {}}
    with pytest.raises(GraphStateError):
        seam({**bad, "_learning": {"kind": "accept"}})

    # 证据不在 phase1 根之下（telemetry 被挪到外部路径）
    outside = roots[1].parent / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    state = _accept_state(ev_root)
    state["agent_runs"][0]["telemetry_uri"] = str(outside)
    with pytest.raises(GraphStateError, match="证据"):
        seam({**state, "_learning": {"kind": "accept"}})

    # failure 语境缺字段：硬错误而非静默跳过
    with pytest.raises(GraphStateError):
        seam({"thread_id": "t1", "track": "track1", "events": [],
              "_learning": {"kind": "failure"}})
    assert store.count() == 0


def test_16_runs_root_tagged_evidence_and_clean_accept(store, roots,
                                                        runs_root):
    """Task7 §1.8：真实 rollout telemetry 在 $SIMPRAXIS_RUN_DIR 下，relpath
    必须带 @runs/ 标记并被接受写库；Gate2 一次通过的干净 run（无打回/补丁/
    意见，三项摘要全空）仍写一条 rung=4 学习（计划 L534）。"""
    from orchestrator.graph.learning import make_learning_seam
    from memory_flywheel.phase1.learning_store import RUNS_ROOT_TAG
    tele = runs_root / "phase1" / "task7_track1" / "run_t_1" / "telemetry.json"
    tele.parent.mkdir(parents=True, exist_ok=True)
    tele.write_text(json.dumps({"_fixture": True}), encoding="utf-8")
    state = {
        "thread_id": "t-clean", "track": "track1",
        "chosen": {"name": "MuJoCo 最小接触", "sim_engine": "mujoco"},
        "agent_runs": [{"status": "ok", "telemetry_uri": str(tele),
                        "coding_round": 0, "timeline": []}],
        "metrics": {"rungs": {"rung1": {"pass": True}, "rung2": {"pass": True},
                              "rung3": {"pass": True},
                              "rung4": {"status": "ok", "mode": "active"}},
                    "rung4": {"status": "ok", "mode": "active"},
                    "rules_pass": True},
        "events": [{"type": "gate2_accepted"}],
    }
    summary = make_learning_seam(store)({**state, "_learning":
                                         {"kind": "accept"}})
    expected = RUNS_ROOT_TAG + "phase1/task7_track1/run_t_1/telemetry.json"
    assert summary["evidence_relpath"] == expected and summary["rung"] == 4
    assert store.count() == 1
    row = store.query("Gate2 验收通过 MuJoCo", top_k=1)[0]
    assert row["run_id"] == "t-clean"
    assert row["evidence_relpath"] == expected
    assert "（无：本轮一次通过）" in row["text"]

    # @runs/ 标记下的 ../ 越界同样拒绝（逃出 runs 根）；直接喂标记 relpath
    # 走 store 层守卫（seam 只接受绝对路径换算，不接受 relpath 直喂）。
    with pytest.raises(ValueError, match="越界"):
        store.add_learning(run_id="t-x", track="track1", rung=1,
                           sim_engine="m", gate_source="gate2_accepted",
                           evidence_relpath=RUNS_ROOT_TAG + "../escape.json",
                           failure_symptom="越界探测", effective=True)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
