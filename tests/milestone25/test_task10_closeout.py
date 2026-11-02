"""Phase 2.5 Task 10 收尾件契约测试（AC-T10-1/2/3/4/6/7；全部 0 付费）。

四层（全部 hermetic：不联网、不读活库、不碰凭据内容）：
1. 对账纯函数（closeout_reconcile）——账 seq/ts 严格性、批准件逐批对账、
   缓存窗口口径、方法学 v2 区间落位、环境读数解析，合成输入反证；
2. 对账硬失败——seq 不连续 / 非 Z 后缀一律 ReconcileError（绝不产出"半绿"）；
3. 看板（build_dashboard_phase25）——测试摘要解析、AC 重数与声明比对不符即
   DashboardError；与对账件字段不符即 DashboardError；
4. 证据包与机判（build_evidence_pack_phase25 / check_phase25_closure）——
   manifest 全覆盖、0700/0600 权限、符号链接只登记不复制、必需件缺失/单件
   超限/越界删除/句柄守卫 fail-closed；证据根标签登记；AC 独立重数。
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from scripts.phase25 import build_dashboard_phase25 as D
from scripts.phase25 import build_evidence_pack_phase25 as P
from scripts.phase25 import closeout_reconcile as R
from scripts.verification import check_phase25_closure as J

REPO = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# 合成件
# ---------------------------------------------------------------------------

def _call(seq: int, *, ok: bool = True, ts: str = "2026-11-02T05:00:0{}Z",
          batch: str = "P25-B1", task: int = 3, stage: str = "coding_debug",
          approved: int = 6, used: int = 1) -> dict:
    return {"event": "call", "seq": seq, "ts_iso": ts.format(seq % 10),
            "ok": ok, "batch_id": batch, "task": task, "stage": stage,
            "batch_approved": approved, "batch_used_after": used,
            "tokens_in": 10, "tokens_out": 2,
            "tokens_cache_read": 0, "tokens_cache_create": 0,
            "err": None if ok else "EmptyResponseError"}


def _write_ledger(path: Path, rows: list[dict]) -> Path:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n",
                    encoding="utf-8")
    return path


def _synthetic_reconcile() -> dict:
    return {
        "schema": R.SCHEMA,
        "ledger_path": "/x/ledger.jsonl",
        "ledger_sha256": "a" * 64,
        "ledger": {
            "n_rows": 3, "n_calls": 3, "events": {"call": 3},
            "seq_first": 1, "seq_last": 3, "seq_contiguous_from_1": True,
            "ts_all_strict_z": True, "ts_non_z_seqs": [],
            "ok": 2, "fail": 1,
            "fail_rows": [{"seq": 2}],
            "by_task": {"3": 3}, "by_stage": {"coding_debug": 3},
            "by_batch": {"P25-B1": 3},
            "tokens": {"tokens_in": 30, "tokens_out": 6,
                       "tokens_cache_read": 0, "tokens_cache_create": 0},
            "first_ts_iso": "2026-11-02T05:00:01Z",
            "last_ts_iso": "2026-11-02T05:00:03Z",
        },
        "approvals": {"batches": {}, "total_approved": 3,
                      "problems": [], "ok": True},
        "call_cache": {"exists": True, "n_entries_total": 3,
                       "n_entries_in_window": [],
                       "n_entries_in_window_count": 0,
                       "coding_hits_structurally_unobservable": True},
        "tables": {k: {"rows": v, "dim": 768, "vector_set_sha256": "b" * 64}
                   for k, v in (("kb_chunks", 1542), ("solution_chunks", 94),
                                ("phase1_learnings", 44),
                                ("phase2_learnings", 1))},
        "methodology": {"per_task": {
            "t3": {"actual": 3, "status": "within_p50_p90",
                   "methodology_failure": False}},
            "problems": [], "ok": True},
        "env": {"containers_present": 0, "container_ids": [],
                "vram_mib": 14, "vram_ok_le_200": True,
                "mnt": {"total_bytes": 1, "used_bytes": 1,
                        "avail_bytes": 1}},
    }


def _plan_text(pairs: list[tuple[int, int]]) -> str:
    return "\n".join(f"- AC-T{t}-{k}：合成判据" for t, k in pairs)


_FULL_CMD = ('pytest tests -q -k "not test_04" '
             '-m "not paid and not kb_integration"')


def _full_suite_log(passed: int = 2044) -> str:
    """收尾两排除全套日志的最小真实形态（命令头 + 摘要 + 单条 rc）。"""
    return (f"# cmd: {_FULL_CMD}\n"
            f"{passed} passed, 7 skipped, 50 deselected in 1.0s\n"
            "rc=0\n")


_KBINT_CMD = ("SIMPRAXIS_KB_INTEGRATION=1 pytest tests -m kb_integration "
              "-q")


def _kbint_log(passed: int = 35) -> str:
    """收尾 kbint 活库套日志的最小真实形态（活库开关 + 选择器 + rc）。"""
    return (f"# cmd: {_KBINT_CMD}\n"
            f"{passed} passed, 2063 deselected in 1.0s\n"
            "rc=0\n")


def _proc7_payload() -> dict:
    """PROC-7 重盖章差异件的最小真实形态：四表行数不变、ok 自证。"""
    rows = {"kb_chunks": 1542, "solution_chunks": 94,
            "phase1_learnings": 44, "phase2_learnings": 1}
    return {
        "schema": "simpraxis.phase25.proc7_restamp_diff.v1",
        "tables": {k: {"rows_before": v, "rows_after": v,
                       "text_changed": [], "meta_noprov_changed": [],
                       "ingested_at_changed_count":
                       (v if k == "solution_chunks" else 0)}
                   for k, v in rows.items()},
        "q1q2q3_recall_equal": True,
        "problems": [],
        "ok": True,
    }


# ---------------------------------------------------------------------------
# 1. 对账纯函数
# ---------------------------------------------------------------------------

def test_ledger_stats_counts_and_flags():
    rows = [_call(1), _call(2, ok=False), {"event": "preflight_blocked",
                                           "seq": None}]
    led = R.ledger_stats(rows)
    assert led["n_calls"] == 2 and led["n_rows"] == 3
    assert led["events"] == {"call": 2, "preflight_blocked": 1}
    assert led["ok"] == 1 and led["fail"] == 1
    assert [(f["seq"], f["err"]) for f in led["fail_rows"]] == [
        (2, "EmptyResponseError")]
    assert led["seq_contiguous_from_1"] is True
    assert led["ts_all_strict_z"] is True
    assert led["by_task"] == {"3": 2}
    assert led["by_stage"] == {"coding_debug": 2}
    assert led["by_batch"] == {"P25-B1": 2}
    assert led["tokens"]["tokens_in"] == 20


def test_ledger_stats_detects_gap_and_non_z():
    rows = [_call(1), _call(3, ts="2026-11-02T05:00:03+00:00")]
    led = R.ledger_stats(rows)
    assert led["seq_contiguous_from_1"] is False
    assert led["ts_all_strict_z"] is False
    assert led["ts_non_z_seqs"] == [3]


def test_approvals_state_ok_and_mismatches(tmp_path):
    adir = tmp_path / "approvals"
    adir.mkdir()
    (adir / "P25-B1.json").write_text(json.dumps(
        {"batch_id": "P25-B1", "approved": 2, "task": "3",
         "ledger_phase": 25}), encoding="utf-8")
    calls = [_call(1, approved=2, used=1), _call(2, approved=2, used=2)]
    st = R.approvals_state(calls, adir)
    assert st["ok"] is True and st["total_approved"] == 2
    assert st["batches"]["P25-B1"]["used"] == 2

    (adir / "P25-B1.json").write_text(json.dumps(
        {"batch_id": "P25-B1", "approved": 1, "task": "3",
         "ledger_phase": 2}), encoding="utf-8")
    st = R.approvals_state(calls, adir)
    assert st["ok"] is False
    assert any("ledger_phase" in p for p in st["problems"])
    assert any("approved" in p for p in st["problems"])
    assert any("超批准" in p for p in st["problems"])

    st = R.approvals_state(calls, tmp_path / "absent")
    assert st["ok"] is False and "缺批准件" in st["problems"][0]


def test_cache_state_window_and_missing_dir(tmp_path):
    first = "2026-11-02T05:00:01Z"
    st = R.cache_state(tmp_path / "absent", first, 3)
    assert st["exists"] is False
    assert st["n_entries_in_window"] == []
    assert st["coding_hits_structurally_unobservable"] is True

    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "old.json").write_text(
        json.dumps({"ts_iso": "2026-10-28T00:00:00Z"}), encoding="utf-8")
    (cache / "new.json").write_text(
        json.dumps({"ts_iso": "2026-11-02T06:00:00Z"}), encoding="utf-8")
    st = R.cache_state(cache, first, 3)
    assert st["n_entries_total"] == 2
    assert [e["file"] for e in st["n_entries_in_window"]] == ["new.json"]
    assert st["n_entries_in_window_count"] == 1


def test_methodology_state_three_way(tmp_path):
    for name, p50, p90 in (("interval_t3", 4, 6), ("interval_t5", 22, 28),
                           ("interval_t9", 70, 101),
                           ("interval_t10", 1, 2)):
        (tmp_path / f"{name}.json").write_text(json.dumps(
            {"estimate": {"p50": p50, "p90": p90}}), encoding="utf-8")
    calls = [_call(i) for i in range(1, 6)]
    st = R.methodology_state(calls, tmp_path)
    assert st["per_task"]["t3"]["status"] == "within_p50_p90"
    assert st["per_task"]["t3"]["methodology_failure"] is False
    assert st["ok"] is True
    # t9 未执行：不判负、不改区间
    assert st["per_task"]["t9"]["status"] == "not_executed"
    assert st["per_task"]["t5b"]["status"] == "not_covered_new_experiment"

    st = R.methodology_state([_call(i) for i in range(1, 8)], tmp_path)
    assert st["per_task"]["t3"]["status"] == "above_p90"
    assert st["per_task"]["t3"]["methodology_failure"] is True
    assert st["ok"] is False
    assert any("方法学失败" in p for p in st["problems"])

    st = R.methodology_state([_call(1)], tmp_path)
    assert st["per_task"]["t3"]["status"] == "below_p50"
    assert st["per_task"]["t3"]["methodology_failure"] is False

    empty = tmp_path / "empty_estimates"
    empty.mkdir()
    st = R.methodology_state([_call(1)], empty)
    assert st["ok"] is False
    assert any("缺区间件" in p for p in st["problems"])


def test_env_state_parsers():
    st = R.env_state(
        docker_ps=lambda: "abc\ndef\n",
        nvidia_smi=lambda: "14\n14\n",
        df=lambda: ("Filesystem 1B-blocks Used Available Capacity Mounted\n"
                    "/dev/sda1 500000000 42000000 458000000 9% /mnt\n"))
    assert st["containers_present"] == 2
    assert st["vram_mib"] == 14 and st["vram_ok_le_200"] is True
    assert st["mnt"]["avail_bytes"] == 458000000

    st = R.env_state(docker_ps=lambda: "", nvidia_smi=lambda: "350\n",
                     df=lambda: "")
    assert st["containers_present"] == 0
    assert st["vram_ok_le_200"] is False
    assert st["mnt"] is None

    st = R.env_state(docker_ps=lambda: "", nvidia_smi=lambda: "",
                     df=lambda: "")
    assert st["vram_mib"] is None  # 空输出按未测处理，不当 0


def test_build_fail_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "tables_state", lambda uri: {
        "kb_chunks": {"rows": 1542, "dim": 768, "vector_set_sha256": "c" * 64},
        "_schema": "simpraxis.t7_2.vector_identity.v1"})
    adir = tmp_path / "approvals"
    adir.mkdir()
    (adir / "P25-B1.json").write_text(json.dumps(
        {"batch_id": "P25-B1", "approved": 2, "task": "3",
         "ledger_phase": 25}), encoding="utf-8")
    est = tmp_path / "est"
    est.mkdir()
    (est / "interval_t3.json").write_text(json.dumps(
        {"estimate": {"p50": 1, "p90": 6}}), encoding="utf-8")
    env = {"containers_present": 0, "container_ids": [], "vram_mib": 14,
           "vram_ok_le_200": True, "mnt": None}

    good = _write_ledger(tmp_path / "good.jsonl", [_call(1), _call(2)])
    out = R.build(ledger_path=good, approvals_dir=adir,
                  cache_dir=tmp_path / "cache", estimates_dir=est, env=env)
    assert out["schema"] == R.SCHEMA
    assert out["ledger"]["n_calls"] == 2
    assert out["tables"]["kb_chunks"]["rows"] == 1542

    gap = _write_ledger(tmp_path / "gap.jsonl", [_call(1), _call(3)])
    with pytest.raises(R.ReconcileError, match="seq 不连续"):
        R.build(ledger_path=gap, approvals_dir=adir,
                cache_dir=tmp_path / "cache", estimates_dir=est, env=env)

    bad_ts = _write_ledger(tmp_path / "z.jsonl",
                           [_call(1, ts="2026-11-02T05:00:01+08:00")])
    with pytest.raises(R.ReconcileError, match="严格 Z"):
        R.build(ledger_path=bad_ts, approvals_dir=adir,
                cache_dir=tmp_path / "cache", estimates_dir=est, env=env)

    empty = _write_ledger(tmp_path / "empty.jsonl", [])
    with pytest.raises(R.ReconcileError, match="为空"):
        R.build(ledger_path=empty, approvals_dir=adir,
                cache_dir=tmp_path / "cache", estimates_dir=est, env=env)


# ---------------------------------------------------------------------------
# 3. 看板
# ---------------------------------------------------------------------------

def test_tests_state_parses_summary_and_rc(tmp_path):
    log = tmp_path / "run.log"
    log.write_text("# cmd: pytest tests -q\n"
                   "xxxx\n1875 passed, 7 skipped, 47 deselected in 321.45s\n"
                   "rc=0\n", encoding="utf-8")
    st = D.tests_state(log)
    assert st["passed"] == 1875 and st["skipped"] == 7
    assert st["deselected"] == 47 and st["rc"] == 0
    assert st["command"] == "pytest tests -q"

    log.write_text("no summary here\n", encoding="utf-8")
    with pytest.raises(D.DashboardError, match="找不到 pytest 摘要"):
        D.tests_state(log)

    # 多段日志：前段 rc=1 / 后段 rc=0 会与"最后一条摘要"错配 → fail-closed
    log.write_text("10 failed, 2 passed in 1.0s\nrc=1\n"
                   "20 passed in 2.0s\nrc=0\n", encoding="utf-8")
    with pytest.raises(D.DashboardError, match="rc= 行应恰 1 条"):
        D.tests_state(log)
    # 单摘要但缺 rc 行同样不许静默当 0
    log.write_text("20 passed in 2.0s\n", encoding="utf-8")
    with pytest.raises(D.DashboardError, match="rc= 行应恰 1 条"):
        D.tests_state(log)
    # 无 `# cmd:` 头的日志无法证明跑的是哪套 → fail-closed（不许顶替规范命令串）
    log.write_text("2044 passed, 7 skipped, 50 deselected in 1.0s\nrc=0\n",
                   encoding="utf-8")
    with pytest.raises(D.DashboardError, match="找不到 `# cmd:` 行"):
        D.tests_state(log)
    assert D.tests_state(None) is None


def test_ac_counts_match_and_mismatch(tmp_path, monkeypatch):
    pairs = [(0, k) for k in range(1, 4)] + [(10, 1)]
    plan = tmp_path / "plan.md"
    plan.write_text(_plan_text(pairs + pairs), encoding="utf-8")  # 重复去重
    monkeypatch.setattr(D, "DECLARED_AC_TOTAL", 4)
    monkeypatch.setattr(D, "DECLARED_AC_PER_TASK",
                        {"T0": 3, "T10": 1, **{f"T{i}": 0
                                              for i in range(1, 10)}})
    counts = D.ac_counts(plan)
    assert counts["total"] == 4 and counts["matches_declared"] is True
    assert counts["per_task"]["T0"] == 3

    monkeypatch.setattr(D, "DECLARED_AC_TOTAL", 5)
    assert D.ac_counts(plan)["matches_declared"] is False


def test_dashboard_build_and_fail_closed(tmp_path, monkeypatch):
    plan = tmp_path / "plan.md"
    pairs = [(0, k) for k in range(1, 4)]
    plan.write_text(_plan_text(pairs), encoding="utf-8")
    monkeypatch.setattr(D, "DECLARED_AC_TOTAL", 3)
    monkeypatch.setattr(D, "DECLARED_AC_PER_TASK",
                        {"T0": 3, **{f"T{i}": 0 for i in range(1, 11)}})
    monkeypatch.setattr(J, "DECLARED_TOTAL", 3)
    monkeypatch.setattr(J, "DECLARED_PER_TASK",
                        {"T0": 3, **{f"T{i}": 0 for i in range(1, 11)}})
    rec_path = tmp_path / "rec.json"
    rec_path.write_text(json.dumps(_synthetic_reconcile()), encoding="utf-8")
    log = tmp_path / "run.log"
    log.write_text(_full_suite_log(), encoding="utf-8")

    dash = D.build(reconcile_path=rec_path, tests_result=log,
                   plan_path=plan)
    assert dash["schema"] == D.SCHEMA
    assert dash["tests"]["rc"] == 0
    assert dash["ac_counts"]["total"] == 3
    # 看板与对账件同口径 → 机判不看板判负
    assert J.check_dashboard(dash, _synthetic_reconcile()) == []

    with pytest.raises(D.DashboardError, match="缺收尾对账件"):
        D.build(reconcile_path=tmp_path / "absent.json", tests_result=None,
                plan_path=plan)

    bad = _synthetic_reconcile()
    bad["schema"] = "other"
    rec_path.write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(D.DashboardError, match="schema"):
        D.build(reconcile_path=rec_path, tests_result=None, plan_path=plan)


def test_check_dashboard_flags_drift():
    rec = _synthetic_reconcile()
    good = {
        "schema": D.SCHEMA,
        "tests": {"rc": 0, "passed": 2044, "deselected": 50,
                  "command": _FULL_CMD},
        "paid_reconciliation": {
            "ledger": {"n_calls": rec["ledger"]["n_calls"]},
            "ledger_sha256": rec["ledger_sha256"]},
        "lance_tables": {k: {"rows": v["rows"]}
                         for k, v in rec["tables"].items()},
        "ac_counts": {"total": J.DECLARED_TOTAL,
                      "per_task": J.DECLARED_PER_TASK},
        "tasks": [{"task": t, "status": "stopped_by_user"}
                  for t in ("T7", "T8", "T9")],
    }
    assert J.check_dashboard(good, rec) == []
    bad = json.loads(json.dumps(good))
    bad["tests"]["rc"] = 1
    bad["paid_reconciliation"]["ledger"]["n_calls"] = 99
    bad["lance_tables"]["kb_chunks"]["rows"] = 1
    bad["tasks"].append({"task": "T6", "status": "stopped_by_user"})
    problems = J.check_dashboard(bad, rec)
    assert len(problems) == 4


# ---------------------------------------------------------------------------
# 4. 证据包
# ---------------------------------------------------------------------------

def _synthetic_tree(tmp_path: Path) -> dict:
    p25 = tmp_path / "phase25"
    runs25 = tmp_path / "runs25"
    repo = tmp_path / "repo"
    p1 = tmp_path / "p1"
    runs25.mkdir()
    p1.mkdir()
    for pack_rel, _root, src_rel, _logical, required in P.FILES:
        if not required:
            continue
        f = p25 / src_rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"synthetic": src_rel}), encoding="utf-8")
    log = p25 / "task10_milestone" / "task10_fullsuite.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(_full_suite_log(), encoding="utf-8")
    # AC-T10-1 的另一条腿与 P11/PROC-7 差异件：同样是包内硬要求
    (p25 / "task10_milestone/task10_kbint.log").write_text(
        _kbint_log(), encoding="utf-8")
    (p25 / "task10_milestone/proc7_restamp_diff.json").write_text(
        json.dumps(_proc7_payload()), encoding="utf-8")
    for _prefix, _root, src_dir, _logical, _req, _ex in P.DIR_COPIES:
        if not _req:          # 可选目录（如三签件）默认不造，留给各自用例
            continue
        d = p25 / src_dir
        d.mkdir(parents=True, exist_ok=True)
        (d / "evidence.json").write_text("{}", encoding="utf-8")
    for rel in ("scripts/phase25/task6_r4_prereg.md",
                "scripts/phase25/snapshots/task6_r4_point_predictions.json",
                "scripts/phase25/task6_r4_reference_sim.py"):
        f = repo / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("synthetic\n", encoding="utf-8")
    # 环境接线：绝对且 broken 的符号链接（墙 A 只读 tour 源）
    pyshim = p25 / "walls/t5_freeze/gate_env/pyshim"
    pyshim.mkdir(parents=True)
    os.symlink("/opt/libero_tour/libero", pyshim / "libero")
    proc = tmp_path / "proc"
    proc.mkdir()
    return {"p25": p25, "runs25": runs25, "repo": repo, "p1": p1,
            "pack": p25 / "task10_milestone/evidence_pack",
            "lance": str(tmp_path / "lancedb"), "proc": str(proc)}


def _build(tree: dict, **kw) -> dict:
    return P.build_pack(p25=tree["p25"], runs25=tree["runs25"],
                        repo=tree["repo"], p1=tree["p1"], pack=tree["pack"],
                        lance_uri=tree["lance"], proc_root=tree["proc"], **kw)


def test_pack_build_manifest_modes_and_symlinks(tmp_path):
    tree = _synthetic_tree(tmp_path)
    man = _build(tree, clean=True)
    pack = tree["pack"]
    assert man["schema"] == P.SCHEMA and man["milestone"] == "2.5"
    template = next(k for k in man["evidence_roots"]
                    if k.startswith(J.REQUIRED_EVIDENCE_ROOT_TAG))
    assert template == "@runs/phase25/<thread>/stage_<id>/"
    assert man["evidence_roots"][template]["rows_using"] == 0
    assert man["policy"]["symlinks_in_pack"] is False
    assert man["policy"]["modes"]["files"] == "0600"
    assert man["secrets_scan_hits"] == []

    listed = {r["path"] for r in man["files"]}
    actual = {str(p.relative_to(pack)) for p in pack.rglob("*")
              if p.is_file()}
    assert listed == actual - {"manifest.json"}, "manifest 必须逐件覆盖"
    assert man["n_files"] == len(man["files"])

    for p in pack.rglob("*"):
        assert not p.is_symlink(), f"包内混入符号链接：{p}"
        mode = stat.S_IMODE(p.stat().st_mode)
        assert mode == (0o700 if p.is_dir() else 0o600), f"{p} mode={oct(mode)}"

    excl = json.loads((pack / "symlinks_excluded.json").read_text())
    inside = excl["items"][0]["symlinks_inside"][0]
    assert inside["target"] == "/opt/libero_tour/libero"
    assert inside["absolute"] is True and inside["copied"] is False
    # 收尾件尚未生成 → 如实登记缺口，绝不伪造绿件
    assert (pack / "closeout/closeout_reconcile_MISSING.json").is_file()
    assert J.check_pack(pack)[0] == []


def test_pack_registers_not_executed_tasks_honestly(tmp_path):
    """T7/T8/T9 用户停做：挂牌 NOT_EXECUTED，不用空目录冒充已执行。"""
    tree = _synthetic_tree(tmp_path)
    man = _build(tree, clean=True)
    pack = tree["pack"]
    for slug, task in (("task7_stages", "Task 7"),
                       ("task8_drill", "Task 8"),
                       ("task9_vla", "Task 9")):
        rec = json.loads((pack / f"{slug}/NOT_EXECUTED.json").read_text())
        assert rec["status"] == "stopped_by_user"
        assert task in rec["task"]
        assert rec["evidence_present"] is False
        assert rec["would_have_included"], "须列出本应入包的证据项"
        assert "2026-11-02" in rec["adjudication"]
    listed = {r["path"] for r in man["files"]}
    assert {"task7_stages/NOT_EXECUTED.json", "task8_drill/NOT_EXECUTED.json",
            "task9_vla/NOT_EXECUTED.json"} <= listed
    # 三签件未落时如实挂牌，落件后由 signs/* 取代
    assert (pack / "task10_closeout/signs_MISSING.json").is_file()


def test_pack_copies_signs_when_present(tmp_path):
    tree = _synthetic_tree(tmp_path)
    signs = tree["p25"] / "task10_milestone" / "signs"
    signs.mkdir(parents=True)
    (signs / "executor.json").write_text('{"role": "executor", "ok": true}\n')
    (signs / "verifier.json").write_text('{"role": "verifier", "ok": true}\n')
    man = _build(tree, clean=True)
    pack = tree["pack"]
    assert (pack / "task10_closeout/signs/executor.json").is_file()
    assert (pack / "task10_closeout/signs/verifier.json").is_file()
    assert not (pack / "task10_closeout/signs_MISSING.json").exists()
    listed = {r["path"] for r in man["files"]}
    assert {"task10_closeout/signs/executor.json",
            "task10_closeout/signs/verifier.json"} <= listed


def test_pack_missing_required_file_fail_closed(tmp_path):
    tree = _synthetic_tree(tmp_path)
    victim = tree["p25"] / P.FILES[0][2]
    victim.unlink()
    with pytest.raises(P.PackError, match="必需证据缺失"):
        _build(tree, clean=True)


def test_pack_undeclared_symlink_is_fatal(tmp_path):
    tree = _synthetic_tree(tmp_path)
    target = tree["p25"] / "watchdog/leak.txt"
    os.symlink("/etc/hostname", target)
    with pytest.raises(SystemExit, match="H-11"):
        _build(tree, clean=True)


def test_pack_oversize_file_fail_closed(tmp_path, monkeypatch):
    tree = _synthetic_tree(tmp_path)
    monkeypatch.setattr(P, "MAX_FILE_BYTES", 16)
    with pytest.raises(P.PackError, match=">10MB"):
        _build(tree, clean=True)


def test_pack_clean_guard_holders_and_bounds(tmp_path, monkeypatch):
    tree = _synthetic_tree(tmp_path)
    monkeypatch.setattr(P.M2, "_processes_holding_paths",
                        lambda *a, **k: [{"pid": 1, "fd": 3,
                                          "open_path": "/x", "cmdline": "y"}])
    with pytest.raises(P.PackError, match="拒绝 --clean"):
        _build(tree, clean=True)

    monkeypatch.undo()
    outside = tmp_path / "outside_pack"
    with pytest.raises(P.PackError, match="不在 phase25 根内"):
        P.build_pack(p25=tree["p25"], runs25=tree["runs25"],
                     repo=tree["repo"], p1=tree["p1"], pack=outside,
                     lance_uri=tree["lance"], proc_root=tree["proc"],
                     clean=True)


def test_pack_secrets_scan_fail_closed(tmp_path):
    tree = _synthetic_tree(tmp_path)
    leak = tree["p25"] / "watchdog/alerts.jsonl"
    # 合成密钥在运行期拼接，文件里不出现可被仓库扫描命中的字面量。
    leak.write_text("api_key = sk-" + "e" * 48 + "\n", encoding="utf-8")
    with pytest.raises(P.PackError, match="密钥正则"):
        _build(tree, clean=True)


# ---------------------------------------------------------------------------
# 4b. 机判（独立重数 + 证据逐字节互证）
# ---------------------------------------------------------------------------

def test_judge_count_acs_independent(tmp_path, monkeypatch):
    plan = tmp_path / "plan.md"
    pairs = [(0, 1), (0, 2), (1, 1)]
    plan.write_text(_plan_text(pairs), encoding="utf-8")
    monkeypatch.setattr(J, "DECLARED_TOTAL", 3)
    monkeypatch.setattr(J, "DECLARED_PER_TASK",
                        {"T0": 2, "T1": 1, **{f"T{i}": 0
                                             for i in range(2, 11)}})
    counts, problems = J.count_acs(plan)
    assert problems == []
    assert counts["total"] == 3 and counts["per_task"]["T0"] == 2
    assert len(counts["ids_sha256"]) == 64

    monkeypatch.setattr(J, "DECLARED_TOTAL", 4)
    _counts, problems = J.count_acs(plan)
    assert any("AC 总数" in p for p in problems)


def _harden(root: Path) -> None:
    """把合成包拨到 0700/0600（机判逐件复算权限，不接受 umask 默认值）。"""
    for p in [root, *sorted(root.rglob("*"))]:
        p.chmod(0o600 if p.is_file() else 0o700)


def _verdict_payload() -> dict:
    """与真实判决件同构（五条件各为带 ok 的对象 + 预注册数值）。"""
    return {
        "verdict": "RETAIN_R4", "retain": True,
        "conditions": {
            "1_thresholds": {"ok": True, "all_pass": True, "detail": {
                "overall": {"hits": 45, "total": 47, "gold_at_8": 0.9574,
                            "pass": True},
                "solution_route": {"hits": 18, "total": 18,
                                   "gold_at_8": 1.0, "pass": True},
                "learning_route": {"hits": 11, "total": 12,
                                   "gold_at_8": 0.9167, "pass": True}}},
            "2_h3_subset_h4": {"ok": True, "n_h3": 38, "n_h4": 45,
                               "flip_loss": []},
            "3_residual_recovery": {"ok": True, "n_residual": 9,
                                    "n_recovered": 7, "n_gold_id_direct": 6,
                                    "recovered": [{"id": f"r{i}"}
                                                  for i in range(7)]},
            "4_data_identity": {"ok": True, "problems": []},
            "5_engineering": {"ok": True, "problems": []},
        },
        "point_prediction_match": {"ok": True, "n_cases": 47, "mismatches": []},
        "iter5_sha256":
            "9a704e0081c62e1d938bd023bddc572eafac3768450f08327c7a8ad09fb59e16",
    }


def _minimal_pack(tmp_path: Path) -> Path:
    pack = tmp_path / "pack"
    win = pack / "task6_stage_b/window" / J._STAGE_B_VERDICT.parent.name
    win.mkdir(parents=True)
    (win / "task6_r4_verdict.json").write_text(
        json.dumps(_verdict_payload()), encoding="utf-8")
    # T7/T8/T9 停做挂牌是机判硬要求：缺挂牌即问题
    for slug in ("task7_stages", "task8_drill", "task9_vla"):
        (pack / slug).mkdir()
        (pack / slug / "NOT_EXECUTED.json").write_text(
            json.dumps({"task": slug, "status": "stopped_by_user",
                        "adjudication": "用户 2026-11-02 裁决"},
                       ensure_ascii=False), encoding="utf-8")
    signs = pack / "task10_closeout/signs"
    signs.mkdir(parents=True)
    for role in ("executor", "reviewer", "verifier"):
        (signs / f"{role}.json").write_text(json.dumps({
            "schema": "simpraxis.phase25.sign.v1", "role": role,
            "commit": "abc", "verdict": "APPROVE"}), encoding="utf-8")
    (pack / "task10_closeout/task10_fullsuite.log").write_text(
        _full_suite_log(), encoding="utf-8")
    # AC-T10-1 两条腿 + P11/PROC-7：kbint 日志与重盖章差异件同为包内硬要求
    (pack / "task10_closeout/task10_kbint.log").write_text(
        _kbint_log(), encoding="utf-8")
    (pack / "closeout").mkdir()
    (pack / "closeout/proc7_restamp_diff.json").write_text(
        json.dumps(_proc7_payload()), encoding="utf-8")
    (pack / "a.txt").write_text("hello\n", encoding="utf-8")
    _write_manifest(pack)
    _harden(pack)
    return pack


def _write_manifest(pack: Path, **override) -> dict:
    """按包内实有文件重建 manifest（全覆盖；manifest.json 自身不登记）。"""
    man = {"git_commit": "abc",
           "evidence_roots": {"@runs/phase25/<thread>/stage_<id>/": {
               "tag": "@runs/", "subdir": "phase25", "rows_using": 0}},
           "files": [{"path": str(p.relative_to(pack)),
                      "sha256": J._sha256(p)}
                     for p in sorted(pack.rglob("*"))
                     if p.is_file() and p.name != "manifest.json"]}
    man.update(override)
    (pack / "manifest.json").write_text(json.dumps(man), encoding="utf-8")
    return man


def _verdict_in(pack: Path) -> Path:
    return (pack / "task6_stage_b/window" / J._STAGE_B_VERDICT.parent.name
            / "task6_r4_verdict.json")


def test_judge_science_and_pack(tmp_path, monkeypatch):
    # 包外活件置空：判决件只认包内副本（证据自足，无包外兜底）
    monkeypatch.setattr(J, "_STAGE_B_VERDICT", tmp_path / "live_absent.json")
    rec = _synthetic_reconcile()
    pack = _minimal_pack(tmp_path)
    assert J.check_science(rec, pack) == ([], [])
    # 包内缺副本即问题——即使包外活件存在也不许兜底
    live = tmp_path / "live.json"
    live.write_text(json.dumps(_verdict_payload()), encoding="utf-8")
    monkeypatch.setattr(J, "_STAGE_B_VERDICT", live)
    problems, _gaps = J.check_science(rec, tmp_path / "no_pack")
    assert any("包内缺 T6 Stage B 判决件" in p for p in problems)
    monkeypatch.setattr(J, "_STAGE_B_VERDICT", tmp_path / "live_absent.json")

    rec["methodology"]["per_task"]["t3"]["methodology_failure"] = True
    problems, _gaps = J.check_science(rec, pack)
    assert any("方法学失败" in p for p in problems)
    rec["methodology"]["per_task"]["t3"]["methodology_failure"] = False

    # 包内副本与包外活件逐字节一致 → 绿；不一致 → 问题（R3 口径 (b)）
    live.write_text(_verdict_in(pack).read_text(encoding="utf-8"),
                    encoding="utf-8")
    monkeypatch.setattr(J, "_STAGE_B_VERDICT", live)
    assert J.check_science(rec, pack) == ([], [])
    live.write_text(json.dumps(_verdict_payload(), indent=1) + "\n",
                    encoding="utf-8")     # 语义等价、字节不同
    problems, _gaps = J.check_science(rec, pack)
    assert any("逐字节不一致" in p for p in problems)
    monkeypatch.setattr(J, "_STAGE_B_VERDICT", tmp_path / "live_absent.json")

    # 五条件必须恰 5 键、每条 ok 为 true，且预注册数值逐项相符
    for mutate, needle in (
        (lambda v: v["conditions"].update({"6_extra": {"ok": True}}),
         "条件键不符"),
        (lambda v: v["conditions"]["5_engineering"].update({"ok": False}),
         "5_engineering 未过"),
        (lambda v: v["conditions"]["1_thresholds"].update({"all_pass": False}),
         "all_pass 非 true"),
        (lambda v: v["conditions"]["1_thresholds"]["detail"]["overall"].update(
            {"hits": 44}), "overall 命中 44/47"),
        (lambda v: v["conditions"]["1_thresholds"]["detail"][
            "learning_route"].update({"gold_at_8": 0.7}), "learning_route 闸未过"),
        (lambda v: v["conditions"]["2_h3_subset_h4"].update(
            {"flip_loss": [{"id": "sol_win10_exit"}]}), "翻失非空"),
        (lambda v: v["conditions"]["3_residual_recovery"].update(
            {"n_recovered": 6}), "残留/回收/直中"),
        (lambda v: v["conditions"]["4_data_identity"].update(
            {"ok": True, "problems": ["x"]}), "4_data_identity 残留问题"),
        (lambda v: v["point_prediction_match"].update({"n_cases": 46}),
         "点预测逐案不符"),
        (lambda v: v["point_prediction_match"].update(
            {"mismatches": [{"case": 1}]}), "点预测逐案不符"),
    ):
        v = _verdict_payload()
        mutate(v)
        _verdict_in(pack).write_text(json.dumps(v), encoding="utf-8")
        problems, _gaps = J.check_science(rec, pack)
        assert any(needle in p for p in problems), (needle, problems)
    _verdict_in(pack).write_text(json.dumps(_verdict_payload()),
                                 encoding="utf-8")

    assert J.check_pack(pack)[0] == []

    man = json.loads((pack / "manifest.json").read_text())
    man["evidence_roots"] = {"@runs/": {}}
    man["files"][0]["sha256"] = "0" * 64
    (pack / "manifest.json").write_text(json.dumps(man), encoding="utf-8")
    problems, _gaps = J.check_pack(pack)
    assert any("evidence_roots" in p for p in problems)
    assert any("sha256 不符" in p for p in problems)

    # 停做挂牌缺失 = 问题（缺证不许静默通过），且缺口入 provenance_limited
    (pack / "task8_drill" / "NOT_EXECUTED.json").unlink()
    problems, gaps = J.check_pack(pack)
    assert any("task8_drill" in p for p in problems)
    assert {g["what"] for g in gaps} == {"task7_stages", "task9_vla"}


def test_judge_pack_signs_and_modes(tmp_path):
    pack = _minimal_pack(tmp_path)
    # 三签件缺一角色 → 问题
    (pack / "task10_closeout/signs/verifier.json").unlink()
    problems, _gaps = J.check_pack(pack)
    assert any("三签件缺角色" in p for p in problems)

    # 非法 verdict → 问题；REJECT 同样不得算作终态三签
    for bad_verdict, needle in (("MAYBE", "未 APPROVE"), ("REJECT", "未 APPROVE")):
        (pack / "task10_closeout/signs/verifier.json").write_text(
            json.dumps({"schema": "simpraxis.phase25.sign.v1",
                        "role": "verifier", "commit": "abc",
                        "verdict": bad_verdict}), encoding="utf-8")
        _write_manifest(pack)
        _harden(pack)
        problems, _gaps = J.check_pack(pack)
        assert any(needle in p for p in problems), (bad_verdict, problems)

    # 签绑定的 commit 必须是包冻结的 manifest.git_commit
    (pack / "task10_closeout/signs/verifier.json").write_text(
        json.dumps({"schema": "simpraxis.phase25.sign.v1",
                    "role": "verifier", "commit": "f" * 7,
                    "verdict": "APPROVE"}), encoding="utf-8")
    _write_manifest(pack)
    _harden(pack)
    problems, _gaps = J.check_pack(pack)
    assert any("绑定 commit" in p for p in problems)

    # role 必须自证且与文件名一致（reviewer 签写成 role=verifier → 判负）
    (pack / "task10_closeout/signs/verifier.json").write_text(
        json.dumps({"schema": "simpraxis.phase25.sign.v1",
                    "role": "reviewer", "commit": "abc",
                    "verdict": "APPROVE"}), encoding="utf-8")
    _write_manifest(pack)
    _harden(pack)
    problems, _gaps = J.check_pack(pack)
    assert any("与文件名不符" in p for p in problems)

    (pack / "task10_closeout/signs/verifier.json").write_text(
        json.dumps({"schema": "simpraxis.phase25.sign.v1",
                    "role": "verifier", "commit": "abc",
                    "verdict": "APPROVE"}), encoding="utf-8")
    (pack / "task10_closeout/signs/reviewer.json").write_text(
        json.dumps({"schema": "simpraxis.phase25.sign.v1",
                    "role": "verifier", "commit": "abc",
                    "verdict": "APPROVE"}), encoding="utf-8")
    _write_manifest(pack)
    _harden(pack)
    problems, _gaps = J.check_pack(pack)
    assert any("与文件名不符" in p for p in problems)

    # 还原成合格三签，继续测权限/覆盖面
    (pack / "task10_closeout/signs/reviewer.json").write_text(
        json.dumps({"schema": "simpraxis.phase25.sign.v1",
                    "role": "reviewer", "commit": "abc",
                    "verdict": "APPROVE"}), encoding="utf-8")
    _write_manifest(pack)
    _harden(pack)
    assert J.check_pack(pack)[0] == []

    # 权限放水（0644 文件 / 0755 目录）→ 问题
    (pack / "a.txt").chmod(0o644)
    problems, _gaps = J.check_pack(pack)
    assert any("权限不符 0700/0600" in p for p in problems)

    # 包内未登记文件（manifest 未全覆盖）→ 问题
    (pack / "a.txt").chmod(0o600)
    (pack / "extra.txt").write_text("x\n", encoding="utf-8")
    (pack / "extra.txt").chmod(0o600)
    problems, _gaps = J.check_pack(pack)
    assert any("未登记文件" in p for p in problems)

    # MISSING 挂牌与实签件并存 → 状态自相矛盾（不许挂牌盖掉 REJECT）
    (pack / "extra.txt").unlink()
    _write_manifest(pack)
    (pack / "task10_closeout/signs_MISSING.json").write_text(
        "{}", encoding="utf-8")
    _write_manifest(pack)
    _harden(pack)
    problems, _gaps = J.check_pack(pack)
    assert any("同时存在" in p for p in problems)

    # 既无签件也无挂牌 → 问题（不是缺口）
    (pack / "task10_closeout/signs_MISSING.json").unlink()
    for p in (pack / "task10_closeout/signs").glob("*.json"):
        p.unlink()
    _write_manifest(pack)
    _harden(pack)
    problems, _gaps = J.check_pack(pack)
    assert any("缺三签件目录且无 signs_MISSING" in p for p in problems)


def test_judge_pack_requires_full_suite_log(tmp_path):
    """AC-T10-1：包内冻结的冷跑日志必须自证两排除 + rc=0 + 规模下限。"""
    pack = _minimal_pack(tmp_path)
    log = pack / "task10_closeout/task10_fullsuite.log"
    good = _full_suite_log()

    cases = [
        ("# cmd: pytest tests/milestone25 -q\n" + good.split("\n", 1)[1],
         "两排除项"),
        (good.replace("rc=0", "rc=1"), "rc 行应恰为 rc=0"),
        (_full_suite_log(passed=25), "passed=25 < 全套下限"),
        (good.replace(", 50 deselected", ""), "缺 deselected 计数"),
        (good + "# cmd: pytest tests -q\n", "`# cmd:` 行应恰 1 条"),
    ]
    for body, needle in cases:
        log.write_text(body, encoding="utf-8")
        _write_manifest(pack)
        _harden(pack)
        problems, _gaps = J.check_pack(pack)
        assert any(needle in p for p in problems), (needle, problems)

    log.unlink()
    _write_manifest(pack)
    _harden(pack)
    problems, _gaps = J.check_pack(pack)
    assert any("缺收尾冷跑日志" in p for p in problems)


def test_judge_tests_cross_two_readers(tmp_path):
    """R3 口径 (a)：看板 tests 段必须与包内冻结日志逐字段相等。

    两个独立读者各自成立还不够——看板被喂了另一份日志时，passed/命令口径
    会分叉，这一层专抓该情形；deselected 下限挡住 `-k` 单排除的子集跑。
    """
    pack = _minimal_pack(tmp_path)
    dash = {"tests": {"command": _FULL_CMD, "passed": 2044, "skipped": 7,
                      "deselected": 50, "rc": 0,
                      "summary": "2044 passed, 7 skipped, 50 deselected "
                                 "in 1.0s"}}
    assert J.check_tests_cross(pack, dash) == []

    for key, value, needle in (
        ("command", 'pytest tests -q -k "not paid and not kb_integration"',
         "tests.command"),
        ("passed", 1875, "tests.passed"),
        ("skipped", 1, "tests.skipped"),
        ("deselected", 47, "tests.deselected"),
        ("rc", 1, "tests.rc"),
        ("summary", "1875 passed, 7 skipped, 47 deselected in 1.0s",
         "tests.summary"),
    ):
        bad = json.loads(json.dumps(dash))
        bad["tests"][key] = value
        assert any(needle in p for p in J.check_tests_cross(pack, bad)), key

    # tests=null / 日志缺失：各自检查已记问题，此处不重复报
    assert J.check_tests_cross(pack, {"tests": None}) == []
    (pack / "task10_closeout/task10_fullsuite.log").unlink()
    assert J.check_tests_cross(pack, dash) == []


def test_judge_suite_log_deselected_floor(tmp_path):
    """`-k "not test_04"` 单排除只有个位数去选——收尾必须是两排除全套。"""
    fields, problems = J.parse_suite_log(_full_suite_log())
    assert fields["deselected"] == 50 and problems == []
    single = ("# cmd: pytest tests -q -k \"not test_04\"\n"
              "2044 passed, 7 skipped, 8 deselected in 1.0s\nrc=0\n")
    _fields, problems = J.parse_suite_log(single)
    assert any("deselected=8" in p for p in problems)


def test_judge_kbint_log_parser(tmp_path):
    """kbint 日志：活库开关/选择器/rc/规模四件缺一不可，默认 skip 冒充不了。"""
    fields, problems = J.parse_kbint_log(_kbint_log())
    assert fields["passed"] == 35 and problems == []
    # 少活库开关（裸跑 kbint 选择器会全 skip）→ 判负
    no_env = ("# cmd: pytest tests -m kb_integration -q\n"
              "35 passed, 2063 deselected in 1.0s\nrc=0\n")
    _f, problems = J.parse_kbint_log(no_env)
    assert any("活库开关" in p for p in problems)
    # rc 非 0 / 重复 rc → 判负
    _f, problems = J.parse_kbint_log(_kbint_log().replace("rc=0", "rc=1"))
    assert any("kbint 日志 rc 行应恰为 rc=0" in p for p in problems)
    _f, problems = J.parse_kbint_log(_kbint_log() + "rc=0\n")
    assert any("kbint 日志 rc 行应恰为 rc=0" in p for p in problems)
    # 规模塌到个位数（等价于大部分被 skip）→ 判负
    _f, problems = J.parse_kbint_log(_kbint_log(passed=3))
    assert any("活库套下限" in p for p in problems)
    # 包内缺 kbint 日志 = 问题（不能靠「默认 skip」蒙混）
    pack = _minimal_pack(tmp_path)
    (pack / "task10_closeout/task10_kbint.log").unlink()
    _write_manifest(pack)
    assert any("缺收尾 kbint 活库套日志" in p
               for p in J.check_pack(pack)[0])


def test_judge_proc7_diff(tmp_path):
    """P11/PROC-7：kbint 重盖章必须由差异件自证「只动戳记」。"""
    pack = _minimal_pack(tmp_path)
    assert J.check_proc7(pack) == []
    path = pack / "closeout/proc7_restamp_diff.json"
    for mutate, needle in (
        (lambda d: d.update({"ok": False}), "未自证 ok"),
        (lambda d: d.update({"problems": ["solution_chunks 文本变化"]}),
         "挂问题"),
        (lambda d: d.update({"q1q2q3_recall_equal": False}), "召回快照恒等"),
        (lambda d: d["tables"]["kb_chunks"].update({"rows_after": 1541}),
         "kb_chunks.rows_after"),
    ):
        d = _proc7_payload()
        mutate(d)
        path.write_text(json.dumps(d), encoding="utf-8")
        assert any(needle in p for p in J.check_proc7(pack)), needle
    path.unlink()
    _write_manifest(pack)
    assert any("缺 PROC-7 重盖章差异件" in p for p in J.check_pack(pack)[0])


def test_judge_dashboard_asserts_suite_command(tmp_path):
    """看板 tests 段不许只看 rc/passed：命令口径与去选数同样入判。"""
    rec = _synthetic_reconcile()
    good = {
        "schema": D.SCHEMA,
        "tests": {"rc": 0, "passed": 2044, "deselected": 50,
                  "command": _FULL_CMD},
        "paid_reconciliation": {
            "ledger": {"n_calls": rec["ledger"]["n_calls"]},
            "ledger_sha256": rec["ledger_sha256"]},
        "lance_tables": {k: {"rows": v["rows"]}
                         for k, v in rec["tables"].items()},
        "ac_counts": {"total": J.DECLARED_TOTAL,
                      "per_task": J.DECLARED_PER_TASK},
        "tasks": [{"task": t, "status": "stopped_by_user"}
                  for t in ("T7", "T8", "T9")],
    }
    assert J.check_dashboard(good, rec) == []

    for mutate, needle in (
        (lambda t: t.update({"command": "pytest tests -q"}), "两排除项"),
        (lambda t: t.update({"command": 'pytest tests -q -k "not test_04"'}), "两排除项"),
        (lambda t: t.update({"deselected": 0}), "deselected=0"),
        (lambda t: t.update({"passed": 25}), "全套下限"),
    ):
        bad = json.loads(json.dumps(good))
        mutate(bad["tests"])
        assert any(needle in p for p in J.check_dashboard(bad, rec)), (
            needle, J.check_dashboard(bad, rec))


def test_judge_tables_pin_rows_and_vector_identity(tmp_path):
    rec = _synthetic_reconcile()
    base = tmp_path / "pre_vector_identity.json"
    base.write_text(json.dumps({"tables": {
        k: {"vector_set_sha256": "b" * 64}
        for k in ("kb_chunks", "solution_chunks",
                  "phase1_learnings", "phase2_learnings")}}), encoding="utf-8")
    assert J.check_tables(rec, base) == []

    bad = _synthetic_reconcile()
    bad["tables"]["solution_chunks"]["rows"] = 93
    bad["tables"]["phase2_learnings"]["dim"] = 1024
    bad["tables"]["phase1_learnings"]["vector_set_sha256"] = "c" * 64
    bad["tables"].pop("kb_chunks")
    problems = J.check_tables(bad, base)
    assert any("缺表 kb_chunks" in p for p in problems)
    assert any("solution_chunks 行数 93" in p for p in problems)
    assert any("phase2_learnings 维度 1024" in p for p in problems)
    assert any("phase1_learnings 向量集" in p for p in problems)
    # 缺窗前基线本身就是问题（不许跳过向量身份比对）
    assert J.check_tables(_synthetic_reconcile(), None)


def test_judge_ledger_recomputes_file_sha(tmp_path):
    led = _write_ledger(tmp_path / "l.jsonl", [_call(1), _call(2),
                                               _call(3)])
    rec = _synthetic_reconcile()
    rec["ledger_path"] = str(led)
    rec["ledger_sha256"] = J._sha256(led)
    assert J.check_ledger(rec) == []

    rec["ledger_sha256"] = "a" * 64          # 自报值与实件不符
    problems = J.check_ledger(rec)
    assert any("sha256 复算" in p for p in problems)

    rec["ledger_path"] = str(tmp_path / "gone.jsonl")
    assert any("账文件不存在" in p for p in J.check_ledger(rec))


def test_judge_stage_b_condition_type_guards(tmp_path, monkeypatch):
    """评审 LOW-2：非对象条件/明细、bool 冒充数值一律判负，不许 .get 崩或放行。"""
    monkeypatch.setattr(J, "_STAGE_B_VERDICT", tmp_path / "live_absent.json")
    rec = _synthetic_reconcile()
    pack = _minimal_pack(tmp_path)
    for mutate, needle in (
        (lambda v: v["conditions"].update({"5_engineering": True}),
         "不是对象"),
        (lambda v: v["conditions"].update({"1_thresholds": {"ok": True,
                                                           "all_pass": True}}),
         "缺 detail 对象"),
        (lambda v: v["conditions"]["1_thresholds"].update({"detail": []}),
         "缺 detail 对象"),
        (lambda v: v["conditions"]["1_thresholds"]["detail"].update(
            {"overall": {"hits": 45, "total": 47, "pass": True,
                         "gold_at_8": True}}), "overall 闸未过"),
        (lambda v: v["conditions"]["3_residual_recovery"].update(
            {"recovered": "abcdefg"}), "非 7 条列表"),
    ):
        v = _verdict_payload()
        mutate(v)
        _verdict_in(pack).write_text(json.dumps(v), encoding="utf-8")
        problems, _gaps = J.check_science(rec, pack)
        assert any(needle in p for p in problems), (needle, problems)


def test_judge_pack_flags_non_regular_files(tmp_path):
    """评审 LOW-3：FIFO/socket 既不在 is_file() 全覆盖里也不进密钥扫描。"""
    pack = _minimal_pack(tmp_path)
    fifo = pack / "pipe.fifo"
    os.mkfifo(fifo, 0o600)
    problems, _gaps = J.check_pack(pack)
    assert any("非普通文件" in p for p in problems)
    fifo.unlink()


def test_judge_commit_binding_dashboard_vs_manifest(tmp_path):
    """评审 MINOR-9：看板与证据包必须冻结在同一提交。"""
    pack = _minimal_pack(tmp_path)
    dash = {"git": {"commit": "abc"}}
    assert J.check_commit_binding(dash, pack) == []
    for bad, needle in (
        ({"git": {"commit": "def"}}, "≠ manifest.git_commit"),
        ({"git": {}}, "git.commit 缺失"),
        ({}, "git.commit 缺失"),
    ):
        assert any(needle in p for p in J.check_commit_binding(bad, pack)), bad
    # 缺 manifest 由 check_pack 记问题，此处不重复报
    assert J.check_commit_binding(dash, tmp_path / "no_pack") == []


def test_judge_verifier_recomputed_crosscheck(tmp_path):
    """评审 MINOR-7：verifier 签自报数与机判重算逐项对撞。"""
    pack = _minimal_pack(tmp_path)
    rec = _synthetic_reconcile()
    ac = {"total": 102, "per_task": J.DECLARED_PER_TASK,
          "ids_sha256": "5a6b46a5" + "0" * 56}
    sign = pack / "task10_closeout/signs/verifier.json"

    def _write(**override) -> None:
        body = {
            "schema": "simpraxis.phase25.sign.v1", "role": "verifier",
            "commit": "abc", "verdict": "APPROVE",
            "recomputed": {
                "ac_total": 102, "ac_per_task": J.DECLARED_PER_TASK,
                "ac_ids_sha256": "5a6b46a5" + "0" * 56,
                "ledger": {"n_calls": rec["ledger"]["n_calls"],
                           "ok": rec["ledger"]["ok"],
                           "fail": rec["ledger"]["fail"],
                           "ledger_sha256": rec["ledger_sha256"]},
                "tables": {k: {"rows": v["rows"]}
                           for k, v in rec["tables"].items()},
                "approvals": {"total_approved":
                              rec["approvals"]["total_approved"]},
            },
        }
        body.update(override)
        sign.write_text(json.dumps(body), encoding="utf-8")

    _write()
    assert J.check_verifier_recomputed(pack, ac, rec) == []

    for path, value, needle in (
        (("ac_total",), 101, "ac_total=101"),
        (("ac_per_task",), {"T0": 0}, "ac_per_task="),
        (("ac_ids_sha256",), "f" * 64, "ac_ids_sha256="),
        (("ledger", "ok"), 99, "ledger.ok=99"),
        (("ledger", "ledger_sha256"), "b" * 64, "ledger_sha256"),
        (("tables", "solution_chunks", "rows"), 93, "solution_chunks.rows=93"),
        (("approvals", "total_approved"), 1, "total_approved"),
    ):
        body = json.loads(sign.read_text())
        node = body["recomputed"]
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = value
        sign.write_text(json.dumps(body), encoding="utf-8")
        assert any(needle in p for p in
                   J.check_verifier_recomputed(pack, ac, rec)), (path, needle)

    # 缺 recomputed 块 = 三签不可与机判互证 → 问题
    _write(recomputed=None)
    assert any("缺 recomputed" in p
               for p in J.check_verifier_recomputed(pack, ac, rec))
    # 签件不在时不重复报（齐备性由 check_pack 判）
    sign.unlink()
    assert J.check_verifier_recomputed(pack, ac, rec) == []


def test_pack_clean_precheck_requires_real_directory(tmp_path):
    """评审 LOW-1：必需目录位被普通文件占据时，--clean 前即 fail-closed。"""
    tree = _synthetic_tree(tmp_path)
    _pfx, _root, src_dir, _logical, required, _ex = next(
        d for d in P.DIR_COPIES if d[4])
    victim = tree["p25"] / src_dir
    if victim.exists():
        import shutil
        shutil.rmtree(victim)
    victim.parent.mkdir(parents=True, exist_ok=True)
    victim.write_text("not a dir\n", encoding="utf-8")
    with pytest.raises(P.PackError, match="必需证据目录缺失"):
        _build(tree, clean=True)


def test_judge_complete_missing_sections_are_problems(tmp_path, monkeypatch):
    """评审 MINOR-10：Complete 缺关键段是问题（缺口不计入通过≠可以没有）。"""
    pack = _minimal_pack(tmp_path)
    doc = tmp_path / "complete.md"
    doc.write_text("有 Task 6 Stage B、RETAIN_R4、T10 三段\n", encoding="utf-8")
    monkeypatch.setattr(J, "COMPLETE", doc)
    assert J.check_pack(pack)[0] == []
    doc.write_text("只有 RETAIN_R4\n", encoding="utf-8")
    problems, _gaps = J.check_pack(pack)
    assert any("缺 Task 6 Stage B 段" in p for p in problems)
    assert any("缺 T10 段" in p for p in problems)
    monkeypatch.setattr(J, "COMPLETE", tmp_path / "no_doc.md")
    assert any("缺 Complete 文档" in p for p in J.check_pack(pack)[0])


def test_judge_report_without_artifacts_is_not_green(tmp_path):
    report = J.build_report(reconcile=tmp_path / "no_rec.json",
                            dashboard=tmp_path / "no_dash.json",
                            pack=tmp_path / "no_pack")
    assert report["ok"] is False
    assert report["schema"] == "simpraxis.phase25.closure_judge.v1"
    assert any("缺收尾对账件" in p for p in report["problems"])
