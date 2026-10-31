"""E6 hermetic：窗后机产快照生成器 + 重嵌入确定性探针。

不加载真模型、不触活库：
- probe_reembed_determinism：合成 44+1+5+5=55 行 tmp lancedb，确定性/非确定性
  两个 fake embedder 验 rc 0/1、exact bitwise 口径、选行规则、0600 报告；
- gen_post_rebuild_snapshots：marker 日志解析、预冻件 sha 校验、Qwen config
  前置、(a)-(d) 对账机判（全过 + 四类失败）、③ schema 同构与 phase0 旗标。
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
from datetime import datetime, timezone
from pathlib import Path

import lancedb
import numpy as np
import pyarrow as pa
import pytest

from memory_flywheel import embedding_selector as selector
from scripts.kb import gen_post_rebuild_snapshots as gps
from scripts.kb import probe_reembed_determinism as probe
from scripts.kb.gen_prewindow_snapshots import table_snapshot

DIM_OLD = 768
DIM_NEW = 1024
QWEN_NAME = "Qwen/Qwen3-Embedding-0.6B"
MPNET_NAME = ("sentence-transformers/"
              "paraphrase-multilingual-mpnet-base-v2")
KB_TARGET = "c5dd717504665b4a01423bfff3e7ee2d5d800666"
KB_OLD = "aac53313c620efa6217fd51a55834b006d78e689"


def _vec(dim: int, tag: float = 0.01) -> list[float]:
    return [tag] * dim


def _make_table(db, name, dim, fields_extra):
    schema = pa.schema([
        pa.field("id", pa.string()),
        pa.field("text", pa.string()),
        pa.field("vector", pa.list_(pa.float32(), dim)),
        *fields_extra,
    ])
    return db.create_table(name, schema=schema, mode="create")


# ================= probe_reembed_determinism =================

class _StableEmbedding:
    """同文本跨调用逐位相同（由文本 sha 派生，确定性）。"""

    def __init__(self, *a, **k):
        pass

    def embed(self, texts):
        out = []
        for t in texts:
            seed = int(hashlib.sha256(str(t).encode("utf-8")).hexdigest()[:16],
                       16) % (2 ** 32)
            out.append(np.random.RandomState(seed).randn(
                DIM_NEW).astype(np.float32))
        return out


class _JitterEmbedding:
    """每次 embed 调用叠加调用序号噪声 → 跨批必不位等。"""

    calls = 0

    def __init__(self, *a, **k):
        pass

    def embed(self, texts):
        type(self).calls += 1
        n = type(self).calls
        return [np.full(DIM_NEW, 0.01, dtype=np.float32)
                + np.full(DIM_NEW, n * 1e-3, dtype=np.float32)
                + i * 1e-9 for i, _ in enumerate(texts)]


@pytest.fixture
def probe_world(tmp_path, monkeypatch):
    uri = tmp_path / "lance"
    db = lancedb.connect(str(uri))
    ts_field = pa.field("ingested_at", pa.string())
    sc_field = pa.field("source_commit", pa.string())
    ch_field = pa.field("content_hash", pa.string())
    _make_table(db, "phase1_learnings", DIM_OLD,
                [pa.field("namespace", pa.string())])
    _make_table(db, "phase2_learnings", DIM_OLD,
                [pa.field("namespace", pa.string()),
                 pa.field("evolution_round", pa.int64())])
    _make_table(db, "kb_chunks", DIM_OLD, [ts_field, sc_field, ch_field])
    _make_table(db, "solution_chunks", DIM_OLD,
                [ts_field, sc_field, ch_field])

    p1 = [{"id": f"p{i:03d}", "text": f"phase1 文本 {i}",
           "namespace": "capabilities", "vector": _vec(DIM_OLD)}
          for i in range(44)]
    db.open_table("phase1_learnings").add(p1)
    db.open_table("phase2_learnings").add([{
        "id": "ev01", "text": "演化志文本一条", "namespace": "phase2_learnings",
        "evolution_round": 1, "vector": _vec(DIM_OLD)}])
    db.open_table("kb_chunks").add([
        {"id": f"k{i:03d}", "text": f"kb 文本 {i}",
         "ingested_at": "2026-10-12T20:37:48+0800",
         "source_commit": KB_OLD, "content_hash": f"ch-k{i}",
         "vector": _vec(DIM_OLD)} for i in range(20)])
    db.open_table("solution_chunks").add([
        {"id": f"s{i:03d}", "text": f"sol 文本 {i}",
         "ingested_at": "2026-10-31T01:18:01+0800",
         "source_commit": KB_TARGET, "content_hash": f"ch-s{i}",
         "vector": _vec(DIM_OLD)} for i in range(20)])

    monkeypatch.setattr(probe, "select_embedding",
                        lambda: {"model_name": QWEN_NAME, "dim": DIM_NEW,
                                 "cache_dir": str(tmp_path / "nocache")})
    _JitterEmbedding.calls = 0
    return {"uri": str(uri), "out": str(tmp_path / "probe.json"), "db": db}


def test_probe_selects_exactly_55_rows_by_rule(probe_world):
    rows = probe.select_probe_rows(lancedb.connect(probe_world["uri"]))
    assert len(rows) == 55
    by_table = {}
    for r in rows:
        by_table.setdefault(r["table"], []).append(r["id"])
    assert len(by_table["phase1_learnings"]) == 44
    assert by_table["phase2_learnings"] == ["ev01"]
    assert len(by_table["kb_chunks"]) == 5
    assert len(by_table["solution_chunks"]) == 5
    # 各表内按 str(id) 排序
    for ids in by_table.values():
        assert ids == sorted(ids)
    # 44 个 phase1 id 恰为排序后全集前 44（即全集）
    assert by_table["phase1_learnings"] == [f"p{i:03d}" for i in range(44)]
    # kb/solutions 取排序前 5
    assert by_table["kb_chunks"] == [f"k{i:03d}" for i in range(5)]


def test_probe_deterministic_embedder_rc0_0600(probe_world, monkeypatch):
    monkeypatch.setattr(probe, "make_model", lambda cfg: _StableEmbedding())
    rc = probe.run_probe(uri=probe_world["uri"], out=probe_world["out"])
    assert rc == 0
    assert stat.S_IMODE(os.stat(probe_world["out"]).st_mode) == 0o600
    report = json.loads(Path(probe_world["out"]).read_text(encoding="utf-8"))
    assert report["all_equal"] is True
    assert report["mismatch_ids"] == []
    assert report["selection"]["total"] == 55
    assert report["vectors_per_row"] == 4
    assert report["comparison"] == "exact_bitwise_np.array_equal"
    assert report["header"] == {"model": QWEN_NAME, "dim": DIM_NEW}
    assert len(report["results"]) == 55
    assert {r["dim"] for r in report["results"]} == {DIM_NEW}


def test_probe_nondeterministic_embedder_rc1(probe_world, monkeypatch):
    monkeypatch.setattr(probe, "make_model", lambda cfg: _JitterEmbedding())
    rc = probe.run_probe(uri=probe_world["uri"], out=probe_world["out"])
    assert rc == 1
    report = json.loads(Path(probe_world["out"]).read_text(encoding="utf-8"))
    assert report["all_equal"] is False
    assert len(report["mismatch_ids"]) == 55
    assert stat.S_IMODE(os.stat(probe_world["out"]).st_mode) == 0o600


def test_probe_row_count_mismatch_refuses(probe_world, monkeypatch):
    db = probe_world["db"]
    db.open_table("phase1_learnings").delete("id = 'p000'")
    monkeypatch.setattr(probe, "make_model", lambda cfg: _StableEmbedding())
    with pytest.raises(probe.ProbeError, match="43 != 44"):
        probe.run_probe(uri=probe_world["uri"], out=probe_world["out"])
    assert not Path(probe_world["out"]).exists()


# ================= gen_post_rebuild_snapshots =================

def test_marker_log_parsing(tmp_path):
    log = tmp_path / "attempts.log"
    log.write_text(
        "some human note\n"
        '{"event": "backup_complete", "ts": "2026-10-31T10:00:00Z"}\n'
        "garbage{not json\n"
        '{"event": "other_event", "ts": "2026-10-31T10:01:00Z"}\n'
        '{"event": "postwindow_begin", "ts": "2026-10-31T11:00:00Z"}\n',
        encoding="utf-8")
    m = gps.read_window_markers(log)
    assert m["t_backup"] == datetime(2026, 10, 31, 10, 0, tzinfo=timezone.utc)
    assert m["t_post_ts"] == "2026-10-31T11:00:00Z"

    missing = tmp_path / "no.log"
    with pytest.raises(gps.PostRebuildError, match="marker 日志不存在"):
        gps.read_window_markers(missing)

    bad = tmp_path / "bad.log"
    bad.write_text(
        '{"event": "backup_complete", "ts": "2026-10-31 10:00:00"}\n'
        '{"event": "postwindow_begin", "ts": "2026-10-31T11:00:00Z"}\n',
        encoding="utf-8")
    with pytest.raises(gps.PostRebuildError, match="YYYY-MM-DDTHH:MM:SSZ"):
        gps.read_window_markers(bad)

    dup = tmp_path / "dup.log"
    dup.write_text(
        '{"event": "backup_complete", "ts": "2026-10-31T10:00:00Z"}\n'
        '{"event": "backup_complete", "ts": "2026-10-31T10:05:00Z"}\n'
        '{"event": "postwindow_begin", "ts": "2026-10-31T11:00:00Z"}\n',
        encoding="utf-8")
    with pytest.raises(gps.PostRebuildError, match="事件重复"):
        gps.read_window_markers(dup)

    reversed_log = tmp_path / "rev.log"
    reversed_log.write_text(
        '{"event": "backup_complete", "ts": "2026-10-31T12:00:00Z"}\n'
        '{"event": "postwindow_begin", "ts": "2026-10-31T11:00:00Z"}\n',
        encoding="utf-8")
    with pytest.raises(gps.PostRebuildError, match="时序倒置"):
        gps.read_window_markers(reversed_log)


def test_prewindow_sha_pinned_real_file_and_tamper_detection(tmp_path):
    pre = gps.load_prewindow(gps.PREWINDOW_PATH)
    assert pre["schema"] == "simpraxis.phase25.embedswap_prewindow.v1"
    assert pre["header"]["kb_head"] == KB_TARGET
    tampered = tmp_path / "pre.json"
    raw = gps.PREWINDOW_PATH.read_bytes()
    tampered.write_bytes(raw + b" ")
    with pytest.raises(gps.PostRebuildError, match="sha256"):
        gps.load_prewindow(tampered)


def test_assert_qwen_config_guard(tmp_path, monkeypatch):
    cfg_path = tmp_path / "embedding_config.json"
    cfg_path.write_text(json.dumps(
        {"model_name": QWEN_NAME, "dim": DIM_NEW,
         "cache_dir": str(tmp_path)}), encoding="utf-8")
    monkeypatch.setattr(selector, "CONFIG_PATH", cfg_path)
    cfg = gps.assert_qwen_config()
    assert cfg["model_name"] == QWEN_NAME and int(cfg["dim"]) == DIM_NEW

    cfg_path.write_text(json.dumps(
        {"model_name": MPNET_NAME, "dim": DIM_OLD,
         "cache_dir": str(tmp_path)}), encoding="utf-8")
    with pytest.raises(gps.PostRebuildError, match="Qwen3"):
        gps.assert_qwen_config()


def _build_world_db(tmp_path, *, dim, kb_source, sol_source, kb_ts, sol_ts,
                    mutate=None, name="lance", kb_doc_type=False,
                    p1_rung=False):
    """四表合成库；mutate 回调可对 post 行做单点破坏。

    kb_doc_type/p1_rung（M2 负测专用，默认关不影响既有世界）：给 kb_chunks
    加 noprov 列 doc_type、给 phase1_learnings 加 meta 列 rung。
    """
    uri = tmp_path / f"{name}_{dim}"
    db = lancedb.connect(str(uri))
    kb_extra = [pa.field("doc_type", pa.string())] if kb_doc_type else []
    _make_table(db, "kb_chunks", dim, [
        pa.field("ingested_at", pa.string()),
        pa.field("source_commit", pa.string()),
        pa.field("content_hash", pa.string()), *kb_extra])
    _make_table(db, "solution_chunks", dim, [
        pa.field("ingested_at", pa.string()),
        pa.field("source_commit", pa.string()),
        pa.field("content_hash", pa.string())])
    p1_extra = [pa.field("rung", pa.int64())] if p1_rung else []
    _make_table(db, "phase1_learnings", dim,
                [pa.field("namespace", pa.string()), *p1_extra])
    _make_table(db, "phase2_learnings", dim, [
        pa.field("namespace", pa.string()),
        pa.field("evolution_round", pa.int64())])

    kb_rows = [{
        "id": f"k{i}", "text": f"kb 文本 {i}", "ingested_at": kb_ts,
        "source_commit": kb_source, "content_hash": f"ch-k{i}",
        "vector": _vec(dim),
        **({"doc_type": "guide"} if kb_doc_type else {})}
        for i in range(2)]
    sol_rows = [{
        "id": f"s{i}", "text": f"sol 文本 {i}", "ingested_at": sol_ts,
        "source_commit": sol_source, "content_hash": f"ch-s{i}",
        "vector": _vec(dim)} for i in range(1)]
    p1_rows = [{"id": f"p{i}", "text": f"phase1 {i}",
                "namespace": "capabilities", "vector": _vec(dim),
                **({"rung": 1} if p1_rung else {})}
               for i in range(2)]
    p2_rows = [{"id": "e1", "text": "演化志", "namespace": "phase2_learnings",
                "evolution_round": 1, "vector": _vec(dim)}]
    payload = {"kb_chunks": kb_rows, "solution_chunks": sol_rows,
               "phase1_learnings": p1_rows, "phase2_learnings": p2_rows}
    if mutate:
        mutate(payload)
    for name, rows in payload.items():
        db.open_table(name).add(rows)
    return db


def _snapshot_all(db):
    return {n: table_snapshot(db.open_table(n)) for n in gps.TABLES}


def _markers():
    return {
        "t_backup": datetime(2026, 10, 31, 10, 0, tzinfo=timezone.utc),
        "t_post": datetime(2026, 10, 31, 11, 0, tzinfo=timezone.utc),
        "t_backup_ts": "2026-10-31T10:00:00Z",
        "t_post_ts": "2026-10-31T11:00:00Z",
    }


def _pre_from_snapshots(snaps):
    return {"header": {"kb_head": KB_TARGET},
            "tables": {n: {**snaps[n], "missing": False}
                       for n in gps.TABLES}}


def test_reconcile_all_pass(tmp_path):
    pre_db = _build_world_db(
        tmp_path, dim=DIM_OLD, kb_source=KB_OLD, sol_source=KB_TARGET,
        kb_ts="2026-10-12T20:37:48+0800", sol_ts="2026-10-31T01:18:01+0800")
    pre = _pre_from_snapshots(_snapshot_all(pre_db))
    post_db = _build_world_db(
        tmp_path, dim=DIM_NEW, kb_source=KB_TARGET, sol_source=KB_TARGET,
        kb_ts="2026-10-31T18:30:00+0800", sol_ts="2026-10-31T18:30:00+0800")
    cfg = {"model_name": QWEN_NAME, "dim": DIM_NEW}
    report = gps._reconcile(pre=pre, db=post_db, markers=_markers(), cfg=cfg)
    assert report["verdict"] == "pass", json.dumps(report, ensure_ascii=False)
    chk = report["checks"]
    assert chk["a_rows_ids_text_content_hash"]["pass"]
    assert chk["b_meta_noprov"]["pass"]
    assert chk["c_learnings_full_meta"]["pass"]
    d = chk["d_provenance_advance"]
    assert d["pass"]
    assert d["tables"]["kb_chunks"]["direction"]["expected"] == \
        "changed_to_kb_head"
    assert d["tables"]["solution_chunks"]["direction"]["expected"] == \
        "unchanged"


def test_reconcile_detects_four_failure_classes(tmp_path):
    pre_db = _build_world_db(
        tmp_path, dim=DIM_OLD, kb_source=KB_OLD, sol_source=KB_TARGET,
        kb_ts="2026-10-12T20:37:48+0800", sol_ts="2026-10-31T01:18:01+0800")
    pre = _pre_from_snapshots(_snapshot_all(pre_db))
    cfg = {"model_name": QWEN_NAME, "dim": DIM_NEW}

    # 1) phase1 文本被改 → (a) text_sha256 差异
    db = _build_world_db(
        tmp_path, dim=DIM_NEW, kb_source=KB_TARGET, sol_source=KB_TARGET,
        kb_ts="2026-10-31T18:30:00+0800", sol_ts="2026-10-31T18:30:00+0800",
        mutate=lambda p: p["phase1_learnings"][0].update(text="被改过的文本"),
        name="fail1")
    r = gps._reconcile(pre=pre, db=db, markers=_markers(), cfg=cfg)
    assert r["verdict"] == "fail"
    assert r["checks"]["a_rows_ids_text_content_hash"]["tables"][
        "phase1_learnings"]["text_sha256_diff_ids"] == ["p0"]

    # 2) kb 行 source_commit 非目标 → (d) 捕获 id
    db = _build_world_db(
        tmp_path, dim=DIM_NEW, kb_source=KB_OLD, sol_source=KB_TARGET,
        kb_ts="2026-10-31T18:30:00+0800", sol_ts="2026-10-31T18:30:00+0800",
        name="fail2")
    r = gps._reconcile(pre=pre, db=db, markers=_markers(), cfg=cfg)
    d = r["checks"]["d_provenance_advance"]
    assert not d["pass"]
    assert sorted(d["tables"]["kb_chunks"]["source_commit_mismatch_ids"]) == \
        ["k0", "k1"]
    assert d["tables"]["kb_chunks"]["direction_pass"] is False

    # 3) ingested_at 越窗（早于 t_backup）→ (d) 捕获
    db = _build_world_db(
        tmp_path, dim=DIM_NEW, kb_source=KB_TARGET, sol_source=KB_TARGET,
        kb_ts="2026-10-31T09:00:00+0800", sol_ts="2026-10-31T18:30:00+0800",
        name="fail3")
    r = gps._reconcile(pre=pre, db=db, markers=_markers(), cfg=cfg)
    d = r["checks"]["d_provenance_advance"]
    assert not d["pass"]
    assert {x["id"] for x in d["tables"]["kb_chunks"][
        "ingested_at_out_of_window"]} == {"k0", "k1"}

    # 4) solutions 方向被改（c5dd→aac）→ direction_fail
    db = _build_world_db(
        tmp_path, dim=DIM_NEW, kb_source=KB_TARGET, sol_source=KB_OLD,
        kb_ts="2026-10-31T18:30:00+0800", sol_ts="2026-10-31T18:30:00+0800",
        name="fail4")
    r = gps._reconcile(pre=pre, db=db, markers=_markers(), cfg=cfg)
    d = r["checks"]["d_provenance_advance"]
    assert not d["tables"]["solution_chunks"]["direction_pass"]


def test_reconcile_detects_kb_noprov_meta_tamper(tmp_path):
    """M2(b)：kb 改一个 noprov 元数据列（doc_type），text/向量/content_hash
    原样不动 → (a) 仍过，(b) meta_noprov_diff_ids 精确捕获该 id。"""
    pre_db = _build_world_db(
        tmp_path, dim=DIM_OLD, kb_source=KB_OLD, sol_source=KB_TARGET,
        kb_ts="2026-10-12T20:37:48+0800", sol_ts="2026-10-31T01:18:01+0800",
        kb_doc_type=True, name="preb")
    pre = _pre_from_snapshots(_snapshot_all(pre_db))
    db = _build_world_db(
        tmp_path, dim=DIM_NEW, kb_source=KB_TARGET, sol_source=KB_TARGET,
        kb_ts="2026-10-31T18:30:00+0800", sol_ts="2026-10-31T18:30:00+0800",
        kb_doc_type=True,
        mutate=lambda p: p["kb_chunks"][0].update(doc_type="被篡改"),
        name="postb")
    r = gps._reconcile(pre=pre, db=db, markers=_markers(),
                       cfg={"model_name": QWEN_NAME, "dim": DIM_NEW})
    assert r["verdict"] == "fail", json.dumps(r, ensure_ascii=False)
    a = r["checks"]["a_rows_ids_text_content_hash"]["tables"]["kb_chunks"]
    assert a["pass"] and a["text_sha256_diff_ids"] == []
    b = r["checks"]["b_meta_noprov"]["tables"]["kb_chunks"]
    assert not b["pass"]
    assert b["meta_noprov_diff_ids"] == ["k0"]
    assert b["id_set_diff"] == []
    assert "doc_type" in b["noprov_cols_post"]
    # 未篡改行不连带；学习表 (c) 不受影响
    assert r["checks"]["c_learnings_full_meta"]["pass"]


def test_reconcile_detects_phase1_rung_meta_tamper(tmp_path):
    """M2(c)：phase1_learnings 改一行 rung（meta 列），text/向量原样不动
    → (a) 仍过，(c) meta_sha256_diff_ids 精确捕获该 id。"""
    pre_db = _build_world_db(
        tmp_path, dim=DIM_OLD, kb_source=KB_OLD, sol_source=KB_TARGET,
        kb_ts="2026-10-12T20:37:48+0800", sol_ts="2026-10-31T01:18:01+0800",
        p1_rung=True, name="prec")
    pre = _pre_from_snapshots(_snapshot_all(pre_db))
    db = _build_world_db(
        tmp_path, dim=DIM_NEW, kb_source=KB_TARGET, sol_source=KB_TARGET,
        kb_ts="2026-10-31T18:30:00+0800", sol_ts="2026-10-31T18:30:00+0800",
        p1_rung=True,
        mutate=lambda p: p["phase1_learnings"][0].update(rung=9),
        name="postc")
    r = gps._reconcile(pre=pre, db=db, markers=_markers(),
                       cfg={"model_name": QWEN_NAME, "dim": DIM_NEW})
    assert r["verdict"] == "fail", json.dumps(r, ensure_ascii=False)
    a = r["checks"]["a_rows_ids_text_content_hash"][
        "tables"]["phase1_learnings"]
    assert a["pass"] and a["text_sha256_diff_ids"] == []
    c = r["checks"]["c_learnings_full_meta"]["tables"]["phase1_learnings"]
    assert not c["pass"]
    assert c["meta_sha256_diff_ids"] == ["p0"]
    assert c["prov_absent"]
    # phase2 行未动
    assert r["checks"]["c_learnings_full_meta"][
        "tables"]["phase2_learnings"]["pass"]


def test_qwen_flag_predicates():
    ok, name = gps._qwen_hit_flag("Q1", [{
        "project": "lw_benchhub", "doc_type": "quickstart",
        "section_path": "xx > 3.4 命令行拗不过 YAML"}])
    assert name == "yaml_override_hit" and ok
    ok, _ = gps._qwen_hit_flag("Q1", [{
        "project": "lw_benchhub", "doc_type": "code",
        "section_path": "无 YAML"}])
    assert not ok
    ok, name = gps._qwen_hit_flag("Q2", [{
        "project": "lw_benchhub", "text": "检查 EGL 环境变量"}])
    assert name == "egl_hit" and ok
    ok, _ = gps._qwen_hit_flag("Q2", [{
        "project": "genie_sim_v3", "text": "EGL 黑屏"}])
    assert not ok  # test_02 同口径要求 lw_benchhub


class _FakeEngine:
    def __init__(self, hits_by_query):
        self._hits = hits_by_query
        self.total = 1542

    def count(self):
        return self.total

    def query_kb(self, query, **kwargs):
        return self._hits[query]


def test_build_qwen_baseline_is_schema_isomorphic(tmp_path, monkeypatch):
    def hit(i, project, doc_type, text="x"):
        return {"id": f"id{i}", "project": project, "doc_type": doc_type,
                "text": text, "kb_relpath": f"k{i}.md",
                "section_path": f"sec {i}", "content_hash": f"ch{i}",
                "source_commit": KB_TARGET, "chunk_index": i,
                "_distance": float(i)}

    h1 = [hit(1, "lw_benchhub", "quickstart")]
    h1[0]["section_path"] = "3.4 YAML"
    h2 = [hit(2, "lw_benchhub", "troubleshooting", "EGL 排查")]
    h3 = [hit(3, "lw_benchhub", "ai")]
    hits = {}
    q1q2q3 = {}
    for name, query, kwargs in gps.SNAPSHOT_QUERIES:
        hs = {"q1_yaml_override": h1, "q2_egl_black_screen": h2,
              "q3_cuda_illegal_address": h3}[name]
        hits[query] = hs
        q1q2q3[name] = {"ids": [h["id"] for h in hs],
                        "filters": dict(kwargs)}
    mp_doc = {}
    for snap, q_key in (("q1_yaml_override", "Q1"),
                        ("q2_egl_black_screen", "Q2"),
                        ("q3_cuda_illegal_address", "Q3")):
        query, kwargs = gps._SNAPSHOT_QUERY_BY_KEY[snap]
        node = {"query": query, "top_k": kwargs["top_k"],
                "hits": [{"id": f"mp{i}"} for i in range(len(hits[query]))]}
        if q_key == "Q3":
            node["composition"] = [["other", "background"]
                                   for _ in hits[query]]
        mp_doc[q_key] = node
    monkeypatch.setattr(gps, "select_embedding",
                        lambda: {"model_name": QWEN_NAME, "dim": DIM_NEW})
    out = gps.build_qwen_baseline(
        eng=_FakeEngine(hits), q1q2q3=q1q2q3,
        mpnet_baseline={"doc": mp_doc, "header_kb_head": KB_TARGET},
        t_post_ts="2026-10-31T11:00:00Z")
    assert set(out) == {"captured_at", "embedding", "dim",
                        "kb_chunks_total", "phase0_baseline_log", "kb_head",
                        "Q1", "Q2", "Q3"}
    assert out["captured_at"] == "2026-10-31T11:00:00Z"
    assert out["embedding"] == QWEN_NAME and out["dim"] == DIM_NEW
    assert out["kb_head"] == KB_TARGET and out["kb_chunks_total"] == 1542
    assert set(out["Q1"]) == {"query", "top_k", "hits",
                              "yaml_override_hit", "order_matches_phase0"}
    assert set(out["Q2"]) == {"query", "top_k", "hits",
                              "egl_hit", "order_matches_phase0"}
    assert set(out["Q3"]) == {"query", "top_k", "hits",
                              "composition", "matches_phase0"}
    assert out["Q1"]["yaml_override_hit"] is True
    assert out["Q2"]["egl_hit"] is True
    assert out["Q3"]["composition"] == [["lw_benchhub", "ai"]]
    # mp ids 不同 → phase0 对照旗标诚实地为 False
    assert out["Q1"]["order_matches_phase0"] is False
    assert out["Q3"]["matches_phase0"] is False
