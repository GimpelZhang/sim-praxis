"""S2c AC-03：G-LOCK-REV 守卫——冻结后改设计的三种形态。

编译自 S2b 真实事故（LOCK 后 8 次适配改 SUT 文件却未重签）。
"""
from __future__ import annotations

import pytest

from orchestrator.hk.guards import GUARDS, GuardCtx, GuardViolation, g_lock_rev


def test_no_design_change_passes():
    g_lock_rev(GuardCtx(stage="K0"))  # 无变更 → 放行


def test_declared_false_design_change_passes():
    # 回归：design_change=False（显式声明不改设计面）必须放行，不得误报 NO_REV。
    g_lock_rev(GuardCtx(stage="K0", lock_rev=2, design_change=False,
                        proposed_lock_rev=2, lock_resigned=False))


def test_design_change_without_rev_rejected():
    ctx = GuardCtx(stage="K1", lock_rev=2, design_change="改因子水平",
                   proposed_lock_rev=2, lock_resigned=True)
    with pytest.raises(GuardViolation) as e:
        g_lock_rev(ctx)
    assert e.value.code == "FROZEN_DESIGN_CHANGE_NO_REV"


def test_design_change_rev_increased_but_unsigned_rejected():
    ctx = GuardCtx(stage="K1", lock_rev=2, design_change="改因子水平",
                   proposed_lock_rev=3, lock_resigned=False)
    with pytest.raises(GuardViolation) as e:
        g_lock_rev(ctx)
    assert e.value.code == "FROZEN_DESIGN_CHANGE_UNSIGNED"


def test_legal_locked_adaptation_passes():
    ctx = GuardCtx(stage="K1", lock_rev=2, design_change="改因子水平",
                   proposed_lock_rev=3, lock_resigned=True)
    g_lock_rev(ctx)  # rev 递增 + 重签 → 放行


def test_guard_registered():
    assert "G-LOCK-REV" in GUARDS
    assert GUARDS["G-LOCK-REV"] is g_lock_rev
