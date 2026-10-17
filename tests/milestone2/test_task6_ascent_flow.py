"""Phase 2 Task6 — ascent 流程前置守卫（随 Task2/3 同批，0 付费，全 hermetic）。

计划 L569 + 附录 A L949-953 的 test_task6_ascent_flow.py 五测拆分（锁内适配）：
task6.py 工厂与 V2 track 脚本是 Task6 出生件，出生前可锁定的 3 测随本批落地，
另 2 测随 Task6 提交，不写空壳、不 skip、不打桩：

  本批（3 测）：
  1. test_task6_thread_uses_ascent_classifier
     图节点口径：initial_state(thread_id=t6-genesis-ascent...) 经
     node_classify(state, GraphDeps(v1_index=夹具dict)) 判 ascent；钉死跨线程
     归并（Task6 新线程复用 V1 t8 档案，分类只按 track+题面 sha）。
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

import hashlib
import json
from pathlib import Path

from orchestrator.feishu_gateway.client import (
    select_gate1_card_view,
    select_gate2_requestor,
)
from orchestrator.graph.ascent import (
    ASCENT,
    STANDARD,
    build_ascent_messages,
    load_v1_index,
)
from orchestrator.graph.nodes import GraphDeps, initial_state, node_classify
from orchestrator.graph.task8 import SPLITS
from orchestrator.judge.gait_rungs import contrast_block
from tests.milestone2.test_task2_ascent import _build_fixture_index  # 同款构造

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
    assert arc["host_workdir"] == str(workdir)
    assert [f["relpath"] for f in arc["files"]][:3] == [
        "summary.json", SPLIT_RELS[0], SPLIT_RELS[1]]

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
    msgs = build_ascent_messages(
        [{"role": "system", "content": "SYS"},
         {"role": "user", "content": "USER-BODY"}],
        v1_archive=classified["v1_archive"],
        ascent_targets=classified["ascent_targets"])
    text = msgs[1]["content"]
    # 字面量取自 render_v1_metrics 真实渲染（全角逗号）：两冰臂各一次
    assert text.count("success_rate=0.0，n_fell=3") == 2
    # normal 臂：3 跑倒 2（对照不稳健的事实）在场
    assert "n_fell=2" in text
    # 矛盾状态串不被插入段改名/映射（段内不消费该串，原文结论只能原样保留）
    head = text[: -len("USER-BODY")]
    assert "contradiction_control_not_robust" not in head


# ---------------------------------------------------------------------------
# 3. Gate2 双媒体 Diff 接线（可前置半：选择器 + 双 split 路径解析对质）
# ---------------------------------------------------------------------------

def test_gate2_diff_card_wired_task6(tmp_path):
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
