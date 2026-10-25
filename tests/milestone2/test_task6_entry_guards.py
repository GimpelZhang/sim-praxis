"""Phase 2 Task6 入场 LOW-2 — legacy direction CLI 的 ascent 防白烧闸（0 付费）。

纪律：全程 monkeypatch，零 ModelGateway/VectorEngine/飞书真实构造、零联网、
零写穿（SIMPRAXIS_PHASE2_ROOT 已由 conftest 重定向 tmp；证据目录再点到
tmp）。V1 索引合成自 test_task2_ascent._build_fixture_index（生产同 schema）。

退出码 6 的五个分支：
  1. 登记升阶题走默认生产路径；
  2. 同题走 --no-gate（SIMPRAXIS_ALLOW_NO_GATE=1 也不放行）；
  3. V1IndexCorrupt 走默认路径；
  4. V1IndexCorrupt 走 --no-gate；
  5. classify 命中路径抛 V1IndexCorrupt/AscentThresholdError（档案自相矛盾/
     DR 包络漂移）。
另钉：缺档案/空档案/未登记题字节级不回归、track1 不误伤、track1 遇损坏
同样 fail-closed（与图节点 node_classify 语义一致）。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.direction import cli as dir_cli
from orchestrator.feishu_gateway import client as feishu_client
from orchestrator.graph import ascent
from orchestrator.judge.ascent_thresholds import AscentThresholdError
from tests.milestone2.test_task2_ascent import _build_fixture_index

THREAD = "t6-entry-guard"
BATCH = "T6-B1-macro"


def _argv(*extra: str, track: str = "track2", index: int = 1) -> list[str]:
    return ["--thread", THREAD, "--track", track, "--index", str(index),
            "--batch-id", BATCH, *extra]


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """把 legacy CLI 在闸之后的一切构造全部换成记账假件（断言零真实构造）。"""
    calls = {"gateway": 0, "ve": 0, "setter": 0, "feishu": 0,
             "loop": 0, "direction": 0, "closed": 0,
             "loop_kw": None, "direction_kw": None}

    class FakeGateway:
        def __init__(self, ledger=None):
            calls["gateway"] += 1

    class FakeVE:
        def __init__(self):
            calls["ve"] += 1

    class FakeFeishu:
        def close(self):
            calls["closed"] += 1

    class FakeSetter:
        def __init__(self, gateway, ve):
            calls["setter"] += 1

        def run_gate1_loop(self, feishu, **kw):
            calls["loop"] += 1
            calls["loop_kw"] = kw
            return {"status": "approved", "gate1_rejects": 0}

        def run_direction(self, **kw):
            calls["direction"] += 1
            calls["direction_kw"] = kw
            return {}

    def fake_feishu_factory():
        calls["feishu"] += 1
        return FakeFeishu()

    monkeypatch.setattr(dir_cli, "ModelGateway", FakeGateway)
    monkeypatch.setattr(dir_cli, "VectorEngine", FakeVE)
    monkeypatch.setattr(dir_cli, "DirectionSetter", FakeSetter)
    monkeypatch.setattr(dir_cli, "QuotaLedger", lambda: object())
    monkeypatch.setattr(dir_cli, "_evidence_dir", lambda: tmp_path)
    monkeypatch.setattr(feishu_client, "gateway_from_env", fake_feishu_factory)
    return calls


def _point_index(monkeypatch, tmp_path: Path) -> Path:
    index, _ = _build_fixture_index(tmp_path)
    p = tmp_path / "v1_index.json"
    monkeypatch.setattr(ascent, "v1_index_path", lambda: p)
    return p


def _assert_zero_construction(calls: dict) -> None:
    assert calls["gateway"] == 0 and calls["ve"] == 0
    assert calls["setter"] == 0 and calls["feishu"] == 0
    assert calls["loop"] == 0 and calls["direction"] == 0
    assert calls["closed"] == 0


# --- 退出码 6：五个分支 ----------------------------------------------------

def test_legacy_cli_ascent_blocked_default_gate_path(
        tmp_path, monkeypatch, capsys, wired):
    """分支 1：登记升阶题 + 默认生产路径 → 6，零网关/飞书构造、零付费面。"""
    _point_index(monkeypatch, tmp_path)
    rc = dir_cli.main(_argv())
    err = capsys.readouterr().err
    assert rc == 6
    _assert_zero_construction(wired)
    assert "orchestrator.graph.cli" in err
    assert "--task 6" in err and THREAD in err
    assert "ascent" in err and "Gate1" in err


def test_legacy_cli_ascent_blocked_no_gate_even_with_allow_env(
        tmp_path, monkeypatch, capsys, wired):
    """分支 2：--no-gate + 显式放行 env 仍拒（升阶保持单一图入口）。"""
    _point_index(monkeypatch, tmp_path)
    monkeypatch.setenv("SIMPRAXIS_ALLOW_NO_GATE", "1")
    rc = dir_cli.main(_argv("--no-gate"))
    err = capsys.readouterr().err
    assert rc == 6
    _assert_zero_construction(wired)
    assert "orchestrator.graph.cli" in err


def test_legacy_cli_corrupt_index_exit6_default_path(
        tmp_path, monkeypatch, capsys, wired):
    """分支 3：档案损坏走默认路径 → 6（fail-closed，禁静默 standard）。"""
    p = tmp_path / "v1_index.json"
    p.write_text("{broken json", encoding="utf-8")
    monkeypatch.setattr(ascent, "v1_index_path", lambda: p)
    rc = dir_cli.main(_argv())
    err = capsys.readouterr().err
    assert rc == 6
    _assert_zero_construction(wired)
    assert "损坏" in err and "V1" in err


def test_legacy_cli_corrupt_index_exit6_no_gate(
        tmp_path, monkeypatch, capsys, wired):
    """分支 4：档案损坏 + --no-gate 同样 6（闸在 no-gate 分叉之前）。"""
    p = tmp_path / "v1_index.json"
    p.write_text("{broken json", encoding="utf-8")
    monkeypatch.setattr(ascent, "v1_index_path", lambda: p)
    monkeypatch.setenv("SIMPRAXIS_ALLOW_NO_GATE", "1")
    rc = dir_cli.main(_argv("--no-gate"))
    assert rc == 6
    _assert_zero_construction(wired)
    assert "损坏" in capsys.readouterr().err


@pytest.mark.parametrize("exc", [
    pytest.param(ascent.V1IndexCorrupt("题面逐字命中但 sha256 不符（模拟）"),
                 id="sha_mismatch"),
    pytest.param(AscentThresholdError("dr_envelope 漂移（模拟）"),
                 id="envelope_drift"),
])
def test_legacy_cli_classify_path_corruption_exit6(
        tmp_path, monkeypatch, capsys, wired, exc):
    """分支 5：loader 放行但 classify 命中路径发现档案不可信 → 6。"""
    _point_index(monkeypatch, tmp_path)

    def _raise(payload, index):
        raise exc

    monkeypatch.setattr(ascent, "classify_run", _raise)
    rc = dir_cli.main(_argv())
    err = capsys.readouterr().err
    assert rc == 6
    _assert_zero_construction(wired)
    assert "档案" in err


# --- 不回归：standard 路径行为不变 -----------------------------------------

def test_legacy_cli_missing_index_passes_through_default(
        tmp_path, monkeypatch, capsys, wired):
    """缺档案（V1IndexMissing=新克隆/CI 常态）：默认路径逐字走原流程。"""
    monkeypatch.setattr(ascent, "v1_index_path",
                        lambda: tmp_path / "absent.json")
    rc = dir_cli.main(_argv())
    out = capsys.readouterr()
    assert rc == 0
    assert wired["gateway"] == wired["ve"] == wired["setter"] == 1
    assert wired["feishu"] == 1 and wired["closed"] == 1
    assert wired["loop"] == 1 and wired["direction"] == 0
    assert wired["loop_kw"]["track"] == "track2"
    assert wired["loop_kw"]["thread_id"] == THREAD
    assert wired["loop_kw"]["batch_id"] == BATCH
    assert '"status": "approved"' in out.out
    # 证据文件照常落（落在被重定向的 tmp 证据目录）
    written = list(tmp_path.glob("gate1_outcome_*.json"))
    assert len(written) == 1


def test_legacy_cli_empty_entries_passes_through(
        tmp_path, monkeypatch, wired):
    """空 entries 索引与缺档案等价：构造器照常调用，rc 0。"""
    import json
    p = tmp_path / "v1_index.json"
    p.write_text(json.dumps(
        {"schema": ascent.V1_INDEX_SCHEMA, "entries": []}),
        encoding="utf-8")
    monkeypatch.setattr(ascent, "v1_index_path", lambda: p)
    rc = dir_cli.main(_argv())
    assert rc == 0
    assert wired["gateway"] == wired["ve"] == wired["setter"] == 1
    assert wired["loop"] == 1


def test_legacy_cli_unregistered_question_no_gate_passes_through(
        tmp_path, monkeypatch, wired):
    """登记索引在场但问未登记题（track2 #2）：--no-gate 红印路径字节级不变。"""
    _point_index(monkeypatch, tmp_path)
    monkeypatch.setenv("SIMPRAXIS_ALLOW_NO_GATE", "1")
    rc = dir_cli.main(_argv("--no-gate", index=2))
    assert rc == 0
    assert wired["gateway"] == wired["ve"] == wired["setter"] == 1
    assert wired["feishu"] == 0 and wired["loop"] == 0
    assert wired["direction"] == 1
    assert wired["direction_kw"]["bypass_stamp"] is True
    assert wired["direction_kw"]["track"] == "track2"


def test_legacy_cli_track1_not_misfired_with_present_index(
        tmp_path, monkeypatch, wired):
    """track1 恒 standard：登记索引在场也不挡、不引导图入口。"""
    _point_index(monkeypatch, tmp_path)
    rc = dir_cli.main(_argv(track="track1", index=1))
    assert rc == 0
    assert wired["gateway"] == wired["ve"] == wired["setter"] == 1
    assert wired["loop"] == 1
    assert wired["loop_kw"]["track"] == "track1"


def test_legacy_cli_track1_corrupt_index_still_fail_closed(
        tmp_path, monkeypatch, capsys, wired):
    """track1 遇损坏档案同样 6：与 node_classify 一致（损坏先于 track 判定）。"""
    p = tmp_path / "v1_index.json"
    p.write_text("{broken json", encoding="utf-8")
    monkeypatch.setattr(ascent, "v1_index_path", lambda: p)
    rc = dir_cli.main(_argv(track="track1", index=1))
    assert rc == 6
    _assert_zero_construction(wired)
    assert "损坏" in capsys.readouterr().err


# ===========================================================================
# ⑨ Task6 run7b 提交 B：rc0 批次结构闸 opt-in 边界（冻结规格
#    .omc/handoffs/task6-schema-gate-design.md v1.0，2026-10-24；简报 §3c）。
#    仅 Task6 工厂传 batch_schema_check=True；task7/task8 一字不传，
#    RunSpec 默认 False → 逐字节旧行为。AST 钉死 + 行号锚定。
# [run7b-schema-gate-tests]
# ===========================================================================

import ast  # noqa: E402 （提交 B 段局部导入）
import dataclasses  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[2]
_RUNSPEC_SITES = (
    ("orchestrator/graph/task6.py", 306),
    ("orchestrator/graph/task7.py", 161),
    ("orchestrator/graph/task8.py", 209),
)


def _runspec_call(rel, pinned_lineno):
    tree = ast.parse((_REPO_ROOT / rel).read_text(encoding="utf-8"))
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name)
             and n.func.id == "RunSpec"]
    assert len(calls) == 1
    assert calls[0].lineno == pinned_lineno, (
        f"{rel} RunSpec 调用行漂移：{calls[0].lineno} != {pinned_lineno}，"
        "更新测试并核对冻结设计 §3")
    return calls[0]


def test_batch_schema_check_opted_in_only_by_task6():
    c6 = _runspec_call(*_RUNSPEC_SITES[0])
    flag = {kw.arg: kw.value for kw in c6.keywords}.get("batch_schema_check")
    assert isinstance(flag, ast.Constant) and flag.value is True

    for rel, lineno in _RUNSPEC_SITES[1:]:
        c = _runspec_call(rel, lineno)
        assert all(kw.arg != "batch_schema_check" for kw in c.keywords), (
            f"{rel} 不得透传 batch_schema_check（默认关旧行为）")


def test_batch_schema_check_runspec_default_off():
    from orchestrator.agent_runner.runner import RunSpec
    field = next(f for f in dataclasses.fields(RunSpec)
                 if f.name == "batch_schema_check")
    assert field.default is False


# ===========================================================================
# ⑩ 锁内适配 Option 2（用户 2026-10-25 裁决）：host-shim 双闸反馈入编码环
#    opt-in 边界——仅 Task6 工厂传 shim_topology_check=True；task7/task8 一字
#    不传，RunSpec 默认 False → 逐字节旧行为。闸本体
#    （task6._require_shim_evidence）一字不改，包装器＝同一闸单真值。
# ===========================================================================

def test_shim_topology_check_opted_in_only_by_task6():
    c6 = _runspec_call(*_RUNSPEC_SITES[0])
    flag = {kw.arg: kw.value for kw in c6.keywords}.get("shim_topology_check")
    assert isinstance(flag, ast.Constant) and flag.value is True

    for rel, lineno in _RUNSPEC_SITES[1:]:
        c = _runspec_call(rel, lineno)
        assert all(kw.arg != "shim_topology_check" for kw in c.keywords), (
            f"{rel} 不得透传 shim_topology_check（默认关旧行为）")


def test_shim_topology_check_runspec_default_off():
    from orchestrator.agent_runner.runner import RunSpec
    field = next(f for f in dataclasses.fields(RunSpec)
                 if f.name == "shim_topology_check")
    assert field.default is False


def test_scan_shim_topology_gate_reuses_gate_single_source():
    """包装器＝_require_shim_evidence 同一闸：违例原文逐字 Gate2 文案、
    空表＝同判放行；9/2 契约/正则/窗口单真值不复制（Option 2）。"""
    from orchestrator.graph import task6 as task6_mod

    def _torch(pid):
        return ("[simpraxis-host-shim v1] 平台托底（非模型代码） site=torch "
                f"pid={pid} label=run_x array_migrated=1 numpy_migrated=0 "
                "cpu_grad_detached=0\n")

    def _idle(pid):
        return ("[simpraxis-host-shim v1] 平台托底（非模型代码） site=idle "
                f"pid={pid} label=x array_migrated=0 numpy_migrated=0 "
                "cpu_grad_detached=0\n")

    def _tl(n_torch, n_idle):
        tail = "".join(_torch(i) for i in range(n_torch))
        tail += "".join(_idle(i) for i in range(n_idle))
        return [{"exec": 1, "rc": 0, "stderr_only_tail": tail}]

    # 9/2 契约形 → 放行（空表）。
    assert task6_mod.scan_shim_topology_gate(_tl(9, 2)) == []
    # run25 墙形（9 torch + 11 idle）→ 恰 1 条违例，文案与 Gate2 逐字同源。
    errs = task6_mod.scan_shim_topology_gate(_tl(9, 11))
    assert len(errs) == 1
    assert errs[0].startswith("Task6 Gate2：")
    assert "site=idle 行数 11 != 契约 2" in errs[0]
    # torch 计数违例走同一闸同一文案（8/2 先于 idle 判）。
    errs8 = task6_mod.scan_shim_topology_gate(_tl(8, 2))
    assert len(errs8) == 1
    assert "site=torch atexit 行数 8 != 契约 9" in errs8[0]
    # 非 rc0 条目不参与 9/2 硬闸（弱闸形态下 shim 在场即放行——与 Gate2 一致：
    # 包装器只在 runner telemetry_ok（rc==0）路径被调用，非 rc0 复现仅防回归）。
    tl_weak = [{"exec": 1, "rc": 1,
                "stderr_only_tail": _torch(0) + _idle(0)}]
    assert task6_mod.scan_shim_topology_gate(tl_weak) == []
