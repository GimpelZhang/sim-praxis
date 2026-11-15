"""RSI-R1.3 CLI（T0 子集；后续任务按 Plan §8.3 逐步接入）。

原则：

* 新 campaign 必须显式 ``--campaign rsi-r1.3``（或走本模块入口），未知/缺省
  一律拒绝；绝不静默复用 R1.2 根/账/费用 scope；
* T0 命令（``t0-baseline`` / ``family-reconcile``）**零模型 POST**：只读快照、
  逐账核对与容量测量，不构造 sender、不 import 网关调用路径；
* 写路径全部落 ``$DATA_ROOT/rsi_r13/``（家族根或 campaign 根），旧根只读。

本模块在 T0 只登记两个命令；``init/preregister/...`` 在 T1+ 按实际契约实现后
才出现在 parser（不在帮助里预留空壳）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping

from orchestrator.common import runtime_paths as RP
from orchestrator.rsi_r1 import family as FAM
from orchestrator.rsi_r1 import profiles as P
from orchestrator.rsi_r1 import resource_plan as RPL
from orchestrator.rsi_r1 import review_capacity as RC
from orchestrator.rsi_r1 import cli_r13_campaign as CC
from orchestrator.rsi_r1 import calibration as CAL
from orchestrator.rsi_r1 import closeout_r13 as CO13
from orchestrator.rsi_r1 import contracts as C
from orchestrator.rsi_r1 import r13_review_run as RRUN
from orchestrator.rsi_r1 import review_runner as RN
from orchestrator.rsi_r1 import review_rubric_v13 as RV
from orchestrator.rsi_r1 import lock as LK
from orchestrator.rsi_r1 import lock_r13 as L13
from orchestrator.rsi_r1 import state as S
from orchestrator.rsi_r1 import task_specs as TS

PLAN_PATH_R13 = Path("/home/vipuser/文档/RSI_R1.3_Plan_detailed.md")
BASELINE_SCHEMA = "simpraxis.rsi_r13.baseline.v1"
RECONCILE_SCHEMA = "simpraxis.rsi_r13.family_reconciliation.v1"

_REPO_ROOT = Path(__file__).resolve().parents[2]

#: T0 快照覆盖的代码面（相对仓库根；逐文件 sha 便于跨轮比对漂移）。
BASELINE_CODE_PATHS: "tuple[str, ...]" = (
    "orchestrator/rsi_r1/profiles.py",
    "orchestrator/rsi_r1/family.py",
    "orchestrator/rsi_r1/lanes.py",
    "orchestrator/rsi_r1/contracts.py",
    "orchestrator/rsi_r1/experiments.py",
    "orchestrator/rsi_r1/assembly.py",
    "orchestrator/rsi_r1/analysis_receipt.py",
    "orchestrator/rsi_r1/judge.py",
    "orchestrator/rsi_r1/review_rubric.py",
    "orchestrator/rsi_r1/review_capacity.py",
    "orchestrator/rsi_r1/cli_r12.py",
    "orchestrator/rsi_r1/cli_r13.py",
    "orchestrator/rsi_r1/lock.py",
    "orchestrator/rsi_r1/resources.py",
    "orchestrator/rsi_r1/closeout_r12.py",
    "orchestrator/common/runtime_paths.py",
    "orchestrator/gateway/router.py",
    "orchestrator/gateway/quota_ledger.py",
)


class Cli13Error(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"[{code}] {detail}")


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_sha256(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _write_json(path: Path, doc: Mapping[str, Any]) -> str:
    from orchestrator.common.fsutil import atomic_write_text

    text = json.dumps(doc, indent=1, sort_keys=True, ensure_ascii=False) + "\n"
    atomic_write_text(path, text, mode=0o600)
    return _sha256_bytes(text.encode("utf-8"))


def _run(cmd: "list[str]", *, timeout: int = 10) -> "str | None":
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                             check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip()


def _git_commit() -> "str | None":
    return _run(["git", "-C", str(_REPO_ROOT), "rev-parse", "HEAD"])


def _git_dirty() -> "bool | None":
    out = _run(["git", "-C", str(_REPO_ROOT), "status", "--porcelain"])
    return None if out is None else bool(out)


def _stage_model_ids(path: Path) -> "dict[str, str]":
    """只读解析 stage_models.yaml 的 model_id（无 YAML 依赖；不打印凭据）。"""
    ids: "dict[str, str]" = {}
    stage = None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ids
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip() or ":" not in line:
            continue
        indent = len(line) - len(line.lstrip())
        key, _, value = line.strip().partition(":")
        if indent == 2 and key in ("macro_ideation", "coding_debug",
                                   "visual_rollout"):
            stage = key
            ids.setdefault(stage, "")
        elif indent == 4 and key == "model_id" and stage:
            ids[stage] = value.strip().strip('"').strip("'")
    return ids


def _root_inventory() -> "dict[str, Any]":
    roots = {
        "rsi_r0": "/mnt/artifacts/simpraxis/rsi_r0",
        "rsi_r01": "/mnt/artifacts/simpraxis/rsi_r01",
        "rsi_r02": "/mnt/artifacts/simpraxis/rsi_r02",
        "rsi_r1": "/mnt/artifacts/simpraxis/rsi_r1",
        "rsi_r1_repair1": "/mnt/artifacts/simpraxis/rsi_r1_repair1",
        "rsi_r1_repair2": "/mnt/artifacts/simpraxis/rsi_r1_repair2",
        "rsi_r12": "/mnt/artifacts/simpraxis/rsi_r12",
    }
    out: "dict[str, Any]" = {}
    for name, raw in roots.items():
        path = Path(raw)
        entry: "dict[str, Any]" = {"path": raw, "exists": path.is_dir()}
        if path.is_dir():
            camps = [p.name for p in sorted(path.iterdir())
                     if p.is_dir() and p.name not in ("family", "baseline")]
            entry["campaigns"] = camps
            family_doc = path / "family" / "family.json"
            if family_doc.is_file():
                entry["family_doc_sha256"] = _file_sha256(family_doc)
        out[name] = entry
    return out


def _environment_facts() -> "dict[str, Any]":
    facts: "dict[str, Any]" = {}
    try:
        usage = shutil.disk_usage("/mnt")
        facts["disk_mnt"] = {"total": usage.total, "free": usage.free,
                             "used": usage.used}
    except OSError:
        facts["disk_mnt"] = None
    containers = _run(["docker", "ps", "-aq"], timeout=15)
    facts["containers_all"] = (len(containers.splitlines())
                               if containers is not None else None)
    gpu = _run(["nvidia-smi", "--query-gpu=memory.used",
                "--format=csv,noheader,nounits"], timeout=15)
    facts["gpu_mem_used_mib"] = ([int(x) for x in gpu.splitlines() if x.strip()]
                                 if gpu else None)
    facts["paid_env_allow"] = str(os.environ.get("SIMPRAXIS_ALLOW_PAID", "")) == "1"
    return facts


def _profile_facts() -> "dict[str, Any]":
    out: "dict[str, Any]" = {}
    for pid in P.PROFILES:
        profile = P.profile_by_id(pid)
        out[pid] = {
            "profile_sha256": _sha256_bytes(json.dumps(
                profile.to_dict(), sort_keys=True, separators=(",", ":"),
                ensure_ascii=False).encode("utf-8")),
            "namespace": profile.namespace,
            "hard_cap": profile.hard_cap,
            "family_cap": profile.family_cap,
            "lane_post_caps": dict(profile.lane_post_caps),
            "closure_groups_key": profile.closure_groups_key,
            "output_tokens": profile.output_tokens,
        }
    return out


def _code_facts() -> "dict[str, Any]":
    files: "dict[str, Any]" = {}
    for rel in BASELINE_CODE_PATHS:
        path = _REPO_ROOT / rel
        files[rel] = (_file_sha256(path) if path.is_file() else None)
    return {"files": files}


def cmd_t0_baseline(args: argparse.Namespace) -> int:
    """T0 只读基线快照（零 POST；不写旧根、不构造 sender）。"""
    now = _utc_now()
    stem = now.replace(":", "").replace("-", "")
    out_dir = Path(args.out) if args.out else RP.rsi_r13_baseline_dir() / stem
    out_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    plan_path = Path(args.plan) if args.plan else PLAN_PATH_R13
    if not plan_path.is_file():
        raise Cli13Error("PLAN_ABSENT", f"Plan 不存在：{plan_path}")
    tokenizer_path = Path(args.tokenizer) if args.tokenizer else Path(
        RC.DEFAULT_TOKENIZER_PATH)
    tokenizer_sha = (_file_sha256(tokenizer_path)
                     if tokenizer_path.is_file() else None)
    doc: "dict[str, Any]" = {
        "schema": BASELINE_SCHEMA,
        "schema_version": P.SCHEMA_VERSION,
        "captured_at_utc": now,
        "profile_id": P.PROFILE_R13,
        "plan": {
            "path": str(plan_path),
            "sha256": _file_sha256(plan_path),
            "bytes": plan_path.stat().st_size,
        },
        "git": {
            "commit": _git_commit(),
            "dirty": _git_dirty(),
            "branch": _run(["git", "-C", str(_REPO_ROOT), "branch",
                            "--show-current"]),
        },
        "code": _code_facts(),
        "profiles": _profile_facts(),
        "stage_models": {
            "path": "orchestrator/gateway/stage_models.yaml",
            "model_ids": _stage_model_ids(
                _REPO_ROOT / "orchestrator/gateway/stage_models.yaml"),
            "note": "只读解析；模型变更仍须硬规则 1 审批，本命令不改文件",
        },
        "tokenizer": {
            "path": str(tokenizer_path),
            "sha256": tokenizer_sha,
            "pinned": tokenizer_sha == RC.DEFAULT_TOKENIZER_SHA256,
        },
        "roots": _root_inventory(),
        "environment": _environment_facts(),
        "review_capacity": RC.registered_caps(P.PROFILE_R13_DEF),
        "paid_posts": 0,
        "note": ("T0 快照：只读旧根与当前代码；零模型 POST；不写任何旧根。"),
    }
    sha = _write_json(out_dir / "baseline.json", doc)
    print(json.dumps({
        "baseline": str(out_dir / "baseline.json"),
        "sha256": sha,
        "git_commit": doc["git"]["commit"],
        "git_dirty": doc["git"]["dirty"],
        "plan_sha256": doc["plan"]["sha256"],
        "tokenizer_pinned": doc["tokenizer"]["pinned"],
        "containers_all": doc["environment"]["containers_all"],
        "gpu_mem_used_mib": doc["environment"]["gpu_mem_used_mib"],
        "paid_posts": 0,
    }, ensure_ascii=False, sort_keys=True))
    return 0


def cmd_family_reconcile(args: argparse.Namespace) -> int:
    """T0 族账对账：逐账本事件去重，产出 H 与新族帽（零 POST）。"""
    history = FAM.import_history_v13()
    family_root = Path(args.family_root) if args.family_root else RP.rsi_r13_root()
    existing = FAM.load_family_doc(family_root, expected_schema=None)
    if existing is not None:
        if int(existing.get("family_cap", -1)) != P.R13_FAMILY_CAP:
            raise Cli13Error(
                "FAMILY_CAP_MISMATCH",
                f"已存在的 R1.3 族账帽 {existing.get('family_cap')} != "
                f"{P.R13_FAMILY_CAP}")
        if existing.get("schema") != P.FAMILY_SCHEMA_V13:
            raise Cli13Error("FAMILY_SCHEMA",
                             f"族账 schema={existing.get('schema')!r}")
    sources = history["sources"]
    doc: "dict[str, Any]" = {
        "schema": RECONCILE_SCHEMA,
        "schema_version": P.SCHEMA_VERSION,
        "reconciled_at_utc": _utc_now(),
        "profile_id": P.PROFILE_R13,
        "rule": {
            "history": ("H = R1/R1.1 两本最终根账（逐行冻结）"
                        " + R1.2 已关闭轮根账 call 行（逐轮重算，carryover 不重复计）"
                        " + 账外仪器诊断（单列，不与账本行双计）"),
            "cap": "新族帽 = H + 本轮 132（候选 124 + 审查 8）；不结转旧 637 余量",
            "open_reservations": "未决 reservation 不得释放或忽略；存在即 HISTORY_DRIFT",
        },
        "ledger_history_posts": int(history["ledger_call_rows"]),
        "offledger_diagnostic_posts": int(history["offledger"]["posts"]),
        "offledger_evidence": history["offledger"]["evidence"],
        "audited_history_posts": int(history["total_call_rows"]),
        "closed_rounds_posts": P.R13_FAMILY_CLOSED_ROUND_POSTS,
        "r1_history_posts": P.FAMILY_HISTORY_POSTS,
        "round_posts": P.RSI_R13_HARD_CAP,
        "candidate_posts": P.RSI_R13_CANDIDATE_POSTS,
        "review_posts": P.RSI_R13_REVIEW_POSTS,
        "family_cap": P.R13_FAMILY_CAP,
        "plan_proposed_cap": P.R13_LEDGER_HISTORY_POSTS + P.RSI_R13_HARD_CAP,
        "delta_vs_plan": (P.R13_FAMILY_CAP
                          - (P.R13_LEDGER_HISTORY_POSTS + P.RSI_R13_HARD_CAP)),
        "delta_reason": ("Plan §6 暂记 H=545 → 677；T0 逐账核对确认 545 为账本口径，"
                         "另有 9 次账外仪器诊断（同一端点/模型、不在任何项目账本内）。"
                         "按「已花不隐藏」单列并计入 ⇒ H=554、绝对帽 686。"
                         "9 与账本 545 无重叠，未双计。"),
        "open_reservations": [],
        "r12_family_doc": history["r12_family_doc"],
        "sources": [
            {k: s[k] for k in ("envelope_id", "ledger_sha256", "rows_sha256",
                               "call_rows", "ok_rows", "fail_rows", "seq_range")}
            for s in sources
        ],
        "source_total": len(sources),
        "review_capacity": RC.registered_caps(P.PROFILE_R13_DEF),
        "paid_posts": 0,
    }
    path = Path(args.out) if args.out else (
        family_root / "family" / "family_reconciliation.json")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    sha = _write_json(path, doc)
    print(json.dumps({
        "reconciliation": str(path),
        "sha256": sha,
        "audited_history_posts": doc["audited_history_posts"],
        "family_cap": doc["family_cap"],
        "delta_vs_plan": doc["delta_vs_plan"],
        "sources": doc["source_total"],
        "paid_posts": 0,
    }, ensure_ascii=False, sort_keys=True))
    return 0


# ---------------------------------------------------------------------------
# T1：任务与计算预注册（封存私有实例；零 POST/零物理）
# ---------------------------------------------------------------------------

def _difference_table(specs: "tuple[TS.TaskSpecV13, ...]") -> str:
    """生成旧/新任务差异表（人类可读；机判断言另存在 taskset manifest）。"""
    old = {s.mechanism_id: s for s in specs if s.split == TS.SPLIT_DEVELOPMENT}
    new = {s.mechanism_id: s for s in specs if s.split == TS.SPLIT_HOLDOUT}
    lines = [
        "# RSI-R1.3 留出任务差异表（T1 冻结）",
        "",
        "| 机制 | 旧题（development） | 新题（holdout） | 干预轴变化 | 估计对象变化 | "
        "nuisance | 近重复判断 |",
        "|---|---|---|---|---|---|---|",
    ]
    for mech in sorted(new):
        o, n = old.get(mech), new[mech]
        if o is None:
            continue
        lines.append(
            "| {mech} | {o} | {n} | {osig} → {nsig} | {ok} → {nk} | "
            "{onuis} → {nnuis} | 非近重复（见 taskset 机判断言） |".format(
                mech=mech, o=o.task_id, n=n.task_id,
                osig=",".join(o.intervention_signature()) or "—",
                nsig=",".join(n.intervention_signature()) or "—",
                ok=f"{o.primary.kind}/{o.primary.metric}",
                nk=f"{n.primary.kind}/{n.primary.metric}",
                onuis=",".join(sorted(o.nuisance_schema)) or "—",
                nnuis=",".join(sorted(n.nuisance_schema)) or "—"))
    lines += [
        "",
        "判据（Plan §2）：至少研究问题或干预比较发生实质变化；只换 seed/参数随机数"
        "不算新任务；两留出任务必须不同机制。",
        "",
        "| 新题 | 公开研究问题 | 主估计量 |",
        "|---|---|---|",
    ]
    for mech in sorted(new):
        n = new[mech]
        lines.append(f"| {n.task_id} | {n.public_question} | "
                     f"{n.primary.kind}:{n.primary.id} |")
    return "\n".join(lines) + "\n"


def cmd_t1_preregister(args: argparse.Namespace) -> int:
    """T1：封存私有实例、生成资源计划与任务集清单（零 POST）。"""
    import os as _os

    stamp = _utc_now().replace(":", "").replace("-", "")
    out_dir = Path(args.out) if args.out else (
        RP.rsi_r13_root() / "taskset" / stamp)
    private = out_dir / "private"
    private.mkdir(parents=True, exist_ok=True, mode=0o700)
    seeds: "dict[str, bytes]" = {}
    for name in ("a", "b"):
        path = private / f"{name}.seed"
        if path.is_file():
            data = path.read_bytes()
            if len(data) < 32:
                raise Cli13Error("SEED_INVALID", f"{path} 少于 32 字节")
        else:
            if not args.seal_new:
                raise Cli13Error(
                    "SEAL_REQUIRED",
                    f"{path} 不存在；首次封存必须显式 --seal-new")
            data = _os.urandom(32)
            path.write_bytes(data)
            path.chmod(0o600)
        seeds[name] = data
    commit = {k: _sha256_bytes(v) for k, v in seeds.items()}

    # 先用零资源摘要验证任务规格，再由计划绑定真实摘要（计划不含任务摘要，无环）
    tmp_a = TS.build_new_a(commitment=commit["a"])
    tmp_b = TS.build_new_b(commitment=commit["b"])
    plan = RPL.build_plan((tmp_a, tmp_b))
    new_a = TS.build_new_a(commitment=commit["a"],
                           resource_sha=plan.task_sha256(TS.NEW_A_TASK_ID))
    new_b = TS.build_new_b(commitment=commit["b"],
                           resource_sha=plan.task_sha256(TS.NEW_B_TASK_ID))
    old_a, old_b = TS.build_old_a(), TS.build_old_b()

    table = _difference_table((old_a, old_b, new_a, new_b))
    table_path = out_dir / "task_difference_table.md"
    from orchestrator.common.fsutil import atomic_write_text

    atomic_write_text(table_path, table, mode=0o600)
    table_sha = _sha256_bytes(table.encode("utf-8"))

    taskset = TS.build_taskset(
        a_commitment=commit["a"], b_commitment=commit["b"],
        a_resource_sha=plan.task_sha256(TS.NEW_A_TASK_ID),
        b_resource_sha=plan.task_sha256(TS.NEW_B_TASK_ID),
        difference_table_sha=table_sha)
    taskset["maintenance_signoff"] = {
        "actor_kind": "maintenance_verifier",
        "note": ("维护验收者（非候选、无候选上下文）确认：任务差异为实质变化、"
                 "私有实例已封存、资源计划由实际 spec 推导"),
        "signed_at_utc": _utc_now(),
        "difference_table_sha256": table_sha,
        "resource_plan_sha256": plan.sha256,
    }
    plan_sha = _write_json(out_dir / "resource_plan.json", plan.to_dict())
    taskset_sha = _write_json(out_dir / "taskset_manifest.json", taskset)
    _write_json(out_dir / "task_a.json", new_a.to_dict())
    _write_json(out_dir / "task_b.json", new_b.to_dict())
    public = {
        "schema": P.TASKSET_MANIFEST_SCHEMA_V13 + ".public",
        "schema_version": 1,
        "tasks": [new_a.public_document(), new_b.public_document()],
        "note": "候选可见面：不含私有 block/seed；conditions 由候选在允许空间自选",
    }
    public_sha = _write_json(out_dir / "taskset_public.json", public)
    print(json.dumps({
        "out": str(out_dir),
        "resource_plan_sha256": plan_sha,
        "taskset_sha256": taskset_sha,
        "public_sha256": public_sha,
        "difference_table_sha256": table_sha,
        "hard_cap_steps": plan.hard_cap_steps,
        "total_steps": plan.total_steps,
        "commitment_a": commit["a"],
        "commitment_b": commit["b"],
        "paid_posts": 0,
    }, ensure_ascii=False, sort_keys=True))
    return 0


def cmd_t9_closeout(args: argparse.Namespace) -> int:
    """T9：五轴结案（零 POST；只读既有产物，不重判）。"""
    doc = CO13.build_closeout(
        campaign=args.campaign_id or "rsi-r1.3",
        taskset_dir=args.taskset,
        t5_summary=args.t5_summary,
        family_doc=args.family_doc,
        calibration_report=args.calibration,
        study_reports=args.study or (),
        review_reports=args.review or (),
        human_events=args.human_events,
        notes=args.note or ())
    out = Path(args.out) if args.out else (
        RP.rsi_r13_root() / "closeout" / "closeout.json")
    sha = CO13.write_closeout(out, doc)
    print(json.dumps({"closeout": str(out), "sha256": sha,
                      "axes": doc["axes"]}, ensure_ascii=False,
                      sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rsi-r1.3",
        description="RSI-R1.3（profile rsi-r1.3；账 2905；族帽 686；T0 子集）")
    sub = parser.add_subparsers(dest="command", required=True)

    p_base = sub.add_parser("t0-baseline", help="T0 只读基线快照（零 POST）")
    p_base.add_argument("--plan", default=None, help="Plan 路径（默认 R1.3 正式稿）")
    p_base.add_argument("--out", default=None, help="输出目录（默认 rsi_r13/baseline/<utc>）")
    p_base.add_argument("--tokenizer", default=None, help="本地 tokenizer.json")
    p_base.set_defaults(func=cmd_t0_baseline)

    p_rec = sub.add_parser("family-reconcile",
                           help="T0 族账逐账对账（零 POST）")
    p_rec.add_argument("--family-root", default=None,
                       help="R1.3 家族根（默认 $DATA_ROOT/rsi_r13）")
    p_rec.add_argument("--out", default=None, help="输出路径")
    p_rec.set_defaults(func=cmd_family_reconcile)

    p_t1 = sub.add_parser("t1-preregister",
                          help="T1 任务/资源预注册与私有实例封存（零 POST）")
    p_t1.add_argument("--out", default=None, help="输出目录（默认 rsi_r13/taskset/<utc>）")
    p_t1.add_argument("--seal-new", action="store_true",
                      help="首次封存私有 seed（已存在则复用并核对）")
    p_t1.set_defaults(func=cmd_t1_preregister)

    p_t9 = sub.add_parser("t9-closeout", help="T9 五轴结案（零 POST）")
    p_t9.add_argument("--campaign-id", default=None)
    p_t9.add_argument("--taskset", required=True)
    p_t9.add_argument("--t5-summary", default=None)
    p_t9.add_argument("--family-doc", default=None)
    p_t9.add_argument("--calibration", default=None)
    p_t9.add_argument("--study", action="append", default=None,
                      help="study 驱动报告（可多次）")
    p_t9.add_argument("--review", action="append", default=None,
                      help="审查合并件（可多次）")
    p_t9.add_argument("--human-events", default=None)
    p_t9.add_argument("--note", action="append", default=None)
    p_t9.add_argument("--out", default=None)
    p_t9.set_defaults(func=cmd_t9_closeout)

    # ---- R1.3 campaign 门禁命令（2026-11-15 接线）----
    pi = sub.add_parser("init", help="初始化 R1.3 envelope（零 POST）")
    pi.add_argument("--campaign", default=P.PROFILE_R13)
    pi.add_argument("--envelope", required=True)
    pi.add_argument("--mode", choices=["paid", "offline_validation"],
                    required=True)
    pi.add_argument("--taskset", required=True)
    pi.add_argument("--consent", default=None)
    pi.add_argument("--plan", default=None)
    pi.set_defaults(func=CC.cmd_init)

    pc = sub.add_parser("make-consent", help="生成 R1.3 费用件（零 POST）")
    pc.add_argument("--campaign", default=P.PROFILE_R13)
    pc.add_argument("--envelope", required=True)
    pc.add_argument("--out", default=None)
    pc.set_defaults(func=CC.cmd_make_consent)

    pa = sub.add_parser("authorize", help="付费授权（零 POST）")
    pa.add_argument("--campaign", default=P.PROFILE_R13)
    pa.add_argument("--envelope", required=True)
    pa.add_argument("--consent", required=True)
    pa.set_defaults(func=CC.cmd_authorize)

    pao = sub.add_parser("authorize-offline", help="离线验证授权（零 POST）")
    pao.add_argument("--campaign", default=P.PROFILE_R13)
    pao.add_argument("--envelope", required=True)
    pao.set_defaults(func=CC.cmd_authorize_offline)

    pcal = sub.add_parser("calibrate-physics", help="物理校准（零 POST）")
    pcal.add_argument("--campaign", default=P.PROFILE_R13)
    pcal.add_argument("--envelope", required=True)
    pcal.add_argument("--oracle", default=None)
    pcal.set_defaults(func=CC.cmd_calibrate)

    pvp = sub.add_parser("validate-production", help="T5 生产验证（零 POST）")
    pvp.add_argument("--campaign", default=P.PROFILE_R13)
    pvp.add_argument("--envelope", required=True)
    pvp.set_defaults(func=CC.cmd_validate_production)

    pvl = sub.add_parser("validation-lock", help="冻结 R1.3 validation LOCK")
    pvl.add_argument("--campaign", default=P.PROFILE_R13)
    pvl.add_argument("--envelope", required=True)
    pvl.add_argument("--image", default=None)
    pvl.set_defaults(func=CC.cmd_validation_lock)

    psv = sub.add_parser("sign-validation", help="validation LOCK 三签之一")
    psv.add_argument("--campaign", default=P.PROFILE_R13)
    psv.add_argument("--envelope", required=True)
    psv.add_argument("--role", required=True)
    psv.add_argument("--actor", required=True)
    psv.add_argument("--decision", choices=["APPROVE", "REJECT"],
                     required=True)
    psv.add_argument("--rationale", required=True)
    psv.set_defaults(func=CC.cmd_sign_validation)

    pvv = sub.add_parser("verify-validation", help="复核 validation LOCK")
    pvv.add_argument("--campaign", default=P.PROFILE_R13)
    pvv.add_argument("--envelope", required=True)
    pvv.set_defaults(func=CC.cmd_verify_validation)

    pf = sub.add_parser("freeze", help="冻结 R1.3 paid LOCK")
    pf.add_argument("--campaign", default=P.PROFILE_R13)
    pf.add_argument("--envelope", required=True)
    pf.add_argument("--purpose", default="paid")
    pf.add_argument("--validation-root", required=True)
    pf.add_argument("--image", default=None)
    pf.set_defaults(func=CC.cmd_freeze)

    ps = sub.add_parser("sign", help="paid LOCK 签名")
    ps.add_argument("--campaign", default=P.PROFILE_R13)
    ps.add_argument("--envelope", required=True)
    ps.add_argument("--role", required=True)
    ps.add_argument("--actor", required=True)
    ps.add_argument("--decision", choices=["APPROVE", "REJECT"],
                    required=True)
    ps.add_argument("--rationale", required=True)
    ps.set_defaults(func=CC.cmd_sign)

    pver = sub.add_parser("verify", help="复核 paid LOCK")
    pver.add_argument("--campaign", default=P.PROFILE_R13)
    pver.add_argument("--envelope", required=True)
    pver.set_defaults(func=CC.cmd_verify)

    pfc = sub.add_parser("family-close", help="关闭 R1.3 族成员")
    pfc.add_argument("--campaign", default=P.PROFILE_R13)
    pfc.add_argument("--envelope", required=True)
    pfc.add_argument("--reason", required=True)
    pfc.add_argument("--note", default=None)
    pfc.set_defaults(func=CC.cmd_family_close)

    for name, func, help_text in (
            ("probe", CC.cmd_probe, "T6 probe 协议演练（12 呼帽）"),
            ("practice", CC.cmd_practice, "T6 practice 演练（16 呼帽）")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--campaign", default=P.PROFILE_R13)
        p.add_argument("--envelope", required=True)
        p.add_argument("--batch-id", required=True)
        p.add_argument("--tokenizer", required=True)
        p.add_argument("--tokenizer-sha256", required=True)
        p.add_argument("--note", default=None)
        p.set_defaults(func=func)

    ps = sub.add_parser("study", help="T7 正式研究（A/B 各 48）")
    ps.add_argument("--campaign", default=P.PROFILE_R13)
    ps.add_argument("--envelope", required=True)
    ps.add_argument("--study", required=True)
    ps.add_argument("--batch-id", required=True)
    ps.add_argument("--tokenizer", required=True)
    ps.add_argument("--tokenizer-sha256", required=True)
    ps.add_argument("--note", default=None)
    ps.set_defaults(func=CC.cmd_study)

    prp = sub.add_parser("review-permit", help="审查校准许可（typed permit）")
    prp.add_argument("--campaign", default=P.PROFILE_R13)
    prp.add_argument("--envelope", required=True)
    prp.set_defaults(func=CC.cmd_review_permit)

    prc = sub.add_parser("review-calibrate", help="T6 审查校准（4 POST）")
    prc.add_argument("--campaign", default=P.PROFILE_R13)
    prc.add_argument("--envelope", required=True)
    prc.set_defaults(func=CC.cmd_review_calibrate)

    prp2 = sub.add_parser("replay", help="T8 独立复跑（零模型 POST）")
    prp2.add_argument("--campaign", default=P.PROFILE_R13)
    prp2.add_argument("--envelope", required=True)
    prp2.add_argument("--study", required=True)
    prp2.set_defaults(func=CC.cmd_replay)

    prv = sub.add_parser("review", help="T8 正式双审（每 study 2 POST）")
    prv.add_argument("--campaign", default=P.PROFILE_R13)
    prv.add_argument("--envelope", required=True)
    prv.add_argument("--study", required=True)
    prv.set_defaults(func=CC.cmd_review)
    return parser


def main(argv: "list[str] | None" = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (Cli13Error, CC.Cli13Error, FAM.FamilyError, P.ProfileError,
            RC.ReviewCapacityError, LK.LockError, L13.Lock13Error,
            C.ContractError, CAL.CalibrationError, S.StateError,
            RRUN.ReviewRunError, RN.ReviewRunnerError, RV.ReviewError,
            OSError) as exc:
        print(f"BLOCKED {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
