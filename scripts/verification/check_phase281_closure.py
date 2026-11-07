#!/usr/bin/env python3
"""Phase 2.8.1 终判机判（计划 §13.6，AC-P281-9-01..17）。

**只读**：本模块的全部检查都从数据根与仓库重新计算，不写任何件。判决件的
**唯一写者**是 ``scripts/benchmark/phase281_closeout.py judge``（计划 §13.2），
避免两处写同一件而破坏确定性。

三段：

1. 纯函数体 ``check_*`` —— 每个函数吃「根」或显式入参，返回
   ``(problems, detail)``；``problems`` 为空即该项通过。
2. ``run_judge(root)`` —— 按 :data:`CHECKS` 顺序跑，汇总成一份 dict。
   **不吃钟表时间**，也不读环境变量 ⇒ 同输入重跑逐字节相同（AC-P281-9-10 ②）。
3. ``verify_fixed_point(root)`` —— 写盘**之后**的两条等式：判决件里的
   ``pack_sha256`` 必须等于 ``evidence_pack/pack.sha256`` 的实际内容；判决件
   必须与重新计算的字节完全相等。这两条只能在写完再读时成立，故不在
   ``run_judge`` 内（否则自指）。

AC 覆盖见 :data:`AC_COVERAGE`；模块导入时断言 17 条 AC 一条不缺——漏一条
就等于「有一段判据没人实现」，这种失败要在 import 时炸，不要等读者去数。
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

REPO = Path(__file__).resolve().parents[2]
for _extra in (str(REPO), str(REPO / "scripts" / "benchmark")):
    if _extra not in sys.path:
        sys.path.insert(0, _extra)

from orchestrator.benchmark.phase281 import analysis as ana  # noqa: E402
from orchestrator.benchmark.phase281 import constants  # noqa: E402
from orchestrator.benchmark.phase281 import verdict as vd  # noqa: E402

SCHEMA = "simpraxis.phase281.closure_judge.v1"
AC_IDS = tuple(f"AC-P281-9-{i:02d}" for i in range(1, 18))

PACK_DIRNAME = "evidence_pack"
JUDGE_RELPATH = "closeout/closure_judge.json"
SIGN_ROLES = ("executor", "reviewer", "verifier")
MAX_FILE_BYTES = 10 * 1024 * 1024
GPU_LIMIT_MIB = 200
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")

#: 允许登记为 ``gaps`` 的**预注册不做项**（§13.6 AC-9-12：其余一律 problem）。
#: 两条都由预注册规则本身产生，不是事后决定：
#:   1. ``CONFIRMATORY_NOT_RUN_PILOT_HOLD`` —— §11.5 规则 2 命中 ⇒ 确认性不开跑；
#:   2. ``SEMANTIC_SCORES_ABSENT_NO_CONFIRMATORY`` —— 确认性未跑 ⇒ R1 无输出可盲评。
ALLOWED_GAPS = frozenset({
    "CONFIRMATORY_NOT_RUN_PILOT_HOLD",
    "SEMANTIC_SCORES_ABSENT_NO_CONFIRMATORY",
})

#: 每条 AC 由哪些检查负责（AC-9 的追溯表；漏登记由 import 期断言拦住）
AC_COVERAGE = {
    "AC-P281-9-01": ("final_report",),
    "AC-P281-9-02": ("final_report",),
    "AC-P281-9-03": ("final_report",),
    "AC-P281-9-04": ("final_report",),          # 敏感性由测试用例承担，见 paper trail
    "AC-P281-9-05": ("final_report",),          # 互斥穷尽由 verdict 单测承担
    "AC-P281-9-06": ("report_text",),
    "AC-P281-9-07": ("report_text",),
    "AC-P281-9-08": ("pack",),
    "AC-P281-9-09": ("judgment_not_packed",),
    "AC-P281-9-10": ("pack", "verify_fixed_point"),
    "AC-P281-9-11": ("signs",),
    "AC-P281-9-12": ("gaps",),
    "AC-P281-9-13": ("cold_run",),
    "AC-P281-9-14": ("ledger",),
    "AC-P281-9-15": ("tables",),
    "AC-P281-9-16": ("old_ledgers",),
    "AC-P281-9-17": ("environment",),
}

CHECKS = ("ac_index", "final_report", "report_text", "pack",
          "judgment_not_packed", "signs", "cold_run", "acceptance", "ledger",
          "tables", "old_ledgers", "environment", "gaps")

_missing = set(AC_IDS) - set(AC_COVERAGE)
_unknown = set(AC_COVERAGE) - set(AC_IDS)
if _missing or _unknown:
    raise RuntimeError(f"AC 覆盖表与 AC_IDS 不一致：缺 {sorted(_missing)}、"
                       f"多 {sorted(_unknown)}")


class JudgeError(RuntimeError):
    """机判自身无法完成（缺件/坏件）；由 ``run_judge`` 记成 problem，不抛出上游。"""


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def _json(path: Path) -> "dict | None":
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _git(*args: str, cwd: "Path | None" = None) -> str:
    try:
        return subprocess.run(["git", *args], cwd=str(cwd or REPO),
                              capture_output=True, text=True, timeout=60,
                              check=False).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def pack_dir(root: Path) -> Path:
    return Path(root) / PACK_DIRNAME


def pack_sha256(root: Path) -> "str | None":
    """``evidence_pack/pack.sha256`` 的**实际内容**（缺件返回 ``None``）。"""
    path = pack_dir(root) / "pack.sha256"
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


# ---------------------------------------------------------------------------
# AC-9-01..05 —— 终局报告
# ---------------------------------------------------------------------------

def check_ac_index(root: Path) -> "tuple[list[str], dict]":
    """AC-9-12 的前置：验收矩阵必须逐条覆盖 AC-P281-9-01..17。"""
    path = Path(root) / "closeout" / "acceptance_matrix.json"
    doc = _json(path)
    if doc is None:
        return [f"缺验收矩阵：{path}"], {"path": str(path), "present": False}
    rows = doc.get("rows")
    if not isinstance(rows, list):
        return ["验收矩阵缺 rows 列表"], {"path": str(path), "present": True}
    ids = [str(r.get("ac") or "") for r in rows if isinstance(r, Mapping)]
    problems: "list[str]" = []
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        problems.append(f"验收矩阵 AC 重复：{dupes}")
    missing = sorted(set(AC_IDS) - set(ids))
    unknown = sorted(set(ids) - set(AC_IDS))
    if missing:
        problems.append(f"验收矩阵缺 AC：{missing}")
    if unknown:
        problems.append(f"验收矩阵含未知 AC：{unknown}")
    failed = sorted(str(r.get("ac")) for r in rows
                    if isinstance(r, Mapping) and not r.get("ok"))
    if failed:
        problems.append(f"验收矩阵未通过的 AC：{failed}")
    return problems, {"path": str(path), "rows": len(rows), "failed": failed}


def _expected_trial_ids() -> "list[str]":
    return [constants.trial_id(t, a, r) for t in constants.TASK_IDS
            for a in constants.ARMS for r in range(constants.REPS_TOTAL)]


def check_final_report(root: Path) -> "tuple[list[str], dict]":
    """AC-9-01（18 Trial 无遗漏无重复）/ 02（三态自洽）/ 03（可复算）。"""
    path = Path(root) / "analysis" / "final_report.json"
    doc = _json(path)
    if doc is None:
        return [f"缺终局报告：{path}"], {"path": str(path), "present": False}
    problems: "list[str]" = []
    detail: "dict[str, Any]" = {"path": str(path), "present": True}

    # AC-9-01：字段集、18 个 Trial、无遗漏无重复
    if frozenset(doc) != ana.FINAL_REPORT_FIELDS:
        problems.append(f"final_report 字段集漂移："
                        f"{sorted(frozenset(doc) ^ ana.FINAL_REPORT_FIELDS)}")
    rows = doc.get("trials") or []
    ids = [str(r.get("trial_id")) for r in rows if isinstance(r, Mapping)]
    expected = _expected_trial_ids()
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        problems.append(f"Trial 重复：{dupes}")
    missing = sorted(set(expected) - set(ids))
    unknown = sorted(set(ids) - set(expected))
    if missing:
        problems.append(f"缺 Trial：{missing}")
    if unknown:
        problems.append(f"未知 Trial：{unknown}")
    if len(rows) != ana.PRE_REGISTERED_TRIALS:
        problems.append(f"Trial 行数 {len(rows)} ≠ 预注册 "
                        f"{ana.PRE_REGISTERED_TRIALS}")
    detail["trials"] = len(rows)

    for row in rows:
        if not isinstance(row, Mapping):
            problems.append("trials 含非映射行")
            continue
        tv = row.get("verdict")
        if row.get("present") and tv not in vd.TRIAL_VERDICTS:
            problems.append(f"{row.get('trial_id')} 的判决 {tv!r} 不在值域")
        if not row.get("present") and tv is not None:
            problems.append(f"{row.get('trial_id')} 不在场却带判决 {tv!r}")
        fc = row.get("failure_class")
        if row.get("present") and fc not in vd.FAILURE_CLASSES:
            problems.append(f"{row.get('trial_id')} 失败类 {fc!r} 不在值域")

    # AC-9-02：五个假设各自三态，且与语义车道自洽
    hyp = doc.get("hypotheses") or {}
    if set(hyp) != set(vd.HYPOTHESIS_IDS):
        problems.append(f"假设集不齐：{sorted(hyp)}")
    for hid in vd.HYPOTHESIS_IDS:
        state = (hyp.get(hid) or {}).get("state")
        if state not in vd.HYPOTHESIS_STATES:
            problems.append(f"{hid} 判决 {state!r} 不在三态值域")
    lane = doc.get("semantic_lane")
    reviewers = doc.get("semantic_lane_reviewers")
    if lane not in ("enabled", "disabled"):
        problems.append(f"semantic_lane={lane!r} 不在 enabled/disabled")
    elif lane == "enabled":
        if not isinstance(reviewers, list) or len(reviewers) != 2 or \
                not all(isinstance(x, str) and x.strip() for x in reviewers):
            problems.append(f"语义车道开启却无两名复核人标识：{reviewers!r}")
    else:
        if (hyp.get("H4") or {}).get("state") == "CAPABILITY_DEMONSTRATED":
            problems.append("语义车道 disabled 却把 H4 判为 CAPABILITY_DEMONSTRATED")
        limitations = " ".join(str(x) for x in (doc.get("limitations") or []))
        if "R1 仅得结构性结论" not in limitations:
            problems.append("语义车道 disabled 但限制节缺「R1 仅得结构性结论」")
    detail["semantic_lane"] = lane
    detail["hypotheses"] = {h: (hyp.get(h) or {}).get("state")
                            for h in vd.HYPOTHESIS_IDS}

    # AC-9-03：同输入重跑逐字节相同（重算，不读钟表）。
    # 语义三件按报告自述的值重算——验的是「其余全部可由数据根推出」，
    # 而不是把本轮裁决（enabled + 两名复核人）当默认值抄进判据。
    on_disk = Path(path).read_text(encoding="utf-8")
    recomputed = json.dumps(
        ana.build_final_report(
            root,
            semantic_lane=str(doc.get("semantic_lane") or ""),
            semantic_lane_reviewers=tuple(doc.get("semantic_lane_reviewers") or ()),
            semantic_present=bool((doc.get("semantic_scores") or {}).get("present")),
        ), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if recomputed != on_disk:
        problems.append("终局报告不可复算：重算结果与磁盘件不逐字节相同")
    detail["deterministic"] = recomputed == on_disk
    return problems, detail


# ---------------------------------------------------------------------------
# AC-9-06 / 07 —— 报告文本
# ---------------------------------------------------------------------------

def check_report_text(root: Path, *,
                      text: "str | None" = None) -> "tuple[list[str], dict]":
    """禁用措辞扫描（剔固定首页后）＋ 首页 sha256 登记自洽。"""
    path = Path(root) / "reports" / "final_report.md"
    if text is None:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return [f"缺报告文本：{path}"], {"path": str(path), "present": False}
    problems: "list[str]" = []

    # AC-9-07：首页三行逐字在场，且件内登记的 sha256 与常量相符
    registered = None
    if vd.SAMPLE_SIZE_DISCLAIMER not in text:
        problems.append("报告首页缺固定样本量声明（§18.3 逐字三行）")
    else:
        registered = vd.disclaimer_sha256()
        if registered not in text:
            problems.append(f"报告未登记首页声明的 sha256（{registered[:12]}…）")
    # AC-9-06：先按已登记 sha256 整块剔除，再逐词扫剩余全文
    hits = vd.scan_forbidden(text, registered_disclaimer_sha256=registered)
    problems.extend(vd.forbidden_report_text_problems(
        text, registered_disclaimer_sha256=registered))
    return problems, {"path": str(path), "disclaimer_sha256": registered,
                      "forbidden_hits": len(hits)}


# ---------------------------------------------------------------------------
# AC-9-08 / 09 —— 证据包
# ---------------------------------------------------------------------------

def _scan_pack_secrets(pack: Path) -> "tuple[list[str], list[str]]":
    """独立重扫密钥（与打包器的两条例外同口径，但实现不复用）。"""
    from orchestrator.common import secrets as secret_scan

    hits: "list[str]" = []
    waived: "list[str]" = []
    for path in sorted(pack.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        if path.stat().st_size > MAX_FILE_BYTES:
            continue
        rel = path.relative_to(pack).as_posix()
        text = path.read_text(encoding="utf-8", errors="ignore")
        for lineno, line in enumerate(text.splitlines(), 1):
            for name, matched in secret_scan.scan_text(line):
                body = str(matched)
                synthetic = (len(body) >= 20 and len(set(body)) == 1)
                if "test_scan_detects_all_strict_forms" in line or synthetic:
                    waived.append(f"{rel}::{name}::L{lineno}")
                else:
                    hits.append(f"{rel}::{name}::L{lineno}")
    return hits, waived


def check_pack(root: Path) -> "tuple[list[str], dict]":
    """AC-9-08：逐件 sha256、0700/0600、软链 0、>10 MiB 0、密钥 0 命中。"""
    pack = pack_dir(root)
    problems: "list[str]" = []
    if not pack.is_dir():
        return [f"缺证据包：{pack}"], {"path": str(pack), "present": False}
    manifest_path = pack / "manifest.json"
    manifest = _json(manifest_path)
    if manifest is None:
        return [f"缺证据包 manifest：{manifest_path}"], {"path": str(pack),
                                                         "present": True}
    recorded = pack_sha256(root)
    if recorded is None:
        problems.append("缺 evidence_pack/pack.sha256")
    elif recorded != _sha256(manifest_path):
        problems.append("pack.sha256 与 manifest.json 的实际 sha256 不符")

    if str(manifest.get("bench_root")) != str(root):
        problems.append(f"manifest.bench_root={manifest.get('bench_root')!r} "
                        f"≠ 数据根 {str(root)!r}")
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        problems.append("manifest.files 为空或不是列表")
        files = []

    missing: "list[str]" = []
    bad_sha: "list[str]" = []
    bad_mode: "list[str]" = []
    big: "list[str]" = []
    for entry in files:
        rel = str(entry.get("relpath") or "")
        target = pack / rel
        if not target.is_file():
            missing.append(rel)
            continue
        if _sha256(target) != str(entry.get("sha256") or ""):
            bad_sha.append(rel)
        if stat.S_IMODE(target.stat().st_mode) != 0o600:
            bad_mode.append(rel)
        if target.stat().st_size > MAX_FILE_BYTES:
            big.append(rel)
    if missing:
        problems.append(f"包内缺件 {len(missing)}：{missing[:5]}")
    if bad_sha:
        problems.append(f"包内件 sha256 与 manifest 不符 {len(bad_sha)}："
                        f"{bad_sha[:5]}")
    if bad_mode:
        problems.append(f"包内件模式非 0600 {len(bad_mode)}：{bad_mode[:5]}")
    if big:
        problems.append(f"包内件 >10 MiB：{big[:5]}")

    links = [p.relative_to(pack).as_posix() for p in pack.rglob("*")
             if p.is_symlink()]
    if links:
        problems.append(f"包内出现符号链接 {len(links)}：{links[:5]}")
    bad_dirs = [p.relative_to(pack).as_posix() for p in pack.rglob("*")
                if p.is_dir() and not p.is_symlink()
                and stat.S_IMODE(p.stat().st_mode) != 0o700]
    if bad_dirs:
        problems.append(f"包内目录模式非 0700 {len(bad_dirs)}：{bad_dirs[:5]}")

    hits, waived = _scan_pack_secrets(pack)
    if hits:
        problems.append(f"包内密钥命中 {len(hits)}：{hits[:3]}")

    return problems, {"path": str(pack), "present": True,
                      "pack_sha256": recorded, "n_files": len(files),
                      "total_bytes": int(manifest.get("total_bytes") or 0),
                      "secrets_hits": len(hits), "secrets_waived": len(waived),
                      "git_commit": manifest.get("git_commit"),
                      "git_dirty": manifest.get("git_dirty")}


def check_judgment_not_packed(root: Path) -> "tuple[list[str], dict]":
    """AC-9-09：判决件族（closure_judge*/acceptance_matrix*/signs/*.json）不在包内。"""
    pack = pack_dir(root)
    if not pack.is_dir():
        return [f"缺证据包：{pack}"], {"path": str(pack), "present": False}
    offenders = [p.relative_to(pack).as_posix() for p in pack.rglob("*")
                 if p.is_file() and _is_judgment_relpath(
                     p.relative_to(pack).as_posix())]
    problems = [f"判决件族出现在包内：{offenders[:5]}"] if offenders else []
    return problems, {"path": str(pack), "offenders": offenders}


def _is_judgment_relpath(relpath: str) -> bool:
    """与 ``build_evidence_pack.is_judgment_relpath`` 逐字同义的独立实现。"""
    parts = PurePosixPath(relpath).parts
    name = parts[-1] if parts else ""
    if fnmatch.fnmatch(name, "closure_judge*.json"):
        return True
    if fnmatch.fnmatch(name, "acceptance_matrix*.json"):
        return True
    if "signs" in parts[:-1] and name.endswith(".json"):
        return True
    return False


# ---------------------------------------------------------------------------
# AC-9-11 —— 三签
# ---------------------------------------------------------------------------

def check_signs(root: Path) -> "tuple[list[str], dict]":
    pack_sha = pack_sha256(root)
    signs_dir = Path(root) / "closeout" / "signs"
    problems: "list[str]" = []
    detail: "dict[str, Any]" = {"dir": str(signs_dir), "pack_sha256": pack_sha,
                                "roles": {}}
    if not signs_dir.is_dir():
        return [f"缺三签目录：{signs_dir}"], detail
    head = _git("rev-parse", "HEAD")
    for role in SIGN_ROLES:
        path = signs_dir / f"{role}.json"
        doc = _json(path)
        if doc is None:
            problems.append(f"缺 {role} 签：{path}")
            continue
        detail["roles"][role] = {"verdict": doc.get("verdict"),
                                 "git_commit": doc.get("git_commit"),
                                 "pack_sha256": doc.get("pack_sha256")}
        if str(doc.get("role") or "") != role:
            problems.append(f"{path.name} 的 role={doc.get('role')!r} ≠ {role!r}")
        if str(doc.get("verdict") or "") != "APPROVE":
            problems.append(f"{role} 签不是 APPROVE：{doc.get('verdict')!r}")
        commit = str(doc.get("git_commit") or "")
        if not _COMMIT_RE.fullmatch(commit):
            problems.append(f"{role} 签的 git_commit 非 40 hex：{commit!r}")
        elif head and subprocess.run(
                ["git", "merge-base", "--is-ancestor", commit, head],
                cwd=str(REPO), capture_output=True, check=False).returncode != 0:
            problems.append(f"{role} 签的 git_commit {commit} 不是 HEAD {head} 的祖先")
        if pack_sha is None:
            problems.append(f"{role} 签无法绑定 pack.sha256（缺件）")
        elif str(doc.get("pack_sha256") or "") != pack_sha:
            problems.append(f"{role} 签的 pack_sha256 与 evidence_pack/pack.sha256 不符")
    return problems, detail


# ---------------------------------------------------------------------------
# AC-9-13 —— 冷跑
# ---------------------------------------------------------------------------

def check_cold_run(root: Path) -> "tuple[list[str], dict]":
    path = Path(root) / "closeout" / "cold_run.json"
    doc = _json(path)
    if doc is None:
        return [f"缺冷跑记录：{path}"], {"path": str(path), "present": False}
    problems: "list[str]" = []
    log_path = Path(str(doc.get("log") or ""))
    detail: "dict[str, Any]" = {"path": str(path), "log": str(log_path),
                                "rc": doc.get("rc")}
    if not log_path.is_file():
        problems.append(f"冷跑日志不存在：{log_path}")
        return problems, detail
    text = log_path.read_text(encoding="utf-8", errors="ignore")
    try:
        from check_closure import parse_pytest_summary
    except Exception as exc:  # pragma: no cover - 仅在脚本目录不在 sys.path 时
        raise JudgeError(f"无法复用冷跑摘要解析器：{exc}") from exc
    summary = parse_pytest_summary(text)
    if summary is None:
        problems.append("冷跑日志里解析不出 pytest 摘要（passed/skipped/deselected）")
    else:
        detail["summary"] = summary
    rc = doc.get("rc")
    if not isinstance(rc, int) or isinstance(rc, bool):
        problems.append(f"冷跑 rc 非整数：{rc!r}")
    elif rc != 0:
        problems.append(f"冷跑 rc={rc}（必须 0）")
    if doc.get("summary") and summary and dict(doc["summary"]) != summary:
        problems.append(f"冷跑记录自述摘要与日志解析不一致："
                        f"{doc.get('summary')} ≠ {summary}")
    return problems, detail


# ---------------------------------------------------------------------------
# AC-9-12 / 收尾车道 —— 验收车道
# ---------------------------------------------------------------------------

def check_acceptance(root: Path) -> "tuple[list[str], dict]":
    """验收车道必须真跑过：rc==0、failed==0、passed>0。

    这条不占 AC 编号（AC-P281-9-01..17 不含它），但它的红会落进
    ``problems``，因而被 AC-9-12「problems == []」兜住——一条跑完什么都没验
    （``0 passed``）的车道，在这里与失败同等对待。
    """
    path = Path(root) / "closeout" / "acceptance_lane.json"
    doc = _json(path)
    if doc is None:
        return [f"缺验收车道记录：{path}"], {"path": str(path), "present": False}
    problems: "list[str]" = []
    detail = {"path": str(path), "rc": doc.get("rc"),
              "passed": doc.get("passed"), "failed": doc.get("failed"),
              "skipped": doc.get("skipped")}
    if doc.get("rc") != 0:
        problems.append(f"验收车道 rc={doc.get('rc')!r}（必须 0）")
    if int(doc.get("failed") or 0) != 0:
        problems.append(f"验收车道 failed={doc.get('failed')}（必须 0）")
    if int(doc.get("passed") or 0) <= 0:
        problems.append(f"验收车道 passed={doc.get('passed')!r}"
                        "（0 条通过＝什么都没验）")
    return problems, detail


# ---------------------------------------------------------------------------
# AC-9-14 —— 终态账本
# ---------------------------------------------------------------------------

def check_ledger(root: Path) -> "tuple[list[str], dict]":
    path = Path(root) / "quota" / "ledger.jsonl"
    if not path.is_file():
        return [f"缺账本：{path}"], {"path": str(path), "present": False}
    from orchestrator.gateway.quota_ledger import ledger_summary, read_lines

    rows = read_lines(path)
    summary = ledger_summary(rows, validate=False, allow_events=("selfcheck",))
    problems: "list[str]" = []
    calls = int(summary.get("call_rows") or 0)
    detail: "dict[str, Any]" = {
        "path": str(path), "calls": calls,
        "seq_first": summary.get("seq_first"), "seq_last": summary.get("seq_last"),
        "by_task": summary.get("calls_by_task"),
        "by_stage": summary.get("calls_by_stage"),
        "calls_ok": summary.get("calls_ok"),
    }
    if calls > constants.HARD_CAP:
        problems.append(f"实呼 {calls} 越过硬帽 {constants.HARD_CAP}")
    seqs = sorted(int(r["seq"]) for r in rows
                  if isinstance(r.get("seq"), int) and not isinstance(r.get("seq"), bool))
    gaps = [b for a, b in zip(seqs, seqs[1:]) if b != a + 1]
    if seqs and seqs[0] != 1:
        problems.append(f"seq 不以 1 开头：{seqs[0]}")
    if gaps:
        problems.append(f"seq 断号：{gaps}")
    by_task = summary.get("calls_by_task") or {}
    want_task = {str(t["task_no"]) for t in constants.TASK_ORDER}
    if not want_task <= set(by_task):
        problems.append(f"by_task 缺键：{sorted(want_task - set(by_task))}")
    extra_task = sorted(set(by_task) - want_task)
    if extra_task:
        problems.append(f"by_task 含未知任务号：{extra_task}")
    by_stage = summary.get("calls_by_stage") or {}
    extra_stage = sorted(set(by_stage) - {"coding_debug", "macro_ideation"})
    if extra_stage:
        problems.append(f"by_stage 含计划外阶段：{extra_stage}")
    return problems, detail


# ---------------------------------------------------------------------------
# AC-9-15 —— 四表未动
# ---------------------------------------------------------------------------

def _table_counts(uri: str) -> "dict[str, int] | None":
    try:
        import lancedb

        conn = lancedb.connect(uri)
        names = list(conn.table_names())
        return {name: int(conn.open_table(name).count_rows()) for name in names}
    except Exception:
        return None


def check_tables(root: Path) -> "tuple[list[str], dict]":
    frozen_path = Path(root) / "intake" / "table_counts.json"
    frozen = _json(frozen_path)
    if frozen is None:
        return [f"缺四表冻结件：{frozen_path}"], {"path": str(frozen_path)}
    want = frozen.get("tables") or {}
    uri = str(frozen.get("lancedb_uri") or "")
    detail: "dict[str, Any]" = {"frozen": want, "uri": uri,
                                "frozen_sha256": _sha256(frozen_path)}
    problems: "list[str]" = []
    live = _table_counts(uri) if uri else None
    if live is None:
        problems.append(f"无法读取活库行数：{uri!r}")
        return problems, detail
    detail["live"] = live
    for name, count in sorted(want.items()):
        if name not in live:
            problems.append(f"活库缺表 {name}")
        elif int(live[name]) != int(count):
            problems.append(f"表 {name} 行数 {live[name]} ≠ 冻结件 {count}")
    extra = sorted(set(live) - set(want))
    if extra:
        problems.append(f"活库出现冻结件之外的表：{extra}")
    return problems, detail


# ---------------------------------------------------------------------------
# AC-9-16 —— 五本旧账封存
# ---------------------------------------------------------------------------

def check_old_ledgers(root: Path) -> "tuple[list[str], dict]":
    frozen_path = Path(root) / "intake" / "ledger_state.json"
    frozen = _json(frozen_path)
    if frozen is None:
        return [f"缺旧账冻结件：{frozen_path}"], {"path": str(frozen_path)}
    problems: "list[str]" = []
    detail: "dict[str, Any]" = {"path": str(frozen_path), "ledgers": {}}
    for name, want in sorted((frozen.get("ledgers") or {}).items()):
        path = Path(str(want.get("path") or ""))
        if not path.is_file():
            problems.append(f"旧账 {name} 不存在：{path}")
            continue
        got = _sha256(path)
        detail["ledgers"][name] = {"sha256": got, "frozen": want.get("sha256")}
        if got != str(want.get("sha256") or ""):
            problems.append(f"旧账 {name} 的 sha256 变化（封存被破坏）")
    return problems, detail


# ---------------------------------------------------------------------------
# AC-9-17 —— 环境
# ---------------------------------------------------------------------------

def check_environment(root: Path) -> "tuple[list[str], dict]":
    path = Path(root) / "closeout" / "environment.json"
    doc = _json(path)
    if doc is None:
        return [f"缺收尾环境件：{path}"], {"path": str(path), "present": False}
    problems: "list[str]" = []
    containers = doc.get("containers")
    gpu = doc.get("gpu_mib")
    compose_rc = doc.get("compose_rc")
    detail = {"path": str(path), "containers": containers, "gpu_mib": gpu,
              "compose_rc": compose_rc, "df_mnt": doc.get("df_mnt")}
    if compose_rc != 0:
        problems.append(f"compose 探测 rc={compose_rc!r}（≠0 时「0 个容器」"
                        "是没看见而不是干净）")
    if containers != 0:
        problems.append(f"容器残留：{containers!r}")
    if not isinstance(gpu, int) or isinstance(gpu, bool) or gpu > GPU_LIMIT_MIB:
        problems.append(f"显存 {gpu!r} 超过 {GPU_LIMIT_MIB} MiB")
    if not doc.get("df_mnt"):
        problems.append("未记录 /mnt 空间")
    return problems, detail


# ---------------------------------------------------------------------------
# AC-9-12 —— gaps 只许登记预注册不做项
# ---------------------------------------------------------------------------

def collect_gaps(root: Path) -> "tuple[list[str], list[str]]":
    """返回 ``(problems, gaps)``：gap 由**终局报告的事实**推出，不由手写。"""
    doc = _json(Path(root) / "analysis" / "final_report.json")
    if doc is None:
        return ["缺终局报告，无法判定 gaps"], []
    gaps: "list[str]" = []
    counts = doc.get("trial_counts") or {}
    if int(counts.get("confirmatory_present") or 0) == 0:
        gaps.append("CONFIRMATORY_NOT_RUN_PILOT_HOLD")
    if not (doc.get("semantic_scores") or {}).get("present"):
        gaps.append("SEMANTIC_SCORES_ABSENT_NO_CONFIRMATORY")
    problems = [f"未登记的 gap：{g}" for g in gaps if g not in ALLOWED_GAPS]
    return problems, gaps


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------

_CHECKS = {
    "ac_index": check_ac_index,
    "final_report": check_final_report,
    "report_text": check_report_text,
    "pack": check_pack,
    "judgment_not_packed": check_judgment_not_packed,
    "signs": check_signs,
    "cold_run": check_cold_run,
    "acceptance": check_acceptance,
    "ledger": check_ledger,
    "tables": check_tables,
    "old_ledgers": check_old_ledgers,
    "environment": check_environment,
}


def run_judge(root: Path, *, checks: "Sequence[str]" = CHECKS) -> dict:
    """跑指定检查，返回判决件 dict（**不写盘**、不含钟表时间）。

    每次运行都重算；``problems`` 汇总为 ``"[check] 说明"``，便于一眼看出谁红。
    """
    root = Path(root)
    unknown = [c for c in checks if c not in _CHECKS and c != "gaps"]
    if unknown:
        raise JudgeError(f"未知检查名：{unknown}")
    results: "dict[str, Any]" = {}
    problems: "list[str]" = []
    for name in checks:
        if name == "gaps":
            continue
        try:
            probs, detail = _CHECKS[name](root)
        except JudgeError as exc:
            probs, detail = [f"机判无法完成：{exc}"], {}
        except Exception as exc:  # noqa: BLE001 - 机判自身出错也要记成 problem
            probs, detail = [f"检查抛异常：{type(exc).__name__}: {exc}"], {}
        results[name] = {"problems": list(probs), "detail": detail}
        problems.extend(f"[{name}] {p}" for p in probs)

    gaps: "list[str]" = []
    if "gaps" in checks:
        gap_problems, gaps = collect_gaps(root)
        results["gaps"] = {"problems": list(gap_problems), "detail": {"gaps": gaps}}
        problems.extend(f"[gaps] {p}" for p in gap_problems)

    return {
        "schema": SCHEMA,
        "git_commit": _git("rev-parse", "HEAD"),
        "pack_sha256": pack_sha256(root),
        "checks": results,
        "problems": problems,
        "gaps": gaps,
        "verdict": "PASS" if not problems else "FAIL",
    }


def judge_text(doc: Mapping[str, Any]) -> str:
    """判决件的**唯一序列化**（写盘与比对共用，防两处格式分叉）。"""
    return json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def write_judge(root: Path) -> Path:
    """把 ``run_judge`` 的结果原子写到 ``<root>/closeout/closure_judge.json``。"""
    from orchestrator.common.fsutil import atomic_write_text, ensure_private_dir

    target = Path(root) / JUDGE_RELPATH
    ensure_private_dir(target.parent)
    atomic_write_text(target, judge_text(run_judge(root)), mode=0o600)
    return target


def verify_fixed_point(root: Path) -> "tuple[list[str], dict]":
    """AC-9-10 的两条不动点等式（**写盘之后**才能跑）。

    ① 判决件的 ``pack_sha256`` == ``evidence_pack/pack.sha256`` 的实际内容；
    ② 同输入重算 ``run_judge`` 与磁盘件**逐字节相同**。
    任何一条不成立 ⇒ 包在判决之后被改过，或判决不可复算。
    """
    root = Path(root)
    path = root / JUDGE_RELPATH
    problems: "list[str]" = []
    detail: "dict[str, Any]" = {"path": str(path), "present": path.is_file()}
    if not path.is_file():
        return [f"缺判决件：{path}"], detail
    on_disk = path.read_text(encoding="utf-8")
    doc = _json(path) or {}
    recorded = pack_sha256(root)
    detail["pack_sha256_recorded"] = doc.get("pack_sha256")
    detail["pack_sha256_actual"] = recorded
    if recorded is None:
        problems.append("缺 evidence_pack/pack.sha256，等式①无法成立")
    elif str(doc.get("pack_sha256") or "") != recorded:
        problems.append("判决件的 pack_sha256 与 evidence_pack/pack.sha256 的实际"
                        "内容不符（包在判决之后被改过？）")
    recomputed = judge_text(run_judge(root))
    detail["deterministic"] = recomputed == on_disk
    if recomputed != on_disk:
        problems.append("重算判决与磁盘件不逐字节相同（判决不可复算）")
    if _is_judgment_relpath(JUDGE_RELPATH):
        detail["judgment_family_excluded"] = True
    else:  # pragma: no cover - 常数写错才会到这里
        problems.append("判决件路径未被判决件族规则命中")
    return problems, detail


def main(argv: "Sequence[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", default=None, help="数据根（缺省 paths.phase281_root()）")
    ap.add_argument("--check-ac-index", action="store_true",
                    help="只跑 AC 索引检查并打印（不落盘）")
    ap.add_argument("--checks", default=None,
                    help="逗号分隔的检查名子集（缺省全部）")
    ap.add_argument("--verify", action="store_true",
                    help="跑不动点两条等式（需已有判决件）")
    args = ap.parse_args(list(argv) if argv is not None else None)

    from orchestrator.benchmark.phase281 import paths

    root = Path(args.root) if args.root else paths.phase281_root()
    if args.verify:
        problems, detail = verify_fixed_point(root)
        print(json.dumps({"problems": problems, "detail": detail},
                         ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if not problems else 1
    checks = ([c.strip() for c in str(args.checks).split(",") if c.strip()]
              if args.checks else CHECKS)
    doc = run_judge(root, checks=checks)
    print(json.dumps({"verdict": doc["verdict"], "problems": doc["problems"],
                      "gaps": doc["gaps"]},
                     ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if not doc["problems"] else 1


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main())
