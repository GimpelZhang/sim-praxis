"""LanceDB 双路向量中枢：一张 kb_chunks 表 + namespace 元数据过滤。

表结构（provenance 全部必填）：
  id            确定性主键 = sha256(kb_relpath|chunk_index|sha1(text))
  text          切块正文（首行注入章节路径）
  namespace     capabilities | troubleshooting
  kb_relpath    相对 KB 仓库根，如 knowledge/projects/lw_benchhub/troubleshooting.md
  project       ge_sim_v2 | genesis_world | lw_benchhub | genie_sim_v3 | global
  layer         global | project
  doc_type      11 类全集（与 ingest_kb.classify 产出一一对应，m13）：
                root_index | index | quickstart | background | ai | troubleshooting |
                code | guide | comparison | practices | terminology
  section_path  标题层级路径
  chunk_index   文件内序号
  evidence_refs 文内出现的 P/D/L/Q 编号
  source_commit KB 入库时的 git commit
  content_hash  sha1(text)
  ingested_at   ISO 时间
  vector        embedding_config.json 决定维度
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Iterable

import pyarrow as pa

import lancedb

from memory_flywheel import kb_pins
from memory_flywheel.embedding_selector import (
    CONFIG_PATH,
    fastembed_embed,
    select_embedding,
)

TABLE_NAME = "kb_chunks"
SOLUTION_TABLE_NAME = "solution_chunks"
NAMESPACES = ("capabilities", "troubleshooting")
# m13：与 ingest_kb.classify 的产出全集一一对应（KB 32 文件实际只会产出这 11 类）
DOC_TYPES = (
    "root_index", "index", "quickstart", "background", "ai",
    "troubleshooting", "code", "guide", "comparison", "practices", "terminology",
)


def _table_vector_dim(tbl) -> int | None:
    """读 Lance 表 vector 字段的固定 list_size（非固定长返回 None）。"""
    return getattr(tbl.schema.field("vector").type, "list_size", None)


class VectorEngine:
    def __init__(self, uri: str | None = None):
        # Task0b：默认库随 KB data/ 软链（$KB_ROOT/data/lancedb，实体在 /mnt）。
        self.uri = uri or kb_pins.default_lancedb_uri()
        # Task0b QA（M1）：缺链/实体 data/ 在任何 mkdir 之前 fail-closed，
        # 杜绝缺链克隆在公开内容仓实建 data/（split-brain）。
        kb_pins.ensure_content_data_link(self.uri)
        Path(self.uri).mkdir(parents=True, exist_ok=True)
        self.config = select_embedding()
        self.dim = int(self.config["dim"])
        self.db = lancedb.connect(self.uri)
        self._assert_existing_vector_dim()
        self._embedder = None

    def _assert_existing_vector_dim(self) -> None:
        """512(bge-small) 与 384(MiniLM) 混库是硬错误：启动即拒，给出 --rebuild 处置。"""
        if TABLE_NAME not in self.db.table_names():
            return
        existing_dim = _table_vector_dim(self.db.open_table(TABLE_NAME))
        if existing_dim is not None and int(existing_dim) != self.dim:
            raise RuntimeError(
                f"已有 {TABLE_NAME} 向量维度 {existing_dim} 与当前嵌入配置 {self.dim} 不一致"
                f"（疑似切换过嵌入模型）。处置：ingest_kb.py --rebuild 整表重建，"
                f"或清空 {self.uri} 后重灌；禁止不同维度混写同一张表")

    # ---------- embedding ----------
    def _model(self):
        if self._embedder is None:
            from fastembed import TextEmbedding
            self._embedder = TextEmbedding(
                model_name=self.config["model_name"],
                cache_dir=self.config["cache_dir"])
        return self._embedder

    def embed(self, texts: list[str]) -> list[list[float]]:
        # 锁内适配（2026-11-01 用户裁决 (a)）：env 闸批大小，未设＝默认。
        return [v.tolist() for v in fastembed_embed(self._model(), texts)]

    # ---------- schema / table ----------
    def _schema(self) -> pa.Schema:
        return pa.schema([
            pa.field("id", pa.string()),
            pa.field("text", pa.string()),
            pa.field("namespace", pa.string()),
            pa.field("kb_relpath", pa.string()),
            pa.field("project", pa.string()),
            pa.field("layer", pa.string()),
            pa.field("doc_type", pa.string()),
            pa.field("section_path", pa.string()),
            pa.field("chunk_index", pa.int32()),
            pa.field("evidence_refs", pa.list_(pa.string())),
            pa.field("source_commit", pa.string()),
            pa.field("content_hash", pa.string()),
            pa.field("ingested_at", pa.string()),
            pa.field("vector", pa.list_(pa.float32(), self.dim)),
        ])

    def table_exists(self) -> bool:
        return TABLE_NAME in self.db.table_names()

    def table(self, create: bool = False):
        if not self.table_exists():
            if not create:
                raise RuntimeError(f"表 {TABLE_NAME} 不存在，先运行 ingest_kb --rebuild")
            self.db.create_table(TABLE_NAME, schema=self._schema(), mode="create")
        return self.db.open_table(TABLE_NAME)

    def drop(self):
        if self.table_exists():
            self.db.drop_table(TABLE_NAME)

    @staticmethod
    def drop_existing(uri: str | None = None) -> bool:
        """在不构造 VectorEngine 的前提下删表（--rebuild 跨维度换模型专用）。

        __init__ 的维度守卫会拒绝 512→768 这类混维度构造，因此换模型后的
        --rebuild 不能走「先实例化再 drop」；只连接 lancedb、不加载嵌入配置。
        返回是否真的删过旧表。
        """
        # Task0b：默认库随 KB data/ 软链（$KB_ROOT/data/lancedb，实体在 /mnt）。
        uri = uri or kb_pins.default_lancedb_uri()
        # Task0b QA（M1）：connect 同样先过软链闸（--rebuild 不得实建 data/）。
        kb_pins.ensure_content_data_link(uri)
        db = lancedb.connect(uri)
        if TABLE_NAME in db.table_names():
            db.drop_table(TABLE_NAME)
            return True
        return False

    # ---------- write ----------
    def replace_by_relpaths(self, records: list[dict[str, Any]], relpaths: Iterable[str]):
        """文件级幂等替换：先删这些 kb_relpath 的旧 chunk，再整批写入。"""
        tbl = self.table(create=True)
        quoted = ", ".join("'" + p.replace("'", "''") + "'" for p in relpaths)
        if quoted:
            tbl.delete(f"kb_relpath IN ({quoted})")
        if not records:
            return
        texts = [r["text"] for r in records]
        vectors = self.embed(texts)
        for r, vec in zip(records, vectors):
            r["vector"] = vec
        tbl.add(records)

    # ---------- read ----------
    def count(self) -> int:
        return self.table().count_rows() if self.table_exists() else 0

    def counts_by(self, column: str) -> dict[str, int]:
        tbl = self.table()
        return {
            row[column]: row["count"]
            for row in tbl.to_pandas()[[column]].value_counts().reset_index(name="count").to_dict("records")
        } if self.count() else {}

    def query_kb(self, query: str, *, namespace: str | None = None,
                 project: str | None = None, doc_types: list[str] | None = None,
                 top_k: int = 5, include_index: bool = False) -> list[dict[str, Any]]:
        if namespace is not None and namespace not in NAMESPACES:
            raise ValueError(f"非法 namespace：{namespace}")
        if doc_types and not set(doc_types) <= set(DOC_TYPES):
            raise ValueError(f"非法 doc_type：{set(doc_types) - set(DOC_TYPES)}；全集 {DOC_TYPES}")
        clauses: list[str] = []
        if namespace:
            clauses.append(f"namespace = '{namespace}'")
        if project:
            clauses.append(f"project = '{project}'")
        if doc_types:
            joined = ", ".join(f"'{t}'" for t in doc_types)
            clauses.append(f"doc_type IN ({joined})")
        if not include_index:
            clauses.append("doc_type NOT IN ('root_index', 'index')")
        where = " AND ".join(clauses)
        qv = self.embed([query])[0]
        search = self.table().search(qv)
        if where:
            search = search.where(where)
        rows = search.limit(top_k).to_list()
        for row in rows:
            row.pop("vector", None)
        return rows

    # ---------- read: solution_chunks 只读查询缝（Phase 2.5 Task6 AC-T6-1） ----------

    def solution_table_exists(self) -> bool:
        return SOLUTION_TABLE_NAME in self.db.table_names()

    def query_solutions(self, query: str, *, top_k: int = 8,
                        solution_id: str | None = None) -> list[dict[str, Any]]:
        """只读语义检索 ``solution_chunks``（Task0b 起 94 块/5 方案）。

        Task6 新增的生产查询通道：此前该表只有入库（ingest_solutions）没有
        生产查询路。本方法**只读、不建表、不改切块**；表不存在即抛
        RuntimeError（缺表 fail-closed，与 query_kb 同语义；四路编排在
        planner_recall 层按路容错）。solution_id 过滤复用入库侧的 C4 校验
        （orchestrator.solutions.validate_solution_id），非法 id 不触库。
        """
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query_solutions 的 query 必须是非空字符串")
        if isinstance(top_k, bool) or not isinstance(top_k, int) or top_k <= 0:
            raise ValueError(f"top_k 必须是正整数，收到 {top_k!r}")
        if not self.solution_table_exists():
            raise RuntimeError(
                f"表 {SOLUTION_TABLE_NAME} 不存在（先运行 ingest_solutions）")
        # 锁内适配（Phase 2.5 换嵌入）：四组合拦截矩阵 §4 的 query_solutions
        # 维度 fail-fast——构造期不检查 solution_chunks（历史行为），故查询前
        # 必须显式拦；否则 768 查询向量打 1024 表只会在 Lance 内核报含混错误。
        sol_tbl = self.db.open_table(SOLUTION_TABLE_NAME)
        existing_dim = _table_vector_dim(sol_tbl)
        if existing_dim is not None and int(existing_dim) != self.dim:
            raise RuntimeError(
                f"已有 {SOLUTION_TABLE_NAME} 向量维度 {existing_dim} 与当前嵌入配置 "
                f"{self.dim} 不一致（疑似切换过嵌入模型）。处置：运行 "
                f"memory_flywheel.scripts.ingest_solutions 按当前嵌入整表重灌 "
                f"{SOLUTION_TABLE_NAME}；禁止不同维度混查同一张表")
        if solution_id is not None:
            from orchestrator.solutions import validate_solution_id
            validate_solution_id(solution_id)
        qv = self.embed([query])[0]
        search = sol_tbl.search(qv)
        if solution_id:
            search = search.where(f"solution_id = '{solution_id}'")
        rows = search.limit(top_k).to_list()
        for row in rows:
            row.pop("vector", None)
        return rows


def make_record(*, kb_relpath: str, chunk_index: int, text: str, namespace: str,
                project: str, layer: str, doc_type: str, section_path: str,
                evidence_refs: list[str], source_commit: str) -> dict[str, Any]:
    content_hash = hashlib.sha1(text.encode("utf-8")).hexdigest()
    rid = hashlib.sha256(
        f"{kb_relpath}|{chunk_index}|{content_hash}".encode("utf-8")).hexdigest()[:32]
    return {
        "id": rid,
        "text": text,
        "namespace": namespace,
        "kb_relpath": kb_relpath,
        "project": project,
        "layer": layer,
        "doc_type": doc_type,
        "section_path": section_path,
        "chunk_index": int(chunk_index),
        "evidence_refs": sorted(set(evidence_refs)),
        "source_commit": source_commit,
        "content_hash": content_hash,
        "ingested_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
