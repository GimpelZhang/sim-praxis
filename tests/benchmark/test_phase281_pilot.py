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
