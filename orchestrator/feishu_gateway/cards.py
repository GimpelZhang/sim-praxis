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

import math
import re
from typing import Any, Optional

from orchestrator.judge.pareto import (
    DOMINATED_BY_V1,
    METRIC_DIRECTIONS,
    METRIC_EPSILON,
    NON_DOMINATED,
    V2_DOMINATES,
)

FEEDBACK_PLACEHOLDER = "打回时必填：具体意见（会原样带回方案重做）"
REJECT_EMPTY_TOAST = {"type": "error", "content": "打回必须填写意见"}
# Task6 M2：ascent Gate1/Gate2 批准必须带真人原文（演化志 opinion fail-loud
# 硬要求）；require_approval_notes=True 时空批准与空打回同等待遇——只 toast、
# 不落决策、行保持 PENDING，避免「空批准落 DECIDED 后 opinion 永久抛错」楔死。
APPROVAL_EMPTY_TOAST = {"type": "error",
                        "content": "批准也必须填写意见（演化志需真人原文）"}
NOT_OWNER_TOAST = {"type": "error", "content": "非审批人操作，已拒绝"}
# H-8：卡片按钮 value.gate 与落库行 gate 不一致时的拒绝 toast。
GATE_MISMATCH_TOAST = {"type": "error",
                       "content": "按钮与审批单不匹配（Gate 不一致），已拒绝"}


# ---------------- H-7：lark_md 宏模型字段转义 ----------------
# 信任边界：task_title / 方案 name/focus/artifacts / explanation 文本来自 macro
# LLM 产出，若原样进 lark_md，可被注入 [批准](https://evil.example) 假链接、
# **加粗**伪系统行等。仅对这些宏字段转义；人类/常量文本（我们自己的标签、
# 数值渲染器产出）不转义。
_MD_ESCAPE_CHARS = ("\\", "`", "*", "_", "~", "[", "]", "(", ")", ">", "#")
# S-4：行首有序列表标记（"1. "）的句点位置；行首竖线（表格引用/管道伪行）。
_OL_RE = re.compile(r"(?m)^(?P<num>\d+)(?P<dot>\. )")


def md_escape(text: Any) -> str:
    """把宏模型产出文本转成 lark_md 安全字面量（markdown 结构字符全部反斜杠化）。

    行首的列表/引用/表格标记（-、+、|、#、>、``1. ``）也转义，防止伪造系统
    条目或伪表格；非字符串强转。
    """
    s = str(text)
    # S-4：记录每个行首 "数字. " 的句点位置，逐字符循环时反斜杠化。
    ol_dots = {m.start("dot") for m in _OL_RE.finditer(s)}
    out: list[str] = []
    for i, ch in enumerate(s):
        if ch in _MD_ESCAPE_CHARS:
            out.append("\\" + ch)
        elif ch in ("-", "+", "|") and (i == 0 or s[i - 1] == "\n"):
            out.append("\\" + ch)
        elif i in ol_dots:
            out.append("\\.")
        else:
            out.append(ch)
    return "".join(out)


def inline_code(text: Any) -> str:
    """安全的行内代码字面量：反斜杠/反引号替换掉，保证 code span 不可被截断。"""
    s = str(text).replace("\\", "/").replace("`", "'")
    return f"`{s}`"


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
         "content": f"**任务目标**：{md_escape(task_title)}\n请在下方选择批准的方案；"
                    f"打回请在意见框填写具体意见（必填，会原样带回重做）。"}
    ]
    for idx, p in enumerate(proposals):
        engine = p.get("sim_engine") or p.get("simulator") or "-"
        focus = p.get("first_principles") or p.get("focus") or "-"
        artifacts = _format_artifacts(p.get("expected_artifacts")
                                       or p.get("artifacts"))
        elements.append({
            "tag": "markdown",
            # H-7：name/engine/focus/artifacts 均为宏模型产出，逐个转义。
            "content": f"**方案 {idx + 1}：{md_escape(p.get('name', '-'))}**\n"
                       f"- 物理基座：{inline_code(engine)}\n"
                       f"- 思路/重点：{md_escape(focus)}\n"
                       f"- 预期产出：{md_escape(artifacts)}",
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


def _task8_batch_metrics_md(metrics: dict[str, Any]) -> str:
    """Task8 Track2 Genesis DR 对照 batch（三臂×3 种子）的卡片渲染。

    与 Task7 单 rollout 完全不同的语义：上半屏判证据可信度（Rung1–3 文件级
    三级判定 + 两处分屏 Rung4），下半屏原样展示组对照与预注册假设状态。
    L571：矛盾/不可分辨本身是结论，卡片不设「实验组必须更好」的褒贬措辞。
    """
    rungs = metrics.get("rungs") if isinstance(metrics.get("rungs"), dict) else {}
    run_id = metrics.get("run_id") or "-"
    lines = [f"**Rung 1–4 验收指标 · Task8 DR 对照 batch**（run `{run_id}`，"
             f"引擎 `{metrics.get('engine', '-')}`）"]

    errs = metrics.get("schema_errors") or []
    vector = metrics.get("vector") if isinstance(metrics.get("vector"), dict) else {}
    if errs:
        lines.append("")
        lines.append("🚧 batch 结构校验未通过，Rung1–3 一律保守判负：")
        for e in list(errs)[:8]:
            lines.append(f"  - {e}")
        lines.append("")
        lines.append("Rung1–3：" + " / ".join(
            f"R{i} {_tick(vector.get(f'rung{i}'))}" for i in (1, 2, 3)))
        return "\n".join(lines)

    r1 = rungs.get("rung1") if isinstance(rungs.get("rung1"), dict) else {}
    c1 = r1.get("checks") if isinstance(r1.get("checks"), dict) else {}
    cal = c1.get("calibration") if isinstance(c1.get("calibration"), dict) else {}
    sel = cal.get("selected_window") if isinstance(cal.get("selected_window"), dict) else {}
    if c1:
        lines += [
            "",
            f"{_tick(r1.get('pass'))} **Rung1 产物齐套 / 全部有限 / 标定件**",
            f"  - 九 run telemetry 齐套：{_tick(c1.get('artifacts_complete'))}"
            f"（n_runs={c1.get('n_runs')}，seeds={c1.get('seeds')}）；缺件："
            f"{c1.get('missing_artifacts') or '无'}",
            f"  - series/outcomes 全部数值有限：{_tick(c1.get('all_finite'))}",
            f"  - 推力选窗标定件 v2：{sel.get('push_n_N', '-')} N × "
            f"{sel.get('push_dur_s', '-')} s；跨进程逐位复验 "
            f"all_bit_exact={_tick(cal.get('all_bit_exact'))}",
        ]
    else:
        lines += ["", f"{_tick(r1.get('pass'))} **Rung1**（无明细）"]

    r2 = rungs.get("rung2") if isinstance(rungs.get("rung2"), dict) else {}
    c2 = r2.get("checks") if isinstance(r2.get("checks"), dict) else {}
    if c2:
        diffs = c2.get("disagreements") or []
        lines += [
            "",
            f"{_tick(r2.get('pass'))} **Rung2 独立重算对质**（不信脚本自报："
            "从 50 Hz series 重算跌倒帧/滑移/峰速/接触力，对质 outcomes 与 CSV）",
            f"  - 重算分歧 run 数：{len(diffs)}"
            + ("" if not diffs else f"（{[d.get('run_id') for d in diffs][:4]}）"),
        ]
    else:
        lines += ["", f"{_tick(r2.get('pass'))} **Rung2**（无明细）"]

    r3 = rungs.get("rung3") if isinstance(rungs.get("rung3"), dict) else {}
    c3 = r3.get("checks") if isinstance(r3.get("checks"), dict) else {}
    if c3:
        violations = c3.get("violations") or []
        lines += [
            "",
            f"{_tick(r3.get('pass'))} **Rung3 物理界 / 扰动交付 / 时间轴**",
            f"  - 滑移速度界 {c3.get('slip_v_bound_mps')} m/s；接触力界 "
            f"{c3.get('contact_force_bound_N')} N（DR Kp 包络另有 20 kN 先验界）",
            f"  - 时间轴等距 dt={c3.get('timeline_regular_dt_s')} s；扰动交付："
            f"{c3.get('disturbance')}",
            f"  - 违规 {len(violations)} 条："
            + ("无" if not violations else f"{violations[:3]}"),
        ]
    else:
        lines += ["", f"{_tick(r3.get('pass'))} **Rung3**（无明细）"]

    # ---- 组对照（科学结论段；原样展示，不做褒贬）----
    groups = metrics.get("groups") if isinstance(metrics.get("groups"), dict) else {}
    contrast = metrics.get("contrast") if isinstance(metrics.get("contrast"), dict) else {}
    if groups:
        name_zh = {"normal": "normal 正常地面 μ=0.7（对照）",
                   "ice_nominal": "ice_nominal 冰面 μ=0.05",
                   "ice_dr": "ice_dr DR 包络"}
        lines += ["", "**三臂组统计（每臂 3 种子；跌倒帧 null=存活）**"]
        for arm in ("normal", "ice_nominal", "ice_dr"):
            g = groups.get(arm)
            if not isinstance(g, dict):
                continue
            lines.append(
                f"  - {name_zh.get(arm, arm)}：成功率 "
                f"{g.get('success_rate')}（跌倒 {g.get('n_fell')}/{g.get('n')}），"
                f"跌倒帧 {g.get('fall_frames')}；推力后峰速 max/均值 "
                f"{g.get('postpush_vmax_max_mps')}/{g.get('postpush_vmax_mean_mps')} "
                f"m/s；滑移距离均值 {g.get('slip_dist_mean_m')} m；"
                f"最大倾角 {g.get('tilt_max_deg_max')}°")
            if arm == "ice_dr" and g.get("mu_per_seed") is not None:
                lines.append(f"    - 各籽实际 μ：{g.get('mu_per_seed')}")
    if contrast:
        dr = contrast.get("dr_vs_nominal") if isinstance(
            contrast.get("dr_vs_nominal"), dict) else {}
        lines += [
            "",
            "**预注册对照（判据写于正式 batch 之前）**",
            f"  - 对照组三籽全部存活（control_stands）："
            f"{_yn(contrast.get('control_stands'))}",
            f"  - 冰/干 推力后峰速组均比 "
            f"{contrast.get('ice_over_normal_postpush_ratio')}，预注册界 ≥"
            f"{contrast.get('preregistered_ratio')}（ordered_slip）："
            f"{_yn(contrast.get('ordered_slip'))}",
            f"  - DR vs 名义冰面：跌倒更少 {_yn(dr.get('fewer_falls'))}，"
            f"峰速更低 {_yn(dr.get('lower_postpush_slip'))}，"
            f"方向 **{dr.get('direction', '-')}**",
            f"  - 假设状态 **{contrast.get('hypothesis_status', '-')}**",
            f"  - L571：{contrast.get('l571_note', '')}",
        ]

    dr_env = metrics.get("dr_envelope") if isinstance(
        metrics.get("dr_envelope"), dict) else {}
    if dr_env:
        lines.append("")
        lines.append("DR 包络（摘要）：" + "；".join(
            f"{k}={v}" for k, v in list(dr_env.items())[:6]))
    disturbance = metrics.get("disturbance") if isinstance(
        metrics.get("disturbance"), dict) else {}
    if disturbance:
        lines.append("扰动参数（摘要）：" + "；".join(
            f"{k}={v}" for k, v in list(disturbance.items())[:6]))

    # ---- Rung4：两张对照分屏各一次六题核片 ----
    r4 = rungs.get("rung4")
    if not isinstance(r4, dict):
        r4 = metrics.get("rung4") if isinstance(metrics.get("rung4"), dict) else None
    lines.append("")
    if isinstance(r4, dict):
        lines.append(f"**Rung4 视觉模型独立核片**（两张对照分屏各 6 题；"
                     f"mode={r4.get('mode')}，只给意见，永不单独放行）")
        status, mode = r4.get("status"), r4.get("mode")
        if status == "uncalibrated" or mode == "uncalibrated":
            lines.append("  - 未标定：本次未发视觉判定（零网络），"
                         "以 Rung1–3 规则 + 你本人看分屏为准")
        elif status == "vlm_error":
            lines.append("  - ⚠️ 视觉模型调用失败（不伪造成通过），以你本人看片为准")
        else:
            n_clean = (r4.get("n_items", 0) or 0) - (r4.get("n_defects", 0) or 0)
            lines.append(
                f"  - 两分屏共 {r4.get('n_items')} 道原子题：{n_clean} 未见缺陷，"
                f"报缺陷 {r4.get('n_defects')}；低置信：{_yn(r4.get('low_confidence'))}")
            for sp in r4.get("splits", []):
                lines.append(
                    f"    - `{sp.get('split')}`：status={sp.get('status')}，"
                    f"缺陷 {sp.get('n_defects')}，低置信 {_yn(sp.get('low_confidence'))}")
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
        f"**Rung1–3 证据可信度汇总：{_tick(rules_pass)}**"
        f"（R1 {_tick(vector.get('rung1'))} / R2 {_tick(vector.get('rung2'))} "
        f"/ R3 {_tick(vector.get('rung3'))}）。Rung1–3 只回答证据是否可信；"
        "DR 是否改善由上方 contrast 原样呈现，矛盾按 L571 由你裁决。",
    ]
    artifacts = metrics.get("artifacts") if isinstance(
        metrics.get("artifacts"), dict) else {}
    if artifacts:
        wd = metrics.get("workdir")
        lines.append(f"证据目录：{wd or '（batch workdir）'}；CSV "
                     f"`{artifacts.get('csv')}`、summary `{artifacts.get('summary')}`"
                     f"、拼图 `{artifacts.get('collage')}`")
    banner = metrics.get("banner")
    if banner:
        lines.append(f"⚠️ {banner}")
    return "\n".join(lines)


def _q2_report_metrics_md(metrics: dict[str, Any]) -> str:
    """Task8 Q2（Carla 固态 LiDAR 雨雪雾）Gate2 渲染：outcome/mechanism 双结论。

    Q2 走计划 D9 条件闸门（不经图、无 Rung1-4 telemetry），2026-06 月补真人
    点验卡片时新增。所有数字由调用方 scripts/milestone1/q2_human_gate.py 从
    invariance_checks.json / control/analysis/invariance_checks.json /
    optics_sweep.csv 实读传入，模板内不写任何指标字面量。
    """
    lines = ["**Q2 实验结论（outcome 与 mechanism 分开报告，矛盾不调参，L571）**"]
    oc = metrics.get("outcome")
    if isinstance(oc, dict):
        lines += ["", "**① Outcome：Carla 0.10.0 原生 LiDAR 无天气物理通道**"]
        geo = oc.get("bit_identical") if isinstance(oc.get("bit_identical"), dict) else {}
        lines.append(
            f"  - 语义几何射线 {geo.get('comparisons', '-')} 组对照逐 bit 相等"
            f"（失败 {geo.get('failures', '-')}）；跨进程重复 "
            f"{geo.get('cross_repeat', '-')} 组同样逐 bit 相等")
        z = oc.get("z_vs_clear") if isinstance(oc.get("z_vs_clear"), dict) else {}
        zc = (oc.get("z_negative_control")
              if isinstance(oc.get("z_negative_control"), dict) else {})
        if z:
            line = ("  - ray_cast 点数跨天气 z="
                    f"rain {z.get('rain_hard', '-'):.2f} / "
                    f"fog {z.get('fog_dense', '-'):.2f}")
            if zc:
                line += ("；三段全 clear 的阴性对照 z="
                         f"rain {zc.get('rain_hard', '-'):.2f} / "
                         f"fog {zc.get('fog_dense', '-'):.2f}")
            line += "（同号同量级 → ~0.5% 漂移是 dropoff RNG 段位置伪相关）"
            lines.append(line)
        di = oc.get("survivor_max_abs_dI")
        if di is not None:
            lines.append(
                f"  - 共有坐标点 intensity 逐点恒等：max|ΔI|={di}"
                "（float32 舍入量级）")
        if oc.get("engine_has_snow") is False:
            lines.append("  - 引擎 WeatherParameters 无 snow 字段：雪不伪造引擎预设，"
                         "只在机制层建模")
    mech = metrics.get("mechanism")
    if isinstance(mech, list) and mech:
        lines += ["",
                  "**② Mechanism：引擎外物理光学材质层**"
                  "（双程 Beer–Lambert / Marshall–Palmer 雨谱 / "
                  "Gunn–Marshall 雪谱 / Koschmieder 雾）"]
        for row in mech:
            if not isinstance(row, dict):
                continue
            line = (f"  - {row.get('label', '-')}：表面点保留 "
                    f"{float(row['surface_kept_pct']):.1f}%，天气伪回波 "
                    f"{float(row['weather_false']):.0f}，双程透射 "
                    f"{float(row['transmission']):.3f}")
            if row.get("near_blind") is not None:
                line += f"，近场盲丢 {float(row['near_blind']):.0f}"
            lines.append(line)
        lines.append("  - 天气伪回波带 is_weather 独立标签，绝不伪装为表面点；"
                     "clear 基线严格恒等；10 个零付费单测可复现")
    residual = metrics.get("residual")
    if isinstance(residual, str) and residual.strip():
        lines += ["", f"**残差声明**：{residual.strip()}"]
    d9 = metrics.get("d9_note")
    if isinstance(d9, str) and d9.strip():
        lines += ["", f"ℹ️ {d9.strip()}"]
    return "\n".join(lines)


def _rungs_metrics_md(metrics: dict[str, Any]) -> "str | None":
    """Task5/7 嵌套 metrics（metrics['rungs'].rungN.pass/checks）的自解释渲染。

    真人 Gate2 打回逐字要求（2026-10-15）：「指标不能全是 -」。这里只读真实
    判定结果，不出任何指标字面量；schema 失败时 rungs 只有 pass，按结构判负展示。
    无嵌套 rungs 的旧式扁平 metrics 返回 None，由调用方走扁平兜底。
    Task8 Track2 batch 指标（metrics_kind=task8_gait_batch）走独立渲染器：
    三臂组统计 + 预注册对照与单 rollout 语义不同，不可复用 Task7 明细。
    Task8 Q2（metrics_kind=q2_invariance_report）同样无 Rung1-4 telemetry：
    它是 D9 条件闸门的物理对照实验，卡片渲染 outcome/mechanism 双结论。
    """
    if metrics.get("metrics_kind") == "task8_gait_batch":
        return _task8_batch_metrics_md(metrics)
    if metrics.get("metrics_kind") == "q2_invariance_report":
        return _q2_report_metrics_md(metrics)
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
            # H-7：explanation 文本来自宏模型产出，标签是我方常量不转义。
            blocks.append(f"**{label}**\n{md_escape(val.strip())}")
    if not blocks:
        return None
    return ("📋 **审批前请先读：任务背景与视频解读（真人 Gate2 要求补充）**\n\n"
            + "\n\n".join(blocks))


def _gate2_metrics_md(
    task_title: str,
    metrics: dict[str, Any],
    intranet_url: "str | None",
    video_fallback: bool = False,
    trim_note: "str | None" = None,
) -> str:
    # H-7：任务名来自宏模型产出，转义；指标正文为我方数值渲染器，不转义。
    lines = [f"**任务目标**：{md_escape(task_title)}"]
    # H-2：原片时长始终展示；实际走到 step4 裁剪时再附裁剪区间行。
    if trim_note:
        lines.append(trim_note)
    # Task8 流体（WJ-Fluid）：aggregate 以 cup_entry_rate 为键（evaluate 接缝
    # 已按 simpraxis.wj_fluid_report.v1 校验过）→ 渲染流体分支。必须先于
    # Task7 WJ-Lite 分支判定——两分支都读 wj_report_path，按 aggregate 键
    # 分流（流体键集合与 success/episodes 不同）。
    agg0 = metrics.get("aggregate")
    if isinstance(agg0, dict) and "cup_entry_rate" in agg0:
        rate = agg0.get("cup_entry_rate")
        rate_s = (f"{rate:.3f}" if isinstance(rate, (int, float))
                  and not isinstance(rate, bool) else "-")
        target = agg0.get("target")
        target_s = (f"{target}" if isinstance(target, (int, float))
                    and not isinstance(target, bool) else "-")
        used = "/".join(str(s) for s in
                        (metrics.get("judge_sources_used") or [])) or "-"
        lines += [
            "**WJ 验收指标 · Task8 流体（simpraxis.wj_fluid_report.v1，"
            "数字引擎现算）**：",
            f"- 入杯率中位数：{rate_s}（目标 {target_s}，"
            f"{'达成' if agg0.get('meets_target') else '未达成'}）",
            f"- 守恒机判：{'通过' if agg0.get('conservation_pass') else '违例'}",
            f"- 飞溅中位：{agg0.get('splash_rate', '-')}；越界中位："
            f"{agg0.get('out_of_bounds_rate', '-')}",
            f"- stage：{metrics.get('stage', '-')}",
            f"- 判分来源：{used}",
            f"- gate2_ready：{'是' if metrics.get('gate2_ready') else '否'}",
            f"- 报表：{inline_code(metrics.get('wj_report_path'))}",
        ]
    # Task7 LIBERO（WJ-Lite）：metrics 带 wj_report_path + aggregate（evaluate
    # 接缝已按 simpraxis.wj_report.v1 校验过）→ 渲染 WJ 分支，不走 rungs 嵌套
    # 也不落旧式扁平兜底（LIBERO 没有那组 pose_error 键，兜底会渲染一屏 '-'）。
    wj_path = metrics.get("wj_report_path")
    if isinstance(agg0, dict) and "cup_entry_rate" in agg0:
        pass  # Task8 流体分支已渲染（见上）
    elif isinstance(wj_path, str) and wj_path and isinstance(
            metrics.get("aggregate"), dict):
        agg = metrics["aggregate"]
        rate = agg.get("success_rate")
        rate_s = (f"{rate:.3f}" if isinstance(rate, (int, float))
                  and not isinstance(rate, bool) else "-")
        used = "/".join(str(s) for s in
                        (metrics.get("judge_sources_used") or [])) or "-"
        lines += [
            "**WJ-Lite 验收指标 · Task7 LIBERO**"
            "（simpraxis.wj_report.v1，数字引擎现算）：",
            f"- 聚合：{agg.get('success')}/{agg.get('episodes')} 局成功"
            f"（成功率 {rate_s}）",
            f"- stage：{metrics.get('stage', '-')}",
            f"- 判分来源：{used}",
            f"- gate2_ready：{'是' if metrics.get('gate2_ready') else '否'}",
            f"- 报表：{inline_code(wj_path)}",
        ]
    else:
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
    trim_note: "str | None" = None,
) -> dict:
    """Gate2 1.0 卡：（可选）任务背景说明 + 指标 markdown（含内网链接）
    + form（验收通过/打回并提出意见）。trim_note 为 H-2 原片/裁剪区间注记。"""
    elements: list[dict] = []
    explain_md = _explanation_md(explanation)
    if explain_md:
        elements.append({"tag": "markdown", "content": explain_md})
        elements.append({"tag": "hr"})
    elements.append({"tag": "markdown",
                     "content": _gate2_metrics_md(task_title, metrics, intranet_url,
                                                  video_fallback, trim_note)})
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
        _div_lark_md(f"**任务目标**：{md_escape(task_title)}\n请在下方选择批准的方案；"
                     f"打回请在意见框填写具体意见（必填，会原样带回重做）。")
    ]
    if not feedback_required:
        body.append(_div_lark_md("_本轮反馈意见为选填（测试模式）_"))
    for idx, p in enumerate(proposals):
        engine = p.get("sim_engine") or p.get("simulator") or "-"
        focus = p.get("first_principles") or p.get("focus") or "-"
        artifacts = _format_artifacts(p.get("expected_artifacts")
                                       or p.get("artifacts"))
        # H-7：name/engine/focus/artifacts 为宏模型产出，逐个转义。
        body.append(_div_lark_md(
            f"**方案 {idx + 1}：{md_escape(p.get('name', '-'))}**\n"
            f"- 物理基座：{inline_code(engine)}\n- 思路/重点：{md_escape(focus)}\n"
            f"- 预期产出：{md_escape(artifacts)}"))
        body.append(_gate1_form_2(idx))
    return {
        "schema": "2.0",
        "config": {"update_multi": True, "wide_screen_mode": True},
        "header": _v2_header("blue", "【SimPraxis】Gate 1：研究方案审批"),
        "body": {"elements": body},
    }


# ---------------- Gate1 · Task7 LIBERO 变体（Stage0 兼容事实随卡） ----------------

def _libero_stage0_md(stage0_facts: dict[str, Any]) -> str:
    """Stage0 兼容事实块正文（Gate1 卡插页；全部来自容器探针机器事实，
    H-7：逐字段转义——非模型文案也不豁免）。"""
    pins = stage0_facts.get("pin_facts") if isinstance(
        stage0_facts.get("pin_facts"), dict) else {}
    pin_line = "、".join(
        f"{md_escape(k)}={inline_code(pins.get(k))}" for k in
        ("numpy_is_shared_pin", "torch_is_shared_pin",
         "mujoco_is_shared_pin", "mujoco_diverged"))
    reasons = stage0_facts.get("reasons") or []
    notes = stage0_facts.get("notes") or []
    return "\n".join([
        "🔬 **Stage0 兼容事实（容器探针产物，非模型文案）**",
        f"- outcome：**{md_escape(stage0_facts.get('outcome', '-'))}**"
        f"（schema `{md_escape(stage0_facts.get('stage0_schema', '-'))}`）",
        f"- 隔离 venv：{inline_code(stage0_facts.get('venv_path', '-'))}",
        f"- pin_facts：{pin_line or '-'}",
        f"- reasons：{md_escape('；'.join(map(str, reasons)) if reasons else '无')}",
        f"- notes：{md_escape('；'.join(map(str, notes)) if notes else '无')}",
        f"- tour 目录：{inline_code(stage0_facts.get('tour_dir', '-'))}",
    ])


_GATE1_LIBERO_TITLE = "【SimPraxis】Gate 1：LIBERO 巡礼方案审批（Stage0 已证兼容）"


def build_gate1_card_libero(
    task_title: str,
    proposals: list[dict[str, Any]],
    stage0_facts: dict[str, Any],
    feedback_required: bool = True,
) -> dict:
    """Gate1 schema 2.0 卡 · LIBERO 变体：通用 v2 卡合成 + Stage0 兼容事实块
    插到 body.elements[1]（任务目标之后、方案 0 之前——真人先看探针事实再
    裁运行案）；只在 outcome=compatible 时被 client 发出（D-4 降级零卡片）。"""
    card = build_gate1_card_v2(task_title, proposals, feedback_required)
    card["header"] = _v2_header("blue", _GATE1_LIBERO_TITLE)
    card["body"]["elements"].insert(
        1, _div_lark_md(_libero_stage0_md(stage0_facts)))
    return card


def build_gate1_card_libero_v1(
    task_title: str,
    proposals: list[dict[str, Any]],
    stage0_facts: dict[str, Any],
    feedback_required: bool = True,
) -> dict:
    """Gate1 1.0 卡 · LIBERO 变体（2.0 被服务端拒时的自动降级形态，与通用
    家法同链路）：通用卡合成 + Stage0 事实 markdown 插 elements[1]。"""
    card = build_gate1_card(task_title, proposals, feedback_required)
    card["header"]["title"]["content"] = _GATE1_LIBERO_TITLE
    card["elements"].insert(
        1, {"tag": "markdown", "content": _libero_stage0_md(stage0_facts)})
    return card


# ---------------- Gate1 · Task8 流体变体（Stage0 流体能力事实随卡） ----------------

def _fluid_stage0_md(stage0_facts: dict[str, Any]) -> str:
    """Stage0 流体能力事实块正文（Gate1 卡插页；全部来自容器探针机器事实，
    H-7：逐字段转义——非模型文案也不豁免）。"""
    gpu = stage0_facts.get("gpu") if isinstance(
        stage0_facts.get("gpu"), dict) else {}
    versions = stage0_facts.get("versions") if isinstance(
        stage0_facts.get("versions"), dict) else {}
    ver_line = "、".join(f"{md_escape(k)}={inline_code(v)}"
                        for k, v in versions.items()) or "-"
    return "\n".join([
        "🔬 **Stage0 流体能力事实（容器探针产物，非模型文案）**",
        f"- outcome：**{md_escape(stage0_facts.get('outcome', '-'))}**"
        f"（schema `{md_escape(stage0_facts.get('stage0_schema', '-'))}`）",
        f"- solver_api：{inline_code(stage0_facts.get('solver_api', '-'))}"
        "（反射发现，非硬编码）",
        f"- n_particles：{md_escape(stage0_facts.get('n_particles', '-'))}"
        f"（Stage1 硬天花板 "
        f"{md_escape(stage0_facts.get('particle_cap', '-'))}）",
        f"- gpu：{md_escape(gpu.get('name', '-'))}"
        f"（peak {md_escape(gpu.get('peak_mib', '-'))} MiB）",
        f"- 入杯目标/沉降窗：{md_escape(stage0_facts.get('cup_entry_target', '-'))}"
        f" / {md_escape(stage0_facts.get('settling_window_s', '-'))} s",
        f"- versions：{ver_line}",
        f"- ts_utc：{md_escape(stage0_facts.get('ts_utc', '-'))}",
    ])


_GATE1_FLUID_TITLE = "【SimPraxis】Gate 1：Q5 厨房倒水方案审批（Stage0 已证流体能力）"


def build_gate1_card_fluid(
    task_title: str,
    proposals: list[dict[str, Any]],
    stage0_facts: dict[str, Any],
    feedback_required: bool = True,
) -> dict:
    """Gate1 schema 2.0 卡 · Task8 流体变体：通用 v2 卡合成 + Stage0 流体
    能力事实块插到 body.elements[1]（任务目标之后、方案 0 之前——真人先看
    探针事实再裁运行案）；只在 propose_run=True 时被 client 发出
    （both_fail/D-5 降级零卡片）。"""
    card = build_gate1_card_v2(task_title, proposals, feedback_required)
    card["header"] = _v2_header("blue", _GATE1_FLUID_TITLE)
    card["body"]["elements"].insert(
        1, _div_lark_md(_fluid_stage0_md(stage0_facts)))
    return card


def build_gate1_card_fluid_v1(
    task_title: str,
    proposals: list[dict[str, Any]],
    stage0_facts: dict[str, Any],
    feedback_required: bool = True,
) -> dict:
    """Gate1 1.0 卡 · Task8 流体变体（2.0 被服务端拒时的自动降级形态，与
    通用/libero 家法同链路）：通用卡合成 + Stage0 事实 markdown 插
    elements[1]。"""
    card = build_gate1_card(task_title, proposals, feedback_required)
    card["header"]["title"]["content"] = _GATE1_FLUID_TITLE
    card["elements"].insert(
        1, {"tag": "markdown", "content": _fluid_stage0_md(stage0_facts)})
    return card


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
    trim_note: "str | None" = None,
) -> dict:
    body: list[dict] = []
    explain_md = _explanation_md(explanation)
    if explain_md:
        body.append(_div_lark_md(explain_md))
        body.append({"tag": "hr"})
    body.append(_div_lark_md(_gate2_metrics_md(
        task_title, metrics, intranet_url, video_fallback, trim_note)))
    body.append(_gate2_form_2(video_fallback))
    return {
        "schema": "2.0",
        "config": {"update_multi": True, "wide_screen_mode": True},
        "header": _v2_header("green", "【SimPraxis】Gate 2：仿真结果验收"),
        "body": {"elements": body},
    }


# ---------------- Phase 2 Task3：ascent 升阶卡 / 双媒体 Diff 卡 ----------------

# Pareto 六指标中文名（与 judge.pareto.METRIC_DIRECTIONS 同一指标集）。
_METRIC_LABELS = {
    "success_rate": "成功率",
    "slip_dist_mean_m": "滑移距离均值 (m)",
    "postpush_vmax_mean_mps": "推力后峰速均值 (m/s)",
    "tilt_max_deg_max": "最大倾角 (°)",
    "solve_wallclock_s": "求解墙钟 (s)",
    "vram_peak_gb": "显存峰值 (GB)",
}

# Pareto 三态机判的用户可见文案（机判仅参考，绝不代决）。
_PARETO_VERDICT_TEXT = {
    V2_DOMINATES: "机判结论：V2 支配 V1（六项指标在容差内全部不差，"
                  "且至少一项严格超越）。",
    NON_DOMINATED: "机判结论：non_dominated——互有胜负或全部容差内持平，"
                   "V2 未支配 V1（禁止凑加权总分翻案）。",
    DOMINATED_BY_V1: "机判结论：V2 被 V1 支配（无严格超越项，且至少一项"
                     "严格劣化）。",
}


def _fmt_num(value: "int | float") -> str:
    """指标数值统一渲染：:.6g（bool 已在调用前挡掉，NaN/Inf 拒绝）。"""
    return f"{float(value):.6g}"


def _finite_number(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{where} 必须是实数，收到 {type(value).__name__}")
    f = float(value)
    if not math.isfinite(f):
        raise ValueError(f"{where} 必须是有限值，收到 {value!r}")
    return f


def _direction_word(sign: int) -> str:
    return "越大越好" if sign == 1 else "越小越好"


def _cell(text: Any) -> str:
    """表格单元格内的 LLM 文本：过 md_escape 后再转义行内竖线。

    md_escape（S-4）只转行首竖线（防伪表格行）；而单元格内的行内 ``|``
    同样会截断列结构，故表格场景在此追加一道（不改动 md_escape 本体）。
    """
    return md_escape(text).replace("|", "\\|")


def _claim_row_left(metric: str) -> str:
    """V1 列：指标标识 + 改善方向；V1 基线数值见卡片顶部 V1 摘要（不在此伪造）。"""
    return (f"`{metric}` {_METRIC_LABELS[metric]}（{_direction_word(METRIC_DIRECTIONS[metric])}）；"
            "V1 基线见上方冻结档案摘要")


def _validated_claim(claim: Any, idx: int) -> dict:
    """ascent_claims 卡片入口校验（生产值已由 setter 规范化；此处 fail-closed）。"""
    where = f"ascent_claims[{idx}]"
    if not isinstance(claim, dict):
        raise ValueError(f"{where} 不是对象")
    metric = claim.get("metric")
    if metric not in METRIC_DIRECTIONS:
        raise ValueError(f"{where}.metric={metric!r} 不在 Pareto 指标集")
    direction = claim.get("direction")
    # bool 是 int 子类（True==1），必须先排 bool 再做成员判定（reviewer LOW-3）。
    if not (isinstance(direction, int) and not isinstance(direction, bool)
            and direction in (1, -1)):
        raise ValueError(f"{where}.direction={direction!r} 只允许 1/-1")
    if direction != METRIC_DIRECTIONS[metric]:
        raise ValueError(f"{where} 方向与指标 {metric} 的改善方向冲突")
    task = claim.get("task")
    if not isinstance(task, str) or not task.strip():
        raise ValueError(f"{where}.task 必须是非空字符串")
    target = _finite_number(claim.get("target"), f"{where}.target")
    epsilon = _finite_number(claim.get("epsilon"), f"{where}.epsilon")
    if epsilon < 0:
        raise ValueError(f"{where}.epsilon 必须非负")
    return {"metric": metric, "direction": int(direction),
            "task": task.strip(), "target": target, "epsilon": epsilon}


def _ascent_proposal_md(idx: int, p: dict[str, Any]) -> str:
    engine = p.get("sim_engine") or p.get("simulator") or "-"
    focus = p.get("first_principles") or p.get("focus") or "-"
    artifacts = _format_artifacts(p.get("expected_artifacts")
                                   or p.get("artifacts"))
    lines = [
        f"**方案 {idx + 1}：{md_escape(p.get('name', '-'))}**",
        f"- 物理基座：{inline_code(engine)}",
        f"- 思路/重点：{md_escape(focus)}",
        f"- 预期产出：{md_escape(artifacts)}",
    ]
    raw_claims = p.get("ascent_claims")
    if raw_claims is None:
        lines.append("- ascent_claims：本候选未给出可量化超越假设"
                     "（未承诺超越项，批准前请自行对质 V1 摘要）")
    else:
        if not isinstance(raw_claims, list) or not raw_claims:
            raise ValueError("ascent_claims 若给出必须是非空数组")
        claims = [_validated_claim(c, j) for j, c in enumerate(raw_claims)]
        lines.append("")
        lines.append("| V1 指标（冻结档案基线） | V2 升阶假设（本候选承诺） |")
        lines.append("| --- | --- |")
        for c in claims:
            arrow = "↑ 增大" if c["direction"] == 1 else "↓ 减小"
            right = (f"目标 `{_fmt_num(c['target'])}`；容差 ε="
                     f"`{_fmt_num(c['epsilon'])}`；方向 {arrow}；"
                     f"验证任务：{_cell(c['task'])}")
            lines.append(f"| {_claim_row_left(c['metric'])} | {right} |")
    return "\n".join(lines)


def _ascent_intro_md(task_title: str, v1_summary: "str | None") -> list[str]:
    blocks = [
        f"**任务目标**：{md_escape(task_title)}",
        "**RSI 螺旋升阶模式（ascent）**：本题存在冻结 V1 档案，禁止伪装首次"
        "求解。下列 V2 候选以 V1 列为基线逐条给出可量化超越假设；Pareto 原则："
        "六项指标全部不差（含容差）且至少一项严格超越才算 V2 支配，互有胜负"
        "必须如实判 non_dominated，禁止凑加权总分。打回请在意见框填写具体意见。",
    ]
    if isinstance(v1_summary, str) and v1_summary.strip():
        # 调用方契约：v1_summary 为 render_v1_metrics 对 sha256 锁定档案渲染的
        # 机器文本（数值/常量，非 LLM 自由文本），与指标渲染器同一信任层。
        blocks.append("**V1 指标摘要（冻结档案基线）**\n" + v1_summary.strip())
    return blocks


def build_gate1_card_ascent(
    task_title: str,
    proposals: list[dict[str, Any]],
    v1_summary: "str | None" = None,
    feedback_required: bool = True,
) -> dict:
    """Gate1 升阶假设卡（仅 schema 2.0，风格对齐 build_gate1_card_v2）。

    内容=任务目标 + V1 冻结指标摘要 + 每候选 V1 指标列 vs V2 假设列两列对照
    （ascent_claims 逐条 metric/方向/target/epsilon/task 表格）。表单按钮 value
    与标准 Gate1 完全一致（gate=1/decision/choice），parse_card_event 无需改
    协议；feedback_<i> 多方案键名沿用 11310 适配。按 Task3 边界不另起 1.0
    分支（真实 schema 2.0 STOP 核验在 Task6）。
    """
    body: list[dict] = [_div_lark_md(md) for md in
                        _ascent_intro_md(task_title, v1_summary)]
    if not feedback_required:
        body.append(_div_lark_md("_本轮反馈意见为选填（测试模式）_"))
    for idx, p in enumerate(proposals):
        body.append(_div_lark_md(_ascent_proposal_md(idx, p)))
        body.append(_gate1_form_2(idx))
    return {
        "schema": "2.0",
        "config": {"update_multi": True, "wide_screen_mode": True},
        "header": _v2_header("blue",
                             "【SimPraxis】Gate 1：升阶研究方案审批（V1→V2）"),
        "body": {"elements": body},
    }


def _normalize_pareto_verdict(verdict: Any) -> str:
    """verdict 接受三态字符串或 {'verdict': ...} dict（state.pareto_verdict）。"""
    if isinstance(verdict, dict):
        verdict = verdict.get("verdict")
    if verdict not in _PARETO_VERDICT_TEXT:
        raise ValueError(
            f"pareto_verdict 必须是 {sorted(_PARETO_VERDICT_TEXT)} 之一，"
            f"收到 {verdict!r}")
    return verdict


def _normalize_diff_rows(metrics_diff: Any) -> list[tuple[str, float, float]]:
    """校验 Δ 表行：恰为 Pareto 六指标各一行 [{metric, v1, v2}, ...]。

    缺指标/多指标/重复/非有限值一律 fail-closed——Δ 表是 Pareto 机判的证据面板，
    缺行的「支配」结论不成立。输出按 METRIC_DIRECTIONS 固定顺序排列（可复现）。
    """
    if not isinstance(metrics_diff, list) or not metrics_diff:
        raise ValueError("metrics_diff 必须是非空行数组 [{metric, v1, v2}, ...]")
    by_metric: dict[str, tuple[float, float]] = {}
    for i, row in enumerate(metrics_diff):
        where = f"metrics_diff[{i}]"
        if not isinstance(row, dict):
            raise ValueError(f"{where} 不是对象")
        metric = row.get("metric")
        if metric not in METRIC_DIRECTIONS:
            raise ValueError(f"{where}.metric={metric!r} 不在 Pareto 指标集")
        if metric in by_metric:
            raise ValueError(f"{where}.metric={metric!r} 重复")
        v1 = _finite_number(row.get("v1"), f"{where}.v1")
        v2 = _finite_number(row.get("v2"), f"{where}.v2")
        by_metric[metric] = (v1, v2)
    missing = sorted(set(METRIC_DIRECTIONS) - set(by_metric))
    extra = sorted(set(by_metric) - set(METRIC_DIRECTIONS))
    if missing or extra:
        raise ValueError(
            f"metrics_diff 必须恰含六项 Pareto 指标（缺 {missing}，多余 {extra}）")
    return [(m, *by_metric[m]) for m in METRIC_DIRECTIONS]


def _diff_arrow(metric: str, v1: float, v2: float) -> str:
    """方向箭头与 Pareto 容差口径一致：ε 带内 ≈ 持平，带外按改善方向 ↑/↓。

    success_rate 绝对 ε=0（任何非零 Δ 都计胜负）；其余相对 |V1| 的 5%。
    """
    direction = METRIC_DIRECTIONS[metric]
    mode, value = METRIC_EPSILON[metric]
    band = 0.0 if mode == "absolute" else value * abs(v1)
    signed = direction * (v2 - v1)
    if signed > band:
        return "↑ 改善"
    if signed < -band:
        return "↓ 劣化"
    return "≈ 持平"


def _gate2_diff_md(
    task_title: str,
    metrics_diff: list[tuple[str, float, float]],
    verdict: str,
    *,
    video_fallback: bool = False,
    explanation: "dict[str, str] | None" = None,
    trim_note: "str | None" = None,
) -> str:
    lines = [f"**任务目标**：{md_escape(task_title)}"]
    if trim_note:
        lines.append(trim_note)
    explain_md = _explanation_md(explanation)
    if explain_md:
        lines += [explain_md, "---"]
    lines += [
        "请对照上方两条视频：**① V1 历史视频**（冻结档案）与 "
        "**② V2 本次视频**（候选实证），逐行核对下表。",
        "",
        "| 指标 | V1（冻结档案） | V2（本次） | Δ（V2−V1） | 方向 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for metric, v1, v2 in metrics_diff:
        delta = v2 - v1
        lines.append(
            f"| `{metric}` {_METRIC_LABELS[metric]} | {_fmt_num(v1)} "
            f"| {_fmt_num(v2)} | {delta:+.6g} "
            f"| {_diff_arrow(metric, v1, v2)} |")
    lines += [
        "",
        _PARETO_VERDICT_TEXT[verdict],
        "机判仅作参考，绝不代决；是否接受由你做**人类最终裁决**（二选一）。"
        "方向列按各指标改善方向与 Pareto 同一 ε 容差判定"
        "（↑ 带外改善 / ↓ 带外劣化 / ≈ 容差内持平）。",
    ]
    if video_fallback:
        lines += ["",
                  "⚠️ 至少一条原生视频上传失败已降级（video_fallback=true），"
                  "请通过证据目录查看原片后再裁决。"]
    return "\n".join(lines)


def build_gate2_card_diff_v2(
    task_title: str,
    metrics_diff: list[dict[str, Any]],
    pareto_verdict: "str | dict[str, Any]",
    *,
    video_fallback: bool = False,
    explanation: "dict[str, str] | None" = None,
    trim_note: "str | None" = None,
) -> dict:
    """Gate2 双媒体 Diff 卡（schema 2.0）：Δ 表 + Pareto 机判 + 人类最终裁决。

    metrics_diff: [{"metric": <Pareto 六指标之一>, "v1": float, "v2": float}]，
    由调用方按重算汇总值组装（脚本自报 outcome 不得入表）。表单 value 与标准
    Gate2 完全一致（gate=2/decision），回调协议不变。
    """
    rows = _normalize_diff_rows(metrics_diff)
    verdict = _normalize_pareto_verdict(pareto_verdict)
    body = [_div_lark_md(_gate2_diff_md(
        task_title, rows, verdict, video_fallback=video_fallback,
        explanation=explanation, trim_note=trim_note))]
    body.append(_div_lark_md("**人类最终裁决（二选一；机判不代决）**"))
    body.append(_gate2_form_2(video_fallback))
    return {
        "schema": "2.0",
        "config": {"update_multi": True, "wide_screen_mode": True},
        "header": _v2_header("green",
                             "【SimPraxis】Gate 2：V1→V2 双视频 Diff 验收"),
        "body": {"elements": body},
    }


def build_gate2_card_diff(
    task_title: str,
    metrics_diff: list[dict[str, Any]],
    pareto_verdict: "str | dict[str, Any]",
    *,
    video_fallback: bool = False,
    explanation: "dict[str, str] | None" = None,
    trim_note: "str | None" = None,
) -> dict:
    """Gate2 双媒体 Diff 卡 1.0 形态（2.0 被拒时的降级，裁决控件仍可操作）。"""
    rows = _normalize_diff_rows(metrics_diff)
    verdict = _normalize_pareto_verdict(pareto_verdict)
    form = _gate2_form_1()
    if video_fallback:
        for btn in form["elements"][1]["actions"]:
            btn["value"] = dict(btn["value"], video_fallback=True)
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": "green",
                   "title": {"tag": "plain_text",
                             "content": "【SimPraxis】Gate 2：V1→V2 双视频 Diff 验收"}},
        "elements": [
            {"tag": "markdown", "content": _gate2_diff_md(
                task_title, rows, verdict, video_fallback=video_fallback,
                explanation=explanation, trim_note=trim_note)},
            {"tag": "markdown",
             "content": "**人类最终裁决（二选一；机判不代决）**"},
            form,
        ],
    }


# ---------------- Phase 2 Task6：ascent Gate1 1.0 降级卡 ----------------

def build_gate1_card_ascent_v1(
    task_title: str,
    proposals: list[dict[str, Any]],
    v1_summary: "str | None" = None,
    feedback_required: bool = True,
) -> dict:
    """Gate1 升阶假设卡的 1.0 降级形态（2.0 被接口拒绝时发送，控件仍可操作）。

    内容与 build_gate1_card_ascent 同一组 markdown 块（任务目标 + V1 冻结指标
    摘要 + ascent_claims 对照表），仅容器换 1.0 elements、表单换 _gate1_form_1；
    按钮 value 协议不变，parse_card_event 无需区分。
    """
    elements: list[dict] = [
        {"tag": "markdown", "content": md}
        for md in _ascent_intro_md(task_title, v1_summary)
    ]
    if not feedback_required:
        elements.append({"tag": "markdown",
                         "content": "_本轮反馈意见为选填（测试模式）_"})
    for idx, p in enumerate(proposals):
        elements.append({"tag": "markdown",
                         "content": _ascent_proposal_md(idx, p)})
        elements.append(_gate1_form_1(idx))
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": "blue",
                   "title": {"tag": "plain_text",
                             "content": "【SimPraxis】Gate 1：升阶研究方案审批（V1→V2）"}},
        "elements": elements,
    }


# ---------------- Phase 2 Task6：四媒体 Diff Gate2（V1/V2 各两张 split） ----------------

# 四条视频的发送顺序钉死（V1 split1 → V1 split2 → V2 split1 → V2 split2）；
# 标签/卡内指引必须与 client._DIFF4_* 同序同名（由 task6 出生测试对质）。
_DIFF4_INTRO = (
    "请严格按发送顺序对照上方四条视频：**① V1 历史视频 A**（冻结档案 "
    "split1：normal vs ice_nominal）、**② V1 历史视频 B**（冻结档案 split2："
    "ice_nominal vs ice_dr）、**③ V2 本次视频 A**（候选实证 split1：normal "
    "vs ice_nominal）、**④ V2 本次视频 B**（候选实证 split2：ice_nominal "
    "vs ice_dr），逐行核对下表。"
)


def _gate2_diff4_md(
    task_title: str,
    metrics_diff: list[tuple[str, float, float]],
    verdict: str,
    *,
    video_fallback: bool = False,
    explanation: "dict[str, str] | None" = None,
    trim_note: "str | None" = None,
) -> str:
    """四媒体 Gate2 卡正文：与双媒体卡同一张六指标 Δ 表/同一 Pareto 口径，
    仅视频指引改为四条；Task3 双媒体文案在 _gate2_diff_md 中一字不动。"""
    lines = [f"**任务目标**：{md_escape(task_title)}"]
    if trim_note:
        lines.append(trim_note)
    explain_md = _explanation_md(explanation)
    if explain_md:
        lines += [explain_md, "---"]
    lines += [
        _DIFF4_INTRO,
        "",
        "| 指标 | V1（冻结档案） | V2（本次） | Δ（V2−V1） | 方向 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for metric, v1, v2 in metrics_diff:
        delta = v2 - v1
        lines.append(
            f"| `{metric}` {_METRIC_LABELS[metric]} | {_fmt_num(v1)} "
            f"| {_fmt_num(v2)} | {delta:+.6g} "
            f"| {_diff_arrow(metric, v1, v2)} |")
    lines += [
        "",
        _PARETO_VERDICT_TEXT[verdict],
        "机判仅作参考，绝不代决；是否接受由你做**人类最终裁决**（二选一）。"
        "方向列按各指标改善方向与 Pareto 同一 ε 容差判定"
        "（↑ 带外改善 / ↓ 带外劣化 / ≈ 容差内持平）。",
    ]
    if video_fallback:
        lines += ["",
                  "⚠️ 至少一条原生视频上传失败已降级（video_fallback=true），"
                  "请通过证据目录查看原片后再裁决。"]
    return "\n".join(lines)


def build_gate2_card_diff4_v2(
    task_title: str,
    metrics_diff: list[dict[str, Any]],
    pareto_verdict: "str | dict[str, Any]",
    *,
    video_fallback: bool = False,
    explanation: "dict[str, str] | None" = None,
    trim_note: "str | None" = None,
) -> dict:
    """Task6 Gate2 四媒体 Diff 卡（schema 2.0）：V1/V2 各两张 split + Δ 表。

    metrics_diff 仍为 Pareto 六指标各行（聚合到 batch 级；split 只承载视觉
    对质），校验/裁决控件与双媒体卡完全同协议。
    """
    rows = _normalize_diff_rows(metrics_diff)
    verdict = _normalize_pareto_verdict(pareto_verdict)
    body = [_div_lark_md(_gate2_diff4_md(
        task_title, rows, verdict, video_fallback=video_fallback,
        explanation=explanation, trim_note=trim_note))]
    body.append(_div_lark_md("**人类最终裁决（二选一；机判不代决）**"))
    body.append(_gate2_form_2(video_fallback))
    return {
        "schema": "2.0",
        "config": {"update_multi": True, "wide_screen_mode": True},
        "header": _v2_header("green",
                             "【SimPraxis】Gate 2：V1→V2 四视频 Diff 验收"),
        "body": {"elements": body},
    }


def build_gate2_card_diff4(
    task_title: str,
    metrics_diff: list[dict[str, Any]],
    pareto_verdict: "str | dict[str, Any]",
    *,
    video_fallback: bool = False,
    explanation: "dict[str, str] | None" = None,
    trim_note: "str | None" = None,
) -> dict:
    """Task6 四媒体 Diff 卡 1.0 形态（2.0 被拒时降级，裁决控件仍可操作）。"""
    rows = _normalize_diff_rows(metrics_diff)
    verdict = _normalize_pareto_verdict(pareto_verdict)
    form = _gate2_form_1()
    if video_fallback:
        for btn in form["elements"][1]["actions"]:
            btn["value"] = dict(btn["value"], video_fallback=True)
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": "green",
                   "title": {"tag": "plain_text",
                             "content": "【SimPraxis】Gate 2：V1→V2 四视频 Diff 验收"}},
        "elements": [
            {"tag": "markdown", "content": _gate2_diff4_md(
                task_title, rows, verdict, video_fallback=video_fallback,
                explanation=explanation, trim_note=trim_note)},
            {"tag": "markdown",
             "content": "**人类最终裁决（二选一；机判不代决）**"},
            form,
        ],
    }


# ---------------- 等待提醒卡片（8h/16h，仅提醒绝不代决） ----------------

def _reminder_body(gate: int, nth: int, waited_hours: int,
                   task_title: "str | None", thread_id: "str | None") -> str:
    # H-6：提醒卡必须自带任务名与线程 ID——共享 P2P 会话里多个审批并存时，
    # 没有身份信息的提醒无法对应到原卡片。任务名为宏产出，转义。
    head = f"Gate{gate} 已等待约 {waited_hours} 小时，仍在等待**人工**结论。\n"
    if task_title:
        head += f"任务：{md_escape(task_title)}\n"
    if thread_id:
        head += f"审批线程：`{thread_id}`\n"
    head += ("系统不会代为决定；请在原审批卡片上操作（批准/打回并提出意见）。")
    return head


def build_reminder_card(
    gate: int,
    nth: int,
    waited_hours: int,
    *,
    task_title: "str | None" = None,
    thread_id: "str | None" = None,
) -> dict:
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": "orange",
                   "title": {"tag": "plain_text",
                             "content": f"【SimPraxis】Gate{gate} 审批等待提醒 {nth}/2"}},
        "elements": [
            {"tag": "markdown",
             "content": _reminder_body(gate, nth, waited_hours,
                                       task_title, thread_id)}
        ],
    }


def build_reminder_card_v2(
    gate: int,
    nth: int,
    waited_hours: int,
    *,
    task_title: "str | None" = None,
    thread_id: "str | None" = None,
) -> dict:
    return {
        "schema": "2.0",
        "config": {"update_multi": True, "wide_screen_mode": True},
        "header": _v2_header("orange",
                             f"【SimPraxis】Gate{gate} 审批等待提醒 {nth}/2"),
        "body": {"elements": [_div_lark_md(
            _reminder_body(gate, nth, waited_hours, task_title, thread_id))]},
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
    text, sender_open_id, message_id, create_time_ms, parent_id, root_id,
    thread_id}（鸭子类型）。

    H-1：parent_id/root_id/thread_id 为消息的回复上下文（lark 1.7.4
    EventMessage 实有字段；非回复消息为 None），通道②据此把意见唯一定位到
    被回复的那张审批卡（卡 message_id / 视频 message_id）。
    """
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
        # H-1：回复关系三字段，用于把 P2P 文本意见定位到具体审批卡线程。
        "parent_id": getattr(msg, "parent_id", None),
        "root_id": getattr(msg, "root_id", None),
        "thread_id": getattr(msg, "thread_id", None),
    }
