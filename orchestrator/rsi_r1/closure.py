"""RSI-R1 T5：结案产物、证据包与独立复算（Plan §5.6/§9，AC26/AC27）。

本模块只做**产物**，不做判定：

* ``confirm/manifest_<study>.json`` / ``judge/report_<study>.json`` /
  ``replay/comparison_<study>.json`` / ``claim/submission_<study>.json`` 的
  落盘（0600，走 campaign 写守卫）；
* ``deliverable/closure.json``——各 study 的终态声明（``COMPLETE`` 只在
  judge 三层全过 **且** replay 全过时才允许）、LOCK sha、人工时间摘要、
  账本计数（零付费可自证）；
* ``evidence_pack/`` 证据包 + ``evidence_manifest.json``（逐件 sha256，包内
  不得有符号链接、不得有 >10 MiB 件、不得有未登记件）；
* ``run_closure_judge`` —— **外部进程**调用纯标准库独立复算器
  ``scripts/verification/check_rsi_r1_closure.py``，读回它的判决件。

纪律：

* 人工时间**未知不填 0**：``write_deliverable`` 默认调用
  ``human_events.assert_complete_for_closeout``，有未结束事件即拒写。
* 账本文件必须**存在**（独立复算器要求 ``quota/ledger.jsonl`` 存在）；
  零付费时写**空文件**，绝不伪造开账行（共享账本工具不认 R1 命名空间）。
* 本模块不 import ``judge`` 的判定函数，也不 import 独立复算器；它只搬运
  已生成的文档。判定在 ``judge.py`` 与复算器里各做一遍。
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping

from orchestrator.common.clock import utc_now_iso_z
from orchestrator.common.fsutil import ensure_private_dir
from orchestrator.rsi_r1 import human_events as HE

DELIVERABLE_RELPATH = "deliverable/closure.json"
DELIVERABLE_SCHEMA = "simpraxis.rsi_r1.deliverable.v1"
PACK_SCHEMA = "simpraxis.rsi_r1.evidence_manifest.v1"
PACK_MANIFEST = "evidence_manifest.json"
PACK_DIRNAME = "evidence_pack"
LEDGER_RELPATH = "quota/ledger.jsonl"
JUDGE_REPORT_RELPATH = "judge/closure_judge.json"
CHECKER_RELPATH = "scripts/verification/check_rsi_r1_closure.py"

STUDIES = ("A", "B")

#: 默认进包前缀（campaign 根下的一级目录名）。``private/``、``observations/``
#: 与 ``evidence_pack/`` 默认**不进包**：前两者含候选不可见材料/大体积原始帧，
#: 后者是包自身。
PACK_PREFIXES = ("contract", "state", "confirm", "judge", "replay", "claim",
                 "deliverable", "quota", "human_events", "protocol", "closeout",
                 "research_packets")

MAX_PACK_FILE_BYTES = 10 * 1024 * 1024

CLAIM_COMPLETE = "COMPLETE"
CLAIM_VALID_FAIL = "VALID_FAIL"
CLAIM_BLOCKED = "BLOCKED"


class ClosureError(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _require_study(study: str) -> str:
    if study not in STUDIES:
        raise ClosureError("UNKNOWN_STUDY", repr(study))
    return study


def _write_private(path: Path, data: bytes, *, root: "Path | None" = None) -> None:
    """0600 原子写；给了 ``root`` 时逐级把父目录收敛到 0700（证据包纪律）。"""
    if root is not None:
        _ensure_chain(path.parent, stop=root)
    else:
        ensure_private_dir(path.parent)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def _ensure_chain(path: Path, *, stop: Path) -> None:
    """从 ``stop`` 往下逐级建目录并 chmod 0700（不碰 ``stop`` 之外的兄弟目录）。"""
    ensure_private_dir(stop)
    cur = stop
    for part in path.relative_to(stop).parts:
        cur = cur / part
        ensure_private_dir(cur)


def _dump(doc: Any) -> bytes:
    return (json.dumps(doc, ensure_ascii=False, sort_keys=True, indent=1,
                       allow_nan=False) + "\n").encode("utf-8")


# ---------------------------------------------------------------------------
# 账本文件（零付费自证）
# ---------------------------------------------------------------------------

def ensure_ledger_file(ctx: Any) -> Path:
    """确保 ``quota/ledger.jsonl`` 存在；不存在时建**空** 0600 文件。

    绝不写开账行/占位行：独立复算器按 ``event == "call"`` 计实呼，空文件
    天然满足「0 实呼、seq 连续、无 selfcheck」三项。
    """
    path = ctx.assert_write_allowed(ctx.relative(LEDGER_RELPATH))
    if not path.exists():
        ensure_private_dir(path.parent)
        fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_EXCL, 0o600)
        os.close(fd)
        os.chmod(path, 0o600)
    return path


def ledger_rows(ctx: Any) -> "list[dict[str, Any]]":
    path = ctx.relative(LEDGER_RELPATH)
    if not path.exists():
        return []
    rows: "list[dict[str, Any]]" = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ClosureError("LEDGER_UNREADABLE", f"第 {lineno} 行：{exc}")
        if not isinstance(row, dict):
            raise ClosureError("LEDGER_UNREADABLE", f"第 {lineno} 行不是对象")
        rows.append(row)
    return rows


def ledger_summary(ctx: Any) -> "dict[str, Any]":
    rows = ledger_rows(ctx)
    calls = [r for r in rows if r.get("event", "call") == "call"]
    by_lane: "dict[str, int]" = {}
    for row in calls:
        lane = str(row.get("lane") or "unknown")
        by_lane[lane] = by_lane.get(lane, 0) + 1
    return {"rows": len(rows), "calls": len(calls),
            "selfcheck": sum(1 for r in rows if r.get("event") == "selfcheck"),
            "ok": sum(1 for r in rows if r.get("event", "call") == "call"
                      and r.get("ok") is True),
            "failed": sum(1 for r in rows if r.get("event", "call") == "call"
                          and r.get("ok") is not True),
            "calls_by_lane": dict(sorted(by_lane.items()))}


# ---------------------------------------------------------------------------
# 逐 study 产物
# ---------------------------------------------------------------------------

def write_confirm_manifest(ctx: Any, study: str, manifest: Any) -> "dict[str, Any]":
    doc = manifest.to_dict() if hasattr(manifest, "to_dict") else dict(manifest)
    _require_study(study)
    if doc.get("study_id") != study:
        raise ClosureError("STUDY_MISMATCH",
                           f"{doc.get('study_id')!r} != {study!r}")
    ctx.write_json(f"confirm/manifest_{study}.json", doc)
    return doc


def write_judge_report(ctx: Any, study: str, report: Mapping[str, Any]) -> "dict[str, Any]":
    _require_study(study)
    if report.get("study_id") != study:
        raise ClosureError("STUDY_MISMATCH",
                           f"{report.get('study_id')!r} != {study!r}")
    ctx.write_json(f"judge/report_{study}.json", dict(report))
    return dict(report)


def write_replay_comparison(ctx: Any, study: str,
                            comparison: Mapping[str, Any]) -> "dict[str, Any]":
    _require_study(study)
    if comparison.get("study_id") != study:
        raise ClosureError("STUDY_MISMATCH",
                           f"{comparison.get('study_id')!r} != {study!r}")
    ctx.write_json(f"replay/comparison_{study}.json", dict(comparison))
    return dict(comparison)


def write_claim_submission(ctx: Any, study: str,
                           submission: Mapping[str, Any]) -> "dict[str, Any]":
    _require_study(study)
    if submission.get("study_id") != study:
        raise ClosureError("STUDY_MISMATCH",
                           f"{submission.get('study_id')!r} != {study!r}")
    ctx.write_json(f"claim/submission_{study}.json", dict(submission))
    return dict(submission)


# ---------------------------------------------------------------------------
# 结案交付件
# ---------------------------------------------------------------------------

def study_entry(*, judge_report: "Mapping[str, Any] | None",
                comparison: "Mapping[str, Any] | None") -> "dict[str, Any]":
    """由两份产物派生一条 study 声明；**只有两轴都过才许 COMPLETE**。"""
    layers = (judge_report or {}).get("layers") or {}
    judge_ok = ((judge_report or {}).get("verdict") == "JUDGE_OK"
                and bool(layers.get("evidence_valid")))
    replay_ok = (comparison or {}).get("ok") is True
    if judge_report is None or comparison is None:
        claim = CLAIM_BLOCKED
    elif judge_ok and replay_ok:
        claim = CLAIM_COMPLETE
    else:
        claim = CLAIM_VALID_FAIL
    scientific = layers.get("scientific") or []
    return {
        "claim": claim,
        "judge_ok": judge_ok,
        "replay_ok": replay_ok,
        "judge_verdict": (judge_report or {}).get("verdict"),
        "claim_status": layers.get("claim_status"),
        "propositions": [{"id": c.get("id"), "proposition": c.get("proposition"),
                          "metric_id": c.get("metric_id"),
                          "status": c.get("status"), "effect": c.get("effect"),
                          "lower": c.get("lower"), "upper": c.get("upper"),
                          "n_blocks": c.get("n_blocks")}
                         for c in scientific],
        "judge_report": (f"judge/report_{judge_report.get('study_id')}.json"
                         if judge_report is not None else None),
        "replay": {"ok": replay_ok,
                   "n_replayed": (comparison or {}).get("n_replayed"),
                   "problems": list((comparison or {}).get("problems") or [])},
    }


def write_deliverable(ctx: Any, *, studies: Mapping[str, Mapping[str, Any]],
                      lock_sha256: str, human_time: "Mapping[str, Any] | None" = None,
                      cost: "Mapping[str, Any] | None" = None,
                      extra: "Mapping[str, Any] | None" = None,
                      now: "Any | None" = None) -> "dict[str, Any]":
    """写 ``deliverable/closure.json``。

    ``human_time`` 缺省时读取人类事件摘要并**强制**完整性：仍有未结束事件
    （时间未知）即 ``UNKNOWN_HUMAN_TIME`` 拒写——结案不许把未知填 0。
    """
    if not isinstance(lock_sha256, str) or len(lock_sha256) != 64:
        raise ClosureError("LOCK_SHA_INVALID", repr(lock_sha256))
    if human_time is None:
        log = HE.load(ctx)
        log.assert_complete_for_closeout()
        human_time = log.summary()
    declared = {study: dict(studies[study]) for study in STUDIES if study in studies}
    for study, entry in declared.items():
        _require_study(study)
        claim = entry.get("claim")
        support = bool(entry.get("judge_ok")) and bool(entry.get("replay_ok"))
        if claim in (CLAIM_COMPLETE, "VERIFIED") and not support:
            raise ClosureError(
                "CLAIM_UNSUPPORTED",
                f"{study}: claim={claim} 但 judge_ok={entry.get('judge_ok')} "
                f"replay_ok={entry.get('replay_ok')}")
    doc: "dict[str, Any]" = {
        "schema": DELIVERABLE_SCHEMA, "schema_version": 1,
        "lock_sha256": lock_sha256,
        "studies": declared,
        "human_time": dict(human_time),
        "cost": dict(cost) if cost is not None else ledger_summary(ctx),
        "created_at_utc": (now or utc_now_iso_z)(),
    }
    if extra:
        doc.update(dict(extra))
    ctx.write_json(DELIVERABLE_RELPATH, doc)
    return doc


# ---------------------------------------------------------------------------
# 证据包
# ---------------------------------------------------------------------------

def checker_path(repo_root: "Path | str | None" = None) -> Path:
    root = Path(repo_root) if repo_root is not None else Path(__file__).resolve().parents[2]
    return root / CHECKER_RELPATH


def _iter_files(ctx: Any, prefixes: Iterable[str]) -> "list[tuple[str, Path]]":
    root = ctx.root
    out: "list[tuple[str, Path]]" = []
    for name in prefixes:
        base = root / name
        if base.is_symlink():
            raise ClosureError("PACK_SYMLINK", f"{name} 是符号链接")
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
            dirnames.sort()
            for fname in sorted(filenames):
                p = Path(dirpath) / fname
                if p.is_symlink():
                    raise ClosureError("PACK_SYMLINK",
                                       p.relative_to(root).as_posix())
                if fname.endswith(".tmp"):
                    raise ClosureError(
                        "PACK_DIRTY",
                        f"{p.relative_to(root).as_posix()} 是未完成的临时文件")
                out.append((p.relative_to(root).as_posix(), p))
    out.sort(key=lambda item: item[0])
    return out


def build_evidence_pack(ctx: Any, *, dest: "Path | str | None" = None,
                        prefixes: Iterable[str] = PACK_PREFIXES,
                        extras: "Mapping[str, Path] | None" = None,
                        repo_root: "Path | str | None" = None,
                        now: "Any | None" = None) -> "dict[str, Any]":
    """把产物逐件复制进 ``evidence_pack/`` 并写 ``evidence_manifest.json``。

    ``extras`` 是 ``包内 relpath → 源文件``（默认带独立复算器源码一份）。
    """
    pack = Path(dest) if dest is not None else ctx.relative(PACK_DIRNAME)
    pack = ctx.assert_write_allowed(pack)
    if pack.exists() and any(pack.iterdir()):
        raise ClosureError("PACK_EXISTS", f"{pack} 非空，拒绝覆盖")
    ensure_private_dir(pack)
    files = _iter_files(ctx, tuple(prefixes))
    entries: "dict[str, str]" = {}
    total = 0
    for rel, src in files:
        size = src.stat().st_size
        if size > MAX_PACK_FILE_BYTES:
            raise ClosureError("PACK_FILE_TOO_LARGE", f"{rel} = {size} B")
        data = src.read_bytes()
        _write_private(pack / rel, data, root=pack)
        entries[rel] = hashlib.sha256(data).hexdigest()
        total += len(data)
    extra_map = dict(extras) if extras is not None else {
        f"checker/{CHECKER_RELPATH.split('/')[-1]}": checker_path(repo_root)}
    for rel, src in sorted(extra_map.items()):
        srcp = Path(src)
        if not srcp.is_file():
            raise ClosureError("PACK_EXTRA_MISSING", f"{rel} ← {srcp}")
        data = srcp.read_bytes()
        _write_private(pack / rel, data, root=pack)
        entries[rel] = hashlib.sha256(data).hexdigest()
        total += len(data)
    manifest = {"schema": PACK_SCHEMA, "schema_version": 1,
                "campaign_root": str(ctx.root),
                "prefixes": list(prefixes),
                "files": entries, "n_files": len(entries),
                "total_bytes": total,
                "created_at_utc": (now or utc_now_iso_z)()}
    _write_private(pack / PACK_MANIFEST, _dump(manifest), root=pack)
    return manifest


def verify_pack(pack: "Path | str") -> "dict[str, Any]":
    """进程内复核对（独立复算器的 ``--verify`` 是第二遍，两遍都要过）。"""
    pack = Path(pack)
    problems: "list[str]" = []
    manifest_path = pack / PACK_MANIFEST
    if not manifest_path.is_file():
        return {"ok": False, "n_files": 0, "problems": ["缺 evidence_manifest.json"]}
    doc = json.loads(manifest_path.read_text(encoding="utf-8"))
    files = doc.get("files") or {}
    if not files:
        problems.append("manifest.files 为空")
    for rel, want in sorted(files.items()):
        path = pack / rel
        if path.is_symlink():
            problems.append(f"{rel}:symlink")
            continue
        if not path.is_file():
            problems.append(f"{rel}:missing")
            continue
        got = _sha256_file(path)
        if got != want:
            problems.append(f"{rel}:sha {want} → {got}")
    listed = set(files)
    for dirpath, _dirnames, filenames in os.walk(pack):
        for name in filenames:
            rel = os.path.relpath(os.path.join(dirpath, name), pack)
            if rel == PACK_MANIFEST:
                continue
            if rel not in listed:
                problems.append(f"{rel}:unlisted")
    for dirpath, _dirnames, filenames in os.walk(pack):
        mode = (Path(dirpath).stat().st_mode & 0o777)
        if mode != 0o700:
            problems.append(f"{os.path.relpath(dirpath, pack)}:dir_mode={oct(mode)}")
        for name in filenames:
            p = Path(dirpath) / name
            fmode = p.stat().st_mode & 0o777
            if fmode != 0o600:
                problems.append(f"{os.path.relpath(p, pack)}:file_mode={oct(fmode)}")
    return {"ok": not problems, "n_files": len(files), "problems": problems}


# ---------------------------------------------------------------------------
# 独立复算器（外部进程）
# ---------------------------------------------------------------------------

def run_closure_judge(ctx: Any, *, repo_root: "Path | str | None" = None,
                      pack: "bool | Path | str | None" = None,
                      report: "Path | str | None" = None,
                      timeout_s: float = 300.0) -> "dict[str, Any]":
    """跑独立复算器并返回其判决件。

    ``pack=None`` 表示「存在 ``evidence_pack/`` 就一并核对」；``False`` 明确不核；
    其余按给定路径核。复算器 rc 0=PASS / 1=FAIL / 2=输入不可读，三者都返回
    判决件（``ok`` 只在 rc==0 且 verdict==PASS 时为真）。
    """
    checker = checker_path(repo_root)
    if not checker.is_file():
        raise ClosureError("CHECKER_MISSING", str(checker))
    dest = Path(report) if report is not None else ctx.relative(JUDGE_REPORT_RELPATH)
    argv = [sys.executable, "-I", str(checker), "--root", str(ctx.root),
            "--report", str(dest)]
    if pack is None:
        default_pack = ctx.relative(PACK_DIRNAME)
        if (default_pack / PACK_MANIFEST).is_file():
            argv += ["--verify", str(default_pack)]
    elif pack is not False:
        argv += ["--verify", str(pack)]
    proc = subprocess.run(argv, capture_output=True, text=True,
                          timeout=timeout_s, check=False)
    try:
        judged = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ClosureError(
            "CHECKER_OUTPUT",
            f"rc={proc.returncode} stdout 非 JSON：{exc}；stderr={proc.stderr[-400:]}")
    dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not dest.is_file():
        # 复算器在 --report 路径上自写；万一它没写（旧版），这里补落。
        _write_private(dest, _dump(judged))
    judged["returncode"] = proc.returncode
    judged["ok"] = (proc.returncode == 0 and judged.get("verdict") == "PASS")
    judged["report_path"] = str(dest)
    judged["stderr_tail"] = proc.stderr[-800:]
    return judged
