#!/usr/bin/env python3
"""RSI-S1 预注册件与双签 LOCK（S1-0；付费前冻结）。

产物（``$SIMPRAXIS_RSI_S1_ROOT/prereg/``，0700/0600）：

* ``study_A_N.json``：A_N 任务规格（``task_specs.build_r1n_a`` 原文 + 构造点哈希）
  + 5 条件 × 32 私有 block = 160 确认 run + 主对比/sham 阴性对比 + 冻结 δ/α/CI；
* ``study_B_N.json``：B_N 4 条件 × 32 = 128 run（开放问题）；
* ``protocol.json``：判定规则、门参数（Gate2 per-Stage 打回 2、触点 4+diagnose 2）、
  停止规则、失败分账、双审协议（六维 + 合并真值表，无第三票）、校准门 ①–⑤；
* ``budgets.json``：网关面 40 POST 分账 + 会话面 20 会话（两面不混算）；
* ``manifest.json``：逐件 sha256 + 逐件字节数 + bundle_sha256；
* ``LOCK.json``：两签互异的 LOCK（同一 bundle_sha256，签名人不同）。

CLI::

    python scripts/s1/prereg_studies.py build
    python scripts/s1/prereg_studies.py sign  --signer lead --role executor
    python scripts/s1/prereg_studies.py sign  --signer review-lane-a --role reviewer
    python scripts/s1/prereg_studies.py verify

纪律：LOCK 一旦建立，改任何一件即新 bundle（旧 LOCK 自动 verify 失败）；付费脚本
（``scripts/s1/run_study.py``）在首次网关调用前强制 ``verify_prereg_lock()``。
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
from orchestrator.common.runtime_paths import rsi_s1_prereg_dir  # noqa: E402
from orchestrator.rsi_r1 import profiles as P  # noqa: E402
from orchestrator.rsi_r1 import rubric as R  # noqa: E402
from orchestrator.rsi_r1 import scenes as S  # noqa: E402
from orchestrator.rsi_r1 import statistics as ST  # noqa: E402
from orchestrator.rsi_r1 import task_specs as TS  # noqa: E402

SCHEMA = "simpraxis.rsi_s1.prereg.v1"
LOCK_SCHEMA = f"{SCHEMA}.lock"
MANIFEST_SCHEMA = f"{SCHEMA}.manifest"

N_BLOCK = P.MAX_CONFIRM_BLOCKS             # 32 个宿主私有配对区块（单一真值 profiles）
STUDY_FILES = ("study_A_N.json", "study_B_N.json", "protocol.json", "budgets.json")

#: 网关面分账（计划 §6；硬帽 40 POST）。
GATEWAY_BUDGET = {
    "hard_cap_posts": 40,
    "lanes": {"dual_review": 4, "review_calibration": 4,
              "probe_smoke": 4, "repair_reserve": 8, "margin": 20},
}
#: 会话面（headless claude -p；计划 §6；硬帽 20 会话）。
SESSION_BUDGET = {
    "hard_cap_sessions": 20,
    "lanes": {"study_A_touchpoints": 4, "study_A_diagnose": 2,
              "study_B_touchpoints": 4, "study_B_diagnose": 2,
              "smoke": 2, "reserve": 6},
}


def _sha_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _canonical(doc: Any) -> bytes:
    return json.dumps(doc, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _sha_file(path: Path) -> str:
    return _sha_bytes(path.read_bytes())


def _write(path: Path, doc: Any) -> Path:
    atomic_write_text(path, json.dumps(doc, ensure_ascii=False, sort_keys=True,
                                       indent=2) + "\n", mode=0o600)
    return path


def _study_document(task, *, study: str, n_runs: int,
                    contrasts: list[Mapping[str, Any]]) -> dict[str, Any]:
    doc = {
        "schema": f"{SCHEMA}.study",
        "study": study,
        "task": task.to_dict(),
        "task_sha256": task.sha256,
        "construction_point": "orchestrator.rsi_r1.task_specs.build_host_spec",
        "n_conditions": len(task.conditions),
        "n_block": N_BLOCK,
        "n_confirm_runs": n_runs,
        "confirm_batch_cap": P.MAX_CONFIRM_RUNS_PER_STUDY,
        "block_assignment": "host 私有（plan_sha256‖study‖block 哈希网格；见 physics_service）",
        "blocks_note": "32 配对区块；块内 nuisance 同值（A: v0_mps / B: target_freq_hz）",
        "contrasts": contrasts,
        "thresholds": {
            "delta": {k: str(v["delta"]) for k, v in sorted(R.METRICS.items())},
            "alpha": "0.05",
            "ci_primary": str(R.CI_PRIMARY),
            "ci_negative": str(R.CI_NEGATIVE),
            "bootstrap_resamples": int(R.BOOTSTRAP_RESAMPLES),
            "bootstrap_seed": int(ST.STATISTICS_SEED),
            "statistics_method_sha256": _sha_bytes(_canonical(ST.public_method_document())),
        },
        "claim_types": ["decrease_gt", "increase_gt", "equivalent(delta)"],
        "registered_at_utc": utc_now_utc(),
    }
    doc["document_sha256"] = _sha_bytes(_canonical(
        {k: v for k, v in doc.items() if k != "document_sha256"}))
    return doc


def utc_now_utc() -> str:
    return utc_now_iso_z()


def build_protocol() -> dict[str, Any]:
    return {
        "schema": f"{SCHEMA}.protocol",
        "judgment": {
            "claim_only": ["decrease_gt", "increase_gt", "equivalent(delta)"],
            "ci": {"primary": str(R.CI_PRIMARY), "negative": str(R.CI_NEGATIVE)},
            "bootstrap": {"method": "percentile", "resamples": int(R.BOOTSTRAP_RESAMPLES),
                          "unit": "block", "seed": int(ST.STATISTICS_SEED)},
            "self_report_vs_host_mismatch": "CLAIM_INVALID",
            "did_equivalence_not_registrable": True,
            "sham_carries_equivalence": "±0.001 m / 90% 区间（R1-N 固化差异表）",
        },
        "gates": {
            "gate1": "研究目标多 Stage 链（stages[]: A_N / B_N / REVIEW）；打回必须带"
                     "human_notes（空意见结案）；2 次打回转 human_failure；批准前零付费零实跑",
            "gate2": {"max_rejects_per_stage": 2,
                      "per_stage_counting": "CR-20：按 Stage 独立计数，不全局相加",
                      "reject_requires_notes": True,
                      "exhaustion": "human_failure 诚实结案；不无限循环"},
            "touchpoints": {"budget_per_study": 4, "diagnose_extra_cap": 2,
                            "over_budget": "TouchpointBudgetExceeded"},
        },
        "stop_rules": {
            "calibration_fail": "改任务设计不改 δ/α/界；两轮不过报用户",
            "sensitivity_or_sham_indistinguishable": "DESIGN_NOT_READY，不发付费",
            "touchpoint_cap": "诚实收口，不加帽翻案（加帽须新预注册）",
            "vendor_transport_failure": "分账（transmit/model/science），失败占分母不补考",
            "review_instrument_failure": "停付修仪器重校准（R1.3 轮次纪律），不放宽容差",
            "claim_host_mismatch": "CLAIM_INVALID 结案，不回改报告",
            "rerun_mismatch": "停止码结案，不重跑求解",
            "paid_over_cap_or_unknown_send": "停链留痕（一切 POST 必落账或 blocked 行）",
            "no_skip_xfail_todo": "视为未完成，不得报结案",
        },
        "failure_accounting": ["transmit", "model", "science"],
        "review_protocol": {
            "dimensions": ["问题与可反驳假设", "设计与混淆", "统计与删失",
                           "结论力度", "局限与下一步", "阴性对照处理"],
            "merge_truth_table": "两票全过=PASS；任一双败=FAIL；其余 INCONCLUSIVE；无第三票",
            "calibration_pack": "6 例（有效正/有效负/伪重复/过度概括/空模板/矛盾结论）"
                               "先过 REVIEW_INSTRUMENT_READY；误接收即停付修仪器",
            "pack_self_contained": "审查包不含机器判决与另一审意见；冻结后不得回改",
        },
        "calibration_gates": {
            "sensitivity": {"mu_low": str(R.SENSITIVITY_MU_LOW),
                            "mu_high": str(R.SENSITIVITY_MU_HIGH),
                            "mass_kg": str(R.SENSITIVITY_MASS_KG),
                            "v0_mps": str(R.SENSITIVITY_V0_MPS),
                            "min_displacement_diff_m": str(R.SENSITIVITY_MIN_DISPLACEMENT_DIFF_M)},
            "sham_positive_control": {"torsional_low": "0.01", "torsional_high": "0.02",
                                      "equivalence_delta_m": "0.001", "ci": "0.90"},
            "settle_dt_check": {"dt_halving": True, "max_settle_delta_s": "0.02",
                                "max_effect_delta": "0.01"},
            "shape_gate": {"max_confirm_runs": int(P.MAX_CONFIRM_RUNS_PER_STUDY),
                           "blocks": N_BLOCK, "per_value_pairing": True},
            "determinism": "同种子两次逐点一致（host_recompute 自证）",
        },
        "registered_at_utc": utc_now_utc(),
    }


def build_prereg(root: Path | None = None) -> dict[str, Any]:
    """写出预注册件 + manifest（不含 LOCK；LOCK 由两次 sign 建立）。"""
    target = ensure_private_dir(root or rsi_s1_prereg_dir(), mode=0o700)
    a = TS.build_r1n_a()
    b = TS.build_r1n_b()
    study_a = _study_document(
        a, study="A_N",
        n_runs=len(a.conditions) * N_BLOCK,
        contrasts=[dict(a.primary.to_dict()), dict(a.negative_control_spec["contrast"])])
    study_b = _study_document(
        b, study="B_N", n_runs=len(b.conditions) * N_BLOCK,
        contrasts=[dict(b.primary.to_dict())])
    files = {
        "study_A_N.json": study_a,
        "study_B_N.json": study_b,
        "protocol.json": build_protocol(),
        "budgets.json": {
            "schema": f"{SCHEMA}.budgets",
            "gateway": GATEWAY_BUDGET,
            "session": SESSION_BUDGET,
            "double_face_note": "两面分列不混算（S0 残尾③）",
            "approvals": "quota/approvals/s1-*.json（自批；逐次记账）",
        },
    }
    written: dict[str, str] = {}
    for name, doc in files.items():
        path = _write(target / name, doc)
        written[name] = _sha_file(path)
    bundle_sha = _sha_bytes(_canonical(
        {name: written[name] for name in sorted(written)}))
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "bundle_sha256": bundle_sha,
        "files": {name: {"sha256": written[name],
                         "bytes": (target / name).stat().st_size}
                  for name in sorted(written)},
        "built_at_utc": utc_now_iso_z(),
        "task_spec_sha256": {"A_N": a.sha256, "B_N": b.sha256},
        "scene_digests": {sid: _sha_bytes(_canonical(S.scene_doc(sid).to_dict()))
                          for sid in sorted(S.SCENE_REGISTRY)},
        "statistics_method_sha256": _sha_bytes(
            _canonical(ST.public_method_document())),
        "rubric_sha256": R.rubric_sha256(),
    }
    _write(target / "manifest.json", manifest)
    lock = target / "LOCK.json"
    if lock.exists():
        lock.unlink()  # 新 bundle 必须重新双签（旧签名自动失效，不留歧义）
    return manifest


def bundle_sha(root: Path | None = None) -> str:
    """从 manifest 复算 bundle sha（逐件重读；不符即 PREG-REG-CHANGED）。"""
    target = Path(root or rsi_s1_prereg_dir())
    manifest_path = target / "manifest.json"
    if not manifest_path.is_file():
        raise SystemExit("PRE-REG-MISSING: manifest.json 不存在（先 build）")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    files = manifest.get("files") or {}
    actual: dict[str, str] = {}
    for name in sorted(files):
        path = target / name
        if not path.is_file():
            raise SystemExit(f"PRE-REG-MISSING: {name}")
        actual[name] = _sha_file(path)
        if actual[name] != files[name]["sha256"]:
            raise SystemExit(f"PRE-REG-CHANGED: {name} 与 manifest 不符（改件即新 bundle）")
    computed = _sha_bytes(_canonical(actual))
    if computed != manifest.get("bundle_sha256"):
        raise SystemExit("PRE-REG-CHANGED: bundle_sha256 不符")
    return computed


def sign(*, signer: str, role: str, root: Path | None = None) -> dict[str, Any]:
    target = Path(root or rsi_s1_prereg_dir())
    sha = bundle_sha(target)
    lock_path = target / "LOCK.json"
    if lock_path.is_file():
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
    else:
        lock = {"schema": LOCK_SCHEMA, "bundle_sha256": sha, "signatures": []}
    if lock.get("bundle_sha256") != sha:
        raise SystemExit("PRE-REG-CHANGED: LOCK 绑定的 bundle 已变（重新 build+双签）")
    if any(s.get("signer") == signer for s in lock["signatures"]):
        raise SystemExit(f"PRE-REG-DUPLICATE-SIGNER: {signer} 已签")
    lock["signatures"].append({"signer": signer, "role": role,
                               "bundle_sha256": sha,
                               "signed_at_utc": utc_now_iso_z()})
    _write(lock_path, lock)
    return lock


def verify_prereg_lock(root: Path | None = None, *, min_signers: int = 2) -> dict[str, Any]:
    """复算 bundle + 校验两签互异；任何不符即 SystemExit/异常（fail-closed）。"""
    target = Path(root or rsi_s1_prereg_dir())
    sha = bundle_sha(target)
    lock_path = target / "LOCK.json"
    if not lock_path.is_file():
        raise SystemExit("PRE-REG-UNLOCKED: LOCK.json 不存在")
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    if lock.get("schema") != LOCK_SCHEMA:
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
    return {"bundle_sha256": sha, "signers": signers,
            "signed_at_utc": [s.get("signed_at_utc") for s in signatures],
            "verified_at_utc": utc_now_iso_z()}


def _cmd_build(args) -> int:
    manifest = build_prereg()
    print(json.dumps({"bundle_sha256": manifest["bundle_sha256"],
                      "files": sorted(manifest["files"])}, ensure_ascii=False))
    return 0


def _cmd_sign(args) -> int:
    lock = sign(signer=args.signer, role=args.role)
    print(json.dumps(lock, ensure_ascii=False, indent=2))
    return 0


def _cmd_verify(args) -> int:
    try:
        report = verify_prereg_lock()
    except SystemExit as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("build")
    p_sign = sub.add_parser("sign")
    p_sign.add_argument("--signer", required=True)
    p_sign.add_argument("--role", required=True)
    sub.add_parser("verify")
    args = parser.parse_args(argv)
    return {"build": _cmd_build, "sign": _cmd_sign,
            "verify": _cmd_verify}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
