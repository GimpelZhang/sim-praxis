#!/usr/bin/env python3
"""把 KB solutions/ 四方案切块写入同库新表 solution_chunks（Task0b，0 付费 0 触网）。

与 kb_chunks 的关系：
- 同库（SIMPRAXIS_LANCEDB_URI / $KB_ROOT/data/lancedb）同嵌入（mpnet 768，
  复用 /mnt 缓存，绝不二次下载），但独立新表 solution_chunks；
- kb_chunks（恒 1542 行）与 phase1_learnings（Task0a 后 5 行）全程只读：
  写入前后各数一次，不一致即失败（默认期望 1542/5，可由参数覆盖用于彩排）；
- 切块口径复用 ingest_kb 的 TARGET_CHARS=1200/HARD_CHARS=2000 与
  chunk_section：每方案 README 一块（atomic=True）、manifest 一块，脚本
  源码按 HARD_CHARS 行窗切 code 块，relpath 一律带 solutions/ 前缀。

幂等：全部方案校验+向量化完成后，按 solution_id 先删后写，重跑行数不翻倍；
任何方案 fail-closed 都发生在首笔写入之前，不留半批新行。

用法：
  python -m memory_flywheel.scripts.ingest_solutions                # 全量幂等
  python -m memory_flywheel.scripts.ingest_solutions --dry-run
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
import time
from pathlib import Path

import pyarrow as pa
import yaml

from memory_flywheel.embedding_selector import select_embedding
from memory_flywheel.kb_pins import DEFAULT_KB_ROOT, head as kb_head
from memory_flywheel.scripts.ingest_kb import (  # 复用切块器与 HARD_CHARS 常量
    HARD_CHARS, chunk_section)
from orchestrator.solutions import SolutionError, load_manifest, solutions_root
import lancedb

TABLE_NAME = "solution_chunks"
HEADER_README = "【方案章节】"
HEADER_MANIFEST = "【方案清单】"
HEADER_CODE = "【方案代码】"


class SolutionIngestError(RuntimeError):
    """入库前置/对账失败：fail-closed，不留半批新行。"""


def _stable_id(rel: str, idx: int, text: str) -> tuple[str, str]:
    content_hash = hashlib.sha1(text.encode("utf-8")).hexdigest()
    rid = hashlib.sha256(
        f"{rel}|{idx}|{content_hash}".encode("utf-8")).hexdigest()[:32]
    return rid, content_hash


def _chunks_for_file(rel: str, raw: bytes) -> list[tuple[int, str, str]]:
    """返回 (chunk_index, kind, text)；rel 以 solutions/ 开头。"""
    name = Path(rel).name
    if name == "manifest.yaml":
        # manifest 每方案一块（小文件，天然 <TARGET_CHARS）；顺带 YAML 可解析
        parsed = raw.decode("utf-8")
        yaml.safe_load(parsed)
        return [(0, "manifest", f"{HEADER_MANIFEST}{rel}\n" + parsed.strip())]
    if name.endswith(".md"):
        # 计划：4 方案 README 各一个原子块（Q2 光学长 prose 必须整块可召回）。
        pieces = chunk_section(rel, raw.decode("utf-8").splitlines(), atomic=True)
        if len(pieces) != 1:  # atomic 捷径恒返回 0/1 块
            raise SolutionIngestError(f"{rel} README 原子切块异常：{len(pieces)} 块")
        body = pieces[0].split("\n", 1)[1]  # 丢掉 chunk_section 自带的【章节】头
        return [(0, "readme", f"{HEADER_README}{rel}\n{body}")]
    if name.endswith(".py"):
        lines = raw.decode("utf-8").splitlines()
        windows: list[str] = []
        cur: list[str] = []
        size = 0
        for line in lines:
            if size + len(line) + 1 > HARD_CHARS and cur:
                windows.append("\n".join(cur))
                cur, size = [], 0
            cur.append(line)
            size += len(line) + 1
        if cur:
            windows.append("\n".join(cur))
        return [(i, "code", f"{HEADER_CODE}{rel}#L窗{i + 1}\n{w}")
                for i, w in enumerate(windows)]
    return []  # manifest 落件集只含 md/yaml/py；其余类型出现即说明清单越界


class SolutionIngester:
    def __init__(self, *, kb_root: Path | None = None,
                 solutions_dir: Path | None = None, uri: str | None = None):
        # 内容层解析优先级（Task0b QA 修复：防 env 改道把合成 fixture 先删后写
        # 写穿活 solution_chunks）：显式 solutions_dir > 显式 kb_root（钉死
        # kb_root/solutions）> env SIMPRAXIS_SOLUTIONS_ROOT。
        if solutions_dir is not None:
            self.solutions_dir = Path(solutions_dir)
            if kb_root is not None:
                self.kb_root = Path(kb_root)
            elif self.solutions_dir.name == "solutions":
                # 显式给了 solutions 目录（彩排/fixture）：KB 根即其上一级
                self.kb_root = self.solutions_dir.parent
            else:
                self.kb_root = Path(os.environ.get("KB_ROOT", str(DEFAULT_KB_ROOT)))
        elif kb_root is not None:
            self.kb_root = Path(kb_root)
            self.solutions_dir = self.kb_root / "solutions"
        else:
            self.solutions_dir = solutions_root()
            if self.solutions_dir.name == "solutions":
                self.kb_root = self.solutions_dir.parent
            else:
                self.kb_root = Path(os.environ.get("KB_ROOT", str(DEFAULT_KB_ROOT)))
        # fail-closed：env 与显式解析不一致即拒绝（典型险情：测试 autouse fixture
        # 把 SIMPRAXIS_SOLUTIONS_ROOT 指到合成 fixture，而 kb_root 是活库）。
        env_solutions = os.environ.get("SIMPRAXIS_SOLUTIONS_ROOT")
        if env_solutions and \
                Path(env_solutions).resolve() != self.solutions_dir.resolve():
            raise SolutionIngestError(
                f"内容层指向冲突：解析结果 {self.solutions_dir}，但 "
                f"SIMPRAXIS_SOLUTIONS_ROOT={env_solutions}（拒绝按 env 改道，"
                "防止合成/异源内容先删后写写穿活 solution_chunks）")
        self.uri = uri or os.environ.get(
            "SIMPRAXIS_LANCEDB_URI",
            str(self.kb_root / "data" / "lancedb"))
        Path(self.uri).mkdir(parents=True, exist_ok=True)
        self.config = select_embedding()
        self.dim = int(self.config["dim"])
        self.db = lancedb.connect(self.uri)
        self._embedder = None
        self._assert_own_vector_dim()

    def _assert_own_vector_dim(self) -> None:
        if TABLE_NAME not in self.db.table_names():
            return
        vtype = self.db.open_table(TABLE_NAME).schema.field("vector").type
        existing = getattr(vtype, "list_size", None)
        if existing is not None and int(existing) != self.dim:
            raise SolutionIngestError(
                f"已有 {TABLE_NAME} 维度 {existing} != 当前嵌入 {self.dim}（禁止混维度）")

    def _model(self):
        if self._embedder is None:
            from fastembed import TextEmbedding
            self._embedder = TextEmbedding(
                model_name=self.config["model_name"],
                cache_dir=self.config["cache_dir"])
        return self._embedder

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [v.tolist() for v in self._model().embed(texts)]

    def _schema(self) -> pa.Schema:
        return pa.schema([
            pa.field("id", pa.string()),
            pa.field("text", pa.string()),
            pa.field("solution_id", pa.string()),
            pa.field("relpath", pa.string()),
            pa.field("kind", pa.string()),
            pa.field("chunk_index", pa.int32()),
            pa.field("source_commit", pa.string()),
            pa.field("content_hash", pa.string()),
            pa.field("ingested_at", pa.string()),
            pa.field("vector", pa.list_(pa.float32(), self.dim)),
        ])

    def table(self, *, create: bool = False):
        if TABLE_NAME not in self.db.table_names():
            if not create:
                raise SolutionIngestError(f"表 {TABLE_NAME} 不存在")
            self.db.create_table(TABLE_NAME, schema=self._schema(), mode="create")
        return self.db.open_table(TABLE_NAME)

    def count(self) -> int:
        return self.db.open_table(TABLE_NAME).count_rows() \
            if TABLE_NAME in self.db.table_names() else 0

    def old_table_rows(self) -> dict[str, int | None]:
        out: dict[str, int | None] = {}
        for name in ("kb_chunks", "phase1_learnings"):
            out[name] = (self.db.open_table(name).count_rows()
                         if name in self.db.table_names() else None)
        return out

    def discover_solution_dirs(self) -> list[Path]:
        root = self.solutions_dir
        if not root.is_dir():
            raise SolutionIngestError(f"solutions 根不存在：{root}")
        dirs = sorted(p for p in root.glob("track[12]_*/*")
                      if p.is_dir() and (p / "manifest.yaml").is_file())
        if not dirs:
            raise SolutionIngestError(f"{root} 下没有任何方案目录")
        return dirs

    def build_records(self, sdir: Path, source_commit: str) -> list[dict]:
        manifest = load_manifest(sdir)  # fail-closed：sha/C4/schema 全量复算
        now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        records: list[dict] = []
        # manifest.yaml 自身不在 files[] 内（清单不列自己），按计划单独成块
        mrel = f"solutions/{manifest.solution_id}/manifest.yaml"
        for idx, kind, text in _chunks_for_file(mrel,
                                                (sdir / "manifest.yaml").read_bytes()):
            rid, chash = _stable_id(mrel, idx, text)
            records.append({
                "id": rid, "text": text,
                "solution_id": manifest.solution_id, "relpath": mrel,
                "kind": kind, "chunk_index": idx,
                "source_commit": source_commit, "content_hash": chash,
                "ingested_at": now})
        for sf in manifest.files:
            rel = f"solutions/{manifest.solution_id}/{sf.relpath}"
            raw = (sdir / sf.relpath).read_bytes()
            chunks = _chunks_for_file(rel, raw)
            if not chunks:
                raise SolutionIngestError(
                    f"{rel} 未产出任何切块（files[] 只允许 md/yaml/py）")
            for idx, kind, text in chunks:
                rid, chash = _stable_id(rel, idx, text)
                records.append({
                    "id": rid, "text": text,
                    "solution_id": manifest.solution_id, "relpath": rel,
                    "kind": kind, "chunk_index": idx,
                    "source_commit": source_commit, "content_hash": chash,
                    "ingested_at": now})
        return records

    def ingest(self, *, expect_kb_rows: int, expect_learnings: int,
               dry_run: bool = False) -> dict:
        before = self.old_table_rows()
        if before["kb_chunks"] is not None and before["kb_chunks"] != expect_kb_rows:
            raise SolutionIngestError(
                f"入库前 kb_chunks={before['kb_chunks']} != 期望 {expect_kb_rows}"
                "（拒绝在污染库上继续）")
        if before["phase1_learnings"] is not None \
                and before["phase1_learnings"] != expect_learnings:
            raise SolutionIngestError(
                f"入库前 phase1_learnings={before['phase1_learnings']} != 期望 "
                f"{expect_learnings}")

        source_commit = kb_head(self.kb_root)
        plan: list[tuple[str, list[dict]]] = []
        for sdir in self.discover_solution_dirs():
            try:
                recs = self.build_records(sdir, source_commit)
            except SolutionError as exc:
                raise SolutionIngestError(f"{sdir.name} 方案校验失败：{exc}") from exc
            plan.append((sdir.name, recs))
        kinds = sorted({r["kind"] for _, recs in plan for r in recs})
        n_chunks = sum(len(recs) for _, recs in plan)
        print(f"[ingest-solutions] 计划：{len(plan)} 方案，{n_chunks} 块，"
              f"kinds={kinds}，源提交 {source_commit[:12]}")
        if dry_run:
            return {"solutions": {name: len(recs) for name, recs in plan},
                    "dry_run": True}

        # 先把全部向量算出来（任何失败都发生在首笔写入之前），再逐方案先删后写。
        vectorized: list[tuple[str, list[dict], list[list[float]]]] = []
        for name, recs in plan:
            vectors = self.embed([r["text"] for r in recs])
            vectorized.append((name, recs, vectors))

        tbl = self.table(create=True)
        for name, recs, vectors in vectorized:
            sid = recs[0]["solution_id"]
            tbl.delete(f"solution_id = '{sid}'")
            for r, vec in zip(recs, vectors):
                r["vector"] = vec
            tbl.add(recs)
            print(f"[ingest-solutions] {name}: {len(recs)} 块（先删后写，幂等）")

        after = self.old_table_rows()
        if after != before:
            raise SolutionIngestError(
                f"老表行数变化（solution_chunks 入库污染基线）：{before} -> {after}")
        total = self.count()
        print(f"[ingest-solutions OK] solution_chunks={total}；"
              f"kb_chunks={after['kb_chunks']}、"
              f"phase1_learnings={after['phase1_learnings']} 冻结")
        return {"solutions": {name: len(recs) for name, recs in plan},
                "solution_chunks_total": total, "old_tables": after}

    def query_solutions(self, query: str, *, top_k: int = 5,
                        solution_id: str | None = None) -> list[dict]:
        qv = self.embed([query])[0]
        search = self.table().search(qv)
        if solution_id:
            search = search.where(f"solution_id = '{solution_id}'")
        rows = search.limit(top_k).to_list()
        for row in rows:
            row.pop("vector", None)
        return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--kb-root", default=None,
                    help="KB 内容仓根（默认取 solutions 目录上一级或 $KB_ROOT）")
    ap.add_argument("--solutions-root", default=None)
    ap.add_argument("--uri", default=None, help="lancedb 目录（默认 env/新数据层）")
    ap.add_argument("--expect-kb-rows", type=int, default=1542)
    ap.add_argument("--expect-learnings", type=int, default=5)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    try:
        ing = SolutionIngester(
            kb_root=Path(args.kb_root) if args.kb_root else None,
            solutions_dir=Path(args.solutions_root) if args.solutions_root else None,
            uri=args.uri)
        ing.ingest(expect_kb_rows=args.expect_kb_rows,
                   expect_learnings=args.expect_learnings,
                   dry_run=args.dry_run)
    except (SolutionIngestError, SolutionError) as exc:
        print(f"[INGEST SOLUTIONS STOP] {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
