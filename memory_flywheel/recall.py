#!/usr/bin/env python3
"""Phase 2 Task1：生产级经验召回中枢（计划 L370-398，0 付费）。

目标：``production_recall_hits > 0`` 在真实接口闭环——macro 方向站与 coding
排障站在 KB 检索之外，并行召回 ``phase1_learnings`` 真实学习行，逐字渲染进
prompt，并把「采纳」落成可机判、可对账、不可伪造的遥测行。

双路检索（``retrieve``）：
- kb 路：``VectorEngine.query_kb``，direction→capabilities(top_k=8)、
  coding→troubleshooting(top_k=6)，与两站 Phase 1 历史常量同参同义；
- learning 路：``LearningStore.query`` 语义检索**真实学习表**（mpnet 768），
  主库 phase1_learnings + 调用方注入的额外 store；表不存在则该路返回 ``[]``
  （不报错、不建表——phase2_learnings 表 Task5 才建，此处只留接缝）。
  返回行保留学习表全部字段（vector 已由 LearningStore.query 弹出），
  本模块不增造、不改写任何字段。

采纳判定（纯函数 ``adopted``）：学习行被**逐字渲染**进最终 prompt 才为真
（精确子串；禁止模糊匹配/同义改写/包含性放宽）。遥测落
``$SIMPRAXIS_PHASE2_ROOT/recall/production_recall.jsonl``（父目录 0700、
首行原子写出生即 0600、其后 append+flush+fsync+chmod600，与
offgraph_learning._append_write_log 同纪律），行 schema 恰好六字段：
``{ts, thread_id, site, learning_id, prompt_sha256, adopted_text_sha256}``。

锁内适配（Task1）：
- 计划签名 ``retrieve(hint, *, top_k=3)`` 只有一站；实际 direction/coding 两
  调用方的 kb namespace 必须二选一，故增加 ``site`` 严校验参数；top_k 仍只
  约束 learning 路，kb 路用 ``kb_top_k``（缺省每站沿用历史 8/6）。
- prompt_text 规范由调用方钉死为 ``json.dumps(messages, ensure_ascii=False)``
  消息列表 JSON 串（与调用级缓存指纹同源，可对账）。行文本在该 JSON 串中以
  转义形出现（换行→``\\n``），故采纳判定文本须取 ``prompt_embedded_text``
  （JSON 字符串体），消息 content 内仍是逐字原文，未做任何改写。
- Task9 看板只在 ``count_adoptions`` 函数上接线，本任务不接看板。
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Iterable

from orchestrator.common.clock import parse_ts_iso_strict, utc_now_iso_z
from orchestrator.common.fsutil import atomic_write_text, ensure_private_dir
from orchestrator.common.runtime_paths import phase2_root
from orchestrator.common.validators import validate_thread_id

DIRECTION_SITE = "direction"
CODING_SITE = "coding"
SITES = (DIRECTION_SITE, CODING_SITE)

# kb 路每站 top_k：沿用两站 Phase 1 历史常量（setter TOP_K=8 / runner TOP_K=6），
# retrieve 不改检索语义，ProductionRecall=None 时两站走原查询路径。
KB_TOP_K = {DIRECTION_SITE: 8, CODING_SITE: 6}
LEARNING_DEFAULT_TOP_K = 3

RECALL_LOG_RELPATH = Path("recall") / "production_recall.jsonl"

# 行 schema 恰好六字段；键序冻结，count_adoptions/看板按此解析对账。
LOG_FIELDS = ("ts", "thread_id", "site", "learning_id",
              "prompt_sha256", "adopted_text_sha256")


def adopted(prompt_text: str, learning_text: str) -> bool:
    """纯函数：学习行被**逐字渲染**进 prompt 才为真。

    精确子串判定（``learning_text in prompt_text``）：不做模糊匹配、同义改写
    或分词包含；learning_text 空串恒 False（空命中不得记采纳）。入参必须是
    字符串——调用方传非字符串属编程错误，直接抛，不静默判否。
    """
    if not isinstance(prompt_text, str) or not isinstance(learning_text, str):
        raise ValueError("adopted 的 prompt_text/learning_text 必须是字符串")
    return bool(learning_text) and learning_text in prompt_text


def prompt_embedded_text(text: str) -> str:
    """行文本在 prompt JSON 串中的逐字出现形（JSON 字符串体，去外层引号）。

    锁内适配（Task1）：采纳遥测的 prompt_text 规范是
    ``json.dumps(messages, ensure_ascii=False, sort_keys=...)`` 消息列表 JSON
    串，换行/制表/引号在其中以 JSON 转义形存在；若直接拿行原文做精确子串
    判定，任何含换行的真实学习行都恒假。取 json.dumps 的字符串体即行文本在
    prompt JSON 中的精确出现形——消息 content 内仍是逐字原文，无任何改写。
    """
    if not isinstance(text, str):
        raise ValueError("prompt_embedded_text 的 text 必须是字符串")
    return json.dumps(text, ensure_ascii=False)[1:-1]


class ProductionRecall:
    """双路召回 + 采纳遥测（0 付费：embedding 走本地 mpnet 缓存）。"""

    def __init__(self, learning_store: Any, vector_engine: Any, *,
                 extra_learning_stores: Iterable[Any] = ()):
        self.learning_store = learning_store
        self.vector_engine = vector_engine
        # 额外学习库接缝（phase2_learnings Task5 才建表）：逐个探表，
        # 表不存在返回 []，不报错不建表。
        self.extra_learning_stores = tuple(extra_learning_stores)

    # ---------- 双路检索 ----------

    @staticmethod
    def _store_learning_hits(store: Any, hint: str, top_k: int) -> list[dict]:
        # 探表用 LearningStore.table_exists()（内部即 db.table_names() 判定），
        # 缺表路径绝不触发表创建。
        if not store.table_exists():
            return []
        return store.query(hint, top_k)

    def retrieve(self, hint: str, *, site: str,
                 top_k: int = LEARNING_DEFAULT_TOP_K,
                 kb_top_k: "int | None" = None) -> dict[str, list[dict]]:
        """双路召回，返回 ``{"kb_hits": [...], "learning_hits": [...]}``。

        锁内适配（Task1）：相对计划签名新增 ``site``（namespace 二选一必需）
        与 ``kb_top_k``（缺省 direction=8/coding=6，保持两站历史语义）。
        learning 路合并主 store 与各 extra store 的真实命中，按 id 去重
        （主库优先；同 id 不重复渲染、不重复记采纳），保留行全部原字段。
        """
        if site not in SITES:
            raise ValueError(f"site 非法：{site!r}；白名单 {SITES}")
        if not isinstance(hint, str) or not hint.strip():
            raise ValueError("retrieve 的 hint 必须是非空字符串")
        kb_k = kb_top_k if kb_top_k is not None else KB_TOP_K[site]
        namespace = ("capabilities" if site == DIRECTION_SITE
                     else "troubleshooting")
        kb_hits = self.vector_engine.query_kb(
            hint, namespace=namespace, top_k=kb_k)

        learning_hits: list[dict] = []
        seen: set[str] = set()
        for store in (self.learning_store, *self.extra_learning_stores):
            for row in self._store_learning_hits(store, hint, top_k):
                rid = row.get("id")
                if rid in seen:
                    continue
                seen.add(rid)
                learning_hits.append(row)
        return {"kb_hits": kb_hits, "learning_hits": learning_hits}

    # ---------- 采纳遥测 ----------

    @staticmethod
    def telemetry_path(root: "str | Path | None" = None) -> Path:
        """默认 jsonl 路径；显式 root 时落在该根下（测试 hermetic 用）。"""
        base = Path(root) if root is not None else phase2_root()
        return base / RECALL_LOG_RELPATH

    def record_adoption(self, *, thread_id: str, site: str, learning_id: str,
                        prompt_text: str, learning_text: str,
                        path: "str | Path | None" = None) -> bool:
        """学习行经纯函数判定确已逐字渲染进 prompt 时，追加一条采纳行。

        - thread_id 过字符集白名单（与证据文件命名同一校验）；
        - site 严校验白名单；learning_id 必须是非空字符串（真实行 id）；
        - ``adopted`` 为假（未渲染/空文本）直接返回 False、不落任何行
          （不可伪造：没有逐字渲染就没有采纳计数）；
        - 行 schema 恰好六字段；父目录 0700，首行原子写出生即 0600，
          其后 append+flush+fsync 并 chmod 0600。
        - ``path`` 给定时即 jsonl 文件全路径（测试落 tmp）；缺省走
          telemetry_path()=$SIMPRAXIS_PHASE2_ROOT/recall/production_recall.jsonl。
        """
        validate_thread_id(thread_id)
        if site not in SITES:
            raise ValueError(f"site 非法：{site!r}；白名单 {SITES}")
        if not isinstance(learning_id, str) or not learning_id.strip():
            raise ValueError("learning_id 必须是非空字符串（学习表真实行 id）")
        if not adopted(prompt_text, learning_text):
            return False

        record = {
            "ts": utc_now_iso_z(),
            "thread_id": thread_id,
            "site": site,
            "learning_id": learning_id,
            "prompt_sha256": hashlib.sha256(
                prompt_text.encode("utf-8")).hexdigest(),
            "adopted_text_sha256": hashlib.sha256(
                learning_text.encode("utf-8")).hexdigest(),
        }
        log_path = Path(path) if path is not None else self.telemetry_path()
        ensure_private_dir(log_path.parent)
        line = json.dumps(record, ensure_ascii=False)
        if not log_path.exists():
            # 首行经原子写创建，出生即 0600（无 umask 宽权限窗口）。
            atomic_write_text(log_path, line + "\n", mode=0o600)
        else:
            with open(log_path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(log_path, 0o600)
        return True


def count_adoptions(path: "str | Path | None" = None) -> int:
    """读取采纳行数（Task9 看板在此函数上接线；本任务只留接口）。

    jsonl 不存在返回 0；任何非空行不可解析、字段集不是冻结六字段或 ts 不是
    严格 UTC Z，一律 fail-closed 抛错——计数必须可对账，不吞损坏行。
    ``path`` 给定时即 jsonl 文件全路径（与 record_adoption 同一语义）。
    """
    log_path = Path(path) if path is not None \
        else ProductionRecall.telemetry_path()
    if not log_path.is_file():
        return 0
    n = 0
    with open(log_path, "r", encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except ValueError as exc:
                raise ValueError(
                    f"采纳日志损坏行（JSON 不可解析）：{log_path}") from exc
            if not isinstance(rec, dict) or tuple(rec.keys()) != LOG_FIELDS:
                raise ValueError(
                    f"采纳日志行 schema 不符（须恰好六字段 {LOG_FIELDS}）："
                    f"{log_path}")
            parse_ts_iso_strict(rec["ts"])  # 严格 Z 时钟口径
            n += 1
    return n
