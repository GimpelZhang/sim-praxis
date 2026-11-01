#!/usr/bin/env python3
"""一次性迁移件：phase1/phase2 学习表换嵌入重算 vector（Phase 2.5 Task6 Stage A）。

授权：冻结预注册 scripts/phase25/task6_embedding_swap_prereg.md（content
sha256 fcb3fcd6…，门 1 双签 APPROVE）§3.2/§5 + 用户 2026-10-31「2+3」
裁决。本件**迁移专用、非生产写入口**：正常学习回写仍只准走
LearningStore.add_learning / EvolutionStore.add_evolution。

维护窗口（§5 八步，lead 亲跑）调用顺序——**先 phase1 后 phase2**：
  1. 窗前备份（整目录 cp）后，先对两表各跑一次 --export-only 落 JSONL；
  2. reselect 切 Qwen3/1024；
  3. python scripts/kb/reembed_learnings.py --table phase1_learnings --apply
  4. python scripts/kb/reembed_learnings.py --table phase2_learnings --apply

--apply 安全前置（无 env/CLI 覆盖口，测试只准 monkeypatch 常量）：
四表 id 集 sha256 必须与预注册 §6 钉死常量全等，否则拒跑（防止在错误的
库/错误的时点 drop）。流程：读全行（非向量列，按 str(id) 排序）→
0600 JSONL 导出（O_EXCL，存在即报错，禁止静默覆写）→ 按当前 config 构
fastembed TextEmbedding：phase1 嵌 **text 列全文**（禁止 compose_text
重放/自行截断），phase2 嵌 evolution_store.embedding_input(text)
（≤2000 全文，超长取前 1200）→ 向量 dim/finite/非零先验 → drop_table →
按**原表 schema 仅替换 vector list_size** 重建（字段顺序/类型机械保留）→
tbl.add 带回全部元数据列（**含原始 created_at 逐字保留**：run7c
(24681e47) 与 54b27b59 两个历史 created_at 尾债原样带过、不修复）→
行数/维度/id 集/逐行非向量列 canonical 自验。

脚本自身不设离线三开关（窗口逐 shell export，冻结件 §5 已核）。
drop 之后 add 失败属中间态：本件 rc≠0 并打印「需整目录回滚，禁止半态
重跑」——不得在半成品表上重试，须整目录恢复备份后重来。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import lancedb
import numpy as np
import pyarrow as pa

from memory_flywheel.embedding_selector import fastembed_embed, select_embedding
from memory_flywheel.kb_pins import default_lancedb_uri
from memory_flywheel.phase2.evolution_store import embedding_input
from scripts.kb.gen_prewindow_snapshots import (
    TABLES,
    _canonical,
    _sha256_text,
    table_snapshot,
)

PHASE1_TABLE = "phase1_learnings"
PHASE2_TABLE = "phase2_learnings"
SUPPORTED_TABLES = (PHASE1_TABLE, PHASE2_TABLE)

EXPORT_NAMES = {
    PHASE1_TABLE: "phase1_learnings_pre_embedswap.jsonl",
    PHASE2_TABLE: "phase2_learnings_pre_embedswap.jsonl",
}
DEFAULT_EXPORT_DIR = "/mnt/artifacts/embodied-kb/data/backups/embedswap_exports/"

# 预注册 §6 活库身份常量（窗前冻结；无 env/CLI 覆盖口）。
EXPECTED_ID_SET_SHA = {
    "kb_chunks":
        "35d4bf61849c3108348a0aa61bea89a269a2bd748b115dc2105b83de88da1b72",
    "solution_chunks":
        "cdab79c7e1de3d6bd821d28e65c8ac60ac412ccac3ba4c94bf5be90a4fd68197",
    PHASE1_TABLE:
        "022341f732776f33b33314db3e34ddfd915c9ed2ab88cfe6f8c0c9e87af43a9d",
    PHASE2_TABLE:
        "82058d2fa23796c4130749ef0a86dce1504aeff3648ddde0caa7f3fbb5731b4e",
}

_ROLLBACK_HINT = "中间态失败：需整目录回滚备份，禁止半态重跑"


def _id_set_sha(tbl) -> str:
    """读 id 列按 str(id) 排序，套 prewindow 单一源哈希口径（不重写公式）。"""
    rows = tbl.search(None).select(["id"]).limit(1_000_000).to_list()
    ids = [str(r["id"]) for r in rows]
    ids.sort()
    return _sha256_text("\n".join(ids))


def _non_vector_columns(tbl) -> list[str]:
    return [f.name for f in tbl.schema if f.name != "vector"]


def _read_rows(tbl) -> list[dict]:
    cols = _non_vector_columns(tbl)
    rows = tbl.search(None).select(cols).limit(1_000_000).to_list()
    rows.sort(key=lambda r: str(r["id"]))
    return rows


def _vector_dim(tbl) -> int:
    return int(tbl.schema.field("vector").type.list_size)


def _write_export_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    # O_EXCL：固定文件名已存在即拒，防静默覆写（窗口纪律由 lead 把 ls 关）。
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False,
                                    sort_keys=True) + "\n")
    except Exception:
        path.unlink(missing_ok=True)
        raise


def _build_embeddings(config: dict, table: str, rows: list[dict]):
    """构 fastembed 模型并按表口径生成新向量；返回 list[np.ndarray]。"""
    from fastembed import TextEmbedding

    if table == PHASE1_TABLE:
        # phase1：行内 text 列全文（compose_text 不可重放；超长也全文）。
        inputs = [str(r["text"]) for r in rows]
    else:
        # phase2：模块级 embedding_input（≤HARD_CHARS 全文，超长锚定前 TARGET_CHARS）。
        inputs = [embedding_input(str(r["text"])) for r in rows]
    model = TextEmbedding(model_name=config["model_name"],
                          cache_dir=config["cache_dir"])
    # 锁内适配（2026-11-01 用户裁决 (a)）：env 闸批大小，未设＝默认。
    vectors = [np.asarray(v, dtype=np.float32)
               for v in fastembed_embed(model, inputs)]
    return inputs, vectors


def _validate_vectors(vectors, dim: int, n_rows: int) -> None:
    if len(vectors) != n_rows:
        raise RuntimeError(f"嵌入条数 {len(vectors)} != 行数 {n_rows}")
    for i, v in enumerate(vectors):
        if len(v) != dim:
            raise RuntimeError(
                f"第 {i} 行嵌入维度 {len(v)} != config dim {dim}，拒绝迁移")
        if not np.isfinite(v).all():
            raise RuntimeError(f"第 {i} 行嵌入含 nan/inf，拒绝迁移")
        if np.count_nonzero(v) == 0:
            raise RuntimeError(f"第 {i} 行嵌入为零向量，拒绝迁移")


def _verify_four_table_identities(db) -> None:
    names = set(db.table_names())
    missing = [t for t in TABLES if t not in names]
    if missing:
        raise RuntimeError(f"--apply 前置失败：活库缺表 {missing}，拒绝迁移")
    for name in TABLES:
        observed = table_snapshot(db.open_table(name))["id_set_sha256"]
        expected = EXPECTED_ID_SET_SHA[name]
        if observed != expected:
            raise RuntimeError(
                f"--apply 前置失败：{name} id 集 sha {observed} != 预注册 §6 "
                f"钉死值 {expected}（活库身份与窗前冻结不符，拒绝迁移）")


def _schema_with_dim(tbl, new_dim: int) -> pa.Schema:
    """原表 schema 逐字段复制，仅把 vector 换成新固定维度（顺序/类型保留）。"""
    fields = []
    for f in tbl.schema:
        if f.name == "vector":
            fields.append(pa.field("vector", pa.list_(pa.float32(), new_dim)))
        else:
            fields.append(f)
    return pa.schema(fields, metadata=tbl.schema.metadata)


def _post_verify(db, table: str, rows: list[dict], new_dim: int) -> None:
    tbl = db.open_table(table)
    if _vector_dim(tbl) != new_dim:
        raise RuntimeError("迁移后自验失败：vector list_size != config dim")
    after = _read_rows(tbl)
    if len(after) != len(rows):
        raise RuntimeError(
            f"迁移后自验失败：行数 {len(after)} != 导出 {len(rows)}")
    before_by_id = {str(r["id"]): r for r in rows}
    after_ids = []
    for r in after:
        rid = str(r["id"])
        after_ids.append(rid)
        if rid not in before_by_id:
            raise RuntimeError(f"迁移后自验失败：出现新 id {rid}")
        if _canonical(r) != _canonical(before_by_id[rid]):
            raise RuntimeError(
                f"迁移后自验失败：id={rid} 非向量列与导出不一致（元数据被改动）")
    if _sha256_text("\n".join(sorted(after_ids))) != \
            _sha256_text("\n".join(sorted(before_by_id))):
        raise RuntimeError("迁移后自验失败：id 集 sha 与迁移前不一致")
    vecs = tbl.search(None).select(["vector"]).limit(1_000_000).to_list()
    for i, row in enumerate(vecs):
        v = np.asarray(row["vector"], dtype=np.float32)
        if not np.isfinite(v).all() or np.count_nonzero(v) == 0:
            raise RuntimeError(f"迁移后自验失败：第 {i} 行向量非有限或为零向量")


def run(*, table: str, apply: bool, export_only: bool, uri: str,
        export_dir: str) -> int:
    if table not in SUPPORTED_TABLES:
        print(f"非法 --table：{table}（只支持 {SUPPORTED_TABLES}，一次一表）",
              file=sys.stderr)
        return 2
    if apply and export_only:
        print("--apply 与 --export-only 互斥", file=sys.stderr)
        return 2

    export_path = Path(export_dir) / EXPORT_NAMES[table]
    db = lancedb.connect(uri)
    if table not in db.table_names():
        print(f"表 {table} 不存在于 {uri}，拒绝操作", file=sys.stderr)
        return 2

    tbl = db.open_table(table)
    old_dim = _vector_dim(tbl)
    rows = _read_rows(tbl)

    if apply:
        # 首行前置：四表身份与预注册 §6 全等。drop 之前的一切拒绝走 rc=2，
        # 不留中间态、不打 traceback（窗口自动化纪律）。
        try:
            _verify_four_table_identities(db)
            config = select_embedding()
            new_dim = int(config["dim"])
            if new_dim == old_dim:
                raise RuntimeError(
                    f"--apply 拒绝：表维度 {old_dim} == config dim {new_dim}，"
                    f"同维无需迁移（防止误跑）")
            if export_path.exists():
                raise RuntimeError(
                    f"--apply 拒绝：导出文件已存在 {export_path}，禁止静默覆写"
                    f"（lead 窗口纪律先 ls 处置）")
            _write_export_jsonl(export_path, rows)
            _inputs, vectors = _build_embeddings(config, table, rows)
            _validate_vectors(vectors, new_dim, len(rows))
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        # 全部先验通过后才进入不可逆段。
        db.drop_table(table)
        try:
            new_tbl = db.create_table(
                table, schema=_schema_with_dim(tbl, new_dim), mode="create")
            payload = [{**r, "vector": v.tolist()}
                       for r, v in zip(rows, vectors)]
            new_tbl.add(payload)
            # M1（门2 delta）：回填后自验与 add 同处不可逆段，必须同语义收口——
            # RuntimeError 即打印同一回滚提示并 rc=3，不留 traceback/半态；
            # 窄捕 RuntimeError，其他异常类型不在此吞。
            _post_verify(db, table, rows, new_dim)
        except RuntimeError as exc:
            print(f"{_ROLLBACK_HINT}：回填后自验失败："
                  f"{type(exc).__name__}: {exc}", file=sys.stderr)
            return 3
        except Exception as exc:
            print(f"{_ROLLBACK_HINT}：drop 后重建/回填失败："
                  f"{type(exc).__name__}: {exc}", file=sys.stderr)
            return 3
        print(f"[reembed] {table} {old_dim}→{new_dim} 完成：{len(rows)} 行，"
              f"导出 {export_path}，四表身份与逐行元数据自验通过")
        return 0

    # 只读模式：导出（O_EXCL 防覆写）。
    if export_path.exists():
        print(f"拒绝导出：{export_path} 已存在，禁止静默覆写", file=sys.stderr)
        return 2
    _write_export_jsonl(export_path, rows)
    print(f"[reembed] {table} 已导出 {len(rows)} 行 → {export_path}（0600）")
    if export_only:
        return 0

    # dry-run：额外实算新嵌入做 dim/finite 断言，但绝不 drop/写表。
    config = select_embedding()
    new_dim = int(config["dim"])
    if new_dim == old_dim:
        print(f"[reembed] dry-run：表维度 {old_dim} == config dim {new_dim}，"
              f"同维无需迁移 rc=0（表未改动）")
        return 0
    _inputs, vectors = _build_embeddings(config, table, rows)
    _validate_vectors(vectors, new_dim, len(rows))
    print(f"[reembed] dry-run：{table} {old_dim}→{new_dim} 预演通过，"
          f"{len(rows)} 行新向量 dim/finite/非零全部合格；未 drop、未写表")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--table", required=True, choices=SUPPORTED_TABLES)
    ap.add_argument("--apply", action="store_true",
                    help="实际执行 drop/重建/回填（默认 dry-run）")
    ap.add_argument("--export-only", action="store_true",
                    help="只导出 0600 JSONL，不加载模型")
    ap.add_argument("--uri", default=None)
    ap.add_argument("--export-dir", default=DEFAULT_EXPORT_DIR)
    args = ap.parse_args(argv)
    uri = args.uri or str(default_lancedb_uri())
    return run(table=args.table, apply=args.apply,
               export_only=args.export_only, uri=uri,
               export_dir=args.export_dir)


if __name__ == "__main__":
    raise SystemExit(main())
