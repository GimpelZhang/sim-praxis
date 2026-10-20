#!/usr/bin/env python3
"""飞书网关 break-glass CLI（--cli decide）单测：0 付费、0 触网。

decide 只写 pending_gates.db（不构造网关、不需 FEISHU_* 凭据）。
minor-1（reviewer 裁定，首个付费 Task6 run 前必修，锁内适配：全任务
break-glass 工具统一强化）：批准与打回对称强制真人原文。空批准会先落
DECIDED（决策行不可改写），再被 Task6 _opinion_payload 空 notes
fail-loud 永久楔死；Gate2 位置时付费 batch 已不可恢复。本文件覆盖：

1. 批准：空意见重问，原文落库 source=cli（Gate1 choice 透传）；
2. 打回：空意见重问不回归；
3. y/n 之外非法输入重问不回归（大写 Y 归一）；
4. 端到端楔死闭环：decide 批准原文 → task6._opinion_payload 产出
   simpraxis.gate_opinion.v1（ts_iso strict Z）；stdin "y" 后 EOF：
   EOFError 上抛、mark_decided 不可达、行保 PENDING（空批准无法产生）；
5. 无 --cli 调 decide 一律 exit 2（自动化路径防误触锚点）。
"""
from __future__ import annotations

import re

import pytest

from orchestrator.feishu_gateway import cli as gwcli
from orchestrator.feishu_gateway.store import PendingStore
from orchestrator.graph import task6 as task6_mod
from orchestrator.phase2 import chronicle

_TS_ISO_Z_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def _claim(tmp_path, thread="t-cli", gate=1):
    store = PendingStore(tmp_path / "pending.db")
    assert store.claim_pending(thread, gate, task_title="break-glass Q?")
    return store


def _feed(monkeypatch, answers, *, eof_after=False):
    seq = iter(answers)

    def fake_input(_prompt: str = "") -> str:
        try:
            return next(seq)
        except StopIteration:
            if eof_after:
                raise EOFError
            raise AssertionError("测试输入序列耗尽")

    monkeypatch.setattr("builtins.input", fake_input)


def _argv(db, *extra):
    return ["--cli", "--db", str(db), "decide", "--thread", "t-cli", *extra]


# ---------- 1. 批准：空意见重问，原文落库（Gate1/Gate2 对称） ----------

@pytest.mark.parametrize("gate,choice_arg,expect_choice",
                         [(1, ["--choice", "1"], 1), (2, [], None)])
def test_decide_approve_requires_notes(
        tmp_path, monkeypatch, gate, choice_arg, expect_choice):
    store = _claim(tmp_path, gate=gate)
    _feed(monkeypatch, ["y", "", "   ", "批准：真人原文留档"])
    rc = gwcli.main(_argv(
        tmp_path / "pending.db", "--gate", str(gate), *choice_arg))
    assert rc == 0
    row = store.get_pending("t-cli", gate)
    assert row["status"] == "DECIDED" and row["decision"] == "approve"
    assert row["human_notes"] == "批准：真人原文留档"
    assert row["source"] == "cli" and row["choice"] == expect_choice
    assert isinstance(row["decided_at"], int) and row["decided_at"] > 0
    store.close()


# ---------- 2. 打回：空意见重问行为不回归 ----------

def test_decide_reject_requires_notes(tmp_path, monkeypatch):
    store = _claim(tmp_path, gate=1)
    _feed(monkeypatch, ["n", "", "  ", "打回：请按意见重做"])
    rc = gwcli.main(_argv(tmp_path / "pending.db", "--gate", "1"))
    assert rc == 0
    row = store.get_pending("t-cli", 1)
    assert row["status"] == "DECIDED"
    assert row["decision"] == "reject_with_comments"
    assert row["human_notes"] == "打回：请按意见重做"
    assert row["source"] == "cli" and row["choice"] is None
    store.close()


# ---------- 3. 非法 y/n 输入重问；大写 Y 归一 ----------

def test_decide_invalid_yn_reasked(tmp_path, monkeypatch, capsys):
    store = _claim(tmp_path, gate=1)
    # 两个非法值被重问，大写 Y 归一为批准，随后空意见重问一次。
    _feed(monkeypatch, ["x", "yes", "Y", "", "批准：非法输入后仍可达"])
    rc = gwcli.main(_argv(tmp_path / "pending.db", "--gate", "1"))
    assert rc == 0
    out = capsys.readouterr().out
    assert out.count("只能输入 y 或 n") == 2
    assert "批准也必须填写意见" in out
    row = store.get_pending("t-cli", 1)
    assert row["decision"] == "approve"
    assert row["human_notes"] == "批准：非法输入后仍可达"
    store.close()


# ---------- 4. 楔死闭环：原文 → gate_opinion.v1；EOF 不落 DECIDED ----------

class _StoreShim:
    """task6._opinion_payload 只依赖 feishu.store.get_pending。"""

    def __init__(self, store):
        self.store = store


def test_decide_approval_then_task6_opinion_payload(tmp_path, monkeypatch):
    store = _claim(tmp_path, gate=1)
    _feed(monkeypatch, ["y", "批准：RL 不动，按六指标对质放行"])
    rc = gwcli.main(_argv(tmp_path / "pending.db", "--gate", "1"))
    assert rc == 0
    row = store.get_pending("t-cli", 1)
    payload = task6_mod._opinion_payload(
        _StoreShim(store), "t-cli", 1,
        {"human_notes": row["human_notes"], "source": "cli"})
    assert payload == {
        "schema": chronicle.GATE_OPINION_SCHEMA, "gate": "gate1",
        "decision": "approve",
        "human_notes": "批准：RL 不动，按六指标对质放行",
        "source": "cli", "ts_iso": payload["ts_iso"]}
    assert _TS_ISO_Z_RE.fullmatch(payload["ts_iso"])
    store.close()


def test_decide_eof_after_yes_keeps_pending(tmp_path, monkeypatch):
    """y 之后 EOF：input() 抛 EOFError，mark_decided 不可达。

    空批准在 break-glass 通道无法产生（进程非零、行保 PENDING，重跑可
    重新裁决），楔死链在第一环即断。
    """
    store = _claim(tmp_path, gate=2)
    _feed(monkeypatch, ["y"], eof_after=True)
    with pytest.raises(EOFError):
        gwcli.main(_argv(tmp_path / "pending.db", "--gate", "2"))
    row = store.get_pending("t-cli", 2)
    assert row["status"] == "PENDING"
    assert row["decision"] is None and row["human_notes"] == ""
    assert row["decided_at"] is None
    # 行仍可被再次 decide（PENDING 未被毒化）。
    _feed(monkeypatch, ["y", "重跑后的批准原文"])
    assert gwcli.main(_argv(tmp_path / "pending.db", "--gate", "2")) == 0
    assert store.get_pending("t-cli", 2)["decision"] == "approve"
    store.close()


# ---------- 5. 无 --cli：decide 一律 exit 2（不触网、不写决策） ----------

def test_decide_without_cli_flag_exit_2(tmp_path, monkeypatch):
    store = _claim(tmp_path, gate=1)
    # 不应有任何交互输入：缺 --cli 在 argparse 后立即拒绝。
    monkeypatch.setattr(
        "builtins.input",
        lambda *_a, **_k: pytest.fail("无 --cli 不得进入交互"), )
    argv = ["--db", str(tmp_path / "pending.db"), "decide",
            "--thread", "t-cli", "--gate", "1"]
    assert gwcli.main(argv) == 2
    row = store.get_pending("t-cli", 1)
    assert row["status"] == "PENDING" and row["decision"] is None
    store.close()
