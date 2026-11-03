"""Phase 2.6 Task4 AC-T4-3/4/5 的纯函数机判（0 付费、全 hermetic）。

覆盖（计划 §T4.4-F + §T4.6）：

- **AC-T4-3 的 iff 双向**：合成 ``WallResult`` 覆盖 2³ = 8 种组合
  （墙 A 过/不过 × 墙 B 过/不过 × ratio 达标/不达标），断言
  ``verdict == "ESTABLISHED"`` **当且仅当**三者全成立；任一组合映射错即红。
- **`INCONCLUSIVE` 的窄口**：只在 (i) 两墙未全过 **且** (ii) 基础设施故障
  **且** (iii) 共享池用满 28 时成立；三条各自缺失都回落
  ``NOT_ESTABLISHED``（防「失败即不确定」的软化）。
- **三态常量纪元**：v3 用英文字面量，**不得**复用 phase25 的中文三态
  （plan §T4.4-B 注：import 了就会让 ``verdict=="ESTABLISHED"`` 恒假）。
- **productive 轮推导**（判据 §3 预注册定义）：allow+非空观察才算产出；
  deny / 空观察 / 控制行（``__condense__``）/ 无模型轮行的残行都不算。
- **AC-T4-4 判据 LOCK 三重取证**：sha256 + mtime + mode 任一不符即拒；
  首呼后改写判据使整批作废。

全部合成数据，不读活账、不触网、不起容器。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.phase26 import run_wall_retest as R
from scripts.phase25.walls import verdict as p25_verdict


# --------------------------------------------------------------- 工具

def _rec(*, passed: bool, rounds: int, productive: int,
         infra: bool = False) -> dict:
    return {"passed": passed, "rounds": rounds,
            "productive_rounds": productive,
            "infra_failure": infra, "fail_reasons": [] if passed else ["x"]}


def _pass(rounds: int = 10, productive: int = 9) -> dict:
    """ratio=0.9（达标）且 passed。"""
    return _rec(passed=True, rounds=rounds, productive=productive)


def _fail(rounds: int = 10, productive: int = 5, *, infra: bool = False) -> dict:
    """ratio=0.5（不达标）且 passed=False。"""
    return _rec(passed=False, rounds=rounds, productive=productive,
                infra=infra)


# --------------------------------------------- AC-T4-3：2³ 组合的 iff 映射

def test_ac_t4_3_established_iff_all_three_hold():
    """穷举 2³：ESTABLISHED 当且仅当「两墙过 ∧ ratio 达标」。"""
    combos = []
    for a_pass in (True, False):
        for b_pass in (True, False):
            for ratio_ok in (True, False):
                rounds, productive = ((10, 9) if ratio_ok else (10, 5))
                a = (_rec(passed=True, rounds=rounds, productive=productive)
                     if a_pass else
                     _rec(passed=False, rounds=rounds, productive=productive))
                b = (_rec(passed=True, rounds=rounds, productive=productive)
                     if b_pass else
                     _rec(passed=False, rounds=rounds, productive=productive))
                combos.append((a_pass, b_pass, ratio_ok, a, b))

    for a_pass, b_pass, ratio_ok, a, b in combos:
        verdict, _reason, ratio = R.compute_verdict(a, b, pool_used=20)
        expect = "ESTABLISHED" if (a_pass and b_pass and ratio_ok) \
            else "NOT_ESTABLISHED"
        assert verdict == expect, (a_pass, b_pass, ratio_ok, verdict)
        # ratio 与输入同义（0.9 / 0.5），证明 verdict 用的是合并且非单墙值
        assert ratio == pytest.approx(0.9 if ratio_ok else 0.5)


def test_ac_t4_3_ratio_uses_pooled_rounds_not_wall_average():
    """合并口径：加权按轮数，不是两墙 ratio 的简单平均。"""
    a = _rec(passed=True, rounds=90, productive=90)      # 1.00
    b = _rec(passed=True, rounds=10, productive=0)       # 0.00
    _v, _r, ratio = R.compute_verdict(a, b, pool_used=10)
    assert ratio == pytest.approx(90 / 100)              # 不是 0.5
    assert R.compute_verdict(a, b, pool_used=10)[0] == "ESTABLISHED"


def test_ac_t4_3_floor_is_inclusive_at_080():
    """预注册界是 `>= 0.80`：恰好 0.80 过，0.799 不过（边界不放水）。"""
    for rounds, productive, expect in ((100, 80, "ESTABLISHED"),
                                       (1000, 799, "NOT_ESTABLISHED")):
        v, _r, ratio = R.compute_verdict(
            _rec(passed=True, rounds=rounds, productive=productive),
            _rec(passed=True, rounds=0, productive=0), pool_used=10)
        assert ratio == pytest.approx(productive / rounds)
        assert v == expect


# --------------------------------------------- AC-T4-3：INCONCLUSIVE 窄口

def test_inconclusive_only_when_not_both_and_infra_and_pool_exhausted():
    """三条件同时成立才是 INCONCLUSIVE。"""
    v, _r, _ratio = R.compute_verdict(
        _fail(infra=True), _fail(), pool_used=R.HARD_ENVELOPE)
    assert v == "INCONCLUSIVE"


def test_inconclusive_requires_pool_exhausted():
    """基础设施故障但储备未尽 → NOT_ESTABLISHED（还有票，不能说"不确定"）。"""
    v, _r, _ratio = R.compute_verdict(
        _fail(infra=True), _fail(), pool_used=R.HARD_ENVELOPE - 1)
    assert v == "NOT_ESTABLISHED"


def test_inconclusive_requires_infra_failure():
    """池用满但没有基础设施故障 → NOT_ESTABLISHED（跑完了，只是没过）。"""
    v, _r, _ratio = R.compute_verdict(
        _fail(), _fail(), pool_used=R.HARD_ENVELOPE)
    assert v == "NOT_ESTABLISHED"


def test_inconclusive_not_used_when_both_walls_passed():
    """两墙全过但 ratio 不达标 → NOT_ESTABLISHED（实验跑完了，效率不达标）。"""
    v, _r, ratio = R.compute_verdict(
        _rec(passed=True, rounds=10, productive=5, infra=True),
        _rec(passed=True, rounds=10, productive=5),
        pool_used=R.HARD_ENVELOPE)
    assert ratio == pytest.approx(0.5)
    assert v == "NOT_ESTABLISHED"


def test_inconclusive_never_returned_without_evidence_of_runs():
    """两墙都没有结果（None）→ NOT_ESTABLISHED，不是 INCONCLUSIVE。"""
    v, _r, ratio = R.compute_verdict(None, None, pool_used=R.HARD_ENVELOPE)
    assert ratio is None
    assert v == "NOT_ESTABLISHED"


# --------------------------------------------- 三态常量纪元

def test_tri_state_constants_are_english_literals_not_phase25():
    """AC-T4-3 的 ``verdict=="ESTABLISHED"`` 依赖本地英文字面量。"""
    assert R.V_ESTABLISHED == "ESTABLISHED"
    assert R.V_NOT_ESTABLISHED == "NOT_ESTABLISHED"
    assert R.V_INCONCLUSIVE == "INCONCLUSIVE"
    # phase25 那一组是中文，且与 v3 三个常量无一相等
    p25 = {p25_verdict.V_ESTABLISHED, p25_verdict.V_NOT_ESTABLISHED,
           p25_verdict.V_INCONCLUSIVE}
    assert not (p25 & {R.V_ESTABLISHED, R.V_NOT_ESTABLISHED,
                       R.V_INCONCLUSIVE})


# --------------------------------------------- 判据 §3：productive 轮推导

def _round(i: int, *, cache_hit: bool = False) -> dict:
    return {"tool": "__model_round__", "round": i, "step": 0,
            "args": {"cache_hit": cache_hit},
            "policy_decision": {"decision": "allow"}}


def _tool(i: int, name: str, *, obs: str = "ok", decision: str = "allow",
          step: int = 1) -> dict:
    return {"tool": name, "round": i, "step": step,
            "obs_head": obs, "obs_tail": "",
            "policy_decision": {"decision": decision}}


def test_productive_round_allow_with_nonempty_observation():
    events = [_round(1), _tool(1, "list_dir", obs="[list_dir] ./\n")]
    rounds, productive = R._productive_rounds(events)
    assert productive == 1 and rounds[0]["productive"] is True
    assert rounds[0]["productive_reason"] == "tool:list_dir"


def test_nonproductive_rounds_deny_empty_and_control_rows():
    """deny / 空观察 / 纯控制行（__condense__）都不算产出轮。"""
    events = [
        _round(1), _tool(1, "write_file", decision="deny", obs="拒绝"),
        _round(2), _tool(2, "run_cmd", obs="   \n"),          # 空白观察
        _round(3), {"tool": "__condense__", "round": 3, "step": 0,
                    "obs_head": "压缩摘要", "obs_tail": "",
                    "policy_decision": {"decision": "allow"}},
    ]
    rounds, productive = R._productive_rounds(events)
    assert len(rounds) == 3 and productive == 0
    assert all(r["productive"] is False for r in rounds)


def test_productive_round_counts_once_even_with_many_tool_calls():
    events = [_round(1), _tool(1, "list_dir"), _tool(1, "read_file", step=2),
              _round(2), _tool(2, "write_file")]
    rounds, productive = R._productive_rounds(events)
    assert productive == 2
    assert [r["productive_reason"] for r in rounds] == [
        "tool:list_dir", "tool:write_file"]


def test_orphan_tool_row_without_model_round_is_ignored():
    """残行（无对应 __model_round__）不得凭空造出产出轮。"""
    events = [_round(1), _tool(1, "list_dir"), _tool(7, "walk")]
    rounds, productive = R._productive_rounds(events)
    assert len(rounds) == 1 and productive == 1


def test_effective_ratio_denominator_counts_cache_hit_rounds():
    """分母是全部模型轮（含缓存命中轮）——命中轮不算产出但占轮。"""
    events = [_round(1), _tool(1, "list_dir"),
              _round(2, cache_hit=True), _tool(2, "list_dir", decision="deny")]
    rounds, productive = R._productive_rounds(events)
    assert len(rounds) == 2 and productive == 1
    assert rounds[1]["cache_hit"] is True


# --------------------------------------------- AC-T4-4：判据 LOCK 三重取证

@pytest.fixture()
def retest_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMPRAXIS_PHASE26_ROOT", str(tmp_path / "phase26"))
    root = R.retest_root()
    root.mkdir(parents=True, exist_ok=True)
    # seal 前置：基线件必须在场（--write-criteria 的同一前置）
    (root / "baseline_effective_ratio.json").write_text(
        json.dumps({"schema": "x", "recomputed": 0.74}), encoding="utf-8")
    return root


def test_criteria_seal_and_verify_triple(retest_env):
    assert R.cmd_write_criteria() == 0
    assert R.cmd_seal_criteria() == 0
    lock = R.verify_criteria_lock()
    assert lock["criteria_mode"] == "0o444"
    assert lock["criteria_sha256"] == R.fc.sha256_file(R.criteria_path())
    # 判据正文里预注册的 R_base 已代入实际值，且三态三条目逐字在
    text = R.criteria_path().read_text(encoding="utf-8")
    assert "R_base = 0.74" in text
    for token in ("ESTABLISHED", "NOT_ESTABLISHED", "INCONCLUSIVE"):
        assert token in text
    # 已封 → 拒绝重写/重封（首呼后零改动）
    with pytest.raises(SystemExit):
        R.cmd_write_criteria()
    with pytest.raises(SystemExit):
        R.cmd_seal_criteria()


def test_verify_criteria_lock_rejects_mode_tamper(retest_env):
    R.cmd_write_criteria()
    R.cmd_seal_criteria()
    R.criteria_path().chmod(0o644)
    with pytest.raises(SystemExit) as exc:
        R.verify_criteria_lock()
    assert "mode 不符" in str(exc.value)


def test_verify_criteria_lock_rejects_content_tamper(retest_env):
    R.cmd_write_criteria()
    R.cmd_seal_criteria()
    p = R.criteria_path()
    p.chmod(0o644)
    p.write_text(p.read_text(encoding="utf-8") + "\n偷偷放宽界\n",
                 encoding="utf-8")
    p.chmod(0o444)
    with pytest.raises(SystemExit) as exc:
        R.verify_criteria_lock()
    assert "sha256 不符" in str(exc.value)


def test_verify_criteria_lock_missing_lock_fails_closed(retest_env):
    R.cmd_write_criteria()
    with pytest.raises(SystemExit) as exc:
        R.verify_criteria_lock()
    assert "LOCK 不存在" in str(exc.value)


def test_seal_refused_after_first_paid_call(retest_env):
    """首呼已在账上时禁止再封判据（否则等于首呼后替换判据）。"""
    R.cmd_write_criteria()
    ledger = R.phase26_ledger()
    path = Path(ledger.ns.ledger_path_fn())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "ts_iso": "2026-11-03T00:00:00Z", "event": "call", "seq": 2,
        "stage": "coding_debug", "model": "m", "thread_id": "t",
        "task": R.TASK_ID, "batch_id": R.BATCH_ID,
        "global_used_after": 1, "ok": True}) + "\n", encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        R.cmd_seal_criteria()
    assert "首呼已发生" in str(exc.value)


# --------------------------------------------- 账外探针 fail-closed（0 付费）

def test_probe_refuses_without_paid_guard(retest_env, monkeypatch):
    """无 SIMPRAXIS_ALLOW_PAID=1 一律 exit 2（双守卫，与付费测试同口径）。"""
    monkeypatch.delenv("SIMPRAXIS_ALLOW_PAID", raising=False)
    assert R.cmd_probe_coding() == 2


def test_probe_refuses_without_ticket(retest_env, monkeypatch):
    """票不在场即不发网（approved=1 是唯一授权面）。"""
    monkeypatch.setenv("SIMPRAXIS_ALLOW_PAID", "1")
    assert R.cmd_probe_coding() == 2


def test_probe_refuses_ticket_over_one(retest_env, monkeypatch):
    """票面 approved>1 也拒（本探针恒 1 次 POST，超票即违票）。"""
    monkeypatch.setenv("SIMPRAXIS_ALLOW_PAID", "1")
    p = R.phase26_root() / "quota" / "approvals" / f"{R.PROBE_BATCH}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "batch_id": R.PROBE_BATCH, "task": R.TASK_ID, "stage": "coding_debug",
        "approved": 2, "ledger_phase": 26}), encoding="utf-8")
    assert R.cmd_probe_coding() == 2


def test_probe_refuses_bad_max_tokens(retest_env, monkeypatch):
    monkeypatch.setenv("SIMPRAXIS_ALLOW_PAID", "1")
    assert R.cmd_probe_coding(R.PROBE_BATCH, 0) == 2


# --------------------------------------------- 预算常量（帽不许抬）

def test_envelope_is_frozen_28_and_subcaps_not_recomputed():
    assert R.SUBCAPS == {"wallA": 16, "wallB": 9}
    assert R.RESERVE == 3
    assert R.HARD_ENVELOPE == 28
    assert R.TASK_ID == 4 and R.BATCH_ID == "P26-B1"
    assert R.CFG.verify_repair_tail is True
    assert R.CFG.verify_repair_tail_rounds == 3
    assert R.CFG.run_expr is True
    assert R.CFG.tolerant_patch is True
    assert R.CFG.scratchpad is True
