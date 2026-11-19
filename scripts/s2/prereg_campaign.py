#!/usr/bin/env python3
"""RSI-S2 双预注册（S2-0）：规则面 LOCK + 战役 LOCK（零付费，付费前冻结）。

两份 LOCK **分开签**（反混淆的制度化，计划 §3 S2-0）：

1. ``prereg/rules/`` —— **规则面**全文（词表 v2 五值、DiD 等效登记、NC 三连判据、
   报告合同六段、review 量表对齐、冻结面/变异面清单）+ 规则模块逐件 sha256。
   签名人 ``s2-spec`` / ``s2-audit``；**必须先于任何候选 diff**（AC-01 时间戳序）。
2. ``prereg/campaign/`` —— **战役面**（6 实例 × 2 臂、配对单位=实例、统计器
   paired-permutation（2^6=64 符号模式、最小 p=0.015625）、非劣约束、Gate 参数、
   触点帽、退出/停止、预算信封、**降样本预登记序**）。

纪律：LOCK 一旦建立，改任何一件即新 bundle（旧 LOCK 自动 verify 失败）；付费脚本
在首次网关调用前强制 ``verify_*_lock()``。A_N/B_N 旧判件只读，本文件不改写。

用法::

    python scripts/s2/prereg_campaign.py rules build|sign|verify
    python scripts/s2/prereg_campaign.py campaign build|sign|verify
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Mapping

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from orchestrator.common.clock import utc_now_iso_z  # noqa: E402
from orchestrator.common.fsutil import atomic_write_text, ensure_private_dir  # noqa: E402
from orchestrator.common.runtime_paths import (  # noqa: E402
    rsi_s2_campaign_prereg_dir, rsi_s2_rules_prereg_dir)
from orchestrator.hk import evolution as EV  # noqa: E402
from orchestrator.hk import nc_registration as NR  # noqa: E402
from orchestrator.hk import report_contract as RC  # noqa: E402
from orchestrator.hk import verdicts as V  # noqa: E402
from orchestrator.rsi_r1 import profiles as P  # noqa: E402
from orchestrator.rsi_r1 import rubric as R  # noqa: E402
from orchestrator.rsi_r1 import scenes as S  # noqa: E402
from orchestrator.rsi_r1 import statistics as ST  # noqa: E402
from orchestrator.rsi_r1 import task_specs as TS  # noqa: E402
from orchestrator.rsi_r1.contrasts import (ContrastV13, KIND_DID,  # noqa: E402
                                           KIND_PAIRWISE, did_terms)

RULES_SCHEMA = "simpraxis.rsi_s2.rules.v1"
CAMPAIGN_SCHEMA = "simpraxis.rsi_s2.campaign.v1"
LOCK_SCHEMA_SUFFIX = ".lock"

#: 规则面锚定模块（逐件 sha256 入 LOCK；改一件即新 bundle）。
RULE_MODULES = (
    "orchestrator/hk/verdicts.py",
    "orchestrator/hk/report_contract.py",
    "orchestrator/hk/nc_registration.py",
    # 检查器/装载器（evolution.py、candidate.py）属**仪器**而非规则面：
    # 其修复（AST 注解赋值覆盖、file 式包件）不改任何判据语义，按 R1.3
    # 「停付修仪器」纪律登记，不进入规则锚定（否则仪器修复会误判 RULE-DRIFT）。
    "orchestrator/hk/contracts.py",
    "orchestrator/hk/driver.py",
    "orchestrator/hk/skills.py",
    "orchestrator/hk/guards.py",
    "orchestrator/hk/touchpoints.py",
    "orchestrator/rsi_bridge/review_s1.py",
    # review_s2.py 是**审查仪器**（pack 序列化/多对象解析），不是判据规则面：
    # 其修复（自足字段、平衡括号解析）按 R1.3「停付修仪器」纪律登记，不锚定。
    "orchestrator/rsi_bridge/host_recompute.py",
    "scripts/s2/paired_analysis.py",
    "orchestrator/rsi_r1/contrasts.py",
    "orchestrator/rsi_r1/rubric.py",
    "orchestrator/rsi_r1/task_specs.py",
)

N_BLOCK = P.MAX_CONFIRM_BLOCKS  # 32 个宿主私有配对区块（单一真值 profiles）

#: 网关面分账（计划 §6；硬帽 40 POST）。
GATEWAY_BUDGET = {
    "hard_cap_posts": 40,
    "lanes": {"dual_review": 24, "review_calibration": 2, "probe_smoke": 2,
              "repair_reserve": 8, "margin": 4},
}
#: 会话面（headless claude -p；计划 §6；硬帽 48 会话）。
SESSION_BUDGET = {
    "hard_cap_sessions": 48,
    "lanes": {"proposer": 3, "smoke": 1, "campaign_touchpoints": 36,
              "diagnose_or_repair": 8},
}

#: 降样本预登记序（预算触顶时按固定序砍尾，不得事后挑）。
SAMPLE_REDUCTION_ORDER = (
    "s2-a1-friction-mass", "s2-b1-kp-tc", "s2-a2-friction-mass",
    "s2-b2-kp-tc", "s2-a3-friction-mass", "s2-b3-kp-tc",
)

# ---------------------------------------------------------------------------
# 6 个新实例（族已见、实例新；每实例自带 NC 槽）
# ---------------------------------------------------------------------------

INSTANCES: "tuple[dict[str, Any], ...]" = (
    {"instance_id": "s2-a1-friction-mass", "family": "A_N",
     "cells": {"hi_mu": "0.6", "lo_mu": "0.3", "hi_mass": "0.9", "lo_mass": "0.3"},
     "sham_mu": "0.6005",
     "question": ("水平面单刚体滑动中，滑动摩擦（0.6→0.3）对 4 s 净位移的效应是否随"
                  "质量（0.9→0.3 kg）改变？主估计量 = 两质量水平下该摩擦效应之差。")},
    {"instance_id": "s2-a2-friction-mass", "family": "A_N",
     "cells": {"hi_mu": "0.5", "lo_mu": "0.25", "hi_mass": "0.75", "lo_mass": "0.25"},
     "sham_mu": "0.5005",
     "question": ("水平面单刚体滑动中，滑动摩擦（0.5→0.25）对 4 s 净位移的效应是否随"
                  "质量（0.75→0.25 kg）改变？主估计量 = 两质量水平下该摩擦效应之差。")},
    {"instance_id": "s2-a3-friction-mass", "family": "A_N",
     "cells": {"hi_mu": "0.7", "lo_mu": "0.2", "hi_mass": "1.0", "lo_mass": "0.4"},
     "sham_mu": "0.7001",
     "question": ("水平面单刚体滑动中，滑动摩擦（0.7→0.2）对 4 s 净位移的效应是否随"
                  "质量（1.0→0.4 kg）改变？主估计量 = 两质量水平下该摩擦效应之差。")},
    {"instance_id": "s2-b1-kp-tc", "family": "B_N",
     "cells": {"hi_kp": "15", "lo_kp": "2", "hi_tc": "0.016", "lo_tc": "0.004"},
     "sham_kd": "0.1005",
     "question": ("单关节 PD 跟踪中，比例增益 kp（15→2）对归一化跟踪 RMSE 的效应是否随"
                  "控制采样周期 Tc（0.016→0.004 s）改变？任何方向都合法。")},
    {"instance_id": "s2-b2-kp-tc", "family": "B_N",
     "cells": {"hi_kp": "8", "lo_kp": "0.5", "hi_tc": "0.032", "lo_tc": "0.008"},
     "sham_kd": "0.1005",
     "question": ("单关节 PD 跟踪中，比例增益 kp（8→0.5）对归一化跟踪 RMSE 的效应是否随"
                  "控制采样周期 Tc（0.032→0.008 s）改变？任何方向都合法。")},
    {"instance_id": "s2-b3-kp-tc", "family": "B_N",
     "cells": {"hi_kp": "12", "lo_kp": "3", "hi_tc": "0.008", "lo_tc": "0.004"},
     "sham_kd": "0.1005",
     "question": ("单关节 PD 跟踪中，比例增益 kp（12→3）对归一化跟踪 RMSE 的效应是否随"
                  "控制采样周期 Tc（0.008→0.004 s）改变？任何方向都合法。")},
)


def _canonical(doc: Any) -> bytes:
    return json.dumps(doc, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _sha(doc: Any) -> str:
    return hashlib.sha256(_canonical(doc)).hexdigest()


def _sha_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _write(path: Path, doc: Any) -> Path:
    atomic_write_text(path, json.dumps(doc, ensure_ascii=False, sort_keys=True,
                                       indent=2) + "\n", mode=0o600)
    return path


def _cond(cid: str, scene_id: str, params: Mapping[str, Any]) -> Any:
    return TS.C.Condition.from_dict(  # noqa: SLF001 - 单一真值构造点
        {"id": cid, "scene_id": scene_id, "params": dict(params)}, where=cid)


# ---------------------------------------------------------------------------
# 实例构造（复用 build_host_spec 的 TaskSpecV13；零引擎改动）
# ---------------------------------------------------------------------------

def _resource_sha(instance_id: str) -> str:
    return _sha({"instance": instance_id, "blocks": N_BLOCK,
                 "max_confirm_runs": int(P.MAX_CONFIRM_RUNS_PER_STUDY),
                 "physics": "mujoco 3.15.0（受信 host 服务）"})


def _a_instance(spec: Mapping[str, Any]) -> "TS.TaskSpecV13":
    cells = spec["cells"]
    scene = S.SCENE_A
    base = {"v0_mps": "0.4", "torsional_friction_m": "0.01"}
    main = (
        ("hi_mu_hi_mass", cells["hi_mu"], cells["hi_mass"]),
        ("hi_mu_lo_mass", cells["hi_mu"], cells["lo_mass"]),
        ("lo_mu_hi_mass", cells["lo_mu"], cells["hi_mass"]),
        ("lo_mu_lo_mass", cells["lo_mu"], cells["lo_mass"]),
    )
    conditions = [
        _cond(cid, scene, {**base, "mass_kg": mass, "sliding_friction": mu})
        for cid, mu, mass in main
    ]
    conditions.append(_cond("sham_tors", scene, {
        **base, "torsional_friction_m": "0.02",
        "mass_kg": cells["hi_mass"], "sliding_friction": cells["hi_mu"]}))
    conditions.append(_cond("sham_mu", scene, {
        **base, "mass_kg": cells["hi_mass"], "sliding_friction": spec["sham_mu"]}))
    primary = ContrastV13(
        id="friction_x_mass", kind=KIND_DID, metric="displacement_x_m",
        proposition="decrease_gt", delta=R.metric_delta("displacement_x_m"),
        terms=did_terms(treatment_high="hi_mu_hi_mass",
                        treatment_low="lo_mu_hi_mass",
                        control_high="hi_mu_lo_mass", control_low="lo_mu_lo_mass"))
    nc_slot = {
        "schema": NR.NC_SLOT_SCHEMA,
        "allowed_diff": ["sliding_friction", "torsional_friction_m"],
        "candidates": ["hi_mu_hi_mass", "sham_tors", "sham_mu"],
        "equivalence_metric": "displacement_x_m_negative_equivalence",
        "delta": "0.001",
        "ci_level": "0.90",
        "max_perturbation": {"sliding_friction": "0.001",
                             "torsional_friction_m": "0.02"},
        "default": {"base": "hi_mu_hi_mass", "sham": "sham_tors"},
        "criteria": dict(NR.NC_CRITERIA),
    }
    task = TS.TaskSpecV13(
        task_id=spec["instance_id"], mechanism_id=scene,
        split=TS.SPLIT_HOLDOUT, scene_version=S.scene_doc(scene).version,
        public_question=spec["question"],
        allowed_design_space=TS._allowed_space(scene),  # noqa: SLF001
        estimand={"metric_id": "displacement_x_m",
                  "contrast_ids": ["friction_x_mass",
                                   "negative_hi_mu_hi_mass_vs_sham_mu"],
                  "target_population": "32 个宿主分配的确认 block（初速度 nuisance）",
                  "aggregation": "paired_block_linear_contrast",
                  "weighting": "equal_block_weight"},
        nuisance_schema={"v0_mps": {"mode": "varying", "support": ["0.1", "0.6"],
                                    "assignment_owner": "host",
                                    "note": "块内同初速"}},
        negative_control_spec=nc_slot,
        private_instance_commitment=_sha({"instance": spec["instance_id"],
                                          "cells": dict(cells),
                                          "sham_mu": spec["sham_mu"]}),
        metric_method_sha=TS._metric_method_sha(),  # noqa: SLF001
        resource_spec_sha=_resource_sha(spec["instance_id"]),
        conditions=tuple(conditions), primary=primary,
        fixed={**TS._fixed_of(scene),  # noqa: SLF001
               "confirm_blocks": str(P.MAX_CONFIRM_BLOCKS),
               "host_nuisance": "v0_mps"})
    return task.validate()


def _b_instance(spec: Mapping[str, Any]) -> "TS.TaskSpecV13":
    cells = spec["cells"]
    scene = S.SCENE_B
    base = {"controller": "PD", "dt_s": "0.001", "kd": "0.1",
            "target_amplitude_rad": "0.2", "target_freq_hz": "1.0",
            "inertia_ratio": "1.0"}
    main = (
        ("hi_kp_hi_tc", cells["hi_kp"], cells["hi_tc"]),
        ("hi_kp_lo_tc", cells["hi_kp"], cells["lo_tc"]),
        ("lo_kp_hi_tc", cells["lo_kp"], cells["hi_tc"]),
        ("lo_kp_lo_tc", cells["lo_kp"], cells["lo_tc"]),
    )
    conditions = [
        _cond(cid, scene, {**base, "kp": kp, "tc_s": tc})
        for cid, kp, tc in main
    ]
    conditions.append(_cond("sham_kd", scene, {
        **base, "kp": cells["hi_kp"], "tc_s": cells["hi_tc"],
        "kd": spec["sham_kd"]}))
    primary = ContrastV13(
        id="kp_x_tc", kind=KIND_DID, metric="normalized_tracking_rmse",
        proposition="increase_gt",
        delta=R.metric_delta("normalized_tracking_rmse"),
        terms=did_terms(treatment_high="hi_kp_hi_tc",
                        treatment_low="lo_kp_hi_tc",
                        control_high="hi_kp_lo_tc", control_low="lo_kp_lo_tc"))
    nc_slot = {
        "schema": NR.NC_SLOT_SCHEMA,
        "allowed_diff": ["kd"],
        "candidates": ["hi_kp_hi_tc", "sham_kd"],
        "equivalence_metric": "normalized_tracking_rmse_negative_equivalence",
        "delta": "0.01",
        "ci_level": "0.90",
        "max_perturbation": {"kd": "0.001"},
        "default": None,  # v0 臂无 NC 提名 → NC_UNREGISTERED（缺陷③）
        "criteria": dict(NR.NC_CRITERIA),
    }
    task = TS.TaskSpecV13(
        task_id=spec["instance_id"], mechanism_id=scene,
        split=TS.SPLIT_HOLDOUT, scene_version=S.scene_doc(scene).version,
        public_question=spec["question"],
        allowed_design_space=TS._allowed_space(scene),  # noqa: SLF001
        estimand={"metric_id": "normalized_tracking_rmse",
                  "contrast_ids": ["kp_x_tc", "negative_hi_kp_hi_tc_vs_sham_kd"],
                  "target_population": "32 个宿主分配的确认 block（目标频率 nuisance）",
                  "aggregation": "paired_block_linear_contrast",
                  "weighting": "equal_block_weight"},
        nuisance_schema={"target_freq_hz": {"mode": "varying",
                                            "support": ["0.2", "2.0"],
                                            "assignment_owner": "host",
                                            "note": "块内同频"}},
        negative_control_spec=nc_slot,
        private_instance_commitment=_sha({"instance": spec["instance_id"],
                                          "cells": dict(cells),
                                          "sham_kd": spec["sham_kd"]}),
        metric_method_sha=TS._metric_method_sha(),  # noqa: SLF001
        resource_spec_sha=_resource_sha(spec["instance_id"]),
        conditions=tuple(conditions), primary=primary,
        fixed={**TS._fixed_of(scene),  # noqa: SLF001
               "confirm_blocks": str(P.MAX_CONFIRM_BLOCKS),
               "host_nuisance": "target_freq_hz"})
    return task.validate()


def build_instance(spec: Mapping[str, Any]) -> "TS.TaskSpecV13":
    family = spec["family"]
    if family == "A_N":
        return _a_instance(spec)
    if family == "B_N":
        return _b_instance(spec)
    raise SystemExit(f"UNKNOWN_FAMILY: {family!r}")


# ---------------------------------------------------------------------------
# 规则面文档
# ---------------------------------------------------------------------------

def _frozen_at() -> str:
    """规则内容的**冻结**时刻（AC-01 时间戳序的锚）。

    优先保留既有 rules.json 的记录；缺失时取锚定规则模块的**最大 mtime**
    （= 规则内容最后一次被修改的时刻，机器可复算）；两者都没有才用当前时刻。
    """
    import datetime

    mtimes = [(_REPO / rel).stat().st_mtime for rel in RULE_MODULES
              if (_REPO / rel).is_file()]
    content_frozen = (datetime.datetime.utcfromtimestamp(max(mtimes)).strftime(
        "%Y-%m-%dT%H:%M:%SZ") if mtimes else utc_now_iso_z())
    path = rsi_s2_rules_prereg_dir() / "rules.json"
    if path.is_file():
        try:
            recorded = str(json.loads(path.read_text(encoding="utf-8")).get(
                "rules_content_frozen_at_utc") or "")
        except ValueError:
            recorded = ""
        # 记录值晚于「规则内容最后修改」= 旧实现的回退产物，按内容时刻更正
        if recorded and recorded <= content_frozen:
            return recorded
    return content_frozen


def _repair_record(anchors: "dict[str, str]") -> "dict[str, Any]":
    """评审修复轮登记：锚定模块在首次冻结后被改时，如实记录修复时刻与清单。

    纪律：`rules_content_frozen_at_utc` 保持**首次冻结**时刻（AC-01 时间戳序的锚），
    修复轮单独记 `rules_repaired_at_utc` 与 `repaired_modules`——规则内容变更不
    改写历史，只追加修复记录。
    """
    path = rsi_s2_rules_prereg_dir() / "rules.json"
    if not path.is_file():
        return {"rules_repaired_at_utc": None, "repaired_modules": []}
    try:
        old = json.loads(path.read_text(encoding="utf-8")).get(
            "rule_anchors_sha256") or {}
    except ValueError:
        return {"rules_repaired_at_utc": None, "repaired_modules": []}
    changed = sorted(k for k in anchors if old.get(k) != anchors[k])
    return {"rules_repaired_at_utc": (utc_now_iso_z() if changed else None),
            "repaired_modules": changed}


def build_rules_document() -> "dict[str, Any]":
    anchors = {path: _sha_file(_REPO / path) for path in RULE_MODULES}
    return {
        "schema": RULES_SCHEMA,
        "version": 2,
        "rules_content_frozen_at_utc": _frozen_at(),
        **_repair_record(anchors),
        "verdict_vocabulary": {
            "values": list(V.VERDICTS_V2),
            "definitions": {
                "SUPPORTED": "方向主张被证据支持（CI 整段落在主张方向且越过 δ）",
                "REFUTED": "证据与主张方向相反且越过 δ（CI 整段在对侧）",
                "NOT_DETECTED": "CI 跨零（无可检出的方向性效应，不等于否证）",
                "EQUIVALENT": "CI 整段落进 ±δ 等效带（等效检验支持）",
                "INCONCLUSIVE": "其余（区间横跨 δ 边界/点区间/样本不足）",
            },
            "equivalent_proposition": "equivalent(δ) 主张退回三值语义（SUPPORTED/REFUTED/INCONCLUSIVE）",
            "module": "orchestrator/hk/verdicts.py",
            "binding": "claim.verdict 必须等于 host 主对比分类（G-CLAIM，HIGH-2 语义不松）",
        },
        "contrast_registration": {
            "did_equivalence_registrable": True,
            "rule": ("撤销「DiD 等效不可注册」规则债：contrast 级 equivalent(δ) 可在任何"
                     "已注册指标上登记（正/负等效同规）；δ 仍由量表目录固定"),
            "negative_equivalence_metric": "仅 pairwise",
            "old_cases_unchanged": "A_N 旧判件与旧预注册只读，不改写",
        },
        "nc_registration": {
            "schema": NR.NC_SLOT_SCHEMA,
            "criteria": dict(NR.NC_CRITERIA),
            "rejection": "退化/超阈/未校准 → 结构拒绝，不进确认批；未注册 → NC_UNREGISTERED",
            "module": "orchestrator/hk/nc_registration.py",
        },
        "report_contract": {
            "schema": RC.SCHEMA,
            "sections": list(RC.SECTIONS),
            "section_to_dimension": dict(RC.SECTION_TO_DIMENSION),
            "min_chars": 8,
            "module": "orchestrator/hk/report_contract.py",
        },
        "review_rubric_alignment": {
            "dimensions": ["problem_hypothesis", "design_confounding",
                           "statistics_censoring", "claim_strength",
                           "limitations_next_steps", "negative_control"],
            "degenerate_nc_scores_zero": True,
            "missing_section_scores_zero": True,
            "truth_table": "两票全过=PASS；任一双败=FAIL；其余 INCONCLUSIVE；无第三票",
        },
        "mutation_surface": list(EV.MUTATION_SURFACE),
        "frozen_surface": list(EV.FROZEN_SURFACE),
        "rule_anchors_sha256": anchors,
        "statistics_method_sha256": _sha(ST.public_method_document()),
        "registered_at_utc": utc_now_iso_z(),
    }


def build_rules(root: Path | None = None) -> "dict[str, Any]":
    target = ensure_private_dir(root or rsi_s2_rules_prereg_dir(), mode=0o700)
    files = {"rules.json": build_rules_document()}
    written: "dict[str, str]" = {}
    for name, doc in files.items():
        path = _write(target / name, doc)
        written[name] = _sha_file(path)
    bundle = _sha(written)
    manifest = {"schema": f"{RULES_SCHEMA}.manifest", "bundle_sha256": bundle,
                "files": {name: {"sha256": sha,
                                 "bytes": (target / name).stat().st_size}
                          for name, sha in sorted(written.items())},
                "built_at_utc": utc_now_iso_z()}
    _write(target / "manifest.json", manifest)
    lock = target / "LOCK.json"
    if lock.exists():
        lock.unlink()  # 新 bundle 必须重新双签
    return manifest


# ---------------------------------------------------------------------------
# 战役面文档
# ---------------------------------------------------------------------------

def build_campaign_document(*, rules_bundle_sha: str) -> "dict[str, Any]":
    instances = []
    for spec in INSTANCES:
        task = build_instance(spec)
        instances.append({
            "instance_id": spec["instance_id"], "family": spec["family"],
            "scene_id": task.mechanism_id, "task_sha256": task.sha256,
            "n_conditions": len(task.conditions),
            "n_confirm_runs": len(task.conditions) * N_BLOCK,
            "public_question": spec["question"],
            "nc_slot": dict(task.negative_control_spec or {}),
            "primary_contrast": task.primary.to_dict(),
        })
    return {
        "schema": CAMPAIGN_SCHEMA,
        "campaign_id": "s2-first-controlled-improvement",
        "rules_bundle_sha256": rules_bundle_sha,
        "arms": ["v0", "v1"],
        "arm_definition": {
            "v0": "S1 as-built 技能/守卫/模板（无 G-REPORT、无 NC 技能、旧模板）",
            "v1": "候选包（G-REPORT 守卫 + nc_toolkit + 触点模板 v2）；须过 EvolutionSpec 机检",
            "shared": "规则面（词表 v2/NC 规则/报告合同/判据）两臂同规",
        },
        "paired_unit": "instance",
        "n_instances": len(instances),
        "instances": instances,
        "claim_scope": "族已见、实例新（A 摩擦×质量 / B kp×Tc）；跨族迁移留 S3，不在此声明",
        "statistics": {
            "family": "paired_permutation",
            "n_pairs": len(instances),
            "n_sign_patterns": 2 ** len(instances),
            # 单侧最小 p（全同向符号模式）；双侧最小 p 另列（2/64），
            # 预注册口径按计划 §3 S2-5 原文「最小 p=0.016」。
            "min_p": str(1 / (2 ** len(instances))),
            "min_p_two_sided": str(2 / (2 ** len(instances))),
            "alpha": "0.05",
            "primary_metric": "review_pass_rate",
            "secondary": ["negative_control_score", "limitations_next_steps_score"],
            "module": "orchestrator/rsi_bridge/selection.py",
            "upstream": "PhysicalRSI_core.self_harness.selection.select_survivor",
            "rule": "统计初判 inherit_child 仅为初判；Gate2 accept 才晋升",
        },
        "non_inferiority": {
            "engineering_completion": "v1 完成实例数 >= v0",
            "rerun_n_diffs": 0,
            "g_claim_violations": 0,
            "session_cost_ratio_max": "1.50",
        },
        "gates": {
            "gate1": {"stages": ["MUTATION", "MACHINE_GATES", "PAIRED_TRIAL",
                                 "GATE2_VERDICT"],
                      "rejects_max": 2, "reject_requires_notes": True,
                      "zero_paid_before_approval": True},
            "gate2": {"max_rejects_per_stage": 2,
                      "per_stage_counting": "CR-20：按 Stage 独立计数",
                      "exhaustion": "retain_parent（v0 维持，v1 入 CandidateArchive）"},
            "touchpoints": {"per_run": 3, "repair": 1, "diagnose": 2},
        },
        "review_protocol": {
            "channels": 2, "packs_per_instance": 2, "n_packs": 2 * len(instances),
            "channel_a": {"temperature": "0.0", "salt": "channel-a"},
            "channel_b": {"temperature": "0.7", "salt": "channel-b"},
            "calibration_cases": 6,
            "calibration_gate": "误接收即停付修仪器（R1.3 纪律）",
            "pack_self_contained": "不含机器判决与另一审意见；_used_packs 溯源",
        },
        "sample_reduction_order": list(SAMPLE_REDUCTION_ORDER),
        "stopping_rules": {
            "rules_mutation_confounded": "规则面与变异面同批上线 → 战役判无效",
            "proposer_not_qualified": "越界/纯抄意见 ≤2 次重提；仍不合格记 PROPOSER_NOT_QUALIFIED",
            "prediction_not_fulfilled": "R2 不成立（即使主指标涨）",
            "review_instrument_failure": "停付修仪器重校准，不放宽容差",
            "budget_exhausted": "按预登记序降样本；不足=INCONCLUSIVE，不加帽事后挑",
            "vendor_failure": "三分账（transmit/model/science），失败占分母不补考",
            "gate2_exhausted": "retain_parent 诚实收口",
            "no_skip_xfail_todo": "视为未完成，不得结案",
        },
        "budget": {"gateway": GATEWAY_BUDGET, "session": SESSION_BUDGET,
                   "physics_runs_estimate": sum(
                       len(build_instance(s).conditions) * N_BLOCK
                       for s in INSTANCES) * 2 + 400},
        "registered_at_utc": utc_now_iso_z(),
    }


def build_protocol() -> "dict[str, Any]":
    return {
        "schema": f"{CAMPAIGN_SCHEMA}.protocol",
        "judgment": {
            "verdict_vocabulary": list(V.VERDICTS_V2),
            "claim_only": ["decrease_gt", "increase_gt", "equivalent(delta)"],
            "ci": {"primary": str(R.CI_PRIMARY), "negative": str(R.CI_NEGATIVE)},
            "bootstrap": {"method": "percentile",
                          "resamples": int(R.BOOTSTRAP_RESAMPLES),
                          "unit": "block", "seed": int(ST.STATISTICS_SEED)},
            "self_report_vs_host_mismatch": "CLAIM_INVALID",
            "statistics_method_sha256": _sha(ST.public_method_document()),
        },
        "review": {
            "dimensions": list(RC.SECTIONS),
            "section_to_dimension": dict(RC.SECTION_TO_DIMENSION),
            "calibration_cases": ["cal-valid-positive", "cal-valid-negative",
                                  "cal-pseudo-replication", "cal-over-generalization",
                                  "cal-empty-template", "cal-contradictory"],
            "truth_table": "两票全过=PASS；任一双败=FAIL；其余 INCONCLUSIVE；无第三票",
            "channel_salt_and_temperature_recorded": True,
        },
        "failure_accounting": ["transmit", "model", "science"],
        "registered_at_utc": utc_now_iso_z(),
    }


def build_campaign(root: Path | None = None) -> "dict[str, Any]":
    target = ensure_private_dir(root or rsi_s2_campaign_prereg_dir(), mode=0o700)
    rules_manifest_path = rsi_s2_rules_prereg_dir() / "manifest.json"
    if not rules_manifest_path.is_file():
        raise SystemExit("RULES-UNLOCKED: 先 build 规则面（LOCK 必须先于战役）")
    rules_bundle = json.loads(
        rules_manifest_path.read_text(encoding="utf-8"))["bundle_sha256"]
    files = {
        "design.json": build_campaign_document(rules_bundle_sha=rules_bundle),
        "protocol.json": build_protocol(),
        "budgets.json": {"schema": f"{CAMPAIGN_SCHEMA}.budgets",
                         "gateway": GATEWAY_BUDGET, "session": SESSION_BUDGET,
                         "double_face_note": "两面分列不混算",
                         "approvals": "quota/approvals/s2-*.json（自批；逐次记账）"},
    }
    written: "dict[str, str]" = {}
    for name, doc in files.items():
        path = _write(target / name, doc)
        written[name] = _sha_file(path)
    bundle = _sha(written)
    manifest = {"schema": f"{CAMPAIGN_SCHEMA}.manifest", "bundle_sha256": bundle,
                "rules_bundle_sha256": rules_bundle,
                "files": {name: {"sha256": sha,
                                 "bytes": (target / name).stat().st_size}
                          for name, sha in sorted(written.items())},
                "built_at_utc": utc_now_iso_z()}
    _write(target / "manifest.json", manifest)
    lock = target / "LOCK.json"
    if lock.exists():
        lock.unlink()
    return manifest


# ---------------------------------------------------------------------------
# 签名 / 验证（两 bundle 同构）
# ---------------------------------------------------------------------------

def _bundle_sha(target: Path, schema: str) -> str:
    manifest_path = target / "manifest.json"
    if not manifest_path.is_file():
        raise SystemExit("PRE-REG-MISSING: manifest.json 不存在（先 build）")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema") != f"{schema}.manifest":
        raise SystemExit(f"PRE-REG-SCHEMA: {manifest.get('schema')!r}")
    files = manifest.get("files") or {}
    actual: "dict[str, str]" = {}
    for name in sorted(files):
        path = target / name
        if not path.is_file():
            raise SystemExit(f"PRE-REG-MISSING: {name}")
        actual[name] = _sha_file(path)
        if actual[name] != files[name]["sha256"]:
            raise SystemExit(f"PRE-REG-CHANGED: {name} 与 manifest 不符")
    computed = _sha(actual)
    if computed != manifest.get("bundle_sha256"):
        raise SystemExit("PRE-REG-CHANGED: bundle_sha256 不符")
    return computed


def sign(*, which: str, signer: str, role: str) -> "dict[str, Any]":
    target, schema = _target(which)
    sha = _bundle_sha(target, schema)
    lock_path = target / "LOCK.json"
    if lock_path.is_file():
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
    else:
        lock = {"schema": f"{schema}{LOCK_SCHEMA_SUFFIX}", "bundle_sha256": sha,
                "signatures": []}
    if lock.get("bundle_sha256") != sha:
        raise SystemExit("PRE-REG-CHANGED: LOCK 绑定的 bundle 已变（重新 build+双签）")
    if any(s.get("signer") == signer for s in lock["signatures"]):
        raise SystemExit(f"PRE-REG-DUPLICATE-SIGNER: {signer} 已签")
    lock["signatures"].append({"signer": signer, "role": role,
                               "bundle_sha256": sha,
                               "signed_at_utc": utc_now_iso_z()})
    _write(lock_path, lock)
    return lock


def _target(which: str) -> "tuple[Path, str]":
    if which == "rules":
        return Path(rsi_s2_rules_prereg_dir()), RULES_SCHEMA
    if which == "campaign":
        return Path(rsi_s2_campaign_prereg_dir()), CAMPAIGN_SCHEMA
    raise SystemExit(f"UNKNOWN_LOCK: {which!r}（rules|campaign）")


def verify_lock(which: str, *, min_signers: int = 2) -> "dict[str, Any]":
    target, schema = _target(which)
    sha = _bundle_sha(target, schema)
    lock_path = target / "LOCK.json"
    if not lock_path.is_file():
        raise SystemExit("PRE-REG-UNLOCKED: LOCK.json 不存在")
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    if lock.get("schema") != f"{schema}{LOCK_SCHEMA_SUFFIX}":
        raise SystemExit("PRE-REG-LOCK-SCHEMA")
    if lock.get("bundle_sha256") != sha:
        raise SystemExit("PRE-REG-CHANGED: LOCK 与当前 bundle 不符")
    signatures = lock.get("signatures") or []
    if len(signatures) < min_signers:
        raise SystemExit(f"PRE-REG-SINGLE-SIGN: 需 ≥{min_signers} 签，实有 {len(signatures)}")
    signers = [s.get("signer") for s in signatures]
    if len(set(signers)) != len(signers):
        raise SystemExit("PRE-REG-SAME-SIGNER: 双签必须互异")
    for item in signatures:
        if item.get("bundle_sha256") != sha:
            raise SystemExit(f"PRE-REG-SIG-MISMATCH: {item.get('signer')}")
    if which == "rules":
        _verify_rule_anchors(target)
    return {"which": which, "bundle_sha256": sha, "signers": signers,
            "signed_at_utc": [s.get("signed_at_utc") for s in signatures],
            "verified_at_utc": utc_now_iso_z()}


def _verify_rule_anchors(target: Path) -> None:
    """规则模块逐件复算：规则面在 LOCK 之后被改即 fail-closed（AC-01 冻结）。"""
    doc = json.loads((target / "rules.json").read_text(encoding="utf-8"))
    anchors = doc.get("rule_anchors_sha256") or {}
    changed = []
    for path, expected in anchors.items():
        actual = _sha_file(_REPO / path)
        if actual != expected:
            changed.append(path)
    if changed:
        raise SystemExit(f"RULE-DRIFT: 规则模块在 LOCK 后被改 {sorted(changed)}")


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("which", choices=["rules", "campaign"])
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("build")
    p_sign = sub.add_parser("sign")
    p_sign.add_argument("--signer", required=True)
    p_sign.add_argument("--role", required=True)
    sub.add_parser("verify")
    args = parser.parse_args(argv)
    if args.cmd == "build":
        manifest = (build_rules() if args.which == "rules"
                    else build_campaign())
        print(json.dumps({"bundle_sha256": manifest["bundle_sha256"],
                          "files": sorted(manifest["files"])},
                         ensure_ascii=False))
        return 0
    if args.cmd == "sign":
        print(json.dumps(sign(which=args.which, signer=args.signer,
                              role=args.role), ensure_ascii=False, indent=2))
        return 0
    try:
        report = verify_lock(args.which)
    except SystemExit as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
