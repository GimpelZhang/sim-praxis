"""Phase 2.7 Task0 基座机判（AC-T0-1..AC-T0-9；0 付费、0 联网）。

覆盖：
  1. AC-T0-1：phase27 私目录族 0700、账 0600、写打开 Phase 1/2/2.5/2.6 四本
     封存账**全部**抛 SealedLedgerError；
  2. AC-T0-2：开账自检行 ``seq==1`` / ``event=="selfcheck"`` / ``model is None``
     / ``ok is True`` / ``ts_iso`` 严格 UTC ``Z``；
  3. AC-T0-3：seq 连续性（实呼整数 seq 自 1 起连续）+ ``check_ledger_phase27.py
     --selftest`` rc=0；
  4. AC-T0-4：``estimates/t0_baseline_phase27.json`` 冻结件与 raw log 摘要行
     逐数字相符（冻结件机产，不得手写）；
  5. AC-T0-5：``sealed_ledger_paths()`` 长度 4 且四项分指 phase1/2/25/26 账；
  6. AC-T0-6：``PHASE27_TASK_CAPS == {4: 63, 6: 85}``、硬帽 178、ns.phase == 27；
  7. AC-T0-7：phase27 落位与计数（phase2 账字节数与 mtime 不变、seq 递增、
     global_used_after 非 0、对 phase2 写打开仍必拒）；
  8. AC-T0-8：旧里程碑测试随事实更新后全绿（子进程 pytest）+ C-3 三行改动登记；
  9. AC-T0-9：只有单一收尾命令字面量；``container`` 标记裸跑 deselect（非 skip）。

纪律：除 AC-T0-2 的开账行与 AC-T0-1 的证据件（计划明确要求落 phase27 生产根）
外，一律 monkeypatch 到 tmp 根，绝不写 /mnt 活账。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

from orchestrator.common import runtime_paths as rp
from orchestrator.common.clock import parse_ts_iso_strict
from orchestrator.gateway import quota_ledger as ql

REPO_ROOT = Path(__file__).resolve().parents[2]
# 生产 phase27 根在**模块导入时点**取一次（此时任何 fixture 的 monkeypatch 尚未
# 生效）。AC-T0-1 的证据件按计划必须落生产根，而该用例自身又把根指到 tmp 做
# 隔离验证——两者并存只能靠这个导入期快照。
PROD_P27_ROOT = rp.phase27_root()
STRICT_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")
STAGE = "coding_debug"
BATCH = "P27-T-FIXTURE"
# Task 0 的父本金（不可变 commit）。严禁改回活 HEAD——HEAD 在本 Task 提交后
# 已含 phase27 块，自指必红（同 Phase 2.5/2.6 的 PRE_T0_PARENT 纪律）。
PRE_TASK0_PARENT = "e1bf27781772b7c9f666169ea94a41db2535fda8"
TWO_EXCLUSIONS = "-m 'not paid and not kb_integration'"


@pytest.fixture()
def p27_roots(tmp_path, monkeypatch):
    """五根全部指 tmp（conftest 已把 phase2 指 tmp；这里补全其余四根）。"""
    monkeypatch.setenv("SIMPRAXIS_PHASE1_ROOT", str(tmp_path / "phase1"))
    monkeypatch.setenv("SIMPRAXIS_PHASE2_ROOT", str(tmp_path / "phase2"))
    monkeypatch.setenv("SIMPRAXIS_PHASE25_ROOT", str(tmp_path / "phase25"))
    monkeypatch.setenv("SIMPRAXIS_PHASE26_ROOT", str(tmp_path / "phase26"))
    monkeypatch.setenv("SIMPRAXIS_PHASE27_ROOT", str(tmp_path / "phase27"))
    # 锁内适配（Phase 2.8.1 T0）：phase27 已封存，写改指 phase281。
    monkeypatch.setenv("SIMPRAXIS_PHASE281_ROOT", str(tmp_path / "phase281"))
    monkeypatch.setenv("SIMPRAXIS_GATEWAY_STATE", str(tmp_path / "gwstate"))
    return tmp_path


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _selfcheck_row(seq: int) -> dict:
    """开账 selfcheck 行：占整数 seq、model=null、ok=true、global_used_after=0。"""
    from orchestrator.gateway.quota_ledger import _now_iso  # noqa: PLC0415
    return {"ts_iso": _now_iso(), "event": "selfcheck", "seq": seq,
            "stage": None, "model": None, "thread_id": "", "task": None,
            "batch_id": None, "global_used_after": 0, "ok": True}


def _read_ledger(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# AC-T0-1：目录族 0700 / 账 0600 / 四本封存账写打开必拒
# ---------------------------------------------------------------------------

def test_ac_t0_1_permissions_and_sealed(p27_roots):
    # (0) 生产由 00_env.sh（umask 077 + chmod 700）建根；测试等价复现，
    #     使 0700 断言不依赖调用方 shell 的 umask（不放宽断言，只固定前置态）。
    from orchestrator.common.fsutil import ensure_private_dir  # noqa: PLC0415
    ensure_private_dir(rp.phase27_root())
    # (1) 访问器建出的 phase27 目录族必须严格 0700（tmp 根，绝不碰活账）。
    fam = {
        "root": rp.phase27_root(),
        "quota": rp.phase27_quota_dir(),
        "approvals": rp.phase27_approvals_dir(),
        "gateway_state": rp.phase27_gateway_state_dir(),
        "estimates": rp.phase27_estimates_dir(),
    }
    for name, path in fam.items():
        assert path.is_dir(), f"phase27 {name} 未建出：{path}"
        assert _mode(path) == 0o700, f"phase27 {name} 必须 0700，实际 {_mode(path):o}"
    # runs 是纯路径访问器（同 phase26，不建目录；目录由 00_env.sh 建）。
    runs = rp.phase27_runs_root()
    assert runs == rp.phase27_root() / "runs"
    if runs.exists():
        assert _mode(runs) == 0o700, f"phase27 runs 必须 0700，实际 {_mode(runs):o}"

    # (2) 账本 0600（写打开后仍 0600）。
    # 锁内适配（Phase 2.8.1 T0）：phase27 已封存，写改指 phase281。
    with rp.open_ledger_append(rp.phase281_ledger_path()) as fh:
        fh.write('{"probe": true}\n')
    ledger = rp.phase281_ledger_path()  # 锁内适配（Phase 2.8.1 T0）：写改指 phase281
    assert _mode(ledger) == 0o600, f"phase281 账必须 0600，实际 {_mode(ledger):o}"

    # (3) 五本封存账：任何写打开必抛 SealedLedgerError（含缺省目标=phase26）。
    # 锁内适配（Phase 2.8.1 T0）：phase27 已封存，写改指 phase281。
    for name, path in {
        "phase1": rp.phase1_ledger_path(),
        "phase2": rp.phase2_ledger_path(),
        "phase25": rp.phase25_ledger_path(),
        "phase26": rp.phase26_ledger_path(),
        "phase27": rp.phase27_ledger_path(),
    }.items():
        with pytest.raises(rp.SealedLedgerError):
            rp.open_ledger_append(path)
    with pytest.raises(rp.SealedLedgerError):
        rp.open_ledger_append()          # 缺省目标仍是 phase26（D0-3 不动缺省）

    # (4) 证据：生产根 estimates/ac_t0_1_dir_modes.json（0600）。
    #     锁内适配 L-10：T0.7 规定 ``task7_milestone/`` 在 Task 0 必须为空
    #     （留给 Task 7），故证据件改落 ``estimates/``——判据不变，只换落位。
    ev_dir = ensure_private_dir(PROD_P27_ROOT / "estimates")
    assert PROD_P27_ROOT != rp.phase27_root(), "证据必须落生产根而非 tmp 根"
    snapshot = {"tmp": {name: {"path": str(p), "mode": oct(_mode(p))}
                        for name, p in fam.items()}}
    prod = PROD_P27_ROOT
    snapshot["prod_root"] = {"path": str(prod),
                             "exists": prod.is_dir(),
                             "mode": (oct(_mode(prod)) if prod.is_dir() else None)}
    ev = ev_dir / "ac_t0_1_dir_modes.json"
    payload = json.dumps({"schema": "simpraxis.phase27.ac_t0_1_dir_modes.v1",
                          "dirs": snapshot}, ensure_ascii=False, indent=1) + "\n"
    ev.write_text(payload, encoding="utf-8")
    os.chmod(ev, 0o600)
    assert _mode(ev) == 0o600
    assert json.loads(ev.read_text(encoding="utf-8"))[
        "dirs"]["tmp"]["quota"]["mode"] == "0o700"


# ---------------------------------------------------------------------------
# AC-T0-2：首行 seq=1 严格 Z + 生产根开账 selfcheck 行
# ---------------------------------------------------------------------------

def test_ac_t0_2_selfcheck_row(tmp_path):
    # (a) 临时根：空账首条实呼 seq=1、ts 严格 Z。
    #     用 patch.dict 而非 fixture：本用例第二段必须落**生产根**。
    tmp_roots = {
        "SIMPRAXIS_PHASE1_ROOT": str(tmp_path / "phase1"),
        "SIMPRAXIS_PHASE2_ROOT": str(tmp_path / "phase2"),
        "SIMPRAXIS_PHASE25_ROOT": str(tmp_path / "phase25"),
        "SIMPRAXIS_PHASE26_ROOT": str(tmp_path / "phase26"),
        "SIMPRAXIS_PHASE27_ROOT": str(tmp_path / "phase27"),
        "SIMPRAXIS_PHASE281_ROOT": str(tmp_path / "phase281"),  # 锁内适配（Phase 2.8.1 T0）
        "SIMPRAXIS_GATEWAY_STATE": str(tmp_path / "gwstate"),
    }
    with mock.patch.dict(os.environ, tmp_roots):
        led = ql.QuotaLedger.phase281()  # 锁内适配（Phase 2.8.1 T0）：写改指 phase281
        row = led.record(STAGE, task=4, batch_id=BATCH, ok=True,
                         model="fixture-model", thread_id="t-ac2")
        assert row["seq"] == 1 and row["event"] == "call"
        parse_ts_iso_strict(row["ts_iso"])
        assert STRICT_Z.match(row["ts_iso"]), row["ts_iso"]
        assert (tmp_path / "phase281" / "quota" / "ledger.jsonl").exists()
    # 开账行占 seq=1 后，生产账的下一条实呼必须从 seq=2 起。
    assert rp.phase281_ledger_path() != tmp_path / "phase281" / "quota" / "ledger.jsonl"  # 锁内适配（Phase 2.8.1 T0）

    # (b) 生产根：开账 selfcheck 行（占 seq=1、不计实呼、永不删除）。
    prod = rp.phase27_ledger_path()
    assert prod.parent == rp.phase27_root() / "quota"
    existing = _read_ledger(prod)
    if not existing:
        with rp.open_ledger_append(prod) as fh:
            fh.write(json.dumps(_selfcheck_row(1), ensure_ascii=False) + "\n")
        existing = _read_ledger(prod)
    first = existing[0]
    assert first["seq"] == 1 and first["event"] == "selfcheck"
    assert STRICT_Z.match(first["ts_iso"]), first["ts_iso"]
    parse_ts_iso_strict(first["ts_iso"])
    assert first["model"] is None and first["ok"] is True
    assert _mode(prod) == 0o600
    # 开账行不计实呼：机判放行 selfcheck，但 call_rows 不算它。
    summary = ql.ledger_summary(existing, allow_events=("selfcheck",))
    assert summary["selfcheck_rows"] == 1
    assert summary["call_rows"] == len(
        [r for r in existing if r.get("event") == "call"])


# ---------------------------------------------------------------------------
# AC-T0-3：seq 连续 + 对账脚本自检
# ---------------------------------------------------------------------------

def test_ac_t0_3_seq_monotonic(p27_roots):
    # 锁内适配（Phase 2.8.1 T0）：phase27 已封存，写改指 phase281。
    led = ql.QuotaLedger.phase281()
    # 开账行先落（模拟生产口径）。
    with rp.open_ledger_append(rp.phase281_ledger_path()) as fh:
        fh.write(json.dumps(_selfcheck_row(1), ensure_ascii=False) + "\n")
    rows = [led.record(STAGE, task=4, batch_id=BATCH, ok=True, model="m",
                       thread_id=f"t{s}") for s in (1, 2, 3)]
    raw = _read_ledger(rp.phase281_ledger_path())  # 锁内适配（Phase 2.8.1 T0）：写改指 phase281
    assert len(raw) == 4
    ints = [r["seq"] for r in raw if isinstance(r.get("seq"), int)]
    assert ints == [1, 2, 3, 4], ints          # selfcheck 占 1，实呼 2..4
    assert [r["seq"] for r in rows] == [2, 3, 4]
    ql.validate_ledger_rows(raw, allow_events=("selfcheck",))
    s = ql.ledger_summary(raw, allow_events=("selfcheck",))
    assert (s["selfcheck_rows"], s["call_rows"], s["total_rows"]) == (1, 3, 4)
    assert (s["seq_first"], s["seq_last"]) == (1, 4)
    # null-seq blocked 行单列对账（不占实呼 seq）。
    blocked = dict(_selfcheck_row(1))
    blocked.update({"seq": None, "event": "preflight_blocked", "ok": False,
                    "global_used_after": 3})
    promoted = raw + [blocked]
    s2 = ql.ledger_summary(promoted, allow_events=("selfcheck",))
    assert s2["blocked_rows"] == 1 and s2["call_rows"] == 3

    # 对账脚本自检（0 联网、自建 tmp 根）。
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "phase27"
                             / "check_ledger_phase27.py"), "--selftest"],
        cwd=REPO_ROOT, text=True, capture_output=True)
    assert proc.returncode == 0, f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"


# ---------------------------------------------------------------------------
# AC-T0-4：基线冻结件与 raw log 一致
# ---------------------------------------------------------------------------

def test_ac_t0_4_baseline_json_frozen():
    root = rp.phase27_root() / "estimates"
    path = root / "t0_baseline_phase27.json"
    assert path.exists(), f"缺少基线冻结件：{path}（先跑全量再确定性生成）"
    d = json.loads(path.read_text(encoding="utf-8"))
    assert d["schema"] == "simpraxis.phase27.t0_baseline.v1"
    assert re.fullmatch(r"[0-9a-f]{40}", d["git_head"]), d["git_head"]
    assert d["cmd"] == ("pytest tests -q -k 'not test_04' "
                        "-m 'not paid and not kb_integration'")
    for k in ("passed", "skipped", "deselected"):
        assert isinstance(d[k], int) and d[k] >= 0, (k, d.get(k))
    assert isinstance(d["duration_s"], float) and d["duration_s"] > 0
    # raw log 必须在场，且其摘要行与冻结件逐数字相符（冻结件不得手写）。
    raw_path = rp.phase27_root() / d["raw_log_relpath"]
    assert raw_path.exists(), raw_path
    raw = raw_path.read_text(encoding="utf-8")
    m = re.search(r"(\d+) passed, (\d+) skipped, (\d+) deselected.*?in ([\d.]+)s",
                  raw)
    assert m, "raw log 摘要行未匹配"
    assert (int(m.group(1)), int(m.group(2)), int(m.group(3))) == (
        d["passed"], d["skipped"], d["deselected"])
    assert abs(float(m.group(4)) - d["duration_s"]) < 1e-6
    # 与 Phase 2.6 收官基线（2198/7/51）不同则必须带 note 说明差异来源。
    if (d["passed"], d["skipped"], d["deselected"]) != (2198, 7, 51):
        assert d.get("note"), "计数与上一里程碑收官基线不同时必须写 note 说明来源"


# ---------------------------------------------------------------------------
# AC-T0-5：封存集长度 4 且分指四账
# ---------------------------------------------------------------------------

def test_ac_t0_5_sealed_set_size(p27_roots):
    sealed = rp.sealed_ledger_paths()
    assert isinstance(sealed, frozenset)
    # 锁内适配（Phase 2.8.1 T0）：phase27 已封存，封存集 4→5（只多加一期）。
    assert len(sealed) == 5, sorted(str(p) for p in sealed)
    assert sealed == frozenset({
        rp.phase1_ledger_path().resolve(strict=False),
        rp.phase2_ledger_path().resolve(strict=False),
        rp.phase25_ledger_path().resolve(strict=False),
        rp.phase26_ledger_path().resolve(strict=False),
        rp.phase27_ledger_path().resolve(strict=False),
    })
    # phase281 自身不在封存集（它是当前写入者）。
    assert rp.phase281_ledger_path().resolve(strict=False) not in sealed  # 锁内适配（Phase 2.8.1 T0）
    for path in sealed:
        with pytest.raises(rp.SealedLedgerError):
            rp.open_ledger_append(path)


# ---------------------------------------------------------------------------
# AC-T0-6：预算常量与命名空间
# ---------------------------------------------------------------------------

def test_ac_t0_6_budget_constants(p27_roots):
    assert dict(ql.PHASE27_TASK_CAPS) == {4: 63, 6: 85}
    assert ql.PHASE27_HARD_CAP == 178
    assert ql.PHASE27_NORMAL_BUDGET == 178
    assert ql.LEDGER_PHASE27 == 27
    ns = ql.QuotaLedger.phase27().ns
    assert ns.phase == 27
    assert ns.hard_cap == 178 and ns.normal_budget == 178
    assert dict(ns.task_caps) == {4: 63, 6: 85}
    # 三个信封常数自洽：148 是受账下限，178 = 148 + 30 缓冲（BR-6）。
    assert sum(ql.PHASE27_TASK_CAPS.values()) == 148
    assert ql.PHASE27_HARD_CAP - sum(ql.PHASE27_TASK_CAPS.values()) == 30
    # 六本命名空间互不并表。
    assert {ql.PHASE2_NAMESPACE.phase, ql.PHASE25_NAMESPACE.phase,
            ql.PHASE26_NAMESPACE.phase, ql.PHASE27_NAMESPACE.phase} == \
        {2, 25, 26, 27}


# ---------------------------------------------------------------------------
# AC-T0-7：phase27 落位与计数（封 BLOCKER-2）
# ---------------------------------------------------------------------------

def test_ac_t0_7_phase27_record_lands(p27_roots):
    # phase2 历史账先造一条（模拟既有关账），再快照字节与 mtime。
    p2 = rp.phase2_ledger_path()
    p2.parent.mkdir(parents=True, exist_ok=True)
    p2.write_text('{"seq": 1, "event": "call", "global_used_after": 1}\n',
                  encoding="utf-8")
    p2_before = p2.read_bytes()
    p2_mtime = p2.stat().st_mtime_ns

    led = ql.QuotaLedger.phase281()  # 锁内适配（Phase 2.8.1 T0）：写改指 phase281
    r1 = led.record(STAGE, task=4, batch_id=BATCH, ok=True, model="m",
                    thread_id="t-a")
    r2 = led.record(STAGE, task=4, batch_id=BATCH, ok=False, model="m",
                    thread_id="t-b", err="fixture failure")
    p27 = rp.phase281_ledger_path()  # 锁内适配（Phase 2.8.1 T0）：写改指 phase281
    landed = _read_ledger(p27)
    # ① 行落在 phase281 账；② phase2 账字节数与 mtime 一字不差。
    assert [r["seq"] for r in landed] == [1, 2]
    assert [r["thread_id"] for r in landed] == ["t-a", "t-b"]
    assert p2.read_bytes() == p2_before
    assert p2.stat().st_mtime_ns == p2_mtime
    # ③ seq 递增、global_used_after 非 0（硬帽口径未静默失效）。
    assert (r1["seq"], r2["seq"]) == (1, 2)
    assert r1["global_used_after"] == 1 and r2["global_used_after"] == 2
    s = ql.ledger_summary(landed, allow_events=("selfcheck",))
    assert (s["call_rows"], s["seq_first"], s["seq_last"]) == (2, 1, 2)
    assert landed[-1]["global_used_after"] == 2
    # ④ 对 phase2 账写打开仍抛 SealedLedgerError（证明 _append 未绕守卫）。
    with pytest.raises(rp.SealedLedgerError):
        rp.open_ledger_append(p2)
    # ⑤ 防退化：phase281 行绝不串写进 phase2 账。
    assert b"t-a" not in p2.read_bytes()


# ---------------------------------------------------------------------------
# AC-T0-8：旧里程碑测试随事实更新后全绿（封 BLOCKER-1）
# ---------------------------------------------------------------------------

def test_ac_t0_8_legacy_tests_flipped():
    files = ["tests/milestone25/test_task0_foundation.py",
             "tests/milestone26/test_task0_foundation.py"]
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", *files, "-q",
         "-p", "no:cacheprovider"],
        cwd=REPO_ROOT, text=True, capture_output=True,
        env={**os.environ, "SIMPRAXIS_ALLOW_PAID": "0"})
    assert proc.returncode == 0, (
        f"旧里程碑测试红：rc={proc.returncode}\n"
        f"STDOUT:\n{proc.stdout[-4000:]}\nSTDERR:\n{proc.stderr[-2000:]}")

    def _diff_lines(path: str) -> tuple[list[str], list[str]]:
        out = subprocess.run(
            ["git", "diff", "--unified=0", PRE_TASK0_PARENT, "--", path],
            cwd=REPO_ROOT, text=True, capture_output=True, check=True).stdout
        added = [ln[1:] for ln in out.splitlines()
                 if ln.startswith("+") and not ln.startswith("+++")]
        removed = [ln[1:] for ln in out.splitlines()
                   if ln.startswith("-") and not ln.startswith("---")]
        return added, removed

    # C-3 表三行各自确有改动（事实翻转 + 期望集扩展），**逐文件**判定。
    a26, r26 = _diff_lines(files[1])
    # 旧的反向断言必须被删（"not in sealed"），且现行文本里 phase26 已并入封存环。
    assert any("phase26_ledger_path()" in r and " not in sealed" in r
               for r in r26), "milestone26 未删旧的反向断言"
    t26 = (REPO_ROOT / files[1]).read_text(encoding="utf-8")
    # 锁内适配（Phase 2.8.1 T0，计划 §4.7 S15）：封存循环现在多了第三项
    # phase27，旧的字面等式（以 "):" 收尾）已不成立，故放宽为**不含 "):" 的
    # 同一子串精确匹配**——仍是逐字子串，不是通配/正则放水；并新增一条断言
    # 证明 phase27 确实进了封存循环。
    assert "rp.phase25_ledger_path(), rp.phase26_ledger_path()" in t26, \
        "milestone26 封存环未含 phase26"
    assert "rp.phase27_ledger_path()" in t26, \
        "milestone26 封存环未扩 phase27（锁内适配（Phase 2.8.1 T0））"
    assert "assert p.resolve(strict=False) in sealed, p" in t26, \
        "milestone26 封存环未断言 in sealed"
    a25, r25 = _diff_lines(files[0])
    assert any("phase26_ledger_path()" in a and " in sealed" in a
               for a in a25), "milestone25 封存反向断言未翻正"
    assert any("phase26_ledger_path()" in r and " not in sealed" in r
               for r in r25), "milestone25 未删旧的反向断言"
    assert any('"d 700 phase27"' in a for a in a25), \
        "milestone25 tree-runs 期望集未扩 phase27"
    assert any("SIMPRAXIS_PHASE27_ROOT" in a for a in a25), \
        "milestone25 env 期望集未扩 SIMPRAXIS_PHASE27_ROOT"
    # 锁内适配 L-5：phase26 已封存，原三例行为验证改指活跃账 phase27（断言强度
    # 不变，仅相位帽随本期常量对齐）。
    assert any("def test_phase27_" in a for a in a25), \
        "milestone25 未按 L-5 把 phase26 写入类用例改指 phase27"
    # 不放水：新增行里不得出现 skip/xfail。
    for line in a25 + a26:
        lowered = line.lower()
        assert "skip" not in lowered and "xfail" not in lowered, \
            f"旧测试改动里出现 skip/xfail：{line.strip()}"


# ---------------------------------------------------------------------------
# AC-T0-9：单一收尾命令字面量 + container 裸跑 deselect（封 MINOR-4）
# ---------------------------------------------------------------------------

def test_ac_t0_9_single_exclusion_literal():
    # ① 全仓命中「两排除」字面量的行里，不得再挂第三项。
    proc = subprocess.run(
        ["grep", "-rn", "--include=*.py", "--include=*.sh", "--include=*.md",
         "not paid and not kb_integration", "."],
        cwd=REPO_ROOT, text=True, capture_output=True)
    assert proc.returncode == 0, "两排除命令字面量在仓内消失了"
    for line in proc.stdout.splitlines():
        assert "and not container" not in line, f"出现了第三项排除：{line}"

    # ②/③ container 标记：裸跑 deselect（不出现在 passed/failed/skipped），
    #      设 SIMPRAXIS_CONTAINER=1 时被收集并执行。
    probe_dir = REPO_ROOT / "tests" / "_p27_ac_t0_9_probe"
    probe = probe_dir / "test_container_probe.py"
    probe_dir.mkdir(parents=True, exist_ok=True)
    probe.write_text(
        '"""合成探针：验证 conftest 的 container 收集期 deselect。"""\n'
        "import pytest\n\n\n"
        "@pytest.mark.container\n"
        "def test_container_only():\n    assert True\n\n\n"
        "def test_plain():\n    assert True\n", encoding="utf-8")
    try:
        env = {k: v for k, v in os.environ.items()
               if k != "SIMPRAXIS_CONTAINER"}
        env["SIMPRAXIS_ALLOW_PAID"] = "0"
        bare = subprocess.run(
            [sys.executable, "-m", "pytest", str(probe), "-q",
             "-p", "no:cacheprovider"],
            cwd=REPO_ROOT, text=True, capture_output=True, env=env)
        out = bare.stdout
        assert bare.returncode == 0, f"STDOUT:\n{out}\nSTDERR:\n{bare.stderr}"
        m = re.search(r"(\d+) passed", out)
        assert m and int(m.group(1)) == 1, out
        m = re.search(r"(\d+) deselected", out)
        assert m and int(m.group(1)) == 1, out
        assert "skipped" not in out or "0 skipped" in out, out

        env_on = {**env, "SIMPRAXIS_CONTAINER": "1"}
        on = subprocess.run(
            [sys.executable, "-m", "pytest", str(probe), "-q",
             "-p", "no:cacheprovider"],
            cwd=REPO_ROOT, text=True, capture_output=True, env=env_on)
        assert on.returncode == 0, f"STDOUT:\n{on.stdout}\nSTDERR:\n{on.stderr}"
        m = re.search(r"(\d+) passed", on.stdout)
        assert m and int(m.group(1)) == 2, on.stdout
    finally:
        shutil.rmtree(probe_dir, ignore_errors=True)
    assert not probe_dir.exists(), "合成探针目录未清理"
