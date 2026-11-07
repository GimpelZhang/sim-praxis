"""Phase 2.8.1 P281-7 Pilot —— AC-P281-7-01..10（计划 §11.6）。

两层，与 P281-6 同一条纪律（**不许 skip 放水**）：

* **合成层**（默认两排除套就跑）：三份批准件的**形状**、无凭据时 preflight 拦截
  且**不触网**、账本 seq 连续/失败占 seq、go/no-go 四条规则的**逐条负例**、
  `pilot_report.json` 的字段集。这些不需要付费产物，跑在 tmp 根上——生产账
  一个字节不碰（每个用例断言生产账前后同尺寸）。
* **活产物层**（`kb_integration` 标记，`SIMPRAXIS_KB_INTEGRATION=1` 才执行）：
  读**真的** Pilot 产物（三份 `P281-B1-*.json`、账本、6 个 `trial.json`、
  `pilot_report.json`）按 §11.6 逐条对账。开关与 ``tests/kb_support`` 同源
  （`require_kb_integration` 的同一个 env），不是第二套闸。

为什么活产物层的「文件不在」是**失败而不是跳过**：这些用例在收尾车道
（显式 `-m kb_integration`）里跑，那时 Pilot 必须已经跑完；文件不在就说明
「本 Task 宣称跑完了，但没有产物」——这正是要抓的假完成。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import stat
from pathlib import Path

import pytest

from orchestrator.benchmark.phase281 import analysis, constants, paths
from orchestrator.common import runtime_paths as rp
from orchestrator.gateway import quota_ledger as ql

#: §11.3 的三票（一 task 一票，各 20，合计 60 = 6 Trial × 10）
PILOT_BATCHES = {1: "P281-B1-1", 2: "P281-B1-2", 3: "P281-B1-3"}
PILOT_APPROVED_PER_TASK = 20
PILOT_TOTAL = 60
LEDGER_PHASE = 281
STAGE = "coding_debug"
STRICT_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
LIVE_GATE = "SIMPRAXIS_KB_INTEGRATION"


# ---------------------------------------------------------------------------
# 夹具与工具
# ---------------------------------------------------------------------------

@pytest.fixture()
def p281_roots(tmp_path, monkeypatch):
    """六根 + 网关态全部指 tmp（与 test_phase281_foundation.p281_roots 同形）。"""
    for env, name in (("SIMPRAXIS_PHASE1_ROOT", "phase1"),
                      ("SIMPRAXIS_PHASE2_ROOT", "phase2"),
                      ("SIMPRAXIS_PHASE25_ROOT", "phase25"),
                      ("SIMPRAXIS_PHASE26_ROOT", "phase26"),
                      ("SIMPRAXIS_PHASE27_ROOT", "phase27"),
                      ("SIMPRAXIS_PHASE281_ROOT", "phase281"),
                      ("SIMPRAXIS_GATEWAY_STATE", "gwstate")):
        monkeypatch.setenv(env, str(tmp_path / name))
    monkeypatch.delenv("SIMPRAXIS_BENCHMARK_ROOT", raising=False)
    return tmp_path


def _prod_ledger() -> Path:
    return Path(rp.DEFAULT_DATA_ROOT) / "phase281" / "quota" / "ledger.jsonl"


def _size(path: Path) -> int:
    return path.stat().st_size if path.exists() else 0


def _live_root() -> Path:
    """活产物根的开关（与 kb_support.require_kb_integration 同一个 env）。"""
    if os.environ.get(LIVE_GATE) != "1":
        pytest.skip(f"需显式 {LIVE_GATE}=1（读活 Pilot 产物，默认 deselect/skip）")
    return paths.phase281_root()


def _live_ledger() -> Path:
    return _live_root() / "quota" / "ledger.jsonl"


def _call_rows(ledger_path: Path) -> "list[dict]":
    return [r for r in ql.read_lines(ledger_path)
            if isinstance(r.get("seq"), int) and not isinstance(r.get("seq"), bool)
            and r.get("event") == "call"]


def _write_trial(root: Path, task_id: str, arm_id: str, rep: int, *,
                 status: str = "FINISHED", birth: str = "BORN",
                 calls: int = 3, verdict: str = "PASS") -> Path:
    """合成一份 Trial 产物 + 评测产物（只用于规则单测）。"""
    d = analysis.trial_dir(root, task_id, arm_id, rep)
    d.mkdir(parents=True, exist_ok=True)
    (d / "trial.json").write_text(json.dumps({
        "status": status, "birth": {"status": birth}, "used_calls": calls,
        "observed": {"calls": calls}, "problems": []}), encoding="utf-8")
    ev = analysis.evaluator_result_path(root, task_id, arm_id, rep)
    ev.parent.mkdir(parents=True, exist_ok=True)
    ev.write_text(json.dumps({"verdict": verdict}), encoding="utf-8")
    return d


def _fill_all(root: Path, **kwargs) -> None:
    for task_id in constants.TASK_IDS:
        for arm_id in constants.ARMS:
            _write_trial(root, task_id, arm_id, 0, **kwargs)


def _empty_ledger(root: Path) -> Path:
    """在根里放一份**空**账本（0 行）。

    H4 之后 ``build_pilot_report`` 缺账本即抛（不再回落读生产账），故每个要出报告的
    合成用例都必须显式把本根的账本写出来——这份「显式」就是 hermetic 本身：合成
    断言永远不会去读真钱数据。
    """
    path = Path(root) / "quota" / "ledger.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# AC-P281-7-01：三份批准件（形状；活产物在下方同号用例）
# ---------------------------------------------------------------------------

def test_ac_p281_7_01_approval_present(p281_roots):
    """合成根写三票 → 逐票字段逐项相符；重复写必拒（不许覆盖）。"""
    ns = ql.QuotaLedger.phase281().ns
    prod_before = _size(_prod_ledger())
    paths_written = []
    for task_no, batch_id in PILOT_BATCHES.items():
        p = ql.write_approval(batch_id=batch_id, task=task_no, stage=STAGE,
                              approved=PILOT_APPROVED_PER_TASK,
                              beyond_normal=False, note="P281-7 合成形状用例",
                              namespace=ns)
        paths_written.append(p)
        doc = json.loads(Path(p).read_text(encoding="utf-8"))
        assert doc["approved"] == PILOT_APPROVED_PER_TASK
        assert not isinstance(doc["approved"], bool)
        assert doc["stage"] == STAGE
        assert doc["task"] == task_no
        # ledger_phase 必须**是整数**（AC 明写：写成字符串即失败）
        assert doc["ledger_phase"] == LEDGER_PHASE
        assert isinstance(doc["ledger_phase"], int) \
            and not isinstance(doc["ledger_phase"], bool)
        assert doc["beyond_normal"] is False
        assert STRICT_Z.match(doc["created_at_iso"]), doc["created_at_iso"]
        assert stat.S_IMODE(Path(p).stat().st_mode) == 0o600
    assert len({p.name for p in paths_written}) == 3
    assert sum(json.loads(p.read_text(encoding="utf-8"))["approved"]
               for p in paths_written) == PILOT_TOTAL
    # 重复写 = 覆盖账面证据，必拒
    with pytest.raises(ql.QuotaError):
        ql.write_approval(batch_id="P281-B1-1", task=1, stage=STAGE, approved=20,
                          beyond_normal=False, note="重复", namespace=ns)
    assert _size(_prod_ledger()) == prod_before, "合成用例写了生产账"


def test_ac_p281_7_01_negative_shape(p281_roots):
    """负例：task 越界 / approved 传 bool 必须拒（不是「警告后写入」）。"""
    ns = ql.QuotaLedger.phase281().ns
    with pytest.raises(ql.QuotaError):
        ql.write_approval(batch_id="P281-B1-9", task=9, stage=STAGE, approved=20,
                          beyond_normal=False, note="越界", namespace=ns)
    with pytest.raises((ql.QuotaError, TypeError)):
        ql.write_approval(batch_id="P281-B1-8", task=1, stage=STAGE, approved=True,
                          beyond_normal=False, note="bool", namespace=ns)


# ---------------------------------------------------------------------------
# AC-P281-7-02：无批准件 → preflight 拦截且**不触网**
# ---------------------------------------------------------------------------

def test_ac_p281_7_02_preflight_blocks(p281_roots):
    from orchestrator.gateway.router import ModelGateway

    calls: "list[str]" = []

    def boom(**kwargs):  # 触网即失败
        calls.append("transport")
        raise AssertionError(f"preflight 放行了未批准批次：{kwargs}")

    ledger = ql.QuotaLedger.phase281()
    gw = ModelGateway(ledger=ledger, transport=boom, max_attempts=1)
    with pytest.raises(ql.GatewayPausedError) as ei:
        with ql.billing_context(task=1, batch_id=PILOT_BATCHES[1], thread_id="t-p7"):
            gw.call(STAGE, [{"role": "user", "content": "ping"}], max_tokens=8)
    assert ei.value.state.get("reason") == "batch_approval_missing"
    assert calls == [], "无批准件时仍然触网了"

    # seq=null 的 preflight_blocked 审计行必须落盘（占批次、不占 seq）
    rows = ql.read_lines(rp.phase281_ledger_path())
    blocked = [r for r in rows if r.get("event") == "preflight_blocked"]
    assert len(blocked) == 1, rows
    assert blocked[0]["seq"] is None
    assert [r for r in rows if isinstance(r.get("seq"), int)] == []

    # 正对照：凭据补齐后同一 preflight 放行（证明拦截原因是凭据本身）
    ql.write_approval(batch_id=PILOT_BATCHES[1], task=1, stage=STAGE,
                      approved=PILOT_APPROVED_PER_TASK, beyond_normal=False,
                      note="P281-7 正对照",
                      namespace=ql.QuotaLedger.phase281().ns)
    ok = ledger.preflight(STAGE, task=1, batch_id=PILOT_BATCHES[1])
    assert ok["batch_approved"] == PILOT_APPROVED_PER_TASK
    # 2.5 起的签收口径：null-seq 的 preflight_blocked **占批次**（uses_batch_occupancy），
    # 故被拦过一次之后 batch_used 是 1 而不是 0——这不是放行漏洞，是审计行的代价。
    assert ok["batch_used"] == 1


# ---------------------------------------------------------------------------
# AC-P281-7-03：账本形状（seq 连续、≤60、selfcheck 占 seq 不计实呼）
# ---------------------------------------------------------------------------

def test_ac_p281_7_03_ledger_shape(p281_roots):
    ledger = ql.QuotaLedger.phase281()
    for i in range(1, 4):
        row = ledger.record(STAGE, task=1, batch_id=PILOT_BATCHES[1], ok=(i != 3),
                            model="fixture-model", thread_id=f"t-{i}",
                            err=None if i != 3 else "synthetic-fail")
        assert row["seq"] == i
    rows = ql.read_lines(rp.phase281_ledger_path())
    ql.validate_ledger_rows(rows)          # seq 必须恰为 1..n，ts 严格 Z
    snap = analysis.ledger_snapshot(rp.phase281_ledger_path())
    assert snap["summary"]["call_rows"] == 3
    assert snap["gaps"] == []
    assert analysis._seq_contiguous(snap["seqs"]) is True
    # 失败也占 seq（AC-09 的合成腿）
    assert [r["ok"] for r in rows] == [True, True, False]
    assert len({r["seq"] for r in rows}) == 3


def test_ac_p281_7_03_seq_gap_detected(p281_roots):
    """负例：断号必须被 `_seq_contiguous` / gaps 抓到（不许静默当连续）。"""
    assert analysis._seq_contiguous([1, 2, 3]) is True
    assert analysis._seq_contiguous([1, 3]) is False
    assert analysis._seq_contiguous([]) is True          # 空账：0 次实呼
    ledger_path = rp.phase281_ledger_path()
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    # 手工行必须带严格 Z 的 ts_iso：账本机判先校验时区，再校验断号——
    # 少一个字段会把「断号」测成「时区格式」。
    ts = "2026-11-07T00:00:00Z"
    ledger_path.write_text(
        json.dumps({"seq": 1, "event": "call", "ok": True, "ts_iso": ts}) + "\n"
        + json.dumps({"seq": 3, "event": "call", "ok": True, "ts_iso": ts}) + "\n",
        encoding="utf-8")
    snap = analysis.ledger_snapshot(ledger_path)
    assert snap["gaps"] == [3]
    assert analysis._seq_contiguous(snap["seqs"]) is False


def _write_batch_ledger(path: Path, batches: "list[str]") -> None:
    """写一份合成账本：``batches[i]`` 决定第 i+1 行的 batch_id（seq 从 1 连号）。

    行字段按活账本的最小形状给（seq/event/ok/ts_iso/batch_id/stage/task/model/
    thread_id）——``ledger_summary`` 只看这些，不补字段会把用例测成 KeyError。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    ts = "2026-11-07T00:00:00Z"
    rows = []
    for i, batch in enumerate(batches, start=1):
        rows.append({"seq": i, "event": "call", "ok": True, "ts_iso": ts,
                     "batch_id": batch, "stage": STAGE, "task": 1,
                     "model": "fixture-model", "thread_id": f"t-{i}"})
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def test_a7_9_ledger_snapshot_scopes_by_batch(tmp_path):
    """重跑批切窗（锁内适配 A7-9）：同账本按 batch_id 分窗，窗内 seq 不从 1 起。"""
    ledger_path = tmp_path / "ledger.jsonl"
    _write_batch_ledger(ledger_path,
                        ["P281-B1-1", "P281-B1-1", "P281-B1-1",
                         "P281-B1b-1", "P281-B1b-1", "P281-B1b-2"])

    whole = analysis.ledger_snapshot(ledger_path)
    assert whole["summary"]["call_rows"] == 6
    assert whole["seqs"] == [1, 2, 3, 4, 5, 6]
    assert whole["batches"] is None          # 缺省口径 = 全账本（首轮行为不变）

    window = analysis.ledger_snapshot(
        ledger_path, batches=["P281-B1b-1", "P281-B1b-2"])
    assert window["summary"]["call_rows"] == 3
    assert window["seqs"] == [4, 5, 6]       # 全局 seq：窗不从 1 起
    assert window["gaps"] == []
    assert window["validation_error"] is None
    assert window["batches"] == ["P281-B1b-1", "P281-B1b-2"]
    # 窗内连续按首号起判；按「从 1 起」判会把每一次重跑批都判成断号。
    assert analysis._seq_contiguous(window["seqs"],
                                   start=window["seqs"][0]) is True
    assert analysis._seq_contiguous(window["seqs"]) is False

    # 空窗 ≠ 全账本：一个 batch_id 都不匹配就是 0 行（不许回退成全账本）
    empty = analysis.ledger_snapshot(ledger_path, batches=["nope"])
    assert empty["summary"]["call_rows"] == 0
    assert empty["seqs"] == []


def test_a7_9_pilot_report_batches_avoid_stop12_false_positive(tmp_path):
    """重跑批的「本轮 ≤60」：不切窗会被首轮行数读成越帽（STOP-12 假阳）。"""
    _fill_all(tmp_path)
    ledger_path = tmp_path / "quota" / "ledger.jsonl"
    # A7-10 起切窗口径还要与 Trial 自报呼数对账，故本轮行数取 6 Trial × 3 呼
    # ＝18（``_fill_all`` 的缺省）；全账 44+18＝62 > 60，不切窗仍是 STOP-12 假阳。
    _write_batch_ledger(ledger_path,
                        ["P281-B1-1"] * 44 + ["P281-B1b-1"] * 18)   # 全账 62 行

    whole = analysis.build_pilot_report(tmp_path, now="2026-11-07T00:00:00Z")
    assert "STOP-12" in whole["stop_open"]
    assert whole["five_gates"]["g281_3"]["ok"] is False

    win = analysis.build_pilot_report(tmp_path, now="2026-11-07T00:00:00Z",
                                      batches=["P281-B1b-1"])
    assert "STOP-12" not in win["stop_open"]
    assert win["five_gates"]["g281_3"]["ok"] is True
    # 字段集不许因为多了口径参数而漂移
    assert frozenset(win) == analysis.REPORT_FIELDS


def test_a7_9_cli_batches_roundtrip(tmp_path):
    """``--batches`` 解析：空值 ⇒ None（全账本）；有值 ⇒ 去空白后的白名单。"""
    assert analysis._parse_batches(None) is None
    assert analysis._parse_batches("") is None
    assert analysis._parse_batches("  ") is None
    assert analysis._parse_batches("P281-B1b-1") == ["P281-B1b-1"]
    assert analysis._parse_batches(" P281-B1b-1 , P281-B1b-2 ,") == \
        ["P281-B1b-1", "P281-B1b-2"]


# ---------------------------------------------------------------------------
# AC-P281-7-04 / 07 / 08：终态、报告字段集、规则可复算（合成腿）
# ---------------------------------------------------------------------------

def test_ac_p281_7_04_all_terminal_synthetic(tmp_path):
    _fill_all(tmp_path)
    rows = analysis.collect_trials(tmp_path)
    assert len(rows) == 6
    assert all(r["present"] for r in rows)
    assert all(r["status"] in analysis.TERMINAL_STATUSES for r in rows)


def test_ac_p281_7_07_pilot_report_synthetic(tmp_path):
    _fill_all(tmp_path)
    _empty_ledger(tmp_path)
    rep = analysis.build_pilot_report(tmp_path, now="2026-11-07T00:00:00Z")
    assert frozenset(rep) == analysis.REPORT_FIELDS
    assert rep["schema"] == analysis.PILOT_REPORT_SCHEMA
    assert rep["go_no_go"] in ("GO", "HOLD")
    assert isinstance(rep["stop_open"], list)
    assert isinstance(rep["reasons"], list)
    assert set(rep["verdicts"]) == {constants.trial_id(t, a, 0)
                                    for t in constants.TASK_IDS
                                    for a in constants.ARMS}
    # 落盘 0600
    out = analysis.write_report(rep, tmp_path / "analysis" / "pilot_report.json")
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    assert stat.S_IMODE(out.parent.stat().st_mode) == 0o700
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert frozenset(doc) == analysis.REPORT_FIELDS
    assert doc["go_no_go"] == rep["go_no_go"]


def test_ac_p281_7_08_rule_reproducible(tmp_path):
    """同一份 Trial 结果重新汇总两次 → 同一判决（规则是数据的外生函数）。"""
    _fill_all(tmp_path)
    _empty_ledger(tmp_path)
    a = analysis.build_pilot_report(tmp_path, now="2026-11-07T00:00:00Z")
    b = analysis.build_pilot_report(tmp_path, now="2026-11-07T00:00:00Z")
    assert a["go_no_go"] == b["go_no_go"]
    assert analysis.decide_go_no_go(a["per_trial"])[0] == a["go_no_go"]


def test_ac_p281_7_08_rules_negative(tmp_path):
    """§11.5 四条规则的负例：每一条都能单独把判决压成 HOLD。"""
    _fill_all(tmp_path)
    rows = analysis.collect_trials(tmp_path)

    # 规则 1：BLOCKED_INFRA ≥ 2
    r1 = [dict(r) for r in rows]
    r1[0]["status"] = r1[1]["status"] = "BLOCKED_INFRA"
    verdict, reasons = analysis.decide_go_no_go(r1)
    assert verdict == "HOLD" and "规则1" in reasons[0]
    # 只有 1 个 BLOCKED_INFRA 不该 HOLD（阈值是 2，不是 1）
    r1b = [dict(r) for r in rows]
    r1b[0]["status"] = "BLOCKED_INFRA"
    assert analysis.decide_go_no_go(r1b)[0] == "GO"

    # 规则 2：无人生还
    r2 = [dict(r) for r in rows]
    for r in r2:
        r["birth_status"] = "NOT_BORN"
    assert analysis.decide_go_no_go(r2)[0] == "HOLD"
    assert "规则2" in analysis.decide_go_no_go(r2)[1][0]

    # 规则 3：全部触帽
    r3 = [dict(r) for r in rows]
    for r in r3:
        r["status"] = "CAP_HIT"
    assert analysis.decide_go_no_go(r3)[0] == "HOLD"
    assert "规则3" in analysis.decide_go_no_go(r3)[1][0]
    # 5/6 触帽仍 GO（规则写的是「全部」）
    r3b = [dict(r) for r in rows]
    for r in r3b[:5]:
        r["status"] = "CAP_HIT"
    assert analysis.decide_go_no_go(r3b)[0] == "GO"

    # 缺件：没有 Trial 产物时**不许**GO（否则「没跑完」会被读成「跑好了」）
    r4 = [dict(r) for r in rows]
    r4[0]["present"] = False
    assert analysis.decide_go_no_go(r4)[0] == "HOLD"


def test_ac_p281_7_07_stop_open_detects(tmp_path):
    """STOP-09（断号）与 STOP-12（越帽）能穿过到报告；否则 G281-4 读不到。"""
    _fill_all(tmp_path)
    ledger_dir = tmp_path / "quota"
    ledger_dir.mkdir(parents=True)
    ts = "2026-11-07T00:00:00Z"
    (ledger_dir / "ledger.jsonl").write_text(
        json.dumps({"seq": 1, "event": "call", "ok": True, "task": 1, "ts_iso": ts}) + "\n"
        + json.dumps({"seq": 9, "event": "call", "ok": True, "task": 1, "ts_iso": ts}) + "\n",
        encoding="utf-8")
    rep = analysis.build_pilot_report(tmp_path, now="2026-11-07T00:00:00Z")
    assert "STOP-09" in rep["stop_open"]
    assert rep["five_gates"]["g281_4"]["ok"] is False


def test_ac_p281_7_07_stop12_threshold_and_selfcheck(tmp_path):
    """M9：STOP-12 真的能进报告，且 selfcheck 开账行**不计**实呼（占 seq 不占钱）。"""
    _fill_all(tmp_path)
    ts = "2026-11-07T00:00:00Z"

    def _rows(calls: int) -> str:
        out = [{"seq": 1, "event": "selfcheck", "ok": True, "ts_iso": ts}]
        for i in range(2, 2 + calls):
            out.append({"seq": i, "event": "call", "ok": True, "ts_iso": ts,
                        "task": 1, "stage": STAGE, "model": "fixture-model",
                        "batch_id": "P281-B1-1"})
        return "".join(json.dumps(r) + "\n" for r in out)

    path = _empty_ledger(tmp_path)
    # 60 次实呼 + 1 行 selfcheck：seq 共 61 行、恰在上限。若把 selfcheck 算进实呼
    # （或把 61 行当 61 呼），这里就会假阳成 STOP-12。
    path.write_text(_rows(60), encoding="utf-8")
    rep = analysis.build_pilot_report(tmp_path, now="2026-11-07T00:00:00Z")
    assert "STOP-12" not in rep["stop_open"]
    assert rep["five_gates"]["g281_3"]["ok"] is True

    # 61 次实呼：越帽必须报出来（STOP-09 不该跟着假阳——seq 仍连续）
    path.write_text(_rows(61), encoding="utf-8")
    rep = analysis.build_pilot_report(tmp_path, now="2026-11-07T00:00:00Z")
    assert "STOP-12" in rep["stop_open"]
    assert "STOP-09" not in rep["stop_open"]
    assert rep["five_gates"]["g281_3"]["ok"] is False


def test_h4_missing_ledger_fails_closed(tmp_path):
    """H4：本根缺账本 ⇒ 抛错，**不要**偷偷去读生产账（否则报告是假合规）。"""
    _fill_all(tmp_path)                      # 有 Trial、无账本
    assert not (tmp_path / "quota" / "ledger.jsonl").exists()
    with pytest.raises(analysis.LedgerMissingError):
        analysis.build_pilot_report(tmp_path, now="2026-11-07T00:00:00Z")


def test_h3_engine_fields_surface_in_per_trial(tmp_path):
    """H3：``trial.json.engine`` 必须出现在 ``per_trial``，否则触帽在报告里不可见。"""
    _fill_all(tmp_path)
    # 把一条 agentic 改写成「引擎级帽」的真实形状：trial.status=FINISHED、
    # problems=[]，但 engine.status=failed / reason=[agentic:token_cap] …
    d = analysis.trial_dir(tmp_path, constants.TASK_IDS[0], "agentic_v1", 0)
    doc = json.loads((d / "trial.json").read_text(encoding="utf-8"))
    doc["engine"] = {"status": "failed",
                     "reason": "[agentic:token_cap] token 帽将被突破：120086/100000"}
    (d / "trial.json").write_text(json.dumps(doc), encoding="utf-8")

    row = [r for r in analysis.collect_trials(tmp_path)
           if r["arm_id"] == "agentic_v1" and r["task_id"] == constants.TASK_IDS[0]][0]
    assert row["status"] == "FINISHED"                    # 引擎帽不改变 Trial 终态
    assert row["engine_status"] == "failed"
    assert "token_cap" in (row["engine_reason"] or "")


def test_m1_non_terminal_trial_holds(tmp_path):
    """M1：存在但没跑完（RUNNING）的 Trial 不许报 GO。"""
    _fill_all(tmp_path)
    rows = analysis.collect_trials(tmp_path)
    assert analysis.decide_go_no_go(rows)[0] == "GO"      # 基线：6/6 终态且有人出生
    rows[0]["status"] = "RUNNING"
    verdict, reasons = analysis.decide_go_no_go(rows)
    assert verdict == "HOLD"
    assert "非终态" in reasons[0]


def test_ac_p281_7_04_terminal_statuses_single_source():
    """L6：``TERMINAL_STATUSES`` 是 harness 状态的镜像，漂移即红（不靠注释自觉）。"""
    from orchestrator.benchmark.phase281 import trial_harness

    assert set(analysis.TERMINAL_STATUSES) == set(trial_harness.STATUSES)


def test_ac_p281_7_07_g281_2_is_a_machine_gate(tmp_path):
    """G281-2 是 §12.2 的**机判门**：探测到就出布尔值，未探测才是 null。

    「未探测 ⇒ null」不是放水，是为了让合成腿不必依赖仓内 LOCK 的当前状态；
    但**探测到 rc!=0 必须 ok=False**——否则 LOCK 被改动会静默通过。
    """
    _fill_all(tmp_path)
    _empty_ledger(tmp_path)
    # 未探测：null（且不许冒充通过）
    rep = analysis.build_pilot_report(tmp_path, now="2026-11-07T00:00:00Z")
    assert rep["five_gates"]["g281_2"]["ok"] is None
    # 通过
    rep = analysis.build_pilot_report(
        tmp_path, now="2026-11-07T00:00:00Z",
        register={"ok": True, "rc": 0, "problems": []})
    assert rep["five_gates"]["g281_2"]["ok"] is True
    # 不通过：rc=2 + problems 必须传进报告
    rep = analysis.build_pilot_report(
        tmp_path, now="2026-11-07T00:00:00Z",
        register={"ok": False, "rc": 2, "problems": ["[verify] PROBLEM LOCK 不在场"]})
    assert rep["five_gates"]["g281_2"]["ok"] is False
    assert rep["five_gates"]["g281_2"]["rc"] == 2
    assert rep["five_gates"]["g281_2"]["problems"]
    # 跑不起来：null（不是 True，也不是 False 的谎）
    rep = analysis.build_pilot_report(
        tmp_path, now="2026-11-07T00:00:00Z",
        register={"ok": None, "rc": None, "problems": [],
                  "error": "FileNotFoundError: python"})
    assert rep["five_gates"]["g281_2"]["ok"] is None


def test_ac_p281_7_07_register_verify_real(tmp_path):
    """真调一次 register --verify（0 付费）并断言它给出了布尔结论。

    不给 LOCK 路径时由 register 自己解析（与用户批准的 LOCK 同一根）；这里只
    要求「能跑且有 rc」，不写死 0——通过与否由活产物用例对**生产 LOCK**判。
    """
    got = analysis.register_verify()
    assert got["ok"] is not None, got
    assert isinstance(got["rc"], int)
    assert got["ok"] is (got["rc"] == 0)
    # 探针必须是只读的：再跑一次结果不变（幂等）
    assert analysis.register_verify()["rc"] == got["rc"]


@pytest.mark.kb_integration
def test_ac_p281_7_01_approval_mapping_live():
    """账本每一行的批准件必须**真的在**且数额相符（A7-6 的审计面）。

    Pilot 实际使用的是**逐 Trial 批准件**（`<trial_id>.json`，`approved=10`），
    而不是 §11.3 写的三张 `P281-B1-*`（逐 Task 各 20，写了但账本没有一行引用）。
    这条用例把「账本行 → 批准件」的映射钉死：以后换回逐 Task 票据时它照样成立，
    但**没有批准件的账本行**会立刻红。
    """
    root = _live_root()
    rows = ql.read_lines(_live_ledger())
    calls = _call_rows(_live_ledger())
    per_task = {}
    for r in calls:
        ref = r.get("approval_ref")
        assert ref, f"seq {r.get('seq')} 没有 approval_ref"
        path = root / ref
        assert path.is_file(), f"seq {r.get('seq')} 指向不存在的批准件 {path}"
        doc = json.loads(path.read_text(encoding="utf-8"))
        assert doc.get("batch_id") == r.get("batch_id"), (doc.get("batch_id"), r)
        assert doc.get("approved") == r.get("batch_approved"), (doc, r)
        assert isinstance(doc.get("approved"), int) and not isinstance(doc.get("approved"), bool)
        per_task[r.get("task")] = per_task.get(r.get("task"), 0) + 1
    assert sum(per_task.values()) == len(calls)
    for task_no, used in per_task.items():
        assert used <= PILOT_APPROVED_PER_TASK, (task_no, used)
    assert len([r for r in rows if r.get("event") == "call"]) == len(calls)


def test_a7_7_gateway_single_attempt():
    """§11.3 钉 `max_attempts=1`：真实运行的网关必须一一对应供应侧请求。

    D4 登记：Pilot 曾用缺省 5（4 条空回答行各内部发了 5 次），账本只记逻辑调用；
    本用例把「一次逻辑调用 = 一次供应侧请求」钉成回归项（A7-7）。
    """
    import importlib.util

    script = Path(__file__).resolve().parents[2] / "scripts" / "benchmark" / "phase281_run_trial.py"
    spec = importlib.util.spec_from_file_location("_p281_run_trial_probe", script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    class _FakeLedger:
        ns = None

    gw = mod._gateway(_FakeLedger())
    assert gw.max_attempts == 1, f"§11.3 要求 max_attempts=1，实得 {gw.max_attempts}"


def test_driver_script_is_thin_and_checks_prereqs():
    """驱动器是薄壳：只调已冻结的入口，不自写判据；缺前置件即拒。"""
    script = Path(__file__).resolve().parents[2] / "scripts" / "benchmark" / "phase281_pilot_driver.sh"
    assert script.is_file()
    assert os.stat(script).st_mode & stat.S_IXUSR, "驱动器必须可执行"
    subprocess.run(["bash", "-n", str(script)], check=True)
    body = script.read_text(encoding="utf-8")
    # 它必须引用冻结臂序与批准件，且**不得**自己写批准件（自签自跑）
    assert "pre_registration/trial_order.json" in body
    assert "P281-B1-" in body
    assert "write_approval" not in body
    # 必须真的调这两个入口（否则「跑过」是空转）
    assert "phase281_run_trial.py" in body and "phase281_evaluate.py" in body
    assert "phase281.analysis" in body
    # G281-2 由分析器实跑登记（§12.2 的机判门，不许留 null 给读者猜）
    assert "--verify-register" in body
    # A7-8：重跑批按 §11.3 的任务票计费，驱动器必须把前缀传给 CLI
    assert "--batch" in body and '"$TICKET_PREFIX-$task_no"' in body


# ---------------------------------------------------------------------------
# A7-8：计费批量身份（D3 对齐 —— trial_id ⇄ 任务票）
# ---------------------------------------------------------------------------

def test_a7_8_ensure_approval_honors_batch_id(p281_roots):
    """票名跟着 ``batch_id`` 走；**已预写的票不覆盖**（先写再跑，不自签自跑）。

    D3 的实质是「账本的批量身份与 §11.3 的任务票不一致」。A7-8 给
    ``run_trial`` 加了可选 ``batch_id``：不传 = 首轮口径（``<trial_id>.json``），
    传了就按票记。本用例把两条分支都钉成回归项。
    """
    from orchestrator.benchmark.phase281 import arm_adapter, constants, trial_harness

    ledger = ql.QuotaLedger.phase281()
    arm = arm_adapter.ARMS["agentic_v1"]
    tid = constants.trial_id("mujoco_contact_push_v1", "agentic_v1", 0)
    approvals = Path(ledger.ns.approvals_dir_fn())

    # 分支一（首轮口径）：按 trial_id 落件，帽 = 该臂的 call_cap
    trial_harness._ensure_approval(ledger, trial_id=tid, arm=arm)
    doc = json.loads((approvals / f"{tid}.json").read_text(encoding="utf-8"))
    assert doc["batch_id"] == tid and doc["approved"] == arm.call_cap

    # 分支二（重跑口径）：按任务票落件；票已由人预写 → 一个字节都不许改
    ql.write_approval(batch_id="P281-B1b-1", task=1, stage=STAGE,
                      approved=PILOT_APPROVED_PER_TASK, beyond_normal=False,
                      note="A7-8 正对照", namespace=ledger.ns)
    pre = (approvals / "P281-B1b-1.json").read_bytes()
    trial_harness._ensure_approval(ledger, trial_id=tid, arm=arm,
                                   batch_id="P281-B1b-1")
    assert (approvals / "P281-B1b-1.json").read_bytes() == pre, \
        "已预写批准件被 harness 覆盖了（等于自签自跑）"
    assert json.loads(pre)["approved"] == PILOT_APPROVED_PER_TASK


def test_a7_8_run_trial_bills_via_bill_batch():
    """结构事实：`run_trial` 的 `billing_context(...)` 用 `bill_batch`，不是硬写 trial_id。

    这是 A7-8 的另一半——`_ensure_approval` 写了哪张票，账本就该挂到哪张票上；
    两者若各写各的，会重新制造 D3（票写了但账本没引用）。用 AST 而不是跑完整
    Trial，是因为后者要起沙盒；本用例只钉「账本批量身份取自同一个变量」。
    """
    import ast

    src = (Path(__file__).resolve().parents[2]
           / "orchestrator" / "benchmark" / "phase281" / "trial_harness.py")
    tree = ast.parse(src.read_text(encoding="utf-8"))
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call)
             and ((isinstance(n.func, ast.Name) and n.func.id == "billing_context")
                  or (isinstance(n.func, ast.Attribute)
                      and n.func.attr == "billing_context"))]
    assert len(calls) == 1, f"billing_context 调用点应恰 1 处，实得 {len(calls)}"
    kw = {k.arg: k.value for k in calls[0].keywords if k.arg}
    assert isinstance(kw.get("batch_id"), ast.Name) and kw["batch_id"].id == "bill_batch"
    assert isinstance(kw.get("thread_id"), ast.Name) and kw["thread_id"].id == "trial_id"


# ---------------------------------------------------------------------------
# 活产物层（SIMPRAXIS_KB_INTEGRATION=1）——AC-P281-7-01/03/04/05/06/07/08/09/10
# ---------------------------------------------------------------------------

@pytest.mark.kb_integration
def test_ac_p281_7_01_approval_present_live():
    root = _live_root()
    docs = {}
    for task_no, batch_id in PILOT_BATCHES.items():
        p = root / "quota" / "approvals" / f"{batch_id}.json"
        assert p.is_file(), f"缺批准件 {p}"
        doc = json.loads(p.read_text(encoding="utf-8"))
        assert doc["approved"] == PILOT_APPROVED_PER_TASK, doc
        assert doc["stage"] == STAGE
        assert doc["ledger_phase"] == LEDGER_PHASE
        assert isinstance(doc["ledger_phase"], int)
        assert isinstance(doc["task"], int) and doc["task"] == task_no
        docs[batch_id] = doc
    assert len({d["task"] for d in docs.values()}) == 3
    assert sum(d["approved"] for d in docs.values()) == PILOT_TOTAL


@pytest.mark.kb_integration
def test_ac_p281_7_03_ledger_shape_live():
    rows = ql.read_lines(_live_ledger())
    ql.validate_ledger_rows(rows, allow_events=("selfcheck",))
    calls = _call_rows(_live_ledger())
    assert len(calls) <= PILOT_TOTAL, f"Pilot 实呼 {len(calls)} > 60（STOP-12）"
    snap = analysis.ledger_snapshot(_live_ledger())
    assert snap["gaps"] == [], f"seq 断号：{snap['gaps']}（STOP-09）"


@pytest.mark.kb_integration
def test_ac_p281_7_04_all_terminal_live():
    root = _live_root()
    rows = analysis.collect_trials(root)
    assert len(rows) == 6
    for row in rows:
        assert row["present"], f"缺 Trial 产物：{row['trial_dir']}"
        assert row["status"] in analysis.TERMINAL_STATUSES, row


@pytest.mark.kb_integration
def test_ac_p281_7_05_per_trial_cap_live():
    root = _live_root()
    calls = _call_rows(_live_ledger())
    for row in analysis.collect_trials(root):
        trial = json.loads((Path(row["trial_dir"]) / "trial.json").read_text(encoding="utf-8"))
        observed = int((trial.get("observed") or {}).get("calls") or 0)
        assert observed <= constants.CALL_CAP_PER_TRIAL, f"{row['trial_id']} 超单 Trial 帽"
        mine = [r for r in calls if r.get("thread_id") == row["trial_id"]]
        assert len(mine) == observed, (
            f"{row['trial_id']}：observed.calls={observed} 与账本 "
            f"thread_id 行数 {len(mine)} 不等")
        assert trial.get("used_calls") == observed, row["trial_id"]


@pytest.mark.kb_integration
def test_ac_p281_7_06_task_cap_live():
    root = _live_root()
    calls = _call_rows(_live_ledger())
    per_task = {}
    for task_id in constants.TASK_IDS:
        no = constants.TASK_NO_BY_ID[task_id]
        per_task[task_id] = sum(1 for r in calls
                                if r.get("task") == no)
        assert per_task[task_id] <= PILOT_APPROVED_PER_TASK, (task_id, per_task)
    assert sum(per_task.values()) == len(calls) <= PILOT_TOTAL


@pytest.mark.kb_integration
def test_ac_p281_7_07_pilot_report_live():
    root = _live_root()
    p = root / "analysis" / "pilot_report.json"
    assert p.is_file(), f"缺 {p}"
    doc = json.loads(p.read_text(encoding="utf-8"))
    assert frozenset(doc) == analysis.REPORT_FIELDS, sorted(doc)
    assert doc["schema"] == analysis.PILOT_REPORT_SCHEMA
    assert doc["go_no_go"] in ("GO", "HOLD")
    assert isinstance(doc["stop_open"], list)
    assert STRICT_Z.match(doc["generated_at"]), doc["generated_at"]
    assert len(doc["per_trial"]) == 6
    # G281-2 是机判门（§12.2）：活报告必须**有布尔值**，不能是 null
    g2 = (doc.get("five_gates") or {}).get("g281_2") or {}
    assert g2.get("ok") is True, f"G281-2 未通过或未探测：{g2}"


@pytest.mark.kb_integration
def test_ac_p281_7_08_rule_reproducible_live():
    root = _live_root()
    doc = json.loads((root / "analysis" / "pilot_report.json").read_text(encoding="utf-8"))
    recomputed, reasons = analysis.decide_go_no_go(doc["per_trial"])
    assert recomputed == doc["go_no_go"], (recomputed, reasons, doc["reasons"])


@pytest.mark.kb_integration
def test_ac_p281_7_09_failures_ledgered_live():
    rows = ql.read_lines(_live_ledger())
    fails = [r for r in rows if r.get("event") == "call" and r.get("ok") is False]
    for r in fails:
        assert isinstance(r.get("seq"), int) and not isinstance(r.get("seq"), bool), r
        assert r.get("err"), f"失败行 {r.get('seq')} 没有 err 字段"
    # 失败行不许被「事后抹平」：seq 全集必须连续覆盖到最大 seq
    calls = _call_rows(_live_ledger())
    seqs = sorted(int(r["seq"]) for r in calls)
    assert seqs == list(range(1, len(seqs) + 1)), "失败行没占 seq"


@pytest.mark.kb_integration
def test_ac_p281_7_10_clean_closeout_live():
    root = _live_root()
    # 锁内适配 A7-4：compose 件在 sandbox/，仓库根裸 `docker compose ps` 只报
    # "no configuration file provided"——空 stdout 会被读成「容器干净」（假绿）。
    repo_root = Path(__file__).resolve().parents[2]
    ps = subprocess.run(
        ["docker", "compose", "-f", str(repo_root / "sandbox" / "docker-compose.yml"),
         "ps", "-q"],
        cwd=str(repo_root), capture_output=True, text=True, timeout=120, check=False)
    assert ps.returncode == 0, ps.stderr[-400:]
    assert [ln for ln in ps.stdout.splitlines() if ln.strip()] == [], "容器残留"
    smi = subprocess.run(
        ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
        capture_output=True, text=True, timeout=60, check=False)
    used = [int(x) for x in smi.stdout.replace(",", " ").split() if x.isdigit()]
    assert used and max(used) <= analysis.GPU_LIMIT_MIB, f"显存残留 {used} MiB"
    # 看门狗残留：本 Task 的包装进程不许还在（p281-pilot）
    pg = subprocess.run(["pgrep", "-af", "run_watched.*p281-pilot"],
                        capture_output=True, text=True, check=False)
    assert pg.stdout.strip() == "", f"看门狗残留：{pg.stdout}"


# ---------------------------------------------------------------------------
# A7-10：票号端到端贯通（2026-11-07 重跑批事故的回归）
# ---------------------------------------------------------------------------

def _write_two_round_ledger(path: Path, *, trial_id: str) -> None:
    """同一 ``thread_id`` 两轮：首轮 ``batch_id=trial_id`` 10 行 + 本轮 0 行。

    这就是 2026-11-07 的真实形状：重跑批把账本切到 ``P281-B1b-*`` 之前/之后，
    同名 Trial 的首轮 10 行仍然在账上。按 ``thread_id`` 数 ⇒ 帽已满 ⇒
    一条网都没发就 ``TrialCapReached``。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    ts = "2026-11-07T00:00:00Z"
    rows = [{"seq": i, "event": "call", "ok": True, "ts_iso": ts,
             "batch_id": trial_id, "stage": STAGE, "task": 1,
             "model": "fixture-model", "thread_id": trial_id,
             "tokens_in": 100, "tokens_out": 10}
            for i in range(1, 11)]
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def test_a7_10_ledger_call_rows_scopes_by_batch(tmp_path):
    """按票切窗：同一 Trial 的旧轮行不再算进本轮。"""
    from orchestrator.benchmark.phase281 import telemetry

    trial_id = "mujoco_contact_push_v1__agentic_v1__rep0"
    ledger_path = tmp_path / "ledger.jsonl"
    _write_two_round_ledger(ledger_path, trial_id=trial_id)

    assert len(telemetry.ledger_call_rows(ledger_path, trial_id)) == 10
    # 缺省（batch_id=None）= 全历史（首轮口径一字未改）
    assert telemetry.ledger_call_rows(ledger_path, trial_id, batch_id=trial_id) == \
        telemetry.ledger_call_rows(ledger_path, trial_id)
    # 切到本轮票 ⇒ 0 行（本轮确实一次网都没发）
    assert telemetry.ledger_call_rows(ledger_path, trial_id,
                                      batch_id="P281-B1b-1") == []


def test_a7_10_trial_budget_not_blocked_by_previous_round(tmp_path):
    """重跑批的帽只数本票：上一轮花满 10 呼不得把本轮堵在发网之前。"""
    from orchestrator.benchmark.phase281 import telemetry

    trial_id = "mujoco_contact_push_v1__agentic_v1__rep0"
    ledger_path = tmp_path / "ledger.jsonl"
    _write_two_round_ledger(ledger_path, trial_id=trial_id)

    old = telemetry.TrialBudget(cap=10, ledger=ledger_path, trial_id=trial_id)
    assert old.used() == 10
    with pytest.raises(telemetry.TrialCapReached):
        old.before_call()                     # 旧口径：重跑必然被堵（事故现场）

    new = telemetry.TrialBudget(cap=10, ledger=ledger_path, trial_id=trial_id,
                                batch_id="P281-B1b-1")
    assert new.used() == 0
    new.before_call()                         # 不抛：本轮票下确实还没花


def test_a7_10_observed_reads_only_current_ticket(tmp_path):
    """``observed`` 的呼数/tokens 必须来自本轮票，不得把旧轮数据冒充本轮。"""
    from orchestrator.benchmark.phase281 import telemetry

    trial_id = "mujoco_contact_push_v1__agentic_v1__rep0"
    ledger_path = tmp_path / "ledger.jsonl"
    _write_two_round_ledger(ledger_path, trial_id=trial_id)
    trial_dir = tmp_path / "trial"
    trial_dir.mkdir()

    old = telemetry.observed(trial_dir, ledger_path, trial_id=trial_id)
    assert old["calls"] == 10 and old["tokens_in"] == 1000

    new = telemetry.observed(trial_dir, ledger_path, trial_id=trial_id,
                             batch_id="P281-B1b-1")
    assert new["calls"] == 0 and new["tokens_in"] == 0


def test_a7_10_g281_3_reconciles_window_against_trials(tmp_path):
    """切窗后窗内 0 行而 Trial 自报 10 呼 ⇒ G281-3 必须红（不许假阳）。"""
    ledger_path = tmp_path / "ledger.jsonl"
    _write_two_round_ledger(ledger_path,
                            trial_id="mujoco_contact_push_v1__agentic_v1__rep0")
    window = analysis.ledger_snapshot(ledger_path, batches=["P281-B1b-1"])
    assert window["summary"]["call_rows"] == 0

    empty_ok = analysis.five_gates(go_no_go="HOLD", stop_open=[], ledger=window,
                                   env=None, trial_calls=0)
    assert empty_ok["g281_3"]["ok"] is True          # 真的没人调用：绿得合理

    vacuous = analysis.five_gates(go_no_go="HOLD", stop_open=[], ledger=window,
                                  env=None, trial_calls=10)
    assert vacuous["g281_3"]["ok"] is False          # 10 呼不知去向：红
    assert "reconciled=False" in vacuous["g281_3"]["detail"]

    # 非切窗口径（首轮）不启用对账，行为与 P281-7 逐位相同
    whole = analysis.ledger_snapshot(ledger_path)
    legacy = analysis.five_gates(go_no_go="HOLD", stop_open=[], ledger=whole,
                                 env=None, trial_calls=0)
    assert legacy["g281_3"]["ok"] is True


def test_a7_10_runspec_uses_ticket_batch_id():
    """``RunSpec.batch_id`` 必须是票号——runner 的内层 billing_context 会覆盖外层。"""
    from types import SimpleNamespace

    from orchestrator.agent_runner.runner import RunSpec
    from orchestrator.benchmark.phase281 import trial_harness

    spec = SimpleNamespace(task_id="mujoco_contact_push_v1")
    arm = SimpleNamespace(max_coding_rounds=10, runspec_agentic=False,
                          tool_grants=(), step_cap=0, token_cap=0,
                          agentic_config=None)
    kwargs = dict(trial_id="mujoco_contact_push_v1__legacy_v1__rep0",
                  workdir=Path("/tmp/p281-a7-10"), cmd=["python", "x.py"],
                  telemetry_relpath="telemetry.jsonl", task_title="t",
                  env_extra={})
    ticket = trial_harness._build_runspec(spec, arm, batch_id="P281-B1b-1",
                                          **kwargs)
    assert isinstance(ticket, RunSpec)
    assert ticket.batch_id == "P281-B1b-1"
    # 缺省（首轮口径）仍是 trial_id，两个非重跑调用点行为不变
    assert trial_harness._build_runspec(spec, arm, **kwargs).batch_id == \
        "mujoco_contact_push_v1__legacy_v1__rep0"


# ---------------------------------------------------------------------------
# M2（2026-11-07 评审）：§11.9 记账证明是**伴生件**，不进 8 字段契约
# ---------------------------------------------------------------------------

def _write_accounting_ledger(path: Path) -> None:
    """两轮的账：seq 1..2 首轮（batch=trial_id）+ seq 3..4 重跑票（P281-B1b-*）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    ts = "2026-11-07T00:00:00Z"
    rows = [
        {"seq": 1, "event": "call", "ok": True, "ts_iso": ts, "stage": STAGE,
         "task": 1, "model": "fixture-model", "batch_id": "t1",
         "thread_id": "mujoco_contact_push_v1__agentic_v1__rep0",
         "tokens_in": 10, "tokens_out": 1},
        {"seq": 2, "event": "call", "ok": False, "ts_iso": ts, "stage": STAGE,
         "task": 2, "model": "fixture-model", "batch_id": "t2",
         "thread_id": "libero_env_adapter_v1__legacy_v1__rep0",
         "tokens_in": 20, "tokens_out": 2},
        {"seq": 3, "event": "call", "ok": True, "ts_iso": ts, "stage": STAGE,
         "task": 1, "model": "fixture-model", "batch_id": "P281-B1b-1",
         "thread_id": "mujoco_contact_push_v1__agentic_v1__rep0",
         "tokens_in": 30, "tokens_out": 3},
        {"seq": 4, "event": "call", "ok": True, "ts_iso": ts, "stage": STAGE,
         "task": 2, "model": "fixture-model", "batch_id": "P281-B1b-2",
         "thread_id": "libero_env_adapter_v1__agentic_v1__rep0",
         "tokens_in": 40, "tokens_out": 4},
    ]
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def test_m2_accounting_is_companion_not_report_contract(tmp_path):
    """§11.9 的记账证明单独成文：``pilot_report`` 仍是**恰好**八个字段。"""
    root = tmp_path / "root"
    _write_accounting_ledger(root / "quota" / "ledger.jsonl")

    acct = analysis.build_accounting(root, batches=["P281-B1b-1", "P281-B1b-2"])
    assert acct["schema"] == analysis.ACCOUNTING_SCHEMA
    # 两件是**不同的 schema**，落在**不同的文件**里：记账件不进报告契约
    report = analysis.build_pilot_report(root, batches=["P281-B1b-1", "P281-B1b-2"])
    assert report["schema"] != acct["schema"]
    assert frozenset(report) == analysis.REPORT_FIELDS
    # 契约字段集本身一字未改（改它就是改预注册）
    assert analysis.REPORT_FIELDS == frozenset(
        {"schema", "generated_at", "per_trial", "verdicts", "go_no_go",
         "reasons", "stop_open", "five_gates"})


def test_m2_accounting_window_and_full_ledger(tmp_path):
    """窗口回答「本轮花多少」，``full_ledger`` 回答「至今花多少」，两栏都在。"""
    root = tmp_path / "root"
    _write_accounting_ledger(root / "quota" / "ledger.jsonl")

    acct = analysis.build_accounting(root, batches=["P281-B1b-1", "P281-B1b-2"])
    assert acct["calls"] == 2 and acct["seqs"] == [3, 4]
    assert acct["calls_ok"] == {"true": 2, "false": 0}
    assert acct["by_task"] == {"1": 1, "2": 1}
    assert acct["by_stage"] == {STAGE: 2}
    assert acct["contiguous"] is True and acct["gaps"] == []
    # 整本：三票两轮合计 4 呼，ok/fail 拆分如实（含 1 条 fail）
    assert acct["full_ledger"]["calls"] == 4
    assert acct["full_ledger"]["calls_ok"] == {"true": 3, "false": 1}
    assert acct["full_ledger"]["seq_first"] == 1
    assert acct["full_ledger"]["seq_last"] == 4


def test_m2_accounting_per_trial_seqs_and_unattributed(tmp_path):
    """逐 Trial 的 seq 列表必须点得着；窗外的行进 ``unattributed`` 而非静默丢。"""
    root = tmp_path / "root"
    ledger = root / "quota" / "ledger.jsonl"
    _write_accounting_ledger(ledger)
    with ledger.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "seq": 5, "event": "call", "ok": True, "ts_iso": "2026-11-07T00:00:00Z",
            "stage": STAGE, "task": 9, "model": "fixture-model",
            "batch_id": "P281-B1b-1", "thread_id": "not_a_trial",
            "tokens_in": 1, "tokens_out": 1}) + "\n")

    acct = analysis.build_accounting(root, batches=["P281-B1b-1"])
    mujoco = acct["per_trial"]["mujoco_contact_push_v1__agentic_v1__rep0"]
    assert mujoco["seqs"] == [3] and mujoco["calls"] == 1
    # 六个预注册 Trial 全在场（没跑的也占一格，读作 0 而不是「不存在」）
    assert len(acct["per_trial"]) == 6
    assert acct["per_trial"]["icy_ground_dr_v1__agentic_v1__rep0"]["calls"] == 0
    assert acct["unattributed"] == [
        {"seq": 5, "thread_id": "not_a_trial", "batch_id": "P281-B1b-1"}]


def test_m2_m3_m7_driver_review_guards():
    """驱动器三处评审修必须留在脚本里（L2：脚本守卫不许只靠人眼）。"""
    script = (Path(__file__).resolve().parents[2]
              / "scripts" / "benchmark" / "phase281_pilot_driver.sh")
    body = script.read_text(encoding="utf-8")

    # M2：§11.9 记账证明走伴生件，驱动器必须传这个开关
    assert "--accounting-out" in body
    assert "pilot_accounting.json" in body

    # M3：收尾（compose down）必须排在分析器之前——G281-5 要证明的是收尾后干净
    teardown = body.index("docker compose -f \"$REPO_ROOT/sandbox/docker-compose.yml\" down")
    # 用**调用行**定位（文件头的说明文字也含同一个模块名，按模块名取会拿到注释）
    analysis_run = body.index("-m orchestrator.benchmark.phase281.analysis")
    assert teardown < analysis_run, "compose down 必须早于 --probe-env 的分析调用"
    # 收尾后要再探一次，且不吞 rc
    assert "ps -aq" in body
    assert "down_rc=$?" in body

    # M7：冻结臂序给出的 Trial 数必须恰好 6，少一个就拒跑
    assert '[ "$planned" -ne 6 ]' in body
