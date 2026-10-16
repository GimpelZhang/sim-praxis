"""UTC 时钟与时间戳解析的唯一来源（Phase 2 Task0，P2 历史债收口）。

Phase 1 台账时间戳用 ``time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())``
生成，+0800/+0000 两种后缀混写（29 行封存账中 18/11），跨时区对账必须先读
机器时区才能还原瞬时。Phase 2 起所有**新行**统一为严格 Z 后缀的 UTC 串：

  utc_now_iso_z()          -> "2026-10-17T03:05:04Z"
  parse_ts_iso_strict(s)   -> aware datetime（只收 Z，新行口径）
  parse_ts_iso_lenient(s)  -> aware datetime（收 Z / ±HHMM / ±HH:MM，读历史行）

两个解析函数都返回 timezone-aware datetime；lenient 仅用于读取 Phase 1 历史
数据（如 normalize_ledger_tz.py），新代码一律 strict。
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

# 严格新行：秒精度、Z 结尾，不许偏移/小数/空格变体。
_Z_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
# 历史偏移：±HHMM（strftime %z 产物）或 ISO 冒号形式 ±HH:MM。
_OFFSET_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})([+-]\d{2}):?(\d{2})$"
)
_STRFMT = "%Y-%m-%dT%H:%M:%SZ"


def utc_now_iso_z() -> str:
    """当前 UTC 时刻，严格 ``YYYY-MM-DDTHH:MM:SSZ``（秒精度）。"""
    return datetime.now(timezone.utc).strftime(_STRFMT)


def parse_ts_iso_strict(value: str) -> datetime:
    """解析严格 Z 后缀时间串，返回 UTC aware datetime；其他形态一律 ValueError。"""
    if not isinstance(value, str) or not _Z_RE.match(value):
        raise ValueError(
            f"时间戳必须是 YYYY-MM-DDTHH:MM:SSZ 严格 Z 形式，收到：{value!r}"
        )
    return datetime.strptime(value, _STRFMT).replace(tzinfo=timezone.utc)


def parse_ts_iso_lenient(value: str) -> datetime:
    """读历史时间串：Z / ±HHMM（Phase 1 台账）/ ±HH:MM，返回 aware datetime。

    三种形式表达同一 UTC 瞬时（Z 与 +0000 完全等价，+0800 减 8 小时），
    由返回值的 tzinfo 保真，调用方按需 .astimezone(timezone.utc) 比较。
    """
    if not isinstance(value, str):
        raise ValueError(f"时间戳必须是字符串，收到：{value!r}")
    if _Z_RE.match(value):
        return parse_ts_iso_strict(value)
    m = _OFFSET_RE.match(value)
    if not m:
        raise ValueError(
            "时间戳必须是 Z 后缀或 ±HHMM/±HH:MM 偏移形式，收到："
            f"{value!r}"
        )
    head, _zh, _zm = m.group(1), m.group(2), m.group(3)
    # strptime %z 同时接受 +0800 与 +0000；重新拼无冒号形式走一条解析路径。
    raw = f"{head}{m.group(2)}{m.group(3)}"
    dt = datetime.strptime(raw, "%Y-%m-%dT%H:%M:%S%z")
    if dt.tzinfo is None:  # pragma: no cover - 正则已保证偏移存在
        raise ValueError(f"时间戳缺时区偏移：{value!r}")
    return dt
