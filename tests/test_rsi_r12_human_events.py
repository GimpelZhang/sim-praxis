"""RSI-R1.2 T3：HumanEvent 真实 CLI 面（Plan §3.2；AC31/32）。

覆盖：

* AC31——``human begin/end`` 是真实 CLI（经 ``cli_r12.main`` 的 parser/异常面），
  持续时长按人工开始/结束计时；中断/未闭事件保留 ``UNKNOWN``，不填 0。
* AC32——实质救援（touch=code/interpretation）触发 ``assisted``；后续成功的
  工具结果不能把它清除，无效救援/纯运维不触发。

全部离线：init 只在 pytest 临时家族根内写件，不触网、不碰 2904 之外账本。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator.rsi_r1 import cli_r12 as CLI
from orchestrator.rsi_r1 import human_events as HE
from orchestrator.rsi_r1 import runtime as RT
from orchestrator.rsi_r1 import state as ST

ENVELOPE = "hc_env"


@pytest.fixture()
def root(tmp_path, monkeypatch) -> Path:
    family = tmp_path / "rsi_r12"
    family.mkdir()
    monkeypatch.setenv("SIMPRAXIS_RSI_R12_ROOT", str(family))
    target = family / ENVELOPE
    rc = CLI.main(["init", "--envelope", ENVELOPE, "--root", str(target)])
    assert rc == 0
    return target


def _ctx(root: Path) -> RT.CampaignContext:
    return RT.CampaignContext(envelope_id=ENVELOPE, root=root.resolve(),
                              protected_roots=())


def _run(capsys, argv) -> dict:
    rc = CLI.main(argv)
    out = capsys.readouterr()
    assert rc == 0, out.err
    return json.loads(out.out.strip().splitlines()[-1])


# ---------------------------------------------------------------------------
# AC31：真实 CLI 计时；未闭事件 UNKNOWN
# ---------------------------------------------------------------------------

def test_begin_end_via_cli_records_duration(root, capsys):
    begin = _run(capsys, ["human", "begin", "--root", str(root),
                          "--kind", "scientific_review", "--actor", "rev-1",
                          "--reason", "审阅 A 方法", "--study", "A",
                          "--touch", "interpretation",
                          "--start-utc", "2026-11-12T10:00:00Z"])
    assert begin["duration_s"] is None and begin["unknown_human_time"] is True
    end = _run(capsys, ["human", "end", "--root", str(root),
                        "--event-id", begin["event_id"],
                        "--end-utc", "2026-11-12T10:37:30Z"])
    assert end["duration_s"] == 2250.0
    assert end["unknown_human_time"] is False
    summary = _run(capsys, ["human", "summary", "--root", str(root)])
    assert summary["complete"] is True
    assert summary["known_seconds_total"] == 2250.0
    assert summary["kinds"]["scientific_review"]["count"] == 1
    assert summary["unknown_event_ids"] == []


def test_interrupted_event_stays_unknown_and_blocks_closeout(root, capsys):
    begin = _run(capsys, ["human", "begin", "--root", str(root),
                          "--kind", "approval", "--actor", "rev-2",
                          "--reason", "未结束的批准",
                          "--start-utc", "2026-11-12T09:00:00Z"])
    summary = _run(capsys, ["human", "summary", "--root", str(root)])
    assert summary["complete"] is False
    assert summary["unknown_human_time"] is True
    assert summary["unknown_event_ids"] == [begin["event_id"]]
    # 未知不进 0：库里同样禁止结案
    log = HE.load(_ctx(root))
    with pytest.raises(HE.HumanEventError) as exc:
        log.assert_complete_for_closeout()
    assert exc.value.code == "UNKNOWN_HUMAN_TIME"
    # 结束后才可结案，时长是真实 30 分钟
    _run(capsys, ["human", "end", "--root", str(root),
                  "--event-id", begin["event_id"],
                  "--end-utc", "2026-11-12T09:30:00Z"])
    log.assert_complete_for_closeout()


def test_reversed_time_is_refused(root, capsys):
    begin = _run(capsys, ["human", "begin", "--root", str(root),
                          "--kind", "maintenance", "--actor", "ops",
                          "--reason", "运维",
                          "--start-utc", "2026-11-12T10:00:00Z"])
    rc = CLI.main(["human", "end", "--root", str(root), "--event-id",
                   begin["event_id"], "--end-utc", "2026-11-12T09:00:00Z"])
    assert rc == 2


# ---------------------------------------------------------------------------
# AC32：实质救援触发 assisted；成功工具结果不能清除
# ---------------------------------------------------------------------------

def _rescue(root, capsys, *, touch: "str | None") -> dict:
    argv = ["human", "rescue", "--root", str(root), "--actor", "maintainer",
            "--reason", "人工修复候选代码", "--order-ref", "ORD-1",
            "--study", "A", "--start-utc", "2026-11-12T11:00:00Z",
            "--end-utc", "2026-11-12T11:20:00Z"]
    if touch:
        argv += ["--touch", touch]
    return _run(capsys, argv)


def test_substantive_rescue_marks_assisted_and_cannot_be_cleared(root, capsys):
    event = _rescue(root, capsys, touch="code")
    assert event["success"] is False and event["adjudicated"] is False
    applied = _run(capsys, ["human", "assisted", "--root", str(root),
                            "--event-id", event["event_id"],
                            "--include-rescue"])
    assert applied["assisted"] is True
    state = ST.load_state(_ctx(root))
    assert state.study_state("A") == ST.ST_ASSISTED
    # 再次裁决/再次 apply 都不会把 ASSISTED 洗回 EXPLORING
    again = _run(capsys, ["human", "assisted", "--root", str(root),
                          "--event-id", event["event_id"]])
    assert again["assisted"] is False
    assert ST.load_state(_ctx(root)).study_state("A") == ST.ST_ASSISTED
    state.mark_assisted("A", reason="重复标记")
    assert state.study_state("A") == ST.ST_ASSISTED
    # 成功的工具结果（study 向前推进）也不能清除 assisted
    with pytest.raises(ST.StateError) as exc:
        state._advance_study("A", ST.ST_CONFIRM_LOCKED, reason="工具成功")
    assert exc.value.code in ("STUDY_TERMINAL", "ILLEGAL_TRANSITION")


def test_non_substantive_rescue_does_not_mark_assisted(root, capsys):
    event = _rescue(root, capsys, touch=None)
    applied = _run(capsys, ["human", "assisted", "--root", str(root),
                            "--event-id", event["event_id"],
                            "--include-rescue"])
    assert applied["assisted"] is False
    assert ST.load_state(_ctx(root)).study_state("A") == ST.ST_EXPLORING
    # 纯运维即使带 include_rescue 也不触发
    maint = _run(capsys, ["human", "begin", "--root", str(root),
                          "--kind", "maintenance", "--actor", "ops",
                          "--reason", "重启网关", "--study", "A",
                          "--touch", "code",
                          "--start-utc", "2026-11-12T12:00:00Z"])
    _run(capsys, ["human", "end", "--root", str(root),
                  "--event-id", maint["event_id"],
                  "--end-utc", "2026-11-12T12:05:00Z"])
    applied = _run(capsys, ["human", "assisted", "--root", str(root),
                            "--event-id", maint["event_id"],
                            "--include-rescue"])
    assert applied["assisted"] is False
    assert ST.load_state(_ctx(root)).study_state("A") == ST.ST_EXPLORING
