"""Phase 2.8.1 T2：出生隔离 v2 的**父/子共享协议**（事件格式 + 事件读写 + 自证探针）。

为什么单独一个模块，而不是把事件格式抄进 ``birth_child.py``：

1. 事件格式是父子的**同一份契约**，两边各写一份必然漂移——本阶段已经有
   P281-1 的「判据算术单点（``scoring.py``）」教训；
2. AC-P281-2-15 要求 ``birth_child.py`` 的源码文本里不出现 ``secrets`` /
   ``token_hex`` / ``nonce`` / ``result_path``。协议字段名（含 ``nonce_absent``）
   与探针实现因此集中在本模块，子进程 ``import`` 使用。

**这不是为了绕过扫描**，把话说清楚：扫描要证明的是「子进程侧不存在**取** nonce
的通道」，而本模块里与 nonce 相关的只有**探针**（判断 ``__main__`` 上有没有那个
属性）——它证明的正是「没有」。v2 的父进程**根本不生成** nonce，也没有结果文件
通道：层结果由父进程按内核可观测量自己判定（见 ``candidate_birth``）。真通道
（v1 的「父进程生成 nonce → stdin 下发 → 运行器写结果文件 → 父进程校验」）在 v2
里整条不存在，而不是被藏起来。

诚实边界（与计划 §6.1 同一口径，不得在报告里写强）：本设计**不提供沙箱**。子进程
仍可 fork、可写自己的文件、可伪造事件流；``nonce_absent`` 是**子进程自报、父进程
校验格式**，父进程无法证明子进程没说谎——说谎这件事由 AC-P281-2-03 的独立负例在
测试层封堵，而不是靠运行期自证。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping

__all__ = (
    "EVENT_FD",
    "EV_ADVANCE", "EV_CONSTRUCTED", "EV_CONSTRUCTION_ERROR", "EV_CONTRACT",
    "EV_CONTRACT_ERROR", "EV_IMPORTED", "EV_IMPORT_ERROR", "EV_LEVEL",
    "EV_START", "EV_STEP", "EV_STEP_ERROR",
    "FIELD_ABSENT", "LEVEL_OUTCOMES", "PROTOCOL_VERSION",
    "CHILD_ENV_ALLOWLIST", "REFUSED_ENV_PREFIXES",
    "count_steps", "emit", "now_wall", "probe_absent", "read_events",
)

PROTOCOL_VERSION = "simpraxis.phase281.birth_events.v2"

#: 事件词表的分工（判决归 ``candidate_birth``，不在这里）：
#:
#: - ``start`` / ``imported`` / ``constructed`` / ``contract`` / ``step`` /
#:   ``advance`` 是**结构性证据事件**——父进程数它们（有/无/几条），不采信其中
#:   的自由文本；
#: - ``*_error`` 是**诊断事件**——携带闭集 ``reason``，父进程只把它当作失败原因
#:   的候选，能自己观测到的类别（``timeout`` / ``memory``）由父进程覆盖；
#: - ``level`` 是**弱证据**——父进程用自己的判定覆盖它，两者不一致即记
#:   ``event_conflict``。
#:
#: 词表里**没有** nonce / 结果路径字段：v2 的子进程一侧不存在这两样东西。

#: 事件流的文件描述符编号（父进程经 ``preexec`` 把写端 dup 到该号）
EVENT_FD = 3

EV_START = "start"
EV_IMPORTED = "imported"
EV_CONSTRUCTED = "constructed"
EV_CONTRACT = "contract"
EV_STEP = "step"
EV_ADVANCE = "advance"
EV_LEVEL = "level"
EV_IMPORT_ERROR = "import_error"
EV_CONSTRUCTION_ERROR = "construction_error"
EV_CONTRACT_ERROR = "contract_error"
EV_STEP_ERROR = "step_error"

#: ``start`` 事件里承载「子进程里没有 nonce」这一自报的字段名
FIELD_ABSENT = "nonce_absent"

LEVEL_OUTCOMES = ("pass", "fail", "not_applicable", "blocked_infra")

#: 子进程环境白名单（顺序无关；只放行这几个键）。与
#: ``scripts/benchmark/phase281_evaluate.py::CHILD_ENV_ALLOWLIST`` 由单测钉死
#: 逐字相等——两处形状必须一致，不许各自漂移。
CHILD_ENV_ALLOWLIST = ("PATH", "HOME", "LANG", "TMPDIR", "SIMPRAXIS_PHASE281_ROOT")

#: 见到这些前缀的环境变量即拒绝开工（凭据不得进候选/出生进程）。
REFUSED_ENV_PREFIXES = ("ANTHROPIC_", "SIMPRAXIS_ALLOW_PAID")

_ISO = "%Y-%m-%dT%H:%M:%S.%fZ"


def now_wall() -> str:
    """严格 ``Z`` 的墙钟（微秒精度；AC-P281-2-13 的事件序比对靠它）。

    用 ``datetime`` 而不是 ``time.strftime``：后者**不支持** ``%f``，会把它
    原样写进字符串（``2026-11-06T09:03:04.%fZ``）——一个看起来像时间戳、
    实际无法解析的字段，比没有它更坏。
    """
    import datetime as _dt

    return _dt.datetime.now(_dt.timezone.utc).strftime(_ISO)


def probe_absent() -> bool:
    """子进程侧的自证探针：``__main__`` 上**不持有**一次性挑战值。

    v1 的残余威胁正是「候选在运行器读走 stdin 之后，从 ``__main__`` 的全局变量
    里把 nonce / 结果路径取出来」。v2 的子进程压根没有这两个名字，探针因此应恒
    为 ``True``；为 ``False`` 只可能是有人把 v1 的挑战通道又接回来了。
    """
    main = sys.modules.get("__main__")
    return getattr(main, "NONCE", None) is None


def emit(fd: int, record: Mapping[str, Any]) -> None:
    """把一条事件写成一行 JSON 到 ``fd``（**无缓冲**，候选 ``os._exit`` 也不丢）。"""
    line = json.dumps(dict(record), ensure_ascii=False, sort_keys=True) + "\n"
    data = line.encode("utf-8")
    while data:
        try:
            written = os.write(fd, data)
        except (BrokenPipeError, OSError):
            return
        if written <= 0:                                # pragma: no cover - 防御
            return
        data = data[written:]


def read_events(path: "str | Path") -> "tuple[list[dict], list[str]]":
    """读回事件流；返回 ``(events, problems)``。

    坏行（截断/非 JSON/非对象）**不丢**——它们进 ``problems``，由父进程作为
    「事件流被截断」的证据；静默丢弃会把崩溃伪装成「候选什么都没做」。
    """
    target = Path(path)
    events: "list[dict]" = []
    problems: "list[str]" = []
    if not target.is_file():
        return events, [f"事件流文件不在场：{target}"]
    for lineno, line in enumerate(
            target.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        if not line.strip():
            continue
        try:
            doc = json.loads(line)
        except json.JSONDecodeError as exc:
            problems.append(f"第 {lineno} 行不可解析：{exc}")
            continue
        if not isinstance(doc, dict):
            problems.append(f"第 {lineno} 行不是对象：{type(doc).__name__}")
            continue
        events.append(doc)
    return events, problems


def count_steps(events: "list[Mapping[str, Any]]", level: "str | None" = None
                ) -> int:
    """数 ``step`` 事件（可按层过滤）——父进程自己数，不采信自报。"""
    return sum(1 for ev in events
               if ev.get("ev") == EV_STEP
               and (level is None or ev.get("level") == level))
