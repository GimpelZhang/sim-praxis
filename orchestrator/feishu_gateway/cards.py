"""Gate1/Gate2 审批卡片构造与回调事件解析（详细计划 §2 Task 1，L374-395）。

两套构造器：
  build_gate1_card / build_gate2_card      —— 卡片 1.0 卡体（tag=markdown），
                                             form/textarea/form_submit 按计划契约内嵌；
  build_gate1_card_v2 / build_gate2_card_v2 —— schema 2.0 整卡（div+lark_md、2.0 表单语法）。

# 锁内适配（基础计划 §5.1 卡片版本，探针 C 2026-10-14 实测裁决）：form/
# action_type=form_submit 是卡片 2.0 组件；多行输入没有 textarea 标签，
# 服务端对 textarea 判 200621「not support tag: textarea」，正确形态是
# tag=input + input_type=multiline_text（实测渲染为多行高框）。
# 1.0 混排 form 时发送接口【静默 code=0】，但飞书客户端只渲染 markdown 文字、
# form 控件整块消失（1.0 卡实测 schema 接受但无意见框/按钮）。故 client 对含
# 表单的 Gate1/Gate2 卡 2.0 先发、被拒再退 1.0；无表单的提醒卡仍 1.0 优先。
# 任何情况下通道② P2P 文本不依赖卡片版本，意见通道始终可用。
"""
from __future__ import annotations

from typing import Any, Optional

FEEDBACK_PLACEHOLDER = "打回时必填：具体意见（会原样带回方案重做）"
REJECT_EMPTY_TOAST = {"type": "error", "content": "打回必须填写意见"}
NOT_OWNER_TOAST = {"type": "error", "content": "非审批人操作，已拒绝"}


# ---------------- 卡片 1.0 ----------------

def _gate1_form_1(i: int) -> dict:
    """每方案一个 form：feedback textarea + 批准方案 i + 危险打回（均 form_submit）。"""
    return {
        "tag": "form",
        "name": f"gate1_form_{i}",
        "elements": [
            {"tag": "textarea", "name": "feedback",
             "placeholder": {"tag": "plain_text", "content": FEEDBACK_PLACEHOLDER},
             "max_length": 1000, "required": False},
            {"tag": "action", "actions": [
                {"tag": "button", "name": "approve",
                 "text": {"tag": "plain_text", "content": f"批准方案 {i + 1}"},
                 "type": "primary" if i == 0 else "default",
                 "action_type": "form_submit",
                 "value": {"gate": 1, "decision": "approve", "choice": i}},
                {"tag": "button", "name": "reject",
                 "text": {"tag": "plain_text", "content": "打回并提出意见"},
                 "type": "danger", "action_type": "form_submit",
                 "value": {"gate": 1, "decision": "reject_with_comments"}},
            ]},
        ],
    }


def build_gate1_card(
    task_title: str,
    proposals: list[dict[str, Any]],
    feedback_required: bool = True,
) -> dict:
    """Gate1 1.0 卡：任务标题 + 候选方案 markdown 块 + 每方案 form（含意见框）。

    proposals: [{"name": str, "sim_engine"/"simulator"?: str,
                 "focus"/"first_principles"?: str, "artifacts"?: str, ...}]
    feedback_required 仅影响提示文案；服务端 form 不能强制 textarea 必填，
    后端二次校验在 client 回调内（空意见 reject 不落决策）。
    """
    elements: list[dict] = [
        {"tag": "markdown",
         "content": f"**任务目标**：{task_title}\n请在下方选择批准的方案；"
                    f"打回请在意见框填写具体意见（必填，会原样带回重做）。"}
    ]
    for idx, p in enumerate(proposals):
        engine = p.get("sim_engine") or p.get("simulator") or "-"
        focus = p.get("first_principles") or p.get("focus") or "-"
        artifacts = _format_artifacts(p.get("expected_artifacts")
                                       or p.get("artifacts"))
        elements.append({
            "tag": "markdown",
            "content": f"**方案 {idx + 1}：{p.get('name', '-')}**\n"
                       f"- 物理基座：`{engine}`\n"
                       f"- 思路/重点：{focus}\n"
                       f"- 预期产出：{artifacts}",
        })
        elements.append(_gate1_form_1(idx))
    if not feedback_required:
        # 契约默认必填；保留形参以贴合计划签名，非必填时仅改提示不改变校验接线。
        elements.insert(1, {"tag": "markdown",
                            "content": "_本轮反馈意见为选填（测试模式）_"})
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": "blue",
                   "title": {"tag": "plain_text",
                             "content": "【SimPraxis】Gate 1：研究方案审批"}},
        "elements": elements,
    }


def _tick(ok: Any) -> str:
    return "✅" if bool(ok) else "❌"


def _yn(ok: Any) -> str:
    return "是" if bool(ok) else "否"


def _rungs_metrics_md(metrics: dict[str, Any]) -> "str | None":
    """Task5/7 嵌套 metrics（metrics['rungs'].rungN.pass/checks）的自解释渲染。

    真人 Gate2 打回逐字要求（2026-10-15）：「指标不能全是 -」。这里只读真实
    判定结果，不出任何指标字面量；schema 失败时 rungs 只有 pass，按结构判负展示。
    无嵌套 rungs 的旧式扁平 metrics 返回 None，由调用方走扁平兜底。
    """
    rungs = metrics.get("rungs")
    if not isinstance(rungs, dict):
        return None
    engine = metrics.get("engine") or "-"
    run_id = metrics.get("run_id") or "-"
    lines = [f"**Rung 1–4 验收指标**（引擎 `{engine}`；run `{run_id}`）"]

    errs = metrics.get("schema_errors") or []
    vector = metrics.get("vector") if isinstance(metrics.get("vector"), dict) else {}
    if errs:
        lines.append("")
        lines.append("🚧 telemetry 结构校验未通过，Rung1–3 一律保守判负：")
        for e in list(errs)[:8]:
            lines.append(f"  - {e}")
        lines.append("")
        lines.append("Rung1–3：" + " / ".join(
            f"R{i} {_tick(vector.get(f'rung{i}'))}" for i in (1, 2, 3)))
        return "\n".join(lines)

    r1 = rungs.get("rung1") if isinstance(rungs.get("rung1"), dict) else {}
    c1 = r1.get("checks") if isinstance(r1.get("checks"), dict) else {}
    if c1:
        lines += [
            "",
            f"{_tick(r1.get('pass'))} **Rung1 数值合法 / 穿模 / 关节力矩**",
            f"  - 最大穿模 `{c1.get('max_penetration_m')}` m，阈值 "
            f"`{c1.get('penetration_limit_m')}` m："
            f"{_tick(c1.get('penetration_within_limit'))}",
            f"  - 全部数值有限（无 NaN/Inf）：{_tick(c1.get('all_finite'))}",
            f"  - 关节力矩在限位内：{_tick(c1.get('joint_torque_within_limits'))}",
            f"  - 穿模检测口径：{c1.get('ccd_method', '-')}",
        ]
    else:
        lines += ["", f"{_tick(r1.get('pass'))} **Rung1**（无明细）"]

    r2 = rungs.get("rung2") if isinstance(rungs.get("rung2"), dict) else {}
    c2 = r2.get("checks") if isinstance(r2.get("checks"), dict) else {}
    if c2:
        lines += [
            "",
            f"{_tick(r2.get('pass'))} **Rung2 物体是否真进目标区 + 稳定接触**"
            "（只按位姿时序算，不读 telemetry 自带的 goal_reached）",
            f"  - 在目标盒内共 {c2.get('inside_box_frames')} 帧；末 "
            f"{c2.get('last_window_frames')} 帧全程在盒："
            f"{_tick(c2.get('last_window_all_inside_box'))}",
            f"  - 最长连续接触 {c2.get('contact_longest_run_s')} s / 要求 ≥"
            f"{c2.get('duration_required_s')} s；接触力均值 "
            f"{c2.get('contact_force_mean_n')} N",
            f"  - 接触力波动 CV {c2.get('contact_cv')} / 上限 {c2.get('cv_max')}"
            f"；稳定接触：{_tick(c2.get('stable_contact'))}",
        ]
    else:
        lines += ["", f"{_tick(r2.get('pass'))} **Rung2**（无明细）"]

    r3 = rungs.get("rung3") if isinstance(rungs.get("rung3"), dict) else {}
    c3 = r3.get("checks") if isinstance(r3.get("checks"), dict) else {}
    if c3 and c3.get("dt_band_ok"):
        lines += [
            "",
            f"{_tick(r3.get('pass'))} **Rung3 末端运动平顺度（jerk 三阶差分）**",
            f"  - 帧 dt={c3.get('dt')} s；jerk 峰值 "
            f"`{c3.get('jerk_max_mps3')}` m/s³，随 dt 锚定阈值 "
            f"`{c3.get('jerk_threshold_mps3')}` m/s³",
            f"  - 超阈帧数 {c3.get('over_threshold_count')}：{_tick(r3.get('pass'))}",
        ]
    elif c3:
        lines += ["", f"{_tick(r3.get('pass'))} **Rung3**："
                  f"dt={c3.get('dt')} 超出锚定带，保守判负（不出 jerk 值）"]
    else:
        lines += ["", f"{_tick(r3.get('pass'))} **Rung3**（无明细）"]

    r4 = rungs.get("rung4")
    if not isinstance(r4, dict):
        r4 = metrics.get("rung4") if isinstance(metrics.get("rung4"), dict) else None
    lines.append("")
    if isinstance(r4, dict):
        lines.append(f"**Rung4 视觉模型独立核片**（mode={r4.get('mode')}，"
                     "只给意见，永不单独放行）")
        status, mode = r4.get("status"), r4.get("mode")
        items = r4.get("items") if isinstance(r4.get("items"), list) else []
        if status == "uncalibrated" or mode == "uncalibrated":
            lines.append("  - 未标定：本次未发视觉判定（零网络），"
                         "以 Rung1–3 规则 + 你本人看片为准")
        elif status == "vlm_error":
            lines.append("  - ⚠️ 视觉模型调用失败（不伪造成通过），以你本人看片为准")
        else:
            n_clean = sum(1 for it in items if not it.get("defect"))
            lines.append(
                f"  - {len(items)} 道原子题：{n_clean}/{len(items)} 未见缺陷，"
                f"报缺陷 {r4.get('n_defects')}；低置信：{_yn(r4.get('low_confidence'))}")
            if mode == "record_only":
                lines.append("  - 当前为 record_only 标定模式：VLM 结果仅展示留痕，"
                             "不参与任何自动放行，本 Gate2 完全由你裁决")
            if r4.get("disagreement"):
                lines.append(f"  - ⚠️ 规则/视觉分歧（{r4.get('disagreement')}）："
                             "以 Rung1–3 规则为准，由你裁决（D8）")
    else:
        lines.append("**Rung4 视觉模型独立核片**：本次无结果")

    rules_pass = metrics.get("rules_pass")
    lines += [
        "",
        f"**Rung1–3 规则汇总：{_tick(rules_pass)}**"
        f"（R1 {_tick(vector.get('rung1'))} / R2 {_tick(vector.get('rung2'))} "
        f"/ R3 {_tick(vector.get('rung3'))}）。自动放行只看 Rung1–3；"
        "Rung4 仅提供意见，任何情况都不单独放行。",
    ]
    banner = metrics.get("banner")
    if banner:
        lines.append(f"⚠️ {banner}")
    return "\n".join(lines)


# 真人 Gate2 打回（2026-10-15）：卡片顶部必须自解释任务背景与视频含义。
# 三个键分别回答审批人的三个问题；缺省键不渲染。
_EXPLAIN_LABELS = (
    ("task", "这个仿真在做什么"),
    ("problem", "解决 / 验证什么问题"),
    ("video", "发来的视频怎么看"),
)


def _explanation_md(explanation: "dict[str, str] | None") -> "str | None":
    if not isinstance(explanation, dict):
        return None
    blocks: list[str] = []
    for key, label in _EXPLAIN_LABELS:
        val = explanation.get(key)
        if isinstance(val, str) and val.strip():
            blocks.append(f"**{label}**\n{val.strip()}")
    if not blocks:
        return None
    return ("📋 **审批前请先读：任务背景与视频解读（真人 Gate2 要求补充）**\n\n"
            + "\n\n".join(blocks))


def _gate2_metrics_md(
    task_title: str,
    metrics: dict[str, Any],
    intranet_url: "str | None",
    video_fallback: bool = False,
) -> str:
    lines = [f"**任务目标**：{task_title}"]
    nested = _rungs_metrics_md(metrics)
    if nested is not None:
        lines.append(nested)
    else:
        # 旧式扁平 metrics 兜底（Task3/合成 Gate2 测试仍用这组键）。
        lines += [
            "**Rung 1-4 验收指标**：",
            f"- 位姿误差：{metrics.get('pose_error', '-')}",
            f"- 最大接触深度：{metrics.get('max_contact_depth', '-')}",
            f"- 接触持续：{metrics.get('contact_duration', '-')}",
            f"- jerk 平顺度：{metrics.get('jerk', '-')}"]
        rung4 = metrics.get("rung4_atomic")
        if rung4 is not None:
            lines.append(f"- Rung4 原子题结果：{rung4}")
    if video_fallback:
        lines.append("")
        lines.append("⚠️ 原生视频上传失败，已降级（video_fallback=true），"
                     "请通过下方内网链接/证据目录查看原片后裁决。")
    if intranet_url:
        # 仅内网/headless 可达地址，不放任何公网地址（本机无公网 IP）。
        lines.append("")
        lines.append(f"内网视频/证据链接（headless）：{intranet_url}")
    return "\n".join(lines)


def _gate2_form_1() -> dict:
    return {
        "tag": "form",
        "name": "gate2_form",
        "elements": [
            {"tag": "textarea", "name": "feedback",
             "placeholder": {"tag": "plain_text", "content": FEEDBACK_PLACEHOLDER},
             "max_length": 1000, "required": False},
            {"tag": "action", "actions": [
                {"tag": "button", "name": "approve",
                 "text": {"tag": "plain_text", "content": "验收通过"},
                 "type": "primary", "action_type": "form_submit",
                 "value": {"gate": 2, "decision": "approve",
                           "video_fallback": False}},
                {"tag": "button", "name": "reject",
                 "text": {"tag": "plain_text", "content": "打回并提出意见"},
                 "type": "danger", "action_type": "form_submit",
                 "value": {"gate": 2, "decision": "reject_with_comments",
                           "video_fallback": False}},
            ]},
        ],
    }


def build_gate2_card(
    task_title: str,
    metrics: dict[str, Any],
    intranet_url: "str | None" = None,
    *,
    video_fallback: bool = False,
    explanation: "dict[str, str] | None" = None,
) -> dict:
    """Gate2 1.0 卡：（可选）任务背景说明 + 指标 markdown（含内网链接）
    + form（验收通过/打回并提出意见）。"""
    elements: list[dict] = []
    explain_md = _explanation_md(explanation)
    if explain_md:
        elements.append({"tag": "markdown", "content": explain_md})
        elements.append({"tag": "hr"})
    elements.append({"tag": "markdown",
                     "content": _gate2_metrics_md(task_title, metrics, intranet_url,
                                                  video_fallback)})
    form = _gate2_form_1()
    if video_fallback:
        for btn in form["elements"][1]["actions"]:
            btn["value"] = dict(btn["value"], video_fallback=True)
    elements.append(form)
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": "green",
                   "title": {"tag": "plain_text",
                             "content": "【SimPraxis】Gate 2：仿真结果验收"}},
        "elements": elements,
    }


# ---------------- 卡片 2.0（schema 2.0 整卡） ----------------

def _v2_header(template: str, title: str) -> dict:
    return {"title": {"tag": "plain_text", "content": title},
            "template": template}


def _div_lark_md(content: str) -> dict:
    return {"tag": "div", "text": {"tag": "lark_md", "content": content}}


def _format_artifacts(value) -> str:
    """Task2 起 expected_artifacts 规范为文件名数组；兼容旧字符串形态。"""
    if isinstance(value, list):
        items = [str(v).strip() for v in value if str(v).strip()]
        return "、".join(items) if items else "-"
    if isinstance(value, str) and value.strip():
        return value.strip()
    return "-"


def _gate1_form_2(i: int) -> dict:
    # 2.0 表单语法：form.elements 内直接放多行输入框与 form_submit 按钮。
    # 锁内适配（探针 C 2026-10-14 实测，服务端码 200621）：多行输入没有
    # textarea 标签（"not support tag: textarea"），正确形态是
    # tag=input + input_type=multiline_text（已实测渲染为多行高框）。
    # 锁内适配（服务端码 11310）：卡片内所有控件 name 必须全卡唯一，
    # 多方案各 form 的输入框/按钮按方案索引命名；提交时 form_value 形如
    # {"feedback_<i>": "..."}，由 parse_card_event 按 value.choice 取键。
    return {
        "tag": "form",
        "name": f"gate1_form_{i}",
        "elements": [
            {"tag": "input", "name": f"feedback_{i}",
             "input_type": "multiline_text",
             "placeholder": {"tag": "plain_text", "content": FEEDBACK_PLACEHOLDER},
             "max_length": 1000, "required": False},
            {"tag": "button", "name": f"approve_{i}",
             "text": {"tag": "plain_text", "content": f"批准方案 {i + 1}"},
             "type": "primary" if i == 0 else "default",
             "action_type": "form_submit",
             "value": {"gate": 1, "decision": "approve", "choice": i}},
            {"tag": "button", "name": f"reject_{i}",
             "text": {"tag": "plain_text", "content": "打回并提出意见"},
             "type": "danger", "action_type": "form_submit",
             "value": {"gate": 1, "decision": "reject_with_comments", "choice": i}},
        ],
    }


def build_gate1_card_v2(
    task_title: str,
    proposals: list[dict[str, Any]],
    feedback_required: bool = True,
) -> dict:
    """Gate1 schema 2.0 卡（1.0 内嵌 form 被服务端拒绝时的自动降级形态）。"""
    body: list[dict] = [
        _div_lark_md(f"**任务目标**：{task_title}\n请在下方选择批准的方案；"
                     f"打回请在意见框填写具体意见（必填，会原样带回重做）。")
    ]
    if not feedback_required:
        body.append(_div_lark_md("_本轮反馈意见为选填（测试模式）_"))
    for idx, p in enumerate(proposals):
        engine = p.get("sim_engine") or p.get("simulator") or "-"
        focus = p.get("first_principles") or p.get("focus") or "-"
        artifacts = _format_artifacts(p.get("expected_artifacts")
                                       or p.get("artifacts"))
        body.append(_div_lark_md(
            f"**方案 {idx + 1}：{p.get('name', '-')}**\n"
            f"- 物理基座：`{engine}`\n- 思路/重点：{focus}\n"
            f"- 预期产出：{artifacts}"))
        body.append(_gate1_form_2(idx))
    return {
        "schema": "2.0",
        "config": {"update_multi": True, "wide_screen_mode": True},
        "header": _v2_header("blue", "【SimPraxis】Gate 1：研究方案审批"),
        "body": {"elements": body},
    }


def _gate2_form_2(video_fallback: bool) -> dict:
    def _val(decision: str) -> dict:
        return {"gate": 2, "decision": decision, "video_fallback": video_fallback}

    return {
        "tag": "form",
        "name": "gate2_form",
        "elements": [
            # 锁内适配（探针 C 2026-10-14）：多行输入用 input+multiline_text，
            # textarea 标签被服务端判 200621 不支持。
            {"tag": "input", "name": "feedback", "input_type": "multiline_text",
             "placeholder": {"tag": "plain_text", "content": FEEDBACK_PLACEHOLDER},
             "max_length": 1000, "required": False},
            {"tag": "button", "name": "approve",
             "text": {"tag": "plain_text", "content": "验收通过"},
             "type": "primary", "action_type": "form_submit",
             "value": _val("approve")},
            {"tag": "button", "name": "reject",
             "text": {"tag": "plain_text", "content": "打回并提出意见"},
             "type": "danger", "action_type": "form_submit",
             "value": _val("reject_with_comments")},
        ],
    }


def build_gate2_card_v2(
    task_title: str,
    metrics: dict[str, Any],
    intranet_url: "str | None" = None,
    *,
    video_fallback: bool = False,
    explanation: "dict[str, str] | None" = None,
) -> dict:
    body: list[dict] = []
    explain_md = _explanation_md(explanation)
    if explain_md:
        body.append(_div_lark_md(explain_md))
        body.append({"tag": "hr"})
    body.append(_div_lark_md(_gate2_metrics_md(
        task_title, metrics, intranet_url, video_fallback)))
    body.append(_gate2_form_2(video_fallback))
    return {
        "schema": "2.0",
        "config": {"update_multi": True, "wide_screen_mode": True},
        "header": _v2_header("green", "【SimPraxis】Gate 2：仿真结果验收"),
        "body": {"elements": body},
    }


# ---------------- 等待提醒卡片（8h/16h，仅提醒绝不代决） ----------------

def build_reminder_card(gate: int, nth: int, waited_hours: int) -> dict:
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": "orange",
                   "title": {"tag": "plain_text",
                             "content": f"【SimPraxis】Gate{gate} 审批等待提醒 {nth}/2"}},
        "elements": [
            {"tag": "markdown",
             "content": f"Gate{gate} 已等待约 {waited_hours} 小时，仍在等待**人工**结论。\n"
                        f"系统不会代为决定；请在原审批卡片上操作（批准/打回并提出意见）。"}
        ],
    }


def build_reminder_card_v2(gate: int, nth: int, waited_hours: int) -> dict:
    return {
        "schema": "2.0",
        "config": {"update_multi": True, "wide_screen_mode": True},
        "header": _v2_header("orange",
                             f"【SimPraxis】Gate{gate} 审批等待提醒 {nth}/2"),
        "body": {"elements": [_div_lark_md(
            f"Gate{gate} 已等待约 {waited_hours} 小时，仍在等待**人工**结论。\n"
            f"系统不会代为决定；请在原审批卡片上操作（批准/打回并提出意见）。")]},
    }


# ---------------- 回调事件解析（纯函数） ----------------

def parse_card_event(event: Any) -> "tuple[dict, str, Optional[str], Optional[str], Optional[str]]":
    """从 P2CardActionTrigger 提取五元组（鸭子类型，合成事件对象也可用于单测）。

    返回：(value, feedback, operator_open_id, open_chat_id, open_message_id)
    已核实 1.7.4 源码字段：
      event.action.value/form_value（CallBackAction，event/callback/model/
      p2_card_action_trigger.py L39-58）、event.operator.open_id（CallBackOperator）、
      event.context.open_chat_id/open_message_id（CallBackContext）。
    feedback 原样返回（不 strip，落库保留人工原文；空值校验由调用方 strip 判断）。
    """
    data = event.event
    action = getattr(data, "action", None)
    value = dict(getattr(action, "value", None) or {})
    form_value = getattr(action, "form_value", None) or {}
    # Gate2 单 form 键名 feedback；Gate1 多方案 form 按 11310 适配改为
    # feedback_<choice>（见 _gate1_form_2）；其余形态回退取唯一字符串值。
    feedback = form_value.get("feedback")
    if not isinstance(feedback, str):
        choice = value.get("choice")
        if isinstance(choice, int):
            cand = form_value.get(f"feedback_{choice}")
            if isinstance(cand, str):
                feedback = cand
    if not isinstance(feedback, str):
        # 兼容形态：取其中唯一的字符串控件值（按钮 value 不进 form_value）
        str_values = [v for v in form_value.values() if isinstance(v, str)]
        feedback = str_values[0] if str_values else ""
    operator = getattr(data, "operator", None)
    context = getattr(data, "context", None)
    return (
        value,
        feedback,
        getattr(operator, "open_id", None),
        getattr(context, "open_chat_id", None),
        getattr(context, "open_message_id", None),
    )


def parse_message_event(event: Any) -> "dict[str, Any]":
    """从 P2ImMessageReceiveV1 提取 {chat_id, chat_type, message_type, content_raw,
    text, sender_open_id, message_id, create_time_ms}（鸭子类型）。"""
    data = event.event
    msg = getattr(data, "message", None)
    sender = getattr(data, "sender", None)
    sender_id = getattr(sender, "sender_id", None)
    content_raw = getattr(msg, "content", None) or ""
    text = ""
    try:
        import json
        parsed = json.loads(content_raw)
        if isinstance(parsed, dict):
            text = str(parsed.get("text", ""))
    except (ValueError, TypeError):
        text = ""
    return {
        "chat_id": getattr(msg, "chat_id", None),
        "chat_type": getattr(msg, "chat_type", None),
        "message_type": getattr(msg, "message_type", None),
        "content_raw": content_raw,
        "text": text,
        "sender_open_id": getattr(sender_id, "open_id", None),
        "message_id": getattr(msg, "message_id", None),
        "create_time_ms": getattr(msg, "create_time", None),
    }
