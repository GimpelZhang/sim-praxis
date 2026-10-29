"""Phase 2.5 Task0 开工准入测试（AC-T0-1..7；0 付费、0 联网、全 hermetic）。

覆盖：
  1. scripts/00_env.sh 纯加法：与 T0 父本金（efc08a3，phase25 块入仓前）
     逐字节对账（env 仅多
     SIMPRAXIS_PHASE25_ROOT、文件树仅多 phase25 目录族）、0700/0600、幂等
     （已存在 ledger 不截断）、可写探针；
  2. runtime_paths phase25 路径族 + Phase1/2 封存账写守卫（SealedLedgerError）；
  3. PHASE25_TASK_CAPS={3:6,5:28,9:160,10:2} 常量/算术/命名空间永不并表；
  4. phase25 独立账：空账首行 seq=1、严格 UTC Z、任务帽/全局门/暂停态隔离、
     blocked（null seq）占批次不占实呼、CLI --ledger-phase；
  5. AC-T0-7 机判：合成正账 + 跳号/重号/混列/bool/非 Z 五负例必红 + 两栏汇总；
  6. 依赖探针裁决器：脱敏 dry-run 夹具（主轨零漂移 / openai-agents 撞 pydantic）；
  7. 测试基线冻结器：纯函数解析（计数/skipped 名单），非全绿拒冻；
  8. 旧路径出生 prompt 黄金快照：tmp 重生成与入库件逐字节一致；
  9. RunSpec 字段集冻结（agentic 层不得在 Task0 给旧路径出生契约加字段）。
"""
from __future__ import annotations

import dataclasses
import hashlib
import importlib.util
import json
import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

from orchestrator.agent_runner.runner import coding_call_cache_key
from orchestrator.common import runtime_paths as rp
from orchestrator.common.clock import parse_ts_iso_strict
from orchestrator.gateway import quota_ledger as ql
from orchestrator.gateway.quota_ledger import (
    LedgerSequenceError,
    PHASE25_NAMESPACE,
)
from orchestrator.gateway.router import GatewayPausedError

REPO_ROOT = Path(__file__).resolve().parents[2]
# T0 攢批108（50e7558）的父提交：phase25 块入仓前 00_env.sh 的最后状态。
# 纯加法回归基线必须钉死这个不可变 commit——活 HEAD 会随 T0 提交移动，导致
# T0 提交后 HEAD 与工作树逐字节相同、env_added=[] 的自指必红（verifier
# 第三签抓到的缺陷）。历史提交不重写，该 blob 永久可用。
PRE_T0_PARENT = "efc08a3"
FIXTURE_DIR = Path(__file__).parent / "fixtures"
GOLDEN_DIR = REPO_ROOT / "tests" / "fixtures" / "phase25_golden"
VENV_PY = "/mnt/venvs/simpraxis/bin/python"
STAGE = "coding_debug"
BATCH = "P25-T-FIXTURE"


def _load_module(name: str, relpath: str):
    path = REPO_ROOT / relpath
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


verdict_mod = _load_module(
    "t0_dependency_verdict", "scripts/phase25/t0_dependency_verdict.py")
baseline_mod = _load_module(
    "t0_freeze_baseline", "scripts/phase25/freeze_baseline.py")
golden_mod = _load_module(
    "t0_freeze_golden", "scripts/phase25/freeze_golden.py")


# ---------------------------------------------------------------------------
# 1) 00_env.sh 纯加法（T0 父本金 efc08a3 vs 当前版，同一 tmp 根先后 source；
#    真凭据走环境）。本测只冻结 T0 这一笔 delta：未来提交再给 00_env.sh
#    加变量/目录的纯加法回归由未来任务自己的对账测试负责，本测无需跟进。
# ---------------------------------------------------------------------------

def _source_snapshot(script_text: str, shared_root: Path) -> dict:
    """在受控 bash 中 source 给定 00_env.sh 文本，回收 SIMPRAXIS env 与文件树。

    凭据从【当前进程已导出的 SIMPRAXIS_LLM_* 或真实 HOME】按脚本既有契约解析；
    快照只回收 SIMPRAXIS_ 变量且过滤一切含 TOKEN 的行（值绝不进入测试输出）。
    """
    script_path = shared_root / "00_env_under_test.sh"
    script_path.write_text(script_text, encoding="utf-8")

    data_root = shared_root / "data"
    env = dict(os.environ)
    env.update({
        "SIMPRAXIS_ROOT": str(REPO_ROOT),
        "SIMPRAXIS_DATA_ROOT": str(data_root),
        "SIMPRAXIS_CACHE_ROOT": str(shared_root / "cache"),
        "KB_ROOT": str(shared_root / "kb"),
        "SIMPRAXIS_RUN_DIR": str(data_root / "runs"),
        "SIMPRAXIS_GATEWAY_STATE": str(data_root / "gateway_state"),
        "SIMPRAXIS_TOURS_DIR": str(shared_root / "tours"),
        "SIMPRAXIS_USE_PROXY": "0",
    })
    # 三个 phase 根一律不预置，强制走脚本默认值（data 根下 phase1/2/25），
    # 既测默认契约也杜绝测试串到外层 shell 的真实 /mnt 根。
    for k in ("SIMPRAXIS_PHASE1_ROOT", "SIMPRAXIS_PHASE2_ROOT",
              "SIMPRAXIS_PHASE25_ROOT"):
        env.pop(k, None)

    driver = (
        "set +x\n"
        f"source {script_path}\n"
        'rc=$?\n'
        'if [ $rc -ne 0 ]; then echo "SOURCE_RC=$rc"; exit $rc; fi\n'
        'echo "---ENV---"\n'
        'env | grep "^SIMPRAXIS_" | grep -v TOKEN | sort\n'
        'echo "---TREE-DATA---"\n'
        f'(cd "{data_root}" && find . -printf "%y %m %P\\n" | sort)\n'
        'echo "---TREE-RUNS---"\n'
        f'(cd "{data_root}/runs" && find . -printf "%y %m %P\\n" | sort)\n'
    )
    proc = subprocess.run(["bash", "-c", driver], env=env, text=True,
                          capture_output=True)
    assert proc.returncode == 0, (
        f"source 失败 rc={proc.returncode}\nSTDOUT:\n{proc.stdout}\n"
        f"STDERR:\n{proc.stderr}")
    sections: dict[str, list[str]] = {}
    cur = None
    for line in proc.stdout.splitlines():
        if line.startswith("---") and line.endswith("---"):
            cur = line.strip("-").lower()
            sections[cur] = []
        elif cur is not None:
            sections[cur].append(line)
    return sections


_PHASE25_DATA_ADDITIONS = {
    "d 700 phase25",
    "d 700 phase25/agentic_traces",
    "d 700 phase25/estimates",
    "d 700 phase25/probes",
    "d 700 phase25/quota",
    "d 700 phase25/quota/approvals",
    "d 700 phase25/quota/gateway_state",
    "f 600 phase25/quota/ledger.jsonl",
    "d 700 phase25/runs",
    "d 700 phase25/task10_milestone",
    "d 700 phase25/walls",
    "d 700 phase25/watchdog",
    "d 700 phase25/watchdog/incidents",
    "d 700 phase25/watchdog/triage_bundles",
    # SIMPRAXIS_RUN_DIR 默认就在 data 根下（data/runs/phase25）。
    "d 700 runs/phase25",
}


def test_env_script_pure_additive_vs_pre_t0_parent(tmp_path):
    # 基线钉死 T0 父本金（不可变 commit），严禁改回活 HEAD——HEAD 在 T0
    # 提交后含 phase25 块，两边逐字节相同会令 added 断言自指必红。
    parent_text = subprocess.run(
        ["git", "show", f"{PRE_T0_PARENT}:scripts/00_env.sh"], cwd=REPO_ROOT,
        text=True, capture_output=True, check=True).stdout
    cur_text = (REPO_ROOT / "scripts" / "00_env.sh").read_text(encoding="utf-8")
    shared = tmp_path / "shared"
    shared.mkdir()
    old = _source_snapshot(parent_text, shared)
    new = _source_snapshot(cur_text, shared)

    # env：仅允许新增 SIMPRAXIS_PHASE25_ROOT（默认落在 data 根下 phase25）。
    env_added = sorted(set(new["env"]) - set(old["env"]))
    assert sorted(set(old["env"]) - set(new["env"])) == []
    assert env_added == ["SIMPRAXIS_PHASE25_ROOT="
                         + str(shared / "data" / "phase25")]

    # 文件树：新增项必须恰好是 phase25 目录族；旧项形态一个字节都不改。
    added_tree = set(new["tree-data"]) - set(old["tree-data"])
    assert set(old["tree-data"]) - set(new["tree-data"]) == set()
    assert added_tree == _PHASE25_DATA_ADDITIONS
    assert set(new["tree-data"]) == (
        set(old["tree-data"]) | _PHASE25_DATA_ADDITIONS)
    assert set(new["tree-runs"]) - set(old["tree-runs"]) == {"d 700 phase25"}
    assert set(old["tree-runs"]) - set(new["tree-runs"]) == set()

    # 空账（父本金版不创建它，当前版首次 source 只 touch 不写内容）。
    ledger = shared / "data" / "phase25" / "quota" / "ledger.jsonl"
    assert ledger.stat().st_size == 0
    assert stat.S_IMODE(ledger.stat().st_mode) == 0o600


def test_env_script_idempotent_ledger_not_truncated(tmp_path):
    cur_text = (REPO_ROOT / "scripts" / "00_env.sh").read_text(encoding="utf-8")
    shared = tmp_path / "idem"
    shared.mkdir()
    first = _source_snapshot(cur_text, shared)
    ledger = shared / "data" / "phase25" / "quota" / "ledger.jsonl"
    ledger.write_text('{"sentinel": true}\n', encoding="utf-8")
    ledger.chmod(0o644)  # 权限被放宽时脚本必须收敛回 600
    second = _source_snapshot(cur_text, shared)
    assert ledger.read_text(encoding="utf-8") == '{"sentinel": true}\n'
    assert stat.S_IMODE(ledger.stat().st_mode) == 0o600
    # 两次快照 tree/env 完全一致（幂等；探针临时文件无残留）。
    assert first["tree-data"] == second["tree-data"]
    assert first["tree-runs"] == second["tree-runs"]
    assert first["env"] == second["env"]
    root = shared / "data" / "phase25"
    for rel in ("", "quota", "quota/approvals", "quota/gateway_state",
                "estimates", "watchdog", "watchdog/incidents",
                "watchdog/triage_bundles", "probes", "agentic_traces",
                "walls", "task10_milestone", "runs"):
        mode = stat.S_IMODE((root / rel).stat().st_mode)
        assert mode == 0o700, f"{rel} 必须 0700，实际 {mode:o}"


# ---------------------------------------------------------------------------
# 2) runtime_paths phase25 路径族 + 封存账守卫
# ---------------------------------------------------------------------------

@pytest.fixture()
def p25_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMPRAXIS_PHASE1_ROOT", str(tmp_path / "phase1"))
    monkeypatch.setenv("SIMPRAXIS_PHASE2_ROOT", str(tmp_path / "phase2"))
    monkeypatch.setenv("SIMPRAXIS_PHASE25_ROOT", str(tmp_path / "phase25"))
    monkeypatch.setenv("SIMPRAXIS_GATEWAY_STATE", str(tmp_path / "gwstate"))
    return tmp_path


def test_phase25_paths_resolve_and_private_dirs(p25_env, monkeypatch):
    assert rp.phase25_root() == p25_env / "phase25"
    assert rp.phase25_runs_root() == p25_env / "phase25" / "runs"
    qdir = rp.phase25_quota_dir()
    assert qdir == p25_env / "phase25" / "quota"
    assert stat.S_IMODE(qdir.stat().st_mode) == 0o700
    assert rp.phase25_ledger_path() == qdir / "ledger.jsonl"
    assert rp.phase25_approvals_dir() == qdir / "approvals"
    assert rp.phase25_gateway_state_path() == (
        p25_env / "phase25" / "quota" / "gateway_state" / "quota_pause.json")
    # env 缺省回退兄弟 phase25/（不建目录）。
    monkeypatch.delenv("SIMPRAXIS_PHASE25_ROOT", raising=False)
    assert rp.phase25_root() == rp.phase2_root().parent / "phase25"


def test_sealed_ledgers_reject_write_open(p25_env):
    sealed = rp.sealed_ledger_paths()
    assert rp.phase1_ledger_path().resolve(strict=False) in sealed
    assert rp.phase2_ledger_path().resolve(strict=False) in sealed
    # phase25 新账不在封存集。
    assert rp.phase25_ledger_path().resolve(strict=False) not in sealed
    with pytest.raises(rp.SealedLedgerError):
        rp.open_ledger_append(rp.phase2_ledger_path())
    with pytest.raises(rp.SealedLedgerError):
        rp.open_ledger_append(rp.phase1_ledger_path())
    # 缺省追加目标 = phase25 新账；写后权限 0600。
    with rp.open_ledger_append() as fh:
        fh.write('{"ok": true}\n')
    p = rp.phase25_ledger_path()
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert p.read_text(encoding="utf-8") == '{"ok": true}\n'


# ---------------------------------------------------------------------------
# 3) 常量/算术/命名空间不并表
# ---------------------------------------------------------------------------

def test_phase25_caps_constants():
    assert ql.PHASE25_TASK_CAPS == {3: 6, 5: 28, 9: 160, 10: 2}
    assert ql.PHASE25_SCIENTIFIC_TASKS == frozenset({3, 5})
    assert ql.PHASE25_HARD_CAP == 196
    assert ql.PHASE25_NORMAL_BUDGET == 196  # 预算全自批：无 beyond_normal 分层
    # 碰撞 task（3/5/9/10）绝不并表：phase2 caps 不含 3/10，task9 旧帽=4。
    assert 3 not in ql.TASK_HARD_CAPS
    assert 10 not in ql.TASK_HARD_CAPS
    assert ql.TASK_HARD_CAPS[9] == 4
    assert ql.TASK_HARD_CAPS[9] != ql.PHASE25_TASK_CAPS[9]
    assert ql.PHASE2_NAMESPACE.task_caps is ql.TASK_HARD_CAPS
    assert PHASE25_NAMESPACE.task_caps is ql.PHASE25_TASK_CAPS
    assert PHASE25_NAMESPACE.hard_cap == 196
    assert ql.PHASE2_NAMESPACE.ledger_path_fn() == rp.phase2_ledger_path()
    assert PHASE25_NAMESPACE.ledger_path_fn() == rp.phase25_ledger_path()
    # 暂停态分别走全局入口与 phase25 私有目录。
    assert PHASE25_NAMESPACE.pause_path_fn() == rp.phase25_gateway_state_path()
    assert ql.PHASE2_NAMESPACE.pause_path_fn() == rp.gateway_state_path()


# ---------------------------------------------------------------------------
# 4) phase25 独立账行为
# ---------------------------------------------------------------------------

@pytest.fixture()
def p25_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMPRAXIS_PHASE1_ROOT", str(tmp_path / "phase1"))
    monkeypatch.setenv("SIMPRAXIS_PHASE2_ROOT", str(tmp_path / "phase2"))
    monkeypatch.setenv("SIMPRAXIS_PHASE25_ROOT", str(tmp_path / "phase25"))
    monkeypatch.setenv("SIMPRAXIS_GATEWAY_STATE", str(tmp_path / "gwstate"))
    (tmp_path / "gwstate").mkdir(exist_ok=True)
    return {"tmp": tmp_path,
            "ledger": tmp_path / "phase25" / "quota" / "ledger.jsonl",
            "pause25": (tmp_path / "phase25" / "quota" / "gateway_state"
                        / "quota_pause.json"),
            "global_pause": tmp_path / "gwstate" / "quota_pause.json"}


def _rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def test_phase25_empty_ledger_first_seq_one_strict_z(p25_ledger):
    led = ql.QuotaLedger.phase25()
    ql.write_approval(batch_id=BATCH, task=10, stage=STAGE, approved=4,
                      beyond_normal=False, note="fixture（零付费）",
                      namespace=PHASE25_NAMESPACE)
    pre = led.preflight(STAGE, task=10, batch_id=BATCH)
    assert pre == {"global_used": 0, "batch_used": 0, "task_used": 0,
                   "batch_approved": 4}
    row = led.record(STAGE, task=10, batch_id=BATCH, ok=True,
                     model="fixture-model", thread_id="t-p25-1")
    assert row["seq"] == 1 and row["event"] == "call"
    parse_ts_iso_strict(row["ts_iso"])  # 严格 Z
    row2 = led.record(STAGE, task=10, batch_id=BATCH, ok=False,
                      model="fixture-model", thread_id="t-p25-1")
    assert row2["seq"] == 2 and row2["global_used_after"] == 2
    # 批准件落 phase25 目录且带 ledger_phase=25。
    approval = json.loads((p25_ledger["tmp"] / "phase25" / "quota"
                           / "approvals" / f"{BATCH}.json").read_text())
    assert approval["ledger_phase"] == 25
    # 机判正账通过。
    ql.validate_ledger_rows(_rows(p25_ledger["ledger"]))
    # phase2 账一个字节都没被写（目录可能由 conftest/路径解析预建，只认账文件）。
    assert not (p25_ledger["tmp"] / "phase2" / "quota" / "ledger.jsonl").exists()


def test_phase25_approval_namespace_membership(p25_ledger):
    # 旧期 task 2 不得在 phase25 出票；默认命名空间（phase2）不认识独占 task 10。
    with pytest.raises(ql.QuotaError, match="不属于 ledger 期 25"):
        ql.write_approval(batch_id="bad-old-task", task=2, stage=STAGE,
                          approved=1, beyond_normal=False, note="x",
                          namespace=PHASE25_NAMESPACE)
    with pytest.raises(ql.QuotaError, match="不属于 ledger 期 2"):
        ql.write_approval(batch_id="bad-new-task", task=10, stage=STAGE,
                          approved=1, beyond_normal=False, note="x")
    # 全局硬门 196：首批 197 直接拒（used=0）。
    with pytest.raises(ql.QuotaError, match="196"):
        ql.write_approval(batch_id="too-big", task=9, stage=STAGE,
                          approved=197, beyond_normal=False, note="x",
                          namespace=PHASE25_NAMESPACE)


def test_phase25_blocked_occupies_batch_not_calls(p25_ledger):
    # 无批准件 → 不发网拦截 + null-seq blocked 行（批次计 1，实呼计 0）。
    led = ql.QuotaLedger.phase25()
    with pytest.raises(GatewayPausedError):
        led.preflight(STAGE, task=9, batch_id=BATCH)
    ql.write_approval(batch_id=BATCH, task=9, stage=STAGE, approved=2,
                      beyond_normal=False, note="fixture",
                      namespace=PHASE25_NAMESPACE)
    # task9 帽 160 不参与本断言；blocked 占批次：此刻批次已用 1。
    assert led.preflight(STAGE, task=9, batch_id=BATCH) == {
        "global_used": 0, "batch_used": 1, "task_used": 0,
        "batch_approved": 2}
    led.record(STAGE, task=9, batch_id=BATCH, ok=True, model="m",
               thread_id="t")
    led.record(STAGE, task=9, batch_id=BATCH, ok=True, model="m",
               thread_id="t")
    # 第 3 次：2 blocked + 2 calls 将占满批次 2（blocked 占批次的核心断言；
    # 本批先撞批次门，task9=160 任务门不参与）。
    with pytest.raises(GatewayPausedError):
        led.preflight(STAGE, task=9, batch_id=BATCH)
    rows = _rows(p25_ledger["ledger"])
    ql.validate_ledger_rows(rows)
    summary = ql.ledger_summary(rows)
    assert summary["call_rows"] == 2
    assert summary["blocked_rows"] == 2
    assert summary["total_rows"] == 4
    assert summary["batches"][BATCH] == {"calls": 2, "blocked": 2, "rows": 4}
    # 暂停态写在 phase25 私有目录，全局 gateway_state 零写入。
    assert p25_ledger["pause25"].exists()
    assert not p25_ledger["global_pause"].exists()
    state = json.loads(p25_ledger["pause25"].read_text())
    assert state["reason"] == "batch_budget_reached"


def test_phase25_scientific_task_cap_enforced(p25_ledger):
    ql.write_approval(batch_id=BATCH, task=3, stage="macro_ideation",
                      approved=10, beyond_normal=False, note="fixture",
                      namespace=PHASE25_NAMESPACE)
    led = ql.QuotaLedger.phase25()
    for _ in range(6):
        led.preflight("macro_ideation", task=3, batch_id=BATCH)
        led.record("macro_ideation", task=3, batch_id=BATCH, ok=True,
                   model="m", thread_id="t3")
    with pytest.raises(GatewayPausedError):
        led.preflight("macro_ideation", task=3, batch_id=BATCH)
    state = json.loads(p25_ledger["pause25"].read_text())
    assert state["reason"] == "task_hard_cap"
    assert state["scope"] == "task"
    # task_hard_cap 不可用 resume+批次扩展恢复（科学硬帽绝对上限）。
    with pytest.raises(ql.QuotaError, match="任务硬顶不可恢复"):
        ql.resume(ticket="golden-resume-1234", batch_id=BATCH,
                  namespace=PHASE25_NAMESPACE)


def test_phase25_resume_writes_audit_in_namespace(p25_ledger):
    ql._write_budget_pause(
        "consecutive_quota_errors",
        {"consecutive_errors": 3, "stage": STAGE}, ns=PHASE25_NAMESPACE)
    audit = ql.resume(ticket="golden-resume-1234", namespace=PHASE25_NAMESPACE)
    assert audit == (p25_ledger["tmp"] / "phase25" / "quota"
                     / "resume_audit.jsonl")
    line = json.loads(audit.read_text().splitlines()[0])
    assert line["ledger_phase"] == 25
    assert not p25_ledger["pause25"].exists()
    assert stat.S_IMODE(audit.stat().st_mode) == 0o600


def test_cli_phase25_status_and_approve(p25_ledger, capsys):
    # 旧期 task 2：argparse choices 取并集放行，写侧命名空间校验拒绝（exit 2）。
    rc = ql.main(["--ledger-phase", "25", "approve", "--batch-id", "x1",
                  "--task", "2", "--stage", STAGE, "--approved", "1",
                  "--note", "x"])
    assert rc == 2
    assert not (p25_ledger["tmp"] / "phase25" / "quota" / "approvals"
                / "x1.json").exists()
    capsys.readouterr()  # 排干错误文案
    # 合法出票。
    rc = ql.main(["--ledger-phase", "25", "approve", "--batch-id", "a1",
                  "--task", "10", "--stage", STAGE, "--approved", "2",
                  "--note", "fixture"])
    assert rc == 0
    doc = json.loads((p25_ledger["tmp"] / "phase25" / "quota" / "approvals"
                      / "a1.json").read_text())
    assert doc["ledger_phase"] == 25
    capsys.readouterr()
    # status：phase25 帽集/路径正确。
    assert ql.main(["--ledger-phase", "25", "status"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ledger_phase"] == 25
    assert out["hard_cap"] == 196 and out["normal_budget"] == 196
    assert out["task_caps"] == {"3": 6, "5": 28, "9": 160, "10": 2}
    assert out["ledger_path"].endswith("phase25/quota/ledger.jsonl")
    assert out["call_rows"] == 0 and out["blocked_rows"] == 0


def test_cli_phase2_status_tolerates_legacy_tz_rows(tmp_path, monkeypatch, capsys):
    # phase2 封存账含 +0000 历史后缀：严格机判不回溯旧账，status 必须不炸。
    monkeypatch.setenv("SIMPRAXIS_PHASE1_ROOT", str(tmp_path / "phase1"))
    monkeypatch.setenv("SIMPRAXIS_PHASE2_ROOT", str(tmp_path / "phase2"))
    monkeypatch.setenv("SIMPRAXIS_GATEWAY_STATE", str(tmp_path / "gwstate"))
    qdir = tmp_path / "phase2" / "quota"
    qdir.mkdir(parents=True)
    (qdir / "ledger.jsonl").write_text(
        json.dumps({"ts_iso": "2026-10-15T08:00:00+0000", "event": "call",
                    "stage": STAGE, "model": "m", "thread_id": "t",
                    "task": 2, "batch_id": "legacy",
                    "seq": 1, "global_used_after": 1, "ok": True}) + "\n",
        encoding="utf-8")
    assert ql.main(["status"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ledger_phase"] == 2 and out["global_used"] == 1


# ---------------------------------------------------------------------------
# 5) AC-T0-7 台账机判：正账 + 五负例 + 两栏汇总
# ---------------------------------------------------------------------------

def _call(seq: int, *, batch=BATCH, task=9, stage=STAGE, ok=True,
          ts="2026-10-29T00:00:00Z") -> dict:
    return {"ts_iso": ts, "event": "call", "stage": stage, "model": "m",
            "thread_id": "t", "task": task, "batch_id": batch, "seq": seq,
            "global_used_after": seq, "ok": ok}


def _blocked(batch=BATCH, ts="2026-10-29T00:00:00Z") -> dict:
    return {"ts_iso": ts, "event": "preflight_blocked", "stage": STAGE,
            "model": None, "thread_id": "t", "task": 9, "batch_id": batch,
            "seq": None, "global_used_after": 0, "ok": False}


def test_judge_positive_continuous_with_blocked_column():
    rows = [_blocked(), _call(1), _call(2), _call(3),
            _blocked(batch="OTHER")]
    ql.validate_ledger_rows(rows)  # 不抛即通过
    s = ql.ledger_summary(rows)
    assert s["call_rows"] == 3 and s["blocked_rows"] == 2
    assert s["total_rows"] == 5
    assert (s["seq_first"], s["seq_last"]) == (1, 3)
    assert s["calls_ok"] == {"true": 3, "false": 0}
    assert s["calls_by_task"] == {"9": 3}
    assert s["batches"][BATCH] == {"calls": 3, "blocked": 1, "rows": 4}
    assert s["batches"]["OTHER"] == {"calls": 0, "blocked": 1, "rows": 1}
    # 空账 = 0 次实呼，合法（首行必须 seq=1）。
    ql.validate_ledger_rows([])
    s0 = ql.ledger_summary([])
    assert s0["call_rows"] == 0 and s0["seq_first"] is None


@pytest.mark.parametrize("rows,marker", [
    ([_call(1), _call(2), _call(4)], "跳号"),
    ([_call(1), _call(1), _call(2)], "重号"),
    ([_call(1), {**_call(2), "event": "preflight_blocked"}], "event 必须为 call"),
    ([{**_blocked(), "event": "call"}], "event 必须为 preflight_blocked"),
])
def test_judge_negatives_red(rows, marker):
    with pytest.raises(LedgerSequenceError) as ei:
        ql.validate_ledger_rows(rows)
    assert marker in str(ei.value)


def test_judge_bool_seq_and_non_z_ts_and_bad_type():
    with pytest.raises(LedgerSequenceError, match="bool"):
        ql.validate_ledger_rows([{**_call(1), "seq": True}])
    with pytest.raises(LedgerSequenceError, match="UTC Z"):
        ql.validate_ledger_rows([_call(1, ts="2026-10-29T00:00:00+0800")])
    with pytest.raises(LedgerSequenceError):
        ql.validate_ledger_rows([{**_call(1), "seq": "1"}])
    # 非严格 Z 串不得混入 fp（parse_ts_iso_strict 同源）。
    with pytest.raises(ValueError):
        parse_ts_iso_strict("2026-10-29T00:00:00+0000")


# ---------------------------------------------------------------------------
# 6) 依赖探针裁决器（脱敏夹具；纯本地解析）
# ---------------------------------------------------------------------------

def _load_fixture(tag: str) -> dict:
    return json.loads((FIXTURE_DIR / f"dryrun_{tag}_sanitized.json").read_text())


def _freeze_pair(tmp_path: Path, *, with_smolagents: bool) -> tuple[Path, Path]:
    base = {p: pkg for p, pkg in (
        ("numpy", "1.26.4"), ("torch", "2.4.1+cu124"),
        ("pydantic", "2.11.10"), ("langgraph", "0.2.28"),
        ("lark-oapi", "1.7.4"), ("huggingface-hub", "1.33.0"),
        ("requests", "2.32.3"), ("rich", "15.0.0"), ("jinja2", "3.1.6"),
        ("pillow", "11.3.0"), ("python-dotenv", "1.2.4"))}
    after = dict(base)
    if with_smolagents:
        after["smolagents"] = "1.26.0"
    before = tmp_path / "freeze_before.txt"
    after_p = tmp_path / "freeze_after.txt"
    before.write_text("\n".join(f"{k}=={v}" for k, v in
                                sorted(base.items())) + "\n", encoding="utf-8")
    after_p.write_text("\n".join(f"{k}=={v}" for k, v in
                                 sorted(after.items())) + "\n", encoding="utf-8")
    return before, after_p


def _pipcheck_pair(tmp_path: Path) -> tuple[Path, Path]:
    warn = ("torch 2.4.1+cu124 has requirement nvidia-cuda-nvrtc-cu12==12.4.99;"
            " but you have nvidia-cuda-nvrtc-cu12 12.9.86.")
    pb, pa = tmp_path / "pc_before.txt", tmp_path / "pc_after.txt"
    pb.write_text(warn + "\n", encoding="utf-8")
    pa.write_text(warn + "\n", encoding="utf-8")
    return pb, pa


def _reports(tmp_path: Path, *, inject_drift: bool = False) -> dict[str, Path]:
    full = _load_fixture("smolagents_full")
    if inject_drift:
        # 模拟全依赖 dry-run 将重装 numpy 2.3.1（撞五 pin）。
        full["install"].append({"metadata": {
            "name": "numpy", "version": "2.3.1", "requires_dist": []}})
    paths: dict[str, Path] = {}
    for tag, doc in (("nodeps", _load_fixture("smolagents_nodeps")),
                     ("full", full),
                     ("openai", _load_fixture("openai_agents"))):
        p = tmp_path / f"report_{tag}.json"
        p.write_text(json.dumps(doc), encoding="utf-8")
        paths[tag] = p
    return paths


def test_verdict_main_branch_zero_drift(tmp_path):
    before, after = _freeze_pair(tmp_path, with_smolagents=True)
    pb, pa = _pipcheck_pair(tmp_path)
    paths = _reports(tmp_path)
    v = verdict_mod.build_verdict(
        nodeps_report_path=paths["nodeps"], full_report_path=paths["full"],
        openai_report_path=paths["openai"], freeze_before_path=before,
        freeze_after_path=after, pipcheck_before_path=pb,
        pipcheck_after_path=pa, pipcheck_before_rc=1, pipcheck_after_rc=1,
        install={"performed": True, "rc": 0}, interpreter=VENV_PY,
        probe_dir=tmp_path)
    assert v["schema"] == "simpraxis.phase25.t0_dependency_verdict.v1"
    assert v["ark_paid_calls"] == 0
    assert v["decision"]["branch"] == "main_venv"
    assert v["decision"]["pin_drift"] == []
    assert all(x["unchanged"] for x in v["five_pins"].values())
    # 六轻包：主依赖约束串来自 nodeps 报告的 requires_dist（marker 依赖剔除）、
    # 现装版本在场、全依赖 dry-run 不重装（六包全部 already satisfied）。
    reqs = {name: v["six_lights"][name]["requires_dist"]
            for name in verdict_mod.SIX_LIGHTS}
    assert set(reqs) == set(verdict_mod.SIX_LIGHTS)
    assert reqs["requests"] == ["requests>=2.32.3"]
    assert reqs["python-dotenv"] == ["python-dotenv"]
    assert reqs["pillow"] == ["pillow>=10.0.1"]
    assert v["six_lights"]["python-dotenv"]["installed_before"] == "1.2.4"
    assert all(not x["in_full_dry_run_would_install"]
               for x in v["six_lights"].values())
    # freeze 只多了 smolagents 一行；五 pin/六轻包无升无降无删。
    assert v["freeze_diff"]["added"] == ["smolagents==1.26.0"]
    assert v["freeze_diff"]["changed"] == []
    assert v["freeze_diff"]["removed"] == []
    # pip check 既有 torch/nvidia 告警原样保留（非本探针引入）。
    assert v["pip_check"]["identical"] is True
    assert v["pip_check"]["new_issues"] == []
    assert len(v["pip_check"]["before_lines"]) == 1
    assert v["target"]["present_after"] is True
    # openai-agents 不安装且记录 pydantic 撞 pin。
    assert v["dry_run"]["openai_agents"]["installed"] is False
    conflicts = {c["package"]: c for c in v["openai_agents_conflicts"]}
    assert conflicts["pydantic"]["would_install"] == "2.14.1"
    assert conflicts["pydantic"]["breaks_pin"] is True


def test_verdict_isolated_branch_on_pin_drift(tmp_path):
    before, after = _freeze_pair(tmp_path, with_smolagents=False)
    pb, pa = _pipcheck_pair(tmp_path)
    paths = _reports(tmp_path, inject_drift=True)
    v = verdict_mod.build_verdict(
        nodeps_report_path=paths["nodeps"], full_report_path=paths["full"],
        openai_report_path=paths["openai"], freeze_before_path=before,
        freeze_after_path=after, pipcheck_before_path=pb,
        pipcheck_after_path=pa, pipcheck_before_rc=1, pipcheck_after_rc=1,
        install={"performed": False, "rc": None}, interpreter=VENV_PY,
        probe_dir=tmp_path)
    assert v["decision"]["branch"] == "isolated"
    drift = {d["canonical"]: d["version"] for d in v["decision"]["pin_drift"]}
    assert drift == {"numpy": "2.3.1"}


# ---------------------------------------------------------------------------
# 7) 测试基线冻结器（纯解析；不跑收集子进程）
# ---------------------------------------------------------------------------

def test_freeze_baseline_parses_counts_and_skipped(tmp_path):
    raw = tmp_path / "raw.log"
    raw.write_text(
        "....s...\n"
        "SHORT TEST SUMMARY:\n"
        "tests/milestone1/test_x.py::test_a SKIPPED [ 50%]\n"
        "tests/milestone1/test_x.py::test_b PASSED [ 51%]\n"
        "SKIPPED [1] tests/milestone1/test_x.py:42: paid marker guard\n"
        "1202 passed, 1 skipped, 42 deselected, 629 warnings in 387.55s\n"
        "EXIT=0\n", encoding="utf-8")
    doc = baseline_mod.build_baseline(
        prechange_log=raw, verbose_log=raw, python=VENV_PY, raw_collect=False)
    c = doc["counts"]
    assert (c["passed"], c["skipped"], c["deselected"], c["failed"]) == (
        1202, 1, 42, 0)
    assert doc["skipped_names"] == [
        "tests/milestone1/test_x.py::test_a"]
    assert doc["skipped_reasons"][0]["reason"] == "paid marker guard"
    assert doc["schema"] == "simpraxis.phase25.t0_test_baseline.v1"


def test_freeze_baseline_rejects_non_green(tmp_path):
    raw = tmp_path / "raw.log"
    raw.write_text("2 failed, 1200 passed, 1 skipped in 300s\nEXIT=1\n",
                   encoding="utf-8")
    with pytest.raises(ValueError, match="非全绿"):
        baseline_mod.build_baseline(prechange_log=raw, verbose_log=None,
                                    python=VENV_PY, raw_collect=False)


# ---------------------------------------------------------------------------
# 8) 旧路径出生 prompt 黄金快照：tmp 重生成逐字节一致（Task 4 回归闸）
# ---------------------------------------------------------------------------

def test_golden_snapshots_byte_regression(tmp_path):
    assert GOLDEN_DIR.is_dir(), "入库黄金快照缺失"
    rebuilt = tmp_path / "phase25_golden"
    manifest_new = golden_mod.build_golden(rebuilt)
    committed = sorted(p.relative_to(GOLDEN_DIR) for p in GOLDEN_DIR.rglob("*")
                       if p.is_file())
    rebuilt_files = sorted(p.relative_to(rebuilt) for p in rebuilt.rglob("*")
                           if p.is_file())
    assert committed == rebuilt_files
    for rel in committed:
        if rel.name == "manifest.json":
            continue
        assert (GOLDEN_DIR / rel).read_bytes() == (rebuilt / rel).read_bytes(), (
            f"黄金 prompt 漂移：{rel}（Task 4 必须解释或回退，不得静默改 prompt）")
    m1 = json.loads((GOLDEN_DIR / "manifest.json").read_text())
    assert m1["schema"] == manifest_new["schema"] == "simpraxis.phase25.t0_golden.v1"
    assert m1["fixed_inputs"] == manifest_new["fixed_inputs"]
    assert m1["factories"] == manifest_new["factories"]
    # 13 条记录：task6 6 拆块 + task7 1 单补丁 + task8 6 拆块；键形状锁定。
    n = sum(len(f["records"]) for f in m1["factories"].values())
    assert n == 13
    expected_chunk_counts = {"task6_ascent": 6, "task7_libero": 1,
                             "task8_fluid": 6}
    for fname, f in m1["factories"].items():
        assert f["chunk_count"] == expected_chunk_counts[fname]
        for r in f["records"]:
            # M-A（2026-10-29 评审修单）：13 件入库 cache_key 必须等于生产
            # 单一源 helper 的输出——测试侧禁止再自拼同形 f-string，否则冻结器
            # 与生产双侧同源漂移时本测试不会红。
            assert r["run_idx"] == 1 and r["round"] == 1
            assert r["cache_key"] == coding_call_cache_key(
                r["thread_id"], r["run_idx"], r["round"],
                chunk_index=r["chunk_index"])
            # sha256/指纹：64 位小写十六进制；prompt 全文与入库文件逐字一致。
            assert re.fullmatch(r"[0-9a-f]{64}", r["prompt_sha256"])
            assert re.fullmatch(r"[0-9a-f]{64}", r["request_fingerprint"])
            assert r["model"] == "golden-oldpath-model"
            prompt_path = GOLDEN_DIR / r["prompt_canonical_json"]
            assert hashlib.sha256(
                prompt_path.read_bytes()).hexdigest() == r["prompt_sha256"]


# ---------------------------------------------------------------------------
# 9) RunSpec 字段集冻结：Task0 红线——agentic 字段不得进旧路径出生契约
# ---------------------------------------------------------------------------

def test_runspec_field_set_frozen_no_agentic_fields():
    fields = {f.name for f in dataclasses.fields(golden_mod.runner_mod.RunSpec)}
    # 2026-10-29 Task0 时点的 25 字段完整集合。Task 4 接入 agentic 层若必须新增，
    # 本测试会先红——按交接件 STOP 并上报，不得在 Task0 静默扩字段。
    frozen_fields = {
        "thread_id", "workdir", "cmd", "telemetry_relpath", "batch_id",
        "run_idx", "billing_task", "gate2_notes", "env_extra",
        "run_timeout_s", "task_title", "video_relpath", "cover_relpath",
        "manifest_relpath", "max_coding_rounds", "coding_max_tokens",
        "coding_timeout_s", "extra_prompt_sections", "chunked_birth",
        "batch_schema_check", "shim_topology_check", "coding_recall",
        "repair_full_source", "repair_prior_refs", "full_source_relpaths",
    }
    assert fields == frozen_fields


# ---------------------------------------------------------------------------
# 10) 2026-10-29 评审修单：M-A 键形单一源 / M-B 三根互异 fail-fast /
#     L3 freeze_diff 去双列 / caps MappingProxyType 不可变
# ---------------------------------------------------------------------------

def test_coding_call_cache_key_literal_pins():
    # 无 chunk 形：完整字面量钉死（含 t25- 前缀与两位 run_idx）。
    assert coding_call_cache_key("t25-alpha", 1, 1) == (
        "coding_t25-alpha_r1_round1")
    assert coding_call_cache_key("t25-bravo", 27, 4) == (
        "coding_t25-bravo_r27_round4")
    # chunk 形：chunk_index=0 必须出后缀（is not None 语义，防 truthy 误写）。
    assert coding_call_cache_key("t25-alpha", 1, 1, chunk_index=0) == (
        "coding_t25-alpha_r1_round1_chunk0")
    assert coding_call_cache_key("t25-bravo", 27, 5, chunk_index=11) == (
        "coding_t25-bravo_r27_round5_chunk11")


def test_freeze_diff_changed_added_removed_disjoint():
    fdiff = verdict_mod._freeze_diff
    # 版本变更只进 changed，不被 added 双列（L3）。
    assert fdiff({"a": "1", "b": "2"}, {"a": "1", "b": "3"}) == {
        "added": [], "removed": [], "changed": ["b: 2 -> 3"]}
    # 新包只进 added。
    assert fdiff({"a": "1"}, {"a": "1", "c": "9"}) == {
        "added": ["c==9"], "removed": [], "changed": []}
    # 删除只进 removed。
    assert fdiff({"a": "1", "b": "2"}, {"a": "1"}) == {
        "added": [], "removed": ["b==2"], "changed": []}
    # 同现同版本三栏皆空。
    assert fdiff({"a": "1"}, {"a": "1"}) == {
        "added": [], "removed": [], "changed": []}


def test_task_caps_are_readonly_proxy():
    from types import MappingProxyType
    assert isinstance(ql.TASK_HARD_CAPS, MappingProxyType)
    assert isinstance(ql.PHASE25_TASK_CAPS, MappingProxyType)
    # 命名空间持有的就是同一代理对象（既有 is 身份断言保持成立）。
    assert ql.PHASE2_NAMESPACE.task_caps is ql.TASK_HARD_CAPS
    assert PHASE25_NAMESPACE.task_caps is ql.PHASE25_TASK_CAPS
    # 原地提顶必须立即 TypeError：提顶只能改源码常量+溯源注释随提交走。
    with pytest.raises(TypeError):
        ql.PHASE25_TASK_CAPS[5] = 999
    with pytest.raises(TypeError):
        ql.TASK_HARD_CAPS[6] = 999


def _tree_signature(root: Path) -> dict[str, str]:
    """封存树的完整指纹：相对路径 -> 类型/内容 sha256（任何多写即红）。"""
    sig: dict[str, str] = {}
    for p in sorted(root.rglob("*")):
        rel = str(p.relative_to(root))
        if p.is_symlink():
            sig[rel] = "symlink->" + os.readlink(p)
        elif p.is_dir():
            sig[rel] = "dir"
        else:
            sig[rel] = "file:" + hashlib.sha256(p.read_bytes()).hexdigest()
    return sig


def _build_fake_sealed_tree(base: Path) -> Path:
    """构造含各型哨兵文件的伪造封存 phase2 树（ledger/approval/pause/锁/runs）。"""
    p2 = base / "phase2"
    q = p2 / "quota"
    (q / "approvals").mkdir(parents=True)
    (q / "gateway_state").mkdir()
    (p2 / "runs").mkdir(parents=True)
    (q / "ledger.jsonl").write_text(
        '{"seq": 455, "sealed": true}\n', encoding="utf-8")
    (q / "ledger.jsonl").chmod(0o600)
    (q / "approvals" / "keep.json").write_text(
        '{"keep": true}\n', encoding="utf-8")
    (q / "gateway_state" / "quota_pause.json").write_text(
        '{"x": 1}\n', encoding="utf-8")
    (q / "ledger.degraded.keep").write_text("{}", encoding="utf-8")
    (q / ".ledger.lock").write_text("", encoding="utf-8")
    (q / "resume_audit.jsonl").write_text("", encoding="utf-8")
    (p2 / "runs" / "sentinel").write_text("sealed-run", encoding="utf-8")
    (base / "phase1" / "quota").mkdir(parents=True)
    return p2


def _assert_three_choke_points_raise() -> None:
    """M-B 三个 Python 卡口（构造/write_approval/resume）全部 fail-fast。"""
    with pytest.raises(rp.PhaseRootAliasedError):
        rp.assert_phase25_roots_isolated()
    with pytest.raises(rp.PhaseRootAliasedError):
        ql.QuotaLedger.phase25()
    with pytest.raises(rp.PhaseRootAliasedError):
        ql.write_approval(
            batch_id="P25-T-MBALIAS", task=10, stage="coding_debug",
            approved=1, beyond_normal=False, note="mb-alias",
            namespace=PHASE25_NAMESPACE)
    with pytest.raises(rp.PhaseRootAliasedError):
        ql.resume(ticket="mbticket-1234", namespace=PHASE25_NAMESPACE)


def test_phase25_aliased_root_fail_fast_zero_tree_write(tmp_path, monkeypatch):
    p2 = _build_fake_sealed_tree(tmp_path)
    monkeypatch.setenv("SIMPRAXIS_PHASE1_ROOT", str(tmp_path / "phase1"))
    monkeypatch.setenv("SIMPRAXIS_PHASE2_ROOT", str(p2))
    monkeypatch.setenv("SIMPRAXIS_PHASE25_ROOT", str(p2))  # 直接同体别名
    monkeypatch.setenv("SIMPRAXIS_GATEWAY_STATE", str(tmp_path / "gwstate"))
    before = _tree_signature(p2)
    _assert_three_choke_points_raise()
    # phase2 命名空间不受影响（隔离检查只对 phase25；其 quota_dir 已存在）。
    ql.QuotaLedger()
    after = _tree_signature(p2)
    assert after == before  # 封存树文件集与内容一字节不变（零 mkdir/零写）


def test_phase25_nested_root_rejected(tmp_path, monkeypatch):
    p2 = _build_fake_sealed_tree(tmp_path)
    monkeypatch.setenv("SIMPRAXIS_PHASE1_ROOT", str(tmp_path / "phase1"))
    monkeypatch.setenv("SIMPRAXIS_PHASE2_ROOT", str(p2))
    # 嵌套别名：phase25 根落在封存树的 quota 子树内。
    monkeypatch.setenv("SIMPRAXIS_PHASE25_ROOT", str(p2 / "quota"))
    monkeypatch.setenv("SIMPRAXIS_GATEWAY_STATE", str(tmp_path / "gwstate"))
    before = _tree_signature(p2)
    _assert_three_choke_points_raise()
    assert _tree_signature(p2) == before
    # 反向嵌套（封存根在 phase25 根内）同样拒绝。
    monkeypatch.setenv("SIMPRAXIS_PHASE2_ROOT", str(tmp_path / "outer" / "sealed"))
    monkeypatch.setenv("SIMPRAXIS_PHASE25_ROOT", str(tmp_path / "outer"))
    with pytest.raises(rp.PhaseRootAliasedError):
        rp.assert_phase25_roots_isolated()


def test_phase25_sibling_roots_unaffected(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMPRAXIS_PHASE1_ROOT", str(tmp_path / "phase1"))
    monkeypatch.setenv("SIMPRAXIS_PHASE2_ROOT", str(tmp_path / "phase2"))
    monkeypatch.setenv("SIMPRAXIS_PHASE25_ROOT", str(tmp_path / "phase25"))
    monkeypatch.setenv("SIMPRAXIS_GATEWAY_STATE", str(tmp_path / "gwstate"))
    (tmp_path / "gwstate").mkdir()
    # 纯函数不抛。
    assert rp.assert_phase25_roots_isolated() is None
    # 卡口 1：phase25 账本正常构造（并建出私有 quota 目录）。
    led = ql.QuotaLedger.phase25()
    assert led.ns.phase == ql.LEDGER_PHASE25
    # 卡口 2：凭据正常落盘（0600）。
    p = ql.write_approval(
        batch_id="P25-T-MBOK", task=10, stage="coding_debug",
        approved=2, beyond_normal=False, note="mb-ok",
        namespace=PHASE25_NAMESPACE)
    assert p.exists()
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    # 卡口 3：隔离闸通过后才进入业务校验——无暂停件报 QuotaError 而非
    # PhaseRootAliasedError，证明顺序正确。
    with pytest.raises(ql.QuotaError, match="无暂停态文件"):
        ql.resume(ticket="mbticket-1234", namespace=PHASE25_NAMESPACE)


def _source_env_subprocess(shared: Path, phase25_override: str):
    """以受控 env source 仓内 00_env.sh，返回 (rc, stderr 文本)。"""
    data = shared / "data"
    env = dict(os.environ)
    env.update({
        "SIMPRAXIS_ROOT": str(REPO_ROOT),
        "SIMPRAXIS_DATA_ROOT": str(data),
        "SIMPRAXIS_CACHE_ROOT": str(shared / "cache"),
        "KB_ROOT": str(shared / "kb"),
        "SIMPRAXIS_RUN_DIR": str(data / "runs"),
        "SIMPRAXIS_GATEWAY_STATE": str(data / "gateway_state"),
        "SIMPRAXIS_TOURS_DIR": str(shared / "tours"),
        "SIMPRAXIS_USE_PROXY": "0",
        "SIMPRAXIS_PHASE1_ROOT": str(data / "phase1"),
        "SIMPRAXIS_PHASE2_ROOT": str(data / "phase2"),
        "SIMPRAXIS_PHASE25_ROOT": phase25_override,
    })
    err = shared / "source.err"
    script = REPO_ROOT / "scripts" / "00_env.sh"
    driver = (f"source '{script}' >/dev/null 2>'{err}'; rc=$?; "
              f'echo "RC=$rc"')
    proc = subprocess.run(["bash", "-c", driver], env=env, text=True,
                          capture_output=True)
    m = re.search(r"RC=(\d+)", proc.stdout)
    rc = int(m.group(1)) if m else -1
    return rc, err.read_text(encoding="utf-8") if err.exists() else ""


@pytest.mark.parametrize("suffix", ["", "quota"])
def test_env_script_rejects_aliased_phase25_root(tmp_path, suffix):
    # ③ 别名（同体）/嵌套两种状态下 source 必须非零退出，且 phase25 块零建目录。
    shared = tmp_path / "aliased"
    shared.mkdir()
    data = shared / "data"
    alias25 = str(data / "phase2" / suffix) if suffix else str(data / "phase2")
    rc, err = _source_env_subprocess(shared, alias25)
    assert rc != 0, f"别名根必须拒绝，stdout/err:\n{err}"
    assert "SIMPRAXIS_PHASE25_ROOT" in err and "STOP" in err
    # 默认 phase25 根与 quota 嵌套子树均不得被创建。
    assert not (data / "phase25").exists()
    assert not (data / "phase2" / "quota" / "quota").exists()
    # 对照：正常兄弟根 source 成功，且 phase2 树与被拒跑的 phase2 树同集
    # （phase25 块没往封存树多落任何东西）。
    ctrl = tmp_path / "control"
    ctrl.mkdir()
    cdata = ctrl / "data"
    rc_ok, err_ok = _source_env_subprocess(ctrl, str(cdata / "phase25"))
    assert rc_ok == 0, f"兄弟根必须正常：{err_ok}"
    rejected = {str(p.relative_to(data / "phase2"))
                for p in (data / "phase2").rglob("*")}
    controlled = {str(p.relative_to(cdata / "phase2"))
                  for p in (cdata / "phase2").rglob("*")}
    assert rejected == controlled
