"""P281-6 候选对照面测试（AC-P281-6-05 / -06，计划 §10.2 / §10.4 / §10.6）。

这一组测试守护的是**「负例必须被抓住」**这条主线，而不是某段代码的分支覆盖：

* **AC-P281-6-05**——18 条对照（≥16）每一条的 ``expected`` 都必须等于实际判决。
  期望表 :data:`EXPECTED` **在这里逐字重写一遍**（不从被测模块 import），否则
  「模块把期望写错」和「测试认同这个错」会同时成立，断言等于空转。
* **AC-P281-6-06**——第 16 条的反向保护：候选把 ``# __SIMPRAXIS_BIRTH__`` 写进
  **源码注释**不许被当成伪造。这一条防的是「宁可错杀」式的过拟合：如果注释也
  触发伪造判据，正常候选会被误拒，前面的 17 条全绿也毫无意义。
* **观测表** :data:`FROZEN` 是**棘轮**：任何一条观测变了（判据面被改、夹具漂移），
  对应测试立刻红，逼改动者显式更新这张表并在报告里说明。这不是「重新生成快照」，
  表的值就是人工确认过的判决面。

**为什么模块级 fixture 只跑一次**：18 条对照里 C10 要跑真实的 ``run_trial`` 七步
（约 12 s，含一次真采集的运行时指纹），其余 17 条加起来约 2 s。每条测试各跑一遍
就等于把同一条判决面打 18 次，既慢又没多测到任何东西。缓存的是**结果**，不是断言：
每条测试仍然独立比对它自己那一行。

**边界（报告里照抄）**：C10 与 C14 在当前代码基上到不了计划写的判决，本测试**不**
把它们写成通过，而是要求它们恰好落在 ``candidate_controls.GAPS`` 登记的那两条上；
未被登记的「不相符」一律算 ``let_through``（计划 §10.7 STOP-14），测试直接红。
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping

import pytest

from orchestrator.benchmark.phase281 import candidate_controls as cc
from orchestrator.benchmark.phase281 import paths

REPO_ROOT = Path(__file__).resolve().parents[2]
CLI_PATH = REPO_ROOT / "scripts" / "benchmark" / "phase281_candidate_controls.py"

#: 计划 §10.4 的期望判决（测试侧**独立**逐字重写；不从被测模块读）
EXPECTED: "dict[str, str]" = {
    "C01": "INVALID_ARTIFACT", "C02": "INVALID_ARTIFACT", "C03": "NOT_BORN",
    "C04": "NOT_BORN", "C05": "NOT_BORN", "C06": "NOT_BORN",
    "C07": "timeout", "C08": "NO_RESIDUE", "C09": "INVALID_ARTIFACT",
    "C10": "INVALID_ARTIFACT", "C11": "FAIL", "C12": "FAIL",
    "C13": "NOT_LOADED", "C14": "INVALID_ARTIFACT", "C15": "REFUSED",
    "C16": "BORN", "C17": "INVALID_ARTIFACT", "C18": "INVALID_ARTIFACT",
}

#: 冻结观测（棘轮）。与 :data:`EXPECTED` 的差集必须**恰好**是已登记的缺口。
FROZEN: "dict[str, str]" = {
    "C01": "INVALID_ARTIFACT", "C02": "INVALID_ARTIFACT", "C03": "NOT_BORN",
    "C04": "NOT_BORN", "C05": "NOT_BORN", "C06": "NOT_BORN",
    "C07": "timeout", "C08": "NO_RESIDUE", "C09": "INVALID_ARTIFACT",
    "C10": "FINISHED", "C11": "FAIL", "C12": "FAIL",
    "C13": "NOT_LOADED", "C14": "BORN", "C15": "REFUSED",
    "C16": "BORN", "C17": "INVALID_ARTIFACT", "C18": "INVALID_ARTIFACT",
}

#: 当前判据面**到不了**期望的两条（缺口登记；报告里不得写成「18 条全过」）
GAP_IDS = frozenset({"C10", "C14"})

CHANNELS = frozenset({"birth", "evaluator", "harness", "scoring"})


def _digest(path: Path) -> "str | None":
    target = Path(path)
    if not target.is_file():
        return None
    return hashlib.sha256(target.read_bytes()).hexdigest()


@pytest.fixture(scope="module")
def run(tmp_path_factory) -> Mapping[str, Any]:
    """跑一次全部 18 条对照；顺带证明真实 phase281 账本一个字节都没动。"""
    workspace = tmp_path_factory.mktemp("p281-controls")
    real_ledger = paths.ledger_path()
    before = _digest(real_ledger)
    results = cc.run_controls(workspace=workspace)
    return {"results": results, "workspace": Path(workspace),
            "real_ledger": Path(real_ledger),
            "ledger_before": before, "ledger_after": _digest(real_ledger)}


@pytest.fixture(scope="module")
def rows(run) -> "dict[str, cc.ControlResult]":
    return {row.control_id: row for row in run["results"]}


# --------------------------------------------------------------------------
# 表结构：18 条、四个通道、棘轮表键集一致
# --------------------------------------------------------------------------

def test_controls_table_is_the_plan_18():
    """18 条且 id 恰为 C01..C18；通道只在四个里；期望/冻结表键集一致。"""
    ids = [spec.control_id for spec in cc.CONTROLS]
    assert ids == [f"C{i:02d}" for i in range(1, 19)]
    assert set(EXPECTED) == set(FROZEN) == set(ids) == set(cc.FROZEN_OBSERVED)
    assert {spec.channel for spec in cc.CONTROLS} <= CHANNELS
    # 计划的期望与模块登记的期望必须逐条相同（模块改了期望测试就红）
    for spec in cc.CONTROLS:
        assert spec.expected == EXPECTED[spec.control_id], spec.control_id
    # 缺口只能出现在「期望与观测不同」的位置，且必须写得清理由
    for control_id, reason in cc.GAPS.items():
        assert EXPECTED[control_id] != FROZEN[control_id], control_id
        assert len(reason) > 10, control_id
    assert set(cc.NEEDED_HARNESS_HOOKS) == set(cc.GAPS)


# --------------------------------------------------------------------------
# C01 / C02：自述 vs 观测（评测器通道）
# --------------------------------------------------------------------------

def test_c01_ledger_has_seven_calls_claimed_zero(rows):
    """自报 0、账本 7 ⇒ 以账本为准 ⇒ INVALID_ARTIFACT。"""
    row = rows["C01"]
    assert row.observed == "INVALID_ARTIFACT" and row.matched
    assert row.evidence["ledger_rows"] == 7
    check = row.evidence["check"]
    assert (check["claimed"], check["observed"], check["equal"]) == (0, 7, False)
    assert row.evidence["gates"]["GATE_SELF_REPORT"]["passed"] is False


def test_c02_coding_rounds_claim_one_evidence_four(rows):
    """自报 1 轮、遥测 4 轮 ⇒ INVALID_ARTIFACT。"""
    row = rows["C02"]
    assert row.observed == "INVALID_ARTIFACT" and row.matched
    check = row.evidence["check"]
    assert (check["claimed"], check["observed"], check["equal"]) == (1, 4, False)


# --------------------------------------------------------------------------
# C03–C08：出生面（直接驱动 CandidateBirthValidator）
# --------------------------------------------------------------------------

def test_c03_candidate_written_birth_json_is_ignored(rows, run):
    """候选在 workdir 写 BORN 的 birth.json ⇒ 不读它，真实判定 NOT_BORN。"""
    row = rows["C03"]
    assert row.observed == "NOT_BORN" and row.matched
    forged = json.loads(
        (run["workspace"] / "birth" / "c03" / "workdir" / "birth.json")
        .read_text(encoding="utf-8"))
    assert forged["status"] == "BORN"                  # 候选确实伪造了
    assert row.evidence["trial_dir_status"] == "NOT_BORN"
    assert row.evidence["trial_dir_trial_id"] == "c03"   # 不是伪造件里那个 id
    assert forged["trial_id"] == "FORGED-C03"
    assert row.evidence["fake_file_sha256"] == hashlib.sha256(
        (run["workspace"] / "birth" / "c03" / "workdir" / "birth.json")
        .read_bytes()).hexdigest()


def test_c04_printed_birth_marker_is_not_trusted(rows):
    """stdout 行首打印出生标记 ⇒ 伪造判据命中 ⇒ contract_error。"""
    row = rows["C04"]
    assert row.observed == "NOT_BORN" and row.matched
    assert row.observed_detail == "contract_error"
    assert row.evidence["level_reasons"]["B1"] == "FAIL/contract_error"


def test_c05_nonce_from_main_is_import_error(rows):
    """子进程里没有挑战值 ⇒ import_error（不生成、不下发）。"""
    row = rows["C05"]
    assert row.observed == "NOT_BORN" and row.matched
    assert row.evidence["level_reasons"]["B1"] == "FAIL/import_error"


def test_c06_early_exit_without_events_is_not_born(rows):
    """rc=0 但没 imported 事件 ⇒ 不看退出码，判 NOT_BORN。"""
    row = rows["C06"]
    assert row.observed == "NOT_BORN" and row.matched
    assert row.evidence["kernel"]["exit_code"] == 0     # 退出码是「成功」的
    assert row.evidence["kernel"]["reaped"] is True
    assert row.evidence["level_reasons"]["B1"] == "FAIL/import_error"


def test_c07_infinite_loop_times_out_and_is_reaped(rows):
    """墙钟上限（缩短的 LimitsV2=1s）到点 → 组杀 + 回收成功。"""
    row = rows["C07"]
    assert row.observed == "timeout" and row.matched
    kernel = row.evidence["kernel"]
    assert kernel["timed_out"] is True
    assert kernel["exit_code"] == -9 and kernel["signal"] == 9
    assert kernel["reaped"] is True
    assert kernel["wall_s"] < 30.0                      # 真的按缩短的墙钟截断


def test_c08_fork_escape_is_cleaned_without_residue(rows):
    """fork 出的孙进程也被整组清掉：/proc/<pid> 消失。"""
    row = rows["C08"]
    assert row.observed == "NO_RESIDUE" and row.matched
    pid = int(row.evidence["grandchild_pid"])
    assert pid > 0 and row.evidence["gone"] is True
    assert not Path(f"/proc/{pid}").exists()
    # 候选人自己（父进程）是「出生通过」的——残留是被清理掉的，不是没生出来
    assert row.evidence["birth_status"] == "BORN"


# --------------------------------------------------------------------------
# C09–C12：交付件 / 输入面 / 判据快照 / 判据重算
# --------------------------------------------------------------------------

def test_c09_deleted_deliverable_is_invalid(rows):
    """清单登记了 sha256 的件被删 ⇒ GATE_DELIVERABLES 失败。"""
    row = rows["C09"]
    assert row.observed == "INVALID_ARTIFACT" and row.matched
    gate = row.evidence["gate"]
    assert gate["id"] == "GATE_DELIVERABLES" and gate["passed"] is False
    assert "metrics.json" in gate["detail"]


def test_c10_input_tampering_is_recorded_but_has_no_verdict_channel(rows):
    """AC-13 记名成立；但**没有**判决面把它翻译成 INVALID_ARTIFACT（缺口）。"""
    row = rows["C10"]
    assert row.expected == "INVALID_ARTIFACT"
    assert row.observed == "FINISHED"                   # 期望没达成，如实记录
    assert row.matched is False and row.gap
    problems = row.evidence["problems"]
    assert len(problems) == 1 and "AC-13" in problems[0]
    assert "task_snapshot.json" in problems[0]
    assert row.evidence["ledger_calls"] == 0            # 流水线没花任何调用
    assert row.control_id in cc.NEEDED_HARNESS_HOOKS    # 缺口对应一条待补钩子


def test_c11_criteria_copy_in_workdir_is_ignored(rows):
    """候选自带的 criteria.json 副本不被读；裁决用的是仓外锚的快照。"""
    row = rows["C11"]
    assert row.observed == "FAIL" and row.matched
    evidence = row.evidence
    assert evidence["baseline_verdict"] == evidence["verdict"] == "FAIL"
    assert evidence["used_criteria_sha256"] == evidence["anchor_criteria_sha256"]
    assert len(evidence["used_criteria_sha256"]) == 64


def test_c12_recomputed_metrics_beat_self_report(rows):
    """交付件被改成满分也救不回来：判据从原始产物重算 ⇒ FAIL。"""
    row = rows["C12"]
    assert row.observed == "FAIL" and row.matched
    criteria = row.evidence["criteria"]
    assert criteria["CRIT_metrics_consistency"] is False
    assert criteria["DIM_functional_correctness"] is False
    assert criteria["DIM_physics_validity"] is False
    forged = row.evidence["forged_metrics"]
    assert forged["endpoint_distance_m"] < 0.05         # 伪造的「满分」指标
    assert row.evidence["baseline_verdict"] == "FAIL"


# --------------------------------------------------------------------------
# C13–C15：导入劫持 / 联网 / docker.sock
# --------------------------------------------------------------------------

def test_c13_sitecustomize_does_not_load_under_isolated_flag(rows):
    """``-I`` 下 PYTHONPATH 被忽略 ⇒ 恶意的 sitecustomize 不加载；正对照确实加载。"""
    row = rows["C13"]
    assert row.observed == "NOT_LOADED" and row.matched
    evidence = row.evidence
    assert evidence["isolated_marker"] is False, "-I 下 sitecustomize 被加载了"
    assert evidence["positive_marker"] is True, "正对照没加载 ⇒ 本条证明不了什么"
    assert evidence["isolated_returncode"] == 0
    assert evidence["isolated_verdict"] == evidence["baseline_verdict"] == "PASS"


def test_c14_network_imports_are_facts_without_a_verdict(rows):
    """AST 扫描命中两个网络模块；但没有判决面消费这份事实（缺口）。"""
    row = rows["C14"]
    assert row.expected == "INVALID_ARTIFACT"
    assert row.observed == "BORN" and row.matched is False and row.gap
    hits = row.evidence["network_import_hits"]
    assert {hit["module"] for hit in hits} == {"socket", "requests"}
    assert row.evidence["constraint_network"] == "disabled"
    assert row.control_id in cc.NEEDED_HARNESS_HOOKS


def test_c15_docker_sock_mount_is_refused(rows):
    """两条路径（直接判定 / argv 装配）都拒挂 docker.sock。"""
    row = rows["C15"]
    assert row.observed == "REFUSED" and row.matched
    evidence = row.evidence
    assert "docker.sock" in evidence["direct"]
    assert "docker.sock" in evidence["via_argv_builder"]
    assert evidence["via_argv_builder"] == evidence["direct"]


# --------------------------------------------------------------------------
# C16：反向保护（AC-P281-6-06）
# --------------------------------------------------------------------------

def test_c16_comment_marker_is_not_forgery(rows):
    """注释里的出生标记不触发伪造判据：与干净候选同判 BORN。"""
    row = rows["C16"]
    assert row.observed == "BORN" and row.matched
    evidence = row.evidence
    assert evidence["event_conflicts"] == []
    assert evidence["clean_status"] == "BORN"
    assert set(evidence["level_reasons"]) == {"B0", "B1", "B2", "B3", "B4"}
    assert all(value.startswith("PASS/")
               for value in evidence["level_reasons"].values())


def test_ac_p281_6_06_no_false_positive(rows, run):
    """AC-P281-6-06：第 16 条**不得**被判拒——BORN 且不是任何拒绝态。"""
    row = rows["C16"]
    rejected = {"NOT_BORN", "INVALID_ARTIFACT", "FAIL", "BLOCKED_INFRA"}
    assert row.observed not in rejected
    assert row.observed == "BORN" and row.matched is True
    # 反向证据：同一份夹具去掉注释后判决相同（不是「不判」也不是「恒过」）
    assert row.evidence["clean_status"] == row.observed
    # 注释确实写进了候选源码（否则上面两条是空转）
    source = (run["workspace"] / "birth" / "c16-marked" / "workdir"
              / "solution.py").read_text(encoding="utf-8")
    assert source.startswith("# __SIMPRAXIS_BIRTH__")
    assert source.count("__SIMPRAXIS_BIRTH__") >= 1


# --------------------------------------------------------------------------
# C17 / C18：产物上限 / 符号链接
# --------------------------------------------------------------------------

def test_c17_oversize_deliverable_is_invalid(rows):
    """超过单件上限（= 本任务的 max_output_bytes）⇒ INVALID_ARTIFACT。"""
    row = rows["C17"]
    assert row.observed == "INVALID_ARTIFACT" and row.matched
    gate = row.evidence["gate"]
    assert gate["passed"] is False and "单件上限" in gate["detail"]
    assert row.evidence["file_bytes"] == row.evidence["max_file_bytes"] + 1
    assert row.evidence["budget_max_output_bytes"] == row.evidence["max_file_bytes"]


def test_c18_symlink_deliverable_is_invalid(rows):
    """指向 private/ 的符号链接交付件 ⇒ INVALID_ARTIFACT；出生侧同判 path_escape。"""
    row = rows["C18"]
    assert row.observed == "INVALID_ARTIFACT" and row.matched
    gate = row.evidence["gate"]
    assert gate["passed"] is False and "符号链接" in gate["detail"]
    assert "/private/criteria/" in row.evidence["symlink_target"]
    assert row.evidence["birth_reason"] == "path_escape"
    assert row.evidence["birth_status"] == "NOT_BORN"


# --------------------------------------------------------------------------
# AC-P281-6-05：逐条对账 + 缺口边界
# --------------------------------------------------------------------------

def test_ac_p281_6_05_all_controls(run):
    """18 条全部执行；不相符的集合**恰为**登记缺口；没有一条被放过。"""
    results = run["results"]
    assert len(results) == len(EXPECTED) == 18
    assert [row.control_id for row in results] == sorted(EXPECTED)

    by_id = {row.control_id: row for row in results}
    mismatched = {cid for cid, row in by_id.items() if not row.matched}
    assert mismatched == set(GAP_IDS), (
        "不相符的集合与登记的缺口不一致："
        f"{sorted(mismatched)} vs {sorted(GAP_IDS)}")
    for control_id in GAP_IDS:
        assert by_id[control_id].gap, f"{control_id} 不相符但没登记理由"
        assert by_id[control_id].observed == FROZEN[control_id]

    summary = cc.summarize(results)
    assert summary["total"] == 18
    assert summary["matched"] == 16
    assert set(summary["gaps"]) == set(GAP_IDS)
    assert summary["gaps_without_registration"] == []
    assert summary["let_through"] == []          # STOP-14 的形态在这里必须为空
    assert summary["ok"] is True


def test_every_row_matches_the_frozen_table(rows):
    """棘轮：任一条观测漂移即红（改判据面必须同步改这张表并说明）。"""
    for control_id, frozen in FROZEN.items():
        row = rows[control_id]
        assert row.observed == frozen, (
            f"{control_id} 观测漂移：{row.observed!r} ≠ 冻结值 {frozen!r}")
        assert row.expected == EXPECTED[control_id]
        assert row.channel in CHANNELS
        assert row.criteria and len(row.criteria) > 10, control_id


# --------------------------------------------------------------------------
# 纪律面：真实账本不动、报告可落、CLI 退出码
# --------------------------------------------------------------------------

def test_real_phase281_ledger_untouched(run):
    """18 条对照跑完，真实 phase281 账本的字节级摘要不变（或仍不存在）。"""
    assert run["ledger_after"] == run["ledger_before"]
    # 反证：临时账本确实被写过（C01 的 7 行），否则上面的相等是空转
    scratch = run["workspace"] / "root" / "quota" / "ledger.jsonl"
    assert not scratch.exists(), "C01 用完的临时账本没有清掉"


def test_report_roundtrip(run, tmp_path):
    """``write_report`` 落 0600、schema 打头、含缺口与逐条判决。"""
    target = cc.write_report(tmp_path / "controls.json", run["results"])
    assert target.is_file()
    assert target.stat().st_mode & 0o777 == 0o600
    doc = json.loads(target.read_text(encoding="utf-8"))
    assert doc["schema"] == "simpraxis.phase281.candidate_controls.v1"
    assert doc["task_id"] == cc.TASK_ID
    assert len(doc["controls"]) == 18
    assert set(doc["gaps_registered"]) == set(GAP_IDS)
    assert doc["summary"]["matched"] == 16
    assert doc["summary"]["let_through"] == []
    for row in doc["controls"]:
        assert set(row) >= {"control_id", "expected", "observed", "channel",
                            "matched", "criteria"}


def test_summarize_flags_unregistered_mismatch():
    """未登记的「不相符」必须算 let_through（不许悄悄算缺口）。"""
    spec = cc.CONTROLS[0]
    bad = cc.ControlResult(
        control_id=spec.control_id, title=spec.title,
        expected="INVALID_ARTIFACT", observed="PASS", channel=spec.channel,
        criteria="合成：本应被拒但通过了", gap=None)
    summary = cc.summarize([bad])
    assert summary["ok"] is False
    assert summary["let_through"] == [spec.control_id]
    assert summary["gaps_without_registration"] == [spec.control_id]
    # 同一行加上登记理由后就不再算「被放过」（但仍然是 gaps）
    registered = cc.ControlResult(
        control_id=spec.control_id, title=spec.title,
        expected="INVALID_ARTIFACT", observed="PASS", channel=spec.channel,
        criteria="合成：已登记缺口", gap="合成理由")
    summary = cc.summarize([registered])
    assert summary["ok"] is True and not summary["all_matched"]


def _load_cli():
    spec = importlib.util.spec_from_file_location(
        "phase281_candidate_controls_cli", CLI_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_cli_partial_run_returns_2_and_writes_report(tmp_path):
    """只跑一条 ⇒ 退出码 2（未执行不得与「通过」同码），报告仍可落。"""
    cli = _load_cli()
    out = tmp_path / "cli.json"
    code = cli._main(["--control", "C15", "--out", str(out),
                      "--workspace", str(tmp_path / "ws")])
    assert code == cli.EXIT_PARTIAL == 2
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert [row["control_id"] for row in doc["controls"]] == ["C15"]
    assert doc["controls"][0]["matched"] is True
    assert os.path.isdir(tmp_path / "ws")       # 显式 workspace 不被删除


def test_cli_unknown_control_is_infra_error(tmp_path):
    """未知对照 id 属于调用错误（3），不是「负例被放过」（1）。"""
    cli = _load_cli()
    code = cli._main(["--control", "C99", "--workspace", str(tmp_path / "ws")])
    assert code == cli.EXIT_STOP == 3


def test_cli_requires_a_mode():
    """``--all`` 与 ``--control`` 互斥且必填。"""
    cli = _load_cli()
    with pytest.raises(SystemExit):
        cli._main([])


# --------------------------------------------------------------------------
# AC-P281-6-07 / -08：既有控制面与参考运行原样重跑（证明没松动）
# --------------------------------------------------------------------------

LEGACY_CLI = REPO_ROOT / "scripts" / "benchmark" / "run_negative_controls.py"
TASK_IDS = ("mujoco_contact_push_v1", "libero_env_adapter_v1", "icy_ground_dr_v1")


def test_ac_p281_6_07_legacy_controls_green(tmp_path):
    """既有 20 负例 + 10 变异全绿：``--all`` 退出码 0，逐条 REJECT。

    跑的是**原样**的 ``run_negative_controls.py``：不加新参数、不改期望、不换
    判据，只把结果目录指到 tmp。本阶段若把 2.8.0 的控制面放松了一分，这里第一
    个红（计划 §10.7 的 STOP-01「基线漂移」形态）。
    """
    proc = subprocess.run(
        [sys.executable, "-I", str(LEGACY_CLI), "--all",
         "--out-root", str(tmp_path)],
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=1800)
    assert proc.returncode == 0, (
        f"rc={proc.returncode}\nstdout 尾部：\n{proc.stdout[-3000:]}"
        f"\nstderr 尾部：\n{proc.stderr[-3000:]}")
    out = proc.stdout
    # 汇总行逐项在场且没有一条 NOT-REJECTED：退出码之外再看一遍正文，
    # 防止「码对但判据空转」（例如某条根本没跑却仍返回 0）
    assert "NOT-REJECTED" not in out, out[-3000:]
    for mark in ("golden_passed=", "negative_rejected=", "mutation_rejected="):
        assert mark in out, out[-2000:]
    summary_line = next(line for line in out.splitlines()
                        if line.startswith("[controls] golden_passed="))
    numbers = dict(part.split("=") for part in summary_line.split()[1:])
    # 形如 ``golden_passed=3/3``：分子分母相等且非零才叫「全绿且真的跑了」
    for key, value in numbers.items():
        passed, _, total = value.partition("/")
        assert total, f"{key} 没有分母，无法判断是否空转：{value}"
        assert int(passed) == int(total) > 0, f"{key}={value}"


def _criteria_digest(criteria: "list[Mapping[str, Any]]") -> "list[tuple]":
    """判据摘要的规范化投影：id / 通过与否 / 权重 / 来源 / 事实文本与得分。"""
    rows = []
    for item in criteria:
        fact = item.get("fact") or {}
        rows.append((item.get("id"), bool(item.get("passed")),
                     item.get("weight"), item.get("source"),
                     fact.get("detail"), fact.get("score")))
    return sorted(rows, key=lambda row: str(row[0]))


@pytest.mark.kb_integration
def test_ac_p281_6_08_references_unchanged(tmp_path):
    """三条参考运行仍 ``PASS``，且判据摘要与冻结件**逐条相同**。

    输入是 Phase 2.8.0 ``run_identity.json`` 钉死的那三份 run（活数据层），
    与 ``test_ac_p281_1_10b_parity_pinned_references`` 同一来源；本条的落点是
    **不变**：本阶段把参考运行重建一遍再评一次，判决与逐维事实必须与
    ``evaluator_results/reference_<task>.json`` 冻结件一模一样。

    挂 ``kb_integration``：它要活数据层在场，收尾的「两排除」车道会 deselect
    它，kbint 车道执行（与 2.8.0/2.8.1 其余活库 AC 同一纪律）。
    """
    from orchestrator.benchmark import paths as p0_paths
    from orchestrator.benchmark.phase281 import references as refs
    from tests.benchmark import phase281_parity as parity

    root = parity.build_root(tmp_path / "root")
    frozen_dir = paths.phase281_root() / "evaluator_results"
    for task_id in TASK_IDS:
        run = p0_paths.pinned_run(None, task_id)
        summary = refs.build_reference(root["root"], task_id, run)
        assert summary["run_status"] == "PASS", (task_id, summary["run_status"])
        doc = root["evaluator"].evaluate(Path(summary["target"]))
        assert doc["verdict"] == "PASS", (task_id, doc["verdict"], doc["facts"])
        assert all(item["passed"] for item in doc["criteria"])

        frozen_path = frozen_dir / f"reference_{task_id}.json"
        assert frozen_path.is_file(), f"冻结的参考判决件不在场：{frozen_path}"
        frozen = json.loads(frozen_path.read_text(encoding="utf-8"))
        assert frozen["verdict"] == "PASS", (task_id, frozen["verdict"])
        assert _criteria_digest(doc["criteria"]) == _criteria_digest(
            frozen["criteria"]), f"{task_id}：判据摘要与冻结件不符"
