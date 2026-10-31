#!/usr/bin/env python3
"""Stage A 换嵌入「窗后机产快照/对账」生成器（Phase 2.5 Task6，门 2 引擎批 E6）。

授权：冻结预注册 scripts/phase25/task6_embedding_swap_prereg.md（content
sha256 fcb3fcd6…，门 1 双签 APPROVE）§3.5 + 门 2 taskbook E6。**只在维护
窗口第 7 步、活 config 已切 Qwen3/1024 且四表重灌完成之后由 lead 亲跑**；
本脚本只读活库并写四个机产件，零联网零付费（离线三开关 setdefault）。

唯一允许的窗界时间来源——marker 日志（锁内适配：脚本自身不产生任何时间，
故连跑可逐字节相同）：
  /mnt/artifacts/simpraxis/phase25/task6_recall/embedswap_attempts.log
格式：每行一个 JSON 对象；本脚本只识别以下两种事件，且各必须恰好一行：
  {"event": "backup_complete", "ts": "2026-10-31T18:00:00Z"}
  {"event": "postwindow_begin", "ts": "2026-10-31T18:40:00Z"}
ts 必须是 UTC 严格 Z（YYYY-MM-DDTHH:MM:SSZ）；backup_complete 在整目录
备份完成后、任何重灌之前写入，postwindow_begin 在运行本生成器之前写入。
不接受 --window-start/--window-end 自由参数。

预冻件单输入：scripts/phase25/snapshots/embedswap_prewindow_snapshot.json
的 sha256 必须逐字等于硬编码 PREWINDOW_SHA256（--prewindow 指向他件同样
校验，禁环境变量覆盖）。

产出（确定性 JSON：sort_keys/ensure_ascii=False/indent=2/单一尾换行，
无 wall-clock/主机名/PID）：
  ① scripts/phase25/snapshots/embedswap_qwen_q1q2q3.json
     {name: {ids, filters}}，形状逐字对齐预冻件 q1q2q3_mpnet_recall；
  ② scripts/phase25/snapshots/embedswap_qwen_task0b_recall_snapshot.json
     {name: [ids]}，逐字同构 migrate_data_layer.recall_snapshot；
  ③ /mnt/artifacts/simpraxis/phase25/task6_recall/
     recall_baseline_q1q2q3_qwen.json（0600），与 Task2 mpnet baseline
     （/mnt/.../phase1/task2_direction/recall_baseline_q1q2q3.json，8603B，
     sha256 d67db07c…）schema 同构（同 8 个顶层键、每 Q 同键集）；
  ④ /mnt/artifacts/simpraxis/phase25/task6_recall/
     embedswap_post_rebuild_reconciliation.json（0600，固定名），
     凭预冻件单输入的 (a)-(d) 机判，任一不符 exit 1（JSON 仍落盘附差异）。

口径纪律：canonical/noprov/table_snapshot/TABLES/CONTENT_HASH_TABLES/
PROV_COLS/SNAPSHOT_QUERIES 全部 import 共享，禁止复制粘贴第二口径。
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

for _v in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_DATASETS_OFFLINE"):
    os.environ.setdefault(_v, "1")

import lancedb  # noqa: E402

from memory_flywheel.embedding_selector import select_embedding  # noqa: E402
from memory_flywheel.kb_pins import default_lancedb_uri  # noqa: E402
from memory_flywheel.vector_engine import VectorEngine  # noqa: E402
from scripts.kb.gen_prewindow_snapshots import (  # noqa: E402
    CONTENT_HASH_TABLES,
    PROV_COLS,
    TABLES,
    _canonical,
    kb_head,
    table_snapshot,
)
from scripts.kb.migrate_data_layer import (  # noqa: E402
    SNAPSHOT_QUERIES,
    recall_snapshot as task0b_recall_snapshot,
)
from scripts.kb.reembed_learnings import EXPECTED_ID_SET_SHA  # noqa: E402

QWEN_MODEL = "Qwen/Qwen3-Embedding-0.6B"
QWEN_DIM = 1024  # 仅用于「与 config 双断言」的期望值；dim 真值仍取自 config

PREWINDOW_PATH = (_REPO_ROOT / "scripts" / "phase25" / "snapshots"
                  / "embedswap_prewindow_snapshot.json")
PREWINDOW_SHA256 = (
    "3784e1c9aef6521d7155a670988945bb7257d2e1ab721c896d4615f65648bace")

MPNET_TASK2_BASELINE = Path(
    "/mnt/artifacts/simpraxis/phase1/task2_direction/"
    "recall_baseline_q1q2q3.json")
MPNET_TASK2_SHA256 = (
    "d67db07c78780ad7fcee5e902db374a45a4624bfab01a084919f33a91724f5e7")

ATTEMPT_LOG = Path(
    "/mnt/artifacts/simpraxis/phase25/task6_recall/embedswap_attempts.log")

TASK6_RECALL_DIR = Path("/mnt/artifacts/simpraxis/phase25/task6_recall")
PRODUCT_Q1Q2Q3 = (_REPO_ROOT / "scripts" / "phase25" / "snapshots"
                  / "embedswap_qwen_q1q2q3.json")
PRODUCT_TASK0B = (_REPO_ROOT / "scripts" / "phase25" / "snapshots"
                  / "embedswap_qwen_task0b_recall_snapshot.json")
PRODUCT_BASELINE_QWEN = TASK6_RECALL_DIR / "recall_baseline_q1q2q3_qwen.json"
PRODUCT_RECONCILIATION = (
    TASK6_RECALL_DIR / "embedswap_post_rebuild_reconciliation.json")

_SNAPSHOT_QUERY_BY_KEY = {name: (query, kwargs)
                          for name, query, kwargs in SNAPSHOT_QUERIES}
_Q_KEY_BY_SNAPSHOT = {
    "q1_yaml_override": "Q1",
    "q2_egl_black_screen": "Q2",
    "q3_cuda_illegal_address": "Q3",
}
_Q_EXTRA_FLAGS = ("Q1", "Q2")  # order_matches_phase0
_Q3_FLAG = "Q3"                # composition + matches_phase0


class PostRebuildError(RuntimeError):
    """窗后生成前置失败（当前不是 Qwen3 窗后态即拒跑）。"""


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _dump_json(data) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def _write_repo_file(path: Path, data) -> None:
    """仓内机产件（随提交冻结）：0644，与 prewindow 快照同位同纪律。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_dump_json(data), encoding="utf-8")
    os.chmod(path, 0o644)


def _write_evidence_0600(path: Path, data) -> None:
    """/mnt 证据件：父目录 0700、文件 0600（O_TRUNC 固定名，连跑逐字节可比）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(_dump_json(data))
    os.chmod(path, 0o600)


def _parse_ts(ts: str) -> datetime:
    """严格 UTC Z 时间（marker 唯一允许格式）。"""
    if not (isinstance(ts, str) and ts.endswith("Z")
            and len(ts) == 20):
        raise PostRebuildError(
            f"marker ts 必须是 YYYY-MM-DDTHH:MM:SSZ：{ts!r}")
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc)
    except ValueError as exc:
        raise PostRebuildError(f"marker ts 无法解析：{ts!r}: {exc}") from exc


def _parse_row_ts(ts: str) -> datetime:
    """行内 ingested_at（time.strftime %Y-%m-%dT%H:%M:%S%z，形如 +0800）。"""
    if len(ts) >= 5 and ts[-5] in "+-" and ":" not in ts[-5:]:
        ts = ts[:-5] + ts[-5:-2] + ":" + ts[-2:]
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError as exc:
        raise PostRebuildError(f"行内 ingested_at 无法解析：{ts!r}: {exc}") \
            from exc
    if dt.tzinfo is None:
        raise PostRebuildError(f"行内 ingested_at 缺时区：{ts!r}")
    return dt


def read_window_markers(path: Path = ATTEMPT_LOG) -> dict:
    """解析 marker 日志；backup_complete/postwindow_begin 各恰好一行。"""
    if not path.is_file():
        raise PostRebuildError(
            f"marker 日志不存在：{path}（lead 须在窗口内先写两行 marker）")
    found: dict[str, str] = {}
    duplicates: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue  # 允许日志中混有其他非 JSONL 行
        ev = obj.get("event")
        if ev in ("backup_complete", "postwindow_begin"):
            if ev in found:
                duplicates.append(ev)
            found[ev] = obj.get("ts")
    if duplicates:
        raise PostRebuildError(
            f"marker 事件重复（必须各恰好一行）：{sorted(set(duplicates))}")
    missing = [e for e in ("backup_complete", "postwindow_begin")
               if e not in found]
    if missing:
        raise PostRebuildError(f"marker 日志缺事件：{missing}（{path}）")
    t_backup = _parse_ts(found["backup_complete"])
    t_post = _parse_ts(found["postwindow_begin"])
    if not t_backup <= t_post:
        raise PostRebuildError(
            f"marker 时序倒置：backup_complete={found['backup_complete']} "
            f"postwindow_begin={found['postwindow_begin']}")
    return {"t_backup": t_backup, "t_post": t_post,
            "t_backup_ts": found["backup_complete"],
            "t_post_ts": found["postwindow_begin"]}


def load_prewindow(path: Path) -> dict:
    if not path.is_file():
        raise PostRebuildError(f"预冻件不存在：{path}")
    digest = _sha256_file(path)
    if digest != PREWINDOW_SHA256:
        raise PostRebuildError(
            f"预冻件 sha256 {digest} != 门 1 冻结值 {PREWINDOW_SHA256}，拒跑"
            f"（{path}；--prewindow 指向他件也必须 sha 相符）")
    return json.loads(path.read_text(encoding="utf-8"))


def assert_qwen_config() -> dict:
    cfg = select_embedding()
    if cfg.get("model_name") != QWEN_MODEL:
        raise PostRebuildError(
            f"活 config model_name={cfg.get('model_name')!r} != {QWEN_MODEL}；"
            "本生成器只允许在窗口切 Qwen3 之后运行")
    if int(cfg.get("dim")) != QWEN_DIM:
        raise PostRebuildError(
            f"活 config dim={cfg.get('dim')!r} != {QWEN_DIM}（与 config 双断言失败）")
    return cfg


def verify_live_identities(db) -> None:
    present = set(db.table_names())
    missing = [t for t in TABLES if t not in present]
    if missing:
        raise PostRebuildError(f"活库缺表 {missing}，拒绝产出窗后件")
    for name in TABLES:
        observed = table_snapshot(db.open_table(name))["id_set_sha256"]
        if observed != EXPECTED_ID_SET_SHA[name]:
            raise PostRebuildError(
                f"{name} 活库 id 集 sha {observed} != 预注册 §6 钉死值 "
                f"{EXPECTED_ID_SET_SHA[name]}，拒绝产出窗后件")


def build_q1q2q3(eng: VectorEngine) -> dict:
    """产物①：{name: {ids, filters}}，对齐 prewindow.q1q2q3_mpnet_recall。"""
    out = {}
    for name, query, kwargs in SNAPSHOT_QUERIES:
        hits = eng.query_kb(query, **kwargs)
        out[name] = {"ids": [h["id"] for h in hits],
                     "filters": {k: v for k, v in kwargs.items()}}
    return out


def build_task0b_snapshot(uri: str) -> dict:
    """产物②：{name: [ids]}，逐字同构 migrate_data_layer.recall_snapshot。"""
    return task0b_recall_snapshot(Path(uri))


def _qwen_hit_flag(q_key: str, hits: list[dict]) -> tuple[bool, str]:
    """③ 的语义命中旗标：谓词与 test_02/Task2 基线同口径（机判，可真可假）。"""
    if q_key == "Q1":
        ok = any(h.get("project") == "lw_benchhub"
                 and h.get("doc_type") == "quickstart"
                 and "YAML" in str(h.get("section_path", ""))
                 for h in hits)
        return ok, "yaml_override_hit"
    # test_milestone0_e2e.test_02 同口径：lw_benchhub 命中行正文含 EGL
    ok = any(h.get("project") == "lw_benchhub" and "EGL" in str(h.get("text", ""))
             for h in hits)
    return ok, "egl_hit"


def build_qwen_baseline(*, eng: VectorEngine, q1q2q3: dict,
                        mpnet_baseline: dict, t_post_ts: str) -> dict:
    """产物③：与 Task2 mpnet baseline schema 同构（同键集）。"""
    out = {
        # 锁内适配（窗界机械来源）：captured_at 不取自 wall-clock，而取 lead
        # 在运行本生成器前写入的 postwindow_begin marker（连跑逐字节相同）。
        "captured_at": t_post_ts,
        "embedding": QWEN_MODEL,
        "dim": int(select_embedding()["dim"]),
        "kb_chunks_total": eng.count(),
        # 同构位：mpnet 件此处指 Phase0 重验日志；qwen 件的对应出处是
        # 换嵌入窗口 marker 日志（锁内适配，schema 同构、值如实指出处）。
        "phase0_baseline_log": str(ATTEMPT_LOG),
        "kb_head": mpnet_baseline["header_kb_head"],
    }
    for snap_name, node in q1q2q3.items():
        q_key = _Q_KEY_BY_SNAPSHOT[snap_name]
        query, kwargs = _SNAPSHOT_QUERY_BY_KEY[snap_name]
        hits = eng.query_kb(query, **kwargs)
        if [h["id"] for h in hits] != node["ids"]:
            # ①与③同一引擎同一查询，id 必须逐字一致，否则不下发半成品。
            raise PostRebuildError(
                f"内部一致性失败：{snap_name} ①与③ 有序 id 不一致")
        mp_node = mpnet_baseline["doc"][q_key]
        item = {
            "query": query,
            "top_k": kwargs["top_k"],
            "hits": hits,
        }
        flag_value, flag_name = _qwen_hit_flag(q_key, hits)
        mp_ids = [h["id"] for h in mp_node["hits"]]
        if q_key == _Q3_FLAG:
            item["composition"] = [[h.get("project"), h.get("doc_type")]
                                   for h in hits]
            item["matches_phase0"] = (
                item["composition"] == mp_node["composition"]
                and [h["id"] for h in hits] == mp_ids)
        else:
            item[flag_name] = flag_value
            item["order_matches_phase0"] = [h["id"] for h in hits] == mp_ids
        out[q_key] = item
    expected_top = {"captured_at", "embedding", "dim", "kb_chunks_total",
                    "phase0_baseline_log", "kb_head", "Q1", "Q2", "Q3"}
    assert set(out) == expected_top, set(out) ^ expected_top
    return out


def _per_row_index(snap: dict) -> dict[str, dict]:
    return {r["id"]: r for r in snap["per_row"]}


def _non_vector_meta_cols(db, name: str) -> list[str]:
    cols = [f.name for f in db.open_table(name).schema]
    return [c for c in cols if c not in ("id", "text", "vector")]


def _reconcile(*, pre: dict, db, markers: dict, cfg: dict) -> dict:
    """产物④：凭预冻件单输入机判 (a)-(d)。"""
    pre_tables = pre["tables"]
    post_tables = {n: table_snapshot(db.open_table(n)) for n in TABLES}
    checks: dict[str, dict] = {}

    # (a) 行数 / 排序 id 集 sha / 逐行 text_sha256；kb+solutions content_hash 集
    a = {"pass": True, "tables": {}}
    for name in TABLES:
        pre_n, post_n = pre_tables[name], post_tables[name]
        row_diff = [i for i, (x, y) in enumerate(
            zip(pre_n["per_row"], post_n["per_row"])) if x["id"] != y["id"]]
        pre_idx, post_idx = _per_row_index(pre_n), _per_row_index(post_n)
        text_diff = sorted(set(pre_idx) ^ set(post_idx))
        text_diff += [rid for rid in pre_idx.keys() & post_idx.keys()
                      if pre_idx[rid]["text_sha256"] != post_idx[rid]["text_sha256"]]
        item = {
            "rows": {"pre": pre_n["rows"], "post": post_n["rows"],
                     "pass": pre_n["rows"] == post_n["rows"]},
            "id_set_sha256": {
                "pre": pre_n["id_set_sha256"], "post": post_n["id_set_sha256"],
                "pass": pre_n["id_set_sha256"] == post_n["id_set_sha256"]},
            "per_row_order_mismatch_at": row_diff,
            "text_sha256_diff_ids": sorted(set(text_diff)),
        }
        if name in CONTENT_HASH_TABLES:
            item["content_hash_set_sha256"] = {
                "pre": pre_n["content_hash_set_sha256"],
                "post": post_n["content_hash_set_sha256"],
                "pass": pre_n["content_hash_set_sha256"]
                        == post_n["content_hash_set_sha256"]}
            item["content_hash_count"] = {
                "pre": pre_n["content_hash_count"],
                "post": post_n["content_hash_count"],
                "pass": pre_n["content_hash_count"]
                        == post_n["content_hash_count"]}
        item["pass"] = (item["rows"]["pass"] and item["id_set_sha256"]["pass"]
                        and not item["per_row_order_mismatch_at"]
                        and not item["text_sha256_diff_ids"]
                        and all(v["pass"] for k, v in item.items()
                                if isinstance(v, dict) and "pass" in v))
        a["tables"][name] = item
    a["pass"] = all(v["pass"] for v in a["tables"].values())
    checks["a_rows_ids_text_content_hash"] = a

    # (b) 四表逐行 meta_noprov_sha256 全等（noprov 列集按活 schema 机械派生）
    b = {"pass": True, "tables": {}}
    for name in TABLES:
        pre_idx, post_idx = (_per_row_index(pre_tables[name]),
                             _per_row_index(post_tables[name]))
        diff = [rid for rid in pre_idx.keys() & post_idx.keys()
                if pre_idx[rid]["meta_noprov_sha256"]
                != post_idx[rid]["meta_noprov_sha256"]]
        missing = sorted(set(pre_idx) ^ set(post_idx))
        noprov_cols = [c for c in
                       _non_vector_meta_cols(db, name) if c not in PROV_COLS]
        item = {"meta_noprov_diff_ids": sorted(diff),
                "id_set_diff": missing,
                "noprov_cols_post": noprov_cols,
                "prov_cols_present_post": post_tables[name]["prov_cols_present"],
                "pass": not diff and not missing}
        b["tables"][name] = item
        b["pass"] &= item["pass"]
    checks["b_meta_noprov"] = b

    # (c) phase1/2 无 provenance 两列，故完整 meta_sha256 也必须全等
    c = {"pass": True, "tables": {}}
    for name in ("phase1_learnings", "phase2_learnings"):
        pre_idx, post_idx = (_per_row_index(pre_tables[name]),
                             _per_row_index(post_tables[name]))
        prov_pre = pre_tables[name]["prov_cols_present"]
        prov_post = post_tables[name]["prov_cols_present"]
        diff = [rid for rid in pre_idx.keys() & post_idx.keys()
                if pre_idx[rid]["meta_sha256"] != post_idx[rid]["meta_sha256"]]
        item = {"prov_cols_present_pre": prov_pre,
                "prov_cols_present_post": prov_post,
                "prov_absent": prov_pre == [] and prov_post == [],
                "meta_sha256_diff_ids": sorted(diff),
                "pass": prov_pre == [] and prov_post == [] and not diff}
        c["tables"][name] = item
        c["pass"] &= item["pass"]
    checks["c_learnings_full_meta"] = c

    # (d) kb/solutions source_commit 逐行 == header.kb_head；变更方向与预冻
    #     source_commit_values_prewindow 一致；ingested_at 落闭窗且严格晚于旧值
    kb_target = pre["header"]["kb_head"]
    t_backup, t_post = markers["t_backup"], markers["t_post"]
    d = {"pass": True, "kb_head_target": kb_target,
         "window": {"t_backup": markers["t_backup_ts"],
                    "t_post": markers["t_post_ts"], "inclusive": True},
         "tables": {}}
    # 锁内适配：预冻件只以 canonical 哈希承诺旧 ingested_at（meta_sha256），
    # 不存原值，故「严格晚于 prewindow 旧值」凭 marker 语义机械推出——
    # 预冻快照在 backup_complete 之前完成（旧值 < t_backup），重灌行
    # ingested_at >= t_backup 即严格晚于全部旧值；下方 basis 逐字落报告。
    strict_later_basis = (
        "预冻快照先于整目录备份完成；旧 ingested_at < backup_complete；"
        "post ingested_at ∈ [t_backup, t_post] ⇒ 严格晚于全部 prewindow 旧值")
    for name in ("kb_chunks", "solution_chunks"):
        tbl = db.open_table(name)
        cols = [f.name for f in tbl.schema if f.name != "vector"]
        rows = tbl.search(None).select(cols).limit(1_000_000).to_list()
        bad_sc = sorted({str(r["id"]) for r in rows
                         if str(r.get("source_commit")) != kb_target})
        out_of_window = []
        for r in rows:
            ts = _parse_row_ts(str(r["ingested_at"]))
            if not (t_backup <= ts <= t_post):
                out_of_window.append({"id": str(r["id"]),
                                      "ingested_at": r["ingested_at"]})
        pre_values = pre_tables[name]["source_commit_values_prewindow"]
        post_values = sorted({str(r.get("source_commit")) for r in rows})
        if name == "kb_chunks":
            # kb：aac…（预冻观测）→ kb_head；方向必须「变」且 post 唯一为目标
            direction_ok = (post_values == [kb_target]
                            and pre_values != post_values)
            direction = {"expected": "changed_to_kb_head",
                         "pre": pre_values, "post": post_values}
        else:
            # solutions：预冻已是 kb_head，重灌不允许变方向
            direction_ok = post_values == pre_values == [kb_target]
            direction = {"expected": "unchanged",
                         "pre": pre_values, "post": post_values}
        item = {"source_commit_mismatch_ids": bad_sc,
                "ingested_at_out_of_window": sorted(
                    out_of_window, key=lambda x: x["id"]),
                "direction": direction,
                "direction_pass": direction_ok,
                "strictly_later_basis": strict_later_basis,
                "pass": not bad_sc and not out_of_window and direction_ok}
        d["tables"][name] = item
        d["pass"] &= item["pass"]
    checks["d_provenance_advance"] = d

    verdict = "pass" if all(c["pass"] for c in checks.values()) else "fail"
    return {
        "schema": "simpraxis.phase25.embedswap_postwindow_reconciliation.v1",
        "header": {
            "config_model": cfg.get("model_name"),
            "config_dim": int(cfg.get("dim")),
            "kb_head": kb_head(),
            "prewindow_sha256": PREWINDOW_SHA256,
            "t_backup": markers["t_backup_ts"],
            "t_post": markers["t_post_ts"],
        },
        "checks": checks,
        "verdict": verdict,
    }


def run(*, prewindow: Path | None = None, uri: str | None = None) -> int:
    pre_path = prewindow or PREWINDOW_PATH
    pre = load_prewindow(pre_path)
    cfg = assert_qwen_config()
    markers = read_window_markers()
    uri = uri or str(default_lancedb_uri())
    db = lancedb.connect(uri)
    verify_live_identities(db)

    # mpnet Task2 baseline（③ 的 schema 与「phase0 对照」参照），同样 sha 钉死
    if not MPNET_TASK2_BASELINE.is_file():
        raise PostRebuildError(f"Task2 mpnet baseline 不存在：{MPNET_TASK2_BASELINE}")
    mp_digest = _sha256_file(MPNET_TASK2_BASELINE)
    if mp_digest != MPNET_TASK2_SHA256:
        raise PostRebuildError(
            f"Task2 mpnet baseline sha {mp_digest} != 冻结值 {MPNET_TASK2_SHA256}")
    mp_doc = json.loads(MPNET_TASK2_BASELINE.read_text(encoding="utf-8"))
    # ③kb_head 单输入取自预冻件 header.kb_head（c5dd 重建目标）；mpnet 件自身
    # 的 kb_head=aac 是历史对照，仅用于 Q1-Q3 phase0 排序比对。
    mpnet_baseline = {"doc": mp_doc, "header_kb_head": pre["header"]["kb_head"]}

    eng = VectorEngine(uri=uri)
    q1q2q3 = build_q1q2q3(eng)
    task0b = build_task0b_snapshot(uri)
    # ①/②跨构件同一批查询的有序 id 必须互证
    for name in q1q2q3:
        if q1q2q3[name]["ids"] != task0b[name]:
            raise PostRebuildError(
                f"内部一致性失败：{name} ①ids != ②ids（两构件查询口径漂移）")

    baseline_qwen = build_qwen_baseline(
        eng=eng, q1q2q3=q1q2q3, mpnet_baseline=mpnet_baseline,
        t_post_ts=markers["t_post_ts"])
    report = _reconcile(pre=pre, db=db, markers=markers, cfg=cfg)

    _write_repo_file(PRODUCT_Q1Q2Q3, q1q2q3)
    _write_repo_file(PRODUCT_TASK0B, task0b)
    _write_evidence_0600(PRODUCT_BASELINE_QWEN, baseline_qwen)
    _write_evidence_0600(PRODUCT_RECONCILIATION, report)

    print(f"[postwindow] ① {PRODUCT_Q1Q2Q3}")
    print(f"[postwindow] ② {PRODUCT_TASK0B}")
    print(f"[postwindow] ③ {PRODUCT_BASELINE_QWEN}（0600）")
    print(f"[postwindow] ④ {PRODUCT_RECONCILIATION}（0600）"
          f" verdict={report['verdict']}")
    if report["verdict"] != "pass":
        for name, c in report["checks"].items():
            if not c["pass"]:
                print(f"[postwindow FAIL] {name}", file=sys.stderr)
        return 1
    return 0


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--prewindow", type=Path, default=None,
                    help="预冻件路径（内容 sha256 仍必须 == 冻结值）")
    ap.add_argument("--uri", default=None)
    args = ap.parse_args(argv)
    try:
        return run(prewindow=args.prewindow, uri=args.uri)
    except PostRebuildError as exc:
        print(f"[POSTWINDOW STOP] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
