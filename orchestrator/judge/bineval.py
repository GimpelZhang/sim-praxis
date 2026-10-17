#!/usr/bin/env python3
"""Task4 Rung4 BinEval 5 题通道（详细计划 L461-463、L477-498）。

与 Phase1 6q 通道**并行独立**（不改其语义）：
- 5 个原子二值问题（rung4_bineval_rubric.json，逐字采计划 L494-498 五问），
  q4 任务目的按 track 槽位注入（gait/libero/fluid 枚举，不接受自由文本）。
- 严格 JSON：模型必须只输出恰含 q1..q5 的扁平 JSON 对象，值只能是 yes/no；
  坏 JSON / markdown 围栏 / 缺题 / 多键 / 非 yes-no / 夹带自信度文本，一律
  抛 BinEvalParseError 拒收（不静默当通过）。自信度自报文本不计分——机判
  置信度唯一来源是 agreement_rate（一致数/5）。
- active 标定件唯一来源：$SIMPRAXIS_PHASE2_ROOT/task4_judge/
  calibration_bineval.json。active 写入条件 fail-closed（计划 L475、L494）：
  片数 ≥10 且逐片 agreement_rate≥0.7 且已知缺陷片召回率=100%；不满足只写
  mode=record_only + 失败报告（含逐片不一致明细），禁止调标签/换片/改阈值。
本模块零网络：VLM 调用在 rung4.BinEvalJudge / 付费测试里经 call_visual 进行。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Optional

from orchestrator.common.clock import utc_now_iso_z
from orchestrator.common.fsutil import atomic_write_text
from orchestrator.common.runtime_paths import phase2_root

QUESTION_IDS: tuple[str, ...] = ("q1", "q2", "q3", "q4", "q5")
ANSWER_YES = "yes"
ANSWER_NO = "no"
VALID_ANSWERS = frozenset({ANSWER_YES, ANSWER_NO})

AGREEMENT_ACTIVE_THRESHOLD = 0.7
DEFECT_RECALL_REQUIRED = 1.0
MIN_CALIBRATION_CLIPS = 10

CALIBRATION_SCHEMA = "simpraxis.bineval_calibration.v1"
CALIBRATION_FILENAME = "calibration_bineval.json"
FAILURE_REPORT_FILENAME = "bineval_failure_report.json"
ANSWER_SUFFIX = "bineval_answer.json"
GROUND_TRUTH_FILENAME = "ground_truth.json"

RUBRIC_PATH = Path(__file__).with_name("rung4_bineval_rubric.json")


class BinEvalParseError(ValueError):
    """模型输出不满足严格 5 题 JSON 契约（区别于网络错误）。"""


def load_bineval_rubric() -> dict:
    with open(RUBRIC_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


def build_prompt(rubric: dict, track: str) -> str:
    """按 track 槽位注入 q4 目标文案。槽位只能取枚举三选一。"""
    slots = rubric.get("track_slots")
    goals = rubric.get("track_goals", {})
    if track not in slots or track not in goals:
        raise ValueError(
            f"track 必须是 rubric 枚举 {slots} 之一，收到 {track!r}")
    lines = [
        "你是具身智能仿真视频的物理一致性核查员。下面是同一段 rollout 的",
        rubric.get("frame_layout", "contact-window 六帧拼图。"),
        f"本片 track={track}，第 4 问的任务目标固定为：{goals[track]}。",
        "逐题作答，每题只允许从 yes/no 中二选一；看不清楚就答 no。",
        "严格只输出一个 JSON 对象（不要解释、不要 markdown 围栏、不要任何"
        "额外文本或自信度声明），恰含 q1..q5 五个键：",
        '{"q1":"yes","q2":"yes","q3":"yes","q4":"yes","q5":"yes"}',
        "",
        "原子题：",
    ]
    for q in rubric["questions"]:
        lines.append(f"- {q['id']}: {q['text']} (yes/no)")
    return "\n".join(lines)


def parse_bineval_answers(raw: str) -> dict[str, str]:
    """严格解析：恰含 q1..q5，值恰为 yes/no。任何偏差抛 BinEvalParseError。"""
    if not isinstance(raw, str):
        raise BinEvalParseError(f"模型输出必须是字符串（收到 {type(raw).__name__}）")
    text = raw.strip()
    if not text or text[0] != "{" or text[-1] != "}":
        raise BinEvalParseError(
            "模型输出必须是且只能是一个 JSON 对象（拒绝围栏/前后缀文本）")
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BinEvalParseError(f"坏 JSON，拒收：{exc}") from exc
    if not isinstance(obj, dict):
        raise BinEvalParseError("JSON 顶层必须是对象")
    keys = set(obj)
    expected = set(QUESTION_IDS)
    if keys != expected:
        missing = sorted(expected - keys)
        extra = sorted(keys - expected)
        raise BinEvalParseError(
            f"键集合必须恰为 {sorted(expected)}（缺题 {missing}、多键 {extra}）；"
            "自信度自报一律不计，多键即拒收")
    answers: dict[str, str] = {}
    for qid in QUESTION_IDS:
        v = obj[qid]
        if not isinstance(v, str):
            raise BinEvalParseError(f"题 {qid} 答案必须是字符串 yes/no，收到 {v!r}")
        ans = v.strip().lower()
        if ans not in VALID_ANSWERS:
            raise BinEvalParseError(f"题 {qid} 答案非法，只认 yes/no：{v!r}")
        answers[qid] = ans
    return answers


def agreement_rate(answers: dict[str, str],
                   labels: dict[str, str]) -> float:
    """机判置信度唯一来源：5 题中与合成标签一致数 / 5。两侧都必须是严格
    q1..q5 → yes/no 映射（标签侧同样严格，坏夹具直接报错而非凑数）。"""
    for side, name in ((answers, "answers"), (labels, "labels")):
        if set(side) != set(QUESTION_IDS):
            raise ValueError(f"{name} 必须恰含 {list(QUESTION_IDS)} 五键")
        bad = [k for k, v in side.items() if v not in VALID_ANSWERS]
        if bad:
            raise ValueError(f"{name} 存在非 yes/no 值：{bad}")
    hits = sum(1 for qid in QUESTION_IDS if answers[qid] == labels[qid])
    return hits / len(QUESTION_IDS)


def is_defect_labels(labels: dict[str, str]) -> bool:
    """已知缺陷片：五题中任一真值为 no（五题统一 good=yes/defect=no）。"""
    return any(labels[qid] == ANSWER_NO for qid in QUESTION_IDS)


def defect_detected(answers: dict[str, str]) -> bool:
    """模型在该片是否检出任何缺陷（任一题答 no）。"""
    return any(answers[qid] == ANSWER_NO for qid in QUESTION_IDS)


# ---------------- 标定件路径/状态（mode 唯一来源） -------------------------

def calibration_bineval_path() -> Path:
    return phase2_root() / "task4_judge" / CALIBRATION_FILENAME


def bineval_calibration_status(path: "Path | None" = None) -> dict:
    """读 BinEval 标定状态。缺件 = uncalibrated（零影响安全默认，不发网）。

    额外回传 active 件的机判证据字段（min_agreement/defect_recall/n_clips），
    供 Rung4 运行时 fail-closed 复检——active 件证据不达标等同坏件，升级人工。
    """
    from orchestrator.judge.rung4 import (  # 局部导入避免循环
        MODE_ACTIVE, MODE_RECORD_ONLY, MODE_UNCALIBRATED)
    p = path or calibration_bineval_path()
    if not p.is_file():
        return {"mode": MODE_UNCALIBRATED, "path": str(p)}
    data = json.loads(p.read_text(encoding="utf-8"))
    return {
        "mode": data.get("mode", MODE_RECORD_ONLY),
        "path": str(p),
        "min_agreement": data.get("min_agreement"),
        "defect_recall": data.get("defect_recall"),
        "n_clips": data.get("n_clips"),
        "batch_id": data.get("batch_id"),
    }


def active_evidence_valid(cal: dict) -> bool:
    """运行时复检：声称 active 的标定件必须带齐达标证据，缺一即不可信。"""
    if cal.get("mode") != "active":
        return False
    n = cal.get("n_clips")
    return (isinstance(n, int) and not isinstance(n, bool)
            and n >= MIN_CALIBRATION_CLIPS
            and isinstance(cal.get("min_agreement"), (int, float))
            and float(cal["min_agreement"]) >= AGREEMENT_ACTIVE_THRESHOLD
            and cal.get("defect_recall") == DEFECT_RECALL_REQUIRED)


# ---------------- 标定目录评估（机判，0 付费） ------------------------------

def evaluate_clips_dir(clips_dir: "str | Path") -> dict:
    """逐片读 ground_truth.json + bineval_answer.json，汇总 agreement/召回。

    缺答案件/坏件进入 errors（fail-closed：有错误就不允许 active）。
    ground_truth 需要 track + labels（5 题）；answer 件由付费运行器写入。
    """
    base = Path(clips_dir)
    if not base.is_dir():
        raise FileNotFoundError(f"标定片目录不存在：{base}")
    rubric = load_bineval_rubric()
    clips: list[dict[str, Any]] = []
    errors: list[str] = []
    for clip_dir in sorted(p for p in base.iterdir() if p.is_dir()):
        gt_path = clip_dir / GROUND_TRUTH_FILENAME
        ans_path = clip_dir / ANSWER_SUFFIX
        name = clip_dir.name
        if not gt_path.is_file():
            errors.append(f"{name}: 缺 {GROUND_TRUTH_FILENAME}")
            continue
        try:
            gt = json.loads(gt_path.read_text(encoding="utf-8"))
            track = gt["track"]
            labels = gt["labels"]
            if track not in rubric["track_slots"]:
                raise ValueError(f"track 非枚举：{track!r}")
            # 严格校验标签本身（坏夹具报错，不静默）。
            agreement_rate({q: ANSWER_YES for q in QUESTION_IDS}, labels)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            errors.append(f"{name}: ground_truth 非法：{exc}")
            continue
        entry: dict[str, Any] = {"clip": name, "track": track,
                                 "labels": labels}
        if not ans_path.is_file():
            errors.append(f"{name}: 缺 {ANSWER_SUFFIX}（VLM 未作答/批次未完）")
            entry.update({"agreement": None, "mismatches": None,
                          "defect_clip": is_defect_labels(labels),
                          "defect_detected": False})
            clips.append(entry)
            continue
        try:
            ans_doc = json.loads(ans_path.read_text(encoding="utf-8"))
            # 答案件契约：{"answers": {q1..q5 -> yes/no}, "raw_text_ref"?: ...}
            if not isinstance(ans_doc, dict) or "answers" not in ans_doc:
                raise BinEvalParseError("答案件必须含 answers 对象")
            answers = parse_bineval_answers(
                json.dumps(ans_doc["answers"], ensure_ascii=False))
        except (BinEvalParseError, KeyError, TypeError,
                json.JSONDecodeError) as exc:
            errors.append(f"{name}: VLM 答案非法：{exc}")
            entry.update({"agreement": None, "mismatches": None,
                          "defect_clip": is_defect_labels(labels),
                          "defect_detected": False})
            clips.append(entry)
            continue
        rate = agreement_rate(answers, labels)
        mismatches = [q for q in QUESTION_IDS if answers[q] != labels[q]]
        entry.update({
            "agreement": rate,
            "mismatches": mismatches,
            "answers": answers,
            "defect_clip": is_defect_labels(labels),
            "defect_detected": defect_detected(answers),
        })
        clips.append(entry)

    judged = [c for c in clips if c["agreement"] is not None]
    min_agreement = min((c["agreement"] for c in judged), default=None)
    defect_clips = [c for c in judged if c["defect_clip"]]
    recalled = [c for c in defect_clips if c["defect_detected"]]
    recall = (len(recalled) / len(defect_clips) if defect_clips else None)
    per_clip_ok = all(c["agreement"] >= AGREEMENT_ACTIVE_THRESHOLD
                      for c in judged)
    can_active = (
        not errors
        and len(clips) >= MIN_CALIBRATION_CLIPS
        and bool(defect_clips)
        and per_clip_ok
        and recall is not None
        and recall >= DEFECT_RECALL_REQUIRED
    )
    return {
        "dir": str(base),
        "n_clips": len(clips),
        "n_defect_clips": len(defect_clips),
        "min_agreement": min_agreement,
        "defect_recall": recall,
        "per_clip_threshold_ok": per_clip_ok,
        "can_active": can_active,
        "clips": clips,
        "errors": errors,
    }


def build_calibration_doc(summary: dict, *, batch_id: "str | None" = None,
                          model: "str | None" = None) -> dict:
    mode = "active" if summary["can_active"] else "record_only"
    doc = {
        "version": 1,
        "schema": CALIBRATION_SCHEMA,
        "ts_iso": utc_now_iso_z(),
        "batch_id": batch_id,
        "model": model,
        "stage": "visual_rollout",
        "rubric": str(RUBRIC_PATH),
        "rubric_version": load_bineval_rubric()["version"],
        "mode": mode,
        "clips_dir": summary["dir"],
        "threshold_agreement": AGREEMENT_ACTIVE_THRESHOLD,
        "required_defect_recall": DEFECT_RECALL_REQUIRED,
        "min_calibration_clips": MIN_CALIBRATION_CLIPS,
        "n_clips": summary["n_clips"],
        "n_defect_clips": summary["n_defect_clips"],
        "min_agreement": summary["min_agreement"],
        "defect_recall": summary["defect_recall"],
        "clips": [
            {"clip": c["clip"], "track": c["track"],
             "agreement": c["agreement"],
             "defect_clip": c["defect_clip"],
             "defect_detected": c["defect_detected"],
             "mismatches": c["mismatches"]}
            for c in summary["clips"]
        ],
        "errors": summary["errors"],
    }
    if mode == "record_only":
        reasons = []
        if summary["errors"]:
            reasons.append(f"{len(summary['errors'])} 个片错误/缺答案")
        if summary["n_clips"] < MIN_CALIBRATION_CLIPS:
            reasons.append(
                f"片数 {summary['n_clips']} < {MIN_CALIBRATION_CLIPS}")
        if not summary["per_clip_threshold_ok"]:
            low = [(c["clip"], c["agreement"]) for c in summary["clips"]
                   if c["agreement"] is not None
                   and c["agreement"] < AGREEMENT_ACTIVE_THRESHOLD]
            reasons.append(f"存在 agreement<{AGREEMENT_ACTIVE_THRESHOLD} 的片：{low}")
        if summary["n_defect_clips"] == 0:
            reasons.append("没有已知缺陷片，无法验证召回")
        elif summary["defect_recall"] is not None and \
                summary["defect_recall"] < DEFECT_RECALL_REQUIRED:
            missed = [c["clip"] for c in summary["clips"]
                      if c["defect_clip"] and not c["defect_detected"]]
            reasons.append(
                f"缺陷片召回 {summary['defect_recall']:.2f} < "
                f"{DEFECT_RECALL_REQUIRED:g}，漏检片：{missed}")
        doc["record_only_reasons"] = reasons
        doc["residual_risk"] = (
            "D7（Rung4 BinEval active 翻转）未达成：本件模式 record_only，"
            "BinEval 意见不进入任何自动逻辑；禁止调标签/换片/改阈值翻案"
            "（硬规则 7），只能以新付费批次重新标定。")
    return doc


def write_calibration(summary: dict, out_path: "str | Path",
                      *, batch_id: "str | None" = None,
                      model: "str | None" = None) -> dict:
    """fail-closed 写标定件；record_only 同时落逐片失败报告。原子写 0600。"""
    out = Path(out_path)
    doc = build_calibration_doc(summary, batch_id=batch_id, model=model)
    out.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(out, json.dumps(doc, ensure_ascii=False, indent=2))
    report_path = out.parent / FAILURE_REPORT_FILENAME
    if doc["mode"] == "record_only":
        report = {
            "ts_iso": doc["ts_iso"], "schema": CALIBRATION_SCHEMA,
            "calibration": str(out), "result": "D7_NOT_ACHIEVED",
            "reasons": doc["record_only_reasons"],
            "per_clip": doc["clips"], "errors": doc["errors"],
            "rule": "agreement>=0.7 逐片 且 缺陷片召回 100% 才允许 active；"
                    "本报告为实测矛盾，禁止翻案（硬规则 7）",
        }
        atomic_write_text(report_path,
                          json.dumps(report, ensure_ascii=False, indent=2))
    return doc


def main(argv: "Optional[list[str]]" = None) -> int:
    ap = argparse.ArgumentParser(
        description="Task4 BinEval 标定机判（0 付费；只汇总已落盘的 VLM 答案）")
    ap.add_argument("--dir", required=True, help="合成标定片目录（逐片子目录）")
    ap.add_argument("--write-calibration", required=True,
                    help="标定件输出路径（active/record_only 均写此件）")
    ap.add_argument("--batch-id", default=None)
    ap.add_argument("--model", default=None)
    args = ap.parse_args(argv)
    summary = evaluate_clips_dir(args.dir)
    doc = write_calibration(summary, args.write_calibration,
                            batch_id=args.batch_id, model=args.model)
    print(json.dumps({
        "mode": doc["mode"],
        "n_clips": doc["n_clips"],
        "n_defect_clips": doc["n_defect_clips"],
        "min_agreement": doc["min_agreement"],
        "defect_recall": doc["defect_recall"],
        "errors": doc["errors"],
        "record_only_reasons": doc.get("record_only_reasons", []),
        "calibration": args.write_calibration,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
