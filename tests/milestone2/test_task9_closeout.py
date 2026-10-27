"""Task9：Milestone 2 看板/证据包构建器全 hermetic 测试（0 付费）。

纪律：
- 不联网、不触 docker/GPU、不读活账/活库；所有根目录走 tmp_path 合成，
  命令收口经假 runner 注入；合成件一律假名（fake-model / T6-FAKE）。
- 唯一读活 LanceDB 的测试打 kb_integration 标记，裸跑自动 skip
  （tests/kb_support.require_kb_integration，与 paid 双守卫同构）。
"""
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
from pathlib import Path

import lancedb
import pytest

from orchestrator.common import secrets as secret_scan
from tests.kb_support import require_kb_integration

REPO = Path(__file__).resolve().parents[2]


def _load(mod_name: str, rel: str):
    spec = importlib.util.spec_from_file_location(mod_name, REPO / rel)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


BD = _load("simpraxis_task9_dashboard",
           "scripts/milestone2/build_dashboard_phase2.py")
BP = _load("simpraxis_task9_pack",
           "scripts/milestone2/build_evidence_pack_phase2.py")

THREAD = "t6-genesis-ascent-20261016a"


# ---------------------------------------------------------------------------
# 合成件工厂
# ---------------------------------------------------------------------------

def _call(seq, *, stage="coding_debug", task=6, ok=True, ts=None,
          usage=(100, 50), model="fake-model-x", event="call"):
    return {
        "ts_iso": ts or f"2026-10-24T01:{seq:02d}:00Z", "event": event,
        "stage": stage, "model": model, "thread_id": "t6-fake",
        "task": task, "batch_id": "T6-FAKE", "seq": seq if event == "call"
        else None,
        "global_used_after": seq if event == "call" else None,
        "batch_approved": 8,
        "batch_used_after": seq if event == "call" else None,
        "tokens_in": usage[0], "tokens_out": usage[1],
        "tokens_cache_read": 0, "tokens_cache_create": 0, "ok": ok,
        "err": None if ok else "fake failure",
        "approval_ref": "approvals/T6-FAKE.json",
        "evidence": f"evidence/seq{seq}.json" if ok else None}


def _blocked(ts="2026-10-24T01:00:30Z"):
    r = _call(0, ts=ts, event="preflight_blocked")
    r["stage"] = "coding_debug"
    r["err"] = "task_hard_cap"
    return r


def _write_jsonl(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n",
                    encoding="utf-8")


def _recall_row(ts, site="coding", tid="learn01"):
    return {"ts": ts, "thread_id": "t6-fake", "site": site,
            "learning_id": tid.ljust(32, "0")[:32],
            "prompt_sha256": "a" * 64, "adopted_text_sha256": "b" * 64}


def _cache_entry(kind, ts, usage, key):
    return {"schema": "simpraxis.call_replay.v1", "kind": kind,
            "model": "fake-model-x", "raw": {"content": "fake"},
            "usage": list(usage), "req_sha256": "c" * 64,
            "ts_iso": ts}


def _make_lance(uri: Path):
    db = lancedb.connect(str(uri))
    for name, n in (("kb_chunks", 3), ("phase1_learnings", 2),
                    ("solution_chunks", 4)):
        db.create_table(
            name,
            data=[{"id": f"{name}-{i}", "vector": [0.1 * i, 0.2]}
                  for i in range(n)])


def _fake_cmd_runner(collect_ids):
    def run(argv, *, timeout=30, cwd=None, env=None):
        if argv[:1] == [sys.executable] and "pytest" in argv:
            return 0, "\n".join(collect_ids) + "\n\n3 tests collected\n", ""
        if argv[0] == "docker":
            return 0, "", ""
        if argv[0] == "nvidia-smi":
            return 0, "12\n", ""
        if argv[0] == "df":
            return 0, "Filesystem 1K-blocks Used Avail Use% Mounted\n" \
                      "/dev/fake 100 10 90 10% /mnt\n", ""
        if argv[0] == "git":
            if argv[1] == "rev-parse":
                return 0, "deadbeefcafe\n", ""
            if argv[1] == "status":
                return 0, "?? scripts/milestone2/x.py\n", ""
            if argv[1] == "rev-list":
                return 0, "3\n", ""
        return 0, "", ""
    return run


def _calibration(mode="record_only"):
    return {
        "mode": mode, "n_clips": 12, "n_defect_clips": 3,
        "threshold_agreement": 0.7, "required_defect_recall": 1.0,
        "min_agreement": 0.8, "defect_recall": 0.0,
        "errors": [{"clip": f"c{i:02d}", "kind": "missed_defect"}
                   for i in range(11)],
        "record_only_reasons": ["D7 未达成：defect_recall 0.0 < 1.0"],
        "residual_risk": "BinEval 意见不进任何自动逻辑"}


def _v1_summary():
    return {
        "schema": "simpraxis.task8.track2.v1_summary.v1",
        "pareto_metrics": {
            "success_rate": 0.1111, "slip_dist_mean_m": 0.25407,
            "postpush_vmax_mean_mps": 7.309, "tilt_max_deg_max": 155.58,
            "solve_wallclock_s": 14.0278, "vram_peak_gb": 1.9815},
        "contrast": {
            "hypothesis_status": "contradiction_control_not_robust",
            "ice_over_normal_postpush_ratio": 0.824,
            "preregistered_ratio": 2.0, "control_stands": False,
            "ordered_slip": "ice<normal（与预注册方向相反）"}}


def _build_synthetic_roots(tmp_path: Path, *, with_lance=True):
    p2 = tmp_path / "p2"
    p1 = tmp_path / "p1"
    runs2 = tmp_path / "runs2"
    lance = tmp_path / "kb" / "data" / "lancedb"

    rows = [_call(1, stage="macro_ideation", usage=(10, 5)),
            _call(2, ok=False, usage=(20, 8)),
            _call(3, stage="visual_rollout", task=4, usage=(30, 9)),
            _call(4, ts="2026-10-24T01:04:00Z", usage=(40, 10)),
            _call(5, ts="2026-10-24T01:05:00Z", usage=(50, 11))]
    _write_jsonl(p2 / "quota" / "ledger.jsonl", rows + [_blocked()])
    (p2 / "quota").mkdir(parents=True, exist_ok=True)
    (p2 / "quota" / "counters.json").write_text(
        json.dumps({"consecutive_quota_errors": 0}), encoding="utf-8")
    _write_jsonl(p2 / "recall" / "production_recall.jsonl",
                 [_recall_row("2026-10-20T06:38:41Z", "direction"),
                  _recall_row("2026-10-20T06:39:41Z", "coding", "learn02"),
                  _recall_row("2026-10-20T06:40:41Z", "coding", "learn02")])

    (p2 / "task4_judge").mkdir(parents=True)
    (p2 / "task4_judge" / "calibration_bineval.json").write_text(
        json.dumps(_calibration()), encoding="utf-8")
    (p2 / "task4_judge" / "bineval_failure_report.json").write_text(
        json.dumps({"result": "D7_NOT_ACHIEVED"}), encoding="utf-8")

    sdir = p2 / "evolution_sources" / THREAD
    sdir.mkdir(parents=True)
    (sdir / "v1_summary.json").write_text(
        json.dumps(_v1_summary()), encoding="utf-8")
    (sdir / "v1_vram_peak.json").write_text(
        json.dumps({"vram_peak_gb": 1.9815}), encoding="utf-8")
    (sdir / "gate1_opinion.json").write_text(
        json.dumps({"decision": "approve"}), encoding="utf-8")
    ed = p2 / "evolutions"
    ed.mkdir(parents=True)
    (ed / "evolution_failed_birth_fake1.md").write_text(
        "# fake failed birth 1", encoding="utf-8")
    (ed / "evolution_failed_birth_fake2.md").write_text(
        "# fake failed birth 2", encoding="utf-8")

    run_dir = p2 / "runs" / "task6_ascent" / f"run_{THREAD}_1"
    run_dir.mkdir(parents=True)
    (run_dir / "summary.json").write_text(json.dumps({
        "schema": "simpraxis.task8.track2.summary.v2",
        "groups": {"normal": {"success_rate": 0.0, "n_fell": 9}}}),
        encoding="utf-8")
    (run_dir / "FAILURE.json").write_text(json.dumps({
        "status": "failed", "reason": "fake", "coding_round": 1,
        "executions": 3}), encoding="utf-8")
    wd = p2 / "runs" / "task6_ascent"
    (wd / "g6_watch_runX_20261024T010000Z.log").write_text(
        "CACHE_NEW k1\nCACHE_NEW k2\n", encoding="utf-8")

    # macro proposals：两 attempt 均非重放
    td = p1 / "task2_direction"
    td.mkdir(parents=True)
    (td / f"proposals_{THREAD}_r0.json").write_text(json.dumps({
        "attempts": [{"attempt": 0, "replay": False},
                     {"attempt": 1, "replay": False}]}), encoding="utf-8")
    # 缓存：3 条目 1:1 配 seq 1/4/5（seq2 失败不 put；seq3 无条目合法）
    cd = p1 / ".call_cache"
    cd.mkdir(parents=True)
    for key, seq, ts, usage in (
            (f"macro_{THREAD}_r0_a0", 1, rows[0]["ts_iso"], (10, 5)),
            (f"coding_{THREAD}_r1_round2_chunk0", 4,
             "2026-10-24T01:04:00Z", (40, 10)),
            (f"coding_{THREAD}_r1_round2", 5,
             "2026-10-24T01:05:05Z", (50, 11))):
        # 第三条目模拟账行/put 时钟差（同 stage+usage、10s 窗口内唯一）
        (cd / f"{key}.json").write_text(
            json.dumps(_cache_entry(
                "macro_ideation" if seq == 1 else "coding_debug",
                ts, usage, key)), encoding="utf-8")
    q1 = p1 / "quota"
    q1.mkdir(parents=True)
    _write_jsonl(q1 / "ledger.jsonl", [_call(i) for i in range(1, 30)])

    if with_lance:
        _make_lance(lance)
    return p2, p1, runs2, lance


# ---------------------------------------------------------------------------
# 纯函数：collect 解析
# ---------------------------------------------------------------------------

def test_parse_collect_output_counts_node_ids_only():
    out = (
        "tests/a/test_x.py::test_one\n"
        "tests/a/test_x.py::test_two[param]\n"
        "\n"
        "2 tests collected, 3 deselected\n"
        "warnings summary mock line\n")
    parsed = BD.parse_collect_output(out)
    assert parsed["n_collected"] == 2
    assert parsed["node_ids"][1].endswith("test_two[param]")


def test_parse_collect_output_empty():
    assert BD.parse_collect_output("")["n_collected"] == 0


def test_collect_sanitizes_synthetic_secret_shaped_node_ids():
    # 合成形状一律运行时拼接（与 milestone1 STRICT_CASES 同惯例）：本测试源码
    # 里不得出现完整字面密钥形状（gate4 scan_secrets 按形状即 FATAL）。
    hf = "hf_" + "a" * 34
    gh = "ghp_" + "b" * 36
    nid_normal = "tests/milestone2/test_x.py::test_normal[param-AB]"
    nid_hf = ("tests/milestone1/test_common_foundation.py::"
              "test_scan_detects_all_strict_forms[hf_token-" + hf + "]")
    nid_gh = ("tests/milestone1/test_common_foundation.py::"
              "test_scan_detects_all_strict_forms[github_token-" + gh + "]")
    stdout = "\n".join([nid_normal, nid_hf, nid_gh]) + \
        "\n\n3 tests collected in 0.01s\n"

    # 保险：纯解析保持原始未脱敏——脱敏只发生在 collect_tests 生产侧。
    raw = BD.parse_collect_output(stdout)
    assert raw["n_collected"] == 3
    assert raw["node_ids"] == [nid_normal, nid_hf, nid_gh]
    assert secret_scan.contains_secret(json.dumps(raw)) is True

    def fake_run(argv, **kw):  # 接 timeout=/cwd=/env= 关键字
        return 0, stdout, ""

    blk = BD.collect_tests(run=fake_run)
    assert blk["ok"] is True
    assert blk["n_collected"] == 3
    ids = blk["node_ids"]
    assert len(ids) == 3
    assert ids[0] == nid_normal  # 未命中的普通节点 id 逐字节不动
    for masked, original, body in ((ids[1], nid_hf, hf),
                                   (ids[2], nid_gh, gh)):
        assert masked != original
        assert secret_scan.REDACTED in masked
        assert body not in masked  # 合成密钥本体不得残留
    assert blk["redacted_node_ids"] == 2
    assert blk["redaction_note"]
    # 核心牙齿：整块序列化后不再命中证据包密钥严格口径（旧代码必红）。
    assert secret_scan.contains_secret(
        json.dumps(blk, ensure_ascii=False)) is False

    # 纯函数口径：普通串原样透传；单点合成串脱敏后严格口径为 False。
    plain = ["tests/a/test_x.py::test_one", "tests/a/test_x.py::test_two[p-1]"]
    out_plain, n_plain = BD.sanitize_node_ids(plain)
    assert out_plain == plain and n_plain == 0
    sk = "sk-" + "e" * 48
    out_sk, n_sk = BD.sanitize_node_ids([sk])
    assert n_sk == 1 and out_sk[0] != sk
    assert secret_scan.contains_secret(out_sk[0]) is False


# ---------------------------------------------------------------------------
# 账对账
# ---------------------------------------------------------------------------

def test_reconcile_ledger_happy_path():
    rows = [_call(1), _call(2, ok=False), _blocked()]
    rec = BD.reconcile_ledger(rows)
    assert rec["calls_real"] == 2
    assert rec["ok"] == 1 and rec["failed"] == 1
    assert rec["blocked_preflight"] == 1
    assert rec["seq_contiguous"] is True
    assert rec["lines"] == 3
    assert rec["by_stage"] == {"coding_debug": 2}


def test_reconcile_ledger_gap_fails_closed():
    with pytest.raises(BD.DashboardError, match="seq 不连续"):
        BD.reconcile_ledger([_call(1), _call(3)])


def test_reconcile_ledger_unknown_event_fails_closed():
    r = _call(1)
    r["event"] = "mystery"
    with pytest.raises(BD.DashboardError, match="未知 event"):
        BD.reconcile_ledger([r])


def test_reconcile_ledger_non_utc_z_fails_closed():
    r = _call(1)
    r["ts_iso"] = "2026-10-24T01:01:00+08:00"
    with pytest.raises(BD.DashboardError, match="UTC Z"):
        BD.reconcile_ledger([r])


def test_reconcile_ledger_call_with_null_seq_fails():
    r = _call(1)
    r["seq"] = None
    with pytest.raises(BD.DashboardError, match="seq/event"):
        BD.reconcile_ledger([r])


def test_reconcile_ledger_blocked_with_seq_fails():
    r = _blocked()
    r["seq"] = 9
    with pytest.raises(BD.DashboardError, match="seq/event"):
        BD.reconcile_ledger([r])


# ---------------------------------------------------------------------------
# 缓存口径
# ---------------------------------------------------------------------------

def test_cache_inventory_exact_and_window_match(tmp_path):
    rows = [_call(1, stage="macro_ideation", usage=(10, 5)),
            _call(2, ts="2026-10-24T01:04:00Z", usage=(40, 10))]
    cd = tmp_path / "cache"
    cd.mkdir()
    (cd / "k1.json").write_text(json.dumps(_cache_entry(
        "macro_ideation", rows[0]["ts_iso"], (10, 5), "k1")), encoding="utf-8")
    (cd / "k2.json").write_text(json.dumps(_cache_entry(
        "coding_debug", "2026-10-24T01:04:06Z", (40, 10), "k2")),
        encoding="utf-8")
    inv = BD.cache_inventory(cd, rows)
    assert inv["n_entries"] == 2
    assert inv["n_distinct_matched_calls"] == 2
    methods = {e["key"]: e["match_method"] for e in inv["entries"]}
    assert methods["k1"] == "ts_exact"
    assert methods["k2"] == "ts_window_usage_unique"


def test_cache_inventory_unmatched_entry_fails_closed(tmp_path):
    rows = [_call(1, usage=(10, 5))]
    cd = tmp_path / "cache"
    cd.mkdir()
    (cd / "orphan.json").write_text(json.dumps(_cache_entry(
        "coding_debug", "2025-01-01T00:00:00Z", (9, 9), "orphan")),
        encoding="utf-8")
    with pytest.raises(BD.DashboardError, match="配不上"):
        BD.cache_inventory(cd, rows)


def test_cache_inventory_missing_dir_is_empty(tmp_path):
    inv = BD.cache_inventory(tmp_path / "nope", [])
    assert inv["exists"] is False and inv["n_entries"] == 0


def test_macro_replay_hits_counts_flags(tmp_path):
    d = tmp_path / "task2_direction"
    d.mkdir()
    (d / f"proposals_{THREAD}_r0.json").write_text(json.dumps({
        "attempts": [{"replay": True}, {"replay": False},
                     {"replay": True}]}), encoding="utf-8")
    out = BD.macro_replay_hits(d)
    assert out["macro_replay_hits_evidenced"] == 2
    assert out["macro_first_calls_evidenced"] == 1


def test_watch_cache_signals(tmp_path):
    (tmp_path / "g6_watch_a.log").write_text(
        "CACHE_NEW x\nCACHE_HIT y\nCACHE_HIT z\n", encoding="utf-8")
    out = BD.watch_cache_signals(tmp_path)
    assert out["cache_new_signals"] == 1
    assert out["cache_hit_signals"] == 2


def test_watch_cache_signals_skips_symlink_latest(tmp_path):
    # .latest.log 是指向真实日志的软链：跟随 read_text 会双计，必须整条跳过。
    real = tmp_path / "g6_watch_run6.log"
    real.write_text(
        "CACHE_NEW alpha\nCACHE_NEW beta\nCACHE_HIT gamma\n",
        encoding="utf-8")
    os.symlink(real, tmp_path / "g6_watch_run6.latest.log")
    out = BD.watch_cache_signals(tmp_path)
    assert out["cache_new_signals"] == 2
    assert out["cache_hit_signals"] == 1
    assert not any(f["file"].endswith(".latest.log") for f in out["files"])


# ---------------------------------------------------------------------------
# production_recall / terminal result
# ---------------------------------------------------------------------------

def test_production_recall_block_aggregates(tmp_path):
    p = tmp_path / "r.jsonl"
    _write_jsonl(p, [_recall_row("2026-10-20T06:38:41Z", "direction"),
                     _recall_row("2026-10-20T06:39:41Z", "coding", "l2"),
                     _recall_row("2026-10-20T06:40:41Z", "coding", "l2")])
    blk = BD.production_recall_block(p)
    assert blk["total_rows"] == 3
    assert blk["by_site"] == {"coding": 2, "direction": 1}
    assert blk["distinct_learning_ids"] == 2
    assert blk["fields_valid"] is True
    assert blk["ts_nondecreasing"] is True


def test_production_recall_block_corrupt_row_raises(tmp_path):
    p = tmp_path / "r.jsonl"
    p.write_text(json.dumps({"ts": "2026-10-20T06:38:41Z", "x": 1}) + "\n",
                 encoding="utf-8")
    with pytest.raises(ValueError):
        BD.production_recall_block(p)


def test_terminal_result_none_when_not_fed(tmp_path):
    assert BD.read_terminal_result(None) is None


def test_terminal_result_validates_types(tmp_path):
    p = tmp_path / "tr.json"
    p.write_text(json.dumps({
        "command": ["pytest"], "passed": 10, "failed": 0,
        "skipped": 1, "deselected": 4, "git_clean": True}), encoding="utf-8")
    out = BD.read_terminal_result(p)
    assert out["green"] is True
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({
        "command": [], "passed": "10", "failed": 0, "skipped": 0,
        "deselected": 0, "git_clean": True}), encoding="utf-8")
    with pytest.raises(BD.DashboardError):
        BD.read_terminal_result(bad)
    missing = tmp_path / "m.json"
    missing.write_text("{}", encoding="utf-8")
    with pytest.raises(BD.DashboardError, match="缺字段"):
        BD.read_terminal_result(missing)


# ---------------------------------------------------------------------------
# closeout / git
# ---------------------------------------------------------------------------

def test_closeout_pass(tmp_path):
    blk = BD.closeout_block(run=_fake_cmd_runner([]))
    assert blk["docker"]["pass"] is True
    assert blk["gpu"]["pass"] is True
    assert blk["gpu"]["memory_used_mib"] == [12]
    assert blk["df_mnt"]["pass"] is True


def test_closeout_failures_detected():
    def run(argv, *, timeout=30, cwd=None, env=None):
        if argv[0] == "docker":
            return 0, "sim_praxis_sandbox\n", ""
        if argv[0] == "nvidia-smi":
            return 0, "350\n", ""
        if argv[0] == "df":
            return 0, "df line", ""
        return 0, "", ""
    blk = BD.closeout_block(run=run)
    assert blk["docker"]["pass"] is False
    assert blk["gpu"]["pass"] is False


def test_git_block_never_invokes_push():
    seen = []

    def run(argv, *, timeout=30, cwd=None, env=None):
        seen.append(argv)
        if argv[1] == "rev-parse":
            return 0, "deadbeef\n", ""
        if argv[1] == "status":
            return 0, "?? x.py\n", ""
        if argv[1] == "rev-list":
            return 0, "3\n", ""
        return 0, "", ""

    blk = BD.git_block(run=run)
    assert blk["ahead_of_origin_main"] == 3
    assert blk["push_state"] == "local_batch_held_not_pushed"
    assert all("push" not in " ".join(a) for a in seen)


# ---------------------------------------------------------------------------
# 端到端：合成根 -> 看板 JSON
# ---------------------------------------------------------------------------

def test_build_dashboard_end_to_end(tmp_path, monkeypatch):
    p2, p1, _runs2, lance = _build_synthetic_roots(tmp_path)
    monkeypatch.setenv("SIMPRAXIS_PHASE2_ROOT", str(p2))
    monkeypatch.setenv("SIMPRAXIS_PHASE1_ROOT", str(p1))
    monkeypatch.setenv("SIMPRAXIS_LANCEDB_URI", str(lance))
    ids = ["tests/a/test_x.py::test_one", "tests/a/test_y.py::test_two"]
    run = _fake_cmd_runner(ids)

    dash, entries = BD.build_dashboard(run=run)
    assert dash["schema"] == "simpraxis.rsi_dashboard_phase2.v1"
    assert dash["tests"]["baseline_collection"]["n_collected"] == 2
    assert dash["tests"]["terminal_run"] is None  # LEAD 未喂入即 null，不冒充
    paid = dash["paid_reconciliation"]
    assert paid["phase2_ledger"]["calls_real"] == 5
    assert paid["phase2_ledger"]["ok"] == 4
    assert paid["phase2_ledger"]["failed"] == 1
    assert paid["blocked_listed_separately"] == 1
    assert paid["phase1_sealed_ledger"]["lines"] == 29
    assert paid["logical_calls_total"]["value"] == 5  # macro replay=false×2
    assert paid["zero_ledger_calls"]["coding_hits"]["count"] is None
    assert paid["watch_signals"]["cache_hit_signals"] == 0
    assert paid["call_cache"]["n_distinct_matched_calls"] == 3
    tables = {t["table"]: t for t in dash["lance_tables"]["tables"]}
    assert tables["kb_chunks"]["rows"] == 3
    assert tables["phase1_learnings"]["rows"] == 2
    assert tables["solution_chunks"]["rows"] == 4
    p2l = tables["phase2_learnings"]
    assert p2l["exists"] is False and p2l["rows"] == 0
    assert dash["production_recall"]["total_rows"] == 3
    pareto = dash["pareto"]
    assert pareto["pareto_verdict"] is None
    assert pareto["v2_summary_present"] is False
    q1 = pareto["q1_contradiction"]
    assert q1["observed"] == 0.824 and q1["preregistered_boundary"] == 2.0
    assert q1["hypothesis_status"] == "contradiction_control_not_robust"
    r4 = dash["rung4_calibration"]
    assert r4["mode"] == "record_only"
    assert r4["active_claim_forbidden"] is True
    assert dash["closeout"]["docker"]["pass"] is True
    assert dash["git"]["push_state"] == "local_batch_held_not_pushed"
    assert len(entries) == 3

    out = tmp_path / "out"
    dash_p, inv_p = BD.write_outputs(dash, entries, out)
    assert json.loads(dash_p.read_text())["schema"] == dash["schema"]
    inv = json.loads(inv_p.read_text())
    assert {e["matched_seq"] for e in inv["entries"]} == {1, 4, 5}


def test_build_dashboard_rung4_active_live_file_fails(tmp_path, monkeypatch):
    p2, p1, _r, lance = _build_synthetic_roots(tmp_path)
    cal_p = p2 / "task4_judge" / "calibration_bineval.json"
    doc = json.loads(cal_p.read_text())
    doc["mode"] = "active"
    cal_p.write_text(json.dumps(doc), encoding="utf-8")
    monkeypatch.setenv("SIMPRAXIS_PHASE2_ROOT", str(p2))
    monkeypatch.setenv("SIMPRAXIS_PHASE1_ROOT", str(p1))
    monkeypatch.setenv("SIMPRAXIS_LANCEDB_URI", str(lance))
    with pytest.raises(BD.DashboardError, match="record_only"):
        BD.build_dashboard(run=_fake_cmd_runner([]))


def test_build_dashboard_missing_required_artifact(tmp_path, monkeypatch):
    p2, p1, _r, lance = _build_synthetic_roots(tmp_path)
    (p2 / "quota" / "ledger.jsonl").unlink()
    monkeypatch.setenv("SIMPRAXIS_PHASE2_ROOT", str(p2))
    monkeypatch.setenv("SIMPRAXIS_PHASE1_ROOT", str(p1))
    monkeypatch.setenv("SIMPRAXIS_LANCEDB_URI", str(lance))
    with pytest.raises(BD.DashboardError, match="必需活件缺失"):
        BD.build_dashboard(run=_fake_cmd_runner([]))


# ---------------------------------------------------------------------------
# /proc 句柄守卫
# ---------------------------------------------------------------------------

def test_processes_holding_paths_detects_fd(tmp_path):
    guarded = tmp_path / "live.db"
    guarded.write_text("x")
    proc = tmp_path / "proc" / "4242" / "fd"
    proc.mkdir(parents=True)
    os.symlink(guarded, proc / "3")
    (proc.parent / "cmdline").write_bytes(b"fake-graph\x00--arg\x00")
    holders = BP._processes_holding_paths([guarded], proc_root=str(proc.parent.parent))
    assert len(holders) == 1
    assert holders[0]["pid"] == 4242 and holders[0]["fd"] == "3"
    assert "fake-graph" in holders[0]["cmdline"]


def test_processes_holding_paths_prefix_and_ignores(tmp_path):
    root = tmp_path / "lance"
    (root / "tbl.lance").mkdir(parents=True)
    data_file = root / "tbl.lance" / "data.mbox"
    data_file.write_text("x")
    proc = tmp_path / "proc" / "7" / "fd"
    proc.mkdir(parents=True)
    os.symlink(data_file, proc / "1")
    os.symlink("socket:[123]", proc / "2")
    (proc.parent / "cmdline").write_bytes(b"x\x00")
    holders = BP._processes_holding_paths([root], proc_root=str(proc.parent.parent))
    assert [h["fd"] for h in holders] == ["1"]
    # exclude_pid 生效
    assert BP._processes_holding_paths(
        [root], proc_root=str(proc.parent.parent), exclude_pid=7) == []


def test_guard_no_handles_exits_with_instructions(tmp_path):
    guarded = tmp_path / "live.db"
    guarded.write_text("x")
    proc = tmp_path / "proc" / "99" / "fd"
    proc.mkdir(parents=True)
    os.symlink(guarded, proc / "1")
    (proc.parent / "cmdline").write_bytes(b"fake\x00")
    with pytest.raises(SystemExit) as ei:
        BP._guard_no_handles([guarded], proc_root=str(proc.parent.parent))
    msg = str(ei.value)
    assert "pid=99" in msg and "不会强制删除" in msg


# ---------------------------------------------------------------------------
# 证据包端到端（合成根）
# ---------------------------------------------------------------------------

def _make_pack_roots(tmp_path):
    p2, p1, runs2, lance = _build_synthetic_roots(tmp_path)
    # Task9 看板产物（LEAD 流程：先看板后证据包）
    t9 = p2 / "task9_milestone"
    t9.mkdir(parents=True)
    (t9 / "rsi_dashboard_phase2.json").write_text(
        json.dumps({"schema": "simpraxis.rsi_dashboard_phase2.v1"}),
        encoding="utf-8")
    (t9 / "call_cache_inventory.json").write_text(
        json.dumps({"schema": "simpraxis.call_cache_inventory.v1",
                    "entries": []}), encoding="utf-8")
    (p2 / "task0b_closeout_evidence.md").write_text("# task0b fake", "utf-8")
    # approvals 原件（不得入包）+ 元数据
    ap = p2 / "quota" / "approvals"
    ap.mkdir(parents=True)
    (ap / "T6-FAKE.json").write_text(
        json.dumps({"batch_id": "T6-FAKE", "approved": 8}), encoding="utf-8")
    # task4 证据树（含二进制件）
    ev = p2 / "task4_judge" / "evidence"
    ev.mkdir(parents=True)
    (ev / "clip_01.req.json").write_text(
        json.dumps({"prompt": "fake req"}), encoding="utf-8")
    (ev / "clip_01.resp.json").write_text(
        json.dumps({"score": 0}), encoding="utf-8")
    (p2 / "task4_judge" / "batch_logs").mkdir(parents=True)
    (p2 / "task4_judge" / "batch_logs" / "b.log").write_text(
        "fake batch log", encoding="utf-8")
    syn = p2 / "task4_judge" / "synth" / "clip_01_x"
    syn.mkdir(parents=True)
    (syn / "video.mp4").write_bytes(b"\x00\x01FAKEMP4\xff")
    (syn / "ground_truth.json").write_text(json.dumps({"defect": True}),
                                           encoding="utf-8")
    arch = p2 / "task4_judge_retry_archive" / "T4-FAKE"
    arch.mkdir(parents=True)
    (arch / "calibration_bineval.json").write_text(
        json.dumps(_calibration()), encoding="utf-8")
    (p2 / "task7").mkdir(parents=True)
    (p2 / "task7" / "libero_tour_structure_manifest.json").write_text(
        json.dumps({"tours": []}), encoding="utf-8")
    (p2 / "v1_index.json").write_text(json.dumps({"v1": True}), "utf-8")
    # 树内绝对符号链接（.latest 场景）+ 其目标实体
    wd = p2 / "runs" / "task6_ascent"
    (wd / "g6_watch_run6.log").write_text("CACHE_NEW k\n", encoding="utf-8")
    os.symlink(wd / "g6_watch_run6.log", wd / "g6_watch_run6.latest.log")
    (wd / "run_x").mkdir(exist_ok=True)
    (wd / "run_x" / "FAILURE.json").write_text(
        json.dumps({"status": "failed"}), encoding="utf-8")
    # 存活 SQLite（p1 根；模拟 phase2 复用 phase1 库的实况）
    sdir = p1 / "state"
    sdir.mkdir(parents=True)
    con = sqlite3.connect(sdir / "pending_gates.db")
    con.execute(
        "CREATE TABLE pending_gates (thread_id TEXT, gate TEXT, status TEXT,"
        " choice TEXT, decision TEXT, human_notes TEXT, source TEXT,"
        " card_message_id TEXT, media_message_id TEXT, created_at TEXT,"
        " decided_at TEXT)")
    con.execute("INSERT INTO pending_gates VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                ("t6-fake", "gate1", "approved", "approve", "ok", None,
                 "card_form", "om_fake", None, "2026-10-24T01:00:00Z",
                 "2026-10-24T01:01:00Z"))
    con.commit()
    con.close()
    con = sqlite3.connect(sdir / "langgraph_checkpoints.db")
    con.execute("CREATE TABLE checkpoint (thread_id TEXT)")
    con.commit()
    con.close()
    # run 侧 Stage0 报告（required）
    t8 = runs2 / "task8" / "stage0"
    t8.mkdir(parents=True)
    (t8 / "stage0_fluid_report.json").write_text(json.dumps({
        "outcome": "sph_ok", "sph": {"finite": True}}), encoding="utf-8")
    t7 = runs2 / "task7" / "stage0"
    t7.mkdir(parents=True)
    (t7 / "stage0_compat_report.json").write_text(json.dumps({
        "ok": False, "outcome": "conflict_isolated_failed"}), encoding="utf-8")
    (t7 / "stage0_compat_report.pre_adaptation_fake.json").write_text(
        json.dumps({"ok": False}), encoding="utf-8")
    (runs2 / "task7_libero" / "stage2_suite" / "run_fake").mkdir(parents=True)
    (runs2 / "task7_libero" / "stage2_suite" / "run_fake" / "probe.json"
     ).write_text(json.dumps({"ok": True}), encoding="utf-8")
    # 越界链接目录（不在任何 DIR_COPIES 内，验证排除语义不依赖误碰）
    wheels = runs2 / "task8" / "stage0_wheels"
    wheels.mkdir(parents=True)
    outside = tmp_path / "outside.whl"
    outside.write_text("not evidence")
    os.symlink(outside, wheels / "fake.whl")
    return p2, p1, runs2, lance


def test_build_pack_end_to_end(tmp_path):
    p2, p1, runs2, lance = _make_pack_roots(tmp_path)
    pack = p2 / "task9_milestone" / "evidence_pack"
    summary = BP.build_pack(p2=p2, p1=p1, runs2=runs2, repo=REPO, pack=pack,
                            lance_uri=str(lance), clean=False)
    assert summary["ok"] is True
    manifest = json.loads((pack / "manifest.json").read_text())
    assert manifest["schema"] == "simpraxis.evidence_pack.v1"
    assert manifest["milestone"] == 2
    assert manifest["ledger"]["calls_real"] == 5
    assert manifest["ledger"]["seq_contiguous"] is True
    assert manifest["ledger"]["blocked_preflight"] == 1
    assert manifest["phase1_sealed_ledger"]["lines"] == 29
    paths = {f["path"]: f for f in manifest["files"]}
    # 两份 Stage0 required 报告
    assert "runs2/task8/stage0/stage0_fluid_report.json" in paths
    assert "runs2/task7/stage0/stage0_compat_report.json" in paths
    assert "runs2/task7/stage0/stage0_compat_report.pre_adaptation_fake.json" \
        in paths
    # SQLite backup API 快照、无侧车
    db_rec = paths["state/langgraph_checkpoints.db"]
    assert db_rec["snapshot"] == "sqlite3_backup_api"
    assert not (pack / "state" / "langgraph_checkpoints.db-wal").exists()
    # 树内 .latest 链接不复制，目标实体复制
    assert not (pack / "runs" / "task6_ascent"
                / "g6_watch_run6.latest.log").exists()
    assert (pack / "runs" / "task6_ascent"
            / "g6_watch_run6.log").is_file()
    # wheelhouse 越界链接树整目录不入包
    assert not (pack / "runs2" / "task8" / "stage0_wheels").exists()
    # approvals 原件排除、索引在
    assert not (pack / "quota" / "approvals" / "T6-FAKE.json").exists()
    aidx = json.loads((pack / "quota" / "approvals_index.json").read_text())
    assert "T6-FAKE" in aidx and aidx["T6-FAKE"]["sha256"]
    # 四表计数派生件：phase2_learnings 不存在如实记录
    counts = json.loads((pack / "lance" / "table_counts.json").read_text())
    tmap = {t["table"]: t for t in counts["tables"]}
    assert tmap["phase2_learnings"]["exists"] is False
    assert tmap["kb_chunks"]["rows"] == 3
    # gates 只读导出
    gates = json.loads((pack / "state" / "gates_export.json").read_text())
    assert gates[0]["thread_id"] == "t6-fake"
    # FAILURE 冻结 + 追溯矩阵 + ADAPTATIONS
    assert any(f["pack_path"].endswith("FAILURE.json")
               for f in manifest["failures"])
    matrix = json.loads(
        (pack / "traceability_matrix.json").read_text())
    assert {r["item"] for r in matrix["rows"]} >= {"T7", "T8", "T9", "paid"}
    adaptations = (pack / "ADAPTATIONS.md").read_text()
    assert "锁内适配 1" in adaptations and "锁内适配 2" in adaptations
    # 包内零符号链接、零 secret（合成件）
    assert all(not p.is_symlink() for p in pack.rglob("*"))
    replay = "\n".join(manifest["replay_commands"])
    assert "push" not in replay
    assert summary["symlinks_indexed"] >= 1


def test_build_pack_clean_rebuild_with_proc_guard(tmp_path):
    p2, p1, runs2, lance = _make_pack_roots(tmp_path)
    pack = p2 / "task9_milestone" / "evidence_pack"
    BP.build_pack(p2=p2, p1=p1, runs2=runs2, repo=REPO, pack=pack,
                  lance_uri=str(lance), clean=False)
    # 合成 /proc：有人持有活 lancedb 文件 -> --clean 必须拒建且保留旧包
    target = next((lance / "kb_chunks.lance").rglob("*"))
    while target.is_dir():
        target = next(target.rglob("*"))
    proc = tmp_path / "proc2" / "55" / "fd"
    proc.mkdir(parents=True)
    os.symlink(target, proc / "4")
    (proc.parent / "cmdline").write_bytes(b"fake-graph\x00")
    sentinel = pack / "state" / "langgraph_checkpoints.db"
    assert sentinel.exists()
    with pytest.raises(SystemExit, match="拒绝 --clean"):
        BP.build_pack(p2=p2, p1=p1, runs2=runs2, repo=REPO, pack=pack,
                      lance_uri=str(lance), clean=True,
                      proc_root=str(tmp_path / "proc2"))
    assert sentinel.exists()  # 旧包未被删
    # 无持有者：干净重建成功
    BP.build_pack(p2=p2, p1=p1, runs2=runs2, repo=REPO, pack=pack,
                  lance_uri=str(lance), clean=True,
                  proc_root=str(tmp_path / "empty_proc"))
    assert (pack / "manifest.json").is_file()


def test_build_pack_missing_required_stage0_names_artifact(tmp_path):
    p2, p1, runs2, lance = _make_pack_roots(tmp_path)
    (runs2 / "task8" / "stage0" / "stage0_fluid_report.json").unlink()
    pack = p2 / "task9_milestone" / "evidence_pack"
    with pytest.raises(SystemExit) as ei:
        BP.build_pack(p2=p2, p1=p1, runs2=runs2, repo=REPO, pack=pack,
                      lance_uri=str(lance))
    assert "stage0_fluid_report.json" in str(ei.value)
    assert "Task8 Stage0" in str(ei.value)


def test_build_pack_out_of_tree_symlink_fatals(tmp_path):
    p2, p1, runs2, lance = _make_pack_roots(tmp_path)
    # task6_ascent 内放越界链接文件（目标在 p2/p1/runs2 之外）
    wd = p2 / "runs" / "task6_ascent"
    outside = tmp_path / "secret_outside.txt"
    outside.write_text("x")
    os.symlink(outside, wd / "evil.latest.log")
    pack = p2 / "task9_milestone" / "evidence_pack"
    with pytest.raises(SystemExit, match="越界符号链接"):
        BP.build_pack(p2=p2, p1=p1, runs2=runs2, repo=REPO, pack=pack,
                      lance_uri=str(lance))


def test_build_pack_out_of_tree_dir_symlink_fatals(tmp_path):
    p2, p1, runs2, lance = _make_pack_roots(tmp_path)
    # task6_ascent 内放越界【目录】链接：os.walk(followlinks=False) 的
    # dirnames 臂必须与文件链接同口径 FATAL（H-11）。
    wd = p2 / "runs" / "task6_ascent"
    outside_dir = tmp_path / "secret_dir"
    outside_dir.mkdir()
    (outside_dir / "y").write_text("y")
    os.symlink(outside_dir, wd / "evil_dir")
    pack = p2 / "task9_milestone" / "evidence_pack"
    with pytest.raises(SystemExit, match="越界符号链接目录"):
        BP.build_pack(p2=p2, p1=p1, runs2=runs2, repo=REPO, pack=pack,
                      lance_uri=str(lance))


def test_build_pack_secret_scan_fatals(tmp_path):
    p2, p1, runs2, lance = _make_pack_roots(tmp_path)
    # 明显假名的密钥形状串；与 test_task0_debt 同惯例用拼接构造，使密钥字面量
    # 不出现在源码里（仓内 scan_secrets.sh grep 不误伤），运行时仍命中第 9 条。
    (p2 / "task4_judge" / "batch_logs" / "leak.log").write_text(
        '"api_key" : "' + "F" * 30 + '"', encoding="utf-8")
    pack = p2 / "task9_milestone" / "evidence_pack"
    with pytest.raises(SystemExit, match="secret scan"):
        BP.build_pack(p2=p2, p1=p1, runs2=runs2, repo=REPO, pack=pack,
                      lance_uri=str(lance))


def test_build_pack_clean_refuses_outside_root(tmp_path):
    p2, p1, runs2, lance = _make_pack_roots(tmp_path)
    stray = tmp_path / "stray_pack"
    stray.mkdir()
    (stray / "x").write_text("x")
    with pytest.raises(SystemExit, match="不在 phase2 根内"):
        BP.build_pack(p2=p2, p1=p1, runs2=runs2, repo=REPO, pack=stray,
                      lance_uri=str(lance), clean=True,
                      proc_root=str(tmp_path / "empty_proc"))


def test_build_pack_clean_first_build_absent_pack(tmp_path):
    p2, p1, runs2, lance = _make_pack_roots(tmp_path)
    # 包目录从未预建：--clean 首建也是合法路径，不得抛 FileNotFoundError。
    pack = p2 / "task9_milestone" / "evidence_pack"
    assert not pack.exists()
    summary = BP.build_pack(p2=p2, p1=p1, runs2=runs2, repo=REPO, pack=pack,
                            lance_uri=str(lance), clean=True,
                            proc_root=str(tmp_path / "empty_proc"))
    assert summary["ok"] is True
    assert (pack / "manifest.json").is_file()


# ---------------------------------------------------------------------------
# 源纪律：无 push 动作 / 无占位
# ---------------------------------------------------------------------------

def test_builders_contain_no_push_or_placeholders():
    # 只扫两个构建器（本测试自身必然包含这些守卫字面量）。
    builders = (
        "scripts/milestone2/build_dashboard_phase2.py",
        "scripts/milestone2/build_evidence_pack_phase2.py")
    for rel in builders:
        text = (REPO / rel).read_text(encoding="utf-8")
        assert "git push" not in text
        assert "push origin" not in text
        assert ".push(" not in text
        for marker in ("TODO", "FIXME", "HACK", "debugger", "test.skip"):
            assert marker not in text, (rel, marker)


# ---------------------------------------------------------------------------
# 活库对账（默认 skip；SIMPRAXIS_KB_INTEGRATION=1 才跑）
# ---------------------------------------------------------------------------

@pytest.mark.kb_integration
def test_live_four_table_counts():
    require_kb_integration(data=True)
    blk = BD.lance_tables_block()
    tmap = {t["table"]: t for t in blk["tables"]}
    assert tmap["kb_chunks"]["rows"] == 1542
    assert tmap["phase1_learnings"]["rows"] == 32
    assert tmap["solution_chunks"]["rows"] == 94
    # 锁内适配（2026-10-27 攒批86 活表对账）：phase2_learnings 表已建——Task6
    # Gate2 accept 后 persist 首行落库（66bafb1d，run27，rung=3），M2 时点
    # 「未建表」断言随活表现实更新为恰 1 行。
    assert tmap["phase2_learnings"]["exists"] is True
    assert tmap["phase2_learnings"]["rows"] == 1
