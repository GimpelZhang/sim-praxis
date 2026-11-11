"""RSI-R1 T8 驱动面（AC32 的零付费前置）：独立复跑两 study 的确认批。

链路全部走生产面：真 `build_specs`/`execute_batch`/`ResourceBook`/`compare_study`/
`closure`；只有两处测试替身——容器 backend（``SyntheticBackend``，零容器）
与模型（T7 阶段用的 ``ToyModel``，零 POST）。本文件要证明的事实：

* 复跑**读盘**重建（confirm 计划/manifest/原件观测全部来自落盘件，不靠内存）；
* 独立性 fail-closed（抄原件/同执行/同容器/同时间/同根 一律拒绝）；
* 比对件**只封一次**；BLOCKED 不封比对件、允许修复后重试（attempt 递增）；
* 两 study 比对件齐备才把 root 从 STUDY_B 推进到 REPLAY。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.rsi_r1 import replay as RP
from orchestrator.rsi_r1 import replay_driver as RPD
from orchestrator.rsi_r1 import state as S
from orchestrator.rsi_r1 import study as STD
from tests.rsi_r1_rehearsal import (
    REHEARSAL_IMAGE,
    RehearsalDriver,
    SyntheticBackend,
)
from tests.test_rsi_r1_protocol import ToyModel


def _counter(text: str) -> int:
    return (len(text.encode("utf-8")) + 3) // 4


def _driver(tmp_path: Path, *, to: str = S.ROOT_PRACTICED) -> RehearsalDriver:
    driver = RehearsalDriver(tmp_path / "campaign")
    driver.setup()
    driver.freeze()
    if to != S.ROOT_FROZEN:
        driver.advance(S.ROOT_PROBED, reason="零付费 replay 测试")
        driver.advance(S.ROOT_PRACTICED, reason="零付费 replay 测试")
    return driver


def _study(driver: RehearsalDriver, study: str, *, batch: str) -> "dict[str, object]":
    report = STD.run_study(
        driver.ctx, study=study, spend=driver.spend,
        transport_factory=lambda: ToyModel(scope=driver.ctx.root,
                                           label=f"{study}-{batch}"),
        runner=driver.runner, image=REHEARSAL_IMAGE, batch_id=batch,
        counter=_counter,
        backend_factory=lambda batch_id: SyntheticBackend(
            observations_root=driver.ctx.subdir("observations"),
            evidence_root=driver.ctx.subdir("evidence_pack"),
            batch_id=batch_id, tag=f"{study}-{batch}", clock_hour=2))
    assert report["status"] == "COMPLETED", report["stopped"]
    return report


def _replay(driver: RehearsalDriver, study: str, *, batch: "str | None" = None,
            factory=None, max_steps: "int | None" = None) -> "dict[str, object]":
    return RPD.run_replay(
        driver.ctx, study=study, runner=driver.runner, image=REHEARSAL_IMAGE,
        batch_id=batch or f"replay-{study}", max_steps=max_steps,
        backend_factory=factory or (lambda name: SyntheticBackend(
            observations_root=driver.ctx.subdir("observations"),
            evidence_root=driver.ctx.subdir("replay") / f"independent_{study}",
            batch_id=name, tag=f"replay-{study}-{name}", clock_hour=3)))


def test_replay_a_completes_and_seals_comparison(tmp_path: Path) -> None:
    driver = _driver(tmp_path)
    _study(driver, "A", batch="t7a")
    report = _replay(driver, "A")
    assert report["status"] == RPD.STATUS_COMPLETE, report["problems"]
    assert report["comparison_ok"] is True
    assert report["problems"] == []
    assert report["n_runs_used"] == report["n_expected"] > 0
    assert report["summary"]["failed"] == 0
    assert report["summary"]["identical"] == report["n_expected"]
    assert report["attempt"] == 1
    assert report["zero_paid_posts"] == 0
    assert driver.ledger_calls() == 0, "复跑零 POST，账本不得出现实呼行"
    # 比对件落盘且 ok
    doc = driver.ctx.read_json(RPD.comparison_relpath("A"))
    assert doc["ok"] is True and doc["study_id"] == "A"
    # 独立性证据逐 run 在场（新执行/新容器/新时间/新根）
    for run in doc["runs"]:
        ind = run["independence"]
        assert ind["record_identical"] is True
        assert ind["execution_id"] != ""
    # root 未推进：study B 还没复跑
    assert report["root_after"] == S.ROOT_STUDY_A
    assert S.ResearchState(driver.ctx).root_state() == S.ROOT_STUDY_A
    # 复跑报告落盘并如实标明零 POST
    rep = driver.ctx.read_json(RPD.report_relpath("A"))
    assert rep["status"] == RPD.STATUS_COMPLETE
    assert rep["zero_paid_posts"] == 0


def test_replay_requires_finished_study_and_root(tmp_path: Path) -> None:
    driver = _driver(tmp_path)  # root=PRACTICED，study A 未跑
    with pytest.raises(RPD.ReplayDriverError) as exc:
        _replay(driver, "A")
    assert exc.value.code == "ROOT_GATE"
    assert not driver.ctx.exists(RPD.comparison_relpath("A"))


def test_replay_refuses_reused_batch_dir(tmp_path: Path) -> None:
    """复跑不得复用**确认批**的观测目录（否则复跑会污染原件证据）。"""
    from orchestrator.rsi_r1 import assembly as A

    driver = _driver(tmp_path)
    _study(driver, "A", batch="t7a")
    token = S.ResearchState(driver.ctx).confirm_lock("A").token
    with pytest.raises(RPD.ReplayDriverError) as exc:
        _replay(driver, "A", batch=A.confirm_batch_id("A", token))
    assert exc.value.code == "BATCH_EXISTS"


def test_replay_rejects_copied_execution_identity(tmp_path: Path) -> None:
    """抄原件（同执行 id）必须 fail-closed——这是「复跑」不是「复制」。"""
    driver = _driver(tmp_path)
    _study(driver, "A", batch="t7a")

    def forged(name: str) -> SyntheticBackend:
        # 新 batch 名、新证据根，但 execution/container/时间与原件全同
        return SyntheticBackend(
            observations_root=driver.ctx.subdir("observations"),
            evidence_root=driver.ctx.subdir("replay") / "independent_A",
            batch_id=name, tag="A-t7a", clock_hour=2)

    with pytest.raises(RP.ReplayError):
        _replay(driver, "A", factory=forged)
    assert not driver.ctx.exists(RPD.comparison_relpath("A"))


def test_replay_is_one_shot(tmp_path: Path) -> None:
    driver = _driver(tmp_path)
    _study(driver, "A", batch="t7a")
    _replay(driver, "A")
    with pytest.raises(RPD.ReplayDriverError) as exc:
        _replay(driver, "A", batch="replay-A-2")
    assert exc.value.code == "REPLAY_ALREADY_RAN"


def test_replay_blocked_keeps_root_and_allows_retry(tmp_path: Path) -> None:
    driver = _driver(tmp_path)
    _study(driver, "A", batch="t7a")
    from orchestrator.rsi_r1 import experiments as E

    calls = {"n": 0}

    def broken(name: str):
        class _Broken(SyntheticBackend):
            def run(self, spec):  # noqa: D401 - 一次性故障注入
                calls["n"] += 1
                raise E.BackendInfraError("SANDBOX_EXIT 1: boom")
        return _Broken(observations_root=driver.ctx.subdir("observations"),
                       evidence_root=driver.ctx.subdir("replay")
                       / "independent_A", batch_id=name, tag="broken",
                       clock_hour=4)

    blocked = _replay(driver, "A", factory=broken)
    assert blocked["status"] == RPD.STATUS_BLOCKED
    assert blocked["blocked"] is True
    assert blocked["comparison_relpath"] is None
    assert not driver.ctx.exists(RPD.comparison_relpath("A"))
    assert S.ResearchState(driver.ctx).root_state() == S.ROOT_STUDY_A
    assert driver.ctx.read_json(RPD.report_relpath("A"))["status"] \
        == RPD.STATUS_BLOCKED

    # 修复后同一 root 重试：attempt 递增，且这次真的封比对件
    again = _replay(driver, "A")
    assert again["status"] == RPD.STATUS_COMPLETE
    assert again["attempt"] == 2
    assert driver.ctx.exists(RPD.comparison_relpath("A"))


def test_both_studies_sealed_advance_root_to_replay(tmp_path: Path) -> None:
    driver = _driver(tmp_path)
    _study(driver, "A", batch="t7a")
    _study(driver, "B", batch="t7b")
    assert S.ResearchState(driver.ctx).root_state() == S.ROOT_STUDY_B
    first = _replay(driver, "A")
    assert first["root_after"] == S.ROOT_STUDY_B, "仅 A 封存不得推进"
    second = _replay(driver, "B")
    assert second["status"] == RPD.STATUS_COMPLETE
    assert second["root_after"] == S.ROOT_REPLAY
    assert S.ResearchState(driver.ctx).root_state() == S.ROOT_REPLAY


def test_missing_original_is_a_driver_error_not_a_crash(tmp_path: Path) -> None:
    """原件观测缺件必须变成可机读的 ``ReplayDriverError``（不是裸 traceback）。

    2026-11-11 审计：读回路径会抛 ``ObservationError``/``LoopError``，驱动层若
    只按 ``LoopError`` 捕获（且未导入模块）就是运维故障变 NameError/AttributeError，
    T8 无法给出 BLOCKED 报告。
    """
    driver = _driver(tmp_path)
    _study(driver, "A", batch="drvA")
    base = sorted(driver.ctx.subdir("observations").glob("confirm-A-*"))[-1]
    victim = sorted(base.glob("*.jsonl"))[0]
    victim.unlink()
    with pytest.raises(RPD.ReplayDriverError) as exc:
        _replay(driver, "A")
    assert exc.value.code == "MANIFEST_MISSING"
    assert str(victim) in exc.value.detail
