"""代码评审 G 批加固回归：QuotaLedger 账行/counters 健壮性（G-4）、批次凭据
字符集与 bool 防伪（G-9）、任务局部硬顶与 resume 拒绝（G-11）、ModelGateway
构造红线源码扫描（G-13）。全部 0 付费 0 联网：不挂真实网关，只直接驱动账本。
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from orchestrator.gateway import quota_ledger as ql
from orchestrator.gateway.quota_ledger import (
    LedgerDegradedError,
    QuotaError,
    QuotaLedger,
)
from orchestrator.gateway.router import GatewayPausedError

STAGE = "macro_ideation"
BATCH = "T-GH-FIXTURE"
REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMPRAXIS_PHASE1_ROOT", str(tmp_path / "phase1"))
    # Task0：台账类路径已切 Phase 2 独立新账（runtime_paths 单一入口）；
    # fixture 账目录随之指向 phase2，G-4/G-9/G-11 断言语义不变。
    monkeypatch.setenv("SIMPRAXIS_PHASE2_ROOT", str(tmp_path / "phase2"))
    monkeypatch.setenv("SIMPRAXIS_GATEWAY_STATE", str(tmp_path / "gwstate"))
    qdir = tmp_path / "phase2" / "quota"
    qdir.mkdir(parents=True)
    return {"tmp": tmp_path, "qdir": qdir,
            "ledger": qdir / "ledger.jsonl",
            "counters": qdir / "counters.json",
            "pause": tmp_path / "gwstate" / "quota_pause.json"}


def _seed_calls(path: Path, n: int, *, task=2, batch_id=BATCH, stage=STAGE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        for j in range(1, n + 1):
            fh.write(json.dumps({
                "ts_iso": "2026-10-16T00:00:00+0800", "event": "call",
                "stage": stage, "model": "fixture", "thread_id": "seed",
                "task": task, "batch_id": batch_id, "seq": j,
                "global_used_after": j, "batch_approved": 99,
                "batch_used_after": j, "ok": True, "err": None,
                "approval_ref": f"quota/approvals/{batch_id}.json"},
                ensure_ascii=False) + "\n")


# ---------- G-4：账行追加失败 → degraded 告警 + 报错，绝不假装成功 ----------

def test_g4_append_failure_writes_degraded_alert_and_raises(isolated, monkeypatch):
    ql.write_approval(batch_id=BATCH, task=2, stage=STAGE, approved=8,
                      beyond_normal=False, note="fixture（零付费）")

    def boom(row):
        raise OSError("模拟 ENOSPC：账行追加失败")

    monkeypatch.setattr(ql, "_append_line", boom)
    led = QuotaLedger()
    with pytest.raises(LedgerDegradedError, match="未入账"):
        led.record(STAGE, task=2, batch_id=BATCH, ok=True, model="fixture",
                   thread_id="t-g4", tokens_in=3, tokens_out=1)

    alerts = sorted(isolated["qdir"].glob("ledger.degraded.*.json"))
    assert len(alerts) == 1, f"必须恰好落一个 degraded 告警件：{alerts}"
    doc = json.loads(alerts[0].read_text(encoding="utf-8"))
    assert doc["exception_type"] == "OSError"
    assert "ENOSPC" in doc["exception"]
    meta = doc["call_meta"]
    assert meta["seq"] == 1 and meta["ok"] is True
    assert meta["stage"] == STAGE and meta["task"] == 2
    assert meta["batch_id"] == BATCH and meta["thread_id"] == "t-g4"
    assert meta["tokens_in"] == 3 and meta["tokens_out"] == 1
    # 账行确实没写成；counters 不得在追加失败后被改动
    assert not isolated["ledger"].exists()
    assert not isolated["counters"].exists()
    # 权限收敛：告警件 0600
    assert (alerts[0].stat().st_mode & 0o777) == 0o600


# ---------- G-4：counters.json 损坏 → 隔离 + 零值回退，成功账行不被反向抹掉 ----------

def test_g4_corrupt_counters_quarantined_successful_row_kept(isolated):
    isolated["counters"].write_text("{截断的 JSON", encoding="utf-8")
    led = QuotaLedger()
    row = led.record(STAGE, task=2, batch_id=BATCH, ok=True, model="fixture",
                     thread_id="t-g4b", tokens_in=2, tokens_out=1)
    assert row["seq"] == 1 and row["ok"] is True

    quarantined = sorted(isolated["qdir"].glob("counters.corrupt.*.json"))
    assert len(quarantined) == 1
    assert quarantined[0].read_text(encoding="utf-8") == "{截断的 JSON"
    fixed = json.loads(isolated["counters"].read_text(encoding="utf-8"))
    assert fixed["consecutive_quota_errors"] == 0

    rows = [json.loads(l) for l in isolated["ledger"].read_text().splitlines()]
    int_rows = [r for r in rows if isinstance(r.get("seq"), int)]
    assert len(int_rows) == 1 and int_rows[0]["thread_id"] == "t-g4b"


def test_g4_corrupt_counters_non_dict_also_quarantined(isolated):
    isolated["counters"].write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    led = QuotaLedger()
    led.record(STAGE, task=2, batch_id=BATCH, ok=True, model="fixture")
    assert len(list(isolated["qdir"].glob("counters.corrupt.*.json"))) == 1
    fixed = json.loads(isolated["counters"].read_text(encoding="utf-8"))
    assert fixed["consecutive_quota_errors"] == 0


# ---------- G-9：batch_id 字符集白名单 + approved bool 显式拒绝（写侧/读侧） ----------

@pytest.mark.parametrize("bad_batch", ["../escape", "a/b", "x y", "b" * 65, "T$"])
def test_g9_write_approval_rejects_traversal_batch_id(isolated, bad_batch):
    with pytest.raises(QuotaError, match="batch_id 非法"):
        ql.write_approval(batch_id=bad_batch, task=2, stage=STAGE, approved=4,
                          beyond_normal=False, note="fixture")
    assert list((isolated["qdir"] / "approvals").glob("*.json")) == []


@pytest.mark.parametrize("bad_batch", ["../escape", "a/b", "T$"])
def test_g9_load_approval_rejects_traversal_batch_id(isolated, bad_batch):
    with pytest.raises(QuotaError, match="batch_id 非法"):
        ql.load_approval(bad_batch)


def test_g9_write_approval_rejects_bool_and_nonpositive(isolated):
    with pytest.raises(QuotaError, match="bool"):
        ql.write_approval(batch_id="T-BOOLW", task=2, stage=STAGE, approved=True,
                          beyond_normal=False, note="fixture")
    with pytest.raises(QuotaError, match="正整数"):
        ql.write_approval(batch_id="T-ZERO", task=2, stage=STAGE, approved=0,
                          beyond_normal=False, note="fixture")


def test_g9_load_approval_rejects_bool_approved_even_in_valid_json(isolated):
    d = isolated["qdir"] / "approvals"
    d.mkdir(parents=True, exist_ok=True)
    (d / "T-BOOLR.json").write_text(json.dumps({
        "batch_id": "T-BOOLR", "task": 2, "stage": STAGE,
        "approved": True, "beyond_normal": False,
        "created_at_iso": "2026-10-16T00:00:00+0800",
        "note": "伪造：JSON true 不是 1 次书面批准"}, ensure_ascii=False),
        encoding="utf-8")
    with pytest.raises(QuotaError, match="bool"):
        ql.load_approval("T-BOOLR")


def test_g9_approval_roundtrip_still_ok(isolated):
    p = ql.write_approval(batch_id="T-OK", task=2, stage=STAGE, approved=3,
                          beyond_normal=False, note="fixture 正常件")
    assert (p.stat().st_mode & 0o777) == 0o600
    doc = ql.load_approval("T-OK")
    assert doc["approved"] == 3 and doc["batch_id"] == "T-OK"


# ---------- G-11：任务局部硬顶独立 reason，resume 拒绝批次扩展恢复 ----------

def test_g11_task_hard_cap_pause_shape_then_resume_refused(isolated):
    _seed_calls(isolated["ledger"], 14)  # Task2 局部硬顶 14
    ql.write_approval(batch_id=BATCH, task=2, stage=STAGE, approved=20,
                      beyond_normal=False, note="fixture 大批")
    led = QuotaLedger()
    with pytest.raises(GatewayPausedError) as ei:
        led.preflight(STAGE, task=2, batch_id=BATCH)
    state = ei.value.state
    assert state["reason"] == "task_hard_cap"
    assert state["schema_version"] == 2 and state["resume_at_epoch"] is None
    assert state["stage"] == STAGE and state["scope"] == "task"
    assert state["affected_stages"] == [STAGE]
    assert state["trigger"]["task_used"] == 14
    assert state["trigger"]["task_hard_cap"] == 14
    assert isolated["pause"].exists()

    # 即使持新批次票，resume 也必须拒绝（任务硬顶不可批次扩展），暂停件保留
    with pytest.raises(QuotaError, match="批次扩展"):
        ql.resume(ticket="ticket-g11-12345678", batch_id="T-NEW-BATCH")
    assert isolated["pause"].exists()

    # CLI 契约：exit 2 + 明确文案，不打 traceback
    rc = ql.main(["resume", "--ticket", "ticket-g11-12345678",
                  "--batch-id", "T-NEW-BATCH"])
    assert rc == 2
    assert isolated["pause"].exists()


# ---------- G-13：生产源码红线——每个 ModelGateway(...) 必须显式挂 ledger/unguarded ----------

# G-13 白名单（Task 4 评审 MINOR 修复）：unguarded=True 只准出现在下列两处。
# 新增任何 unguarded 构造点即红——「白名单语义被本测试锁定」现在是真的锁定。
UNGUARDED_ALLOWLIST = {
    # --validate-models 账外 ping（脚本入口自身，票外批次 P26-B0 走 miss4 记账）
    "orchestrator/gateway/router.py",
    # 付费票据外探针：approved==1 + probe_consumed_<batch>.json 消费标记双守卫
    "scripts/phase26/run_wall_retest.py",
}


def _unguarded_enabled(value: ast.expr) -> bool:
    """``unguarded`` 的真值判定不看字面量写法（评审 R4）。

    ``unguarded=1`` / ``= 'yes'`` 同样会让 ``ModelGateway`` 跳过账本，故只认
    ``is True`` 会被绕过；非字面量表达式无法静态判定，按「开启」fail-closed。
    """
    if isinstance(value, ast.Constant):
        return bool(value.value)
    return True


def test_g13_model_gateway_construction_requires_ledger_or_unguarded():
    """AST 扫描 orchestrator/** 与 scripts/**：无 ledger= 又无 unguarded= 即违规。

    扫描半径原只含 orchestrator/**，而真正逐次烧钱的付费入口大多在 scripts/
    （phase25 探针与墙、phase26 run_wall_retest），故一并纳入同等要求。
    unguarded=True 不是自由通行证：只允许 UNGUARDED_ALLOWLIST 两处，其余即红。
    （tests/ 不在半径内：单测一律 monkeypatch transport，0 触网 0 记账。）
    """
    offenders = []
    unguarded_sites = []
    scanned = {"orchestrator": 0, "scripts": 0}
    for root in ("orchestrator", "scripts"):
        for py in (REPO_ROOT / root).rglob("*.py"):
            rel = str(py.relative_to(REPO_ROOT))
            tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if isinstance(func, ast.Name):
                    fname = func.id
                elif isinstance(func, ast.Attribute):
                    fname = func.attr
                else:
                    continue
                if fname != "ModelGateway":
                    continue
                scanned[root] += 1
                kwargs = {k.arg for k in node.keywords if k.arg}
                unguarded_true = any(
                    k.arg == "unguarded" and _unguarded_enabled(k.value)
                    for k in node.keywords)
                if unguarded_true and rel not in UNGUARDED_ALLOWLIST:
                    unguarded_sites.append(f"{rel}:{node.lineno}")
                # ledger 是第 10 个位置参数；位置传入同样算合规（实际无此风格）
                if "ledger" in kwargs or "unguarded" in kwargs or len(node.args) >= 10:
                    continue
                offenders.append(f"{rel}:{node.lineno}")
    assert scanned["orchestrator"] >= 5 and scanned["scripts"] >= 5, (
        f"红线扫描应覆盖 orchestrator/ 与 scripts/ 两处生产构造点（实际 {scanned}）")
    assert not offenders, (
        "ModelGateway 构造必须显式 ledger=QuotaLedger() 或 unguarded=True："
        + ", ".join(offenders))
    assert not unguarded_sites, (
        "unguarded=True 仅限 UNGUARDED_ALLOWLIST（router --validate-models / "
        "run_wall_retest 账外探针）：" + ", ".join(unguarded_sites))
