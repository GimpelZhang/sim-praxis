"""RSI-R0.2 T0：授权/基线/资产复用/暴露面/228 槽位/旧根封存（零付费、零网络）。

计划 §3.3、§5.1、§7、§8-T0。本脚本只读旧根，产物全部落在新 campaign 根内
（0700/0600）；不触网、不调模型、不改共享模型配置。

用法::

    SIMPRAXIS_RSI_R02_CAMPAIGN=r02_<UTC> \\
    /mnt/venvs/simpraxis/bin/python scripts/rsi_r02/t0_baseline.py \\
        --plan ~/文档/RSI_R0.2_Plan_detailed.md \\
        --consent <campaign>/contract/consent.json \\
        --allow-reason "G-S 早停后用户授权恢复"

重复运行是幂等的：除 `recorded_at_utc` 外的字段逐字节相同；上下文已存在时只做
一致性校验（计划/授权/提交漂移即拒）。
"""
from __future__ import annotations

import argparse
import importlib.metadata as md
import json
import os
import platform
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:  # 允许以脚本路径直接运行时 import 包
    sys.path.insert(0, str(REPO_ROOT))

from orchestrator.common.clock import utc_now_iso_z  # noqa: E402
from orchestrator.common.runtime_paths import (  # noqa: E402
    rsi_r02_campaign_id,
    rsi_r02_root,
)
from orchestrator.rsi_r02 import campaign as C
from orchestrator.rsi_r02 import protocol_fixture as PF  # noqa: E402

#: R0.2 绑定的引擎版本（硬规则 8 的 pinned 集；真值以实测解释器为准）。
PINNED = {
    "genesis-world": "1.3.3",
    "langgraph": "0.2.28",
    "lark-oapi": "1.7.4",
    "mani_skill": "3.0.1",
    "mujoco": "3.15.0",
    "numpy": "1.26.4",
    "sapien": "3.0.3",
    "torch": "2.4.1+cu124",
}

#: R0.1 的 r02 campaign（§5.1：本阶段复用的校准资产来源；只读）。
DEFAULT_SOURCE_CAMPAIGN = Path(
    "/mnt/artifacts/simpraxis/rsi_r01/r02_20261109T081354Z")

#: 验证固定顺序（§6.3）：E1,D1,R1,E2,D2,R2。
VALIDATION_ORDER = ("E-val-1", "D-val-1", "R-val-1", "E-val-2", "D-val-2", "R-val-2")
DEV_ORDER = ("E-dev", "D-dev", "R-dev")          # §6.2 E-dev→D-dev→R-dev
PROTOCOL_CHAINS = ("A", "B")
#: 每链 4 个必做动作 + 2 个**自修余量槽**（2026-11-10 用户裁决「开新票据重做协议链」：
#: 首跑 P1 在 plan 字段缺失上耗掉 4 槽后链 FAIL，根因之一是"零余量"设计——坏 JSON /
#: 缺字段 / prereg 不合法都算一次 POST，修正却无槽可用）。余量槽 required_action=None：
#: 任何动作都可落槽，但**不豁免**动作顺序与最终 submit 契约（verify_run 判据不变）。
#: 槽数口径只从 ``protocol_fixture`` 取，本文件不写 6/12 字面量（评审 MINOR-3）。
PROTOCOL_ROUNDS = tuple(
    (str(i), act) for i, act in enumerate(
        list(PF.ACTION_ORDER) + [None] * PF.REPAIR_SLOTS_PER_CHAIN, start=1))
PER_TRIAL_CAP = 24
FAMILY_CAP = 72
PROTOCOL_CAP = PF.TOTAL_PROTOCOL_SLOTS
TOTAL_HARD_CAP = PROTOCOL_CAP + 3 * PER_TRIAL_CAP + 6 * PER_TRIAL_CAP

#: 本 campaign 的实现面文件（LOCK 前置清单的一部分；后续 Task 追加）。
CODE_FILES = (
    "orchestrator/common/runtime_paths.py",
    "orchestrator/gateway/quota_ledger.py",
    "orchestrator/rsi_r02/__init__.py",
    "orchestrator/rsi_r02/campaign.py",
    "scripts/rsi_r02/t0_baseline.py",
)


def _sha(path: "str | Path") -> str:
    return C.sha256_file(path)


def _now() -> str:
    return utc_now_iso_z()


def _git(repo_root: Path) -> "dict[str, Any]":
    def run(*args: str) -> str:
        return subprocess.run(["git", "-C", str(repo_root), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    dirty = run("status", "--porcelain")
    return {
        "branch": run("rev-parse", "--abbrev-ref", "HEAD"),
        "commit": run("rev-parse", "HEAD"),
        "dirty": bool(dirty),
        "dirty_entries": len([ln for ln in dirty.splitlines() if ln.strip()]),
        "head_author": run("log", "-1", "--format=%an <%ae>"),
    }


def _versions() -> "dict[str, Any]":
    """实测依赖版本；别名候选都试（发行名与 import 名不同）。"""
    aliases = {
        "mani_skill": ("mani_skill", "mani-skill"),
        "genesis-world": ("genesis-world", "genesis"),
    }
    got: "dict[str, Any]" = {}
    for dist, want in PINNED.items():
        names = aliases.get(dist, (dist,))
        found = None
        for name in names:
            try:
                found = md.version(name)
                break
            except md.PackageNotFoundError:
                continue
        got[dist] = found
    return got


def stage_models(path: Path) -> "dict[str, Any]":
    """读唯一模型配置点（只读；与 R0.1 lock.stage_models 同口径，独立实现）。"""
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    stages = (doc.get("stages") or doc.get("stage_models") or {})
    models = {name: (cfg or {}).get("model_id") for name, cfg in stages.items()
              if isinstance(cfg, Mapping)}
    meta = doc.get("meta") or doc.get("gateway") or {}
    return {"path": str(path), "sha256": _sha(path), "models": models,
            "endpoint": meta.get("endpoint"),
            "base_url_env": meta.get("base_url_env")}


def build_slots() -> "dict[str, Any]":
    """228 槽位表（§7 预算表自修余量版：协议 12 = 2×(4+2) + 开发 3×24 + 验证 6×24）。

    槽位是**分配表**，不是已花额度：初始 status=UNUSED。发网前阻断（preflight_
    blocked）不占槽；UNKNOWN_SENT 占槽（§10.2）；未用额度作废不挪用。
    """
    slots: "list[dict[str, Any]]" = []
    for chain in PROTOCOL_CHAINS:
        for round_no, action in PROTOCOL_ROUNDS:
            slots.append({
                "slot_id": f"P-{chain}-{round_no}",
                "phase": "protocol",
                "family": "protocol",
                "chain": chain,
                "round": int(round_no),
                "required_action": action,
                "post_cap": 1,
                "status": "UNUSED",
            })
    for family in ("E", "D", "R"):
        for i in range(1, PER_TRIAL_CAP + 1):
            slots.append({
                "slot_id": f"{family}-dev-{i:02d}",
                "phase": "dev",
                "family": family,
                "instance_id": f"{family}-dev",
                "index": i,
                "post_cap": 1,
                "status": "UNUSED",
            })
    order_index = 1
    for instance_id in VALIDATION_ORDER:
        family = instance_id[0]
        for i in range(1, PER_TRIAL_CAP + 1):
            slots.append({
                "slot_id": f"{family}-val-{order_index}-{i:02d}",
                "phase": "validation",
                "family": family,
                "instance_id": instance_id,
                "validation_order": order_index,
                "index": i,
                "post_cap": 1,
                "status": "UNUSED",
            })
        order_index += 1
    counts = {"protocol": 0, "dev": 0, "validation": 0}
    for s in slots:
        counts[s["phase"]] += 1
    by_family: "dict[str, int]" = {}
    for s in slots:
        if s["phase"] != "protocol":
            by_family[s["family"]] = by_family.get(s["family"], 0) + 1
    total = len(slots)
    expect = {"protocol": PROTOCOL_CAP, "dev": 3 * PER_TRIAL_CAP,
              "validation": 6 * PER_TRIAL_CAP}
    if total != TOTAL_HARD_CAP or counts != expect:
        raise SystemExit(f"槽位表自检失败：total={total} counts={counts}")
    if any(v != FAMILY_CAP for v in by_family.values()):
        raise SystemExit(f"槽位表族帽自检失败：{by_family}")
    return {
        "schema": "simpraxis.rsi_r02.slots.v1",
        "recorded_at_utc": _now(),
        "allocation": {
            "protocol": PROTOCOL_CAP,
            "dev": {f: PER_TRIAL_CAP for f in ("E", "D", "R")},
            "validation": {i: PER_TRIAL_CAP for i in VALIDATION_ORDER},
        },
        "order": {"dev": list(DEV_ORDER), "validation": list(VALIDATION_ORDER),
                  "protocol": list(PROTOCOL_CHAINS)},
        "rules": [
            "UNKNOWN_SENT 占槽（§10.2）；发网前阻断（preflight_blocked）不占槽",
            "未用额度作废不挪用（§7）",
            "槽位状态只能经 TrialRunner/账本机判迁移，本文件只记录分配",
        ],
        "counts": {"total": total, "by_phase": counts, "by_family": by_family},
        "slots": slots,
    }


def build_asset_reuse(source_campaign: Path) -> "dict[str, Any]":
    """§5.1：复用 R0.1 r02 的三开发+六验证实例；内容 hash 全登记。"""
    src = Path(source_campaign)
    if not src.is_dir():
        raise SystemExit(f"来源 campaign 不存在：{src}")
    instances: "list[dict[str, Any]]" = []
    for inst in (*DEV_ORDER, *VALIDATION_ORDER):
        family, role = inst[0], ("dev" if inst.endswith("dev") else "validation")
        pub = src / "public" / "tasks" / inst
        priv = src / "private" / "instances" / f"{inst}.json"
        pub_tree = C.hash_tree(pub)
        instances.append({
            "instance_id": inst,
            "family": family,
            "role": role,
            "public_task": {
                "path": str(pub),
                "exists": pub_tree["exists"],
                "n_files": pub_tree["n_files"],
                "manifest_sha256": pub_tree["manifest_sha256"],
                "files": [{"path": e["path"], "sha256": e["sha256"]}
                          for e in pub_tree["entries"]],
            },
            "private_instance": {
                "path": str(priv),
                "exists": priv.is_file(),
                "sha256": _sha(priv) if priv.is_file() else None,
            },
        })
    calibration_dir = src / "analysis" / "calibration"
    calibration = {
        p.name: _sha(p) for p in sorted(calibration_dir.glob("*.json"))
    } if calibration_dir.is_dir() else {}
    task_matrix = src / "private" / "task_matrix.json"
    selection = src / "contract" / "task_selection.json"
    return {
        "schema": "simpraxis.rsi_r02.asset_reuse.v1",
        "recorded_at_utc": _now(),
        "source_campaign": str(src),
        "source_campaign_id": src.name,
        "source_read_only": True,
        "instances": instances,
        "hashes": {
            "task_matrix": _sha(task_matrix) if task_matrix.is_file() else None,
            "task_selection": _sha(selection) if selection.is_file() else None,
            "calibration": calibration,
        },
        "reuse_impact": {
            "direct_reuse": [
                "公开任务目录（public/tasks/*）：逐件 hash 见 instances[].public_task",
                "私有实例真值（private/instances/*.json）：只在新 root 重生成/复制，旧根只读",
                "任务矩阵与科学阈值（private/task_matrix.json）：E 距离/速度容差、D 故障族、R 阻尼/初角/窗长",
                "标定资产（analysis/calibration/*.json）：R 可辨识性/收敛/数值误差预算作为依赖记录",
            ],
            "must_reverify_in_r02": [
                "候选 runner（全新）：model reply→dispatch→真实观测→后继请求，T2/T3 需控制重跑",
                "计费/传输 adapter（全新）：预留-结算-UNKNOWN 语义，T2 控制全测",
                "协议链（两条新 fixture）：T4 准入，不复用 R0.1 长链结论",
                "D 判据：若仅验修复功能，必须标 repair_functional_success，诊断归因另列未验证",
                "R 固定四格矩阵：值不变即无需重标，但需 T3 同路径 reference 证明链完整",
            ],
            "not_reused": [
                "R0.1 T6 长上下文测量仪器（IF-1/IF-2 缺陷已判负，不得沿用）",
                "R0.1 付费账/attempts/预注册假设（新旧账绝不并表）",
                "R0.1 LOCK 双签（R0.2 必须自建 LOCK）",
            ],
        },
        "visibility": ("校准域内隔离验证：内容对新 candidate 未运行过，但维护者/"
                       "公开文档已知部分参数与参考答案；不得称 R2 未见机制确认。"),
    }


def build_exposure_map(asset_reuse: "Mapping[str, Any]") -> "dict[str, Any]":
    """§5.1：谁见过任务/真值/参考、是否公开、哪些源可进入 candidate。"""
    entries: "list[dict[str, Any]]" = []
    for inst in asset_reuse.get("instances", []):
        inst_id = inst["instance_id"]
        entries.append({
            "instance_id": inst_id,
            "task_text": "public（复制到新 root public/tasks）",
            "private_truth": "private（新 root private/，候选不可见）",
            "reference_program": "maintainer-only（只进控制 lane，候选 lane 禁用）",
            "maintainer_has_seen": True,
            "public_docs_may_expose": True,
            "candidate_has_run_before": False,
            "notes": ("candidate 此前未运行该实例；同模型可能有先验知识，"
                      "报告须列此限制，不把权限隔离当训练数据无污染证明"),
        })
    return {
        "schema": "simpraxis.rsi_r02.exposure_map.v1",
        "recorded_at_utc": _now(),
        "principle": ("登记谁能看到任务/真值/参考、是否公开、哪些源可进入 candidate；"
                      "若实际执行侧把答案/参考源码喂给候选，该题标污染，"
                      "不能删掉这题继续凑通过"),
        "candidate_mount_allowlist": [
            "当前 trial 的公开任务副本（public/tasks/<instance>）",
            "候选自身工作区（每 trial 新会话）",
            "当前 trial 的观测目录（observations/public/<trial>）",
        ],
        "candidate_denylist": [
            "完整 repo（/home/vipuser/sim-praxis）",
            "KB 仓与向量库（~/embodied-ai-sim-knowledge-base、$KB_ROOT）",
            "任何 Complete/Plan/复盘文档",
            "旧 campaign 根（rsi_r0、rsi_r01 全族）与旧 trace",
            "reference 程序与参考源码",
            "私有实例真值（private/instances、private/task_matrix 私有段）",
            "全局 memory / 跨 trial 技能",
        ],
        "instances": entries,
    }


def build_baseline(ctx: C.CampaignContext, repo_root: Path,
                   models: "Mapping[str, Any]", git: "Mapping[str, Any]",
                   versions: "Mapping[str, Any]") -> "dict[str, Any]":
    code_hashes = {}
    for rel in CODE_FILES:
        p = repo_root / rel
        code_hashes[rel] = _sha(p) if p.is_file() else None
    import orchestrator.gateway.quota_ledger as Q  # 局部导入：避免顶层副作用

    return {
        "schema": "simpraxis.rsi_r02.baseline.v1",
        "recorded_at_utc": _now(),
        "campaign_id": ctx.campaign_id,
        "campaign_root": str(ctx.root),
        "namespace": ctx.namespace,
        "plan": {"path": ctx.plan_path, "sha256": ctx.plan_sha256},
        "consent": {"path": ctx.consent_path, "sha256": ctx.consent_sha256},
        "git": dict(git),
        "python": {"executable": sys.executable,
                   "version": platform.python_version(),
                   "platform": platform.platform()},
        "dependencies": dict(versions),
        "pinned_expected": dict(PINNED),
        "stage_models": dict(models),
        "model_change_this_campaign": False,
        "namespace_scan": {
            "occupied": [2, 25, 26, 27, 281, 2900, 2901],
            "assigned": Q.LEDGER_RSI_R02,
            "task_caps": {str(k): v for k, v in Q.RSI_R02_TASK_CAPS.items()},
            "hard_cap": Q.RSI_R02_HARD_CAP,
            "normal_budget": Q.RSI_R02_NORMAL_BUDGET,
            "sum_task_caps": sum(Q.RSI_R02_TASK_CAPS.values()),
        },
        "spend_basis": {
            "mode": "user_waived_self_declared_upper_bound",
            "per_post_reservation_usd": 1.5,
            "batch_cap_usd": 100.0,
            "evidence": "contract/consent.json#waiver + contract/spend_contract.json",
        },
        "paid_calls": 0,
        "code_hashes": code_hashes,
        "notes": [
            "code_commit 绑定 T0 开工 HEAD；最终 LOCK 由 T3 绑实现面全量 hash。",
            "stage_models.yaml 是模型 ID 唯一源；本 campaign 不写死模型 ID、不换供。",
        ],
    }


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--campaign-id", default=None,
                    help="缺省取 SIMPRAXIS_RSI_R02_CAMPAIGN")
    ap.add_argument("--root", default=None, help="缺省 $DATA_ROOT/rsi_r02")
    ap.add_argument("--repo-root", default=str(REPO_ROOT))
    ap.add_argument("--plan", default=str(Path.home() / "文档" / "RSI_R0.2_Plan_detailed.md"))
    ap.add_argument("--consent", default=None,
                    help="缺省 <campaign>/contract/consent.json")
    ap.add_argument("--source-campaign", default=str(DEFAULT_SOURCE_CAMPAIGN))
    ap.add_argument("--allow-reason", default="",
                    help="attach 既有 T0 早停根时必须给出的偏差理由")
    args = ap.parse_args(argv)

    repo_root = Path(args.repo_root)
    campaign_id = args.campaign_id or rsi_r02_campaign_id()
    base = Path(args.root) if args.root else rsi_r02_root()
    campaign_root = (base / campaign_id).resolve(strict=False)
    consent = Path(args.consent) if args.consent else campaign_root / "contract" / "consent.json"

    if (campaign_root / "contract" / C.CONTEXT_FILENAME).is_file():
        ctx = C.load_campaign(campaign_id=campaign_id, root=base)
    else:
        ctx = C.attach_campaign_context(
            campaign_id=campaign_id, plan_path=args.plan, consent_path=consent,
            repo_root=repo_root, root=base, allow_reason=args.allow_reason)

    models = stage_models(repo_root / "orchestrator/gateway/stage_models.yaml")
    git = _git(repo_root)
    versions = _versions()
    asset_reuse = build_asset_reuse(Path(args.source_campaign))
    exposure = build_exposure_map(asset_reuse)
    slots = build_slots()
    baseline = build_baseline(ctx, repo_root, models, git, versions)

    written = {
        "baseline": str(C.write_json(ctx, "contract/baseline.json", baseline)),
        # D-T4-2：协议合同必须在冻结前真的落盘，且被 LOCK 逐件钉哈希。
        "protocol_contract": str(C.write_json(
            ctx, "contract/protocol_contract.json",
            PF.contract_payload(campaign_id=ctx.campaign_id))),
        "asset_reuse": str(C.write_json(ctx, "contract/asset_reuse.json", asset_reuse)),
        "exposure_map": str(C.write_json(ctx, "contract/exposure_map.json", exposure)),
        "slots": str(C.write_json(ctx, "quota/slots.json", slots)),
    }

    snapshot = C.snapshot_protected()
    snap_path = C.write_json(ctx, "contract/sealed_snapshot.json", snapshot)
    diff = C.verify_protected(snapshot)
    written["sealed_snapshot"] = str(snap_path)

    summary = {
        "campaign_id": ctx.campaign_id,
        "campaign_root": str(ctx.root),
        "namespace": ctx.namespace,
        "models": models["models"],
        "stage_models_sha256": models["sha256"],
        "git": git,
        "slots": {"total": slots["counts"]["total"],
                  "by_phase": slots["counts"]["by_phase"],
                  "by_family": slots["counts"]["by_family"]},
        "instances": len(asset_reuse["instances"]),
        "sealed_diff": diff,
        "written": written,
        "paid_calls": 0,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if diff:
        raise SystemExit(f"旧根封存校验失败（写后重算有差异）：{diff}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
