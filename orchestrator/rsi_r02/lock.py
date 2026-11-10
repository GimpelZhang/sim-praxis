"""RSI-R0.2 最终 LOCK（计划 §8-T3 / §10 / AC21）。

LOCK 是「先冻结再模型请求」的那道门：它把**实现面 + 共享依赖 + 阶段证据**逐件
sha256 钉住；任一文件改动 ⇒ ``verify`` 报问题 ⇒ ``cli.run`` 直接拒绝付费 POST
（``FREEZE_INVALID``），不靠自觉。

覆盖范围（AC21「shared gateway/判据/runner/closeout 依赖 hash 纳入」）：

* 本族实现：``orchestrator/rsi_r02/*.py``、``scripts/rsi_r02/*.py``（含 gather/pack）；
* 共享模块：gateway（router/quota_ledger/stage_models）、common（runtime_paths/
  fsutil/secrets）、R0.1 判据/观测/runner 依赖、继承负控与独立 oracle；
* 阶段证据：T3 控制报告、结案预演报告、两条协议链记录、花费/任务/复用合同；
* 授权：plan/consent sha 与三阶段 model_id（模型变更本就要用户裁决，LOCK 只做取证）。

签名：``reviewer`` / ``verifier`` 两份独立签名必须绑定**同一个** ``lock_sha256``；
``final_lock.json`` 在两者齐备后写入，之后任何语义变更只能开新 campaign。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from orchestrator.common.clock import utc_now_iso_z
from orchestrator.rsi_r02 import campaign as C

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_LOCK = "simpraxis.rsi_r02.lock.v1"
SCHEMA_SIG = "simpraxis.rsi_r02.lock_signature.v1"
CANDIDATE_FILENAME = "contract/lock_candidate.json"
FINAL_FILENAME = "contract/final_lock.json"
SIG_DIR = "contract/signatures"
SIG_ROLES = ("reviewer", "verifier")

IMPL_GLOBS = ("orchestrator/rsi_r02/*.py", "scripts/rsi_r02/*.py")
#: 依赖闭包的起点：本族实现（含 gather/pack/控制脚本）。
CLOSURE_PREFIXES = ("orchestrator.", "scripts.")
#: 显式钉死、但不在静态 import 图里的共享件：R0.1 负控/独立 oracle/参考 actor
#: 由 T3 控制脚本按 sys.path 裸名 import（`import controls`），AST 看不到。
SHARED_FILES = (
    "orchestrator/gateway/stage_models.yaml",
    "orchestrator/gateway/router.py",
    "orchestrator/gateway/quota_ledger.py",
    "orchestrator/common/runtime_paths.py",
    "orchestrator/common/fsutil.py",
    "orchestrator/common/secrets.py",
    "orchestrator/common/clock.py",
    "orchestrator/rsi_r01/actions.py",
    "orchestrator/rsi_r01/analysis_runner.py",
    "orchestrator/rsi_r01/evaluator.py",
    "orchestrator/rsi_r01/observations.py",
    "orchestrator/rsi_r01/schemas.py",
    "orchestrator/rsi_r01/task_contract.py",
    "orchestrator/rsi_r01/service.py",
    "scripts/rsi_r01/controls.py",
    "scripts/rsi_r01/independent_oracle.py",
    "scripts/rsi_r01/reference_actor.py",
)
EVIDENCE_FILES = {
    "t3_control_report": "t3_control/report.json",
    "t3_closeout_rehearsal": "t3_control/closeout_rehearsal.json",
    "protocol_P1": "t3_control/records/proto_P1.json",
    "protocol_P2": "t3_control/records/proto_P2.json",
    "spend_contract": "contract/spend_contract.json",
    "task_contract": "contract/task_contract.json",
    "asset_reuse": "contract/asset_reuse.json",
    # D-T4-2：协议合同（动作顺序/槽数/必填字段表/公开文档 sha256 与自足性）
    # 以前只写在 docstring 里、从未物化，于是「候选看到的文档是否自足」无从
    # 签署。现在它是冻结面的一件证据，缺件即 ``证据缺件，不得签``。
    "protocol_contract": "contract/protocol_contract.json",
}
_UNHASHED = ("signatures", "lock_sha256", "created_utc", "frozen_utc")


class LockError(RuntimeError):
    """LOCK 缺件/漂移/未冻结。"""


def _sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha_file(path: Path) -> str:
    return _sha_bytes(path.read_bytes())


def _canonical(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def core_sha256(doc: Mapping[str, Any]) -> str:
    core = {k: v for k, v in doc.items() if k not in _UNHASHED}
    return _sha_bytes(_canonical(core))


def _module_file(repo_root: Path, module: str) -> "Path | None":
    base = repo_root.joinpath(*module.split("."))
    for cand in (base.with_suffix(".py"), base / "__init__.py"):
        if cand.is_file() and not cand.is_symlink():
            return cand
    return None


def _relative_module(rel: str, level: int, module: "str | None") -> str:
    """把 ``from .x import y`` 解析成绝对模块名（评审 L2）。

    包内相对导入若不解析就会被静默漏锁——以后有人把共享逻辑改成 ``from .common
    import …``，LOCK 会假装覆盖实际没覆盖。解析到仓根之外（层级过深）就报错，
    不猜、不跳过。
    """
    parts = rel.split("/")[:-1]
    if level > 1:
        if level - 1 > len(parts):
            raise LockError(f"相对导入层级越界：{rel} level={level}")
        parts = parts[: len(parts) - (level - 1)]
    if not parts:
        raise LockError(f"相对导入不在任何包内：{rel} level={level}")
    base = ".".join(parts)
    return f"{base}.{module}" if module else base


def _imported_modules(path: Path, rel: str) -> "list[tuple[str, bool]]":
    """``(模块名, 是否必须解析)``：``from X import y`` 里的 ``X.y`` 是软候选
    （y 可能是函数/常量），解析不到不算漏锁；``X`` 自身解析不到才算。

    相对导入（``level>0``）按文件所在包解析成绝对名后同样纳入闭包。
    """
    import ast

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    out: "list[tuple[str, bool]]" = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.extend((a.name, True) for a in node.names)
        elif isinstance(node, ast.ImportFrom) and (node.module or node.level):
            if node.level:
                resolved = _relative_module(rel, node.level, node.module)
                # ``from . import x``（module=None）只需解析包名，包自身不必
                # 有 __init__.py；具名子模块作软候选。
                if node.module:
                    out.append((resolved, True))
                out.extend((f"{resolved}.{a.name}", False) for a in node.names)
            else:
                out.append((node.module, True))
                out.extend((f"{node.module}.{a.name}", False)
                           for a in node.names)
    return [(m, req) for m, req in out if m.startswith(CLOSURE_PREFIXES)]


def dependency_closure(repo_root: Path = REPO_ROOT) -> "set[str]":
    """本族实现的**静态 import 闭包**（orchestrator.* / scripts.*）。

    AC21 要求「通用共享模块不能因不在 rsi_r02 目录就漏锁」：手写清单会漏
    （例如 TrialRunner 在 ``orchestrator/rsi_r01/trial.py``，经 cli 间接使用）。
    这里从实现面出发按 AST 递归，取全闭包；解析不到的**必需**模块名如实报错，
    不静默跳过。
    """
    pending: "list[str]" = []
    for pattern in IMPL_GLOBS:
        for path in sorted(repo_root.glob(pattern)):
            pending.append(str(path.relative_to(repo_root)))
    seen: "set[str]" = set()
    missing: "set[str]" = set()
    while pending:
        rel = pending.pop()
        if rel in seen:
            continue
        path = repo_root / rel
        if not path.is_file() or path.is_symlink():
            continue
        seen.add(rel)
        for module, required in _imported_modules(path, rel):
            target = _module_file(repo_root, module)
            if target is None:
                if required:
                    missing.add(module)
                continue
            sub = str(target.relative_to(repo_root))
            if sub not in seen:
                pending.append(sub)
    if missing:
        raise LockError(f"依赖闭包模块解析不到（不得漏锁）：{sorted(missing)[:8]}")
    return seen


def implementation_manifest(repo_root: Path = REPO_ROOT) -> "dict[str, str]":
    out: "dict[str, str]" = {}
    for rel in sorted(dependency_closure(repo_root)):
        out[rel] = _sha_file(repo_root / rel)
    for rel in SHARED_FILES:
        path = repo_root / rel
        if not path.is_file():
            raise LockError(f"共享依赖缺件：{rel}（实现面不完整，不得签）")
        out[rel] = _sha_file(path)
    if not any(r.startswith("orchestrator/rsi_r02/") for r in out):
        raise LockError("实现面为空：orchestrator/rsi_r02/*.py 未找到")
    return out


def stage_models(repo_root: Path = REPO_ROOT) -> "dict[str, Any]":
    import yaml

    path = repo_root / "orchestrator/gateway/stage_models.yaml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    stages = doc.get("stage_models") or doc.get("stages") or doc
    models: "dict[str, str | None]" = {}
    for stage in ("macro_ideation", "coding_debug", "visual_rollout"):
        entry = stages.get(stage) if isinstance(stages, Mapping) else None
        models[stage] = (None if not isinstance(entry, Mapping)
                         else entry.get("model_id"))
    return {"sha256": _sha_file(path), "models": models}


def gs_check(ctx: C.CampaignContext) -> "dict[str, Any]":
    """G-S 复核：豁免件在冻结时**重新校验内容**，不只信 hash。

    用户 2026-11-09 逐字豁免 G-S（$1.5/POST、整批上限 $100.0）。冻结时必须仍
    满足：状态为 WAIVED_BY_USER、逐字豁免 sha 与 consent 内嵌文本一致、
    预算上限与 ``billing.USD_CAP`` 同值、逐 POST 预约上界不超过整批上限。
    """
    from orchestrator.rsi_r02 import billing as B

    problems: "list[str]" = []
    path = ctx.relative("contract/spend_contract.json")
    if not path.is_file():
        return {"ok": False, "problems": ["SPEND_CONTRACT_MISSING"]}
    doc = json.loads(path.read_text(encoding="utf-8"))
    if doc.get("gate") != "G-S" or doc.get("status") != "WAIVED_BY_USER":
        problems.append("GS_NOT_WAIVED")
    authority = doc.get("authority") or {}
    text = str(authority.get("verbatim_text") or "")
    if _sha_bytes(text.encode("utf-8")) != authority.get("verbatim_sha256"):
        problems.append("GS_WAIVER_SHA_MISMATCH")
    try:
        reservation = float(doc.get("per_post_reservation_usd"))
        batch = float(doc.get("batch_cap_usd"))
    except (TypeError, ValueError):
        problems.append("GS_BUDGET_UNPARSEABLE")
        reservation = batch = -1.0
    if batch != B.USD_CAP:
        problems.append(f"GS_BATCH_CAP_MISMATCH: {batch} != {B.USD_CAP}")
    if reservation <= 0 or reservation > batch:
        problems.append(f"GS_RESERVATION_OUT_OF_RANGE: {reservation}")
    if doc.get("claimed_as_supplier_guarantee") is not False:
        problems.append("GS_CLAIMED_AS_SUPPLIER_GUARANTEE")
    return {"ok": not problems, "problems": problems,
            "per_post_reservation_usd": reservation,
            "batch_cap_usd": batch}


def build_candidate(ctx: C.CampaignContext, *,
                    repo_root: Path = REPO_ROOT) -> "dict[str, Any]":
    evidence: "dict[str, str]" = {}
    missing: "list[str]" = []
    for name, rel in EVIDENCE_FILES.items():
        path = ctx.relative(rel)
        if not path.is_file():
            missing.append(rel)
            continue
        evidence[name] = _sha_file(path)
    if missing:
        raise LockError(f"证据缺件，不得签：{missing}")
    impl = implementation_manifest(repo_root)
    gs = gs_check(ctx)
    if not gs["ok"]:
        raise LockError(f"G-S 复核不通过，不得签：{gs['problems']}")
    doc = {
        "schema": SCHEMA_LOCK,
        "created_utc": utc_now_iso_z(),
        "campaign_id": ctx.campaign_id,
        # 判决面钉的是**此刻磁盘上的**计划/授权文件，不是开账时的快照值。
        "plan": {"path": ctx.plan_path,
                 "sha256": _sha_file(Path(ctx.plan_path))},
        "consent": {"path": ctx.consent_path,
                    "sha256": _sha_file(Path(ctx.consent_path))},
        "stage_models": stage_models(repo_root),
        "gates": {"G-S": gs},
        "implementation": impl,
        "implementation_sha256": _sha_bytes(_canonical(impl)),
        "evidence": evidence,
        "rules": {
            "freeze_before_paid": True,
            "any_change_refuses_post": "FREEZE_INVALID",
            "sig_roles": list(SIG_ROLES),
            "judgement_outside_pack": True,
            "private_never_packed": True,
        },
    }
    doc["lock_sha256"] = core_sha256(doc)
    return doc


def write_candidate(ctx: C.CampaignContext, *,
                    repo_root: Path = REPO_ROOT) -> "dict[str, Any]":
    doc = build_candidate(ctx, repo_root=repo_root)
    C.write_json(ctx, CANDIDATE_FILENAME, doc)
    return doc


def load_candidate(ctx: C.CampaignContext) -> "dict[str, Any]":
    path = ctx.relative(CANDIDATE_FILENAME)
    if not path.is_file():
        raise LockError(f"LOCK 候选缺件：{CANDIDATE_FILENAME}")
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise LockError(f"LOCK 候选不可读：{CANDIDATE_FILENAME}: {exc}") from exc
    if doc.get("lock_sha256") != core_sha256(doc):
        raise LockError("LOCK 候选自证不符（文件被改过）")
    return doc


def sign(ctx: C.CampaignContext, *, role: str, signer: str, statement: str,
         verdict: str = "APPROVE") -> "dict[str, Any]":
    if role not in SIG_ROLES:
        raise LockError(f"未知签名角色：{role!r}（只允许 {SIG_ROLES}）")
    if verdict != "APPROVE":
        raise LockError("只有 APPROVE 可写入 LOCK 签名（REJECT 走评审记录）")
    cand = load_candidate(ctx)
    sig = {"schema": SCHEMA_SIG, "role": role, "signer": signer,
           "verdict": verdict, "statement": statement,
           "lock_sha256": cand["lock_sha256"],
           "signed_at_utc": utc_now_iso_z()}
    C.write_json(ctx, f"{SIG_DIR}/{role}.json", sig)
    return sig


def freeze(ctx: C.CampaignContext, *, repo_root: Path = REPO_ROOT
           ) -> "dict[str, Any]":
    cand = load_candidate(ctx)
    live = build_candidate(ctx, repo_root=repo_root)
    if live["lock_sha256"] != cand["lock_sha256"]:
        raise LockError("候选与当前实现/证据不一致：先重出候选再签")
    sigs = {}
    signers: "dict[str, str]" = {}
    for role in SIG_ROLES:
        path = ctx.relative(f"{SIG_DIR}/{role}.json")
        if not path.is_file():
            raise LockError(f"缺 {role} 签名")
        try:
            sig = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise LockError(f"{role} 签名不可读：{exc}") from exc
        if sig.get("verdict") != "APPROVE":
            raise LockError(f"{role} 未 APPROVE")
        if sig.get("lock_sha256") != cand["lock_sha256"]:
            raise LockError(f"{role} 签名绑定的是另一版 LOCK（hash 不同）")
        signers[role] = str(sig.get("signer") or "").strip()
        sigs[role] = _sha_file(path)
    # 评审 M2：两签必须来自**不同**签署人——同一人占两角色等于没有第二道门。
    if not all(signers.values()) or len(set(signers.values())) != len(SIG_ROLES):
        raise LockError(f"两签签署人不得相同/为空：{signers}")
    doc = dict(cand)
    doc["signatures"] = sigs
    doc["frozen_utc"] = utc_now_iso_z()
    C.write_json(ctx, FINAL_FILENAME, doc)
    return doc


def verify(ctx: C.CampaignContext, *, repo_root: Path = REPO_ROOT,
           require: bool = True) -> "dict[str, Any]":
    """重算实现面并比对最终 LOCK；``require=False``（T4 前）允许未冻结但如实报告。"""
    path = ctx.relative(FINAL_FILENAME)
    if not path.is_file():
        out = {"ok": False, "required": require,
               "problems": ["FINAL_LOCK_MISSING"]}
        if require:
            return out
        out["ok"] = True
        return out
    try:
        final = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"ok": False, "required": require,
                "problems": [f"FINAL_LOCK_UNREADABLE: {FINAL_FILENAME}: {exc}"]}
    problems: "list[str]" = []
    if final.get("lock_sha256") != core_sha256(final):
        problems.append("LOCK_SELF_SHA_MISMATCH")
    # 评审 M1：plan/consent 的 sha 只写进 LOCK 不复核，等于冻结后还能换计划。
    # 注意必须**重算文件**：``ctx.plan_sha256`` 是开账时写死的快照值，改文件
    # 不会动它，拿快照互比是空检查。
    def _rehash(rel: "str | None") -> "str | None":
        if not rel:
            return None
        try:
            return _sha_file(Path(rel))
        except OSError as exc:
            problems.append(f"PLAN_OR_CONSENT_UNREADABLE: {exc}")
            return None

    plan_sha = _rehash(getattr(ctx, "plan_path", None))
    consent_sha = _rehash(getattr(ctx, "consent_path", None))
    if plan_sha is not None and (final.get("plan") or {}).get("sha256") != plan_sha:
        problems.append("PLAN_CHANGED")
    if (consent_sha is not None
            and (final.get("consent") or {}).get("sha256") != consent_sha):
        problems.append("CONSENT_CHANGED")
    try:
        live = build_candidate(ctx, repo_root=repo_root)
    except LockError:
        # 冻结后实现/证据不再可组装（如 G-S 复核失败）：逐项重算，问题照实报，
        # 不吞掉也不因一处异常就放弃其余检查。
        gs = gs_check(ctx)
        if not gs["ok"]:
            problems.append(f"GS_RECHECK_FAILED: {gs['problems']}")
        try:
            impl: "dict[str, str]" = implementation_manifest(repo_root)
        except LockError as exc:
            problems.append(f"IMPLEMENTATION_UNREADABLE: {exc}")
            impl = {}
        evidence: "dict[str, str]" = {}
        for name, rel in EVIDENCE_FILES.items():
            p = ctx.relative(rel)
            if p.is_file():
                evidence[name] = _sha_file(p)
        live = {"implementation": impl, "evidence": evidence,
                "stage_models": None, "gates": {"G-S": gs}}
    for rel, sha in sorted((final.get("implementation") or {}).items()):
        now = live["implementation"].get(rel)
        if now is None:
            problems.append(f"MISSING: {rel}")
        elif now != sha:
            problems.append(f"CHANGED: {rel}")
    for rel in sorted(set(live["implementation"]) - set(
            (final.get("implementation") or {}))):
        problems.append(f"ADDED: {rel}")
    if live.get("evidence") != final.get("evidence"):
        problems.append("EVIDENCE_CHANGED")
    if (live.get("stage_models") is not None
            and live.get("stage_models") != final.get("stage_models")):
        problems.append("MODEL_CONFIG_CHANGED")
    gs = gs_check(ctx)
    if not gs["ok"]:
        problems.append(f"GS_RECHECK_FAILED: {gs['problems']}")
    elif live.get("gates") != final.get("gates"):
        problems.append("GATE_RECORD_CHANGED")
    problems.extend(_signature_problems(ctx, final))
    return {"ok": not problems, "required": require, "problems": problems,
            "lock_sha256": final.get("lock_sha256")}


def _signature_problems(ctx: C.CampaignContext,
                        final: Mapping[str, Any]) -> "list[str]":
    """签名三道检查：文件未变、**内容仍 APPROVE**、绑定的是**这一版** LOCK。

    只比对 ``signatures`` 里的 sha 是不够的（评审 H2）：把 final_lock.json 的
    实现面与 ``lock_sha256`` 一起重写后，旧签名文件的 sha 一个字节没变，
    只查「文件没变」就放行，AC21 的付费门等于被绕过。所以必须重读每个签名
    文件，逐字确认它内嵌的 ``lock_sha256`` 与当前最终 LOCK 相等、判词仍是
    ``APPROVE``。
    """
    problems: "list[str]" = []
    sigs = final.get("signatures") or {}
    current = final.get("lock_sha256")
    signers: "dict[str, str]" = {}
    for role in SIG_ROLES:
        p = ctx.relative(f"{SIG_DIR}/{role}.json")
        if not p.is_file() or _sha_file(p) != sigs.get(role):
            problems.append(f"SIGNATURE_MISSING_OR_CHANGED: {role}")
            continue
        try:
            sig = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            problems.append(f"SIGNATURE_UNREADABLE: {role}")
            continue
        if sig.get("verdict") != "APPROVE":
            problems.append(f"SIGNATURE_NOT_APPROVE: {role}")
        if sig.get("lock_sha256") != current:
            problems.append(f"SIGNATURE_BINDS_OTHER_LOCK: {role}")
        signers[role] = str(sig.get("signer") or "").strip()
    # 与 freeze 同一道门（评审 M2）：复核时两签也必须来自不同签署人。
    if len(signers) == len(SIG_ROLES) and len(set(signers.values())) != len(signers):
        problems.append(f"SIGNATURES_NOT_DISTINCT: {signers}")
    return problems


def main(argv: "list[str] | None" = None) -> int:
    import argparse
    from pathlib import Path as _P

    ap = argparse.ArgumentParser(description="RSI-R0.2 最终 LOCK")
    ap.add_argument("action", choices=("candidate", "sign", "freeze", "verify"))
    ap.add_argument("--campaign-id", required=True)
    ap.add_argument("--root", default=None)
    ap.add_argument("--role", default=None)
    ap.add_argument("--signer", default=None)
    ap.add_argument("--statement", default="")
    ap.add_argument("--allow-unfrozen", action="store_true")
    args = ap.parse_args(list(argv) if argv is not None else None)
    base = _P(args.root) if args.root else None
    ctx = C.load_campaign(campaign_id=args.campaign_id, root=base)
    if args.action == "candidate":
        out = write_candidate(ctx)
    elif args.action == "sign":
        out = sign(ctx, role=str(args.role), signer=str(args.signer),
                   statement=args.statement)
    elif args.action == "freeze":
        out = freeze(ctx)
    else:
        out = verify(ctx, require=not args.allow_unfrozen)
    print(json.dumps(out, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if (out.get("ok", True) and args.action != "verify") or \
        (args.action == "verify" and out.get("ok")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
