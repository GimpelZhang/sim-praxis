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
from orchestrator.rsi_r1 import lock as LK
from orchestrator.rsi_r1 import offline_suite as OS
from orchestrator.rsi_r1 import offline_validation as OV
from orchestrator.rsi_r1 import prereg as PREREG
from orchestrator.rsi_r1 import profiles as P
from orchestrator.rsi_r1 import resources as RS
from orchestrator.rsi_r1 import runtime as RT
from orchestrator.rsi_r1 import sandbox as SANDBOX
from orchestrator.rsi_r1 import state as ST

PLAN_PATH_R12 = Path.home() / "文档" / "RSI_R1.2_Plan_detailed.md"

ENVELOPE_RELPATH = "contract/envelope.json"
BUDGET_RELPATH = "contract/budget.json"
CONSENT_RELPATH = "contract/consent.json"
CONTRACT_JSON_RELPATH = "contract/public_contract.json"
CONTRACT_MD_RELPATH = "contract/public_contract.md"
#: 候选可见面：生产链（``protocol._seed_public`` / ``assembly._design_inputs``）
#: 只认 ``public/`` 这两个文件；它们是 ``contract/`` 唯一源的**逐字节投影**。
PUBLIC_VIEW_JSON_RELPATH = "public/public_contract.json"
PUBLIC_VIEW_MD_RELPATH = "public/public_contract.md"
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
    if mode == "offline_validation":
        # 锁内适配（2026-11-12 T5 全量演练）：离线根**零 POST**，付费账本不该有
        # 任何行——但「文件不存在」会被结案/复算器读成「缺账本」（真空满足被拒）。
        # 离线模式建一个**空** 0600 账本：0 行是事实陈述（零付费调用），账务面
        # 也因此统一。paid 模式不预建，账本仍由首次写入方创建。
        ledger = root / "quota" / "ledger.jsonl"
        if not ledger.is_file():
            atomic_write_text(ledger, "", mode=0o600)

    plan_path, plan_sha = _plan_freeze(Path(args.plan) if args.plan else PLAN_PATH_R12)

    budget = C.default_budget_v2(envelope_id)
    budget_sha = C.document_hash(budget.to_dict())
    _write_json(root / BUDGET_RELPATH, budget.to_dict())

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
    budget = C.budget_from_dict(budget_doc)
    if not isinstance(budget, C.BudgetSpecV2):
        raise Cli12Error("BUDGET_SCHEMA", "R1.2 envelope 必须携带 budget.v2")
    # R1.2：envelope.budget_sha256 是预算**文档哈希**（与 lock/assembly 同一口径），
    # 不是 budget.json 的字节 hash——字节可被重排而不改语义，哈希口径必须单一。
    if budget.sha256 != envelope["budget_sha256"]:
        raise Cli12Error("BUDGET_TAMPERED", "budget.json 文档哈希与 envelope 不符")
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

    def assert_write_allowed(self, target: "str | Path") -> Path:
        """公开写闸面（``state.Journal``/``closure``/``repair`` 的共用入口）。"""
        path = Path(target)
        self._assert_write_allowed(path)
        return path

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
    if report["verdict"] == "CALIBRATION_PASS":
        state = ST.ResearchState(ctx)
        if state.root_state() == ST.ROOT_AUTHORIZED:
            state.advance_root(
                ST.ROOT_CALIBRATED,
                reason=f"controls v2 真容器校准 verdict={report['verdict']}（零 POST）")
            print(f"  root    : {state.root_state()}")
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

    # ---- T5：离线生产准入与 LOCK ----
    ppr = sub.add_parser("prereg", help="仪器预注册（T5 前置，幂等）")
    _root_args(ppr)
    ppr.set_defaults(func=cmd_prereg)

    pao = sub.add_parser("authorize-offline",
                         help="写离线授权件 + 固定套注册件（零 POST）")
    _root_args(pao)
    pao.set_defaults(func=cmd_authorize_offline)

    pfr = sub.add_parser("publish-public",
                         help="把 contract/ 公开合同逐字节投影到候选可见面 public/")
    _root_args(pfr)
    pfr.set_defaults(func=cmd_publish_public)

    pfr = sub.add_parser("freeze", help="冻结 validation LOCK（root → FROZEN）")
    _root_args(pfr)
    pfr.add_argument("--image", default=None,
                     help="镜像 pin（缺省取校准报告的 container_image）")
    pfr.set_defaults(func=cmd_freeze)

    pra = sub.add_parser(
        "regression-attest",
        help="真跑 T2/T3 回归测试，落绑定冻结闭包的取证件（零 POST）")
    _root_args(pra)
    pra.add_argument("--python", default=None,
                     help="解释器（缺省 sys.executable）")
    pra.set_defaults(func=cmd_regression_attest)

    psg = sub.add_parser("sign", help="签 validation LOCK（报告/实现/画像绑定）")
    _root_args(psg)
    psg.add_argument("--role", required=True, choices=list(LK.VALIDATION_ROLES))
    psg.add_argument("--actor", required=True)
    psg.add_argument("--decision", required=True, choices=list(LK.DECISIONS))
    psg.add_argument("--rationale", default="")
    psg.set_defaults(func=cmd_sign)

    pv = sub.add_parser("verify", help="终局校验 LOCK + 三签绑定")
    _root_args(pv)
    pv.set_defaults(func=cmd_verify)

    pst = sub.add_parser("state", help="操作者推进 root（仅 AUTHORIZED/CALIBRATED）")
    _root_args(pst)
    pst.add_argument("--to", required=True)
    pst.add_argument("--reason", default=None)
    pst.set_defaults(func=cmd_state)

    pvp = sub.add_parser(
        "validate-production",
        help="零 POST 全链演练：正式 CLI/装配/DockerRunner + 公开消费者")
    _root_args(pvp)
    pvp.add_argument("--image", default=None, help="镜像 pin（缺省取 LOCK 内 pin）")
    pvp.add_argument("--only", nargs="*", default=None,
                     help="只跑指定套件（调试用；子集报告判 INCOMPLETE、"
                          "不得签署——六套全跑才算验收）")
    pvp.set_defaults(func=cmd_validate_production)
    return p


# ---------------------------------------------------------------------------
# T5：离线生产准入 → 冻结（validation LOCK）→ 演练 → 三签 → 终局 verify
# ---------------------------------------------------------------------------

def _r12_ctx(root: Path, envelope_id: str) -> "R12Context":
    return R12Context(root, envelope_id)


def _ledger_path(root: Path) -> Path:
    return root / "quota" / "ledger.jsonl"


def cmd_prereg(args: argparse.Namespace) -> int:
    """仪器预注册（先注册后跑；幂等且漂移即拒）。"""
    root = _resolve_root(args.root, args.envelope)
    envelope = _load_envelope(root)
    ctx = _r12_ctx(root, str(envelope["envelope_id"]))
    doc = PREREG.write_prereg(
        ctx, envelope_id=str(envelope["envelope_id"]),
        plan_sha256=str(envelope["plan_sha256"]),
        model_config_sha256=envelope.get("model_config_sha256"))
    print(f"prereg ok  envelope={envelope['envelope_id']}")
    print(f"  registered_at : {doc['registered_at_utc']}")
    print(f"  document_sha  : {doc['document_sha256'][:16]}…")
    print(f"  rubric_sha    : {doc['rubric_sha256'][:16]}…")
    return 0


def cmd_authorize_offline(args: argparse.Namespace) -> int:
    """写不可变离线授权件 + 固定套注册件（零 POST；root INIT→AUTHORIZED）。"""
    root = _resolve_root(args.root, args.envelope)
    envelope = _load_envelope(root)
    ctx = _r12_ctx(root, str(envelope["envelope_id"]))
    auth = OV.authorize_offline(
        ctx, repo_root=LK.REPO_ROOT, ledger_path=_ledger_path(root),
        consumer_path=OV.consumer_source(LK.REPO_ROOT))
    spec = OV.suite_spec_document(
        ctx, authorization_sha256=C.canonical_sha256(auth))
    ctx.write_json(OV.SPEC_RELPATH, spec)
    print(f"authorize-offline ok  envelope={envelope['envelope_id']}")
    print(f"  consumer_sha256 : {(auth.get('consumer') or {}).get('sha256', '')[:16]}…")
    print(f"  profile_sha256  : {str(auth.get('profile_sha256'))[:16]}…")
    print(f"  suites          : {sorted(spec['suites'])}")
    print(f"  root            : {ST.ResearchState(ctx).root_state()}")
    return 0


def _publish_public_view(root: Path, envelope: Mapping[str, Any]) -> "dict[str, str]":
    """``contract/`` → ``public/`` 的逐字节投影（唯一源校验后复制）。"""
    src_json = root / CONTRACT_JSON_RELPATH
    src_md = root / CONTRACT_MD_RELPATH
    for path in (src_json, src_md):
        if not path.is_file():
            raise Cli12Error("FILE_ABSENT", f"文件不存在：{path}")
    json_text = src_json.read_text(encoding="utf-8")
    json_sha = _sha256_bytes(json_text.encode("utf-8"))
    if json_sha != str(envelope.get("public_contract_sha256")):
        raise Cli12Error(
            "PUBLIC_CONTRACT_TAMPERED",
            f"{CONTRACT_JSON_RELPATH} 哈希 {json_sha[:16]}… 与 "
            f"envelope.public_contract_sha256 不符")
    md_text = src_md.read_text(encoding="utf-8")
    ensure_private_dir(root / "public")
    atomic_write_text(root / PUBLIC_VIEW_JSON_RELPATH, json_text, mode=0o600)
    atomic_write_text(root / PUBLIC_VIEW_MD_RELPATH, md_text, mode=0o600)
    return {"json_sha256": json_sha,
            "md_sha256": _sha256_bytes(md_text.encode("utf-8"))}


def _verify_public_view(root: Path, envelope: Mapping[str, Any]) -> None:
    """freeze 前闸：公开视图必须在场且与 ``contract/`` 源件逐字节相等。"""
    for rel in (PUBLIC_VIEW_JSON_RELPATH, PUBLIC_VIEW_MD_RELPATH):
        if not (root / rel).is_file():
            raise Cli12Error(
                "PUBLIC_VIEW_MISSING",
                f"{rel} 缺件（生产链读 public/；先跑 publish-public）")
    expect_json = (root / CONTRACT_JSON_RELPATH).read_bytes()
    actual_json = (root / PUBLIC_VIEW_JSON_RELPATH).read_bytes()
    if actual_json != expect_json:
        raise Cli12Error(
            "PUBLIC_VIEW_DRIFT",
            "public/public_contract.json 与 contract/public_contract.json "
            "不是逐字节相等（重跑 publish-public）")
    expect_md = (root / CONTRACT_MD_RELPATH).read_bytes()
    if (root / PUBLIC_VIEW_MD_RELPATH).read_bytes() != expect_md:
        raise Cli12Error(
            "PUBLIC_VIEW_DRIFT",
            "public/public_contract.md 与 contract/public_contract.md "
            "不是逐字节相等（重跑 publish-public）")
    if _sha256_bytes(actual_json) != str(envelope.get("public_contract_sha256")):
        raise Cli12Error(
            "PUBLIC_CONTRACT_TAMPERED",
            "public/public_contract.json 哈希与 envelope 绑定值不符")


def cmd_publish_public(args: argparse.Namespace) -> int:
    """把 ``contract/`` 的唯一源公开合同逐字节投影到候选可见面 ``public/``。

    生产链（``protocol._seed_public`` / ``assembly._design_inputs``）读的是
    ``public/public_contract.json``；R1.2 的权威件在 ``contract/``。发布是
    **派生投影**：源件哈希必须与 envelope 绑定值一致，投影逐字节相等，
    不重新渲染、不夹带私有信息。幂等（重复发布结果不变）。
    """
    root = _resolve_root(args.root, args.envelope)
    envelope = _load_envelope(root)
    result = _publish_public_view(root, envelope)
    print(f"publish-public ok  envelope={envelope['envelope_id']}")
    print(f"  contract sha256 : {str(envelope.get('public_contract_sha256'))[:16]}…")
    print(f"  public json     : {PUBLIC_VIEW_JSON_RELPATH}  "
          f"sha256={result['json_sha256'][:16]}…")
    print(f"  public md       : {PUBLIC_VIEW_MD_RELPATH}  "
          f"sha256={result['md_sha256'][:16]}…")
    return 0


def cmd_freeze(args: argparse.Namespace) -> int:
    """冻结 validation LOCK（不可变）并把 root 推到 FROZEN。

    三签不在这一步：验证 LOCK 的签名绑定对象（离线验证报告）要等演练跑完
    才存在，见 ``lock.verify_lock(require_signatures=...)`` 的引导期说明。
    """
    root = _resolve_root(args.root, args.envelope)
    envelope = _load_envelope(root)
    if envelope.get("mode") != "offline_validation":
        raise Cli12Error(
            "OFFLINE_MODE_REQUIRED",
            f"freeze 只用于离线验证根（envelope.mode={envelope.get('mode')!r}）")
    ctx = _r12_ctx(root, str(envelope["envelope_id"]))
    state = ST.ResearchState(ctx)
    if state.root_state() != ST.ROOT_CALIBRATED:
        raise Cli12Error("ROOT_STATE",
                         f"freeze 需要 root={ST.ROOT_CALIBRATED}，"
                         f"当前 {state.root_state()}（先跑 controls --real-containers）")
    # 冻结前公开视图必须已在候选可见面发布且与唯一源逐字节一致：
    # 生产链（_seed_public/_design_inputs）只读 public/，缺件在 freeze 就拦。
    _verify_public_view(root, envelope)
    frozen = LK.freeze_lock(ctx, repo_root=LK.REPO_ROOT,
                            image_pin=args.image)
    state.advance_root(ST.ROOT_FROZEN, reason="validation LOCK 已冻结（零 POST）")
    print(f"freeze ok  lock_sha256={frozen['sha256']}")
    print(f"  purpose : {LK.lock_purpose(frozen['doc'])}")
    print(f"  roles   : {list(LK.VALIDATION_ROLES)}（演练跑完后签）")
    print(f"  root    : {state.root_state()}")
    return 0


def cmd_sign(args: argparse.Namespace) -> int:
    root = _resolve_root(args.root, args.envelope)
    envelope = _load_envelope(root)
    ctx = _r12_ctx(root, str(envelope["envelope_id"]))
    doc = LK.sign_lock(ctx, role=str(args.role), actor=str(args.actor),
                       decision=str(args.decision),
                       rationale=str(args.rationale or ""))
    print(f"sign ok  role={doc['role']}  actor={doc['actor']}  "
          f"decision={doc['decision']}")
    print(f"  report_sha256         : {str(doc.get('report_sha256'))[:16]}…")
    print(f"  implementation_sha256 : "
          f"{str(doc.get('implementation_sha256'))[:16]}…")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    root = _resolve_root(args.root, args.envelope)
    envelope = _load_envelope(root)
    ctx = _r12_ctx(root, str(envelope["envelope_id"]))
    result = LK.verify_lock(ctx, repo_root=LK.REPO_ROOT)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


def cmd_state(args: argparse.Namespace) -> int:
    """操作者显式推进 root（只有 AUTHORIZED/CALIBRATED 两档；FROZEN 走 freeze）。"""
    root = _resolve_root(args.root, args.envelope)
    envelope = _load_envelope(root)
    ctx = _r12_ctx(root, str(envelope["envelope_id"]))
    state = ST.ResearchState(ctx)
    target = str(args.to).upper()
    if target not in (ST.ROOT_AUTHORIZED, ST.ROOT_CALIBRATED):
        raise Cli12Error("STATE_OPERATOR_SCOPE",
                         f"操作者只能推进 {[ST.ROOT_AUTHORIZED, ST.ROOT_CALIBRATED]}"
                         f"（FROZEN 由 freeze 推）")
    if state.root_state() == target:
        print(f"state ok  root={target}（已是目标，幂等）")
        return 0
    state.advance_root(target, reason=args.reason or f"操作者推进 → {target}")
    print(f"state ok  {target}")
    return 0


def _local_counter() -> "Any":
    """离线用的本地 token 估算（只过 20000 token 请求门，不参与计费/判决）。"""
    def count(text: str) -> int:
        return max(1, len(text) // 4)
    return count


def cmd_regression_attest(args: argparse.Namespace) -> int:
    """真跑失败矩阵登记的回归测试，落绑定冻结闭包的取证件（零 POST）。

    必须在 ``freeze`` 之后、``validate-production`` 之前跑：先 ``verify_closure``
    证明当前代码就是 LOCK 冻结的那一份，再跑测试，取证件的 ``closure_sha256``
    直接取 LOCK 闭包值。演练面只认闭包相等且 returncode==0 的取证件。
    """
    root = _resolve_root(args.root, args.envelope)
    envelope = _load_envelope(root)
    if envelope.get("mode") != "offline_validation":
        raise Cli12Error("OFFLINE_MODE_REQUIRED",
                         f"envelope.mode={envelope.get('mode')!r}")
    ctx = _r12_ctx(root, str(envelope["envelope_id"]))
    lock_doc = ctx.read_json(LK.LOCK_RELPATH)
    closure = lock_doc.get("closure") or {}
    # 组表口径与 ``freeze_lock`` 同源：取 envelope 的 profile_id（不硬编码 R12），
    # 否则 legacy 根在此处会拿错组表、把「代码没变」误判成闭包漂移。
    profile_id = str(envelope.get("profile_id") or P.PROFILE_LEGACY)
    groups_key = P.profile_by_id(profile_id).closure_groups_key
    LK.verify_closure(closure, LK.REPO_ROOT, groups_key=groups_key)
    closure_sha = str(closure.get("closure_sha256") or "")
    if not closure_sha:
        raise Cli12Error("CLOSURE_MISSING", "LOCK.closure.closure_sha256 缺失")
    doc = OV.run_regression_attestation(
        ctx, closure_sha256=closure_sha, python=args.python)
    failed = [f for f, rec in (doc.get("files") or {}).items()
              if int(rec.get("failed") or 0) or int(rec.get("passed") or 0) == 0]
    print(f"regression-attest {'ok' if not failed else 'FAIL'}  "
          f"closure={closure_sha[:16]}…  rc={doc['returncode']}")
    for rel, rec in sorted((doc.get("files") or {}).items()):
        print(f"  {rel:44s} passed={rec.get('passed')} "
              f"failed={rec.get('failed')} skipped={rec.get('skipped')}")
    print(f"  evidence: {OV.REGRESSION_EVIDENCE_RELPATH}")
    return 0 if (doc["returncode"] == 0 and not failed) else 3


def cmd_validate_production(args: argparse.Namespace) -> int:
    """零 POST 全链演练：正式 CLI/装配/DockerRunner + 公开消费者。

    拒绝在付费闸打开的状态下运行（``SIMPRAXIS_ALLOW_PAID=1`` 一律 BLOCKED）：
    本模式永远不构造生产 gateway，模型面只允许确定性公开消费者/脚本回合。
    """
    if os.environ.get("SIMPRAXIS_ALLOW_PAID") == "1":
        raise Cli12Error(
            "OFFLINE_MODE_REFUSES_PAID",
            "validate-production 禁止在 SIMPRAXIS_ALLOW_PAID=1 下运行")
    root = _resolve_root(args.root, args.envelope)
    envelope = _load_envelope(root)
    if envelope.get("mode") != "offline_validation":
        raise Cli12Error("OFFLINE_MODE_REQUIRED",
                         f"envelope.mode={envelope.get('mode')!r}")
    ctx = _r12_ctx(root, str(envelope["envelope_id"]))
    lock_doc = ctx.read_json(LK.LOCK_RELPATH)
    # 演练前置（独立评审）：先在**当前树**上复算闭包——改过代码没重建 LOCK 时
    # 立刻拒（几秒），而不是跑满 40 分钟再在签名面发现。组表口径取 envelope 的
    # profile_id（与 ``freeze_lock`` 同源），不硬编码 R12。
    _groups_key = P.profile_by_id(
        str(envelope.get("profile_id") or P.PROFILE_LEGACY)).closure_groups_key
    LK.verify_closure(lock_doc.get("closure") or {}, LK.REPO_ROOT,
                      groups_key=_groups_key)
    locked_pin = str((lock_doc.get("image") or {}).get("pin") or "")
    image = str(args.image or locked_pin or "")
    if not image:
        raise Cli12Error("IMAGE_PIN_INVALID", "LOCK 未登记镜像 pin 且未给 --image")
    # 锁内适配（2026-11-12 T5 独立评审 MAJOR-4）：`--image` 不是自由覆盖——
    # 冻结镜像 pin 必须与实际运行镜像逐字相等（run_validation 里还有第二道同判）。
    if locked_pin and image != locked_pin:
        raise Cli12Error("IMAGE_MISMATCH",
                         f"--image {image} ≠ LOCK {locked_pin}")
    runs = OS.run_validation(
        ctx, image=image, runner_factory=SANDBOX.DockerRunner,
        counter=_local_counter(), ledger_path=_ledger_path(root),
        repo_root=LK.REPO_ROOT, only=args.only)
    authorization = OV.load_authorization(ctx)
    reasons = [f"{k}: {v.get('expected')} → {v.get('status')}"
               for k, v in runs["suites"].items() if v.get("status") != "PASS"]
    missing = list(runs.get("missing_suites") or [])
    if missing:
        reasons.append(f"未跑套件（--only 子集不得作为验收报告）：{missing}")
    verdict = str(runs["verdict"])
    # MINOR-3：零 POST 用**报告生成时实测**的账本行数，不再抄调用方字面量；
    # 缺键/非严格 int 一律拒（`or 0` 会把缺席读成零 POST 证据）。
    measured_rows = runs.get("ledger_call_rows")
    if isinstance(measured_rows, bool) or not isinstance(measured_rows, int):
        raise Cli12Error("VALIDATION_REPORT_INCOMPLETE",
                         f"run_validation 未给出实测 ledger_call_rows："
                         f"{measured_rows!r}")
    reasons.extend(str(r) for r in (runs.get("reasons") or []))
    doc = OV.report_document(
        ctx=ctx, authorization=authorization,
        lock_sha256=str(runs["lock_sha256"]), suites=runs["suites"],
        turns=runs.get("turn_entries") or [], verdict=verdict,
        reasons=reasons, ledger_call_rows=measured_rows,
        suites_required=runs.get("suites_required") or [],
        suites_run=runs.get("suites_run") or [],
        complete=bool(runs.get("complete")),
        profile_sha256=str(runs.get("profile_sha256") or ""),
        image=str(runs.get("image") or image),
        failure_matrix=runs.get("failure_matrix"),
        suite_spec_sha256=str(runs.get("suite_spec_sha256") or ""))
    doc["signatures_required"] = False   # 本报告是签名绑定对象，先于三签产生
    report_sha = OV.write_report(ctx, doc)
    print(f"validate-production {verdict}  envelope={envelope['envelope_id']}")
    for name, row in runs["suites"].items():
        print(f"  {name:32s} {row.get('status')}")
    print(f"  report : {OV.REPORT_RELPATH}  sha256={report_sha[:16]}…")
    print(f"  turns  : {len(doc['turns']['request_sha256'])}  "
          f"ledger_call_rows={doc['ledger_call_rows']}")
    return 0 if verdict == "PASS" else 3


def main(argv: "list[str] | None" = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (Cli12Error, C.ContractError, FAM.FamilyError, P.ProfileError,
            RP.RsiR12CampaignUnsetError, HE.HumanEventError,
            RT.CampaignWriteRefused, OSError, CAL.CalibrationError,
            LK.LockError, OV.OfflineValidationError, OS.OfflineSuiteError,
            PREREG.PreregError, ST.StateError, RS.ResourceError) as exc:
        print(f"BLOCKED {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
