"""recall：只读经验召回端口（AC-T4-6；Task 6 全量 KB Gate1 召回未交付）。

本 Task 的形态：

- ``RecallPort`` 只读协议（``search``），agentic 包**不创建任何表**、
  不写向量库；
- ``HermeticRecallPort`` 测试/密封端口：内存词元命中，零网络零模型；
- ``ProductionRecallPort`` 适配既有 ``memory_flywheel.recall.ProductionRecall``
  的双路 retrieve（site 固定 coding），把 kb/learning 命中统一投影为
  ``RecallChunk(id, relpath, source, text)``；learning 表缺失时上游本就
  返回 []（不建表）；
- 采纳遥测复用既有六字段 jsonl（``record_adoption``，纯函数逐字渲染
  判定，不可伪造）：方法 ``record_rendered_adoptions`` 在本 Task **没有
  引擎调用方**——T4 只交付端口与判定形，四路线召回（Gate1 全量召回/
  环内工具/下一轮产物对账）是 Task 6 接线范围（AC-T4-6，尾债 T4-LOW-1
  已登记 handoff）。本工具自身不记采纳（未渲染不计数）；在 T6 接线前，
  任何「引擎会调」的描述都是失真，不得据此刻画生产行为。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from ..policy import PolicyDecision, PreToolUsePolicy, ToolContext
from ..schema import SchemaError, int_field, str_field, validate
from . import ToolResult

SOURCE_KB = "kb"
SOURCE_LEARNING = "phase1_learnings"
SOURCE_HERMETIC = "hermetic"

_MAX_QUERY_CHARS = 2_000
_CHUNK_TEXT_CHARS = 4_000
_FIELDS = (
    str_field("query", max_chars=_MAX_QUERY_CHARS),
    int_field("k", default=5, minimum=1, maximum=10),
)


@dataclass(frozen=True)
class RecallChunk:
    id: str
    relpath: str
    source: str
    text: str


class RecallPort:
    """只读召回端口协议（生产/测试两实现；永不建表、永不写库）。"""

    def search(self, query: str, *, k: int) -> list[RecallChunk]:
        raise NotImplementedError


class HermeticRecallPort(RecallPort):
    """密封内存端口：词元重叠确定性排序（无嵌入、无网络、可重放）。"""

    def __init__(self, chunks: "list[RecallChunk] | tuple[RecallChunk, ...]"):
        seen: set[str] = set()
        ordered: list[RecallChunk] = []
        for c in chunks:
            if not isinstance(c, RecallChunk) or c.id in seen:
                raise ValueError("HermeticRecallPort 只收 id 唯一的 RecallChunk")
            seen.add(c.id)
            ordered.append(c)
        self._chunks = tuple(ordered)

    def search(self, query: str, *, k: int) -> list[RecallChunk]:
        terms = {t for t in re.split(r"\W+", query.lower()) if len(t) >= 2}

        def score(c: RecallChunk) -> int:
            hay = f"{c.id} {c.relpath} {c.text}".lower()
            return sum(1 for t in terms if t in hay)

        ranked = sorted(
            ((score(c), idx, c) for idx, c in enumerate(self._chunks)),
            key=lambda row: (-row[0], row[1]))
        return [c for s, _i, c in ranked if s > 0][:k]


class ProductionRecallPort(RecallPort):
    """既有 ProductionRecall 的只读适配（coding 站双路；无表不建表）。"""

    def __init__(self, production_recall: object, *, site: str = "coding"):
        if site != "coding":
            # agentic 内环只在 coding_debug 阶段；direction 站不开放。
            raise ValueError("agentic recall 端口只允许 site=coding")
        self._pr = production_recall
        self.site = site
        self.served: list[tuple[str, str]] = []   # (id, source) 对账观察点

    @staticmethod
    def _row_text(row: dict) -> str:
        for key in ("content", "text", "chunk", "summary"):
            value = row.get(key)
            if isinstance(value, str) and value:
                return value
        return ""

    def search(self, query: str, *, k: int) -> list[RecallChunk]:
        result = self._pr.retrieve(query, site=self.site, top_k=k)
        out: list[RecallChunk] = []
        for row in result.get("kb_hits", [])[:k]:
            cid = str(row.get("id") or row.get("chunk_id") or "")
            if not cid:
                continue
            out.append(RecallChunk(
                id=cid, relpath=str(row.get("relpath") or row.get("source") or ""),
                source=SOURCE_KB, text=self._row_text(row)[:_CHUNK_TEXT_CHARS]))
        for row in result.get("learning_hits", [])[:k]:
            cid = str(row.get("id") or "")
            if not cid:
                continue
            out.append(RecallChunk(
                id=cid, relpath="",
                source=SOURCE_LEARNING, text=self._row_text(row)[:_CHUNK_TEXT_CHARS]))
        self.served.extend((c.id, c.source) for c in out)
        return out

    def record_rendered_adoptions(self, *, thread_id: str, prompt_text: str,
                                  chunks: "list[RecallChunk]",
                                  path: "str | None" = None) -> int:
        """逐字渲染判定后落既有六字段采纳遥测（learning 路；未渲染 0 行）。"""
        n = 0
        for c in chunks:
            if c.source != SOURCE_LEARNING or not c.text:
                continue
            if self._pr.record_adoption(
                    thread_id=thread_id, site=self.site, learning_id=c.id,
                    prompt_text=prompt_text, learning_text=c.text, path=path):
                n += 1
        return n


def recall(ctx: ToolContext, **kwargs) -> ToolResult:
    try:
        args = validate(_FIELDS, kwargs)
    except SchemaError as exc:
        return ToolResult.denied_result(PolicyDecision.deny(
            f"schema 拒绝：{exc}"))
    decision = PreToolUsePolicy(
        ctx.grants, workdir=ctx.workdir, nonprod=ctx.nonprod,
        probe_registry=ctx.probe_registry).check("recall", args)
    if decision.decision == "deny":
        return ToolResult.denied_result(decision)

    port = ctx.recall_port
    if port is None:
        return ToolResult.denied_result(PolicyDecision.deny(
            "recall_port 未装配（生产须注入只读 ProductionRecallPort）"))
    chunks = port.search(args["query"], k=args["k"])
    if not chunks:
        body = "[recall] (无命中)"
    else:
        rows = [f"[recall] {len(chunks)} 条命中（query={args['query']!r}）："]
        for i, c in enumerate(chunks, 1):
            rows.append(f"--- [{i}] id={c.id} source={c.source} "
                        f"relpath={c.relpath or '-'} ---")
            rows.append(c.text)
        body = "\n".join(rows)
    return ToolResult.allow_result(body)
