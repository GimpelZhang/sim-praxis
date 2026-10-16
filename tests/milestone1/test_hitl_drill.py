#!/usr/bin/env python3
"""Task9 L580 HITL 真人点验演练（真实付费 + 真实飞书阻塞，paid 测试）。

唯一合法运行方式（首行守卫 fail-closed）：
    SIMPRAXIS_ALLOW_PAID=1 /mnt/venvs/simpraxis/bin/python -m pytest \
        tests/milestone1/test_hitl_drill.py -q -m paid -s

演练内容（用户 2026-10-16 经 AskUserQuestion 书面批准，T9-B1：0/0/4，
抽验 run=t7-maniskill-20261015a）：
1. 从 langgraph 检查点取回该 run 当时 Gate2 的原始 metrics/视频/方案，对同一张
   rung4_collage.png 发 2 次 Rung4 视觉重验实呼（代理复现性，逐次入 ledger，
   task=9，batch_id=T9-B1）；
2. 向真人飞书重放 Gate2 卡片+原生视频（新线程，真实阻塞等待 card_form 回调），
   由真人核对证据原件后点按钮；
3. 全程写 task9_milestone/hitl_drill.log（证据包 E9.2）+ hitl_evidence/ 脱敏
   req/resp。真人 reject 即本测如实失败，禁止任何自动批准。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path

import msgpack
import pytest

from orchestrator.gateway.quota_ledger import (
    QuotaLedger,
    billing_context,
    call_lines,
    read_lines,
    set_current_evidence,
)
from orchestrator.gateway.router import ModelGateway, redact
from orchestrator.judge.rung4 import Rung4Judge
from orchestrator.judge.rungs import evaluate_telemetry_file

pytestmark = pytest.mark.paid

PHASE1_ROOT = Path(os.environ.get(
    "SIMPRAXIS_PHASE1_ROOT", "/mnt/artifacts/simpraxis/phase1"))
RUNS_ROOT = Path(os.environ.get(
    "SIMPRAXIS_RUN_DIR", "/mnt/artifacts/simpraxis/runs")) / "phase1"
CKPT_DB = PHASE1_ROOT / "state" / "langgraph_checkpoints.db"
GATES_DB = PHASE1_ROOT / "state" / "pending_gates.db"

BATCH = "T9-B1"
SOURCE_THREAD = "t7-maniskill-20261015a"
DRILL_THREAD = "t9-hitl-maniskill-20261016"
RUN_DIR = RUNS_ROOT / "task7_track1" / f"run_{SOURCE_THREAD}_1"
EVID_DIR = PHASE1_ROOT / "task9_milestone" / "hitl_evidence"
LOG = PHASE1_ROOT / "task9_milestone" / "hitl_drill.log"
TITLE = ("[T9 HITL 点验重放] Track1 工具链巡礼：ManiSkill PushCube"
         f"（原 run {SOURCE_THREAD}-r1）")
N_REVALIDATIONS = 2

_log_lines: list[str] = []


def _log(line: str) -> None:
    stamped = f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {line}"
    _log_lines.append(stamped)
    print(stamped, flush=True)


def _flush_log() -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    LOG.write_text("\n".join(_log_lines) + "\n", encoding="utf-8")
    os.chmod(LOG, 0o644)


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _original_state() -> dict:
    """从检查点取回原 run 结案时的图状态（只读，msgpack 是 langgraph 原生格式）。"""
    con = sqlite3.connect(f"file:{CKPT_DB}?mode=ro", uri=True)
    try:
        row = con.execute(
            "SELECT checkpoint FROM checkpoints WHERE thread_id=? "
            "ORDER BY checkpoint_id DESC LIMIT 1", (SOURCE_THREAD,)).fetchone()
    finally:
        con.close()
    assert row is not None, f"检查点缺原线程 {SOURCE_THREAD}"
    return msgpack.unpackb(row[0], raw=False, strict_map_key=False)["channel_values"]


def _ledger_seq_set() -> set[int]:
    return {r["seq"] for r in call_lines(read_lines())}


class _DrillVisualGateway:
    """同 task7._VisualEvidenceGateway 的脱敏证据/计费纪律，证据落 Task9 目录。"""

    def __init__(self, gateway, *, run_id: str, collage_path: str):
        self._g = gateway
        self._run_id = run_id
        self._collage = collage_path
        self.req_ref: "str | None" = None

    def model_id(self, stage: str) -> str:
        return self._g.model_id(stage)

    def call_visual(self, prompt: str, image_b64: str, **kwargs):
        EVID_DIR.mkdir(parents=True, exist_ok=True)
        stem = f"rung4_{DRILL_THREAD}-{self._run_id}"
        req = {
            "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "stage": "visual_rollout",
            "model": self._g.model_id("visual_rollout"),
            "thread_id": DRILL_THREAD, "batch_id": BATCH,
            "source_thread": SOURCE_THREAD, "run_id": self._run_id,
            "kind": "task9_hitl_rung4_revalidation",
            "collage_png": self._collage,
            "collage_sha256": _sha(Path(self._collage)),
            "image_b64_len": len(image_b64),
            "max_tokens": kwargs.get("max_tokens", 512),
            "prompt": redact(prompt),
        }
        req_p = EVID_DIR / f"{stem}.req.json"
        req_p.write_text(json.dumps(req, ensure_ascii=False, indent=2),
                         encoding="utf-8")
        os.chmod(req_p, 0o600)
        ref = f"phase1/task9_milestone/hitl_evidence/{req_p.name}"
        self.req_ref = ref
        with billing_context(task=9, batch_id=BATCH, thread_id=DRILL_THREAD):
            set_current_evidence(ref)
            raw, usage = self._g.call_visual(prompt, image_b64, **kwargs)
        resp_p = EVID_DIR / f"{stem}.resp.json"
        resp_p.write_text(json.dumps(
            {"ok": True, "text": redact(raw),
             "tokens_in": usage[0], "tokens_out": usage[1]},
            ensure_ascii=False, indent=2), encoding="utf-8")
        os.chmod(resp_p, 0o600)
        return raw, usage


def test_hitl_drill_spotcheck_maniskill():
    if os.environ.get("SIMPRAXIS_ALLOW_PAID") != "1":
        pytest.skip("paid HITL 演练：需用户书面批准 T9-B1 并显式 "
                    "SIMPRAXIS_ALLOW_PAID=1（计划 L580）")

    EVID_DIR.mkdir(parents=True, exist_ok=True)
    rows_before = call_lines(read_lines())
    used_before = max((r["global_used_after"] for r in rows_before), default=0)
    seq_before = _ledger_seq_set()
    # 凭据件：task/stage/approved 必须与本演练一致；本批此前应零使用
    approval = json.loads(
        (PHASE1_ROOT / "quota/approvals/T9-B1.json").read_text(encoding="utf-8"))
    assert approval["batch_id"] == BATCH and approval["task"] == 9
    assert approval["stage"] == "visual_rollout" and approval["approved"] == 4
    batch_used_before = sum(1 for r in rows_before if r.get("batch_id") == BATCH)
    assert batch_used_before == 0, "T9-B1 已被使用过，演练必须整批可对账"
    assert used_before + approval["approved"] <= 36, (
        f"本批最坏 {used_before}+4 不得越过常态 36")

    state = _original_state()
    chosen, rec = state["chosen"], state["agent_runs"][-1]
    metrics_orig = state["metrics"]
    video = Path(rec["video_path"])
    cover = Path(rec["cover_png"])
    collage = RUN_DIR / "rung4_collage.png"
    telemetry = Path(rec["telemetry_uri"])
    for p in (video, cover, collage, telemetry):
        assert p.is_file(), f"原件缺失：{p}"
    orig_items = metrics_orig["rungs"]["rung4"]["items"]
    _log(f"[hitl] T9-B1 批准 4（实际 ≤2）；全局已用 {used_before}/36；"
         f"抽验 {SOURCE_THREAD}")
    _log(f"[hitl] SIMPRAXIS_ALLOW_PAID=1；证据原件 sha256："
         f"video={_sha(video)[:16]} cover={_sha(cover)[:16]} "
         f"collage={_sha(collage)[:16]} telemetry={_sha(telemetry)[:16]}")
    # 规则真值现场重算（不照抄结案 metrics，证据可重放）
    verdict = evaluate_telemetry_file(telemetry)
    assert verdict["rules_pass"] is True
    _log(f"[hitl] Rung1-3 现场重算 rules_pass=True（vector={verdict['vector']}）")
    _flush_log()

    # 1) 两次 Rung4 视觉重验（同一张 collage，代理复现性）
    image_b64 = base64.b64encode(collage.read_bytes()).decode("ascii")
    model_gw = ModelGateway(ledger=QuotaLedger())
    revalidations = []
    for i in range(1, N_REVALIDATIONS + 1):
        run_id = f"rv{i}"
        dg = _DrillVisualGateway(
            model_gw, run_id=run_id, collage_path=str(collage))
        judge = Rung4Judge(dg)
        r4 = judge.judge(image_b64, rules_pass=True)
        assert r4.status == "ok", f"第 {i} 次重验未成功：{r4.status}"
        assert len(r4.items) == len(orig_items) == 6
        agree = sum(1 for it, o in zip(r4.items, orig_items)
                    if it["answer"] == o["answer"])
        revalidations.append({
            "run_id": run_id, "req_ref": dg.req_ref,
            "mode": r4.mode, "items": r4.items,
            "n_defects": r4.n_defects,
            "low_confidence": r4.low_confidence,
            "agreement_with_original": agree,
        })
        _log(f"[hitl] 重验 {i}/2：status=ok mode={r4.mode} "
             f"n_defects={r4.n_defects} low_conf={r4.low_confidence} "
             f"与原始 r1 一致 {agree}/6 req={dg.req_ref}")
        _flush_log()

    # 两次实呼必须已入 ledger（task=9/batch=T9-B1/ok=true/visual）
    rows_now = call_lines(read_lines())
    new_rows = [r for r in rows_now if r["seq"] not in seq_before]
    assert len(new_rows) == N_REVALIDATIONS
    for r in new_rows:
        assert r["task"] == 9 and r["batch_id"] == BATCH
        assert r["stage"] == "visual_rollout" and r["ok"] is True
        assert r["approval_ref"] == "quota/approvals/T9-B1.json"
    used_after_calls = max(r["global_used_after"] for r in rows_now)
    assert used_after_calls == used_before + N_REVALIDATIONS
    _log(f"[hitl] ledger 新增 seq {[r['seq'] for r in new_rows]}，"
         f"全局 {used_before}→{used_after_calls}/36")

    # 2) Gate2 卡片 + 原生视频重放，真实阻塞等待真人 card_form 裁决
    from orchestrator.feishu_gateway.client import gateway_from_env
    from orchestrator.graph.task7 import build_gate2_explanation

    feishu = gateway_from_env()
    explanation = build_gate2_explanation(
        {"chosen": chosen, "telemetry_uri": str(telemetry)}, rec)
    assert explanation is not None, "sapien 说明块必须存在（真人 D10 要求）"
    _log(f"[hitl] 重放 Gate2 卡片到真人飞书 thread={DRILL_THREAD}，"
         f"video={video.name} duration_ms={rec.get('duration_ms')}；等待点按钮…")
    _flush_log()
    decision = feishu.request_gate2_approval(
        DRILL_THREAD, TITLE, metrics_orig,
        video_path=rec["video_path"],
        intranet_url=rec.get("intranet_url"),
        duration_ms=rec.get("duration_ms"),
        cover_png=rec.get("cover_png"),
        explanation=explanation)
    _log(f"[hitl] 真人裁决返回：{json.dumps(decision, ensure_ascii=False)}")

    # DB 落库核对：必须是 card_form 真人按钮（No Fake Approval）
    con = sqlite3.connect(f"file:{GATES_DB}?mode=ro", uri=True)
    try:
        grow = con.execute(
            "SELECT status,choice,decision,human_notes,source,card_message_id,"
            "media_message_id,open_chat_id FROM pending_gates "
            "WHERE thread_id=? AND gate=2", (DRILL_THREAD,)).fetchone()
    finally:
        con.close()
    assert grow is not None, "演练 Gate2 未落库"
    status, choice, dec, notes, source, card_id, media_id, chat_id = grow
    _log(f"[hitl] DB：status={status} decision={dec} source={source} "
         f"notes={notes!r} card={card_id} media={media_id}")
    assert status == "DECIDED" and source == "card_form"
    assert dec == "approve", f"真人点验未通过：{dec} / 意见：{notes!r}"
    # Gate2 按钮 value 无 choice 字段（与 Gate1 多方案不同），列恒 NULL
    assert choice is None

    # 3) 收尾：批次用量与全局上限，写日志与结构化结果
    rows_final = call_lines(read_lines())
    batch_used = sum(1 for r in rows_final if r.get("batch_id") == BATCH)
    used_final = max(r["global_used_after"] for r in rows_final)
    assert batch_used == N_REVALIDATIONS <= approval["approved"]
    assert used_final <= 36
    result = {
        "schema": "simpraxis.hitl_drill.v1",
        "batch_id": BATCH, "source_thread": SOURCE_THREAD,
        "drill_thread": DRILL_THREAD,
        "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "env_guard": "SIMPRAXIS_ALLOW_PAID=1",
        "global_used_before": used_before, "global_used_after": used_final,
        "revalidations": revalidations,
        "original_rung4": orig_items,
        "rules_recomputed": {"rules_pass": True, "vector": verdict["vector"]},
        "human_decision": {"decision": dec, "choice": choice,
                           "human_notes": notes, "source": source,
                           "card_message_id": card_id,
                           "media_message_id": media_id,
                           "open_chat_id": chat_id},
        "originals_sha256": {
            "video.mp4": _sha(video), "cover.png": _sha(cover),
            "rung4_collage.png": _sha(collage),
            "telemetry.json": _sha(telemetry)},
    }
    rp = EVID_DIR / "drill_result.json"
    rp.write_text(json.dumps(result, ensure_ascii=False, indent=2),
                  encoding="utf-8")
    os.chmod(rp, 0o600)
    _log(f"[hitl] 点验通过（真人 approve/card_form）；T9-B1 用量 "
         f"{batch_used}/4，全局 {used_final}/36；结果 {rp}")
    _flush_log()
