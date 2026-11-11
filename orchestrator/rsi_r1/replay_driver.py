"""RSI-R1 T8：独立复跑驱动面（两 study 各一次，零 POST、真容器）。

Plan §10 T8：确认批的结论必须能被**独立复跑**检验——同一个已冻结确认计划，
由 host 按 manifest 重建 spec，在**新批次/新证据根/新容器**里再跑一遍，
逐 run 比对并给出独立性证据（执行 id、容器、开始时间、raw sha、根目录）。
本模块只做驱动：spec 重建在 ``judge.build_specs``、比对在 ``replay``、
落件在 ``closure``；不复制其中任何一条判据。

与 T7 的三点差别：

* **零 POST**：不建网关、不建 transport、不占任何 lane 的呼量（账本恒不动）；
* **新进程可重入**：confirm 计划与 manifest 一律从 ``confirm/`` 落盘件读回，
  原件观测从 ``observations/confirm-<X>-<token12>/`` 读回并先过
  ``O.verify_manifest``——不依赖上一进程的 dispatcher 内存；
* **一次封存**：``replay/comparison_<X>.json`` 在场即拒重跑（科学产物只封一次）；
  失败/阻断的报告件不构成 comparison，允许在修复后重试并在报告里累计 attempts。

root 推进：两个 study 的 comparison **都**在场才从 ``STUDY_B`` 推进到 ``REPLAY``；
任何一次 BLOCKED（基础设施整批失败）都不推进。
"""
from __future__ import annotations

import json
from typing import Any, Callable, Mapping

from orchestrator.common.clock import utc_now_iso_z
from orchestrator.rsi_r1 import assembly as A
from orchestrator.rsi_r1 import backend as BK
from orchestrator.rsi_r1 import closure as CL
from orchestrator.rsi_r1 import contracts as C
from orchestrator.rsi_r1 import experiments as E
from orchestrator.rsi_r1 import judge as J
from orchestrator.rsi_r1 import lock as LK
from orchestrator.rsi_r1 import observations as O
from orchestrator.rsi_r1 import protocol as PT
from orchestrator.rsi_r1 import replay as RP
from orchestrator.rsi_r1 import state as S

REPORT_SCHEMA = "simpraxis.rsi_r1.replay_report.v1"
SCHEMA_VERSION = 1

#: study → 允许开跑的最低 root（复跑发生在其 own study 收官之后）。
REPLAY_GATE: "Mapping[str, str]" = {"A": S.ROOT_STUDY_A, "B": S.ROOT_STUDY_B}

STATUS_COMPLETE = "COMPLETE"      # comparison 已封存且 problems 为空
STATUS_MISMATCH = "MISMATCH"      # comparison 已封存但有逐 run 差异
STATUS_BLOCKED = "BLOCKED"        # 基础设施整批失败，无 comparison


class ReplayDriverError(RuntimeError):
    """复跑门禁/配置错误（不是科学结论）。``code`` 公开。"""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


def report_relpath(study: str) -> str:
    return f"replay/report_{study}.json"


def comparison_relpath(study: str) -> str:
    return f"replay/comparison_{study}.json"


def _check_gate(ctx: Any, study: str) -> "tuple[LK.LockDocument, dict[str, Any]]":
    if study not in S.STUDIES:
        raise ReplayDriverError("STUDY_UNKNOWN", repr(study))
    if ctx.exists(comparison_relpath(study)):
        raise ReplayDriverError(
            "REPLAY_ALREADY_RAN",
            f"{comparison_relpath(study)} 已存在：比对件只封一次（失败另开新 root）")
    lock, freeze_report = LK.require_frozen(ctx)
    if lock.envelope_id != ctx.envelope_id:
        raise ReplayDriverError(
            "LOCK_MISMATCH",
            f"LOCK.envelope={lock.envelope_id!r} != {ctx.envelope_id!r}")
    state = S.ResearchState(ctx)
    root = state.root_state()
    want = REPLAY_GATE[study]
    if S.ROOT_ORDER.index(root) < S.ROOT_ORDER.index(want):
        raise ReplayDriverError(
            "ROOT_GATE", f"study {study} 复跑需要 root≥{want}，当前 {root}")
    # 复跑检验的是**已收官**的确认批：判据与主张件必须在场。
    for rel in (f"judge/report_{study}.json", f"claim/submission_{study}.json"):
        if not ctx.exists(rel):
            raise ReplayDriverError(
                "STUDY_NOT_FINISHED", f"缺 {rel}：确认批尚未收官，不能复跑")
    return lock, freeze_report


def _load_locked_confirm(
        ctx: Any, study: str, lock: LK.LockDocument
) -> "tuple[C.ResearchPlan, E.ConfirmManifest, str]":
    """从落盘件重建 confirm 计划与 manifest，并逐项核对与冻结事实一致。"""
    plan_rel = f"confirm/plan_{study}.json"
    man_rel = f"confirm/manifest_{study}.json"
    for rel in (plan_rel, man_rel):
        if not ctx.exists(rel):
            raise ReplayDriverError("CONFIRM_MISSING", rel)
    plan = C.ResearchPlan.from_dict(ctx.read_json(plan_rel))
    C.validate_plan(plan)
    if plan.study_id != study:
        raise ReplayDriverError("STUDY_MISMATCH",
                                f"confirm 计划 study={plan.study_id!r} != {study!r}")
    if plan.phase != "confirm":
        raise ReplayDriverError("PLAN_PHASE_INVALID", repr(plan.phase))
    manifest = E.manifest_from_doc(ctx.read_json(man_rel))
    if manifest.study_id != study:
        raise ReplayDriverError("STUDY_MISMATCH",
                                f"manifest study={manifest.study_id!r} != {study!r}")
    if manifest.plan_sha256 != plan.sha256:
        raise ReplayDriverError(
            "PLAN_MISMATCH",
            f"manifest.plan_sha256={manifest.plan_sha256} != 计划 {plan.sha256}")
    confirm_lock = S.ResearchState(ctx).confirm_lock(study)
    if confirm_lock.plan_sha256 != plan.sha256:
        raise ReplayDriverError(
            "PLAN_MISMATCH",
            f"确认锁 plan_sha256={confirm_lock.plan_sha256} != 计划 {plan.sha256}")
    return plan, manifest, confirm_lock.token


def _load_originals(
        ctx: Any, study: str, token: str,
        specs: Mapping[str, E.ExperimentSpec]) -> "tuple[dict[str, dict[str, Any]], str]":
    """读回原件观测：先核 manifest 的逐件 sha/大小/步数，再取 record+meta。"""
    batch_id = A.confirm_batch_id(study, token)
    base = ctx.subdir("observations") / batch_id
    manifest_path = base / "manifest.json"
    if not manifest_path.is_file():
        raise ReplayDriverError("OBSERVATION_MISSING", str(manifest_path))
    manifest_doc = json.loads(manifest_path.read_text(encoding="utf-8"))
    O.verify_manifest(manifest_doc, base)
    ordered = [specs[rid] for rid in sorted(specs)]
    artifacts = A.collect_batch_artifacts(ctx.subdir("observations"), batch_id,
                                          ordered)
    roots = {str(artifacts[rid]["meta"].get("root") or "") for rid in artifacts}
    if len(roots) != 1:
        raise ReplayDriverError("ROOT_AMBIGUOUS", f"原件根不唯一：{sorted(roots)}")
    return artifacts, roots.pop()


def _read_attempts(ctx: Any, study: str) -> int:
    rel = report_relpath(study)
    if not ctx.exists(rel):
        return 0
    doc = ctx.read_json(rel) or {}
    try:
        return int(doc.get("attempt", 0) or 0)
    except (TypeError, ValueError):
        return 0


def run_replay(
        ctx: Any, *, study: str, runner: Any, image: str, batch_id: str,
        clock: Callable[[], str] = utc_now_iso_z,
        backend_factory: "Callable[[str], Any] | None" = None,
        max_steps: "int | None" = None,
) -> "dict[str, Any]":
    """复跑一个 study 的已冻结确认批并落 ``replay/report_<study>.json``。

    成功/失配都会**封存** ``replay/comparison_<study>.json``（科学产物只封一次）；
    基础设施整批失败只落 BLOCKED 报告、不封 comparison，修复后可在同一 root 重试。
    """
    if study not in S.STUDIES:
        raise ReplayDriverError("STUDY_UNKNOWN", repr(study))
    if not str(batch_id).strip():
        raise ReplayDriverError("BATCH_MISSING", "batch_id 不得为空")
    lock, freeze_report = _check_gate(ctx, study)
    root_before = S.ResearchState(ctx).root_state()
    plan, manifest, token = _load_locked_confirm(ctx, study, lock)
    specs = J.build_specs(plan, manifest)
    ordered = [specs[rid] for rid in sorted(specs)]
    originals, original_root = _load_originals(ctx, study, token, specs)

    budget = C.BudgetSpec.from_dict(ctx.read_json("contract/budget.json"))
    steps_cap = int(max_steps if max_steps is not None
                    else budget.max_steps)
    factory = backend_factory or (
        lambda name: BK.PhysicsBackend(
            observations_root=ctx.subdir("observations"),
            evidence_root=ctx.subdir("replay") / f"independent_{study}",
            work_root=ctx.subdir("trials") / f"replay-{study}",
            runner=runner, image=image, batch_id=name))

    _check_replay_root(ctx.subdir("observations"), batch_id)
    backend = factory(batch_id)
    RP.assert_roots_independent(original_root, str(backend.evidence_root))

    attempts_before = _read_attempts(ctx, study)
    started = clock()
    pump = PT.HeartbeatPump(ctx.subdir("state")).start()
    try:
        pump.set_phase(f"replay:study-{study}")
        book = E.ResourceBook(max_runs=len(ordered), max_steps=steps_cap)
        evidence = E.execute_batch(ordered, backend, book)
    finally:
        pump.stop()

    report: "dict[str, Any]" = {
        "schema": REPORT_SCHEMA, "schema_version": SCHEMA_VERSION,
        "study": study, "lane": None, "zero_paid_posts": 0,
        "envelope_id": ctx.envelope_id, "lock_sha256": lock.sha256,
        "lock_verified": bool(freeze_report.get("ok")),
        "image": image, "batch_id": batch_id,
        "confirm_batch_id": A.confirm_batch_id(study, token),
        "plan_sha256": plan.sha256, "confirm_manifest_sha256": manifest.sha256,
        "started_at_utc": started, "ended_at_utc": clock(),
        "attempt": attempts_before + 1,
        "root_before": root_before,
        "n_expected": len(ordered), "n_runs_used": book.used_runs,
        "steps_used": book.used_steps,
        "runs": [{"run_id": o.run_id, "condition_id": o.condition_id,
                  "block_id": o.block_id, "ok": bool(o.ok),
                  "stop_reason": o.stop_reason, "error_code": o.error_code}
                 for o in evidence.outcomes],
    }
    if evidence.blocked:
        report.update({"status": STATUS_BLOCKED, "blocked": True,
                       "problems": [], "comparison_relpath": None,
                       "root_after": root_before,
                       "notes": ("基础设施整批失败：不补样本、不封比对件；"
                                 "修复后可在同一 root 重试（attempt 递增）。")})
        ctx.write_json(report_relpath(study), report)
        return report

    manifest_out = backend.write_manifest()
    if manifest_out is None:
        raise ReplayDriverError("MANIFEST_MISSING", "复跑批没有观测 manifest")
    O.verify_manifest(manifest_out, ctx.subdir("observations") / batch_id)
    if int(manifest_out.get("count", -1)) != len(ordered):
        raise ReplayDriverError(
            "MANIFEST_COUNT",
            f"复跑 manifest count={manifest_out.get('count')} != {len(ordered)}")
    replays = A.collect_batch_artifacts(ctx.subdir("observations"), batch_id,
                                        ordered)
    comparison = RP.compare_study(plan=plan, specs=specs, originals=originals,
                                  replays=replays)
    CL.write_replay_comparison(ctx, study, comparison)
    runs = list(comparison.get("runs") or [])
    report.update({
        "status": STATUS_COMPLETE if comparison.get("ok") else STATUS_MISMATCH,
        "blocked": False,
        "replay_manifest_sha256": C.canonical_sha256(manifest_out),
        "n_replayed": comparison.get("n_replayed"),
        "comparison_ok": bool(comparison.get("ok")),
        "problems": list(comparison.get("problems") or []),
        "summary": {"runs": len(runs),
                    "ok": sum(1 for r in runs if r.get("ok")),
                    "failed": sum(1 for r in runs if not r.get("ok")),
                    "identical": sum(1 for r in runs
                                     if (r.get("independence") or {})
                                     .get("record_identical"))},
        "comparison_relpath": comparison_relpath(study),
        "notes": ("对比件已封存：ok 与否都是结论，不补跑、不改判；"
                  "独立性证据逐 run 附在 comparison.independence。"),
    })
    state = S.ResearchState(ctx)
    report["root_after"] = state.root_state()
    if all(ctx.exists(comparison_relpath(s)) for s in S.STUDIES):
        if state.root_state() == S.ROOT_STUDY_B:
            state.advance_root(
                S.ROOT_REPLAY,
                reason="study A/B 复跑比对件齐备（T8 收官）")
            report["root_after"] = state.root_state()
    ctx.write_json(report_relpath(study), report)
    return report


def _check_replay_root(observations_root: Any, batch_id: str) -> None:
    """复跑批次名不得与既有观测批次重名（新批次是新证据的前提）。"""
    target = observations_root / str(batch_id)
    if target.exists():
        raise ReplayDriverError(
            "BATCH_EXISTS", f"{target} 已存在：复跑必须使用新批次")


__all__ = [
    "REPLAY_GATE", "REPORT_SCHEMA", "SCHEMA_VERSION", "STATUS_BLOCKED",
    "STATUS_COMPLETE", "STATUS_MISMATCH", "ReplayDriverError",
    "comparison_relpath", "report_relpath", "run_replay",
]
