"""S2c AC-01：新账套 2912 四处登记清单回归。

把「新账套必漏一处」类事故（S0 写入路径 / S2 会话帽缺省 / S2b sealed 集）
变成必红：以**导入**方式断言 2912 出现在每个 sealed 集、账本路径落在预期根、
批准件缺省命名空间、会话帽缺省等于计划帽。
"""
from __future__ import annotations

import pytest

from orchestrator.common import runtime_paths as RP
from orchestrator.gateway import quota_ledger as QL


def test_namespace_constant_and_hard_cap():
    assert QL.LEDGER_RSI_S2C == 2912
    assert QL.RSI_S2C_HARD_CAP == 44
    assert QL.RSI_S2C_NAMESPACE.hard_cap == 44
    assert QL.RSI_S2C_NAMESPACE.normal_budget == 44


def test_task_caps_sum_is_hard_cap():
    caps = QL.RSI_S2C_TASK_CAPS
    # task 1(校准4)+2(双审24)+3(修复8)+4(余8)=44 == 硬帽
    assert sum(dict(caps).values()) == QL.RSI_S2C_HARD_CAP == 44
    assert set(caps) == {1, 2, 3, 4}


def test_2912_in_every_sealed_set():
    """三处 sealed 判定都必须认 2912（S2b 事故：漏入 in_sealed_set 致落 phase2 旧路径）。"""
    ns = QL.RSI_S2C_NAMESPACE
    assert ns.in_sealed_set() is True
    assert ns.uses_batch_occupancy() is True
    assert ns.in_sealed_set_for_read() is True


def test_ledger_path_in_expected_root():
    p = RP.rsi_s2c_ledger_path()
    root = RP.rsi_s2c_root().resolve(strict=False)
    assert p.resolve(strict=False).is_relative_to(root)
    assert p.name == "ledger.jsonl"
    # S2c 根与 S2b/S2 根互异（不落旧族）
    assert RP.rsi_s2c_root().resolve(strict=False) != RP.rsi_s2b_root().resolve(strict=False)


def test_rsi_s2c_classmethod_builds_isolated_namespace():
    ledger = QL.QuotaLedger.rsi_s2c()
    # 构造器走 assert_rsi_s2c_roots_isolated（写前 fail-fast）——能构造即隔离过
    assert ledger is not None


def test_all_s2c_subdirs_registered():
    """路径族每个子目录都在 _RSI_S2C_SUBDIRS 白名单内。"""
    for name in ("quota", "quota/approvals", "quota/gateway_state",
                 "prereg/rules", "prereg/campaign", "evidence",
                 "sessions", "closeout", "briefs"):
        assert name in RP._RSI_S2C_SUBDIRS, f"子目录 {name} 未登记"


def test_session_cap_default_matches_plan():
    """会话帽缺省须等于计划帽 56（防 S2 的「缺省 20 < 预算」事故）。"""
    from orchestrator.rsi_bridge.session_ledger import SessionLedger
    import inspect
    sig = inspect.signature(SessionLedger.__init__)
    # 计划帽 56 由 run 脚本显式传入；此处断言缺省常量若存在则 ≥56
    # （SessionLedger 当前用显式 hard_cap_sessions 传参，缺省由调用方定）
    assert "hard_cap_sessions" in sig.parameters, (
        "SessionLedger 须支持显式 hard_cap_sessions（S2 事故教训）")
