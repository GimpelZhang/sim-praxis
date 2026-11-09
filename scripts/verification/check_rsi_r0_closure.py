#!/usr/bin/env python3
"""RSI-R0 T8 结案机判（AC31–AC34 + 全矩阵证据指针 + 无循环 + 三签绑定）。

独立重算，不复用结案件自述：
  * AC31 账：直接读 ``quota/ledger.jsonl`` / ``attempts.jsonl`` /
    ``resource_reservations/``，跑 ``reconcile_campaign`` 现算，再与
    ``closeout_zero_paid.json`` 的自述对比（不一致即 problem，不采信自述）。
  * AC32 测试与越权：冷跑/静态门禁日志必须在场且带 rc 标记；封存六账逐一
    过 ``assert_not_sealed_ledger``（**不写打开**），并比对前后 sha256。
  * AC33 无循环：报告必须引用包 manifest sha256；包内不得出现
    结果/报告/矩阵/成本/机判/签名六类判决件；包内每件 sha256 与 manifest 相等；
    manifest 的 git_commit 必须是 HEAD 的祖先或自身。
  * AC34 资源回收：``docker ps -a`` 计数 0、显存 ≤200 MiB、无看门狗残留、
    ``/mnt`` 用量记录；本件同时产出 ``closeout/cleanup.json`` 与
    ``closeout/ledger_seals.json`` 两件被矩阵引用的原始证据。
  * 三签：``closeout/signs/executor.json|reviewer.json|verifier.json`` 各自绑定
    **同一** report_sha256 / pack_manifest_sha256 / git_commit，verdict=APPROVE，
    三者 signer 互异。缺任一即 problem（结案未完成）。

判决：problems 为空 ⇒ rc=0 且 ``verdict=PASS``；否则 rc=1 并逐条列出。
零模型调用、零发网、零容器；只读战场 + 写 ``closeout/``。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))

DEFAULT_CAMPAIGN = "/mnt/artifacts/simpraxis/rsi_r0/r0_20261108T0825Z"
VERDICT_FILES = ("result.json", "human_report.md", "ac_matrix.json",
                 "ledger_cost.json", "closure_judge.json", "signs")


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))


def load_jsonl(p: Path) -> list[dict]:
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines()
            if line.strip()]


class Judge:
    def __init__(self) -> None:
        self.problems: list[dict] = []
        self.checks: list[dict] = []

    def check(self, name: str, ok: bool, detail=None) -> bool:
        self.checks.append({"check": name, "ok": bool(ok), "detail": detail})
        if not ok:
            self.problems.append({"code": name, "detail": detail})
        return bool(ok)

    def problem(self, code: str, detail) -> None:
        self.check(code, False, detail)


# ---------------------------------------------------------------------------
# 指针核验
# ---------------------------------------------------------------------------
def walk_pointer(doc, pointer: str):
    cur = doc
    for part in pointer.split("/"):
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(cur, dict):
            if part not in cur:
                return None
            cur = cur[part]
        else:
            return None
    return cur


def verify_pointer(campaign: Path, ev: dict) -> tuple[bool, str]:
    kind = ev.get("kind")
    rel = ev.get("rel") or ""
    if kind == "campaign_file":
        p = campaign / rel
        return (p.is_file(), f"{rel} exists={p.is_file()}")
    if kind == "campaign_dir":
        p = campaign / rel
        return (p.is_dir(), f"{rel} is_dir={p.is_dir()}")
    if kind == "campaign_json_path":
        p = campaign / rel
        if not p.is_file():
            return False, f"{rel} 缺文件"
        val = walk_pointer(load(p), ev.get("pointer") or "")
        return (val is not None,
                f"{rel}#{ev.get('pointer')} → {json.dumps(val, ensure_ascii=False)[:120]}"
                if val is not None else f"{rel}#{ev.get('pointer')} → null/missing")
    if kind == "repo_file":
        p = _REPO / rel
        if not p.is_file():
            return False, f"{rel} 缺文件"
        sym = ev.get("symbol")
        if not sym:
            return True, f"{rel} exists"
        text = p.read_text(encoding="utf-8", errors="replace")
        hit = re.search(rf"^(async )?def {re.escape(sym)}\b", text, re.M) \
            or re.search(rf"^class {re.escape(sym)}\b", text, re.M)
        return (bool(hit), f"{rel}::{sym} {'在场' if hit else '缺席'}")
    return False, f"未知证据类型 {kind}"


# ---------------------------------------------------------------------------
# AC31 账与 attempt 对账（独立重算）
# ---------------------------------------------------------------------------
def check_ledger(j: Judge, campaign: Path, out_dir: Path) -> dict:
    os.environ.setdefault("SIMPRAXIS_RSI_R0_ROOT", str(campaign.parent))
    os.environ.setdefault("SIMPRAXIS_RSI_R0_CAMPAIGN", campaign.name)

    from orchestrator.rsi_r0.attempts import AttemptJournal, BudgetReservations
    from orchestrator.rsi_r0.contracts import ResourceCaps
    from orchestrator.rsi_r0.reconcile import reconcile_campaign

    ledger_path = campaign / "quota" / "ledger.jsonl"
    rows = load_jsonl(ledger_path)
    calls = [r for r in rows if r.get("event") == "call"]
    seqs = [int(r["seq"]) for r in calls]
    j.check("ledger.seqs_contiguous",
            sorted(seqs) == list(range(1, len(seqs) + 1)),
            {"first": min(seqs), "last": max(seqs), "n": len(seqs)})

    att = load_jsonl(campaign / "quota" / "attempts.jsonl")
    states = {}
    for r in att:
        states[r.get("state")] = states.get(r.get("state"), 0) + 1
    settled_ok = states.get("SETTLED_OK", 0)
    settled_fail = states.get("SETTLED_FAIL", 0)
    settled = settled_ok + settled_fail
    j.check("attempts.settled_matches_ledger_calls", settled == len(calls),
            {"settled": settled, "calls": len(calls)})
    j.check("attempts.ok_fail_match_ledger",
            (settled_ok, settled_fail)
            == (sum(1 for r in calls if r.get("ok") is True),
                sum(1 for r in calls if r.get("ok") is not True)),
            {"attempts_ok": settled_ok, "attempts_fail": settled_fail})

    # 最强的一条：SETTLED 行的 seq 集合必须与账本 call 的 seq 集合**逐位相等**
    settled_seqs = {r["seq"] for r in att
                    if r.get("state", "").startswith("SETTLED")}
    j.check("attempts.settled_seq_set_equals_ledger_call_seqs",
            settled_seqs == set(seqs),
            {"only_in_attempts": sorted(settled_seqs - set(seqs)),
             "only_in_ledger": sorted(set(seqs) - settled_seqs)})

    # POST 事实的**唯一**可靠口径是「已结算」：append-only 日志会在过 preflight
    # 之前先写一行 SENT，且 attempts_http 在该行是待用计数而非事实计数。
    # 本战役 2 个请求正是 PREPARED→SENT→BLOCKED_PREFLIGHT（从未发网），
    # 若按 attempts_http>=1 去重会误报 39 个 POST。
    posts = len({r["request_id"] for r in att
                 if r.get("state", "").startswith("SETTLED")})
    distinct_requests = len({r["request_id"] for r in att})
    blocked_reqs = {r["request_id"] for r in att
                    if r.get("state") == "BLOCKED_PREFLIGHT"}
    j.check("attempts.posts_equals_ledger_calls", posts == len(calls),
            {"settled_requests": posts, "calls": len(calls)})
    j.check("attempts.distinct_requests_equals_calls_plus_blocked",
            distinct_requests == len(calls) + len(blocked_reqs),
            {"distinct_requests": distinct_requests, "calls": len(calls),
             "preflight_blocked": len(blocked_reqs)})
    sent_with_http = {r["request_id"] for r in att
                      if r.get("state") == "SENT"
                      and int(r.get("attempts_http") or 0) >= 1}
    blocked_settled = [r["request_id"] for r in att
                       if r["request_id"] in blocked_reqs
                       and str(r.get("state", "")).startswith("SETTLED")]
    j.check("attempts.preflight_blocked_never_settled", not blocked_settled,
            {"blocked": sorted(blocked_reqs),
             "blocked_but_settled": blocked_settled,
             "sent_rows_with_counter": len(sent_with_http)})
    j.check("attempts.no_unknown_sent",
            "UNKNOWN_SENT" not in states, states)

    res_dir = campaign / "quota" / "resource_reservations"
    if res_dir.is_dir():
        reservations = BudgetReservations(ResourceCaps(), res_dir)
    else:
        # 机判不得改动战场：目录不在场就挂牌，让 BudgetReservations 的
        # ensure_private_dir 不要在结案时凭空造一个空预留账。
        j.problem("reservations.dir_missing", {"dir": str(res_dir)})
        reservations = None
    journal = AttemptJournal(campaign / "quota" / "attempts.jsonl", reservations)
    ok, detail = reconcile_campaign(ledger_path, journal, reservations)
    j.check("reconcile.ok", ok, detail)
    res = reservations.load_all() if reservations is not None else {}
    j.check("reservations.count_matches_requests",
            len(res) == len({r["request_id"] for r in att}),
            {"reservations": len(res),
             "distinct_requests": len({r["request_id"] for r in att})})

    # 不采信 closeout_zero_paid.json 自述：只做交叉对比
    zp_path = campaign / "closeout" / "closeout_zero_paid.json"
    cross = {}
    if zp_path.is_file():
        zp = load(zp_path)
        claimed = (zp.get("attempts") or {})
        cross = {
            "claimed_http_posts": claimed.get("http_posts"),
            "actual_settled_posts": posts,
            "claimed_requests": claimed.get("requests"),
            "actual_distinct_requests": distinct_requests,
            "match": (claimed.get("http_posts") == posts
                      and claimed.get("requests") == distinct_requests),
            "note": ("不一致即不采信自述；本文件是零付费车道自检件，"
                     "其 primary_verdict 描述的是零付费车道，不是 T8 主判决"),
        }
        j.check("zero_paid.self_report_cross_checked", cross["match"], cross)

    facts = {
        "schema": "rsi_r0.ledger_reconcile.v1",
        "generated_at_utc": utcnow(),
        "ledger_rows": len(rows),
        "calls": len(calls),
        "ok": sum(1 for r in calls if r.get("ok") is True),
        "fail": sum(1 for r in calls if r.get("ok") is not True),
        "attempt_states": states,
        "settled_posts": posts,
        "distinct_requests": distinct_requests,
        "preflight_blocked_requests": sorted(blocked_reqs),
        "sent_rows_with_http_counter": len(sent_with_http),
        "reservations": len(res),
        "reconcile_ok": ok,
        "reconcile_detail": detail,
        "self_report_cross_check": cross,
    }
    (out_dir / "ledger_reconcile.json").write_text(
        json.dumps(facts, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(out_dir / "ledger_reconcile.json", 0o600)
    return facts


# ---------------------------------------------------------------------------
# AC32 封存账拒写探针（只读；产出 ledger_seals.json）
# ---------------------------------------------------------------------------
def check_seals(j: Judge, campaign: Path, out_dir: Path) -> dict:
    os.environ.setdefault("SIMPRAXIS_RSI_R0_ROOT", str(campaign.parent))
    os.environ.setdefault("SIMPRAXIS_RSI_R0_CAMPAIGN", campaign.name)

    from orchestrator.common.runtime_paths import (
        SealedLedgerError, assert_not_sealed_ledger, sealed_ledger_paths)

    rows = []
    all_ok = True
    for p in sorted(sealed_ledger_paths()):
        before = sha256_file(p) if p.is_file() else None
        refused = False
        err = None
        try:
            assert_not_sealed_ledger(p)
        except SealedLedgerError as exc:
            refused = True
            err = str(exc)[:180]
        after = sha256_file(p) if p.is_file() else None
        all_ok = all_ok and refused and before == after
        rows.append({"path": str(p), "exists": p.is_file(),
                     "write_open_refused": refused, "guard_message": err,
                     "sha256_before": before, "sha256_after": after,
                     "unchanged": before == after})
    # 本战役新账必须**可写**（否则封存集误伤）
    new_ledger = campaign / "quota" / "ledger.jsonl"
    writable = False
    try:
        assert_not_sealed_ledger(new_ledger)
        writable = True
    except SealedLedgerError:
        writable = False
    all_ok = all_ok and writable

    facts = {
        "schema": "rsi_r0.ledger_seals.v1",
        "generated_at_utc": utcnow(),
        "probe": "assert_not_sealed_ledger（只读判定，绝不写打开）",
        "sealed": rows,
        "campaign_ledger_writable": writable,
        "all_refused_and_unchanged": all_ok,
    }
    (out_dir / "ledger_seals.json").write_text(
        json.dumps(facts, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(out_dir / "ledger_seals.json", 0o600)
    j.check("seals.refused_and_unchanged", all_ok,
            {"n_sealed": len(rows), "campaign_writable": writable})
    return facts


# ---------------------------------------------------------------------------
# AC32 日志在场
# ---------------------------------------------------------------------------
def check_logs(j: Judge, campaign: Path) -> dict:
    out = {}
    for name, must in (("t8_fullsuite.log", "passed"),
                       ("gate_static.log", "OK")):
        p = campaign / "closeout" / name
        if not p.is_file():
            j.problem("logs.missing", {"file": name})
            out[name] = None
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        out[name] = {"path": str(p), "bytes": p.stat().st_size,
                     "sha256": sha256_file(p), "has_marker": must in text,
                     "tail": text.strip().splitlines()[-1][:200]
                     if text.strip() else ""}
        j.check(f"logs.{name}.present_and_marked", must in text, out[name])
    return out


# ---------------------------------------------------------------------------
# AC33 无循环 + 包完整性
# ---------------------------------------------------------------------------
def check_no_circularity(j: Judge, campaign: Path, result: dict,
                         report_text: str) -> dict:
    pack = campaign / "closeout" / "evidence_pack"
    man_path = pack / "manifest.json"
    if not man_path.is_file():
        j.problem("pack.missing_manifest", {"pack": str(pack)})
        return {}
    man = load(man_path)
    pack_sha = sha256_file(man_path)
    facts = {"pack": str(pack), "manifest_sha256": pack_sha,
             "n_files": man.get("n_files"),
             "total_bytes": man.get("total_bytes"),
             "git_commit": man.get("git_commit")}

    j.check("pack.report_cites_manifest_sha", pack_sha in report_text,
            {"manifest_sha256": pack_sha})
    j.check("pack.result_cites_manifest_sha",
            (result.get("evidence_pack") or {}).get("manifest_sha256") == pack_sha,
            (result.get("evidence_pack") or {}).get("manifest_sha256"))

    leaked = []
    for f in man.get("files") or []:
        parts = Path(f["path"]).parts
        if any(p in VERDICT_FILES for p in parts):
            leaked.append(f["path"])
    j.check("pack.contains_no_verdict_artifacts", not leaked, leaked or "none")

    bad_hash = []
    for f in man.get("files") or []:
        p = pack / f["path"]
        if not p.is_file():
            bad_hash.append({"path": f["path"], "why": "missing"})
        elif sha256_file(p) != f["sha256"]:
            bad_hash.append({"path": f["path"], "why": "sha256_drift"})
    j.check("pack.files_match_manifest", not bad_hash, bad_hash[:10] or "all match")

    sym = [str(p.relative_to(pack)) for p in pack.rglob("*") if p.is_symlink()]
    j.check("pack.no_symlinks", not sym, sym or "none")

    commit = man.get("git_commit") or ""
    head = subprocess.run(["git", "-C", str(_REPO), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    anc = subprocess.run(
        ["git", "-C", str(_REPO), "merge-base", "--is-ancestor", commit, "HEAD"],
        capture_output=True, text=True) if commit else None
    is_anc = bool(anc is not None and anc.returncode == 0)
    j.check("pack.commit_is_ancestor_or_head",
            is_anc or commit == head, {"pack_commit": commit, "head": head})

    signs_dir = campaign / "closeout" / "signs"
    outside = {"result.json", "human_report.md", "ac_matrix.json",
               "ledger_cost.json", "closure_judge.json"}
    for name in outside:
        j.check(f"pack.outside.{name}", not (pack / name).exists(),
                str(pack / name))
    j.check("pack.outside.signs_dir", not (pack / "signs").exists(),
            str(pack / "signs"))

    facts.update({"symlinks": sym, "hash_mismatch": bad_hash[:10],
                  "contains_verdict_artifacts": leaked,
                  "commit_is_ancestor_or_head": is_anc})
    return facts


# ---------------------------------------------------------------------------
# AC33 三签
# ---------------------------------------------------------------------------
ROLES = ("executor", "reviewer", "verifier")


def check_signs(j: Judge, campaign: Path, report_sha: str,
                pack_sha: str | None) -> dict:
    signs_dir = campaign / "closeout" / "signs"
    head = subprocess.run(["git", "-C", str(_REPO), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    found = {}
    for role in ROLES:
        p = signs_dir / f"{role}.json"
        if not p.is_file():
            j.problem("signs.missing", {"role": role, "path": str(p)})
            continue
        s = load(p)
        found[role] = {
            "file": str(p), "sha256": sha256_file(p),
            "signer": s.get("signer"),
            "verdict": s.get("verdict"),
            "report_sha256": s.get("report_sha256"),
            "pack_manifest_sha256": s.get("pack_manifest_sha256"),
            "git_commit": s.get("git_commit"),
            "lane": s.get("lane"),
        }
        j.check(f"signs.{role}.binds_report", s.get("report_sha256") == report_sha,
                {"signed": s.get("report_sha256"), "actual": report_sha})
        j.check(f"signs.{role}.binds_pack",
                s.get("pack_manifest_sha256") == pack_sha,
                {"signed": s.get("pack_manifest_sha256"), "actual": pack_sha})
        j.check(f"signs.{role}.binds_commit", s.get("git_commit") == head,
                {"signed": s.get("git_commit"), "head": head})
        j.check(f"signs.{role}.approve", str(s.get("verdict")).upper() == "APPROVE",
                s.get("verdict"))
    signers = [v.get("signer") for v in found.values()]
    j.check("signs.three_distinct_signers",
            len(found) == 3 and len({s for s in signers if s}) == 3, signers)
    rev = found.get("reviewer") or {}
    ver = found.get("verifier") or {}
    lanes = [((rev.get("lane") or {}).get("kind")),
             ((ver.get("lane") or {}).get("kind"))]
    j.check("signs.reviewer_and_verifier_are_independent_lanes",
            lanes == ["independent_readonly", "independent_readonly"],
            lanes)
    return {"role_signs": found, "signers": signers, "lanes": lanes}


# ---------------------------------------------------------------------------
# AC34 清理事实
# ---------------------------------------------------------------------------
def _run(cmd: list[str]) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return p.returncode, (p.stdout + p.stderr).strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, str(exc)


def check_cleanup(j: Judge, campaign: Path, out_dir: Path) -> dict:
    rc_d, out_d = _run(["docker", "ps", "-aq"])
    containers = [x for x in out_d.splitlines() if x.strip()] if rc_d == 0 else []
    rc_g, out_g = _run(["nvidia-smi", "--query-gpu=memory.used",
                        "--format=csv,noheader,nounits"])
    gpu = []
    if rc_g == 0:
        for line in out_g.splitlines():
            try:
                gpu.append(int(line.strip().split()[0]))
            except (ValueError, IndexError):
                pass
    rc_w, out_w = _run(["pgrep", "-af", "rsi_r0_watchdog|watchdog_sop"])
    watchdog = [x for x in out_w.splitlines() if x.strip()] if rc_w == 0 else []
    rc_f, out_f = _run(["df", "-h", "/mnt"])
    mnt = out_f.splitlines()[-1] if out_f else ""

    facts = {
        "schema": "rsi_r0.cleanup.v1",
        "generated_at_utc": utcnow(),
        "campaign": str(campaign),
        "docker": {"rc": rc_d, "containers": containers,
                   "count": len(containers)},
        "gpu_memory_used_mib": gpu,
        "gpu_under_200_mib": all(g <= 200 for g in gpu) if gpu else None,
        "watchdog_processes": watchdog,
        "mnt_df": mnt,
        "other_party_processes_touched": False,
        "not_started": [
            "T7 六条验证 slot", "R 单次复测（8 呼信封）",
            "Phase 3 各项（RL/VLA 训练、World Judge、RSI L3 元 prompt、"
            "四 tour 实跑、能力对比重设计）",
        ],
    }
    (out_dir / "cleanup.json").write_text(
        json.dumps(facts, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(out_dir / "cleanup.json", 0o600)
    j.check("cleanup.containers_zero", len(containers) == 0,
            {"count": len(containers)})
    j.check("cleanup.gpu_le_200_mib", facts["gpu_under_200_mib"] is True,
            {"gpu_mib": gpu})
    j.check("cleanup.no_watchdog_residue", not watchdog, watchdog)
    return facts


# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--campaign", default=DEFAULT_CAMPAIGN)
    ap.add_argument("--report", default=None,
                    help="机判件路径（缺省 <campaign>/closeout/closure_judge.json）")
    args = ap.parse_args(argv)
    campaign = Path(args.campaign).resolve()
    out_dir = campaign / "closeout"
    report_path = Path(args.report) if args.report else out_dir / "closure_judge.json"
    if not out_dir.is_dir():
        print(f"[JUDGE-FAIL] 缺 closeout 目录：{out_dir}", file=sys.stderr)
        return 2

    j = Judge()
    for name in ("result.json", "human_report.md", "ac_matrix.json",
                 "ledger_cost.json"):
        p = out_dir / name
        j.check(f"artifacts.{name}.present", p.is_file(), str(p))

    result = load(out_dir / "result.json") if (out_dir / "result.json").is_file() else {}
    matrix = load(out_dir / "ac_matrix.json") if (out_dir / "ac_matrix.json").is_file() else {}
    report_text = ((out_dir / "human_report.md").read_text(encoding="utf-8")
                   if (out_dir / "human_report.md").is_file() else "")

    # --- 自哈希 ---
    if result:
        claimed = result.get("result_sha256")
        from orchestrator.rsi_r0.trusted import sha256_of
        actual = sha256_of({k: v for k, v in result.items()
                            if k != "result_sha256"})
        j.check("result.self_hash", claimed == actual,
                {"claimed": claimed, "actual": actual})
    if matrix:
        from orchestrator.rsi_r0.trusted import sha256_of
        claimed = matrix.get("matrix_sha256")
        actual = sha256_of({k: v for k, v in matrix.items()
                            if k != "matrix_sha256"})
        j.check("matrix.self_hash", claimed == actual,
                {"claimed": claimed, "actual": actual})

    # --- artifacts 段与磁盘一致 ---
    for key, rec in (result.get("artifacts") or {}).items():
        p = Path(rec.get("path") or "")
        j.check(f"artifacts.{key}.sha256_matches_disk",
                p.is_file() and sha256_file(p) == rec.get("sha256"),
                {"path": str(p)})

    # --- 矩阵证据指针逐项核验 ---
    bad = []
    for it in matrix.get("items") or []:
        for ev in it.get("evidence") or []:
            ok, detail = verify_pointer(campaign, ev)
            if not ok:
                bad.append({"ac": it.get("id"), "evidence": ev, "detail": detail})
    j.check("matrix.every_evidence_pointer_resolves", not bad, bad[:12] or "all ok")

    ids = [it.get("id") for it in matrix.get("items") or []]
    j.check("matrix.ac01_to_ac34_complete",
            ids == [f"AC{i:02d}" for i in range(1, 35)],
            {"n": len(ids), "first": ids[:2], "last": ids[-2:]})
    j.check("matrix.statuses_legal",
            all(it.get("status") in ("PASS", "PARTIAL", "NOT_RUN_WITH_REASON",
                                     "FAIL")
                for it in matrix.get("items") or []),
            matrix.get("counts"))
    j.check("matrix.no_unqualified_pass_for_blocked_ac",
            all(it.get("status") != "PASS"
                for it in matrix.get("items") or []
                if it.get("id") in ("AC10",)),
            {"AC10": next((it.get("status") for it in matrix.get("items") or []
                           if it.get("id") == "AC10"), None)})

    # --- 双轴判决自洽 ---
    j.check("result.process_verdict_known",
            result.get("process_verdict") in ("COMPLETE", "INCOMPLETE"),
            result.get("process_verdict"))
    j.check("result.calibration_verdict_known",
            result.get("calibration_verdict") in (
                "SAFETY_INVALID", "FREEZE_INVALID", "INVALID_INSTRUMENT",
                "BLOCKED_INFRA", "BLOCKED_MODEL", "BLOCKED_COST",
                "ABORTED_INCOMPLETE", "PROTOCOL_BLOCKED", "ENGINEERING_FLOOR",
                "RESEARCH_FLOOR", "CEILING_WARNING", "CALIBRATED_PILOT"),
            result.get("calibration_verdict"))
    j.check("result.reason_codes_complete",
            (result.get("calibration_verdict") == "INVALID_INSTRUMENT"
             and "BLOCKED_MODEL" in (result.get("reason_codes") or [])
             and "ABORTED_INCOMPLETE" in (result.get("reason_codes") or [])),
            {"verdict": result.get("calibration_verdict"),
             "reason_codes": result.get("reason_codes")})
    j.check("result.not_run_keeps_denominator",
            all(nr.get("in_denominator") is True
                for nr in (result.get("not_run") or [])
                if "validation" in str(nr.get("slot_class"))),
            result.get("not_run"))
    j.check("result.report_matches_disk",
            (result.get("artifacts") or {}).get("human_report", {}).get("sha256")
            == sha256_file(out_dir / "human_report.md"),
            "report sha in result.artifacts")

    # --- 事实重算 ---
    ledger_facts = check_ledger(j, campaign, out_dir)
    seals = check_seals(j, campaign, out_dir)
    logs = check_logs(j, campaign)
    pack_facts = check_no_circularity(j, campaign, result, report_text)
    signs = check_signs(j, campaign,
                        (result.get("artifacts") or {}).get("human_report", {})
                        .get("sha256") or "",
                        pack_facts.get("manifest_sha256"))
    cleanup = check_cleanup(j, campaign, out_dir)

    # 报告必须逐字含双轴判决
    j.check("report.states_both_axes",
            str(result.get("calibration_verdict")) in report_text
            and str(result.get("process_verdict")) in report_text,
            {"calibration": result.get("calibration_verdict"),
             "process": result.get("process_verdict")})
    j.check("report.does_not_claim_capability",
            ("不得" in report_text or "不宣称" in report_text),
            "报告含禁止能力推论的措辞")

    # 矩阵里 AC31–AC34 是**声明式** PASS：本机判必须用自己的检查逐条兜住它们，
    # 兜不住就是矩阵夸口（记 problem），而不是让矩阵自证。
    judge_map = {
        "AC31": ["reconcile.ok",
                 "attempts.settled_seq_set_equals_ledger_call_seqs",
                 "attempts.posts_equals_ledger_calls",
                 "attempts.distinct_requests_equals_calls_plus_blocked",
                 "reservations.count_matches_requests"],
        "AC32": ["logs.t8_fullsuite.log.present_and_marked",
                 "logs.gate_static.log.present_and_marked",
                 "seals.refused_and_unchanged"],
        "AC33": ["pack.report_cites_manifest_sha",
                 "pack.contains_no_verdict_artifacts",
                 "pack.files_match_manifest",
                 "pack.no_symlinks",
                 "signs.executor.binds_report",
                 "signs.reviewer.binds_pack",
                 "signs.verifier.binds_commit",
                 "signs.three_distinct_signers",
                 "signs.reviewer_and_verifier_are_independent_lanes"],
        "AC34": ["cleanup.containers_zero", "cleanup.gpu_le_200_mib",
                 "cleanup.no_watchdog_residue"],
    }
    by_code = {c["check"]: c for c in j.checks}
    for ac, needs in judge_map.items():
        missing = [n for n in needs if n not in by_code]
        failed = [n for n in needs if by_code.get(n) and not by_code[n]["ok"]]
        j.check(f"matrix.{ac}_backed_by_judge_checks",
                not missing and not failed,
                {"missing": missing, "failed": failed} if (missing or failed)
                else "all checks true")

    verdict = "PASS" if not j.problems else "FAIL"
    doc = {
        "schema": "rsi_r0.closure_judge.v1",
        "milestone": "rsi_r0",
        "campaign": str(campaign),
        "generated_at_utc": utcnow(),
        "head": subprocess.run(["git", "-C", str(_REPO), "rev-parse", "HEAD"],
                               capture_output=True, text=True).stdout.strip(),
        "verdict": verdict,
        "n_problems": len(j.problems),
        "problems": j.problems,
        "checks": j.checks,
        "facts": {
            "ledger": ledger_facts,
            "seals": {"n_sealed": len(seals.get("sealed") or []),
                      "all_refused_and_unchanged":
                          seals.get("all_refused_and_unchanged")},
            "logs": logs,
            "pack": pack_facts,
            "signs": signs,
            "cleanup": cleanup,
        },
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
    os.chmod(report_path, 0o600)
    print(json.dumps({"verdict": verdict, "problems": len(j.problems),
                      "report": str(report_path),
                      "first_problems": j.problems[:6]},
                     ensure_ascii=False, indent=2))
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
