"""付费发送的唯一受闸通道（reviewer MEDIUM-4：``assert_paid_allowed`` 生产接线）。

设计约束：
* 本模块是 **T6–T9 所有付费 POST 的唯一入口**——Agent 侧的付费 runner 必须持
  :class:`PaidTransport`，不得直接调用 :class:`transport.Transport`。
* 每一 POST 之前重算全部 LOCK 事实（自哈希 + 实现面 25 件 + G-I 证据 + 计划/授权
  活文件 + 价格/预算 + 双签）；任一漂移抛 :class:`lock.LockError`，**零 POST**。
* 闸门失败不消耗 attempt、不占用 seq、不写台账——因为还没发。
"""
from __future__ import annotations

from typing import Any, Callable, Mapping

from orchestrator.rsi_r01 import campaign as C
from orchestrator.rsi_r01 import lock as L
from orchestrator.rsi_r01 import transport as T

__all__ = ["PaidTransport"]


class PaidTransport:
    """在 :class:`transport.Transport` 之上强制 LOCK 闸的付费通道。"""

    def __init__(self, ctx: C.CampaignContext, *,
                 repo_root: "Any" = L.REPO_ROOT) -> None:
        self.ctx = ctx
        self.repo_root = repo_root
        self.inner = T.Transport()

    # --- 透传只读视图 ---------------------------------------------------
    @property
    def attempts(self) -> "list[T.Attempt]":
        return self.inner.attempts

    @property
    def posts(self) -> int:
        return self.inner.posts

    def summary(self) -> dict[str, Any]:
        return self.inner.summary()

    # --- 受闸发送 ---------------------------------------------------------
    def send(self, plan: T.SendPlan, accounting: T.Accounting, *,
             slot_id: str, sender: "Callable[[T.SendPlan], Any]") -> T.Attempt:
        """先过 LOCK 闸再发；闸失败 ⇒ ``LockError`` 且 sender 一次都没被调用。"""
        L.assert_paid_allowed(self.ctx, repo_root=self.repo_root)
        return self.inner.send(plan, accounting, slot_id=slot_id, sender=sender)
