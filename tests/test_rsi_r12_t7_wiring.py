"""RSI-R1.2 T7/T8 接线（2026-11-13）：study_A/study_B/replay 的零 POST 拒绝面。

真跑要容器与付费；这里只锁三件事：
1. 解析器把三条命令挂上，lane/帽从 profile 单一源取（A/B 各 48）；
2. 付费闸先于一切副作用：无 ``SIMPRAXIS_ALLOW_PAID=1`` 时零动作；
3. 复跑必须显式 ``--real-containers``（零模型 POST 但仍会跑容器）。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.rsi_r1 import assembly as ASM
from orchestrator.rsi_r1 import cli_r12
from orchestrator.rsi_r1 import gateway_transport as GT
from orchestrator.rsi_r1 import profiles as P


def _parser():
    return cli_r12.build_parser()


def _args(parser, *argv):
    return parser.parse_args(list(argv))


def test_study_and_replay_are_wired():
    parser = _parser()
    for name in ("study_A", "study_B", "replay", "probe", "practice",
                 "closeout", "pack", "judge"):
        assert name in parser._subparsers._group_actions[0].choices


def test_study_lanes_and_caps_come_from_profile():
    assert dict(ASM.STUDY_LANE) == {"A": "study_A", "B": "study_B"}
    caps = P.PROFILE_R12_DEF.lane_post_caps
    assert caps["study_A"] == 48 and caps["study_B"] == 48


def test_study_refuses_without_paid_env(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("SIMPRAXIS_ALLOW_PAID", raising=False)
    args = _args(_parser(), "study_A", "--envelope", "env-x",
                 "--batch-id", "B1", "--tokenizer", "t", "--tokenizer-sha256", "s")
    with pytest.raises(GT.TransportError) as exc:
        cli_r12.cmd_study(args)
    assert "PAID" in str(exc.value).upper()


def test_replay_requires_real_containers_flag(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("SIMPRAXIS_ALLOW_PAID", raising=False)
    args = _args(_parser(), "replay", "--envelope", "env-x", "--id", "A",
                 "--batch-id", "R1")
    with pytest.raises(cli_r12.Cli12Error) as exc:
        cli_r12.cmd_replay(args)
    assert exc.value.code == "REPLAY_REAL_CONTAINERS_REQUIRED"


def test_sandbox_memory_limit_covers_real_confirm_volume():
    """D-6 回归：内存帽必须覆盖实测（模板峰值 695 MB@96 runs/278 MB）。"""
    from orchestrator.rsi_r1 import sandbox as SB

    raw = SB.MEMORY_LIMIT.strip().lower()
    assert raw.endswith("g"), "内存帽必须 ≥1g（512m 在真实确认批上 OOM）"
    assert float(raw[:-1]) >= 2, "实测峰值 695 MB，至少 2g 余量"
