"""E2 hermetic 四组合拦截矩阵：换嵌入 config×既有表维度冲突必须 fail-fast。

冻结预注册 §4（task6_embedding_swap_prereg）四格：
  ① 新 config(1024)+旧 kb_chunks(768)：VectorEngine 构造即抛（既有守卫，补钉）；
  ② 新 config+旧 phase1/phase2：LearningStore/EvolutionStore 构造抛（既有反向守卫）；
  ③ 新 config+旧 solution_chunks：SolutionIngester 构造抛（既有）且
     VectorEngine.query_solutions 抛（Phase 2.5 新增 fail-fast）；
  ④ 旧 config(768)+新表(1024)：kb 查询路径 Lance 内核查询期 fail（绕过构造守卫的
     纵深防御实测）、两店构造反向抛、query_solutions 新守卫抛。

全部合成 tmp lancedb + monkeypatch 配置，不加载真模型、不触活库。
"""
from __future__ import annotations

import json
import types

import lancedb
import numpy as np
import pyarrow as pa
import pytest

from memory_flywheel import embedding_selector as selector
from memory_flywheel.phase1.learning_store import LearningStore
from memory_flywheel.phase2.evolution_store import (
    EvolutionSchemaError,
    EvolutionStore,
)
from memory_flywheel.scripts.ingest_solutions import (
    SolutionIngestError,
    SolutionIngester,
)
from memory_flywheel.vector_engine import VectorEngine

# 合成场景维度（hermetic 矩阵专用常量，非活库事实源；活库 dim 只准来自
# select_embedding()["dim"]）。
OLD_DIM = 768
NEW_DIM = 1024
MPNET_NAME = ("sentence-transformers/"
              "paraphrase-multilingual-mpnet-base-v2")
QWEN_NAME = "Qwen/Qwen3-Embedding-0.6B"


@pytest.fixture
def env(tmp_path, monkeypatch):
    uri = tmp_path / "lance"
    for d in ("phase1", "phase2", "runs", "solutions"):
        (tmp_path / d).mkdir(parents=True, exist_ok=True)
    cfg_path = tmp_path / "embedding_config.json"

    def set_config(dim: int) -> None:
        cfg_path.write_text(json.dumps({
            "model_name": QWEN_NAME if dim == NEW_DIM else MPNET_NAME,
            "dim": dim,
            "cache_dir": str(tmp_path / "cache"),
        }), encoding="utf-8")
        monkeypatch.setattr(selector, "CONFIG_PATH", cfg_path)

    db = lancedb.connect(str(uri))
    return {"uri": uri, "db": db, "tmp": tmp_path, "set_config": set_config}


def _make_table(db, name: str, dim: int, *, extra_cols=()):
    cols = [pa.field("id", pa.string())]
    cols += [pa.field(n, pa.string()) for n in extra_cols]
    cols.append(pa.field("vector", pa.list_(pa.float32(), dim)))
    db.create_table(name, schema=pa.schema(cols), mode="create")


# ---------- ① 新 config + 旧 kb ----------

def test_combo1_new_config_old_kb_raises_at_construction(env):
    env["set_config"](OLD_DIM)
    _make_table(env["db"], "kb_chunks", OLD_DIM)
    # 同维可构造（守卫只拦异维；embed 惰性，不触网）
    VectorEngine(uri=str(env["uri"]))
    env["set_config"](NEW_DIM)
    with pytest.raises(RuntimeError, match="kb_chunks"):
        VectorEngine(uri=str(env["uri"]))


# ---------- ② 新 config + 旧 phase1/phase2 ----------

def test_combo2_new_config_old_learning_stores_raise(env):
    _make_table(env["db"], "phase1_learnings", OLD_DIM)
    _make_table(env["db"], "phase2_learnings", OLD_DIM)
    env["set_config"](NEW_DIM)
    with pytest.raises(RuntimeError, match="phase1_learnings"):
        LearningStore(
            uri=str(env["uri"]),
            evidence_root=env["tmp"] / "phase1",
            runs_evidence_root=env["tmp"] / "runs")
    # EvolutionStore 继承 LearningStore.__init__：基类守卫先查 phase1_learnings
    # （super().__init__ 阶段即抛）；其专属 phase2 守卫见下格独立测试。
    with pytest.raises(RuntimeError, match="phase1_learnings|phase2_learnings"):
        EvolutionStore(
            uri=str(env["uri"]),
            evidence_root=env["tmp"] / "phase2",
            runs_evidence_root=env["tmp"] / "runs")


def test_combo2_evolution_own_guard_on_phase2_table(env):
    """phase1 缺席时，EvolutionStore 专属守卫对异维 phase2 表抛 EvolutionSchemaError。"""
    _make_table(env["db"], "phase2_learnings", OLD_DIM)
    env["set_config"](NEW_DIM)
    with pytest.raises(EvolutionSchemaError, match="维度|phase2_learnings"):
        EvolutionStore(
            uri=str(env["uri"]),
            evidence_root=env["tmp"] / "phase2",
            runs_evidence_root=env["tmp"] / "runs")


# ---------- ③ 新 config + 旧 solutions ----------

def test_combo3_new_config_old_solutions_ingester_and_query_raise(env):
    _make_table(env["db"], "solution_chunks", OLD_DIM,
                extra_cols=("solution_id",))
    env["set_config"](NEW_DIM)
    with pytest.raises(SolutionIngestError, match="solution_chunks"):
        SolutionIngester(
            solutions_dir=env["tmp"] / "solutions", uri=str(env["uri"]))
    # kb 表缺席，VectorEngine 构造期不查 solutions（历史行为）→ 构造成功
    eng = VectorEngine(uri=str(env["uri"]))
    assert eng.dim == NEW_DIM
    # 新守卫在 embed/validate 之前抛：无需加载模型即可 fail-fast
    with pytest.raises(RuntimeError, match="solution_chunks"):
        eng.query_solutions("任意查询")
    with pytest.raises(RuntimeError, match="solution_chunks"):
        eng.query_solutions("任意查询", solution_id="01_egl_headless")


# ---------- ④ 旧 config + 新表 ----------

class _FakeEmbedder:
    """按当前 config 模型记录的 768 维伪嵌入（绕过 fastembed，不触网）。"""

    def __init__(self, dim: int):
        self.dim = dim

    def embed(self, texts, batch_size=256):  # 镜像 fastembed 真实签名（env 闸透传）
        for _t in texts:
            yield np.full(self.dim, 0.01, dtype=np.float32)


def test_combo4_old_config_new_tables_raise(env):
    env["set_config"](OLD_DIM)

    # 两店构造反向抛
    _make_table(env["db"], "phase1_learnings", NEW_DIM)
    _make_table(env["db"], "phase2_learnings", NEW_DIM)
    with pytest.raises(RuntimeError, match="phase1_learnings"):
        LearningStore(
            uri=str(env["uri"]),
            evidence_root=env["tmp"] / "phase1",
            runs_evidence_root=env["tmp"] / "runs")
    with pytest.raises(RuntimeError, match="phase1_learnings|phase2_learnings"):
        EvolutionStore(
            uri=str(env["uri"]),
            evidence_root=env["tmp"] / "phase2",
            runs_evidence_root=env["tmp"] / "runs")

    # ingester 反向守卫对称
    _make_table(env["db"], "solution_chunks", NEW_DIM,
                extra_cols=("solution_id",))
    with pytest.raises(SolutionIngestError, match="solution_chunks"):
        SolutionIngester(
            solutions_dir=env["tmp"] / "solutions", uri=str(env["uri"]))

    # query_solutions 新守卫：先于 Lance 内核给出可读处置
    eng = VectorEngine(uri=str(env["uri"]))
    with pytest.raises(RuntimeError, match="solution_chunks"):
        eng.query_solutions("任意查询")

    # kb 查询路径：绕过构造守卫（建引擎时表缺席）后，768 查询向量打 1024 表，
    # Lance 内核在查询期拒绝（纵深防御实测）。
    eng._embedder = _FakeEmbedder(OLD_DIM)
    _make_table(env["db"], "kb_chunks", NEW_DIM)
    # 构造期守卫此刻不会重跑；search 由 Lance 内核拒绝异维向量
    with pytest.raises(Exception, match=r"(?i)dimension|维度|vector"):
        eng.query_kb("EGL 黑屏如何排查", top_k=3)


def test_query_solutions_guard_fires_before_embed_call(env):
    """守卫必须先于 embed：异维时即使 embedder 损坏也不影响报错。"""
    env["set_config"](NEW_DIM)
    _make_table(env["db"], "solution_chunks", OLD_DIM,
                extra_cols=("solution_id",))
    eng = VectorEngine(uri=str(env["uri"]))

    def _boom(texts):
        raise AssertionError("异维时不得调用 embed")

    eng._embedder = types.SimpleNamespace(embed=_boom)
    with pytest.raises(RuntimeError, match="solution_chunks"):
        eng.query_solutions("任意查询")
