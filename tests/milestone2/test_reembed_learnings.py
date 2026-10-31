"""E3 hermetic：scripts/kb/reembed_learnings.py 换嵌入迁移件。

合成 tmp lancedb（768 旧表：phase1 12 列含 >1200 长文、phase2 15 列含 >2000
演化志与 3 额外列），fake TextEmbedding 捕获入参，不加载真模型、不触活库。
覆盖：dry-run 不动表/export-only 0600 JSONL/apply 后 1024 全列全等与嵌入
口径（phase1 全文、phase2 前 1200 字）/身份前置拒跑/JSONL 防覆写/同维拒/
nan 与异维向量先验（drop 前拒）/drop 后 add 失败的中间态回滚文案。
"""
from __future__ import annotations

import json
import os
import stat
import sys
import types

import lancedb
import numpy as np
import pyarrow as pa
import pytest

from memory_flywheel import embedding_selector as selector
from memory_flywheel.phase1.learning_store import LearningStore
from memory_flywheel.phase2.evolution_store import (
    EvolutionStore,
    embedding_input,
)
from scripts.kb import reembed_learnings as rel
from scripts.kb.gen_prewindow_snapshots import TABLES, _canonical, table_snapshot

OLD_DIM = 768
NEW_DIM = 1024
LONG_P1 = "phase1长文" * 300          # 1800 字 > TARGET_CHARS 1200
LONG_P2 = "演化志" * 700              # 2100 字 > HARD_CHARS 2000
MPNET_NAME = ("sentence-transformers/"
              "paraphrase-multilingual-mpnet-base-v2")
QWEN_NAME = "Qwen/Qwen3-Embedding-0.6B"


def _vec(dim: int, tag: float = 0.01) -> list[float]:
    v = [tag] * dim
    return v


class FakeEmbedding:
    """可配故障的伪 fastembed；记录每次 embed 的入参文本。"""

    calls: list[list[str]] = []
    mode: str = "ok"

    def __init__(self, model_name=None, cache_dir=None, **kwargs):
        self.model_name = model_name

    def embed(self, texts):
        inputs = list(texts)
        type(self).calls.append(inputs)
        if type(self).mode == "nan":
            return [np.full(NEW_DIM, np.nan, dtype=np.float32)
                    for _ in inputs]
        if type(self).mode == "wrongdim":
            return [np.ones(OLD_DIM, dtype=np.float32) for _ in inputs]
        return [np.full(NEW_DIM, 0.01, dtype=np.float32) + (i % 7) * 1e-4
                for i, _ in enumerate(inputs)]


@pytest.fixture
def world(tmp_path, monkeypatch):
    uri = tmp_path / "lance"
    for d in ("phase1", "phase2", "runs", "exports", "cache"):
        (tmp_path / d).mkdir(parents=True, exist_ok=True)
    cfg_path = tmp_path / "embedding_config.json"

    def write_config(dim: int) -> None:
        cfg_path.write_text(json.dumps({
            "model_name": QWEN_NAME if dim == NEW_DIM else MPNET_NAME,
            "dim": dim, "cache_dir": str(tmp_path / "cache"),
        }), encoding="utf-8")

    write_config(OLD_DIM)
    monkeypatch.setattr(selector, "CONFIG_PATH", cfg_path)
    monkeypatch.setitem(sys.modules, "fastembed",
                        types.SimpleNamespace(TextEmbedding=FakeEmbedding))
    FakeEmbedding.calls = []
    FakeEmbedding.mode = "ok"

    db = lancedb.connect(str(uri))
    p1 = LearningStore(uri=str(uri), evidence_root=tmp_path / "phase1",
                       runs_evidence_root=tmp_path / "runs")
    db.create_table(rel.PHASE1_TABLE, schema=p1._schema(), mode="create")
    p2 = EvolutionStore(uri=str(uri), evidence_root=tmp_path / "phase2",
                        runs_evidence_root=tmp_path / "runs")
    db.create_table(rel.PHASE2_TABLE, schema=p2._schema(), mode="create")
    for name in ("kb_chunks", "solution_chunks"):
        db.create_table(name, schema=pa.schema([
            pa.field("id", pa.string()),
            pa.field("text", pa.string()),
            pa.field("vector", pa.list_(pa.float32(), OLD_DIM)),
        ]), mode="create")

    p1_rows = [
        {"id": "p1a", "text": "短文本一条", "namespace": "capabilities",
         "layer": "global", "run_id": "r1", "track": "track1", "rung": 1,
         "sim_engine": "mujoco", "gate_source": "gate2_accepted",
         "evidence_relpath": "runs/r1/t.json",
         "created_at": "2026-10-15T01:02:03+0000",
         "vector": _vec(OLD_DIM)},
        {"id": "p1b", "text": LONG_P1, "namespace": "capabilities",
         "layer": "global", "run_id": "r2", "track": "track1", "rung": 0,
         "sim_engine": "genesis", "gate_source": "human_failure",
         "evidence_relpath": "runs/r2/t.json",
         # 历史尾债样式：混写时区后缀，迁移必须逐字带回、不得修复
         "created_at": "2026-10-16T04:05:06+0800",
         "vector": _vec(OLD_DIM, 0.02)},
    ]
    db.open_table(rel.PHASE1_TABLE).add(p1_rows)
    p2_rows = [{
        "id": "p2a", "text": LONG_P2, "namespace": "phase2_learnings",
        "layer": "phase2_evolution_note", "run_id": "run27",
        "track": "track1", "rung": 3, "sim_engine": "mujoco",
        "gate_source": "gate2_accepted",
        "evidence_relpath": "@phase2/evolutions/e.json",
        "created_at": "2026-10-26T07:08:09+0000",
        "evolution_round": 1, "parent_run_id": "run1",
        "pareto_gain_ratio": 0.5,
        "vector": _vec(OLD_DIM, 0.03),
    }]
    db.open_table(rel.PHASE2_TABLE).add(p2_rows)
    db.open_table("kb_chunks").add([
        {"id": "k1", "text": "kb", "vector": _vec(OLD_DIM)}])
    db.open_table("solution_chunks").add([
        {"id": "s1", "text": "sol", "vector": _vec(OLD_DIM)}])

    pins = {n: table_snapshot(db.open_table(n))["id_set_sha256"]
            for n in TABLES}
    monkeypatch.setattr(rel, "EXPECTED_ID_SET_SHA", pins)

    write_config(NEW_DIM)
    return {
        "uri": str(uri), "db": db, "tmp": tmp_path,
        "exports": str(tmp_path / "exports"),
        "p1_rows": p1_rows, "p2_rows": p2_rows,
        "export_name": rel.EXPORT_NAMES,
        "write_config": write_config,
    }


def _dim(db, name: str) -> int:
    return db.open_table(name).schema.field("vector").type.list_size


def _read_jsonl(path) -> list[dict]:
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _table_nonvec(db, name: str) -> list[dict]:
    tbl = db.open_table(name)
    cols = [f.name for f in tbl.schema if f.name != "vector"]
    rows = tbl.search(None).select(cols).limit(1_000_000).to_list()
    return sorted(rows, key=lambda r: str(r["id"]))


# ---------- dry-run / export-only ----------

def test_dry_run_previews_but_never_writes(world):
    rc = rel.run(table=rel.PHASE1_TABLE, apply=False, export_only=False,
                 uri=world["uri"], export_dir=world["exports"])
    assert rc == 0
    assert _dim(world["db"], rel.PHASE1_TABLE) == OLD_DIM
    assert FakeEmbedding.calls and len(FakeEmbedding.calls[0]) == 2
    # phase1 入参为行内 text 全文：长文未截断
    assert LONG_P1 in FakeEmbedding.calls[0]
    path = world["tmp"] / "exports" / world["export_name"][rel.PHASE1_TABLE]
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_export_only_skips_model_and_writes_canonical_jsonl(world,
                                                             monkeypatch):
    class Boom:
        def __init__(self, *a, **k):
            raise AssertionError("export-only 不得加载模型")

    monkeypatch.setitem(sys.modules, "fastembed",
                        types.SimpleNamespace(TextEmbedding=Boom))
    rc = rel.run(table=rel.PHASE1_TABLE, apply=False, export_only=True,
                 uri=world["uri"], export_dir=world["exports"])
    assert rc == 0
    objs = _read_jsonl(world["tmp"] / "exports"
                       / world["export_name"][rel.PHASE1_TABLE])
    got = sorted(objs, key=lambda r: r["id"])
    want = [{k: v for k, v in r.items() if k != "vector"}
            for r in sorted(world["p1_rows"], key=lambda r: r["id"])]
    assert [_canonical(g) for g in got] == [_canonical(w) for w in want]


# ---------- apply ----------

def test_apply_phase1_reembeds_full_text_and_preserves_all_columns(world):
    rc = rel.run(table=rel.PHASE1_TABLE, apply=True, export_only=False,
                 uri=world["uri"], export_dir=world["exports"])
    assert rc == 0
    db = world["db"]
    assert _dim(db, rel.PHASE1_TABLE) == NEW_DIM
    after = _table_nonvec(db, rel.PHASE1_TABLE)
    before = sorted(
        [{k: v for k, v in r.items() if k != "vector"}
         for r in world["p1_rows"]], key=lambda r: r["id"])
    assert [_canonical(r) for r in after] == [_canonical(r) for r in before]
    # created_at 逐字保留（含历史混写后缀，不修复尾债）
    by_id = {r["id"]: r for r in after}
    assert by_id["p1a"]["created_at"] == "2026-10-15T01:02:03+0000"
    assert by_id["p1b"]["created_at"] == "2026-10-16T04:05:06+0800"
    # 新向量 finite/非零且恰 NEW_DIM
    vecs = db.open_table(rel.PHASE1_TABLE).search(None).select(
        ["vector"]).limit(1_000_000).to_list()
    for row in vecs:
        v = np.asarray(row["vector"], dtype=np.float32)
        assert v.shape == (NEW_DIM,) and np.isfinite(v).all() \
            and np.count_nonzero(v) > 0
    # 其余三表维度未受影响
    assert _dim(db, rel.PHASE2_TABLE) == OLD_DIM
    assert _dim(db, "kb_chunks") == OLD_DIM
    assert _dim(db, "solution_chunks") == OLD_DIM


def test_apply_phase2_uses_head_anchored_input_and_keeps_extra_columns(world):
    expected_input = embedding_input(LONG_P2)
    assert expected_input == LONG_P2[:1200]  # >HARD_CHARS 锚定首部
    rc = rel.run(table=rel.PHASE2_TABLE, apply=True, export_only=False,
                 uri=world["uri"], export_dir=world["exports"])
    assert rc == 0
    assert _dim(world["db"], rel.PHASE2_TABLE) == NEW_DIM
    assert FakeEmbedding.calls[0] == [expected_input]
    after = {r["id"]: r for r in
             _table_nonvec(world["db"], rel.PHASE2_TABLE)}
    row = after["p2a"]
    assert row["evolution_round"] == 1
    assert row["parent_run_id"] == "run1"
    assert row["pareto_gain_ratio"] == 0.5
    assert row["rung"] == 3


# ---------- 拒跑路径 ----------

def test_identity_mismatch_refuses_with_zero_writes(world, monkeypatch):
    bad = dict(rel.EXPECTED_ID_SET_SHA)
    bad["kb_chunks"] = "0" * 64
    monkeypatch.setattr(rel, "EXPECTED_ID_SET_SHA", bad)
    rc = rel.run(table=rel.PHASE1_TABLE, apply=True, export_only=False,
                 uri=world["uri"], export_dir=world["exports"])
    assert rc == 2
    assert _dim(world["db"], rel.PHASE1_TABLE) == OLD_DIM
    # 无导出文件、无新表
    assert not (world["tmp"] / "exports"
                / world["export_name"][rel.PHASE1_TABLE]).exists()
    assert set(world["db"].table_names()) == set(TABLES)


def test_existing_jsonl_aborts_apply(world):
    path = world["tmp"] / "exports" / world["export_name"][rel.PHASE1_TABLE]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("旧备份\n", encoding="utf-8")
    rc = rel.run(table=rel.PHASE1_TABLE, apply=True, export_only=False,
                 uri=world["uri"], export_dir=world["exports"])
    assert rc == 2
    assert path.read_text(encoding="utf-8") == "旧备份\n"
    assert _dim(world["db"], rel.PHASE1_TABLE) == OLD_DIM


def test_same_dim_apply_refused(world):
    world["write_config"](OLD_DIM)
    rc = rel.run(table=rel.PHASE1_TABLE, apply=True, export_only=False,
                 uri=world["uri"], export_dir=world["exports"])
    assert rc == 2
    assert _dim(world["db"], rel.PHASE1_TABLE) == OLD_DIM


def test_nan_embeddings_rejected_before_drop(world):
    FakeEmbedding.mode = "nan"
    rc = rel.run(table=rel.PHASE1_TABLE, apply=True, export_only=False,
                 uri=world["uri"], export_dir=world["exports"])
    assert rc == 2
    assert _dim(world["db"], rel.PHASE1_TABLE) == OLD_DIM
    assert world["db"].open_table(rel.PHASE1_TABLE).count_rows() == 2


def test_wrong_dim_embeddings_rejected_before_drop(world):
    FakeEmbedding.mode = "wrongdim"
    rc = rel.run(table=rel.PHASE1_TABLE, apply=True, export_only=False,
                 uri=world["uri"], export_dir=world["exports"])
    assert rc == 2
    assert _dim(world["db"], rel.PHASE1_TABLE) == OLD_DIM


def test_midstate_add_failure_prints_rollback_hint(world, monkeypatch, capsys):
    import lancedb.table as lt

    def boom(self, *a, **k):
        raise RuntimeError("synthetic add failure")

    monkeypatch.setattr(lt.LanceTable, "add", boom)
    rc = rel.run(table=rel.PHASE1_TABLE, apply=True, export_only=False,
                 uri=world["uri"], export_dir=world["exports"])
    assert rc == 3
    err = capsys.readouterr().err
    assert "需整目录回滚" in err and "禁止半态重跑" in err
    # 中间态：表已按新维重建但为空（回滚指引必须与该状态同时出现）
    assert _dim(world["db"], rel.PHASE1_TABLE) == NEW_DIM
    assert world["db"].open_table(rel.PHASE1_TABLE).count_rows() == 0


def test_postverify_runtime_error_prints_rollback_hint_rc3(
        world, monkeypatch, capsys):
    """M1：add 成功后 _post_verify 抛 RuntimeError 必须与 add 失败同语义收口——
    同一 try 内窄捕 RuntimeError，打印 _ROLLBACK_HINT 并 return 3（无 traceback）。"""

    def boom(db, table, rows, new_dim):
        raise RuntimeError("synthetic post-verify failure")

    monkeypatch.setattr(rel, "_post_verify", boom)
    rc = rel.run(table=rel.PHASE1_TABLE, apply=True, export_only=False,
                 uri=world["uri"], export_dir=world["exports"])
    assert rc == 3
    err = capsys.readouterr().err
    assert "需整目录回滚" in err and "禁止半态重跑" in err
    assert "回填后自验失败" in err and "synthetic post-verify failure" in err
    # 与 add 失败空表中间态相区别：add 已成功，新维表 2 行在场
    assert _dim(world["db"], rel.PHASE1_TABLE) == NEW_DIM
    assert world["db"].open_table(rel.PHASE1_TABLE).count_rows() == 2


def test_missing_table_refuses(world, tmp_path):
    other = tmp_path / "empty"
    other.mkdir()
    rc = rel.run(table=rel.PHASE1_TABLE, apply=False, export_only=True,
                 uri=str(other), export_dir=world["exports"])
    assert rc == 2
