"""E1 hermetic 测试：embedding_selector 显式登记件校验（Phase 2.5 换嵌入）。

不加载真模型、不触活 /mnt 缓存：tmp_path 造 refs/main→snapshot→blobs 两级
符号链接缓存树，TextEmbedding 以 sys.modules 注入伪件。覆盖：
- Qwen3 双件快乐路径（逐件清单 + 主件/数据件路径键）；
- onnx_data 缺失/截断/sha 掉包、量化件出现、refs/main 缺失、缓存子目录错位、
  refs/main 指向非预钉快照；
- 模块源码不再含 SIMPRAXIS_ONNX_SHA256 env 覆盖分支；
- select_embedding 端到端 config schema 钉死（含 finite/非零/维度守卫）；
- MiniLM 末位候选保留 legacy 最小字节闸且无伪造 bytes/sha。
"""
from __future__ import annotations

import dataclasses
import hashlib
import inspect
import json
import os
import sys
import types

import numpy as np
import pytest

from memory_flywheel import embedding_selector as es


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _make_cache(root, hf_repo: str, snapshot: str, files: dict, *,
                refs_text: str | None = None, extra_files=()):
    """造 HF 两级形态缓存：blobs/<sha> 实体 + snapshots/<sha>/<rel> 符号链接。"""
    repo = root / ("models--" + hf_repo.replace("/", "--"))
    snap_dir = repo / "snapshots" / snapshot
    snap_dir.mkdir(parents=True)
    blobs = repo / "blobs"
    blobs.mkdir(parents=True)
    for rel, blob in files.items():
        blobsha = _sha(blob)
        bpath = blobs / blobsha
        if not bpath.exists():
            bpath.write_bytes(blob)
        link = snap_dir / rel
        link.parent.mkdir(parents=True, exist_ok=True)
        # 与真实 HF 缓存同形：snapshots/<sha>/onnx/x → ../../../blobs/<sha>
        link.symlink_to(os.path.relpath(bpath, link.parent))
    for rel, blob in extra_files:
        p = snap_dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(blob)
    refs = repo / "refs"
    refs.mkdir(parents=True, exist_ok=True)
    (refs / "main").write_text(refs_text if refs_text is not None else snapshot)
    return snap_dir


def _qwen_candidate(tmp_cache, files_meta):
    """以测试常量替换 Qwen3 登记件（缓存树同样指向 tmp_cache）。"""
    base = es.PREFERENCES[0]
    return dataclasses.replace(base, files=tuple(files_meta)), tmp_cache


ONNX_MAIN = b"fake-onnx-main"
ONNX_DATA = b"fake-onnx-data-payload"
QUANT = b"fake-quantized"


def _qwen_tree(root, *, blobs=None, refs_text=None, with_quantized=False):
    blobs = blobs if blobs is not None else {
        "onnx/model.onnx": ONNX_MAIN,
        "onnx/model.onnx_data": ONNX_DATA,
    }
    extra = [( "onnx/model_quantized.onnx", QUANT)] if with_quantized else ()
    return _make_cache(
        root, "Qdrant/Qwen3-Embedding-0.6B-onnx",
        "af95f2c416ffe9379369ad64f9113e865db6112c", blobs,
        refs_text=refs_text, extra_files=extra)


def _registered_qwen(root):
    files_meta = [
        ("onnx/model.onnx", len(ONNX_MAIN), _sha(ONNX_MAIN)),
        ("onnx/model.onnx_data", len(ONNX_DATA), _sha(ONNX_DATA)),
    ]
    cand, _ = _qwen_candidate(root, files_meta)
    return cand


def test_qwen_happy_path_two_files(tmp_path):
    _qwen_tree(tmp_path)
    cand = _registered_qwen(tmp_path)
    art = es._verify_onnx_artifact(str(tmp_path), cand)
    assert set(art) == {"onnx_path", "onnx_bytes", "onnx_sha256",
                        "onnx_files", "onnx_data_path"}
    assert art["onnx_bytes"] == len(ONNX_MAIN)
    assert art["onnx_sha256"] == _sha(ONNX_MAIN)
    rels = [f["relpath"] for f in art["onnx_files"]]
    assert rels == ["onnx/model.onnx", "onnx/model.onnx_data"]
    for f in art["onnx_files"]:
        assert set(f) == {"relpath", "bytes", "sha256", "resolved_path"}
        assert f["bytes"] > 0 and len(f["sha256"]) == 64
        # resolved_path 必须跟随两级符号链接落到 blob 实体且可读
        assert es.Path(f["resolved_path"]).is_file()
    assert art["onnx_path"].endswith("model.onnx")
    assert art["onnx_data_path"].endswith("model.onnx_data")


def test_qwen_data_file_missing(tmp_path):
    _qwen_tree(tmp_path, blobs={"onnx/model.onnx": ONNX_MAIN})
    cand = _registered_qwen(tmp_path)
    with pytest.raises(RuntimeError, match="缓存件缺失"):
        es._verify_onnx_artifact(str(tmp_path), cand)


def test_qwen_data_file_truncated(tmp_path):
    _qwen_tree(tmp_path)
    base = es.PREFERENCES[0]
    cand = dataclasses.replace(base, files=(
        ("onnx/model.onnx", len(ONNX_MAIN), _sha(ONNX_MAIN)),
        # 期望字节数大于实际 → 截断
        ("onnx/model.onnx_data", len(ONNX_DATA) + 1, _sha(ONNX_DATA)),
    ))
    with pytest.raises(RuntimeError, match="字节数不符"):
        es._verify_onnx_artifact(str(tmp_path), cand)


def test_qwen_data_file_sha_swapped(tmp_path):
    _qwen_tree(tmp_path)
    base = es.PREFERENCES[0]
    cand = dataclasses.replace(base, files=(
        ("onnx/model.onnx", len(ONNX_MAIN), _sha(ONNX_MAIN)),
        ("onnx/model.onnx_data", len(ONNX_DATA), "0" * 64),
    ))
    with pytest.raises(RuntimeError, match="SHA256 掉包"):
        es._verify_onnx_artifact(str(tmp_path), cand)


def test_qwen_quantized_file_rejected(tmp_path):
    _qwen_tree(tmp_path, with_quantized=True)
    cand = _registered_qwen(tmp_path)
    with pytest.raises(RuntimeError, match="量化件|model_quantized"):
        es._verify_onnx_artifact(str(tmp_path), cand)


def test_refs_main_missing(tmp_path):
    _qwen_tree(tmp_path)
    (tmp_path / "models--Qdrant--Qwen3-Embedding-0.6B-onnx" / "refs"
     / "main").unlink()
    cand = _registered_qwen(tmp_path)
    with pytest.raises(RuntimeError, match="refs/main 缺失"):
        es._verify_onnx_artifact(str(tmp_path), cand)


def test_cache_subdir_derived_from_hf_repo_not_model_name(tmp_path):
    # 仿旧 model_name 推导的错位目录名（models--Qwen--Qwen3-Embedding-0.6B）
    wrong = tmp_path / "models--Qwen--Qwen3-Embedding-0.6B"
    snap = wrong / "snapshots" / "af95f2c416ffe9379369ad64f9113e865db6112c"
    (snap / "onnx").mkdir(parents=True)
    (snap / "onnx" / "model.onnx").write_bytes(ONNX_MAIN)
    (snap / "onnx" / "model.onnx_data").write_bytes(ONNX_DATA)
    refs = wrong / "refs"
    refs.mkdir(parents=True)
    (refs / "main").write_text("af95f2c416ffe9379369ad64f9113e865db6112c")
    cand = _registered_qwen(tmp_path)
    with pytest.raises(RuntimeError, match="缓存子目录缺失"):
        es._verify_onnx_artifact(str(tmp_path), cand)


def test_refs_main_points_to_missing_snapshot(tmp_path):
    _qwen_tree(tmp_path, refs_text="deadbeef0000000000000000000000000000beef")
    cand = _registered_qwen(tmp_path)
    with pytest.raises(RuntimeError, match="快照目录不存在"):
        es._verify_onnx_artifact(str(tmp_path), cand)


def test_refs_main_points_to_unpinned_snapshot(tmp_path):
    # 另造一个存在的非预钉快照且 refs/main 指向它
    _qwen_tree(tmp_path)
    other = "0123456789abcdef0123456789abcdef01234567"
    repo = tmp_path / "models--Qdrant--Qwen3-Embedding-0.6B-onnx"
    (repo / "snapshots" / other).mkdir(parents=True)
    (repo / "refs" / "main").write_text(other)
    cand = _registered_qwen(tmp_path)
    with pytest.raises(RuntimeError, match="与预钉快照"):
        es._verify_onnx_artifact(str(tmp_path), cand)


def test_cache_root_missing(tmp_path):
    cand = _registered_qwen(tmp_path / "nope")
    with pytest.raises(RuntimeError, match="缓存目录不存在"):
        es._verify_onnx_artifact(str(tmp_path / "nope"), cand)


def test_no_env_sha_override_or_rglob_in_source():
    import ast

    src = inspect.getsource(es)
    tree = ast.parse(src)
    # 无 SIMPRAXIS_ONNX_SHA256_* 环境分支：代码中的字符串常量（拼接旧 env 名
    # 也算）不得再出现该 token；模块 docstring 的「已删除」历史说明除外。
    docstrings: set[ast.AST] = set()
    for scope in ast.walk(tree):
        if isinstance(scope, (ast.Module, ast.FunctionDef,
                               ast.AsyncFunctionDef, ast.ClassDef)) \
                and scope.body and isinstance(scope.body[0], ast.Expr) \
                and isinstance(scope.body[0].value, ast.Constant) \
                and isinstance(scope.body[0].value.value, str):
            docstrings.add(scope.body[0].value)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and node not in docstrings \
                and "SIMPRAXIS_ONNX_SHA256" in node.value:
            raise AssertionError("不得保留 SIMPRAXIS_ONNX_SHA256 env 覆盖口")
    for node in ast.walk(tree):
        # 禁止 glob/rglob 取件（注释里的「rglob」字样不是调用）
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in {"rglob", "glob"}, \
                f"禁止 rglob/glob 取末写法，发现 .{node.func.attr}(...)"
        # 禁止 sorted(...)[-1] 取末
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Call):
            call = node.value
            if isinstance(call.func, ast.Name) and call.func.id == "sorted":
                raise AssertionError("禁止 sorted(...)[-1] 取末写法")


def test_preferences_order_and_pinned_constants():
    names = [c.model_name for c in es.PREFERENCES]
    assert names == [
        "Qwen/Qwen3-Embedding-0.6B",
        "sentence-transformers/paraphrase-multilingual-mpnet-base-v2",
        "BAAI/bge-small-zh-v1.5",
        "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
    ]
    dims = {c.model_name: c.dim for c in es.PREFERENCES}
    assert dims["Qwen/Qwen3-Embedding-0.6B"] == 1024
    assert dims["sentence-transformers/paraphrase-multilingual-mpnet-base-v2"] == 768
    qwen = es.PREFERENCES[0]
    assert qwen.hf_repo == "Qdrant/Qwen3-Embedding-0.6B-onnx"
    assert [f[0] for f in qwen.files] == ["onnx/model.onnx",
                                          "onnx/model.onnx_data"]
    assert qwen.reject_files == ("onnx/model_quantized.onnx",)
    mpnet = es.PREFERENCES[1]
    assert mpnet.hf_repo == "Xenova/paraphrase-multilingual-mpnet-base-v2"
    assert mpnet.files[0][:2] == ("onnx/model.onnx", 1110059084)
    bge = es.PREFERENCES[2]
    assert bge.hf_repo == "Qdrant/bge-small-zh-v1.5"
    assert bge.files[0][:2] == ("model_optimized.onnx", 94781076)


def test_minilm_legacy_row_has_no_fabricated_constants():
    minilm = es.PREFERENCES[3]
    assert minilm.files == ()
    assert minilm.snapshot is None
    assert minilm.legacy_file == "model_optimized.onnx"
    assert minilm.legacy_min_bytes == 300 * 1024 * 1024
    assert minilm.hf_repo == "Qdrant/paraphrase-multilingual-MiniLM-L12-v2-onnx-Q"


def test_minilm_legacy_min_bytes_gate(tmp_path):
    cand = es.PREFERENCES[3]
    snap = _make_cache(
        tmp_path, cand.hf_repo, "anysnapshot",
        {"model_optimized.onnx": b"x" * 16})
    with pytest.raises(RuntimeError, match="疑似截断"):
        es._verify_onnx_artifact(str(tmp_path), cand)
    # 放宽到测试可造尺寸后通过（legacy 不钉 sha，实测登记）
    cand2 = dataclasses.replace(cand, legacy_min_bytes=8)
    art = es._verify_onnx_artifact(str(tmp_path), cand2)
    assert art["onnx_data_path"] is None
    assert art["onnx_files"][0]["relpath"] == "model_optimized.onnx"
    assert art["onnx_files"][0]["bytes"] == 16


class _FakeQwenEmbedding:
    """模拟 fastembed TextEmbedding：1024 维，前两条近、第三条远。"""

    last_init = None

    def __init__(self, model_name=None, cache_dir=None, **kwargs):
        type(self).last_init = {"model_name": model_name,
                                "cache_dir": cache_dir, "kwargs": kwargs}

    def embed(self, docs):
        v0 = np.zeros(1024, dtype=np.float32)
        v0[0] = 1.0
        v1 = np.zeros(1024, dtype=np.float32)
        v1[0] = 0.995
        v1[1] = 0.1
        v1 /= np.linalg.norm(v1)
        v2 = np.zeros(1024, dtype=np.float32)
        v2[1] = 1.0
        return [v0, v1, v2]


def _run_select_with_fake(tmp_path, monkeypatch, fake_cls, *, cand=None):
    _qwen_tree(tmp_path)
    cand = cand or _registered_qwen(tmp_path)
    monkeypatch.setattr(es, "PREFERENCES", [cand])
    monkeypatch.setattr(es, "CONFIG_PATH", tmp_path / "embedding_config.json")
    # select_embedding 的缓存目录只从 FASTEMBED_CACHE_PATH 读取，指向 tmp 缓存树
    monkeypatch.setenv("FASTEMBED_CACHE_PATH", str(tmp_path))
    monkeypatch.setitem(sys.modules, "fastembed",
                        types.SimpleNamespace(TextEmbedding=fake_cls))
    return es.select_embedding(force=True), str(tmp_path)


def test_select_embedding_config_schema_qwen(tmp_path, monkeypatch):
    cfg, cache = _run_select_with_fake(tmp_path, monkeypatch, _FakeQwenEmbedding)
    assert cfg["model_name"] == "Qwen/Qwen3-Embedding-0.6B"
    assert cfg["dim"] == 1024
    assert set(cfg["onnx_files"][0]) == {"relpath", "bytes", "sha256",
                                         "resolved_path"}
    assert cfg["onnx_path"].endswith("model.onnx")
    assert cfg["onnx_data_path"].endswith("model.onnx_data")
    assert cfg["cache_dir"] == cache
    assert cfg["sanity"]["sim_related"] > cfg["sanity"]["sim_unrelated"] + 0.05
    # TextEmbedding 收注册名与 cache_dir，无 sources 注入
    assert _FakeQwenEmbedding.last_init["model_name"] == \
        "Qwen/Qwen3-Embedding-0.6B"
    # 配置落盘且可再读
    on_disk = json.loads(es.CONFIG_PATH.read_text(encoding="utf-8"))
    assert on_disk["onnx_files"][1]["relpath"] == "onnx/model.onnx_data"


def test_select_embedding_rejects_nan_vectors(tmp_path, monkeypatch):
    class NanEmbedding(_FakeQwenEmbedding):
        def embed(self, docs):
            vecs = list(super().embed(docs))
            vecs[2][5] = float("nan")
            return vecs

    with pytest.raises(RuntimeError, match="所有候选嵌入模型均不可用"):
        _run_select_with_fake(tmp_path, monkeypatch, NanEmbedding)


def test_select_embedding_rejects_zero_vectors(tmp_path, monkeypatch):
    class ZeroEmbedding(_FakeQwenEmbedding):
        def embed(self, docs):
            return [np.zeros(1024, dtype=np.float32) for _ in docs]

    with pytest.raises(RuntimeError, match="所有候选嵌入模型均不可用"):
        _run_select_with_fake(tmp_path, monkeypatch, ZeroEmbedding)


def test_select_embedding_rejects_wrong_dim(tmp_path, monkeypatch):
    class Dim768(_FakeQwenEmbedding):
        def embed(self, docs):
            return [np.ones(768, dtype=np.float32) for _ in docs]

    with pytest.raises(RuntimeError, match="所有候选嵌入模型均不可用"):
        _run_select_with_fake(tmp_path, monkeypatch, Dim768)


def test_cached_config_reused_without_loading(tmp_path, monkeypatch):
    cfg_path = tmp_path / "embedding_config.json"
    cfg_path.write_text(json.dumps(
        {"model_name": "sentence-transformers/paraphrase-multilingual-"
                       "mpnet-base-v2", "dim": 768}), encoding="utf-8")
    monkeypatch.setattr(es, "CONFIG_PATH", cfg_path)

    def _boom(*a, **k):
        raise AssertionError("复用配置时不得 import/构造 fastembed")

    monkeypatch.setitem(sys.modules, "fastembed",
                        types.SimpleNamespace(TextEmbedding=_boom))
    out = es.select_embedding()
    assert out["dim"] == 768
