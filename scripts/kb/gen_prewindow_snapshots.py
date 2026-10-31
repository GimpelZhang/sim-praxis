#!/usr/bin/env python3
"""Stage A 换嵌入「窗前冻结快照」机械生成器（lead 起草，随冻结提交预冻）。

只读活库（窗口前为 mpnet/768），产出
scripts/phase25/snapshots/embedswap_prewindow_snapshot.json：
四表行数、排序 id 集 sha256（口径＝sorted(str(id))/"\\n".join/utf-8/无尾换行）、
kb/solutions 逐行 content_hash 集与 source_commit 观测值集、四表逐行
(id, sha256(text), meta_sha256, meta_noprov_sha256——剔除
ingested_at/source_commit 两戳记的归约哈希，供 postwindow 仅凭本件机判
「除两戳记外其余列全等」；phase1/2 无两列故两值相等)、
header.kb_head 钉 KB 仓 HEAD（重建后 source_commit 全等目标）、
Q1/Q2/Q3 有序命中 id（过滤器/N 逐字同 migrate_data_layer.SNAPSHOT_QUERIES）。

输出确定性：json sort_keys、indent=2、ensure_ascii=False、单一尾换行；
无时间戳/wall-clock/主机名/PID；头部只有静态 config 模型名/dim、KB 仓
HEAD 与由数据派生的 id 集 sha。同一活库连续两次运行必须逐字节相同（双
lane 据此 diff）。
零联网零付费（离线三开关 setdefault）。
"""
from __future__ import annotations

import hashlib
import json
import os

for _v in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_DATASETS_OFFLINE"):
    os.environ.setdefault(_v, "1")

from memory_flywheel.embedding_selector import select_embedding  # noqa: E402
from memory_flywheel.kb_pins import default_lancedb_uri  # noqa: E402
from scripts.kb.migrate_data_layer import SNAPSHOT_QUERIES  # noqa: E402
import lancedb  # noqa: E402

TABLES = ("kb_chunks", "solution_chunks", "phase1_learnings",
          "phase2_learnings")
CONTENT_HASH_TABLES = {"kb_chunks", "solution_chunks"}
# critic MAJOR-A：换嵌入重灌只许这两列 provenance 戳记前进；
# meta_noprov_sha256 剔除它们后归约，postwindow 可仅凭预冻件机判其余列全等。
PROV_COLS = ("ingested_at", "source_commit")


def _sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def table_snapshot(tbl) -> dict:
    cols = [f.name for f in tbl.schema]
    assert "id" in cols and "text" in cols and "vector" in cols
    non_vector = [c for c in cols if c != "vector"]
    meta_cols = [c for c in non_vector if c not in ("id", "text")]
    rows = tbl.search(None).select(non_vector).limit(1_000_000).to_list()
    rows.sort(key=lambda r: str(r["id"]))

    ids = [str(r["id"]) for r in rows]
    id_set_sha = _sha256_text("\n".join(ids))

    prov_present = [c for c in PROV_COLS if c in meta_cols]
    noprov_cols = [c for c in meta_cols if c not in PROV_COLS]
    per_row = []
    content_hashes: list[str] = []
    source_commits: set[str] = set()
    for r in rows:
        per_row.append({
            "id": str(r["id"]),
            "text_sha256": _sha256_text(str(r["text"])),
            "meta_sha256": _sha256_text(_canonical({c: r.get(c) for c in meta_cols})),
            "meta_noprov_sha256": _sha256_text(
                _canonical({c: r.get(c) for c in noprov_cols})),
        })
        if "content_hash" in meta_cols and r.get("content_hash") is not None:
            content_hashes.append(str(r["content_hash"]))
        if "source_commit" in meta_cols and r.get("source_commit") is not None:
            source_commits.add(str(r["source_commit"]))

    out = {"rows": len(rows), "id_set_sha256": id_set_sha,
           "prov_cols_present": prov_present, "per_row": per_row}
    if tbl.name in CONTENT_HASH_TABLES:
        out["content_hash_set_sha256"] = _sha256_text(
            "\n".join(sorted(set(content_hashes))))
        out["content_hash_count"] = len(set(content_hashes))
        out["source_commit_values_prewindow"] = sorted(source_commits)
    return out


def kb_head() -> str:
    """KB 仓 HEAD 全 sha（冻结的重建后 source_commit 目标）；确定性、只读。"""
    import subprocess
    from memory_flywheel.kb_pins import DEFAULT_KB_ROOT
    return subprocess.run(
        ["git", "-C", str(DEFAULT_KB_ROOT), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True).stdout.strip()


def recall_snapshot(uri: str) -> dict:
    from memory_flywheel.vector_engine import VectorEngine
    eng = VectorEngine(uri=uri)
    snap = {}
    for name, query, kwargs in SNAPSHOT_QUERIES:
        snap[name] = {"ids": [h["id"] for h in eng.query_kb(query, **kwargs)],
                      "filters": {k: v for k, v in kwargs.items()}}
    return snap


def build(uri: str | None = None) -> dict:
    uri = uri or str(default_lancedb_uri())
    cfg = select_embedding()
    db = lancedb.connect(uri)
    present = set(db.table_names())
    tables = {}
    for name in TABLES:
        if name not in present:
            tables[name] = {"rows": None, "id_set_sha256": None,
                            "missing": True}
        else:
            tables[name] = table_snapshot(db.open_table(name))
            tables[name]["missing"] = False
    return {
        "schema": "simpraxis.phase25.embedswap_prewindow.v1",
        "header": {
            "config_model": cfg.get("model_name"),
            "config_dim": cfg.get("dim"),
            "kb_head": kb_head(),
            "uri": uri,
            "table_id_set_sha256": {n: tables[n].get("id_set_sha256")
                                    for n in TABLES},
        },
        "tables": tables,
        "q1q2q3_mpnet_recall": recall_snapshot(uri),
    }


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--uri", default=None)
    ap.add_argument("--out",
                    default="scripts/phase25/snapshots/"
                            "embedswap_prewindow_snapshot.json")
    args = ap.parse_args(argv)

    data = build(args.uri)
    text = json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2)
    out_dir = os.path.dirname(os.path.abspath(args.out))
    os.makedirs(out_dir, mode=0o700, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(text + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
