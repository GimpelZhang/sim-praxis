"""RSI-R0.1 负控/独立 oracle 单测（T3 / AC13、AC17、AC18）：零网络、零容器。

容器类负控（单位/符号/漏格/伪数据/错run/晚登记/旧反例）由
``scripts/rsi_r01/controls.py`` 真实执行并落 ``analysis/controls/controls.json``；
这里只锁**不依赖容器**的判据本身，以及 oracle 的独立性审计。
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
for _p in (_REPO / "scripts" / "rsi_r01", _REPO / "scripts" / "verification"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import controls as C  # noqa: E402
import independent_oracle as ORACLE  # noqa: E402
import oracle_audit as OA  # noqa: E402

from orchestrator.rsi_r01 import evaluator as EV  # noqa: E402
from orchestrator.rsi_r01 import schemas as S  # noqa: E402


def test_summary_omitting_decisive_tail_is_rejected():
    out = C.ctl_tail_omission()
    assert out["rejected"], out["observed"]
    obs = out["observed"]
    assert obs["same_summary"] is True
    assert obs["full_file_observation"] == S.ObservationStatus.CENSORED
    assert obs["full_file_claim"] == S.ClaimStatus.INCONCLUSIVE
    assert obs["summary_claim"] == S.ClaimStatus.SUPPORTED
    assert obs["summary_replay_observation"] == S.ObservationStatus.INVALID


def test_numeric_boundaries_hold():
    out = C.ctl_numeric_boundaries()
    assert out["rejected"], out["observed"]
    assert out["observed"]["exactly_threshold_inside"] is False
    assert out["observed"]["t_low_zero"]["reason"] == "t_low_zero"


def test_oracle_import_audit_rejects_evaluator_dependency(tmp_path):
    good = OA.import_audit(_REPO / "scripts" / "rsi_r01" / "independent_oracle.py")
    assert good["ok"], good
    assert "evaluator" not in " ".join(good["imports"])

    # 反例：临时模块 import evaluator ⇒ 审计必须拒绝（审计不是同义反复）。
    tmp = tmp_path / "fake_oracle.py"
    tmp.write_text(
        "from orchestrator.rsi_r01 import evaluator\n"
        "def settle():\n    return evaluator.settling\n",
        encoding="utf-8")
    bad = OA.import_audit(tmp)
    assert not bad["ok"] and bad["touches_evaluator"]


def test_oracle_and_judge_agree_on_header_only_difference():
    """两算法（反向扫描 vs 后缀归约）在真实形状上逐项一致。"""
    n = S.EXPECTED_ROWS
    t = [i * S.DT_S for i in range(n)]
    theta = [0.5 if i < 300 else 0.0 for i in range(n)]
    omega = [0.0] * n
    judge = EV.settling(theta, omega, t)
    oracle = ORACLE.settle_suffix(theta, omega, t)
    assert judge.index == oracle["index"] == 300
    assert abs(judge.time_s - oracle["time_s"]) <= ORACLE.TOL


def test_oracle_compare_flags_tampered_claim():
    oracle = {"claim_status": S.ClaimStatus.SUPPORTED, "mean_effect": 0.5,
              "z": 0.5, "settle": {}}
    judge = {"claim_status": S.ClaimStatus.REFUTED, "mean_effect": 0.5,
             "z": 0.5, "cells": {}}
    problems = ORACLE.compare(judge, oracle)
    assert any("claim_status" in p for p in problems)
