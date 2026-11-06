"""P281-4 提示面隔离测试（AC-P281-4-04 / -05 / -19 / -06，计划 §8.7 / §8.3.1）。

四条机判构成一组**同方向**的断言：候选看见的东西里，不许有判据（数值与文件）、
不许有「去哪读判据」的坐标（绝对路径字面），且交付件必须是运行结束时刻的冻结
快照（否则候选可以在评测前改写答案）。

为什么 AC-19 的检测器要单独造三种**合成泄漏样本**：一个只会对真实 Trial 报
「干净」的检测器，无法区分「真的干净」和「检测器根本没在扫」。三种样本各命中
一次，等于给三条通道各做一次正对照；干净样本返回 ``[]`` 才能排除「恒红」的
反向失效。

AC-06 的权限面同样是**双向**的：0700/0600 是「别人读不到」的正向保证，而
``delivered/`` 无符号链接是「评测器不会顺着链接读到运行外的件」的另一半。
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from orchestrator.benchmark.phase281 import anchor as anchor_mod
from orchestrator.benchmark.phase281 import candidate_birth, constants
from orchestrator.benchmark.phase281 import task_prompt, telemetry
from tests.benchmark import phase281_parity as parity
from tests.benchmark import phase281_trial_fixtures as F


# --------------------------------------------------------------------------
# AC-P281-4-04：TASK.md 不含任何判据阈值
# --------------------------------------------------------------------------

def test_ac_p281_4_04_no_threshold_leak(tmp_path):
    """三个任务逐条验：数值零命中 + ASCII 旁证零命中 + 两臂逐字节同文。"""
    for task_id in constants.TASK_IDS:
        spec = candidate_birth.load_task(task_id)
        text = task_prompt.render_task_md(
            spec, rep=0, seed="0" * 64, input_relpaths=("task_snapshot.json",))
        assert task_prompt.threshold_hits(text, spec=spec) == [], task_id
        assert "threshold" not in text.lower(), task_id
    # M1/R1 有判据、L1 没有（``{}``）：检测器在「无判据」时不可能是靠恒红通过
    assert task_prompt.threshold_values(
        candidate_birth.load_task("mujoco_contact_push_v1"))
    assert task_prompt.threshold_values(
        candidate_birth.load_task("libero_env_adapter_v1")) == []

    # 真流水线产出的 TASK.md：两臂**逐字节**相同（臂 id 不进说明）
    spec = candidate_birth.load_task("icy_ground_dr_v1")
    legacy = F.run_case(tmp_path, task_id="icy_ground_dr_v1", arm="legacy_v1")
    agentic = F.run_case(tmp_path, task_id="icy_ground_dr_v1", arm="agentic_v1")
    legacy_md = legacy.base / "TASK.md"
    agentic_md = agentic.base / "TASK.md"
    text = legacy_md.read_text(encoding="utf-8")
    assert text == agentic_md.read_text(encoding="utf-8")
    assert task_prompt.threshold_hits(text, spec=spec) == []
    assert "threshold" not in text.lower()
    # trial.json 记的是同一份 bytes 的 sha256（不是另算一遍）
    assert legacy.trial["task_md"]["sha256"] == task_prompt.task_md_sha256(text)
    assert legacy.trial["task_md"]["path"] == str(legacy_md)

    # 负例：判据值 / ASCII 旁证一旦混进渲染件，写盘**前**就被拒，盘上不留件
    spec_m1 = candidate_birth.load_task("mujoco_contact_push_v1")
    values = task_prompt.threshold_values(spec_m1)
    assert 2.765 in values and 0.001 in values
    for poison in ("接触力峰值上限 2.765 N", "the threshold is a secret",
                   "max penetration 0.001 m"):
        target = tmp_path / "poison"
        with pytest.raises(task_prompt.PromptIsolationError):
            task_prompt.write_task_md.__globals__["_assert_clean"](poison, spec_m1)
        with pytest.raises(task_prompt.PromptIsolationError):
            _write_poisoned(target, spec_m1, poison)
        assert not (target / task_prompt.TASK_MD_NAME).exists()


def _write_poisoned(target: Path, spec, poison: str):
    """把渲染件替换成毒文本后调 ``write_task_md``（验自检在**落盘前**）。"""
    original = task_prompt.render_task_md
    task_prompt.render_task_md = lambda *a, **k: poison
    try:
        return task_prompt.write_task_md(target, spec, rep=0, seed="0" * 64)
    finally:
        task_prompt.render_task_md = original


# --------------------------------------------------------------------------
# AC-P281-4-05：criteria/ 不在 input/、不在 workdir/
# --------------------------------------------------------------------------

def test_ac_p281_4_05_criteria_not_in_workdir(tmp_path):
    """判据文件只在数据根 ``private/criteria/``；候选面一个字节都没有。"""
    ctx = parity.build_root(tmp_path)          # 仓外锚 + private/criteria/
    root = ctx["root"]
    run = F.run_case(tmp_path, task_id="icy_ground_dr_v1")
    base = run.base

    # ① 路径面：input/ 与 workdir/ 里没有任何 criteria 目录/件
    for area in ("input", "workdir"):
        hits = [p.relative_to(base).as_posix()
                for p in (base / area).rglob("*")
                if "criteria" in p.name.lower()]
        assert hits == [], area
    assert not (base / "input" / "criteria").exists()
    assert not (base / "workdir" / "criteria").exists()
    # 判据确实在场——在数据根的 private/criteria 里（Trial 之外）
    crit = root / "private" / "criteria" / "icy_ground_dr_v1.json"
    assert crit.is_file() and stat.S_IMODE(crit.stat().st_mode) == 0o600
    assert anchor_mod.anchor_path(root).is_file()

    # ② 内容面：输入快照不带判据体（连键名都不带）
    snapshot = json.loads((base / "input" / "task_snapshot.json")
                          .read_text(encoding="utf-8"))
    assert "thresholds" not in snapshot
    assert "thresholds" not in json.dumps(snapshot, ensure_ascii=False)

    # ③ 检测器在真 Trial 上也不许有意见（候选面 = delivered + workdir）
    criteria = json.loads(crit.read_text(encoding="utf-8"))
    assert criteria.get("thresholds")
    assert telemetry.leak_scan(base, root=root, criteria=criteria) == []


# --------------------------------------------------------------------------
# AC-P281-4-19：文件系统通道检测器（三通道各一条正例 + 干净样本）
# --------------------------------------------------------------------------

def _build_sample(base: Path, *, leak: str) -> Path:
    """造一个 Trial 形状的目录，按 ``leak`` 注入**一条**泄漏。"""
    root = base / "root"
    trial = base / "trial"
    for path in (root / "private" / "criteria", root / "references",
                 trial / "delivered", trial / "workdir"):
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(path, 0o700)
    (root / "private" / "criteria" / "icy_ground_dr_v1.json").write_text(
        json.dumps({"thresholds": {"ablation_min_groups": {"value": 3}}}),
        encoding="utf-8")
    (root / "references" / "reference_solution.py").write_text(
        "print('the reference answer')\n", encoding="utf-8")
    (trial / "delivered" / "experiment_plan.yaml").write_text(
        "sections: []\n", encoding="utf-8")
    (trial / "workdir" / "metrics.json").write_text(
        json.dumps({"groups": 1}), encoding="utf-8")
    if leak == "absolute_path":
        (trial / "delivered" / "notes.md").write_text(
            f"读这里：{root / 'private'} / 或 /private/criteria\n", encoding="utf-8")
    elif leak == "sha256_match":
        (trial / "delivered" / "copied.py").write_bytes(
            (root / "references" / "reference_solution.py").read_bytes())
    elif leak == "threshold_constant":
        (trial / "workdir" / "metrics.json").write_text(
            json.dumps({"groups": 3}), encoding="utf-8")
    elif leak != "clean":
        raise AssertionError(leak)
    return trial, root


def test_ac_p281_4_19_leak_scan(tmp_path):
    """三条通道各命中一次、且**只**命中一条；干净样本返回 ``[]``。"""
    criteria = {"thresholds": {"ablation_min_groups": {"value": 3}}}
    kinds = ("absolute_path", "sha256_match", "threshold_constant")
    for kind in kinds:
        trial, root = _build_sample(tmp_path / kind, leak=kind)
        hits = telemetry.leak_scan(trial, root=root, criteria=criteria)
        assert len(hits) == 1, (kind, hits)
        assert hits[0]["kind"] == kind, (kind, hits)
        assert hits[0]["path"] and hits[0]["detail"]

    clean, root = _build_sample(tmp_path / "clean", leak="clean")
    assert telemetry.leak_scan(clean, root=root, criteria=criteria) == []

    # 检测器自身的两条边界：目录不在场 → 抛；无判据时阈值通道静默（不是恒红）
    with pytest.raises(telemetry.LeakScanError):
        telemetry.leak_scan(tmp_path / "nope", root=root, criteria=criteria)
    trial, root = _build_sample(tmp_path / "nothresh", leak="clean")
    assert telemetry.leak_scan(trial, root=root, criteria=None) == []


# --------------------------------------------------------------------------
# AC-P281-4-06：Trial 目录权限与符号链接
# --------------------------------------------------------------------------

def test_ac_p281_4_06_trial_dir_hygiene(tmp_path):
    """逐级 0700 / 逐件 0600；Trial 树里没有符号链接。"""
    run = F.run_case(tmp_path, task_id="icy_ground_dr_v1",
                     engine=F.Engine(calls=1,
                                     write={"experiment_plan.yaml": "a: 1\n"}))
    base = run.base
    bad: "list[str]" = []
    links: "list[str]" = []
    for path in [base] + sorted(base.rglob("*")):
        mode = stat.S_IMODE(path.lstat().st_mode)
        rel = path.relative_to(base).as_posix() if path != base else "."
        if path.is_symlink():
            links.append(rel)
            continue
        want = 0o700 if path.is_dir() else 0o600
        if mode != want:
            bad.append(f"{rel}: {oct(mode)} ≠ {oct(want)}")
    assert bad == [], bad
    assert links == [], links

    # delivered/ 是包含式采集的结果：集合与清单逐件相等，且只含常规文件
    delivered = run.base / "delivered"
    on_disk = {p.relative_to(base).as_posix()
               for p in delivered.rglob("*") if p.is_file()}
    manifest = {entry["relpath"] for entry in run.manifest["deliverables"]}
    assert on_disk == manifest, (sorted(on_disk), sorted(manifest))
    assert all(not (base / rel).is_symlink() for rel in manifest)
    assert "delivered/runtime_fingerprint.json" in manifest
    assert "delivered/run_manifest.json" in manifest
