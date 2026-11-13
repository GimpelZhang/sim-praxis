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
from orchestrator.common.fsutil import (atomic_write_bytes, atomic_write_text,
                                        ensure_private_dir)
from orchestrator.gateway import quota_ledger as QL
from orchestrator.rsi_r1 import assembly as ASM
from orchestrator.rsi_r1 import calibration as CAL
from orchestrator.rsi_r1 import closeout as CO
from orchestrator.rsi_r1 import cli_r12_closeout as CL12C
from orchestrator.rsi_r1 import closeout_r12 as CO12
from orchestrator.rsi_r1 import closure as CLOSURE
from orchestrator.rsi_r1 import contracts as C
from orchestrator.rsi_r1 import family as FAM
from orchestrator.rsi_r1 import gateway_transport as GT
from orchestrator.rsi_r1 import human_events as HE
from orchestrator.rsi_r1 import lock as LK
from orchestrator.rsi_r1 import offline_suite as OS
from orchestrator.rsi_r1 import offline_validation as OV
from orchestrator.rsi_r1 import prereg as PREREG
from orchestrator.rsi_r1 import profiles as P
from orchestrator.rsi_r1 import protocol as PT
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
        # 锁内适配（2026-11-13，T6）：Plan §6.1 的顺序是「init(paid 根) 先、
        # authorize --consent 后」——T0–T5 离线工作期间费用门保持 BLOCKED。
        # 原实现在 init 阶段就拒「paid 无 consent」，等于逼着先有授权件再建
        # 根，与 Plan 顺序冲突。改为：init 只建 BLOCKED 门（零 POST），解锁
        # 必须走 ``authorize --consent``（见 ``cmd_authorize``）。
        cost_gate = {
            **cost_gate,
            "note": ("T6 费用门 BLOCKED：paid 根已建（零 POST），付费须先 "
                     "``authorize --consent <ConsentV2>`` 解锁"),
        }

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
    if getattr(args, "import_verified", None):
        return _import_verified_calibration(
            root=root, envelope=envelope, ctx=ctx,
            report_path=Path(args.import_verified))
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
# T6：费用门解锁 → 校准验签导入 → 付费资格检查（probe 12 / practice 16）
# ---------------------------------------------------------------------------

def _append_jsonl(path: Path, row: Mapping[str, Any]) -> None:
    """append-only 私有 jsonl（0600；只在尾部追加，整份原子重写）。"""
    ensure_private_dir(path.parent)
    old = path.read_text(encoding="utf-8") if path.is_file() else ""
    line = json.dumps(dict(row), ensure_ascii=False, sort_keys=True) + "\n"
    atomic_write_text(path, old + line, mode=0o600)


def cmd_authorize(args: argparse.Namespace) -> int:
    """T6 费用门：在已建 paid 根上写入/校验 ConsentV2 并推进 INIT→AUTHORIZED。

    Plan §6.1 的顺序是 init(paid 根) → …离线工作… → ``authorize --consent``。
    init 只落 BLOCKED 门；本命令是唯一解锁点，且只接受 R1.2 新 schema、绑定
    当前 Plan 字节的授权件（旧 R1/R0.x 豁免不承继）。幂等：同一授权件重复
    执行为 no-op；不同字节一律拒（不可覆盖）。
    """
    root = _resolve_root(args.root, args.envelope)
    envelope = _load_envelope(root, args.envelope)
    if envelope.get("mode") != "paid":
        raise Cli12Error("MODE_INVALID",
                         "authorize 只用于 paid 根（离线根用 authorize-offline）")
    consent = C.consent_from_dict(_read_json(Path(args.consent)))
    if not isinstance(consent, C.ConsentV2):
        raise Cli12Error("CONSENT_SCHEMA",
                         f"R1.2 只接受 {C.CONSENT_SCHEMA_V2}（旧 R1 豁免不承继）")
    if consent.envelope_id != envelope["envelope_id"]:
        raise Cli12Error("CONSENT_ENVELOPE",
                         f"consent.envelope_id={consent.envelope_id!r} 不符")
    C.verify_consent_binds_plan(consent, plan_path=str(envelope["plan_path"]),
                                plan_sha256=str(envelope["plan_sha256"]))
    if not consent.paid_authorized:
        raise Cli12Error("COST_GATE_BLOCKED",
                         f"consent.decision={consent.decision!r} 未解锁付费")
    consent_file = root / CONSENT_RELPATH
    new_sha = _sha256_bytes(_json_text(consent.to_dict()).encode("utf-8"))
    first = not consent_file.is_file()
    if not first:
        if _file_sha256(consent_file) != new_sha:
            raise Cli12Error("CONSENT_EXISTS",
                             f"{CONSENT_RELPATH} 已存在且字节不同（授权件不可覆盖）")
        consent_sha = new_sha
    else:
        consent_sha = _write_json(consent_file, consent.to_dict())
    gate = dict(envelope.get("cost_gate") or {})
    gate.update({
        "decision": consent.decision,
        "paid_authorized": True,
        "consent_path": CONSENT_RELPATH,
        "consent_sha256": consent_sha,
        "note": consent.exposure_note or consent.notes,
    })
    updated = dict(envelope)
    updated["cost_gate"] = gate
    C.validate_envelope_v2(updated)
    _write_json(root / ENVELOPE_RELPATH, updated)
    if first:
        _append_jsonl(root / "contract" / "cost_gate_history.jsonl", {
            "schema": "simpraxis.rsi_r12.cost_gate_event.v1",
            "schema_version": 1,
            "envelope_id": envelope["envelope_id"],
            "decision": consent.decision,
            "consent_sha256": consent_sha,
            "plan_sha256": consent.plan_sha256,
            "at_utc": _utc_now(),
        })
    ctx = _r12_ctx(root, str(envelope["envelope_id"]))
    state = ST.ResearchState(ctx)
    if state.root_state() == ST.ROOT_INIT:
        state.advance_root(ST.ROOT_AUTHORIZED,
                           reason="ConsentV2 费用门已解锁（零 POST）")
    print(f"authorize ok  envelope={envelope['envelope_id']}")
    print(f"  decision : {consent.decision}")
    print(f"  consent  : {CONSENT_RELPATH}  sha256={consent_sha[:16]}…")
    print(f"  plan_sha : {consent.plan_sha256[:16]}…")
    print(f"  root     : {state.root_state()}")
    return 0


def _import_verified_calibration(*, root: Path, envelope: Mapping[str, Any],
                                 ctx: "R12Context",
                                 report_path: Path) -> int:
    """T4/T6：把 T5 根已三签的 v2 校准件按原字节导入付费根（零 POST）。

    导入不是重跑：逐项核验来源根（验证 LOCK 全量验签、闭包/镜像/画像对当前
    源码）、报告必须是 T5 LOCK ``calibration`` 绑定的那一件；付费根只落
    **原字节 + 来源取证件**，不冒称自己跑过。幂等；不同源/不同字节拒绝覆盖。
    """
    if envelope.get("mode") != "paid":
        raise Cli12Error("MODE_INVALID",
                         "controls --import-verified 只用于 paid 根")
    provenance, raw = LK.build_calibration_import(
        _artefact_root(report_path), report_path=report_path)
    dest_rel = LK.CALIBRATION_REPORTS[0]      # contract/calibration_report_v2.json
    dest = root / dest_rel
    import_rel = "contract/calibration_import.json"
    if dest.is_file():
        if dest.read_bytes() != raw:
            raise Cli12Error("CALIBRATION_EXISTS",
                             f"{dest_rel} 已存在且字节不同（导入件不可覆盖）")
    else:
        ensure_private_dir(dest.parent)
        atomic_write_bytes(dest, raw, mode=0o600)
    existing = root / import_rel
    if existing.is_file():
        old = _read_json(existing)
        if {k: old.get(k) for k in ("validation_root", "report_sha256")} != {
                "validation_root": provenance["validation_root"],
                "report_sha256": provenance["report_sha256"]}:
            raise Cli12Error("CALIBRATION_IMPORT_EXISTS",
                             f"{import_rel} 已登记另一来源（不可覆盖）")
    else:
        _write_json(existing, provenance)
    state = ST.ResearchState(ctx)
    current = state.root_state()
    if current not in (ST.ROOT_AUTHORIZED, ST.ROOT_CALIBRATED):
        raise Cli12Error("ROOT_STATE",
                         f"导入校准件需要 root ∈ {{AUTHORIZED, CALIBRATED}}，"
                         f"当前 {current}（先 authorize --consent）")
    if current == ST.ROOT_AUTHORIZED:
        state.advance_root(
            ST.ROOT_CALIBRATED,
            reason=(f"v2 校准验签导入（源 {provenance['validation_root']}，"
                    f"零 POST）"))
    print(f"controls import ok  envelope={envelope['envelope_id']}")
    print(f"  source root : {provenance['validation_root']}")
    print(f"  source lock : {provenance['validation_lock_sha256'][:16]}…")
    print(f"  report      : {dest_rel}  sha256={provenance['report_sha256'][:16]}…")
    print(f"  verdict     : {provenance['report_verdict']}")
    print(f"  root        : {state.root_state()}")
    return 0


def _tokenizer_counter(path: str, sha256_hex: str):
    """本地 token 计数（只用于 20000 token 请求门；不参与任何科学判决）。"""
    raw = Path(path).read_bytes()
    got = _sha256_bytes(raw)
    if got != sha256_hex:
        raise Cli12Error("TOKENIZER_DRIFT",
                         f"tokenizer sha256 不符：{path} 实为 {got}")
    from tokenizers import Tokenizer  # 本地文件；不联网

    tok = Tokenizer.from_file(str(path))

    def count(text: str) -> int:
        return len(tok.encode(text, add_special_tokens=False).ids)

    return count


def _paid_gate(root: Path, envelope: Mapping[str, Any]) -> "C.ConsentV2":
    """付费前置闸：mode/费用门/授权件字节/Plan 绑定逐项复核（不触网）。"""
    if envelope.get("mode") != "paid":
        raise Cli12Error("MODE_INVALID",
                         "付费命令只在 paid 根上运行（离线根用 validate-production）")
    gate = envelope.get("cost_gate") or {}
    if not gate.get("paid_authorized"):
        raise Cli12Error("COST_GATE_BLOCKED",
                         f"费用门 decision={gate.get('decision')!r}；"
                         "先 authorize --consent")
    consent_file = root / CONSENT_RELPATH
    if not consent_file.is_file():
        raise Cli12Error("CONSENT_MISSING", CONSENT_RELPATH)
    if _file_sha256(consent_file) != gate.get("consent_sha256"):
        raise Cli12Error("CONSENT_DRIFT",
                         "consent 字节与 envelope.cost_gate.consent_sha256 不符")
    consent = C.consent_from_dict(_read_json(consent_file))
    if not isinstance(consent, C.ConsentV2):
        raise Cli12Error("CONSENT_SCHEMA", "R1.2 只接受 ConsentV2")
    C.verify_consent_binds_plan(consent, plan_path=str(envelope["plan_path"]),
                                plan_sha256=str(envelope["plan_sha256"]))
    return consent


def _run_protocol_cmd(args: argparse.Namespace, lane: str) -> int:
    """T6 唯一付费入口：环境闸 → 费用门 → 批准件 → 网关 → 单链 → 报告。

    与旧 R1 的 probe/practice 同一纪律，但走 R1.2 画像（12/16 帽、单链）、
    2904 账套与 R1.2 根布局；``--repair`` 在 R1.2 没有入口（``run_protocol``
    的 bounded 语义直接拒非本 lane 记账）。
    """
    root = _resolve_root(args.root, args.envelope)
    envelope = _load_envelope(root, args.envelope)
    # 环境闸先于一切副作用（含批准件写入与 tokenizer 加载）：
    # 没有 SIMPRAXIS_ALLOW_PAID=1 就不动作。
    GT.require_paid_env(os.environ)
    batch_id = str(args.batch_id or "").strip()
    if not batch_id:
        raise Cli12Error("BATCH_MISSING", "--batch-id 不得为空")
    if not args.tokenizer or not args.tokenizer_sha256:
        raise Cli12Error("TOKENIZER_REQUIRED",
                         "付费运行需要 --tokenizer 与 --tokenizer-sha256")
    consent = _paid_gate(root, envelope)
    profile = P.resolve_profile(envelope)
    envelope_id = str(envelope["envelope_id"])
    if root.resolve(strict=False) != RP.rsi_r12_campaign_root().resolve(strict=False):
        raise Cli12Error(
            "CAMPAIGN_ENV_MISMATCH",
            f"根 {root} 与 SIMPRAXIS_RSI_R12_CAMPAIGN 解析的 "
            f"{RP.rsi_r12_campaign_root()} 不一致（账本路径必须与根同源）")
    ctx = _r12_ctx(root, envelope_id)
    counter = _tokenizer_counter(args.tokenizer, args.tokenizer_sha256)
    cap = int(profile.lane_post_caps[lane])
    approval = PT.ensure_approval(
        ctx, batch_id=batch_id, lane=lane, approved=cap,
        note=args.note or "", namespace=QL.RSI_R12_NAMESPACE)
    lock_doc = _read_json(root / LK.LOCK_RELPATH)
    image = args.image or str((lock_doc.get("image") or {}).get("pin") or "")
    if not image:
        raise Cli12Error("IMAGE_PIN_INVALID", "缺 --image 且 LOCK 未登记镜像 pin")
    spend = ASM.SpendContract(mode=ASM.MODE_PAID, consent=consent)
    gateway = GT.build_gateway(
        ledger=QL.QuotaLedger.rsi_r12(),
        pause_state_path=RP.rsi_r12_gateway_state_path())
    transport = GT.build_transport(
        gateway=gateway, lane=lane, batch_id=batch_id,
        thread_id=args.thread_id or f"rsi-r12-{lane}", env=os.environ)
    report = PT.run_protocol(
        ctx, lane=lane, spend=spend, transport_factory=lambda: transport,
        runner=SANDBOX.DockerRunner(), image=image, batch_id=batch_id,
        counter=counter, profile=profile,
        resource_ledger=RS.ResourceLedger(root, profile=profile))
    posts = report.get("posts") or {}
    print(f"{lane} {report['verdict']}  attempt={report['attempt']}  "
          f"posts={posts.get('this_attempt')}/{posts.get('allowed')}  "
          f"root={report['root_after']}")
    if report.get("reason"):
        print(f"  reason: {report['reason']}")
    for study in report.get("studies") or []:
        print(f"  study {study.get('study')}: {study.get('ok_count')}/8 ok  "
              f"stopped={study.get('stopped') or '-'}  "
              f"state={study.get('study_state')}")
    print(f"  approval={approval}")
    print(f"  report={ctx.relative(f'protocol/report_{lane}.json')}")
    # D-2 修复（2026-11-13）：付费路径自己把已用 POST 记进族账，不让「谁忘了
    # 调 record_usage」变成跨轮账目缺口。以账本 call 行为准（不是 report 自述），
    # 单调且幂等；记账失败不阻断已完成的研究动作，但必须原样打出来。
    try:
        calls = int(CO.ledger_facts(CO._rows(ctx.root))["calls"])
        FAM.record_usage(_family_root(), envelope_id, calls)
        print(f"  family : posts_used={calls}（按账本 call 行补记）")
    except FAM.FamilyError as exc:
        print(f"  WARN family usage not recorded: "
              f"{getattr(exc, 'code', '')} {exc}", file=sys.stderr)
    return 0 if report["verdict"] == PT.VERDICT_PASS else 2


def cmd_probe(args: argparse.Namespace) -> int:
    return _run_protocol_cmd(args, PT.PROBE)


def cmd_practice(args: argparse.Namespace) -> int:
    return _run_protocol_cmd(args, PT.PRACTICE)


# ---------------------------------------------------------------------------
# family（换轮）：关闭离线验证轮 → 释放 124 预留 → 付费轮才能登记
# ---------------------------------------------------------------------------

def cmd_family_migrate_cap(args: argparse.Namespace) -> int:
    """把族帽抬到当前常量（零 POST、只增不减、幂等、留痕）。

    锁内适配（2026-11-13，用户逐字裁决 155→160）：上一轮以仪器缺陷结案时
    已真实花掉 5 呼；新口径「族帽占用 = 历史 + 已关闭轮实花 + 活动预留」下，
    155 无法容纳 31+5+124，故抬到 160。迁移只改族文档（含 ``cap_history``
    与旧成员 ``family_cap_at_registration`` 回填），不碰任何根、不产生 POST。
    """
    reason = str(getattr(args, "reason", "") or "").strip()
    if not reason:
        raise Cli12Error("FAMILY_CAP_REASON_REQUIRED", "迁移必须有 --reason")
    family_root = _family_root()
    decision = str(getattr(args, "decision", "") or "").strip()
    doc = FAM.migrate_cap(
        family_root, reason=reason,
        evidence={"decision": decision} if decision else {})
    usage = FAM.usage(family_root)
    print(f"family migrate-cap ok  cap={doc['family_cap']}  "
          f"reserved_total={usage['reserved_total']}  "
          f"remaining={usage['remaining']}")
    for entry in doc.get("cap_history") or []:
        print(f"  cap_history {entry['from_cap']}→{entry['to_cap']} "
              f"{entry['at_utc']}  {entry['reason']}")
    return 0


def cmd_family_close(args: argparse.Namespace) -> int:
    """关闭离线验证轮（Plan §5.2/§6.1 的换轮闸；零 POST）。

    只允许 ``mode=offline_validation`` 的根：付费根关闭走 T9 closeout，不在
    本命令范围。关闭前现场全量复核 validation LOCK（闭包/依赖/校准/报告
    三签，``verify_lock``）且验证报告 verdict=PASS；随后 root→CLOSED 并
    ``family.close_member`` 释放该成员 124 预留（族帽占用回到 31）。
    幂等：成员已 closed 时重复执行 no-op。
    """
    root = _resolve_root(args.root, args.envelope)
    envelope = _load_envelope(root, args.envelope)
    mode = envelope.get("mode")
    reason = getattr(args, "reason", None)
    if not isinstance(reason, str) or not reason.strip():
        raise Cli12Error("FAMILY_CLOSE_REASON_REQUIRED", "关闭必须有 --reason")
    ctx = _r12_ctx(root, str(envelope["envelope_id"]))
    state = ST.ResearchState(ctx)
    cur = state.root_state()
    if mode != "offline_validation":
        # 锁内适配（2026-11-13，用户裁决全量重跑）：付费轮经 T9 closeout 关根后，
        # 族成员仍停在 registered，会把下一轮挡在 FAMILY_ROUND_OPEN。付费轮只
        # 接受「根已 CLOSED + T9 readout 在盘」作为证据（不再复核 LOCK——付费
        # LOCK 的闭包面已被 T9 工具演进触发 CLOSURE_DRIFT，复核会误报）。
        if cur != ST.ROOT_CLOSED:
            raise Cli12Error(
                "FAMILY_CLOSE_SCOPE",
                f"付费轮须先由 T9 closeout 关根（root_state={cur}）；"
                "离线验证根才走 LOCK+报告复核路径")
        readout_path = ctx.root / CO12.READOUT_RELPATH
        if not readout_path.exists():
            raise Cli12Error("FAMILY_CLOSE_NO_READOUT",
                             f"缺 {CO12.READOUT_RELPATH}，不得关闭付费成员")
        readout_sha = _sha256_bytes(readout_path.read_bytes())
        fam_doc = FAM.close_member(
            _family_root(), str(envelope["envelope_id"]), reason=reason,
            evidence={
                "mode": str(mode),
                "root_state_at_close": cur,
                "readout_relpath": CO12.READOUT_RELPATH,
                "readout_sha256": readout_sha,
            })
        member = next(m for m in fam_doc["members"]
                      if m["envelope_id"] == envelope["envelope_id"])
        print(f"family close ok  envelope={envelope['envelope_id']}（付费轮）")
        print(f"  state  : {member.get('state')}  "
              f"closed_at={member.get('closed_at_utc')}")
        print(f"  readout: {readout_sha[:16]}…")
        print(f"  family : history={fam_doc['history']['total_call_rows']} + "
              f"closed_used={member.get('posts_used')} / "
              f"cap={fam_doc['family_cap']}")
        return 0
    if getattr(args, "abandon", False):
        # 显式放弃失败的离线演练根（2026-11-13 锁内适配）：演练在根内消耗了
        # aux 步数帽，重跑必须换新根；但失败根既不能走「报告 PASS 才关闭」，
        # 也不能占着 124 预留把新根挡在族帽外。只有「报告非 PASS 且零签名」
        # 的根可放弃；证据按报告 sha 与签名目录留档，不冒充通过。
        report_path = ctx.root / OV.REPORT_RELPATH
        report = _read_json(report_path) if report_path.exists() else None
        if report is None:
            raise Cli12Error("FAMILY_ABANDON_NO_REPORT",
                             "放弃失败演练根需要已有的 validation 报告")
        if str(report.get("verdict")) == "PASS":
            raise Cli12Error("FAMILY_ABANDON_PASSED",
                             "报告 PASS 的根不得按「放弃」关闭（走三签流程）")
        sig_dir = ctx.root / LK.VALIDATION_SIG_DIR
        if sig_dir.is_dir() and any(sig_dir.glob("*.json")):
            raise Cli12Error("FAMILY_ABANDON_SIGNED",
                             "已有签名的根不得按「放弃」关闭")
        report_sha = _sha256_bytes(report_path.read_bytes())
        if cur != ST.ROOT_CLOSED:
            state.close_root(reason=f"family abandon：{reason}")
        fam_doc = FAM.close_member(
            _family_root(), str(envelope["envelope_id"]), reason=reason,
            evidence={
                "abandoned": True,
                "mode": str(mode),
                "report_relpath": OV.REPORT_RELPATH,
                "report_sha256": report_sha,
                "report_verdict": report.get("verdict"),
                "signatures_present": False,
            })
        member = next(m for m in fam_doc["members"]
                      if m["envelope_id"] == envelope["envelope_id"])
        print(f"family close ok  envelope={envelope['envelope_id']}（放弃失败演练根）")
        print(f"  state  : {member.get('state')}  "
              f"report_verdict={report.get('verdict')}  sha={report_sha[:16]}…")
        print(f"  family : history={fam_doc['history']['total_call_rows']} + "
              f"closed_used={member.get('posts_used')} / "
              f"cap={fam_doc['family_cap']}")
        return 0

    if cur != ST.ROOT_CLOSED:
        # 现场复核：LOCK 必须是最新的 validation LOCK 且三签齐（闭包零漂移）。
        ver = LK.verify_lock(ctx, repo_root=LK.REPO_ROOT)
        if ver.get("purpose") != "validation":
            raise Cli12Error("FAMILY_CLOSE_LOCK_PURPOSE",
                             f"LOCK purpose={ver.get('purpose')!r} 非 validation")
        report = _read_json(ctx.root / OV.REPORT_RELPATH)
        if str(report.get("verdict")) != "PASS":
            raise Cli12Error(
                "FAMILY_CLOSE_REPORT_NOT_PASS",
                f"验证报告 verdict={report.get('verdict')!r}，不得关闭换轮")
        state.close_root(reason=f"family close：{reason}")
    lock_doc = LK.load_frozen(ctx)
    report_sha = _sha256_bytes((ctx.root / OV.REPORT_RELPATH).read_bytes())
    fam_doc = FAM.close_member(
        _family_root(), str(envelope["envelope_id"]), reason=reason,
        evidence={
            "mode": str(envelope["mode"]),
            "root_state_at_close": ST.ResearchState(ctx).root_state(),
            "lock_sha256": lock_doc.sha256,
            "report_relpath": OV.REPORT_RELPATH,
            "report_sha256": report_sha,
        })
    member = next(m for m in fam_doc["members"]
                  if m["envelope_id"] == envelope["envelope_id"])
    reserved_active = sum(int(m["posts_reserved"]) for m in fam_doc["members"]
                          if m.get("state") != FAM.MEMBER_CLOSED)
    print(f"family close ok  envelope={envelope['envelope_id']}")
    print(f"  state  : {member.get('state')}  closed_at={member.get('closed_at_utc')}")
    print(f"  lock   : {lock_doc.sha256[:16]}…  report: {report_sha[:16]}…")
    print(f"  family : history={fam_doc['history']['total_call_rows']} + "
          f"active_reserved={reserved_active} / cap={fam_doc['family_cap']}")
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
    pctl.add_argument("--import-verified", default=None, metavar="REPORT",
                      help="T6：导入 T5 根已三签的 v2 校准件（验签+闭包复核，"
                           "零 POST；只用于 paid 根）")
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

    pfr = sub.add_parser("freeze", help="冻结 LOCK（validation / paid 两种目的）")
    _root_args(pfr)
    pfr.add_argument("--image", default=None,
                     help="镜像 pin（缺省取校准报告的 container_image）")
    pfr.add_argument("--purpose", default=None, choices=list(LK.PURPOSES),
                     help="T6：paid 根冻结固定 paid（须配 --validation-report）")
    pfr.add_argument("--validation-report", default=None, metavar="PATH",
                     help="T6：T5 根的 offline_validation_report.json（单向绑定"
                          " T5 LOCK/报告/三签；paid 冻结必填）")
    pfr.set_defaults(func=cmd_freeze)

    pra = sub.add_parser(
        "regression-attest",
        help="真跑 T2/T3 回归测试，落绑定冻结闭包的取证件（零 POST）")
    _root_args(pra)
    pra.add_argument("--python", default=None,
                     help="解释器（缺省 sys.executable）")
    pra.set_defaults(func=cmd_regression_attest)

    psg = sub.add_parser("sign",
                         help="签 LOCK（validation 三签 / paid 双签；绑定实现与报告）")
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

    # ---- 换轮：关闭离线验证轮（释放 124 预留；付费轮登记前必经）----
    pfam = sub.add_parser("family", help="族账操作（换轮：close 离线验证成员）")
    fam_sub = pfam.add_subparsers(dest="family_cmd", required=True)
    pfc = fam_sub.add_parser(
        "close", help="关闭离线验证轮（核 LOCK 三签+报告 PASS，root→CLOSED）")
    _root_args(pfc)
    pfc.add_argument("--reason", required=True)
    pfc.add_argument(
        "--abandon", action="store_true",
        help="显式放弃一次**未通过**的离线演练根（报告非 PASS、零签名），"
             "释放其未用预留换新轮重来；不得用于通过过的根")
    pfc.set_defaults(func=cmd_family_close)
    pmc = fam_sub.add_parser(
        "migrate-cap",
        help="把族帽抬到当前常量（只增不减、留痕；2026-11-13 用户裁决 155→160）")
    pmc.add_argument("--reason", required=True)
    pmc.add_argument("--decision", default="",
                     help="用户裁决引用（原文/出处），写入 cap_history")
    pmc.set_defaults(func=cmd_family_migrate_cap)

    # ---- T6：费用门 → 校准导入 → 唯一付费资格检查（probe/practice）----
    pau = sub.add_parser("authorize",
                         help="T6：写入 ConsentV2 费用授权（INIT → AUTHORIZED）")
    _root_args(pau)
    pau.add_argument("--consent", required=True, help="ConsentV2 JSON")
    pau.set_defaults(func=cmd_authorize)

    for _name, _func, _help in (
            ("probe", cmd_probe,
             "T6：probe 链（≤12 POST，单链有界自修；须 SIMPRAXIS_ALLOW_PAID=1）"),
            ("practice", cmd_practice,
             "T6：practice 链（≤16 POST，同根依次；须 SIMPRAXIS_ALLOW_PAID=1）")):
        _p = sub.add_parser(_name, help=_help)
        _root_args(_p)
        _p.add_argument("--batch-id", required=True)
        _p.add_argument("--tokenizer", default=None, help="本地 tokenizer 文件")
        _p.add_argument("--tokenizer-sha256", default=None)
        _p.add_argument("--thread-id", default=None)
        _p.add_argument("--image", default=None,
                        help="镜像 pin（缺省取 LOCK 内 pin）")
        _p.add_argument("--note", default="")
        _p.set_defaults(func=_func)

    for _name, _study, _help in (
            ("study_A", "A",
             "T7：正式 study A（≤48 POST，真实物理；须 SIMPRAXIS_ALLOW_PAID=1）"),
            ("study_B", "B",
             "T7：正式 study B（≤48 POST，须先完成 A；须 SIMPRAXIS_ALLOW_PAID=1）")):
        _ps = sub.add_parser(_name, help=_help)
        _root_args(_ps)
        _ps.add_argument("--batch-id", required=True)
        _ps.add_argument("--tokenizer", default=None, help="本地 tokenizer 文件")
        _ps.add_argument("--tokenizer-sha256", default=None)
        _ps.add_argument("--thread-id", default=None)
        _ps.add_argument("--image", default=None,
                         help="镜像 pin（缺省取 LOCK 内 pin）")
        _ps.add_argument("--note", default="")
        _ps.set_defaults(func=cmd_study, study=_study)

    prp = sub.add_parser(
        "replay",
        help="T8：独立复跑（A/B 各一次；零模型 POST + 真容器）")
    _root_args(prp)
    prp.add_argument("--id", dest="replay_id", required=True, choices=["A", "B"])
    prp.add_argument("--batch-id", required=True,
                     help="复跑观测批次名（新批次；不得与既有批次重名）")
    prp.add_argument("--real-containers", action="store_true",
                     help="显式确认真跑容器（无模型 POST）")
    prp.add_argument("--max-steps", type=int, default=None)
    prp.add_argument("--image", default=None,
                     help="容器镜像 name@sha256:<64>（缺省取 LOCK 登记值）")
    prp.set_defaults(func=cmd_replay)

    pvp = sub.add_parser(
        "validate-production",
        help="零 POST 全链演练：正式 CLI/装配/DockerRunner + 公开消费者")
    _root_args(pvp)
    pvp.add_argument("--image", default=None, help="镜像 pin（缺省取 LOCK 内 pin）")
    pvp.add_argument("--only", nargs="*", default=None,
                     help="只跑指定套件（调试用；子集报告判 INCOMPLETE、"
                          "不得签署——六套全跑才算验收）")
    pvp.set_defaults(func=cmd_validate_production)

    # T9 收尾命令在冻结面之外（见 cli_r12_closeout 模块 docstring）
    CL12C.register(sub)
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


def _artefact_root(path: Path) -> Path:
    """由「根内产物」路径反推 campaign 根（最近的含 envelope 的祖先）。"""
    target = path.resolve(strict=False)
    if not target.is_file():
        raise Cli12Error("FILE_ABSENT", f"文件不存在：{target}")
    for parent in target.parents:
        if (parent / ENVELOPE_RELPATH).is_file():
            return parent
    raise Cli12Error("ROOT_UNRESOLVED", f"{target} 不在任何 R1.2 根内")


def cmd_freeze(args: argparse.Namespace) -> int:
    """冻结 LOCK（不可变）并把 root 推到 FROZEN。

    离线根 → ``purpose=validation``（原 T5 行为，逐字节不变）；R1.2 付费根 →
    ``purpose=paid`` **且必须** ``--validation-report <t5-report>``：冻结前按现场
    重算 T5 根（闭包/依赖对当前源码、报告与三签），把 T5 的 LOCK/报告/三签/
    实现身份单向绑进付费 LOCK（Plan L212/L247）。
    """
    root = _resolve_root(args.root, args.envelope)
    envelope = _load_envelope(root, args.envelope)
    mode = envelope.get("mode")
    purpose = getattr(args, "purpose", None)
    if purpose is not None and purpose not in LK.PURPOSES:
        raise Cli12Error("LOCK_PURPOSE", f"--purpose={purpose!r}")
    report_arg = getattr(args, "validation_report", None)
    if mode == "offline_validation":
        if purpose not in (None, "validation"):
            raise Cli12Error("LOCK_PURPOSE",
                             f"离线根只能冻结 validation（收到 {purpose!r}）")
        if report_arg:
            raise Cli12Error("VALIDATION_BINDING_NOT_PAID",
                             "离线根不接受 --validation-report（那是付费冻结的绑定面）")
    else:
        if purpose not in (None, "paid"):
            raise Cli12Error("LOCK_PURPOSE",
                             f"付费根只能冻结 paid（收到 {purpose!r}）")
        if not report_arg:
            raise Cli12Error(
                "PAID_BINDING_MISSING",
                "R1.2 付费冻结必须给 --validation-report <t5-report>"
                "（绑定 T5 验证报告与三签，Plan L247）")
    ctx = _r12_ctx(root, str(envelope["envelope_id"]))
    state = ST.ResearchState(ctx)
    if state.root_state() != ST.ROOT_CALIBRATED:
        raise Cli12Error("ROOT_STATE",
                         f"freeze 需要 root={ST.ROOT_CALIBRATED}，"
                         f"当前 {state.root_state()}（先跑 controls）")
    # 冻结前公开视图必须已在候选可见面发布且与唯一源逐字节一致：
    # 生产链（_seed_public/_design_inputs）只读 public/，缺件在 freeze 就拦。
    _verify_public_view(root, envelope)
    if mode == "offline_validation":
        frozen = LK.freeze_lock(ctx, repo_root=LK.REPO_ROOT,
                                image_pin=args.image, purpose="validation")
        reason = "validation LOCK 已冻结（零 POST）"
        binding = None
    else:
        t5_root = _artefact_root(Path(report_arg))
        binding = LK.build_validation_binding(t5_root)
        frozen = LK.freeze_lock(
            ctx, repo_root=LK.REPO_ROOT,
            image_pin=args.image or binding["image_pin"], purpose="paid",
            validation_binding=binding)
        reason = "付费 LOCK 已冻结（绑定 T5 报告与三签）"
    state.advance_root(ST.ROOT_FROZEN, reason=reason)
    print(f"freeze ok  lock_sha256={frozen['sha256']}")
    print(f"  purpose : {LK.lock_purpose(frozen['doc'])}")
    if binding is None:
        print(f"  roles   : {list(LK.VALIDATION_ROLES)}（演练跑完后签）")
    else:
        print(f"  T5 root : {binding['validation_root']}  "
              f"lock={binding['lock_sha256'][:16]}…")
        print(f"  T5 report: {binding['report_relpath']}  "
              f"sha256={binding['report_sha256'][:16]}…")
        print(f"  T5 signs: {sorted(binding['signatures'])}  "
              f"impl={binding['implementation_sha256'][:16]}…")
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


# ---------------------------------------------------------------------------
# T9：结案（五轴 + 失败链）→ 证据包 → 独立复算（全程零 POST）
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# T7/T8：正式 A/B 研究与独立复跑（2026-11-13 接线，用户裁决全量重跑）
# ---------------------------------------------------------------------------

def cmd_study(args: argparse.Namespace) -> int:
    """T7：正式研究单 study（A 48 / B 48；先 A 后 B；真实物理 + 真实 POST）。

    与 probe/practice 同一条付费纪律：环境闸 → 批准件 → 网关 → 研究链 → 报告。
    前置由 ``study.py`` 的 root gate 强制（A 需 PRACTICED、B 需 STUDY_A）；
    同一 study 只跑一次（报告件在场即拒）。
    """
    from orchestrator.rsi_r1 import study as STD

    # 环境闸先于一切副作用（含读 envelope）：无 PAID 许可时零文件 IO。
    GT.require_paid_env(os.environ)
    root = _resolve_root(args.root, args.envelope)
    envelope = _load_envelope(root, args.envelope)
    study = str(args.study).strip().upper()
    batch_id = str(args.batch_id or "").strip()
    if not batch_id:
        raise Cli12Error("BATCH_MISSING", "--batch-id 不得为空")
    if not args.tokenizer or not args.tokenizer_sha256:
        raise Cli12Error("TOKENIZER_REQUIRED",
                         "付费运行需要 --tokenizer 与 --tokenizer-sha256")
    consent = _paid_gate(root, envelope)
    profile = P.resolve_profile(envelope)
    envelope_id = str(envelope["envelope_id"])
    if root.resolve(strict=False) != RP.rsi_r12_campaign_root().resolve(strict=False):
        raise Cli12Error(
            "CAMPAIGN_ENV_MISMATCH",
            f"根 {root} 与 SIMPRAXIS_RSI_R12_CAMPAIGN 解析的 "
            f"{RP.rsi_r12_campaign_root()} 不一致（账本路径必须与根同源）")
    ctx = _r12_ctx(root, envelope_id)
    counter = _tokenizer_counter(args.tokenizer, args.tokenizer_sha256)
    lane = ASM.STUDY_LANE[study]
    cap = int(profile.lane_post_caps[lane])
    approval = PT.ensure_approval(
        ctx, batch_id=batch_id, lane=lane, approved=cap,
        note=args.note or "", namespace=QL.RSI_R12_NAMESPACE)
    lock_doc = _read_json(root / LK.LOCK_RELPATH)
    image = args.image or str((lock_doc.get("image") or {}).get("pin") or "")
    if not image:
        raise Cli12Error("IMAGE_PIN_INVALID", "缺 --image 且 LOCK 未登记镜像 pin")
    spend = ASM.SpendContract(mode=ASM.MODE_PAID, consent=consent)
    gateway = GT.build_gateway(
        ledger=QL.QuotaLedger.rsi_r12(),
        pause_state_path=RP.rsi_r12_gateway_state_path())
    transport = GT.build_transport(
        gateway=gateway, lane=lane, batch_id=batch_id,
        thread_id=args.thread_id or f"rsi-r12-{lane}", env=os.environ)
    report = STD.run_study(
        ctx, study=study, spend=spend, transport_factory=lambda: transport,
        runner=SANDBOX.DockerRunner(), image=image, batch_id=batch_id,
        counter=counter, profile=profile,
        resource_ledger=RS.ResourceLedger(root, profile=profile))
    print(f"study {study} {report['status']}  state={report['study_state']}  "
          f"posts={report['posts']['this_attempt']}/"
          f"{report['posts']['allowed']}  root={report['root_after']}  "
          f"stopped={report['stopped'] or '-'}")
    print(f"  ok turns={report['ok_count']}  "
          f"judge={report['judge'].get('verdict') or '-'}  "
          f"claim={'present' if report['claim_submitted'] else 'missing'}")
    print(f"  approval={approval}")
    print(f"  report={ctx.relative(STD.report_relpath(study))}")
    _record_family_usage(ctx, envelope_id)
    return 0 if report["status"] == "COMPLETED" else 2


def cmd_replay(args: argparse.Namespace) -> int:
    """T8：独立复跑（零模型 POST、真容器；比对件只封一次）。

    不建网关、不占任何 lane 呼量，因此不需要付费环境闸与批准件；但会真跑
    容器，必须显式 ``--real-containers``。
    """
    from orchestrator.rsi_r1 import replay_driver as RPD

    # 显式旗标检查先于文件 IO（fail-closed：没有 --real-containers 就不读根）。
    if not args.real_containers:
        raise Cli12Error("REPLAY_REAL_CONTAINERS_REQUIRED",
                         "复跑会真跑容器：需显式 --real-containers（无模型 POST）")
    root = _resolve_root(args.root, args.envelope)
    envelope = _load_envelope(root, args.envelope)
    study = str(args.replay_id).strip().upper()
    batch_id = str(args.batch_id or "").strip()
    if not batch_id:
        raise Cli12Error("BATCH_MISSING", "--batch-id 不得为空")
    ctx = _r12_ctx(root, str(envelope["envelope_id"]))
    profile = P.resolve_profile(envelope)
    lock_doc = _read_json(root / LK.LOCK_RELPATH)
    image = args.image or str((lock_doc.get("image") or {}).get("pin") or "")
    if not image:
        raise Cli12Error("IMAGE_PIN_INVALID", "缺 --image 且 LOCK 未登记镜像 pin")
    report = RPD.run_replay(
        ctx, study=study, runner=SANDBOX.DockerRunner(), image=image,
        batch_id=batch_id, max_steps=args.max_steps, profile=profile)
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
    return 0 if report["status"] in (RPD.STATUS_COMPLETE,
                                     RPD.STATUS_MISMATCH) else 2


def _record_family_usage(ctx: Any, envelope_id: str) -> None:
    """D-2：付费动作完成后按账本 call 行补记族用量（幂等、单调）。"""
    try:
        calls = int(CO.ledger_facts(CO._rows(ctx.root))["calls"])
        FAM.record_usage(_family_root(), envelope_id, calls)
        print(f"  family : posts_used={calls}（按账本 call 行补记）")
    except FAM.FamilyError as exc:
        print(f"  WARN family usage not recorded: "
              f"{getattr(exc, 'code', '')} {exc}", file=sys.stderr)


def main(argv: "list[str] | None" = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (Cli12Error, C.ContractError, FAM.FamilyError, P.ProfileError,
            RP.RsiR12CampaignUnsetError, HE.HumanEventError,
            RT.CampaignWriteRefused, OSError, CAL.CalibrationError,
            LK.LockError, OV.OfflineValidationError, OS.OfflineSuiteError,
            PREREG.PreregError, ST.StateError, RS.ResourceError,
            GT.TransportError, PT.ProtocolError, ASM.AssemblyError,
            CO12.Closeout12Error, CLOSURE.ClosureError) as exc:
        print(f"BLOCKED {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
