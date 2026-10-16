#!/usr/bin/env python3
"""Phase 2 Task 0a — Q2 Carla 知识补沉淀 + 图外回写通路（附录 A 七条，0 付费）。

合同：Phase_2_Plan_detailed.md Task 0a（L281-317）+ 附录 A（L906-912）+
纪律 12（L832）。全部临时库 + 断网 env + monkeypatch；真实 lancedb 只读
（按 run_id 选老 4 行作预置/快照，补写后活库为 5 行不影响选择），测试不向
真实 phase1_learnings 与 kb_chunks 写任何内容。

七测映射：
  1 test_gate_source_whitelist_extension          附录 A 第 1 条
  2 test_q2_backfill_exactly_one_row_five_total   附录 A 第 2 条
  3 test_old_four_rows_byte_unchanged             附录 A 第 3 条
  4 test_requires_real_approve_decision_json      附录 A 第 4 条（四拒写情形）
  5 test_offgraph_cli_idempotent_same_id          附录 A 第 5 条
  6 test_compose_text_facts_traced_and_empty_notes 附录 A 第 6 条
  7 test_q2_semantic_recall_offline               附录 A 第 7 条
"""
from __future__ import annotations

import importlib.util
import json
import os
import stat
import struct
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from memory_flywheel.phase1 import learning_store as ls_mod
from memory_flywheel.phase1.learning_store import (
    GATE_ACCEPTED,
    GATE_ACCEPTED_SUPPLEMENTARY,
    GATE_SOURCES,
    TABLE_NAME,
    LearningStore,
)
from memory_flywheel.phase1.offgraph_learning import (
    DECISION_SCHEMA,
    OffgraphDecisionError,
    write_offgraph_learning,
)
from orchestrator.common.clock import parse_ts_iso_strict
from tests.kb_support import require_kb_integration  # Task0b：活库读取守门

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNS_ROOT = Path(os.environ.get("SIMPRAXIS_RUN_DIR",
                                "/mnt/artifacts/simpraxis/runs"))
REAL_DECISION = (RUNS_ROOT / "phase1" / "task8_track2" / "q2_carla"
                 / "human_gate" / "decision.json")
Q2_RELPATH = ("@runs/phase1/task8_track2/q2_carla/human_gate/"
              "decision.json")
THREAD = "t8-q2carla-hitl-20261016"

# 老 4 行按 run_id 钉死（lead 已从活库只读核实，2026-10-17）；id 前 8 位
# dc0d01e7/48ec4eb4/1f1713ca/fa7e8782 作佐证注释。活库一次性 4→5 补写后，
# 选择/快照逻辑必须仍只取这 4 行，不随活库总行数漂移。
OLD_FOUR_RUN_IDS = frozenset({
    "t7-mujoco-20261015", "t7-mujoco-20261015b",
    "t7-maniskill-20261015a", "t8-genesis-20261016a",
})
_OLD_FOUR_ID_PREFIXES = frozenset(
    {"dc0d01e7", "48ec4eb4", "1f1713ca", "fa7e8782"})


@pytest.fixture(autouse=True)
def _offline_env(monkeypatch):
    """断网：fastembed/transformers 任何下载尝试都应直接失败（模型已在
    /mnt/caches 本地缓存）；测试通过即证明 0 下载。"""
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    monkeypatch.setenv("HF_DATASETS_OFFLINE", "1")


@pytest.fixture
def site(tmp_path):
    """临时库 + 双证据根 + phase2 根；decision.json 为真实裁决件逐字副本。"""
    assert REAL_DECISION.is_file(), (
        f"真实 Q2 裁决件缺失：{REAL_DECISION}（只读取证前提）")
    real = json.loads(REAL_DECISION.read_text(encoding="utf-8"))
    assert real["schema"] == DECISION_SCHEMA
    assert real["thread_id"] == THREAD

    lance_uri = tmp_path / "lance"
    phase1_root = tmp_path / "phase1"
    runs_root = tmp_path / "runs"
    phase2_root = tmp_path / "phase2"
    phase1_root.mkdir()
    phase2_root.mkdir()
    decision_dst = (runs_root / "phase1" / "task8_track2" / "q2_carla"
                    / "human_gate")
    decision_dst.mkdir(parents=True)
    decision_path = decision_dst / "decision.json"
    decision_path.write_text(
        REAL_DECISION.read_text(encoding="utf-8"), encoding="utf-8")
    os.chmod(decision_path, 0o600)

    store = LearningStore(uri=str(lance_uri), evidence_root=phase1_root,
                          runs_evidence_root=runs_root)
    return {
        "store": store,
        "lance_uri": lance_uri,
        "phase1_root": phase1_root,
        "runs_root": runs_root,
        "phase2_root": phase2_root,
        "decision_path": decision_path,
        "decision": real,
    }


def _select_old_four(rows: list[dict]) -> list[dict]:
    """从任意行集合中按 run_id 选出老 4 行并校验 id 前缀一一对应。

    补写后活库为 5 行时本函数仍只返回这 4 行（QA 第 1 轮：不得用总行数
    count==4 作前提，否则补写后断言永久变红、快照被 Q2 行污染）。
    """
    old = [r for r in rows if r["run_id"] in OLD_FOUR_RUN_IDS]
    assert len(old) == 4 and {r["run_id"] for r in old} == OLD_FOUR_RUN_IDS, (
        f"老 4 行选择异常：选中 {len(old)} 行，"
        f"run_id={sorted(r['run_id'] for r in old)}")
    assert {r["id"][:8] for r in old} == _OLD_FOUR_ID_PREFIXES, (
        f"老 4 行 id 前缀与只读核实结果不符："
        f"{sorted(r['id'][:8] for r in old)}")
    return old


def _assert_supplementary_at_most_one(rows: list[dict]) -> None:
    """活库 gate2_accepted_supplementary 行至多 1（补写前 0、补写后 1）。

    第二行 Q2 沉淀（错误重跑/串 run）必须让测试报警；确定性 id 先删后写
    保证同参重放不产生第二行，本守卫盯的是异参重复沉淀。
    """
    n = sum(1 for r in rows
            if r["gate_source"] == GATE_ACCEPTED_SUPPLEMENTARY)
    assert n <= 1, f"活库 supplementary 行={n}，超过至多 1 的守卫"


def _live_four_rows() -> list[dict]:
    """只读连活库：取全量 → supplementary ≤1 守卫 → 选老 4 行；零写。"""
    live = LearningStore()
    rows = live.table().to_arrow().to_pylist()
    _assert_supplementary_at_most_one(rows)
    return _select_old_four(rows)


def _vector_bytes(vec) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


@pytest.fixture
def seeded_site(site):
    """临时库预置真实老 4 行（id/text/vector/relpath 逐列照搬），模拟补写前
    状态；老 4 行按 run_id 钉死，不随活库行数（4 或补写后 5）漂移。

    Task0b：活 lancedb 已划属 KB 数据层（$KB_ROOT/data/lancedb），读活库的
    预置步骤改由 kb_integration 开关守门（与 paid marker 同构，默认 skip
    非放水）；SIMPRAXIS_KB_INTEGRATION=1 且数据层迁移完成后执行。"""
    require_kb_integration(data=True)
    rows = _live_four_rows()
    assert len(rows) == 4
    site["store"].table(create=True).add(rows)
    site["live_rows"] = rows
    assert site["store"].count() == 4  # tmp 库从空起，只有预置的 4 行
    return site


def _run_cli(site, *extra_args) -> subprocess.CompletedProcess:
    """子进程跑 CLI，env 全部显式重定向到临时根（绝不继承活库 URI）。"""
    env = os.environ.copy()
    env.update({
        "SIMPRAXIS_LANCEDB_URI": str(site["lance_uri"]),
        "SIMPRAXIS_PHASE1_ROOT": str(site["phase1_root"]),
        "SIMPRAXIS_RUN_DIR": str(site["runs_root"]),
        "SIMPRAXIS_PHASE2_ROOT": str(site["phase2_root"]),
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "HF_DATASETS_OFFLINE": "1",
    })
    env.pop("SIMPRAXIS_ALLOW_PAID", None)
    argv = [sys.executable, "-m", "memory_flywheel.phase1.offgraph_learning",
            "--decision", str(site["decision_path"]),
            "--relpath", Q2_RELPATH, *extra_args]
    return subprocess.run(argv, cwd=str(REPO_ROOT), env=env,
                          capture_output=True, text=True, timeout=300)


def _cli_json(proc: subprocess.CompletedProcess) -> dict:
    """CLI stdout 末行为结果 JSON（embedding_selector 有复用配置提示行在前）。"""
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    assert proc.returncode == 0, proc.stdout[-1500:] + proc.stderr[-1500:]
    return json.loads(lines[-1])


def _new_row(store) -> dict:
    rows = [r for r in store.table().to_arrow().to_pylist()
            if r["run_id"] == THREAD]
    assert len(rows) == 1, f"Q2 学习行应恰好 1 行，实得 {len(rows)}"
    return rows[0]


# ---------------- 1. 白名单扩展（附录 A 第 1 条） ----------------

@pytest.mark.parametrize("bad_source", ["phase1_sync", "",
                                        "gate2_accepted_supplementary_x"])
def test_gate_source_whitelist_extension(site, bad_source):
    store = site["store"]
    ev = site["runs_root"] / "phase1" / "w1" / "telemetry.json"
    ev.parent.mkdir(parents=True)
    ev.write_text("{}", encoding="utf-8")
    base = dict(run_id="w1", track="track1", sim_engine="x",
                evidence_relpath="@runs/phase1/w1/telemetry.json",
                failure_symptom="s", fix_action="f", human_opinion="h")

    # 新常量：在白名单内、不被 failure 正则匹配
    assert GATE_ACCEPTED_SUPPLEMENTARY == "gate2_accepted_supplementary"
    assert GATE_ACCEPTED_SUPPLEMENTARY in GATE_SOURCES
    assert ls_mod._FAILURE_RE.match(GATE_ACCEPTED_SUPPLEMENTARY) is None
    assert len(GATE_SOURCES) == 5

    # supplementary：rung 1..4 合法（True 是 bool 必须拒），0/5/-1 拒
    for rung in (1, 2, 3, 4):
        rec = store.add_learning(
            **base, gate_source=GATE_ACCEPTED_SUPPLEMENTARY, rung=rung,
            effective=True)
        store.table().delete(f"id = '{rec['id']}'")
    for bad_rung in (True, 5, -1, 0):
        with pytest.raises(ValueError, match="rung"):
            store.add_learning(
                **base, gate_source=GATE_ACCEPTED_SUPPLEMENTARY,
                rung=bad_rung, effective=True)

    # 任意他串仍拒（白名单不放宽为任意字符串）
    with pytest.raises(ValueError, match="gate_source"):
        store.add_learning(**base, gate_source=bad_source, rung=1,
                           effective=True)

    # 老 4 值语义不变：gate2_accepted rung>=1；三个 human_failure_* rung=0
    rec = store.add_learning(**base, gate_source=GATE_ACCEPTED, rung=1,
                             effective=True)
    store.table().delete(f"id = '{rec['id']}'")
    for failure_source in ("human_failure_agent_run", "human_failure_gate1",
                           "human_failure_gate2"):
        rec = store.add_learning(
            **base, gate_source=failure_source, rung=0, effective=False)
        store.table().delete(f"id = '{rec['id']}'")
    assert store.count() == 0


# ---------------- 2. CLI 补沉淀恰好 5 行（附录 A 第 2 条） ----------------

@pytest.mark.kb_integration
def test_q2_backfill_exactly_one_row_five_total(seeded_site):
    proc = _run_cli(seeded_site)
    out = _cli_json(proc)
    store = seeded_site["store"]
    assert store.count() == 5
    assert out["gate_source"] == GATE_ACCEPTED_SUPPLEMENTARY
    row = _new_row(store)
    assert row["run_id"] == THREAD
    assert row["track"] == "track2" and row["sim_engine"] == "carla"
    assert row["rung"] == 1            # 非 4：Q2 无 Rung1-4 阶梯/无 VLM
    assert row["gate_source"] == GATE_ACCEPTED_SUPPLEMENTARY
    assert row["evidence_relpath"] == Q2_RELPATH
    assert len(row["vector"]) == 768
    # created_at 严格 Z 且为写入时刻（UTC 今天），不回填 2026-10-16
    created = parse_ts_iso_strict(row["created_at"])
    assert created.date() == datetime.now(timezone.utc).date()
    decision_ts = seeded_site["decision"]["ts_iso"]  # +0800 历史件
    assert not row["created_at"].endswith("+0800")
    assert decision_ts.startswith("2026-10-16")


# ---------------- 3. 老 4 行逐字节不变（附录 A 第 3 条） ----------------

@pytest.mark.kb_integration
def test_old_four_rows_byte_unchanged(seeded_site):
    store = seeded_site["store"]
    before = {r["id"]: (r["text"], r["evidence_relpath"],
                        _vector_bytes(r["vector"]))
              for r in store.table().to_arrow().to_pylist()}
    # 预置内容本身必须与活库老 4 行逐字节一致（快照同源）
    for live in seeded_site["live_rows"]:
        assert before[live["id"]] == (
            live["text"], live["evidence_relpath"],
            _vector_bytes(live["vector"]))

    proc = _run_cli(seeded_site)
    new_id = _cli_json(proc)["id"]
    after = {r["id"]: (r["text"], r["evidence_relpath"],
                       _vector_bytes(r["vector"]))
             for r in store.table().to_arrow().to_pylist()}
    assert set(after) == set(before) | {new_id}
    for rid, snap in before.items():
        assert after[rid] == snap, f"老行被改动：{rid}"

    # 临时库只有 phase1_learnings 一张表（kb_chunks 不可见写）
    assert store.db.table_names() == [TABLE_NAME]
    # 活库 kb_chunks 只读核对 1542（不被本任务触碰）
    from memory_flywheel.vector_engine import VectorEngine
    assert VectorEngine().table().count_rows() == 1542


# ---------------- 3b. 选择逻辑在「补写后 5 行」状态下仍钉死老 4 行
# （QA 第 1 轮回归，纯 tmp 合成行，不连活库） ----------------

def _synthetic_row(run_id, ident, gate_source):
    """选择函数只读 run_id/id/gate_source 三列，合成行无需向量。"""
    return {"run_id": run_id, "id": ident, "gate_source": gate_source}


def test_old_four_selection_survives_five_row_state():
    old = [
        _synthetic_row("t7-mujoco-20261015",
                       "dc0d01e7aaaaaaaaaaaaaaaaaaaaaaaa", GATE_ACCEPTED),
        _synthetic_row("t7-mujoco-20261015b",
                       "48ec4eb4bbbbbbbbbbbbbbbbbbbbbbbb", GATE_ACCEPTED),
        _synthetic_row("t7-maniskill-20261015a",
                       "1f1713cacccccccccccccccccccccccc", GATE_ACCEPTED),
        _synthetic_row("t8-genesis-20261016a",
                       "fa7e8782dddddddddddddddddddddddd", GATE_ACCEPTED),
    ]
    # 补写后：老 4 + 恰好 1 行 Q2 supplementary —— 选择仍只出老 4
    five = old + [_synthetic_row(
        THREAD, "86d4198ceeeeeeeeeeeeeeeeeeeeeeee",
        GATE_ACCEPTED_SUPPLEMENTARY)]
    picked = _select_old_four(five)
    assert len(picked) == 4
    assert {r["run_id"] for r in picked} == OLD_FOUR_RUN_IDS
    assert all(r["id"][:8] in _OLD_FOUR_ID_PREFIXES for r in picked)
    _assert_supplementary_at_most_one(five)  # 1 行：守卫通过

    # 异参重复沉淀（2 行 supplementary）：守卫必须报警
    six = five + [_synthetic_row(
        "t8-q2carla-hitl-DUP", "77777777ffffffffffffffffffffffff",
        GATE_ACCEPTED_SUPPLEMENTARY)]
    with pytest.raises(AssertionError, match="supplementary 行=2"):
        _assert_supplementary_at_most_one(six)

    # 老 4 缺任一：选择必须失败（不得静默返回 3 行）
    with pytest.raises(AssertionError, match="老 4 行选择异常"):
        _select_old_four(five[:3])
    # 补写后 5 行场景下 seeded 预置只取老 4（tmp 库计数前提）
    assert [r for r in five
            if r["run_id"] in OLD_FOUR_RUN_IDS] == picked


# ---------------- 4. 无真人 approve 一律拒写（附录 A 第 4 条） ----------------

@pytest.mark.parametrize("case", [
    "missing", "schema_bad", "not_approve",
    "relpath_empty", "relpath_traversal", "relpath_missing",
])
@pytest.mark.kb_integration
def test_requires_real_approve_decision_json(seeded_site, case, tmp_path):
    site = seeded_site
    decision = site["decision_path"]
    relpath = Q2_RELPATH
    if case == "missing":
        decision = tmp_path / "no_such_decision.json"
        expect = OffgraphDecisionError
    elif case == "schema_bad":
        bad = dict(site["decision"])
        bad["schema"] = "simpraxis.other.v1"
        p = tmp_path / "bad_schema.json"
        p.write_text(json.dumps(bad), encoding="utf-8")
        decision = p
        expect = OffgraphDecisionError
    elif case == "not_approve":
        bad = json.loads(json.dumps(site["decision"]))
        bad["human_decision"]["decision"] = "reject_with_comments"
        bad["human_decision"]["human_notes"] = "真人打回"
        p = tmp_path / "reject.json"
        p.write_text(json.dumps(bad), encoding="utf-8")
        decision = p
        expect = OffgraphDecisionError
    elif case == "relpath_empty":
        relpath = ""
        expect = ValueError
    elif case == "relpath_traversal":
        relpath = "@runs/../escape.json"
        expect = ValueError
    else:  # relpath_missing
        relpath = "@runs/phase1/no_such/decision.json"
        expect = FileNotFoundError

    with pytest.raises(expect):
        write_offgraph_learning(
            decision_json=decision, evidence_relpath=relpath,
            store=site["store"], phase2_root_path=site["phase2_root"])

    # 零残行：仍为预置 4 行；操作日志不得产生
    assert site["store"].count() == 4
    log = site["phase2_root"] / "recall" / "offgraph_writes.jsonl"
    assert not log.exists()


# ---------------- 5. CLI 幂等 + 0600 日志 + q2 脚本二次运行（附录 A 第 5 条） ----

@pytest.mark.kb_integration
def test_offgraph_cli_idempotent_same_id(seeded_site):
    site = seeded_site
    p1 = _run_cli(site)
    p2 = _run_cli(site)
    assert p1.returncode == 0 and p2.returncode == 0, (
        p1.stderr[-1000:] + p2.stderr[-1000:])
    id1, id2 = _cli_json(p1)["id"], _cli_json(p2)["id"]
    assert id1 == id2
    assert site["store"].count() == 5          # 先删后写，不产生第二行
    rows = [r for r in site["store"].table().to_arrow().to_pylist()
            if r["run_id"] == THREAD]
    assert len(rows) == 1 and rows[0]["id"] == id1

    log = site["phase2_root"] / "recall" / "offgraph_writes.jsonl"
    mode = stat.S_IMODE(log.stat().st_mode)
    assert mode == 0o600
    records = [json.loads(ln) for ln in log.read_text(encoding="utf-8")
               .splitlines()]
    assert len(records) == 2                   # 两次操作如实追加
    assert [r["id"] for r in records] == [id1, id2]
    assert all(r["run_id"] == THREAD for r in records)
    assert stat.S_IMODE(log.parent.stat().st_mode) == 0o700

    # q2_human_gate.py：decision.json 已存在时第二次 run() 在 H-5 守卫处退出，
    # 不构造网关、不触发第二次回写。
    spec = importlib.util.spec_from_file_location(
        "q2_human_gate_task0a_under_test",
        str(REPO_ROOT / "scripts" / "milestone1" / "q2_human_gate.py"))
    q2 = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = q2
    spec.loader.exec_module(q2)

    q2d = site["runs_root"] / "phase1" / "task8_track2" / "q2_carla"
    ana = q2d / "analysis"
    ctl = q2d / "control" / "analysis"
    rep = q2d / "collect_rep1"
    state = site["phase1_root"] / "state"
    state.mkdir(parents=True, exist_ok=True)
    paths = q2.Q2Paths(
        q2_dir=q2d, analysis=ana,
        control_invariance=ctl / "invariance_checks.json",
        gates_db=state / "pending_gates.db",
        meta_rep1=rep / "meta_rep1.json",
        summary_md=ana / "SUMMARY.md",
        optics_model=site["phase1_root"] / "optics_model.py",
        video=ana / "q2_overlay.mp4")
    for p in (paths.video, paths.summary_md,
              ana / "invariance_checks.json", paths.control_invariance,
              ana / "optics_sweep.csv", paths.gates_db, paths.meta_rep1,
              paths.optics_model):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.touch()
    assert paths.decision_path.is_file()       # 上一步 CLI 产物已在

    calls: list[Path] = []

    def writer(p):
        calls.append(Path(p))
        return {"id": id1}

    def boom():
        raise AssertionError("decision.json 已存在时不得构造网关")

    code = q2.run(paths, gateway_factory=boom, count_paid=lambda: 0,
                  offgraph_writer=writer)
    assert code == 1
    assert calls == []
    assert site["store"].count() == 5


# ---------------- 6. 四段文案事实同源 + 空意见不代笔（附录 A 第 6 条） ----

@pytest.mark.kb_integration
def test_compose_text_facts_traced_and_empty_notes(seeded_site):
    site = seeded_site
    assert site["decision"]["human_decision"]["human_notes"] == ""
    rec = write_offgraph_learning(
        decision_json=site["decision_path"], evidence_relpath=Q2_RELPATH,
        store=site["store"], phase2_root_path=site["phase2_root"])
    assert site["store"].count() == 5
    row = _new_row(site["store"])
    text = row["text"]
    assert rec["id"] == row["id"]

    # 四段齐（抬头 + 失败现象/修复动作/人工意见/生效与否）
    for head in ("[phase1_learnings] gate_source=", "失败现象：", "修复动作：",
                 "人工意见：（无）", "生效与否：是"):
        assert head in text

    # 抽样数值按与 decision.json 同源的格式化现算，逐字命中正文
    o = site["decision"]["metrics"]["outcome"]
    mech = site["decision"]["metrics"]["mechanism"]
    rain = next(r for r in mech if "rain 100" in r["label"])
    samples = [
        f"{rain['surface_kept_pct']:.2f}%",     # 71.73%
        f"{o['z_vs_clear']['rain_hard']:.3f}",  # -1.116
        str(o["survivor_max_abs_dI"]),          # 4.45e-05（件内原值字符串）
        f"{rain['transmission']:.3f}",          # 0.871
        "Qback=0.4",
        f"engine_has_snow={str(o['engine_has_snow']).lower()}",
    ]
    for s in samples:
        assert s in text, f"事实锚点缺失：{s}"

    # 计划 L296 草案摘要口径同样在正文
    for s in ("71.7%", "-1.12", "6346", "8078", "10.7%", "80 项 bit 相等",
              "含跨进程 20 项"):
        assert s in text, f"计划草案文案缺失：{s}"

    # 空 human_notes：渲染「（无）」，卡片 id 等裁决元数据不得被代笔进意见段
    card_id = site["decision"]["human_decision"]["card_message_id"]
    assert card_id and card_id not in text
    assert "真人意见" not in text


# ---------------- 7. 断网语义召回命中新行（附录 A 第 7 条） ----------------

@pytest.mark.kb_integration
def test_q2_semantic_recall_offline(seeded_site):
    site = seeded_site
    # mpnet 必须命中共机缓存（0 下载的物证）
    assert site["store"].config["model_name"].endswith(
        "paraphrase-multilingual-mpnet-base-v2")
    assert Path(site["store"].config["onnx_path"]).is_file()

    write_offgraph_learning(
        decision_json=site["decision_path"], evidence_relpath=Q2_RELPATH,
        store=site["store"], phase2_root_path=site["phase2_root"])
    hits = site["store"].query(
        "lidar rain fog snow weather carla", top_k=3)
    assert hits, "断网临时库应可召回（mpnet 本地缓存）"
    assert hits[0]["run_id"] == THREAD, (
        f"Q2 新行未居首：{[h['run_id'] for h in hits]}")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
