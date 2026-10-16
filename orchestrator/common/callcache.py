"""付费调用级重放缓存（L-1：kill-9 后 LangGraph 节点重放不得二次计费）。

LangGraph 0.2.28 检查点是超步（节点）粒度：节点内实呼已返回但节点未返回时
进程被杀，resume 会从零重放整个节点，导致同一逻辑调用二次实付、确定性证据
文件名被覆盖、账证无法对账。

机制：付费调用前以 ``(thread, run_idx, split/round)`` 派生的稳定 key 查缓存；
命中且请求哈希一致 → 直接复用上次的 (raw, usage)，**不发网、不记账、不重写
证据**（原 req/resp 证据保持不动，账行与证据继续可对账）。

缓存是操作件而非证据：落在调用方给定目录（生产为 phase1 根下的点目录
``.call_cache``，0700），证据包 rglob 不收录点路径；内容 0600 原子写。
缓存损坏/字段不符按 miss 处理（安全方向：正常计费重呼），不阻塞生产。
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Optional

from .clock import utc_now_iso_z
from .fsutil import atomic_write_text, ensure_private_dir

SCHEMA = "simpraxis.call_replay.v1"
_KEY_RE = re.compile(r"^[A-Za-z0-9_.-]{1,160}$")


def request_fingerprint(prompt: str, image_b64: "str | None" = None,
                        extra: "dict[str, Any] | None" = None, *,
                        stage: "str | None" = None,
                        model: "str | None" = None) -> str:
    """逻辑请求指纹：stage + model + prompt + 图像 + 影响计费/结果的参数。

    S-8：stage/model 必须并入指纹——同一 prompt 在 macro/coding/visual 三阶段
    或不同模型下不是同一个逻辑请求，旧实现仅 hash prompt+image+extra，存在跨
    阶段/跨模型错配重放的风险。并入后旧缓存全部自然失配重付一次（安全方向）。
    代码改了 prompt 或 max_tokens 后旧缓存同样自然失配（按 miss 处理）。
    """
    h = hashlib.sha256()
    h.update(json.dumps({"stage": stage, "model": model,
                         "prompt": prompt, "image_b64": image_b64,
                         "extra": extra or {}},
                        ensure_ascii=False, sort_keys=True,
                        default=str).encode("utf-8"))
    return h.hexdigest()


class CallReplay:
    """按 key 存取成功调用结果。键由调用方按 (thread, run, split/round) 派生。"""

    def __init__(self, cache_dir: "str | Path"):
        self.dir = ensure_private_dir(cache_dir)

    def _path(self, key: str) -> Path:
        if not _KEY_RE.match(key):
            raise ValueError(f"非法 replay key（只允许 1-160 位 [A-Za-z0-9_.-]）：{key!r}")
        return self.dir / f"{key}.json"

    def get(self, key: str, fingerprint: str) -> "Optional[dict[str, Any]]":
        """命中返回 {raw, usage, model, kind, ts_iso}；任何不一致/损坏返回 None。"""
        p = self._path(key)
        if not p.is_file():
            return None
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return None
        if not isinstance(rec, dict) or rec.get("schema") != SCHEMA:
            return None
        if rec.get("req_sha256") != fingerprint:
            return None
        usage = rec.get("usage")
        # C2 口径：usage 必须是两个“非布尔”非负整数——bool 是 int 子类，
        # isinstance(True, int) 为 True，故用 type(v) is int 严口径拒布尔。
        if (not isinstance(usage, list) or len(usage) != 2
                or not all(type(v) is int and v >= 0 for v in usage)):
            return None
        if not isinstance(rec.get("raw"), str) or not rec.get("model"):
            return None
        return {"raw": rec["raw"], "usage": tuple(usage),
                "model": rec["model"], "kind": rec.get("kind", ""),
                "ts_iso": rec.get("ts_iso", "")}

    def put(self, key: str, *, kind: str, model: str, raw: str,
            usage: "tuple[int, int] | list[int]", fingerprint: str,
            ts_iso: "str | None" = None) -> None:
        """成功调用后落缓存（0600 原子写）。失败/异常路径禁止调用。"""
        # C2 口径：入参同样拒绝布尔（bool 是 int 子类），与 get 的严口径对称。
        if (not isinstance(usage, (list, tuple)) or len(usage) != 2
                or not all(type(v) is int and v >= 0 for v in usage)):
            raise ValueError("usage 必须是两个非布尔非负整数 (input_tokens, output_tokens)")
        rec = {
            "schema": SCHEMA,
            "kind": kind,
            "model": model,
            "raw": raw,
            "usage": [int(usage[0]), int(usage[1])],
            "req_sha256": fingerprint,
            "ts_iso": ts_iso or utc_now_iso_z(),
        }
        atomic_write_text(self._path(key),
                          json.dumps(rec, ensure_ascii=False, indent=2))

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return f"CallReplay({self.dir})"
