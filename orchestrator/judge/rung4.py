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
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

_RUBRIC_PATH = Path(__file__).with_name("rung4_rubric.json")

MODE_ACTIVE = "active"
MODE_RECORD_ONLY = "record_only"
MODE_UNCALIBRATED = "uncalibrated"


def load_rubric() -> dict:
    with open(_RUBRIC_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


def build_prompt(rubric: dict) -> str:
    lines = [
        "你是具身智能仿真视频的物理一致性核查员。下面是同一段 rollout 的",
        "2×3 抽帧拼图（首/接触-5帧/接触/接触+5帧/中/末）。逐题作答，只允许",
        "从 yes/no 中二选一；看不清楚就把 low_confidence 置 true。",
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
