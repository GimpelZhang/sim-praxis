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
from orchestrator.rsi_r1 import contracts as C
from orchestrator.rsi_r1 import family as FAM
from orchestrator.rsi_r1 import profiles as P

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
    return p


def main(argv: "list[str] | None" = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (Cli12Error, C.ContractError, FAM.FamilyError, P.ProfileError,
            RP.RsiR12CampaignUnsetError, OSError) as exc:
        print(f"BLOCKED {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
