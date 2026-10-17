"""Phase 2 Task5：五段式演化志渲染（计划 L514-515，0 付费/0 模型调用）。

``render_evolution_changelog`` 把一次 V1→V2 升阶结案渲染成确定性五段 Markdown
证据件，结构单一源是 ``templates/evolution_changelog_template.md``（段落标题
被 tests/milestone2/test_task5_evolution.py 直接引用）：

  ① 演化动机            —— 只从 Gate1 真人批准意见件读取，原文中性化照录
  ② V1 局限（逐条）     —— 数值只从 V1 summary 的 groups/pareto_metrics 格式化
  ③ V2 创新与公式       —— V2 summary.innovation（控制律原文 + DR 分布机产 JSON）
  ④ 物理真值对照数据    —— 六指标 V1/V2/Δ 表 + contrast + Pareto 机判（仅供参考）
  ⑤ 踩坑与消除的假设    —— Gate2 真人 Diff 裁决意见（中性化）+ V2 声明的消除假设

信任边界（fail-loud，缺来源即异常，不渲染空段、不自由发挥）：
- 四份来源件（V1/V2 summary + Gate1/Gate2 意见）全部必填、可解析、schema
  相符；summary schema 沿用 simpraxis.task8.track2.telemetry.v1（Task6/7/8
  track summary 同契约），chronicle 要求其上的机产扩展块
  ``pareto_metrics``（恰六指标 finite），V2 另要求 ``innovation`` 块；
- Gate 意见件 schema = ``simpraxis.gate_opinion.v1``，decision 必须是
  ``"approve"``（与 pending_gates.db 生产词表一致）；其 human_notes 是
  LLM/真人可控文本，入 md 前按卡片 md_escape
  同口径中性化（orchestrator/phase2 与 feishu_gateway 分属不同 lane，不跨
  lane import 卡片模块，口径在本模块内对齐并有测试钉死），并以引用块照录、
  明确标注「其中数字不作为指标真值」；
- 六指标表的每一个数字只从 summary 的 pareto_metrics 实读格式化，Gate 文本
  里的伪数值进不了表格（防顶替）；Δ 与 Pareto 判定由机算产生
  （orchestrator.judge.pareto，METRIC_DIRECTIONS 方向表唯一真值）。

字段契约可直接被 Task6/7/8 复用：调用方只需按上述 schema 准备四份 JSON 件
并传入 load_evolution_sources()→render_evolution_changelog()。
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

from orchestrator.common.clock import utc_now_iso_z
from orchestrator.judge.gait_thresholds import TELEMETRY_SCHEMA
from orchestrator.judge.pareto import (
    DOMINATED_BY_V1,
    METRIC_DIRECTIONS,
    NON_DOMINATED,
    V2_DOMINATES,
    pareto_verdict,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_PATH = REPO_ROOT / "templates" / "evolution_changelog_template.md"

GATE_OPINION_SCHEMA = "simpraxis.gate_opinion.v1"

# 段落/占位符契约（与模板一一对应；Task6/7/8 只依赖这些名字）。
_MOTIVATION = "SECTION_MOTIVATION"
_V1_LIMITS = "SECTION_V1_LIMITS"
_V2_INNOVATION = "SECTION_V2_INNOVATION"
_METRICS = "SECTION_METRICS"
_PITFALLS = "SECTION_PITFALLS"
_SECTION_TOKENS = (_MOTIVATION, _V1_LIMITS, _V2_INNOVATION, _METRICS, _PITFALLS)
_TITLE_TOKENS = ("EVOLUTION_ROUND", "V2_RUN_ID", "PARENT_RUN_ID", "GENERATED_AT")

_ARM_LABELS = {
    "normal": "正常地面",
    "ice_nominal": "结冰名义",
    "ice_dr": "结冰+DR",
}
_METRIC_LABELS = {
    "success_rate": "成功率 success_rate",
    "slip_dist_mean_m": "滑移距离均值 slip_dist_mean_m (m)",
    "postpush_vmax_mean_mps": "推力后峰速均值 postpush_vmax_mean_mps (m/s)",
    "tilt_max_deg_max": "倾角极值 tilt_max_deg_max (°)",
    "solve_wallclock_s": "求解墙钟 solve_wallclock_s (s)",
    "vram_peak_gb": "显存峰值 vram_peak_gb (GB)",
}
_VERDICT_LABELS = {
    V2_DOMINATES: "v2_dominates（V2 在容差内逐项不差于 V1 且至少一项严格更优）",
    DOMINATED_BY_V1: "dominated_by_v1（V2 无严格更优项且至少一项严格劣于 V1）",
    NON_DOMINATED: "non_dominated（互有胜负或全部 ε 内持平，禁止加权翻案）",
}

# 与 feishu_gateway.cards.md_escape 同口径（不跨 lane import）。
_MD_ESCAPE_CHARS = ("\\", "`", "*", "_", "~", "[", "]", "(", ")", ">", "#")
_OL_RE = re.compile(r"(?m)^(?P<num>\d+)(?P<dot>\. )")
# 四反引号围栏是本模块的最高安全围栏；内容自身出现即拒，防围栏突破。
_QUAD_BACKTICK_RE = re.compile(r"`{4,}")
_SLUG_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class ChronicleError(ValueError):
    """演化志渲染错误基类（fail-loud，不渲染半成品）。"""


class ChronicleSourceError(ChronicleError):
    """来源件缺失/不可解析/schema 不符/必填段为空：拒渲染。"""


def fmt_num(value: "int | float") -> str:
    """指标数值的唯一格式化口径（%.6g）；测试按本函数回溯，不硬编码巧合数字。"""
    return f"{float(value):.6g}"


# ---------------------------------------------------------------------------
# LLM/真人可控文本中性化（与卡片 md_escape 同口径）
# ---------------------------------------------------------------------------

def neutralize_text(text: Any) -> str:
    """把 Gate 意见文本转成 md 安全字面量（结构字符反斜杠化，数值原样保留）。

    数值不做改写——伪数值防护靠「表格数字只从 summary 格式化 + Gate 文本进
    引用块并标注不作真值」，不靠删数字（删了就不是逐字证据）。
    """
    s = str(text)
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


def _quote_block(text: str) -> str:
    """中性化后包成逐字引用块（其数字明确不作为指标真值）。"""
    body = neutralize_text(text)
    return "\n".join("> " + line if line else ">"
                     for line in body.splitlines()) or ">"


def _safe_fenced(lang: str, content: str, where: str) -> str:
    """四反引号围栏；内容若含四连反引号（可突破围栏）直接拒渲染。"""
    if _QUAD_BACKTICK_RE.search(content):
        raise ChronicleSourceError(
            f"{where} 含连续四反引号，禁止进入演化志围栏（防注入）")
    return f"````{lang}\n{content}\n````"


# ---------------------------------------------------------------------------
# 来源件校验
# ---------------------------------------------------------------------------

def _is_real_num(x: Any) -> bool:
    return not isinstance(x, bool) and isinstance(x, (int, float)) \
        and math.isfinite(float(x))


def _load_json_file(path: "str | Path", label: str) -> dict:
    p = Path(path)
    if not p.is_file():
        raise ChronicleSourceError(f"{label} 来源件缺失：{p}（缺来源 fail-loud）")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ChronicleSourceError(f"{label} 来源件不可解析：{p}: {exc}") from exc
    if not isinstance(data, dict):
        raise ChronicleSourceError(f"{label} 来源件顶层必须是对象：{p}")
    return data


def _validate_pareto_metrics(block: Any, where: str) -> dict[str, float]:
    if not isinstance(block, dict):
        raise ChronicleSourceError(f"{where} 缺 pareto_metrics 对象（六指标表必填）")
    if set(block) != set(METRIC_DIRECTIONS):
        missing = sorted(set(METRIC_DIRECTIONS) - set(block))
        extra = sorted(set(block) - set(METRIC_DIRECTIONS))
        raise ChronicleSourceError(
            f"{where}.pareto_metrics 必须恰含六指标 {sorted(METRIC_DIRECTIONS)}；"
            f"缺 {missing}，多余 {extra}")
    for key, value in block.items():
        if not _is_real_num(value):
            raise ChronicleSourceError(
                f"{where}.pareto_metrics[{key}] 必须是 finite 实数"
                f"（bool/NaN/Inf 拒），收到 {value!r}")
    return {k: float(block[k]) for k in METRIC_DIRECTIONS}


def _validate_groups(groups: Any, where: str) -> dict[str, dict]:
    if not isinstance(groups, dict) or not groups:
        raise ChronicleSourceError(f"{where} 缺非空 groups 对照臂块")
    for arm, g in groups.items():
        if not isinstance(g, dict):
            raise ChronicleSourceError(f"{where}.groups[{arm}] 必须是对象")
        for key in ("n", "n_fell"):
            v = g.get(key)
            if isinstance(v, bool) or not isinstance(v, int):
                raise ChronicleSourceError(
                    f"{where}.groups[{arm}].{key} 必须是整数，收到 {v!r}")
        sr = g.get("success_rate")
        if not _is_real_num(sr):
            raise ChronicleSourceError(
                f"{where}.groups[{arm}].success_rate 必须是 finite 实数，"
                f"收到 {sr!r}")
        tilt = g.get("tilt_max_deg_max")
        if tilt is not None and not _is_real_num(tilt):
            raise ChronicleSourceError(
                f"{where}.groups[{arm}].tilt_max_deg_max 若非空必须 finite，"
                f"收到 {tilt!r}")
    return groups


def _validate_summary(data: dict, *, v2: bool) -> dict:
    where = "v2_summary" if v2 else "v1_summary"
    if data.get("schema") != TELEMETRY_SCHEMA:
        raise ChronicleSourceError(
            f"{where}.schema 必须是 {TELEMETRY_SCHEMA}，收到 {data.get('schema')!r}")
    run_id = data.get("run_id")
    if not isinstance(run_id, str) or not run_id.strip():
        raise ChronicleSourceError(f"{where}.run_id 必须是非空字符串")
    _validate_groups(data.get("groups"), where)
    data["pareto_metrics"] = _validate_pareto_metrics(
        data.get("pareto_metrics"), where)
    contrast = data.get("contrast")
    if contrast is not None and not isinstance(contrast, dict):
        raise ChronicleSourceError(
            f"{where}.contrast 若非空必须是机产对象，收到 {type(contrast).__name__}")
    if v2:
        innovation = data.get("innovation")
        if not isinstance(innovation, dict):
            raise ChronicleSourceError(
                f"{where}.innovation 必填（V2 控制律/DR 分布来源，缺源不补）")
        law = innovation.get("control_law")
        if not isinstance(law, str) or not law.strip():
            raise ChronicleSourceError(
                f"{where}.innovation.control_law 必须是非空字符串"
                "（公式以文本/LaTeX 片段承载）")
        dr = innovation.get("dr_distribution")
        if not isinstance(dr, dict) or not dr:
            raise ChronicleSourceError(
                f"{where}.innovation.dr_distribution 必须是非空机产对象")
        eliminated = data.get("eliminated_assumptions")
        if eliminated is not None:
            if not isinstance(eliminated, list) or not all(
                    isinstance(x, str) and x.strip() for x in eliminated):
                raise ChronicleSourceError(
                    f"{where}.eliminated_assumptions 必须是非空字符串数组")
    return data


def _validate_opinion(data: dict, gate: str) -> dict:
    where = f"{gate}_opinion"
    if data.get("schema") != GATE_OPINION_SCHEMA:
        raise ChronicleSourceError(
            f"{where}.schema 必须是 {GATE_OPINION_SCHEMA}，收到 "
            f"{data.get('schema')!r}")
    if data.get("gate") != gate:
        raise ChronicleSourceError(
            f"{where}.gate 必须为 {gate!r}，收到 {data.get('gate')!r}")
    if data.get("decision") != "approve":
        # decision 词表与 pending_gates.db 的生产字面量一致（store.py:
        # decision in approve | reject_with_comments）；演化志只记真人放行结案。
        raise ChronicleSourceError(
            f"{where}.decision 必须为 'approve'（演化志只记真人批准结案），收到 "
            f"{data.get('decision')!r}")
    for key in ("human_notes", "source", "ts_iso"):
        value = data.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ChronicleSourceError(
                f"{where}.{key} 必须是非空字符串（空真人意见不得代笔）")
    return data


def load_evolution_sources(*, v1_summary_path: "str | Path",
                           v2_summary_path: "str | Path",
                           gate1_opinion_path: "str | Path",
                           gate2_opinion_path: "str | Path") -> dict[str, dict]:
    """读四份来源 JSON 件并校验；任一缺失/损坏/schema 不符即 ChronicleSourceError。"""
    v1_summary = _validate_summary(
        _load_json_file(v1_summary_path, "v1_summary"), v2=False)
    v2_summary = _validate_summary(
        _load_json_file(v2_summary_path, "v2_summary"), v2=True)
    gate1_opinion = _validate_opinion(
        _load_json_file(gate1_opinion_path, "gate1_opinion"), "gate1")
    gate2_opinion = _validate_opinion(
        _load_json_file(gate2_opinion_path, "gate2_opinion"), "gate2")
    return {"v1_summary": v1_summary, "v2_summary": v2_summary,
            "gate1_opinion": gate1_opinion, "gate2_opinion": gate2_opinion}


# ---------------------------------------------------------------------------
# 五段渲染
# ---------------------------------------------------------------------------

def load_template() -> str:
    try:
        return TEMPLATE_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        raise ChronicleError(f"演化志模板缺失/不可读：{TEMPLATE_PATH}: {exc}") \
            from exc


def section_headings() -> list[str]:
    """模板中的五个二级标题（结构单一源；测试直接引用本返回值）。"""
    tpl = load_template()
    headings = re.findall(r"^## .+$", tpl, flags=re.M)
    if len(headings) != 5:
        raise ChronicleError(
            f"演化志模板必须恰含 5 个 ## 段标题，实得 {len(headings)}")
    return headings


def _section_motivation(gate1_opinion: dict, parent_run_id: str,
                        v2_run_id: str, evolution_round: int) -> str:
    lines = [
        f"- 父运行（V1）：`{parent_run_id}` → 本运行（V2）：`{v2_run_id}`；"
        f"演化轮次：{evolution_round}",
        "- Gate1 真人批准意见（升阶假设经真人批准后容器才允许实跑 V2；"
        "以下原文中性化照录，**其中数字不作为指标真值**）：",
        "",
        _quote_block(gate1_opinion["human_notes"]),
        f"> 裁决时间 {gate1_opinion['ts_iso']}；渠道 {gate1_opinion['source']}。",
    ]
    return "\n".join(lines)


def _section_v1_limits(v1_summary: dict) -> str:
    lines = ["**对照臂机产值（V1 summary.groups，独立重算口径）：**"]
    for arm, label in _ARM_LABELS.items():
        g = v1_summary["groups"].get(arm)
        if not isinstance(g, dict):
            continue
        line = (f"- {label}（n={g['n']}）：success_rate="
                f"{fmt_num(g['success_rate'])}，n_fell={g['n_fell']}")
        if g.get("tilt_max_deg_max") is not None:
            line += f"，倾角极值 {fmt_num(g['tilt_max_deg_max'])}°"
        lines.append(line)
    lines.append("")
    lines.append("**六指标 V1 基线（V1 summary.pareto_metrics，逐条对应期望方向）：**")
    for metric in METRIC_DIRECTIONS:
        better = "越大越好" if METRIC_DIRECTIONS[metric] == 1 else "越小越好"
        lines.append(
            f"- {_METRIC_LABELS[metric]}：V1={fmt_num(v1_summary['pareto_metrics'][metric])}"
            f"（{better}，V2 须在 ε 容差内不差于该值）")
    return "\n".join(lines)


def _section_v2_innovation(v2_summary: dict) -> str:
    innovation = v2_summary["innovation"]
    parts = [
        "**控制律（V2 summary.innovation.control_law 原文，公式以文本/LaTeX 片段承载）：**",
        "",
        _safe_fenced("text", innovation["control_law"].strip(),
                     "v2_summary.innovation.control_law"),
        "",
        "**域随机化分布（V2 summary.innovation.dr_distribution，机产 JSON）：**",
        "",
        _safe_fenced("json",
                     json.dumps(innovation["dr_distribution"], ensure_ascii=False,
                                sort_keys=True, indent=2),
                     "v2_summary.innovation.dr_distribution"),
    ]
    eliminated = v2_summary.get("eliminated_assumptions")
    if eliminated:
        parts += ["", "**V2 显式声明消除的假设（eliminated_assumptions，中性化照录）：**"]
        parts += [f"- {neutralize_text(item.strip())}" for item in eliminated]
    return "\n".join(parts)


def _section_metrics(v1_summary: dict, v2_summary: dict) -> str:
    v1m, v2m = v1_summary["pareto_metrics"], v2_summary["pareto_metrics"]
    lines = [
        "| 指标 | 方向 | V1 | V2 | Δ(V2−V1) |",
        "|---|---|---|---|---|",
    ]
    for metric in METRIC_DIRECTIONS:
        a, b = v1m[metric], v2m[metric]
        better = "越大越好" if METRIC_DIRECTIONS[metric] == 1 else "越小越好"
        lines.append(
            f"| {_METRIC_LABELS[metric]} | {better} | {fmt_num(a)} | "
            f"{fmt_num(b)} | {fmt_num(b - a)} |")
    verdict = pareto_verdict(v1m, v2m)
    lines += [
        "",
        "**Pareto 机判（仅参考，不替代真人 Gate2 裁决；判定只用重算汇总值，"
        "脚本自报不参与）：**",
        f"`{verdict}` —— {_VERDICT_LABELS[verdict]}",
    ]
    for tag, summary in (("V1", v1_summary), ("V2", v2_summary)):
        contrast = summary.get("contrast")
        if isinstance(contrast, dict):
            body = json.dumps(contrast, ensure_ascii=False, sort_keys=True,
                              indent=2)
            lines += [
                "",
                f"**{tag} contrast 预注册对照（机产 JSON，仅参考）：**",
                "",
                _safe_fenced("json", body, f"{tag.lower()}_summary.contrast"),
            ]
    return "\n".join(lines)


def _section_pitfalls(gate2_opinion: dict, v2_summary: dict) -> str:
    lines = [
        "Gate2 真人双媒体 Diff 裁决意见（以下原文中性化照录，"
        "**其中数字不作为指标真值**）：",
        "",
        _quote_block(gate2_opinion["human_notes"]),
        f"> 裁决时间 {gate2_opinion['ts_iso']}；渠道 {gate2_opinion['source']}。",
    ]
    eliminated = v2_summary.get("eliminated_assumptions")
    if eliminated:
        lines += ["", "V2 显式声明消除的假设（与第三段同源）："]
        lines += [f"- {neutralize_text(item.strip())}" for item in eliminated]
    return "\n".join(lines)


def render_evolution_changelog(*, v1_summary: dict, v2_summary: dict,
                               gate1_opinion: dict, gate2_opinion: dict,
                               parent_run_id: str, evolution_round: int,
                               generated_at: "str | None" = None) -> str:
    """渲染五段式演化志 Markdown。来源件缺段/非法即 ChronicleError，绝不半渲染。"""
    v1_summary = _validate_summary(dict(v1_summary), v2=False)
    v2_summary = _validate_summary(dict(v2_summary), v2=True)
    gate1_opinion = _validate_opinion(dict(gate1_opinion), "gate1")
    gate2_opinion = _validate_opinion(dict(gate2_opinion), "gate2")

    if not isinstance(parent_run_id, str) or not parent_run_id.strip():
        raise ChronicleSourceError("parent_run_id 必填非空（V1 父运行锚点）")
    parent_run_id = parent_run_id.strip()
    if parent_run_id != v1_summary["run_id"]:
        raise ChronicleSourceError(
            f"parent_run_id={parent_run_id!r} 与 v1_summary.run_id="
            f"{v1_summary['run_id']!r} 不一致（V1 父运行必须同源对质）")
    v2_run_id = v2_summary["run_id"].strip()
    if v2_run_id == parent_run_id:
        raise ChronicleSourceError("V2 run_id 不得与 V1 父运行相同")
    if isinstance(evolution_round, bool) or not isinstance(evolution_round, int) \
            or evolution_round < 1:
        raise ChronicleSourceError(
            "evolution_round 必须是 >=1 的整数，收到 "
            f"{evolution_round!r}")

    sections = {
        _MOTIVATION: _section_motivation(gate1_opinion, parent_run_id,
                                         v2_run_id, evolution_round),
        _V1_LIMITS: _section_v1_limits(v1_summary),
        _V2_INNOVATION: _section_v2_innovation(v2_summary),
        _METRICS: _section_metrics(v1_summary, v2_summary),
        _PITFALLS: _section_pitfalls(gate2_opinion, v2_summary),
    }
    for token, body in sections.items():
        if not body.strip():
            raise ChronicleSourceError(f"段落 {token} 渲染为空：禁止空段")  # pragma: no cover

    tpl = load_template()
    headings = section_headings()  # 模板恰 5 段，否则已抛
    md = tpl
    for token in _TITLE_TOKENS:
        placeholder = "{{" + token + "}}"
        if md.count(placeholder) != 1:
            raise ChronicleError(
                f"演化志模板占位符 {placeholder} 必须恰出现 1 次")  # pragma: no cover
    md = (md.replace("{{EVOLUTION_ROUND}}", str(evolution_round))
            .replace("{{V2_RUN_ID}}", v2_run_id)
            .replace("{{PARENT_RUN_ID}}", parent_run_id)
            .replace("{{GENERATED_AT}}", generated_at or utc_now_iso_z()))
    for token in _SECTION_TOKENS:
        placeholder = "{{" + token + "}}"
        if md.count(placeholder) != 1:
            raise ChronicleError(
                f"演化志模板段落占位符 {placeholder} 必须恰出现 1 次")  # pragma: no cover
        md = md.replace(placeholder, sections[token])
    if "{{" in md or "}}" in md:
        raise ChronicleError("演化志仍有未填充占位符")  # pragma: no cover
    for heading in headings:
        if heading not in md:
            raise ChronicleError(f"演化志缺段：{heading}")  # pragma: no cover
    return md


def evolution_filename(*, parent_run_id: str, v2_run_id: str,
                       evolution_round: int) -> str:
    """演化志证据文件名（thread_id 字符集 C4 安全；同参重跑同名，原子写覆盖）。"""
    for name, value in (("parent_run_id", parent_run_id),
                        ("v2_run_id", v2_run_id)):
        if not isinstance(value, str) or not _SLUG_RE.match(value):
            raise ChronicleError(
                f"{name} 仅允许 [A-Za-z0-9_-]，收到 {value!r}")
    if isinstance(evolution_round, bool) or not isinstance(evolution_round, int) \
            or evolution_round < 1:
        raise ChronicleError("evolution_round 必须是 >=1 的整数")
    return (f"evolution_{parent_run_id}_to_{v2_run_id}"
            f"_r{evolution_round}.md")
