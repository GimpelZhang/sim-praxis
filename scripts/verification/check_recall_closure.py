#!/usr/bin/env python3
"""生产召回闭环机判（Phase 2 验收 #4：≥1 真实采纳（Task6 端到端），计数与
prompt 文本互证）。

背景：M2 收官时诚实挂牌「真实采纳命中=0——Task6 全程无 Gate2 accept、无成功
V2」。2026-10-26 run27 Gate2 真人 approve（"V2通过"）+ persist/done 之后，闭环
谓词按同一机判标准重新求值，本脚本是其可复跑的机器证据。

五道断言（全部 fail-closed，损坏即非零退出；不放水、不放宽、不模糊匹配）：

  [A] 计数互证：count_adoptions() == jsonl 行数（六字段旧行/七字段新行两种
      严格 schema + 严格 UTC Z，任何损坏行抛错）；并做来源路审计——旧行缺
      source_route → unknown，新行空字段/未知合法值也归 unknown，逐行归类
      不崩；unknown 行与非本闭环线程行只如实挂牌，不计入通过证据。
  [B] id 真实性按路对账：kb 行 id ∈ kb_chunks、solutions 行 ∈
      solution_chunks、learnings 行 ∈ phase1/phase2_learnings；unknown
      旧行按历史写入语义查学习表并集（不可伪造：没有真实行就没有采纳）。
  [C] prompt 互证：闭环线程（THREAD）至少 1 条采纳行的 prompt_sha256 能由
      **实际 req 证据**（redact 前发送串的重建：coding sort_keys=True /
      direction sort_keys=False，与 setter/runner 规范串同源）逐字节复原，
      且该行命中文本（prompt_embedded_text JSON 转义形）按其来源路取真实
      行文本、逐字出现在该 prompt 中；已复原集合内**零容忍**——文本缺失/
      非子串一律判伪；
  [D] 端到端：采纳行同线程在 pending_gates 有 Gate2 DECIDED approve，
      且 LangGraph checkpoint 最新行事件链含 persist_learning/done；
  [E] 时间序：闭环线程采纳行 ts 全部早于 Gate2 decided_at（采纳发生在成功
      结果之前，而非事后补记）。

不能由 req 证据复原的采纳行（证据随 run 归档迁移/异常轮证据缺失）与非闭环
线程行计入 provenance_limited / off_thread 如实报告，不计入通过条件也不掩盖
——通过条件由 [C] 的已复原集合承担。

锁内适配（Phase 2.5 Task6，AC-T6-8）：生产 jsonl 自 Task7/Task8 起已多线程
追加（1077 行/4 线程，全部旧六字段），原 [A]「线程集恰为 {THREAD}」硬断言与
单一学习表 [B] 已过时；改为纯函数 audit_adoption_rows 的来源路审计 + 按路
id 对账，[C]/[D]/[E] 的闭环证据仍严格限定 THREAD 线程，判据/阈值不变。

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

from memory_flywheel.recall import (  # noqa: E402
    LOG_FIELDS,
    LOG_FIELDS_V2,
    SOURCE_ROUTES,
    SOURCE_ROUTE_UNKNOWN,
    count_adoptions,
    iter_adoption_rows,
    prompt_embedded_text,
    row_source_route,
)

THREAD = "t6-genesis-ascent-20261016a"
WINDOW_S = 180  # 未匹配行归因用的同窗 req 文件判定半径（秒）


def audit_adoption_rows(rows: list[dict], *, id_sets: dict[str, set[str]]
                        ) -> dict:
    """纯函数：对**已解析合法**的采纳行做来源路审计与按路 id 对账（AC-T6-8）。

    不读盘、不触网，hermetic 测试可直接合成行调用。容忍四形而不崩：
      - 旧六字段行（缺 source_route）→ unknown，missing_route 计数；
      - 完整七字段新行（kb/solutions/learnings）→ 该路；
      - 空字段（None/""/空白）→ unknown，empty_route 计数；
      - 未知但合法的非空字符串 → unknown，unknown_value_route 计数。
    id 对账：已知路行的 learning_id 必须在该路 id 集合内，否则入 ghost；
    unknown 行按历史唯一写入语义查学习表并集（learnings 集合），查无同样
    入 ghost（route 标 unknown）。unknown/off-thread 行永不在调用方计入通过
    证据——本函数只分类与抓 ghost，不放水。

    id_sets 必含键 kb/solutions/learnings（值为 id set；缺表给空集）。
    """
    for key in ("kb", "solutions", "learnings"):
        if key not in id_sets:
            raise ValueError(f"id_sets 缺键 {key!r}")
    by_route = {r: 0 for r in (*SOURCE_ROUTES, SOURCE_ROUTE_UNKNOWN)}
    detail = {"missing_route": 0, "empty_route": 0,
              "unknown_value_route": 0}
    unknown_values: dict[str, int] = {}
    ghost: list[dict] = []
    classified: list[dict] = []
    for idx, rec in enumerate(rows):
        raw_v = rec.get("source_route", None)
        if "source_route" not in rec:
            detail["missing_route"] += 1
        elif not isinstance(raw_v, str) or not raw_v.strip():
            detail["empty_route"] += 1
        elif raw_v not in SOURCE_ROUTES:
            detail["unknown_value_route"] += 1
            unknown_values[raw_v] = unknown_values.get(raw_v, 0) + 1
        route = row_source_route(rec)
        by_route[route] += 1
        rid = str(rec.get("learning_id", ""))
        id_ok = (rid in id_sets[route]) if route in SOURCE_ROUTES \
            else (rid in id_sets["learnings"])
        if not id_ok:
            ghost.append({"index": idx, "route": route, "learning_id": rid})
        classified.append({"route": route, "learning_id": rid,
                           "ts": rec.get("ts"), "site": rec.get("site"),
                           "thread_id": rec.get("thread_id"),
                           "prompt_sha256": rec.get("prompt_sha256")})
    return {"n": len(rows), "by_route": by_route, "form_detail": detail,
            "unknown_values": unknown_values, "ghost": ghost,
            "rows": classified}


def _live_route_index() -> tuple[dict[str, set[str]], dict[str, dict[str, str]]]:
    """只读连接活 lancedb，取四路 id 集合与 {route: {id: text}}；缺表给空。"""
    import lancedb

    from memory_flywheel import kb_pins
    db = lancedb.connect(kb_pins.default_lancedb_uri())
    names = set(db.table_names())
    id_sets: dict[str, set[str]] = {
        "kb": set(), "solutions": set(), "learnings": set()}
    texts: dict[str, dict[str, str]] = {
        "kb": {}, "solutions": {}, "learnings": {}}

    def _load(table: str, route: str, learnings: bool = False) -> None:
        if table not in names:
            return
        for r in db.open_table(table).search().select(
                ["id", "text"]).limit(1_000_000).to_list():
            rid = str(r["id"])
            id_sets[route].add(rid)
            texts[route][rid] = str(r.get("text", ""))
            if learnings:
                id_sets["learnings"].add(rid)
                # 并集文本以 phase1 先入为准（与召回合并主库优先同序）
                texts["learnings"].setdefault(rid, str(r.get("text", "")))

    _load("kb_chunks", "kb")
    _load("solution_chunks", "solutions")
    _load("phase1_learnings", "learnings", learnings=True)
    _load("phase2_learnings", "learnings", learnings=True)
    return id_sets, texts


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
    # iter_adoption_rows 接受六字段旧行/七字段新行两种冻结 schema，严格 Z；
    # 损坏行/其他 schema fail-closed 抛错（不放水）。
    rows = iter_adoption_rows(recall_path)

    # [A] 计数互证 + schema 两形统计 + 来源路审计（四形不崩；见模块 docstring
    # 的锁内适配说明）。
    n_count = count_adoptions(recall_path)
    if n_count != len(rows):
        _fail(f"[A] count_adoptions={n_count} != 行数 {len(rows)}")
    schema_forms = collections.Counter(
        "v2" if tuple(r.keys()) == LOG_FIELDS_V2 else "legacy"
        for r in rows)
    bad_fields = [i for i, r in enumerate(rows)
                  if tuple(r.keys()) not in (LOG_FIELDS, LOG_FIELDS_V2)]
    if bad_fields:
        _fail(f"[A] schema 不符行：{bad_fields[:5]}")
    by_thread = collections.Counter(r["thread_id"] for r in rows)

    # [B] 按路 id 真实性对账（kb/solutions/learnings；unknown 旧行查学习表并集）。
    id_sets, route_texts = _live_route_index()
    audit = audit_adoption_rows(rows, id_sets=id_sets)
    if audit["ghost"]:
        _fail(f"[B] 采纳行引用了来源路不存在的 id：{audit['ghost'][:5]}")
    adopt_ids = sorted({r["learning_id"] for r in rows})

    # 闭环证据严格限定 THREAD 线程；其余线程行只挂牌（off_thread），不计通过。
    thread_rows = [r for r in rows if r["thread_id"] == THREAD]
    off_thread = len(rows) - len(thread_rows)
    if not thread_rows:
        _fail(f"[A] 闭环线程 {THREAD} 零采纳行（多线程审计：{dict(by_thread)}）")

    # [C] prompt 互证（THREAD 行；req 证据重建 + 按路取真实文本，零容忍）
    req_index, n_req_docs = _index_req_evidence()
    by_prompt: dict[str, list[dict]] = collections.defaultdict(list)
    for r in thread_rows:
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
            route = row_source_route(r)
            # unknown 旧行按历史唯一写入语义取学习表并集文本。
            tmap = route_texts.get(route) or route_texts["learnings"]
            t = tmap.get(r["learning_id"], "")
            if not t or prompt_embedded_text(t) not in pj:
                _fail(f"[C] 已复原 prompt 内命中文本非逐字/缺失："
                      f"route={route} learning_id={r['learning_id']} "
                      f"ts={r['ts']} req={path}")
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

    # [E] 时间序：闭环线程采纳行早于 Gate2 decided_at（他线程不参与本闭环）
    late = [r["ts"] for r in thread_rows
            if _parse_z(r["ts"]) >= decided_at]
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
        "A_count": {
            "count_adoptions": n_count, "rows": len(rows),
            "schema_forms": {"legacy_six_fields": schema_forms["legacy"],
                             "v2_seven_fields": schema_forms["v2"]},
            "legacy_schema_fields": list(LOG_FIELDS),
            "v2_schema_fields": list(LOG_FIELDS_V2),
            "by_thread": dict(by_thread),
            "off_thread_rows": off_thread,
            "closure_thread_rows": len(thread_rows),
        },
        "route_audit": {
            "by_route": audit["by_route"],
            "form_detail": audit["form_detail"],
            "unknown_values": audit["unknown_values"],
            "ghost": audit["ghost"],
        },
        "B_learning_ids": {
            "distinct": len(adopt_ids),
            "all_real_by_route": True,
            "id_set_sizes": {k: len(v) for k, v in id_sets.items()},
        },
        "C_prompt_crosscheck": {
            "population_thread": THREAD,
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
        "E_time_order": {
            "adoption_rows_before_gate2": len(thread_rows) - len(late),
            "late_rows": len(late), "population": "closure_thread_only"},
        "by_site": dict(collections.Counter(r["site"] for r in rows)),
        "by_thread": dict(by_thread),
        "distinct_learning_ids": len(adopt_ids),
    }

    out = Path(args.json_out) if args.json_out else (
        p2 / "recall" /
        f"adoption_crosscheck_{report['ts_utc'].replace(':', '')}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    out.write_text(text, encoding="utf-8")
    os.chmod(out, 0o600)

    print(f"OK 闭环成立：{verified}/{len(thread_rows)} 闭环线程行逐字互证"
          f"（未复原 {prov_limited} 行挂牌；另有 {off_thread} 他线程行只审计不计数）")
    print(f"OK Gate2 approve（{g2[2]}）+ checkpoint {ev[-1]}")
    print(f"报告：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
