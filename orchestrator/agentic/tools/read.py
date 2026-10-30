"""只读三具：read_file / list_dir / grep（AC-T4-3）。

强制链：jail（realpath/软链/形态）→ schema（类型/长度）→ policy
（Gate1 授予表）→ 读盘。读量上限在 jail.py 常量处集中；grep 拒二进制。
"""
from __future__ import annotations

import re
from pathlib import Path

from ..jail import (
    GREP_LINE_CHARS,
    GREP_MAX_BYTES,
    GREP_MAX_MATCHES,
    LIST_MAX_ENTRIES,
    READ_MAX_BYTES,
    READ_MAX_LINES,
    JailError,
    looks_binary,
)
from ..policy import PolicyDecision, PreToolUsePolicy, ToolContext, resolve_or_deny
from ..schema import (
    MAX_PATH_CHARS,
    MAX_PATTERN_CHARS,
    Field,
    SchemaError,
    int_field,
    str_field,
    validate,
)
from . import ToolResult

_READ_FIELDS = (
    str_field("path", max_chars=MAX_PATH_CHARS),
    int_field("max_lines", default=READ_MAX_LINES, minimum=1,
              maximum=READ_MAX_LINES),
)
_LIST_FIELDS = (str_field("path", required=False, default=".",
                          max_chars=MAX_PATH_CHARS),)
_GREP_FIELDS = (
    str_field("pattern", max_chars=MAX_PATTERN_CHARS),
    str_field("path", required=False, default=".", max_chars=MAX_PATH_CHARS),
    int_field("max_matches", default=GREP_MAX_MATCHES, minimum=1,
              maximum=GREP_MAX_MATCHES),
)


def _policy(ctx: ToolContext) -> PreToolUsePolicy:
    return PreToolUsePolicy(ctx.grants, workdir=ctx.workdir,
                            nonprod=ctx.nonprod,
                            probe_registry=ctx.probe_registry)


def _deny(decision: PolicyDecision) -> ToolResult:
    return ToolResult.denied_result(decision)


def read_file(ctx: ToolContext, **kwargs) -> ToolResult:
    # 第一闸 jail：路径形态/realpath/软链（先于 schema 字段集校验）。
    raw_path = kwargs.get("path")
    target, reason = resolve_or_deny(ctx.jail, raw_path, must_exist=True)
    if target is None:
        return _deny(PolicyDecision.deny(reason))
    if not target.is_file():
        return _deny(PolicyDecision.deny(f"不是常规文件：{raw_path}"))
    try:
        args = validate(_READ_FIELDS, kwargs)
    except SchemaError as exc:
        return _deny(PolicyDecision.deny(f"schema 拒绝：{exc}"))
    decision = _policy(ctx).check("read_file", args, resolved_path=target)
    if decision.decision == "deny":
        return _deny(decision)
    size = target.stat().st_size
    if size > READ_MAX_BYTES:
        return _deny(PolicyDecision.deny(
            f"文件 {size}B 超过只读上限 {READ_MAX_BYTES}B"))
    data = target.read_bytes()
    if looks_binary(data):
        return _deny(PolicyDecision.deny("二进制文件拒绝按文本读取"))
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if len(lines) > args["max_lines"]:
        kept = "\n".join(lines[:args["max_lines"]])
        text = (kept + f"\n... [read_file] 共 {len(lines)} 行，"
                       f"仅回前 {args['max_lines']} 行 ...")
    header = f"[read_file] {ctx.jail.relpath(target)} ({size}B)\n"
    return ToolResult.allow_result(header + text)


def list_dir(ctx: ToolContext, **kwargs) -> ToolResult:
    raw_path = kwargs.get("path", ".")
    target, reason = resolve_or_deny(ctx.jail, raw_path, must_exist=True)
    if target is None:
        return _deny(PolicyDecision.deny(reason))
    if not target.is_dir():
        return _deny(PolicyDecision.deny(f"不是目录：{raw_path}"))
    try:
        args = validate(_LIST_FIELDS, kwargs)
    except SchemaError as exc:
        return _deny(PolicyDecision.deny(f"schema 拒绝：{exc}"))
    decision = _policy(ctx).check("list_dir", args, resolved_path=target)
    if decision.decision == "deny":
        return _deny(decision)
    entries = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name))
    truncated = len(entries) > LIST_MAX_ENTRIES
    rows = []
    for p in entries[:LIST_MAX_ENTRIES]:
        try:
            st = p.stat()
            kind = "d" if p.is_dir() else ("l" if p.is_symlink() else "f")
            rows.append(f"{kind} {st.st_size:>10} {p.name}")
        except OSError as exc:  # 竞态删除/权限：报单项不毁整列
            rows.append(f"? {'?':>10} {p.name} ({type(exc).__name__})")
    body = "\n".join(rows)
    if truncated:
        body += (f"\n... [list_dir] 共 {len(entries)} 项，"
                 f"仅回前 {LIST_MAX_ENTRIES} ...")
    header = f"[list_dir] {ctx.jail.relpath(target)}/\n"
    return ToolResult.allow_result(header + body)


def grep(ctx: ToolContext, **kwargs) -> ToolResult:
    try:
        args = validate(_GREP_FIELDS, kwargs)
    except SchemaError as exc:
        return _deny(PolicyDecision.deny(f"schema 拒绝：{exc}"))
    target, reason = resolve_or_deny(ctx.jail, args["path"], must_exist=True)
    if target is None:
        return _deny(PolicyDecision.deny(reason))
    decision = _policy(ctx).check("grep", args, resolved_path=target)
    if decision.decision == "deny":
        return _deny(decision)
    try:
        pattern = re.compile(args["pattern"])
    except re.error as exc:
        return _deny(PolicyDecision.deny(f"正则非法：{exc}"))
    roots = [target] if target.is_file() else sorted(
        p for p in target.rglob("*") if p.is_file())
    matches: list[str] = []
    scanned = 0
    for fp in roots:
        if len(matches) >= args["max_matches"]:
            break
        try:
            if not fp.is_file() or fp.stat().st_size > GREP_MAX_BYTES:
                continue
            data = fp.read_bytes()
        except OSError:
            continue
        scanned += 1
        if looks_binary(data):
            # AC-T4-3：二进制拒（跳过且显式挂牌，不静默当无匹配）。
            matches.append(f"[grep] 跳过二进制文件：{ctx.jail.relpath(fp)}")
            if len(matches) >= args["max_matches"]:
                break
            continue
        for lineno, line in enumerate(
                data.decode("utf-8", errors="replace").splitlines(), start=1):
            if pattern.search(line):
                shown = line[:GREP_LINE_CHARS]
                matches.append(
                    f"{ctx.jail.relpath(fp)}:{lineno}: {shown}")
                if len(matches) >= args["max_matches"]:
                    break
    header = (f"[grep] pattern={args['pattern']!r} "
              f"root={ctx.jail.relpath(target)} files={scanned} "
              f"matches={len(matches)}\n")
    body = "\n".join(matches) if matches else "(无匹配)"
    full = header + body
    return ToolResult.allow_result(full)
