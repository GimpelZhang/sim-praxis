#!/usr/bin/env python3
"""Phase 2.5 Task6 Stage B（r4）：精确术语词法通道（BM25）+ 秩融合（RRF）。

**纯模块**：无 I/O、无全局可变态、无时间依赖、无随机源——同一输入恒得同一
输出（术语按字典序求和，平局按 ``str(id)``）。规则逐字对应预注册
``scripts/phase25/task6_r4_prereg.md`` §3.1–§3.3：

- 术语抽取：ASCII ``[A-Za-z_][A-Za-z0-9_]{3,}``（**大小写敏感**，长度 ≥4）∪
  中文连续段的长度 4 滑窗 4-gram（段长 <4 不产术语）；去重后按字典序返回。
- BM25（Robertson/Walker）：``k1=1.2``、``b=0.75``、
  ``idf=ln(1+(N-df+0.5)/(df+0.5))``、``tf=text.count(term)``（子串次数）、
  ``dl=len(text)``；``score==0`` 的行不入结果。
- 词法排序：``(-score, str(id))`` 升序，取前 ``top_k``。
- RRF（Cormack et al. 默认）：``k=60``、两通道等权、
  ``score=Σ 1/(k+rank)``（rank 从 1 起），最终 ``(-score, str(id))`` 升序。

设计意图：稠密通道是语义压缩，对库内独有的精确标识符（``ExecuteMode`` /
``pick_offset_angle`` 这类 DF≤5 的拼写）不敏感；本模块提供一条**全表倒排**的
确定性通道，与稠密 top-8 做秩融合，不比较跨尺度分数（BM25 分与 ``_distance``
不可比，只比名次），因此稠密侧已命中的行不会被词法噪声挤出。
"""
from __future__ import annotations

import math
import re

# ---- 预注册 §3 冻结常量（不随调用可调） ----
ASCII_TERM_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{3,}")
CJK_RUN_RE = re.compile(r"[一-鿿]+")
CJK_NGRAM = 4
BM25_K1 = 1.2
BM25_B = 0.75
RRF_K = 60
DEFAULT_TOP_K = 8


def tokenize_terms(text: str) -> list[str]:
    """预注册 §3.1：返回**去重、字典序**的术语列表。

    字典序是确定性契约的一部分：BM25 逐术语累加，排序后可保证跨进程浮点
    求和顺序一致（否则 set 迭代序会让同一文本在两次运行间产生末位差）。
    """
    terms = {m.group(0) for m in ASCII_TERM_RE.finditer(text)}
    for match in CJK_RUN_RE.finditer(text):
        run = match.group(0)
        for i in range(len(run) - CJK_NGRAM + 1):
            terms.add(run[i:i + CJK_NGRAM])
    return sorted(terms)


class Bm25Index:
    """预注册 §3.2：某一路语料的全表 BM25 倒排（只读、确定性）。

    ``rows`` 只要求每行含 ``id`` 与 ``text`` 两键；行按 ``str(id)`` 排序建立
    内部序，使打分与截断不受调用方传入顺序影响。
    """

    def __init__(self, rows, *, id_key: str = "id", text_key: str = "text"):
        self.rows = sorted(rows, key=lambda r: str(r.get(id_key)))
        self.ids = [str(r.get(id_key)) for r in self.rows]
        self.by_id = {rid: row for rid, row in zip(self.ids, self.rows)}
        self.texts = [str(r.get(text_key) or "") for r in self.rows]
        self.lengths = [len(t) for t in self.texts]
        self.n = len(self.texts)
        self.avgdl = (sum(self.lengths) / self.n) if self.n else 0.0
        df: dict[str, int] = {}
        for text in self.texts:
            for term in set(tokenize_terms(text)):
                df[term] = df.get(term, 0) + 1
        self.df = df

    def _idf(self, term: str) -> float:
        d = self.df[term]
        return math.log(1.0 + (self.n - d + 0.5) / (d + 0.5))

    def scores(self, query: str) -> list[tuple[float, str]]:
        """全表打分：返回 ``[(score>0, id), ...]``，按 ``(-score, str(id))`` 升序。"""
        terms = tokenize_terms(query)
        out: list[tuple[float, str]] = []
        for idx, text in enumerate(self.texts):
            score = 0.0
            for term in terms:                  # 字典序（确定性求和）
                df = self.df.get(term, 0)
                if not df:                      # 语料里从未出现：idf/df 无定义
                    continue
                tf = text.count(term)
                if not tf:
                    continue
                norm = BM25_K1 * (
                    1.0 - BM25_B + BM25_B * self.lengths[idx] / self.avgdl)
                score += self._idf(term) * (tf * (BM25_K1 + 1.0)) / (tf + norm)
            if score > 0.0:
                out.append((score, self.ids[idx]))
        out.sort(key=lambda item: (-item[0], item[1]))
        return out

    def top(self, query: str,
            top_k: int = DEFAULT_TOP_K) -> list[tuple[float, str]]:
        return self.scores(query)[:top_k]


def rrf_fuse(dense_ids, lexical, *,
             top_k: int = DEFAULT_TOP_K,
             k: int = RRF_K) -> list[tuple[float, str]]:
    """预注册 §3.3：把稠密 id 序与词法 ``(score, id)`` 序做 RRF 秩融合。

    只比名次不比分数：同一 id 在两通道的名次贡献 ``1/(k+rank)`` 相加，
    等权、``rank`` 从 1 起。返回 ``[(fused_score, id), ...]``（升序），长度
    至多 ``top_k``。
    """
    if not isinstance(top_k, int) or isinstance(top_k, bool) or top_k <= 0:
        raise ValueError(f"top_k 必须是正整数，收到 {top_k!r}")
    if not isinstance(k, int) or isinstance(k, bool) or k <= 0:
        raise ValueError(f"RRF k 必须是正整数，收到 {k!r}")
    fused: dict[str, float] = {}
    for rank, rid in enumerate(dense_ids, 1):
        key = str(rid)
        fused[key] = fused.get(key, 0.0) + 1.0 / (k + rank)
    for rank, (_score, rid) in enumerate(lexical, 1):
        key = str(rid)
        fused[key] = fused.get(key, 0.0) + 1.0 / (k + rank)
    ordered = sorted(fused.items(), key=lambda item: (-item[1], item[0]))
    return [(score, rid) for rid, score in ordered[:top_k]]
