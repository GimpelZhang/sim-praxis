"""RSI-R0.2 T1：最小资产适配与接口合同（计划 §8-T1，AC06–AC09）。

职责边界（刻意窄）：

* **只读** R0.1 的 r02 campaign（`public/tasks/*`、`private/instances/*`、
  `private/task_matrix.json`），把公开件/私有件**分别复制**进新 campaign 根；
* 逐项绑定任务/场景/故障/单位/观测/分析容器/判据的 hash 与常量来源；
* 产出 `contract/task_contract.json`、`contract/reuse_impact.json`、
  `contract/public_api.json`；
* **不复制引擎**：观测/判据/分析容器的实现仍由 R0.1 模块提供（只读依赖），
  本模块只把它们的文件 sha 与常量值钉进合同——这样最终 LOCK 能发现任何漂移。

不触网、不调模型、不写旧根。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from orchestrator.common.clock import utc_now_iso_z  # noqa: E402
from orchestrator.rsi_r01 import observations as OBS  # noqa: E402
from orchestrator.rsi_r01 import schemas as S  # noqa: E402
from orchestrator.rsi_r01 import task_contract as TC  # noqa: E402
from orchestrator.rsi_r02 import campaign as C  # noqa: E402

TASK_CONTRACT_SCHEMA = "simpraxis.rsi_r02.task_contract.v1"
REUSE_IMPACT_SCHEMA = "simpraxis.rsi_r02.reuse_impact.v1"
PUBLIC_API_SCHEMA = "simpraxis.rsi_r02.public_api.v1"

#: 私有真值键——出现在公开件里即公私隔离失败（AC07）。
PRIVATE_ONLY_KEYS = frozenset({
    "reference_gains",
    "reference_steps",
    "tolerance",
    "fault",
    "cells",
    "expected_mechanism",
    "settle_band",
    "evidence_grade",
    "param_bounds",
})

FAMILY_UNITS = {
    "E": {"distance": "m", "final_speed": "m/s", "force": "N"},
    "D": {"distance": "m", "final_speed": "m/s", "force": "N"},
    "R": {"theta": "rad", "omega": "rad/s", "damping": "N*m*s/rad",
          "time": "s", "effect": "dimensionless"},
}

#: 判据绑定（**实测**于 R0.1 代码：D 只验功能，不验诊断归因）。
JUDGES = {
    "E": {"module": "orchestrator/rsi_r01/evaluator.py",
          "function": "evaluate_engineering", "layer": "L4",
          "verdict_type": "repair_functional_success",
          "diagnosis_attribution": "N/A（E 族无诊断任务）"},
    "D": {"module": "orchestrator/rsi_r01/evaluator.py",
          "function": "evaluate_engineering", "layer": "L4",
          "verdict_type": "repair_functional_success",
          "diagnosis_attribution": "NOT_VERIFIED_IN_R02（现判据只看 distance/final_speed；"
                                   "诊断陈述另列，不参与成功判定）"},
    "R": {"module": "orchestrator/rsi_r01/evaluator.py",
          "function": "judge_research", "layer": "L5",
          "verdict_type": "research_hypothesis",
          "diagnosis_attribution": "N/A（R 族是公开假设检验）"},
}


class AdapterError(RuntimeError):
    """资产缺失/公私隔离失败/复制后 hash 不符。"""


def _sha(path: "str | Path") -> str:
    return C.sha256_file(path)


def _now() -> str:
    return utc_now_iso_z()


def _walk_keys(obj: Any, out: "set[str] | None" = None) -> "set[str]":
    out = set() if out is None else out
    if isinstance(obj, Mapping):
        for k, v in obj.items():
            out.add(str(k))
            _walk_keys(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _walk_keys(v, out)
    return out


def public_leaks(public_doc: Mapping[str, Any],
                 private_doc: Mapping[str, Any]) -> "list[str]":
    """公开件里出现的私有键（按私有件里**实际存在的**键判定，不按想象列）。

    只报「私有件真有、公开件也有」的键：这样 E 族没有的键不会误报，D 族
    `fault` 若被误放进公开件则必报。
    """
    priv_keys: "set[str]" = set()
    _walk_keys(private_doc, priv_keys)
    pub_keys: "set[str]" = set()
    _walk_keys(public_doc, pub_keys)
    hits = sorted((priv_keys & pub_keys) & PRIVATE_ONLY_KEYS)
    return hits


def load_source(source_campaign: "str | Path") -> "dict[str, Any]":
    src = Path(source_campaign)
    if not src.is_dir():
        raise AdapterError(f"来源 campaign 不存在：{src}")
    instances: "dict[str, Any]" = {}
    for inst_id in (*TC.DEV_IDS, *TC.VALIDATION_IDS):
        pub_path = src / "public" / "tasks" / inst_id / "task.json"
        priv_path = src / "private" / "instances" / f"{inst_id}.json"
        for p in (pub_path, priv_path):
            if not p.is_file():
                raise AdapterError(f"来源缺件：{p}")
        pub = json.loads(pub_path.read_text(encoding="utf-8"))
        priv = json.loads(priv_path.read_text(encoding="utf-8"))
        instances[inst_id] = {"public": pub, "private": priv,
                              "public_sha": _sha(pub_path),
                              "private_sha": _sha(priv_path),
                              "public_path": pub_path, "private_path": priv_path}
    matrix = src / "private" / "task_matrix.json"
    return {
        "root": src,
        "instances": instances,
        "task_matrix_sha": _sha(matrix) if matrix.is_file() else None,
        "task_matrix_path": matrix,
    }


def copy_assets(ctx: C.CampaignContext, src: "Mapping[str, Any]") -> "dict[str, Any]":
    """把公开/私有件逐字节复制进新根；读回比对 sha（copy→verify→record）。"""
    copied: "dict[str, Any]" = {"public": [], "private": []}
    for inst_id, rec in src["instances"].items():
        pub_text = rec["public_path"].read_text(encoding="utf-8")
        rel_pub = f"public/tasks/{inst_id}/task.json"
        ctx.write_text(rel_pub, pub_text)
        back = (ctx.root / rel_pub).read_bytes()
        if back != rec["public_path"].read_bytes():
            raise AdapterError(f"公开件复制后字节不符：{rel_pub}")
        copied["public"].append({"path": rel_pub, "sha256": rec["public_sha"]})

        priv_text = rec["private_path"].read_text(encoding="utf-8")
        rel_priv = f"private/instances/{inst_id}.json"
        ctx.write_text(rel_priv, priv_text)
        if (ctx.root / rel_priv).read_bytes() != rec["private_path"].read_bytes():
            raise AdapterError(f"私有件复制后字节不符：{rel_priv}")
        copied["private"].append({"path": rel_priv, "sha256": rec["private_sha"]})
    if src["task_matrix_path"].is_file():
        rel = "private/task_matrix.json"
        ctx.write_text(rel, src["task_matrix_path"].read_text(encoding="utf-8"))
        if (ctx.root / rel).read_bytes() != src["task_matrix_path"].read_bytes():
            raise AdapterError("task_matrix 复制后字节不符")
        copied["private"].append({"path": rel, "sha256": src["task_matrix_sha"]})
    return copied


def observation_spec() -> "dict[str, Any]":
    """观测合同常量（来源 R0.1 schemas/observations 模块，只读依赖）。"""
    return {
        "kinds": {"push": S.OBS_KIND_PUSH, "pendulum": S.OBS_KIND_PENDULUM},
        "dt_s": S.DT_S, "n_steps": S.N_STEPS, "expected_rows": S.EXPECTED_ROWS,
        "t_end_s": S.T_END_S,
        "bands": {"theta_rad": S.THETA_BAND, "omega_rad_s": S.OMEGA_BAND,
                  "delta": S.DELTA},
        "max_observation_bytes": S.MAX_OBSERVATION_BYTES,
        "max_trial_observation_bytes": S.MAX_TRIAL_OBSERVATION_BYTES,
        "trusted_service": {
            "image": OBS.IMAGE_REF,
            "image_digest": OBS.IMAGE_DIGEST,
            "service_script": str(OBS.SERVICE_SCRIPT),
            "service_script_sha256": _sha(OBS.SERVICE_SCRIPT),
            "timeout_s": OBS.DEFAULT_TIMEOUT_S,
        },
        "source_module": {
            "path": "orchestrator/rsi_r01/schemas.py",
            "sha256": _sha(REPO_ROOT / "orchestrator/rsi_r01/schemas.py"),
        },
        "source_observations_module": {
            "path": "orchestrator/rsi_r01/observations.py",
            "sha256": _sha(REPO_ROOT / "orchestrator/rsi_r01/observations.py"),
        },
    }


def analysis_spec() -> "dict[str, Any]":
    """分析容器合同（受限容器 + 配额常量，来源 R0.1 analysis_runner/schemas）。"""
    return {
        "image": OBS.IMAGE_REF,
        "image_digest": OBS.IMAGE_DIGEST,
        "limits": {
            "max_source_bytes": S.ANALYSIS_MAX_SOURCE_BYTES,
            "max_stream_bytes": S.ANALYSIS_MAX_STREAM_BYTES,
            "max_output_file_bytes": S.ANALYSIS_MAX_OUTPUT_FILE_BYTES,
            "max_per_trial": S.ANALYSIS_MAX_PER_TRIAL,
            "timeout_s": S.ANALYSIS_TIMEOUT_S,
            "cpus": S.ANALYSIS_CPUS, "memory": S.ANALYSIS_MEMORY,
            "pids": S.ANALYSIS_PIDS,
            "output_names": list(S.ANALYSIS_OUTPUT_NAMES),
            "entry_name": S.ANALYSIS_ENTRY_NAME,
        },
        "isolation": ["no-network", "non-root", "read-only root", "cap-drop ALL",
                      "no-new-privileges", "no docker.sock"],
        "source_module": {
            "path": "orchestrator/rsi_r01/analysis_runner.py",
            "sha256": _sha(REPO_ROOT / "orchestrator/rsi_r01/analysis_runner.py"),
        },
    }


def build_contract(ctx: C.CampaignContext, src: "Mapping[str, Any]") -> "dict[str, Any]":
    obs = observation_spec()
    ana = analysis_spec()
    instances: "list[dict[str, Any]]" = []
    for inst_id, rec in src["instances"].items():
        pub, priv = rec["public"], rec["private"]
        family = str(priv.get("family") or pub.get("family"))
        leaks = public_leaks(pub, priv)
        if leaks:
            raise AdapterError(f"{inst_id} 公开件泄漏私有键：{leaks}（AC07）")
        judge = dict(JUDGES[family])
        judge["module_sha256"] = _sha(REPO_ROOT / judge["module"])
        instances.append({
            "instance_id": inst_id,
            "family": family,
            "template": priv.get("template") or pub.get("template"),
            "role": priv.get("role") or pub.get("role"),
            "hashes": {"public": rec["public_sha"], "private": rec["private_sha"],
                       "public_relpath": f"public/tasks/{inst_id}/task.json",
                       "private_relpath": f"private/instances/{inst_id}.json"},
            "public_interface": pub.get("public", {}).get("interface"),
            "public_goal": pub.get("public", {}).get("goal"),
            "public_research": pub.get("public", {}).get("research"),
            "private_keys": sorted(_walk_keys(priv, set())),
            "units": FAMILY_UNITS[family],
            "judge": judge,
            "observation": {k: obs[k] for k in
                            ("kinds", "dt_s", "n_steps", "expected_rows",
                             "t_end_s", "bands")},
            "thresholds_source": "private/instances/<id>.json#tolerance 或 #settle_band"
                                 "（私有件，候选不可见；由判据侧读取）",
        })
    return {
        "schema": TASK_CONTRACT_SCHEMA,
        "recorded_at_utc": _now(),
        "campaign_id": ctx.campaign_id,
        "source_campaign": str(src["root"]),
        "source_read_only": True,
        "instances": instances,
        "observation_contract": obs,
        "analysis_contract": ana,
        "judge_modules": {
            "evaluator": {"path": "orchestrator/rsi_r01/evaluator.py",
                          "sha256": _sha(REPO_ROOT / "orchestrator/rsi_r01/evaluator.py")},
            "task_contract": {"path": "orchestrator/rsi_r01/task_contract.py",
                              "sha256": _sha(REPO_ROOT / "orchestrator/rsi_r01/task_contract.py")},
        },
        "d_diagnosis_note": ("R0.2 的 D 判据**只验修复功能**（distance/final_speed 容差），"
                             "故 success 一律记 repair_functional_success；诊断陈述"
                             "（故障归因）另列 NOT_VERIFIED，不放大为因果诊断成功。"),
    }


def build_public_api(contract: Mapping[str, Any]) -> "dict[str, Any]":
    """AC08 的**静态**半边：公开接口字段清单（动态充分性由 T3 reference 证明）。"""
    entries = []
    for inst in contract["instances"]:
        iface = inst.get("public_interface") or {}
        entries.append({
            "instance_id": inst["instance_id"],
            "family": inst["family"],
            "kind": iface.get("kind"),
            "keys": list(iface.get("keys") or []),
            "limits": iface.get("limits"),
            "steps_max": iface.get("steps_max"),
            "public_goal": inst.get("public_goal"),
            "public_research": inst.get("public_research"),
        })
    return {
        "schema": PUBLIC_API_SCHEMA,
        "recorded_at_utc": _now(),
        "action_protocol": {
            "format": "单条文本 JSON（无原生 tool call）",
            "allowed_actions": ["preregister", "run_experiment", "analyze", "submit"],
            "action_schema_source": "orchestrator/rsi_r01/actions.py",
            "action_schema_sha256": _sha(REPO_ROOT / "orchestrator/rsi_r01/actions.py"),
        },
        "instances": entries,
        "sufficiency_status": "STATIC_ONLY（动态充分性须由 T3 的九实例 reference "
                              "与两协议 fixture 全通过证明；本文件不预支该结论）",
    }


def build_reuse_impact(ctx: C.CampaignContext, src: "Mapping[str, Any]",
                       contract: Mapping[str, Any]) -> "dict[str, Any]":
    deps = {
        "source_public": {i["instance_id"]: i["hashes"]["public"]
                          for i in contract["instances"]},
        "source_private": {i["instance_id"]: i["hashes"]["private"]
                           for i in contract["instances"]},
        "source_task_matrix": src["task_matrix_sha"],
        "evaluator": contract["judge_modules"]["evaluator"]["sha256"],
        "task_contract": contract["judge_modules"]["task_contract"]["sha256"],
        "schemas": contract["observation_contract"]["source_module"]["sha256"],
        "observations": contract["observation_contract"]["source_observations_module"]["sha256"],
        "analysis_runner": contract["analysis_contract"]["source_module"]["sha256"],
        "trusted_service_script": contract["observation_contract"]["trusted_service"]["service_script_sha256"],
        "container_image": contract["observation_contract"]["trusted_service"]["image"],
    }
    return {
        "schema": REUSE_IMPACT_SCHEMA,
        "recorded_at_utc": _now(),
        "campaign_id": ctx.campaign_id,
        "dependency_hashes": deps,
        "decisions": [
            {"item": "任务/场景/故障定义（public/tasks + private/instances + task_matrix）",
             "decision": "REUSED_UNCHANGED", "basis": "逐字节相同（copy 后读回比对）"},
            {"item": "观测实现（trusted service + schemas 常量）",
             "decision": "REUSED_IF_UNCHANGED",
             "basis": "模块 sha 已钉；任何改动 ⇒ 重跑对应校准与独立 oracle，"
                      "失败即 STOP，不改阈值"},
            {"item": "分析容器（受限 docker 运行）",
             "decision": "REUSED_IF_UNCHANGED", "basis": "同上，绑 analysis_runner sha"},
            {"item": "判据（evaluate_engineering / judge_research）",
             "decision": "REUSED_IF_UNCHANGED",
             "basis": "绑 evaluator sha；D 仅功能判据，诊断归因不参与"},
            {"item": "候选 runner / 计费 / 状态机（R0.2 新代码）",
             "decision": "REVERIFY_IN_T3",
             "basis": "路径与 runner 改变 ⇒ 必须真实集成控制（T3），不能凭旧证据推断"},
            {"item": "R0.1 T6 长链测量仪器",
             "decision": "NOT_REUSED", "basis": "IF-1/IF-2 缺陷已判负"},
        ],
        "revalidation_required": [
            "若 evaluator/schemas/observations/analysis_runner 任一 sha 变化：重跑 T3 控制与独立 oracle",
            "若任务/私有真值任一 sha 变化：该实例本轮不得计入验证（污染）",
            "路径/runner 变化本身不要求重跑物理校准，但要求 T3 的同路径集成控制",
        ],
        "d_verdict_scope": "D 成功 = repair_functional_success；诊断归因 NOT_VERIFIED。",
    }


def adapt(*, ctx: C.CampaignContext, source_campaign: "str | Path") -> "dict[str, Any]":
    """一步到位：读源 → 复制 → 建三件合同（幂等；重复跑结果除时间戳外相同）。"""
    src = load_source(source_campaign)
    copied = copy_assets(ctx, src)
    contract = build_contract(ctx, src)
    api = build_public_api(contract)
    reuse = build_reuse_impact(ctx, src, contract)
    paths = {
        "task_contract": str(C.write_json(ctx, "contract/task_contract.json", contract)),
        "public_api": str(C.write_json(ctx, "contract/public_api.json", api)),
        "reuse_impact": str(C.write_json(ctx, "contract/reuse_impact.json", reuse)),
    }
    return {"copied": copied, "paths": paths, "instances": len(contract["instances"])}


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--campaign-id", default=None)
    ap.add_argument("--root", default=None)
    ap.add_argument("--source-campaign", required=True)
    args = ap.parse_args(argv)
    from orchestrator.common.runtime_paths import (
        rsi_r02_campaign_id, rsi_r02_root)

    cid = args.campaign_id or rsi_r02_campaign_id()
    base = Path(args.root) if args.root else rsi_r02_root()
    ctx = C.load_campaign(campaign_id=cid, root=base)
    out = adapt(ctx=ctx, source_campaign=args.source_campaign)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
