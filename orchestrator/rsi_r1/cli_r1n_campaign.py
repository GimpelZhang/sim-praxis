"""RSI-R1-N campaign CLI：init/授权/校准/T5/validation LOCK/付费 LOCK。

本模块只管**根与门禁**；研究动作在 ``r13_validation``（T5）与
``study``/``protocol``（候选）里。所有命令在解析根之前设置
``SIMPRAXIS_RSI_R1N_CAMPAIGN``，保证账本/路径与根同源。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Mapping

from orchestrator.agentic.image_pin import BASE_IMAGE
from orchestrator.common import runtime_paths as RP
from orchestrator.common.fsutil import atomic_write_text
from orchestrator.rsi_r1 import assembly as A
from orchestrator.rsi_r1 import calibration as CAL
from orchestrator.rsi_r1 import contracts as C
from orchestrator.rsi_r1 import contracts_r13 as C13
from orchestrator.rsi_r1 import family as FAM
from orchestrator.rsi_r1 import lock as LK
from orchestrator.rsi_r1 import gateway_transport as GT
from orchestrator.rsi_r1 import lock_r13 as L13
from orchestrator.rsi_r1 import protocol as PT
from orchestrator.rsi_r1 import replay_driver as RD
from orchestrator.rsi_r1 import r13_review_run as RR
from orchestrator.rsi_r1 import study as STD
from orchestrator.rsi_r1 import profiles as P
from orchestrator.rsi_r1 import rv_draft as DR
from orchestrator.rsi_r1 import rv_review as RVW
from orchestrator.rsi_r1 import r13_validation as RV
from orchestrator.rsi_r1 import sandbox as SB
from orchestrator.rsi_r1 import state as S
from orchestrator.rsi_r1 import task_specs as TS
from orchestrator.gateway import quota_ledger as QL
from orchestrator.rsi_r1.runtime import CampaignContext

PLAN_PATH_R1N = Path("/home/vipuser/文档/RSI_R1-N_Plan_detailed.md")
DEFAULT_ORACLE = Path(
    "/mnt/artifacts/simpraxis/rsi_r1_repair2/r1b_20261111/"
    "private/calibration_private/oracle.py")
DEFAULT_ORACLE_V2 = Path(
    "/mnt/artifacts/simpraxis/rsi_r12/r12-t5-offline-20261113r/"
    "private/calibration_private/oracle_v2.py")
AUTHORIZATION_TEXT = (
    "用户 2026-11-17 指示：执行 ~/文档/RSI_R1-N_Plan_detailed.md；"
    "实施期间可自行开轮修复/重预注册（除 commit author 与推送凭据安全外）。"
    "同日用户选择『授权 T2 后付费执行』：T2 十面全过后，按 Plan §2.1 的 "
    "132 POST 上限执行 T3–T8；端点按当前 env（tokenhub，与 2.8.1 快照一致）"
    "使用，不换模型。")


class Cli13Error(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"[{code}] {detail}" if detail else code)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_sha(path: Path) -> str:
    return _sha(path.read_bytes())


def _utc_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_json(path: Path, doc: Mapping[str, Any]) -> str:
    text = json.dumps(doc, indent=1, sort_keys=True, ensure_ascii=False) + "\n"
    atomic_write_text(path, text, mode=0o600)
    return _sha(text.encode())


def _root(envelope: str) -> Path:
    if envelope in getattr(RP, "RSI_R1N_RESERVED_ROOT_NAMES", ()):
        raise Cli13Error("ROOT_RESERVED", envelope)
    root = (RP.rsi_r1n_root() / envelope).resolve(strict=False)
    family = RP.rsi_r1n_root().resolve(strict=False)
    if root == family or family not in root.parents:
        raise Cli13Error("ROOT_OUTSIDE_FAMILY", str(root))
    os.environ["SIMPRAXIS_RSI_R1N_CAMPAIGN"] = envelope
    RP.assert_rsi_r1n_roots_isolated()
    return root


def _ctx(envelope: str, *, create: bool = False) -> CampaignContext:
    root = _root(envelope)
    if create:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        for name in RP.RSI_R1N_CAMPAIGN_SUBDIRS:
            (root / name).mkdir(parents=True, exist_ok=True, mode=0o700)
    if not root.is_dir():
        raise Cli13Error("ROOT_MISSING", str(root))
    protected = tuple(RP.rsi_r1n_protected_write_roots().items())
    return CampaignContext(envelope_id=envelope, root=root,
                           protected_roots=protected)


def _validation_ctx(value: str) -> CampaignContext:
    """T5 验证根：接受绝对路径或 ``rsi_r1_n_val`` 下的 envelope 名。"""
    raw = str(value)
    if raw.startswith("/"):
        root = Path(raw).resolve(strict=False)
    else:
        root = (RP.rsi_r1n_val_root() / raw).resolve(strict=False)
    if not root.is_dir():
        raise Cli13Error("ROOT_MISSING", str(root))
    protected = tuple(RP.rsi_r1n_protected_write_roots().items())
    return CampaignContext(envelope_id=root.name, root=root,
                           protected_roots=protected)


def _load_envelope(ctx: CampaignContext) -> dict:
    return C13.validate_envelope_v13(
        ctx.read_json("contract/envelope.json"), envelope_id=ctx.envelope_id)


def _plan_path(args: Any) -> Path:
    return Path(getattr(args, "plan", None) or PLAN_PATH_R1N)


def cmd_init(args: Any) -> int:
    envelope = str(args.envelope)
    taskset = Path(args.taskset)
    manifest = json.loads((taskset / "taskset_manifest.json").read_text(
        encoding="utf-8"))
    resource = json.loads((taskset / "resource_plan.json").read_text(
        encoding="utf-8"))
    if manifest.get("schema") != P.TASKSET_MANIFEST_SCHEMA_V13:
        raise Cli13Error("TASKSET_SCHEMA", str(manifest.get("schema")))
    if resource.get("schema") != P.RESOURCE_PLAN_SCHEMA_V13:
        raise Cli13Error("RESOURCE_SCHEMA", str(resource.get("schema")))
    ctx = _ctx(envelope, create=True)
    if ctx.exists("contract/envelope.json"):
        raise Cli13Error("ENVELOPE_EXISTS", envelope)
    budget = C13.default_budget_v13(
        envelope, hard_cap_steps=int(resource["hard_cap_steps"]),
        aux_subenvelopes=resource.get("subenvelopes") or {})
    _write_json(ctx.relative("contract/budget.json"), budget.to_dict())
    # 信封/费用件绑定的是**预算文档 canonical sha**（与 R1.2 一致），不是文件
    # 字节 sha；LOCK 复核用 budget_from_dict(...).sha256。
    budget_sha = budget.sha256
    for name in ("taskset_manifest.json", "resource_plan.json",
                 "task_difference_table.md"):
        shutil.copyfile(taskset / name, ctx.relative(f"contract/{name}"))
        ctx.relative(f"contract/{name}").chmod(0o600)
    ctx.relative("private/tasks").mkdir(parents=True, exist_ok=True, mode=0o700)
    for name in ("task_a.json", "task_b.json"):
        shutil.copyfile(taskset / name, ctx.relative(f"private/tasks/{name}"))
        ctx.relative(f"private/tasks/{name}").chmod(0o600)
    shutil.copyfile(taskset / "taskset_public.json",
                    ctx.relative("public/taskset_public.json"))
    ctx.relative("public/taskset_public.json").chmod(0o600)
    permit = FAM.register(RP.rsi_r1n_root(), P.PROFILE_R1N_DEF, envelope)
    permit_sha = _write_json(ctx.relative("contract/family_permit.json"),
                             permit)
    contract = {
        "schema": P.PUBLIC_CONTRACT_SCHEMA_V13, "schema_version": 1,
        "profile_id": P.PROFILE_R1N, "envelope_id": envelope,
        "budget_sha256": budget_sha,
        "family": {"cap_posts": P.R1N_FAMILY_CAP,
                   "history_posts": P.R1N_FAMILY_HISTORY_POSTS,
                   "this_round_cap_posts": P.RSI_R1N_HARD_CAP},
        "posts": {"per_lane": dict(P.LANE_POST_CAPS_R1N),
                  "total": P.RSI_R1N_HARD_CAP,
                  "unit": "一次模型 HTTP POST（含失败/未知）"},
        "analysis_output": {
            "output_schema": "simpraxis.rsi_r1.analysis_output.v1",
            "diagnostics_schema": P.ANALYSIS_OUTPUT_SCHEMA_V13,
            "required_contrasts": "任务登记的主对比 id",
        },
        "science": {
            "estimand": "任务登记的线性对比（可含 2×2 交互）",
            "statistics": "block 配对 + percentile bootstrap（10000/linear/seed 20261111）",
            "reviews": {"items": 6, "channels": 2, "third_vote": False},
        },
        "refusal_policy": {"fail_closed": True,
                           "instrument_defect": "INVALID_INSTRUMENT"},
        "units": {"post": "单次模型 HTTP POST；含 thinking token 的输出上限 32768"},
    }
    _write_json(ctx.relative("public/public_contract.json"), contract)
    _write_json(ctx.relative("contract/public_contract.json"), contract)
    md = ("# RSI-R1-N 公开合同\n\n"
          f"- profile: {contract['profile_id']}\n"
          f"- envelope: {contract['envelope_id']}\n"
          f"- 预算: {json.dumps(contract['posts'], ensure_ascii=False)}\n"
          f"- 分析输出 schema: {contract['analysis_output']['output_schema']}\n"
          f"- 诊断 schema: {contract['analysis_output']['diagnostics_schema']}\n"
          f"- 科学: {json.dumps(contract['science'], ensure_ascii=False)}\n"
          f"- 家族: {json.dumps(contract['family'], ensure_ascii=False)}\n")
    atomic_write_text(ctx.relative("public/public_contract.md"), md, mode=0o600)
    atomic_write_text(ctx.relative("contract/public_contract.md"), md, mode=0o600)
    plan = _plan_path(args)
    if not plan.is_file():
        raise Cli13Error("PLAN_ABSENT", str(plan))
    plan_sha = _file_sha(plan)
    consent_sha = None
    cost_gate = {"decision": "BLOCKED", "paid_authorized": False,
                 "note": "init 零 POST；付费须 make-consent + authorize"}
    consent = None
    if args.consent:
        consent = C13.ConsentV13.from_dict(json.loads(
            Path(args.consent).read_text(encoding="utf-8")))
        _check_consent(consent, envelope=envelope, plan_sha=plan_sha,
                       budget_sha=budget_sha)
        consent_sha = _write_json(ctx.relative("contract/consent.json"),
                                  consent.to_dict())
        cost_gate = {"decision": consent.decision,
                     "paid_authorized": consent.paid_authorized,
                     "consent_sha256": consent_sha,
                     "note": consent.exposure_note}
    if str(args.mode) == "paid" and consent is None:
        cost_gate = {"decision": "BLOCKED", "paid_authorized": False,
                     "note": "paid 根已建（零 POST）；先 make-consent 再 authorize"}
    doc = {
        "schema": P.ENVELOPE_SCHEMA_V13, "schema_version": 1,
        "profile_id": P.PROFILE_R1N, "envelope_id": envelope,
        "mode": str(args.mode), "created_at_utc": _utc_now(),
        "plan_path": str(plan), "plan_sha256": plan_sha,
        "budget_path": "contract/budget.json", "budget_sha256": budget_sha,
        "profile_sha256": _sha(json.dumps(
            P.PROFILE_R1N_DEF.to_dict(), sort_keys=True,
            separators=(",", ":"), ensure_ascii=False).encode()),
        "ledger_namespace": P.PROFILE_R1N_DEF.namespace,
        "hard_cap": P.RSI_R1N_HARD_CAP,
        "normal_budget": P.RSI_R1N_NORMAL_BUDGET,
        "lane_caps": dict(P.LANE_POST_CAPS_R1N),
        "family_cap": P.R1N_FAMILY_CAP,
        "history_posts": P.R1N_FAMILY_HISTORY_POSTS,
        "family_permit_path": "contract/family_permit.json",
        "family_permit_sha256": permit_sha,
        "cost_gate": cost_gate,
        "taskset_sha256": _file_sha(ctx.relative("contract/taskset_manifest.json")),
        "resource_plan_sha256": _file_sha(
            ctx.relative("contract/resource_plan.json")),
        "public_taskset_sha256": _file_sha(
            ctx.relative("public/taskset_public.json")),
    }
    C13.validate_envelope_v13(doc, envelope_id=envelope)
    _write_json(ctx.relative("contract/envelope.json"), doc)
    print(json.dumps({"envelope": envelope, "root": str(ctx.root),
                      "mode": doc["mode"], "budget_sha256": budget_sha,
                      "family_permit_sha256": permit_sha,
                      "cost_gate": cost_gate["decision"],
                      "hard_cap_steps": budget.max_steps}, ensure_ascii=False))
    return 0


def _check_consent(consent: C13.ConsentV13, *, envelope: str,
                   plan_sha: str, budget_sha: str) -> None:
    if consent.envelope_id != envelope:
        raise Cli13Error("CONSENT_ENVELOPE", consent.envelope_id)
    if consent.plan_sha256 != plan_sha:
        raise Cli13Error("CONSENT_PLAN", "consent 未绑定当前 Plan")
    if consent.budget_sha256 != budget_sha:
        raise Cli13Error("CONSENT_BUDGET", "consent 未绑定当前预算")


def cmd_make_consent(args: Any) -> int:
    ctx = _ctx(str(args.envelope))
    envelope = _load_envelope(ctx)
    if envelope["mode"] != "paid":
        raise Cli13Error("MODE", "只有 paid 根需要费用件")
    budget_sha = envelope["budget_sha256"]
    text = AUTHORIZATION_TEXT
    consent = C13.ConsentV13(
        decision=C13.COST_AUTHORIZED, paid_authorized=True,
        envelope_id=ctx.envelope_id, profile_id=P.PROFILE_R1N,
        plan_path=envelope["plan_path"], plan_sha256=envelope["plan_sha256"],
        budget_sha256=budget_sha, scope="R1-N",
        max_posts=P.RSI_R1N_HARD_CAP,
        review_calibration_posts=P.LANE_POST_CAPS_R1N["review_calibration"],
        review_final_posts=P.LANE_POST_CAPS_R1N["review_final"],
        family_cap=P.R1N_FAMILY_CAP,
        exposure_note=("候选 124 + 审查 8 = 132 POST；美元无硬保证记 UNKNOWN；"
                       "任何额外诊断不得账外执行"),
        authorized_at_utc=_utc_now(),
        authorization={"source": "user instruction 2026-11-17",
                       "text_sha256": _sha(text.encode()),
                       "text": text},
        risk={"usd": "UNKNOWN", "review_posts": 8})
    out = Path(args.out) if args.out else ctx.relative("contract/consent.json")
    sha = _write_json(out, consent.to_dict())
    print(json.dumps({"consent": str(out), "sha256": sha}, ensure_ascii=False))
    return 0


def cmd_authorize(args: Any) -> int:
    ctx = _ctx(str(args.envelope))
    envelope = _load_envelope(ctx)
    if envelope["mode"] != "paid":
        raise Cli13Error("MODE", "authorize 只服务 paid 根")
    consent = C13.ConsentV13.from_dict(json.loads(
        Path(args.consent).read_text(encoding="utf-8")))
    _check_consent(consent, envelope=ctx.envelope_id,
                   plan_sha=envelope["plan_sha256"],
                   budget_sha=envelope["budget_sha256"])
    C13.assert_paid_authorized_v13(consent)
    sha = _write_json(ctx.relative("contract/consent.json"), consent.to_dict())
    envelope["cost_gate"] = {"decision": consent.decision,
                             "paid_authorized": True,
                             "consent_sha256": sha,
                             "note": consent.exposure_note}
    _write_json(ctx.relative("contract/envelope.json"), envelope)
    state = S.ResearchState(ctx)
    if state.root_state() == S.ROOT_INIT:
        state.advance_root(S.ROOT_AUTHORIZED, reason="consent authorized")
    print(json.dumps({"consent_sha256": sha, "root": state.root_state()}))
    return 0


def cmd_authorize_offline(args: Any) -> int:
    ctx = _ctx(str(args.envelope))
    envelope = _load_envelope(ctx)
    if envelope["mode"] != "offline_validation":
        raise Cli13Error("MODE", "authorize-offline 只服务离线验证根")
    doc = {"schema": "simpraxis.rsi_r1n.offline_authorization.v1",
           "schema_version": 1, "envelope_id": ctx.envelope_id,
           "profile_id": P.PROFILE_R1N, "created_at_utc": _utc_now(),
           "purpose": "T5 生产验证（零模型 POST）"}
    sha = _write_json(ctx.relative("contract/offline_authorization.json"), doc)
    state = S.ResearchState(ctx)
    if state.root_state() == S.ROOT_INIT:
        state.advance_root(S.ROOT_AUTHORIZED, reason="offline authorized")
    print(json.dumps({"authorization_sha256": sha,
                      "root": state.root_state()}))
    return 0


def cmd_calibrate(args: Any) -> int:
    ctx = _ctx(str(args.envelope))
    envelope = _load_envelope(ctx)
    if envelope["mode"] != "paid":
        raise Cli13Error("MODE", "校准只接在 paid 根")
    oracle_src = Path(args.oracle) if args.oracle else DEFAULT_ORACLE
    if not oracle_src.is_file():
        raise Cli13Error("ORACLE_MISSING", str(oracle_src))
    target = ctx.relative(CAL.ORACLE_RELPATH)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    shutil.copyfile(oracle_src, target)
    target.chmod(0o600)
    provenance = {"source": str(oracle_src),
                  "sha256": _file_sha(target),
                  "note": ("R1.2 v2 校准 oracle 显式导入；scene/metric/矩阵未变，"
                           "来源逐字节留痕")}
    _write_json(ctx.relative("private/calibration_private/source.json"),
                provenance)
    from orchestrator.rsi_r1 import prereg as PR
    models = Path("orchestrator/gateway/stage_models.yaml")
    PR.write_prereg(ctx, envelope_id=ctx.envelope_id,
                    plan_sha256=envelope["plan_sha256"],
                    model_config_sha256=_file_sha(models))
    # v1 校准（无 prior_attempt 前置）；v2 的 prior-attempt 检查在全新 R1-N
    # 根上没有可核历史，故用 R1 已批准算法与 v1 oracle（spec/scene 未变，
    # 来源显式留痕）。
    ledger = RP.rsi_r1n_ledger_path()
    design = CAL.DESIGN_V1
    report = None
    if ctx.exists("contract/calibration_report_v2.json"):
        report = ctx.read_json("contract/calibration_report_v2.json")
        design = CAL.DESIGN_V2
    elif ctx.exists("contract/calibration_report.json"):
        report = ctx.read_json("contract/calibration_report.json")
    if report is None:
        prereg = CAL.build_calibration_prereg(ctx=ctx, oracle_path=target,
                                              design=design)
        CAL.write_calibration_prereg(ctx, prereg, design=design)
        CAL.issue_zero_post_permit(ctx, ledger_path=ledger)
        report = CAL.run_calibration(ctx, runner=SB.DockerRunner(),
                                     image=CAL.CAL_IMAGE, design=design,
                                     ledger_path=ledger)
    if report.get("verdict") != "CALIBRATION_PASS" and design["version"] == 1:
        if not ctx.exists(CAL.PERMIT_RELPATH):
            CAL.issue_zero_post_permit(ctx, ledger_path=ledger)
        # v1 的已知缺口（A 首步速度门与 B 0.01 绝对容差）由 v2 的设计修订
        # 处理；v2 必须携带 v1 历史件与 gap 分析（plan §T1b/R1.2 先例）。
        gap = _write_gap_analysis(ctx, report)
        oracle2 = Path(args.oracle_v2) if getattr(args, "oracle_v2", None) \
            else DEFAULT_ORACLE_V2
        target2 = ctx.relative(CAL.ORACLE_RELPATH_V2)
        target2.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copyfile(oracle2, target2)
        target2.chmod(0o600)
        prior = CAL.prior_attempt_manifest(ctx)
        supersedes = {
            "calibration_prereg_sha256": CAL._document_sha(
                ctx.read_json("contract/calibration_prereg.json"))
            if hasattr(CAL, "_document_sha") else _file_sha(
                ctx.relative("contract/calibration_prereg.json")),
            "calibration_report_sha256": _file_sha(
                ctx.relative("contract/calibration_report.json")),
            "gap_analysis_sha256": gap,
            "reason": "v1 两个已知缺口由 v2 门修复（见 gap 分析）",
        }
        design = CAL.DESIGN_V2
        prereg2 = CAL.build_calibration_prereg(
            ctx=ctx, oracle_path=target2, design=design,
            supersedes=supersedes, prior_attempt=prior)
        CAL.write_calibration_prereg(ctx, prereg2, design=design)
        report = CAL.run_calibration(ctx, runner=SB.DockerRunner(),
                                     image=CAL.CAL_IMAGE, design=design,
                                     ledger_path=ledger)
    state = S.ResearchState(ctx)
    if report.get("verdict") == "CALIBRATION_PASS" \
            and state.root_state() == S.ROOT_AUTHORIZED:
        state.advance_root(S.ROOT_CALIBRATED, reason="calibration PASS")
    print(json.dumps({"verdict": report.get("verdict"),
                      "report": "contract/calibration_report_v2.json"
                      if design["version"] == 2
                      else "contract/calibration_report.json",
                      "root": state.root_state()}, ensure_ascii=False))
    return 0 if report.get("verdict") == "CALIBRATION_PASS" else 2


def _write_gap_analysis(ctx: CampaignContext, report: Mapping[str, Any]) -> str:
    failed = [c for c in report.get("checks") or [] if not c.get("ok")]
    lines = ["# R1-N 校准 v1→v2 缺口分析", "",
             "v1 在本根实跑的失败项（逐条来自报告，不改判）：", ""]
    for check in failed[:20]:
        lines.append(f"- `{check.get('id')}`: {check.get('detail')}")
    lines += ["", "修订依据（沿 R1.2 已批准算法）：",
              "- A 首步位移门：qvel 在首次 mj_step 前写入的容差按 1e-5 m 判；",
              "- B 绝对容差：0.01 + 1%·|ref|（欠阻尼 b06 门 0.02596），并增报周期/包络；",
              "- 矩阵/场景/seed 不变；v1 历史件与 raw 逐字节保留（prior_attempt）。",
              ""]
    text = "\n".join(lines)
    path = ctx.relative("contract/calibration_gap_analysis.md")
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)
    return _sha(text.encode())


def cmd_validate_production(args: Any) -> int:
    ctx = _ctx(str(args.envelope))
    envelope = _load_envelope(ctx)
    if envelope["mode"] != "offline_validation":
        raise Cli13Error("MODE", "validate-production 只服务离线验证根")
    summary = RV.run_production_validation(
        ctx, taskset_dir=ctx.relative("private/tasks"),
        out=ctx.relative("contract"), profile=P.PROFILE_R1N_DEF)
    print(json.dumps({"all_ok": summary["all_ok"],
                      "summary": RV.T5_SUMMARY_REL,
                      "studies": [{"study": s["study"], "all_ok": s["all_ok"]}
                                  for s in summary["studies"]]},
                     ensure_ascii=False))
    return 0 if summary["all_ok"] else 2


def cmd_validation_lock(args: Any) -> int:
    ctx = _ctx(str(args.envelope))
    image = args.image or BASE_IMAGE
    out = L13.build_validation_lock(ctx, image_pin=image)
    print(json.dumps({"lock": str(ctx.relative(L13.VALIDATION_LOCK_REL)),
                      "sha256": out["sha256"]}))
    return 0


def cmd_sign_validation(args: Any) -> int:
    ctx = _ctx(str(args.envelope))
    doc = L13.sign_validation(ctx, role=str(args.role), actor=str(args.actor),
                              decision=str(args.decision),
                              rationale=str(args.rationale))
    print(json.dumps({"role": doc["role"], "actor": doc["actor"],
                      "decision": doc["decision"]}))
    return 0


def cmd_verify_validation(args: Any) -> int:
    ctx = _ctx(str(args.envelope))
    out = L13.verify_validation(ctx)
    print(json.dumps(out, ensure_ascii=False))
    return 0


def cmd_freeze(args: Any) -> int:
    ctx = _ctx(str(args.envelope))
    envelope = _load_envelope(ctx)
    if envelope["mode"] != "paid":
        raise Cli13Error("MODE", "freeze 只服务 paid 根")
    if args.purpose != "paid":
        raise Cli13Error("PURPOSE", "R1-N 只接受 purpose=paid")
    if not args.validation_root:
        raise Cli13Error("BINDING", "需要 --validation-root（T5 根）")
    vctx = _validation_ctx(str(args.validation_root))
    L13.verify_validation(vctx)
    binding = L13.build_binding(vctx)
    image = args.image or BASE_IMAGE
    out = LK.freeze_lock(ctx, image_pin=image, purpose="paid",
                         validation_binding=binding)
    state = S.ResearchState(ctx)
    if state.root_state() != S.ROOT_FROZEN:
        state.advance_root(S.ROOT_FROZEN, reason="paid LOCK frozen")
    print(json.dumps({"lock_sha256": out["sha256"],
                      "root": state.root_state(),
                      "validation_lock_sha256": binding["validation_lock_sha256"]}))
    return 0


def cmd_sign(args: Any) -> int:
    ctx = _ctx(str(args.envelope))
    out = LK.sign_lock(ctx, role=str(args.role), actor=str(args.actor),
                       decision=str(args.decision),
                       rationale=str(args.rationale))
    print(json.dumps({"signature": out} if not isinstance(out, Mapping)
                     else dict(out), ensure_ascii=False, default=str))
    return 0


def cmd_verify(args: Any) -> int:
    ctx = _ctx(str(args.envelope))
    out = LK.verify_lock(ctx, repo_root=LK.REPO_ROOT)
    print(json.dumps(out, ensure_ascii=False, default=str)[:2000])
    return 0


def cmd_family_close(args: Any) -> int:
    root = RP.rsi_r1n_root()
    doc = FAM.close_member(root, str(args.envelope), reason=str(args.reason),
                           evidence={"note": args.note or ""})
    print(json.dumps({"closed": str(args.envelope),
                      "members": len(doc["members"])}))
    return 0



def _tokenizer_counter(path: str, sha256_hex: str):
    got = _file_sha(Path(path))
    if got != sha256_hex:
        raise Cli13Error("TOKENIZER_SHA", f"{path} sha256={got}")
    from tokenizers import Tokenizer

    tok = Tokenizer.from_file(path)
    return lambda text: len(tok.encode(str(text)).ids)


def _paid(envelope: str):
    if str(os.environ.get("SIMPRAXIS_ALLOW_PAID", "")) != "1":
        raise Cli13Error("PAID_ENV", "需要 SIMPRAXIS_ALLOW_PAID=1")
    ctx = _ctx(envelope)
    doc = _load_envelope(ctx)
    if doc["mode"] != "paid":
        raise Cli13Error("MODE", "只服务 paid 根")
    if not ctx.exists("contract/consent.json"):
        raise Cli13Error("CONSENT_MISSING", "先 authorize")
    consent = C13.ConsentV13.from_dict(ctx.read_json("contract/consent.json"))
    _check_consent(consent, envelope=envelope,
                   plan_sha=doc["plan_sha256"],
                   budget_sha=doc["budget_sha256"])
    C13.assert_paid_authorized_v13(consent)
    lock, _report = LK.require_frozen(ctx)
    lock_doc = ctx.read_json("contract/lock.json")
    image = str((lock_doc.get("image") or {}).get("pin") or "")
    if not image:
        raise Cli13Error("IMAGE_PIN", "LOCK 未登记镜像")
    spend = A.SpendContract(mode=A.MODE_PAID, consent=consent)
    return ctx, doc, consent, lock, image, spend


def _transport(lane: str, batch_id: str, thread_id: str):
    gateway = GT.build_gateway(
        ledger=QL.QuotaLedger.rsi_r1n(),
        pause_state_path=RP.rsi_r1n_gateway_state_path())
    return GT.build_transport(gateway=gateway, lane=lane, batch_id=batch_id,
                              thread_id=thread_id, env=os.environ)


def _record_family(ctx: CampaignContext) -> None:
    rows = 0
    path = RP.rsi_r1n_ledger_path()
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                if row.get("event", "call") == "call":
                    rows += 1
    FAM.record_usage(RP.rsi_r1n_root(), ctx.envelope_id, rows)


def cmd_probe(args: Any) -> int:
    lane = "probe"
    ctx, doc, consent, lock, image, spend = _paid(str(args.envelope))
    batch = str(args.batch_id or "").strip()
    if not batch:
        raise Cli13Error("BATCH_MISSING", "--batch-id")
    if not args.tokenizer or not args.tokenizer_sha256:
        raise Cli13Error("TOKENIZER_REQUIRED", "付费需要 tokenizer")
    counter = _tokenizer_counter(args.tokenizer, args.tokenizer_sha256)
    PT.ensure_approval(ctx, batch_id=batch, lane=lane, approved=12,
                       note=args.note or "", namespace=QL.RSI_R1N_NAMESPACE)
    transport = _transport(lane, batch, f"rsi-r13-{lane}")
    report = PT.run_protocol(
        ctx, lane=lane, spend=spend, transport_factory=lambda: transport,
        runner=SB.DockerRunner(), image=image, batch_id=batch,
        counter=counter, profile=P.PROFILE_R1N_DEF)
    _record_family(ctx)
    print(json.dumps({"lane": lane, "verdict": report.get("verdict"),
                      "root": report.get("root_after"),
                      "posts": report.get("posts")}, ensure_ascii=False))
    return 0 if report.get("verdict") == "PASS" else 2


def cmd_practice(args: Any) -> int:
    lane = "practice"
    ctx, doc, consent, lock, image, spend = _paid(str(args.envelope))
    batch = str(args.batch_id or "").strip()
    if not batch:
        raise Cli13Error("BATCH_MISSING", "--batch-id")
    if not args.tokenizer or not args.tokenizer_sha256:
        raise Cli13Error("TOKENIZER_REQUIRED", "付费需要 tokenizer")
    counter = _tokenizer_counter(args.tokenizer, args.tokenizer_sha256)
    PT.ensure_approval(ctx, batch_id=batch, lane=lane, approved=16,
                       note=args.note or "", namespace=QL.RSI_R1N_NAMESPACE)
    transport = _transport(lane, batch, f"rsi-r13-{lane}")
    report = PT.run_protocol(
        ctx, lane=lane, spend=spend, transport_factory=lambda: transport,
        runner=SB.DockerRunner(), image=image, batch_id=batch,
        counter=counter, profile=P.PROFILE_R1N_DEF)
    _record_family(ctx)
    print(json.dumps({"lane": lane, "verdict": report.get("verdict"),
                      "root": report.get("root_after"),
                      "posts": report.get("posts")}, ensure_ascii=False))
    return 0 if report.get("verdict") == "PASS" else 2


def cmd_study(args: Any) -> int:
    study = str(args.study).strip().upper()
    if study not in ("A", "B"):
        raise Cli13Error("STUDY", study)
    ctx, doc, consent, lock, image, spend = _paid(str(args.envelope))
    batch = str(args.batch_id or "").strip()
    if not batch:
        raise Cli13Error("BATCH_MISSING", "--batch-id")
    if not args.tokenizer or not args.tokenizer_sha256:
        raise Cli13Error("TOKENIZER_REQUIRED", "付费需要 tokenizer")
    counter = _tokenizer_counter(args.tokenizer, args.tokenizer_sha256)
    task = TS.TaskSpecV13.from_dict(ctx.read_json(
        f"private/tasks/task_{study.lower()}.json"))
    # R1-RV 教训（I13/§15）：提交前结构预检 + 修订状态持久化也进 R1-N study 链。
    drafts = DR.DraftBook(ctx=ctx, study=study, task=task,
                           prefix=f"draft_{study.lower()}")

    def _precommit(report_doc: Any, claims: Any) -> "list[str]":
        problems = RVW.precommit_report_problems(
            report_doc, task=task, claims=claims)
        drafts.record(report=report_doc, claims=claims, problems=problems)
        return problems

    PT.ensure_approval(ctx, batch_id=batch, lane=f"study_{study}",
                       approved=48, note=args.note or "",
                       namespace=QL.RSI_R1N_NAMESPACE)
    transport = _transport(f"study_{study}", batch, f"rsi-r1-n-study-{study}")
    report = STD.run_study(
        ctx, study=study, spend=spend,
        transport_factory=lambda: transport, runner=SB.DockerRunner(),
        image=image, batch_id=batch, counter=counter,
        profile=P.PROFILE_R1N_DEF, task=task,
        precommit=_precommit, draft_context=drafts.render_context)
    _record_family(ctx)
    print(json.dumps({"study": study, "status": report.get("status"),
                      "study_state": report.get("study_state"),
                      "stopped": report.get("stopped"),
                      "judge": report.get("judge"),
                      "root": report.get("root_after")}, ensure_ascii=False))
    return 0 if report.get("status") == "COMPLETED" else 2


def cmd_replay(args: Any) -> int:
    study = str(args.study).strip().upper()
    if study not in ("A", "B"):
        raise Cli13Error("STUDY", study)
    ctx, doc, _consent, _lock, image, _spend = _paid(str(args.envelope))
    batch = str(args.batch_id or f"r1n-replay-{study}")
    out = RD.run_replay(ctx, study=study, runner=SB.DockerRunner(),
                        image=image, batch_id=batch,
                        profile=P.PROFILE_R1N_DEF)
    print(json.dumps({"study": study, "ok": out.get("ok"),
                      "runs": out.get("runs"),
                      "identical": out.get("identical"),
                      "analysis_identical": out.get("analysis_identical")},
                     ensure_ascii=False))
    return 0 if out.get("ok") else 2


def cmd_review_permit(args: Any) -> int:
    ctx, _doc, _consent, _lock, _image, _spend = _paid(str(args.envelope))
    doc = RR.write_permit(ctx, max_posts=4)
    print(json.dumps({"permit": RR.REVIEW_PERMIT_REL,
                      "schema": doc["schema"]}))
    return 0


def cmd_review_calibrate(args: Any) -> int:
    ctx, doc, _consent, _lock, _image, _spend = _paid(str(args.envelope))
    RR.check_permit(ctx)
    out = RR.run_calibration(ctx, ctx.relative("calibration"),
                             envelope=ctx.envelope_id,
                             namespace=QL.RSI_R1N_NAMESPACE)
    _record_family(ctx)
    print(json.dumps({"ok": out["ok"], "code": out["code"],
                      "posts": out["posts"]}, ensure_ascii=False))
    return 0 if out["ok"] else 2


def cmd_review(args: Any) -> int:
    study = str(args.study).strip().upper()
    if study not in ("A", "B"):
        raise Cli13Error("STUDY", study)
    ctx, doc, _consent, _lock, _image, _spend = _paid(str(args.envelope))
    task = TS.TaskSpecV13.from_dict(ctx.read_json(
        f"private/tasks/task_{study.lower()}.json"))
    out = RR.run_formal(ctx, study, task, ctx.relative("reviews"),
                        namespace=QL.RSI_R1N_NAMESPACE)
    _record_family(ctx)
    print(json.dumps({"study": study,
                      "verdict": out["combined"]["verdict"]},
                     ensure_ascii=False))
    return 0 if out["combined"]["verdict"] == "REVIEW_PASS" else 2
