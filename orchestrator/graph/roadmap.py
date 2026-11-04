"""多 Stage 路线图纯函数层（Phase 2.7 Task 5，AC-T5-1/AC-T5-2/AC-T5-4）。

设计（计划 §T5.3 D5-2）：本件**只做数据与纯函数**——``validate_roadmap`` /
``current_stage`` / ``advance`` / ``grants_for_stage``；无 I/O、无 langgraph
依赖、无 agentic 依赖（授予串语法的单一真值仍在 agentic 层的
``parse_grant_strings``，本件只做**收窄**判定），
故 100% 可单测，并能在 CLI ``--dry-run`` 下演练。

注：本文件刻意不写 agentic 包的**全限定导入名**——图侧「agentic 引用唯一
（组合根懒导入）」由 tests/milestone25 的文本扫描锁定，扫描不区分代码与
注释，docstring 里写全名会被算作第二个命中。

schema 名 ``simpraxis.roadmap.v1`` 沿用提纲命名，与历史**无继承关系**
（计划 §T5.3 锁内适配 1：Phase 2.5/2.6 从未交付 roadmap 状态机，本 Task 从零建）。

失败一律 fail-closed：空路线图 / 重复 stage_id / 空 tool_grants / 越界索引
均抛 ``RoadmapError``（ValueError 子类），绝不「尽力而为地继续」。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

ROADMAP_SCHEMA = "simpraxis.roadmap.v1"

# 授予串的形态前缀（与 agentic.wiring.parse_grant_strings 同一语法族；
# 本件只用来判定「子集 = 收窄」，语法合法性的唯一判据仍在 wiring）。
_GRANT_PREFIXES = ("run:", "write:", "probe:", "netprobe:", "expr:")
STAGE_STATUSES = ("pending", "running", "approved", "rejected")

# 裸工具名（与 wiring.TOOL_NAMES 同集合；本件不 import agentic——golden
# 子进程测试锁死「旧路径零 agentic import 面」，单测另行逐名对齐两者）。
_BARE_TOOL_NAMES = frozenset({
    "read_file", "list_dir", "grep", "write_file", "apply_patch",
    "run_cmd", "run_probe", "recall", "finish", "fail",
    "run_expr", "update_scratchpad",
})


class RoadmapError(ValueError):
    """路线图非法（schema/字段/索引越界）：fail-closed，不猜测意图。"""


@dataclass(frozen=True)
class Stage:
    stage_id: str
    title: str
    objective: str
    exit_ac: tuple[str, ...]         # 人类可读 + 机判 id 并列
    tool_grants: tuple[str, ...]     # 语法同 wiring.parse_grant_strings
    max_rounds: int = 8


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise RoadmapError(msg)


def _nonempty_str(value: Any, what: str) -> str:
    _require(isinstance(value, str) and value.strip() == value and value != "",
             f"{what} 必须是非空且无首尾空白的字符串：{value!r}")
    return value


def _str_tuple(value: Any, what: str) -> tuple[str, ...]:
    _require(isinstance(value, (list, tuple)) and bool(value),
             f"{what} 必须是非空数组：{value!r}")
    return tuple(_nonempty_str(item, f"{what} 的元素") for item in value)


def _grants_tuple(value: Any, what: str) -> tuple[str, ...]:
    grants = _str_tuple(value, what)
    for g in grants:
        # 轻量形态校验（语法合法性的单一真值在 wiring.parse_grant_strings）：
        # 裸名或已知前缀；未知前缀在此即拒，避免把错字带到容器里才炸。
        _require(g in _BARE_TOOL_NAMES or g.startswith(_GRANT_PREFIXES),
                 f"{what} 含未知形态的授予串：{g!r}")
    return grants


def stage_from_mapping(doc: Mapping[str, Any]) -> Stage:
    """单条 Stage 字典 → :class:`Stage`（state 里的路线图是 JSON 形态）。"""
    _require(isinstance(doc, Mapping), f"Stage 必须是映射：{doc!r}")
    extra = set(doc) - {"stage_id", "title", "objective", "exit_ac",
                        "tool_grants", "max_rounds"}
    _require(not extra, f"Stage 含未声明字段：{sorted(extra)}")
    max_rounds = doc.get("max_rounds", 8)
    _require(type(max_rounds) is int and max_rounds > 0,
             f"max_rounds 必须是正整数：{max_rounds!r}")
    return Stage(
        stage_id=_nonempty_str(doc.get("stage_id"), "stage_id"),
        title=_nonempty_str(doc.get("title"), "title"),
        objective=_nonempty_str(doc.get("objective"), "objective"),
        exit_ac=_str_tuple(doc.get("exit_ac"), "exit_ac"),
        tool_grants=_grants_tuple(doc.get("tool_grants"), "tool_grants"),
        max_rounds=max_rounds,
    )


def stage_to_mapping(stage: Stage) -> dict:
    """Stage → 可进 checkpoint 的纯 JSON 形态（tuple → list）。"""
    return {
        "stage_id": stage.stage_id,
        "title": stage.title,
        "objective": stage.objective,
        "exit_ac": list(stage.exit_ac),
        "tool_grants": list(stage.tool_grants),
        "max_rounds": stage.max_rounds,
    }


def validate_roadmap(doc: Mapping[str, Any]) -> tuple[Stage, ...]:
    """路线图文档 → 不可变 Stage 元组；任何结构问题即 ``RoadmapError``。"""
    _require(isinstance(doc, Mapping), f"路线图必须是映射：{doc!r}")
    _require(doc.get("schema") == ROADMAP_SCHEMA,
             f"路线图 schema 必须是 {ROADMAP_SCHEMA}，收到 {doc.get('schema')!r}")
    raw = doc.get("stages")
    _require(isinstance(raw, (list, tuple)) and bool(raw),
             "路线图 stages 必须是非空数组")
    extra = set(doc) - {"schema", "stages", "title", "notes"}
    _require(not extra, f"路线图含未声明字段：{sorted(extra)}")
    stages = tuple(stage_from_mapping(item) for item in raw)
    ids = [s.stage_id for s in stages]
    dup = sorted({i for i in ids if ids.count(i) > 1})
    _require(not dup, f"stage_id 重复：{dup}")
    return stages


def coerce_stages(value: "Iterable[Any] | None") -> tuple[Stage, ...]:
    """state 里的 ``stages``（JSON 形态或 Stage 形态）→ Stage 元组。"""
    if not value:
        return ()
    out = [item if isinstance(item, Stage) else stage_from_mapping(item)
           for item in value]
    ids = [s.stage_id for s in out]
    dup = sorted({i for i in ids if ids.count(i) > 1})
    _require(not dup, f"stage_id 重复：{dup}")
    return tuple(out)


def current_stage(stages: Sequence[Stage], index: int) -> Stage:
    """取第 ``index`` 个 Stage；越界即 ``RoadmapError``（不返回 None）。"""
    _require(isinstance(index, int) and not isinstance(index, bool),
             f"stage_index 必须是整数：{index!r}")
    _require(0 <= index < len(stages),
             f"stage_index 越界：{index}（共 {len(stages)} 个 Stage）")
    return stages[index]


def advance(stages: Sequence[Stage], index: int) -> int:
    """当前 Stage 通过后的下一个索引；已是最后一个即 ``StopIteration`` 语义。"""
    current_stage(stages, index)          # 先校验 index 合法
    if index + 1 >= len(stages):
        raise StopIteration("路线图已到最后一个 Stage")
    return index + 1


# ---------------------------------------------------------------------------
# 授权收窄（AC-T5-2）
# ---------------------------------------------------------------------------

def _covered(grant: str, ceiling: Sequence[str]) -> bool:
    """授予串 ``grant`` 是否被上限集 ``ceiling`` 覆盖（语义同 wiring 的隐含授予）。"""
    if grant in ceiling:
        return True
    # 隐含授予的裸名（parse_grant_strings 同源）：write: → write_file+apply_patch；
    # run: → run_cmd；probe:/netprobe: → run_probe；expr: → run_expr。
    if grant in ("write_file", "apply_patch"):
        return any(c.startswith("write:") for c in ceiling)
    if grant == "run_cmd":
        return any(c.startswith("run:") for c in ceiling)
    if grant == "run_probe":
        return any(c.startswith(("probe:", "netprobe:")) for c in ceiling)
    if grant == "run_expr":
        return any(c.startswith("expr:") for c in ceiling)
    if grant.startswith("run:"):
        argv = grant.split(":")[1:]
        for c in ceiling:
            if not c.startswith("run:"):
                continue
            c_argv = c.split(":")[1:]
            if c_argv and argv[:len(c_argv)] == c_argv:
                return True            # 上限是 argv 前缀 ⇒ 覆盖更长的子集
        return False
    if grant.startswith("write:"):
        rel = grant[len("write:"):]
        for c in ceiling:
            if not c.startswith("write:"):
                continue
            c_rel = c[len("write:"):]
            if c_rel == "" or c_rel == rel or rel.startswith(c_rel + "/"):
                return True            # ""=整个 workdir；前缀覆盖子路径
        return False
    if grant.startswith("expr:"):
        mods = {m for m in grant[len("expr:"):].split(",") if m}
        for c in ceiling:
            if c.startswith("expr:") and mods <= {
                    m for m in c[len("expr:"):].split(",") if m}:
                return True
        return False
    return False


def grants_for_stage(stages: Sequence[Stage], index: int,
                     global_grants: "Iterable[str] | None") -> tuple[str, ...]:
    """Stage 的授予 = 自身声明 ∩ 全局上限（**只收窄，绝不扩张**）。

    计划 §T5.3 D5-4：全局 grants 只是上限；Stage 未声明的一律不授予，
    Stage 声明了但全局未勾选的一律**丢弃**（Gate 1 未勾选 = 不授权）。
    返回顺序沿用 Stage 声明顺序（稳定，便于快照比对）。
    """
    stage = current_stage(stages, index)
    ceiling = tuple(str(g) for g in (global_grants or ()))
    return tuple(g for g in stage.tool_grants if _covered(g, ceiling))


def stage_view(stage: Stage, grants: Sequence[str]) -> dict:
    """state['stage_frame'] 的只读视图（节点写，agent_run 读）。"""
    return {
        "stage_id": stage.stage_id,
        "title": stage.title,
        "objective": stage.objective,
        "exit_ac": list(stage.exit_ac),
        "tool_grants": list(grants),
        "max_rounds": stage.max_rounds,
    }


def stage_statuses(stages: Sequence[Stage]) -> dict:
    """初始 stage_status：全部 pending。"""
    return {s.stage_id: "pending" for s in stages}


def narrow_spec_grants(spec: Any, state: Mapping[str, Any]) -> Any:
    """图接缝用：有 ``stage_frame`` 时按 Stage 授予覆盖 RunSpec.tool_grants。

    计划 §T5.4-C：``node_agent_run`` 构造 RunSpec 时用 ``stage_frame.tool_grants``
    覆盖。图侧构造 RunSpec 的唯一落点是 ``agent_runner.runner.agent_run_seam``，
    故覆盖在该处调用本函数完成（不改 ``RunSpec.agentic`` 默认值——BR-2 冻结）。

    无 ``stage_frame``（旧路径/未给路线图）时**原样返回同一对象**，保证旧路径
    逐字节不变。
    """
    frame = state.get("stage_frame") or {}
    if not isinstance(frame, Mapping) or not frame.get("stage_id"):
        return spec
    grants = tuple(frame.get("tool_grants") or ())
    from dataclasses import replace
    return replace(spec, tool_grants=grants)
