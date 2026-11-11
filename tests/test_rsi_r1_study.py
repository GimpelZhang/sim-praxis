"""RSI-R1 T7 驱动面（AC29–31 的零付费前置）：正式研究 study A/B 的循环。

链路全部走生产面：真 ``ResearchLoop``/``CampaignAdmission``/``SlotBook``/状态机/
dispatcher；只有两处测试替身——transport（脚本化候选，零 POST）与 backend
（``SyntheticBackend``，零容器）。与 protocol 演练的区别正是本 T7 的要点：
**允许合同内自修**（额度照计）、同一 study 只跑一次、终态与 claim/判据齐备才
推进 root。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator.rsi_r1 import loop as L
from orchestrator.rsi_r1 import state as S
from orchestrator.rsi_r1 import study as STD
from tests.rsi_r1_rehearsal import (
    REHEARSAL_IMAGE,
    RehearsalDriver,
    SyntheticBackend,
)
from tests.test_rsi_r1_protocol import BAD_RUN_BATCH, ToyModel

CHAIN = ("write_artifact", "propose_plan", "run_batch", "write_artifact",
         "analyze", "freeze_confirm", "run_confirm", "submit_claim")


def _counter(text: str) -> int:
    return (len(text.encode("utf-8")) + 3) // 4


def _driver(tmp_path: Path, *, to: str = S.ROOT_PRACTICED) -> RehearsalDriver:
    driver = RehearsalDriver(tmp_path / "campaign")
    driver.setup()
    driver.freeze()
    if to != S.ROOT_FROZEN:
        driver.advance(S.ROOT_PROBED, reason="零付费 study 测试")
        driver.advance(S.ROOT_PRACTICED, reason="零付费 study 测试")
    return driver


def _run(driver: RehearsalDriver, study: str, model, *,
         batch: str = "t7") -> "dict[str, object]":
    return STD.run_study(
        driver.ctx, study=study, spend=driver.spend,
        transport_factory=lambda: model, runner=driver.runner,
        image=REHEARSAL_IMAGE, batch_id=batch, counter=_counter,
        backend_factory=lambda batch_id: SyntheticBackend(
            observations_root=driver.ctx.subdir("observations"),
            evidence_root=driver.ctx.subdir("evidence_pack"),
            batch_id=batch_id, tag=f"{study}-{batch_id}", clock_hour=2))


class _AlwaysBad:
    """恒发合同内非法动作：把 lane 的 48 呼耗到帽尽（零容器、零 POST）。

    用 ``propose_plan`` 的空文档（合同缺字段 ⇒ 非终止类拒绝），而不是
    ``run_batch``——后者在没有探索计划时会撞终止类 ``PLAN_MISSING``，
    测不到「帽尽」这条路径。
    """

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, request) -> L.Reply:
        self.calls += 1
        return L.Reply(text=json.dumps({"action": "propose_plan", "plan": {}}),
                       response_id=f"resp-bad-{self.calls:03d}",
                       usage={"input_tokens": 1, "output_tokens": 1})


def test_study_a_completes_and_advances_root(tmp_path: Path) -> None:
    driver = _driver(tmp_path)
    report = _run(driver, "A", ToyModel(scope=driver.ctx.root, label="a"))
    assert report["status"] == "COMPLETED", report["stopped"]
    assert report["study_state"] == S.ST_VERIFIED
    assert report["stopped"] == "" and report["terminal"] is False
    assert report["ok_count"] == 8 == report["posts"]["this_attempt"]
    assert [a["action"] for a in report["actions"]] == list(CHAIN)
    assert all(a["ok"] for a in report["actions"])
    assert report["claim_submitted"] is True
    assert report["judge"]["verdict"] == "JUDGE_OK"
    assert report["judge"]["claim_status"] == "MATCH"
    assert report["root_before"] == S.ROOT_PRACTICED
    assert report["root_after"] == S.ROOT_STUDY_A
    assert S.ResearchState(driver.ctx).root_state() == S.ROOT_STUDY_A
    # 报告落盘且与内存返回逐字节一致（取证面不二次解释）
    rel = driver.ctx.relative(STD.report_relpath("A"))
    assert json.loads(rel.read_text(encoding="utf-8"))["status"] == "COMPLETED"
    # 零付费：账本没有任何实呼行
    assert driver.ledger_calls() == 0


def test_study_b_requires_study_a_root(tmp_path: Path) -> None:
    driver = _driver(tmp_path)  # root=PRACTICED，A 未跑
    model = ToyModel(scope=driver.ctx.root, label="b")
    with pytest.raises(STD.StudyError) as exc:
        _run(driver, "B", model)
    assert exc.value.code == "ROOT_GATE"
    assert model.calls == 0, "门禁拒绝不得触达 transport"
    assert not driver.ctx.exists(STD.report_relpath("B"))


def test_study_self_repairs_within_budget(tmp_path: Path) -> None:
    """合同内失败重发合法：多花一呼，仍可完成（与 probe 首错即止相反）。"""
    driver = _driver(tmp_path)
    model = ToyModel(scope=driver.ctx.root, label="afix",
                     repair_at=2, bad_doc=BAD_RUN_BATCH)
    report = _run(driver, "A", model)
    assert report["status"] == "COMPLETED", report["stopped"]
    assert report["posts"]["this_attempt"] == 9
    assert report["ok_count"] == 8
    assert any(a.get("code") == "PLAN_MISMATCH" for a in report["actions"])
    assert report["study_state"] == S.ST_VERIFIED


def test_study_cannot_run_twice(tmp_path: Path) -> None:
    driver = _driver(tmp_path)
    _run(driver, "A", ToyModel(scope=driver.ctx.root, label="a1"))
    model = ToyModel(scope=driver.ctx.root, label="a2")
    with pytest.raises(STD.StudyError) as exc:
        _run(driver, "A", model, batch="t7b")
    assert exc.value.code == "STUDY_ALREADY_RAN"
    assert model.calls == 0


def test_study_posts_exhausted_is_incomplete_and_keeps_root(
        tmp_path: Path) -> None:
    driver = _driver(tmp_path)
    bad = _AlwaysBad()
    report = _run(driver, "A", bad)
    assert report["status"] == "INCOMPLETE"
    assert report["stopped"] == "POSTS_EXHAUSTED"
    assert report["posts"]["this_attempt"] == STD.STUDY_POSTS["A"] == 48
    assert bad.calls == 48
    assert report["root_after"] == S.ROOT_PRACTICED
    assert S.ResearchState(driver.ctx).root_state() == S.ROOT_PRACTICED
    assert report["claim_submitted"] is False
    assert driver.ledger_calls() == 0


def test_study_report_is_one_shot_even_when_incomplete(tmp_path: Path) -> None:
    """INCOMPLETE 也留档：失败尝试不得被静默重跑覆盖（另开新 root 才重来）。"""
    driver = _driver(tmp_path)
    _run(driver, "A", _AlwaysBad())
    with pytest.raises(STD.StudyError) as exc:
        _run(driver, "A", ToyModel(scope=driver.ctx.root, label="a2"), batch="x")
    assert exc.value.code == "STUDY_ALREADY_RAN"
