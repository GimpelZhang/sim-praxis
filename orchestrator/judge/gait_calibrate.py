#!/usr/bin/env python3
"""Task8 Track2 步态 Rung4 六题标定 CLI（详细计划 L561-572，口径同 L526-527）。

与 calibrate.py（Task5 夹爪 rubric）完全平行，但 rubric / 合成片 / 证据目录
相互独立：
- rubric：gait_rung4_rubric.json（冰面滑移/倾覆属预期物理，只查几何时序）
- 合成片：gait_clips.py（2D 踏步人形，好/坏各一）
- 决策件：phase1/task8_track2_judge/calibration_gait.json
- 媒体原件：runs/phase1/task8_gait_calibration/<ts>/

子命令：
  prepare  只生成好/坏合成标定片与 2×3 拼图（0 付费，可在批准前跑）
  run      2 次 visual_rollout 实呼（好片+坏片各 1 次）→ calibration_gait.json
           需 task=8/visual_rollout 批次批准凭据（load_approval 显式拦截；
           网关 preflight 再兜底，无凭据不触网，fail-closed）。
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import time
from pathlib import Path

from orchestrator.common.clock import utc_now_iso_z
from orchestrator.common.fsutil import atomic_write_bytes, atomic_write_text
from orchestrator.gateway.quota_ledger import (
    QuotaLedger,
    billing_context,
    load_approval,
    phase1_root,
    set_current_evidence,
)
from orchestrator.gateway.router import ModelGateway, redact

from .clips import N_FRAMES
from .collage import build_collage_png, select_frame_indices
from .gait_clips import write_gait_clip_bundle
from .rung4 import (
    MODE_ACTIVE,
    MODE_RECORD_ONLY,
    Rung4Judge,
    parse_answers,
)

CAL_TIMEOUT_S = 300
CAL_STAGE = "visual_rollout"
CAL_TASK = 8

RUBRIC_PATH = Path(__file__).with_name("gait_rung4_rubric.json")


def load_gait_rubric() -> dict:
    with open(RUBRIC_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


def gait_calibration_file() -> Path:
    return phase1_root() / "task8_track2_judge" / "calibration_gait.json"


def runs_root() -> Path:
    return Path(os.environ.get(
        "SIMPRAXIS_RUN_DIR", "/mnt/artifacts/simpraxis/runs"))


def prepare(out_dir: "str | Path | None" = None) -> dict:
    """生成好/坏片 + 拼图 + ground truth（纯本地，0 付费）。"""
    ts = time.strftime("%Y%m%d-%H%M%S")
    base = runs_root() / "phase1" / "task8_gait_calibration"
    d = Path(out_dir) if out_dir else base / ts
    d.mkdir(parents=True, exist_ok=True)
    bundles = {}
    for kind in ("good", "bad"):
        b = write_gait_clip_bundle(kind, d)
        frames = b.pop("frames")
        idx = select_frame_indices(N_FRAMES, b["contact_frame"])
        # 中性标签：不向 VLM 泄漏好/坏身份（真值只在 ground_truth.json）
        labels = [f"frame {i:02d}" for i in idx]
        png = d / f"{kind}_collage.png"
        # S-6：拼图原子写 0600；mp4/ground_truth 在 gait_clips 产出点已锁 0600。
        atomic_write_bytes(
            png, build_collage_png([frames[i] for i in idx], labels))
        b["collage_png"] = str(png)
        b["frame_indices"] = idx
        bundles[kind] = b
    manifest = d / "manifest.json"
    # S-6：manifest 原子写 0600 收口 fsutil。
    atomic_write_text(manifest, json.dumps(
        {"ts": ts, "dir": str(d), "rubric": str(RUBRIC_PATH),
         "clips": {k: {"mp4": v["mp4"], "ground_truth": v["ground_truth"],
                       "collage": v["collage_png"],
                       "frame_indices": v["frame_indices"]}
                   for k, v in bundles.items()}},
        ensure_ascii=False, indent=2))
    if not out_dir:
        latest = base / "latest"
        if latest.is_symlink() or latest.exists():
            latest.unlink()
        latest.symlink_to(d.name)
    return {"dir": str(d), "bundles": bundles, "ts": ts}


def _evidence_dir() -> Path:
    d = phase1_root() / "task8_track2_judge"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_evidence(stem: str, kind: str, payload: dict) -> str:
    p = _evidence_dir() / f"{stem}.{kind}.json"
    # S-6：原子写 0600 收口 fsutil。
    atomic_write_text(p, json.dumps(payload, ensure_ascii=False, indent=2))
    return f"phase1/task8_track2_judge/{p.name}"


def run_calibration(batch_id: str, clips_dir: str, *,
                    ev_tag: str = "", timeout_s: int = CAL_TIMEOUT_S) -> dict:
    """2 次实呼（好/坏各 1）→ 逐题混淆矩阵 + 模式闸门。付费调用，需批准。

    闸门口径同 calibrate.py：任一坏片注入缺陷漏检 → record_only；好片误报
    与坏片 q3/q5/q6 误报照实记录。冰面滑移/倾覆不是缺陷的 rubric 语义见
    gait_rung4_rubric.json scoring.note。
    """
    approval = load_approval(batch_id)
    if approval["task"] != CAL_TASK or approval["stage"] != CAL_STAGE:
        raise RuntimeError(
            f"批次凭据归属不符：task={approval['task']} stage={approval['stage']}，"
            f"要求 task={CAL_TASK} stage={CAL_STAGE}")

    rubric = load_gait_rubric()
    defect_answer = {q["id"]: q["defect_if"] for q in rubric["questions"]}
    judge = Rung4Judge(ModelGateway(ledger=QuotaLedger()),
                       rubric=rubric,
                       calibration_file=gait_calibration_file())
    ts = time.strftime("%Y%m%d-%H%M%S")
    thread = f"t8-gait-cal-{ts}" + (f"-{ev_tag}" if ev_tag else "")
    clips = Path(clips_dir)
    clip_meta: dict = {}
    suffix = f"_{ev_tag}" if ev_tag else ""

    for kind in ("good", "bad"):
        png_path = clips / f"{kind}_collage.png"
        gt_path = clips / f"{kind}.ground_truth.json"
        gt = json.loads(gt_path.read_text(encoding="utf-8"))
        expected = gt["expected_answers"]
        png_b64 = base64.b64encode(png_path.read_bytes()).decode("ascii")
        stem = f"gait_cal_{kind}{suffix}"
        req_ref = _write_evidence(stem, "req", {
            # P2（Task0）：Phase 2 新证据行统一严格 UTC Z。
            "ts_iso": utc_now_iso_z(),
            "stage": CAL_STAGE,
            "model": judge.gateway.model_id(CAL_STAGE),
            "thread_id": thread, "batch_id": batch_id,
            "clip": kind, "collage": str(png_path),
            "ground_truth": str(gt_path),
            "rubric": str(RUBRIC_PATH),
            "expected_answers": expected,
            "max_tokens": 512, "timeout_s": timeout_s,
            "prompt": redact(judge.prompt),
        })
        with billing_context(task=CAL_TASK, batch_id=batch_id,
                             thread_id=thread):
            # 锁内适配（计划 L501，同 Task2/4/5）：evidence 引用必须在
            # billing_context 内挂接。
            set_current_evidence(req_ref)
            try:
                raw, usage = judge.gateway.call_visual(
                    judge.prompt, png_b64, timeout=timeout_s)
            except Exception as exc:  # 账行已由网关记账；补失败证据后原样抛
                _write_evidence(stem, "resp",
                                {"ok": False, "error": redact(str(exc))[:1000]})
                raise
        _write_evidence(stem, "resp",
                        {"ok": True, "text": redact(raw),
                         "tokens_in": usage[0], "tokens_out": usage[1]})
        parsed = parse_answers(raw, rubric)
        per_q = [{"id": qid, "expected": expected[qid],
                  "observed": parsed["answers"][qid],
                  "correct": expected[qid] == parsed["answers"][qid]}
                 for qid in expected]
        clip_meta[kind] = {"per_q": per_q,
                           "low_confidence": parsed["low_confidence"]}

    tp = fp = tn = fn = 0
    for meta in clip_meta.values():
        for q in meta["per_q"]:
            observed_defect = q["observed"] == defect_answer[q["id"]]
            expected_defect = q["expected"] == defect_answer[q["id"]]
            if observed_defect and expected_defect:
                tp += 1
            elif observed_defect and not expected_defect:
                fp += 1
            elif not observed_defect and expected_defect:
                fn += 1
            else:
                tn += 1
    bad = clip_meta["bad"]["per_q"]
    misses = [q["id"] for q in bad
              if not q["correct"]
              and q["expected"] == defect_answer[q["id"]]]
    false_alarms_bad = [q["id"] for q in bad
                        if not q["correct"]
                        and q["expected"] != defect_answer[q["id"]]]
    false_alarms = [q["id"] for q in clip_meta["good"]["per_q"]
                    if not q["correct"]]
    mode = MODE_ACTIVE if not misses else MODE_RECORD_ONLY
    out = {
        # P2（Task0）：结论件时间戳严格 UTC Z。
        "version": 1, "ts_iso": utc_now_iso_z(),
        "batch_id": batch_id, "thread_id": thread,
        "stage": CAL_STAGE,
        "model": judge.gateway.model_id(CAL_STAGE),
        "rubric": str(RUBRIC_PATH),
        "rubric_version": rubric["version"],
        "mode": mode,
        "clips_dir": str(clips),
        "clips": clip_meta,
        "bad_clip_misses": misses,
        "good_clip_false_alarms": false_alarms,
        "bad_clip_extra_false_alarms": false_alarms_bad,
        "confusion_matrix": {"tp": tp, "fp": fp, "tn": tn, "fn": fn},
        "residual_risk": ("标定片为 2D 合成示意动画（PIL 踏步人形）而非 Genesis "
                          "渲染；真实 batch 分屏视频与合成片域差异大，Rung4 "
                          "意见只作人工 Gate2 的参考代理（永不单独放行）；"
                          "Task9 HITL 抽验或后续 run 可用真实片复标。"),
    }
    p = gait_calibration_file()
    # S-6：结论件原子写 0600 收口 fsutil。
    atomic_write_text(p, json.dumps(out, ensure_ascii=False, indent=2))
    return out


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(
        description="Phase1 Task8 Track2 步态 Rung4 标定")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prepare", help="合成好/坏步态标定片与拼图（0 付费）")
    p.add_argument("--out", default=None)
    r = sub.add_parser("run", help="2 次 visual_rollout 实呼标定（需批次批准）")
    r.add_argument("--batch-id", required=True)
    r.add_argument("--clips-dir", required=True)
    r.add_argument("--ev-tag", default="",
                   help="证据文件名后缀（重跑时不覆盖前轮 req/resp）")
    r.add_argument("--timeout", type=int, default=CAL_TIMEOUT_S)
    args = ap.parse_args(argv)
    if args.cmd == "prepare":
        info = prepare(args.out)
        print(json.dumps({"dir": info["dir"], "ts": info["ts"]},
                         ensure_ascii=False, indent=2))
        return 0
    out = run_calibration(args.batch_id, args.clips_dir,
                          ev_tag=args.ev_tag, timeout_s=args.timeout)
    print(json.dumps(
        {"mode": out["mode"], "bad_clip_misses": out["bad_clip_misses"],
         "good_clip_false_alarms": out["good_clip_false_alarms"],
         "confusion_matrix": out["confusion_matrix"],
         "calibration": str(gait_calibration_file())},
        ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
