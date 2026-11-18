"""`physicalrsi.task/v1` 环境适配（check/run/evolve/status）。

纪律：
- `check()` 是宿主判据（g21_raw_checker 模式）：特权真值不进入 actor 视野，只回
  pass/fail + 可公开的原因码；
- `run()` 执行一批 run，返回内容寻址 receipt 引用（sha256），不内联大字节；
- `evolve()` 只暴露 `orchestrator.hk.params.ParameterSpace` 的字段——δ/α/判据字段
  根本不存在于返回值里；
- `status()` 是 run 状态机（pending/running/ok/fail + run_idx）。

S0 骨架：checker/runner 由调用方注入（S1 接 MuJoCo 容器批量执行器）；未注入时
`run()` 明确抛 `SkillEngineNotWired`，不造假 run。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from orchestrator.hk import params
from orchestrator.hk.skills import SkillEngineNotWired

__all__ = ["TASK_SCHEMA", "SimPraxisTaskAdapter", "AdapterError"]

TASK_SCHEMA = "physicalrsi.task/v1"
_STATES = ("pending", "running", "ok", "fail")


class AdapterError(RuntimeError):
    """适配协议违规（未知状态、缺 receipt 哈希等）。"""


@dataclass
class SimPraxisTaskAdapter:
    """一个任务包的适配器；`descriptor()` 是协议面，`entrypoint()` 是工厂。"""

    task_id: str
    runner: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None
    checker: Callable[[Mapping[str, Any]], bool] | None = None
    revision: str = "s0"
    _runs: list[dict] = field(default_factory=list)

    # —— 协议面 ——
    def descriptor(self) -> dict:
        return {
            "schema": TASK_SCHEMA,
            "id": self.task_id,
            "revision": self.revision,
            "entrypoint": "SimPraxisTaskAdapter.entrypoint",
            "methods": ["check", "run", "evolve", "status"],
        }

    @classmethod
    def entrypoint(cls, task_id: str, **kwargs) -> "SimPraxisTaskAdapter":
        return cls(task_id=task_id, **kwargs)

    # —— 四方法 ——
    def check(self) -> dict:
        """宿主判据：只回公共 verdict，不回特权数据。"""
        if self.checker is None:
            raise SkillEngineNotWired("checker 未注入：宿主判据在 S1 由容器侧判分器接线")
        try:
            passed = bool(self.checker({"task_id": self.task_id}))
        except Exception as exc:  # noqa: BLE001 - 判分器崩溃是仪器失败，如实上报
            return {"task_id": self.task_id, "verdict": "error",
                    "reason": f"{type(exc).__name__}"}
        return {"task_id": self.task_id, "verdict": "pass" if passed else "fail"}

    def run(self, value: Mapping[str, Any] | None = None) -> dict:
        """执行一批 run；返回 receipt 引用（内容寻址，不含特权数据）。"""
        if self.runner is None:
            raise SkillEngineNotWired(
                "runner 未注入：S0 只落协议与 receipt 形状；真实跑批在 S1 接容器")
        batch = dict(value or {})
        run_idx = len(self._runs) + 1
        self._runs.append({"run_idx": run_idx, "state": "running"})
        try:
            payload = dict(self.runner(batch))
        except Exception:
            self._runs[-1]["state"] = "fail"
            raise
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":")).encode("utf-8")
        receipt = {
            # 单批 run 收据，**不是** experiment-receipt/v2（后者由 receipts.py
            # 与证据包 manifest 逐件互证；两者形状不同，不可混用）
            "schema": "simpraxis.s0.run-receipt.v1",
            "task_id": self.task_id,
            "run_idx": run_idx,
            "sha256": hashlib.sha256(body).hexdigest(),
            "payload": payload,
        }
        self._runs[-1].update({"state": "ok", "receipt_sha256": receipt["sha256"]})
        return {"receipt": receipt}

    def evolve(self) -> dict:
        """可进化面 = ParameterSpace 字段（工程容差）；科学判据结构性不存在。"""
        return {
            "task_id": self.task_id,
            "space_identity": params.SPACE.identity(),
            "fields": sorted(params.FIELDS),
        }

    def status(self) -> dict:
        return {"task_id": self.task_id, "runs": [dict(r) for r in self._runs]}

    # —— 内部校验 ——
    def _assert_state(self, state: str) -> None:
        if state not in _STATES:
            raise AdapterError(f"未知 run 状态 {state!r}；只有 {_STATES}")
