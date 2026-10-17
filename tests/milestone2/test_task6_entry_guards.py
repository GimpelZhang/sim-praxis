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
