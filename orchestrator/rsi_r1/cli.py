"""RSI-R1 CLI：``init`` / ``check``（T0）+ 后续阶段命令。

Plan §10 的命令面（拟建）：

    python -m orchestrator.rsi_r1.cli init   --envelope <id> --consent <file>
    python -m orchestrator.rsi_r1.cli check  --envelope <id>
    python -m orchestrator.rsi_r1.cli controls --root <root> --real-containers
    python -m orchestrator.rsi_r1.cli freeze  --root <root>
    python -m orchestrator.rsi_r1.cli probe   --root <root>
    python -m orchestrator.rsi_r1.cli practice --root <root>
    python -m orchestrator.rsi_r1.cli study   --root <root> --id A|B
    python -m orchestrator.rsi_r1.cli replay  --root <root> --out <independent_root>
    python -m orchestrator.rsi_r1.cli closeout --root <root>

T0 只实现 ``init`` / ``check``：它们**不触网、不调用模型、不跑仿真**，
只写本 campaign 的新根（0700/0600），并把单真值预算、授权决定、模型配置
hash、依赖索引落成可复核的件。

``init`` 的 fail-closed 纪律：
  * 目标 campaign 已存在 ``contract/scope.json`` → 拒绝（不静默覆盖）；
  * consent 文档 schema/scope/plan-sha 任一不符 → 拒绝（旧豁免不承继）；
  * 预算与账本命名空间任一项不等 → 拒绝（AC02）；
  * 未给 ``--envelope`` 或 envelope id 不安全 → 拒绝。
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping

from orchestrator.common.clock import utc_now_iso_z
from orchestrator.common.runtime_paths import (
    assert_rsi_r1_roots_isolated,
    rsi_r1_campaign_root,
    rsi_r1_subdir,
)
from orchestrator.gateway import quota_ledger as QL
from orchestrator.rsi_r1 import calibration as CAL
from orchestrator.rsi_r1 import contracts as C
from orchestrator.rsi_r1 import lock as LK
from orchestrator.rsi_r1 import observations as OBS
from orchestrator.rsi_r1 import prereg as PREREG
from orchestrator.rsi_r1 import rubric as RUBRIC
from orchestrator.rsi_r1 import runtime
from orchestrator.rsi_r1 import sandbox as SANDBOX
from orchestrator.rsi_r1 import scenes as SCENES
from orchestrator.rsi_r1.lanes import LANE_ORDER, LANE_POST_CAPS

#: Plan §1.1「已核实的源码接缝（只读10文件）」+ T0 要求定位的装配点。
SEAM_FILES: "tuple[str, ...]" = (
    "orchestrator/rsi_r02/candidate.py",
    "orchestrator/rsi_r02/cli.py",
    "orchestrator/rsi_r02/messages.py",
    "orchestrator/rsi_r02/billing.py",
    "orchestrator/rsi_r02/state.py",
    "orchestrator/rsi_r02/stages.py",
    "orchestrator/rsi_r02/task_adapter.py",
    "orchestrator/rsi_r01/trial.py",
    "orchestrator/rsi_r01/analysis_runner.py",
    "orchestrator/rsi_r01/observations.py",
    "orchestrator/gateway/quota_ledger.py",
    "orchestrator/common/runtime_paths.py",
    "orchestrator/common/watched.py",
    "orchestrator/rsi_r02/lock.py",
    "orchestrator/rsi_r01/lock.py",
)

REPO_ROOT = Path(__file__).resolve().parents[2]
PLAN_PATH = Path.home() / "文档" / "RSI_R1_Plan_detailed.md"
MODEL_CONFIG = REPO_ROOT / "orchestrator" / "gateway" / "stage_models.yaml"

SCOPE_SCHEMA = f"{C.SCHEMA_PREFIX}.scope.v1"
ENVELOPE_SCHEMA = f"{C.SCHEMA_PREFIX}.envelope.v1"
DEPINDEX_SCHEMA = f"{C.SCHEMA_PREFIX}.dependency_index.v1"


class CliError(RuntimeError):
    """CLI 前置条件不满足（fail-closed，不写半成品）。"""


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def _sha256_file(path: "Path | str") -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _git_head() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(REPO_ROOT),
                             capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise CliError(f"无法读取 git HEAD：{exc}") from exc
    return out.stdout.strip()


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _repo_relative(path: Path) -> str:
    """仓内路径写成相对形式；仓外（测试/别处）原样保留绝对路径。"""
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


# ---------------------------------------------------------------------------
# 依赖索引（C1：文件→符号→行号区间，一条命令落盘）
# ---------------------------------------------------------------------------

def build_dependency_index(repo_root: Path = REPO_ROOT) -> "dict[str, Any]":
    """用 AST 生成接缝文件的符号索引（纯静态，不 import、不执行）。"""
    files: "list[dict[str, Any]]" = []
    for rel in SEAM_FILES:
        p = repo_root / rel
        if not p.exists():
            files.append({"path": rel, "exists": False, "symbols": [],
                          "sha256": None, "bytes": None})
            continue
        src = p.read_text(encoding="utf-8")
        tree = ast.parse(src, filename=rel)
        symbols: "list[dict[str, Any]]" = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                symbols.append({"kind": "function", "name": node.name,
                                "lineno": node.lineno,
                                "end_lineno": getattr(node, "end_lineno", node.lineno)})
            elif isinstance(node, ast.ClassDef):
                methods = [
                    {"kind": "method", "name": f"{node.name}.{m.name}",
                     "lineno": m.lineno,
                     "end_lineno": getattr(m, "end_lineno", m.lineno)}
                    for m in node.body
                    if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
                ]
                symbols.append({"kind": "class", "name": node.name,
                                "lineno": node.lineno,
                                "end_lineno": getattr(node, "end_lineno", node.lineno),
                                "methods": methods})
        files.append({"path": rel, "exists": True, "sha256": _sha256_file(p),
                      "bytes": len(src.encode("utf-8")), "symbols": symbols})
    return {
        "schema": DEPINDEX_SCHEMA,
        "schema_version": C.SCHEMA_VERSION,
        "generated_at_utc": utc_now_iso_z(),
        "generator": "orchestrator.rsi_r1.cli.build_dependency_index (ast, 不执行)",
        "files": files,
    }


# ---------------------------------------------------------------------------
# 账户暴露：旧 root H 的 UNKNOWN 必须可见（Plan §8.3；不改旧账）
# ---------------------------------------------------------------------------

#: 旧 root H（R0.2 campaign r02_20261110T1218Z）第七个 D-dev POST 撞 120s
#: 硬墙钟 → UNKNOWN_SENT。逐字取自该 campaign 的 ``quota/pause.json`` 与
#: ledger seq=26；R1 只**读取**该事实，不修改任何旧行。
OLD_ROOT_H_UNKNOWN: "dict[str, Any]" = {
    "campaign": "r02_20261110T1218Z",
    "campaign_root": "/mnt/artifacts/simpraxis/rsi_r02/r02_20261110T1218Z",
    "ledger_seq": 26,
    "ts_iso": "2026-11-10T13:16:32Z",
    "call_id": "D-dev::D-dev-07",
    "event": "call",
    "ok": False,
    "err": ("GatewayError: InternalServerError 重试 1 次仍失败："
            "AnthropicException - stage=coding_debug 单次尝试硬墙钟 120s 到点"),
    "request_id": None,
    "status": "UNKNOWN_SENT",
    "reserved_usd": "1.5",
    "resolution": None,
    "source": "pause.json.reason + ledger seq=26（只读）",
}


def _account_exposure(envelope_id: str) -> "dict[str, Any]":
    return C.account_exposure_doc(
        envelope_id=envelope_id,
        pending_unknown=[OLD_ROOT_H_UNKNOWN],
        note=("旧 root H 的 UNKNOWN_SENT 仍计入同账户暴露，R1 不删除/不改写旧行；"
              "仅在拿到原始渠道可核验记录后由旧 campaign 追加 resolution。"
              "旧 R0.2 的费用豁免不承继到 R1（Plan §8.3）。"),
    )


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------

def cmd_init(args: argparse.Namespace) -> int:
    envelope = C.require_safe_id(args.envelope, where="--envelope")
    assert_rsi_r1_roots_isolated()
    ctx = runtime.create_context(envelope)
    campaign_root = ctx.root

    if ctx.exists("contract/scope.json") and not args.force:
        raise CliError(f"campaign 已初始化（{campaign_root}/contract/scope.json 存在）；"
                       "拒绝静默覆盖（--force 仅供审计重写）")

    # --- 1. consent：显式决定书 + 绑定当前 Plan 字节 ---
    consent_path = Path(args.consent).expanduser()
    if not consent_path.is_file():
        raise CliError(f"consent 文件不存在：{consent_path}")
    consent_raw = _load_json(consent_path)
    consent = C.Consent.from_dict(consent_raw)
    if consent.envelope_id != envelope:
        raise CliError(
            f"consent.envelope_id={consent.envelope_id!r} != --envelope {envelope!r}")
    plan_sha = _sha256_file(PLAN_PATH)
    C.verify_consent_binds_plan(consent, plan_path=str(PLAN_PATH), plan_sha256=plan_sha)
    # 决定书一旦落盘就不允许被静默改写：先把原字节存证。
    consent_bytes = consent_path.read_bytes()
    consent_doc_sha = hashlib.sha256(consent_bytes).hexdigest()

    # --- 2. 预算单真值 + AC02 对账 ---
    budget = C.default_budget(envelope)
    C.assert_single_truth(
        budget,
        ledger_task_caps=QL.RSI_R1_TASK_CAPS,
        ledger_hard_cap=QL.RSI_R1_HARD_CAP,
        ledger_normal_budget=QL.RSI_R1_NORMAL_BUDGET,
    )
    C.assert_run_budget_consistent()
    slots = budget.slots()
    if len(slots) != QL.RSI_R1_HARD_CAP:
        raise CliError(f"槽表 {len(slots)} != 硬帽 {QL.RSI_R1_HARD_CAP}")

    model_sha = _sha256_file(MODEL_CONFIG) if MODEL_CONFIG.exists() else None
    head = _git_head()
    now = utc_now_iso_z()

    # --- 3. 落件（全部 0600 / 目录 0700；每次写都过 CampaignContext 守卫）---
    ctx.write_json("contract/consent.json", consent.to_dict())
    ctx.write_json("contract/budget.json", budget.to_dict())
    ctx.write_json("contract/slots.json", {
        "schema": f"{C.SCHEMA_PREFIX}.slots.v1",
        "schema_version": C.SCHEMA_VERSION,
        "envelope_id": envelope,
        "budget_sha256": budget.sha256,
        "total": len(slots),
        "normal": budget.normal_posts,
        "reserve": budget.posts_by_lane["repair_reserve"],
        "slots": slots,
    })
    ctx.write_json("contract/refusals.json", {
        "schema": f"{C.SCHEMA_PREFIX}.refusals.v1",
        "schema_version": C.SCHEMA_VERSION,
        "clauses": dict(C.REFUSAL_CLAUSES),
    })
    ctx.write_json("contract/account_exposure.json", _account_exposure(envelope))
    ctx.write_json("contract/dependency_index.json", build_dependency_index())

    envelope_doc = {
        "schema": ENVELOPE_SCHEMA,
        "schema_version": C.SCHEMA_VERSION,
        "envelope_id": envelope,
        "campaign_root": str(campaign_root),
        "created_at_utc": now,
        "git_head": head,
        "plan_path": str(PLAN_PATH),
        "plan_sha256": plan_sha,
        "model_config_path": _repo_relative(MODEL_CONFIG),
        "model_config_sha256": model_sha,
        "consent_sha256": consent.sha256,
        "consent_document_sha256": consent_doc_sha,
        "cost_decision": consent.decision,
        "paid_authorized": consent.paid_authorized,
        "budget_sha256": budget.sha256,
        "ledger_namespace": QL.LEDGER_RSI_R1,
        "hard_cap": QL.RSI_R1_HARD_CAP,
        "normal_budget": QL.RSI_R1_NORMAL_BUDGET,
        "lane_caps": {lane: LANE_POST_CAPS[lane] for lane in LANE_ORDER},
        "old_waiver_inherited": False,
    }
    ctx.write_json("contract/envelope.json", envelope_doc)

    scope_doc = {
        "schema": SCOPE_SCHEMA,
        "schema_version": C.SCHEMA_VERSION,
        "envelope_id": envelope,
        "root_state": "INIT",
        "research_scope": {
            "studies": {
                "A": "接触/摩擦作用条件（含透明校准阴性控制 + 独立灵敏度控制）",
                "B": "无接触单关节跟踪：控制采样 Tc 与积分步长 dt 的耦合（开放问题，无预定方向）",
            },
            "claim_if_passed": "两案例的最小自主研究可行性（MINIMAL_RESEARCH_LOOP_ESTABLISHED）",
            "explicitly_not_claimed": [
                "普遍科研能力", "技能迁移", "RSI", "R2 已就绪",
            ],
            "no_model_change": True,
            "no_new_platform": True,
        },
        "cost": {
            "decision": consent.decision,
            "paid_authorized": consent.paid_authorized,
            "decision_verbatim": consent.verbatim,
            "old_r02_waiver_inherited": False,
            "max_usd": str(budget.max_usd),
            "hard_cap_posts": QL.RSI_R1_HARD_CAP,
            "normal_budget_posts": QL.RSI_R1_NORMAL_BUDGET,
            "note": ("Plan §8.3：Plan 批准只批准设计，不自动豁免费用门；"
                     "本文件的 decision 字段是唯一的付费放行依据"),
        },
        "account_exposure_pending_unknown": 1,
        "model_config_sha256": model_sha,
        "git_head": head,
        "artifacts": {
            "budget": "contract/budget.json",
            "slots": "contract/slots.json",
            "consent": "contract/consent.json",
            "envelope": "contract/envelope.json",
            "dependency_index": "contract/dependency_index.json",
            "account_exposure": "contract/account_exposure.json",
            "refusals": "contract/refusals.json",
        },
        "created_at_utc": now,
    }
    ctx.write_json("contract/scope.json", scope_doc)

    print(f"init ok  envelope={envelope}  campaign={campaign_root}")
    print(f"  budget  : {budget.total_posts} slots "
          f"(normal {budget.normal_posts} + reserve {budget.posts_by_lane['repair_reserve']})"
          f"  sha={budget.sha256[:16]}…")
    print(f"  consent : {consent.decision}  paid_authorized={consent.paid_authorized}")
    print(f"  plan    : {plan_sha[:16]}…  model_cfg={str(model_sha)[:16]}…")
    print(f"  deps    : {len(SEAM_FILES)} 个接缝文件已索引")
    return 0


# ---------------------------------------------------------------------------
# prereg（T1a）：仪器预注册必须先于任何真实校准 raw
# ---------------------------------------------------------------------------

def _load_envelope_contract(envelope: str):
    ctx = runtime.load_context(envelope)
    env_doc = ctx.read_json("contract/envelope.json")
    if env_doc.get("envelope_id") != envelope:
        raise CliError("envelope.json 与 --envelope 不符")
    return ctx, env_doc


def cmd_prereg(args: argparse.Namespace) -> int:
    envelope = C.require_safe_id(args.envelope, where="--envelope")
    ctx, env_doc = _load_envelope_contract(envelope)
    if not args.force:
        pass  # 默认幂等：已存在且来源未变则复用（见 write_prereg）
    doc = PREREG.write_prereg(
        ctx, envelope_id=envelope,
        plan_sha256=env_doc["plan_sha256"],
        model_config_sha256=env_doc.get("model_config_sha256"))
    print(f"prereg ok  envelope={envelope}")
    print(f"  registered_at : {doc['registered_at_utc']}")
    print(f"  document_sha  : {doc['document_sha256'][:16]}…")
    print(f"  rubric_sha    : {doc['rubric_sha256'][:16]}…")
    print(f"  scenes        : {sorted(doc['scenes'])}")
    return 0


def cmd_contract(args: argparse.Namespace) -> int:
    envelope = C.require_safe_id(args.envelope, where="--envelope")
    ctx, _ = _load_envelope_contract(envelope)
    scene_ids = args.scenes or (SCENES.SCENE_A, SCENES.SCENE_B)
    for sid in scene_ids:
        SCENES.scene_doc(sid)  # 未知场景立即抛 SceneError
    doc = C.public_contract_document(scene_ids=tuple(scene_ids))
    C.assert_public_contract_complete(doc)
    text = C.render_public_contract(scene_ids=tuple(scene_ids))
    ctx.write_json("public/public_contract.json", doc)
    ctx.write_text("public/public_contract.md", text)
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    print(f"contract ok  envelope={envelope}  scenes={list(scene_ids)}")
    print(f"  bytes={len(text.encode('utf-8'))}  sha256={sha[:16]}…")
    print(f"  rubric_sha={RUBRIC.rubric_sha256()[:16]}…")
    return 0


# ---------------------------------------------------------------------------
# controls（T1b）：先预注册再沿正式 dispatcher 跑固定组合；零模型 POST
# ---------------------------------------------------------------------------

def cmd_controls(args: argparse.Namespace) -> int:
    envelope = C.require_safe_id(args.envelope, where="--envelope")
    ctx, _ = _load_envelope_contract(envelope)
    design = CAL.design_for(getattr(args, "design_version", 1))
    ledger = CAL._default_ledger(ctx)
    oracle_path = CAL._oracle_path(ctx, design)
    if args.prepare_only:
        supersedes = None
        prior = None
        if design["version"] >= 2:
            prior = CAL.prior_attempt_manifest(ctx)
            equivalence = CAL.assert_oracle_v2_equivalence(ctx)
            prior = {**prior, "oracle_equivalence": equivalence}
            v1_prereg = ctx.read_json(CAL.PREREG_RELPATH)
            v1_report = ctx.read_json(CAL.REPORT_RELPATH)
            supersedes = {
                "prereg_schema": CAL.PREREG_SCHEMA,
                "prereg_relpath": CAL.PREREG_RELPATH,
                "prereg_file_sha256": CAL.oracle_sha256(ctx.relative(CAL.PREREG_RELPATH)),
                "prereg_document_sha256": v1_prereg.get("document_sha256"),
                "report_relpath": CAL.REPORT_RELPATH,
                "report_file_sha256": CAL.oracle_sha256(ctx.relative(CAL.REPORT_RELPATH)),
                "report_verdict": v1_report.get("verdict"),
                "gap_relpath": CAL.GAP_RELPATH,
                "gap_file_sha256": CAL.oracle_sha256(ctx.relative(CAL.GAP_RELPATH)),
            }
        doc = CAL.build_calibration_prereg(ctx=ctx, oracle_path=oracle_path,
                                           design=design, supersedes=supersedes,
                                           prior_attempt=prior)
        path = CAL.write_calibration_prereg(ctx, doc, design)
        permit = CAL.issue_zero_post_permit(ctx, ledger_path=ledger)
        print(f"controls prepare ok  envelope={envelope} design=v{design['version']}")
        print(f"  prereg  : {path}")
        print(f"  sha256  : {doc['document_sha256'][:16]}…")
        print(f"  oracle  : {doc['oracle']['sha256'][:16]}…")
        if prior is not None:
            eq = prior["oracle_equivalence"]
            print(f"  prior   : files={prior['n_files']} raws={prior['n_raws']} "
                  f"files_sha={prior['files_sha256'][:16]}… "
                  f"raws_sha={prior['raws_sha256'][:16]}…")
            print(f"  oracle≡v1 max|ΔRMSE|={eq['max_abs_diff']!r}（8 block）")
        print(f"  permit  : {permit['kind']} posts={permit['model_posts_allowed']} "
              f"refusal={permit['sender_refusal']['code']}")
        print(f"  ledger  : {ledger} call_rows=0")
        return 0
    if not args.real_containers:
        print("BLOCKED 校准会真跑容器：需显式 --real-containers（无模型 POST）",
              file=sys.stderr)
        return 2
    report = CAL.run_calibration(
        ctx, runner=SANDBOX.DockerRunner(), image=args.image,
        ledger_path=ledger, design=design,
        notes=[f"T1b 真实校准（design=v{design['version']}）沿正式 dispatcher/"
               f"raw/资源账；零项目模型 POST"])
    print(f"controls envelope={envelope} design=v{report['schema_version']}  "
          f"verdict={report['verdict']}")
    print(f"  runs={len(report['runs'])} checks={len(report['checks'])} "
          f"failures={len(report['failures'])}")
    print(f"  budget runs={report['budget']['total_runs']} steps={report['budget']['total_steps']}")
    for c in report["failures"][:10]:
        print(f"  FAIL {c['id']}: {c['detail']}")
    return 0 if report["verdict"] == "CALIBRATION_PASS" else 2


# ---------------------------------------------------------------------------
# freeze（T5）：不可变闭包 LOCK + 双签（AC27）
# ---------------------------------------------------------------------------

def cmd_freeze(args: argparse.Namespace) -> int:
    """冻结八组闭包 + 镜像钉死 + 预注册/校准引用，随后按 ``--sign`` 逐个签名。

    签名格式 ``ROLE:ACTOR:DECISION[:RATIONALE]``，ROLE ∈ {reviewer, verifier}；
    两个角色必须不同 actor 且都 APPROVE，最后一次 ``verify_lock`` 才算冻结完成。
    """
    envelope = C.require_safe_id(args.envelope, where="--envelope")
    ctx, _ = _load_envelope_contract(envelope)
    repo_root = Path(args.repo_root).resolve() if args.repo_root else None
    frozen = LK.freeze_lock(ctx, repo_root=repo_root, image_pin=args.image)
    print(f"freeze ok  envelope={envelope}  lock_sha256={frozen['sha256']}")
    print(f"  closure_sha256={frozen['doc']['closure_sha256']}")
    print(f"  image={frozen['doc']['image']['pin']}")
    for spec in args.sign:
        parts = spec.split(":", 3)
        if len(parts) < 3:
            raise CliError(f"--sign 需 ROLE:ACTOR:DECISION[:RATIONALE]，收到 {spec!r}")
        role, actor, decision = parts[:3]
        rationale = parts[3] if len(parts) > 3 else ""
        doc = LK.sign_lock(ctx, role=role, actor=actor, decision=decision,
                           rationale=rationale)
        print(f"  signed role={doc['role']} actor={doc['actor']} "
              f"decision={doc['decision']}")
    verified = LK.verify_lock(ctx, repo_root=repo_root)
    print(f"verify ok  lock_sha256={verified['lock_sha256']}  "
          f"signatures={sorted(verified['signatures'])}")
    return 0


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------

def cmd_check(args: argparse.Namespace) -> int:
    envelope = C.require_safe_id(args.envelope, where="--envelope")
    assert_rsi_r1_roots_isolated()
    contract = rsi_r1_subdir("contract")

    problems: "list[str]" = []

    def load(name: str) -> Any:
        p = contract / name
        if not p.is_file():
            problems.append(f"缺件：{p}")
            return None
        return _load_json(p)

    scope = load("scope.json")
    envelope_doc = load("envelope.json")
    budget_doc = load("budget.json")
    slots_doc = load("slots.json")
    consent_doc = load("consent.json")
    deps = load("dependency_index.json")
    exposure = load("account_exposure.json")
    if problems:
        for p in problems:
            print(f"PROBLEM {p}")
        return 2

    # --- 预算 / 槽 / 账本单真值 ---
    try:
        budget = C.BudgetSpec.from_dict(budget_doc)
        C.assert_single_truth(
            budget,
            ledger_task_caps=QL.RSI_R1_TASK_CAPS,
            ledger_hard_cap=QL.RSI_R1_HARD_CAP,
            ledger_normal_budget=QL.RSI_R1_NORMAL_BUDGET,
        )
        C.assert_run_budget_consistent()
    except C.ContractError as exc:
        problems.append(f"AC02 预算：{exc}")
        budget = None
    if budget is not None:
        if envelope_doc["budget_sha256"] != budget.sha256:
            problems.append("envelope.budget_sha256 与 budget.json 不符")
        if slots_doc["total"] != budget.total_posts:
            problems.append(f"slots.total={slots_doc['total']} != {budget.total_posts}")
        if slots_doc["budget_sha256"] != budget.sha256:
            problems.append("slots.budget_sha256 与 budget.json 不符")
        if len(slots_doc["slots"]) != budget.total_posts:
            problems.append("slots 明细条数与 total 不符")

    # --- consent / 费用门 ---
    try:
        consent = C.Consent.from_dict(consent_doc)
        plan_sha = _sha256_file(PLAN_PATH)
        C.verify_consent_binds_plan(consent, plan_path=str(PLAN_PATH),
                                    plan_sha256=plan_sha)
        if envelope_doc["consent_sha256"] != consent.sha256:
            problems.append("envelope.consent_sha256 与 consent.json 不符")
        if envelope_doc["cost_decision"] != consent.decision:
            problems.append("envelope.cost_decision 与 consent 不符")
        if consent.decision == C.COST_DECISION_BLOCKED:
            problems.append("费用门 BLOCKED：付费 lane 不可启动（设计/校准仍可做）")
    except C.ContractError as exc:
        problems.append(f"AC01 授权：{exc}")

    # --- 模型配置未漂移 ---
    model_sha = _sha256_file(MODEL_CONFIG) if MODEL_CONFIG.exists() else None
    if envelope_doc["model_config_sha256"] != model_sha:
        problems.append(
            f"模型配置已变：envelope={envelope_doc['model_config_sha256']} "
            f"now={model_sha}（AC04：本阶段不允许改 stage_models.yaml）")

    # --- 仪器预注册（已注册时：量表/场景/指标不得漂移；AC06/AC09）---
    prereg_path = contract / "instrument_prereg.json"
    if prereg_path.is_file():
        try:
            PREREG.assert_prereg_matches_sources(_load_json(prereg_path))
        except PREREG.PreregError as exc:
            problems.append(f"AC06/09 预注册：{exc}")

    # --- 依赖索引可复核（重新生成并比对路径集合与 hash）---
    fresh = build_dependency_index()
    old_map = {f["path"]: f["sha256"] for f in deps["files"]}
    new_map = {f["path"]: f["sha256"] for f in fresh["files"]}
    drift = sorted(p for p in set(old_map) | set(new_map) if old_map.get(p) != new_map.get(p))
    if drift:
        problems.append(f"依赖索引漂移（接缝文件在本轮被改动）：{drift}")

    # --- 账户暴露 ---
    if not exposure["pending_unknown"]:
        problems.append("account_exposure 丢了旧 root H 的 UNKNOWN（§8.3 要求可见）")

    print(f"check envelope={envelope}  root={rsi_r1_campaign_root()}")
    if problems:
        for p in problems:
            print(f"PROBLEM {p}")
        print(f"RESULT FAIL problems={len(problems)}")
        return 2
    print(f"RESULT PASS  budget={budget.total_posts}slots "
          f"norm={budget.normal_posts} reserve={budget.posts_by_lane['repair_reserve']} "
          f"cost={consent.decision} deps={len(deps['files'])}")
    return 0


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m orchestrator.rsi_r1.cli",
        description="RSI-R1 研究闭环 CLI（T0：init/check；其余命令随后续 Task 接线）")
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("init", help="初始化 R1 campaign：预算/授权/依赖索引落件")
    pi.add_argument("--envelope", required=True, help="envelope id（同时作 campaign id）")
    pi.add_argument("--consent", required=True, help="费用门决定书 JSON 路径")
    pi.add_argument("--force", action="store_true",
                    help="已初始化时允许审计重写（默认拒绝）")
    pi.set_defaults(func=cmd_init)

    pc = sub.add_parser("check", help="复核 R1 campaign 的预算/授权/模型/依赖索引")
    pc.add_argument("--envelope", required=True)
    pc.set_defaults(func=cmd_check)

    pp = sub.add_parser("prereg", help="写/校验仪器预注册（必须先于真实校准）")
    pp.add_argument("--envelope", required=True)
    pp.add_argument("--force", action="store_true",
                    help="保留位：默认幂等复用；来源漂移时仍拒绝，须人工删旧件")
    pp.set_defaults(func=cmd_prereg)

    pk = sub.add_parser("contract", help="渲染公开研究合同到 public/")
    pk.add_argument("--envelope", required=True)
    pk.add_argument("--scenes", nargs="*", default=None,
                    help="场景 id 列表（默认两场景）")
    pk.set_defaults(func=cmd_contract)

    pc2 = sub.add_parser(
        "controls",
        help="T1b 校准：先写预注册 + 零 POST permit，再沿正式 dispatcher 跑固定组合")
    pc2.add_argument("--envelope", required=True)
    pc2.add_argument("--prepare-only", action="store_true",
                     help="只写 calibration_prereg.json 与零 POST permit（不跑容器）")
    pc2.add_argument("--real-containers", action="store_true",
                     help="显式确认真跑容器（仍不发任何模型 POST）")
    pc2.add_argument("--image", default=CAL.CAL_IMAGE,
                     help=f"容器镜像（默认钉死 {CAL.CAL_IMAGE[:32]}…）")
    pc2.add_argument("--design-version", type=int, choices=(1, 2), default=1,
                     help="校准设计版本：1=首轮（历史留档）、2=2026-11-11 批准的修正设计")
    pc2.set_defaults(func=cmd_controls)

    pf = sub.add_parser(
        "freeze",
        help="T5 冻结：八组闭包 + 镜像钉死 + 预注册/校准引用，可选双签后复核")
    pf.add_argument("--envelope", required=True)
    pf.add_argument("--image", default=None,
                    help="容器镜像 name@sha256:<64>（缺省取校准报告 container_image）")
    pf.add_argument("--sign", action="append", default=[],
                    metavar="ROLE:ACTOR:DECISION[:RATIONALE]",
                    help="冻结后立即签名（可重复；两个角色须不同 actor）")
    pf.add_argument("--repo-root", default=None,
                    help="闭包与依赖索引的仓库根（缺省为源码仓根）")
    pf.set_defaults(func=cmd_freeze)

    return p


def main(argv: "list[str] | None" = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (CliError, C.ContractError, PREREG.PreregError,
            SCENES.SceneError, OBS.ObservationError,
            CAL.CalibrationError, LK.LockError) as exc:
        print(f"BLOCKED {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
