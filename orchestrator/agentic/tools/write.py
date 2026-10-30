"""写二具：apply_patch / write_file（AC-T4-4）。

- apply_patch 复用 agent_runner/runner.py 既有锚点机制（structured
  old/new 恰好 1 命中、unified diff 唯一 hunk、replace.v1 围栏三类通道
  与同一族 PatchSafetyError），补丁全部先内存投影、再过 AST 闸，最后统一
  commit——任一失败零落盘；异名锚点（old/new 顶层函数/类名不一致）拒绝；
  不引 Aider 式模糊匹配（D-8 以后选项，本 Task 明确不做）。
- write_file 走同一 write-root policy + jail，.py 文件过 AST 闸，
  fsutil 0600/0700 原子写。
"""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path

from orchestrator.agent_runner.runner import (
    REPLACE_FENCE_NUCLEUS,
    PatchAnchorError,
    PatchSafetyError,
    PatchTransportError,
    _apply_replace_staged,
    _commit_staged,
    _json_object,
    _scan_replace_fences,
    _stage_structured,
    _stage_unified_section,
    _validate_target,
)
from orchestrator.common.fsutil import (
    atomic_write_text,
    chmod_keep_user_exec,
    ensure_private_dir,
)

from ..jail import JailError
from ..policy import (
    PolicyDecision,
    PreToolUsePolicy,
    ToolContext,
    resolve_or_deny,
)
from ..schema import MAX_PATH_CHARS, SchemaError, str_field, validate
from . import ToolResult

_MAX_CONTENT_CHARS = 200_000
_MAX_PATCH_CHARS = 200_000

_WRITE_FIELDS = (
    str_field("path", max_chars=MAX_PATH_CHARS),
    str_field("content", max_chars=_MAX_CONTENT_CHARS),
)
_PATCH_FIELDS = (str_field("patch", max_chars=_MAX_PATCH_CHARS),)

_DIFF_GIT_RE = re.compile(r"^diff --git .+$", re.M)
_PLUS3_RE = re.compile(r"^\+\+\+ (?:b/)?(.+)$", re.M)


def _deny(reason: str) -> ToolResult:
    return ToolResult.denied_result(PolicyDecision.deny(reason))


def _policy(ctx: ToolContext) -> PreToolUsePolicy:
    return PreToolUsePolicy(ctx.grants, workdir=ctx.workdir,
                            nonprod=ctx.nonprod,
                            probe_registry=ctx.probe_registry)


def _gate_target(ctx: ToolContext, rel: str) -> "Path | ToolResult":
    """jail（含软链逃逸）+ runner 禁改件 + Gate1 可写前缀，三连过。"""
    target, reason = resolve_or_deny(ctx.jail, rel)
    if target is None:
        return _deny(reason)
    try:
        # 复用旧引擎禁改件守卫（stage_models.yaml/00_env.sh/access 段）。
        _validate_target(Path(rel), ctx.workdir)
    except PatchSafetyError as exc:
        return _deny(str(exc))
    decision = _policy(ctx).check("write_file", {}, resolved_path=target)
    if decision.decision == "deny":
        return ToolResult.denied_result(decision)
    return target


def _ast_gate(staged: dict[str, str]) -> "str | None":
    """落盘前 AST 闸：所有投影后的 .py 必须可解析。返回拒绝原因或 None。"""
    for key, text in staged.items():
        if not key.endswith(".py"):
            continue
        try:
            ast.parse(text)
        except SyntaxError as exc:
            rel = Path(key).name
            return (f"AST 闸拒绝：{rel} 投影后语法不可解析"
                    f"（行 {exc.lineno}：{exc.msg}）；本轮零落盘")
    return None


def _top_level_def_name(snippet: str) -> "str | None":
    """取片段首个顶层 FunctionDef/ClassDef 名（片段不可独立解析返 None）。"""
    try:
        tree = ast.parse(snippet)
    except SyntaxError:
        return None
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return node.name
    return None


def _anchor_name_mismatch(old: str, new: str) -> bool:
    """异名锚点：old/new 各自首个顶层定义名不同即拒（防偷换函数体）。"""
    old_name = _top_level_def_name(old)
    new_name = _top_level_def_name(new)
    return (old_name is not None and new_name is not None
            and old_name != new_name)


def write_file(ctx: ToolContext, **kwargs) -> ToolResult:
    try:
        args = validate(_WRITE_FIELDS, kwargs)
    except SchemaError as exc:
        return _deny(f"schema 拒绝：{exc}")
    gated = _gate_target(ctx, args["path"])
    if isinstance(gated, ToolResult):
        return gated
    target: Path = gated
    if target.suffix == ".py":
        try:
            ast.parse(args["content"])
        except SyntaxError as exc:
            return _deny(
                f"AST 闸拒绝：新内容语法不可解析（行 {exc.lineno}：{exc.msg}）")
    existed = target.exists()
    ensure_private_dir(target.parent)
    atomic_write_text(target, args["content"], mode=0o600)
    if existed:
        # 补丁既有 0700/0755 脚本时保留属主执行位（与 runner L-11 同口径）。
        chmod_keep_user_exec(target)
    rel = ctx.jail.relpath(target)
    return ToolResult.allow_result(
        f"[write_file] 已原子写入 {rel}（{len(args['content'])} 字符，0600）")


def apply_patch(ctx: ToolContext, **kwargs) -> ToolResult:
    try:
        args = validate(_PATCH_FIELDS, kwargs)
    except SchemaError as exc:
        return _deny(f"schema 拒绝：{exc}")
    decision = _policy(ctx).check("apply_patch", args)
    if decision.decision == "deny":
        return ToolResult.denied_result(decision)
    raw = args["patch"]
    workdir = ctx.workdir
    try:
        return _apply(ctx, raw, workdir)
    except (PatchSafetyError, json.JSONDecodeError, JailError, OSError) as exc:
        # 锚点/传输/AST/路径错误全部是模型可自修错：零落盘，观察回模型。
        kind = type(exc).__name__
        if isinstance(exc, PatchAnchorError):
            kind = f"PatchAnchorError(hits={exc.hits})"
        return _deny(f"补丁被拒（{kind}）：{str(exc)[:500]}")


def _apply(ctx: ToolContext, raw: str, workdir: Path) -> ToolResult:
    # ---- replace.v1 围栏通道（_apply_replace_staged 内置重 parse AST 闸） ----
    if REPLACE_FENCE_NUCLEUS in raw:
        blocks = _scan_replace_fences(raw)
        if blocks:
            for block in blocks:
                gated = _gate_target(ctx, block.path)
                if isinstance(gated, ToolResult):
                    return gated
            applied = _apply_replace_staged(workdir, blocks)
            return ToolResult.allow_result(
                f"[apply_patch] replace 围栏已应用：{applied}")
        # 有核记但扫描无合法块：扫描器自身会抛 PatchTransportError，
        # 落到这里说明核记仅出现在正文——按 JSON 通道继续（与旧引擎同序）。
    # ---- JSON 通道：structured patches[] 或 unified_diff ----
    obj = _json_object(raw)
    if not isinstance(obj, dict):
        raise PatchTransportError("补丁不是 JSON 对象")
    staged: dict[str, str] = {}
    applied: list[str] = []
    if "unified_diff" in obj:
        diff = obj["unified_diff"]
        if not isinstance(diff, str) or not diff.strip():
            raise PatchTransportError("unified_diff 为空")
        sections = _DIFF_GIT_RE.split(diff)
        for section in sections:
            if not section.strip():
                continue
            m = _PLUS3_RE.search(section)
            if m:
                gated = _gate_target(ctx, m.group(1).strip())
                if isinstance(gated, ToolResult):
                    return gated
            rel = _stage_unified_section(staged, workdir, section)
            if rel is not None:
                applied.append(rel)
    else:
        patches = obj.get("patches")
        if not isinstance(patches, list) or not patches:
            raise PatchTransportError("既无 unified_diff 也无 patches[]")
        for p in patches:
            if not isinstance(p, dict):
                raise PatchTransportError("patches 元素必须是对象")
            rel0 = p.get("path")
            if not isinstance(rel0, str):
                raise PatchTransportError("补丁缺 path 字段")
            gated = _gate_target(ctx, rel0)
            if isinstance(gated, ToolResult):
                return gated
            old, new = p.get("old"), p.get("new")
            if isinstance(old, str) and isinstance(new, str) \
                    and _anchor_name_mismatch(old, new):
                raise PatchTransportError(
                    "异名锚点拒绝：old/new 顶层函数或类名不一致"
                    f"（{_top_level_def_name(old)} -> {_top_level_def_name(new)}）")
            applied.append(_stage_structured(staged, workdir, p))
    reason = _ast_gate(staged)
    if reason:
        raise PatchTransportError(reason)
    _commit_staged(staged, workdir)
    return ToolResult.allow_result(
        f"[apply_patch] 已应用：{sorted(set(applied))}")
