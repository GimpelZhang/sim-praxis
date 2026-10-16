#!/usr/bin/env python3
"""Q2 三层分离后的引擎侧契约测试（Task0b，0 付费 0 触网）。

Task0b 前本文件含 13 条光学物理数值断言 + 2 条 write_summary L-10 断言；
脚本与断言均已迁入 KB 内容层：
- solutions/track2_questions/02_lidar_weather/tests/test_optics_model.py（13 条物理）
- solutions/track2_questions/02_lidar_weather/tests/test_analyze_q2_contract.py
  （2 条 L-10 write_summary + 1 条 S-7 load_frame）

引擎仓 sandbox/track2 已删，不再 import 任何 Q2 实现，只保留编排契约：
1. q2_human_gate.stage_optics_model 经 resolver 从内容层 stage 合成 fixture；
2. 非入口落件 0600、sha256 等于 manifest 声明、重跑幂等、读源码不 import、
   workdir 篡改 fail-closed（Q2FactError，不网络兜底）；
3. read_qback_default 在 staged 副本上读到 QBACK_DEFAULT=0.4；
4. 内容层缺方案即 Q2FactError；
5. kb_integration：活 KB stage 同契约 + 迁走的 16 条测试在内容层在场。
"""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import stat
import sys
from pathlib import Path

import pytest

from orchestrator.solutions import resolve_solution
from tests.kb_support import require_kb_integration

_REPO = Path(__file__).resolve().parents[2]
_FIXTURE_ROOT = _REPO / "tests" / "fixtures" / "solutions"
Q2_SOLUTION_ID = "track2_questions/02_lidar_weather"


def _load_q2_gate(monkeypatch, tmp_path, solutions_root):
    """以隔离 env 加载 scripts/milestone1/q2_human_gate.py（模块级常量读 env）。"""
    monkeypatch.setenv("SIMPRAXIS_PHASE1_ROOT", str(tmp_path / "phase1"))
    monkeypatch.setenv("SIMPRAXIS_RUN_DIR", str(tmp_path / "runs"))
    monkeypatch.setenv("SIMPRAXIS_SOLUTIONS_ROOT", str(solutions_root))
    spec = importlib.util.spec_from_file_location(
        "q2_human_gate_task0b_contract",
        str(_REPO / "scripts" / "milestone1" / "q2_human_gate.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# 合成 fixture 内容层：默认套件必跑（不依赖活 KB）
# ---------------------------------------------------------------------------

def test_stage_q2_solution_from_content_layer(monkeypatch, tmp_path):
    q2 = _load_q2_gate(monkeypatch, tmp_path, _FIXTURE_ROOT)
    staged = q2.stage_optics_model()

    # stage 落位：phase1 state 工作目录（非证据树），非入口 0600
    assert staged == q2.Q2_SOLUTION_DIR / "optics_model.py"
    assert staged.is_file()
    assert stat.S_IMODE(staged.stat().st_mode) == 0o600
    # 入口脚本 0700
    entry = q2.Q2_SOLUTION_DIR / "collect_weather_lidar.py"
    assert stat.S_IMODE(entry.stat().st_mode) == 0o700

    # sha256 等于内容层 manifest 声明
    manifest = resolve_solution(Q2_SOLUTION_ID)
    declared = manifest.file_map["optics_model.py"].sha256
    assert _sha256(staged) == declared
    assert _sha256(staged) == _sha256(
        manifest.solution_dir / "optics_model.py")

    # 读源码不 import：stage 后 sys.modules 无 optics_model
    assert "optics_model" not in sys.modules
    # 真人闸门读源码常量在 staged 副本上工作
    assert q2.read_qback_default(staged) == 0.4


def test_stage_idempotent_same_path(monkeypatch, tmp_path):
    q2 = _load_q2_gate(monkeypatch, tmp_path, _FIXTURE_ROOT)
    first = q2.stage_optics_model()
    before = first.read_bytes()
    second = q2.stage_optics_model()
    assert second == first and second.read_bytes() == before


def test_tampered_stage_fail_closed(monkeypatch, tmp_path):
    q2 = _load_q2_gate(monkeypatch, tmp_path, _FIXTURE_ROOT)
    staged = q2.stage_optics_model()
    # 模拟 workdir 内被改写：resolver 复验 sha 必须 fail-closed
    staged.write_text(staged.read_text(encoding="utf-8") + "\nQBACK_DEFAULT = 0.9\n",
                      encoding="utf-8")
    with pytest.raises(q2.Q2FactError, match="stage 失败"):
        q2.stage_optics_model()


def test_missing_solution_fails_closed(monkeypatch, tmp_path):
    empty = tmp_path / "empty_solutions"
    empty.mkdir()
    q2 = _load_q2_gate(monkeypatch, tmp_path, empty)
    with pytest.raises(q2.Q2FactError, match="stage 失败"):
        q2.stage_optics_model()


# ---------------------------------------------------------------------------
# 活 KB 一致性（kb_integration：需 SIMPRAXIS_KB_INTEGRATION=1，默认 skip）
# ---------------------------------------------------------------------------

@pytest.mark.kb_integration
def test_live_kb_q2_stage_sha_and_qback(monkeypatch, tmp_path):
    kb = require_kb_integration()
    q2 = _load_q2_gate(monkeypatch, tmp_path, kb / "solutions")
    staged = q2.stage_optics_model()
    manifest = resolve_solution(Q2_SOLUTION_ID)
    assert _sha256(staged) == manifest.file_map["optics_model.py"].sha256
    assert stat.S_IMODE(staged.stat().st_mode) == 0o600
    assert q2.read_qback_default(staged) == pytest.approx(0.4)


@pytest.mark.kb_integration
def test_migrated_physics_tests_present_in_content_layer():
    """迁走的 16 条断言必须在内容层在场（13 物理 + 2 L-10 + 1 S-7）。"""
    kb = require_kb_integration()
    tdir = kb / "solutions" / Q2_SOLUTION_ID / "tests"

    def _test_count(name: str) -> set[str]:
        tree = ast.parse((tdir / name).read_text(encoding="utf-8"))
        return {n.name for n in tree.body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and n.name.startswith("test_")}

    physics = _test_count("test_optics_model.py")
    contract = _test_count("test_analyze_q2_contract.py")
    assert len(physics) == 13
    assert len(contract) == 3
    assert "test_s7_load_frame_missing_file_and_key" in contract
