#!/usr/bin/env python3
"""Task4 BinEval 付费标定批次 T4-B1-visual（详细计划 L485-498）。

运行方式（真实触网计费，裸跑自动 skip，零触网）：
  $PY -m orchestrator.judge.bineval_clips \\
      --out "$SIMPRAXIS_PHASE2_ROOT/task4_judge/synth" --seed 20261016
  SIMPRAXIS_ALLOW_PAID=1 $PY -m pytest tests/milestone2 -q \\
      -m paid -k bineval_calibration

B2 补测（B1 批次 2026-10-17 供应侧端点级 HTTP200 空体 12/12 全败、零有效
答案封档后）须前置 SIMPRAXIS_T4_BATCH_ID=T4-B2-visual；同片/同标签/同阈值/
同 prompt，禁止借换批次名换片/改标签/改阈值/改 prompt（硬规则 7）。

每片 1 次 visual_rollout（12 片 = 12 次，批次批准 14）；经
ModelGateway.call_visual 唯一入口，ledger 逐次记账（失败也占 seq）。
机判 fail-closed：逐片 agreement≥0.7 且缺陷片召回 100% 才写 active；
否则 calibration_bineval.json 模式 record_only + 失败报告，本测 fail
即 STOP——禁止调标签/换片/改阈值翻案（硬规则 7）。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from orchestrator.common.clock import utc_now_iso_z
from orchestrator.common.fsutil import atomic_write_text
from orchestrator.common.runtime_paths import phase2_root
from orchestrator.gateway.quota_ledger import (
    QuotaLedger,
    billing_context,
    load_approval,
    set_current_evidence,
)
from orchestrator.gateway.router import ModelGateway, redact
from orchestrator.judge.bineval import (
    ANSWER_SUFFIX,
    BinEvalParseError,
    build_prompt,
    evaluate_clips_dir,
    load_bineval_rubric,
    parse_bineval_answers,
    write_calibration,
)

pytestmark = pytest.mark.paid

# 批次 ID：默认 T4-B1-visual（B1 transport void 封档后口径不变）；通道恢复后
# B2 同条件补测用 SIMPRAXIS_T4_BATCH_ID=T4-B2-visual，默认仍为 B1。
BATCH_ID = os.environ.get("SIMPRAXIS_T4_BATCH_ID", "T4-B1-visual")
CAL_TASK = 4
CAL_STAGE = "visual_rollout"
CAL_TIMEOUT_S = 300


def test_bineval_calibration_paid_batch():
    # 双守卫：paid marker（-m 过滤）+ 首行显式环境变量（误触防护，零触网）。
    if os.environ.get("SIMPRAXIS_ALLOW_PAID") != "1":
        pytest.skip(f"paid：BinEval 标定真实触网计费，需书面批准批次 {BATCH_ID} "
                    "并显式 SIMPRAXIS_ALLOW_PAID=1")

    approval = load_approval(BATCH_ID)
    assert approval["task"] == CAL_TASK, approval
    assert approval["stage"] == CAL_STAGE, approval

    synth = Path(os.environ.get(
        "SIMPRAXIS_T4_BINEVAL_DIR",
        str(phase2_root() / "task4_judge" / "synth")))
    if not synth.is_dir():
        pytest.skip(f"合成片目录不存在，先跑 bineval_clips prepare：{synth}")
    clip_dirs = sorted(p for p in synth.iterdir() if p.is_dir())
    assert len(clip_dirs) >= 10, f"标定片至少 10 片，实际 {len(clip_dirs)}"

    judge_root = phase2_root() / "task4_judge"
    ev_dir = judge_root / "evidence"
    ev_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(ev_dir, 0o700)

    def _evidence(stem: str, kind: str, payload: dict) -> str:
        p = ev_dir / f"{stem}.{kind}.json"
        atomic_write_text(p, json.dumps(payload, ensure_ascii=False, indent=2))
        return f"phase2/task4_judge/evidence/{p.name}"

    rubric = load_bineval_rubric()
    gateway = ModelGateway(ledger=QuotaLedger())
    model = gateway.model_id(CAL_STAGE)
    ts = time.strftime("%Y%m%d-%H%M%S")
    thread = f"t4-bineval-cal-{ts}"

    table = []
    for i, cd in enumerate(clip_dirs):
        gt = json.loads((cd / "ground_truth.json").read_text(encoding="utf-8"))
        track = gt["track"]
        png = cd / "bineval_collage.png"
        assert png.is_file(), f"{cd.name} 缺 bineval_collage.png"
        import base64
        img_b64 = base64.b64encode(png.read_bytes()).decode("ascii")
        prompt = build_prompt(rubric, track)
        stem = f"bineval_cal_{i:02d}_{cd.name}"
        req_ref = _evidence(stem, "req", {
            "ts_iso": utc_now_iso_z(),
            "stage": CAL_STAGE, "model": model,
            "thread_id": thread, "batch_id": BATCH_ID,
            "clip": cd.name, "track": track,
            "collage": str(png),
            "ground_truth": str(cd / "ground_truth.json"),
            "rubric_questions": [q["text"] for q in rubric["questions"]],
            "max_tokens": 512, "timeout_s": CAL_TIMEOUT_S,
            "prompt": redact(prompt),
        })
        parsed = None
        parse_error = None
        try:
            with billing_context(task=CAL_TASK, batch_id=BATCH_ID,
                                 thread_id=thread):
                set_current_evidence(req_ref)
                raw, usage = gateway.call_visual(
                    prompt, img_b64, timeout=CAL_TIMEOUT_S)
            _evidence(stem, "resp",
                      {"ok": True, "text": redact(raw),
                       "tokens_in": usage[0], "tokens_out": usage[1]})
            try:
                parsed = parse_bineval_answers(raw)
            except BinEvalParseError as exc:
                parse_error = str(exc)
        except Exception as exc:  # 失败也已由网关占 seq；落失败证据后继续
            _evidence(stem, "resp",
                      {"ok": False, "error": redact(str(exc))[:1000]})
            parse_error = f"call_failed: {type(exc).__name__}: {exc}"
        answer_doc = {"raw_text_ref": req_ref}
        if parsed is not None:
            answer_doc["answers"] = parsed
        else:
            answer_doc["parse_error"] = parse_error
        atomic_write_text(cd / ANSWER_SUFFIX,
                          json.dumps(answer_doc, ensure_ascii=False, indent=2))
        labels = gt["labels"]
        if parsed is not None:
            agree = sum(1 for q in parsed if parsed[q] == labels[q]) / 5
            table.append((cd.name, track, agree,
                          any(labels[q] == "no" for q in labels),
                          any(parsed[q] == "no" for q in parsed)))
        else:
            table.append((cd.name, track, None,
                          any(labels[q] == "no" for q in labels), False))

    print("\n[bineval calibration] clip / track / agreement / defect / "
          "detected:")
    for row in table:
        print(" ", row)

    summary = evaluate_clips_dir(synth)
    cal_path = judge_root / "calibration_bineval.json"
    doc = write_calibration(summary, cal_path, batch_id=BATCH_ID, model=model)
    print(json.dumps({
        "mode": doc["mode"], "n_clips": doc["n_clips"],
        "min_agreement": doc["min_agreement"],
        "defect_recall": doc["defect_recall"],
        "errors": doc["errors"],
        "record_only_reasons": doc.get("record_only_reasons", []),
        "calibration": str(cal_path),
    }, ensure_ascii=False, indent=2))

    if doc["mode"] != "active":
        pytest.fail(
            "D7 未达成（mode=record_only）：STOP，禁止调标签/换片/改阈值翻案；"
            f"失败报告 {judge_root / 'bineval_failure_report.json'}")
