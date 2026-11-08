"""T7 六条独立验证轨迹（固定顺序、每 slot 新会话、至多 24 请求）。

与 T6 的差别只在参数与纪律：

* 固定顺序（计划 §3.3 的 VALIDATION_ORDER），不允许调序或跳过；
* 每条轨迹一个新会话、一个新工作区，**无人工解题、无中间反馈改执行器**；
* 全部指定 slot 记终态：失败的、空回答的、超时的都进预定分母；
* 全局安全/资源硬门触发即停批，其余 slot 标 ``CANCELLED``（不消失、不补测）；
* 条件于基础设施可用的比例只做**次要诊断**，不拿来当主指标。

本模块复用 :class:`orchestrator.rsi_r0.develop.DevelopmentTrial`：同一台机器、
同一协议、同一记账，避免"开发与验证两套实现"这种典型的口径漂移。
"""
from __future__ import annotations

import time
from typing import Any, Callable, Mapping, Sequence

from orchestrator.common import runtime_paths as rp
from orchestrator.rsi_r0 import develop as dev
from orchestrator.rsi_r0.attempts import campaign_lock
from orchestrator.rsi_r0.contracts import sha256_of

REPORT_SCHEMA = "rsi_r0.validation_report.v1"

#: 固定顺序 + 批次 + 账本 task（前三条 task=2、后三条 task=3；各 72 帽）。
VALIDATION_PLAN: tuple[tuple[str, str, str, int], ...] = (
    ("E", "E-v1", "R0-V-E1", 2),
    ("D", "D-v1", "R0-V-D1", 2),
    ("R", "R-v1", "R0-V-R1", 2),
    ("E", "E-v2", "R0-V-E2", 3),
    ("D", "D-v2", "R0-V-D2", 3),
    ("R", "R-v2", "R0-V-R2", 3),
)
VALIDATION_CAP = 24


class ValidationError(RuntimeError):
    """没资格跑验证（G-V 不成立/票据缺失/合同缺失）。"""


def run_validation(runner: Any, gateway: Any, *,
                   plan: Sequence[tuple[str, str, str, int]] = VALIDATION_PLAN,
                   session_factory: Callable[..., Any] | None = None,
                   clock: Callable[[], str] | None = None) -> dict[str, Any]:
    """按固定顺序跑六条验证轨迹；任一 slot 的**仪器类**失败即停批。

    全程持 campaign 锁（与 T5/T6 同一口径）：验证批次也是付费路径，并发第二个
    执行器会被直接拒绝，而不是靠"人不会同时跑"。
    """
    with campaign_lock(rp.rsi_r0_quota_dir()):
        return _run_validation_locked(runner, gateway, plan=plan,
                                      session_factory=session_factory, clock=clock)


def _run_validation_locked(runner: Any, gateway: Any, *,
                           plan: Sequence[tuple[str, str, str, int]],
                           session_factory: Callable[..., Any] | None = None,
                           clock: Callable[[], str] | None = None) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    halted: str | None = None
    for family_key, instance_id, batch_id, task_id in plan:
        if halted is not None:
            rows.append({"family": family_key, "instance_id": instance_id,
                         "batch_id": batch_id, "role": "validation",
                         "state": "CANCELLED", "cancel_reason": halted,
                         "requests_used": 0, "ledger_seqs": [],
                         "evaluation": {}, "checkpoints": [], "log": []})
            continue
        kwargs: dict[str, Any] = {}
        if session_factory is not None:
            kwargs["session_factory"] = session_factory
        trial = dev.DevelopmentTrial(
            runner, gateway, family_key, instance_id=instance_id, batch_id=batch_id,
            role="validation", task_id=task_id, request_cap=VALIDATION_CAP,
            checkpoints=(), clock=clock, **kwargs)
        try:
            row = trial.run()
        except dev.DevelopmentBlocked as exc:
            row = {"family": family_key, "instance_id": instance_id,
                   "batch_id": batch_id, "role": "validation", "state": "BLOCKED",
                   "blocked_reason": str(exc), "requests_used": trial._used,
                   "ledger_seqs": list(trial._seqs), "evaluation": {},
                   "checkpoints": [], "log": []}
            if _is_instrument_failure(str(exc)):
                halted = f"{instance_id}: {exc}"
        except dev.DevelopmentError as exc:
            raise ValidationError(f"{instance_id} 前置不成立：{exc}") from exc
        rows.append(row)

    assigned = len(plan)
    terminal = [r for r in rows if r["state"] in ("SUBMITTED", "NO_SUBMISSION")]
    report = {
        "schema": REPORT_SCHEMA,
        "campaign": str(runner.root),
        "created_at_utc": (clock or (lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                           time.gmtime())))(),
        "protocol_spec": dict(dev.PROTOCOL_SPEC),
        "protocol_spec_sha256": dev.protocol_spec_hash(),
        "model": {"stage": dev.STAGE, "image": dev.IMAGE_REF},
        "plan": [{"family": f, "instance_id": i, "batch_id": b, "task_id": t}
                 for f, i, b, t in plan],
        "halted": halted,
        "slots": rows,
        "all_assigned": {
            "assigned": assigned,
            "completed": len(terminal),
            "cancelled": sum(1 for r in rows if r["state"] == "CANCELLED"),
            "blocked": sum(1 for r in rows if r["state"] == "BLOCKED"),
            "denominator": assigned,
            "note": "失败/空回答/超时全部留在分母里；cancelled 也不从分母移除",
        },
        "totals": {
            "requests_used": sum(int(r.get("requests_used") or 0) for r in rows),
            "ledger_seqs": [s for r in rows for s in (r.get("ledger_seqs") or [])],
        },
    }
    report["report_sha256"] = sha256_of(report)
    return report


def _is_instrument_failure(reason: str) -> bool:
    """仪器类失败（隐藏重试/记账缺口/容器不可用）才停批；能力失败不扩散。"""
    markers = (dev.HIDDEN_RETRY_MARKER, "记账缺口", "可信服务失败", "docker")
    return any(m in reason for m in markers)


def write_report(report: Mapping[str, Any], path=None) -> Any:
    import json
    from pathlib import Path

    target = Path(path) if path else (rp.rsi_r0_protocol_dir() / "validation_report.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                      encoding="utf-8")
    return target
