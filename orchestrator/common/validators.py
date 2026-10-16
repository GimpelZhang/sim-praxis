"""外部字符串进入证据文件路径/批次名前的边界校验（G-5/G-9）。

文件名即信任边界：thread_id 会被插值进证据 JSON 文件名，batch_id 进批次
批准件路径。统一字符集 ``^[A-Za-z0-9_-]{1,64}$``，拒绝 ``/``、``..``、空字节
与绝对路径；写后再做 resolve containment 断言。
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Union

PathLike = Union[str, Path]

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def validate_thread_id(value: object, what: str = "thread_id") -> str:
    if not isinstance(value, str) or not _ID_RE.match(value):
        raise ValueError(
            f"{what} 非法：只允许 1-64 位 [A-Za-z0-9_-]，收到 {value!r}")
    return value


def validate_batch_id(value: object, what: str = "batch_id") -> str:
    # bool 是 int 子类、str 子类型风险同样显式挡掉。
    if not isinstance(value, str) or not _ID_RE.match(value):
        raise ValueError(
            f"{what} 非法：只允许 1-64 位 [A-Za-z0-9_-]，收到 {value!r}")
    return value


def is_within(path: PathLike, root: PathLike) -> bool:
    """resolve 后判定 path 是否仍在 root 之内（防 ``../`` 逃逸）。"""
    root_resolved = Path(root).resolve()
    target = Path(path).resolve()
    return target == root_resolved or root_resolved in target.parents


def assert_within(path: PathLike, root: PathLike) -> Path:
    """写盘后防御：目标 resolve 必须仍在证据根目录内，否则 ValueError。"""
    target = Path(path).resolve()
    if not is_within(target, Path(root).resolve()):
        raise ValueError(f"路径逃逸证据根目录：{path} 不在 {root} 之内")
    return target


def safe_join(root: PathLike, name: str) -> Path:
    """root/name 的安全拼接：name 不允许含路径分隔符或 ``..``。

    与 validate_thread_id 不同，这里允许文件名自带后缀（``.``），供证据
    文件名使用；只做路径段级别守卫。
    """
    if (not isinstance(name, str) or not name
            or name in (".", "..") or "\x00" in name
            or os.sep in name or "/" in name
            or Path(name).is_absolute()):
        raise ValueError(f"非法文件名段：{name!r}")
    candidate = Path(root) / name
    return assert_within(candidate, root)
