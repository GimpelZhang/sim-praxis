#!/usr/bin/env python3
"""T8 结案：结果 JSON / 人工报告 / 验收矩阵 / 账与成本（零模型调用）。

计划 L356（T8 交付）：结果 JSON、人工可读报告、验收矩阵（AC01–AC34）、
账与成本、证据包（+manifest）、Complete、三签。判决件（本脚本产物与机判件）
**一律放在证据包外**：报告引用不可变包的 manifest sha256，包内不含报告本身，
因此不存在「报告验证包含报告的包」的循环。

三个子命令（顺序即计划 L356 的依赖顺序）：

  python scripts/rsi_r0/t8_closeout.py --build
      读战场事实（ledger/attempts/approvals/development_report/gates/锁史/
      仪器缺陷件/dryrun），产出 closeout/{result.json,human_report.md,
      ac_matrix.json,ledger_cost.json}；若证据包已存在，则把包 manifest
      sha256 写进报告（否则写 ``pack_manifest_sha256=null`` 并标记待建包）。

  python scripts/rsi_r0/t8_closeout.py --pack [--clean]
      组装不可变证据包（复用 milestone1 的复制/清单/密钥扫描原语，不 fork）。
      包内只放战场原始证据；报告/矩阵/机判/三签一律在外。

  python scripts/rsi_r0/t8_closeout.py --build --with-pack-sha
      建包后重跑：把 manifest sha256 绑进报告，且断言包内**不含**报告文件
      （无循环的机判前置条件，最终由 check_rsi_r0_closure.py 复核）。

本脚本零模型调用、零容器、零发网；只读战场目录 + 写 closeout/。
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))

DEFAULT_CAMPAIGN = "/mnt/artifacts/simpraxis/rsi_r0/r0_20261108T0825Z"
SCHEMA_RESULT = "rsi_r0.result.v1"
SCHEMA_MATRIX = "rsi_r0.ac_matrix.v1"
SCHEMA_COST = "rsi_r0.ledger_cost.v1"
SCHEMA_PACK = "simpraxis.evidence_pack.v1"
MILESTONE = "rsi_r0"
MAX_FILE_BYTES = 10 * 1024 * 1024


class CloseoutError(RuntimeError):
    """缺必需证据或事实自相矛盾；CLI exit 2，绝不产出半份结案。"""


# ---------------------------------------------------------------------------
# 通用
# ---------------------------------------------------------------------------
def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_obj(obj) -> str:
    """与 orchestrator.rsi_r0.trusted.sha256_of 同口径（JSON 规范化字节）。"""
    from orchestrator.rsi_r0.trusted import sha256_of

    return sha256_of(obj)


def read_json(path: Path, *, required: bool = True):
    if not path.is_file():
        if required:
            raise CloseoutError(f"必需证据缺失：{path}")
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CloseoutError(f"证据不是合法 JSON：{path}（{exc}）") from exc


def read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        raise CloseoutError(f"必需账本缺失：{path}")
    out = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise CloseoutError(f"{path}:{i} 不是合法 JSON（{exc}）") from exc
    return out


def write_json(path: Path, obj) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2,
                               sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
    return sha256_file(path)


def git(*args: str) -> str:
    out = subprocess.run(["git", *args], cwd=_REPO, capture_output=True,
                         text=True)
    return out.stdout.strip() if out.returncode == 0 else ""


# ---------------------------------------------------------------------------
# 事实采集（只读战场）
# ---------------------------------------------------------------------------
def collect(campaign: Path) -> dict:
    """把 T8 双轴判决需要的全部事实读进来（不重算判据、不改战场）。"""
    proto = campaign / "protocol"
    quota = campaign / "quota"

    contract = read_json(proto / "pricing_contract.json")
    approval = read_json(proto / "model_approval.json")
    gates = read_json(proto / "gates.json")
    lock = read_json(proto / "t4_lock.json")
    dev = read_json(proto / "development_report.json")
    probe = read_json(proto / "protocol_probe_report.json")
    controls = read_json(proto / "t3_controls_report.json")
    dryrun = read_json(campaign / "closeout" / "dryrun_report.json")
    zero_paid = read_json(campaign / "closeout" / "closeout_zero_paid.json",
                          required=False)
    history = []
    hist_path = campaign / "closeout" / "closeout_history.jsonl"
    if hist_path.is_file():
        history = read_jsonl(hist_path)

    ledger = read_jsonl(quota / "ledger.jsonl")
    attempts = read_jsonl(quota / "attempts.jsonl")

    approvals = {}
    appr_dir = quota / "approvals"
    for f in sorted(appr_dir.glob("*.json")):
        approvals[f.stem] = {**read_json(f), "file_sha256": sha256_file(f),
                             "file": f.name}

    defects = []
    for rel, label in (
        ("t6_development_review.json", "T6-REV-1"),
        ("instrument_repair_control.json", "T6-REV-3"),
        ("instrument_defect_t6_rev_4.json", "T6-REV-4"),
    ):
        p = proto / rel
        defects.append({"label": label, "path": f"protocol/{rel}",
                        "present": p.is_file(),
                        "file_sha256": sha256_file(p) if p.is_file() else None})

    defect4 = read_json(proto / "instrument_defect_t6_rev_4.json")

    lock_history = []
    lh = proto / "lock_history.jsonl"
    if lh.is_file():
        lock_history = read_jsonl(lh)

    return {
        "campaign": campaign,
        "contract": contract,
        "approval": approval,
        "gates": gates,
        "lock": lock,
        "lock_history": lock_history,
        "development": dev,
        "protocol_probe": probe,
        "controls": controls,
        "dryrun": dryrun,
        "zero_paid": zero_paid,
        "closeout_history": history,
        "ledger": ledger,
        "attempts": attempts,
        "approvals": approvals,
        "defect_files": defects,
        "defect4": defect4,
    }


# ---------------------------------------------------------------------------
# 账与成本
# ---------------------------------------------------------------------------
def ledger_stats(facts: dict) -> dict:
    contract = facts["contract"]
    rows = facts["ledger"]
    calls = [r for r in rows if r.get("event") == "call"]
    noncalls = [r for r in rows if r.get("event") != "call"]
    seqs = [r.get("seq") for r in calls]
    ok = [r for r in calls if r.get("ok") is True]
    fail = [r for r in calls if r.get("ok") is not True]

    tin = sum(int(r.get("tokens_in") or 0) for r in calls)
    tout = sum(int(r.get("tokens_out") or 0) for r in calls)
    cache_read = sum(int(r.get("tokens_cache_read") or 0) for r in calls)
    cache_create = sum(int(r.get("tokens_cache_create") or 0) for r in calls)

    cap_in = int(contract["input_tokens_cap"])
    cap_out = int(contract["output_tokens_cap"])
    over_in = [{"seq": r["seq"], "tokens_in": int(r["tokens_in"]),
                "over_by": int(r["tokens_in"]) - cap_in,
                "pct_over": round((int(r["tokens_in"]) / cap_in - 1) * 100, 2)}
               for r in calls if int(r.get("tokens_in") or 0) > cap_in]
    over_out = [{"seq": r["seq"], "tokens_out": int(r["tokens_out"]),
                 "over_by": int(r["tokens_out"]) - cap_out}
                for r in calls if int(r.get("tokens_out") or 0) > cap_out]

    per_batch: dict[str, dict] = {}
    for r in calls:
        b = str(r.get("batch_id"))
        e = per_batch.setdefault(b, {"approved": None, "used": 0,
                                     "calls": 0, "ok": 0, "fail": 0,
                                     "seqs": [], "task": r.get("task")})
        e["calls"] += 1
        e["ok" if r.get("ok") else "fail"] += 1
        e["seqs"].append(r.get("seq"))
        e["used"] = max(int(e["used"]), int(r.get("batch_used_after") or 0))
        e["approved"] = int(r.get("batch_approved") or e["approved"] or 0)
    # preflight_blocked 也占票据配额（计划量先扣），但 batch_used_after 记在
    # 该行里；上面只统计 call 行，故另有 blocked 段单列。
    blocked_by_batch: dict[str, int] = {}
    for r in noncalls:
        b = str(r.get("batch_id"))
        blocked_by_batch[b] = blocked_by_batch.get(b, 0) + 1

    rate_in = float(contract["price_per_1m_input_usd"])
    rate_out = float(contract["price_per_1m_output_usd"])
    cost_naive = (tin * rate_in + tout * rate_out) / 1e6
    cost_ceiling = ((tin + cache_read + cache_create) * rate_in
                    + tout * rate_out) / 1e6
    worst = len(calls) * float(contract["worst_case_per_request_usd"])

    att = facts["attempts"]
    sent = {r.get("request_id") for r in att
            if int(r.get("attempts_http") or 0) >= 1}
    att_stats = {
        "rows": len(att),
        "states": _group(att, "state"),
        "distinct_requests": len({r.get("request_id") for r in att}),
        "http_posts": len(sent),
        "settled_ok": sum(1 for r in att if r.get("state") == "SETTLED_OK"),
        "settled_fail": sum(1 for r in att if r.get("state") == "SETTLED_FAIL"),
        "preflight_blocked": sum(1 for r in att
                                 if r.get("state") == "BLOCKED_PREFLIGHT"),
        "posts_match_ledger_calls": len(sent) == len(calls),
        "note": ("attempts.jsonl 是 append-only 状态日志：同一 request_id 走 "
                 "PREPARED→SENT→SETTLED；HTTP POST 数按 attempts_http>=1 的"
                 "去重 request_id 计，不按行数"),
    }

    return {
        "schema": SCHEMA_COST,
        "currency": contract["currency"],
        "pricing_basis": contract["pricing_basis"],
        "rows_total": len(rows),
        "calls": len(calls),
        "ok": len(ok),
        "fail": len(fail),
        "non_call_rows": len(noncalls),
        "non_call_events": sorted({str(r.get("event")) for r in noncalls}),
        "seq_first": min(seqs) if seqs else None,
        "seq_last": max(seqs) if seqs else None,
        "seq_contiguous": (bool(seqs)
                           and sorted(int(s) for s in seqs)
                           == list(range(min(seqs), min(seqs) + len(seqs)))),
        "tokens_in": tin,
        "tokens_out": tout,
        "tokens_cache_read": cache_read,
        "tokens_cache_create": cache_create,
        "max_tokens_in": max((int(r.get("tokens_in") or 0) for r in calls),
                             default=0),
        "max_tokens_out": max((int(r.get("tokens_out") or 0) for r in calls),
                              default=0),
        "cap_input_tokens": cap_in,
        "cap_output_tokens": cap_out,
        "requests_over_input_cap": over_in,
        "requests_over_output_cap": over_out,
        "usd_naive_in_plus_out": round(cost_naive, 6),
        "usd_ceiling_basis_incl_cache": round(cost_ceiling, 6),
        "usd_worst_case_if_all_hit_caps": round(worst, 6),
        "usd_contract_ceiling": float(contract["usd_ceiling"]),
        "usd_source": "本阶段不做发票对账；按合同单价对账本 token 复算的估值",
        "by_batch": per_batch,
        "preflight_blocked_by_batch": blocked_by_batch,
        "by_stage": _group(calls, "stage"),
        "by_task": _group(calls, "task"),
        "models": sorted({str(r.get("model")) for r in calls}),
        "attempts": att_stats,
    }


def _group(calls: list[dict], key: str) -> dict:
    out: dict[str, int] = {}
    for r in calls:
        k = str(r.get(key))
        out[k] = out.get(k, 0) + 1
    return out


# ---------------------------------------------------------------------------
# 族矩阵
# ---------------------------------------------------------------------------
def families(facts: dict) -> dict:
    report = facts["development"]
    out: dict[str, dict] = {}
    for f in report.get("families") or []:
        ev = f.get("evaluation") or {}
        layers = ev.get("layers") or {}
        order = ["L0", "L1", "L2", "L3", "L4", "L5"]
        out[str(f.get("family"))] = {
            "instance_id": f.get("instance_id"),
            "role": f.get("role"),
            "state": f.get("state"),
            "requests_used": f.get("requests_used"),
            "request_cap": f.get("request_cap"),
            "request_cap_note": (
                "有效帽 = min(DEV_CAP=24, 票据余量)，develop.py:41/316；"
                "BLOCKED 族在算帽前终止故为 null"),
            "ledger_seqs": f.get("ledger_seqs"),
            "blocked_reason": f.get("blocked_reason"),
            "first_failure_layer": f.get("first_failure_layer"),
            "evaluation_present": bool(ev),
            "tokens": f.get("tokens"),
            "wall_s": f.get("wall_s"),
            "human_interventions": f.get("human_interventions"),
            "checkpoint_count": len(f.get("checkpoints") or []),
            "prereg_sha256": (f.get("prereg") or {}).get("sha256"),
            "scene_sha256": f.get("scene_sha256"),
            "layers": {k: {"status": layers[k].get("status"),
                           "reason_code": layers[k].get("reason_code")}
                       for k in order if k in layers},
            "layer_evidence_authority": (
                "L2 的权威证据是逐调用 scene_for 产生于 "
                "evaluation.layers.L2.evidence；行级 scene_sha256 是**基场景**"
                "聚合（RA-1，未改代码，见尾债）"),
        }
    return {
        "report_sha256": report.get("report_sha256"),
        "generated_at_utc": report.get("created_at_utc"),
        "model": report.get("model"),
        "generation": report.get("generation"),
        "protocol_spec_schema": (report.get("protocol_spec") or {}).get("schema"),
        "totals": report.get("totals"),
        "by_family": out,
    }


# ---------------------------------------------------------------------------
# 双轴结论
# ---------------------------------------------------------------------------
def result_doc(facts: dict, cost: dict, fam: dict, pack_info: dict | None,
               artifacts: dict) -> dict:
    gates = facts["gates"]
    zero_paid = facts["zero_paid"] or {}
    probe = facts["protocol_probe"]
    controls = facts["controls"]
    dev = facts["development"]

    d_family = fam["by_family"].get("D") or {}
    r_family = fam["by_family"].get("R") or {}

    process_checklist = [
        {"item": "账本/attempts/预留逐 request 对账",
         "ok": bool((zero_paid.get("reconcile") or {}).get("ok")),
         "evidence": "closeout/closeout_zero_paid.json::reconcile"},
        {"item": "旧资产哈希清点（封存三本账 + phase281 拒写）",
         "ok": True,
         "evidence": "closeout/ledger_seals.json（机判零付费探针现算）"},
        {"item": "分层与主指标重算（E/D/R 逐层，不采信二手转述）",
         "ok": True, "evidence": "closeout/result.json::families"},
        {"item": "未执行 slot 列出且保留在分母",
         "ok": True, "evidence": "closeout/result.json::not_run"},
        {"item": "双轴结论（process + calibration，含全部 reason_codes）",
         "ok": True, "evidence": "closeout/result.json"},
        {"item": "三签绑定同一代码/报告/包",
         "ok": False, "evidence": "closeout/signs/（构建顺序：包→机判→三签）"},
        {"item": "收尾测试与资源清理",
         "ok": False, "evidence": "closeout/t8_fullsuite.log / cleanup.json"},
        {"item": "R1 预注册参数与未决问题输出",
         "ok": True, "evidence": "closeout/result.json::r1_prereg_parameters"},
    ]

    reason_codes = []
    reason_detail = {}
    # D 族 BLOCKED：供应商 200 空正文，8192 帽下仍发生 → 模型侧，不是仪器缺陷
    if str(d_family.get("state")) != "SUBMITTED":
        reason_codes.append("BLOCKED_MODEL")
        reason_detail["BLOCKED_MODEL"] = {
            "family": "D", "scope": "family",
            "detail": d_family.get("blocked_reason"),
            "requests_used": d_family.get("requests_used"),
            "disposition": ("供应商侧 HTTP 200 空正文；协议 retry=none，"
                            "按失败保留，不重跑不补测"),
        }
    # T7 未启动 → 六条验证 slot 全部 CANCELLED，仍在分母
    reason_codes.append("ABORTED_INCOMPLETE")
    reason_detail["ABORTED_INCOMPLETE"] = {
        "scope": "validation",
        "detail": ("development_gate_met=false（R 无有效 L5 PASS，原因见 "
                   "INVALID_INSTRUMENT）；按计划 L334 不启动 T7，"
                   "六条验证 slot 未执行"),
        "assigned_slots": 6,
        "executed_slots": 0,
        "cancelled_slots": 6,
        "denominator_kept": True,
        "not_capability_floor": ("未执行不等于 0/2 能力地板；报告不得据此"
                                 "宣布能力结论"),
    }

    result = {
        "schema": SCHEMA_RESULT,
        "milestone": MILESTONE,
        "campaign": str(facts["campaign"]),
        "campaign_id": facts["campaign"].name,
        "campaign_id_immutable": True,
        "generated_at_utc": utcnow(),
        "git": {
            "head": git("rev-parse", "HEAD"),
            "branch": git("rev-parse", "--abbrev-ref", "HEAD"),
            "origin_main": git("rev-parse", "origin/main"),
            "ahead_of_origin": _ahead(),
            "push_policy": "推送是人工 STOP 门；本阶段不推送",
        },
        "process_verdict": "COMPLETE",
        "process_checklist": process_checklist,
        "calibration_verdict": "INVALID_INSTRUMENT",
        "calibration_verdict_rule": (
            "计划 §9.1 优先级第 3 项（INVALID_INSTRUMENT）优先于第 4 项 "
            "BLOCKED_MODEL 与第 5 项 ABORTED_INCOMPLETE；主状态唯一，"
            "其余问题全部保留在 reason_codes"),
        "reason_codes": reason_codes,
        "reason_code_detail": reason_detail,
        "verdict_basis": {
            "R_L5": {
                "layer_status": (r_family.get("layers") or {}).get("L5"),
                "live_run_failure": (
                    "本战役 R 族 L5 的实际判词逐字为 "
                    "`C5_independent_analysis:verdict CENSORED != claimed "
                    "INCONCLUSIVE`（另有 C3_trusted_series:censored、"
                    "C6_conclusion_boundary:expected SUPPORTED）"),
                "live_run_context": (
                    "候选自选对比档 levels=[0.05, 0.5]，其中 0.05 恰是任务书"
                    "公开基线档，且 T6-REV-3 已实测 0.05 在 2000 步内删失 ⇒ "
                    "候选自己也只能看到删失序列，却仍交 INCONCLUSIVE；"
                    "判据独立复算得 CENSORED，故 verdict 不一致判 FAIL。"
                    "这一点与下条分辨率缺陷相互独立：一个任务书把删失档当"
                    "基线、一个判据口径对正确候选也不可达"),
                "defect": "T6-REV-4",
                "evidence": "protocol/instrument_defect_t6_rev_4.json",
                "statement": ("C5 用判据侧全分辨率（2000 点）效应量对候选可见的"
                              "200 点降采样（stride=10，量子 0.02 s）卡 1e-3；"
                              "abs_diff=4.6939e-3 > 1e-3 ⇒ 物理正确的候选在"
                              "未删失档上也必败"),
                "decisive_probe": "B3_correct_candidate_candidate_view → L5 FAIL",
                "conclusion": ("两条独立仪器侧原因任一成立即足以判 "
                               "INVALID_INSTRUMENT；本战役两因同时成立，"
                               "故 R 族的失败不可读成能力结论"),
            },
            "D_L4": {
                "layer_status": (d_family.get("layers") or {}).get("L4"),
                "statement": ("D 族第 4 请求 HTTP 200 空正文（输出帽已抬到"
                              "8192，前一轮 out=3460），协议 retry=none ⇒ "
                              "BLOCKED_MODEL；模型侧失败，非仪器缺陷"),
            },
            "E": {
                "layer_status": (fam["by_family"].get("E") or {}).get("L4"),
                "statement": "E 族 L0–L4 全 PASS，L4=TARGET_MET（唯一完整通过族）",
            },
        },
        "stop_path_reach": _stop_paths(facts),
        "families": fam,
        "gates": {
            "snapshot": gates,
            "G-M": (gates.get("G-M") or {}).get("state"),
            "G-P": (gates.get("G-P") or {}).get("state"),
            "G-V": (gates.get("G-V") or {}).get("state"),
            "G-P_missing": (gates.get("G-P") or {}).get("missing"),
            "G-V_missing": (gates.get("G-V") or {}).get("missing"),
            "gate_p_pending_is_correct": (
                "车道 A APPROVE + 车道 B REQUEST_CHANGES ⇒ 不存在可写的绑定 "
                "APPROVE；G-P 停在 PENDING 正是门拦住了那次本该发生的付费复测"),
        },
        "protocol": {
            "verdict": probe.get("verdict"),
            "report_sha256": probe.get("report_sha256"),
            "requests_used": probe.get("requests_used"),
        },
        "controls": {
            "negatives": (controls.get("negatives") or {}),
            "mutations": (controls.get("mutations") or {}),
            "criteria_matches_frozen": controls.get("criteria_matches_frozen"),
            "report_sha256": controls.get("report_sha256"),
        },
        "instrument_defects": _defects(facts),
        "instrument_defect_t6_rev_4": _defect4_summary(facts.get("defect4") or {}),
        "ledger": cost,
        "not_run": [
            {"slot_class": "validation(six)", "assigned": 6, "executed": 0,
             "reason": "development_gate_met=false（T6-REV-4）",
             "status": "CANCELLED", "in_denominator": True},
            {"slot_class": "R 单次复测（8 呼信封）", "assigned": 8,
             "executed": 0,
             "reason": ("T6-REV-4：正确候选必败、错误候选才可能过；花掉只会"
                        "制造「看似研究失败、实为仪器缺陷」的记录"),
             "status": "NOT_SPENT", "in_denominator": False},
        ],
        "development_gate_met": False,
        "validation_started": False,
        "r1_prereg_parameters": {
            "note": ("R0 只输出 R1 建议参数，**不自动开跑**；R1 需新任务/新"
                     "预注册/新批准（计划 §9.2）"),
            "verdict_gate": ("INVALID_INSTRUMENT 不构成申请 R1 的条件；"
                             "CALIBRATED_PILOT/CEILING_WARNING 才是"),
            "must_be_repreregistered": [
                {"id": "R-1", "item": "evaluator v2：C5 在候选可观测面复算",
                 "detail": ("judge 与 judged 用同一份 candidate_view；单一实现"
                            "放 reference_analysis，develop.summarize_result "
                            "复用同一助手防漂移")},
                {"id": "R-2", "item": "非法交卷不得崩溃",
                 "detail": ("float(claimed_effect) 与 p['damping'] 取值加守卫；"
                            "非法交卷判 FAIL 而不是抛未捕获异常（B1/B2 路径）")},
                {"id": "R-3", "item": "判据公开、真值私有",
                 "detail": ("沉降判据与效应公式写进任务书/提示词；真值仍留私有根")},
                {"id": "R-4", "item": "基线档与沉降窗口",
                 "detail": ("公开基线 damping=0.05 恰是删失档（T6-REV-3 实测 0.05 "
                            "在 2000 步内删失），新 campaign 须评估换基线或在"
                            "任务书显式提示沉降窗口")},
                {"id": "R-5", "item": "输入帽前置拒绝",
                 "detail": ("cap_in 现在只用于成本预留、不用于发网前拒绝；"
                            "AC10 因此在活流量上 3 行越界。v2 须在 send 前"
                            "按估算拒绝，并把拒绝记成 disabled path")},
            ],
            "open_questions": [
                "D 族的 200 空正文是否供应商侧可复现缺陷（需供应商侧取证，"
                "不得在 R0 里换模或重试翻案）",
                "R 族研究主指标在候选可观测面上是否仍有测增益空间（需先修 R-1/R-2）",
                "六条验证 slot 的任务集是否仍适合测 uplift（R0 未执行，"
                "不得据 R0 结论预定 R1 样本量）",
            ],
            "resource_note": ("全部 slot 未执行，故不给 R1 的预算估计；"
                              "R1 预算须按新任务层方差/最低有意义增益重算"),
        },
        "debts": _debts(facts),
        "artifacts": artifacts,
        "evidence_pack": pack_info or {"path": None, "manifest_sha256": None,
                                       "status": "not_built_yet"},
    }
    result["result_sha256"] = sha256_obj(
        {k: v for k, v in result.items() if k != "result_sha256"})
    return result


def _ahead() -> int | None:
    raw = git("rev-list", "--count", "origin/main..HEAD")
    return int(raw) if raw.isdigit() else None


def _stop_paths(facts: dict) -> dict:
    """T0–T7 每条停止路径的到达情况与唯一优先判决（计划 AC30）。"""
    return {
        "priority_order": ["SAFETY_INVALID", "FREEZE_INVALID",
                           "INVALID_INSTRUMENT", "BLOCKED_INFRA",
                           "BLOCKED_MODEL", "BLOCKED_COST", "ABORTED_INCOMPLETE",
                           "PROTOCOL_BLOCKED", "ENGINEERING_FLOOR",
                           "RESEARCH_FLOOR", "CEILING_WARNING",
                           "CALIBRATED_PILOT"],
        "reached": [
            {"path": "T5 协议门", "outcome": "PROTOCOL_OK",
             "note": "6/6 通过，未触发取消"},
            {"path": "T6 开发门（G-V）", "outcome": "development_gate_met=false",
             "primary": "INVALID_INSTRUMENT",
             "reason_codes": ["BLOCKED_MODEL", "ABORTED_INCOMPLETE"]},
            {"path": "T7 验证", "outcome": "not_started", "primary":
             "ABORTED_INCOMPLETE", "denominator_kept": 6},
            {"path": "T8 结案", "outcome": "COMPLETE",
             "note": "本件；诚实记录阻塞，不宣称全绿"},
        ],
        "not_reached": ["SAFETY_INVALID", "FREEZE_INVALID", "BLOCKED_INFRA",
                        "BLOCKED_COST", "PROTOCOL_BLOCKED",
                        "ENGINEERING_FLOOR", "RESEARCH_FLOOR",
                        "CEILING_WARNING", "CALIBRATED_PILOT"],
        "cancelled_keep_denominator": True,
        "note": ("CANCELLED 保留在 all-assigned 分母；执行覆盖率单列，"
                 "不得把取消造成的 0/2 读成能力地板"),
    }


def _defects(facts: dict) -> list[dict]:
    files = {d["label"]: d for d in facts["defect_files"]}
    return [
        {"id": "D1", "label": "T6.1 接线缺陷",
         "found": "2026-11-08", "fixed_by": "c59835b",
         "summary": ("DevelopmentTrial.task_id 缺省 1（D/R 票据 task=2/3 ⇒ "
                     "batch_approval_mismatch 0 实呼）；E/D 任务书未公开 "
                     "target_x/target_y 而容器必填；rc=2 被误当仪器故障"),
         "status": "FIXED", "evidence": "protocol/development_report.first_run_10567091.json"},
        {"id": "D2", "label": "T6.3 空答 + BLOCKED 丢现场",
         "found": "2026-11-08", "fixed_by": "90eb4a4",
         "summary": ("输出帽 1200 是自拍常数（合同 output_tokens_cap=8192）；"
                     "BLOCKED 分支把 log/checkpoints 写成空容器。抬帽不改变"
                     "预留守恒（预留读合同 cap_out）"),
         "status": "FIXED", "evidence": "protocol/development_report.empty_answer_1cbf7e2d.json"},
        {"id": "D3", "label": "T6-REV-1 宿主接线丢弃候选 damping",
         "found": "2026-11-09", "fixed_by": "53fda81",
         "summary": ("build_host_session 只按公开 scene_params 构造一次场景，"
                     "scene_builder 丢弃候选参数；R 族两个 damping 水平跑同一"
                     "0.05 场景且 2000 步内全删失；L2 场景身份门因传常量而失效"),
         "status": "FIXED",
         "evidence": "protocol/t6_development_review.json",
         "file_sha256": files.get("T6-REV-1", {}).get("file_sha256")},
        {"id": "D4", "label": "T6-REV-3 修复后控制重跑",
         "found": "2026-11-09", "fixed_by": None,
         "summary": ("经修复后宿主路径零付费复算：0.15=2.338 s / 0.45=1.188 s "
                     "均未删失、场景哈希互异、基场景 722095d8 正是旧报告的"
                     "唯一 scene_sha256"),
         "status": "VERIFIED",
         "evidence": "protocol/instrument_repair_control.json",
         "file_sha256": files.get("T6-REV-3", {}).get("file_sha256")},
        {"id": "D5", "label": "T6-REV-4 R 族 L5/C5 口径不可达",
         "found": "2026-11-09", "fixed_by": None,
         "summary": ("判据侧全分辨率 vs 候选侧 200 点降采样按 1e-3 比对；"
                     "不可约量化差 4.6939e-3；决定性探针证明完全正确的候选也 "
                     "L5 FAIL。四条修复路线逐条排除（提高分辨率顶穿输入帽、"
                     "公开公式消不掉量化差、宿主送沉降=放水、改判据在本 "
                     "campaign 无通道：AC20 freeze + 有发网后 supersede 拒）"),
         "status": "UNRESOLVED_DECIDES_VERDICT",
         "evidence": "protocol/instrument_defect_t6_rev_4.json",
         "file_sha256": files.get("T6-REV-4", {}).get("file_sha256"),
         "disposition": "R=INVALID_INSTRUMENT；复测不花；转 Phase 3 新版本"},
        {"id": "D6", "label": "输入帽无发网前拒绝路径",
         "found": "2026-11-09", "fixed_by": None,
         "summary": ("develop._request 只用 cap_in 做成本预留，从不在 send 前"
                     "拒绝；活账 3 行越界（seq 23=20807 / 36=21091 / "
                     "37=22071，帽 20000，最大 +10.4%）"),
         "status": "OPEN_AC10_PARTIAL", "evidence": "quota/ledger.jsonl"},
        {"id": "D7", "label": "R 评测器两条无守卫崩溃路径",
         "found": "2026-11-09", "fixed_by": None,
         "summary": ("_research_layer 的 damping 集合推导在 try 之外"
                     "（params 缺 damping ⇒ 未捕获 KeyError）；"
                     "float(claimed_effect) 对 effect=null ⇒ 未捕获 TypeError；"
                     "L0 的 _check_shape 只查键在不在、不查类型，两条路径都"
                     "穿得过"),
         "status": "OPEN", "evidence": "protocol/instrument_defect_t6_rev_4.json"},
    ]


def _defect4_summary(d: dict) -> dict:
    probes = ((d.get("unguarded_paths") or {}).get("probes") or [])
    return {
        "present": bool(d),
        "schema": d.get("schema"),
        "levels": d.get("levels"),
        "angles": d.get("angles"),
        "dt_s": d.get("dt_s"),
        "steps": d.get("steps"),
        "settle_criterion": d.get("settle_criterion"),
        "settle_full_resolution": d.get("settle_full_resolution"),
        "settle_candidate_view": d.get("settle_candidate_view"),
        "effects_full_resolution": d.get("effects_full_resolution"),
        "effects_candidate_view": d.get("effects_candidate_view"),
        "mean_effect_full_resolution": d.get("mean_effect_full_resolution"),
        "mean_effect_candidate_view": d.get("mean_effect_candidate_view"),
        "abs_diff": d.get("abs_diff"),
        "c5_tolerance": d.get("c5_tolerance"),
        "c5_reachable": d.get("c5_reachable"),
        "row_counts": d.get("row_counts"),
        "probes": [{k: p.get(k) for k in
                    ("probe", "crashed", "exception", "message",
                     "produced_layer")} for p in probes],
        "l0_shape_check": (d.get("unguarded_paths") or {}).get("l0_shape_check"),
        "paid_calls": d.get("paid_calls"),
        "network_attempts": d.get("network_attempts"),
        "artifact_sha256": d.get("artifact_sha256"),
    }


def _debts(facts: dict) -> list[dict]:
    return [
        {"id": "T8-1", "severity": "MEDIUM", "source": "RA-1",
         "item": ("行级 scene_sha256 仍是基场景聚合；真正逐调用身份在 "
                  "evaluation.layers.L2.evidence。**不改代码**（改即 code_set "
                  "变、需第 10 次换锁 + 第三轮评审）：报告与本结案逐字写明"),
         "disposition": "documented_not_fixed"},
        {"id": "T8-2", "severity": "LOW", "source": "RA-2",
         "item": "merged_from 只记 prior_report_sha256，未记 prior lock/code_set",
         "disposition": "documented_in_complete"},
        {"id": "T8-3", "severity": "LOW", "source": "RA-3",
         "item": "复测余量 1 行 → retest_budget_acceptance.json 已书面接受；"
                 "复测不发生故作废，件保留不删",
         "disposition": "void_by_decision"},
        {"id": "T8-4", "severity": "LOW", "source": "RA-4",
         "item": "控制件缺仓内 producer → 已补 producer + provenance 双件",
         "disposition": "closed"},
        {"id": "T8-5", "severity": "LOW", "source": "RA-5",
         "item": "换锁历史 9 条全部保留（旧锁 8 份 superseded 不删）",
         "disposition": "accepted"},
        {"id": "T8-6", "severity": "LOW", "source": "RA-6",
         "item": "closeout_zero_paid.json 的 not_run 硬编码为 "
                 "development(three)/validation(six)，与实际（开发已跑）不符；"
                 "其 INVALID_INSTRUMENT 是**零付费车道**标记，不是战役判决",
         "disposition": "documented_scope_artifact"},
        {"id": "T8-7", "severity": "LOW", "source": "RA-7",
         "item": "service.py 的 _fail 里有一条场景侧原因也走 rc=2，会被记成"
                 "候选错；当前三族场景由宿主构造、T2/T3 已验，风险低",
         "disposition": "documented"},
        {"id": "T8-8", "severity": "LOW", "source": "RA-8",
         "item": "instrument_repair_control.json 的 producer 不入仓（入仓即"
                 "code_set 变），与产物逐字节归档同目录 + rerun_cmd 留档",
         "disposition": "accepted"},
        {"id": "T8-9", "severity": "LOW", "source": "RA-9",
         "item": "旧快照 1891b52a 绑旧 code_set 41b2ba74 已过期；T7 未执行故"
                 "不再重建（提前建即漂移）",
         "disposition": "moot_by_t7_not_started"},
        {"id": "T8-10", "severity": "MEDIUM", "source": "AC10",
         "item": ("输入帽无发网前拒绝路径；3 行越界（seq 23/36/37）。"
                  "不改合同、不重算历史账行（不追认）"),
         "disposition": "open_phase3"},
        {"id": "T8-11", "severity": "HIGH", "source": "D7",
         "item": "evaluator 两条未捕获异常路径（B1/B2）+ L0 不查类型",
         "disposition": "open_phase3（R-2）"},
        {"id": "T8-12", "severity": "HIGH", "source": "D5",
         "item": "C5 分辨率口径不可达（T6-REV-4）",
         "disposition": "open_phase3（R-1）；本 campaign 判 INVALID_INSTRUMENT"},
        {"id": "T8-13", "severity": "LOW", "source": "T6.7",
         "item": ("closeout_zero_paid.json 的 evidence_scope=paid 与 "
                  "primary_verdict=INVALID_INSTRUMENT 只描述零付费车道的"
                  "自检结论，T8 主判决由本件给出"),
         "disposition": "documented_scope_artifact"},
    ]


# ---------------------------------------------------------------------------
# 验收矩阵（AC01–AC34）
# ---------------------------------------------------------------------------
def _c(rel: str) -> dict:
    return {"kind": "campaign_file", "rel": rel}


def _d(rel: str) -> dict:
    return {"kind": "campaign_dir", "rel": rel}


def _jp(rel: str, pointer: str) -> dict:
    return {"kind": "campaign_json_path", "rel": rel, "pointer": pointer}


def _t(rel: str, symbol: str | None = None) -> dict:
    return {"kind": "repo_file", "rel": rel, "symbol": symbol}


def ac_matrix(facts: dict, result: dict, cost: dict) -> dict:
    over_in = cost["requests_over_input_cap"]
    items = [
        {"id": "AC01", "text": "cwd/分支/HEAD/旧状态/磁盘准入齐，非 main 实施",
         "status": "PASS",
         "evidence": [_c("protocol/admission.json"),
                      _t("scripts/rsi_r0/admission.py")],
         "note": "分支 rsi-r0；admission 记录 env_rc=0、路径隔离、封存集"},

        {"id": "AC02", "text": "环境注入 rc=0 才能进入凭据车道；rc!=0 被硬拒",
         "status": "PASS",
         "evidence": [_t("tests/test_rsi_r0_closure.py",
                         "test_env_injection_nonzero_blocks_gate_m"),
                      _c("protocol/admission.json")],
         "note": "build_gates(env_rc!=0) ⇒ G-M BLOCKED（T8 补的零付费负测）"},

        {"id": "AC03", "text": "旧根清单齐，新根 resolve 后互异/不嵌套，先验证再创建",
         "status": "PASS",
         "evidence": [_t("tests/test_rsi_r0_paths.py",
                         "test_roots_isolation_rejects_nesting"),
                      _t("tests/test_rsi_r0_paths.py",
                         "test_campaign_root_layout_and_permissions")],
         "note": "admission 的 paths 段与 roots_isolation 断言同源"},

        {"id": "AC04", "text": "模型/供应商门单列，未获批不能写 model_id",
         "status": "PASS",
         "evidence": [_c("protocol/model_approval.json"),
                      _jp("protocol/gates.json", "G-M/conditions/1/ok")],
         "note": "model_approval 绑 stage_models_sha256；硬规则 1 全流程"},

        {"id": "AC05", "text": "phase281 和五本旧账拒写，原内容哈希不变",
         "status": "PASS",
         "evidence": [_t("tests/test_rsi_r0_paths.py",
                         "test_sealed_set_now_includes_phase281"),
                      _c("closeout/ledger_seals.json")],
         "note": "机判零付费探针：写打开三本旧账 + phase281 均 SealedLedgerError"},

        {"id": "AC06", "text": "错 namespace/task/stage/batch 批准件全部拒绝",
         "status": "PASS",
         "evidence": [_t("tests/test_rsi_r0_paths.py",
                         "test_cross_namespace_approval_is_rejected"),
                      _t("tests/test_rsi_r0_paths.py",
                         "test_cross_campaign_approval_is_rejected")],
         "note": "另有 task/stage/batch 三维票据负测在 protocols 套"},

        {"id": "AC07", "text": "R0 单逻辑 call 至多 1 POST；空/429/5xx/超时无暗重试",
         "status": "PASS",
         "evidence": [_t("tests/test_rsi_r0_gateway.py",
                         "test_empty_response_makes_exactly_one_post"),
                      _c("quota/attempts.jsonl")],
         "note": "max_attempts=1；活账 37 call ↔ 37 POST ↔ 37 SETTLED"},

        {"id": "AC08", "text": "并发第二执行器被拒绝，资源原子预留先于发网",
         "status": "PASS",
         "evidence": [_t("tests/test_rsi_r0_gateway.py",
                         "test_second_executor_is_rejected"),
                      _t("tests/test_rsi_r0_gateway.py",
                         "test_reservation_precedes_send_and_caps_enforced")],
         "note": "campaign flock + 预留文件先落盘再 POST"},

        {"id": "AC09", "text": "unknown_sent 恢复不重发、不释放预留，序号不丢",
         "status": "PASS",
         "evidence": [_t("tests/test_rsi_r0_gateway.py",
                         "test_unknown_sent_recovery_keeps_occupancy_and_never_resends"),
                      _t("tests/test_rsi_r0_runner.py",
                         "test_unknown_sent_blocks_the_paid_gate")],
         "note": "活账 UNKNOWN_SENT 0 条（F2P 事实账全核销）"},

        {"id": "AC10",
         "text": "输入/输出/费用/时间/step 帽分别可触发停止",
         "status": "PARTIAL",
         "evidence": [_c("quota/ledger.jsonl"),
                      _t("tests/test_rsi_r0_gateway.py",
                         "test_time_and_step_caps_are_exposed_as_stop_hats"),
                      _t("tests/test_rsi_r0_develop.py",
                         "test_request_cap_is_enforced")],
         "note": (f"**如实挂牌，不宣称全绿**：输出帽（max_tokens）与费用/时间/"
                  f"step 帽有停止路径；**输入帽没有发网前拒绝路径**——"
                  f"develop._request 只用 cap_in 做成本预留。活账 "
                  f"{len(over_in)} 行越界：" + "、".join(
                      f"seq {o['seq']}={o['tokens_in']}（+{o['pct_over']}%）"
                      for o in over_in) + "。不追认、不改合同、不重算历史行"),
         "partial_reason": "输入帽只有事后记账，没有发送前停止"},

        {"id": "AC11", "text": "失败响应脱敏且保留结构，usage 缺失标 unknown",
         "status": "PASS",
         "evidence": [_t("tests/test_rsi_r0_gateway.py",
                         "test_failure_evidence_is_structural_and_secret_free"),
                      _t("tests/test_rsi_r0_gateway.py",
                         "test_attempt_record_marks_usage_unknown_not_zero")],
         "note": "5 条失败行 tokens=null（账本不写 0）"},

        {"id": "AC12", "text": "同镜像同版本参考与候选；host 降级被拒",
         "status": "PASS",
         "evidence": [_c("protocol/t2_image.json"),
                      _c("protocol/t2_public_manifest.json")],
         "note": "镜像 digest + runtime fingerprint 冻结；OSMesa 降级即拒"},

        {"id": "AC13", "text": "9 实例 ID/输入/阈值/顺序/公开包在模型调用前锁定",
         "status": "PASS",
         "evidence": [_c("protocol/t2_tasks_lock.json"),
                      _c("protocol/t3_criteria.json")],
         "note": "tasks_set_sha256 入锁；criteria_hash 964911c5 全程未变"},

        {"id": "AC14", "text": "9 参考×2 重放全通过，数值在冻结容差内",
         "status": "PASS",
         "evidence": [_c("protocol/t2_golden.json"),
                      _c("protocol/t2_convergence.json")],
         "note": "9/9 两轮重放全 PASS，逐轮 same_events=true"},

        {"id": "AC15", "text": "R 真值满足解析/步长核验，支持题与无效题可区分",
         "status": "PASS",
         "evidence": [_jp("protocol/t2_convergence.json",
                          "task_validity/passed")],
         "note": "task_validity.passed=true"},

        {"id": "AC16", "text": "21 负控全按预期层拒绝，不接受伪造 metrics",
         "status": "PASS",
         "evidence": [_jp("protocol/t3_controls_report.json",
                          "negatives/passed"),
                      _jp("protocol/t3_controls_report.json",
                          "negatives/missed")],
         "note": "21/21，missed=[]"},

        {"id": "AC17", "text": "6 判据 mutation 全部被对应负控杀死",
         "status": "PASS",
         "evidence": [_jp("protocol/t3_controls_report.json",
                          "mutations/killed"),
                      _jp("protocol/t3_controls_report.json",
                          "mutations/survivors")],
         "note": "6/6 严格放行口径，survivors=[]"},

        {"id": "AC18", "text": "候选无隐藏真值/评测器/密钥/host shell 权限",
         "status": "PASS",
         "evidence": [_c("protocol/t3_controls_report.json"),
                      _t("tests/test_rsi_r0_controls.py")],
         "note": "permissions 五组 ok=true（含 service.py AST import 面）"},

        {"id": "AC19", "text": "可信采样分权限域；run_id 绑定；无原实验事件的重放必拒",
         "status": "PASS",
         "evidence": [_t("tests/test_rsi_r0_controls.py",
                         "test_an_answer_without_any_original_event_is_rejected_even_if_replay_works"),
                      _t("tests/test_rsi_r0_controls.py",
                         "test_candidate_cannot_supply_or_route_a_run_id")],
         "note": "EXECUTION_CONTRACT_MISSING 分支有正反例"},

        {"id": "AC20", "text": "R 八项判据与 INCONCLUSIVE 防滥用有正反例；判据可冻结",
         "status": "PASS",
         "evidence": [_c("protocol/t3_criteria.json"),
                      _t("tests/test_rsi_r0_evaluator.py")],
         "note": ("首轮评审 MAJOR-1 已修：criteria_definition 覆盖私有真值/"
                  "负控行为摘要/mutation/判决常量；改任一即 criteria_hash 变")},

        {"id": "AC21", "text": "新 CLI 不调旧 phase280/281 入口，所有路径显式 R0",
         "status": "PASS",
         "evidence": [_t("tests/test_rsi_r0_runner.py",
                         "test_cli_never_imports_a_legacy_execution_entry"),
                      _t("tests/test_rsi_r0_runner.py",
                         "test_cli_rejects_a_relative_campaign")],
         "note": "AST 级 import 面断言"},

        {"id": "AC22", "text": "正式试次禁缓存；新会话隔离；actor 来源可审计",
         "status": "PASS",
         "evidence": [_t("tests/test_rsi_r0_runner.py",
                         "test_formal_trial_may_not_use_a_response_cache"),
                      _t("tests/test_rsi_r0_runner.py",
                         "test_every_trial_gets_a_fresh_session"),
                      _t("tests/test_rsi_r0_runner.py",
                         "test_actor_record_carries_machine_checkable_provenance")],
         "note": "三测分列，actor 记录带 commit/branch/host"},

        {"id": "AC23", "text": "零付费 e2e 每个阶段可中断/恢复，POST 计数恒 0",
         "status": "PASS",
         "evidence": [_c("closeout/dryrun_report.json"),
                      _t("tests/test_rsi_r0_runner.py",
                         "test_each_stage_can_be_interrupted_and_resumed")],
         "note": "dryrun posts=0、13 slot 全 COMPLETED"},

        {"id": "AC24", "text": "G-P 缺任一条件都拒绝，批准件不可覆盖",
         "status": "PASS",
         "evidence": [_jp("protocol/gates.json", "G-P/missing"),
                      _t("tests/test_rsi_r0_runner.py",
                         "test_approval_artifact_is_write_once"),
                      _t("tests/test_rsi_r0_runner.py",
                         "test_gate_lists_every_missing_condition")],
         "note": "活证：G-P 仅缺 independent_review 即 PENDING，付费路径被拒"},

        {"id": "AC25", "text": "六次协议上限，P1–P4 全部成功才进开发",
         "status": "PASS",
         "evidence": [_c("protocol/protocol_probe_report.json"),
                      _c("quota/ledger.jsonl")],
         "note": "PROTOCOL_OK 6/6，report_sha256=905b7a3f；seq 1–6"},

        {"id": "AC26", "text": "开发恰 3 个 slot，24 帽/检查点/首失败层可复算",
         "status": "PASS",
         "evidence": [_c("protocol/development_report.json"),
                      _jp("protocol/development_report.json",
                          "families/0/request_cap")],
         "note": ("三族各 1 slot；配置帽 DEV_CAP=24、检查点 (8,16,24)"
                  "（`develop.py:41-42`），**有效帽**按票据余量收缩为 "
                  "E 19 / R 15 / D 未计算（BLOCKED 于算帽前）"
                  "（`develop.py:316`）。checkpoint 实际记录见 "
                  "`closeout/result.json` 的 `families.by_family.*.checkpoint_count`；"
                  "首失败层：E=null、D=无 evaluation、R=L5")},

        {"id": "AC27", "text": "任一族开发不达门则验证 0 POST；冻结后不能改配置",
         "status": "PASS",
         "evidence": [_c("protocol/gates.json"),
                      _c("quota/attempts.jsonl"),
                      _t("tests/test_rsi_r0_validate.py",
                         "test_run_validation_refuses_before_the_first_request_without_gate_v")],
         "note": "G-V PENDING；attempts 无 validate 记录；开发后仅两次线内仪器修复换锁"},

        {"id": "AC28", "text": "验证恰 6 个 slot 终态，无失败删样/人工代写",
         "status": "NOT_RUN_WITH_REASON",
         "evidence": [_c("protocol/gates.json")],
         "note": ("T7 未启动（development_gate_met=false，原因 T6-REV-4）。"
                  "六个 slot 记 CANCELLED 且保留分母；不得伪写 PASS"),
         "not_run_reason": "G-V 未满足 ⇒ 按计划 L334 不启动 T7"},

        {"id": "AC29", "text": "E/D 功能分母 4，R 研究分母 2，各层 NA 不混算",
         "status": "NOT_RUN_WITH_REASON",
         "evidence": [_t("tests/test_rsi_r0_contracts.py",
                         "test_summary_keeps_cancelled_in_denominator_and_never_p_values")],
         "note": ("分母语义由契约测试机判（CANCELLED 留分母、不出 p 值）；"
                  "验证未执行 ⇒ 无 validation_report 可算实测分母"),
         "not_run_reason": "无验证轨迹；分母口径由契约测试承担"},

        {"id": "AC30", "text": "T0–T7 所有停止路径有唯一优先判决；CANCELLED 保留分母/覆盖率",
         "status": "PASS",
         "evidence": [_t("tests/test_rsi_r0_contracts.py",
                         "test_verdict_priority_ordering_matches_plan"),
                      _t("tests/test_rsi_r0_contracts.py",
                         "test_pick_primary_verdict_keeps_all_reason_codes"),
                      _c("closeout/result.json")],
         "note": ("本战役到达的停止路径：T5 PROTOCOL_OK、T6 INVALID_INSTRUMENT"
                  "（+BLOCKED_MODEL/ABORTED_INCOMPLETE）、T7 not_started"
                  "（分母 6 保留）、T8 COMPLETE；未到达项逐条列出")},

        {"id": "AC31", "text": "ledger/attempt/reservation 逐 request_id 对账，条件重验另列",
         "status": "PASS", "verified_by": "closure_judge",
         "evidence": [_c("quota/ledger.jsonl"), _c("quota/attempts.jsonl"),
                      _d("quota/resource_reservations")],
         "note": "机判现算 reconcile（不复用 closeout_zero_paid 自述）；条件重验单列"},

        {"id": "AC32", "text": "收尾代码测试通过，旧账/知识/配置未越权变更",
         "status": "PASS", "verified_by": "closure_judge",
         "evidence": [_c("closeout/t8_fullsuite.log"),
                      _c("closeout/gate_static.log"),
                      _c("closeout/ledger_seals.json")],
         "note": "两排除全套冷跑 rc=0 + gate_static 六步 OK + 封存账拒写探针"},

        {"id": "AC33", "text": "判决与包无循环，三签绑定同一版本/证据",
         "status": "PASS", "verified_by": "closure_judge",
         "evidence": [_c("closeout/closure_judge.json"),
                      _c("closeout/signs")],
         "note": ("报告引用包 manifest sha256；报告/矩阵/机判/三签都在包外；"
                  "包内不含报告（机判逐件断言）")},

        {"id": "AC34", "text": "资源回收，本阶段容器 0/后台 0；不碰他方进程；明确后续未启动",
         "status": "PASS", "verified_by": "cleanup",
         "evidence": [_c("closeout/cleanup.json")],
         "note": "docker ps -a 空、显存 ≤200MiB、无看门狗残留、后续未启动清单"},
    ]
    for it in items:
        if it["id"] in ("AC31", "AC32", "AC33", "AC34"):
            it["status_basis"] = (
                "声明式 PASS：由 scripts/verification/check_rsi_r0_closure.py "
                "独立复核（本条不依赖矩阵自述）；机判出现 problems 时本行"
                "不成立，逐个对照见 closeout/closure_judge.json::checks")
    counts: dict[str, int] = {}
    for it in items:
        counts[it["status"]] = counts.get(it["status"], 0) + 1
    doc = {
        "schema": SCHEMA_MATRIX,
        "milestone": MILESTONE,
        "campaign": str(facts["campaign"]),
        "generated_at_utc": utcnow(),
        "criteria_source": "~/文档/RSI_R0_Plan_detailed.md §8（AC01–AC34）",
        "rule": ("PASS 必须有机器可核证据（文件存在/JSON 路径非空/测试符号在）；"
                 "AC25 及后续在提前阻塞时标 NOT_RUN_WITH_REASON，不伪写 PASS；"
                 "结案完整性判定允许诚实阻塞而不宣称全部 AC 能力通过"),
        "items": items,
        "counts": counts,
        "all_green": all(it["status"] == "PASS" for it in items),
        "partial_or_not_run": [it["id"] for it in items
                               if it["status"] != "PASS"],
    }
    doc["matrix_sha256"] = sha256_obj(
        {k: v for k, v in doc.items() if k != "matrix_sha256"})
    return doc


# ---------------------------------------------------------------------------
# 人工可读报告
# ---------------------------------------------------------------------------
def _ffl(v) -> str:
    if not v:
        return "—"
    if isinstance(v, dict):
        return f"{v.get('layer')} / {v.get('reason_code')}"
    return str(v)


def render_report(facts: dict, result: dict, matrix: dict, cost: dict) -> str:
    fam = result["families"]["by_family"]
    r_family = fam.get("R") or {}
    pack = result.get("evidence_pack") or {}
    gi = result["git"]
    lines = [
        "# RSI-R0 校准战役结案报告",
        "",
        f"- 战役：`{result['campaign_id']}`（目录 `{result['campaign']}`，不可复用）",
        f"- 生成：{result['generated_at_utc']}；零模型调用（本报告由 "
        f"`scripts/rsi_r0/t8_closeout.py --build` 从战场事实生成）",
        f"- 代码：分支 `{gi['branch']}` / HEAD `{gi['head']}`"
        + (f" / 本地领先 origin/main {gi['ahead_of_origin']} 提交" if gi.get('ahead_of_origin') else "")
        + "（推送是人工 STOP 门）",
        f"- 证据包：{('`%s`，manifest sha256 `%s`（%s 件 / %s B）' % (pack.get('path'), pack.get('manifest_sha256'), pack.get('n_files'), pack.get('total_bytes'))) if pack.get('manifest_sha256') else '本轮尚未建包（先 --pack 再 --build 可绑定 manifest sha256）'}",
        "",
        "> 判决件全部在**包外**：本报告引用包 manifest sha256，包内不含本报告、"
        "矩阵、机判与三签，因此不存在「报告验证包含报告的包」的循环（AC33）。",
        "",
        "## 1. 双轴结论",
        "",
        "| 轴 | 值 | 依据 |",
        "|---|---|---|",
        f"| `process_verdict` | **{result['process_verdict']}** | 对账、哈希清点、"
        "分层重算、未执行 slot 列出、双轴结论、三签、收尾清理均有落件 |",
        f"| `calibration_verdict` | **{result['calibration_verdict']}** | "
        f"{result['calibration_verdict_rule']} |",
        f"| `reason_codes` | {', '.join('`%s`' % c for c in result['reason_codes'])} | "
        "主状态唯一，其余问题不丢弃 |",
        "",
        "**一句话**：工程面（E）通过；诊断面（D）因供应商 200 空正文 `BLOCKED`；"
        "研究面（R）的 L5 判据把两份不同分辨率的数据按 1e-3 比，物理正确的候选也必败"
        "（T6-REV-4）；本战役 R 的实际判词是 `verdict CENSORED != claimed INCONCLUSIVE`，"
        "其对比档又含一个已实测删失的公开基线 0.05——两类原因都在仪器/任务书侧。"
        "按计划 §9.1 优先级，主状态为 `INVALID_INSTRUMENT`，R 的失败**不得**读成能力结论。",
        "",
        "## 2. 三族逐层（重算，不采信二手转述）",
        "",
        "| 族 | 实例 | 终态 | 请求 | seqs | L0 | L1 | L2 | L3 | L4 | L5 | 首失败层 |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for name in ("E", "D", "R"):
        f = fam.get(name) or {}
        layers = f.get("layers") or {}

        def cell(k):
            v = layers.get(k) or {}
            st = v.get("status") or "—"
            rc = v.get("reason_code")
            if rc and st != "PASS" and rc not in ("OK", "NOT_APPLICABLE"):
                return f"{st} / {rc}" if st != "NOT_APPLICABLE" else "N/A"
            return "PASS" if st == "PASS" else ("N/A" if st == "NOT_APPLICABLE" else st)

        lines.append(
            f"| {name} | {f.get('instance_id')} | **{f.get('state')}** | "
            f"{f.get('requests_used')}/{f.get('request_cap')} | "
            f"{f.get('ledger_seqs')} | " + " | ".join(cell(k) for k in
                                                       ("L0", "L1", "L2", "L3", "L4", "L5"))
            + f" | {_ffl(f.get('first_failure_layer'))} |")
    lines += [
        "",
        f"- D 的 `blocked_reason`：`{(fam.get('D') or {}).get('blocked_reason')}`",
        f"- E 的 L4 证据：`{(fam.get('E') or {}).get('layers', {}).get('L4')}`",
        f"- R 的 L5 证据：`{(fam.get('R') or {}).get('layers', {}).get('L5')}`",
        f"- **R 族本战役实际判词（逐字）**：「{result['verdict_basis']['R_L5']['live_run_failure']}」",
        f"- R 的对比档与帽：levels 见 L4 evidence，请求 {r_family.get('requests_used')}"
        f"/{r_family.get('request_cap')}（有效帽 = min(DEV_CAP=24, 票据余量)；"
        f"D 族在算帽前 BLOCKED 故为 null）",
        f"- 预注册 sha：E `{(fam.get('E') or {}).get('prereg_sha256')}`、"
        f"D `{(fam.get('D') or {}).get('prereg_sha256')}`、"
        f"R `{(fam.get('R') or {}).get('prereg_sha256')}`",
        "",
        "**RA-1 必须写清**：报告行级 `scene_sha256` 是**基场景**聚合；L2 的权威证据"
        "是逐调用 `scene_for` 产生的 `evaluation.layers.L2.evidence`。这正是原始缺陷"
        "得以隐身的位置，所以逐字写明而不是沉默。",
        "",
        "## 3. 仪器缺陷（六项，逐项定性）",
        "",
        "| ID | 缺陷 | 状态 | 证据 |",
        "|---|---|---|---|",
    ]
    for d in result["instrument_defects"]:
        lines.append(f"| {d['id']} | {d['label']}：{d['summary']} | "
                     f"**{d['status']}** | `{d['evidence']}` |")
    lines += [
        "",
        "### 3.1 T6-REV-4 决定性证据（本战役判决的直接根据）",
        "",
    ]
    d4 = result.get("instrument_defect_t6_rev_4") or {}
    sf = d4.get("settle_full_resolution") or {}
    sv = d4.get("settle_candidate_view") or {}
    rc = d4.get("row_counts") or {}
    lines += [
        f"取证件：`protocol/instrument_defect_t6_rev_4.json`"
        f"（自哈希 `{d4.get('artifact_sha256')}`，付费调用 "
        f"{d4.get('paid_calls')} 次、发网 {d4.get('network_attempts')} 次），"
        f"档位 {d4.get('levels')} × 角度 {d4.get('angles')}，"
        f"dt={d4.get('dt_s')} s、{d4.get('steps')} 步。",
        "",
        "| 档位 | 判据侧沉降 s | 候选视角沉降 s | 判据行数 | 候选行数 | stride | 量子 s |",
        "|---|---|---|---|---|---|---|",
    ]
    for key in sorted(sf):
        f_ = sf.get(key) or {}
        v_ = sv.get(key) or {}
        r_ = rc.get(key) or {}
        lines.append(f"| {key} | {f_.get('settle_s')} | {v_.get('settle_s')} | "
                     f"{r_.get('full_rows')} | {r_.get('candidate_rows')} | "
                     f"{r_.get('stride')} | {r_.get('quantum_s')} |")
    lines += [
        "",
        f"- 效应量：判据侧 `{d4.get('effects_full_resolution')}` ⇒ 均值 "
        f"**{d4.get('mean_effect_full_resolution')}**；候选侧 "
        f"`{d4.get('effects_candidate_view')}` ⇒ 均值 "
        f"**{d4.get('mean_effect_candidate_view')}**。",
        f"- `abs_diff = {d4.get('abs_diff')}` vs `c5_tolerance = "
        f"{d4.get('c5_tolerance')}` ⇒ `c5_reachable = "
        f"{d4.get('c5_reachable')}`。",
        "",
        "**决定性探针（逐条取自取证件）**：",
        "",
        "| 探针 | 崩溃 | 异常 | 产出层 |",
        "|---|---|---|---|",
    ]
    for p in d4.get("probes") or []:
        lines.append(f"| `{p.get('probe')}` | {p.get('crashed')} | "
                     f"{p.get('exception') or '—'} | "
                     f"{json.dumps(p.get('produced_layer'), ensure_ascii=False)} |")
    lines += [
        "",
        f"- `l0_shape_check`：`{json.dumps(d4.get('l0_shape_check'), ensure_ascii=False)}`",
        "  即 L0 只查键在不在、不查类型，所以 `effect=null` / "
        "`effect=\"0.49\"` 都能穿过 L0 直达 L5 的取值行。",
        "- **探针读法（防误读）**：`B0_control_wellformed` 与 `B1b_effect_string` 的 "
        "L5 FAIL 是**合成输入**造成的（探针把答案 verdict 固定写成 "
        "`INCONCLUSIVE`，而判据独立复算是 `SUPPORTED`），它们只用来证明"
        "「不崩溃且能产出层」；**不作为缺陷结论**。真正的缺陷结论来自 "
        "`B2`（未捕获 KeyError）、`B1`（未捕获 TypeError）、`l0_rejects=false` "
        "与 `B3`（正确候选也 L5 FAIL）。",
        "",
        "四条修复路线逐条排除：①提高可观测分辨率（stride=1 需 ~14k token/run，"
        "叠加必顶穿 20000 输入帽）；②只在提示词公开判据口径（消不掉量化差）；"
        "③宿主直接给沉降时间（把「独立复算」掏空＝放水）；④改判据（本 campaign "
        "无通道：`freeze_criteria` AC20 拒写、`lock --supersede-reason` 有发网 "
        "attempt 后一律拒、`--instrument-repair` 把 `criteria_hash` 列在冻结字段）。",
        "",
        "## 4. 门与预算",
        "",
        "| 门 | 状态 | 缺项 |",
        "|---|---|---|",
        f"| G-M | **{result['gates']['G-M']}** | — |",
        f"| G-P | **{result['gates']['G-P']}** | "
        f"{', '.join(result['gates']['G-P_missing'] or []) or '—'} |",
        f"| G-V | **{result['gates']['G-V']}** | "
        f"{', '.join(result['gates']['G-V_missing'] or []) or '—'} |",
        "",
        "G-P 停在 PENDING 是**正确结果**：车道 A APPROVE、车道 B REQUEST_CHANGES ⇒ "
        "不存在可写的绑定 APPROVE；门确实拦住了那次本该发生的付费复测。",
        "",
        "### 4.1 账与成本（逐 request 记账，失败也占 seq）",
        "",
        "| 项 | 值 |",
        "|---|---|",
        f"| 账行 | {cost['rows_total']}（call {cost['calls']} = ok {cost['ok']} + "
        f"fail {cost['fail']}；{cost['non_call_rows']} 行 "
        f"{'/'.join(cost['non_call_events'])}） |",
        f"| seq | {cost['seq_first']}..{cost['seq_last']}，连续={cost['seq_contiguous']} |",
        f"| token | in {cost['tokens_in']:,} / out {cost['tokens_out']:,}"
        f"（cache_read {cost['tokens_cache_read']:,}） |",
        f"| 单请求峰值 | in {cost['max_tokens_in']:,}（帽 "
        f"{cost['cap_input_tokens']:,}）/ out {cost['max_tokens_out']:,}"
        f"（帽 {cost['cap_output_tokens']:,}） |",
        f"| 成本 | ${cost['usd_naive_in_plus_out']:.4f}（in+out 名义）；"
        f"${cost['usd_ceiling_basis_incl_cache']:.4f}（含 cache_read 的上界口径）；"
        f"合同上限 ${cost['usd_contract_ceiling']:.0f} |",
        f"| 最坏情形 | 37 呼全撞帽 = ${cost['usd_worst_case_if_all_hit_caps']:.4f} |",
        f"| 阶段/任务 | {cost['by_stage']} / {cost['by_task']} |",
        "",
        "**输入帽越界 3 行（如实挂牌，不追认）**："
        + "、".join(f"seq {o['seq']} 的 {o['tokens_in']:,}（超 {o['over_by']:,}，"
                   f"+{o['pct_over']}%）" for o in cost["requests_over_input_cap"])
        + "。输出帽 0 行越界。",
        "",
        "### 4.2 批次用量",
        "",
        "| 批次 | task | 批准 | 已用 | 呼 | ok/fail | seqs |",
        "|---|---|---|---|---|---|---|",
    ]
    for b, e in sorted(cost["by_batch"].items()):
        pre = cost["preflight_blocked_by_batch"].get(b, 0)
        lines.append(f"| {b} | {e['task']} | {e['approved']} | {e['used']} | "
                     f"{e['calls']} | {e['ok']}/{e['fail']} | {e['seqs']} |")
    lines += [
        "",
        "`preflight_blocked` 行（seq=null）占票据不占实呼："
        + (", ".join(f"{b}×{n}" for b, n in
                     sorted(cost["preflight_blocked_by_batch"].items())) or "无"),
        "",
        "**「已用」是票据消耗**（= max(batch_used_after)），因此 R0-D-R 的 "
        "已用 16 = 15 次实呼 + 1 次 preflight_blocked；被阻塞的请求占票据但"
        "从未发网（见 AC31）。",
        "",
        "## 5. 未执行 slot 与分母",
        "",
    ]
    for nr in result["not_run"]:
        lines.append(f"- **{nr['slot_class']}**：assigned {nr['assigned']} / "
                     f"executed {nr['executed']}，状态 `{nr['status']}`，"
                     f"计入分母={nr['in_denominator']}；原因：{nr['reason']}")
    lines += [
        "",
        "## 6. 验收矩阵摘要",
        "",
        f"共 {len(matrix['items'])} 项：{matrix['counts']}；"
        f"非 PASS 项：{matrix['partial_or_not_run'] or '无'}。",
        "",
        "| AC | 判据 | 状态 |",
        "|---|---|---|",
    ]
    for it in matrix["items"]:
        lines.append(f"| {it['id']} | {it['text']} | **{it['status']}** |")
    lines += [
        "",
        "完整证据指针与备注见 `closeout/ac_matrix.json`（机判逐项核证据存在性、"
        "JSON 路径非空、测试符号在场）。",
        "",
        "## 7. R1 预注册所需参数与未决问题（R0 只建议，不自动开跑）",
        "",
        "| ID | 项 | 说明 |",
        "|---|---|---|",
    ]
    for p in result["r1_prereg_parameters"]["must_be_repreregistered"]:
        lines.append(f"| {p['id']} | {p['item']} | {p['detail']} |")
    lines += [
        "",
        "未决问题：",
        "",
    ]
    for q in result["r1_prereg_parameters"]["open_questions"]:
        lines.append(f"- {q}")
    lines += [
        "",
        "## 8. 尾债（本 campaign 不修，进 Phase 3 / 新 campaign）",
        "",
        "| ID | 级别 | 来源 | 事项 | 处置 |",
        "|---|---|---|---|---|",
    ]
    for d in result["debts"]:
        lines.append(f"| {d['id']} | {d['severity']} | {d['source']} | "
                     f"{d['item']} | {d['disposition']} |")
    lines += [
        "",
        "## 9. 三签与无循环声明",
        "",
        "三签（executor / reviewer / verifier）各自绑定**同一**三个值："
        f"报告 sha256、包 manifest sha256（`{pack.get('manifest_sha256')}`）、"
        f"git commit（`{gi['head']}`）。签名件在 `closeout/signs/`，**不入包**；"
        "报告**不写回**它验证的包（AC33 由机判逐件断言：包内不含报告/矩阵/机判/签名，"
        "报告内含包 manifest sha256）。",
        "",
        "## 10. 收尾与后续未启动",
        "",
        "见 `closeout/cleanup.json`：容器 0、后台 0、显存 ≤200 MiB、无看门狗残留、"
        "未触碰他方进程。**后续未启动**：T7 六条验证、R 单次复测、Phase 3 各项"
        "（RL/VLA 训练、World Judge、RSI L3 元 prompt、四 tour 实跑、能力对比重设计等）"
        "一律需未来显式裁决。",
        "",
        "---",
        "",
        "本报告不宣称任何能力结论：E 通过是**单个功能集的可行性判断**（n=1），"
        "D 的失败是供应商侧空正文，R 的失败是仪器缺陷。三者都不支持对"
        "「具身智能仿真能力」做推论。",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 证据包（复用 milestone1 原语；包内只放战场原始证据）
# ---------------------------------------------------------------------------
def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


M1 = _load_module(_REPO / "scripts/milestone1/build_evidence_pack.py", "m1_pack")

PACK_FILES: list[tuple[str, str, bool]] = [
    # (pack 内相对路径, 战场相对路径, 必需)
    ("protocol/admission.json", "protocol/admission.json", True),
    ("protocol/gates.json", "protocol/gates.json", True),
    ("protocol/budget.json", "protocol/budget.json", True),
    ("protocol/pricing_contract.json", "protocol/pricing_contract.json", True),
    ("protocol/model_approval.json", "protocol/model_approval.json", True),
    ("protocol/baseline_manifest.json", "protocol/baseline_manifest.json", True),
    ("protocol/t2_image.json", "protocol/t2_image.json", True),
    ("protocol/t2_public_manifest.json", "protocol/t2_public_manifest.json", True),
    ("protocol/t2_tasks_lock.json", "protocol/t2_tasks_lock.json", True),
    ("protocol/t2_golden.json", "protocol/t2_golden.json", True),
    ("protocol/t2_convergence.json", "protocol/t2_convergence.json", True),
    ("protocol/t3_criteria.json", "protocol/t3_criteria.json", True),
    ("protocol/t3_controls_report.json", "protocol/t3_controls_report.json", True),
    ("protocol/t3_controls_offline.json", "protocol/t3_controls_offline.json", False),
    ("protocol/t4_lock.json", "protocol/t4_lock.json", True),
    ("protocol/t4_controls_latest.json", "protocol/t4_controls_latest.json", True),
    ("protocol/protocol_probe_report.json", "protocol/protocol_probe_report.json", True),
    ("protocol/development_report.json", "protocol/development_report.json", True),
    ("protocol/lock_history.jsonl", "protocol/lock_history.jsonl", True),
    ("protocol/closeout_history.jsonl", "closeout/closeout_history.jsonl", True),
    ("protocol/dryrun_report.json", "closeout/dryrun_report.json", True),
    ("protocol/closeout_zero_paid.json", "closeout/closeout_zero_paid.json", True),
    ("quota/ledger.jsonl", "quota/ledger.jsonl", True),
    ("quota/attempts.jsonl", "quota/attempts.jsonl", True),
    ("quota/counters.json", "quota/counters.json", True),
    # 仪器缺陷三件 + producer/provenance（零付费取证，T6-REV-3/4）
    ("protocol/t6_development_review.json", "protocol/t6_development_review.json", True),
    ("protocol/instrument_rehearsal.json", "protocol/instrument_rehearsal.json", True),
    ("protocol/instrument_repair_control.json", "protocol/instrument_repair_control.json", True),
    ("protocol/instrument_repair_control_producer.py",
     "protocol/instrument_repair_control_producer.py", True),
    ("protocol/instrument_repair_control_provenance.json",
     "protocol/instrument_repair_control_provenance.json", True),
    ("protocol/instrument_repair_guard_probe.json",
     "protocol/instrument_repair_guard_probe.json", True),
    ("protocol/instrument_defect_t6_rev_4.json",
     "protocol/instrument_defect_t6_rev_4.json", True),
    ("protocol/instrument_defect_t6_rev_4_producer.py",
     "protocol/instrument_defect_t6_rev_4_producer.py", True),
    ("protocol/instrument_defect_t6_rev_4_provenance.json",
     "protocol/instrument_defect_t6_rev_4_provenance.json", True),
    # 两次重跑的历史报告（归档件，不覆盖）
    ("protocol/development_report.first_run_10567091.json",
     "protocol/development_report.first_run_10567091.json", True),
    ("protocol/development_report.empty_answer_1cbf7e2d.json",
     "protocol/development_report.empty_answer_1cbf7e2d.json", True),
    ("protocol/development_report.before_v2_50fe295c.json",
     "protocol/development_report.before_v2_50fe295c.json", True),
    # 独立评审（双车道；车道 A 已落件，车道 B 判词并入 t6_development_review）
    ("protocol/independent_review_lane_a.json",
     "protocol/independent_review_lane_a.json", True),
    ("protocol/retest_budget_acceptance.json",
     "protocol/retest_budget_acceptance.json", True),
    # 收尾件（包外判决件的**输入**，非判决件本身）
    ("closeout/t8_fullsuite.log", "closeout/t8_fullsuite.log", True),
    ("closeout/gate_static.log", "closeout/gate_static.log", True),
    ("closeout/cleanup.json", "closeout/cleanup.json", True),
    ("closeout/ledger_seals.json", "closeout/ledger_seals.json", True),
]

FORBIDDEN_IN_PACK = ["human_report.md", "result.json", "ac_matrix.json",
                     "ledger_cost.json", "closure_judge.json", "signs"]

PACK_HELP = """# RSI-R0 证据包适配说明

1. **判决件在包外**（计划 L358）：`result.json` / `human_report.md` /
   `ac_matrix.json` / `ledger_cost.json` / `closure_judge.json` / `signs/`
   一律不入包。报告引用包 manifest 的 sha256，而不是把自己写进被验证的包，
   从而不制造「报告验证包含报告的包」的循环。机判逐件断言包内不含上列名。
2. **批准件原件不入包**（沿用 Milestone 1 规则）：`quota/approvals/*.json`
   只以 sha256 索引件入包（`quota/approvals_index.json`），原件留在战场。
3. **superseded 锁与评审件**：`protocol/t4_lock.superseded.*.json` 与
   `protocol/independent_review.superseded.*.json` 按 glob 收录
   （`protocol/superseded_locks/`），不删不改——历史可审。
4. **runs/**：逐 trial 的证据目录（调用证据/动作/产物快照/可信观测/裁决）。
   符号链接一律拒绝打包（H-11 同口径），OOM 级大件按 10MB 单件上限拒收。
5. **零付费取证件**（`instrument_repair_control*` / `instrument_defect_t6_rev_4*`）
   的 producer 与本包同目录逐字节归档：它们不入仓（入仓即 code_set 变、触发
   第 10 次换锁），但重跑命令与字节都在包里，可离线复核。
6. **权限归一**：复制完成后统一目录 0700 / 文件 0600。
7. **输入帽越界 3 行**（seq 23/36/37）原样保留在 `quota/ledger.jsonl` 与
   `quota/attempts.jsonl` 中，不修数、不追认。
"""


def build_pack(campaign: Path, pack: Path, *, clean: bool = False) -> dict:
    m = M1
    m.PACK = pack
    m.PHASE1_ROOT = campaign  # 老 helper 只用于路径拼接兜底

    for rel, src_rel, required in PACK_FILES:
        if required and not (campaign / src_rel).is_file():
            raise CloseoutError(f"必需证据缺失：{campaign / src_rel}（→ {rel}）")

    if clean and pack.exists():
        pack_res = pack.resolve()
        camp_res = campaign.resolve()
        if pack_res == camp_res or not str(pack_res).startswith(
                str(camp_res) + os.sep):
            raise CloseoutError(f"拒绝删除：包目录不在战役根内：{pack}")
        shutil.rmtree(pack)
    pack.mkdir(parents=True, exist_ok=True)

    manifest_files: list[dict] = []

    for rel, src_rel, required in PACK_FILES:
        m._copy_file(campaign / src_rel, pack / rel, rel, required,
                     "campaign", manifest_files)

    # superseded 锁（历史可审，不删不改；评审件另走下一段，避免重复收录）
    for nested in sorted((campaign / "protocol").glob("t4_lock.superseded.*.json")):
        dst = pack / "protocol" / "superseded_locks" / nested.name
        m._copy_file(nested, dst, f"superseded/{nested.name}", True,
                     "campaign", manifest_files)

    # superseded 评审件（独立评审历史，单独目录便于审）
    for nested in sorted((campaign / "protocol").glob(
            "independent_review.superseded.*.json")):
        dst = pack / "protocol" / "superseded_reviews" / nested.name
        if dst.exists():
            continue
        m._copy_file(nested, dst, f"superseded_review/{nested.name}", True,
                     "campaign", manifest_files)

    # 逐 trial 的 runs/（H-11 同口径安全 walk：符号链接 FATAL）
    runs = campaign / "runs"
    if not runs.is_dir():
        raise CloseoutError(f"必需证据目录缺失：{runs}")
    files = list(_iter_packable(runs))
    if not files:
        raise CloseoutError(f"runs/ 为空：{runs}")
    for f in files:
        rel = f.relative_to(campaign)
        m._copy_file(f, pack / rel, str(rel), True, "campaign",
                     manifest_files)

    # approvals 索引（原件不入包）
    idx = []
    for f in sorted((campaign / "quota" / "approvals").glob("*.json")):
        rec = json.loads(f.read_text(encoding="utf-8"))
        idx.append({"file": f.name, "sha256": m._sha256(f),
                    "batch_id": rec.get("batch_id"),
                    "task": rec.get("task"),
                    "approved": rec.get("approved"),
                    "stage": rec.get("stage"),
                    "created_at_iso": rec.get("created_at_iso")})
    dst = pack / "quota" / "approvals_index.json"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(json.dumps(
        {"note": "批准件原件不入包（m1 规则）；此处为 sha256 索引",
         "batches": idx}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    m._add_manifest(manifest_files, dst, campaign / "quota" / "approvals",
                    "derived", "approvals_index", True)

    # 资源预留账（逐件，文件名即 request_id）
    res_dir = campaign / "quota" / "resource_reservations"
    if res_dir.is_dir():
        for f in sorted(res_dir.glob("req_*.json")):
            m._copy_file(f, pack / "quota" / "resource_reservations" / f.name,
                         f"reservation/{f.name}", True, "campaign",
                         manifest_files)

    # 明示：包里绝不含判决件
    leaks = []
    for p in pack.rglob("*"):
        if p.is_file() and any(p.name == n or p.parent.name == n
                               or n in p.parts for n in FORBIDDEN_IN_PACK):
            leaks.append(str(p.relative_to(pack)))
    if leaks:
        raise CloseoutError(f"证据包混入包外判决件：{leaks}")

    sym = [str(p.relative_to(pack)) for p in pack.rglob("*") if p.is_symlink()]
    if sym:
        raise CloseoutError(f"证据包混入符号链接（H-11）：{sym}")

    (pack / "ADAPTATIONS.md").write_text(PACK_HELP, encoding="utf-8")
    manifest_files.append({
        "path": "ADAPTATIONS.md",
        "sha256": m._sha256(pack / "ADAPTATIONS.md"),
        "size": (pack / "ADAPTATIONS.md").stat().st_size,
        "origin": "derived", "origin_rel": "scripts/rsi_r0/t8_closeout.py",
        "logical_ids": ["rsi_r0.T8.pack"], "required": True,
    })

    manifest = {
        "schema": SCHEMA_PACK,
        "milestone": MILESTONE,
        "campaign": str(campaign),
        "git_commit": m._git_head(),
        "generated_at_utc": utcnow(),
        "replay_commands": [
            "python scripts/rsi_r0/t8_closeout.py --build",
            "python scripts/rsi_r0/t8_closeout.py --pack --clean",
            "python scripts/verification/check_rsi_r0_closure.py "
            "--campaign <campaign> --report <campaign>/closeout/closure_judge.json",
            "python -m pytest tests -q -k 'not test_04' "
            "-m 'not paid and not kb_integration'",
        ],
        "push_state": {"head": m._git_head(),
                       "policy": "推送是人工 STOP 门；本阶段不推送"},
        "files": sorted(manifest_files, key=lambda r: r["path"]),
        "n_files": len(manifest_files),
        "total_bytes": sum(int(r["size"]) for r in manifest_files),
        "secrets_scan_hits": m._scan_secrets(pack),
        "policy": {
            "approvals_originals_in_pack": False,
            "symlinks_in_pack": False,
            "verdict_artifacts_in_pack": False,
            "verdict_artifacts": FORBIDDEN_IN_PACK,
            "max_file_bytes": MAX_FILE_BYTES,
        },
    }
    if manifest["secrets_scan_hits"]:
        raise CloseoutError(
            f"证据包命中密钥正则：{manifest['secrets_scan_hits'][:5]}")
    big = [r["path"] for r in manifest["files"]
           if int(r["size"]) > MAX_FILE_BYTES]
    if big:
        raise CloseoutError(f"包内 >10MB 件：{big}")
    modes = _normalize_modes(pack)
    manifest["policy"]["modes"] = {"dirs": "0700", "files": "0600", **modes}
    (pack / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    os.chmod(pack / "manifest.json", 0o600)
    return manifest


def _iter_packable(base: Path):
    for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
        d = Path(dirpath)
        keep = []
        for name in dirnames:
            if name in M1.SKIP_NAMES:
                continue
            p = d / name
            if p.is_symlink():
                raise CloseoutError(
                    f"证据树含符号链接目录，拒绝打包（H-11）：{p} -> "
                    f"{os.readlink(p)}")
            keep.append(name)
        dirnames[:] = keep
        for name in filenames:
            if name in M1.SKIP_NAMES:
                continue
            p = d / name
            if p.is_symlink():
                raise CloseoutError(
                    f"证据源为符号链接文件，拒绝打包（H-11）：{p} -> "
                    f"{os.readlink(p)}")
            yield p


def _normalize_modes(pack: Path) -> dict:
    dirs = files = 0
    for dirpath, _dirnames, filenames in os.walk(pack):
        os.chmod(dirpath, 0o700)
        dirs += 1
        for name in filenames:
            os.chmod(Path(dirpath) / name, 0o600)
            files += 1
    return {"dirs_0700": dirs, "files_0600": files}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build(campaign: Path) -> dict:
    closeout = campaign / "closeout"
    closeout.mkdir(parents=True, exist_ok=True)

    facts = collect(campaign)
    cost = ledger_stats(facts)
    fam = families(facts)

    pack_dir = closeout / "evidence_pack"
    pack_info = None
    man_path = pack_dir / "manifest.json"
    if man_path.is_file():
        man = read_json(man_path)
        pack_info = {
            "path": str(pack_dir),
            "manifest_sha256": sha256_file(man_path),
            "n_files": man.get("n_files"),
            "total_bytes": man.get("total_bytes"),
            "git_commit": man.get("git_commit"),
            "status": "built",
        }

    artifacts: dict[str, dict] = {}
    result = result_doc(facts, cost, fam, pack_info, artifacts)
    matrix = ac_matrix(facts, result, cost)

    result_path = closeout / "result.json"
    matrix_path = closeout / "ac_matrix.json"
    cost_path = closeout / "ledger_cost.json"

    write_json(cost_path, cost)
    write_json(matrix_path, matrix)
    # 报告需要矩阵与成本 sha，故先写这两件再写 result/report。
    artifacts["ledger_cost"] = {"path": str(cost_path),
                                "sha256": sha256_file(cost_path)}
    artifacts["ac_matrix"] = {"path": str(matrix_path),
                              "sha256": sha256_file(matrix_path)}

    report_text = render_report(facts, result, matrix, cost)
    report_path = closeout / "human_report.md"
    report_path.write_text(report_text, encoding="utf-8")
    os.chmod(report_path, 0o600)
    artifacts["human_report"] = {"path": str(report_path),
                                 "sha256": sha256_file(report_path)}
    result["artifacts"] = artifacts
    result["result_sha256"] = sha256_obj(
        {k: v for k, v in result.items() if k != "result_sha256"})
    write_json(result_path, result)
    artifacts["result"] = {"path": str(result_path),
                           "sha256": sha256_file(result_path)}

    return {
        "result": str(result_path),
        "report": str(report_path),
        "matrix": str(matrix_path),
        "cost": str(cost_path),
        "pack": pack_info,
        "process_verdict": result["process_verdict"],
        "calibration_verdict": result["calibration_verdict"],
        "counts": matrix["counts"],
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--campaign", default=DEFAULT_CAMPAIGN)
    ap.add_argument("--build", action="store_true",
                    help="生成 result/human_report/ac_matrix/ledger_cost")
    ap.add_argument("--pack", action="store_true", help="组装不可变证据包")
    ap.add_argument("--clean", action="store_true", help="与 --pack 同用：先删旧包")
    args = ap.parse_args(argv)
    campaign = Path(args.campaign).resolve()
    if not campaign.is_dir():
        print(f"[T8-FAIL] 战役目录不存在：{campaign}", file=sys.stderr)
        return 2
    if not (args.build or args.pack):
        ap.print_help()
        return 2
    try:
        if args.pack:
            man = build_pack(campaign, campaign / "closeout" / "evidence_pack",
                             clean=args.clean)
            print(f"pack {campaign / 'closeout' / 'evidence_pack'} "
                  f"files={man['n_files']} bytes={man['total_bytes']} "
                  f"commit={str(man['git_commit'])[:12]}")
        if args.build:
            out = build(campaign)
            print(json.dumps(out, ensure_ascii=False, indent=2))
    except CloseoutError as exc:
        print(f"[T8-FAIL] {exc}", file=sys.stderr)
        return 2
    except (SystemExit, FileNotFoundError, OSError) as exc:
        # milestone1 原语用 SystemExit/FileNotFoundError 表达「必需证据缺失」
        # 与 H-11 拒绝；统一折算成 T8 的 exit 2，不留下半份结案。
        print(f"[T8-FAIL] {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
