#!/usr/bin/env python3
"""Phase 2.7 Task 7 —— 里程碑终验（AC-T7-1..7 机判）。

七条 AC 各自一个用例，判据见计划 §T7.6。全部**只读**、0 付费、不联网：

- AC-T7-1 冷跑两排除全套：``rc=0``、``failed==0``、``passed >= 2198``，
  且 ``cold_run.json`` 与 ``cold_run.raw.log`` 逐数字相符（log 的 sha256 也钉在
  json 里，防止「换了日志忘了换数」）；
- AC-T7-2 账：``seq`` 连续、``ts_iso`` 全 ``Z``、实呼 ≤ 178、票额不超；
- AC-T7-3 四表：``kb_chunks==1542``、``phase1_learnings==44``、
  ``phase2_learnings==1``、``solution_chunks == 94 + Δ``（Δ 取自 Task 6 的
  ``RECYCLE.json``，必须与批准件一致）；另跑 ``check_solutions``；
- AC-T7-4 证据包：逐件 sha256 复算、无 >10MB、无符号链接、无多登记、
  密钥扫描 0 命中；
- AC-T7-5 环境出清：无 sim_praxis 容器、显存 ≤ 200 MiB、``/mnt`` 有记录；
- AC-T7-6 三签：三份签署件存在、``APPROVE``、绑定 commit == HEAD；
- AC-T7-7 机判件：``closure_judge.json`` 的 ``problems == []``，
  ``gaps`` 逐条挂牌（未达成项不得计为 pass）。

**车道纪律（锁内适配 3）**：本文件整组打 ``milestone_closeout`` 标记，裸跑
收集期 deselect，需 ``SIMPRAXIS_MILESTONE_CLOSEOUT=1`` 才执行——它验的是
**已落盘的收尾证据**（冷跑日志/证据包/三签/机判件），证据建成前必然红；
若把它塞进冷跑，冷跑就会被自己正在写的日志拖红，只能靠 ``--ignore`` 绕，
而 ``--ignore`` 正是 AC-T7-1 要禁的「少收一条」口子。验收套因此与 container
同构：独立车道、独立日志留证。AC-T7-3 另需 ``SIMPRAXIS_KB_INTEGRATION=1``。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

pytestmark = pytest.mark.milestone_closeout

MILESTONE = "task7_milestone"
SIGN_SCHEMA = "simpraxis.phase27.sign.v1"
CLOSURE_SCHEMA = "simpraxis.phase27.closure_judge.v1"
COLD_JSON_SCHEMA = "simpraxis.phase27.cold_run.v1"

COLD_PASSED_FLOOR = 2198
HARD_CAP = 178
TABLE_BASELINE = {"kb_chunks": 1542, "phase1_learnings": 44,
                  "phase2_learnings": 1}
SOLUTION_BASELINE_PRE = 94
MAX_FILE_BYTES = 10 * 1024 * 1024
REQUIRED_GAP_TAGS = ("T4_UNFREEZE_NOT_ESTABLISHED", "T6_NO_CONTROL_ARM",
                     "T6_MAIN_POLICY_ZERO")


# ------------------------------------------------------------------ 路径与工具
def phase27_root() -> Path:
    env = os.environ.get("SIMPRAXIS_PHASE27_ROOT")
    if env:
        return Path(env)
    raise AssertionError(
        "SIMPRAXIS_PHASE27_ROOT 未设置（先 source scripts/00_env.sh）")


def milestone() -> Path:
    return phase27_root() / MILESTONE


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_json(p: Path):
    assert p.is_file(), f"缺件：{p}"
    return json.loads(p.read_text(encoding="utf-8"))


def _head() -> str:
    return subprocess.run(["git", "-C", str(_REPO), "rev-parse", "HEAD"],
                          capture_output=True, text=True,
                          check=True).stdout.strip()


def _parse_log(text: str) -> dict:
    """``# cmd:`` / 摘要行 / ``rc=`` 三元；各计数按名提取，不依赖次序。"""
    cmds = re.findall(r"^# cmd: (.+)$", text, re.M)
    rcs = re.findall(r"^rc=(\d+)$", text, re.M)
    lines = [ln.strip() for ln in text.splitlines()
             if re.search(r"\b\d+ passed\b", ln)]
    assert len(cmds) == 1, f"`# cmd:` 行应恰 1 条，实得 {len(cmds)}"
    assert len(rcs) == 1, f"`rc=` 行应恰 1 条，实得 {len(rcs)}"
    assert lines, "日志里没有 pytest 摘要行"

    def _n(field: str) -> int:
        m = re.search(rf"\b(\d+) {field}\b", lines[-1])
        return int(m.group(1)) if m else 0

    return {"command": cmds[0].strip(), "summary": lines[-1],
            "passed": _n("passed"), "failed": _n("failed"),
            "skipped": _n("skipped"), "deselected": _n("deselected"),
            "rc": int(rcs[0])}


# ------------------------------------------------------------------ AC-T7-1
def test_ac_t7_1_cold_run():
    raw = milestone() / "cold_run.raw.log"
    assert raw.is_file(), f"缺冷跑原始日志：{raw}"
    parsed = _parse_log(raw.read_text(encoding="utf-8", errors="replace"))

    assert parsed["rc"] == 0, f"冷跑 rc={parsed['rc']}（日志 {raw}）"
    assert parsed["failed"] == 0, f"冷跑 failed={parsed['failed']}"
    # 锁内适配 1：基线是地板不是等式，新增测试数单列不与既有基线混算
    assert parsed["passed"] >= COLD_PASSED_FLOOR, (
        f"冷跑 passed={parsed['passed']} < 地板 {COLD_PASSED_FLOOR}")

    # 命令口径：必须是 CLAUDE.md 收尾命令的**逐字形态**——解释器 + 固定参数
    # 尾部，尾部之前不得再夹任何旗标（否则 `--ignore=…` 这类「少收几条测试」
    # 也能骗过子串检查）。
    _CANON = ('-m pytest tests -q -k "not test_04" '
              '-m "not paid and not kb_integration"')
    cmd = parsed["command"]
    assert cmd.endswith(_CANON), (
        f"冷跑命令不是 CLAUDE.md 的逐字形态（尾部应为 {_CANON!r}）：{cmd}")
    prefix = cmd[:-len(_CANON)]
    assert prefix and " " not in prefix and "--" not in prefix, (
        f"冷跑命令在解释器与固定尾部之间夹了额外参数：{prefix!r}")
    assert "--ignore" not in cmd, "冷跑命令不得 --ignore 任何测试文件"

    doc = _read_json(milestone() / "cold_run.json")
    assert doc.get("schema") == COLD_JSON_SCHEMA, doc.get("schema")
    assert doc["log_sha256"] == _sha256(raw), (
        "cold_run.json 记的 log sha256 与实际日志字节不符"
        "（换了日志没换数）")
    for field in ("command", "passed", "failed", "skipped", "deselected", "rc"):
        assert doc[field] == parsed[field], (
            f"cold_run.json.{field}={doc[field]!r} ≠ 日志 {parsed[field]!r}")


# ------------------------------------------------------------------ AC-T7-2
def test_ac_t7_2_ledger_reconcile():
    from orchestrator.common.runtime_paths import (
        phase27_approvals_dir, phase27_ledger_path)

    path = phase27_ledger_path()
    assert path.is_file(), f"账本不存在：{path}"
    rows = [json.loads(ln) for ln in
            path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert rows, f"账本为空：{path}"

    import tempfile
    with tempfile.TemporaryDirectory() as td:
        rep = Path(td) / "ledger_check.json"
        proc = subprocess.run(
            [sys.executable, str(_REPO / "scripts/phase27/check_ledger_phase27.py"),
             "--ledger", str(path), "--report", str(rep)],
            cwd=str(_REPO), capture_output=True, text=True)
        assert rep.is_file(), f"账对账器没产出报告：{proc.stderr[-1000:]}"
        doc = json.loads(rep.read_text(encoding="utf-8"))
    assert proc.returncode == 0, (
        "账对账不通过（check_ledger_phase27 rc=%d）：%s"
        % (proc.returncode, "；".join(doc["problems"])))
    assert doc["problems"] == [], "；".join(doc["problems"])
    n_calls = doc["summary"]["call_rows"]
    assert n_calls <= HARD_CAP, f"实呼 {n_calls} 超硬帽 {HARD_CAP}"

    # 每张票都必须有批准件，用量不超票额（无票不许发网），且该批实呼的
    # (task, stage) 必须与票面声明逐项一致——只查票额会放过「把 task 从 4
    # 改成 6 但仍挂在 P27-B1 下」这种账实不符（AC-T7-2 的 by_task/by_stage 口径）
    by_batch: "dict[str, int]" = {}
    for r in rows:
        if r.get("event") == "call":
            key = str(r.get("batch_id"))
            by_batch[key] = by_batch.get(key, 0) + 1
    assert by_batch, "账上没有按批次登记的实呼（票账不可审）"
    approvals = phase27_approvals_dir()
    for batch_id, used in by_batch.items():
        p = approvals / f"{batch_id}.json"
        assert p.is_file(), f"批次 {batch_id} 有 {used} 呼但没有批准件：{p}"
        doc = _read_json(p)
        cap = doc.get("approved")
        assert isinstance(cap, int), f"批准件 {batch_id} 的 approved 非 int"
        assert used <= cap, f"批次 {batch_id} 用量 {used} 超票额 {cap}"

        want = {(str(doc.get("task")), str(doc.get("stage")))}
        got = {(str(r.get("task")), str(r.get("stage")))
               for r in rows
               if r.get("event") == "call"
               and str(r.get("batch_id")) == batch_id}
        assert got == want, (
            f"批次 {batch_id} 实呼 (task, stage)={sorted(got)} 与票面声明 "
            f"{sorted(want)} 不符")


# ------------------------------------------------------------------ AC-T7-3
@pytest.mark.kb_integration
def test_ac_t7_3_tables():
    import lancedb
    from memory_flywheel import kb_pins

    db = lancedb.connect(kb_pins.default_lancedb_uri())
    rows = {}
    for name in ("kb_chunks", "solution_chunks", "phase1_learnings",
                 "phase2_learnings"):
        rows[name] = db.open_table(name).count_rows()
    for name, want in TABLE_BASELINE.items():
        assert rows[name] == want, f"{name} 行数 {rows[name]} ≠ 基线 {want}"

    rec_path = phase27_root() / "task6_vla" / "manifests" / "RECYCLE.json"
    rec = _read_json(rec_path)
    delta = rec.get("solution_chunks_delta")
    assert isinstance(delta, int), f"回收件未声明 solution_chunks 增量：{rec_path}"
    assert rec.get("solution_chunks_before") == SOLUTION_BASELINE_PRE, (
        f"回收件基线 {rec.get('solution_chunks_before')!r} ≠ "
        f"{SOLUTION_BASELINE_PRE}")
    assert rows["solution_chunks"] == SOLUTION_BASELINE_PRE + delta, (
        f"solution_chunks={rows['solution_chunks']} ≠ "
        f"{SOLUTION_BASELINE_PRE}+Δ{delta}")

    # 内容层分层校验（KB pin + 6 方案 + data 忽略）
    proc = subprocess.run(
        [sys.executable, str(_REPO / "scripts/kb/check_solutions.py")],
        cwd=str(_REPO), capture_output=True, text=True)
    assert proc.returncode == 0, (
        f"check_solutions rc={proc.returncode}\n{proc.stdout[-2000:]}\n"
        f"{proc.stderr[-2000:]}")


# ------------------------------------------------------------------ AC-T7-4
def test_ac_t7_4_evidence_pack():
    pack = milestone() / "evidence_pack"
    man_path = pack / "manifest.json"
    man = _read_json(man_path)
    files = man.get("files") or []
    assert files, "证据包 manifest.files 为空（空包不算通过）"
    assert man.get("git_commit"), "manifest.git_commit 为空"

    declared = set()
    for entry in files:
        rel = entry["path"]
        p = pack / rel
        assert p.is_file(), f"包内缺件：{rel}"
        assert not p.is_symlink(), f"包内出现符号链接：{rel}"
        assert _sha256(p) == entry["sha256"], f"包内 sha 不符：{rel}"
        assert p.stat().st_size == int(entry["size"]), f"包内大小不符：{rel}"
        assert p.stat().st_size <= MAX_FILE_BYTES, f"包内 >10MB：{rel}"
        declared.add(rel)

    on_disk = {p.relative_to(pack).as_posix()
               for p in pack.rglob("*") if p.is_file()}
    orphans = sorted(on_disk - declared - {"manifest.json"})
    assert not orphans, f"包内出现 manifest 未登记的件：{orphans[:5]}"
    links = [p.relative_to(pack).as_posix()
             for p in pack.rglob("*") if p.is_symlink()]
    assert not links, f"包内出现符号链接（全树扫）：{links[:5]}"

    # 密钥双扫：Python 单一源 10 条严口径（gate 4 用的是同一份）
    from orchestrator.common import secrets as secret_scan
    hits = []
    for p in pack.rglob("*"):
        if not p.is_file() or p.is_symlink():
            continue
        if p.stat().st_size > MAX_FILE_BYTES:
            continue
        text = p.read_text(encoding="utf-8", errors="ignore")
        for name, matched in secret_scan.scan_text(text):
            hits.append(f"{p.relative_to(pack).as_posix()}::{name}::"
                        f"{str(matched)[:24]}")
    assert not hits, f"包内命中密钥正则 {len(hits)} 处：{hits[:3]}"

    roots = man.get("evidence_roots") or {}
    assert any(str(k).startswith("@runs/") for k in roots), (
        "manifest.evidence_roots 未登记任何 @runs/ 根")


# ------------------------------------------------------------------ AC-T7-5
def test_ac_t7_5_env_clean():
    ps = subprocess.run(["docker", "ps", "-a", "--format", "{{.Names}}"],
                        capture_output=True, text=True)
    assert ps.returncode == 0, f"docker ps -a 失败：{ps.stderr[-500:]}"
    left = [n for n in ps.stdout.split()
            if "simpraxis" in n.lower() or "sim_praxis" in n.lower()]
    assert not left, f"仍有 sim_praxis 容器残留：{left}"

    smi = subprocess.run(
        ["nvidia-smi", "--query-gpu=memory.used",
         "--format=csv,noheader,nounits"],
        capture_output=True, text=True)
    assert smi.returncode == 0, f"nvidia-smi 失败：{smi.stderr[-500:]}"
    used = [int(x) for x in smi.stdout.split()]
    assert used, "nvidia-smi 没返回显存读数"
    assert max(used) <= 200, f"显存 {max(used)} MiB > 200 MiB"

    dash = _read_json(milestone() / "dashboard_phase27.json")
    mnt = (dash.get("closeout") or {}).get("mnt")
    assert mnt, "看板未记录 `df -h /mnt` 读数"
    assert re.search(r"\d+%", mnt), f"`df -h /mnt` 读数形状异常：{mnt!r}"


# ------------------------------------------------------------------ AC-T7-6
def test_ac_t7_6_three_signs():
    signs = milestone() / "signs"
    head = _head()
    seen = {}
    for role in ("executor", "reviewer", "verifier"):
        p = signs / f"{role}.json"
        assert p.is_file(), f"缺签署件：{p}"
        doc = _read_json(p)
        assert doc.get("schema") == SIGN_SCHEMA, (
            f"{role} schema={doc.get('schema')!r}")
        assert doc.get("role") == role, (
            f"{role} 的 role={doc.get('role')!r} 与文件名不符")
        assert doc.get("verdict") == "APPROVE", (
            f"{role} verdict={doc.get('verdict')!r}：三签必须全 APPROVE")
        assert doc.get("commit") == head, (
            f"{role} 绑定 commit {doc.get('commit')} ≠ HEAD {head}")
        seen[role] = doc.get("verdict")
    assert set(seen) == {"executor", "reviewer", "verifier"}


# ------------------------------------------------------------------ AC-T7-7
def test_ac_t7_7_closure_judge():
    rep = _read_json(milestone() / "closure_judge.json")
    assert rep.get("schema") == CLOSURE_SCHEMA, rep.get("schema")
    assert rep["problems"] == [], (
        "机判 problems 非空：" + "；".join(rep["problems"]))
    gaps = rep.get("gaps") or []
    assert gaps, "gaps 为空：Phase 2.7 存在未达成项，不得洗成通过"
    tags = {g.get("tag") for g in gaps}
    missing = [t for t in REQUIRED_GAP_TAGS if t not in tags]
    assert not missing, f"未达成项未挂牌：{missing}"
    for g in gaps:
        for field in ("tag", "what", "why_not_pass", "owner"):
            assert g.get(field), f"gap {g.get('tag')} 缺字段 {field}"
    pack_sum = rep["checks"]["evidence_pack"]
    assert pack_sum["git_commit"] == _head(), (
        "机判里的证据包 commit 与 HEAD 不符（判决件过期）")

    # 判决件必须与**当前盘上的**证据包/签署件逐字节对齐——只比 commit 会放过
    # 「签完又改了证据包/换了签署件但没重跑机判」的陈旧判决件。
    man = _read_json(milestone() / "evidence_pack" / "manifest.json")
    assert pack_sum["n_files"] == man["n_files"], (
        f"机判记的包件数 {pack_sum['n_files']} ≠ manifest {man['n_files']}")
    assert pack_sum["bytes"] == man["total_bytes"], (
        f"机判记的包字节 {pack_sum['bytes']} ≠ manifest {man['total_bytes']}")

    signs = rep["checks"]["signs"]
    assert set(signs) == {"executor", "reviewer", "verifier"}, (
        f"机判签署件不齐：{sorted(signs)}")
    for role, blk in signs.items():
        p = milestone() / "signs" / f"{role}.json"
        assert p.is_file(), f"缺签署件：{p}"
        assert blk.get("sha256") == _sha256(p), (
            f"机判记的 {role} 签署件 sha 与盘上不符（签后改动未重判）")
        assert blk.get("commit") == _head(), (
            f"机判记的 {role} 绑定 commit {blk.get('commit')} ≠ HEAD")
