"""0600 tmp+replace 原子写与私有目录工具。

正确时序（Plan L193「0600 tmp+replace」）：mkstemp（本身 0600）→ fchmod 锁权限
→ 写内容 → flush+fsync → os.replace。replace 之前目标内容从未以 umask 权限
出现；tmp 名带 pid/随机后缀，并发写者不互踩固定 `.tmp` 名（G-7）。
"""
from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path
from typing import Union

PathLike = Union[str, Path]

_TMP_SUFFIX = ".tmp"


def ensure_private_dir(path: PathLike, mode: int = 0o700) -> Path:
    """mkdir(parents) 并把权限收敛到 mode（默认 0700）。

    对已存在目录同样 chmod：证据目录在不经 00_env 的环境下也不放宽。
    """
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(p, mode)
    except OSError:
        # 目录被外部以更严权限持有（如上层挂载）：读/写探针由调用方负责，
        # 这里不掩盖真正的写失败（后续 atomic_write 会再抛）。
        pass
    return p


def _atomic_write(path: PathLike, data: bytes, mode: int) -> None:
    p = Path(path)
    if not p.parent.is_dir():
        raise FileNotFoundError(f"父目录不存在：{p.parent}")
    # 前缀含目标名与 pid，后缀固定；mkstemp 补随机串，天然无撞名。
    fd, tmp = tempfile.mkstemp(prefix=f".{p.name}.{os.getpid()}.",
                               suffix=_TMP_SUFFIX, dir=str(p.parent))
    try:
        try:
            os.fchmod(fd, mode)
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, p)
        except BaseException:
            # mkstemp 成功后 fd 已归 fdopen（异常时 with 关闭）；仅需清残留 tmp。
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise
    finally:
        # 正常路径 replace 后 tmp 已不存在；防御性兜底（mkstemp 之后、fdopen
        # 之前被 kill 的极端窗口由目录 owner 人工清理，不在此误删新文件）。
        if os.path.exists(tmp):  # pragma: no cover - 仅异常窗口
            try:
                os.unlink(tmp)
            except OSError:
                pass


def atomic_write_bytes(path: PathLike, data: bytes, mode: int = 0o600) -> Path:
    _atomic_write(path, data, mode)
    return Path(path)


def atomic_write_text(path: PathLike, text: str, mode: int = 0o600,
                      encoding: str = "utf-8") -> Path:
    _atomic_write(path, text.encode(encoding), mode)
    return Path(path)


def chmod_keep_user_exec(path: PathLike, base: int = 0o600) -> int:
    """写补丁后收敛权限但保留属主执行位（L-11）。

    统一 chmod 0o600 会抹掉原 0o700/0o755 可执行脚本的执行位；规则是
    `base | (old & 0o700)`：只保留属主三位（读/写/执行），组/其他位不保留。
    返回新模式。
    """
    p = Path(path)
    old = stat.S_IMODE(p.stat().st_mode)
    new = base | (old & 0o700)
    os.chmod(p, new)
    return new
