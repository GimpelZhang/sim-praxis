"""RSI-R1.2 T4：校准 v2 新 profile 分派（AC56）。

覆盖：

* ``controls --calibration-version v2``：v1 一律拒绝（历史件不在新根重跑）；
* 新根零模型 POST permit：paid LOCK 不存在 + 生产装配入口机械证明 LOCK_MISSING；
* 当前 root 的 R1.2 账（2904）0 行；**族历史 31 不是当前 root 付费**，不参与判定，
  当前 root 一旦有 call 行即 ``NOT_ZERO_POST``；
* 来源/镜像/矩阵/量表/oracle 身份（import 加载 + sha 对账）漂移各自拒绝；
* v1 历史件逐字节引用（旧 root 只读）；漂移即 ``PRIOR_ATTEMPT_DRIFT`` 且不跑容器；
* 校准全程零 socket 连接（零 POST 的可执行证据）。
"""
from __future__ import annotations

import json
import shutil
import socket
from pathlib import Path

import pytest

from orchestrator.common import runtime_paths as RP
from orchestrator.rsi_r1 import calibration as CAL
from orchestrator.rsi_r1 import cli_r12
from orchestrator.rsi_r1 import experiments as E
from orchestrator.rsi_r1 import family as FAM
from tests.test_rsi_r1_calibration import FakeCtx

STUB = Path(__file__).parent / "fixtures" / "rsi_r1" / "calibration_oracle_stub.py"
SCHEMA_V1 = "simpraxis.rsi_r1.calibration_oracle.v1"
SCHEMA_V2 = "simpraxis.rsi_r1.calibration_oracle.v2"


def _oracle_text(schema: str) -> str:
    text = STUB.read_text(encoding="utf-8")
    return text.replace(
        'ORACLE_SCHEMA = "simpraxis.rsi_r1.calibration_oracle.v1"',
        f'ORACLE_SCHEMA = "{schema}"')


def _init_r12(tmp_path: Path, monkeypatch, envelope: str = "env_cal") -> Path:
    root = tmp_path / "rsi_r12"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("SIMPRAXIS_RSI_R12_ROOT", str(root))
    monkeypatch.setenv("SIMPRAXIS_RSI_R12_CAMPAIGN", envelope)
    plan = tmp_path / "plan.md"
    plan.write_text("plan bytes", encoding="utf-8")
    assert cli_r12.main(["init", "--envelope", envelope, "--plan", str(plan)]) == 0
    return root / envelope


def _prior_root(tmp_path: Path) -> Path:
    """合成旧 R1 campaign 根：v1 预注册/报告/缺口件/oracle/raw（只读历史）。"""
    prior = tmp_path / "prior_r1"
    (prior / "contract").mkdir(parents=True)
    (prior / "private" / "calibration_private").mkdir(parents=True)
    (prior / "contract" / "calibration_prereg.json").write_text(
        json.dumps({"schema": CAL.PREREG_SCHEMA,
                    "document_sha256": "deadbeef" * 8}) + "\n",
        encoding="utf-8")
    (prior / "contract" / "calibration_report.json").write_text(
        json.dumps({"schema": CAL.REPORT_SCHEMA,
                    "verdict": "DESIGN_NOT_READY"}) + "\n", encoding="utf-8")
    for rel in ("contract/calibration_report.md",
                "contract/calibration_gap_analysis.md"):
        (prior / rel).write_text(f"v1 historical bytes for {rel}\n",
                                 encoding="utf-8")
    (prior / "private" / "calibration_private" / "oracle.py").write_text(
        _oracle_text(SCHEMA_V1), encoding="utf-8")
    for batch in CAL.PRIOR_ATTEMPT_RAW_BATCHES:
        d = prior / "observations" / batch
        d.mkdir(parents=True)
        (d / "run_0001.jsonl").write_text('{"time_s": [0.0]}\n',
                                          encoding="utf-8")
    return prior


def _new_oracle(root: Path, schema: str = SCHEMA_V2) -> Path:
    target = root / CAL.ORACLE_RELPATH_V2
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_oracle_text(schema), encoding="utf-8")
    return target


def _prepare(root: Path, prior: Path) -> int:
    return cli_r12.main(["controls", "--root", str(root), "--prepare-only",
                         "--prior-root", str(prior)])


@pytest.fixture()
def camp(tmp_path, monkeypatch):
    root = _init_r12(tmp_path, monkeypatch)
    prior = _prior_root(tmp_path)
    _new_oracle(root)
    return root, prior


# ---------------------------------------------------------------------------
# 版本分派
# ---------------------------------------------------------------------------

def test_v1_calibration_version_is_rejected(camp, capsys) -> None:
    root, prior = camp
    # argparse choices 只放 v2；绕过 parser 直接调 cmd 也必须拒绝
    ns = type("NS", (), {
        "root": str(root), "envelope": None, "calibration_version": "v1",
        "prepare_only": True, "real_containers": False, "image": None,
        "prior_root": str(prior)})()
    with pytest.raises(cli_r12.Cli12Error) as exc:
        cli_r12.cmd_controls(ns)
    assert exc.value.code == "R12_CALIBRATION_VERSION"
    assert not (root / CAL.PREREG_RELPATH_V2).exists()


def test_prepare_requires_prior_root(camp) -> None:
    root, _ = camp
    with pytest.raises(cli_r12.Cli12Error) as exc:
        cli_r12.cmd_controls(type("NS", (), {
            "root": str(root), "envelope": None, "calibration_version": "v2",
            "prepare_only": True, "real_containers": False, "image": None,
            "prior_root": None})())
    assert exc.value.code == "PRIOR_ROOT_REQUIRED"


def test_prior_root_inside_r12_family_is_rejected(camp) -> None:
    root, _ = camp
    with pytest.raises(cli_r12.Cli12Error) as exc:
        cli_r12.cmd_controls(type("NS", (), {
            "root": str(root), "envelope": None, "calibration_version": "v2",
            "prepare_only": True, "real_containers": False, "image": None,
            "prior_root": str(root)})())
    assert exc.value.code == "PRIOR_ROOT_INSIDE_R12"


# ---------------------------------------------------------------------------
# prepare：预注册 + 零 POST permit（零网络、旧根只读）
# ---------------------------------------------------------------------------

def test_prepare_only_writes_prereg_and_zero_post_permit(camp, capsys,
                                                         monkeypatch) -> None:
    root, prior = camp
    prior_before = {p: p.read_bytes() for p in prior.rglob("*") if p.is_file()}

    def _no_connect(*_a, **_k):
        raise AssertionError("prepare-only 期间不得建立任何网络连接")

    monkeypatch.setattr(socket.socket, "connect", _no_connect)
    assert _prepare(root, prior) == 0
    out = capsys.readouterr().out

    prereg = json.loads((root / CAL.PREREG_RELPATH_V2).read_text())
    assert prereg["schema"] == CAL.PREREG_SCHEMA_V2
    assert prereg["design_version"] == 2
    assert prereg["envelope_id"] == "env_cal"
    assert prereg["container_image"] == CAL.CAL_IMAGE
    assert prereg["supersedes"]["prior_root"] == str(prior.resolve())
    assert prereg["prior_attempt"]["n_files"] == len(CAL.PRIOR_ATTEMPT_FILES)
    assert prereg["prior_attempt"]["n_raws"] == 2

    permit = json.loads((root / CAL.PERMIT_RELPATH).read_text())
    assert permit["schema"] == CAL.PERMIT_SCHEMA
    assert permit["model_posts_allowed"] == 0
    assert permit["sender_refusal"]["code"] == "LOCK_MISSING"
    assert permit["sender_refusal"]["transport_factory_called"] is False
    assert permit["ledger_path"] == str(root / "quota" / "ledger.jsonl")
    assert permit["ledger_call_rows_at_issue"] == 0

    # 旧根逐字节未动（只读引用）
    prior_after = {p: p.read_bytes() for p in prior.rglob("*") if p.is_file()}
    assert prior_after == prior_before
    assert "permit  : zero_post_control posts=0 refusal=LOCK_MISSING" in out


def test_family_history_31_is_not_current_root_paid(camp) -> None:
    root, prior = camp
    fam = FAM.usage(RP.rsi_r12_root())
    assert fam["history_posts"] == 31          # 族历史确实存在…
    assert _prepare(root, prior) == 0          # …但不阻塞新根
    permit = json.loads((root / CAL.PERMIT_RELPATH).read_text())
    assert permit["ledger_call_rows_at_issue"] == 0
    # 当前 root 一旦出现 call 行：permit 立即不可用
    ledger = root / "quota" / "ledger.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(json.dumps({"event": "call", "seq": 1}) + "\n",
                      encoding="utf-8")
    ctx = cli_r12.R12Context(root, "env_cal")
    with pytest.raises(CAL.CalibrationError) as exc:
        CAL.assert_permit_usable(ctx, permit, ledger_path=ledger)
    assert exc.value.code == "NOT_ZERO_POST"
    with pytest.raises(CAL.CalibrationError) as exc2:
        CAL.issue_zero_post_permit(ctx, ledger_path=ledger)
    assert exc2.value.code == "NOT_ZERO_POST"


def test_r12_ledger_dispatch(camp, monkeypatch) -> None:
    root, _ = camp
    monkeypatch.setenv("SIMPRAXIS_RSI_R1_CAMPAIGN", "legacy-cam")
    ctx = cli_r12.R12Context(root, "env_cal")
    assert CAL._default_ledger(ctx) == RP.rsi_r12_ledger_path()
    assert CAL._default_ledger(FakeCtx(root)) == RP.rsi_r1_ledger_path()


def test_prior_root_context_refuses_writes(camp) -> None:
    root, prior = camp
    ctx = cli_r12.R12Context(prior.resolve(), "env_cal", readonly=True)
    with pytest.raises(cli_r12.Cli12Error) as exc:
        ctx.write_json("contract/evil.json", {"x": 1})
    assert exc.value.code == "PRIOR_ROOT_READONLY"


# ---------------------------------------------------------------------------
# 漂移拒绝（来源/镜像/矩阵/量表/oracle），且都在跑容器之前
# ---------------------------------------------------------------------------

def _runner_that_must_not_run():
    class _R:
        def __call__(self, *a, **k):
            raise AssertionError("漂移必须在跑容器之前拒绝")

        def run(self, *a, **k):
            raise AssertionError("漂移必须在跑容器之前拒绝")
    return _R()


def test_image_drift_rejected_before_containers(camp) -> None:
    root, prior = camp
    assert _prepare(root, prior) == 0
    ctx = cli_r12.R12Context(root, "env_cal")
    with pytest.raises(CAL.CalibrationError) as exc:
        CAL.run_calibration(ctx, runner=_runner_that_must_not_run(),
                            image="other/image@sha256:" + "0" * 64,
                            ledger_path=root / "quota" / "ledger.jsonl",
                            design=CAL.DESIGN_V2,
                            prior_ctx=cli_r12.R12Context(
                                prior.resolve(), "env_cal", readonly=True))
    assert exc.value.code == "IMAGE_DRIFT"


def test_oracle_drift_rejected_before_containers(camp) -> None:
    root, prior = camp
    assert _prepare(root, prior) == 0
    oracle = root / CAL.ORACLE_RELPATH_V2
    oracle.write_text(oracle.read_text() + "\n# drift\n", encoding="utf-8")
    ctx = cli_r12.R12Context(root, "env_cal")
    with pytest.raises(CAL.CalibrationError) as exc:
        CAL.run_calibration(ctx, runner=_runner_that_must_not_run(),
                            image=CAL.CAL_IMAGE,
                            ledger_path=root / "quota" / "ledger.jsonl",
                            design=CAL.DESIGN_V2,
                            prior_ctx=cli_r12.R12Context(
                                prior.resolve(), "env_cal", readonly=True))
    assert exc.value.code == "ORACLE_DRIFT"


def test_oracle_import_verification(camp, tmp_path) -> None:
    bad = tmp_path / "bad_oracle.py"
    bad.write_text(_oracle_text("simpraxis.wrong.schema"), encoding="utf-8")
    with pytest.raises(CAL.CalibrationError) as exc:
        CAL.load_oracle(bad, schema=SCHEMA_V2)
    assert exc.value.code == "ORACLE_INTERFACE"
    missing = tmp_path / "missing_oracle.py"
    with pytest.raises(CAL.CalibrationError) as exc2:
        CAL.load_oracle(missing, schema=SCHEMA_V2)
    assert exc2.value.code == "ORACLE_MISSING"


def test_prior_attempt_drift_blocks_without_containers(camp, monkeypatch) -> None:
    root, prior = camp
    assert _prepare(root, prior) == 0
    # 改旧根一条 raw（历史不可改写）
    raw = prior / "observations" / "calibration_a" / "run_0001.jsonl"
    raw.write_text('{"time_s": [1.0]}\n', encoding="utf-8")
    called = {"n": 0}

    def _boom(*a, **k):
        called["n"] += 1
        raise AssertionError("prior 漂移时不得执行任何物理批次")

    monkeypatch.setattr(E, "execute_batch", _boom)
    ctx = cli_r12.R12Context(root, "env_cal")
    report = CAL.run_calibration(
        ctx, runner=_runner_that_must_not_run(), image=CAL.CAL_IMAGE,
        ledger_path=root / "quota" / "ledger.jsonl", design=CAL.DESIGN_V2,
        prior_ctx=cli_r12.R12Context(prior.resolve(), "env_cal", readonly=True))
    assert called["n"] == 0
    assert report["verdict"] == "INVALID"
    assert report["blocked"].startswith("PRIOR_ATTEMPT")
    prior_check = [c for c in report["checks"]
                   if c["id"] == "GLOBAL-PRIOR-ATTEMPT"]
    assert len(prior_check) == 1 and prior_check[0]["ok"] is False
    # 报告落新根（v2 路径），不写旧根
    assert (root / CAL.REPORT_RELPATH_V2).is_file()
    assert not (prior / CAL.REPORT_RELPATH_V2).exists()


def test_real_containers_flag_required(camp, capsys) -> None:
    root, prior = camp
    assert _prepare(root, prior) == 0
    rc = cli_r12.main(["controls", "--root", str(root),
                       "--prior-root", str(prior)])
    assert rc == 2
    assert "--real-containers" in capsys.readouterr().err
