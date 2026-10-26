#!/usr/bin/env python3
"""生产召回闭环机判（Phase 2 验收 #4：≥1 真实采纳（Task6 端到端），计数与
prompt 文本互证）。

背景：M2 收官时诚实挂牌「真实采纳命中=0——Task6 全程无 Gate2 accept、无成功
V2」。2026-10-26 run27 Gate2 真人 approve（"V2通过"）+ persist/done 之后，闭环
谓词按同一机判标准重新求值，本脚本是其可复跑的机器证据。

五道断言（全部 fail-closed，损坏即非零退出；不放水、不放宽、不模糊匹配）：

  [A] 计数互证：count_adoptions() == jsonl 行数（六字段严格 schema + 严格
      UTC Z，任何损坏行抛错）；
  [B] 采纳行的学习 id 全部是 phase1_learnings 真实行（不可伪造：没有真实行
      就没有采纳）；
  [C] prompt 互证：至少 1 条采纳行的 prompt_sha256 能由**实际 req 证据**
      （redact 前发送串的重建：coding sort_keys=True / direction
      sort_keys=False，与 setter/runner 规范串同源）逐字节复原，且该行学习
      文本（prompt_embedded_text JSON 转义形）逐字出现在该 prompt 中；
      已复原集合内**零容忍**——文本缺失/非子串一律判伪；
  [D] 端到端：采纳行同线程在 pending_gates 有 Gate2 DECIDED approve，
      且 LangGraph checkpoint 最新行事件链含 persist_learning/done；
  [E] 时间序：采纳行 ts 全部早于 Gate2 decided_at（采纳发生在成功结果之前，
      而非事后补记）。

不能由 req 证据复原的采纳行（证据随 run 归档迁移/异常轮证据缺失）计入
provenance_limited 如实报告，不计入通过条件也不掩盖——通过条件由 [C] 的
已复原集合承担。

用法：
    python scripts/verification/check_recall_closure.py \
        [--json-out PATH]  # 缺省写 $SIMPRAXIS_PHASE2_ROOT/recall/
                           # adoption_crosscheck_<UTC>.json

锁内适配（2026-10-26，攢批第 70 条批次）：计划 L705 验收 #4 的「计数与
prompt 文本互证」在 M2 时点因 Gate2 未建行只能诚实判否；本脚本按同一标准
在 accept 落账后补跑，不改判据、不改阈值。
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from memory_flywheel.phase1.learning_store import LearningStore  # noqa: E402
from memory_flywheel.recall import (  # noqa: E402
    LOG_FIELDS,
    count_adoptions,
    prompt_embedded_text,
)

THREAD = "t6-genesis-ascent-20261016a"
WINDOW_S = 180  # 未匹配行归因用的同窗 req 文件判定半径（秒）


def _phase2_root() -> Path:
    from orchestrator.common.runtime_paths import phase2_root
    return phase2_root()


def _parse_z(ts: str) -> dt.datetime:
    return dt.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=dt.timezone.utc)


def _fail(msg: str) -> None:
    print(f"FAIL {msg}", file=sys.stderr)
    raise SystemExit(1)


def _index_req_evidence() -> tuple[dict, int]:
    """全量索引 thread 的 req 证据 → {prompt_sha256: (site, path)}。

    prompt 重建规范与写入方逐字节同源（见 setter._ask / runner.
    _cached_coding_call 的 prompt_json 注释）：direction（macro_* 文件）
    sort_keys=False、coding sort_keys=True；req 证据存 redact 形 messages，
    无密钥内容时重建即发送原串。扫描范围 /mnt/artifacts/simpraxis 全树
    （run 归档迁移不丢匹配），thread_id 过滤，hash 相等即同一发送串。
    """
    out = subprocess.check_output(
        ["bash", "-c",
         "find /mnt/artifacts/simpraxis -name '*.req.json' 2>/dev/null"],
        text=True)
    index: dict[str, tuple[str, str]] = {}
    n_docs = 0
    for f in out.split():
        try:
            doc = json.loads(Path(f).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        tid = doc.get("thread_id")
        if tid not in (None, THREAD):
            continue
        msgs = doc.get("messages")
        if msgs is None:
            continue
        n_docs += 1
        is_direction = Path(f).name.startswith("macro_")
        pj = json.dumps(msgs, ensure_ascii=False, sort_keys=not is_direction)
        sha = hashlib.sha256(pj.encode("utf-8")).hexdigest()
        index.setdefault(
            sha, ("direction" if is_direction else "coding", f))
    return index, n_docs


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json-out", default=None,
                    help="机判报告 JSON 落点（缺省 recall/ 下带 UTC 戳）")
    args = ap.parse_args()

    p2 = _phase2_root()
    recall_path = p2 / "recall" / "production_recall.jsonl"
    rows = [json.loads(l) for l in
            open(recall_path, encoding="utf-8") if l.strip()]

    # [A] 计数互证（count_adoptions 内部 fail-closed：六字段 + 严格 Z）
    n_count = count_adoptions(recall_path)
    if n_count != len(rows):
        _fail(f"[A] count_adoptions={n_count} != 行数 {len(rows)}")
    bad_fields = [i for i, r in enumerate(rows)
                  if tuple(r.keys()) != LOG_FIELDS]
    if bad_fields:
        _fail(f"[A] schema 不符行：{bad_fields[:5]}")
    threads = {r["thread_id"] for r in rows}
    if threads != {THREAD}:
        _fail(f"[A] 采纳行线程集越界：{threads}")

    # [B] 学习 id 全部真实行
    store = LearningStore()
    if not store.table_exists():
        _fail("[B] phase1_learnings 表不存在")
    table_rows = store.db.open_table("phase1_learnings").search().select(
        ["id", "text"]).limit(10000).to_list()
    texts = {r["id"]: str(r.get("text", "")) for r in table_rows}
    adopt_ids = sorted({r["learning_id"] for r in rows})
    ghost = [i for i in adopt_ids if i not in texts]
    if ghost:
        _fail(f"[B] 采纳行引用了学习表不存在的 id：{ghost[:5]}")

    # [C] prompt 互证（req 证据重建 + 逐字文本；已复原集合零容忍）
    req_index, n_req_docs = _index_req_evidence()
    by_prompt: dict[str, list[dict]] = collections.defaultdict(list)
    for r in rows:
        by_prompt[r["prompt_sha256"]].append(r)
    matched = [s for s in by_prompt if s in req_index]
    unmatched = [s for s in by_prompt if s not in req_index]
    verified = 0
    for s in matched:
        # sha 相等已在索引时证毕；site 决定重建规范（与写入方同源）。
        site, path = req_index[s]
        msgs = json.loads(Path(path).read_text(encoding="utf-8"))["messages"]
        pj = json.dumps(msgs, ensure_ascii=False,
                        sort_keys=(site == "coding"))
        for r in by_prompt[s]:
            t = texts[r["learning_id"]]
            if not t or prompt_embedded_text(t) not in pj:
                _fail(f"[C] 已复原 prompt 内学习文本非逐字/缺失："
                      f"learning_id={r['learning_id']} ts={r['ts']} "
                      f"req={path}")
            verified += 1
    if verified < 1:
        _fail("[C] 零条已复原逐字互证采纳行（验收需 ≥1）")

    # [D] 端到端：Gate2 approve + checkpoint done
    g = sqlite3.connect(
        "file:/mnt/artifacts/simpraxis/phase1/state/pending_gates.db?mode=ro",
        uri=True)
    g2 = g.execute(
        "SELECT status, decision, human_notes, decided_at FROM pending_gates "
        "WHERE thread_id=? AND gate=2 ORDER BY rowid DESC LIMIT 1",
        (THREAD,)).fetchone()
    g.close()
    if not (g2 and g2[0] == "DECIDED" and g2[1] == "approve"):
        _fail(f"[D] Gate2 非 DECIDED approve：{g2}")
    decided_ms = g2[3] if isinstance(g2[3], int) else int(g2[3])
    decided_at = dt.datetime.fromtimestamp(
        decided_ms / 1000, tz=dt.timezone.utc)

    cp_db = "/mnt/artifacts/simpraxis/phase1/state/langgraph_checkpoints.db"
    con = sqlite3.connect(f"file:{cp_db}?mode=ro", uri=True)
    row = con.execute(
        "SELECT checkpoint FROM checkpoints WHERE thread_id=? AND "
        "checkpoint_ns='' ORDER BY rowid DESC LIMIT 1", (THREAD,)).fetchone()
    con.close()
    if row is None:
        _fail(f"[D] checkpoint 库无 {THREAD} 线程行")
    blob = row[0]
    try:
        cp = json.loads(blob)
    except (TypeError, ValueError):
        import msgpack
        cp = msgpack.unpackb(blob, raw=False, strict_map_key=False)
    ev = [e.get("type") if isinstance(e, dict) else e
          for e in (cp.get("channel_values", {}).get("events") or [])]
    if not ("persist_learning" in ev and "done" in ev):
        _fail(f"[D] 最新 checkpoint 事件链缺 persist_learning/done：{ev}")

    # [E] 时间序：全部采纳行早于 Gate2 decided_at
    late = [r["ts"] for r in rows if _parse_z(r["ts"]) >= decided_at]
    if late:
        _fail(f"[E] 存在 Gate2 决定后补记的采纳行（{len(late)}）：{late[:3]}")

    # 未复原行归因（如实报告，不进通过条件）
    prov_limited = 0
    for s in unmatched:
        prov_limited += len(by_prompt[s])

    report = {
        "schema": "simpraxis.recall_closure.v1",
        "ts_utc": dt.datetime.now(dt.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"),
        "thread_id": THREAD,
        "verdict": "closed",
        "recall_path": str(recall_path),
        "A_count": {"count_adoptions": n_count, "rows": len(rows),
                    "schema_fields": list(LOG_FIELDS)},
        "B_learning_ids": {"distinct": len(adopt_ids),
                           "all_in_phase1_learnings": True},
        "C_prompt_crosscheck": {
            "req_docs_indexed": n_req_docs,
            "distinct_prompts": len(by_prompt),
            "prompts_reconstructed": len(matched),
            "rows_verified_verbatim": verified,
            "rows_provenance_limited": prov_limited,
            "rows_on_unreconstructable_prompts": sum(
                len(by_prompt[s]) for s in unmatched),
        },
        "D_end_to_end": {
            "gate2": {"status": g2[0], "decision": g2[1],
                      "human_notes": g2[2],
                      "decided_at": decided_at.strftime("%Y-%m-%dT%H:%M:%SZ")},
            "checkpoint_tail_events": ev[-7:],
        },
        "E_time_order": {"adoption_rows_before_gate2": len(rows) - len(late),
                         "late_rows": len(late)},
        "by_site": dict(collections.Counter(r["site"] for r in rows)),
        "distinct_learning_ids": len(adopt_ids),
    }

    out = Path(args.json_out) if args.json_out else (
        p2 / "recall" /
        f"adoption_crosscheck_{report['ts_utc'].replace(':', '')}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    out.write_text(text, encoding="utf-8")
    os.chmod(out, 0o600)

    print(f"OK 闭环成立：{verified}/{len(rows)} 行逐字互证"
          f"（未复原 {prov_limited} 行如实挂牌）")
    print(f"OK Gate2 approve（{g2[2]}）+ checkpoint {ev[-1]}")
    print(f"报告：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
