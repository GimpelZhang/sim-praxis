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

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from orchestrator.common.clock import utc_now_iso_z
from orchestrator.common.fsutil import ensure_private_dir
from orchestrator.rsi_r1 import analysis_receipt as AR
from orchestrator.rsi_r1 import assembly as A
from orchestrator.rsi_r1 import backend as BK
from orchestrator.rsi_r1 import closure as CL
from orchestrator.rsi_r1 import contracts as C
from orchestrator.rsi_r1 import experiments as E
from orchestrator.rsi_r1 import judge as J
from orchestrator.rsi_r1 import lock as LK
from orchestrator.rsi_r1 import loop as L
from orchestrator.rsi_r1 import observations as O
from orchestrator.rsi_r1 import protocol as PT
from orchestrator.rsi_r1 import replay as RP
from orchestrator.rsi_r1 import sandbox as SB
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


# ---------------------------------------------------------------------------
# RSI-R1.2（Plan §5.1 / T4）：复跑时执行冻结候选分析并生成新 receipt
# ---------------------------------------------------------------------------

def _is_r12(profile: Any) -> bool:
    return profile is not None and bool(getattr(profile, "is_r12", False))


def analysis_receipt_relpath(study: str) -> str:
    """复跑 receipt：与 assembly 的 ``private/receipts/<study>-<phase>.json`` 同规。"""
    return f"private/receipts/{study}-replay.json"


def _write_private(path: Path, data: bytes) -> None:
    """0600 原子写（与 assembly 同纪律；receipt 目录候选不可写）。"""
    ensure_private_dir(path.parent)
    tmp = path.with_name(path.name + ".tmp")
    if tmp.exists():
        tmp.unlink()
    fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def _view_run_id(relpath: str) -> str:
    name = Path(str(relpath)).name
    if name.endswith(".meta.json"):
        return name[: -len(".meta.json")]
    if name.endswith(".jsonl"):
        return name[: -len(".jsonl")]
    return name


def _contrast_ids(plan: C.ResearchPlan) -> "list[str]":
    ids = [plan.primary.id]
    if plan.negative_control is not None:
        ids.append(plan.negative_control.id)
    return ids


def _artifact_binding(ctx: Any, source_sha: str) -> "dict[str, Any]":
    """从 journal 取该源码的**登记**事件（producer/reply/action 的唯一来源）。"""
    for ev in reversed(S.ResearchState(ctx).journal.events()):
        if ev.event == "artifact" \
                and ev.payload.get("source_sha256") == source_sha:
            return dict(ev.payload)
    raise ReplayDriverError("UNBOUND_SOURCE",
                            f"{source_sha[:12]} 未在 journal 登记")


def _analysis_source_text(ctx: Any, source_sha: str) -> str:
    """读冻结分析源码并重算字节 SHA：文件漂移即拒（不信任路径名）。"""
    if len(source_sha) != 64 or any(c not in "0123456789abcdef"
                                    for c in source_sha):
        raise ReplayDriverError("SOURCE_SHA_INVALID", repr(source_sha))
    path = ctx.relative(f"designs/{source_sha}.py")
    if not path.is_file():
        raise ReplayDriverError("SOURCE_FILE_MISSING", str(path))
    raw = path.read_bytes()
    actual = hashlib.sha256(raw).hexdigest()
    if actual != source_sha:
        raise ReplayDriverError(
            "SOURCE_FILE_DRIFT",
            f"登记 {source_sha[:12]} != 磁盘 {actual[:12]}")
    return raw.decode("utf-8")


def _replay_view_entries(ctx: Any, batch_id: str,
                         run_ids: Sequence[str]) -> "list[SB.ViewEntry]":
    """只从**本批复跑**观测目录取视图：旧 confirm raw 不得替代（AC53）。"""
    base = ctx.subdir("observations")
    entries: "list[SB.ViewEntry]" = []
    for run_id in run_ids:
        rel = f"{batch_id}/{run_id}.jsonl"
        path = base / rel
        if not path.is_file():
            raise ReplayDriverError("OBSERVATION_MISSING", rel)
        entries.append(SB.ViewEntry(rel,
                                    hashlib.sha256(path.read_bytes()).hexdigest()))
        meta_rel = f"{batch_id}/{run_id}.meta.json"
        meta = base / meta_rel
        if meta.is_file():
            entries.append(SB.ViewEntry(
                meta_rel, hashlib.sha256(meta.read_bytes()).hexdigest()))
    return entries


def _r12_replay_analysis(
        ctx: Any, *, study: str, plan: C.ResearchPlan,
        manifest: "E.ConfirmManifest", batch_id: str,
        artifacts: "Mapping[str, Mapping[str, Any]]",
        runner: Any, image: str, profile: Any) -> "dict[str, Any]":
    """在 fresh 复跑视图上执行冻结分析源码 → 新 receipt + 独立数值重算。

    输入只取本批复跑观测，输出只取本次容器字节；旧 output/旧 receipt/旧 raw
    一律不读。失败也落 receipt（invalid），由调用方计入 problems。
    """
    source_sha = str(plan.analysis_source_sha or "")
    if not source_sha:
        raise ReplayDriverError(
            "ANALYSIS_SOURCE_MISSING", "确认计划未绑定分析源码")
    text = _analysis_source_text(ctx, source_sha)
    run_ids = sorted(str(r) for r in artifacts)
    if not run_ids:
        raise ReplayDriverError("OBSERVATION_MISSING", "复跑批没有观测")
    entries = _replay_view_entries(ctx, batch_id, run_ids)
    got = {_view_run_id(e.relpath) for e in entries}
    if got != set(run_ids):  # 冗余校验：视图必须恰是完整 run 集合
        raise ReplayDriverError(
            "RUN_SET_MISMATCH", f"view={sorted(got)} expected={run_ids}")
    context = SB.AnalysisContext(
        phase="replay",
        max_runs=int(profile.resource_limits.replay_runs_per_study),
        allowed_run_ids=tuple(run_ids))
    try:
        ev = SB.run_analysis(
            text, entries, evidence_root=ctx.subdir("observations"),
            work_root=ctx.subdir("trials") / f"replay-analysis-{study}",
            runner=runner, image=image, retain_bytes=True,
            analysis_context=context,
            # 锁内适配（2026-11-12 T5 全量演练）：复跑视图必须与生产 analyze
            # 同构——同一份冻结分析源码按公开合同从视图读计划来配对，缺
            # plan-<sha12>.json 时合规脚本只能 ``expected exactly one plan``
            # 退出。宿主输入走 assembly 的唯一构造点，逐字节同源。
            extra_files=A.analysis_host_inputs(ctx, plan))
    except SB.SandboxError as exc:
        # 视图/作用域等**执行前**拒绝：不产生 receipt，直接是驱动错误。
        raise ReplayDriverError(exc.code, exc.detail) from exc
    payload = ev.blobs.get(A.ANALYSIS_OUTPUT)
    output_doc: "dict[str, Any] | None" = None
    judge_output: "dict[str, Any] | None" = None
    error = ""
    reasons: "list[str]" = []
    if ev.timed_out:
        reasons.append("ANALYSIS_TIMEOUT")
    if int(ev.exit_code) != 0:
        reasons.append("ANALYSIS_EXIT_NONZERO")
    if payload is None:
        reasons.append("ANALYSIS_OUTPUT_MISSING")
        error = "ANALYSIS_OUTPUT_MISSING"
    else:
        try:
            raw = json.loads(payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            reasons.append("ANALYSIS_OUTPUT_BAD_JSON")
            error = "ANALYSIS_OUTPUT_BAD_JSON"
        else:
            try:
                parsed = AR.parse_output(
                    raw, expected_run_ids=run_ids,
                    expected_contrasts=_contrast_ids(plan))
                output_doc = dict(raw)
                output_doc["contrasts"] = [parsed["contrasts"][cid]
                                           for cid in _contrast_ids(plan)]
                # receipt 存候选原始 schema（list 形态）；判决吃规范化
                # {cid: item}——claim_consistency/数值比较共用该形态。
                judge_output = {"run_ids": list(parsed["run_ids"]),
                                "contrasts": dict(parsed["contrasts"])}
            except AR.ReceiptError as exc:
                reasons.append(exc.code)
                error = exc.code
    output_bytes = payload or b""
    inputs = [{"run_id": _view_run_id(e.relpath),
               "relative_path": e.relpath, "sha256": e.sha256}
              for e in entries]
    art = _artifact_binding(ctx, source_sha)
    # 锁内适配（2026-11-12 T5 全量演练）：注册表词汇 ``candidate_model`` 不能
    # 直塞凭据 schema（只认 candidate/maintainer）。与 analyze/confirm 共用
    # ``assembly.receipt_producer`` 唯一映射；未知词汇 ⇒ 空串 ⇒ 凭据 fail-closed。
    producer = A.receipt_producer(art)
    reply_id = art.get("response_id")
    action_id = art.get("action_sha256")
    receipt = AR.build_receipt(
        profile_id=str(profile.profile_id), study_id=study, phase="replay",
        plan_sha256=plan.sha256, confirm_manifest_sha256=manifest.sha256,
        source_sha256=source_sha, registered_reply_id=reply_id,
        registered_action_id=action_id, producer=producer, inputs=inputs,
        execution_id=ev.container,
        sandbox_status=("failed" if reasons else "ok"),
        exit_code=int(ev.exit_code), timed_out=bool(ev.timed_out),
        output_bytes=output_bytes, output_doc=output_doc,
        validity=not reasons, reason_codes=reasons)
    rel = analysis_receipt_relpath(study)
    data = (json.dumps(receipt, ensure_ascii=False, sort_keys=True,
                       indent=1, allow_nan=False) + "\n").encode("utf-8")
    _write_private(ctx.relative(rel), data)
    expect = AR.ReceiptExpectation(
        study_id=study, phase="replay", plan_sha256=plan.sha256,
        source_sha256=source_sha, source_file_sha256=source_sha,
        producer=producer, registered_reply_id=reply_id,
        registered_action_id=action_id,
        confirm_manifest_sha256=manifest.sha256,
        allowed_run_ids=tuple(run_ids),
        expected_inputs=tuple(inputs), output_bytes=output_bytes,
        execution_id=ev.container, exit_code=int(ev.exit_code),
        timed_out=bool(ev.timed_out),
        profile_id=str(profile.profile_id))
    receipt_ok, receipt_reasons = AR.validate_receipt(receipt, expect)
    submission: "dict[str, Any] | None" = None
    sub_rel = f"claim/submission_{study}.json"
    if ctx.exists(sub_rel):
        sub = ctx.read_json(sub_rel)
        submission = dict(sub) if isinstance(sub, Mapping) else None
    report = J.judge_study_r12(
        plan=plan, manifest=manifest, artifacts=artifacts,
        submission=submission, receipt_ok=receipt_ok,
        receipt_reasons=receipt_reasons, output=judge_output,
        output_error=error)
    layers = report.get("layers") or {}
    analysis_ok = bool(receipt_ok and layers.get("numerics_valid")
                       and report.get("verdict") == "JUDGE_OK")
    return {
        "analysis_ok": analysis_ok,
        "receipt_relpath": rel,
        "receipt_sha256": AR.sha256_bytes(data),
        "receipt_valid": bool(receipt_ok),
        "receipt_reasons": list(receipt_reasons)[:8],
        "source_sha256": source_sha,
        "n_runs": len(run_ids),
        "execution_id": str(ev.container),
        "output_sha256": AR.sha256_bytes(output_bytes),
        "output_error": error,
        "judge_verdict": report.get("verdict"),
        "layers": layers,
        "reason_codes": list(report.get("reason_codes") or [])[:8],
    }


def _r12_reports_ok(ctx: Any, current_study: str,
                    current_analysis_ok: bool) -> bool:
    """两个 study 的 replay 报告都声明 analysis_ok 才算科学门齐备。"""
    if not current_analysis_ok:
        return False
    for other in S.STUDIES:
        if other == current_study:
            continue
        rel = report_relpath(other)
        if not ctx.exists(rel):
            return False
        if not bool((ctx.read_json(rel) or {}).get("analysis_ok")):
            return False
    return True



def run_replay(
        ctx: Any, *, study: str, runner: Any, image: str, batch_id: str,
        clock: Callable[[], str] = utc_now_iso_z,
        backend_factory: "Callable[[str], Any] | None" = None,
        max_steps: "int | None" = None,
        profile: Any = None,
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
    try:
        originals, original_root = _load_originals(ctx, study, token, specs)
    # 观测缺件是运维面错误（manifest 验签失败/记录缺失），统一成可机读的驱动码；
    # 否则同一条运维故障在 T8 表现为裸 traceback 而不是 BLOCKED 报告。
    except (L.LoopError, O.ObservationError, KeyError, ValueError) as exc:
        code = getattr(exc, "code", None) or type(exc).__name__
        detail = getattr(exc, "detail", None) or str(exc)
        raise ReplayDriverError(code, detail) from exc

    budget = C.budget_from_dict(ctx.read_json("contract/budget.json"))
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
    analysis: "dict[str, Any] | None" = None
    if _is_r12(profile):
        # R1.2：复跑的独立数值面 = 冻结候选分析在 fresh replay 视图上重跑
        # （新 receipt），不再借用确认期 output/raw（AC53）。
        analysis = _r12_replay_analysis(
            ctx, study=study, plan=plan, manifest=manifest, batch_id=batch_id,
            artifacts=replays, runner=runner, image=image, profile=profile)
    analysis_ok = analysis is None or bool(analysis.get("analysis_ok"))
    problems = list(comparison.get("problems") or [])
    if analysis is not None and not analysis_ok:
        problems.append("ANALYSIS_INVALID")
        problems.extend(str(c) for c in analysis.get("reason_codes") or [])
    runs = list(comparison.get("runs") or [])
    report.update({
        "status": (STATUS_COMPLETE
                   if bool(comparison.get("ok")) and analysis_ok
                   else STATUS_MISMATCH),
        "blocked": False,
        "replay_manifest_sha256": C.canonical_sha256(manifest_out),
        "n_replayed": comparison.get("n_replayed"),
        "comparison_ok": bool(comparison.get("ok")),
        "problems": problems,
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
    if analysis is not None:
        report.update({"analysis": analysis, "analysis_ok": analysis_ok,
                       "analysis_receipt_relpath": analysis["receipt_relpath"],
                       "analysis_receipt_sha256": analysis["receipt_sha256"]})
    state = S.ResearchState(ctx)
    report["root_after"] = state.root_state()
    if all(ctx.exists(comparison_relpath(s)) for s in S.STUDIES):
        ready = (not _is_r12(profile)
                 or _r12_reports_ok(ctx, study, analysis_ok))
        if ready and state.root_state() == S.ROOT_STUDY_B:
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
    "analysis_receipt_relpath", "comparison_relpath", "report_relpath",
    "run_replay",
]
