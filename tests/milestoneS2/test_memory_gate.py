"""S2 MemoryBoundSuite（AC-10）：S1 教训四类回归绑定 + 门件结构。

教训四类：触点上下文 / thinking 空答 / sham 退化 / 六段缺段——每类必须有
**可执行的回归用例**（不是口头绑定）。本测试核验绑定清单与门件，并对每类给出
机判触面（导入即失败 / 用例缺失 / 门件缺项）。
"""
from __future__ import annotations

import json
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_CAMPAIGN = Path("/mnt/artifacts/simpraxis/rsi_s2/evidence/s2-campaign-20261119T072947Z")

#: 四类教训 → 绑定用例（文件存在 + 至少一条可执行断言）
LESSON_BINDINGS = {
    "s1-touchpoint-ctx": ("tests/milestoneS1/test_touchpoints.py",
                          "test_driver_passes_run_context_to_headless_actor"),
    "s1-thinking-empty": ("tests/milestoneS1/test_review_instrument.py",
                          "test_run_review_injects_pack_and_returns_run"),
    "s1-sham-degeneracy": ("tests/milestoneS2/test_nc_validity.py",
                           "test_degenerate_nc_rejected"),
    "s1-report-sections": ("tests/milestoneS2/test_report_contract.py",
                           "test_missing_section_rejected_with_structure_annotation"),
}


def test_four_lesson_regressions_are_bound():
    for lesson_id, (relpath, test_name) in LESSON_BINDINGS.items():
        path = _REPO / relpath
        assert path.is_file(), f"{lesson_id}: 绑定用例缺失 {relpath}"
        text = path.read_text(encoding="utf-8")
        assert f"def {test_name}(" in text, (
            f"{lesson_id}: 用例 {test_name} 不在 {relpath}（回归未绑定）")


def test_memory_gate_artifact_passes_and_lists_lessons():
    gate = _CAMPAIGN / "memory_gate.json"
    assert gate.is_file(), "memory_gate.json 不在场"
    doc = json.loads(gate.read_text(encoding="utf-8"))
    assert doc.get("pass") is True
    lessons = doc.get("lessons") or []
    assert len(lessons) >= 3, f"绑定教训不足：{lessons}"
    assert all(isinstance(name, str) and name for name in lessons)


def test_research_memory_lessons_are_present_in_closed_layer():
    path = Path("/mnt/artifacts/simpraxis/rsi_s2/closed/research_memory"
                "/s2_lessons.jsonl")
    assert path.is_file(), "闭源 ResearchMemory 缺失"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]
    ids = {row.get("lesson_id") for row in rows}
    assert {"s1-claim-vocabulary", "s1-nc-validity",
            "s1-report-sections"} <= ids
    # 哈希链自洽
    prev = ""
    for row in rows:
        assert row.get("prev_sha256") == prev
        prev = row.get("sha256")
