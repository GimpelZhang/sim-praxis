"""RSI-R1.2 CLI（T0 面）：init / check / contract / status。

入口：

    python -m orchestrator.rsi_r1.cli_r12 init    --envelope <id> [--mode ...] [--consent <file>]
    python -m orchestrator.rsi_r1.cli_r12 check   --root <root>
    python -m orchestrator.rsi_r1.cli_r12 contract --root <root> [--json]
    python -m orchestrator.rsi_r1.cli_r12 status  --root <root>

也可经旧入口转发：``python -m orchestrator.rsi_r1.cli r12 <同上参数>``。

T0 纪律：

* 不构造任何 sender、不触网、不读写 2904 之外任何账本；
* 根必须位于 ``$SIMPRAXIS_RSI_R12_ROOT``（缺省 ``$DATA_ROOT/rsi_r12``）之内，
  且与封存期根 / R0/R0.1/R0.2/R1 家族根互异互不嵌套（AC03）；
* init 只写 contract/、validation/family_permit.json、quota/ 目录结构；
  不写任何"成功"或"已验证"状态——那是 T5/T6 的事。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from orchestrator.common import runtime_paths as RP
from orchestrator.common.fsutil import atomic_write_text, ensure_private_dir
from orchestrator.gateway import quota_ledger as QL
from orchestrator.rsi_r1 import calibration as CAL
from orchestrator.rsi_r1 import contracts as C
from orchestrator.rsi_r1 import family as FAM
from orchestrator.rsi_r1 import human_events as HE
from orchestrator.rsi_r1 import profiles as P
from orchestrator.rsi_r1 import runtime as RT

PLAN_PATH_R12 = Path.home() / "文档" / "RSI_R1.2_Plan_detailed.md"

ENVELOPE_RELPATH = "contract/envelope.json"
BUDGET_RELPATH = "contract/budget.json"
CONSENT_RELPATH = "contract/consent.json"
CONTRACT_JSON_RELPATH = "contract/public_contract.json"
CONTRACT_MD_RELPATH = "contract/public_contract.md"
SCOPE_RELPATH = "contract/public_science_scope.json"
PERMIT_RELPATH = "validation/family_permit.json"


class Cli12Error(RuntimeError):
    """R1.2 CLI 自身错误（路径/状态/重复 init）。"""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"[{code}] {detail}")


# ---------------------------------------------------------------------------
# 基础 IO
# ---------------------------------------------------------------------------

def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _json_text(doc: Mapping[str, Any]) -> str:
    return json.dumps(doc, indent=1, sort_keys=True, ensure_ascii=False) + "\n"


def _read_json(path: Path) -> "dict[str, Any]":
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise Cli12Error("FILE_ABSENT", f"文件不存在：{path}") from None
    except json.JSONDecodeError:
        raise Cli12Error("FILE_CORRUPT", f"文件不可解析：{path}") from None


def _write_json(path: Path, doc: Mapping[str, Any]) -> str:
    text = _json_text(doc)
    atomic_write_text(path, text, mode=0o600)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_sha256(path: Path) -> str:
    try:
        return _sha256_bytes(path.read_bytes())
    except FileNotFoundError:
        raise Cli12Error("FILE_ABSENT", f"文件不存在：{path}") from None


def _profile_sha(profile: P.CampaignProfile) -> str:
    return C.document_hash(profile.to_dict())


# ---------------------------------------------------------------------------
# 根与隔离
# ---------------------------------------------------------------------------

def _family_root() -> Path:
    return RP.rsi_r12_root()


def _assert_root_within_family(root: Path) -> Path:
    family = _family_root().resolve(strict=False)
    resolved = root.resolve(strict=False)
    if resolved == family or family not in resolved.parents:
        raise Cli12Error(
            "ROOT_OUTSIDE_FAMILY",
            f"envelope 根 {resolved} 必须位于 R1.2 家族根 {family} 之内")
    return resolved


def _assert_root_isolated(root: Path) -> None:
    protected = tuple(RP.rsi_r12_protected_write_roots().values())
    RP._assert_roots_isolated(  # noqa: SLF001 —— 唯一实现复用，不另写比对
        root, protected, label="SIMPRAXIS_RSI_R12_ROOT",
        message=("R1.2 envelope 根必须与封存期根及 RSI-R0/R0.1/R0.2/R1 "
                 "家族根互异且互不嵌套"))


def _resolve_root(explicit: "str | None", envelope_id: "str | None") -> Path:
    if explicit:
        root = Path(explicit)
    else:
        if not envelope_id:
            raise Cli12Error("ROOT_UNSPECIFIED", "必须给 --root 或 --envelope")
        root = _family_root() / envelope_id
    root = _assert_root_within_family(root)
    _assert_root_isolated(root)
    return root


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------

def _plan_freeze(plan_path: Path) -> "tuple[str, str]":
    if not plan_path.is_file():
        raise Cli12Error("PLAN_ABSENT", f"Plan 文件不存在：{plan_path}")
    return str(plan_path), _file_sha256(plan_path)


def _science_scope_doc(envelope_id: str, profile: P.CampaignProfile) -> "dict[str, Any]":
    """冻结公开科学范围与失败政策（Plan §5 原合同不变；T0 冻结面）。"""
    contract = P.public_contract_document(
        profile, envelope_id=envelope_id, budget_sha256="0" * 64)
    return {
        "schema": "simpraxis.rsi_r12.public_science_scope.v1",
        "schema_version": P.SCHEMA_VERSION,
        "envelope_id": envelope_id,
        "profile_id": profile.profile_id,
        "science": contract["science"],
        "refusal_policy": contract["refusal_policy"],
        "error_classes": contract["error_classes"],
        "recovery": contract["recovery"],
        "units": contract["units"],
        "aux_subenvelopes": contract["aux_subenvelopes"],
        "note": ("两案例最小自主研究可行性；阴性控制不足即 INCONCLUSIVE，"
                 "不满足阴性门；不外推普遍科研能力"),
    }


def cmd_init(args: argparse.Namespace) -> int:
    profile = P.profile_by_id(P.PROFILE_R12)
    envelope_id = args.envelope
    if not envelope_id:
        raise Cli12Error("ENVELOPE_REQUIRED", "init 必须给 --envelope")
    root = _resolve_root(args.root, envelope_id)
    envelope_path = root / ENVELOPE_RELPATH
    if envelope_path.exists():
        raise Cli12Error("ENVELOPE_EXISTS",
                         f"envelope 已初始化：{envelope_path}（不覆盖，需新 envelope id）")
    mode = args.mode
    if mode not in C.ENVELOPE_MODES:
        raise Cli12Error("MODE_INVALID", f"--mode {mode!r} 不在 {C.ENVELOPE_MODES}")

    for name in RP.RSI_R12_CAMPAIGN_SUBDIRS:
        ensure_private_dir(root / name)

    plan_path, plan_sha = _plan_freeze(Path(args.plan) if args.plan else PLAN_PATH_R12)

    budget = C.default_budget_v2(envelope_id)
    budget_sha = _write_json(root / BUDGET_RELPATH, budget.to_dict())

    permit = FAM.register(_family_root(), profile, envelope_id)
    permit_sha = _write_json(root / PERMIT_RELPATH, permit)

    cost_gate: "dict[str, Any]" = {
        "decision": C.COST_DECISION_BLOCKED_V2,
        "paid_authorized": False,
        "consent_path": None,
        "consent_sha256": None,
        "note": ("T0 缺省 BLOCKED：离线 T0–T5 可继续，T6 付费探针不得发网；"
                 "付费须呈交 R1.2 范围 ConsentV2"),
    }
    if args.consent:
        consent_path = Path(args.consent)
        consent_doc = _read_json(consent_path)
        consent = C.consent_from_dict(consent_doc)
        if not isinstance(consent, C.ConsentV2):
            raise Cli12Error("CONSENT_SCHEMA",
                             "R1.2 只接受 simpraxis.rsi_r12.consent.v1")
        if consent.envelope_id != envelope_id:
            raise Cli12Error("CONSENT_ENVELOPE",
                             f"consent.envelope_id={consent.envelope_id!r} 不符")
        C.verify_consent_binds_plan(consent, plan_path=plan_path,
                                    plan_sha256=plan_sha)
        consent_sha = _write_json(root / CONSENT_RELPATH, consent.to_dict())
        if mode != "paid":
            raise Cli12Error("MODE_INVALID",
                             "附付费授权时 --mode 必须是 paid")
        cost_gate = {
            "decision": consent.decision,
            "paid_authorized": consent.paid_authorized,
            "consent_path": CONSENT_RELPATH,
            "consent_sha256": consent_sha,
            "note": consent.exposure_note or consent.notes,
        }
    elif mode == "paid":
        raise Cli12Error(
            "COST_GATE_BLOCKED",
            "paid 模式必须附 --consent（R1.2 范围 ConsentV2）；"
            "T0–T5 离线工作不受影响")

    scope_doc = _science_scope_doc(envelope_id, profile)
    scope_sha = _write_json(root / SCOPE_RELPATH, scope_doc)

    contract_doc = P.public_contract_document(
        profile, envelope_id=envelope_id, budget_sha256=budget_sha)
    P.assert_public_contract_v2_complete(contract_doc)
    contract_sha = _write_json(root / CONTRACT_JSON_RELPATH, contract_doc)
    atomic_write_text(root / CONTRACT_MD_RELPATH,
                      P.render_public_contract_v2(contract_doc) + "\n",
                      mode=0o644)

    envelope_doc = {
        "schema": P.ENVELOPE_SCHEMA_V2,
        "schema_version": P.SCHEMA_VERSION,
        "profile_id": profile.profile_id,
        "envelope_id": envelope_id,
        "mode": mode,
        "created_at_utc": _utc_now(),
        "plan_path": plan_path,
        "plan_sha256": plan_sha,
        "budget_path": BUDGET_RELPATH,
        "budget_sha256": budget_sha,
        "profile_sha256": _profile_sha(profile),
        "ledger_namespace": profile.namespace,
        "hard_cap": profile.hard_cap,
        "normal_budget": profile.normal_budget,
        "lane_caps": {lane: profile.lane_post_caps[lane]
                      for lane in profile.lane_order},
        "family_cap": profile.family_cap,
        "history_posts": profile.family_history_posts,
        "family_permit_path": PERMIT_RELPATH,
        "family_permit_sha256": permit_sha,
        "cost_gate": cost_gate,
        "public_science_scope_sha256": scope_sha,
        "public_contract_sha256": contract_sha,
    }
    C.validate_envelope_v2(envelope_doc, envelope_id=envelope_id)
    _write_json(envelope_path, envelope_doc)
    print(json.dumps({
        "envelope_id": envelope_id,
        "root": str(root),
        "profile_id": profile.profile_id,
        "ledger_namespace": profile.namespace,
        "hard_cap": profile.hard_cap,
        "family_cap": profile.family_cap,
        "mode": mode,
        "cost_gate": cost_gate["decision"],
        "budget_sha256": budget_sha,
        "contract_sha256": contract_sha,
    }, ensure_ascii=False, sort_keys=True))
    return 0


# ---------------------------------------------------------------------------
# check / status / contract
# ---------------------------------------------------------------------------

def _load_envelope(root: Path, envelope_id: "str | None" = None
                   ) -> "dict[str, Any]":
    doc = _read_json(root / ENVELOPE_RELPATH)
    return C.validate_envelope_v2(doc, envelope_id=envelope_id)


def _check_envelope(root: Path) -> "dict[str, Any]":
    envelope = _load_envelope(root)
    profile = P.resolve_profile(envelope)
    budget_doc = _read_json(root / str(envelope["budget_path"]))
    if _sha256_bytes(_json_text(budget_doc).encode("utf-8")) \
            != envelope["budget_sha256"]:
        raise Cli12Error("BUDGET_TAMPERED", "budget.json 字节哈希与 envelope 不符")
    budget = C.budget_from_dict(budget_doc)
    if not isinstance(budget, C.BudgetSpecV2):
        raise Cli12Error("BUDGET_SCHEMA", "R1.2 envelope 必须携带 budget.v2")
    C.assert_single_truth_v2(
        budget,
        ledger_task_caps=QL.RSI_R12_TASK_CAPS,
        ledger_hard_cap=QL.RSI_R12_HARD_CAP,
        ledger_normal_budget=QL.RSI_R12_NORMAL_BUDGET,
        family_cap=envelope["family_cap"])
    if _profile_sha(profile) != envelope["profile_sha256"]:
        raise Cli12Error("PROFILE_DRIFT", "profile 定义自 init 后已变化")
    permit = _read_json(root / str(envelope["family_permit_path"]))
    if _sha256_bytes(_json_text(permit).encode("utf-8")) \
            != envelope["family_permit_sha256"]:
        raise Cli12Error("PERMIT_TAMPERED", "族许可件字节哈希与 envelope 不符")
    FAM.verify_permit(_family_root(), permit, profile, envelope["envelope_id"])
    scope_doc = _read_json(root / SCOPE_RELPATH)
    if _sha256_bytes(_json_text(scope_doc).encode("utf-8")) \
            != envelope["public_science_scope_sha256"]:
        raise Cli12Error("SCOPE_TAMPERED", "公开科学范围件字节哈希与 envelope 不符")
    contract_doc = _read_json(root / CONTRACT_JSON_RELPATH)
    if _sha256_bytes(_json_text(contract_doc).encode("utf-8")) \
            != envelope["public_contract_sha256"]:
        raise Cli12Error("CONTRACT_TAMPERED", "公开合同件字节哈希与 envelope 不符")
    P.assert_public_contract_v2_complete(contract_doc)
    if contract_doc["budget_sha256"] != envelope["budget_sha256"]:
        raise Cli12Error("CONTRACT_TAMPERED", "公开合同未绑定本 envelope 预算")
    ledger = root / "quota" / "ledger.jsonl"
    rows = 0
    if ledger.is_file():
        rows = sum(1 for line in ledger.read_text(encoding="utf-8").splitlines()
                   if line.strip())
    return {
        "ok": True,
        "envelope_id": envelope["envelope_id"],
        "root": str(root),
        "profile_id": profile.profile_id,
        "mode": envelope["mode"],
        "cost_gate": envelope["cost_gate"]["decision"],
        "ledger_namespace": envelope["ledger_namespace"],
        "posts_cap": budget.total_posts,
        "family": FAM.usage(_family_root()),
        "ledger_rows": rows,
        "plan_sha256": envelope["plan_sha256"],
    }


def cmd_check(args: argparse.Namespace) -> int:
    root = _resolve_root(args.root, args.envelope)
    report = _check_envelope(root)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    root = _resolve_root(args.root, args.envelope)
    envelope = _load_envelope(root)
    print(json.dumps({
        "envelope_id": envelope["envelope_id"],
        "root": str(root),
        "profile_id": envelope["profile_id"],
        "mode": envelope["mode"],
        "cost_gate": envelope["cost_gate"],
        "posts_cap": envelope["hard_cap"],
        "family": FAM.usage(_family_root()),
    }, ensure_ascii=False, sort_keys=True))
    return 0


def cmd_contract(args: argparse.Namespace) -> int:
    root = _resolve_root(args.root, args.envelope)
    _load_envelope(root)
    if args.json:
        doc = _read_json(root / CONTRACT_JSON_RELPATH)
        P.assert_public_contract_v2_complete(doc)
        print(json.dumps(doc, ensure_ascii=False, sort_keys=True))
        return 0
    doc = _read_json(root / CONTRACT_JSON_RELPATH)
    P.assert_public_contract_v2_complete(doc)
    print(P.render_public_contract_v2(doc))
    return 0


# ---------------------------------------------------------------------------
# human（T3）：人工时间/救援令/assisted 的真实 CLI 面
# ---------------------------------------------------------------------------

def _human_log(root: Path) -> HE.HumanEventLog:
    _load_envelope(root)
    ctx = RT.CampaignContext(envelope_id=str(root.name), root=root,
                             protected_roots=RT.protected_roots_tuple())
    return HE.load(ctx)


def cmd_human_begin(args: argparse.Namespace) -> int:
    root = _resolve_root(args.root, args.envelope)
    log = _human_log(root)
    ev = log.begin(kind=args.kind, actor=args.actor, reason=args.reason,
                   artifact_refs=tuple(args.artifact or ()),
                   start_utc=args.start_utc, study=args.study,
                   touch=args.touch)
    print(json.dumps(ev.as_dict(), ensure_ascii=False, sort_keys=True))
    return 0


def cmd_human_end(args: argparse.Namespace) -> int:
    root = _resolve_root(args.root, args.envelope)
    ev = _human_log(root).end(event_id=args.event_id, end_utc=args.end_utc)
    print(json.dumps(ev.as_dict(), ensure_ascii=False, sort_keys=True))
    return 0


def cmd_human_rescue(args: argparse.Namespace) -> int:
    root = _resolve_root(args.root, args.envelope)
    ev = _human_log(root).record_rescue_order(
        actor=args.actor, reason=args.reason, order_ref=args.order_ref,
        start_utc=args.start_utc, end_utc=args.end_utc, study=args.study,
        touch=args.touch)
    print(json.dumps(ev.as_dict(), ensure_ascii=False, sort_keys=True))
    return 0


def cmd_human_adjudicate(args: argparse.Namespace) -> int:
    root = _resolve_root(args.root, args.envelope)
    success = bool(args.success)
    if args.failure:
        success = False
    ev = _human_log(root).adjudicate_rescue(
        event_id=args.event_id, success=success, actor=args.actor,
        evidence_ref=args.evidence_ref)
    print(json.dumps(ev.as_dict(), ensure_ascii=False, sort_keys=True))
    return 0


def cmd_human_summary(args: argparse.Namespace) -> int:
    root = _resolve_root(args.root, args.envelope)
    summary = _human_log(root).summary()
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


def cmd_human_assisted(args: argparse.Namespace) -> int:
    """把关键解释/代码修订（可选含实质救援）推进为 ``ASSISTED``。

    ``assisted`` 是单向门：推进后不可由本 CLI 清除；纯运维不得触发。
    """
    root = _resolve_root(args.root, args.envelope)
    log = _human_log(root)
    from orchestrator.rsi_r1.state import load_state  # 局部导入避免环
    state = load_state(RT.CampaignContext(
        envelope_id=str(root.name), root=root,
        protected_roots=RT.protected_roots_tuple()))
    applied = log.apply_assisted_rule(state, args.event_id,
                                      include_rescue=bool(args.include_rescue))
    print(json.dumps({"event_id": args.event_id, "assisted": applied,
                      "include_rescue": bool(args.include_rescue)},
                     ensure_ascii=False, sort_keys=True))
    return 0


# ---------------------------------------------------------------------------
# controls（T4 / AC56）：新根 v2 校准（零模型 POST）
# ---------------------------------------------------------------------------

class R12Context:
    """校准模块所需的 campaign ctx 面（R1.2 根；写守卫同族纪律）。

    ``readonly=True`` 用于旧 R1 campaign 根（历史件只读，任何写直接拒绝）。
    """

    is_r12 = True

    def __init__(self, root: Path, envelope_id: str, *,
                 readonly: bool = False) -> None:
        self.root = Path(root)
        self.envelope_id = envelope_id
        self.readonly = readonly

    def subdir(self, name: str) -> Path:
        if name not in RP.RSI_R12_CAMPAIGN_SUBDIRS:
            raise Cli12Error("SUBDIR_UNKNOWN", f"未知 R1.2 子目录：{name!r}")
        return self.root / name

    def relative(self, relpath: "str | Path") -> Path:
        rel = Path(relpath)
        if rel.is_absolute() or ".." in rel.parts:
            raise Cli12Error("REL_PATH_REFUSED", f"只接受根内相对路径：{rel}")
        return self.root / rel

    def _assert_write_allowed(self, target: Path) -> None:
        if self.readonly:
            raise Cli12Error("PRIOR_ROOT_READONLY",
                             f"旧 root 只读，拒绝写入：{target}")
        base = self.root.resolve(strict=False)
        rp = target.resolve(strict=False)
        if rp != base and base not in rp.parents:
            raise Cli12Error("WRITE_OUTSIDE_ROOT", f"写路径越出根：{rp}")
        for name, prot in RP.rsi_r12_protected_write_roots().items():
            prot_r = Path(prot).resolve(strict=False)
            if rp == prot_r or prot_r in rp.parents:
                raise Cli12Error("WRITE_PROTECTED_ROOT",
                                 f"写路径落进 {name}（{prot_r}）")

    def write_text(self, relpath: "str | Path", text: str) -> Path:
        target = self.relative(relpath)
        self._assert_write_allowed(target)
        ensure_private_dir(target.parent)
        atomic_write_text(target, text, mode=0o600)
        return target

    def write_json(self, relpath: "str | Path", obj: Any) -> Path:
        return self.write_text(
            relpath, json.dumps(obj, ensure_ascii=False, indent=1,
                                sort_keys=True, allow_nan=False) + "\n")

    def read_json(self, relpath: "str | Path") -> Any:
        return _read_json(self.relative(relpath))

    def exists(self, relpath: "str | Path") -> bool:
        return self.relative(relpath).is_file()


def _readonly_prior_context(prior_root: "str | None", envelope_id: str
                            ) -> "R12Context":
    if not prior_root:
        raise Cli12Error(
            "PRIOR_ROOT_REQUIRED",
            "v2 校准必须给 --prior-root（旧 R1 campaign 根；只读引用其 v1 "
            "预注册/报告/oracle/raw，不在新根重跑 v1）")
    root = Path(prior_root)
    if not root.is_dir():
        raise Cli12Error("PRIOR_ROOT_ABSENT", f"旧 root 不存在：{root}")
    resolved = root.resolve(strict=False)
    family = _family_root().resolve(strict=False)
    if resolved == family or family in resolved.parents:
        raise Cli12Error("PRIOR_ROOT_INSIDE_R12", f"旧 root 不得位于 R1.2 家族根内：{root}")
    return R12Context(resolved, envelope_id, readonly=True)


def cmd_controls(args: argparse.Namespace) -> int:
    """AC56：``--calibration-version v2`` 新 profile 分派。

    前置（fail-closed，任一不成立即停，不调界/不换 seed）：

    * 版本只接受 v2；v1 是历史件，不在新根重跑；
    * 当前 root 的 R1.2 账（2904）无任何 call 行——族历史 31 不是当前 root 付费，
      不参与本判定；
    * 零 POST permit：paid LOCK 不存在 + 生产装配入口机械证明 LOCK_MISSING；
    * 来源/镜像/矩阵/量表/oracle 身份逐项核验（import 加载 + sha 对账）。
    """
    version = str(args.calibration_version)
    if version != "v2":
        raise Cli12Error(
            "R12_CALIBRATION_VERSION",
            f"R1.2 只接受 --calibration-version v2（得到 {version!r}）；"
            "v1 是旧根历史件，不在新根重跑")
    root = _resolve_root(args.root, args.envelope)
    envelope = _load_envelope(root, args.envelope)
    profile = P.resolve_profile(envelope)
    envelope_id = str(envelope["envelope_id"])
    ledger = root / "quota" / "ledger.jsonl"
    ctx = R12Context(root, envelope_id)
    design = CAL.design_for(version)
    oracle_path = ctx.relative(design["oracle_relpath"])

    if args.prepare_only:
        prior_ctx = _readonly_prior_context(args.prior_root, envelope_id)
        prior = CAL.prior_attempt_manifest(prior_ctx)
        equivalence = CAL.assert_oracle_v2_equivalence_paths(
            prior_ctx.relative(CAL.DESIGN_V1["oracle_relpath"]), oracle_path)
        prior = {**prior, "oracle_equivalence": equivalence}
        v1_prereg = prior_ctx.read_json(CAL.PREREG_RELPATH)
        v1_report = prior_ctx.read_json(CAL.REPORT_RELPATH)
        supersedes = {
            "prereg_schema": CAL.PREREG_SCHEMA,
            "prereg_relpath": CAL.PREREG_RELPATH,
            "prereg_file_sha256": CAL.oracle_sha256(
                prior_ctx.relative(CAL.PREREG_RELPATH)),
            "prereg_document_sha256": v1_prereg.get("document_sha256"),
            "report_relpath": CAL.REPORT_RELPATH,
            "report_file_sha256": CAL.oracle_sha256(
                prior_ctx.relative(CAL.REPORT_RELPATH)),
            "report_verdict": v1_report.get("verdict"),
            "gap_relpath": CAL.GAP_RELPATH,
            "gap_file_sha256": CAL.oracle_sha256(
                prior_ctx.relative(CAL.GAP_RELPATH)),
            "prior_root": str(prior_ctx.root),
        }
        doc = CAL.build_calibration_prereg(
            ctx=ctx, oracle_path=oracle_path, design=design,
            supersedes=supersedes, prior_attempt=prior)
        path = CAL.write_calibration_prereg(ctx, doc, design)
        permit = CAL.issue_zero_post_permit(ctx, ledger_path=ledger)
        print(f"controls prepare ok  envelope={envelope_id} design=v2 root={ctx.root}")
        print(f"  prereg  : {path}")
        print(f"  sha256  : {doc['document_sha256'][:16]}…")
        print(f"  oracle  : {doc['oracle']['sha256'][:16]}…")
        print(f"  prior   : root={prior_ctx.root} files={prior['n_files']} "
              f"raws={prior['n_raws']} files_sha={prior['files_sha256'][:16]}… "
              f"raws_sha={prior['raws_sha256'][:16]}…")
        print(f"  oracle≡v1 max|ΔRMSE|={equivalence['max_abs_diff']!r}（8 block）")
        print(f"  permit  : {permit['kind']} posts={permit['model_posts_allowed']} "
              f"refusal={permit['sender_refusal']['code']}")
        print(f"  ledger  : {ledger} call_rows=0（族历史不计入当前 root）")
        return 0

    if not args.real_containers:
        print("BLOCKED 校准会真跑容器：需显式 --real-containers（无模型 POST）",
              file=sys.stderr)
        return 2
    from orchestrator.rsi_r1 import sandbox as SANDBOX

    prior_ctx = None
    if args.prior_root:
        prior_ctx = _readonly_prior_context(args.prior_root, envelope_id)
    report = CAL.run_calibration(
        ctx, runner=SANDBOX.DockerRunner(), image=args.image or CAL.CAL_IMAGE,
        ledger_path=ledger, design=design, prior_ctx=prior_ctx,
        notes=[f"R1.2 v2 校准（envelope={envelope_id}）沿正式 dispatcher/"
               f"raw/资源账；零项目模型 POST"])
    print(f"controls envelope={envelope_id} design=v{report['schema_version']}  "
          f"verdict={report['verdict']}")
    print(f"  runs={len(report['runs'])} checks={len(report['checks'])} "
          f"failures={len(report['failures'])}")
    print(f"  budget runs={report['budget']['total_runs']} "
          f"steps={report['budget']['total_steps']}")
    return 0


# ---------------------------------------------------------------------------
# parser / main
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m orchestrator.rsi_r1.cli_r12",
        description="RSI-R1.2（profile rsi-r1.2；账 2904；族帽 155）")
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("init", help="初始化 R1.2 envelope（零 POST）")
    pi.add_argument("--envelope", required=True)
    pi.add_argument("--root", default=None)
    pi.add_argument("--mode", default="offline_validation",
                    choices=list(C.ENVELOPE_MODES))
    pi.add_argument("--plan", default=None)
    pi.add_argument("--consent", default=None)
    pi.set_defaults(func=cmd_init)

    pc = sub.add_parser("check", help="机读自检（profile/预算/族/合同/账行）")
    pc.add_argument("--root", default=None)
    pc.add_argument("--envelope", default=None)
    pc.set_defaults(func=cmd_check)

    ps = sub.add_parser("status", help="envelope 与族账只读摘要")
    ps.add_argument("--root", default=None)
    ps.add_argument("--envelope", default=None)
    ps.set_defaults(func=cmd_status)

    pk = sub.add_parser("contract", help="渲染公开资源合同 v2")
    pk.add_argument("--root", default=None)
    pk.add_argument("--envelope", default=None)
    pk.add_argument("--json", action="store_true")
    pk.set_defaults(func=cmd_contract)

    pctl = sub.add_parser(
        "controls",
        help="T4 校准：新根 v2（--calibration-version v2）；零模型 POST")
    pctl.add_argument("--root", default=None)
    pctl.add_argument("--envelope", default=None)
    pctl.add_argument("--calibration-version", default="v2", choices=("v2",),
                      help="R1.2 只接受 v2（v1 是旧根历史件，不重跑）")
    pctl.add_argument("--prepare-only", action="store_true",
                      help="只写 v2 预注册 + 零 POST permit（不跑容器）")
    pctl.add_argument("--real-containers", action="store_true",
                      help="显式确认真跑容器（仍不发任何模型 POST）")
    pctl.add_argument("--image", default=None,
                      help="容器镜像（缺省取钉死的校准镜像）")
    pctl.add_argument("--prior-root", default=None,
                      help="旧 R1 campaign 根（只读；v2 必须引用其 v1 历史件）")
    pctl.set_defaults(func=cmd_controls)

    ph = sub.add_parser("human", help="人工时间/救援令/assisted（T3）")
    hsub = ph.add_subparsers(dest="human_cmd", required=True)

    def _root_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--root", default=None)
        p.add_argument("--envelope", default=None)

    hb = hsub.add_parser("begin", help="开始一条人工事件（未结束不填 0）")
    _root_args(hb)
    hb.add_argument("--kind", required=True, choices=list(HE.KINDS))
    hb.add_argument("--actor", required=True)
    hb.add_argument("--reason", required=True)
    hb.add_argument("--study", default=None)
    hb.add_argument("--touch", default=None, choices=list(HE.ASSISTED_TOUCHES))
    hb.add_argument("--artifact", action="append", default=None)
    hb.add_argument("--start-utc", default=None)
    hb.set_defaults(func=cmd_human_begin)

    hd = hsub.add_parser("end", help="结束一条人工事件")
    _root_args(hd)
    hd.add_argument("--event-id", required=True)
    hd.add_argument("--end-utc", default=None)
    hd.set_defaults(func=cmd_human_end)

    hr = hsub.add_parser("rescue", help="登记救援令（success 恒 False，需裁决）")
    _root_args(hr)
    hr.add_argument("--actor", required=True)
    hr.add_argument("--reason", required=True)
    hr.add_argument("--order-ref", required=True)
    hr.add_argument("--study", default=None)
    hr.add_argument("--touch", default=None, choices=list(HE.ASSISTED_TOUCHES))
    hr.add_argument("--start-utc", default=None)
    hr.add_argument("--end-utc", default=None)
    hr.set_defaults(func=cmd_human_rescue)

    ha = hsub.add_parser("adjudicate", help="外部裁决救援令成败（一次性）")
    _root_args(ha)
    ha.add_argument("--event-id", required=True)
    ha.add_argument("--success", action="store_true")
    ha.add_argument("--failure", action="store_true")
    ha.add_argument("--actor", required=True)
    ha.add_argument("--evidence-ref", required=True)
    ha.set_defaults(func=cmd_human_adjudicate)

    hs = hsub.add_parser("summary", help="人工时间汇总（未知事件不填 0）")
    _root_args(hs)
    hs.set_defaults(func=cmd_human_summary)

    has_ = hsub.add_parser("assisted", help="实质接触 → ASSISTED（单向）")
    _root_args(has_)
    has_.add_argument("--event-id", required=True)
    has_.add_argument("--include-rescue", action="store_true",
                      help="实质救援（touch=code/interpretation）也算接触")
    has_.set_defaults(func=cmd_human_assisted)
    return p


def main(argv: "list[str] | None" = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (Cli12Error, C.ContractError, FAM.FamilyError, P.ProfileError,
            RP.RsiR12CampaignUnsetError, HE.HumanEventError,
            RT.CampaignWriteRefused, OSError, CAL.CalibrationError) as exc:
        print(f"BLOCKED {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
