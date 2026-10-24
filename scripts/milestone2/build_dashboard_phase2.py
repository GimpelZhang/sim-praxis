#!/usr/bin/env python3
"""Task9：生成 Milestone 2 RSI 看板 rsi_dashboard_phase2.json（计划 L618-635，
§6.6 对账方法 L798-800；0 付费、确定性、只读活件）。

schema simpraxis.rsi_dashboard_phase2.v1，必含段：
- version/git/generated_at；
- tests：pytest 实跑收集清单（collect-only 计数，禁止手抄过期数）与终态全绿
  机判（由 LEAD 在 Task7/8 提交后以 --tests-result 喂入真实全跑结果；未喂入
  时如实为 null，不把收集清单冒充全绿）；
- lance_tables：四表行数实读（kb_chunks/phase1_learnings/solution_chunks/
  phase2_learnings；phase2_learnings gate2 accept 从未发生 → 活表不存在，
  如实 exists=false/rows=0，不造表）；
- production_recall：219 行生产采纳遥测明细（复用 memory_flywheel.recall
  的严格计数口径 + 按 site/thread_id 聚合）；
- pareto：V1 冻结档案六指标 + Q1 矛盾结论（0.824<界 2.0，矛盾即结论、未翻案），
  V2 终态无 Pareto 判定（v2_summary.json 从未生成，Gate2 从未建行）；
- rung4_calibration：record_only 终态（D7 未达成，禁止声称 active）；
- paid_reconciliation：phase2 新账整数 seq=实呼（120）+ call_cache 命中口径
  （0 账行调用）+ phase1 封存账 29 行只读 + blocked 6 行单列；
- closeout：docker ps -a 零 sim_praxis 容器、显存 ≤200MiB、df /mnt、git
  攒批未推送事实（只读 ahead 计数；锁内适配：用户常设指令覆盖计划 L635）。

输出落 $SIMPRAXIS_PHASE2_ROOT/task9_milestone/。本脚本不调用 ModelGateway、
不联网、不读凭据内容；pytest collect-only 离线运行。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))

import lancedb  # noqa: E402

from memory_flywheel.kb_pins import default_lancedb_uri  # noqa: E402
from memory_flywheel.recall import LOG_FIELDS, count_adoptions  # noqa: E402
from orchestrator.common import secrets as secret_scan  # noqa: E402
from orchestrator.common.clock import parse_ts_iso_strict, utc_now_iso_z  # noqa: E402
from orchestrator.common.runtime_paths import phase1_root, phase2_root  # noqa: E402
from orchestrator.gateway.quota_ledger import call_lines  # noqa: E402

SCHEMA = "simpraxis.rsi_dashboard_phase2.v1"
VERSION = 1

ASCENT_THREAD = "t6-genesis-ascent-20261016a"
GPU_VRAM_LIMIT_MIB = 200
# 实呼时间戳（秒）与缓存 put 时间戳的允许偏差：账行在 call 入口写、缓存 ts
# 在成功返回时写（实测 seq119 差 1s）；仅在时间戳不逐字相等且 (kind,usage)
# 唯一匹配时启用，窗口外/歧义一律 fail-closed。
CACHE_TS_MATCH_WINDOW_S = 10
COLLECT_TIMEOUT_S = 300

# 终态机判所需关键活件；缺失即 fail-closed（V2 summary 缺如是真实终态，
# 不在此列）。
REQUIRED_PHASE2 = [
    ("quota/ledger.jsonl", "phase2 台账"),
    ("recall/production_recall.jsonl", "生产采纳遥测"),
    ("task4_judge/calibration_bineval.json", "Rung4 BinEval 标定件"),
    (f"evolution_sources/{ASCENT_THREAD}/v1_summary.json", "V1 冻结增强 summary"),
]


class DashboardError(RuntimeError):
    """看板取数 fail-closed：缺件/账证不符一律停，不编数字。"""


def _load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DashboardError(f"JSON 件不可读/非法：{path}（{exc}）") from exc


def _load_jsonl(path: Path) -> list[dict]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise DashboardError(f"JSONL 件不可读：{path}（{exc}）") from exc
    rows = []
    for i, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except ValueError as exc:
            raise DashboardError(f"{path} 第 {i} 行 JSON 非法：{exc}") from exc
    return rows


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _run_cmd(argv: list[str], *, timeout: int = 30, cwd: Path | None = None,
             env: dict | None = None) -> tuple[int, str, str]:
    """命令收口（测试 monkeypatch 本函数即全 hermetic，不触 docker/GPU/git）。"""
    proc = subprocess.run(argv, cwd=str(cwd) if cwd else None, env=env,
                          capture_output=True, text=True, timeout=timeout)
    return proc.returncode, proc.stdout, proc.stderr


# ---------------------------------------------------------------------------
# tests 段：实跑收集（inventory）与终态全绿（terminal verdict）分离
# ---------------------------------------------------------------------------

def parse_collect_output(stdout: str) -> dict:
    """纯函数解析 ``pytest --collect-only -q`` 输出为节点 id 清单。

    collect-only 的节点行形如 ``tests/milestone0/test_x.py::test_y``；
    摘要行（``N tests collected``/``error``）与空行不计。
    """
    ids: list[str] = []
    for line in stdout.splitlines():
        line = line.strip()
        if "::" in line and not line.startswith(("=", "-")) and " " not in line:
            ids.append(line)
    return {"n_collected": len(ids), "node_ids": ids}


def sanitize_node_ids(node_ids: list[str]) -> tuple[list[str], int]:
    """对 collect-only 节点 id 做单一源脱敏（只处理严格口径命中者）。

    背景：密钥扫描自测（milestone1 test_scan_detects_all_strict_forms）在
    源码里运行时拼接假名，git 扫描无字面量；但收集期参数化 id 会展开为完整
    密钥形状。证据包扫描只按形状，故看板不得把这些 id 原样入产物。
    检测用严格口径（证据 FATAL 口径），脱敏用单一源 redact_text（保留已知
    前缀）；未命中的普通节点 id 逐字节不动，避免宽模式过杀。脱敏后仍命中即
    fail-closed。n_collected 与清单长度不变，可对收集输出重放本函数复现。
    """
    out: list[str] = []
    redacted = 0
    for nid in node_ids:
        if secret_scan.contains_secret(nid):
            masked = secret_scan.redact_text(nid)
            if secret_scan.contains_secret(masked):
                raise DashboardError(
                    "节点 id 经单一源 redact_text 脱敏后仍命中密钥严格形状，"
                    f"拒绝产出看板：{nid.split('[')[0]}[...]")
            out.append(masked)
            redacted += 1
        else:
            out.append(nid)
    return out, redacted


def collect_tests(run=_run_cmd) -> dict:
    """实跑收集（两排除口径与收尾命令逐字一致）；离线 env，不执行用例。"""
    argv = [sys.executable, "-m", "pytest", "tests", "--collect-only", "-q",
            "-k", "not test_04", "-m", "not paid and not kb_integration"]
    env = {**os.environ,
           "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
           "HF_DATASETS_OFFLINE": "1"}
    try:
        rc, out, err = run(argv, timeout=COLLECT_TIMEOUT_S, cwd=_REPO, env=env)
    except Exception as exc:  # noqa: BLE001 - 收集失败要带命令证据 fail-closed
        return {"command": argv, "ok": False, "error": f"{type(exc).__name__}: {exc}",
                "n_collected": 0, "node_ids": [], "redacted_node_ids": 0}
    parsed = parse_collect_output(out)
    safe_ids, n_red = sanitize_node_ids(parsed["node_ids"])
    return {"command": argv, "ok": rc == 0, "n_collected": parsed["n_collected"],
            "node_ids": safe_ids,
            "redacted_node_ids": n_red,
            "redaction_note": (
                "milestone1 密钥扫描自测参数化 id 在收集期展开为合成密钥形状"
                "（源码运行时拼接、无字面量）；经 orchestrator.common.secrets "
                "单一源脱敏，n_collected 与清单长度不变，仅这 "
                f"{n_red} 个 id 的密钥本体被替换为 ***REDACTED***，可对 "
                "pytest --collect-only 输出重放 sanitize_node_ids 复现。"),
            "collect_stderr_tail": err[-800:] if err else ""}


def read_terminal_result(path: Path | None) -> dict | None:
    """终态全绿机判件（LEAD 在 Task7/8 提交后实跑全套并喂入）。

    未喂入 → None（如实标注）；喂入则强校验字段，failed/skipped/deselected
    必须是机产非负整数且 git_clean 为布尔——禁止用本通道手抄过期数。
    """
    if path is None:
        return None
    doc = _load_json(path)
    required = {"command", "passed", "failed", "skipped", "deselected",
                "git_clean"}
    missing = required - set(doc)
    if missing:
        raise DashboardError(f"终态机判件缺字段 {sorted(missing)}：{path}")
    for k in ("passed", "failed", "skipped", "deselected"):
        if type(doc[k]) is not int or doc[k] < 0:
            raise DashboardError(f"终态机判件 {k} 必须是非负整数：{path}")
    if type(doc["git_clean"]) is not bool:
        raise DashboardError(f"终态机判件 git_clean 必须是布尔：{path}")
    doc["green"] = doc["failed"] == 0 and doc["git_clean"]
    return doc


# ---------------------------------------------------------------------------
# 账：phase2 ledger 对账（§6.6）
# ---------------------------------------------------------------------------

def reconcile_ledger(rows: list[dict]) -> dict:
    """整数 seq 行=实呼；preflight_blocked 单列；seq 连续性 fail-closed。"""
    calls = call_lines(rows)
    blocked = [r for r in rows if r.get("event") == "preflight_blocked"]
    unknown = [r for r in rows
               if r.get("event") not in ("call", "preflight_blocked")]
    if unknown:
        raise DashboardError(
            "ledger 出现未知 event：" + ", ".join(sorted({
                str(r.get("event")) for r in unknown})))
    # call 行必须都有整数 seq；blocked 行必须 seq 为 null。
    bad_call = [r for r in rows if r.get("event") == "call"
                and not isinstance(r.get("seq"), int)]
    bad_block = [r for r in blocked if r.get("seq") is not None]
    if bad_call or bad_block:
        raise DashboardError("ledger seq/event 口径混乱：call 须整数 seq、"
                             f"blocked 须 null（bad_call={len(bad_call)}, "
                             f"bad_block={len(bad_block)}）")
    seqs = sorted(r["seq"] for r in calls)
    contiguous = seqs == list(range(1, len(seqs) + 1))
    if not contiguous:
        raise DashboardError(
            f"实呼 seq 不连续：{len(seqs)} 行但非 1..N 完备集")
    all_z = all(str(r.get("ts_iso", "")).endswith("Z") for r in rows)
    if not all_z:
        raise DashboardError("ledger 存在非严格 UTC Z 时间戳（ts_iso 须以 Z 结尾）")
    by_stage = Counter(r["stage"] for r in calls)
    by_task = Counter(str(r["task"]) for r in calls)
    by_batch = Counter(r["batch_id"] for r in calls)
    return {
        "lines": len(rows),
        "calls_real": len(calls),
        "seq_min": seqs[0] if seqs else None,
        "seq_max": seqs[-1] if seqs else None,
        "seq_contiguous": contiguous,
        "ok": sum(1 for r in calls if r["ok"]),
        "failed": sum(1 for r in calls if not r["ok"]),
        "blocked_preflight": len(blocked),
        "all_ts_utc_z": all_z,
        "by_stage": dict(sorted(by_stage.items())),
        "by_task": dict(sorted(by_task.items())),
        "by_batch": dict(sorted(by_batch.items())),
        "calls_with_evidence_ref": sum(
            1 for r in calls if isinstance(r.get("evidence"), str)
            and r["evidence"]),
        "blocked_detail": [
            {"ts_iso": r["ts_iso"], "stage": r.get("stage"),
             "task": r.get("task"), "batch_id": r.get("batch_id"),
             "err": r.get("err")} for r in blocked],
    }


# ---------------------------------------------------------------------------
# .call_cache 命中口径（§6.6，难点见模块 docstring 与看板 method 段）
# ---------------------------------------------------------------------------

def cache_inventory(cache_dir: Path, calls: list[dict]) -> dict:
    """缓存件清单与「命中」可辩护口径。

    事实结构（orchestrator/common/callcache.py、runner.py:1694-1697、
    setter.py:601-609）：
    - 每个 .json 是一次**成功首呼后** put 的结果（key 维内容寻址，同 key
      覆盖）；条目本身不是命中事件；
    - coding 侧命中直接 return，**不落任何持久标记**（无日志/无证据/无账行），
      故事后无法从缓存状态权威重放命中数；
    - macro 侧 setter 把 replay 布尔写进 proposals attempts（权威标记）。

    口径：①每个条目必须与且仅与一个 ok 整数 seq 账行配对（ts 逐字相等；
    不逐字时仅在 ±10s 且 kind+usage 唯一匹配时配对 seq119 型时钟差），
    配不上即 fail-closed——put 只可能发生在账行成功返回之后；
    ②macro 命中数取 proposals 实读 replay=true 计数（外部传入）；
    ③coding 命中数标记为 structurally_unobservable，以账证连续性、
    g6_watch 日志 CACHE_NEW/CACHE_HIT 信号交叉佐证；
    ④逻辑调用总数 = 实呼 + 有证据命中；coding 侧残余不确定性如实标注。
    """
    ok_rows = [r for r in calls if r.get("ok")]
    entries: list[dict] = []
    unmatched: list[str] = []
    if not cache_dir.is_dir():
        return {"dir": str(cache_dir), "exists": False, "n_entries": 0,
                "entries": [], "matched_calls": 0}
    for f in sorted(cache_dir.glob("*.json")):
        rec = _load_json(f)
        ts = rec.get("ts_iso", "")
        kind = rec.get("kind", "")
        usage = rec.get("usage")
        exact = [r for r in ok_rows if r.get("ts_iso") == ts]
        method = "ts_exact"
        match = exact[0] if len(exact) == 1 else None
        if match is None:
            # 时钟差配对：同 stage、usage 逐字相等、窗口内唯一
            try:
                ts_dt = parse_ts_iso_strict(ts)
            except (ValueError, TypeError):
                ts_dt = None
            near = []
            if ts_dt is not None:
                for r in ok_rows:
                    if r.get("stage") != kind:
                        continue
                    if not _usage_equal(r, usage):
                        continue
                    try:
                        delta = abs(
                            (parse_ts_iso_strict(r["ts_iso"]) - ts_dt)
                            .total_seconds())
                    except (ValueError, TypeError):
                        continue
                    if delta <= CACHE_TS_MATCH_WINDOW_S:
                        near.append(r)
            if len(near) == 1:
                match = near[0]
                method = "ts_window_usage_unique"
        entry = {"key": f.stem, "file": f.name, "kind": kind,
                 "ts_iso": ts, "usage": usage, "size": f.stat().st_size,
                 "matched_seq": match["seq"] if match else None,
                 "match_method": method if match else "unmatched"}
        entries.append(entry)
        if match is None:
            unmatched.append(f.name)
    if unmatched:
        raise DashboardError(
            ".call_cache 有条目配不上任何 ok 整数 seq 账行（命中不写缓存、"
            "put 必随成功账行；不配即账证不符）：" + ", ".join(unmatched))
    matched = sorted({e["matched_seq"] for e in entries})
    return {
        "dir": str(cache_dir), "exists": True,
        "n_entries": len(entries),
        "n_distinct_matched_calls": len(matched),
        "matched_seqs": matched,
        "entries": entries,
        "method": (
            "条目=成功首呼后的 put 状态（非命中事件）；逐条与 ok 整数 seq "
            "账行配对（ts 逐字相等，或 10s 内 kind+usage 唯一匹配）。coding "
            "侧重放命中在 runner.py 静默返回、无持久标记，故事后不可权威"
            "重放；命中数取 macro proposals attempts[].replay 实读，coding "
            "以 seq 连续性与 g6_watch 日志 CACHE_* 信号交叉佐证。"),
    }


def _usage_equal(row: dict, usage) -> bool:
    return (isinstance(usage, list) and len(usage) == 2
            and row.get("tokens_in") == usage[0]
            and row.get("tokens_out") == usage[1])


def macro_replay_hits(direction_dir: Path) -> dict:
    """macro proposals attempts[].replay 权威标记实读（setter.py:681-685）。"""
    files = sorted(direction_dir.glob(
        f"proposals_{ASCENT_THREAD}_r*.json"))
    n_true = n_false = 0
    per_file = []
    for p in files:
        doc = _load_json(p)
        attempts = doc.get("attempts")
        if attempts is None:  # 兼容 round_meta 旧键名
            attempts = doc.get("round_meta") or []
        t = sum(1 for a in attempts if a.get("replay") is True)
        f = sum(1 for a in attempts if a.get("replay") is False)
        n_true += t
        n_false += f
        per_file.append({"file": p.name, "attempts": len(attempts),
                         "replay_true": t, "replay_false": f})
    return {"proposals_files": per_file,
            "macro_replay_hits_evidenced": n_true,
            "macro_first_calls_evidenced": n_false}


def watch_cache_signals(ascent_logs_dir: Path) -> dict:
    """g6_watch 人工观察日志的 CACHE_NEW/CACHE_HIT 信号计数（交叉佐证）。"""
    counts = Counter()
    files = []
    for log in sorted(ascent_logs_dir.glob("g6_watch_*.log")):
        if log.is_symlink():
            continue  # .latest.log 软链只在证据包 walker 登记；计数跳过以免跟随目标双计
        n_new = n_hit = 0
        for line in log.read_text(encoding="utf-8", errors="ignore").splitlines():
            if "CACHE_NEW" in line:
                n_new += 1
            if "CACHE_HIT" in line:
                n_hit += 1
        files.append({"file": log.name, "cache_new": n_new,
                      "cache_hit": n_hit})
        counts["new"] += n_new
        counts["hit"] += n_hit
    return {"files": files, "cache_new_signals": counts["new"],
            "cache_hit_signals": counts["hit"]}


def paid_reconciliation(*, p2_root: Path, p1_root: Path) -> tuple[dict, list[dict]]:
    """组装付费双账对账段；同时返回缓存清单 entries（供导出件复用）。"""
    rows = _load_jsonl(p2_root / "quota" / "ledger.jsonl")
    led = reconcile_ledger(rows)
    calls = call_lines(rows)

    cache_dir = p1_root / ".call_cache"  # 锁内适配：phase2 沿用 phase1 根下缓存
    inv = cache_inventory(cache_dir, calls)
    macro = macro_replay_hits(p1_root / "task2_direction")
    watch = watch_cache_signals(p2_root / "runs" / "task6_ascent")

    sealed_path = p1_root / "quota" / "ledger.jsonl"
    sealed_lines = 0
    sealed_readonly = True
    if sealed_path.is_file():
        sealed_lines = sum(
            1 for l in sealed_path.read_text(encoding="utf-8").splitlines()
            if l.strip())
    else:
        sealed_readonly = False

    # 有证据的 0 账行调用（命中）：macro proposals 权威标记；coding 侧无标记。
    hits_evidenced = macro["macro_replay_hits_evidenced"]
    coding_observable = False
    block = {
        "phase2_ledger": {"path": str(p2_root / "quota" / "ledger.jsonl"),
                          **led},
        "phase1_sealed_ledger": {
            "path": str(sealed_path), "lines": sealed_lines,
            "present": sealed_readonly,
            "access": "read_only：Phase 2 任何路径不得追加（§6.6）"},
        "call_cache": {
            "dir": inv["dir"], "exists": inv["exists"],
            "n_entries": inv["n_entries"],
            "n_distinct_matched_calls": inv.get("n_distinct_matched_calls", 0),
            "matched_seqs": inv.get("matched_seqs", []),
            "method": inv["method"],
            "inventory_export": "task9_milestone/call_cache_inventory.json",
        },
        "macro_replay": macro,
        "watch_signals": watch,
        "zero_ledger_calls": {
            "hits_evidenced_total": hits_evidenced,
            "macro_hits_evidenced": hits_evidenced,
            "coding_hits": {
                "count": None,
                "status": "structurally_unobservable",
                "detail": (
                    "coding 命中在 runner.py:1694-1697 静默返回，无日志/证据/"
                    "账行标记；交叉佐证：实呼 seq 1..N 连续无缺失、缓存条目"
                    "逐条配对成功、g6_watch 仅见 CACHE_NEW 无 CACHE_HIT。"),
            },
        },
        "logical_calls_total": {
            "value": led["calls_real"] + hits_evidenced,
            "formula": "实呼整数 seq 行数 + 有证据重放命中数",
            "calls_real": led["calls_real"],
            "hits_evidenced": hits_evidenced,
            "coding_side_uncertainty": (
                "coding 侧若发生过重放命中不会留下持久证据；当前账证/日志"
                "交叉佐证未发现，但该残余不确定性不可由本看板消除。"),
        },
        "blocked_listed_separately": led["blocked_preflight"],
        "note": ("实呼数=整数 seq 行数（§6.6）；preflight_blocked 单列不计"
                 "实呼；Phase 1 封存账只读；缓存条目是成功首呼的 put 状态，"
                 "不是命中计数。"),
    }
    return block, inv.get("entries", [])


# ---------------------------------------------------------------------------
# production_recall 明细
# ---------------------------------------------------------------------------

def production_recall_block(path: Path) -> dict:
    total = count_adoptions(path)  # 严格六字段 + 严格 Z，损坏即抛
    rows = _load_jsonl(path)
    by_site = Counter(r["site"] for r in rows)
    by_thread = Counter(r["thread_id"] for r in rows)
    by_site_thread: dict[str, dict] = {}
    for r in rows:
        d = by_site_thread.setdefault(r["site"], Counter())
        d[r["thread_id"]] += 1
    fields_ok = all(tuple(r.keys()) == LOG_FIELDS for r in rows)
    ts_sorted = all(
        rows[i - 1]["ts"] <= rows[i]["ts"] for i in range(1, len(rows)))
    return {
        "path": str(path), "total_rows": total, "fields": list(LOG_FIELDS),
        "fields_valid": fields_ok, "ts_nondecreasing": ts_sorted,
        "by_site": dict(sorted(by_site.items())),
        "by_thread": dict(sorted(by_thread.items())),
        "by_site_thread": {k: dict(sorted(v.items()))
                           for k, v in sorted(by_site_thread.items())},
        "distinct_learning_ids": len({r["learning_id"] for r in rows}),
        "distinct_prompt_sha256": len({r["prompt_sha256"] for r in rows}),
        "ts_first": rows[0]["ts"] if rows else None,
        "ts_last": rows[-1]["ts"] if rows else None,
    }


# ---------------------------------------------------------------------------
# 四表行数
# ---------------------------------------------------------------------------

def lance_tables_block(uri: str | None = None) -> dict:
    uri = uri or default_lancedb_uri()
    p = Path(uri)
    db = lancedb.connect(uri)
    names = set(db.table_names())
    table_specs = [
        ("kb_chunks", "KB 切块基线（只读，永不写）"),
        ("phase1_learnings", "Phase 1 学习行（含 Task6/run7a 活表 12 行）"),
        ("solution_chunks", "方案切块独立第三表"),
        ("phase2_learnings", "Phase 2 升阶 accept 行（无 accept 即无表）"),
    ]
    tables = []
    for name, desc in table_specs:
        exists = name in names
        # 锁内适配（规则 7）：计划 L628 写 phase1_learnings=5 是 Task0a 时点
        # 旧值；看板取活值（Task6 human_failure 回写 + run7a 后实读 12）。
        rows_n = int(db.open_table(name).count_rows()) if exists else 0
        tables.append({"table": name, "exists": exists, "rows": rows_n,
                       "note": desc})
    return {
        "uri": uri,
        "uri_is_symlink": p.is_symlink(),
        "uri_resolved": str(p.resolve(strict=False)),
        "tables": tables,
        "phase2_learnings_note": (
            "gate2 accept 从未发生，phase2_learnings 表在活库不存在（"
            "table_exists=False）即真实终态；看板不造表、不填占位行。"),
    }


# ---------------------------------------------------------------------------
# V1→V2 Pareto 段
# ---------------------------------------------------------------------------

def pareto_block(p2_root: Path) -> dict:
    sdir = p2_root / "evolution_sources" / ASCENT_THREAD
    v1_path = sdir / "v1_summary.json"
    v2_path = sdir / "v2_summary.json"
    if not v1_path.is_file():
        raise DashboardError(f"V1 冻结增强 summary 缺失：{v1_path}")
    v1 = _load_json(v1_path)
    contrast = v1.get("contrast") or {}
    # Q1 红线（硬规则 7）：矛盾事实逐字呈现，不改名、不改判。
    q1 = {
        "metric": "ice_over_normal_postpush_ratio",
        "observed": contrast.get("ice_over_normal_postpush_ratio"),
        "preregistered_boundary": contrast.get("preregistered_ratio"),
        "hypothesis_status": contrast.get("hypothesis_status"),
        "control_stands": contrast.get("control_stands"),
        "ordered_slip": contrast.get("ordered_slip"),
        "l571_note": contrast.get("l571_note"),
        "conclusion": ("预注册方向相反的矛盾结果（0.824 < 界 2.0），矛盾即"
                       "结论；V2 五战失败未翻案，禁止调参/换种子/改界。"),
    }
    evo_dir = p2_root / "evolutions"
    notes = [{"file": f.name, "sha256": _sha256(f), "size": f.stat().st_size}
             for f in sorted(evo_dir.glob("*.md"))]
    run_dir = p2_root / "runs" / "task6_ascent" / (
        f"run_{ASCENT_THREAD}_1")
    observed = None
    envelope = None
    sp = run_dir / "summary.json"
    if sp.is_file():
        s = _load_json(sp)
        # 仅作机产事实呈现：未经 evaluate seam 默认口径重算/采纳，不是
        # Pareto 机判输入（v2_summary.json 才是，而它从未生成）。
        observed = {
            "run_dir": str(run_dir), "summary_schema": s.get("schema"),
            "groups_success_rate": {
                k: v.get("success_rate") for k, v in
                (s.get("groups") or {}).items()},
            "groups_n_fell": {k: v.get("n_fell") for k, v in
                              (s.get("groups") or {}).items()},
            "provenance": "V2 批次物理跑过（run7b/7c 期工作目录），但图从未"
                          "产出 v2_summary.json；本块是 run 目录原始观察值，"
                          "非 Pareto 机判、非 Gate2 证据。",
        }
    fp = run_dir / "FAILURE.json"
    if fp.is_file():
        fdoc = _load_json(fp)
        envelope = {"path": str(fp), "status": fdoc.get("status"),
                    "reason": fdoc.get("reason"),
                    "coding_round": fdoc.get("coding_round"),
                    "executions": fdoc.get("executions")}
    return {
        "v1_summary_path": str(v1_path),
        "v1_pareto_metrics": v1.get("pareto_metrics"),
        "v1_contrast": contrast,
        "q1_contradiction": q1,
        "v2_summary_path": str(v2_path),
        "v2_summary_present": v2_path.is_file(),
        "pareto_verdict": None,
        "no_verdict_reason": (
            "V2 从未通过 Gate2（gate2 全程未建行），v2_summary.json 从未"
            "生成；按 pareto.py 与失败演化志，终态既非 non_dominated 亦非 "
            "dominated_by_v1——是升阶方案未跨过代码出生/结构闸的工程结局，"
            "不构成对 V1 科学结论的支持或削弱。"),
        "evolution_failure_notes": notes,
        "v2_batch_observed_nonauthoritative": observed,
        "final_envelope": envelope,
    }


# ---------------------------------------------------------------------------
# Rung4 标定模式段
# ---------------------------------------------------------------------------

def rung4_block(p2_root: Path) -> dict:
    cal_path = p2_root / "task4_judge" / "calibration_bineval.json"
    fail_path = p2_root / "task4_judge" / "bineval_failure_report.json"
    if not cal_path.is_file():
        raise DashboardError(f"Rung4 标定件缺失：{cal_path}")
    cal = _load_json(cal_path)
    mode = cal.get("mode")
    if mode != "record_only":
        raise DashboardError(
            f"Rung4 活件 mode={mode!r}，与用户签收终态 record_only 不符；"
            "按规则 1/7 停下报 LEAD，禁止看板自动声称 active")
    fail = _load_json(fail_path) if fail_path.is_file() else {}
    return {
        "calibration_path": str(cal_path),
        "failure_report_path": str(fail_path) if fail_path.is_file() else None,
        "mode": mode,
        "d7_result": fail.get("result"),
        "threshold_agreement": cal.get("threshold_agreement"),
        "required_defect_recall": cal.get("required_defect_recall"),
        "observed_min_agreement": cal.get("min_agreement"),
        "observed_defect_recall": cal.get("defect_recall"),
        "n_clips": cal.get("n_clips"),
        "n_defect_clips": cal.get("n_defect_clips"),
        "n_errors": len(cal.get("errors") or []),
        "record_only_reasons": cal.get("record_only_reasons"),
        "residual_risk": cal.get("residual_risk"),
        "active_claim_forbidden": True,
        "note": ("Rung4 VLM 终态 record_only（用户签收）：BinEval 意见不进入"
                 "任何自动逻辑；D7 active 翻转只能以新付费批次重新标定达成，"
                 "看板禁止声称 active。"),
    }


# ---------------------------------------------------------------------------
# 容器/GPU/git 收尾段
# ---------------------------------------------------------------------------

def closeout_block(run=_run_cmd) -> dict:
    rc, out, err = run(["docker", "ps", "-a",
                        "--filter", "name=sim_praxis",
                        "--format", "{{.Names}}"])
    containers = [l for l in out.splitlines() if l.strip()]
    docker_ok = rc == 0 and not containers

    rc2, out2, err2 = run(["nvidia-smi", "--query-gpu=memory.used",
                           "--format", "csv,noheader,nounits"])
    mibs = []
    if rc2 == 0:
        for l in out2.splitlines():
            l = l.strip()
            if l.isdigit():
                mibs.append(int(l))
    gpu_ok = rc2 == 0 and bool(mibs) and max(mibs) <= GPU_VRAM_LIMIT_MIB

    rc3, out3, err3 = run(["df", "-P", "/mnt"])
    return {
        "docker": {"command": "docker ps -a --filter name=sim_praxis",
                   "rc": rc, "containers": containers,
                   "pass": docker_ok,
                   "stderr_tail": err[-300:] if err else ""},
        "gpu": {"command": "nvidia-smi --query-gpu=memory.used",
                "rc": rc2, "memory_used_mib": mibs,
                "limit_mib": GPU_VRAM_LIMIT_MIB, "pass": gpu_ok,
                "stderr_tail": err2[-300:] if err2 else ""},
        "df_mnt": {"command": "df -P /mnt", "rc": rc3,
                   "raw": out3.strip(), "pass": rc3 == 0},
    }


def git_block(run=_run_cmd) -> dict:
    def g(*args):
        rc, out, _ = run(["git", *args], cwd=_REPO)
        return rc, out.strip()
    rc, head = g("rev-parse", "HEAD")
    rc2, porcelain = g("status", "--porcelain")
    rc3, ahead = g("rev-list", "--count", "origin/main..HEAD")
    # 锁内适配（规则 7）：计划 L635「里程碑推送 origin/main」被用户常设指令
    # 「暂不推送，攒批」覆盖；本构建器只读 ahead，不包含也不建议任何 push。
    return {
        "head": head if rc == 0 else None,
        "status_porcelain_lines": [l for l in porcelain.splitlines() if l]
        if rc2 == 0 else None,
        "ahead_of_origin_main": int(ahead) if rc3 == 0 and ahead.isdigit()
        else None,
        "push_state": "local_batch_held_not_pushed",
        "push_note": ("用户常设指令「暂不推送，攒批」覆盖计划 L635 的里程碑"
                      "推送；本看板只做只读 ahead 计数，构建器无任何 push 动作。"),
    }


# ---------------------------------------------------------------------------
# 组装
# ---------------------------------------------------------------------------

def build_dashboard(*, tests_result_path: Path | None = None,
                    run=_run_cmd) -> tuple[dict, list[dict]]:
    p2 = phase2_root()
    p1 = phase1_root()
    for rel, desc in REQUIRED_PHASE2:
        if not (p2 / rel).is_file():
            raise DashboardError(f"必需活件缺失（{desc}）：{p2 / rel}")

    tests = {
        "baseline_collection": collect_tests(run=run),
        "terminal_run": read_terminal_result(tests_result_path),
        "inventory_vs_verdict": (
            "collect-only 计数是清单口径（零执行、零付费）；全绿机判以 "
            "terminal_run 为准（tests/.../test_milestone0_e2e.py::"
            "test_05_repos_clean_and_author 在 Task7/8 文件未提交期间必红，"
            "终态由 LEAD 在两任务提交后喂入真实全跑结果，不弱化该测试）。"),
    }
    paid, cache_entries = paid_reconciliation(p2_root=p2, p1_root=p1)
    dashboard = {
        "schema": SCHEMA, "version": VERSION,
        "generated_at_iso": utc_now_iso_z(),
        "git": git_block(run=run),
        "tests": tests,
        "lance_tables": lance_tables_block(),
        "production_recall": production_recall_block(
            p2 / "recall" / "production_recall.jsonl"),
        "pareto": pareto_block(p2),
        "rung4_calibration": rung4_block(p2),
        "paid_reconciliation": paid,
        "closeout": closeout_block(run=run),
    }
    _self_check(dashboard)
    return dashboard, cache_entries


def _self_check(d: dict) -> None:
    required = {"schema", "version", "tests", "lance_tables",
                "production_recall", "pareto", "rung4_calibration",
                "paid_reconciliation", "closeout"}
    missing = required - set(d)
    if missing:
        raise DashboardError(f"看板缺段：{sorted(missing)}")
    paid = d["paid_reconciliation"]
    led = paid["phase2_ledger"]
    if not led["seq_contiguous"] or not led["all_ts_utc_z"]:
        raise DashboardError("自校验失败：账行连续性/UTC Z")
    rec = d["production_recall"]
    by_site_total = sum(rec["by_site"].values())
    if by_site_total != rec["total_rows"]:
        raise DashboardError(
            f"自校验失败：采纳行 site 聚合 {by_site_total} != 总数 "
            f"{rec['total_rows']}")
    rung4 = d["rung4_calibration"]
    if rung4["mode"] != "record_only" or not rung4["active_claim_forbidden"]:
        raise DashboardError("自校验失败：Rung4 终态必须 record_only")
    tables = {t["table"]: t for t in d["lance_tables"]["tables"]}
    for name in ("kb_chunks", "phase1_learnings", "solution_chunks",
                 "phase2_learnings"):
        if name not in tables:
            raise DashboardError(f"自校验失败：四表段缺 {name}")
    if tables["phase2_learnings"]["exists"]:
        # 活表出现 phase2_learnings 不算错误（未来 accept 后会有），但终态
        # 说明段需要由 LEAD 更新；此处仅不做相反假设。
        pass


def write_outputs(dashboard: dict, cache_entries: list[dict],
                  out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    dash_path = out_dir / "rsi_dashboard_phase2.json"
    dash_path.write_text(
        json.dumps(dashboard, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    inv_path = out_dir / "call_cache_inventory.json"
    inv = {"schema": "simpraxis.call_cache_inventory.v1",
           "generated_at_iso": dashboard["generated_at_iso"],
           "method": dashboard["paid_reconciliation"]["call_cache"]["method"],
           "entries": cache_entries}
    inv_path.write_text(
        json.dumps(inv, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return dash_path, inv_path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tests-result", type=Path, default=None,
                    help="终态全套实跑机判件 JSON（LEAD 提交后喂入）")
    ap.add_argument("--out-dir", type=Path, default=None,
                    help="输出目录（默认 $SIMPRAXIS_PHASE2_ROOT/task9_milestone）")
    args = ap.parse_args(argv)
    dashboard, cache_entries = build_dashboard(
        tests_result_path=args.tests_result)
    out_dir = args.out_dir or (phase2_root() / "task9_milestone")
    dash_path, inv_path = write_outputs(dashboard, cache_entries, out_dir)
    tables = {t["table"]: t["rows"]
              for t in dashboard["lance_tables"]["tables"]}
    print(json.dumps({
        "ok": True, "dashboard": str(dash_path),
        "inventory": str(inv_path),
        "tests_collected": dashboard["tests"]["baseline_collection"][
            "n_collected"],
        "terminal_run_green": (dashboard["tests"]["terminal_run"] or {}).get(
            "green"),
        "tables": tables,
        "recall_rows": dashboard["production_recall"]["total_rows"],
        "calls_real": dashboard["paid_reconciliation"]["phase2_ledger"][
            "calls_real"],
        "blocked": dashboard["paid_reconciliation"]["blocked_listed_separately"],
        "closeout_pass": {k: v["pass"] for k, v in
                          dashboard["closeout"].items()},
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
