"""Phase 2 Task6 — ascent 流程前置守卫（随 Task2/3 同批，0 付费，全 hermetic）。

计划 L569 + 附录 A L949-953 的 test_task6_ascent_flow.py 五测拆分（锁内适配）：
task6.py 工厂与 V2 track 脚本是 Task6 出生件，出生前可锁定的 3 测随本批落地，
另 2 测随 Task6 提交，不写空壳、不 skip、不打桩：

  本批（3 测）：
  1. test_task6_thread_uses_ascent_classifier
     真实双线程口径：V1 档案登记 thread_id=t8-genesis-20261016a（与生产
     v1_index.json 同构），Task6 运行线程 t6-genesis-ascent-20261017a。
     initial_state(t6…) 经 node_classify(state, GraphDeps(v1_index=夹具dict))
     判 ascent，v1_archive 归并回 t8 档案；classify_run 只按 track+题面
     逐字+sha 命中、从不读 thread_id（payload 改任意线程值结果逐字段相等）。
     若回归成按线程过滤，Task6 会静默 standard 白烧 macro（Task2 C1 同类面）。
  2. test_q1_contradiction_not_overwritten
     Q1 红线（硬规则 7）：冻结 V1 workdir 经 evaluate_batch 独立重算捕获的
     groups/contrast（fixture v1_contrast_q1.json）现算必须逐值同值且保持
     contradiction_control_not_robust + worsened；build_ascent_messages
     必须原样携带矛盾事实（两冰臂 success_rate=0.0，n_fell=3；normal n_fell=2）。
  3. test_gate2_diff_card_wired_task6（可前置半）
     纯选择器 select_gate1_card_view/select_gate2_requestor 的 ascent 分支；
     档案 files 两张 V1 split relpath 经 load 校验、tmp 占位字节 sha256 对质。
  随 Task6（出生件依赖，非 skip 占位）：
     - test_run_level_coding_cap_3（需 orchestrator/graph/task6.py 工厂）
     - test_v2_track_script_telemetry_contract（需 V2 track 脚本）
     - V1 双 split 的 concat 冻结与 V2 推送同样在 task6.py 工厂出生，本测
       不实现、不打桩（见 test_gate2_diff_card_wired_task6 内留痕）。

全程不触网络/方舟/飞书/真 V1 workdir（权威值已一次性离线捕获进夹具）。
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
import subprocess
from pathlib import Path

import pytest

from orchestrator.feishu_gateway import client as client_mod
from orchestrator.feishu_gateway.client import (
    FeishuGateway,
    select_gate1_card_view,
    select_gate2_requestor,
)
from orchestrator.graph import task6 as task6_mod
from orchestrator.graph import task8
from orchestrator.graph.ascent import (
    ASCENT,
    STANDARD,
    V1_INDEX_SCHEMA,
    build_ascent_messages,
    classify_run,
    load_v1_index,
)
from orchestrator.graph.nodes import (
    GraphDeps,
    GraphStateError,
    initial_state,
    node_classify,
)
from orchestrator.graph.task8 import SPLITS
from orchestrator.judge.ascent_thresholds import tight_profile
from orchestrator.judge.gait_rungs import (
    contrast_block,
    evaluate_batch,
    recompute_run,
)
from orchestrator.judge.pareto import METRIC_DIRECTIONS
from orchestrator.phase2 import chronicle
from orchestrator.solutions import resolve_solution
from tests.milestone1.test_task8_seam import (
    CSV_FIELDS,
    FIXTURE_SOLUTIONS_ROOT,
    FakeVisualGateway,
    _calibration_v2,
    _csv_row,
    _gait_raw,
    _outcomes_from,
    build_synthetic_batch,
)
from tests.milestone2.test_task2_ascent import _build_fixture_index  # 同款构造
from tests.milestone2.test_task3_diff_cards import (
    APP_ID,
    APP_SECRET,
    OWNER,
    _install_video,
    _kinds,
    _run_in_thread,
    _texts,
    _wait_pending_card,
    make_fake_post,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TS_ISO_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{4})$")

FIX = Path(__file__).parent / "fixtures"
CONTRAST_FIXTURE = FIX / "v1_contrast_q1.json"

T6_THREAD = "t6-genesis-ascent-20261017a"
V1_THREAD = "t8-genesis-20261016a"
SPLIT_RELS = [name for _, name in SPLITS]


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _shim_rc0_timeline() -> list[dict]:
    """run7：Gate2 host-shim 双闸合成夹具（9 rollout torch 进程：9 banner+
    9 atexit site=torch；V2 batch 父 + gpu_sampler：2 idle；A=9/B=0/C=0）。

    真实 run 的锚由各 rollout 子进程经纯 stderr atexit 写出，本 e2e 接缝测
    只验 Gate2 卡链/四媒体顺序，时间线按生产行格式合成（run7 零实跑）。
    """
    banner = "".join(
        f"[simpraxis-host-shim v1] 平台托底激活（非模型代码） site=torch "
        f"pid=2{i}00 label=genesis_humanoid_dr_v2.py torch=2.4.1+cu124\n"
        for i in range(9))
    torch_atexit = "".join(
        f"[simpraxis-host-shim v1] 平台托底（非模型代码） site=torch "
        f"pid=2{i}01 label=genesis_humanoid_dr_v2.py "
        f"array_migrated=1 numpy_migrated=0 cpu_grad_detached=0\n"
        for i in range(9))
    idle = "".join(
        f"[simpraxis-host-shim v1] 平台托底（非模型代码） site=idle "
        f"pid=3{i}00 label={name} "
        f"array_migrated=0 numpy_migrated=0 cpu_grad_detached=0\n"
        for i, name in enumerate(
            ("genesis_humanoid_dr_v2.py-batch", "gpu_sampler.py")))
    tail = banner + torch_atexit + idle
    return [{"exec": 1, "rc": 0, "stderr_tail": "",
             "stderr_only_tail": tail,
             "stdout_chars": 0, "stderr_chars": len(tail)}]


# ---------------------------------------------------------------------------
# 1. 分类节点：t6 新线程 + 冻结题 → ascent，v1_archive 归并到 V1 t8 档案
# ---------------------------------------------------------------------------

def test_task6_thread_uses_ascent_classifier(tmp_path):
    index, workdir = _build_fixture_index(tmp_path)
    question = index["entries"][0]["question"]  # 冻结题面取自夹具，禁止重打

    state = initial_state(thread_id=T6_THREAD, track="track2", question=question)
    out = node_classify(state, GraphDeps(v1_index=index))

    assert out["run_mode"] == ASCENT
    assert "events" not in out and out["ascent_targets"]
    arc = out["v1_archive"]
    # 跨线程归并：分类只按 track + 题面逐字/sha，与当前线程 id 无关——
    # Task6 跑在新线程 t6-…，复用的是冻结 V1 档案 t8-…。
    assert state["thread_id"] == T6_THREAD
    assert arc["thread_id"] == V1_THREAD
    assert arc["thread_id"] != state["thread_id"]   # 锁死「新线程复用旧档案」
    assert arc["host_workdir"] == str(workdir)
    assert [f["relpath"] for f in arc["files"]][:3] == [
        "summary.json", SPLIT_RELS[0], SPLIT_RELS[1]]

    # classify_run 只按 track+题面逐字+sha 命中，从不读 thread_id：payload
    # 显式携带运行线程 t6 与任意别的线程值，分类结果必须逐字段相等。
    r_t6 = classify_run(
        {"track": "track2", "question": question, "thread_id": T6_THREAD},
        index)
    r_other = classify_run(
        {"track": "track2", "question": question,
         "thread_id": "any-other-thread-9z9"}, index)
    assert r_t6 == r_other == out
    assert r_t6["v1_archive"]["thread_id"] == V1_THREAD

    # 对照：同一 t6 线程 id 配未登记题面 → standard（线程前缀不构成命中）
    unregistered = initial_state(
        thread_id=T6_THREAD, track="track2", question="一道未登记的全新题面")
    assert node_classify(unregistered, GraphDeps(v1_index=index)) == {
        "run_mode": STANDARD, "v1_archive": None, "ascent_targets": {}}


# ---------------------------------------------------------------------------
# 2. Q1 矛盾红线：冻结重算值不得被「清洗」/改判（硬规则 7）
# ---------------------------------------------------------------------------

def test_q1_contradiction_not_overwritten(tmp_path):
    """Q1 反向实验结果矛盾即结论，禁止调参/改界/改名翻案。

    夹具 groups/contrast 是 2026-10-17 对冻结 V1 workdir 跑 evaluate_batch
    （三级独立重算口径）一次性离线只读捕获的产物，禁止手工编辑。任何
    「清洗」该矛盾结论或改判的改动必须先过用户（预注册反向结果禁止翻案）。
    """
    doc = json.loads(CONTRAST_FIXTURE.read_text(encoding="utf-8"))
    fresh = contrast_block(doc["groups"])
    # 现算结果与捕获值逐值相等（防双向漂移：实现改判或夹具被改都立刻红）
    assert fresh == doc["contrast"]
    assert fresh["hypothesis_status"] == "contradiction_control_not_robust"
    assert fresh["control_stands"] is False
    assert fresh["ordered_slip"] is False
    assert fresh["ice_over_normal_postpush_ratio"] == 0.824
    assert fresh["preregistered_ratio"] == 2.0
    assert fresh["dr_vs_nominal"]["direction"] == "worsened"

    # 批判段必须原样携带矛盾事实（数值经 v1_summary 夹具同一构造口径入档）
    index, _ = _build_fixture_index(tmp_path / "cls")
    classified = node_classify(
        initial_state(thread_id=T6_THREAD, track="track2",
                      question=index["entries"][0]["question"]),
        GraphDeps(v1_index=load_v1_index(tmp_path / "cls" / "v1_index.json")))
    user_body = "USER-BODY"
    msgs = build_ascent_messages(
        [{"role": "system", "content": "SYS"},
         {"role": "user", "content": user_body}],
        v1_archive=classified["v1_archive"],
        ascent_targets=classified["ascent_targets"])
    assert msgs[0] == {"role": "system", "content": "SYS"}
    text = msgs[1]["content"]
    # 只做前缀式插入：system 不动、user 原文逐字节保留
    assert text.endswith(user_body)
    head = text[: -len(user_body)]
    # 真实档案数值经 render_v1_metrics 进批判段（先实跑确认的全角逗号字面量）：
    # 结冰名义/结冰+DR 两臂各一次 success_rate=0.0，n_fell=3
    assert text.count("success_rate=0.0，n_fell=3") == 2
    # 正常臂：0.333… 成功率 + 3 跑倒 2（对照不稳健的事实）在场
    assert "success_rate=0.3333333333333333，n_fell=2" in text
    # 插入段不得出现把矛盾「解决掉」的覆盖性文案，防翻案硬约束必须在场
    for banned in ("已解决", "矛盾已改善", "contradiction_control_not_robust →",
                   "hypothesis_status 映射"):
        assert banned not in head
    assert "禁止凑加权总分翻案" in head
    # 插入段不消费/改名矛盾状态串（render_v1_metrics 无此字段）
    assert "contradiction_control_not_robust" not in head
    # render_v1_metrics 不渲染 hypothesis_status/0.824（Q1 裁决显式进 Gate2/
    # 演化志是 Task6 接线设计项）：本测不对其入批判段做断言。


# ---------------------------------------------------------------------------
# 3. Gate2 双媒体 Diff 接线（可前置半：选择器 + 双 split 路径解析对质）
# ---------------------------------------------------------------------------

def test_gate2_diff_card_wired_task6(tmp_path, monkeypatch):
    # 选择器：ascent 形态 → 升阶卡/双媒体 Diff；其余一律标准请求器
    assert select_gate1_card_view({"run_mode": ASCENT}) == "ascent"
    for state in ({}, None, {"run_mode": STANDARD}):
        assert select_gate1_card_view(state) == STANDARD
        assert select_gate2_requestor(state) == STANDARD
    assert select_gate2_requestor({"run_mode": ASCENT}) == "diff"

    # 档案登记的两张 V1 split：tmp 占位字节（路径占位非视频帧，不违反 numpy
    # 假帧禁令）sha256 现场复算；经生产 load 校验必须通过。
    index, workdir = _build_fixture_index(tmp_path)
    loaded = load_v1_index(tmp_path / "v1_index.json")
    entry = loaded["entries"][0]
    files_by_rel = {f["relpath"]: f for f in entry["files"]}
    assert SPLIT_RELS[0] in files_by_rel and SPLIT_RELS[1] in files_by_rel

    split_paths = []
    for rel in SPLIT_RELS:
        p = workdir / rel
        assert p.is_file(), f"登记 split 缺失：{p}"
        # join(host_workdir, rel) 字面锁定且不越界
        assert p == Path(entry["host_workdir"]) / rel
        assert p.is_relative_to(workdir) and ".." not in p.parts
        assert _sha256_file(p) == files_by_rel[rel]["sha256"]
        split_paths.append(p)
    assert split_paths[0] != split_paths[1]

    # 锁内适配留痕：V1 双 split 的 concat 冻结与 V2 推送在 Task6 task6.py
    # 工厂出生；附录 A 的 run 级 3 轮 cap 与 V2 track script 遥测契约两测
    # 同样随 Task6——此处不实现、不打桩。

    # ==================================================================
    # 生产接线半（随 Task6 出生）：真 FeishuGateway(start_ws=False)+假 HTTP
    # 走完 Gate1 ascent 卡（真人批准）→ 真 task6 evaluate（record_only 下
    # 零视觉实呼）→ Gate2 四媒体严格顺序 + 恰好一张 Δ 卡（真人批准），
    # 四来源件随后通过生产 chronicle 校验。全程 0 联网/0 付费。
    # ==================================================================
    calls: list[dict] = []
    pushes: list[str] = []
    monkeypatch.setattr(client_mod.requests, "post", make_fake_post(calls))
    # 夹具 V1 split 是 10 字节占位：生产锁定字节表本测清空（sha256 仍逐件
    # 对质，路径仍只许 host_workdir+登记 relpath）。
    monkeypatch.setattr(task6_mod, "LOCKED_V1_SPLIT_SIZES", {})

    # Task4 终裁 record_only（2026-10-19 用户签收）：task8 接缝读 active 闸
    # 必须发生在任何抽帧之前。
    cal_dir = tmp_path / "phase1" / "task8_track2_judge"
    cal_dir.mkdir(parents=True)
    (cal_dir / "calibration_gait.json").write_text(
        json.dumps({"mode": "record_only"}), encoding="utf-8")
    monkeypatch.setenv("SIMPRAXIS_PHASE1_ROOT", str(tmp_path / "phase1"))

    # V2 真 batch（三臂×3 籽全存活；两张真 mp4 split 由步态合成仪器产出）。
    v2_wd = tmp_path / "v2batch"
    build_synthetic_batch(v2_wd, falls={})
    (v2_wd / task6_mod.VRAM_NAME).write_text(
        json.dumps({task6_mod.VRAM_KEY: 18.42}), encoding="utf-8")
    innovation = {
        "control_law": "V2 阻抗律：u=-Kp(θ−θ*)−Kd·θ̇，Kp/Kd 按臂分级",
        "dr_distribution": {"mu": "loguniform[0.04,0.45]",
                            "Kp": "loguniform[8000,900000]",
                            "Kd": "loguniform[12,180]"},
        "eliminated_assumptions": ["名义 μ=0.05 单点即代表冰面"],
    }
    (v2_wd / task6_mod.INNOVATION_NAME).write_text(
        json.dumps(innovation, ensure_ascii=False), encoding="utf-8")

    sdir = task6_mod.sources_dir(T6_THREAD)
    # 夹具 V1 档案的 9 份 telemetry 是桩件（不能真重算）；生产 V1 真重算由
    # test_v2_track_script_telemetry_contract 另走真 batch 覆盖。本 half 的
    # V1 增强 summary 用 2026-10-17 离线捕获的 Q1 groups/contrast 替身
    # materialize——0.824<2.0/contradiction_control_not_robust 原样进入来源件。
    q1 = json.loads(CONTRAST_FIXTURE.read_text(encoding="utf-8"))
    v1_vector = task6_mod._pareto_vector(q1["groups"], 9.0, 20.0)

    def _fake_materialize(thread_id, v1_archive):
        assert thread_id == T6_THREAD
        assert v1_archive["thread_id"] == V1_THREAD
        doc = task6_mod._augmented_summary(
            run_id=V1_THREAD, groups=q1["groups"],
            pareto_metrics=v1_vector, contrast=q1["contrast"])
        return doc, task6_mod._write_json(sdir / "v1_summary.json", doc)

    monkeypatch.setattr(task6_mod, "materialize_v1_summary",
                        _fake_materialize)

    proposals = [{
        "name": "V2 阻抗分级 + DR 应力端外扩",
        "sim_engine": "genesis",
        "first_principles": "低摩擦下以阻抗稳定替代高增益踩冰",
        "expected_artifacts": ["三臂×3 籽 batch", "两张对照分屏"],
        "ascent_claims": [{
            "metric": "success_rate", "direction": 1,
            "task": "ice_nominal/ice_dr 各 3 籽", "target": 1.0,
            "epsilon": 0.0}],
    }]
    arc = loaded["entries"][0]
    state0 = {
        "thread_id": T6_THREAD, "v1_archive": arc, "proposals": proposals,
        "chosen": proposals[0], "agent_runs": [], "events": [],
        "gate1_rejects": 0,
    }

    # ---- 真 task6 evaluate 接缝：record_only 必须零视觉实呼 ----
    visual = FakeVisualGateway(_gait_raw())
    eval_seam = task6_mod.make_evaluate_seam(
        visual, "T6-B2-visual", T6_THREAD)
    metrics = eval_seam({
        **state0,
        "telemetry_uri": str(v2_wd / "summary.json"),
        "agent_runs": [{"run_idx": 1, "status": "ok"}],
    })
    assert visual.visual_calls == []
    r4 = metrics["rungs"]["rung4"]
    assert r4["zero_visual_calls"] is True and r4["mode"] == "record_only"
    asc = metrics["ascent"]
    assert asc["parent_run_id"] == V1_THREAD
    assert [r["metric"] for r in asc["metrics_diff"]] == list(METRIC_DIRECTIONS)
    assert asc["pareto_verdict"] in (
        "v2_dominates", "non_dominated", "dominated_by_v1")

    # ---- 真飞书网关（假 HTTP）：Gate1 ascent 卡 → 真人批准 ----
    gw = FeishuGateway(
        APP_ID, APP_SECRET, OWNER,
        db_path=tmp_path / "state" / "pending_gates.db",
        evidence_dir=tmp_path / "task6_feishu",
        run_dir=str(tmp_path / "runs"), start_ws=False)
    _install_video(gw, pushes, calls)

    def _approve(t, box, gate, notes):
        _wait_pending_card(gw, T6_THREAD, gate=gate)
        gw.store.mark_decided(
            T6_THREAD, gate, decision="approve", source="cli",
            human_notes=notes)
        gw._set_event(T6_THREAD, gate)
        t.join(5)
        assert not t.is_alive(), f"Gate{gate} 接缝线程未退出"
        assert box.get("decision") == "approve"

    g1 = task6_mod.make_gate1_seam(gw, T6_THREAD, "Task6 ascent 出生测试")
    t1, box1 = _run_in_thread(lambda: g1(state0))
    _approve(t1, box1, 1, "真人批准：V2 只改控制器/DR，RL 不动，按六指标对质")
    partial = box1["evolution_sources"]
    assert partial["evolution_round"] == 1
    g1_opinion = json.loads(
        Path(partial["gate1_opinion_path"]).read_text(encoding="utf-8"))
    assert g1_opinion == {**g1_opinion, "schema": chronicle.GATE_OPINION_SCHEMA,
                          "gate": "gate1", "decision": "approve",
                          "source": "cli"}
    assert _TS_ISO_RE.fullmatch(g1_opinion["ts_iso"])
    assert Path(partial["v1_summary_path"]).is_file()

    # ---- Gate2：四媒体严格顺序 → 恰好一张 Δ 卡 → 真人批准 ----
    n_before = len(calls)
    rec = {"duration_ms": 8000,
           "cover_png": str(v2_wd / "cover.png"),
           "video_path": str(v2_wd / SPLIT_RELS[0]),
           "timeline": _shim_rc0_timeline()}
    state2 = {**state0, "metrics": metrics, "agent_runs": [rec],
              "evolution_sources": partial, "gate2_rejects": 0}
    g2 = task6_mod.make_gate2_seam(
        gw, T6_THREAD, "Task6 ascent 出生测试",
        trim_for=lambda st, r: None)
    t2, box2 = _run_in_thread(lambda: g2(state2))
    _approve(t2, box2, 2, "真人最终裁决：四媒体与六指标 Δ 表对质通过")

    tail = _kinds(calls)[n_before:]
    assert tail == ["text", "video", "text", "video",
                    "text", "video", "text", "video", "interactive"], tail
    assert _texts(calls)[-4:] == [
        FeishuGateway._DIFF4_LABEL_V1A, FeishuGateway._DIFF4_LABEL_V1B,
        FeishuGateway._DIFF4_LABEL_V2A, FeishuGateway._DIFF4_LABEL_V2B]
    # 推送路径顺序：V1 split1 → V1 split2 → V2 split1 → V2 split2
    assert pushes == [str(workdir / SPLIT_RELS[0]),
                      str(workdir / SPLIT_RELS[1]),
                      str(v2_wd / SPLIT_RELS[0]),
                      str(v2_wd / SPLIT_RELS[1])]
    row = gw.store.get_pending(T6_THREAD, 2)
    for col in ("v1a", "v1b", "v2a", "v2b"):
        assert row[f"media_message_id_{col}"], f"{col} 列未回填媒体 id"
        assert row[f"media_message_id_{col}"].startswith("om_media_")
    block = box2["evolution_sources"]
    assert set(block) == {"v1_summary_path", "v2_summary_path",
                          "gate1_opinion_path", "gate2_opinion_path",
                          "evolution_round"}
    assert block["evolution_round"] == 1
    assert block["v2_summary_path"] == asc["v2_summary_path"]
    g2_opinion = json.loads(
        Path(block["gate2_opinion_path"]).read_text(encoding="utf-8"))
    assert g2_opinion["gate"] == "gate2" and _TS_ISO_RE.fullmatch(
        g2_opinion["ts_iso"])

    # ---- 四来源件过生产 chronicle 全量校验（含 Q1 矛盾原样保留）----
    sources = chronicle.load_evolution_sources(
        v1_summary_path=partial["v1_summary_path"],
        v2_summary_path=asc["v2_summary_path"],
        gate1_opinion_path=partial["gate1_opinion_path"],
        gate2_opinion_path=block["gate2_opinion_path"])
    assert sources["v1_summary"]["run_id"] == V1_THREAD
    assert sources["v2_summary"]["run_id"] == T6_THREAD
    assert sources["v2_summary"]["parent_run_id"] == V1_THREAD
    assert sources["v1_summary"]["contrast"][
        "hypothesis_status"] == "contradiction_control_not_robust"
    assert sources["v1_summary"]["contrast"][
        "ice_over_normal_postpush_ratio"] == 0.824
    gw.close()


# ---------------------------------------------------------------------------
# 4. run 级 coding cap=5（run7：用户 2026-10-23 裁决「2+3 一起做」，
#    cap 3→5 覆盖 plan L193/L544，票 T6-B11-run7；全局 MAX_CODING_ROUNDS=5
#    一字不动；拆块出生本地夹具 cap==3 另见 test_chunked_birth_local_cap_3）
# ---------------------------------------------------------------------------

def test_run_level_coding_cap_5(tmp_path, monkeypatch):
    from orchestrator.agent_runner import runner as runner_mod

    monkeypatch.setenv("SIMPRAXIS_SOLUTIONS_ROOT",
                       str(FIXTURE_SOLUTIONS_ROOT))
    cal_src = tmp_path / "cal_src.json"
    cal_src.write_text(json.dumps(_calibration_v2()), encoding="utf-8")
    monkeypatch.setattr(task8, "CALIBRATION_SOURCE", cal_src)

    # 全局上限仍是 5（Task6 只在 RunSpec 上 run 级收窄，runner 零改动）。
    assert runner_mod.MAX_CODING_ROUNDS == 5

    state = {
        "chosen": {"sim_engine": "genesis", "name": "V2 阻抗+DR"},
        "v1_archive": {"thread_id": V1_THREAD,
                       "host_workdir": str(tmp_path / "v1arch")},
        "agent_runs": [], "events": [], "gate1_rejects": 0,
        "proposals": []}
    factory = task6_mod.make_spec_factory(T6_THREAD, "T6-B1-coding")
    spec = factory(state)

    # run7 轮帽 3→5（用户 2026-10-23 裁决「2+3 一起做」，覆盖 plan
    # L193/L544，票 T6-B11-run7）；全局 MAX_CODING_ROUNDS 仍 5 不动。
    assert spec.max_coding_rounds == task6_mod.RUN_CODING_CAP == 5
    assert runner_mod.MAX_CODING_ROUNDS == 5
    assert spec.billing_task == 6 and spec.batch_id == "T6-B1-coding"
    assert spec.run_timeout_s == 2400
    assert spec.workdir.parent == task6_mod.phase2_runs_root() / "task6_ascent"
    # 同法 VRAM wrapper 前缀：[VENV_PY, <repo 绝对 gpu_sampler.py>,
    # --sidecar, workdir/vram_peak.json, --, <原 V2 batch cmd 9 项>]。
    # wrapper 必须是仓库内现有文件（runner cwd=workdir，以绝对路径启动）。
    sampler = Path(spec.cmd[1])
    assert spec.cmd[0] == task8.VENV_PY
    assert sampler == task6_mod.GPU_SAMPLER
    assert sampler.is_absolute() and sampler.is_file()
    assert sampler == (task6_mod.REPO_ROOT
                       / "orchestrator" / "common" / "gpu_sampler.py")
    assert spec.cmd[2:4] == [
        "--sidecar", str(spec.workdir / task6_mod.VRAM_NAME)]
    assert spec.cmd[4] == "--"
    child_cmd = spec.cmd[5:]
    assert len(child_cmd) == 9
    v2_target = Path(child_cmd[1])
    assert v2_target.name == task6_mod.V2_ENTRYPOINT
    # 出生时 V2 入口不在 workdir：引擎层/resolver 只 stage 冻结 V1 基线。
    assert not v2_target.exists()
    v1_target = spec.workdir / "genesis_humanoid_dr.py"
    assert v1_target.is_file()
    manifest = resolve_solution(
        task8.ENGINE_SCENARIOS["genesis"]["solution_id"])
    assert v1_target.read_bytes() == (
        manifest.solution_dir / "genesis_humanoid_dr.py").read_bytes()
    # `--` 后恰为原 V2 cmd 9 参数序列（一字不增删，仅前置 wrapper）。
    assert child_cmd == [task8.VENV_PY, str(v2_target), "batch",
                         "--out-dir", str(spec.workdir),
                         "--seeds", "3", "--calibration-json",
                         str(spec.workdir / task8.CALIBRATION_NAME)]
    assert spec.env_extra["PYOPENGL_PLATFORM"] == "egl"

    # runner 新文件补丁力学（coding_debug 唯一出生路径）：空文件 old=""
    # 恰好 1 命中，V2 脚本在付费 workdir 内出生。
    born = runner_mod._apply_structured_patch(spec.workdir, {
        "path": task6_mod.V2_ENTRYPOINT, "old": "",
        "new": "#!/usr/bin/env python\n# V2 born via coding_debug\n"})
    assert Path(born).name == task6_mod.V2_ENTRYPOINT and v2_target.is_file()

    # Gate2 打回再跑：全新 run 目录，补丁不继承，V2 重新 absent。
    spec2 = factory({**state, "agent_runs": [{"run_idx": 1}],
                     "events": [{"type": "gate2_rejected",
                                 "human_notes": "DR 应力端再外扩"}]})
    assert spec2.run_idx == 2
    assert spec2.max_coding_rounds == 5  # run7 cap 3→5（同溯源，更新不删除）
    assert spec2.workdir != spec.workdir
    # 新 run 同样包 wrapper；`--` 后的 V2 入口仍 absent（补丁不继承）。
    assert Path(spec2.cmd[1]) == task6_mod.GPU_SAMPLER
    assert spec2.cmd[4] == "--" and len(spec2.cmd[5:]) == 9
    assert not Path(spec2.cmd[spec2.cmd.index("--") + 2]).exists()
    assert spec2.gate2_notes == "DR 应力端再外扩"

    # 守卫：非 genesis / 缺 v1_archive 都 fail-closed，绝不退化成普通 task8。
    with pytest.raises(GraphStateError, match="ascent"):
        factory({**state, "v1_archive": None})
    with pytest.raises(GraphStateError, match="sim_engine"):
        factory({**state, "chosen": {"sim_engine": "mujoco"}})


def test_chunked_birth_local_cap_3_not_raised(tmp_path):
    """P3 新增 pin（run7）：拆块出生路径的轮帽只认 RunSpec 实值——本地夹具
    RunSpec(max_coding_rounds=3) 每轮块 0 即整轮失败时恰耗 3 轮 3 次调用，
    不被 Task6 run 级轮帽 RUN_CODING_CAP==5 误伤。

    tests/milestone1/test_agent_runner.py:1167 的 chunked 夹具本身不动，
    本 pin 独立落在 task6 flow 件（规格 v1.1a §4/§6-P3）。
    """
    import sys
    from orchestrator.agent_runner import AgentRunner, RunSpec
    from orchestrator.agent_runner.runner import ChunkSpec
    from orchestrator.common.callcache import CallReplay

    # 对照前提：run 级轮帽已提 5；runner 永不读本常量，只认 spec。
    assert task6_mod.RUN_CODING_CAP == 5

    wd = tmp_path / "work"
    wd.mkdir(parents=True)
    target = "v2.py"
    assert not (wd / target).exists()

    bad_begin = "<<<<<<< simpraxis.chunk.v1 path=v2.py total=6"  # 缺 index
    bad_response = (bad_begin + "\nx = 1\n"
                    ">>>>>>> simpraxis.chunk.v1.end")

    class _AlwaysBadGateway:
        def __init__(self):
            self.calls: list[dict] = []

        def model_id(self, stage):
            return "fake-coding-cap3-pin"

        def call(self, stage, messages, **kw):
            self.calls.append({"stage": stage, "messages": messages, "kw": kw})
            return bad_response, (None, None)

    class _NoHits:
        def query_kb(self, query, *, namespace, top_k):
            return []

    gw = _AlwaysBadGateway()
    cache = CallReplay(tmp_path / "cache")
    runner = AgentRunner(gw, _NoHits(), evidence_root=tmp_path / "ev",
                         call_cache=cache)
    spec = RunSpec(
        thread_id="t6-cap3pin", workdir=wd,
        cmd=[sys.executable, str(wd / target)],
        telemetry_relpath="telemetry.json", batch_id="T6-CAP3PIN",
        billing_task=6, run_idx=1, max_coding_rounds=3,
        coding_max_tokens=32768, coding_timeout_s=600,
        chunked_birth=ChunkSpec(
            target_path=target, chunk_count=6,
            chunk_titles=(
                "头部/合成常量", "场景/合成注入", "步态/合成控制",
                "采集/合成判定", "batch/合成数据产物", "媒体/合成产物"),
            per_call_sections=(
                ("V2 升阶任务合同（夹具）", "合同夹具"),
                ("已批准 macro 提案（夹具）", '{"name": "fixture"}'),
                ("V1 结构骨架（夹具）", "V1 骨架行")),
            chunk_boundaries=tuple(f"第 {i} 块边界（夹具）。"
                                   for i in range(6))))
    rec = runner.run(spec)
    assert rec["status"] == "failed"
    assert len(gw.calls) == 3, "cap==3 夹具形态必须恰 3 次调用（每轮块0短路）"
    assert rec["coding_round"] == 3
    assert "（RunSpec 上限）" in rec["reason"]
    assert "D10" not in rec["reason"]  # reason redline：cap=3 夹具串也无 D10
    # 目标文件三轮整轮失败后仍未出生；时间线 coding_round 恰 1/2/3。
    assert not (wd / target).exists()
    rounds = [t["coding_round"] for t in rec["timeline"]
              if "coding_round" in t]
    assert rounds == [1, 2, 3]
    # 每次调用都是拆块出生轮的块 0（chunk_index=0 指纹），无 round4/5 触达。
    for call in gw.calls:
        assert call["stage"] == "coding_debug"


# ---------------------------------------------------------------------------
# 5. V2 track script 遥测契约：增强 V2 summary（真重算）过 telemetry.v1 +
#    chronicle；引擎仓永不携带 genesis_humanoid_dr_v2.py
# ---------------------------------------------------------------------------

def test_v2_track_script_telemetry_contract(tmp_path, monkeypatch):
    # record_only 标定（视觉零实呼；本测焦点是遥测/契约不是 Rung4）。
    cal_dir = tmp_path / "phase1" / "task8_track2_judge"
    cal_dir.mkdir(parents=True)
    (cal_dir / "calibration_gait.json").write_text(
        json.dumps({"mode": "record_only"}), encoding="utf-8")
    monkeypatch.setenv("SIMPRAXIS_PHASE1_ROOT", str(tmp_path / "phase1"))

    # 冻结 V1：真 batch（独立重算口径，默认 profile），arc 只带白名单字段。
    v1_wd = tmp_path / "v1batch"
    build_synthetic_batch(v1_wd)
    arc = {
        "schema": V1_INDEX_SCHEMA, "track": "track2",
        "thread_id": V1_THREAD, "host_workdir": str(v1_wd),
        "files": [{"relpath": rel, "sha256": _sha256_file(v1_wd / rel)}
                  for rel in SPLIT_RELS],
        "v1_metrics": {},
    }
    sdir = task6_mod.sources_dir(T6_THREAD)
    (sdir / f"v1_{task6_mod.VRAM_NAME}").write_text(
        json.dumps({task6_mod.VRAM_KEY: 20.0}), encoding="utf-8")

    # V2：真 batch（tight profile 下三臂×3 籽全存活）+ 两份随批机产扩展件。
    v2_wd = tmp_path / "v2batch"
    build_synthetic_batch(v2_wd, falls={})
    (v2_wd / task6_mod.VRAM_NAME).write_text(
        json.dumps({task6_mod.VRAM_KEY: 18.42}), encoding="utf-8")
    innovation = {
        "control_law": "u=-Kp(θ−θ*)−Kd·θ̇（Kp/Kd 臂分级，RL 策略端不动）",
        "dr_distribution": {"mu": "loguniform[0.04,0.45]",
                            "Kp": "loguniform[8000,9000]"},
    }
    (v2_wd / task6_mod.INNOVATION_NAME).write_text(
        json.dumps(innovation, ensure_ascii=False), encoding="utf-8")

    visual = FakeVisualGateway(_gait_raw())
    seam = task6_mod.make_evaluate_seam(visual, "T6-B2-visual", T6_THREAD)
    metrics = seam({
        "thread_id": T6_THREAD, "v1_archive": arc,
        "telemetry_uri": str(v2_wd / "summary.json"),
        "agent_runs": [{"run_idx": 1, "status": "ok"}], "events": []})

    # 视觉通道零触达（record_only + active-only）。
    assert visual.visual_calls == []
    assert metrics["rungs"]["rung4"]["zero_visual_calls"] is True

    v2_doc = json.loads(Path(asc_path := metrics["ascent"][
        "v2_summary_path"]).read_text(encoding="utf-8"))
    v1_doc = json.loads(Path(metrics["ascent"][
        "v1_summary_path"]).read_text(encoding="utf-8"))

    # 增强 V2 summary：telemetry.v1 schema、恰三臂×3 籽、run_id/parent 钉死。
    assert v2_doc["schema"] == "simpraxis.task8.track2.telemetry.v1"
    assert set(v2_doc["groups"]) == {"normal", "ice_nominal", "ice_dr"}
    assert len(list((v2_wd / "telemetry").glob("*.json"))) == 9
    for arm, g in v2_doc["groups"].items():
        assert g["n"] == 3 and g["n_fell"] == 0 and g["success_rate"] == 1.0
    assert v2_doc["run_id"] == T6_THREAD
    assert v2_doc["parent_run_id"] == V1_THREAD == v1_doc["run_id"]
    assert "parent_run_id" not in v1_doc and "innovation" not in v1_doc
    # Pareto 六指标恰六项、皆 finite；solve_wallclock 取 9 行 CSV 均值。
    assert set(v2_doc["pareto_metrics"]) == set(METRIC_DIRECTIONS)
    assert v2_doc["pareto_metrics"]["solve_wallclock_s"] == pytest.approx(12.3)
    assert v2_doc["pareto_metrics"]["vram_peak_gb"] == pytest.approx(18.42)
    # innovation 必须是随批机产件原文（接缝不代笔）。
    assert v2_doc["innovation"] == innovation

    # 四来源件过生产 chronicle（意见件用测试夹具意见，summary 是生产产物）。
    def _opinion(gate):
        return {"schema": chronicle.GATE_OPINION_SCHEMA, "gate": gate,
                "decision": "approve",
                "human_notes": f"测试夹具意见：{gate} 批准",
                "source": "cli", "ts_iso": "2026-10-19T01:02:03Z"}

    g1p = sdir / "fix_gate1_opinion.json"
    g2p = sdir / "fix_gate2_opinion.json"
    g1p.write_text(json.dumps(_opinion("gate1")), encoding="utf-8")
    g2p.write_text(json.dumps(_opinion("gate2")), encoding="utf-8")
    loaded_sources = chronicle.load_evolution_sources(
        v1_summary_path=metrics["ascent"]["v1_summary_path"],
        v2_summary_path=asc_path, gate1_opinion_path=g1p,
        gate2_opinion_path=g2p)
    assert loaded_sources["v2_summary"]["groups"]["ice_dr"]["n"] == 3

    # 三层分离铁律：引擎仓（git 跟踪面）永不携带 V2 解题脚本。
    ls = subprocess.run(["git", "-C", str(_REPO_ROOT), "ls-files"],
                        capture_output=True, text=True, check=True)
    tracked = ls.stdout.split()
    assert not [p for p in tracked
                if Path(p).name == task6_mod.V2_ENTRYPOINT]
    grep = subprocess.run(
        ["git", "-C", str(_REPO_ROOT), "grep", "-l", "-z", "--",
         "genesis_humanoid_dr_v2"],
        capture_output=True, text=True)
    mentioned = [p for p in grep.stdout.split("\0") if p]
    # 只允许引擎代码/测试按名引用（契约/缺件断言）；脚本本体已被上面的
    # ls-files 名字断言钉死（任何跟踪路径都不得叫这个文件名）。
    assert all(p.startswith(("orchestrator/", "tests/"))
               for p in mentioned), mentioned


# ---------------------------------------------------------------------------
# 6. ascent 索引：V2 链入条目（evolution_of）父链校验 + 同题豁免 + 分类跳过
# ---------------------------------------------------------------------------

def test_v2_linked_entries_parent_chain(tmp_path):
    """evolution_of/evolution_round 加法：父必须在、题面可复用、thread 仍唯一。"""
    index, _ = _build_fixture_index(tmp_path / "chain")
    parent = index["entries"][0]
    v2_entry = dict(parent)
    v2_entry.update({
        "thread_id": "t6-genesis-ascent-20261017a",
        "host_workdir": str(tmp_path / "chain" / "run_v2_1"),
        "evolution_of": V1_THREAD, "evolution_round": 1,
    })
    index["entries"].append(v2_entry)
    p = tmp_path / "chain" / "v1_index.json"
    p.write_text(json.dumps(index, ensure_ascii=False, indent=2),
                encoding="utf-8")
    loaded = load_v1_index(p)  # 同题复用不触发重复登记
    # 分类器只对未链入的冻结档案命中：V2 行被跳过，仍归并到父档案。
    result = classify_run(
        {"track": "track2", "question": parent["question"]}, loaded)
    assert result["run_mode"] == ASCENT
    assert result["v1_archive"]["thread_id"] == V1_THREAD

    # 悬空父链 → 损坏
    bad = json.loads(p.read_text(encoding="utf-8"))
    bad["entries"][1]["evolution_of"] = "no-such-parent"
    bp = tmp_path / "chain" / "bad_dangling.json"
    bp.write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(Exception, match="evolution_of"):
        load_v1_index(bp)

    # thread_id 重复（两条链档共用新 thread；父仍存在）仍损坏
    dup = json.loads(p.read_text(encoding="utf-8"))
    other_link = dict(dup["entries"][1])
    other_link["host_workdir"] = str(tmp_path / "chain" / "run_v2_1b")
    dup["entries"].append(other_link)
    dp = tmp_path / "chain" / "dup_thread.json"
    dp.write_text(json.dumps(dup), encoding="utf-8")
    with pytest.raises(Exception, match="thread_id"):
        load_v1_index(dp)

    # 成对缺失/轮次非正/自指 → 损坏
    for mutate, match in (
        (lambda e: e.pop("evolution_round"), "成对"),
        (lambda e: e.__setitem__("evolution_round", 0), "evolution_round"),
        (lambda e: e.__setitem__("evolution_of",
                                 "t6-genesis-ascent-20261017a"), "自身"),
    ):
        broken = json.loads(p.read_text(encoding="utf-8"))
        mutate(broken["entries"][1])
        qp = tmp_path / "chain" / "broken.json"
        qp.write_text(json.dumps(broken), encoding="utf-8")
        with pytest.raises(Exception, match=match):
            load_v1_index(qp)


# ---------------------------------------------------------------------------
# 7. M1（冷验必修）：V2 Pareto/groups/contrast 走默认口径，tight 只管验收
# ---------------------------------------------------------------------------

def _rewrite_force_gap_batch(wd: Path) -> None:
    """9 份 telemetry 的 200..239 帧改成 1.8N 接触力 + 0.005m/s 滑移块。

    力值落在 (1.6, 2.0]N：默认 2.0N 滑移积分门排除这 40 帧、tight 1.6N 门
    计入——复刻 lead 在冻结 V1 9 条 telemetry 上实测 5/9 滑移被改写
    （normal 0.1424→0.1427、ice_nominal 0.1834→0.1835）的口径分叉。
    outcomes/CSV 按生产引擎同口径（默认 profile）重算，故默认 evaluate 全
    干净、tight rung2 的 contact_frames 精确对质必失败（验收仍按加严）。
    """
    rows = []
    for arm in ("normal", "ice_nominal", "ice_dr"):
        for seed in range(3):
            run_id = f"{arm}_s{seed}"
            tp = wd / "telemetry" / f"{run_id}.json"
            data = json.loads(tp.read_text(encoding="utf-8"))
            series = data["series"]
            for i in range(200, 240):
                series["contact_force_R"][i] = 1.8
                series["contact_force_L"][i] = 1.8
                series["slip_v_R"][i] = 0.005
                series["slip_v_L"][i] = 0.005
            rec = recompute_run(data, profile=None)
            data["outcomes"] = _outcomes_from(rec)
            tp.write_text(json.dumps(data), encoding="utf-8")
            rows.append(_csv_row(run_id, arm, seed, data, rec))
    with open(wd / task8.CSV_NAME, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def test_m1_v2_pareto_default_caliber_tight_only_acceptance(
        tmp_path, monkeypatch):
    """计划 L415-423：Pareto 两侧同定义（ε=5%）；加严的只是验收严苛度。

    (a) V2 Pareto 向量与 v2_summary.groups/contrast == 同一 workdir 默认
        profile 重算值，且与 tight groups 值数值不同；
    (b) rungs/rules_pass/加严证据段仍按 tight 口径（含 contact_frames 分歧）；
    (c) V1 侧恒默认口径（tight 重算值不同也不得入 v1_summary）。
    """
    # record_only 标定（视觉零实呼；本测焦点是口径不是 Rung4）。
    cal_dir = tmp_path / "phase1" / "task8_track2_judge"
    cal_dir.mkdir(parents=True)
    (cal_dir / "calibration_gait.json").write_text(
        json.dumps({"mode": "record_only"}), encoding="utf-8")
    monkeypatch.setenv("SIMPRAXIS_PHASE1_ROOT", str(tmp_path / "phase1"))

    v1_wd = tmp_path / "v1batch"
    build_synthetic_batch(v1_wd, falls={})
    _rewrite_force_gap_batch(v1_wd)
    v2_wd = tmp_path / "v2batch"
    build_synthetic_batch(v2_wd, falls={})
    _rewrite_force_gap_batch(v2_wd)

    # 独立双口径基线：先证明夹具确实判别 1.6/2.0 两道力门。
    default_v2 = evaluate_batch(v2_wd, profile=None)
    tight_v2 = evaluate_batch(v2_wd, profile=tight_profile())
    assert not default_v2["schema_errors"]
    assert not tight_v2["schema_errors"]
    slip = "slip_dist_mean_m"
    assert default_v2["groups"]["normal"][slip] \
        != tight_v2["groups"]["normal"][slip]
    assert default_v2["rung2"]["pass"] is True
    assert tight_v2["rung2"]["pass"] is False
    disagreements = tight_v2["rung2"]["checks"]["disagreements"]
    assert any(any(k.startswith("contact_frames") for k in d)
               for d in disagreements)
    default_v1 = evaluate_batch(v1_wd, profile=None)
    tight_v1 = evaluate_batch(v1_wd, profile=tight_profile())
    assert task6_mod._arm_mean(default_v1["groups"], slip) \
        != task6_mod._arm_mean(tight_v1["groups"], slip)

    arc = {
        "schema": V1_INDEX_SCHEMA, "track": "track2",
        "thread_id": V1_THREAD, "host_workdir": str(v1_wd),
        "files": [{"relpath": rel, "sha256": _sha256_file(v1_wd / rel)}
                  for rel in SPLIT_RELS],
        "v1_metrics": {},
    }
    sdir = task6_mod.sources_dir(T6_THREAD)
    (sdir / f"v1_{task6_mod.VRAM_NAME}").write_text(
        json.dumps({task6_mod.VRAM_KEY: 20.0}), encoding="utf-8")
    (v2_wd / task6_mod.VRAM_NAME).write_text(
        json.dumps({task6_mod.VRAM_KEY: 18.42}), encoding="utf-8")
    (v2_wd / task6_mod.INNOVATION_NAME).write_text(json.dumps({
        "control_law": "u=-Kp(θ−θ*)−Kd·θ̇（臂分级，RL 策略端不动）",
        "dr_distribution": {"mu": "loguniform[0.04,0.45]"},
    }, ensure_ascii=False), encoding="utf-8")

    visual = FakeVisualGateway(_gait_raw())
    metrics = task6_mod.make_evaluate_seam(
        visual, "T6-M1-visual", T6_THREAD)({
            "thread_id": T6_THREAD, "v1_archive": arc,
            "telemetry_uri": str(v2_wd / "summary.json"),
            "agent_runs": [{"run_idx": 1, "status": "ok"}], "events": []})
    assert visual.visual_calls == []

    asc = metrics["ascent"]
    v2_doc = json.loads(Path(asc["v2_summary_path"]).read_text(encoding="utf-8"))
    v1_doc = json.loads(Path(asc["v1_summary_path"]).read_text(encoding="utf-8"))

    # (a) V2 Pareto/groups/contrast 与 V1 同口径（默认 2.0N 门）。
    assert v2_doc["groups"] == default_v2["groups"]
    assert v2_doc["contrast"] == default_v2["contrast"]
    assert metrics["groups"] == tight_v2["groups"]
    assert metrics["contrast"] == tight_v2["contrast"]
    expect_v2_vector = task6_mod._pareto_vector(
        default_v2["groups"], 12.3, 18.42)
    for m_name, val in expect_v2_vector.items():
        assert v2_doc["pareto_metrics"][m_name] == pytest.approx(val)
    tight_slip = task6_mod._arm_mean(tight_v2["groups"], slip)
    assert v2_doc["pareto_metrics"][slip] != tight_slip
    assert asc["v2_pareto_metrics"][slip] == \
        pytest.approx(v2_doc["pareto_metrics"][slip])

    # (b) 升阶验收仍 tight：rules_pass/三级判定与独立 tight 复算等值。
    assert metrics["rules_pass"] is False
    assert metrics["rules_pass"] == tight_v2["rules_pass"]
    for rung in ("rung1", "rung2", "rung3"):
        assert metrics["rungs"][rung]["pass"] == tight_v2[rung]["pass"]
    assert asc["tight_acceptance"] == {
        "profile": "ascent_tight",
        "rules_pass": False,
        "rungs_pass": {r: bool(tight_v2[r]["pass"])
                       for r in ("rung1", "rung2", "rung3")},
    }
    assert metrics["auto_release"] is False
    assert metrics["escalate_human"] is True

    # (c) V1 恒默认口径；tight 数字不得溯及冻结档案。
    assert v1_doc["groups"] == default_v1["groups"]
    assert v1_doc["contrast"] == default_v1["contrast"]
    v1_slip = task6_mod._pareto_vector(
        default_v1["groups"], 12.3, 20.0)[slip]
    assert v1_doc["pareto_metrics"][slip] == pytest.approx(v1_slip)
    assert v1_doc["pareto_metrics"][slip] != \
        task6_mod._arm_mean(tight_v1["groups"], slip)

    # 两侧同定义：同内容 batch 的滑移 Δ 行 v1==v2（旧实现此处是 tight vs
    # 默认的伪 Δ）。
    slip_row = next(r for r in asc["metrics_diff"] if r["metric"] == slip)
    assert slip_row["v1"] == pytest.approx(slip_row["v2"])


# ---------------------------------------------------------------------------
# 8. N1（冷验必修）：V1 split 核验路径以登记 relpath 为准（含越界拒绝）
# ---------------------------------------------------------------------------

def test_n1_resolve_v1_splits_registered_relpath_with_root_decoy(
        tmp_path, monkeypatch):
    """登记 relpath=splits/x.mp4 + 根目录同名同字节替身：必须只验登记件。"""
    monkeypatch.setattr(task6_mod, "LOCKED_V1_SPLIT_SIZES", {})
    wd = tmp_path / "v1frozen"
    build_synthetic_batch(wd, falls={})
    (wd / "splits").mkdir()
    rels: dict[str, str] = {}
    for _, name in SPLITS:
        (wd / name).replace(wd / "splits" / name)
        # 根目录同名同字节替身（lead 变异：旧实现 wd/name 会误验替身）。
        (wd / name).write_bytes((wd / "splits" / name).read_bytes())
        rels[name] = f"splits/{name}"

    # 生产档案的登记 sha 在核验期间固定不变（禁止按磁盘现状重算）。
    arc = {"host_workdir": str(wd), "files": [
        {"relpath": rels[n], "sha256": _sha256_file(wd / rels[n])}
        for _, n in SPLITS]}

    paths = task6_mod.resolve_v1_splits(arc)
    assert paths == [wd / rels[n] for _, n in SPLITS]

    # 根替身全部调包成垃圾字节：仍通过（证明核验读的是登记 relpath；
    # 旧实现此刻必在根文件 sha 对质上失败）。
    for _, name in SPLITS:
        (wd / name).write_bytes(b"root-decoy-garbage")
    task6_mod.resolve_v1_splits(arc)

    # 登记件（子目录）被改：固定登记 sha 对质必须 fail-loud。
    p0 = wd / rels[SPLITS[0][1]]
    p0.write_bytes(p0.read_bytes() + b"x")
    with pytest.raises(GraphStateError, match="sha256"):
        task6_mod.resolve_v1_splits(arc)


def test_n1_resolve_v1_splits_rejects_illegal_relpaths(tmp_path, monkeypatch):
    """绝对路径/.. 分段/symlink 逃逸/多义登记/缺登记一律 fail-closed。"""
    monkeypatch.setattr(task6_mod, "LOCKED_V1_SPLIT_SIZES", {})
    wd = tmp_path / "v1frozen"
    build_synthetic_batch(wd, falls={})
    name0, name1 = SPLITS[0][1], SPLITS[1][1]
    good1 = {"relpath": name1, "sha256": _sha256_file(wd / name1)}

    def _resolve(rec0):
        return task6_mod.resolve_v1_splits(
            {"host_workdir": str(wd), "files": [rec0, good1]})

    # 绝对/.. 记录的 basename 与登记 split 同名：先过格式闸 fail-closed，
    # 轮不到文件/sha 核验（防止绝对路径 join 逃逸 host_workdir）。
    with pytest.raises(GraphStateError, match="非法"):
        _resolve({"relpath": f"/tmp/{name0}", "sha256": "x"})
    with pytest.raises(GraphStateError, match="非法"):
        _resolve({"relpath": f"../{name0}", "sha256": "x"})
    with pytest.raises(GraphStateError, match="多义"):
        task6_mod.resolve_v1_splits({"host_workdir": str(wd), "files": [
            {"relpath": f"a/{name0}", "sha256": "x"},
            {"relpath": f"b/{name0}", "sha256": "y"}, good1]})
    with pytest.raises(GraphStateError, match="未登记"):
        task6_mod.resolve_v1_splits(
            {"host_workdir": str(wd), "files": [good1]})

    # symlink 逃逸：目标在 host_workdir 外且 sha 登记正确，仍必须拒绝。
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"\x00\x00\x00esc")
    (wd / name0).unlink()
    (wd / name0).symlink_to(outside)
    with pytest.raises(GraphStateError, match="越界"):
        _resolve({"relpath": name0, "sha256": _sha256_file(outside)})


# ---------------------------------------------------------------------------
# 锁内适配（计划 v1.2 外，seq42-47 拆块出生，用户 2026-10-20 裁决；
# 2026-10-21 run4 起 sentinel 围栏纯文本定界，n=4→6 工厂规格）：
# 工厂在 V2 出生轮挂 6 块 ChunkSpec（合同/提案/骨架三注入、32768/600），
# 出生后修补轮 chunked_birth=None。合成 chosen + 合成短 V1 夹具，严禁真实
# 43KB 方案脚本（硬规则 3）。
# ---------------------------------------------------------------------------

def test_factory_chunked_birth_spec(tmp_path, monkeypatch):
    from orchestrator.agent_runner import runner as runner_mod
    from orchestrator.graph.task6_v2_prompts import V2_CHUNK_TITLES

    monkeypatch.setenv("SIMPRAXIS_SOLUTIONS_ROOT",
                       str(FIXTURE_SOLUTIONS_ROOT))
    cal_src = tmp_path / "cal_src.json"
    cal_src.write_text(json.dumps(_calibration_v2()), encoding="utf-8")
    monkeypatch.setattr(task8, "CALIBRATION_SOURCE", cal_src)

    chosen = {
        "sim_engine": "genesis",
        "name": "V2 ζ 条件化阻抗 + Hunt-Crossley 退化升阶",
        "control_law": (
            "ζ~logU[0.3,0.9] 先采；Kd=2ζ√(Kp·m_eff) clip[15,2000]；"
            "Stribeck μs/μk/vs episode 等效"),
        "physics_degradations": [
            "Hunt-Crossley Fn=Kδ^1.5(1+c δ̇) 退化为线性 sol Kp/ζ 等效",
            "关节 armature/damping 无 dof API 时如实记录未施加项"],
    }
    state = {
        "chosen": chosen,
        "v1_archive": {"thread_id": V1_THREAD,
                       "host_workdir": str(tmp_path / "v1arch")},
        "agent_runs": [], "events": [], "gate1_rejects": 0,
        "proposals": []}
    factory = task6_mod.make_spec_factory(T6_THREAD, "T6-B1-chunk")
    spec = factory(state)

    # RunSpec 级 V2 调用口径：32768 输出余量 / 600s 超时；无 RunSpec 级额外段
    # （合同/提案/骨架只挂在 ChunkSpec 上，单补丁路径永远看不到它们）。
    assert spec.coding_max_tokens == task6_mod.CODING_MAX_TOKENS_V2 == 32768
    assert spec.coding_timeout_s == task6_mod.CODING_TIMEOUT_S_V2 == 600
    assert spec.extra_prompt_sections == ()

    cs = spec.chunked_birth
    assert isinstance(cs, runner_mod.ChunkSpec)
    assert cs.target_path == task6_mod.V2_ENTRYPOINT
    assert cs.chunk_count == 6                      # n 固定，禁止模型自报（run4 n=6）
    assert cs.chunk_titles is V2_CHUNK_TITLES or cs.chunk_titles == V2_CHUNK_TITLES
    assert len(cs.chunk_titles) == 6
    assert len(cs.chunk_boundaries) == 6
    assert all("行数预算" in b for b in cs.chunk_boundaries)

    titles = [t for t, _ in cs.per_call_sections]
    assert titles == ["V2 升阶任务合同", "已批准 macro 提案", "V1 结构骨架"]
    contract, proposal, skeleton = [b for _, b in cs.per_call_sections]
    assert task6_mod.V2_ENTRYPOINT in contract
    # 第 7 条「产物路径纪律」（2026-10-21 用户裁决①，run1 临时 chdir+相对
    # 录制路径人因结案后重挂 run2）：通用纪律三要素必须在合同文本中。
    assert "7. **产物路径纪律**" in contract
    assert "绝对路径" in contract and "惰性" in contract
    assert "恢复原 cwd" in contract
    # 骨架两处短注同步指向合同 7：chdir 仅为解析 MJCF、录制文件名绝对路径。
    assert "见合同 7" in skeleton
    assert "--video 给录制器的必须是绝对路径" in skeleton
    # 防回潮：1-6 条编号锚点未乱；七个编号标题按序且恰好七个。
    assert "1. **围栏纯文本分块交付" in contract
    assert "6. **禁硬编码结果" in contract
    assert re.findall(r"(?m)^([1-9])\. \*\*", contract) == [
        str(i) for i in range(1, 8)]
    # 条间风格齐一：每条（2-7）紧跟前条句号，无空行。
    assert "\n\n7. **" not in contract
    # 提案 JSON 必须是 chosen 逐字序列化：ζ/Hunt-Crossley/clip 关键串不得丢。
    assert json.loads(proposal) == chosen
    assert "ζ" in proposal and "Hunt-Crossley" in proposal
    assert "clip[15,2000]" in proposal
    # 骨架 @@SHA@@ 必须被 staged V1（合成夹具短脚本）sha256 前 16 位替换。
    staged_v1 = spec.workdir / "genesis_humanoid_dr.py"
    assert staged_v1.is_file()
    v1_sha16 = hashlib.sha256(staged_v1.read_bytes()).hexdigest()[:16]
    assert "@@SHA@@" not in skeleton
    assert v1_sha16 in skeleton

    # 非出生轮（同 run workdir 内 V2 已出生）：chunked_birth=None；32768/600
    # 的 RunSpec 级口径保留（出生后的单补丁修补轮同样需要思考余量）。
    runner_mod._apply_structured_patch(spec.workdir, {
        "path": task6_mod.V2_ENTRYPOINT, "old": "",
        "new": "#!/usr/bin/env python\n# synthetic born V2 (fixture)\n"})
    spec_repair = factory(dict(state))
    assert spec_repair.workdir == spec.workdir     # run_idx 仍为 1 → 同目录
    assert spec_repair.chunked_birth is None
    assert spec_repair.coding_max_tokens == task6_mod.CODING_MAX_TOKENS_V2
    assert spec_repair.coding_timeout_s == 600

    # Gate2 打回后的**新 run 目录**：V2 重新 absent → 重新挂 ChunkSpec。
    spec_run2 = factory({**state, "agent_runs": [{"run_idx": 1}]})
    assert spec_run2.workdir != spec.workdir
    assert isinstance(spec_run2.chunked_birth, runner_mod.ChunkSpec)
    assert spec_run2.chunked_birth.chunk_count == 6

    # chosen 缺失：engine 映射先 fail-closed（GraphStateError），结构上不可能
    # 以 {} 静默装配 ChunkSpec（工厂内另以 state["chosen"] 裸取双保险）。
    with pytest.raises(GraphStateError, match="sim_engine"):
        factory({k: v for k, v in state.items() if k != "chosen"})
