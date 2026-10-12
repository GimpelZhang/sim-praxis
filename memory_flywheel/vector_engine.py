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
import os
import time
from pathlib import Path
from typing import Any, Iterable

import pyarrow as pa

import lancedb

from memory_flywheel.embedding_selector import CONFIG_PATH, select_embedding

TABLE_NAME = "kb_chunks"
NAMESPACES = ("capabilities", "troubleshooting")
# m13：与 ingest_kb.classify 的产出全集一一对应（KB 32 文件实际只会产出这 11 类）
DOC_TYPES = (
    "root_index", "index", "quickstart", "background", "ai",
    "troubleshooting", "code", "guide", "comparison", "practices", "terminology",
)


class VectorEngine:
    def __init__(self, uri: str | None = None):
        self.uri = uri or os.environ.get(
            "SIMPRAXIS_LANCEDB_URI", "/mnt/artifacts/simpraxis/lancedb")
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
        vtype = self.db.open_table(TABLE_NAME).schema.field("vector").type
        existing_dim = getattr(vtype, "list_size", None)
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
        return [v.tolist() for v in self._model().embed(texts)]

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
        uri = uri or os.environ.get(
            "SIMPRAXIS_LANCEDB_URI", "/mnt/artifacts/simpraxis/lancedb")
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
