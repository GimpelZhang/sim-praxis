#!/usr/bin/env python3
"""Rung4：visual_rollout 原子 rubric VLM 判定（详细计划 L525-529）。

铁律：
- 唯一入口 gateway.call_visual（stage 硬绑 visual_rollout）；本模块永不
  构造文本 stage 调用；runner/judge 的视觉路径只能经此入口（硬规则1）。
- Rung4 永不单独放行：只产生缺陷项与「升级人工」建议。
- 标定先行：calibration_6q.json 状态为 active 才允许进入组合判定；任一坏片
  漏检 → record_only，Gate2 卡片照显但不参与任何自动逻辑（L527）。
- 规则/VLM 分歧（D8）：Rung1–3 否决但 VLM 全绿，或规则全过但 VLM 报缺陷，
  以规则为准并在结果顶部标 disagreement，由人裁决。

Task4 并行新增 BinEval 5 题通道（BinEvalJudge，见同文件下半部），不改本
6q 通道任何语义：标定件唯一来源
$SIMPRAXIS_PHASE2_ROOT/task4_judge/calibration_bineval.json；缺件/非 active
时该通道零影响（不发网、不升级、字节级不回归 Phase1 行为）。active 下
rules XOR VLM、active 件证据不达标（含 agreement<0.7）、VLM 错误/超时一律
escalate_human fail-closed——绝不静默降级 record_only（计划 L471-473）。
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from .bineval import (
    AGREEMENT_ACTIVE_THRESHOLD,
    BinEvalParseError,
    active_evidence_valid,
    bineval_calibration_status,
    build_prompt as build_bineval_prompt,
    load_bineval_rubric,
    parse_bineval_answers,
)

_RUBRIC_PATH = Path(__file__).with_name("rung4_rubric.json")

MODE_ACTIVE = "active"
MODE_RECORD_ONLY = "record_only"
MODE_UNCALIBRATED = "uncalibrated"


def load_rubric() -> dict:
    with open(_RUBRIC_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


def build_prompt(rubric: dict) -> str:
    layout = rubric.get("frame_layout",
                        "2×3 抽帧拼图（首/接触-5帧/接触/接触+5帧/中/末）。")
    lines = [
        "你是具身智能仿真视频的物理一致性核查员。下面是同一段 rollout 的",
        layout,
        "逐题作答，只允许从 yes/no 中二选一；看不清楚就把 low_confidence 置 true。",
        "严格输出 JSON（不要解释、不要 markdown 围栏）：",
        '{"answers":[{"id":"q1","answer":"yes"}, ...], "low_confidence": false}',
        "",
        "原子题：",
    ]
    for q in rubric["questions"]:
        lines.append(f"- {q['id']}: {q['text']} (yes/no)")
    return "\n".join(lines)


def parse_answers(raw: str, rubric: dict) -> dict:
    """解析 VLM 返回；答案归一化，缺题/非法值报错（不静默当通过）。"""
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        s, e = text.find("{"), text.rfind("}")
        if s < 0 or e <= s:
            raise
        obj = json.loads(text[s:e + 1])
    answers = obj.get("answers")
    if not isinstance(answers, list):
        raise ValueError("Rung4 返回缺 answers 数组")
    by_id = {a.get("id"): str(a.get("answer", "")).strip().lower()
             for a in answers if isinstance(a, dict)}
    valid = set(rubric["answer_set"])
    result = {}
    for q in rubric["questions"]:
        ans = by_id.get(q["id"])
        if ans not in valid:
            raise ValueError(f"Rung4 题 {q['id']} 答案非法/缺失：{ans!r}")
        result[q["id"]] = ans
    low = bool(obj.get("low_confidence", False))
    return {"answers": result, "low_confidence": low,
            "n_questions": len(rubric["questions"])}


def expected_answers(rubric: dict, *, good_clip: bool) -> dict[str, str]:
    """合成标定片的标准答案：好片全部 good_answer；坏片全部 defect_if。"""
    key = "good_answer" if good_clip else "defect_if"
    return {q["id"]: q[key] for q in rubric["questions"]}


def calibration_path() -> Path:
    root = Path(os.environ.get(
        "SIMPRAXIS_PHASE1_ROOT", "/mnt/artifacts/simpraxis/phase1"))
    return root / "task5_judge" / "calibration_6q.json"


def calibration_status(path: "Path | None" = None) -> dict:
    """读标定状态；文件不存在 = uncalibrated（未标定时 Rung4 不参与判定）。"""
    p = path or calibration_path()
    if not p.is_file():
        return {"mode": MODE_UNCALIBRATED, "path": str(p)}
    data = json.loads(p.read_text(encoding="utf-8"))
    return {"mode": data.get("mode", MODE_RECORD_ONLY),
            "bad_clip_misses": data.get("bad_clip_misses", []),
            "good_clip_false_alarms": data.get("good_clip_false_alarms", []),
            "path": str(p)}


@dataclass
class Rung4Result:
    status: str                    # ok | vlm_error | record_only | uncalibrated
    mode: str
    items: list[dict]
    n_defects: int
    low_confidence: bool
    disagreement: "str | None"
    raw_text_ref: "str | None" = None

    def as_dict(self) -> dict:
        return {"status": self.status, "mode": self.mode,
                "items": self.items, "n_defects": self.n_defects,
                "low_confidence": self.low_confidence,
                "disagreement": self.disagreement,
                "raw_text_ref": self.raw_text_ref}


class Rung4Judge:
    """组合 visual 网关与标定闸门。gateway 仅需提供 call_visual。"""

    def __init__(self, gateway, *, rubric: "dict | None" = None,
                 calibration_file: "Path | None" = None):
        self.gateway = gateway
        self.rubric = rubric or load_rubric()
        self.prompt = build_prompt(self.rubric)
        self._calibration_file = calibration_file

    def ask(self, image_b64: str, *, max_tokens: int = 512,
            timeout: int = 120) -> tuple[str, dict]:
        """发一次视觉判定。返回 (原始文本, 解析结果)；stage 由网关硬绑。"""
        raw, _usage = self.gateway.call_visual(
            self.prompt, image_b64, max_tokens=max_tokens, timeout=timeout)
        return raw, parse_answers(raw, self.rubric)

    def judge(self, image_b64: str, rules_pass: "bool | None",
              *, raw_text_ref: "str | None" = None,
              max_tokens: int = 512, timeout: int = 120) -> Rung4Result:
        cal = calibration_status(self._calibration_file)
        if cal["mode"] == MODE_UNCALIBRATED:
            # 从未标定：不发网、不出意见（付费批次未批准时的安全默认）
            return Rung4Result(
                status=MODE_UNCALIBRATED, mode=MODE_UNCALIBRATED, items=[],
                n_defects=0, low_confidence=False,
                disagreement=None, raw_text_ref=raw_text_ref)
        try:
            raw, parsed = self.ask(image_b64, max_tokens=max_tokens,
                                   timeout=timeout)
        except Exception:  # noqa: BLE001 - VLM 故障不得变伪造通过
            return Rung4Result(
                status="vlm_error", mode=cal["mode"], items=[],
                n_defects=0, low_confidence=False, disagreement=None,
                raw_text_ref=raw_text_ref)
        items = [{"id": qid, "answer": ans,
                  "defect": ans == next(
                      q["defect_if"] for q in self.rubric["questions"]
                      if q["id"] == qid)}
                 for qid, ans in parsed["answers"].items()]
        n_defects = sum(1 for it in items if it["defect"])
        vlm_clean = n_defects == 0 and not parsed["low_confidence"]
        disagreement: "str | None" = None
        if cal["mode"] == MODE_ACTIVE:
            # 仅 active 标定允许意见进入自动逻辑（含升级建议）；
            # record_only 卡片照显原始 items，但分歧标志强制清空（L527）。
            if rules_pass is True and n_defects > 0:
                disagreement = "rules_pass_vlm_defects"
            elif rules_pass is False and vlm_clean:
                disagreement = "rules_fail_vlm_clean"
        return Rung4Result(
            status="ok", mode=cal["mode"], items=items,
            n_defects=n_defects,
            low_confidence=parsed["low_confidence"],
            disagreement=disagreement, raw_text_ref=raw_text_ref)


def combine_vector(rules: dict, rung4: "Rung4Result | dict | None") -> dict:
    """汇总分级向量：放行只看 Rung1–3；Rung4 给意见/升级标志。"""
    r4 = rung4.as_dict() if isinstance(rung4, Rung4Result) else rung4
    vector = {
        "rung1": rules["rung1"]["pass"],
        "rung2": rules["rung2"]["pass"],
        "rung3": rules["rung3"]["pass"],
    }
    rules_pass = all(vector.values())
    r4_active = (r4 or {}).get("mode") == MODE_ACTIVE
    out = {"vector": vector, "rules_pass": rules_pass, "rung4": r4,
           "auto_release": rules_pass,
           "escalate_human": bool(
               not rules_pass
               or (r4_active and (r4 or {}).get("disagreement"))
               or (r4_active and (r4 or {}).get("low_confidence"))
               or (r4_active and (r4 or {}).get("status") == "vlm_error"))}
    if (r4 or {}).get("disagreement"):
        out["banner"] = "规则/VLM 分歧：以 Rung1–3 规则为准，由人裁决（D8）"
    return out


# ===========================================================================
# Task4 BinEval 5 题通道（与 6q 通道并行；D7 active 翻转，fail-closed）
# ===========================================================================

# status 取值：
#   uncalibrated             缺标定件：不发网、零影响
#   record_only              非 active 件：照实呼留痕，意见不进自动逻辑
#   bad_active_calibration   active 件证据不达标（含 agreement<0.7）：升级人工
#   vlm_error                active 件在但 VLM 调用/解析失败/超时：升级人工
#   ok                       正常 5 题作答
BINEVAL_STATUS_UNCALIBRATED = "uncalibrated"
BINEVAL_STATUS_RECORD_ONLY = MODE_RECORD_ONLY
BINEVAL_STATUS_BAD_ACTIVE_CALIBRATION = "bad_active_calibration"
BINEVAL_STATUS_VLM_ERROR = "vlm_error"
BINEVAL_STATUS_OK = "ok"


@dataclass
class BinEvalResult:
    status: str
    mode: str
    track: "str | None" = None
    answers: "dict[str, str] | None" = None
    vlm_pass: "bool | None" = None
    rules_pass: "bool | None" = None
    disagreement: "str | None" = None
    reasons: list[str] = field(default_factory=list)
    escalate_human: bool = False
    raw_text_ref: "str | None" = None

    def as_dict(self) -> dict:
        return {"status": self.status, "mode": self.mode, "track": self.track,
                "answers": self.answers, "vlm_pass": self.vlm_pass,
                "rules_pass": self.rules_pass,
                "disagreement": self.disagreement, "reasons": self.reasons,
                "escalate_human": self.escalate_human,
                "raw_text_ref": self.raw_text_ref}


class BinEvalJudge:
    """BinEval 5 题视觉判定。gateway 仅需提供 call_visual（stage 硬绑）。

    mode 唯一来源 = calibration_bineval.json：
    - 缺件（uncalibrated）：零实呼、零升级，与 Phase1 行为字节级一致；
    - record_only：照实呼留痕，但分歧/升级标志强制清空（同 6q 口径）；
    - active 且证据复检通过：rules XOR VLM / VLM 错误超时 → 强制升级；
    - active 但证据不达标（min_agreement<0.7、召回≠100%、片数不足）：
      不静默降级，直接 bad_active_calibration + 升级人工（fail-closed）。
    """

    def __init__(self, gateway, *, rubric: "dict | None" = None,
                 calibration_file: "Path | None" = None):
        self.gateway = gateway
        self.rubric = rubric or load_bineval_rubric()
        self._calibration_file = calibration_file

    def prompt_for(self, track: str) -> str:
        return build_bineval_prompt(self.rubric, track)

    def ask(self, image_b64: str, track: str, *, max_tokens: int = 512,
            timeout: int = 120) -> tuple[str, dict]:
        raw, _usage = self.gateway.call_visual(
            self.prompt_for(track), image_b64,
            max_tokens=max_tokens, timeout=timeout)
        return raw, parse_bineval_answers(raw)

    def judge(self, image_b64: str, rules_pass: "bool | None", track: str,
              *, raw_text_ref: "str | None" = None,
              max_tokens: int = 512, timeout: int = 120) -> BinEvalResult:
        cal = bineval_calibration_status(self._calibration_file)
        mode = cal["mode"]
        if mode == MODE_UNCALIBRATED:
            return BinEvalResult(
                status=BINEVAL_STATUS_UNCALIBRATED, mode=mode, track=track,
                rules_pass=rules_pass, raw_text_ref=raw_text_ref)

        active = mode == MODE_ACTIVE
        if active and not active_evidence_valid(cal):
            # active 件在但证据不达标（含逐片 agreement<0.7、召回≠100%、
            # 片数不足）：不允许静默退回 record_only，升级人工核查。
            return BinEvalResult(
                status=BINEVAL_STATUS_BAD_ACTIVE_CALIBRATION, mode=mode,
                track=track, rules_pass=rules_pass, escalate_human=True,
                reasons=[
                    "active 标定件证据不达标（要求 "
                    f"min_agreement>={AGREEMENT_ACTIVE_THRESHOLD:g}、"
                    "defect_recall=1.0、片数>=10）："
                    f"min_agreement={cal.get('min_agreement')!r}, "
                    f"defect_recall={cal.get('defect_recall')!r}, "
                    f"n_clips={cal.get('n_clips')!r}"],
                raw_text_ref=raw_text_ref)

        try:
            raw, answers = self.ask(image_b64, track,
                                    max_tokens=max_tokens, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - 视觉故障不得变伪造通过
            reasons = [f"VLM 调用失败/超时：{type(exc).__name__}"]
            return BinEvalResult(
                status=BINEVAL_STATUS_VLM_ERROR, mode=mode, track=track,
                rules_pass=rules_pass,
                # active 件在但视觉坏掉 = 升级人工；record_only 仅留痕不升级。
                escalate_human=active,
                reasons=reasons if active else [],
                raw_text_ref=raw_text_ref)

        vlm_pass = all(answers[qid] == "yes" for qid in answers)
        disagreement: "str | None" = None
        if active:
            if rules_pass is True and not vlm_pass:
                disagreement = "rules_pass_vlm_defects"
            elif rules_pass is False and vlm_pass:
                disagreement = "rules_fail_vlm_clean"
        escalate = active and disagreement is not None
        return BinEvalResult(
            status=BINEVAL_STATUS_OK, mode=mode, track=track, answers=answers,
            vlm_pass=vlm_pass, rules_pass=rules_pass,
            disagreement=disagreement if active else None,
            escalate_human=escalate,
            reasons=[f"规则/VLM 分歧：{disagreement}"] if escalate else [],
            raw_text_ref=raw_text_ref)


def bineval_gate_verdict(rules_pass: "bool | None",
                         result: "BinEvalResult | dict | None") -> dict:
    """Gate2 自动通过裁决（BinEval 通道）。

    规则不过本身即升级；active BinEval 升级态同样禁止自动通过。
    result=None / uncalibrated / record_only 零影响——裁决只看规则。
    """
    r = result.as_dict() if isinstance(result, BinEvalResult) else \
        (result or {})
    escalate = bool(not rules_pass or r.get("escalate_human"))
    out = {"rules_pass": bool(rules_pass),
           "bineval_status": r.get("status"),
           "bineval_mode": r.get("mode"),
           "escalate_human": escalate,
           "auto_release": bool(rules_pass) and not escalate}
    if r.get("disagreement"):
        out["banner"] = "规则/VLM 分歧（BinEval）：以规则为准，由人裁决（D8）"
    if r.get("status") == BINEVAL_STATUS_BAD_ACTIVE_CALIBRATION:
        out["banner"] = "BinEval active 标定件证据不达标，升级人工核查（D7）"
    if r.get("status") == BINEVAL_STATUS_VLM_ERROR and r.get("escalate_human"):
        out["banner"] = "BinEval 视觉判定故障（active），升级人工（fail-closed）"
    return out
