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
import os
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


def test_birth_round_is_productive_without_any_tool_call():
    """判据 §3 条款 (b)：出生轮本身计产出（该轮可以只有一次落盘之外的行为）。"""
    events = [_round(1), _tool(1, "write_file", decision="deny", obs="拒绝"),
              _round(2)]
    rounds, productive = R._productive_rounds(events, born_at_round=2)
    assert productive == 1
    assert rounds[1]["productive_reason"] == "birth_round"
    # 未给出出生轮（None）时条款 (b) 不触发：本批两墙 born 全 null 的实测口径
    rounds2, productive2 = R._productive_rounds(events)
    assert productive2 == 0 and rounds2[1]["productive"] is False


def test_birth_round_keeps_tool_reason_when_already_productive():
    """出生轮已因工具产出时不覆盖原因（只增不减，口径不放大）。"""
    events = [_round(2), _tool(2, "write_file", obs="[write_file] ok")]
    rounds, productive = R._productive_rounds(events, born_at_round=2)
    assert productive == 1
    assert rounds[0]["productive_reason"] == "tool:write_file"


# ------------------------- D4-6：基础设施故障唯一机判（INCONCLUSIVE 的闸）

def test_infra_5xx_retry_exhausted_is_infra():
    """生产形态：reason 恒被 model.py 包成「供应侧错误（…）：HTTP …」（评审 R1）。"""
    ok, cls = R.classify_infra_failure(
        "supplier_error",
        "供应侧错误（GatewayError）：HTTP 503 重试 5 次仍失败：upstream unavailable")
    assert ok is True and cls == "gateway_5xx_429_exhausted"
    # 未包装形态仍命中（回归：不得因适配包装而收紧成只认包装）
    ok2, cls2 = R.classify_infra_failure(
        "supplier_error", "HTTP 503 重试 5 次仍失败：upstream unavailable")
    assert ok2 is True and cls2 == "gateway_5xx_429_exhausted"


def test_infra_429_backoff_exhausted_is_infra():
    ok, cls = R.classify_infra_failure(
        "supplier_error",
        "GATEWAY PAUSED stage=coding_debug until 2026-11-03T03:00:00Z "
        "reason=429 退避预算耗尽，转长熔断")
    assert ok is True and cls == "gateway_5xx_429_exhausted"
    # 5xx/429 的「重试 N 次仍失败」在包装后同样命中
    ok2, cls2 = R.classify_infra_failure(
        "supplier_error",
        "供应侧错误（GatewayError）：HTTP 429 重试 5 次仍失败：rate limited")
    assert ok2 is True and cls2 == "gateway_5xx_429_exhausted"


def test_infra_ignores_model_controlled_stdout_stderr():
    """评审 R2：模型 print 一行 MemoryError/ENOSPC 不得打开基础设施窄口。

    分类器**不接受** stdout/stderr —— 契约层面就关掉这条不可信信道。
    """
    import inspect
    params = inspect.signature(R.classify_infra_failure).parameters
    assert "stdout" not in params and "stderr" not in params
    # 模型可控文本即便进了 reason，只要 kind 不是 supplier_error 也不算 (a)
    ok, cls = R.classify_infra_failure(
        "engine_error", "Traceback (most recent call last): MemoryError")
    assert ok is False and cls == ""
    ok, cls = R.classify_infra_failure(
        "birth_absent", "model printed: out of memory / ENOSPC / OOMKilled")
    assert ok is False and cls == ""


def test_infra_excludes_quality_and_config_failures():
    """4xx / 配额长熔断 / 未出生 / 引擎错：全部**不是**基础设施故障。"""
    for kind, reason in (
        ("supplier_error", "HTTP 400 stage=coding_debug：invalid request"),
        ("supplier_error", "404/model-not-found stage=coding_debug：nope"),
        ("supplier_error",
         "GATEWAY PAUSED stage=coding_debug until X reason=quota exceeded"),
        ("supplier_error", "凭据缺失：ANTHROPIC_AUTH_TOKEN"),
        ("birth_absent", "8 轮未见入口脚本"),
        ("engine_error", "ValueError: bad patch"),
        ("round_cap", "轮帽 16 用尽"),
        ("finish", "implicit_final_answer（未经 finish 原语）"),
    ):
        ok, cls = R.classify_infra_failure(kind, reason)
        assert ok is False and cls == "", (kind, reason)


def test_infra_oom_requires_kernel_counter_in_addition_to_exit_code():
    """(b) 双证据：退出码 137 **且** 内核 oom_kill 增量 ≥1（评审 R7）。

    冻结评分器在容器/宿主内 `exec_module` 跑模型写的 `agent_solution.py`，
    模型一行 `os._exit(137)` 即可把评分器退出码设成 137；只看退出码等于
    把 D4-6(b) 窄口交给模型控制。
    """
    ok, cls = R.classify_infra_failure("engine_error", "容器被杀")
    assert ok is False                        # 无正面证据即 False
    ok, cls = R.classify_infra_failure("engine_error", "", scorer_rc=137)
    assert ok is False                        # R7：退出码可伪造，单证不足
    ok, cls = R.classify_infra_failure("engine_error", "", scorer_rc=137,
                                       oom_kill_delta=0)
    assert ok is False                        # 有计数但窗口内零增量 ⇒ 伪造
    ok, cls = R.classify_infra_failure("engine_error", "", scorer_rc=137,
                                       oom_kill_delta=None)
    assert ok is False                        # 计数不可读 ⇒ fail-closed
    ok, cls = R.classify_infra_failure("engine_error", "", scorer_rc=137,
                                       oom_kill_delta=1)
    assert ok is True and cls == "container_oom_or_killed"
    ok, cls = R.classify_infra_failure("engine_error", "", scorer_rc=139,
                                       oom_kill_delta=3)
    assert ok is False                        # 非 SIGKILL 退出码不归因到 OOM


def test_infra_disk_uses_harness_signal_only():
    """(c) 只认我方观测的 /mnt 评分时点剩余空间。"""
    ok, cls = R.classify_infra_failure("finish", "", disk_free_gib=0.4)
    assert ok is True and cls == "disk_full"
    ok, cls = R.classify_infra_failure("finish", "", disk_free_gib=42.0)
    assert ok is False


def test_wall_a_score_gate_requires_verdict_and_all_twelve_checks():
    """评审 R9：墙 A 评分侧过闸 = 件源可信 ∧ rc 0 ∧ schema/verdict ∧ 12 项逐项 PASS。

    冻结评分器在自身进程内 `exec_module` 跑模型自著的 `agent_solution.py`，
    模型一行 `os._exit(0)` 就能让 rc 变 0 却一项检查都没跑；只看 final_rc
    等于把「全 12 项 PASS」交给模型控制（与 R7 同源）。
    """
    good = {"schema": R.WALL_A_SCORE_SCHEMA, "verdict": "pass",
            "checks": [{"check": f"c{i}", "ok": True} for i in range(12)]}
    ok = lambda rc, sc, prov=True: R.wall_a_score_ok(  # noqa: E731
        rc, sc, provenance_ok=prov)
    assert ok(0, good) is True
    assert ok(0, None) is False                 # 评分件缺席
    assert ok(0, {}) is False                   # 空件（伪造 rc0）
    assert ok(0, {"verdict": "fail", "checks": good["checks"]}) is False
    assert ok(0, {"verdict": "pass", "checks": good["checks"][:11]}) is False
    assert ok(0, {"verdict": "pass"}) is False  # 无 checks 键
    assert ok(137, good) is False
    assert ok(None, good) is False              # 无退出码即 False
    # 评审 R9 残留 2（MINOR）：12 条但逐项 ok=False 必须判否（判据 §1「全 PASS」）。
    all_bad = {"schema": R.WALL_A_SCORE_SCHEMA, "verdict": "pass",
               "checks": [{"check": f"c{i}", "ok": False} for i in range(12)]}
    assert ok(0, all_bad) is False
    # 12 条全 True 但名字重复 → 不足 12 项独立检查。
    dup = {"schema": R.WALL_A_SCORE_SCHEMA, "verdict": "pass",
           "checks": [{"check": "c0", "ok": True} for _ in range(12)]}
    assert ok(0, dup) is False
    # schema 不符（伪造件最容易漏的一格）。
    assert ok(0, {**good, "schema": "x"}) is False
    # 评审 R9 残留 1（MAJOR）：件源不可信时，内容再好也判否——无缺省值可绕过。
    assert ok(0, good, prov=False) is False


def test_select_score_file_rejects_forged_and_ambiguous_artifacts(tmp_path):
    """R9 残留 1：`.score` 是模型可写目录，件源三条必要条件必须 fail-closed。

    复现 executor 第四轮：真件 `wall_a-20261103T021400Z.json` 旁再放一件
    future-dated 的 `wall_a-99999999T999999Z.json`，旧 `sorted()[-1]` 必选后者。
    """
    start, end = 1_793_672_000.0, 1_793_672_120.0   # 2026-11-03T02:13:20Z 窗口
    sc = tmp_path / ".score"
    sc.mkdir()
    assert R.select_score_file(R.WALL_A, sc, window_start=start,
                               window_end=end) == (None, "score_artifact_absent")

    forged = sc / "wall_a-99999999T999999Z.json"     # executor 复现用的名字
    forged.write_text("{}")
    assert R.select_score_file(R.WALL_A, sc, window_start=start,
                               window_end=end) == (
        None, "score_artifact_name_unparsable")
    forged.unlink()

    future = sc / "wall_a-20991231T235959Z.json"     # 可解析但远在窗口外
    future.write_text("{}")
    assert R.select_score_file(R.WALL_A, sc, window_start=start,
                               window_end=end) == (
        None, "score_artifact_outside_window")
    future.unlink()

    legit = sc / "wall_a-20261103T021400Z.json"     # 窗口内（start+60s）
    legit.write_text("{}")
    also = sc / "wall_a-20261103T021401Z.json"      # 同在窗口内的第二件
    also.write_text("{}")
    p, why = R.select_score_file(R.WALL_A, sc, window_start=start,
                                 window_end=end)
    assert p is None and why.startswith("score_artifact_not_unique")  # 两件并存

    also.unlink()                                    # 只留一件 → 选中
    assert R.select_score_file(R.WALL_A, sc, window_start=start,
                               window_end=end) == (legit, "")

    legit.unlink()
    (sc / "wall_a-notatimestamp.json").write_text("{}")
    assert R.select_score_file(R.WALL_A, sc, window_start=start,
                               window_end=end) == (
        None, "score_artifact_name_unparsable")


def test_select_score_file_is_wall_scoped(tmp_path):
    """按墙别精确 glob：wall_b 件不得被墙 A 选中（反之亦然）。"""
    start, end = 1_793_672_000.0, 1_793_672_120.0
    sc = tmp_path / ".score"
    sc.mkdir()
    (sc / "wall_b-20261103T021400Z.json").write_text("{}")
    assert R.select_score_file(R.WALL_A, sc, window_start=start,
                               window_end=end)[1] == "score_artifact_absent"
    assert R.select_score_file(R.WALL_B, sc, window_start=start,
                               window_end=end)[0] is not None


def test_wall_a_pass_requires_all_five_conditions():
    """五项合取：出生轮 ≤8、final_rc=0、评分件全过、run_expr 成功≥1、追授轮≥1。"""
    base = dict(born_at_round=3, final_rc=0, score_ok=True,
                run_expr_ok_calls=2, tail_rounds=4)
    assert R.wall_a_passed(**base) is True
    for key, bad in (("born_at_round", None), ("born_at_round", 9),
                     ("final_rc", 1), ("score_ok", False),
                     ("run_expr_ok_calls", 0), ("tail_rounds", 0)):
        assert R.wall_a_passed(**{**base, key: bad}) is False, (key, bad)


def test_cgroup_read_point_aggregates_container_scopes(monkeypatch, tmp_path):
    """评审 R7-CGROUP-PATH：读点必须是**包含容器 scope 的分层聚合器**。

    本机 systemd 驱动下容器 scope 是 `system.slice` 的子级、`docker.service`
    的兄弟；读 `docker.service/memory.events` 会漏掉真容器 OOM（reviewer 实测：
    128m 容器炸弹 → 容器 scope 与 system.slice 的 oom_kill 0→1，docker.service
    保持 0）。故断言读点含 `system.slice`、且**不再**同时含 `docker.service`
    （后代，并读会重复计数）。
    """
    seen = []
    real_exists = R.Path.exists

    def fake_exists(self):
        seen.append(str(self))
        return True

    real_read = R.Path.read_text

    def fake_read(self, *a, **k):
        if str(self) == "/proc/self/cgroup":
            return "0::/user.slice/user-1000.slice/session-7.scope\n"
        return real_read(self, *a, **k)

    monkeypatch.setattr(R.Path, "exists", fake_exists)
    monkeypatch.setattr(R.Path, "read_text", fake_read)
    paths = [str(p) for p in R._cgroup_event_paths()]
    assert "/sys/fs/cgroup/system.slice/memory.events" in paths
    assert not any("/docker.service/" in p for p in paths)
    assert "/sys/fs/cgroup/user.slice/user-1000.slice/session-7.scope/memory.events" \
        in paths


def test_cgroup_read_point_falls_back_without_system_slice(monkeypatch):
    """`system.slice` 不可读时回退 `docker.service`（cgroupfs 驱动的老路径）。"""
    monkeypatch.setattr(R.Path, "exists", lambda self: False)
    monkeypatch.setattr(R.Path, "read_text",
                        lambda self, *a, **k: (_ for _ in ()).throw(OSError()))
    paths = [str(p) for p in R._cgroup_event_paths()]
    assert paths == [
        "/sys/fs/cgroup/system.slice/docker.service/memory.events"]


def test_oom_counter_parsing_and_delta(tmp_path, monkeypatch):
    """cgroup `memory.events` 解析：缺文件/坏行不猜 0；任一端缺席即 None。"""
    a = tmp_path / "docker.memory.events"
    b = tmp_path / "self.memory.events"
    a.write_text("low 0\nhigh 0\nmax 0\noom 1\noom_kill 7\n", encoding="utf-8")
    b.write_text("max 0\noom_kill 2\n", encoding="utf-8")
    monkeypatch.setattr(R, "_cgroup_event_paths", lambda: (a, b))
    counts = R._read_oom_kill_counts()
    assert counts == {str(a): 7, str(b): 2}
    after = {str(a): 8, str(b): 2}
    assert R._oom_kill_delta(counts, after) == 1
    assert R._oom_kill_delta(counts, dict(counts)) == 0
    # 计数回退（cgroup 重建）不计负增量
    assert R._oom_kill_delta(counts, {str(a): 3, str(b): 2}) == 0
    # 一端缺席 ⇒ 不可归因
    assert R._oom_kill_delta(counts, {str(a): 9}) is None
    assert R._oom_kill_delta(None, after) is None
    # 坏行/缺键不算，也不抛
    c = tmp_path / "broken.memory.events"
    c.write_text("oom_kill not-an-int\n", encoding="utf-8")
    d = tmp_path / "no-counter.memory.events"
    d.write_text("oom 0\n", encoding="utf-8")
    monkeypatch.setattr(R, "_cgroup_event_paths", lambda: (c, d))
    assert R._read_oom_kill_counts() == {}


def test_infra_failure_does_not_open_inconclusive_on_quality_failure():
    """回归闸：模型质量类失败即使池用满也不得进 INCONCLUSIVE。"""
    rec = {"passed": False, "rounds": 9, "productive_rounds": 3,
           "infra_failure": R.classify_infra_failure(
               "birth_absent", "9 轮未见 agent_solution.py")[0],
           "fail_reasons": ["witness_not_12"]}
    v, _r, _ratio = R.compute_verdict(rec, rec, pool_used=R.HARD_ENVELOPE)
    assert rec["infra_failure"] is False
    assert v == "NOT_ESTABLISHED"


# ------------------------- I4-5：同一墙只许一轮非基础设施故障结果

def _write_wall_result(root: Path, wall: str, stamp: str, *,
                       infra: bool, passed: bool = False) -> Path:
    d = root / "runs" / wall / stamp
    d.mkdir(parents=True, exist_ok=True)
    rec = {"wall": wall, "run_dir": str(d), "passed": passed,
           "infra_failure": infra,
           "infra_class": "gateway_5xx_429_exhausted" if infra else "",
           "attempt_index": 0, "ts_iso": stamp,
           "rounds": 9, "productive_rounds": 3, "fail_reasons": ["x"],
           "paid_calls": 9, "cap_exhausted": True}
    p = d / "wall_result_v3.json"
    p.write_text(json.dumps(rec), encoding="utf-8")
    return p


def test_single_run_guard_allows_first_attempt(retest_env):
    assert R._guard_single_run("wallA") == 0


def test_single_run_guard_refuses_rerun_after_quality_failure(retest_env):
    """判负不重跑：已有非基础设施故障结果时，第二次开跑必须炸。"""
    _write_wall_result(retest_env, "wallA", "20261103T020317Z", infra=False)
    with pytest.raises(SystemExit) as exc:
        R._guard_single_run("wallA")
    assert "I4-5" in str(exc.value)


def test_single_run_guard_allows_one_d4_6_reserve_retry(retest_env):
    _write_wall_result(retest_env, "wallA", "20261103T020317Z", infra=True)
    assert R._guard_single_run("wallA") == 1
    _write_wall_result(retest_env, "wallA", "20261103T030000Z", infra=True)
    with pytest.raises(SystemExit) as exc:
        R._guard_single_run("wallA")
    assert "额度已用尽" in str(exc.value)


def test_wall_result_selection_refuses_two_non_infra_attempts(retest_env):
    """聚合不得静默取最后一次：两份非基础设施故障结果 = 人工裁决。"""
    _write_wall_result(retest_env, "wallA", "20261103T020317Z", infra=False)
    _write_wall_result(retest_env, "wallB", "20261103T020954Z", infra=False)
    assert R._latest_wall_result("wallB")["wall"] == "wallB"
    _write_wall_result(retest_env, "wallB", "20261103T040000Z", infra=False)
    with pytest.raises(SystemExit) as exc:
        R._latest_wall_result("wallB")
    assert "I4-5" in str(exc.value)


def test_wall_result_selection_prefers_non_infra_over_infra(retest_env):
    _write_wall_result(retest_env, "wallA", "20261103T020000Z", infra=True)
    _write_wall_result(retest_env, "wallA", "20261103T020317Z", infra=False,
                       passed=True)
    rec = R._latest_wall_result("wallA")
    assert rec["infra_failure"] is False and rec["passed"] is True


def test_single_run_guard_refuses_crashed_attempt_without_result(retest_env):
    """评审 R5：已开跑（有 run_meta.json）但崩溃未落判决件 = 不可静默重跑。"""
    d = retest_env / "runs" / "wallA" / "20261103T050000Z"
    d.mkdir(parents=True)
    (d / "run_meta.json").write_text("{}", encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        R._guard_single_run("wallA")
    assert "人工分诊" in str(exc.value)
    assert R._unrecorded_attempts("wallA") == [str(d)]
    # 补记判决件后恢复正常口径（该件 infra=True ⇒ 仍可走 1 次储备重跑）
    _write_wall_result(retest_env, "wallA", "20261103T050000Z", infra=True)
    assert R._unrecorded_attempts("wallA") == []
    assert R._guard_single_run("wallA") == 1


def test_cmd_run_requires_matching_preflight(retest_env, monkeypatch):
    """评审 R6：同批 --preflight 由机判强制，不是操作员纪律。"""
    monkeypatch.setenv("SIMPRAXIS_ALLOW_PAID", "1")
    with pytest.raises(SystemExit) as exc:
        R.cmd_run("wallA", R.BATCH_ID)
    assert "预检件" in str(exc.value) or "缺少本批预检件" in str(exc.value)
    # 批次不符同样拒
    (retest_env / "preflight.json").write_text(
        json.dumps({"schema": "x", "paid_calls": 0,
                    "ticket": {"batch_id": "P26-OTHER"}}), encoding="utf-8")
    with pytest.raises(SystemExit) as exc2:
        R.cmd_run("wallA", R.BATCH_ID)
    assert "批次不符" in str(exc2.value)
    # 预检自证花过钱同样拒
    (retest_env / "preflight.json").write_text(
        json.dumps({"schema": "x", "paid_calls": 2,
                    "ticket": {"batch_id": R.BATCH_ID}}), encoding="utf-8")
    with pytest.raises(SystemExit) as exc3:
        R.cmd_run("wallA", R.BATCH_ID)
    assert "paid_calls" in str(exc3.value)


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


def test_verify_criteria_lock_rejects_mtime_tamper(retest_env):
    """三重取证缺一不可：内容与 mode 不变，只动 mtime 也算篡改。"""
    R.cmd_write_criteria()
    R.cmd_seal_criteria()
    p = R.criteria_path()
    st = p.stat()
    os.utime(p, (st.st_atime, st.st_mtime + 1))
    with pytest.raises(SystemExit) as exc:
        R.verify_criteria_lock()
    assert "mtime 不符" in str(exc.value)


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


def test_probe_refuses_replay_after_ticket_consumed(retest_env, monkeypatch):
    """防重放：票已用尽（标记在发网前落盘）即拒，绝不第二次 POST。"""
    monkeypatch.setenv("SIMPRAXIS_ALLOW_PAID", "1")
    p = retest_env / "quota" / "approvals" / f"{R.PROBE_BATCH}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "batch_id": R.PROBE_BATCH, "task": R.TASK_ID, "stage": "coding_debug",
        "approved": 1, "ledger_phase": 26}), encoding="utf-8")
    (retest_env / f"probe_consumed_{R.PROBE_BATCH}.json").write_text(
        json.dumps({"batch_id": R.PROBE_BATCH,
                    "consumed_at_iso": "2026-11-03T01:59:37Z"}),
        encoding="utf-8")
    assert R.cmd_probe_coding() == 2       # 票据合法但额度已用尽 → 不发网


def test_offledger_actuals_checks_evidence_presence_hermetically(
        retest_env, monkeypatch, tmp_path):
    """账外审计件不得只写「像路径的字符串」：证据在场与否必须机判。

    合成条目（不读活盘、不依赖本机 P26-B0 件），验证绝对路径与相对
    ``phase26_root()`` 两种写法都被解析成绝对路径并给出存在性。
    """
    abs_ev = tmp_path / "abs_evidence.json"
    abs_ev.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(R, "OFF_LEDGER_ACTUALS", {
        "P26-X1": {"approved": 1, "posts_used": 1, "kind": "probe",
                   "outcome": "OK", "evidence": str(abs_ev)},
        "P26-X2": {"approved": 1, "posts_used": 1, "kind": "probe",
                   "outcome": "OK", "evidence_rel": "walls/retest/missing.json"},
    })
    got = R.offledger_actuals()
    assert got["P26-X1"]["evidence_present"] is True
    assert got["P26-X1"]["evidence"] == str(abs_ev)
    assert got["P26-X2"]["evidence_present"] is False
    assert got["P26-X2"]["evidence"].startswith(str(retest_env))
    assert "evidence_rel" not in got["P26-X2"]


def test_offledger_actuals_real_entries_point_at_existing_files():
    """真实三条账外记录的 ``evidence`` 必须是绝对路径（不再有歧义相对根）。"""
    for bid, rec in R.OFF_LEDGER_ACTUALS.items():
        assert "evidence_rel" in rec or rec["evidence"].startswith("/"), bid


# --------------------------------------------- 审计件封存账对账（fail-closed）

def test_sealed_ledger_status_fails_closed_without_snapshot(tmp_path):
    """快照缺席不得被读成「未改」：--audit-ledger 的 ok 判据曾用
    ``sealed_untouched is not False``，None 也放行，等于自述算数。"""
    ok, detail = R._sealed_ledger_status(tmp_path / "nope.json")
    assert ok is False
    assert detail["__error__"]["error"] == "快照缺席"


def test_sealed_ledger_status_fails_closed_on_empty_snapshot(tmp_path):
    p = tmp_path / "snap.json"
    p.write_text(json.dumps({"paths": {}}), encoding="utf-8")
    ok, detail = R._sealed_ledger_status(p)
    assert ok is False and "空表" in detail["__error__"]["error"]


def test_sealed_ledger_status_true_only_when_all_unchanged(tmp_path):
    f = tmp_path / "sealed.ledger"
    f.write_text("{}\n", encoding="utf-8")
    snap = tmp_path / "snap.json"
    snap.write_text(json.dumps({"paths": {
        str(f): {"exists": True, "mtime": int(f.stat().st_mtime)}}}),
        encoding="utf-8")
    ok, detail = R._sealed_ledger_status(snap)
    assert ok is True and detail[str(f)]["changed"] is False
    # 追加一行 ⇒ mtime 变 ⇒ 判负
    f.write_text("{}\n{}\n", encoding="utf-8")
    os.utime(f, (1, 1))
    ok2, detail2 = R._sealed_ledger_status(snap)
    assert ok2 is False and detail2[str(f)]["changed"] is True


def test_sealed_ledger_status_detects_deleted_file(tmp_path):
    f = tmp_path / "sealed.ledger"
    f.write_text("{}\n", encoding="utf-8")
    snap = tmp_path / "snap.json"
    snap.write_text(json.dumps({"paths": {
        str(f): {"exists": True, "mtime": int(f.stat().st_mtime)}}}),
        encoding="utf-8")
    f.unlink()
    ok, detail = R._sealed_ledger_status(snap)
    assert ok is False and detail[str(f)]["after"]["exists"] is False


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
