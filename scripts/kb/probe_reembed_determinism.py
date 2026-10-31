#!/usr/bin/env python3
"""换嵌入重算向量的位级确定性探针（Phase 2.5 Task6 Stage A，门 2 E6）。

冻结预注册 §5 第 6 步要求机械化验证 Qwen3 重嵌入的跨批次确定性。锁内适配：
预注册只写「重算可复现」原则，本件是其引擎侧落地（taskbook E6 立项）。

抽 55 行（确定性选择，不抽样不随机）：
  phase1_learnings 全 44 行（按 str(id) 排序）
  + phase2_learnings 全 1 行
  + kb_chunks 按 str(id) 排序前 5 行
  + solution_chunks 按 str(id) 排序前 5 行
每行构造四份向量——同批两份（一次 embed 放两份相同文本）＋分两批各一份
（两次独立全量 embed 调用）——四份必须 **exact bitwise** 全等（np.array_equal，
不容差、不 normalize 后比较）。任一行任一分量不符 exit 1。

入参口径与真实重灌逐字一致：phase1/kb/solutions 嵌 text 全文；phase2 嵌
evolution_store.embedding_input(text)（≤2000 全文，超长取前 1200）。
默认线程（不传 threads），离线三开关 setdefault，零联网零付费。
报告 0600 落 /mnt/artifacts/simpraxis/phase25/task6_recall/
（reembed_determinism_probe.json，确定性 JSON、无时间戳/主机名/PID）。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

for _v in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_DATASETS_OFFLINE"):
    os.environ.setdefault(_v, "1")

import numpy as np  # noqa: E402

import lancedb  # noqa: E402

from memory_flywheel.embedding_selector import select_embedding  # noqa: E402
from memory_flywheel.kb_pins import default_lancedb_uri  # noqa: E402
from memory_flywheel.phase2.evolution_store import embedding_input  # noqa: E402

PHASE1_EXPECTED = 44
PHASE2_EXPECTED = 1
FIRST_N_OTHER = 5
PROBE_ROWS_EXPECTED = PHASE1_EXPECTED + PHASE2_EXPECTED + 2 * FIRST_N_OTHER

DEFAULT_OUT = ("/mnt/artifacts/simpraxis/phase25/task6_recall/"
               "reembed_determinism_probe.json")


class ProbeError(RuntimeError):
    """探针前置失败（缺表/行数不符/模型构造失败）。"""


def _read_id_text(tbl) -> list[tuple[str, str]]:
    rows = tbl.search(None).select(["id", "text"]).limit(1_000_000).to_list()
    out = [(str(r["id"]), str(r["text"])) for r in rows]
    out.sort(key=lambda x: x[0])
    return out


def select_probe_rows(db) -> list[dict]:
    """55 行确定性选择；返回 [{table, id, input}]。行数不足 fail-closed。"""
    names = set(db.table_names())
    required = ("phase1_learnings", "phase2_learnings",
                "kb_chunks", "solution_chunks")
    missing = [n for n in required if n not in names]
    if missing:
        raise ProbeError(f"活库缺表 {missing}，探针拒跑")
    phase1 = _read_id_text(db.open_table("phase1_learnings"))
    phase2 = _read_id_text(db.open_table("phase2_learnings"))
    kb = _read_id_text(db.open_table("kb_chunks"))[:FIRST_N_OTHER]
    sol = _read_id_text(db.open_table("solution_chunks"))[:FIRST_N_OTHER]
    if len(phase1) != PHASE1_EXPECTED:
        raise ProbeError(
            f"phase1_learnings {len(phase1)} != {PHASE1_EXPECTED}（活库身份时点不符）")
    if len(phase2) != PHASE2_EXPECTED:
        raise ProbeError(
            f"phase2_learnings {len(phase2)} != {PHASE2_EXPECTED}（活库身份时点不符）")
    if len(kb) < FIRST_N_OTHER or len(sol) < FIRST_N_OTHER:
        raise ProbeError("kb/solutions 不足 5 行，探针拒跑")
    rows = [{"table": "phase1_learnings", "id": i, "input": t}
            for i, t in phase1]
    rows += [{"table": "phase2_learnings", "id": i,
              "input": embedding_input(t)} for i, t in phase2]
    rows += [{"table": "kb_chunks", "id": i, "input": t} for i, t in kb]
    rows += [{"table": "solution_chunks", "id": i, "input": t} for i, t in sol]
    if len(rows) != PROBE_ROWS_EXPECTED:
        raise ProbeError(f"探针选行 {len(rows)} != {PROBE_ROWS_EXPECTED}")
    return rows


def make_model(config: dict):
    """与 embedding_selector 同构造（默认线程，不传 threads）。"""
    from fastembed import TextEmbedding
    return TextEmbedding(model_name=config["model_name"],
                         cache_dir=config["cache_dir"])


def _to_f32(vec) -> np.ndarray:
    return np.asarray(vec, dtype=np.float32)


def run_probe(*, uri: str | None = None, out: str = DEFAULT_OUT) -> int:
    cfg = select_embedding()
    uri = uri or str(default_lancedb_uri())
    db = lancedb.connect(uri)
    rows = select_probe_rows(db)
    texts = [r["input"] for r in rows]

    model = make_model(cfg)
    paired = list(model.embed([t for text in texts for t in (text, text)]))
    batch_b = [_to_f32(v) for v in model.embed(texts)]
    batch_c = [_to_f32(v) for v in model.embed(texts)]
    if len(paired) != 2 * len(rows) or len(batch_b) != len(rows) \
            or len(batch_c) != len(rows):
        raise ProbeError("模型返回条数与输入不符")

    results = []
    mismatch_ids = []
    for i, r in enumerate(rows):
        v1, v2 = _to_f32(paired[2 * i]), _to_f32(paired[2 * i + 1])
        v3, v4 = batch_b[i], batch_c[i]
        shapes = {v.shape for v in (v1, v2, v3, v4)}
        equal = (len(shapes) == 1
                 and all(np.array_equal(v1, v) for v in (v2, v3, v4)))
        results.append({"table": r["table"], "id": r["id"],
                        "dim": int(v1.shape[0]) if v1.ndim else None,
                        "equal": bool(equal)})
        if not equal:
            mismatch_ids.append({"table": r["table"], "id": r["id"]})

    all_equal = not mismatch_ids
    report = {
        "schema": "simpraxis.phase25.reembed_determinism_probe.v1",
        "header": {"model": cfg.get("model_name"),
                   "dim": int(cfg.get("dim"))},
        "selection": {"phase1_learnings": PHASE1_EXPECTED,
                      "phase2_learnings": PHASE2_EXPECTED,
                      "kb_chunks_first": FIRST_N_OTHER,
                      "solution_chunks_first": FIRST_N_OTHER,
                      "total": len(rows)},
        "vectors_per_row": 4,
        "comparison": "exact_bitwise_np.array_equal",
        "threads": "default",
        "results": results,
        "mismatch_ids": mismatch_ids,
        "all_equal": all_equal,
    }
    out_path = Path(out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(out_path.parent, 0o700)
    fd = os.open(str(out_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(report, ensure_ascii=False, sort_keys=True,
                            indent=2) + "\n")
    os.chmod(out_path, 0o600)
    print(f"[probe] {len(rows)} 行 × 4 向量 exact bitwise："
          f"{'全全等' if all_equal else str(len(mismatch_ids)) + ' 行不符'}"
          f" → {out_path}（0600）")
    return 0 if all_equal else 1


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--uri", default=None)
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args(argv)
    try:
        return run_probe(uri=args.uri, out=args.out)
    except ProbeError as exc:
        print(f"[PROBE STOP] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
