"""S1-7 双审仪器（六维 + 合并真值表；无第三票）。

纪律（计划 S1-7）：

* **仪器校准先过**：6 例校准包（有效正/有效负/伪重复/过度概括/空模板/矛盾结论）
  经网关 ``coding_debug`` 审阅；任一无效应例被接受（误接收）即停付修仪器，不放宽。
* **审查包自足**：只含研究问题/方法/结果数值/主张；**不含机器判决、不含另一审的
  意见**（负控：夹带哨兵必须被 ``assert_pack_self_contained`` 拒绝）。
* **两通道 × 两研究**：正式双审各跑一次（包冻结后不得回改；见 ``freeze_report``）。
* **合并真值表**：两票全过=PASS；任一双败=FAIL；其余 INCONCLUSIVE；**无第三票**。
* 六维评分：问题与可反驳假设 / 设计与混淆 / 统计与删失 / 结论力度 / 局限与下一步 /
  阴性对照处理。

模型调用一律 ``ModelGateway.call("coding_debug", ...)``（入 2909 账；批准件自批）。
本模块不做网络：``Reviewer`` 接受一个 ``call`` 可调用，便于单测与生产注入。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

__all__ = ["SCHEMA", "DIMENSIONS", "ReviewError", "CALIBRATION_CASES",
           "assert_pack_self_contained", "build_review_pack", "parse_review",
           "merge_truth_table", "run_review", "calibration_expectations",
           "SELF_CONTAINED_FORBIDDEN_KEYS"]

SCHEMA = "simpraxis.rsi_s1.review.v1"

DIMENSIONS = ("problem_hypothesis", "design_confounding", "statistics_censoring",
              "claim_strength", "limitations_next_steps", "negative_control")

#: 自足负控：包内出现这些键或其值含哨兵即拒（夹带另一审意见/机器判决）。
SELF_CONTAINED_FORBIDDEN_KEYS = ("machine_verdict", "pareto_verdict",
                                 "other_review", "reviewer_b_opinion")
SELF_CONTAINED_SENTINEL = "OTHER_REVIEW_SENTINEL"

REVIEW_PROMPT = """你是独立审稿人。请只依据下面这条自足材料审阅一项微研究，
输出 JSON（不要额外文字）：
{"dimension_scores": {{"problem_hypothesis": 0-4, "design_confounding": 0-4,
"statistics_censoring": 0-4, "claim_strength": 0-4,
"limitations_next_steps": 0-4, "negative_control": 0-4}},
 "verdict": "accept" 或 "reject",
 "reason": "一句话理由"}

评判口径：主张必须由给定设计与统计支撑；阴性对照处理是必查维度；过度概括、
伪重复（同一 block 当独立样本）、空模板、与数据矛盾的结论一律 reject。

材料：
{pack}
"""


class ReviewError(RuntimeError):
    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


def _canonical(doc: Any) -> bytes:
    return json.dumps(doc, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _sha(doc: Any) -> str:
    return hashlib.sha256(_canonical(doc)).hexdigest()


# ---------------------------------------------------------------------------
# 校准 6 例（已知答案；仪器 READY 的机判面）
# ---------------------------------------------------------------------------

CALIBRATION_CASES: tuple[dict[str, Any], ...] = (
    {"id": "cal-valid-positive", "expected": "accept",
     "summary": ("研究问题：摩擦系数降低是否增大滑动位移？设计：2 条件 × 32 配对 block，"
                 "宿主私有 nuisance；统计：95% 配对 bootstrap；结果：效应 +0.021 m，"
                 "CI [0.019, 0.023] 不含 0；阴性对照：torsional sham 落 ±0.001 m 等效带。"
                 "结论：降低摩擦增大位移（方向与设计一致）。")},
    {"id": "cal-valid-negative", "expected": "accept",
     "summary": ("研究问题：kp 对跟踪误差的效应是否随采样周期改变？设计：2×2 全因子 32 block；"
                 "结果：DiD 效应 0.002，95% CI [-0.004, 0.008] 含 0；结论：INCONCLUSIVE，"
                 "不宣称交互存在；局限：样本量有限。")},
    {"id": "cal-pseudo-replication", "expected": "reject",
     "summary": ("研究问题：摩擦对位移效应。设计：4 条件，每条件 200 步轨迹。结果：把 200 个"
                 "时间步当 200 个独立样本做 t 检验，得到 p<0.001；结论：强支持假设。"
                 "（同一 run 的时间步不是独立单元）")},
    {"id": "cal-over-generalization", "expected": "reject",
     "summary": ("研究问题：单刚体滑动摩擦效应。结果：效应显著。结论：该结果证明所有接触模型、"
                 "所有机器人操作任务中的摩擦规律，可直接外推到真实世界。")},
    {"id": "cal-empty-template", "expected": "reject",
     "summary": ("研究问题：（略）方法：（见附件）结果：（略）结论：显著优于基线。")},
    {"id": "cal-contradictory", "expected": "reject",
     "summary": ("研究问题：降低摩擦是否增大位移。结果：效应 −0.030 m，CI [−0.032, −0.028]"
                 "（即降低摩擦**减小**位移）；结论：数据支持降低摩擦增大位移。")},
)


def calibration_expectations() -> dict[str, str]:
    return {case["id"]: case["expected"] for case in CALIBRATION_CASES}


def calibration_cases() -> "tuple[dict[str, Any], ...]":
    return CALIBRATION_CASES


# ---------------------------------------------------------------------------
# 审查包（自足 + 冻结）
# ---------------------------------------------------------------------------

def assert_pack_self_contained(pack: Mapping[str, Any]) -> None:
    bad = [k for k in SELF_CONTAINED_FORBIDDEN_KEYS if k in pack]
    if bad:
        raise ReviewError("PACK_NOT_SELF_CONTAINED", f"禁止键 {bad}")
    text = json.dumps(pack, ensure_ascii=False)
    if SELF_CONTAINED_SENTINEL in text:
        raise ReviewError("PACK_NOT_SELF_CONTAINED", "含另一审哨兵")
    for key in ("question", "methods", "results", "claim"):
        if key not in pack:
            raise ReviewError("PACK_INCOMPLETE", f"缺 {key}")


def build_review_pack(*, study: str, question: str, methods: str,
                      results: Sequence[Mapping[str, Any]], claim: Mapping[str, Any],
                      negative_control: str = "") -> dict[str, Any]:
    pack = {
        "schema": f"{SCHEMA}.pack",
        "study": study,
        "question": question,
        "methods": methods,
        "results": [dict(row) for row in results],
        "claim": dict(claim),
        "negative_control": negative_control,
        "nonce": hashlib.sha256(
            json.dumps([study, question, methods, list(results), dict(claim)],
                       ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")).encode("utf-8")).hexdigest()[:16],
    }
    assert_pack_self_contained(pack)
    pack["pack_sha256"] = _sha({k: v for k, v in pack.items() if k != "pack_sha256"})
    return pack


def render_prompt(pack: Mapping[str, Any]) -> str:
    # 用 replace 而非 format：模板里的 JSON 示例本身含花括号。
    return REVIEW_PROMPT.replace(
        "{pack}", json.dumps(pack, ensure_ascii=False, sort_keys=True, indent=2))


# ---------------------------------------------------------------------------
# 解析 / 合并
# ---------------------------------------------------------------------------

def parse_review(text: str, *, pack_sha256: str | None = None) -> dict[str, Any]:
    stripped = (text or "").strip()
    start, end = stripped.find("{"), stripped.rfind("}")
    if start < 0 or end <= start:
        raise ReviewError("REVIEW_PARSE", "输出不含 JSON 对象")
    try:
        doc = json.loads(stripped[start:end + 1])
    except ValueError as exc:
        raise ReviewError("REVIEW_PARSE", str(exc)) from None
    if not isinstance(doc, dict):
        raise ReviewError("REVIEW_PARSE", "不是对象")
    scores = doc.get("dimension_scores")
    if not isinstance(scores, Mapping):
        raise ReviewError("REVIEW_SCHEMA", "缺 dimension_scores")
    missing = [d for d in DIMENSIONS if d not in scores]
    if missing:
        raise ReviewError("REVIEW_SCHEMA", f"缺维度 {missing}")
    for dim in DIMENSIONS:
        value = scores[dim]
        if isinstance(value, bool) or not isinstance(value, (int, float)) \
                or not (0 <= float(value) <= 4):
            raise ReviewError("REVIEW_SCHEMA", f"{dim} 分数非法 {value!r}")
    verdict = str(doc.get("verdict") or "").strip().lower()
    if verdict not in ("accept", "reject"):
        raise ReviewError("REVIEW_SCHEMA", f"verdict={verdict!r}")
    if pack_sha256 is not None:
        doc["pack_sha256"] = pack_sha256
    doc["verdict"] = verdict
    doc["dimension_scores"] = {d: float(scores[d]) for d in DIMENSIONS}
    doc["reason"] = str(doc.get("reason") or "")[:1000]
    return doc


def merge_truth_table(channel_a: Mapping[str, Any], channel_b: Mapping[str, Any]
                      ) -> dict[str, Any]:
    """两票合并：全过=PASS；任一双败=FAIL；其余 INCONCLUSIVE；无第三票。"""
    a = str(channel_a.get("verdict") or "")
    b = str(channel_b.get("verdict") or "")
    if a == b == "accept":
        verdict, rule = "PASS", "two_accept"
    elif "reject" in (a, b):
        verdict, rule = "FAIL", "any_reject"
    else:
        verdict, rule = "INCONCLUSIVE", "mixed_or_unknown"
    return {"verdict": verdict, "rule": rule,
            "channel_a": a, "channel_b": b,
            "third_vote": None}


# ---------------------------------------------------------------------------
# 审阅执行（注入 call 可调用）
# ---------------------------------------------------------------------------

@dataclass
class ReviewRun:
    channel: str
    pack_sha256: str
    raw_text: str
    parsed: Mapping[str, Any]
    tokens: "tuple[int | None, int | None]"
    reviewed_at_utc: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"channel": self.channel, "pack_sha256": self.pack_sha256,
                "raw_text": self.raw_text, "parsed": dict(self.parsed),
                "tokens": list(self.tokens),
                "reviewed_at_utc": self.reviewed_at_utc}


def run_review(call: Callable[[str], "tuple[str, tuple[int | None, int | None]]"],
               *, pack: Mapping[str, Any], channel: str,
               max_tokens: int = 1024) -> ReviewRun:
    assert_pack_self_contained(pack)
    prompt = render_prompt(pack)
    text, tokens = call(prompt)
    parsed = parse_review(text, pack_sha256=str(pack.get("pack_sha256") or ""))
    return ReviewRun(channel=channel, pack_sha256=str(pack.get("pack_sha256") or ""),
                     raw_text=text, parsed=parsed, tokens=tokens)


def freeze_report(document: Mapping[str, Any]) -> dict[str, Any]:
    """冻结审查报告：加 report_sha256；之后任何回改都会使 sha 不符（只读证据）。"""
    frozen = dict(document)
    frozen.pop("report_sha256", None)
    frozen["report_sha256"] = _sha(frozen)
    return frozen


def verify_frozen_report(document: Mapping[str, Any]) -> str:
    claimed = str(document.get("report_sha256") or "")
    body = {k: v for k, v in document.items() if k != "report_sha256"}
    actual = _sha(body)
    if claimed != actual:
        raise ReviewError("REPORT_TAMPERED",
                          f"report_sha256 {claimed[:12]}… != {actual[:12]}…")
    return actual
