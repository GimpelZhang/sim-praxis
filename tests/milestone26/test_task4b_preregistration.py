"""Phase 2.6 Task 4b 第二次预注册的机判（0 付费、全 hermetic）。

STOP-4a（2026-11-03）用户裁决 **(b) 同墙同帽换预注册再战一次**，硬约束五条：

1. **新票据**（`P26-B1b`）——不是 `P26-B1` 的子额度拆分（口径见 Complete §T4.10）；
2. **新判据 sha**（`VERDICT_CRITERIA_V4.md`，自带 LOCK）；
3. **新目录**（`walls/retest_v4/`）——V3 的 runs/判决件/LOCK 一字节不动；
4. **新批次号**，且 `_guard_single_run` 必须按 campaign 目录隔离，
   否则新战役在结构上不可能开跑；
5. **墙 / 帽 / 模型 / 阈值全不变**，唯一仪器变更＝出生闸候选名
   （D26-8d：冻结墙任务书要 `agent_solution.py`，V3 只看 `.entrypoint.py`）。

本文件只做纯函数与路径级机判，不读活账、不触网、不起容器。
"""
from __future__ import annotations

import json
import os

import pytest

from orchestrator.agentic import loop as L
from orchestrator.gateway import quota_ledger as QL
from scripts.phase26 import run_wall_retest as R


# --------------------------------------------------------------- 夹具

@pytest.fixture(autouse=True)
def _reset_campaign():
    """每条用例前后都把进程级 campaign 复位成 V3（缺省即 V3）。

    ``_CAMPAIGN`` 是模块级全局：不自动复位的话，本文件里任何
    ``set_campaign("v4")`` 都会泄漏到**后续测试模块**（实测
    ``test_task4_verdict_v3.py`` 的 8 条用例因此变红）。autouse 保证
    本文件的每条用例都自带同一缺省，且退出时不留痕。
    """
    R.set_campaign("v3")
    try:
        yield
    finally:
        R.set_campaign("v3")


@pytest.fixture()
def v3_default():
    """显式声明「本条依赖 V3 缺省」的用例用它（复位由 autouse 夹具负责）。"""
    R.set_campaign("v3")
    return R


# --------------------------------------------- 1. campaign 注册表与缺省同一性

def test_default_campaign_is_v3_and_byte_identical():
    """缺省必须逐字节等于 V3 的既有路径/批次/文件名（回滚面）。"""
    R.set_campaign("v3")
    c = R.campaign()
    assert c.key == "v3"
    assert c.batch_id == R.BATCH_ID == "P26-B1"
    assert c.subdir == "retest"
    assert c.criteria_name == "VERDICT_CRITERIA_V3.md"
    assert c.verdict_name == "verdict_v3.json"
    assert c.wall_result_name == "wall_result_v3.json"
    assert c.entrypoint_names == (".entrypoint.py",)


def test_v4_campaign_is_a_separate_namespace():
    c = R.CAMPAIGNS["v4"]
    assert c.key == "v4"
    assert c.batch_id == "P26-B1b"
    assert c.subdir == "retest_v4"
    assert c.criteria_name == "VERDICT_CRITERIA_V4.md"
    assert c.verdict_name == "verdict_v4.json"
    assert c.wall_result_name == "wall_result_v4.json"
    assert c.entrypoint_names == ("agent_solution.py",)
    # 五条硬约束里最容易走样的一条：不得与 V3 共用任何命名空间要素
    v3 = R.CAMPAIGNS["v3"]
    for field in ("batch_id", "subdir", "criteria_name", "verdict_name",
                  "wall_result_name", "entrypoint_names"):
        assert getattr(c, field) != getattr(v3, field), field


def test_campaign_paths_follow_the_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMPRAXIS_PHASE26_ROOT", str(tmp_path / "p26"))
    R.set_campaign("v4")
    assert R.retest_root() == tmp_path / "p26" / "walls" / "retest_v4"
    assert R.criteria_path().name == "VERDICT_CRITERIA_V4.md"
    assert R.lock_path() == (tmp_path / "p26" / "walls" / "retest_v4"
                             / "manifests" / "LOCK.json")
    R.set_campaign("v3")
    assert R.retest_root() == tmp_path / "p26" / "walls" / "retest"


def test_set_campaign_rejects_unknown_key():
    with pytest.raises(SystemExit):
        R.set_campaign("v5")
    # 拒绝不得改变当前 campaign
    assert R.campaign().key == "v3"


# --------------------------------------------- 2. 出生闸候选名（唯一仪器变更）

@pytest.mark.parametrize("bad", [
    "agent_solution.py",            # 字符串不是序列
    (),
    ("",),
    ("a/b.py",),
    ("a\\b.py",),
    (".",),
    ("..",),
    (1,),
    (None,),
])
def test_normalize_entrypoint_names_rejects(bad):
    with pytest.raises(ValueError):
        L.normalize_entrypoint_names(bad)


def test_normalize_entrypoint_names_accepts_and_dedups():
    assert L.normalize_entrypoint_names(None) == (L.ENTRYPOINT,)
    assert L.normalize_entrypoint_names([".entrypoint.py"]) == (".entrypoint.py",)
    assert L.normalize_entrypoint_names(
        ("agent_solution.py", "agent_solution.py", ".entrypoint.py")) == (
            "agent_solution.py", ".entrypoint.py")


def test_v4_criteria_text_names_the_wall_artifact_only():
    """判据 §1 的出生件名必须与 campaign 的候选名同源（单真值）。"""
    R.set_campaign("v4")
    text = R.criteria_body()
    body = R.CRITERIA_BODY           # 基线（V3 原文），下面逐条还原增量
    assert text != body
    assert "`agent_solution.py` 首次同时满足" in text
    # 还原三条声明内补丁后必须逐字节等于 V3 原文
    for old, new, want in R.V4_CRITERIA_PATCHES:
        rendered = new.replace("{entry}", "agent_solution.py")
        assert text.count(rendered) == want, old
        text = text.replace(rendered, old)
    assert text == body
    # `.entrypoint.py` 只允许出现在「v4 由来」说明段（正文条款里不得残留）
    full = R.criteria_body()
    head, sep, tail = full.partition("## 1. 墙 A")
    assert sep
    assert ".entrypoint.py" in head                  # 由来段如实说明被替换的旧名
    assert ".entrypoint.py" not in tail              # 条款本体已换名


def test_v3_criteria_body_is_untouched(v3_default):
    assert v3_default.criteria_body() is v3_default.CRITERIA_BODY


def test_v4_criteria_patch_anchor_drift_fails_closed(monkeypatch):
    """正文漂移时补丁必须熔断，而不是静默产生一份「只差一点点」的判据。"""
    R.set_campaign("v4")
    monkeypatch.setattr(R, "CRITERIA_BODY",
                        R.CRITERIA_BODY.replace(
                            "- `rounds_to_first_birth <= 8`（入口脚本 "
                            "`.entrypoint.py` 首次同时满足",
                            "# 锚点被抹掉"))
    with pytest.raises(SystemExit):
        R.criteria_body()


# --------------------------------------------- 3. 出生检测的候选名顺序

def _write_entry(path, name, size=400):
    p = path / name
    p.write_text("# x\n" * (size // 4 + 1), encoding="utf-8")
    return p


def test_born_entrypoint_priority_and_absence(tmp_path):
    assert L._born_entrypoint(tmp_path, ("agent_solution.py",)) is None
    _write_entry(tmp_path, ".entrypoint.py")
    assert L._born_entrypoint(tmp_path, (".entrypoint.py",)) == ".entrypoint.py"
    # V4 的候选集里没有 .entrypoint.py ⇒ 写它不算出生（这正是 D26-8d 的反面）
    assert L._born_entrypoint(tmp_path, ("agent_solution.py",)) is None
    _write_entry(tmp_path, "agent_solution.py")
    assert L._born_entrypoint(
        tmp_path, ("agent_solution.py", ".entrypoint.py")) == "agent_solution.py"
    assert L._born_entrypoint(
        tmp_path, (".entrypoint.py", "agent_solution.py")) == ".entrypoint.py"


def test_born_entrypoint_requires_the_three_conditions(tmp_path):
    (tmp_path / "agent_solution.py").write_text("x", encoding="utf-8")  # <200B
    assert L._born_entrypoint(tmp_path, ("agent_solution.py",)) is None
    (tmp_path / "agent_solution.py").write_text(
        "def f(:\n", encoding="utf-8")                                  # 语法错
    assert L._born_entrypoint(tmp_path, ("agent_solution.py",)) is None


# --------------------------------------------- 4. 批间隔离（新战役结构上可开跑）

def _row(batch_id, task=4):
    return {"task": task, "batch_id": batch_id}


def test_campaign_rows_isolate_batches(monkeypatch):
    rows = [_row("P26-B1"), _row("P26-B1"), _row("P26-B1b"), _row("p26-B1")]
    monkeypatch.setattr(R, "_ledger_task_rows", lambda: list(rows))
    R.set_campaign("v3")
    assert len(R._ledger_campaign_rows()) == 3          # P26-B1 + p26-b1 两条
    R.set_campaign("v4")
    assert len(R._ledger_campaign_rows()) == 1


def test_seal_criteria_not_blocked_by_the_other_campaign(
        tmp_path, monkeypatch):
    """V3 已有 20 行实呼，不得因此拒封 V4 判据（反之亦然）。"""
    monkeypatch.setenv("SIMPRAXIS_PHASE26_ROOT", str(tmp_path / "p26"))
    R.set_campaign("v4")
    R.retest_root().mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(R, "_ledger_task_rows", lambda: [_row("P26-B1")] * 20)
    # 无判据件 ⇒ 先报「判据文件缺失」，说明「首呼已发生」闸已放行
    with pytest.raises(SystemExit) as exc:
        R.cmd_seal_criteria()
    assert "判据文件缺失" in str(exc.value)


def test_seal_criteria_still_blocks_same_campaign_after_first_call(
        tmp_path, monkeypatch):
    monkeypatch.setenv("SIMPRAXIS_PHASE26_ROOT", str(tmp_path / "p26"))
    R.set_campaign("v4")
    root = R.retest_root()
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(R, "_ledger_task_rows", lambda: [_row("P26-B1b")])
    with pytest.raises(SystemExit) as exc:
        R.cmd_seal_criteria()
    assert "P26-B1b" in str(exc.value)


def _write_wall_result(root, wall, stamp, *, passed, infra=False):
    d = root / "runs" / wall / stamp
    d.mkdir(parents=True, exist_ok=True)
    rec = {"schema": "simpraxis.phase26.wall_result.v3", "wall": wall,
           "passed": passed, "fail_reasons": [] if passed else ["x"],
           "infra_failure": infra, "run_dir": str(d)}
    (d / R.campaign().wall_result_name).write_text(
        json.dumps(rec), encoding="utf-8")
    return d


def test_guard_single_run_is_scoped_per_campaign(tmp_path, monkeypatch):
    """V3 判负 ≠ V4 判负：新战役必须能在自己的目录里首跑。"""
    monkeypatch.setenv("SIMPRAXIS_PHASE26_ROOT", str(tmp_path / "p26"))
    R.set_campaign("v3")
    root = R.retest_root()
    root.mkdir(parents=True, exist_ok=True)
    _write_wall_result(root, "wallA", "20261103T020317Z", passed=False)
    with pytest.raises(SystemExit):
        R._guard_single_run("wallA")            # V3 判负后重跑被拒（I4-5）
    R.set_campaign("v4")
    assert R._guard_single_run("wallA") == 0    # V4 是全新战役，不在半径内


# --------------------------------------------- 5. 帽：只有 Task4 变，其余不动

def test_caps_only_task4_moved_and_hard_cap_followed():
    assert QL.PHASE26_TASK_CAPS[4] == 56          # 28（V3）+ 28（V4）
    assert QL.PHASE26_TASK_CAPS[7] == 16
    assert QL.PHASE26_TASK_CAPS[8] == 70
    assert QL.PHASE26_TASK_CAPS[9] == 2
    assert QL.PHASE26_HARD_CAP == 148             # 120 + 28
    assert QL.PHASE26_NORMAL_BUDGET == QL.PHASE26_HARD_CAP


def test_envelope_and_subcaps_are_the_frozen_25_plus_3():
    """Task 4b 不重算子帽（硬规则 7：不许抬/降帽）。"""
    assert R.SUBCAPS == {"wallA": 16, "wallB": 9}
    assert R.RESERVE == 3
    assert R.HARD_ENVELOPE == 28
    assert R.TAIL_ROUNDS == 3


# --------------------------------------------- 6. LOCK / 判决件带 campaign 取证

def test_criteria_lock_record_records_campaign(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMPRAXIS_PHASE26_ROOT", str(tmp_path / "p26"))
    R.set_campaign("v4")
    R.retest_root().mkdir(parents=True, exist_ok=True)
    R.criteria_path().write_text("# v4\n", encoding="utf-8")
    os.chmod(R.criteria_path(), 0o444)
    rec = R._criteria_lock_record()
    assert rec["campaign"] == "v4"
    assert rec["batch_id"] == "P26-B1b"
    assert rec["entrypoint_names"] == ["agent_solution.py"]
    assert rec["criteria_path"].endswith("VERDICT_CRITERIA_V4.md")


def test_cmd_run_rejects_the_other_campaign_batch(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMPRAXIS_PHASE26_ROOT", str(tmp_path / "p26"))
    monkeypatch.setenv("SIMPRAXIS_ALLOW_PAID", "1")
    R.set_campaign("v4")
    with pytest.raises(SystemExit) as exc:
        R.cmd_run("wallA", "P26-B1")
    assert "P26-B1b" in str(exc.value)
