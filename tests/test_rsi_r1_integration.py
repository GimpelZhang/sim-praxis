"""RSI-R1 T5：全路径零付费集成（AC26）+ LOCK 先存在（AC27 的装配侧）。

这些测试**不打桩生产面**：准入、预算槽、LOCK 复核、动作绑定、状态机、
判据、复跑比对、结案交付件与独立复算器全部走生产代码；被替换的只有三处
外部资源——容器（``LocalRunner`` 在本机跑同一脚本）、物理引擎（合成记录
过 ``observations.validate_record``）、模型供应商（``ScriptedModel``）。

三条纪律的机器证据：

* **零付费**：ledger 全程 0 条实呼行（``ledger_summary`` 与逐行复核双证）；
* **not-a-skip**：本文件与 RSI-R1 其余测试文件里没有 pytest 跳过标记、
  没有占位符注释、也没有未实现的分支；
* **原接口回归**：T0–T1b 的 CLI 子命令与 SEAM_FILES 面没有被 T5 破坏。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from orchestrator.rsi_r1 import assembly as A
from orchestrator.rsi_r1 import cli as CLI
from orchestrator.rsi_r1 import closure as CL
from orchestrator.rsi_r1 import contracts as C
from orchestrator.rsi_r1 import lock as LK
from orchestrator.rsi_r1 import state as ST
from tests.rsi_r1_helpers import ENVELOPE
from tests.rsi_r1_rehearsal import (PRACTICE_CHAIN, PROBE_CHAIN,
                                    REHEARSAL_IMAGE, RehearsalDriver)

EIGHT_ACTIONS = ["write_artifact", "propose_plan", "run_batch",
                 "write_artifact", "analyze", "freeze_confirm",
                 "run_confirm", "submit_claim"]


# ---------------------------------------------------------------------------
# 夹具：一次完整正式 campaign（模块级，只建一次）
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def formal(tmp_path_factory: pytest.TempPathFactory) -> dict:
    driver = RehearsalDriver(tmp_path_factory.mktemp("formal"))
    driver.setup()
    driver.freeze()
    driver.advance(ST.ROOT_PROBED, reason="probe lane 排演合格")
    driver.advance(ST.ROOT_PRACTICED, reason="practice lane 排演合格")
    for study, lane, root in (("A", "study_A", ST.ROOT_STUDY_A),
                              ("B", "study_B", ST.ROOT_STUDY_B)):
        driver.chain(study, lane)
        driver.advance(root, reason=f"study {study} 完成")
    replays = {study: driver.replay(study) for study in ("A", "B")}
    driver.advance(ST.ROOT_REPLAY, reason="独立复跑完成")
    driver.advance(ST.ROOT_REVIEW, reason="两 study 复核完成")
    close = driver.closeout()
    return {"driver": driver, "replays": replays, "close": close}


# ---------------------------------------------------------------------------
# AC26 · 全路径零付费
# ---------------------------------------------------------------------------

def test_formal_path_runs_the_eight_action_chain(formal: dict) -> None:
    driver = formal["driver"]
    assert set(driver.chains) == {"A/study_A", "B/study_B"}
    for key in ("A/study_A", "B/study_B"):
        record = driver.chains[key]
        assert [o.get("action") for o in record["outcomes"]] == EIGHT_ACTIONS
        last = record["outcomes"][-1]["result"]
        assert last["judge_verdict"] == "JUDGE_OK"
        assert last["verdict"] == "VERIFIED"
        assert last["claim_status"] == "MATCH"
        # 模型每轮都被真实状态驱动：后呼必须消费前呼结果（ScriptedModel 内断言）


def test_replay_is_independent_and_passes(formal: dict) -> None:
    for study in ("A", "B"):
        comparison = formal["replays"][study]
        assert comparison["ok"] is True, comparison["problems"][:5]
        assert comparison["problems"] == []


def test_closeout_and_independent_checker(formal: dict) -> None:
    close = formal["close"]
    for study in ("A", "B"):
        entry = close["studies"][study]
        assert entry["claim"] == "COMPLETE"
        assert entry["judge_ok"] is True and entry["replay_ok"] is True
    judged = close["judged"]
    assert judged["verdict"] == "PASS"
    assert judged["problems"] == []
    assert len(judged["checks"]) >= 30
    pack = formal["driver"].ctx.relative(CL.PACK_DIRNAME)
    assert CL.verify_pack(pack)["ok"] is True


def test_zero_paid_ledger_is_empty_throughout(formal: dict) -> None:
    driver = formal["driver"]
    summary = CL.ledger_summary(driver.ctx)
    assert summary["calls"] == 0
    rows = CL.ledger_rows(driver.ctx)
    assert [r for r in rows if r.get("event", "call") == "call"] == []


def test_lock_frozen_before_any_exchange(formal: dict) -> None:
    driver = formal["driver"]
    verified = LK.verify_lock(driver.ctx)
    assert verified["ok"] is True
    assert verified["lock_sha256"] == LK.read_lock_sha(driver.ctx)
    doc = driver.ctx.read_json(LK.LOCK_RELPATH)
    groups = doc["closure"]["groups"]
    assert set(groups) == set(LK.CLOSURE_GROUPS)
    # 调度/结案/冻结机件本身也在闭包里：LOCK 必须钉死产生它的同一份代码。
    schema_paths = [f["path"] for f in groups["schema"]["files"]]
    for rel in ("orchestrator/rsi_r1/assembly.py",
                "orchestrator/rsi_r1/closure.py",
                "orchestrator/rsi_r1/lock.py"):
        assert rel in schema_paths
    checker = groups["checker"]["files"][0]["path"]
    assert checker == LK.CLOSURE_GROUPS["checker"][0]
    assert doc["image"]["pin"].startswith("simpraxis/rsi-r1-rehearsal@sha256:")
    sigs = LK.signatures(driver.ctx)
    assert {r: sigs[r]["decision"] for r in LK.ROLES} == {
        "reviewer": "APPROVE", "verifier": "APPROVE"}
    assert sigs["reviewer"]["actor"] != sigs["verifier"]["actor"]
    assert {sigs[r]["lock_sha256"] for r in LK.ROLES} == {
        verified["lock_sha256"]}


def test_candidate_visible_text_has_no_private_material(formal: dict) -> None:
    driver = formal["driver"]
    forbidden = [str(driver.ctx.root), "keeper", "private/", "reservations_",
                 "quota/"]
    for text in driver.public_texts():
        for token in forbidden:
            assert token not in text, f"公开文本泄漏 {token!r}"


# ---------------------------------------------------------------------------
# AC26/AC28 · probe 8 与 practice 16 的边界
# ---------------------------------------------------------------------------

def test_probe_lane_stops_at_eight(tmp_path: Path) -> None:
    driver = RehearsalDriver(tmp_path / "probe")
    driver.setup()
    driver.freeze()
    record = driver.chain("A", "probe")
    assert len(record["outcomes"]) == PROBE_CHAIN
    assert all(o["ok"] for o in record["outcomes"])
    refused = driver.expect_refused("A", "probe")
    assert refused["code"] == "BUDGET_REFUSED"
    slots = (driver.ctx.root / "quota" / "reservations_probe.jsonl")
    assert len(slots.read_text(encoding="utf-8").splitlines()) == PROBE_CHAIN
    assert driver.ledger_calls() == 0


def test_practice_lane_stops_at_sixteen(tmp_path: Path) -> None:
    driver = RehearsalDriver(tmp_path / "practice")
    driver.setup()
    driver.freeze()
    driver.advance(ST.ROOT_PROBED, reason="probe 排演合格")
    for study in ("A", "B"):
        record = driver.chain(study, "practice")
        assert len(record["outcomes"]) == PROBE_CHAIN
        assert all(o["ok"] for o in record["outcomes"])
    slots = (driver.ctx.root / "quota" / "reservations_practice.jsonl")
    assert len(slots.read_text(encoding="utf-8").splitlines()) == PRACTICE_CHAIN
    refused = driver.expect_refused("B", "practice")
    assert refused["code"] == "BUDGET_REFUSED"
    assert driver.ledger_calls() == 0


# ---------------------------------------------------------------------------
# 费用门 fail-closed（零付费不可夹带付费；付费无授权即拒）
# ---------------------------------------------------------------------------

def test_paid_mode_without_consent_is_refused() -> None:
    with pytest.raises(A.AssemblyError) as exc:
        A.SpendContract(mode=A.MODE_PAID)
    assert exc.value.code == "PAID_NOT_AUTHORIZED"


def test_zero_paid_mode_rejects_ledger_call_rows() -> None:
    spend = A.SpendContract(mode=A.MODE_ZERO_PAID)
    envelope = A.Envelope(envelope_id="r1_test", budget_sha256="b" * 64)
    spend.check(envelope=envelope, ledger_rows=[])
    with pytest.raises(A.AssemblyError) as exc:
        spend.check(envelope=envelope,
                    ledger_rows=[{"event": "call", "seq": 1}])
    assert exc.value.code == "SPEND_MODE_VIOLATION"


# ---------------------------------------------------------------------------
# 原接口回归 + 禁用标记扫描
# ---------------------------------------------------------------------------

def test_original_cli_interface_survives() -> None:
    parser = CLI.build_parser()
    sub = [a for a in parser._actions if a.dest == "cmd"][0]
    assert {"init", "check", "prereg", "contract", "controls"} <= set(
        sub.choices)
    assert "freeze" in sub.choices, "T5 未接线 freeze 子命令"
    assert CLI.SEAM_FILES and len(CLI.SEAM_FILES) >= 15
    for rel in CLI.SEAM_FILES:
        assert (LK.REPO_ROOT / rel).is_file(), rel


def test_cli_freeze_freezes_signs_and_verifies(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    driver = RehearsalDriver(tmp_path / "cli")
    driver.setup()
    envelope_doc = driver.ctx.read_json("contract/envelope.json")
    monkeypatch.setattr(
        CLI, "_load_envelope_contract",
        lambda env: (driver.ctx, envelope_doc))
    rc = CLI.main(["freeze", "--envelope", ENVELOPE,
                   "--image", REHEARSAL_IMAGE,
                   "--sign", "reviewer:cli-reviewer:APPROVE:排演双签",
                   "--sign", "verifier:cli-verifier:APPROVE:排演双签"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "verify ok" in out
    verified = LK.verify_lock(driver.ctx)
    sigs = LK.signatures(driver.ctx)
    assert sigs["reviewer"]["actor"] != sigs["verifier"]["actor"]
    assert verified["lock_sha256"] == LK.read_lock_sha(driver.ctx)


def test_no_new_skips_or_todos_in_rsi_r1_tests() -> None:
    """代码面用 AST 扫描（避免命中本检查自身的模式字符串），注释面单独扫。"""
    import ast

    placeholder = re.compile(r"TODO|FIXME")
    test_dir = Path(__file__).resolve().parent
    files = sorted(test_dir.glob("test_rsi_r1_*.py")) + [
        test_dir / "rsi_r1_rehearsal.py"]
    assert len(files) >= 6
    for path in files:
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                # pytest.mark.skip / .skipif / .xfail / pytest.importorskip
                if (isinstance(node.value, ast.Name) and node.value.id == "pytest"
                        and node.attr in ("skip", "skipif", "xfail",
                                          "importorskip")):
                    raise AssertionError(f"{path.name}:{node.lineno} 用了 "
                                         f"pytest.{node.attr}")
            elif isinstance(node, ast.Call):
                fn = node.func
                if (isinstance(fn, ast.Attribute) and fn.attr in (
                        "skip", "xfail") and isinstance(fn.value, ast.Name)
                        and fn.value.id == "pytest"):
                    raise AssertionError(f"{path.name}:{node.lineno} 调用了 "
                                         f"pytest.{fn.attr}()")
            elif isinstance(node, ast.Raise):
                exc = node.exc
                name = getattr(exc, "id", None) or getattr(
                    getattr(exc, "func", None), "id", None)
                if name == "NotImplementedError":
                    raise AssertionError(
                        f"{path.name}:{node.lineno} 抛 NotImplementedError")
        for lineno, line in enumerate(text.splitlines(), 1):
            stripped = line.lstrip()
            if not stripped.startswith("#"):
                continue
            hits = placeholder.findall(stripped)
            assert not hits, f"{path.name}:{lineno} 注释含 {hits}"
