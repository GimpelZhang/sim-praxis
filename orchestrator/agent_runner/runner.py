#!/usr/bin/env python3
"""Task4 agent_runner：真实脚本执行 + coding_debug 多轮补救（计划 L496-507）。

契约要点：
- 每 run 最多 5 轮 coding_debug（D10 v1.1）；第 5 次调用后仍败 → 写
  FAILURE.json 带完整轨迹，run 结案转人工；结构上不可能发出第 6 次调用。
- 检索 troubleshooting top_k=6，取行字段 **text**（非幻影字段 solution_steps）
  + evidence_refs，prompt 以 [来源: kb_relpath#section_path] 列注。
- Gate2 打回意见与 traceback 一起进 prompt，意见标「最高优先级」。
- 补丁安全：只允许写 workdir 下运行目录；禁改仓库 tracked 文件、
  stage_models.yaml、00_env.sh、~/access；越界补丁拒绝且不发起重试。
- 视觉禁用：runner 构造的消息只含纯文本（测试断言无 image 类型）。
- coding_round 计数仅在单次 run 内有效；Gate2 reject 计数在图侧独立维护。
"""
from __future__ import annotations

import ast
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any, Callable, Optional

from memory_flywheel.recall import ProductionRecall, prompt_embedded_text
from memory_flywheel.vector_engine import VectorEngine
from orchestrator.common.callcache import CallReplay, request_fingerprint
from orchestrator.common.clock import utc_now_iso_z
from orchestrator.common.fsutil import atomic_write_text, chmod_keep_user_exec
from orchestrator.gateway.quota_ledger import (
    billing_context,
    phase1_root,
    set_current_evidence,
)
from orchestrator.gateway.router import (
    GatewayPausedError,
    ModelGateway,
    redact,
)

STAGE = "coding_debug"
TOP_K = 6
MAX_CODING_ROUNDS = 5          # D10 v1.1：每 run 最多 5 次 coding_debug
MAX_TOKENS = 8192
TEMPERATURE = 0.1
# 锁内适配（计划 v1.2 外，seq42-47 拆块出生，用户 2026-10-20 裁决）：
# Task6 V2 出生拆 4 个顺序小编排调用；网关 call 的 timeout 旧路径隐式吃
# router 默认 120，现经 RunSpec 显式化（默认 120，行为不变）。
DEFAULT_CODING_TIMEOUT_S = 120
CHUNK_MIN_CODE_CHARS = 800          # 单块 code 下限（机器校验，短于即整轮失败）
CHUNK_MIN_ASSEMBLED_CHARS = 12000   # 4 块拼接总长下限，防模型逃单块/碎块
CHUNK_MAX_CODE_CHARS = 20000  # 单块 code 硬上限（seq48 24000 首发；2026-10-21 随输出顶 16384→32768 收紧）
# 32768 output token 仍要容纳 JSON 结构与转义，20000 字符留余量；用户选项 A（seq56 探针，见 task6 注释），超长即整轮失败。
TB_TAIL_CHARS = 4096           # traceback 尾部 ≤4KB
SNIPPET_CTX_LINES = 8
SNIPPET_TOTAL_CHARS = 4096
RUN_TIMEOUT_S = 1800
FAILURE_JSON = "FAILURE.json"

# 禁改的字面文件名（任何目录层级都拒）
FORBIDDEN_BASENAMES = frozenset({"stage_models.yaml", "00_env.sh"})


class CodingLimitError(GatewayPausedError):
    """5 轮用尽/失败 run 被再次进入：等价硬停，不发网（计划 L505 ③）。

    父类 GatewayPausedError.__init__ 绑定 router 长熔断的 pause-state dict；
    这里只复用「可被同一硬停子句捕获、绝不发网」的语义，故越过它直接走
    RuntimeError 初始化，isinstance(x, GatewayPausedError) 仍然成立。
    """

    def __init__(self, message: str):
        RuntimeError.__init__(self, message)


class PatchSafetyError(RuntimeError):
    """补丁越界或无法安全应用：拒绝且不重试（计划 L505 ②）。"""


class ChunkBirthError(RuntimeError):
    """拆块出生轮本地校验失败：整轮作废、不半成品落盘，主循环进入下一轮。

    锁内适配（计划 v1.2 外，seq42-47 拆块出生，用户 2026-10-20 裁决）：
    与 CodingLimitError 同属 RuntimeError 族的「不触网失败信号」——失败发生
    在本地 JSON/ast 校验，不产生新的网关调用。区别在生命周期：本异常在 run
    循环内被捕获后，走既有「失败→再执行→下一 coding 轮」机制，仍受
    RunSpec.max_coding_rounds 约束；它不是 CodingLimitError 那种结案硬停。
    消息必须含块号与机器原因（下一轮 prompt 的失败反馈段原样带上）。
    """


@dataclass(frozen=True)
class ChunkSpec:
    """拆块出生规格。锁内适配（计划 v1.2 外，seq42-47 拆块出生，
    用户 2026-10-20 裁决）：chunk_count 固定来自工厂（Task6=4），禁止运行时
    由模型自报块数（防模型逃单块）。

    chunk_boundaries 是工单四字段外的最小增补（同条锁内适配）：runner 在
    底层（不得反向 import orchestrator.graph），渲染「本块边界/行数预算」
    所需文本只能经规格从工厂流入，故与 chunk_titles 同形并列，构造即校长度。
    """

    target_path: str
    chunk_count: int                                  # Task6=4
    chunk_titles: tuple[str, ...]                     # 长度=chunk_count
    per_call_sections: tuple[tuple[str, str], ...]    # 合同/提案/骨架，每 call 都带
    chunk_boundaries: tuple[str, ...]                 # 每块允许章节+行数预算文案

    def __post_init__(self) -> None:
        if not isinstance(self.target_path, str) or not self.target_path.strip():
            raise ValueError("ChunkSpec.target_path 必须是非空字符串")
        if not isinstance(self.chunk_count, int) \
                or isinstance(self.chunk_count, bool) or self.chunk_count < 1:
            raise ValueError(f"ChunkSpec.chunk_count 非法：{self.chunk_count!r}")
        for name in ("chunk_titles", "chunk_boundaries"):
            val = getattr(self, name)
            if (not isinstance(val, tuple) or len(val) != self.chunk_count
                    or not all(isinstance(x, str) and x.strip() for x in val)):
                raise ValueError(
                    f"ChunkSpec.{name} 必须是 {self.chunk_count} 个非空字符串")
        if not isinstance(self.per_call_sections, tuple) \
                or not all(
                    isinstance(s, tuple) and len(s) == 2
                    and all(isinstance(x, str) for x in s)
                    for s in self.per_call_sections):
            raise ValueError("ChunkSpec.per_call_sections 必须是 (标题, 正文) 元组")


@dataclass(frozen=True)
class ChunkPromptContext:
    """单次拆块调用的渲染输入（runner 循环内按块构造，不进任何持久状态）。"""

    index: int
    total: int
    target_path: str
    titles: tuple[str, ...]
    boundaries: tuple[str, ...]
    previous_code: str = ""
    round_feedback: str = ""


@dataclass
class RunSpec:
    thread_id: str
    workdir: Path
    cmd: list[str]
    telemetry_relpath: str                 # 成功产物（相对 workdir）
    batch_id: str
    # L-1：本 run 在 thread 内的稳定序号（与证据文件名 r{run_idx} 同一约定，
    # 工厂用 len(agent_runs)+1 预占、接缝用 len(agent_runs) 复核，二者同号）。
    # 仅当显式给出时启用调用级重放缓存；None（如 Task4 直连）不查不写缓存。
    run_idx: Optional[int] = None
    billing_task: int = 4                  # Task7/8 联调时传 7/8（锁内适配 L506）
    gate2_notes: Optional[str] = None      # 图侧 gate2_rejected 事件注入
    env_extra: dict[str, str] = field(default_factory=dict)
    run_timeout_s: int = RUN_TIMEOUT_S
    task_title: str = ""
    # Task7：真实 rollout 视频产物（相对 workdir）。存在才回填视频四字段；
    # duration_ms 优先读场景 manifest（n_frames×frame_dt_s），缺失再 imageio 解码。
    video_relpath: Optional[str] = None
    cover_relpath: Optional[str] = None
    manifest_relpath: Optional[str] = None
    # Phase 2 Task2：每 run coding_debug 上限可配置；默认 MAX_CODING_ROUNDS(=5)
    # 与 Phase 1 行为逐值一致。Task6 升阶 run 工厂将显式传 3（run 级收窄，
    # 见计划 L193），本任务不改 Task7/8 既有工厂。
    max_coding_rounds: int = MAX_CODING_ROUNDS
    # 锁内适配（计划 v1.2 外，seq42-47 拆块出生，用户 2026-10-20 裁决）：
    # 以下四字段默认值保证 Task7/8 与既有流程字节级旧行为——coding 调用仍
    # 8192 tokens/120s（120 与 router.call 默认值同值，旧路径只是隐式吃默认），
    # 无额外 prompt 段、无拆块。Task6 V2 出生轮显式传 32768/600 + ChunkSpec。
    coding_max_tokens: int = MAX_TOKENS
    coding_timeout_s: int = DEFAULT_CODING_TIMEOUT_S
    extra_prompt_sections: tuple[tuple[str, str], ...] = ()
    chunked_birth: "ChunkSpec | None" = None


@dataclass
class _ExecResult:
    rc: int
    stdout: str
    stderr: str


def _default_evidence_root(thread_id: str) -> Path:
    d = phase1_root() / "task4_runner" / thread_id
    d.mkdir(parents=True, exist_ok=True)
    return d


SubprocessRunner = Callable[[list[str], Path, float, dict], _ExecResult]


def _default_subprocess(cmd, workdir, timeout_s, env_extra) -> _ExecResult:
    env = dict(os.environ)
    env.update(env_extra)
    proc = subprocess.run(
        cmd, cwd=str(workdir), env=env, capture_output=True, text=True,
        timeout=timeout_s)
    return _ExecResult(proc.returncode, proc.stdout or "", proc.stderr or "")


# ---------------------------------------------------------------------------
# traceback 取证（只准读 workdir 内的帧文件）
# ---------------------------------------------------------------------------

_FRAME_RE = re.compile(r'File "(?P<path>[^"]+)", line (?P<line>\d+)')


def _collect_snippets(workdir: Path, text: str) -> list[dict]:
    workdir_r = workdir.resolve()
    seen: set[tuple[str, int]] = set()
    out: list[dict] = []
    total = 0
    for m in _FRAME_RE.finditer(text):
        fp = Path(m.group("path"))
        try:
            resolved = fp.resolve(strict=False)
            resolved.relative_to(workdir_r)
        except (OSError, ValueError):
            continue  # stdlib / site-packages / 仓库外：禁读
        key = (str(resolved), int(m.group("line")))
        if key in seen:
            continue
        seen.add(key)
        try:
            lines = resolved.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        ln = int(m.group("line"))
        lo, hi = max(0, ln - 1 - SNIPPET_CTX_LINES), ln + SNIPPET_CTX_LINES
        block = "\n".join(f"{i+1:5d}| {lines[i]}" for i in range(lo, min(hi, len(lines))))
        if total + len(block) > SNIPPET_TOTAL_CHARS:
            break
        total += len(block)
        out.append({"file": str(resolved.relative_to(workdir_r)),
                    "line": ln, "snippet": block})
    return out


# ---------------------------------------------------------------------------
# 补丁解析与安全应用
# ---------------------------------------------------------------------------

def _validate_target(path: Path, workdir: Path) -> Path:
    name = path.name
    if name in FORBIDDEN_BASENAMES:
        raise PatchSafetyError(f"补丁企图修改禁改文件：{name}")
    if "access" in path.parts:
        raise PatchSafetyError(f"补丁企图进入 ~/access 区域：{path}")
    workdir_r = workdir.resolve()
    if path.is_absolute():
        target = path.resolve(strict=False)
    else:
        target = (workdir_r / path).resolve(strict=False)
    try:
        target.relative_to(workdir_r)
    except ValueError:
        raise PatchSafetyError(
            f"补丁目标越出 workdir：{path} -> {target}（workdir={workdir_r}）")
    # 拒绝 symlink 逃逸（路径上任何一环是指向 workdir 外的链接）
    cur = workdir_r
    for part in target.relative_to(workdir_r).parts:
        cur = cur / part
        if cur.is_symlink():
            real = cur.resolve(strict=False)
            try:
                real.relative_to(workdir_r)
            except ValueError:
                raise PatchSafetyError(f"补丁经符号链接逃逸 workdir：{cur} -> {real}")
    return target


_HUNK_RE = re.compile(
    r"^@@ -(?P<o>\d+)(?:,\d+)? \+(?P<n>\d+)(?:,\d+)? @@", re.M)


def _apply_unified_diff(workdir: Path, diff_text: str) -> list[str]:
    """最小 unified diff 应用器（git 风格头）；每段旧块必须唯一命中。"""
    files = re.split(r"^diff --git .+$", diff_text, flags=re.M)
    applied: list[str] = []
    for section in files:
        if not section.strip():
            continue
        m_old = re.search(r"^--- (?:a/)?(.+)$", section, re.M)
        m_new = re.search(r"^\+\+\+ (?:b/)?(.+)$", section, re.M)
        if not m_new:
            raise PatchSafetyError("unified diff 缺 +++ 头")
        rel = m_new.group(1).strip()
        target = _validate_target(Path(rel), workdir)
        body_start = section.find("\n@@")
        body = section[body_start + 1:] if body_start >= 0 else ""
        old_lines = target.read_text(encoding="utf-8").splitlines(keepends=True) \
            if target.exists() else []
        # 按 hunk 顺序定位替换
        result: list[str] = []
        cursor = 0
        hunks = list(_HUNK_RE.finditer(body))
        if not hunks:
            raise PatchSafetyError("unified diff 无 hunk")
        for hi, hm in enumerate(hunks):
            hunk_end = hunks[hi + 1].start() if hi + 1 < len(hunks) else len(body)
            hunk_lines = body[hm.start():hunk_end].splitlines()
            old_block, new_block = [], []
            for hl in hunk_lines[1:]:
                if hl.startswith("\\"):
                    continue
                if hl.startswith("-"):
                    old_block.append(hl[1:])
                elif hl.startswith("+"):
                    new_block.append(hl[1:])
                else:
                    ctx = hl[1:] if hl[:1] in (" ", "") else hl
                    old_block.append(ctx)
                    new_block.append(ctx)
            window = [l.rstrip("\n") for l in old_lines[cursor:]]
            old_s = [l.rstrip("\n") for l in old_block]
            hits = [i for i in range(len(window) - len(old_s) + 1)
                    if window[i:i + len(old_s)] == old_s]
            if len(hits) != 1:
                raise PatchSafetyError(
                    f"hunk 上下文命中 {len(hits)} 次（需恰好 1 次）：{rel}")
            at = cursor + hits[0]
            result.extend(old_lines[cursor:at])
            result.extend("\n".join(new_block).splitlines(keepends=True))
            cursor = at + len(old_s)
        result.extend(old_lines[cursor:])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("".join(result), encoding="utf-8")
        # L-11：收敛到属主 rw，但保留补丁目标文件原有的属主执行位（0700 脚本）。
        chmod_keep_user_exec(target)
        applied.append(str(target.relative_to(workdir.resolve())))
    return applied


def _apply_structured_patch(workdir: Path, patch: dict) -> str:
    rel = patch.get("path")
    if not isinstance(rel, str) or not rel:
        raise PatchSafetyError("补丁缺 path 字段")
    target = _validate_target(Path(rel), workdir)
    old, new = patch.get("old"), patch.get("new")
    if not isinstance(old, str) or not isinstance(new, str):
        raise PatchSafetyError("补丁 old/new 必须为字符串")
    text = target.read_text(encoding="utf-8") if target.exists() else ""
    if text.count(old) != 1:
        raise PatchSafetyError(
            f"old 块命中 {text.count(old)} 次（需恰好 1 次）：{rel}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text.replace(old, new, 1), encoding="utf-8")
    # L-11：0700/0755 可执行脚本补丁后保留属主执行位，不再被抹成 0600。
    chmod_keep_user_exec(target)
    return str(target.relative_to(workdir.resolve()))


def _parse_and_apply(workdir: Path, raw: str) -> list[str]:
    obj = _json_object(raw)
    if not isinstance(obj, dict):
        raise PatchSafetyError("coding_debug 返回非 JSON 对象")
    if "unified_diff" in obj:
        diff = obj["unified_diff"]
        if not isinstance(diff, str) or not diff.strip():
            raise PatchSafetyError("unified_diff 为空")
        return _apply_unified_diff(workdir, diff)
    patches = obj.get("patches")
    if not isinstance(patches, list) or not patches:
        raise PatchSafetyError("返回中既无 unified_diff 也无 patches[]")
    applied = []
    for p in patches:
        if not isinstance(p, dict):
            raise PatchSafetyError("patches 元素必须是对象")
        applied.append(_apply_structured_patch(workdir, p))
    return applied


def _json_object(raw: str) -> Any:
    """容忍 ```json 围栏（与 Task2 parse_json_object 同风格的最小提取）。"""
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start:end + 1])
        raise


# ---------------------------------------------------------------------------
# 拆块出生：逐块 JSON 机器校验 + 拼接整体校验（锁内适配，见 ChunkSpec）
# ---------------------------------------------------------------------------

def _extract_chunk(raw: str, *, target: str, index: int, total: int) -> str:
    """解析单块响应并机器校验；任何不符抛 ChunkBirthError（块号+机器原因）。"""
    try:
        obj = _json_object(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ChunkBirthError(
            f"块 {index} 返回非合法 JSON：{type(exc).__name__}: "
            f"{str(exc)[:160]}") from None
    if not isinstance(obj, dict):
        raise ChunkBirthError(
            f"块 {index} 顶层不是 JSON 对象：{type(obj).__name__}")
    chk = obj.get("chunk")
    if not isinstance(chk, dict):
        raise ChunkBirthError(
            f"块 {index} 缺 chunk 对象（收到 {type(chk).__name__}）")
    if chk.get("path") != target:
        raise ChunkBirthError(
            f"块 {index} path 不符：收到 {chk.get('path')!r}，应为 {target!r}")
    # bool 是 int 子类，index/total 用 type() is int 严口径（与缓存 usage 同纪律）
    if type(chk.get("index")) is not int or chk.get("index") != index:
        raise ChunkBirthError(
            f"块 {index} index 不符：收到 {chk.get('index')!r}，应为 {index}")
    if type(chk.get("total")) is not int or chk.get("total") != total:
        raise ChunkBirthError(
            f"块 {index} total 不符：收到 {chk.get('total')!r}，应为 {total}")
    code = chk.get("code")
    if not isinstance(code, str) or not code:
        raise ChunkBirthError(f"块 {index} code 必须是非空字符串")
    if code.strip().startswith("```"):
        raise ChunkBirthError(
            f"块 {index} code 去首尾空白后是 markdown 围栏（禁止）")
    if len(code) < CHUNK_MIN_CODE_CHARS:
        raise ChunkBirthError(
            f"块 {index} code 长度 {len(code)} < {CHUNK_MIN_CODE_CHARS} chars")
    if len(code) > CHUNK_MAX_CODE_CHARS:
        raise ChunkBirthError(
            f"块 {index} code 长度 {len(code)} > {CHUNK_MAX_CODE_CHARS} chars"
            "（输出预算超限；精简注释、禁止复述合同/骨架/提案）")
    return code


def _validate_assembled_chunks(code: str) -> None:
    """拼接整体校验：ast 可解析、顶层无重复 def/class 名、总长 ≥ 下限。"""
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        raise ChunkBirthError(
            f"拼接源码 ast.parse 失败（行 {exc.lineno} 列 {exc.offset}）："
            f"{type(exc).__name__}: {str(exc)[:160]}") from None
    seen: set[str] = set()
    dups: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name in seen and node.name not in dups:
                dups.append(node.name)
            seen.add(node.name)
    if dups:
        raise ChunkBirthError(
            "拼接源码顶层重复 def/class 名（硬失败）：" + ", ".join(dups))
    if len(code) < CHUNK_MIN_ASSEMBLED_CHARS:
        raise ChunkBirthError(
            f"拼接总长 {len(code)} < {CHUNK_MIN_ASSEMBLED_CHARS} chars"
            "（防碎块/逃单块）")


# ---------------------------------------------------------------------------
# prompt
# ---------------------------------------------------------------------------

def _kb_block(hits: list[dict]) -> str:
    if not hits:
        return "（troubleshooting 检索 0 行；不得据此编造 KB 引用）"
    lines = []
    for i, h in enumerate(hits, 1):
        src = f"{h.get('kb_relpath') or '?'}#{h.get('section_path') or ''}"
        refs = h.get("evidence_refs") or []
        ref_str = (f" | evidence_refs: {', '.join(map(str, refs))}"
                   if refs else "")
        lines.append(f"[S{i} 来源: {src}{ref_str}]\n"
                     f"{(h.get('text') or '').strip()}")
    return "\n\n".join(lines)


def _learning_block(learning_hits: "list[dict] | None") -> str:
    """【实战演化避坑】段（Phase 2 Task1）：只渲染学习表真实行，逐字引用。

    空/None → ""（调用方整段不拼，0 命中时 prompt 不出现避坑标题；
    learning_hits=None 时消息与 Phase 1 一致，旧测试零改动）。补丁片段只能
    来自这些真实行的 text（含修复动作/人工意见原文），不得合成 KB 引用。
    """
    if not learning_hits:
        return ""
    lines = ["以下条目逐字摘自历史真实运行学习表 phase1_learnings，必须作为"
             "避坑约束对照；补丁只能引用这些真实条目，不得编造其中不存在的"
             "修复或 KB 引用："]
    for i, h in enumerate(learning_hits, 1):
        lines.append(
            f"[L{i} 来源 learning_id={h.get('id', '?')} "
            f"run_id={h.get('run_id', '?')} "
            f"gate_source={h.get('gate_source', '?')} "
            f"rung={h.get('rung', '?')}]\n{str(h.get('text', ''))}")
    return "\n\n".join(lines)


def _chunk_section(ctx: "ChunkPromptContext") -> str:
    """渲染《拆块交付（第 i+1/n 块）》段（锁内适配，见 ChunkSpec 注释）。

    锁内适配（计划 v1.2 外，seq42-47 拆块出生，用户 2026-10-20 裁决）：
    清单（编号+标题+行数预算/边界）、本块边界、前序块逐字定稿围栏、上一轮
    机器失败反馈（仅该轮块 0）、严格 chunk JSON 交付格式五部分。
    """
    i, n = ctx.index, ctx.total
    lines = [f"## 拆块交付（第 {i + 1}/{n} 块）", "",
             f"目标文件分 {n} 块顺序出生，你正在交付第 {i + 1}/{n} 块；"
             "本次只允许产出这一块。", "",
             "### 交付块清单（编号、标题、行数预算与边界）"]
    for j, title in enumerate(ctx.titles):
        lines.append(f"{j + 1}. 《{title}》")
        lines.append(ctx.boundaries[j])
    lines += [
        "",
        "### 本块边界（只允许写的章节）",
        f"你本次只能产出第 {i + 1}/{n} 块《{ctx.titles[i]}》："
        f"{ctx.boundaries[i]}",
        "严禁在本块 code 中写入任何其他块的章节，也不得重复前序已定稿内容。",
    ]
    if ctx.previous_code:
        lines += [
            "",
            "### 前序已定稿块（逐字拼接文本，已最终定稿，禁止重复/改动）",
            f"以下是前 {i} 块拼接后的逐字定稿，你的 code 会被直接拼在其后；"
            "拼接结果必须是合法 Python：不得重复其中 import/常量/函数，"
            "不得改动或重排其中任何字符。",
            "```python",
            ctx.previous_code,
            "```",
        ]
    if ctx.round_feedback:
        lines += [
            "",
            "### 上一轮拆块交付失败反馈（机器原因，本轮必须避免）",
            ctx.round_feedback,
        ]
    lines += [
        "",
        "### 交付格式（严格 JSON）",
        "只允许输出一个 JSON 对象，JSON 外不得有任何文字、解释或 markdown 围栏：",
        '{"chunk":{"path":"' + ctx.target_path + '","index":' + str(i)
        + ',"total":' + str(n) + ',"code":"<本块 Python 原文>"}}',
        f'- path 必须逐字等于 "{ctx.target_path}"；index 必须是 {i}；'
        f"total 必须是 {n}；",
        "- code 只含本块 Python 原文：无 markdown 围栏、无解释；非空且"
        f"不少于 {CHUNK_MIN_CODE_CHARS} 字符；",
        f"- code 最长不超过 {CHUNK_MAX_CODE_CHARS} 字符（含注释；32768 output "
        "token 还要容纳 JSON 结构与转义）；超限必然在闭合前被截断、整轮失败。"
        "注释精简，禁止复述合同/提案/骨架原文。",
        f"- 全部 {n} 块拼接后须为完整可运行文件（总长不少于 "
        f"{CHUNK_MIN_ASSEMBLED_CHARS} 字符），块边界不得重叠。",
        "",
    ]
    return "\n".join(lines)


def _build_messages(*, gate2_notes: Optional[str], traceback_tail: str,
                    snippets: list[dict], hits: list[dict],
                    cmd: list[str],
                    learning_hits: "list[dict] | None" = None,
                    # 锁内适配（计划 v1.2 外，seq42-47 拆块出生，用户
                    # 2026-10-20 裁决）：两新参 None/空时输出与旧版逐字一致。
                    extra_sections: "tuple[tuple[str, str], ...] | None" = None,
                    chunk_context: "ChunkPromptContext | None" = None,
                    ) -> list[dict[str, str]]:
    system = (
        "你是具身智能仿真工具链的排障 coding agent。只允许输出严格 JSON："
        '{"patches":[{"path":"相对运行工作目录的路径","old":"原文（须唯一）",'
        '"new":"替换文"}]}，或 {"unified_diff":"git 风格 diff"}。'
        "补丁只能改本次运行工作目录内的文件；禁止改配置、凭据、仓库源码。"
        "不要输出解释、不要输出 image。")
    notes_block = ""
    if gate2_notes and gate2_notes.strip():
        notes_block = (
            "## 人工评审意见（必须逐条响应，最高优先级）\n"
            f"{gate2_notes.strip()}\n")
    snippet_block = "\n\n".join(
        f"### {s['file']}:{s['line']}\n{s['snippet']}" for s in snippets) \
        or "（traceback 帧均在工作目录外，无源片段可读）"
    # Phase 2 Task1：避坑段仅在 learning 路有真实命中时出现，置于 kb 段之后；
    # 0 命中不拼标题（禁止据此编造 fix/KB 引用）。
    learning_section = ""
    learning_text = _learning_block(learning_hits)
    if learning_text:
        learning_section = "\n\n## 实战演化避坑（phase1_learnings 真实行，逐字引用）\n" \
            + learning_text
    # 锁内适配（计划 v1.2 外，seq42-47 拆块出生，用户 2026-10-20 裁决）：
    # extra 段位于 notes 之后、失败命令之前；chunk 段紧随其后。两参数为空时
    # 两个 block 都是 ""，拼接结果与旧模板逐字一致（字符级回归测试锁定）。
    extra_block = ""
    if extra_sections:
        for title, body in extra_sections:
            extra_block += f"## {title}\n{body}\n\n"
    chunk_block = _chunk_section(chunk_context) + "\n" if chunk_context else ""
    user = (
        f"{notes_block}{extra_block}{chunk_block}"
        "## 失败命令\n" + " ".join(cmd) + "\n\n"
        "## traceback / 日志尾部\n" + traceback_tail + "\n\n"
        "## 相关源文件片段（仅限运行工作目录）\n" + snippet_block + "\n\n"
        "## troubleshooting 知识库命中（namespaces=troubleshooting, top_k=6）\n"
        + _kb_block(hits) + learning_section)
    return [{"role": "system", "content": system},
            {"role": "user", "content": user}]


def _tb_tail(exec_result: _ExecResult) -> str:
    blob = (exec_result.stderr or "") + "\n" + (exec_result.stdout or "")
    return blob[-TB_TAIL_CHARS:]


# ---------------------------------------------------------------------------
# 主类
# ---------------------------------------------------------------------------

class AgentRunner:
    def __init__(self, gateway: ModelGateway, vector_engine: VectorEngine, *,
                 evidence_root: "Path | None" = None,
                 subprocess_runner: "SubprocessRunner | None" = None,
                 evidence_writer: "Callable | None" = None,
                 call_cache: "CallReplay | None" = None,
                 recall: "ProductionRecall | None" = None):
        self.gateway = gateway
        self.vector_engine = vector_engine
        self._evidence_root = evidence_root
        self._run = subprocess_runner or _default_subprocess
        self._write_evidence = evidence_writer  # 测试注入：(root, stem, kind, payload)->ref
        # Phase 2 Task1：None=Phase 1 行为（revert 单文件回滚）；注入后启用
        # learning 双路召回 + 采纳遥测（coding 站，site="coding"）。
        self.recall = recall
        # L-1：None=调用方未注入；默认目录 phase1_root()/.call_cache 延迟到
        # 首用（且 spec.run_idx 非 None）才建，避免 Task4 直连在 /mnt 留件。
        self._call_cache: "Optional[CallReplay]" = call_cache

    def _replay_cache(self, spec: "RunSpec") -> "Optional[CallReplay]":
        """L-1：仅 run_idx 显式给出时启用（稳定 key coding_{t}_r{idx}_round{n}
        的前提）；默认缓存目录延迟创建。run_idx=None 一律不查不写。"""
        if spec.run_idx is None:
            return None
        if self._call_cache is None:
            self._call_cache = CallReplay(phase1_root() / ".call_cache")
        return self._call_cache

    @staticmethod
    def _usage_ints(usage: Any) -> "Optional[tuple[int, int]]":
        """真实网关返回两个非负 int；fake/无计量网关可能给 (None, None)——
        此类结果不入重放缓存（CallReplay.put 不接受非 int）。"""
        if (isinstance(usage, (list, tuple)) and len(usage) == 2
                and all(isinstance(v, int) and not isinstance(v, bool)
                        and v >= 0 for v in usage)):
            return int(usage[0]), int(usage[1])
        return None

    def _root(self, spec: RunSpec) -> Path:
        if self._evidence_root is not None:
            d = self._evidence_root / spec.thread_id
        else:
            d = _default_evidence_root(spec.thread_id)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _evidence(self, root: Path, stem: str, kind: str, payload: Any) -> str:
        if self._write_evidence is not None:
            return self._write_evidence(root, stem, kind, payload)
        p = root / f"{stem}.{kind}.json"
        # S-6：write→chmod 收口 fsutil 原子写（mkstemp 0600→fsync→replace）。
        atomic_write_text(p, json.dumps(payload, ensure_ascii=False, indent=2))
        return f"phase1/task4_runner/{root.name}/{p.name}"

    @staticmethod
    def _video_artifacts(workdir: Path, spec: "RunSpec") \
            -> "tuple[Optional[str], Optional[int], Optional[str]]":
        """Task7：从 workdir 真实产物回填视频四字段（不存在即 None，不合成）。

        duration_ms 优先取场景 manifest 的 n_frames×frame_dt_s（免解码）；
        manifest 缺失再用 imageio 读 fps 并逐帧计数（venv pin 2.36.1）。
        intranet_url 恒 None：Gate2 先走原生视频 ≤28MB 直传，全失败才由
        feishu video 阶梯自行降级，runner 不编造内网地址。
        """
        if not spec.video_relpath:
            return None, None, None
        video = (workdir / spec.video_relpath)
        if not video.is_file():
            return None, None, None
        duration_ms: "Optional[int]" = None
        if spec.manifest_relpath:
            mf = workdir / spec.manifest_relpath
            if mf.is_file():
                try:
                    meta = json.loads(mf.read_text(encoding="utf-8"))
                    n_frames = meta.get("n_frames")
                    frame_dt = meta.get("frame_dt_s")
                    if isinstance(n_frames, int) and n_frames > 0 \
                            and isinstance(frame_dt, (int, float)) and frame_dt > 0:
                        duration_ms = int(round(n_frames * float(frame_dt) * 1000))
                except (OSError, ValueError):
                    duration_ms = None
        if duration_ms is None:
            try:
                import imageio.v3 as iio  # type: ignore
                meta_i = iio.immeta(video)
                fps = float(meta_i.get("fps") or 0.0)
                if fps > 0:
                    duration_ms = int(round(
                        sum(1 for _ in iio.imiter(video)) / fps * 1000))
            except Exception:  # noqa: BLE001 - 解码失败不伪造，交 Gate2 降级
                duration_ms = None
        cover = None
        if spec.cover_relpath and (workdir / spec.cover_relpath).is_file():
            cover = str(workdir / spec.cover_relpath)
        return str(video), duration_ms, cover

    def run(self, spec: RunSpec) -> dict:
        """执行一次 run：失败 → 至多 5 轮 coding 补救 → 重跑。"""
        workdir = Path(spec.workdir)
        workdir.mkdir(parents=True, exist_ok=True)
        failure_marker = workdir / FAILURE_JSON
        if failure_marker.is_file():
            # 计划 L505 ③：已结案 run 再进入，计数处硬停，零网络
            raise CodingLimitError(
                f"{spec.thread_id} 已有 {FAILURE_JSON}，禁止重启失败 run")

        root = self._root(spec)
        telemetry = (workdir / spec.telemetry_relpath).resolve(strict=False)
        timeline: list[dict] = []
        coding_calls = 0
        last_result: Optional[_ExecResult] = None
        # 锁内适配（计划 v1.2 外，seq42-47 拆块出生，用户 2026-10-20 裁决）：
        # 上一拆块轮的机器原因（含块号），原样进下一轮块 0 的失败反馈段。
        chunk_feedback = ""

        while True:
            exec_no = len(timeline) + 1
            try:
                result = self._run(list(spec.cmd), workdir,
                                   spec.run_timeout_s, dict(spec.env_extra))
            except subprocess.TimeoutExpired as exc:
                result = _ExecResult(124, exc.stdout or "",
                                     (exc.stderr or "") + f"\nTIMEOUT after "
                                     f"{spec.run_timeout_s}s")
            timeline.append({"exec": exec_no, "rc": result.rc,
                             "stderr_tail": _tb_tail(result)[-500:]})
            last_result = result
            if result.rc == 0 and telemetry.is_file():
                vp, dur_ms, cover = self._video_artifacts(workdir, spec)
                return {"status": "ok", "telemetry_uri": str(telemetry),
                        "coding_round": coding_calls, "executions": exec_no,
                        "timeline": timeline,
                        "video_path": vp, "duration_ms": dur_ms,
                        "cover_png": cover, "intranet_url": None}

            # —— 失败路径：coding 调用用尽即结案，结构上不存在多一次调用 ——
            if coding_calls >= spec.max_coding_rounds:
                reason = (f"{spec.max_coding_rounds} 轮 coding_debug "
                          f"后仍失败（RunSpec 上限；默认 D10=5）")
                if chunk_feedback:
                    reason += f"；末次拆块出生机器原因：{chunk_feedback}"
                return self._fail(root, spec, timeline, last_result, reason=reason)

            tail = _tb_tail(result)
            snippets = _collect_snippets(workdir, tail)
            # Phase 2 Task1：recall 注入时 kb 路由 ProductionRecall 同参查询
            # （query_kb(namespace=troubleshooting, top_k=TOP_K)），
            # ret["kb_hits"] 与原 hits 同源同语义；learning 路只查真实学习表。
            learning_hits: list[dict] = []
            if self.recall is not None:
                ret = self.recall.retrieve(
                    "仿真失败 " + tail[-500:], site="coding")
                hits = ret["kb_hits"]
                learning_hits = ret["learning_hits"]
            else:
                hits = self.vector_engine.query_kb(
                    "仿真失败 " + tail[-500:], namespace="troubleshooting",
                    top_k=TOP_K)
            messages = _build_messages(
                gate2_notes=spec.gate2_notes, traceback_tail=tail,
                snippets=snippets, hits=hits, cmd=spec.cmd,
                learning_hits=learning_hits or None,
                extra_sections=spec.extra_prompt_sections or None)
            # 硬规则1：runner 永不构造 image block（双保险，网关入口亦拒）
            for mm in messages:
                if not isinstance(mm.get("content"), str):
                    raise PatchSafetyError("runner 消息出现非文本块（硬规则1）")

            coding_calls += 1
            stem = f"coding_round{coding_calls}"
            model = self.gateway.model_id(STAGE)
            cs = spec.chunked_birth
            # 锁内适配（计划 v1.2 外，seq42-47 拆块出生，用户 2026-10-20 裁决）
            # run loop 分支：ChunkSpec 仅在目标文件**出生前**的轮次生效；
            # 同 run 内文件已出生后的修补轮自动回到既有单次补丁路径（规格不
            # 变，runner 按磁盘现状选路，绝不重复出生已存在文件）。
            # 已知键漂移（评审 Minor 3，多付不错放）：出生占 round1 的 _chunk<i>
            # 键，紧接的修补轮用无后缀 round2 键；若 kill-9 后整节点重放，工厂
            # 见文件已存在→不挂 spec，重放按无后缀键查缓存 miss 会多重付一次，
            # 但请求指纹含前序补丁串，跨规格绝不错放旧补丁（miss 安全方向）。
            chunked_birth = cs is not None and not _validate_target(
                Path(cs.target_path), workdir).is_file()
            try:
                if chunked_birth:
                    codes = self._run_chunked_birth_round(
                        spec=spec, root=root, model=model,
                        coding_round=coding_calls, tail=tail, snippets=snippets,
                        hits=hits, learning_hits=learning_hits,
                        round_feedback=chunk_feedback)
                    assembled = "".join(codes)
                    # 拼接整体 ast/重名/总长机器校验通过后，才合成单个 old=""
                    # 出生补丁；补丁应用路径一字不改（仍走 _parse_and_apply）。
                    _validate_assembled_chunks(assembled)
                    raw = json.dumps(
                        {"patches": [{"path": cs.target_path, "old": "",
                                      "new": assembled}]}, ensure_ascii=False)
                else:
                    # L-1 调用级幂等：kill-9 落在实呼返回后、节点返回前，
                    # LangGraph 0.2.28 会整节点重放。稳定 key + 请求指纹命中
                    # 即复用上次 raw，不发网、不进 billing_context、不写
                    # ledger，req/resp 证据不动；补丁仍要重放应用。
                    raw = self._cached_coding_call(
                        spec=spec, root=root, stem=stem, model=model,
                        messages=messages, coding_round=coding_calls,
                        learning_hits=learning_hits,
                        cache_key=(f"coding_{spec.thread_id}_r{spec.run_idx}"
                                   f"_round{coding_calls}"),
                        fp_extra={"max_tokens": spec.coding_max_tokens,
                                  "temperature": TEMPERATURE},
                        max_tokens=spec.coding_max_tokens,
                        timeout=spec.coding_timeout_s)
            except ChunkBirthError as exc:
                # 任一块不符 → 整轮失败、不半成品落盘；机器原因进 decision
                # 证据/时间线/下一轮块 0 反馈段，继续既有失败轮机制
                # （RUN_CODING_CAP 不变），无任何 patch 应用、无文件出生。
                self._evidence(root, stem, "decision",
                               {"applied": [], "rejected": str(exc)})
                timeline[-1]["coding_round"] = coding_calls
                timeline[-1]["chunk_birth_rejected"] = str(exc)
                chunk_feedback = str(exc)
                continue
            try:
                applied = _parse_and_apply(workdir, raw)
            except PatchSafetyError as exc:
                # 越界/坏补丁：拒绝且不发起重试（L505 ②），直接结案
                self._evidence(root, stem, "decision",
                               {"applied": [], "rejected": str(exc)})
                return self._fail(
                    root, spec, timeline, last_result,
                    reason=f"补丁安全拒绝（不重试）：{exc}")
            self._evidence(root, stem, "decision",
                           {"applied": applied, "rejected": None})
            timeline[-1]["coding_round"] = coding_calls
            timeline[-1]["applied"] = applied

    # -- 锁内适配（计划 v1.2 外，seq42-47 拆块出生，用户 2026-10-20 裁决）--

    def _cached_coding_call(self, *, spec: RunSpec, root: Path, stem: str,
                            model: str, messages: list[dict],
                            coding_round: int, learning_hits: list[dict],
                            cache_key: str, fp_extra: dict,
                            max_tokens: int, timeout: int,
                            req_extra: "dict | None" = None,
                            resp_payload: "Callable[[str], dict] | None" = None
                            ) -> str:
        """单次 coding 网关调用的缓存/证据/计费/采纳遥测收口（单次与拆块共用）。

        命中调用级重放缓存：直接返回 raw，不发网、不进 billing_context、
        不写 ledger、req/resp 证据不动。未命中：req 证据 → billing_context
        内 set_current_evidence 后 call（锁内适配 L501：证据必须在上下文进入
        之后挂接）→ resp 证据 → 成功返回后才入缓存 → 采纳遥测。
        ``resp_payload`` 为拆块路径的校验器：返回 resp 证据 dict；它在本地
        校验失败时可自写 reject resp 证据并抛 ChunkBirthError（不 put 缓存，
        安全方向：重放按 miss 重新计费）。
        """
        cache = self._replay_cache(spec)
        # S-8：指纹并入 stage/model；Task1：prompt JSON 规范串同时是采纳遥测
        # prompt_sha256 的规范串——两 sha 同源可交叉对账。
        prompt_json = json.dumps(messages, ensure_ascii=False, sort_keys=True)
        fp = request_fingerprint(prompt_json, None, fp_extra,
                                 stage=STAGE, model=model)
        cached = cache.get(cache_key, fp) if cache is not None else None
        if cached is not None:
            # Task1：重放不重复记采纳行——首呼已记，节点重放属同一逻辑调用。
            return cached["raw"]
        req_doc = {
            # P2（Task0）：Phase 2 新证据行时间戳统一严格 UTC Z。
            "ts_iso": utc_now_iso_z(),
            "stage": STAGE, "model": model,
            "thread_id": spec.thread_id, "batch_id": spec.batch_id,
            "coding_round": coding_round, "gate2_notes_present":
                bool(spec.gate2_notes and spec.gate2_notes.strip()),
            "messages": json.loads(redact(json.dumps(
                messages, ensure_ascii=False))),
        }
        if req_extra:
            req_doc.update(req_extra)
        req_ref = self._evidence(root, stem, "req", req_doc)
        # 锁内适配（计划 L501）：evidence 必须在 billing_context 进入之后
        # 挂接——该上下文管理器每次创建新 BillingContext，提前 set 会被重置。
        with billing_context(task=spec.billing_task, batch_id=spec.batch_id,
                             thread_id=spec.thread_id):
            set_current_evidence(req_ref)
            raw, _usage = self.gateway.call(
                STAGE, messages, max_tokens=max_tokens,
                temperature=TEMPERATURE, timeout=timeout)
        # Task1 采纳遥测（QA-1 M1）：唯一条件=call 成功返回；replay 不记，
        # 发网前失败 0 采纳行。必须先于 resp_payload 本地校验——拆块路径上
        # 网关已成功返回但本块 JSON/ast 校验失败时，采纳语义与旧单次路径
        # （坏 JSON 响应亦记）保持一致；崩溃只漏记不重记（安全方向）。
        # 锁内适配评审 Minor 1（2026-10-20 全新 opus reviewer）。
        if self.recall is not None:
            for hit in learning_hits:
                self.recall.record_adoption(
                    thread_id=spec.thread_id, site="coding",
                    learning_id=hit["id"], prompt_text=prompt_json,
                    learning_text=prompt_embedded_text(
                        str(hit.get("text", ""))))
        if resp_payload is not None:
            resp_doc = resp_payload(raw)
        else:
            resp_doc = {"ok": True, "text": redact(raw)}
        self._evidence(root, stem, "resp", resp_doc)
        # 成功返回后才入缓存（异常路径不 put，按 miss 安全重付）。
        usage_ints = self._usage_ints(_usage)
        if cache is not None and usage_ints is not None:
            cache.put(cache_key, kind=STAGE, model=model, raw=raw,
                      usage=usage_ints, fingerprint=fp)
        return raw

    def _chunk_resp_payload(self, root: Path, stem: str, raw: str, *,
                            target: str, index: int, total: int) -> dict:
        """拆块 resp 证据构造兼机器校验：成功存元信息+code 全文；失败自写
        reject resp 证据（raw head/tail 各 400，与探针证据口径一致）后抛。"""
        try:
            code = _extract_chunk(raw, target=target, index=index, total=total)
        except ChunkBirthError as exc:
            self._evidence(root, stem, "resp", {
                "ok": False, "chunk_index": index, "chunk_total": total,
                "parse_rejected": str(exc),
                "raw_head": redact(raw[:400]), "raw_tail": redact(raw[-400:])})
            raise
        return {
            "ok": True,
            "chunk": {"path": target, "index": index, "total": total},
            # code 全文按密钥口径 redact 后落证据；拼接用未脱敏原文。
            "code": redact(code),
            "raw_head": redact(raw[:400]), "raw_tail": redact(raw[-400:])}

    def _run_chunked_birth_round(self, *, spec: RunSpec, root: Path,
                                 model: str, coding_round: int,
                                 tail: str, snippets: list[dict],
                                 hits: list[dict], learning_hits: list[dict],
                                 round_feedback: str) -> list[str]:
        """一个 coding 轮内顺序完成 n 次出生调用，返回各块原文列表。

        每块：构造带合同/提案/骨架 + chunk_context 的消息（硬规则1逐块文本
        守卫）→ 幂等键 coding_<t>_r<run>_round<r>_chunk<i>，指纹含
        chunk_index/total → 调用（max_tokens/timeout 取 RunSpec）→ 逐块
        JSON 机器校验。任何一块不符即 ChunkBirthError 整轮失败（不继续为
        后续块付费），由 run() 捕获进入下一轮；全过程不写任何目标文件。
        """
        cs = spec.chunked_birth
        codes: list[str] = []
        for i in range(cs.chunk_count):
            ctx = ChunkPromptContext(
                index=i, total=cs.chunk_count, target_path=cs.target_path,
                titles=cs.chunk_titles, boundaries=cs.chunk_boundaries,
                previous_code="".join(codes),
                round_feedback=round_feedback if i == 0 else "")
            messages = _build_messages(
                gate2_notes=spec.gate2_notes, traceback_tail=tail,
                snippets=snippets, hits=hits, cmd=spec.cmd,
                learning_hits=learning_hits or None,
                extra_sections=cs.per_call_sections, chunk_context=ctx)
            # 硬规则1：逐块消息同样只允许纯文本（网关入口另有递归守卫）。
            for mm in messages:
                if not isinstance(mm.get("content"), str):
                    raise PatchSafetyError("runner 消息出现非文本块（硬规则1）")
            stem = f"coding_round{coding_round}.chunk{i}"
            raw = self._cached_coding_call(
                spec=spec, root=root, stem=stem, model=model,
                messages=messages, coding_round=coding_round,
                learning_hits=learning_hits,
                cache_key=(f"coding_{spec.thread_id}_r{spec.run_idx}"
                           f"_round{coding_round}_chunk{i}"),
                fp_extra={"max_tokens": spec.coding_max_tokens,
                          "temperature": TEMPERATURE,
                          "chunk_index": i, "chunk_total": cs.chunk_count},
                max_tokens=spec.coding_max_tokens,
                timeout=spec.coding_timeout_s,
                req_extra={"chunk_index": i, "chunk_total": cs.chunk_count},
                resp_payload=partial(
                    self._chunk_resp_payload, root, stem,
                    target=cs.target_path, index=i, total=cs.chunk_count))
            # 拼接用未脱敏原文（resp 证据内是 redact 后的口径）。
            codes.append(_extract_chunk(
                raw, target=cs.target_path, index=i, total=cs.chunk_count))
        return codes

    def _fail(self, root: Path, spec: RunSpec, timeline: list[dict],
              result: Optional[_ExecResult], *, reason: str) -> dict:
        # L-4：失败结案被重放（kill 落在 FAILURE.json 落盘后、节点返回前）时，
        # 保留首次写入的 ts_iso，时间线以首次失败时刻为准，不滚动成新时间戳。
        # P2（Task0）：首写时间戳改严格 UTC Z；旧件 +0000/+0800 串原样保留。
        ts_iso = utc_now_iso_z()
        existing_p = Path(spec.workdir) / FAILURE_JSON
        if existing_p.is_file():
            try:
                old_ts = json.loads(existing_p.read_text(
                    encoding="utf-8")).get("ts_iso")
                if isinstance(old_ts, str) and old_ts.strip():
                    ts_iso = old_ts
            except (ValueError, OSError):
                pass
        payload = {
            "status": "failed",
            "thread_id": spec.thread_id,
            "reason": reason,
            "coding_round": len([t for t in timeline if "coding_round" in t]),
            "executions": len(timeline),
            "last_rc": result.rc if result else None,
            "last_stderr_tail": _tb_tail(result)[-2000:] if result else "",
            "timeline": timeline,
            "ts_iso": ts_iso,
        }
        p = Path(spec.workdir) / FAILURE_JSON
        # S-6：原子写 0600；L-4 首写 ts 保留语义在上方不变。
        atomic_write_text(p, json.dumps(payload, ensure_ascii=False, indent=2))
        self._evidence(root, "failure", "json", payload)
        return {"status": "failed", "failure_json": str(p),
                "coding_round": payload["coding_round"],
                "executions": len(timeline), "timeline": timeline,
                "reason": reason}


def agent_run_seam(runner: AgentRunner, spec_factory: "Callable[[dict], RunSpec]"):
    """图接缝工厂：agent_run 节点每次按当前 state 构造 RunSpec 后执行。

    Gate2 打回意见由 spec_factory 从 state 的 gate2_rejected 事件提取。
    """
    def seam(state: dict) -> dict:
        spec = spec_factory(state)
        record = runner.run(spec)
        if record["status"] == "ok":
            return {"telemetry_uri": record["telemetry_uri"],
                    "coding_round": record["coding_round"],
                    "executions": record.get("executions"),
                    "timeline": record.get("timeline", []),
                    "status": "ok",
                    "video_path": record.get("video_path"),
                    "duration_ms": record.get("duration_ms"),
                    "cover_png": record.get("cover_png"),
                    "intranet_url": record.get("intranet_url")}
        return {"telemetry_uri": "", "status": "failed",
                "coding_round": record["coding_round"],
                "failure_json": record["failure_json"],
                "reason": record["reason"]}
    return seam
