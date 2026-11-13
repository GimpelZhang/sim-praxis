#!/usr/bin/env python
"""RSI-R1.2 结案独立复算器（T9；纯标准库，刻意不 import 引擎）。

纪律（与 R1 复算器同一套路，数值全为**本文件内独立字面量**）：

* 只读盘面：账本、批准件、协议报告、族文档、结案 readout、证据包；
* 不 import ``orchestrator.*``，不调用模型、不触网、不写 campaign（只写
  ``--report`` 指定的判决件）；
* 逐项**重算**后再与 readout 比对；readout 自述不作为证据；
* rc：0=PASS（problems 为空），1=FAIL（problems 非空），2=输入不可读。

判据面（T9 行「历史 31 + 已关闭轮 + 新实呼 ≤ 族帽」）：

    ROUND_CAP 124   FAMILY_CAP 165   HISTORY_POSTS 31（历史块字面量）

族帽 160 是 2026-11-13 用户裁决（155 → 160）后的口径：历史 31 + 已关闭轮
实花（``r12-paid-20261113a`` 5）+ 新轮 124。**已关闭轮的实花由族文档逐行
重算**（``state=="closed"`` 且非本轮 envelope 的 ``posts_used`` 之和），
不写死 5；族文档的 ``family_cap`` 仍必须等于字面量 160。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

SCHEMA = "simpraxis.rsi_r12.closure_judge.v1"
SCHEMA_VERSION = 1

ROUND_CAP = 124
FAMILY_CAP = 165
HISTORY_POSTS = 31

READOUT_RELPATH = "closeout/r12_readout.json"
PACK_MANIFEST = "evidence_manifest.json"
PACK_SCHEMA = "simpraxis.rsi_r1.evidence_manifest.v1"
MAX_PACK_FILE_BYTES = 10 * 1024 * 1024

#: 当前宿主 prompt 模板逐字节钉死（2026-11-13 D-1 修复后：模板与门单一源）。
TEMPLATE_SHA256 = (
    "9d2b8f5e2866dfe020eab5b2907790148184b0841e818419e209135afb378592")
#: 历史模板 sha（`r12-paid-20261113a` 那轮公开的旧示例；该轮证据按当时字节判）。
TEMPLATE_SHA256_HISTORY = (
    "daf30754f8eb9b0e6b468d6d714c9579b73a2b6afbf668b9b7edafcc3e7d4f63",
    TEMPLATE_SHA256)
#: T4 分析门要求的输出 schema（与宿主模板写的 protocol_analysis.v1 冲突）。
GATE_SCHEMA = "simpraxis.rsi_r1.analysis_output.v1"
#: 宿主 prompt 模板实际写出的 schema（逐字；用于字节级证明「模板↔门」自相矛盾）。
TEMPLATE_WRITES_SCHEMA = "simpraxis.rsi_r1.protocol_analysis.v1"
GATE_REASON = "OUTPUT_SCHEMA_MISMATCH"
#: 2026-11-13 实测：prompt 把候选指向合同自身的 schema 键，该键值 ≠ 门 schema。
CLASS_MISDIRECT = "INSTRUMENT_DEFECT_CONTRACT_KEY_MISDIRECTION"
MISDIRECT_POINTER = "analysis_output.schema"
CLASS_PROTOCOL_VIOLATION = "MODEL_PROTOCOL_VIOLATION"
CONTRACT_VIOLATION_CODES = frozenset({
    "UNKNOWN_KEY", "MISSING_KEY", "WRONG_TYPE", "SCHEMA_MISMATCH",
    "INVALID_VALUE", "PLAN_INVALID", "ACTION_UNKNOWN"})

AXIS_LEGAL = {
    "process": ("COMPLETE", "CLOSEOUT_INCOMPLETE"),
    "measurement": ("VALID", "INVALID_INSTRUMENT", "CONTAMINATED", "INCOMPLETE"),
    "study_execution": ("NOT_STARTED", "COMPLETED", "VALID_FAIL", "BLOCKED",
                        "ASSISTED"),
    "claim": ("NONE", "SUPPORTED", "REFUTED", "INCONCLUSIVE", "CLAIM_INVALID"),
    "r1": ("NOT_MEASURED", "INCOMPLETE_COVERAGE", "NOT_ESTABLISHED",
           "MINIMAL_RESEARCH_LOOP_ESTABLISHED"),
}
STUDIES = ("A", "B")
STUDY_ARTIFACTS = {
    "confirm": "confirm/manifest_{study}.json",
    "judge": "judge/report_{study}.json",
    "replay": "replay/comparison_{study}.json",
}
LANES = ("probe", "practice")


def _sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _read_rows(path: Path):
    rows = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
    except OSError:
        return None
    except json.JSONDecodeError:
        return None
    return rows


# ---------------------------------------------------------------------------
# 复算
# ---------------------------------------------------------------------------

def ledger_facts(rows):
    calls = [r for r in rows if r.get("event", "call") == "call"]
    seqs = [r.get("seq") for r in rows]
    seqs_int = [int(s) for s in seqs if isinstance(s, int)]
    tokens = {"in": 0, "out": 0, "cache_create": 0, "cache_read": 0}
    for r in calls:
        tokens["in"] += int(r.get("tokens_in") or 0)
        tokens["out"] += int(r.get("tokens_out") or 0)
        tokens["cache_create"] += int(r.get("tokens_cache_create") or 0)
        tokens["cache_read"] += int(r.get("tokens_cache_read") or 0)
    batches = {}
    for r in calls:
        bid = str(r.get("batch_id") or "")
        b = batches.setdefault(bid, {"calls": 0, "ok": 0, "failed": 0})
        b["calls"] += 1
        if r.get("ok") is True:
            b["ok"] += 1
        elif r.get("ok") is False:
            b["failed"] += 1
    return {
        "rows": len(rows),
        "calls": len(calls),
        "events": len(rows) - len(calls),
        "ok": sum(1 for r in calls if r.get("ok") is True),
        "failed": sum(1 for r in calls if r.get("ok") is False),
        "unknown": sum(1 for r in calls if r.get("ok") is None),
        "tokens": tokens,
        "seq_contiguous": seqs_int == list(range(1, len(seqs_int) + 1)),
        "seq_max": max(seqs_int) if seqs_int else 0,
        "batches": batches,
    }


def family_facts(campaign_root: Path, envelope_id: str):
    path = campaign_root.parent / "family" / "family.json"
    doc = _read_json(path)
    if not isinstance(doc, dict):
        return {"present": False, "path": str(path)}
    members = doc.get("members") or []
    member = next((m for m in members if m.get("envelope_id") == envelope_id),
                  None)
    closed_rounds = sum(
        int(m.get("posts_used") or 0) for m in members
        if m.get("state") == "closed" and m.get("envelope_id") != envelope_id)
    reserved = sum(int(m.get("posts_reserved") or 0) for m in members
                   if m.get("state") != "closed")
    history = int(((doc.get("history") or {}).get("total_call_rows")) or 0)
    return {
        "present": True,
        "path": str(path),
        "family_cap": int(doc.get("family_cap") or 0),
        "history_posts": history,
        "closed_rounds_posts": closed_rounds,
        "reserved_total": history + closed_rounds + reserved,
        "member": member,
        "member_used": int((member or {}).get("posts_used") or 0),
    }


def instrument_facts(root: Path, lane: str = "probe", repo_root=None):
    report = _read_json(root / "protocol" / f"report_{lane}.json")
    if not isinstance(report, dict):
        return {"available": False, "lane": lane}
    qual = report.get("qualification") or {}
    first = qual.get("first_error") or {}
    rel = str(report.get("attempt_relpath") or "")
    facts = {"available": True, "lane": lane,
             "verdict": report.get("verdict"),
             "first_error_name": first.get("name"),
             "attempt_relpath": rel or None,
             "classification": "NOT_APPLICABLE",
             "posts_this_attempt": ((report.get("posts") or {})
                                    .get("this_attempt"))}
    if report.get("verdict") == "PASS" or not rel:
        return facts
    receipt = _read_json(root / rel / "private" / "receipts" / "A-explore.json")
    if not isinstance(receipt, dict):
        code = str(first.get("code") or "")
        name = str(first.get("name") or "")
        if code in CONTRACT_VIOLATION_CODES or code == "BUDGET_UNREACHABLE":
            facts["classification"] = CLASS_PROTOCOL_VIOLATION
        elif code or name:
            facts["classification"] = "MODEL_OR_OTHER_FAILURE"
        return facts
    reasons = [str(c) for c in (receipt.get("reason_codes") or [])]
    source_sha = str(receipt.get("source_sha256") or "")
    source_present = (root / rel / "designs" / f"{source_sha}.py").is_file()
    facts.update({
        "analysis_source_sha256": source_sha or None,
        "analysis_reason_codes": reasons,
        "source_file_present": source_present,
        "source_is_host_template": bool(
            source_present and source_sha in TEMPLATE_SHA256_HISTORY),
        "gate_schema": receipt.get("output_schema"),
    })
    # 字节级证明：候选源码（== 宿主模板）写出的是旧 schema，且从不写门要的 schema。
    if source_present:
        try:
            text = (root / rel / "designs" / f"{source_sha}.py").read_text(
                encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        facts["source_writes_protocol_schema"] = TEMPLATE_WRITES_SCHEMA in text
        facts["source_writes_gate_schema"] = GATE_SCHEMA in text
        facts["source_reads_contract_key"] = (
            "analysis_output" in text and "schema" in text)
    contract = _read_json(root / "public" / "public_contract.json") or {}
    declared = ((contract.get("analysis_output") or {}).get("schema"))
    facts["contract_analysis_output_schema"] = declared
    prompt_text = ""
    if repo_root is not None:
        proto = Path(repo_root) / "orchestrator" / "rsi_r1" / "protocol.py"
        try:
            prompt_text = proto.read_text(encoding="utf-8", errors="replace")
        except OSError:
            prompt_text = ""
    facts["prompt_points_at_contract_schema"] = bool(
        prompt_text and MISDIRECT_POINTER in prompt_text)
    if (str(first.get("name") or "") == "analyze"
            and GATE_REASON in reasons
            and facts["source_is_host_template"]
            and facts.get("source_writes_protocol_schema")
            and not facts.get("source_writes_gate_schema")):
        facts["classification"] = "INSTRUMENT_DEFECT_TEMPLATE_GATE_CONTRADICTION"
    elif (str(first.get("name") or "") == "analyze"
          and GATE_REASON in reasons
          and declared is not None and declared != GATE_SCHEMA
          and facts.get("source_reads_contract_key")):
        facts["classification"] = CLASS_MISDIRECT
    elif str(first.get("code") or "") in CONTRACT_VIOLATION_CODES \
            or str(first.get("code") or "") == "BUDGET_UNREACHABLE":
        facts["classification"] = CLASS_PROTOCOL_VIOLATION
    else:
        facts["classification"] = "MODEL_OR_OTHER_FAILURE"
    return facts


def studies_started(root: Path) -> bool:
    for study in STUDIES:
        for rel in STUDY_ARTIFACTS.values():
            if (root / rel.format(study=study)).is_file():
                return True
    return False


def check_pack(pack: Path):
    problems = []
    manifest = _read_json(pack / PACK_MANIFEST)
    if not isinstance(manifest, dict):
        return ["pack: 缺 evidence_manifest.json"], {}
    if manifest.get("schema") != PACK_SCHEMA:
        problems.append(f"pack: schema {manifest.get('schema')!r}")
    files = manifest.get("files") or {}
    if not files:
        problems.append("pack: files 为空")
    for rel, want in sorted(files.items()):
        p = pack / rel
        if p.is_symlink():
            problems.append(f"pack: {rel}:symlink")
            continue
        if not p.is_file():
            problems.append(f"pack: {rel}:missing")
            continue
        if p.stat().st_size > MAX_PACK_FILE_BYTES:
            problems.append(f"pack: {rel}:too_large")
        got = _sha_file(p)
        if got != want:
            problems.append(f"pack: {rel}:sha {want} -> {got}")
        if (p.stat().st_mode & 0o777) != 0o600:
            problems.append(f"pack: {rel}:mode={oct(p.stat().st_mode & 0o777)}")
    listed = set(files)
    for dirpath, _dirnames, filenames in os.walk(pack):
        for name in filenames:
            rel = os.path.relpath(os.path.join(dirpath, name), pack)
            if rel == PACK_MANIFEST:
                continue
            if rel not in listed:
                problems.append(f"pack: {rel}:unlisted")
    return problems, {"n_files": len(files),
                      "total_bytes": manifest.get("total_bytes")}


def drift_facts(root: Path, repo_root: "Path | None"):
    """逐文件重算 LOCK 闭包 vs 当前仓库（只读；无 repo 则跳过）。"""
    if repo_root is None:
        return None
    lock = _read_json(root / "contract" / "lock.json")
    if not isinstance(lock, dict):
        return {"available": False}
    closure = lock.get("closure") or {}
    mismatches = []
    checked = 0
    for gname, group in (closure.get("groups") or {}).items():
        for item in (group.get("files") or []):
            rel = str(item.get("path") or "")
            want = item.get("sha256")
            if not rel or not want:
                continue
            checked += 1
            path = repo_root / rel
            got = _sha_file(path) if path.is_file() else None
            if got != want:
                mismatches.append({"group": gname, "path": rel,
                                   "locked": want, "current": got})
    return {"available": True, "files_checked": checked,
            "verify": "CLOSURE_DRIFT" if mismatches else "OK",
            "mismatches": mismatches,
            "lock_closure_sha256": closure.get("closure_sha256")}


def judge(root: Path, pack: "Path | None" = None,
          repo_root: "Path | None" = None):
    problems = []
    recomputed = {}
    readout_path = root / READOUT_RELPATH
    readout = _read_json(readout_path)
    if not isinstance(readout, dict):
        return 2, {"verdict": "UNREADABLE",
                   "problems": [f"缺或不可读 {READOUT_RELPATH}"]}, {}
    if readout.get("schema") != "simpraxis.rsi_r12.closeout.v1":
        problems.append(f"readout.schema={readout.get('schema')!r}")

    envelope = _read_json(root / "contract" / "envelope.json") or {}
    envelope_id = str(envelope.get("envelope_id") or "")

    rows = _read_rows(root / "quota" / "ledger.jsonl")
    if rows is None:
        return 2, {"verdict": "UNREADABLE",
                   "problems": ["缺或不可读 quota/ledger.jsonl"]}, {}
    led = ledger_facts(rows)
    recomputed["ledger"] = led
    if not led["seq_contiguous"]:
        problems.append(f"ledger seq 不连续 (max={led['seq_max']})")
    if led["unknown"]:
        problems.append(f"ledger 有 {led['unknown']} 行 outcome UNKNOWN")
    rd_led = readout.get("ledger") or {}
    for key in ("calls", "ok", "failed", "unknown_outcome", "seq_contiguous"):
        mine = led["unknown"] if key == "unknown_outcome" else led[key]
        if rd_led.get(key) != mine:
            problems.append(f"readout.ledger.{key}={rd_led.get(key)!r} ≠ 重算 {mine!r}")
    if (rd_led.get("tokens") or {}) != led["tokens"]:
        problems.append("readout.ledger.tokens 与重算不符")

    # 批次批准件
    for bid, entry in sorted(led["batches"].items()):
        path = root / "quota" / "approvals" / f"{bid}.json"
        doc = _read_json(path)
        if not isinstance(doc, dict):
            problems.append(f"batch {bid!r} 缺批准件")
            continue
        approved = doc.get("approved")
        if not isinstance(approved, int) or approved < entry["calls"]:
            problems.append(
                f"batch {bid!r} 实呼 {entry['calls']} > 批准 {approved!r}")

    # 协议报告
    proto = {}
    for lane in LANES:
        doc = _read_json(root / "protocol" / f"report_{lane}.json")
        proto[lane] = None if doc is None else {
            "verdict": doc.get("verdict"),
            "batch_id": doc.get("batch_id"),
            "posts": (doc.get("posts") or {}).get("this_attempt"),
            "reason": doc.get("reason"),
        }
    recomputed["protocol"] = proto
    rd_proto = readout.get("protocol") or {}
    for lane in LANES:
        mine = proto[lane]
        theirs = rd_proto.get(lane)
        if (mine is None) != (theirs is None):
            problems.append(f"protocol.{lane} 存在性不符")
            continue
        if mine is None:
            continue
        if mine["verdict"] != theirs.get("verdict"):
            problems.append(f"protocol.{lane}.verdict 不符")
        if mine["batch_id"] != theirs.get("batch_id"):
            problems.append(f"protocol.{lane}.batch_id 不符")
    posts_sum = sum(int((proto[l] or {}).get("posts") or 0) for l in LANES)
    if posts_sum != led["calls"]:
        problems.append(f"协议 posts 合计 {posts_sum} ≠ 账本 call 行 {led['calls']}")

    # 族账
    fam = family_facts(root, envelope_id)
    recomputed["family"] = fam
    if not fam["present"]:
        problems.append("族账缺失（family/family.json）")
    else:
        if fam["family_cap"] != FAMILY_CAP:
            problems.append(f"族帽 {fam['family_cap']} ≠ 字面量 {FAMILY_CAP}")
        if fam["history_posts"] != HISTORY_POSTS:
            problems.append(
                f"历史 {fam['history_posts']} ≠ 字面量 {HISTORY_POSTS}")
        if fam["reserved_total"] > FAMILY_CAP:
            problems.append(f"族预留总量 {fam['reserved_total']} > {FAMILY_CAP}")
        if fam["member"] is None:
            problems.append(f"族账无成员 {envelope_id!r}")
        elif fam["member_used"] != led["calls"]:
            problems.append(
                f"族成员 posts_used={fam['member_used']} ≠ 账本 {led['calls']}")
        total = (fam["history_posts"] + fam["closed_rounds_posts"]
                 + led["calls"])
        recomputed["family_total"] = total
        if total > FAMILY_CAP:
            problems.append(
                f"历史+已关闭轮+本轮 {total} > 族帽 {FAMILY_CAP}")
    if led["calls"] > ROUND_CAP:
        problems.append(f"本轮实呼 {led['calls']} > 轮帽 {ROUND_CAP}")
    rd_tot = readout.get("totals") or {}
    if rd_tot.get("round_calls") != led["calls"]:
        problems.append("readout.totals.round_calls 不符")
    if fam["present"]:
        want_total = (fam["history_posts"] + fam["closed_rounds_posts"]
                      + led["calls"])
        if rd_tot.get("family_total") != want_total:
            problems.append(
                f"readout.totals.family_total={rd_tot.get('family_total')!r} "
                f"≠ 重算 {want_total}")

    # 仪器失败判定面
    ins = instrument_facts(root, repo_root=repo_root)
    recomputed["instrument"] = ins
    rd_ins = readout.get("instrument") or {}
    if ins.get("available"):
        if ins.get("classification") != rd_ins.get("classification"):
            problems.append(
                f"instrument.classification {rd_ins.get('classification')!r} ≠ "
                f"重算 {ins.get('classification')!r}")
        if rd_ins.get("template_sha256") not in (
                None, *TEMPLATE_SHA256_HISTORY):
            problems.append("readout 模板 sha ≠ 字面量钉死值")
        if (rd_ins.get("gate_schema") not in (None, GATE_SCHEMA)):
            problems.append("readout 门 schema ≠ 字面量钉死值")
        if ins.get("classification") == "INSTRUMENT_DEFECT_TEMPLATE_GATE_CONTRADICTION":
            if not ins.get("source_is_host_template"):
                problems.append("仪器判定缺少「候选源码==宿主模板」字节证据")

    # 轴位
    axes = readout.get("axes") or {}
    if set(axes) != set(AXIS_LEGAL):
        problems.append(f"axes 键集不符：{sorted(axes)}")
    for name, value in axes.items():
        legal = AXIS_LEGAL.get(name)
        if legal is not None and value not in legal:
            problems.append(f"axis {name}={value!r} 非法")
    started = studies_started(root)
    recomputed["studies_started"] = started
    if not started:
        if axes.get("study_execution") != "NOT_STARTED":
            problems.append("研究未启动但 study_execution 不是 NOT_STARTED")
        if axes.get("claim") != "NONE":
            problems.append("研究未启动但 claim 不是 NONE")
        if axes.get("r1") != "NOT_MEASURED":
            problems.append("研究未启动但 r1 不是 NOT_MEASURED")
    if ins.get("classification") == "INSTRUMENT_DEFECT_TEMPLATE_GATE_CONTRADICTION":
        if axes.get("measurement") != "INVALID_INSTRUMENT":
            problems.append("仪器缺陷但 measurement 不是 INVALID_INSTRUMENT")
    if ins.get("classification") == CLASS_PROTOCOL_VIOLATION:
        if axes.get("measurement") != "VALID":
            problems.append("合同违背（测量有效）但 measurement 不是 VALID")
        if axes.get("study_execution") != "NOT_STARTED":
            problems.append("资格失败但 study_execution 不是 NOT_STARTED")
    # 过程轴：只认记录矛盾
    contradictions = (
        (not led["seq_contiguous"]) or led["unknown"] > 0
        or (fam["present"] and fam["member"] is not None
            and fam["member_used"] != led["calls"])
        or (fam["present"] and fam["reserved_total"] > FAMILY_CAP)
        or led["calls"] > ROUND_CAP
    )
    want_process = "CLOSEOUT_INCOMPLETE" if contradictions else "COMPLETE"
    if axes.get("process") != want_process:
        problems.append(
            f"process={axes.get('process')!r} ≠ 重算 {want_process!r}")

    # 根态
    if readout.get("root_state") != "CLOSED":
        problems.append(f"root_state={readout.get('root_state')!r}（应 CLOSED）")
    rd_fam = readout.get("family") or {}
    if fam["present"] and rd_fam.get("member_usage_matches_ledger") is not True:
        problems.append("readout 未承认族用量与账本一致（应 True）")

    # 证据件逐件核对
    for rel, item in sorted((readout.get("evidence") or {}).items()):
        p = root / rel
        if not p.is_file():
            problems.append(f"evidence {rel}: missing")
            continue
        if _sha_file(p) != item.get("sha256"):
            problems.append(f"evidence {rel}: sha 不符")
        if p.stat().st_size != item.get("bytes"):
            problems.append(f"evidence {rel}: bytes 不符")

    pack_facts = {}
    if pack is not None:
        pack_problems, pack_facts = check_pack(Path(pack))
        problems.extend(pack_problems)

    drift = drift_facts(root, repo_root)
    if drift is not None:
        recomputed["post_freeze_drift"] = drift
        rd = readout.get("post_freeze_drift") or {}
        if not isinstance(rd, dict) or not rd.get("available"):
            problems.append("readout 未登记 post_freeze_drift（给了 --repo-root 就必须有）")
        elif drift.get("available"):
            if rd.get("verify") != drift["verify"]:
                problems.append(
                    f"post_freeze_drift.verify={rd.get('verify')!r} ≠ "
                    f"重算 {drift['verify']!r}")
            want = sorted(f"{m['path']}" for m in drift["mismatches"])
            got = sorted(str(m.get("path")) for m in (rd.get("mismatches") or []))
            if want != got:
                problems.append(f"post_freeze_drift.mismatches 不符：{got} ≠ {want}")

    verdict = "PASS" if not problems else "FAIL"
    report = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "run_at_utc": _utc_now(),
        "campaign_root": str(root),
        "envelope_id": envelope_id,
        "verdict": verdict,
        "problems": problems,
        "gaps": list(readout.get("gaps") or []),
        "failures": [e.get("classification")
                     for e in (readout.get("failure_chain") or [])],
        "axes": axes,
        "recomputed": recomputed,
        "pack": pack_facts,
        "validator_source_sha256": _sha_file(Path(__file__).resolve()),
    }
    return 0 if not problems else 1, report, recomputed


def _utc_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, help="R1.2 campaign 根")
    parser.add_argument("--report", default=None, help="判决件输出路径")
    parser.add_argument("--verify", default=None, help="同时核对证据包目录")
    parser.add_argument("--repo-root", default=None,
                        help="仓库根（给了就重算 LOCK 闭包与当前源码）")
    args = parser.parse_args(argv)
    root = Path(args.root).expanduser()
    if not root.is_dir():
        print(json.dumps({"verdict": "UNREADABLE",
                          "problems": [f"根不存在：{root}"]}, ensure_ascii=False))
        return 2
    rc, report, _ = judge(root, Path(args.verify) if args.verify else None,
                          Path(args.repo_root) if args.repo_root else None)
    text = json.dumps(report, ensure_ascii=False, indent=1, sort_keys=True)
    if args.report:
        dest = Path(args.report).expanduser()
        dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = dest.with_suffix(dest.suffix + ".tmp")
        tmp.write_text(text + "\n", encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, dest)
    print(text)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
