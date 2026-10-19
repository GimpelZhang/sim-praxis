#!/usr/bin/env python3
"""Phase 2 Task5 — phase2_learnings 独立表 + 五段式演化志（计划 L509-527）。

附录 A 五测（名字逐字钉死）：
  1 test_phase2_table_idempotent_dim_guard   幂等建表；异维度/异列同名表
                                             EvolutionSchemaError 且不自愈
  2 test_evolution_columns_constraints       12+3 schema 同构对质；round/parent/
                                             ratio 约束矩阵；None 可空；幂等一行
  3 test_chronicle_five_sections_traceable   五段齐全；>=3 数字回溯 summary JSON；
                                             Gate 伪数值进不了指标表；缺源 fail-loud
  4 test_recall_new_table_offline            断网 env 断言 + 本地 mpnet 语义召回；
                                             phase1/kb 表行数不被污染
  5 test_low3_schema_v0_message              v0/损坏 manifest → 专属异常 + 人工
                                             备份/drop/老两表不受影响处置文案

外加接缝回归（standard-isolation）：
  6 test_standard_accept_path_no_phase2      standard accept 逐字节旧行为：
                                             phase2 表/evolutions 目录零出生
  7 test_ascent_seam_chronicle_and_phase2    ascent accept：md 落盘+两表各一行，
                                             kill-9 重放逐字节幂等
  8 test_failure_path_never_writes_phase2    failure 结案（即便 run_mode=ascent）
                                             永不触达演化志/phase2 表

纪律：全 tmp_path LanceDB，禁止活库；断网 env（mpnet 走 /mnt/caches 本地缓存）；
0 付费、0 触网、无 skip/TODO/桩。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import lancedb
import pyarrow as pa
import pytest

from memory_flywheel.phase1.learning_store import (
    TABLE_NAME as P1_TABLE,
    LearningStore,
)
from memory_flywheel.phase2.evolution_store import (
    COLUMNS,
    TABLE_NAME,
    EvolutionSchemaError,
    EvolutionStore,
)
from orchestrator.graph import ascent
from orchestrator.graph.learning import make_learning_seam
from orchestrator.graph.nodes import GraphStateError
from orchestrator.phase2 import chronicle

V1_RUN = "t8-genesis-20261016a"
V2_RUN = "t6-genesis-ascent-20261017a"
GATE_TS = "2026-10-17T01:02:03Z"

# 六指标 V1/V2 基线（数字全部刻意取独特值，便于逐字回溯/防顶替）。
V1_METRICS = {
    "success_rate": 0.3333333333,
    "slip_dist_mean_m": 0.4125,
    "postpush_vmax_mean_mps": 0.5875,
    "tilt_max_deg_max": 132.4,
    "solve_wallclock_s": 184.25,
    "vram_peak_gb": 5.75,
}
V2_METRICS = {
    "success_rate": 0.6666666667,
    "slip_dist_mean_m": 0.20125,
    "postpush_vmax_mean_mps": 0.30125,
    "tilt_max_deg_max": 28.6,
    "solve_wallclock_s": 151.5,
    "vram_peak_gb": 5.9,
}


@pytest.fixture(autouse=True)
def _offline_env(monkeypatch):
    """断网三件套：fastembed 任何下载尝试都应直接失败（mpnet 本地缓存）。"""
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    monkeypatch.setenv("HF_DATASETS_OFFLINE", "1")


# ---------------------------------------------------------------------------
# 夹具合成件
# ---------------------------------------------------------------------------

def _v1_summary() -> dict:
    return {
        "schema": "simpraxis.task8.track2.telemetry.v1",
        "run_id": V1_RUN,
        "track": "track2",
        "seeds": [101, 102, 103],
        "disturbance": {"push_n": 35.0, "push_dur_s": 0.2},
        "dr_envelope": {
            "mu_loguniform": [0.04, 0.35],
            "Kp_loguniform": [0.6, 1.4],
            "Kd_loguniform": [0.7, 1.3],
            "wrench": {"f_n": 35.0, "dur_s": 0.2}},
        "groups": {
            "normal": {"n": 3, "success_rate": 0.3333333333, "n_fell": 2,
                       "tilt_max_deg_max": 41.2},
            "ice_nominal": {"n": 3, "success_rate": 0.0, "n_fell": 3,
                            "tilt_max_deg_max": 132.4},
            "ice_dr": {"n": 3, "success_rate": 0.0, "n_fell": 3,
                       "tilt_max_deg_max": 128.9},
        },
        "contrast": {
            "hypothesis_status": "contradiction_control_not_robust",
            "control_stands": False,
            "ordered_slip": False,
            "ice_over_normal_postpush_ratio": 0.824,
            "preregistered_ratio": 2.0,
            "dr_vs_nominal": {"direction": "worsened"},
        },
        "pareto_metrics": dict(V1_METRICS),
    }


def _v2_summary() -> dict:
    return {
        "schema": "simpraxis.task8.track2.telemetry.v1",
        "run_id": V2_RUN,
        "track": "track2",
        "seeds": [201, 202, 203],
        "disturbance": {"push_n": 35.0, "push_dur_s": 0.2},
        "dr_envelope": {
            "mu_loguniform": [0.03, 0.40],
            "Kp_loguniform": [0.8, 1.8],
            "Kd_loguniform": [0.8, 1.6],
            "wrench": {"f_n": 35.0, "dur_s": 0.2}},
        "groups": {
            "normal": {"n": 3, "success_rate": 1.0, "n_fell": 0,
                       "tilt_max_deg_max": 12.5},
            "ice_nominal": {"n": 3, "success_rate": 0.6666666667, "n_fell": 1,
                            "tilt_max_deg_max": 28.6},
            "ice_dr": {"n": 3, "success_rate": 0.3333333333, "n_fell": 2,
                       "tilt_max_deg_max": 31.8},
        },
        "pareto_metrics": dict(V2_METRICS),
        "innovation": {
            "control_law": (
                "tau_ankle = -Kp(mu) * theta - Kd(mu) * theta_dot\n"
                "with Kp(mu) = Kp0 * (1 + \\alpha \\log(\\mu_0/\\mu)),\n"
                "$\\alpha = 0.35$, friction $\\mu \\sim "
                "\\mathrm{LogUniform}(0.03, 0.40)$."),
            "dr_distribution": {
                "mu_loguniform": [0.03, 0.40],
                "Kp_loguniform": [0.8, 1.8],
                "Kd_loguniform": [0.8, 1.6],
                "wrench": {"f_n": 35.0, "dur_s": 0.2}},
        },
        "eliminated_assumptions": [
            "假设名义 PD 增益在结冰地面同样有效（已消除）",
            "假设 DR 臂必然优于名义臂（Q1 矛盾结论保留，不翻案）"],
    }


def _opinion(gate: str, notes: str) -> dict:
    # source 必须是生产词表成员（feishu_gateway/store.py mark_decided 白名单）。
    return {"schema": "simpraxis.gate_opinion.v1", "gate": gate,
            "decision": "approve", "human_notes": notes,
            "source": "card_form", "ts_iso": GATE_TS}


def _write_sources(tmp_path: Path, *, gate2_notes: "str | None" = None) -> dict:
    """四来源件落盘，返回 {paths..., dict...}（数字唯一真值在 summary JSON）。"""
    d = tmp_path / "sources"
    d.mkdir(parents=True, exist_ok=True)
    v1, v2 = _v1_summary(), _v2_summary()
    g1 = _opinion("gate1", "批准升阶假设：V1 在冰面抗扰不成立，允许 V2 实跑。")
    g2 = _opinion("gate2",
                  gate2_notes or "双媒体 Diff 确认冰面摔倒减少，同意结案。")
    paths = {}
    for name, doc in (("v1", v1), ("v2", v2), ("g1", g1), ("g2", g2)):
        p = d / f"{name}.json"
        p.write_text(json.dumps(doc, ensure_ascii=False, indent=2),
                     encoding="utf-8")
        paths[name] = p
    return {"v1": v1, "v2": v2, "g1": g1, "g2": g2,
            "v1_path": paths["v1"], "v2_path": paths["v2"],
            "g1_path": paths["g1"], "g2_path": paths["g2"]}


def _new_stores(tmp_path: Path) -> tuple[LearningStore, EvolutionStore, Path]:
    uri = tmp_path / "lance"
    p1_root = tmp_path / "phase1"
    p2_root = tmp_path / "phase2"
    runs_root = tmp_path / "runs"
    for p in (p1_root, p2_root, runs_root):
        p.mkdir(parents=True, exist_ok=True)
    p1 = LearningStore(uri=str(uri), evidence_root=p1_root,
                       runs_evidence_root=runs_root)
    p2 = EvolutionStore(uri=str(uri), evidence_root=p2_root,
                        runs_evidence_root=runs_root)
    return p1, p2, p2_root


def _render(src: dict, *, parent: str = V1_RUN, round_: int = 1) -> str:
    return chronicle.render_evolution_changelog(
        v1_summary=src["v1"], v2_summary=src["v2"],
        gate1_opinion=src["g1"], gate2_opinion=src["g2"],
        parent_run_id=parent, evolution_round=round_,
        generated_at=GATE_TS)


# ---------------------------------------------------------------------------
# 1. 幂等建表 + 异维度/异列守卫（不自愈）
# ---------------------------------------------------------------------------

def test_phase2_table_idempotent_dim_guard(tmp_path):
    uri = tmp_path / "lance"
    root = tmp_path / "p2"
    # 表不存在：反复构造不生表、不报错（幂等且零副作用）
    s1 = EvolutionStore(uri=str(uri), evidence_root=root,
                        runs_evidence_root=tmp_path / "runs")
    s2 = EvolutionStore(uri=str(uri), evidence_root=root,
                        runs_evidence_root=tmp_path / "runs")
    assert s1.table_exists() is False and s2.table_exists() is False
    assert lancedb.connect(str(uri)).table_names() == []

    # 异维度同名表（128 维，疑似换过嵌入模型的 v0 文件）→ 专属异常
    foreign_dir = tmp_path / "foreign128"
    foreign_dir.mkdir()
    fdb = lancedb.connect(str(foreign_dir))
    fdb.create_table(TABLE_NAME, schema=pa.schema([
        pa.field("id", pa.string()),
        pa.field("vector", pa.list_(pa.float32(), 128))]), mode="create")
    with pytest.raises(EvolutionSchemaError, match="维度"):
        EvolutionStore(uri=str(foreign_dir), evidence_root=root,
                       runs_evidence_root=tmp_path / "runs")
    # 严禁自愈 ALTER：128 维表原样留存
    opened = fdb.open_table(TABLE_NAME)
    assert opened.schema.field("vector").type.list_size == 128

    # 维度同为 768 但列集合不符（12 列 phase1 样式/异 schema 文件冒充新表）
    wrong_cols_dir = tmp_path / "wrongcols"
    wrong_cols_dir.mkdir()
    wdb = lancedb.connect(str(wrong_cols_dir))
    wdb.create_table(TABLE_NAME, schema=pa.schema([
        pa.field("id", pa.string()),
        pa.field("text", pa.string()),
        pa.field("vector", pa.list_(pa.float32(), s1.dim))]), mode="create")
    with pytest.raises(EvolutionSchemaError, match="列集合"):
        EvolutionStore(uri=str(wrong_cols_dir), evidence_root=root,
                       runs_evidence_root=tmp_path / "runs")

    # 正常表二次构造：校验通过（同构），不重建、不清空
    ok_dir = tmp_path / "ok"
    ok_dir.mkdir()
    ok = EvolutionStore(uri=str(ok_dir), evidence_root=root,
                        runs_evidence_root=tmp_path / "runs")
    ok.table(create=True)
    assert EvolutionStore(uri=str(ok_dir), evidence_root=root,
                          runs_evidence_root=tmp_path / "runs").table_exists()


# ---------------------------------------------------------------------------
# 2. 12+3 列契约 + 写入约束矩阵
# ---------------------------------------------------------------------------

def test_evolution_columns_constraints(tmp_path):
    p1, p2, p2_root = _new_stores(tmp_path)

    # --- 12+3 schema 逐字同构对质（同构性的验证方法：前 12 名同序、类型逐列相等）
    evo_schema = p2._schema()
    base_schema = p1._schema()
    assert COLUMNS[:12] == tuple(base_schema.names)
    assert tuple(evo_schema.names) == COLUMNS and len(COLUMNS) == 15
    for name in COLUMNS[:12]:
        assert evo_schema.field(name).type == base_schema.field(name).type, \
            f"{name} 类型与 phase1 不同构"
    assert evo_schema.field("evolution_round").type == pa.int64()
    assert evo_schema.field("parent_run_id").type == pa.string()
    assert evo_schema.field("pareto_gain_ratio").type == pa.float64()
    assert evo_schema.field("vector").type.list_size == p2.dim == 768

    # 证据件（双根校验复用继承实现）
    evo_md = p2_root / "evolutions" / "evolution_v1_to_v2_r1.md"
    evo_md.parent.mkdir(parents=True, exist_ok=True)
    evo_md.write_text("# fixture evolution", encoding="utf-8")
    rel = "evolutions/evolution_v1_to_v2_r1.md"

    base_kwargs = dict(
        run_id=V2_RUN, track="track2", rung=3, sim_engine="genesis",
        gate_source="gate2_accepted", evidence_relpath=rel,
        evolution_round=1, parent_run_id=V1_RUN,
        text="冰面低摩擦抗扰 V1→V2 演化：摩擦力 loguniform 域随机化与踝增益自适应。",
        pareto_gain_ratio=None)

    def expect_rejected(**over):
        kw = dict(base_kwargs)
        kw.update(over)
        with pytest.raises(ValueError):
            p2.add_evolution(**kw)

    # evolution_round：缺省/0/负数/bool 一律拒
    expect_rejected(evolution_round=0)
    expect_rejected(evolution_round=-3)
    expect_rejected(evolution_round=True)
    with pytest.raises((TypeError, ValueError)):
        p2.add_evolution(**{k: v for k, v in base_kwargs.items()
                            if k != "evolution_round"})
    # parent_run_id：空串/空白/等于本 run/缺失拒
    expect_rejected(parent_run_id="")
    expect_rejected(parent_run_id="   ")
    expect_rejected(parent_run_id=V2_RUN)
    with pytest.raises((TypeError, ValueError)):
        p2.add_evolution(**{k: v for k, v in base_kwargs.items()
                            if k != "parent_run_id"})
    # pareto_gain_ratio：NaN/Inf/bool/字符串拒；None 合法（可空）
    expect_rejected(pareto_gain_ratio=float("nan"))
    expect_rejected(pareto_gain_ratio=float("inf"))
    expect_rejected(pareto_gain_ratio=float("-inf"))
    expect_rejected(pareto_gain_ratio=True)
    expect_rejected(pareto_gain_ratio="0.5")
    # failure 类 gate_source 不允许进本表（失败学习只进 phase1）
    expect_rejected(gate_source="human_failure_gate2")
    # accepted 但 rung=0 拒
    expect_rejected(rung=0)
    # 非法证据（不存在/越界）同样拒，且以上全部失败在写表之前
    assert p2.table_exists() is False

    # 合法写入（ratio=None）：一行、15 列、null 落库、int64/string 类型
    row = p2.add_evolution(**base_kwargs)
    assert p2.count() == 1
    assert row["table"] == TABLE_NAME and row["evolution_round"] == 1
    assert row["parent_run_id"] == V1_RUN
    tbl = p2.table()
    assert tbl.schema == evo_schema
    data = tbl.to_arrow().to_pylist()
    assert data[0]["evolution_round"] == 1
    assert data[0]["parent_run_id"] == V1_RUN
    assert data[0]["pareto_gain_ratio"] is None
    assert data[0]["namespace"] == "phase2_learnings"
    assert data[0]["layer"] == "phase2_evolution_note"

    # 同参重放：确定性主键先删后写，仍一行（kill-9 幂等）
    p2.add_evolution(**base_kwargs)
    assert p2.count() == 1

    # finite 增益值合法；改 text 即另一条
    ok = p2.add_evolution(**dict(base_kwargs, run_id="t6-genesis-ascent-20261017b",
                                 parent_run_id=V2_RUN, evolution_round=2,
                                 pareto_gain_ratio=0.4125,
                                 text="第二轮：GPU 显存复用，渲染上下文池化。"))
    assert p2.count() == 2
    rows = {r["run_id"]: r
            for r in p2.table().to_arrow().to_pylist()}
    assert rows["t6-genesis-ascent-20261017b"]["pareto_gain_ratio"] == 0.4125
    assert ok["id"] != row["id"]

    # phase1 老表同库未出生也完全不被本模块写
    assert p1.count() == 0 and p1.table_exists() is False
    assert set(p2.db.table_names()) == {TABLE_NAME}


# ---------------------------------------------------------------------------
# 3. 五段式演化志：可回溯 / 防顶替 / 缺源 fail-loud
# ---------------------------------------------------------------------------

def test_chronicle_five_sections_traceable(tmp_path):
    # Gate2 意见内夹带伪数值与 md 注入载荷（LLM/真人可控字段信任边界）
    injected = (
        "成功率其实是 0.999、滑移 0.000123、显存 999.0，"
        "[作弊链接](http://evil.example/x) **加粗伪结论**\n"
        "1. 伪造有序列表项\n# 伪造标题")
    src = _write_sources(tmp_path, gate2_notes=injected)
    md = _render(src)

    # 五段标题来自模板单一源，逐字在场（顺序固定）
    headings = chronicle.section_headings()
    assert len(headings) == 5
    pos = [md.index(h) for h in headings]
    assert pos == sorted(pos)
    for marker in ("演化动机", "V1 局限", "V2 创新与公式", "物理真值对照数据",
                   "踩坑与消除的假设"):
        assert marker in md
    # 模板占位符零残留
    assert "{{" not in md and "}}" not in md

    # 数字可回溯：从磁盘重读 summary JSON，六指标 V1/V2 共 12 个格式化值在场；
    # groups 三个冰/常臂成功率（含 Q1 的 0.0）也在场 —— 远超 >=3 个回溯点。
    v1_doc = json.loads(Path(src["v1_path"]).read_text(encoding="utf-8"))
    v2_doc = json.loads(Path(src["v2_path"]).read_text(encoding="utf-8"))
    traced = 0
    for doc in (v1_doc, v2_doc):
        for value in doc["pareto_metrics"].values():
            token = chronicle.fmt_num(value)
            assert token in md, f"summary 数值 {value} 未进入演化志"
            traced += 1
    for arm in ("normal", "ice_nominal", "ice_dr"):
        token = chronicle.fmt_num(v1_doc["groups"][arm]["success_rate"])
        assert token in md
        traced += 1
    assert traced >= 15
    # 独特值双保险：V1/V2 同指标不同值都在场（Δ 机算 0.333333 等）
    assert "| 成功率 success_rate | 越大越好 | 0.333333 | 0.666667 | 0.333333 |" in md
    assert "132.4" in md and "28.6" in md

    # Gate 伪数值防顶替：指标表行只认 summary 格式化值
    table_rows = [ln for ln in md.splitlines() if ln.startswith("|")]
    success_row = next(ln for ln in table_rows if "success_rate" in ln)
    for fake in ("0.999", "0.000123", "999"):
        assert fake not in success_row
    # 伪数值只允许原样出现在 Gate2 引用块（逐字证据），且仅出现一次
    assert md.count("0.999") == 1
    quote_line = next(ln for ln in md.splitlines() if "0.999" in ln)
    assert quote_line.startswith(">")

    # md 中性化：注入结构字符被反斜杠化（链接/强调/有序列表/标题均失效）
    assert r"\[作弊链接\]\(http://evil.example/x\)" in md
    assert r"\*\*加粗伪结论\*\*" in md
    assert "1\\. 伪造有序列表项" in md
    assert "\n# 伪造标题" not in md  # 被引用块前缀 + # 转义
    assert r"\# 伪造标题" in md
    # 信任标注在场：Gate 数字不作真值；Pareto/contrast 仅参考
    assert "其中数字不作为指标真值" in md
    # 模板抬头信任边界声明 1 次 + Gate1/Gate2 引用块各 1 次
    assert md.count("其中数字不作为指标真值") == 3
    assert "仅参考" in md
    verdict_line = next(ln for ln in md.splitlines() if "v2_dominates" in ln
                        or "non_dominated" in ln or "dominated_by_v1" in ln)
    assert "仅参考" in md
    # contrast 机产块（Q1 矛盾事实）围栏照录，矛盾结论不得被改写
    assert "contradiction_control_not_robust" in md
    assert "0.824" in md and "2.0" in md
    # 创新段：控制律原文围栏 + DR 分布 JSON 围栏
    assert "````text" in md and "````json" in md
    assert r"\alpha" in md and "LogUniform" in md

    # --- 缺源/非法 fail-loud（绝不渲染空段）---
    # Gate2 件缺失
    missing = tmp_path / "sources" / "g2_missing.json"
    with pytest.raises(chronicle.ChronicleSourceError, match="来源件缺失"):
        chronicle.load_evolution_sources(
            v1_summary_path=src["v1_path"], v2_summary_path=src["v2_path"],
            gate1_opinion_path=src["g1_path"], gate2_opinion_path=missing)
    # V2 缺 innovation 块
    bad_v2 = _v2_summary()
    del bad_v2["innovation"]
    with pytest.raises(chronicle.ChronicleSourceError, match="innovation"):
        chronicle.render_evolution_changelog(
            v1_summary=src["v1"], v2_summary=bad_v2,
            gate1_opinion=src["g1"], gate2_opinion=src["g2"],
            parent_run_id=V1_RUN, evolution_round=1)
    # pareto_metrics 缺指标/NaN 拒
    bad_metrics = _v2_summary()
    bad_metrics["pareto_metrics"]["vram_peak_gb"] = float("nan")
    with pytest.raises(chronicle.ChronicleSourceError, match="finite"):
        chronicle.render_evolution_changelog(
            v1_summary=src["v1"], v2_summary=bad_metrics,
            gate1_opinion=src["g1"], gate2_opinion=src["g2"],
            parent_run_id=V1_RUN, evolution_round=1)
    # Gate 意见非 approve / 空意见 / schema 错 拒
    rejected = _opinion("gate2", "x")
    rejected["decision"] = "reject_with_comments"
    with pytest.raises(chronicle.ChronicleSourceError, match="approve"):
        chronicle.render_evolution_changelog(
            v1_summary=src["v1"], v2_summary=src["v2"],
            gate1_opinion=src["g1"], gate2_opinion=rejected,
            parent_run_id=V1_RUN, evolution_round=1)
    empty_notes = _opinion("gate1", "   ")
    with pytest.raises(chronicle.ChronicleSourceError, match="human_notes"):
        chronicle.render_evolution_changelog(
            v1_summary=src["v1"], v2_summary=src["v2"],
            gate1_opinion=empty_notes, gate2_opinion=src["g2"],
            parent_run_id=V1_RUN, evolution_round=1)
    # parent_run_id 与 v1_summary 不一致 / round=0 拒
    with pytest.raises(chronicle.ChronicleSourceError, match="不一致"):
        _render(src, parent="some-other-run")
    with pytest.raises(chronicle.ChronicleSourceError, match="evolution_round"):
        _render(src, round_=0)
    # 四反引号注入控制律 → 拒渲染（防围栏突破）
    fence_v2 = _v2_summary()
    fence_v2["innovation"]["control_law"] = "evil ```` break"
    with pytest.raises(chronicle.ChronicleSourceError, match="四反引号"):
        chronicle.render_evolution_changelog(
            v1_summary=src["v1"], v2_summary=fence_v2,
            gate1_opinion=src["g1"], gate2_opinion=src["g2"],
            parent_run_id=V1_RUN, evolution_round=1)

    # 文件名 C4 安全
    fn = chronicle.evolution_filename(
        parent_run_id=V1_RUN, v2_run_id=V2_RUN, evolution_round=1)
    assert fn == f"evolution_{V1_RUN}_to_{V2_RUN}_r1.md"
    with pytest.raises(chronicle.ChronicleError):
        chronicle.evolution_filename(
            parent_run_id="../x", v2_run_id=V2_RUN, evolution_round=1)


# ---------------------------------------------------------------------------
# 3b. 评审 M1/M2：groups exact-3 臂与 LOW-6 同严 + opinion source/ts_iso 词表
# ---------------------------------------------------------------------------

def test_chronicle_groups_exact_three_arms_same_strict_as_low6(tmp_path):
    """M1：chronicle groups 校验与 ascent LOW-6 加载器同严——缺臂/多臂/
    数值越界/bool/NaN 一律 fail-loud，错误消息点名缺/多臂。"""
    assert chronicle._GROUPS_ARMS == ascent._GROUPS_ARMS  # 两本地副本漂移必红（chronicle 不能 import ascent 会成环，跨 lane 一致性由测试钉死）
    src = _write_sources(tmp_path)

    def reject_v1(mutate, match):
        v1 = _v1_summary()
        mutate(v1)
        with pytest.raises(chronicle.ChronicleSourceError, match=match):
            chronicle.render_evolution_changelog(
                v1_summary=v1, v2_summary=src["v2"],
                gate1_opinion=src["g1"], gate2_opinion=src["g2"],
                parent_run_id=V1_RUN, evolution_round=1)

    # 缺臂：消息点名所缺臂
    reject_v1(lambda d: d["groups"].pop("ice_dr"), r"缺.*ice_dr")
    # 神秘第四臂：消息点名多余臂
    def _add_mystery(d):
        d["groups"]["mystery_arm"] = dict(d["groups"]["normal"])
    reject_v1(_add_mystery, r"多余.*mystery_arm")
    # 三臂齐全但数值界/类型坏（与 ascent 同口径）
    reject_v1(lambda d: d["groups"]["normal"].__setitem__("n_fell", 4),
              "n_fell")
    reject_v1(lambda d: d["groups"]["normal"].__setitem__("n", True),
              r"\.n 必须")
    reject_v1(
        lambda d: d["groups"]["ice_nominal"].__setitem__(
            "success_rate", float("nan")),
        "success_rate")


def test_chronicle_opinion_source_ts_iso_strict(tmp_path):
    """M2：source 必须是生产词表成员，ts_iso 必须是带 Z/±HHMM 的 ISO-8601；
    每个生产 source 枚举值与 ±HHMM 偏移时间戳均合法照录。"""
    src = _write_sources(tmp_path)

    def reject(gate, **over):
        opinion = _opinion(gate, "真人意见原文")
        opinion.update(over)
        kw = dict(v1_summary=src["v1"], v2_summary=src["v2"],
                  gate1_opinion=src["g1"], gate2_opinion=src["g2"],
                  parent_run_id=V1_RUN, evolution_round=1)
        kw["gate1_opinion" if gate == "gate1" else "gate2_opinion"] = opinion
        with pytest.raises(chronicle.ChronicleSourceError):
            chronicle.render_evolution_changelog(**kw)

    reject("gate1", ts_iso="not-a-time")
    reject("gate1", source="**bold** [x](http://evil.example)")
    reject("gate2", source="card_form\n")
    reject("gate2", ts_iso="2026-10-17 12:28:06Z")   # 空格替 T：不收
    reject("gate1", ts_iso="2026-10-17T12:28:06")    # 无时区后缀：不收
    reject("gate2", ts_iso="2026-10-17T12:28:06Z\n")  # 尾随换行必拒（match 的 $ 放行，fullmatch 拒）

    # 生产词表每个枚举值合法（出处：feishu_gateway/store.py mark_decided）
    for source in ("card_form", "p2p_text", "cli"):
        opinion = _opinion("gate1", "批准升阶")
        opinion["source"] = source
        md = chronicle.render_evolution_changelog(
            v1_summary=src["v1"], v2_summary=src["v2"],
            gate1_opinion=opinion, gate2_opinion=src["g2"],
            parent_run_id=V1_RUN, evolution_round=1)
        assert f"渠道 {source}。" in md

    # ±HHMM 偏移（graph/nodes.py _ev 的 strftime %z 生产写法）合法并照录
    offset = _opinion("gate2", "同意结案")
    offset["ts_iso"] = "2026-10-17T20:28:06+0800"
    md = chronicle.render_evolution_changelog(
        v1_summary=src["v1"], v2_summary=src["v2"],
        gate1_opinion=src["g1"], gate2_opinion=offset,
        parent_run_id=V1_RUN, evolution_round=1,
        generated_at=offset["ts_iso"])
    assert "2026-10-17T20:28:06+0800" in md


# ---------------------------------------------------------------------------
# 4. 断网语义召回（本地 mpnet，新表可召回，老表不污染）
# ---------------------------------------------------------------------------

def test_recall_new_table_offline(tmp_path):
    # 断网 env 真实在场（fastembed 只允许走本地缓存）
    assert os.environ["HF_HUB_OFFLINE"] == "1"
    assert os.environ["TRANSFORMERS_OFFLINE"] == "1"
    assert os.environ["HF_DATASETS_OFFLINE"] == "1"

    p1, p2, p2_root = _new_stores(tmp_path)
    ev_dir = p2_root / "evolutions"
    ev_dir.mkdir(parents=True)

    ice_text = (
        "## 一、演化动机\n冰面低摩擦地面上 V1 人形平衡控制失效：35N 推力后"
        "双脚打滑、机器人倾覆；V2 对摩擦系数做域随机化并按摩擦估计自适应踝部"
        "PD 增益，提升结冰地面抗扰平衡。\n"
        "## 二、V1 局限\n结冰名义臂成功率 0，三跑全部摔倒，推力后滑移速度失控。")
    vram_text = (
        "## 一、演化动机\nGPU 显存峰值超限导致 Genesis 离屏渲染 CUDA OOM，"
        "墙钟时间被纹理池反复初始化拉长；V2 复用渲染上下文、降低离屏分辨率并"
        "池化 CUDA buffer。\n## 二、V1 局限\nvram 峰值贴顶、solve wallclock 偏高。")
    (ev_dir / "ice.md").write_text(ice_text, encoding="utf-8")
    (ev_dir / "vram.md").write_text(vram_text, encoding="utf-8")
    p2.add_evolution(run_id="run-ice", track="track2", rung=3,
                     sim_engine="genesis", gate_source="gate2_accepted",
                     evidence_relpath="evolutions/ice.md",
                     evolution_round=1, parent_run_id="run-ice-v1",
                     text=ice_text)
    p2.add_evolution(run_id="run-vram", track="track2", rung=2,
                     sim_engine="genesis", gate_source="gate2_accepted",
                     evidence_relpath="evolutions/vram.md",
                     evolution_round=1, parent_run_id="run-vram-v1",
                     text=vram_text)
    assert p2.count() == 2

    # 同库 phase1 种一行：跨表隔离对质
    p1_ev = tmp_path / "phase1" / "telemetry.json"
    p1_ev.write_text("{}", encoding="utf-8")
    p1.add_learning(run_id="p1-row", track="track1", rung=1,
                    sim_engine="mujoco", gate_source="gate2_accepted",
                    evidence_relpath="telemetry.json",
                    failure_symptom="EGL 无头渲染初始化失败",
                    fix_action="导出 MUJOCO_GL=egl 并装 libegl1",
                    human_opinion="", effective=True)
    assert p1.count() == 1

    # 语义召回：冰面抗扰查询 → ice 行第一；显存/OOM 查询 → vram 行第一
    ice_hits = p2.query("结冰地面摩擦力很低 推力之后双脚打滑摔倒 怎么平衡",
                        top_k=2)
    assert ice_hits[0]["run_id"] == "run-ice"
    vram_hits = p2.query("显存不够 CUDA out of memory 渲染 墙钟耗时", top_k=2)
    assert vram_hits[0]["run_id"] == "run-vram"
    assert all("vector" not in r for r in ice_hits + vram_hits)

    # 表隔离：tmp 库只有两张学习表，kb_chunks 未出生；老表行数不变
    assert set(p2.db.table_names()) == {TABLE_NAME, P1_TABLE}
    assert p1.count() == 1 and p2.count() == 2
    assert "kb_chunks" not in p2.db.table_names()
    assert "solution_chunks" not in p2.db.table_names()


# ---------------------------------------------------------------------------
# 5. LOW-3：v0/损坏存储 → 专属异常 + 处置文案
# ---------------------------------------------------------------------------

def test_low3_schema_v0_message(tmp_path):
    uri = tmp_path / "lance"
    root = tmp_path / "p2"
    store = EvolutionStore(uri=str(uri), evidence_root=root,
                           runs_evidence_root=tmp_path / "runs")
    store.table(create=True)  # 空表也会落 manifest

    manifests = list((Path(uri) / f"{TABLE_NAME}.lance" / "_versions").glob(
        "*.manifest"))
    assert manifests, "Lance manifest 未出生，无法模拟 v0 损坏"
    manifests[0].write_bytes(b"v0corrupt")  # 截断式损坏（异版本/不可识别件）

    with pytest.raises(EvolutionSchemaError) as ei:
        EvolutionStore(uri=str(uri), evidence_root=root,
                       runs_evidence_root=tmp_path / "runs")
    msg = str(ei.value)
    # 处置四要素：人工备份 → drop 本新表 → 重建 → 老两表/solution_chunks 不受影响
    assert "人工备份" in msg
    assert f"drop {TABLE_NAME}" in msg
    assert "重建" in msg
    assert "kb_chunks" in msg and "phase1_learnings" in msg
    assert "solution_chunks" in msg
    assert "ALTER" in msg  # 明确禁止自愈
    # 专属类型（RuntimeError 子类），调用方可精确捕获且不得降级成默认建表
    assert isinstance(ei.value, EvolutionSchemaError)


# ---------------------------------------------------------------------------
# 接缝回归 6：standard accept 零 phase2 副作用，返回形状逐字节旧口径
# ---------------------------------------------------------------------------

def _accept_state(tmp_path: Path, *, run_mode: "str | None" = None,
                  thread: str = "t-standard-1") -> dict:
    p1_root = tmp_path / "phase1"
    tele_dir = p1_root / "runs" / thread
    tele_dir.mkdir(parents=True, exist_ok=True)
    tele = tele_dir / "telemetry.json"
    tele.write_text(json.dumps({"status": "ok"}), encoding="utf-8")
    state = {
        "thread_id": thread,
        "track": "track1",
        "chosen": {"sim_engine": "mujoco"},
        "agent_runs": [{
            "status": "ok", "telemetry_uri": str(tele),
            "coding_round": 1, "timeline": []}],
        "metrics": {"rungs": {"rung1": {"status": "ok"},
                              "rung2": {"status": "ok"}}},
        "events": [{"type": "gate2_accepted"}],
    }
    if run_mode is not None:
        state["run_mode"] = run_mode
    return state


def test_standard_accept_path_no_phase2(tmp_path):
    p1, p2, p2_root = _new_stores(tmp_path)
    seam = make_learning_seam(store=p1, evolution_store=p2)

    state = _accept_state(tmp_path, run_mode="standard")
    row = seam(state)

    assert p1.count() == 1
    assert p2.table_exists() is False
    assert not (p2_root / "evolutions").exists()
    # 旧返回形状：table/gate_source/rung/evidence_relpath，无 evolution 注记
    assert row["table"] == P1_TABLE
    assert row["gate_source"] == "gate2_accepted" and row["rung"] == 2
    assert "evolution" not in row

    # 无 run_mode 键（老状态机）同样不走 ascent 分支
    state2 = _accept_state(tmp_path / "b", thread="t-standard-2")
    p1_b, p2_b, p2_root_b = _new_stores(tmp_path / "b")
    seam_b = make_learning_seam(store=p1_b, evolution_store=p2_b)
    row_b = seam_b(state2)
    assert p1_b.count() == 1 and p2_b.table_exists() is False
    assert "evolution" not in row_b
    assert not (p2_root_b / "evolutions").exists()


# ---------------------------------------------------------------------------
# 接缝回归 7：ascent accept 渲染落盘 + 两表各一行 + 重放幂等
# ---------------------------------------------------------------------------

def test_ascent_seam_chronicle_and_phase2(tmp_path):
    p1, p2, p2_root = _new_stores(tmp_path)
    src = _write_sources(tmp_path)
    state = _accept_state(tmp_path, run_mode="ascent", thread=V2_RUN)
    state["track"] = "track2"
    state["chosen"] = {"sim_engine": "genesis"}
    state["v1_archive"] = {"thread_id": V1_RUN,
                           "host_workdir": str(tmp_path / "v1work")}
    state["evolution_sources"] = {
        "v1_summary_path": str(src["v1_path"]),
        "v2_summary_path": str(src["v2_path"]),
        "gate1_opinion_path": str(src["g1_path"]),
        "gate2_opinion_path": str(src["g2_path"]),
        "evolution_round": 1,
        "pareto_gain_ratio": 0.4125,
    }
    seam = make_learning_seam(store=p1, evolution_store=p2)

    row = seam(state)
    assert p1.count() == 1 and p2.count() == 1
    expected_fn = f"evolution_{V1_RUN}_to_{V2_RUN}_r1.md"
    md_path = p2_root / "evolutions" / expected_fn
    assert md_path.is_file()
    md_bytes = md_path.read_bytes()
    for h in chronicle.section_headings():
        assert h.encode() in md_bytes

    evo = row["evolution"]
    assert evo["table"] == TABLE_NAME
    assert evo["evidence_relpath"] == f"evolutions/{expected_fn}"
    assert evo["evolution_round"] == 1 and evo["parent_run_id"] == V1_RUN
    evo_row = p2.table().to_arrow().to_pylist()[0]
    assert evo_row["run_id"] == V2_RUN
    assert evo_row["parent_run_id"] == V1_RUN
    assert evo_row["evolution_round"] == 1
    assert evo_row["pareto_gain_ratio"] == 0.4125
    assert evo_row["text"].encode() == md_bytes
    # 文件权限 0600 / 目录 0700
    assert (md_path.stat().st_mode & 0o777) == 0o600
    assert ((p2_root / "evolutions").stat().st_mode & 0o777) == 0o700

    # kill-9 resume：同参再结案，两表仍各一行、md 逐字节不变（确定性时间锚）
    row2 = seam(state)
    assert p1.count() == 1 and p2.count() == 1
    assert md_path.read_bytes() == md_bytes
    assert row2["evolution"]["id"] == evo["id"]

    # 缺 evolution_sources：写库前 fail-loud（两表都不新增、md 不出生）
    bad = dict(state)
    bad["evolution_sources"] = {"evolution_round": 1}
    with pytest.raises(GraphStateError, match="缺必填键"):
        seam(bad)
    assert p1.count() == 1 and p2.count() == 1

    # 缺 v1_archive：同样 fail-loud
    no_arc = {k: v for k, v in state.items() if k != "v1_archive"}
    with pytest.raises(GraphStateError, match="v1_archive"):
        seam(no_arc)
    assert p1.count() == 1 and p2.count() == 1


# ---------------------------------------------------------------------------
# 接缝回归 7b：nit-3 非法 slug 的 ChronicleError 经接缝归一 GraphStateError
# ---------------------------------------------------------------------------

def test_ascent_seam_bad_slug_raises_graph_state_error(tmp_path):
    """evolution_filename 的 ChronicleError(ValueError) 不得从接缝裸逃逸：
    非法父运行 id 必须归一 GraphStateError，且在任何写库/落盘之前失败。"""
    p1, p2, p2_root = _new_stores(tmp_path)
    src = _write_sources(tmp_path)
    bad_v1 = _v1_summary()
    bad_v1["run_id"] = "../x"        # 渲染对质通过，文件名 C4 slug 校验必拒
    Path(src["v1_path"]).write_text(
        json.dumps(bad_v1, ensure_ascii=False, indent=2), encoding="utf-8")
    state = _accept_state(tmp_path, run_mode="ascent", thread=V2_RUN)
    state["track"] = "track2"
    state["chosen"] = {"sim_engine": "genesis"}
    state["v1_archive"] = {"thread_id": "../x",
                           "host_workdir": str(tmp_path / "v1work")}
    state["evolution_sources"] = {
        "v1_summary_path": str(src["v1_path"]),
        "v2_summary_path": str(src["v2_path"]),
        "gate1_opinion_path": str(src["g1_path"]),
        "gate2_opinion_path": str(src["g2_path"]),
        "evolution_round": 1,
    }
    seam = make_learning_seam(store=p1, evolution_store=p2)

    with pytest.raises(GraphStateError, match="演化志渲染被拒"):
        seam(state)
    # 失败在写库/落盘之前：两表零行、evolutions 目录不出生
    assert p1.count() == 0 and p2.count() == 0
    assert p2.table_exists() is False
    assert not (p2_root / "evolutions").exists()


# ---------------------------------------------------------------------------
# 接缝回归 8：failure 结案永不触达 phase2（即便 run_mode=ascent）
# ---------------------------------------------------------------------------

def test_failure_path_never_writes_phase2(tmp_path):
    p1, p2, p2_root = _new_stores(tmp_path)
    state = {
        "thread_id": V2_RUN,
        "track": "track2",
        "run_mode": "ascent",                      # 即使是 ascent 模式
        "chosen": {"sim_engine": "genesis"},
        "agent_runs": [],
        "events": [{"type": "gate1_hard_failure",
                    "human_notes": "升阶假设依据不足"}],
        "v1_archive": {"thread_id": V1_RUN},
    }
    failure_json = tmp_path / "phase1" / "FAILURE.json"
    failure_json.parent.mkdir(parents=True, exist_ok=True)
    failure_json.write_text("{}", encoding="utf-8")
    state["_learning"] = {"kind": "failure", "stage": "gate1",
                          "reason": "Gate1 真人打回", "evidence": str(failure_json)}
    seam = make_learning_seam(store=p1, evolution_store=p2)

    row = seam(state)
    assert row["gate_source"] == "human_failure_gate1" and row["rung"] == 0
    assert p1.count() == 1
    assert p2.table_exists() is False               # 表不出生
    assert not (p2_root / "evolutions").exists()    # 演化志不落盘
    assert "evolution" not in row
