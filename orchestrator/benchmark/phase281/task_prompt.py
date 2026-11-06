"""Phase 2.8.1 T4：``TASK.md`` 渲染与渲染件 sha256（**判据零泄漏**）。

候选看到的作业说明只有一个来源：本模块。它由 ``trial_harness`` 在输入
staging 之后、任何引擎行为之前写出，内容**只由 ``(task_id, rep, seed)``
决定**——同一 ``(任务, 重复轮次)`` 的两条实验臂收到**逐字节相同**的说明
（臂 id 不进文件；把它写进去，等于把实验分组泄露给模型）。

三条硬约束（每条都有机判）：

1. **不带任何判据数值**（AC-P281-4-04）。判据件在 ``private/criteria/``，
   候选可见面一个数值都不许出现——包括「≤ 20 mm」这种把 prose 判据翻译成
   数字的写法。检测按**数值 token 精确等值**比对，**不**用朴素子串：R1 的
   唯一判据值是 ``3``，朴素子串会把「13 项必填段」判成泄漏，也会把
   ``0.0014`` 判成 ``0.001``——那是检测器的假阳性，不是泄漏。
2. **不含 ASCII ``threshold``**（计划 §8.6 步骤 3 的 ``grep -c`` 旁证）。
3. **契约必须写全**：候选要产出什么、以什么 argv 被调用、产物要满足什么
   **结构**（不是数值）——不写，任务就是不可答的，失败会变成「没说清楚」
   而不是「能力不足」。结构与数值的界线是：``scoring.py`` 里的
   ``MUJOCO_TELEMETRY_FIELDS`` / ``MUJOCO_METRIC_KEYS`` / ``R1_REQUIRED_SECTIONS``
   / ``R1_PARAMETER_CLASSES`` 是**契约**（候选不给就注定无效），
   ``criteria.thresholds`` 是**判据**（候选一旦知道就等于看答案）。
   本模块从 ``scoring`` **导入**契约名，不复制字面量：两处各写一份，改一处
   就会悄悄漂移。

写文件时**先自检后落盘**：:func:`write_task_md` 对本模块渲染出的文本做
阈值比对与 ASCII 旁证，命中即抛 :class:`PromptIsolationError`，**不写文件**。
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from orchestrator.benchmark import scoring
from orchestrator.common.fsutil import atomic_write_text, ensure_private_dir

__all__ = (
    "FORBIDDEN_ASCII",
    "HARNESS_OUTPUTS",
    "PROMPT_SCHEMA",
    "PromptIsolationError",
    "TASK_MD_NAME",
    "candidate_outputs",
    "extra_candidate_outputs",
    "render_task_md",
    "task_md_sha256",
    "threshold_hits",
    "threshold_values",
    "write_task_md",
)

TASK_MD_NAME = "TASK.md"
PROMPT_SCHEMA = "simpraxis.phase281.task_prompt.v1"

#: ASCII 旁证（计划 §8.6 步骤 3：``grep -c "threshold"`` 期望 0）
FORBIDDEN_ASCII = ("threshold",)

#: **运行器代写**的产物：出现在 ``task.yaml.outputs`` 里，但不是候选的活。
#: ``runtime_fingerprint.json`` 由 harness 在运行前后采集（L1 的动作/观测
#: 契约取自候选的 ``env_probe.json``）；``run_manifest.json`` 与
#: ``birth/functional_birth.json`` 分别由 ``reference._write_manifest`` 同形
#: 生成与出生验证器写出。让候选去写它们，等于让被告自己写笔录。
HARNESS_OUTPUTS = ("runtime_fingerprint.json", "run_manifest.json",
                   "birth/functional_birth.json")

#: M1 的复现件由 harness 用同种子/换种子各再跑一次候选入口产出
#: （与 ``reference._publish_repro_runs`` 同口径）——``_repro_facts`` 只认这三组
#: 原始件，缺一即 ``independent=False``（不许退回自报 ``reproducibility.json``）。
M1_REPRO_OUTPUTS = (
    "repro/same_seed/metrics.json",
    "repro/same_seed/telemetry.jsonl",
    "repro/cross_seed/metrics.json",
    "repro/cross_seed/telemetry.jsonl",
)

#: 数值字面量 token（与 ``telemetry`` 的泄漏检测同口径：不得紧贴词字符/小数点）
_NUMBER_RE = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)(?![\w.])")

_TITLE_BY_TASK = {
    "mujoco_contact_push_v1": "MuJoCo 接触推动（M1 / Track 1 engineering）",
    "libero_env_adapter_v1": "LIBERO 环境适配（L1 / Track 1 environment_integration）",
    "icy_ground_dr_v1": "冰面域随机化研究设计（R1 / Track 2 research_design）",
}


class PromptIsolationError(RuntimeError):
    """渲染件里出现了不该给候选看的东西（判据数值 / ASCII 旁证针）。"""


# ---------------------------------------------------------------------------
# 判据数值的取用与检测
# ---------------------------------------------------------------------------

def threshold_values(spec: Any) -> "list[float]":
    """``spec.thresholds`` 里的全部数值（``{"name": {"value": v, ...}}``）。"""
    thresholds = getattr(spec, "thresholds", None) or {}
    out: "set[float]" = set()
    if not isinstance(thresholds, Mapping):
        return []
    for entry in thresholds.values():
        value = entry.get("value") if isinstance(entry, Mapping) else entry
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            out.add(float(value))
    return sorted(out)


def threshold_hits(text: str, *,
                   values: "Iterable[float] | None" = None,
                   spec: Any = None) -> "list[str]":
    """文本里与判据值**精确相等**的数值 token（空列表 = 未泄漏）。

    ``values`` 优先；否则从 ``spec.thresholds`` 取。**只做精确等值**，不做
    子串包含——理由见模块 docstring 第 1 条。「``0.001`` 是 ``0.0014`` 的
    子串」这种朴素口径会把合法的容差说明判成泄漏。
    """
    if values is None:
        values = threshold_values(spec) if spec is not None else ()
    wanted = {float(v) for v in values}
    if not wanted:
        return []
    hits: "set[str]" = set()
    for token in _NUMBER_RE.findall(text or ""):
        try:
            number = float(token)
        except ValueError:  # pragma: no cover - 正则已保证可解析
            continue
        if number in wanted:
            hits.add(token)
    return sorted(hits)


# ---------------------------------------------------------------------------
# 产物面：契约（可写）与判据（不可写）的分界
# ---------------------------------------------------------------------------

def candidate_outputs(spec: Any) -> "list[str]":
    """候选**必须自己产出**的文件（``task.yaml.outputs`` 去掉运行器代写件）。"""
    return [str(item) for item in (getattr(spec, "outputs", ()) or ())
            if str(item) not in HARNESS_OUTPUTS]


def extra_candidate_outputs(task_id: str) -> "list[str]":
    """``task.yaml.outputs`` 之外、判分层还会读的候选产物（按任务）。"""
    if task_id == "mujoco_contact_push_v1":
        # 由 harness 三次执行产出；不要求候选手写，但必须知道「会被重跑」。
        return list(M1_REPRO_OUTPUTS)
    return []


def _runtime_block(spec: Any) -> "list[str]":
    runtime = dict(getattr(spec, "runtime", {}) or {})
    packages = runtime.get("packages") or {}
    lines = [f"- runtime_id：`{runtime.get('runtime_id') or '?'}`"]
    if runtime.get("python"):
        lines.append(f"- 解释器：`{runtime.get('python')}`（运行器提供）")
    if packages:
        rendered = "、".join(f"`{name}` {version}"
                             for name, version in sorted(packages.items()))
        lines.append(f"- 依赖（**已冻结，禁止升级/安装/换版本**）：{rendered}")
    budget = dict(getattr(spec, "budget", {}) or {})
    if budget.get("wall_time_seconds") is not None:
        lines.append(f"- 单次执行墙钟上限：{int(budget['wall_time_seconds'])} 秒")
    if budget.get("max_output_bytes") is not None:
        lines.append(
            f"- 全部产物总量上限：{int(budget['max_output_bytes'])} 字节")
    return lines


def _invocation_block(spec: Any) -> "list[str]":
    """入口的调用形状。**按入口类型分派，不按臂分派**（两臂逐字节同文）。

    三条分支对应三种入口契约：

    * ``document_bundle``（R1）：入口是**机读文档**，运行器不拿 argv 启动它，
      只做结构检查——写一段 argv 会凭空造出一个不存在的调用契约。
    * ``libero_env_adapter_v1``（L1）：入口**由候选自己写进工作根**。旧稿写成
      ``python3 /work/{entry}``（``/work`` 是只读输入面）是错的：那一刻
      ``adapter.py`` 还不存在，候选按它去写 `/work` 也写不进去。工作根在容器
      内挂 ``/out`` 且是当前目录，故 argv 用**工作根相对**写法。
    * 其余（M1 宿主形态）：工作根即当前目录，``--out-dir .`` 与参考运行
      （``reference.run_host_reference`` 的 ``--out-dir <target>`` + ``cwd=target``）
      同义。
    """
    task_id = str(getattr(spec, "task_id", ""))
    entrypoint = dict(getattr(spec, "entrypoint", {}) or {})
    entry = str(entrypoint.get("path") or "?")
    if str(entrypoint.get("kind") or "") == "document_bundle":
        return [
            f"本任务的入口是一份**机读文档**（`{entry}`），不是可执行脚本：",
            "",
            "- 运行器**不**以 argv 启动它；它在工作根里按**结构**被检查"
            "（见「验收口径（结构）」）。",
            "- 文档与其余产物一律写进工作根（当前工作目录）。",
        ]
    if task_id == "libero_env_adapter_v1":
        return [
            "运行器在**任务镜像**内、以工作根为当前目录，按如下 argv 启动你的入口：",
            "",
            "```",
            f"python3 {entry} --out-dir . --seed <本 Trial 种子> "
            "--chown-uid <运行用户 uid> --chown-gid <运行用户 gid>",
            "```",
            "",
            f"- 入口要**你自己写**进工作根（容器内 `/out/{entry}`）；运行器不提供"
            "任何起步代码。",
            "- 工作根在容器内挂在 `/out`（可写，当前目录）。输入面是另外几个"
            "**只读**挂载：`/work`（输入件）、`/skills`、`/assets`——只读面不可写，"
            "产物一律写 `/out`。",
            "- 容器 `--network none`；镜像自带所需的 robosuite / mujoco / bddl / "
            "libero，**不要安装任何包**。",
            "- `--chown-uid/--chown-gid` 用于把产物交回调用者：写完产物后按参数"
            "把文件属主改为这两个值（以 root 运行时；无法 chown 时忽略并继续）。",
        ]
    return [
        "运行器以如下 argv 启动你的入口（工作根 = 当前目录 = `--out-dir`）：",
        "",
        "```",
        f"<python> -I {entry} --out-dir . --seed <本 Trial 种子>",
        "```",
        "",
        "- `-I` 隔离模式：**不要依赖环境变量或用户 site-packages**；需要的库"
        "在冻结依赖里，直接 import。",
        "- 产物一律写进工作根（等于当前工作目录）。",
    ]


def _invocation_preamble(spec: Any) -> "list[str]":
    """§五 的首行：说明「这段是契约，不是命令建议」，**臂中立**。"""
    entrypoint = dict(getattr(spec, "entrypoint", {}) or {})
    if str(entrypoint.get("kind") or "") == "document_bundle":
        return []
    return [
        "入口的调用契约如下（运行器在**出生检查**与**复现重跑**时按此调用；"
        "你若自行执行，也请用同一形状——只接受这一个形状的调用）：",
        "",
    ]


def _acceptance_block(task_id: str) -> "list[str]":
    """结构契约（**不含任何判据数值**）。名字从 ``scoring`` 导入，不复制。"""
    if task_id == "mujoco_contact_push_v1":
        fields = "、".join(f"`{name}`" for name in scoring.MUJOCO_TELEMETRY_FIELDS)
        metrics = "、".join(f"`{name}`" for name in scoring.MUJOCO_METRIC_KEYS)
        return [
            "- `telemetry.jsonl` 每行是一个 JSON 对象，**至少**含这些字段："
            + fields + "（逐帧一行，按步序递增）。",
            f"- `metrics.json` 至少含这些键：{metrics}。自报值会与"
            "`telemetry.jsonl` **重算值**逐键对账，对不上即判无效。",
            "- `scene_spec.json` 说明你装配的场景（几何/质量/摩擦/控制参数），"
            "供复核与复算。",
            "- 复现件：入口就绪后，运行器会以**同一种子**与**另一种子**各再执行"
            "一次你的入口，把两次运行的 `metrics.json`/`telemetry.jsonl` 放进"
            "`repro/same_seed/` 与 `repro/cross_seed/`。因此你的入口必须在给定"
            "`--seed` 下**确定性可重跑**（同种子逐字段一致），且不要把"
            "`wall_time_seconds` 之类计时量混进会被比对的键里。",
        ]
    if task_id == "libero_env_adapter_v1":
        return [
            "- `env_probe.json`：必须记录真实环境探针结果，至少含 "
            "`python_executable`、`python_version`、`captured_in`（容器内运行写 "
            "`\"container\"`）、`action_dim`（正整数）、`observation_keys`（非空列表）。",
            "- `one_step_result.json`：一次预注册 step 的结果，至少含 "
            "`advanced`（布尔，状态确实推进）与 `state`（非空映射，数值全部有限）。",
            "- 出生检查（import → 构造 Environment → 契约 → 一次最小 step）由运行器"
            "在你的入口就绪后执行；任一层未过，本 Trial 即以「未出生」结案，"
            "不进入评分。",
            "- 禁止 `compile()/exec()` 冒充 import，禁止 mock Environment，禁止"
            "伪造 `env_spec`，禁止跑 tour 自带的 demo / README 流程。",
        ]
    if task_id == "icy_ground_dr_v1":
        sections = "、".join(f"`{name}`" for name in scoring.R1_REQUIRED_SECTIONS)
        classes = "、".join(f"`{name}`" for name in scoring.R1_PARAMETER_CLASSES)
        return [
            f"- `experiment_plan.yaml` 必须含这 {len(scoring.R1_REQUIRED_SECTIONS)} "
            f"个机读段：{sections}。",
            f"- 参数定义至少覆盖这三类参数：{classes}；每个参数必须带物理单位与取值。",
            "- 三段划分（全量随机化 / 单因素摘除 / 无随机化）互斥；指标必须可计算"
            "且给出统计检验；结论必须能指回条目。",
            "- `parameter_space.json` 与 `evaluation_metrics.json` 与 "
            "`experiment_plan.yaml` 自洽；`research_report.md` 是给人读的报告。",
            "- 本任务只评估研究设计质量：**不执行训练、不产生新实验数据**。"
            "历史结论已冻结（冰面实验不成立），任何产物都不得声称已翻案或已取得"
            "真机零样本成功。",
        ]
    return []


def _constraints_block(spec: Any) -> "list[str]":
    constraints = dict(getattr(spec, "constraints", {}) or {})
    lines = []
    if str(constraints.get("network") or "") == "disabled":
        lines.append("- **网络禁用**：不要尝试下载模型/依赖/数据。")
    if constraints.get("readonly_assets"):
        lines.append("- 输入资产只读：**不得修改**输入根里的任何文件"
                     "（宿主形态是工作根下的 `input/`，容器形态是另挂的只读目录）。")
    if constraints.get("readonly_skills"):
        lines.append("- 技能包只读：只读使用，不修改、不复制进产物目录。")
    return lines


def _outputs_block(spec: Any) -> "list[str]":
    task_id = str(getattr(spec, "task_id", ""))
    must = candidate_outputs(spec)
    lines = ["**你必须产出**（写进工作根）：", ""]
    lines += [f"- `{name}`" for name in must]
    extras = extra_candidate_outputs(task_id)
    if extras:
        lines += ["", "**运行器代跑产出**（你会被重跑，产物位置固定）：", ""]
        lines += [f"- `{name}`" for name in extras]
    lines += [
        "",
        "**运行器代写**（不要自己写；写了也不会被采信）：",
        "",
    ]
    lines += [f"- `{name}`" for name in HARNESS_OUTPUTS]
    return lines


def render_task_md(spec: Any, *, rep: int, seed: str,
                   input_relpaths: "Sequence[str]" = (),
                   budget_note: "str | None" = None) -> str:
    """渲染一份 ``TASK.md``（纯函数：同参同文，与臂无关）。

    ``seed`` 是**公开**种子（``pair_seed`` 的十六进制串，计划 §1.4.2）；
    隐藏变体种子在 ``private/``，不经过本函数（写进来就是泄漏）。
    """
    task_id = str(getattr(spec, "task_id", ""))
    if not task_id:
        raise PromptIsolationError("spec 缺 task_id，无法渲染作业说明")
    if type(rep) is not int or rep < 0:
        raise PromptIsolationError(f"rep 必须是非负整数：{rep!r}")
    if not isinstance(seed, str) or not seed:
        raise PromptIsolationError("seed 必须是非空字符串")

    objective = " ".join(str(getattr(spec, "objective", "") or "").split())
    title = _TITLE_BY_TASK.get(task_id, task_id)

    lines = [
        f"# {title}",
        "",
        f"> 本文件由运行器生成（schema `{PROMPT_SCHEMA}`），"
        "同一任务同一重复轮次的两条实验臂收到逐字节相同的说明。",
        f"> 任务：`{task_id}`　重复轮次：{rep}　公开种子：`{seed}`",
        "",
        "## 一、目标",
        "",
        objective or "（任务未提供目标描述）",
        "",
        "## 二、运行环境",
        "",
    ]
    lines += _runtime_block(spec)
    lines += ["", "## 三、输入", ""]
    if input_relpaths:
        lines.append("以下文件已在**只读输入面**就位（路径相对**输入根**）：")
        lines.append("")
        lines += [f"- `{rel}`" for rel in input_relpaths]
        lines += [
            "",
            "输入根的位置随运行形态而定、但**内容相同**：宿主形态下它是工作根下的"
            "`input/`；容器形态下是另挂的只读目录（见「入口与调用契约」）。"
            "你不必关心它在哪，只需要**读得到**。",
        ]
    else:
        lines.append("（本 Trial 无额外输入件。）")
    lines += ["", "## 四、交付物", ""]
    lines += _outputs_block(spec)
    lines += ["", "## 五、入口与调用契约", ""]
    lines += _invocation_preamble(spec)
    lines += _invocation_block(spec)
    lines += ["", "## 六、约束", ""]
    lines += _constraints_block(spec) or ["- 无额外约束。"]
    if budget_note:
        lines += ["", budget_note]
    acceptance = _acceptance_block(task_id)
    if acceptance:
        lines += ["", "## 七、验收口径（结构）", ""]
        lines += acceptance
    lines += ["",
              "## 八、交付纪律",
              "",
              "- 产物写完再结束；缺件即按无效处理（不是「部分得分」）。",
              "- 不要读取本说明之外的判据文件；判据不在你的可写/可读面内。",
              "- 所有产物必须由你的程序**真实计算**得出，不得写死常数冒充结果。",
              ""]
    return "\n".join(lines)


def task_md_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _assert_clean(text: str, spec: Any) -> None:
    hits = threshold_hits(text, spec=spec)
    if hits:
        raise PromptIsolationError(
            "TASK.md 出现判据数值 token："
            + "、".join(hits) + "（AC-P281-4-04）")
    lowered = text.lower()
    for needle in FORBIDDEN_ASCII:
        if needle in lowered:
            raise PromptIsolationError(
                f"TASK.md 出现 ASCII 旁证针 {needle!r}（计划 §8.6 步骤 3）")


def write_task_md(trial_dir: "str | Path", spec: Any, *, rep: int, seed: str,
                  input_relpaths: "Sequence[str]" = (),
                  budget_note: "str | None" = None) -> "tuple[Path, str]":
    """渲染 → 自检 → 原子写 ``<trial_dir>/TASK.md``（0600）；返回 ``(path, sha256)``。

    **先自检后落盘**：命中即抛，盘上不留半成品——否则失败的渲染会被当成
    「候选看过阈值的证据」留在证据包里，方向正好反了。
    """
    text = render_task_md(spec, rep=rep, seed=seed,
                          input_relpaths=input_relpaths,
                          budget_note=budget_note)
    _assert_clean(text, spec)
    base = Path(trial_dir)
    ensure_private_dir(base)
    path = base / TASK_MD_NAME
    atomic_write_text(path, text, mode=0o600)
    return path, task_md_sha256(text)
