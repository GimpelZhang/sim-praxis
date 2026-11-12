"""RSI-R1.2 T2 测试共用：假链部件与脚本化回合（全离线，零 POST）。

``drive_chain`` 的 R1.2 路径只依赖 ``LaneParts`` 的四个接缝：
``admission.posts_used`` / ``loop.turn`` / ``state.study_status`` / ``state.study_state``。
这里用最小假件驱动，不构造 campaign 根、不触及 Docker/Judge。
"""
from __future__ import annotations

import types
from typing import Any

from orchestrator.rsi_r1 import loop as L
from orchestrator.rsi_r1 import protocol as PT


class ScriptedLoop:
    """按脚本吐回合；脚本用尽还调用即断言（证明没有多发一呼）。"""

    def __init__(self, outcomes: "list[L.StepOutcome]", admission: "FakeAdmission"
                 ) -> None:
        self.outcomes = list(outcomes)
        self.admission = admission
        self.turns = 0

    def turn(self) -> L.StepOutcome:
        if not self.outcomes:
            raise AssertionError("脚本用尽仍在发送：驱动多发了 POST")
        self.turns += 1
        self.admission.used += 1
        return self.outcomes.pop(0)


class FakeAdmission:
    def __init__(self) -> None:
        self.used = 0

    def posts_used(self, lane: str) -> int:
        del lane
        return self.used


class FakeState:
    def __init__(self, *, confirm_lock: Any = None,
                 open_intent: Any = None) -> None:
        self._status = {"confirm_lock": confirm_lock,
                        "open_intent": open_intent}

    def study_status(self, study: str) -> "dict[str, Any]":
        del study
        return dict(self._status)

    def study_state(self, study: str) -> str:
        del study
        return "EXPLORING"


def ok(action: str, **result: Any) -> L.StepOutcome:
    return L.StepOutcome(ok=True, action=action,
                         result={"action": action, **result})


def fail(action: "str | None", code: str, *,
         effect: str = "NONE", **result: Any) -> L.StepOutcome:
    return L.StepOutcome(ok=False, action=action, code=code,
                         result={"action": action, "code": code,
                                 "side_effect_status": effect, **result})


def make_parts(outcomes: "list[L.StepOutcome]", *,
               state: FakeState | None = None,
               lane: str = PT.PRACTICE,
               study: str = "A") -> "types.SimpleNamespace":
    admission = FakeAdmission()
    return types.SimpleNamespace(
        lane=lane, billing_lane=lane, study=study, admission=admission,
        state=state or FakeState(), loop=ScriptedLoop(outcomes, admission),
        scope=None, slots=None, dispatcher=None, store=None, sender=None)
