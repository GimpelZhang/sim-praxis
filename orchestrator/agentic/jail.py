"""路径 jail 与读量上限（AC-T4-3/4/7 三硬闸之第一闸）。

与 ``orchestrator/agent_runner/runner.py::_validate_target`` 同语义的独立
实现：agentic 包不反向依赖 agent_runner（旧引擎路径保持零 import 变化，
见 AC-T4-11）。所有模型给路径在碰文件系统前必须过：

1. 字符形态（拒绝对 None/非 str、NUL 字节、空串）；
2. realpath 前缀 jail（resolve 后必须仍在 workdir 之内，拒 ``../`` 穿越）；
3. 逐段软链逃逸检查（路径上任一环节是指向 workdir 外的符号链接即拒，
   含「最终路径 resolve 回来仍在内部、但中间环节外指」的形态——单纯
   ``Path.resolve().is_relative_to`` 检不出这种逐段外指）。

只读件另有大小/行数上限与二进制拒绝（grep）。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# 读量上限（经验常量集中处之一；与 forensics.OBS_* 同为单一配置点）。
READ_MAX_BYTES = 1_000_000        # read_file 单文件 ≤1MB
READ_MAX_LINES = 2_000           # read_file 最多回 2000 行
LIST_MAX_ENTRIES = 2_000         # list_dir 最多列 2000 项
GREP_MAX_BYTES = 2_000_000       # grep 单文件 ≤2MB
GREP_MAX_MATCHES = 200           # grep 最多回 200 个匹配行
GREP_LINE_CHARS = 1_000          # grep 单匹配行截断长度


class JailError(ValueError):
    """路径 jail 拒绝（硬拒绝，不是 confirm；exit 2 语义见 policy.POLICY_DENY_EXIT）。"""


@dataclass(frozen=True)
class PathJail:
    """workdir 只读/写路径的 realpath 前缀 jail。"""

    workdir: Path

    def __post_init__(self) -> None:
        # 构造侧（我方代码）必须传真实目录；workdir 本身不要求已存在
        # （出生前可能先 write_file 建文件），由 engine 负责 mkdir。
        object.__setattr__(self, "workdir", Path(self.workdir).resolve(strict=False))

    @property
    def root(self) -> Path:
        return self.workdir

    def _check_shape(self, rel: object) -> str:
        if not isinstance(rel, str) or not rel:
            raise JailError(f"路径必须是非空字符串，收到 {rel!r}")
        if "\x00" in rel:
            raise JailError("路径含 NUL 字节")
        return rel

    def resolve(self, rel: object, *, must_exist: bool = False) -> Path:
        """把模型给的相对路径解析到 workdir 内的绝对路径。

        拒绝绝对路径（即便指向 workdir 内——模型面只给相对路径，少一条
        混淆面）、``..`` 穿越与逐段符号链接逃逸。
        """
        rel = self._check_shape(rel)
        p = Path(rel)
        if p.is_absolute():
            raise JailError(f"只允许 workdir 相对路径，拒绝绝对路径：{rel}")
        target = (self.workdir / p).resolve(strict=False)
        try:
            target.relative_to(self.workdir)
        except ValueError:
            raise JailError(
                f"路径穿越 workdir：{rel} -> {target}（workdir={self.workdir}）") from None
        # 逐段软链逃逸：resolve() 后的最终位置可能仍在内部，但路径上某段
        # 外指再绕回（…/link-out -> /etc，link 内再 ../../workdir）同样拒。
        cur = self.workdir
        for part in target.relative_to(self.workdir).parts:
            cur = cur / part
            if cur.is_symlink():
                real = cur.resolve(strict=False)
                try:
                    real.relative_to(self.workdir)
                except ValueError:
                    raise JailError(
                        f"路径经符号链接逃逸 workdir：{cur} -> {real}") from None
        if must_exist and not target.exists():
            raise JailError(f"目标不存在：{rel}")
        return target

    def relpath(self, target: Path) -> str:
        """绝对路径转 workdir 相对串（回模型面/入指纹用）。"""
        try:
            return str(Path(target).resolve(strict=False).relative_to(self.workdir))
        except ValueError:
            raise JailError(f"目标不在 workdir 内：{target}") from None


def looks_binary(data: bytes) -> bool:
    """与 grep(1) 同口径的简化二进制判定：含 NUL 即二进制。"""
    return b"\x00" in data


def head_tail_lines(text: str, *, max_lines: int = READ_MAX_LINES) -> tuple[str, bool]:
    """read_file 行数上限：超限保留前 max_lines 行并加截断标记。"""
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text, False
    kept = "\n".join(lines[:max_lines])
    return (kept + f"\n... [jail] 已截断：共 {len(lines)} 行，仅回前 {max_lines} 行 ...",
            True)
