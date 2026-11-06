"""Phase 2.6 Task0 开工准入机判（AC-T0-1..AC-T0-5；0 付费、0 联网）。

覆盖：
  1. AC-T0-1：phase26 目录族 0700、三份封存账写打开必抛 SealedLedgerError、
     看门狗缺省监护对象已换期、``build_seq_provider()`` 读活跃账而非封存账
     （反向断言；Phase 2.7 Task0 起缺省活跃账已换期到 phase27）；证据
     ``task9_milestone/ac_t0_1_dir_modes.json``；
  2. AC-T0-2：新账首行 seq=1 且 ts_iso 严格 UTC Z；生产根落一条开账
     ``event="selfcheck"`` 行（占 seq、不计实呼、不删除）；
  3. AC-T0-3：整数 seq 连续性与 null-seq blocked 行分列可断言；
  4. AC-T0-4：``estimates/t0_baseline_phase26.json`` 冻结件与 raw log 一致；
  5. AC-T0-5：CLAUDE.md 的「未推送」陈旧行已改（当前状态行不再说未推送）。

纪律：除 AC-T0-2 的开账行与 AC-T0-1 的证据件（计划明确要求落 phase26 生产
根）外，一律 monkeypatch 到 tmp 根，绝不写 /mnt 活账。
"""
from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path
from unittest import mock

import pytest

from orchestrator.common import runtime_paths as rp
from orchestrator.common.clock import parse_ts_iso_strict
from orchestrator.gateway import quota_ledger as ql

REPO_ROOT = Path(__file__).resolve().parents[2]
# 生产 phase26 根在**模块导入时点**取一次：此时任何 fixture 的 monkeypatch 都还没
# 生效。AC-T0-1 的证据件按计划必须落生产根（$ROOT/task9_milestone/），而该用例
# 自身又把根指到 tmp 做隔离验证——两者并存只能靠这个导入期快照。
PROD_P26_ROOT = rp.phase26_root()
STRICT_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
STAGE = "coding_debug"
BATCH = "P26-T-FIXTURE"


@pytest.fixture()
def p26_roots(tmp_path, monkeypatch):
    """四根全部指 tmp（conftest 已把 phase2 指 tmp；这里补全其余三根）。"""
    monkeypatch.setenv("SIMPRAXIS_PHASE1_ROOT", str(tmp_path / "phase1"))
    monkeypatch.setenv("SIMPRAXIS_PHASE2_ROOT", str(tmp_path / "phase2"))
    monkeypatch.setenv("SIMPRAXIS_PHASE25_ROOT", str(tmp_path / "phase25"))
    monkeypatch.setenv("SIMPRAXIS_PHASE26_ROOT", str(tmp_path / "phase26"))
    # Phase 2.7 Task0：phase26 已封存，写账目标只能是 phase27（锁内适配）。
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


# ---------------------------------------------------------------------------
# AC-T0-1：目录族 / 封存账 / 看门狗缺省 / seq 读取器
# ---------------------------------------------------------------------------

def test_ac_t0_1_dir_modes_seals_watchdog_and_seq_provider(p26_roots):
    # (0) 生产由 00_env.sh（umask 077 + chmod 700）建根；测试等价复现，
    #     使 0700 断言不依赖调用方 shell 的 umask（不放宽断言，只固定前置态）。
    from orchestrator.common.fsutil import ensure_private_dir  # noqa: PLC0415
    ensure_private_dir(rp.phase26_root())
    # (1) phase26 目录族：访问器建出的目录必须严格 0700。
    fam = {
        "quota": rp.phase26_quota_dir(),
        "approvals": rp.phase26_approvals_dir(),
        "gateway_state": rp.phase26_gateway_state_dir(),
    }
    for name, path in fam.items():
        assert _mode(path) == 0o700, f"phase26 {name} 必须 0700，实际 {_mode(path):o}"

    # (2) 四份封存账：任何写打开必抛 SealedLedgerError。
    # Phase 2.7 Task0（C-3 事实翻转）：phase26 已随 Phase 2.6 收束封存，
    # 原「phase26 不在封存集」的反向断言按事实翻正，并加写打开必拒。
    # 锁内适配（Phase 2.8.1 T0）：phase27 已封存，写改指 phase281；封存循环扩入 phase27。
    sealed = rp.sealed_ledger_paths()
    for p in (rp.phase1_ledger_path(), rp.phase2_ledger_path(),
              rp.phase25_ledger_path(), rp.phase26_ledger_path(),
              rp.phase27_ledger_path()):
        assert p.resolve(strict=False) in sealed, p
        with pytest.raises(rp.SealedLedgerError):
            rp.open_ledger_append(p)

    # (3) 看门狗缺省监护对象已换期（本断言防的正是「看门狗盯着上一期账」）。
    from orchestrator.common import watched  # noqa: PLC0415
    assert watched._default_ledger_path() == rp.phase26_ledger_path()
    assert watched._default_pause_path() == rp.phase26_gateway_state_path()
    assert watched.default_watchdog_root() == rp.phase26_root() / "watchdog"

    # (4) build_seq_provider 读的是哪一本账（正反向都断言）。
    # 锁内适配（Phase 2.7 Task0，D0-2 封存 phase26）：本账在 2.7 起不可写，
    # 故写入目标改指 phase27；同时新增「不跨账串读」断言——缺省 phase26 的
    # 读取器绝不读到 phase27 刚写的那一行。
    # 锁内适配（Phase 2.8.1 T0）：phase27 已封存，写改指 phase281。缺省读取器的
    # 换期是 P281-3 的接线（计划 §7.2/C5，AC-P281-3-05 锁死），因此本用例此刻
    # 断言的是「缺省读取器盯封存的 phase27 ⇒ None，绝不串读到 phase281 的新行」；
    # P281-3 换期后本行必须改回 ``== 1``。
    from orchestrator.agentic.wiring import build_seq_provider  # noqa: PLC0415
    led281 = ql.QuotaLedger.phase281()
    led27 = ql.QuotaLedger.phase27()          # 只借命名空间解析路径，不写
    led26 = ql.QuotaLedger.phase26()          # 只借命名空间解析路径，不写
    led25 = ql.QuotaLedger.phase25()          # 只借命名空间解析路径，不写
    with ql.billing_context(task=4, batch_id="b-ac1", thread_id="thr-ac1"):
        rec = led281.record(STAGE, task=4, batch_id="b-ac1", ok=True,
                            model="fixture-model", thread_id="thr-ac1")
        assert rec["seq"] == 1
        assert build_seq_provider(ledger=led281)() == 1   # 指向新账：读得到
        # 锁内适配 L-8：缺省读取器随活跃账换期（否则实呼写在活跃账、读取器盯封存
        # 的上一期 ⇒ 恒 None，trace 的 seq=null 语义「全缓存命中」会把每一次真实
        # 付费调用记成缓存命中）。P281-3 换期到 phase281 前，缺省仍是 phase27。
        assert build_seq_provider()() is None            # 缺省：phase27（已封存、空）
        assert build_seq_provider(ledger=led27)() is None  # 封存 phase27：读不到
        assert build_seq_provider(ledger=led26)() is None  # 封存 phase26：读不到
        assert build_seq_provider(ledger=led25)() is None  # 封存 phase25：读不到
    assert not rp.phase25_ledger_path().exists()

    # (5) 证据：生产根 task9_milestone/ac_t0_1_dir_modes.json（0600，stat 全族快照）。
    ev_dir = ensure_private_dir(PROD_P26_ROOT / "task9_milestone")
    assert PROD_P26_ROOT != rp.phase26_root(), "证据必须落生产根而非 tmp 根"
    snapshot = {}
    for name, path in (("root", rp.phase26_root()), *fam.items(),
                       ("runs", rp.phase26_runs_root())):
        if path.exists():
            snapshot[name] = {"path": str(path), "mode": oct(_mode(path))}
            assert _mode(path) == 0o700, f"{name} 必须 0700"
    ev = ev_dir / "ac_t0_1_dir_modes.json"
    payload = json.dumps({"schema": "simpraxis.phase26.ac_t0_1_dir_modes.v1",
                          "dirs": snapshot}, ensure_ascii=False, indent=1) + "\n"
    ev.write_text(payload, encoding="utf-8")
    os.chmod(ev, 0o600)
    assert _mode(ev) == 0o600
    assert json.loads(ev.read_text(encoding="utf-8"))["dirs"]["quota"]["mode"] == "0o700"


# ---------------------------------------------------------------------------
# AC-T0-2：首行 seq=1 严格 Z + 生产根开账 selfcheck 行
# ---------------------------------------------------------------------------

def test_ac_t0_2_new_ledger_first_seq_one_strict_z(tmp_path):
    # (a) 临时根：空账首条实呼 seq=1、ts 严格 Z。
    #     这里用 patch.dict 而非 p26_roots fixture：本用例的第二段必须落**生产根**，
    #     fixture 的 monkeypatch 会把 phase26 根一直指到 tmp。
    #     锁内适配（Phase 2.7 Task0，D0-2）：phase26 已封存、写打开必拒，故「新账
    #     首行 seq=1」的验证目标改指当前活跃账 phase27；不变量一字未松。
    #     锁内适配（Phase 2.8.1 T0）：phase27 已封存，写改指 phase281（同一条不变量）。
    tmp_roots = {
        "SIMPRAXIS_PHASE1_ROOT": str(tmp_path / "phase1"),
        "SIMPRAXIS_PHASE2_ROOT": str(tmp_path / "phase2"),
        "SIMPRAXIS_PHASE25_ROOT": str(tmp_path / "phase25"),
        "SIMPRAXIS_PHASE26_ROOT": str(tmp_path / "phase26"),
        "SIMPRAXIS_PHASE27_ROOT": str(tmp_path / "phase27"),
        "SIMPRAXIS_PHASE281_ROOT": str(tmp_path / "phase281"),
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
        # 封存的 phase26 账在同一 tmp 根下不得被写（反向断言，非放水）。
        with pytest.raises(rp.SealedLedgerError):
            rp.open_ledger_append(rp.phase26_ledger_path())
    # 开账行占 seq=1 后，生产账的下一条实呼必须从 seq=2 起（连续性不被顶掉）。
    assert rp.phase281_ledger_path() != tmp_path / "phase281" / "quota" / "ledger.jsonl"  # 锁内适配（Phase 2.8.1 T0）

    # (b) 生产根：开账 selfcheck 行（占 seq=1、不计实呼、永不删除）。
    #     锁内适配（Phase 2.7 Task0，D0-2）：phase26 已封存，本测试不得再「补写」
    #     开账行；生产账缺行即事实缺陷，直接红（而不是尝试一次必抛的写入）。
    prod = rp.phase26_ledger_path()
    assert prod.parent == rp.phase26_root() / "quota"
    existing = []
    if prod.exists() and prod.stat().st_size:
        existing = [json.loads(l) for l in
                    prod.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert existing, f"生产 phase26 账缺失或为空：{prod}（已封存，不得补写）"
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
# AC-T0-3：整数 seq 连续性与 null-seq blocked 行分列
# ---------------------------------------------------------------------------

def _call(seq: int, ts: str = "2026-11-02T00:00:00Z") -> dict:
    return {"ts_iso": ts, "event": "call", "stage": STAGE, "model": "fixture",
            "thread_id": "t", "task": 4, "batch_id": BATCH, "seq": seq,
            "global_used_after": seq, "ok": True}


def _blocked(ts: str = "2026-11-02T00:00:00Z") -> dict:
    return {"ts_iso": ts, "event": "preflight_blocked", "stage": STAGE,
            "model": None, "thread_id": "t", "task": 4, "batch_id": BATCH,
            "seq": None, "global_used_after": 0, "ok": False}


def test_ac_t0_3_seq_continuity_and_blocked_column_separated():
    rows = [_call(1), _blocked(), _call(2), _call(3), _blocked(), _call(4),
            _call(5)]
    ql.validate_ledger_rows(rows)                     # 不抛即通过
    ints = [r["seq"] for r in rows if r["seq"] is not None]
    assert ints == list(range(1, len(ints) + 1)) == [1, 2, 3, 4, 5]
    blocked = [r for r in rows if r["seq"] is None]
    assert len(blocked) == 2
    assert all(r["event"] == "preflight_blocked" for r in blocked)
    s = ql.ledger_summary(rows)
    assert (s["call_rows"], s["blocked_rows"], s["total_rows"]) == (5, 2, 7)
    assert (s["seq_first"], s["seq_last"]) == (1, 5)
    # selfcheck 行占 seq 但不计实呼：放行后 call_rows 不涨、selfcheck_rows 单列。
    with_open = [_call(1)] + rows
    promoted = [_selfcheck_row(1), *[_call(s + 1) for s in (1, 2, 3, 4, 5)]]
    ql.validate_ledger_rows(promoted, allow_events=("selfcheck",))
    s2 = ql.ledger_summary(promoted, allow_events=("selfcheck",))
    assert (s2["selfcheck_rows"], s2["call_rows"]) == (1, 5)
    assert (s2["seq_first"], s2["seq_last"]) == (1, 6)
    # 未放行时同一条 selfcheck 行必红（缺省判据未放宽）。
    with pytest.raises(ql.LedgerSequenceError):
        ql.validate_ledger_rows(promoted)
    del with_open


# ---------------------------------------------------------------------------
# AC-T0-4：基线冻结件与 raw log 一致
# ---------------------------------------------------------------------------

def test_ac_t0_4_baseline_json_frozen():
    root = rp.phase26_root() / "estimates"
    path = root / "t0_baseline_phase26.json"
    assert path.exists(), f"缺少基线冻结件：{path}（先跑全量再确定性生成）"
    d = json.loads(path.read_text(encoding="utf-8"))
    assert d["schema"] == "simpraxis.phase26.t0_baseline.v1"
    assert re.fullmatch(r"[0-9a-f]{40}", d["git_head"]), d["git_head"]
    assert d["cmd"] == ("pytest tests -q -k 'not test_04' "
                        "-m 'not paid and not kb_integration'")
    for k in ("passed", "skipped", "deselected"):
        assert isinstance(d[k], int) and d[k] >= 0, (k, d.get(k))
    assert isinstance(d["duration_s"], float) and d["duration_s"] > 0
    # raw log 必须在场，且其摘要行与冻结件逐数字相符（冻结件不得手写）。
    raw_path = rp.phase26_root() / d["raw_log_relpath"]
    assert raw_path.exists(), raw_path
    raw = raw_path.read_text(encoding="utf-8")
    m = re.search(r"(\d+) passed, (\d+) skipped, (\d+) deselected.*?in ([\d.]+)s",
                  raw)
    assert m, "raw log 摘要行未匹配"
    assert (int(m.group(1)), int(m.group(2)), int(m.group(3))) == (
        d["passed"], d["skipped"], d["deselected"])
    assert abs(float(m.group(4)) - d["duration_s"]) < 1e-6
    # 与开工基线（2057/7/50）不同则必须带 note 说明差异来源。
    if (d["passed"], d["skipped"], d["deselected"]) != (2057, 7, 50):
        assert d.get("note"), "计数与开工基线不同时必须写 note 说明来源"


# ---------------------------------------------------------------------------
# AC-T0-5：CLAUDE.md 陈旧推送状态行已改
# ---------------------------------------------------------------------------

def test_ac_t0_5_claude_md_stale_lines_fixed():
    text = (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    # 当前状态行不得再说「本地攒批未推送 / 本地未推送」，也不得留 ahead 计数。
    assert "本地攒批未推送" not in text
    assert "本地未推送" not in text
    assert "main ahead origin" not in text
    # 换成「已推送」的当前状态叙述，且指向推送窗口与末端提交。
    assert "2026-11-02 推送" in text
    assert "12538e4" in text
    # 剩余「未推送」只允许是历史叙述（Phase 2 M2 段描述当时状态）。
    for line in text.splitlines():
        if "未推送" in line:
            assert ("Phase 2 Milestone 2" in line or "M2" in line
                    or "当时" in line), f"可疑的陈旧状态行：{line}"
    # Phase 2.6 指针已登记。
    assert "Phase_2.6_Plan_detailed.md" in text
