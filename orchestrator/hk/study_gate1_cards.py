"""S1 Gate1 多 Stage 研究目标卡（S1-3）。

计划：``stages[]`` 链（Stage A_N / Stage B_N / Stage REVIEW，各带目标/预算/依赖/
停止条件）→ 注入 ``GraphDeps.gate1`` 的生产实现（飞书长连接卡片 schema 2.0 +
P2P 文本双通道）。

**零修改卡面**（计划 §2）：本模块只**构造内容**，复用现役
``feishu_gateway.cards.build_gate1_card/build_gate1_card_v2``（不改 cards.py）。
多 Stage 链作为一个方案（proposal）呈现：name=链名、focus=Stage 目标与依赖、
artifacts=预算与停止条件；真人批准粒度=整链。

打回循环语义不在这里实现（driver 已对齐 nodes.py）：
- 打回必须带 human_notes（空意见结案）；
- 2 次打回转 human_failure；
- 批准前零付费零实跑（No Fake Approval）。
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

__all__ = ["SCHEMA", "GoalCardError", "stage_summary", "build_goal_card",
           "build_gate1_card_payload"]

SCHEMA = "simpraxis.rsi_s1.gate1_card.v1"

#: 每个 Stage 必填字段（缺一即拒绝：卡面不允许半张链）。
STAGE_KEYS = frozenset({"stage_id", "goal", "budget", "depends_on", "stop_rules"})


class GoalCardError(ValueError):
    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


def _require_stage(stage: Mapping[str, Any], index: int) -> dict[str, Any]:
    if not isinstance(stage, Mapping):
        raise GoalCardError("STAGE_SHAPE", f"stages[{index}] 不是对象")
    missing = sorted(STAGE_KEYS - set(stage))
    if missing:
        raise GoalCardError("STAGE_INCOMPLETE", f"stages[{index}] 缺 {missing}")
    depends = stage["depends_on"]
    if not isinstance(depends, (list, tuple)):
        raise GoalCardError("STAGE_DEPENDS", f"stages[{index}].depends_on 必须是列表")
    return dict(stage)


def stage_summary(stage: Mapping[str, Any]) -> str:
    """单 Stage 一行摘要（卡面 focus 字段用；不打乱顺序）。"""
    return (f"{stage['stage_id']}：{stage['goal']}｜预算 {stage['budget']}｜"
            f"依赖 {list(stage['depends_on']) or ['—']}｜停止 {stage['stop_rules']}")


def build_goal_card(*, title: str, stages: Sequence[Mapping[str, Any]],
                    notes: Sequence[str] = ()) -> dict[str, Any]:
    """多 Stage 目标卡（driver.gate1_hook 的输入；审核内容自解释）。"""
    if not isinstance(stages, (list, tuple)) or not stages:
        raise GoalCardError("STAGES_EMPTY", "至少一个 Stage")
    checked = [_require_stage(stage, i) for i, stage in enumerate(stages)]
    ids = [s["stage_id"] for s in checked]
    if len(set(ids)) != len(ids):
        raise GoalCardError("STAGE_DUPLICATE", f"{ids}")
    known = set(ids)
    for stage in checked:
        unknown = sorted(set(stage["depends_on"]) - known)
        if unknown:
            raise GoalCardError("STAGE_DEPENDS_UNKNOWN",
                                f"{stage['stage_id']} 依赖未登记 {unknown}")
    return {"schema": SCHEMA, "title": title,
            "stages": checked, "notes": list(notes)}


def build_gate1_card_payload(card: Mapping[str, Any]) -> dict[str, Any]:
    """把目标卡映射成 ``build_gate1_card*`` 的 (task_title, proposals)。

    单方案=整链（批准粒度=整条链）；focus 逐 Stage 摘要；artifacts 列停止条件与
    预算。返回的 dict 直接喂 ``request_gate1_approval(thread_id, title,
    proposals)``。
    """
    if not isinstance(card, Mapping) or card.get("schema") != SCHEMA:
        raise GoalCardError("CARD_SCHEMA", "不是 S1 目标卡")
    stages = card.get("stages") or []
    focus_lines = "\n".join(f"- {stage_summary(s)}" for s in stages)
    artifacts = "; ".join(f"{s['stage_id']}≤{s['budget']}" for s in stages)
    notes = card.get("notes") or []
    note_block = ("\n" + "\n".join(f"- {n}" for n in notes)) if notes else ""
    proposal = {
        "name": f"S1 三 Stage 研究链（{len(stages)} 段）",
        "sim_engine": "mujoco 3.15.0（受信 host 服务）",
        "focus": focus_lines + note_block,
        "artifacts": artifacts,
    }
    return {"task_title": card.get("title", "RSI-S1 研究链"),
            "proposals": [proposal]}
