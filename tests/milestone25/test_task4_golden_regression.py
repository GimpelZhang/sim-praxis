"""Phase 2.5 Task4 AC-T4-11：agentic=False 旧路径零漂移回归（0 付费）。

三道独立于 test_task0_foundation 的 Task4 侧闸：

1. tmp 重跑黄金冻结器，task6/7/8 三工厂 13 件 prompt 与入库夹具逐字节
   相等（本件是 Task 4 自己的回归闸——agentic 包落地后旧出生管线必须
   一字未动）；
2. 旧引擎模块在全新解释器里 import，``orchestrator.agentic`` 与
   ``smolagents`` 都不得进入 sys.modules（opt-in 接线必须懒导入，
   agentic=False 全路径连模块都不加载）；
3. RunSpec 出生契约＝T0 的 25 旧字段 + LEAD 2026-10-29 方案 A 裁决的
   5 个 agentic opt-in 字段（默认旧契约，非法值 fail-closed）；本测试与
   test_task0_foundation.py:711 已同批改判（STOP 指针已履行，禁止单侧松）。
"""
from __future__ import annotations

import dataclasses
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from orchestrator.agentic.forensics import CACHE_DISK_PREFIX
from orchestrator.agent_runner.runner import coding_call_cache_key

REPO_ROOT = Path(__file__).resolve().parents[2]
GOLDEN_DIR = REPO_ROOT / "tests" / "fixtures" / "phase25_golden"
VENV_PY = "/mnt/venvs/simpraxis/bin/python"


def _load_module(name: str, relpath: str):
    path = REPO_ROOT / relpath
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


golden_mod = _load_module(
    "t4_freeze_golden", "scripts/phase25/freeze_golden.py")


# ---------------------------------------------------------------------------
# 1) 黄金 prompt tmp 重生成：逐字节回归
# ---------------------------------------------------------------------------

def test_oldpath_golden_rebuild_byte_identical(tmp_path):
    rebuilt = tmp_path / "phase25_golden"
    manifest_new = golden_mod.build_golden(rebuilt)
    committed = sorted(p.relative_to(GOLDEN_DIR) for p in GOLDEN_DIR.rglob("*")
                       if p.is_file())
    rebuilt_files = sorted(p.relative_to(rebuilt) for p in rebuilt.rglob("*")
                           if p.is_file())
    assert committed == rebuilt_files, "agentic 落地后黄金文件集都不许变"
    for rel in committed:
        if rel.name == "manifest.json":
            continue  # manifest 下面按结构对账（含构建环境字段）
        assert (GOLDEN_DIR / rel).read_bytes() == (rebuilt / rel).read_bytes(), (
            f"旧路径 prompt 漂移：{rel}（agentic 包必须纯加法）")
    m1 = json.loads((GOLDEN_DIR / "manifest.json").read_text())
    assert m1["schema"] == manifest_new["schema"] == "simpraxis.phase25.t0_golden.v1"
    assert m1["fixed_inputs"] == manifest_new["fixed_inputs"]
    assert m1["factories"] == manifest_new["factories"]


def test_golden_cache_keys_stay_in_legacy_coding_namespace():
    # 13 件入库键全部走旧路径单一源 helper（coding_ 前缀）；
    # agentic 版本化键空间 agentic-v2- 与之零交集，旧键一字不改。
    manifest = json.loads((GOLDEN_DIR / "manifest.json").read_text())
    for factory in manifest["factories"].values():
        for r in factory["records"]:
            expected = coding_call_cache_key(
                r["thread_id"], r["run_idx"], r["round"],
                chunk_index=r["chunk_index"])
            assert r["cache_key"] == expected
            assert r["cache_key"].startswith("coding_")
            assert not r["cache_key"].startswith(CACHE_DISK_PREFIX)
    assert CACHE_DISK_PREFIX == "agentic-v2-"


# ---------------------------------------------------------------------------
# 2) 旧引擎 import 面：不加载 agentic 包 / smolagents（子进程隔离测试污染）
# ---------------------------------------------------------------------------

def test_oldpath_imports_never_load_agentic_or_smolagents():
    code = (
        "import sys, json\n"
        "import orchestrator.agent_runner.runner\n"
        "import orchestrator.graph.cli\n"
        "print(json.dumps({"
        "'agentic': sorted(m for m in sys.modules "
        "if m == 'orchestrator.agentic' "
        "or m.startswith('orchestrator.agentic.')), "
        "'smolagents': sorted(m for m in sys.modules "
        "if m == 'smolagents' or m.startswith('smolagents.'))}))\n"
    )
    proc = subprocess.run(
        [VENV_PY, "-c", code], cwd=str(REPO_ROOT),
        capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    loaded = json.loads(proc.stdout.strip().splitlines()[-1])
    assert loaded["agentic"] == [], (
        "agentic=False 旧路径不得加载 orchestrator.agentic——接线必须懒导入")
    assert loaded["smolagents"] == [], (
        "agentic=False 旧路径不得加载 smolagents——新框架只能活在 opt-in 分支")


# ---------------------------------------------------------------------------
# 3) RunSpec 五字段已按 LEAD 方案 A 落地（AC-T4-11）：opt-in 默认旧契约、
#    __post_init__ fail-closed；与 T0:711 同批改判（STOP 指针已履行）
# ---------------------------------------------------------------------------

_AGENTIC_FIELDS = (
    "agentic", "tool_grants", "step_cap", "token_cap",
    "agentic_trace_relpath",
)


def test_runspec_agentic_fields_landed_opt_in(tmp_path):
    RunSpec = golden_mod.runner_mod.RunSpec
    fields = {f.name: f for f in dataclasses.fields(RunSpec)}
    assert len(fields) == 30                      # 25 旧字段 + 5 新字段
    for name in _AGENTIC_FIELDS:
        assert name in fields
    spec = RunSpec(thread_id="t", workdir=tmp_path, cmd=["python", "x.py"],
                   telemetry_relpath="telemetry.json", batch_id="b")
    # 默认出生契约逐字旧行为：False/空元组/0/0/None。
    assert (spec.agentic, spec.tool_grants, spec.step_cap, spec.token_cap,
            spec.agentic_trace_relpath) == (False, (), 0, 0, None)


@pytest.mark.parametrize("kwargs", [
    {"agentic": 1},
    {"agentic": "yes"},
    {"tool_grants": ["read_file"]},              # 必须 tuple
    {"tool_grants": ("",)},                      # 空授予串非法
    {"tool_grants": (1,)},                       # 非字符串
    {"step_cap": -1},
    {"step_cap": True},                          # bool 不是 int
    {"token_cap": -3},
    {"agentic_trace_relpath": ""},
    {"agentic_trace_relpath": "../escape"},
    {"agentic_trace_relpath": "a/../../b"},
    {"agentic_trace_relpath": "/abs/path"},
    {"agentic_trace_relpath": "x\x00y"},
])
def test_runspec_agentic_fields_fail_closed(tmp_path, kwargs):
    RunSpec = golden_mod.runner_mod.RunSpec
    with pytest.raises(ValueError):
        RunSpec(thread_id="t", workdir=tmp_path, cmd=["python", "x.py"],
                telemetry_relpath="telemetry.json", batch_id="b", **kwargs)


def test_runspec_agentic_fields_accept_legal_values(tmp_path):
    RunSpec = golden_mod.runner_mod.RunSpec
    spec = RunSpec(thread_id="t", workdir=tmp_path, cmd=["python", "x.py"],
                   telemetry_relpath="telemetry.json", batch_id="b",
                   agentic=True, tool_grants=("read_file", "run:python"),
                   step_cap=10, token_cap=200_000,
                   agentic_trace_relpath="agentic_traces/t")
    assert spec.agentic is True and spec.step_cap == 10


def test_agentic_fields_never_enter_legacy_cache_key():
    # 旧路径缓存键单一源 helper 签名不读 RunSpec（更不读五字段）：
    # 同 thread/run/round/chunk 的键与授予/帽/开关无关。
    k1 = coding_call_cache_key("t", 1, 1)
    k2 = coding_call_cache_key("t", 1, 1)
    assert k1 == k2 == "coding_t_r1_round1"
    assert not k1.startswith("agentic")
