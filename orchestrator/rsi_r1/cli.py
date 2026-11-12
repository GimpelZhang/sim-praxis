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
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping

from orchestrator.common.clock import utc_now_iso_z
from orchestrator.common.runtime_paths import (
    assert_rsi_r1_roots_isolated,
    rsi_r1_campaign_root,
    rsi_r1_gateway_state_path,
    rsi_r1_subdir,
)
from orchestrator.gateway import quota_ledger as QL
from orchestrator.rsi_r1 import assembly as ASM
from orchestrator.rsi_r1 import calibration as CAL
from orchestrator.rsi_r1 import closeout as CLOSEOUT
from orchestrator.rsi_r1 import closure as CLOSURE
from orchestrator.rsi_r1 import contracts as C
from orchestrator.rsi_r1 import gateway_transport as GT
from orchestrator.rsi_r1 import lock as LK
from orchestrator.rsi_r1 import observations as OBS
from orchestrator.rsi_r1 import prereg as PREREG
from orchestrator.rsi_r1 import profiles as PPROF
from orchestrator.rsi_r1 import protocol as PT
from orchestrator.rsi_r1 import repair as RPR
from orchestrator.rsi_r1 import replay_driver as RPD
from orchestrator.rsi_r1 import rubric as RUBRIC
from orchestrator.rsi_r1 import runtime
from orchestrator.rsi_r1 import sandbox as SANDBOX
from orchestrator.rsi_r1 import scenes as SCENES
from orchestrator.rsi_r1 import state as ST
from orchestrator.rsi_r1 import study as STD
from orchestrator.rsi_r1 import watchdog as W
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
        # 签署原件的路径：AUTHORIZED 前置条件要回读**源件字节**复核（副本是
        # 规范化重序列化，字节永不等于源件；两个 hash 各管一层，见校验函数）。
        "consent_document_path": str(consent_path),
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
# state：root 推进入口（T0/T1/T5 三段，操作者显式推进）
# ---------------------------------------------------------------------------
#
# 状态机本身只做「+1 向前」的结构守卫；**语义前置条件**在这里 fail-closed：
# AUTHORIZED 要付费授权与 consent 字节逐字节对得上，CALIBRATED 要校准报告
# 判决 PASS，FROZEN 要 ``verify_lock`` 全量复核（闭包/依赖/校准/双签）。
# PROBED 起不设操作者入口——那一步只能由协议链 PASS 或研究闭环本身推进。

#: 操作者可推进的三步（顺序即状态机顺序）。
OPERATOR_STATES: "tuple[str, ...]" = (
    ST.ROOT_AUTHORIZED, ST.ROOT_CALIBRATED, ST.ROOT_FROZEN)


def _check_authorized_preconditions(ctx: Any, env_doc: Mapping[str, Any]) -> None:
    """AUTHORIZED 前置条件：付费授权 + 决定书两层完整性。

    两个 hash 各管一层，不可互换（L-R1-07 同类「值 vs 字节」缺陷的修复）：

    * ``consent_sha256`` —— 决定书**值**的规范 hash。校验对象是 campaign 内的
      ``contract/consent.json``（init 时由 ``Consent.to_dict()`` 规范化重序列化写出，
      字节与源件不同），检测副本落盘后被改写/损坏。
    * ``consent_document_sha256`` —— 签署**源件**的原始字节 hash。校验对象是
      init 时登记的 ``consent_document_path``；源件在授权后被改写（哪怕只是
      加一个空格）都说明「这份授权不是当前这版文字」，必须阻断。
      旧 envelope 没有该路径字段时退回副本字节口径（历史兼容）。
    """
    if env_doc.get("paid_authorized") is not True:
        raise CliError("AUTHORIZED_PRECONDITION: envelope.paid_authorized 不是 true")
    if not ctx.exists("contract/consent.json"):
        raise CliError("AUTHORIZED_PRECONDITION: 缺 contract/consent.json")
    try:
        signed = C.Consent.from_dict(ctx.read_json("contract/consent.json"))
    except (C.ContractError, KeyError, TypeError) as exc:
        raise CliError(
            "AUTHORIZED_PRECONDITION: contract/consent.json 不是合法决定书："
            f"{exc}") from exc
    want_value = env_doc.get("consent_sha256")
    if want_value and signed.sha256 != want_value:
        raise CliError(
            "AUTHORIZED_PRECONDITION: consent 规范值 sha256="
            f"{signed.sha256} != envelope.consent_sha256={want_value}")
    want_doc = env_doc.get("consent_document_sha256")
    if not want_doc:
        return
    src = env_doc.get("consent_document_path")
    if src:
        path = Path(str(src))
        if not path.is_file():
            raise CliError(
                "AUTHORIZED_PRECONDITION: consent 原件不在，无法复核签署字节："
                f"{path}")
        got = hashlib.sha256(path.read_bytes()).hexdigest()
        what = f"consent 原件 {path}"
    else:
        got = hashlib.sha256(
            ctx.relative("contract/consent.json").read_bytes()).hexdigest()
        what = "contract/consent.json 字节（旧 envelope 无源件路径）"
    if got != want_doc:
        raise CliError(
            f"AUTHORIZED_PRECONDITION: {what} sha256={got} != "
            f"envelope.consent_document_sha256={want_doc}")


def _check_calibrated_preconditions(ctx: Any) -> None:
    for rel in LK.CALIBRATION_REPORTS:
        if ctx.exists(rel):
            doc = ctx.read_json(rel)
            if doc.get("verdict") == "CALIBRATION_PASS":
                return
            raise CliError(
                f"CALIBRATED_PRECONDITION: {rel} verdict={doc.get('verdict')!r}")
    raise CliError(
        "CALIBRATED_PRECONDITION: 缺校准报告（先跑 controls）")


def cmd_state(args: argparse.Namespace) -> int:
    """显式推进 root（仅 AUTHORIZED/CALIBRATED/FROZEN；幂等；前置条件 fail-closed）。"""
    envelope = C.require_safe_id(args.envelope, where="--envelope")
    ctx, env_doc = _load_envelope_contract(envelope)
    state = ST.ResearchState(ctx)
    cur = state.root_state()
    target = str(args.to).upper()
    if target not in OPERATOR_STATES:
        raise CliError(
            f"STATE_OPERATOR_SCOPE: 操作者只能推进 {list(OPERATOR_STATES)}，"
            f"收到 {target!r}")
    if target == cur:
        print(f"state ok  envelope={envelope}  root={cur}（已是目标，幂等）")
        return 0
    if target == ST.ROOT_AUTHORIZED:
        _check_authorized_preconditions(ctx, env_doc)
    elif target == ST.ROOT_CALIBRATED:
        _check_calibrated_preconditions(ctx)
    else:  # FROZEN
        verified = LK.verify_lock(ctx)
        print(f"  lock_sha256={verified['lock_sha256']}  "
              f"signatures={sorted(verified['signatures'])}")
    state.advance_root(target, reason=args.reason or f"操作者推进 → {target}")
    print(f"state ok  envelope={envelope}  {cur} → {target}")
    return 0


# ---------------------------------------------------------------------------
# watch：主机外部监护者（Plan §8.4 / AC24）
# ---------------------------------------------------------------------------
#
# 监护对象**显式**传：campaign 根、账本文件、暂停件。三者在启动时写进
# ``state/watchdog_operator.json``（0600），使「它到底在看谁」可复核。
# SIGINT/SIGTERM 都转成 KeyboardInterrupt，``Watcher.run`` 会据此写 teardown
# （不擅杀他方进程；只写事件与暂停件）。

def cmd_watch(args: argparse.Namespace) -> int:
    envelope = C.require_safe_id(args.envelope, where="--envelope")
    ctx, _ = _load_envelope_contract(envelope)
    state_dir = ctx.subdir("state")
    ledger = ctx.relative("quota/ledger.jsonl")
    pause = ctx.relative("state/pause.json")
    operator = {
        "schema": "simpraxis.rsi_r1.watchdog_operator.v1", "schema_version": 1,
        "envelope_id": envelope, "campaign_root": str(ctx.root),
        "state_dir": str(state_dir), "ledger_path": str(ledger),
        "pause_path": str(pause), "pid": os.getpid(),
        "started_at_utc": utc_now_iso_z()}
    ctx.write_json("state/watchdog_operator.json", operator)
    watcher = W.Watcher(W.WatchConfig(state_dir=state_dir, poll_s=args.poll))

    def _on_sigterm(signum, frame):  # noqa: ARG001 —— 签名由 signal 规定
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGTERM, _on_sigterm)
    try:
        teardown = watcher.run(max_ticks=args.max_ticks)
    finally:
        signal.signal(signal.SIGTERM, previous)
    print(f"watch teardown  reason={teardown.get('reason')}  "
          f"ticks={teardown.get('ticks')}  "
          f"last_state={teardown.get('last_state')}  "
          f"pause_preserved={teardown.get('pause_preserved')}")
    print(f"  operator={ctx.relative('state/watchdog_operator.json')}")
    return 0


# ---------------------------------------------------------------------------
# probe / practice（T6，AC28）：真实模型 POST + 真实容器
# ---------------------------------------------------------------------------

def _tokenizer_counter(path: str, sha256_hex: str):
    """本地 token 计数（只用于 20000 token 请求门；不参与任何科学判决）。"""
    raw = Path(path).read_bytes()
    got = hashlib.sha256(raw).hexdigest()
    if got != sha256_hex:
        raise CliError(f"tokenizer sha256 不符：{path} 实为 {got}")
    from tokenizers import Tokenizer  # 本地文件；不联网

    tok = Tokenizer.from_file(str(path))

    def count(text: str) -> int:
        return len(tok.encode(text, add_special_tokens=False).ids)

    return count


def _run_protocol_cmd(args: argparse.Namespace, lane: str) -> int:
    """probe/practice 的唯一 CLI 路径：批准件 → 付费闸 → 网关 → 链 → 报告。

    ``--repair`` 是 Plan §8.1 的仪器修复重做：协议动作与报告仍按 ``lane``
    （probe/practice），但**全部实呼记 ``repair_reserve``**（task 4，帽 24），
    且需一次性授权件（``cli repair-authorize``）在场。批准件/账本/线程名
    一律跟随记账 lane，协议 gate 仍按协议 lane 配对 root（probe→FROZEN、
    practice→PROBED）。
    """
    envelope = C.require_safe_id(args.envelope, where="--envelope")
    ctx, _ = _load_envelope_contract(envelope)
    billing = RPR.LANE if getattr(args, "repair", False) else lane
    if billing != lane:
        # 授权件缺失要在扣任何东西之前就拒绝（fail-closed，不写批准件）。
        RPR.require_repair_gate(ctx, root=ST.ResearchState(ctx).root_state(),
                                protocol=lane)
    assert_rsi_r1_roots_isolated()
    # 环境闸先于一切副作用（含批准件写入与 tokenizer 加载）：没有
    # SIMPRAXIS_ALLOW_PAID=1 就不动作，也不引入任何配置依赖。
    GT.require_paid_env(os.environ)
    if not args.tokenizer or not args.tokenizer_sha256:
        raise CliError("付费运行需要 --tokenizer 与 --tokenizer-sha256")
    counter = _tokenizer_counter(args.tokenizer, args.tokenizer_sha256)
    consent = C.Consent.from_dict(ctx.read_json("contract/consent.json"))
    spend = ASM.SpendContract(mode=ASM.MODE_PAID, consent=consent)
    posts = RPR.POSTS_BY_PROTOCOL[lane] if billing != lane else (
        PT.PROBE_POSTS if lane == PT.PROBE else PT.PRACTICE_POSTS)
    approval = PT.ensure_approval(ctx, batch_id=args.batch_id, lane=billing,
                                 approved=posts, note=args.note or "")
    lock_doc = ctx.read_json(LK.LOCK_RELPATH)
    image = args.image or str((lock_doc.get("image") or {}).get("pin") or "")
    if not image:
        raise CliError("缺 --image 且 LOCK 未登记镜像 pin")
    gateway = GT.build_gateway(
        ledger=QL.QuotaLedger.rsi_r1(),
        pause_state_path=rsi_r1_gateway_state_path())
    default_thread = (f"rsi-r1-{lane}" if billing == lane
                      else f"rsi-r1-repair-{lane}")
    transport = GT.build_transport(
        gateway=gateway, lane=billing, batch_id=args.batch_id,
        thread_id=args.thread_id or default_thread, env=os.environ)
    report = PT.run_protocol(
        ctx, lane=lane, spend=spend, transport_factory=lambda: transport,
        runner=SANDBOX.DockerRunner(), image=image, batch_id=args.batch_id,
        counter=counter, billing_lane=billing)
    print(f"{lane} {report['verdict']}  attempt={report['attempt']}  "
          f"billing={report.get('billing_lane')}  "
          f"posts={report['posts']['this_attempt']}/"
          f"{report['posts']['allowed']}  root={report['root_after']}")
    if report["reason"]:
        print(f"  reason: {report['reason']}")
    for study in report["studies"]:
        print(f"  study {study['study']}: {study['ok_count']}/8 ok  "
              f"stopped={study['stopped'] or '-'}  "
              f"state={study['study_state']}  "
              f"judge={study['judge_verdict'] or '-'}")
    print(f"  approval={approval}")
    print(f"  report={ctx.relative(f'protocol/report_{lane}.json')}")
    return 0 if report["verdict"] == PT.VERDICT_PASS else 2


def cmd_probe(args: argparse.Namespace) -> int:
    return _run_protocol_cmd(args, PT.PROBE)


def cmd_practice(args: argparse.Namespace) -> int:
    return _run_protocol_cmd(args, PT.PRACTICE)


def cmd_study(args: argparse.Namespace) -> int:
    """T7 正式研究：单 study（A 48 / B 48），真实物理 + 真实 POST。

    与 probe/practice 同一条付费纪律：环境闸 → 批准件 → 网关 → 研究链 → 报告。
    study A 须 root=PRACTICED、study B 须 root=STUDY_A；同一 study 只跑一次
    （报告件在场即拒）。``INCOMPLETE`` 不推进 root（§11.2：STOP 不自动继续）。
    """
    envelope = C.require_safe_id(args.envelope, where="--envelope")
    ctx, _ = _load_envelope_contract(envelope)
    study = str(args.id).strip().upper()
    if study not in ST.STUDIES:
        raise CliError(f"--id 只能是 A 或 B，收到 {args.id!r}")
    assert_rsi_r1_roots_isolated()
    GT.require_paid_env(os.environ)
    if not args.tokenizer or not args.tokenizer_sha256:
        raise CliError("付费运行需要 --tokenizer 与 --tokenizer-sha256")
    counter = _tokenizer_counter(args.tokenizer, args.tokenizer_sha256)
    consent = C.Consent.from_dict(ctx.read_json("contract/consent.json"))
    spend = ASM.SpendContract(mode=ASM.MODE_PAID, consent=consent)
    lane = STD.STUDY_LANE[study]
    cap = STD.STUDY_POSTS[study]
    approval = PT.ensure_approval(ctx, batch_id=args.batch_id, lane=lane,
                                  approved=cap, note=args.note or "")
    lock_doc = ctx.read_json(LK.LOCK_RELPATH)
    image = args.image or str((lock_doc.get("image") or {}).get("pin") or "")
    if not image:
        raise CliError("缺 --image 且 LOCK 未登记镜像 pin")
    gateway = GT.build_gateway(
        ledger=QL.QuotaLedger.rsi_r1(),
        pause_state_path=rsi_r1_gateway_state_path())
    transport = GT.build_transport(
        gateway=gateway, lane=lane, batch_id=args.batch_id,
        thread_id=args.thread_id or f"rsi-r1-{lane}", env=os.environ)
    report = STD.run_study(
        ctx, study=study, spend=spend, transport_factory=lambda: transport,
        runner=SANDBOX.DockerRunner(), image=image, batch_id=args.batch_id,
        counter=counter)
    print(f"study {study} {report['status']}  state={report['study_state']}  "
          f"posts={report['posts']['this_attempt']}/"
          f"{report['posts']['allowed']}  root={report['root_after']}  "
          f"stopped={report['stopped'] or '-'}")
    print(f"  ok turns={report['ok_count']}  "
          f"judge={report['judge'].get('verdict') or '-'}  "
          f"claim={'present' if report['claim_submitted'] else 'missing'}")
    print(f"  approval={approval}")
    print(f"  report={ctx.relative(STD.report_relpath(study))}")
    return 0 if report["status"] == "COMPLETED" else 2


def cmd_replay(args: argparse.Namespace) -> int:
    """T8 独立复跑：零模型 POST、真容器；比对件只封一次。

    不建网关、不占任何 lane 呼量（账本恒不动），因此**不需要**付费环境闸与
    批准件；但会真跑容器，必须显式 ``--real-containers``。study A 需
    root≥STUDY_A、study B 需 root≥STUDY_B，且**目标 study** 的判据/主张件齐备。
    """
    envelope = C.require_safe_id(args.envelope, where="--envelope")
    ctx, env_doc = _load_envelope_contract(envelope)
    study = str(args.id).strip().upper()
    if study not in ST.STUDIES:
        raise CliError(f"--id 只能是 A 或 B，收到 {args.id!r}")
    if not args.real_containers:
        raise CliError("复跑会真跑容器：需显式 --real-containers（无模型 POST）")
    assert_rsi_r1_roots_isolated()
    lock_doc = ctx.read_json(LK.LOCK_RELPATH)
    image = args.image or str((lock_doc.get("image") or {}).get("pin") or "")
    if not image:
        raise CliError("缺 --image 且 LOCK 未登记镜像 pin")
    # R1.2 复跑还要执行冻结候选分析并生成新 receipt（Plan §5.1/AC53）；
    # legacy 根解析为 legacy profile，行为逐字节不变。
    profile = PPROF.resolve_profile(env_doc)
    report = RPD.run_replay(
        ctx, study=study, runner=SANDBOX.DockerRunner(), image=image,
        batch_id=args.batch_id, max_steps=args.max_steps, profile=profile)
    print(f"replay {study} {report['status']}  runs={report['n_runs_used']}/"
          f"{report['n_expected']}  root={report.get('root_after')}  "
          f"attempt={report['attempt']}")
    if report.get("summary"):
        s2 = report["summary"]
        print(f"  comparison ok={s2['ok']} failed={s2['failed']} "
              f"identical={s2['identical']}  problems={len(report['problems'])}")
    else:
        print("  blocked=infra：无比对件，修复后可在同一 root 重试")
    if report.get("analysis") is not None:
        print(f"  analysis ok={report['analysis_ok']}  "
              f"reasons={report['analysis'].get('reason_codes') or '-'}")
        print(f"  receipt={ctx.relative(report['analysis_receipt_relpath'])}")
    print(f"  report={ctx.relative(RPD.report_relpath(study))}"
          + (f"  comparison={ctx.relative(RPD.comparison_relpath(study))}"
             if report.get("comparison_relpath") else ""))
    return 0 if report["status"] in (RPD.STATUS_COMPLETE, RPD.STATUS_MISMATCH) else 2


def cmd_repair_authorize(args: argparse.Namespace) -> int:
    """写 Plan §8.1 的一次性修复授权（新 root INIT 位）+ 旧消耗逐字节搬入。"""
    envelope = C.require_safe_id(args.envelope, where="--envelope")
    ctx, _ = _load_envelope_contract(envelope)
    decision_path = Path(args.decision_file).expanduser()
    if not decision_path.is_file():
        raise CliError(f"裁决正文不存在：{decision_path}")
    evidence: "dict[str, str]" = {}
    for spec in args.evidence or []:
        name, sep, path = str(spec).partition("=")
        if not sep or not name.strip() or not path.strip():
            raise CliError(f"--evidence 需 NAME=PATH，收到 {spec!r}")
        if name in evidence:
            raise CliError(f"--evidence 重名：{name!r}")
        evidence[name] = path.strip()
    doc = RPR.authorize(
        ctx, prior_root=args.prior_root,
        decision_text=decision_path.read_text(encoding="utf-8"),
        evidence=evidence, note=args.note or "")
    print(f"repair-authorize ok  envelope={envelope}  "
          f"lane={doc['lane']} task={doc['lane_task_id']} cap={doc['cap_posts']}")
    print(f"  prior_root : {doc['prior_root']}")
    print(f"  prior_calls: {doc['prior_calls']}  by_task={doc['prior_calls_by_task']}")
    print(f"  decision   : sha256={doc['decision_text_sha256'][:16]}…")
    print(f"  evidence   : {sorted(doc['evidence'])}")
    print(f"  authorization={ctx.relative(RPR.AUTH_RELPATH)}")
    print(f"  carryover   ={ctx.relative(RPR.CARRYOVER_RELPATH)}")
    return 0


# ---------------------------------------------------------------------------
# closeout（T9，离线）
# ---------------------------------------------------------------------------

def cmd_closeout(args: argparse.Namespace) -> int:
    """T9 离线结案：只读盘面写 readout/failure_map/r2_readiness/包与交付件。

    AC35 要求「无 env/授权/LOCK/部分 batch 可出 missing 报告」：给 ``--root``
    时按显式根装配上下文（不读 env），否则沿用 export 的 campaign 根。全程
    **不触网、不调用模型、不启动容器**；``docker ps -a`` 只读探针失败不阻断。
    """
    envelope, ctx = _closeout_ctx(args)
    image_pin = None
    if ctx.exists(LK.LOCK_RELPATH):
        image_pin = ((ctx.read_json(LK.LOCK_RELPATH) or {}).get("image") or {}).get("pin")
    readout = CLOSEOUT.build(ctx, image_pin=image_pin, notes=list(args.note or []))
    axes = readout["axes"]
    print(f"closeout ok  envelope={envelope}  root={readout['campaign_root']}")
    print(f"  axes: process={axes['process']} measurement={axes['measurement']} "
          f"study_execution={axes['study_execution']} claim={axes['claim']} "
          f"r1={axes['r1']}")
    print(f"  posts used={readout['ledger']['calls']} "
          f"(ok={readout['ledger']['ok']} failed={readout['ledger']['failed']}) "
          f"hard_cap={readout['budget']['hard_cap']}")
    print(f"  document_sha256={readout['document_sha256']}")
    if readout["process_reasons"]:
        print("  process_reasons:")
        for reason in readout["process_reasons"]:
            print(f"    - {reason}")
    print(f"  readout={ctx.relative(CLOSEOUT.READOUT_RELPATH)}")
    return 0


# ---------------------------------------------------------------------------
# pack / judge（T9 离线复验）
# ---------------------------------------------------------------------------

def _envelope_from_root(root: Path) -> str:
    """从盘面自证 envelope_id：``contract/envelope.json`` 优先，退回 ``scope.json``。

    Plan §10 的离线结案命令只给 ``--root``（不含 ``--envelope``）：结案时入参
    不能成为「知道 genesis id」的前提，否则 AC35 在 env/票据缺失时仍会被卡住。
    """
    for rel in ("contract/envelope.json", "contract/scope.json"):
        path = root / rel
        if not path.is_file():
            continue
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise C.ContractError(f"{rel} 不可读：{exc}") from None
        value = doc.get("envelope_id") if isinstance(doc, dict) else None
        if isinstance(value, str) and value:
            return value
    raise C.ContractError(
        f"无法从 {root}/contract/ 推断 envelope_id（envelope.json/scope.json 均缺）")


def _closeout_ctx(args: argparse.Namespace):
    root = getattr(args, "root", None)
    if root:
        root_path = Path(root).expanduser()
        envelope = args.envelope or _envelope_from_root(root_path)
        envelope = C.require_safe_id(envelope, where="--envelope")
        return envelope, runtime.adopt_context(envelope, root_path)
    if not args.envelope:
        raise C.ContractError("未给 --root 时必须显式给 --envelope（无法从 env 外推断根）")
    envelope = C.require_safe_id(args.envelope, where="--envelope")
    return envelope, runtime.load_context(envelope)


def cmd_pack(args: argparse.Namespace) -> int:
    """把产物逐件复制进 ``evidence_pack/`` 并写逐件 sha256 的 manifest（离线）。"""
    envelope, ctx = _closeout_ctx(args)
    dest = Path(args.dest).expanduser() if args.dest else None
    manifest = CLOSURE.build_evidence_pack(ctx, dest=dest,
                                           repo_root=args.repo_root)
    print(f"pack ok  envelope={envelope}  n_files={manifest['n_files']}  "
          f"total_bytes={manifest['total_bytes']}")
    print(f"  pack={dest or ctx.relative('evidence_pack')}")
    return 0


def cmd_judge(args: argparse.Namespace) -> int:
    """跑独立复算器（第二遍由外部进程执行），可选同时核对证据包。"""
    envelope, ctx = _closeout_ctx(args)
    pack = None if args.verify_pack else False
    judged = CLOSURE.run_closure_judge(
        ctx, repo_root=args.repo_root, pack=pack,
        report=Path(args.report).expanduser() if args.report else None)
    print(f"judge {judged.get('verdict')}  rc={judged.get('returncode')}  "
          f"envelope={envelope}")
    print(f"  problems={len(judged.get('problems') or [])}")
    for problem in (judged.get("problems") or [])[:10]:
        print(f"    - {problem}")
    print(f"  report={judged.get('report_path')}")
    return 0 if judged.get("ok") else 1


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

def _run_r12(argv: "list[str]") -> int:
    """``r12`` 子命令转发；延迟 import 避免旧路径加载新模块。"""
    from orchestrator.rsi_r1 import cli_r12

    return cli_r12.main(list(argv))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m orchestrator.rsi_r1.cli",
        description="RSI-R1 研究闭环 CLI（T0：init/check；其余命令随后续 Task 接线）")
    sub = p.add_subparsers(dest="cmd", required=True)

    # RSI-R1.2（profile rsi-r1.2；账 2904）：转发到独立 CLI，旧行为逐字节不变。
    p12 = sub.add_parser("r12", help="RSI-R1.2 子命令（init/check/status/contract）")
    p12.add_argument("r12_args", nargs=argparse.REMAINDER,
                     help="原样转发给 orchestrator.rsi_r1.cli_r12")
    p12.set_defaults(func=lambda a: _run_r12(a.r12_args))

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

    pst = sub.add_parser(
        "state",
        help="显式推进 root（仅 AUTHORIZED/CALIBRATED/FROZEN；前置条件 fail-closed）")
    pst.add_argument("--envelope", required=True)
    pst.add_argument("--to", required=True,
                     choices=[s.lower() for s in OPERATOR_STATES] + list(OPERATOR_STATES),
                     help="目标 root 状态：authorized|calibrated|frozen")
    pst.add_argument("--reason", default=None, help="推进理由（写入 append-only 日志）")
    pst.set_defaults(func=cmd_state)

    pwt = sub.add_parser(
        "watch", help="主机外部监护者（AC24）：每 tick 读心跳，静默/死亡即暂停 dispatch")
    pwt.add_argument("--envelope", required=True)
    pwt.add_argument("--poll", type=float, default=1.0, help="poll 间隔秒（默认 1.0）")
    pwt.add_argument("--max-ticks", type=int, default=None,
                     help="只跑 N 个 tick（默认无限，SIGINT/SIGTERM 收尾）")
    pwt.set_defaults(func=cmd_watch)

    for name, func, cap in (("probe", cmd_probe, PT.PROBE_POSTS),
                            ("practice", cmd_practice, PT.PRACTICE_POSTS)):
        pp2 = sub.add_parser(
            name, help=f"T6 {name} 协议链（{cap} 次真实 POST + 真实容器）")
        pp2.add_argument("--envelope", required=True)
        pp2.add_argument("--batch-id", required=True,
                         help="本批账本 batch_id（批准件同名，0600 落 quota/approvals/）")
        pp2.add_argument("--image", default=None,
                         help="容器镜像 name@sha256:<64>（缺省取 LOCK 登记值）")
        pp2.add_argument("--tokenizer", default=None,
                         help="本地 tokenizer.json 路径（20000 token 请求门）")
        pp2.add_argument("--tokenizer-sha256", default=None,
                         help="tokenizer.json 的 sha256（逐字节钉死）")
        pp2.add_argument("--thread-id", default="",
                         help="账本 thread_id（默认 rsi-r1-<lane>）")
        pp2.add_argument("--note", default=None, help="批准件备注")
        pp2.add_argument("--repair", action="store_true",
                         help="Plan §8.1 仪器修复重做：实呼全记 repair_reserve"
                              "（需先 repair-authorize；协议报告仍按本 lane）")
        pp2.set_defaults(func=func)

    psty = sub.add_parser(
        "study", help="T7 正式研究（A/B 各 48 次真实 POST + 真实容器）")
    psty.add_argument("--envelope", required=True)
    psty.add_argument("--id", required=True, choices=["A", "B", "a", "b"],
                      help="研究编号：A（接触/摩擦）或 B（控制采样/积分）")
    psty.add_argument("--batch-id", required=True,
                      help="本批账本 batch_id（批准件同名，0600 落 quota/approvals/）")
    psty.add_argument("--image", default=None,
                      help="容器镜像 name@sha256:<64>（缺省取 LOCK 登记值）")
    psty.add_argument("--tokenizer", default=None,
                      help="本地 tokenizer.json 路径（20000 token 请求门）")
    psty.add_argument("--tokenizer-sha256", default=None,
                      help="tokenizer.json 的 sha256（逐字节钉死）")
    psty.add_argument("--thread-id", default="",
                      help="账本 thread_id（默认 rsi-r1-study_<A|B>）")
    psty.add_argument("--note", default=None, help="批准件备注")
    psty.set_defaults(func=cmd_study)

    prp = sub.add_parser(
        "replay",
        help="T8 独立复跑（A/B 各一次；零模型 POST + 真容器；比对件只封一次）")
    prp.add_argument("--envelope", required=True)
    prp.add_argument("--id", required=True, choices=["A", "B", "a", "b"],
                     help="研究编号：A 或 B（须该 study 已收官）")
    prp.add_argument("--batch-id", required=True,
                     help="复跑观测批次名（新批次；不得与既有批次重名）")
    prp.add_argument("--image", default=None,
                     help="容器镜像 name@sha256:<64>（缺省取 LOCK 登记值）")
    prp.add_argument("--max-steps", type=int, default=None,
                     help="整批物理步数帽（缺省取 contract/budget.json）")
    prp.add_argument("--real-containers", action="store_true",
                     help="确认真跑容器（无模型 POST、零付费）")
    prp.set_defaults(func=cmd_replay)

    pra = sub.add_parser(
        "repair-authorize",
        help="Plan §8.1 一次性修复授权（新 root 的 INIT 位；旧消耗逐字节搬入）")
    pra.add_argument("--envelope", required=True)
    pra.add_argument("--prior-root", required=True,
                     help="旧 campaign root（只读；必须同 envelope、无正式 study 实呼）")
    pra.add_argument("--decision-file", required=True,
                     help="用户授权裁决正文（UTF-8；逐字节 sha 入授权件）")
    pra.add_argument("--evidence", action="append", default=[], metavar="NAME=PATH",
                     help="缺陷证据件（可多次；逐件 sha256/bytes 入册）")
    pra.add_argument("--note", default=None, help="授权件备注")
    pra.set_defaults(func=cmd_repair_authorize)

    pco = sub.add_parser(
        "closeout",
        help="T9 离线结案（AC34–36）：readout/failure_map/R2 提案/包，不触网不跑容器")
    pco.add_argument("--envelope", default=None,
                       help="campaign id（给 --root 时可缺省，从 contract/ 推断）")
    pco.add_argument("--root", default=None,
                     help="campaign 根（缺省读 export；AC35 离线结案时显式给出，免 env）")
    pco.add_argument("--note", action="append", default=[],
                     help="写入 readout.notes 的备注（可多次）")
    pco.set_defaults(func=cmd_closeout)

    ppk = sub.add_parser(
        "pack", help="T9：把产物复制进 evidence_pack/ 并写逐件 sha256 manifest（离线）")
    ppk.add_argument("--envelope", default=None,
                       help="campaign id（给 --root 时可缺省，从 contract/ 推断）")
    ppk.add_argument("--root", default=None, help="campaign 根（缺省读 export）")
    ppk.add_argument("--dest", default=None, help="包目录（缺省 <root>/evidence_pack）")
    ppk.add_argument("--repo-root", default=None, help="复算器所在仓库根")
    ppk.set_defaults(func=cmd_pack)

    pjd = sub.add_parser(
        "judge", help="T9：跑独立复算器（外部进程），可选核对证据包")
    pjd.add_argument("--envelope", default=None,
                       help="campaign id（给 --root 时可缺省，从 contract/ 推断）")
    pjd.add_argument("--root", default=None, help="campaign 根（缺省读 export）")
    pjd.add_argument("--verify-pack", action="store_true",
                     help="同时按 manifest 核对 <root>/evidence_pack")
    pjd.add_argument("--report", default=None,
                     help="判决件输出路径（缺省 <root>/judge/closure_judge.json）")
    pjd.add_argument("--repo-root", default=None, help="复算器所在仓库根")
    pjd.set_defaults(func=cmd_judge)

    return p


def main(argv: "list[str] | None" = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (CliError, C.ContractError, PREREG.PreregError,
            SCENES.SceneError, OBS.ObservationError,
            CAL.CalibrationError, LK.LockError, PT.ProtocolError,
            RPR.RepairError, CLOSEOUT.CloseoutError, CLOSURE.ClosureError,
            STD.StudyError, RPD.ReplayDriverError, runtime.CampaignError,
            GT.TransportError, ASM.AssemblyError, QL.QuotaError,
            ST.StateError) as exc:
        print(f"BLOCKED {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
