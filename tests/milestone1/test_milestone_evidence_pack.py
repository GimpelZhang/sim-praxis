#!/usr/bin/env python3
"""Task9 证据闭环测试（详细计划 L574-586，§4 追溯矩阵第 7 项）。

断言（全部 not paid、零联网、零付费）：
1. 证据包 manifest 内每个文件真实存在、size/sha256 逐字一致；
2. §1.8 必需逻辑编号 E0.1…E9.3 全部有文件（含 HITL 演练日志 E9.2）；
3. 每个 JSON/JSONL/DB schema 合法；telemetry 过 Rung1-3 结构校验且
   Gate 验收 run 重算 rules_pass=True（No Fake Physics）；
4. ledger：seq 严格递增、累计 ≤ 常态 36 且 ≤ 硬顶 48、batch_id 逐行对账
   批次凭据（索引 sha256 回指原件）、used≤approved；
5. FAILURE.json 冻结：包内/原件/ manifest 三处哈希一致、字段合法、
   任何 latest 软链都不指向 FAILURE 目录；
6. 包内无凭据原件，过密钥扫描（正则单一事实源
   orchestrator.common.secrets，H-12：10 条严格口径含 cli_/app_secret）；
7. §4 七项追溯矩阵逐条有证据编号且编号在 manifest 存在；
8. rsi_dashboard.json schema 合法、付费计数与 ledger 一致。

重建证据：$PY scripts/milestone1/build_evidence_pack.py --clean
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

from orchestrator.common.secrets import scan_text
from orchestrator.gateway.quota_ledger import call_lines
from orchestrator.judge.gait_rungs import evaluate_batch
from orchestrator.judge.rungs import evaluate_telemetry_file, validate_telemetry

_REPO = Path(__file__).resolve().parents[2]


def _load_bep():
    """以脚本路径加载 build_evidence_pack.py（scripts/ 非包）。"""
    name = "build_evidence_pack_under_test"
    spec = importlib.util.spec_from_file_location(
        name, str(_REPO / "scripts" / "milestone1" / "build_evidence_pack.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


bep = _load_bep()

PHASE1_ROOT = Path(os.environ.get(
    "SIMPRAXIS_PHASE1_ROOT", "/mnt/artifacts/simpraxis/phase1"))
RUNS_PHASE1 = Path(os.environ.get(
    "SIMPRAXIS_RUN_DIR", "/mnt/artifacts/simpraxis/runs")) / "phase1"
PACK = PHASE1_ROOT / "task9_milestone" / "evidence_pack"

CAP_NORMAL = 36
CAP_HARD = 48

REQUIRED_IDS = {
    "E0.1", "E0.2", "E0.3", "E0.4",                    # 签收/环境
    "E1.1", "E1.2", "E1.3", "E1.4", "E1.5",            # 飞书四探针+决策库
    "E2.1", "E2.2",                                    # 方向
    "E3.1", "E3.2", "E3.3",                            # 图/重启/检查点
    "E4.1", "E4.2",                                    # runner 补救
    "E5.1", "E5.2", "E5.3", "E5.4",                    # judge 四件套
    "E6.1", "E6.2",                                    # 飞轮隔离
    "E7.1", "E7.2", "E7.3", "E7.4",                    # track1 run/信封/日志/VLM
    "E8.0", "E8.1", "E8.2", "E8.3", "E8.4",            # track2
    "E9.1", "E9.2", "E9.3",                            # 看板/HITL/矩阵
    "E$.1", "E$.2", "E$.3",                            # 账套
}
LEDGER_KEYS = {"seq", "event", "stage", "model", "task", "batch_id",
               "global_used_after", "batch_approved", "batch_used_after",
               "approval_ref", "ok", "ts_iso"}


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@pytest.fixture(scope="module")
def manifest() -> dict:
    assert PACK.is_dir(), (
        f"证据包不存在：{PACK}；先跑 scripts/milestone1/build_evidence_pack.py")
    m = json.loads((PACK / "manifest.json").read_text(encoding="utf-8"))
    assert m["schema"] == "simpraxis.evidence_pack.v1"
    return m


def _by_id(manifest: dict) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for f in manifest["files"]:
        for lid in f["logical_ids"]:
            out.setdefault(lid, []).append(f["path"])
    return out


# ---------------------------------------------------------------------------
# 1. 逐文件存在 + 哈希
# ---------------------------------------------------------------------------
def test_01_every_manifest_file_exists_and_hash_matches(manifest):
    paths = [f["path"] for f in manifest["files"]]
    assert "manifest.json" not in paths  # 自描述件不入自身清单
    assert len(paths) == len(set(paths)), "manifest 出现重复路径"
    for f in manifest["files"]:
        p = PACK / f["path"]
        assert p.is_file(), f"清单文件缺失：{f['path']}"
        assert p.stat().st_size == f["size"], f"体积不符：{f['path']}"
        assert _sha(p) == f["sha256"], f"sha256 不符：{f['path']}"


def test_02_required_logical_ids_present(manifest):
    have = set(_by_id(manifest))
    missing = REQUIRED_IDS - have
    assert not missing, f"§1.8 必需证据编号缺失：{sorted(missing)}"


# ---------------------------------------------------------------------------
# 3. schema 合法
# ---------------------------------------------------------------------------
def test_03_json_artifacts_parse_and_schemas(manifest):
    for f in manifest["files"]:
        if f["path"].endswith(".json"):
            p = PACK / f["path"]
            json.loads(p.read_text(encoding="utf-8"))  # 合法 JSON，否则抛
    # ledger jsonl 逐行合法
    rows = [json.loads(l) for l in
            (PACK / "quota/ledger.jsonl").read_text(encoding="utf-8").splitlines()
            if l.strip()]
    assert rows and all(LEDGER_KEYS <= set(r) for r in rows)
    # counters 只允许两个键（长熔断口径，禁止偷偷记账到别处）
    counters = json.loads((PACK / "quota/counters.json").read_text())
    assert set(counters) == {"consecutive_quota_errors", "updated_at_iso"}
    # rubric / 标定件顶层结构
    rubric = json.loads((PACK / "task5_judge/rung4_rubric.json").read_text())
    assert isinstance(rubric.get("questions"), list) and rubric["questions"]
    cal = json.loads((PACK / "task5_judge/calibration_6q.json").read_text())
    assert {"mode", "results"} <= set(cal) or "mode" in cal
    # 两个 SQLite 可打开且自洽（H-13：backup 快照必须过 integrity_check）
    for rel in ("state/pending_gates.db", "state/langgraph_checkpoints.db"):
        con = sqlite3.connect(f"file:{PACK/rel}?mode=ro", uri=True)
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        con.close()
        rec = next(f for f in manifest["files"] if f["path"] == rel)
        assert rec.get("snapshot") == "sqlite3_backup_api", (
            f"{rel} 必须标记为 backup API 一致性快照（H-13）")
    # H-13：-wal/-shm 永不入包（含 DIR_COPIES 树内任何 SQLite 伴生件）
    sidecars = [str(p.relative_to(PACK)) for p in PACK.rglob("*")
                if p.is_file() and p.name.endswith(("-wal", "-shm"))]
    assert not sidecars, f"包内混入 SQLite 伴生件：{sidecars}"


def test_04_telemetry_schema_and_rules_pass(manifest):
    tel_files = [p for p in PACK.rglob("telemetry.json")
                 if "q2_carla" not in str(p)]
    assert len(tel_files) >= 4, "track1 run telemetry 不足 4 份"
    accepted = {
        "run_t7-mujoco-20261015_2": True,
        "run_t7-mujoco-20261015b_1": True,
        "run_t7-maniskill-20261015a_1": True,
    }
    for tel in tel_files:
        data = json.loads(tel.read_text(encoding="utf-8"))
        errs = validate_telemetry(data)
        assert not errs, f"{tel}: schema 错误 {errs}"
        if tel.parent.name in accepted:
            verdict = evaluate_telemetry_file(tel)
            assert verdict["rules_pass"] is True, (
                f"{tel.parent.name} Rung1-3 重算未通过")
    # Track2 步态 batch：九 run × 三臂规则重算（series 不信 outcomes）
    batch_dir = PACK / "runs/task8_track2/q1_genesis/run_t8-genesis-20261016a_1"
    assert batch_dir.is_dir()
    batch = evaluate_batch(batch_dir)
    assert batch["ok"] and batch["rules_pass"] and batch["seeds"] == 3
    assert set(batch["groups"]) == {"normal", "ice_nominal", "ice_dr"}


def test_05_run_family_required_artifacts(manifest):
    base_t8 = PACK / "runs/task8_track2/q1_genesis/run_t8-genesis-20261016a_1"
    for name in ("dr_controls_vs_experimental.csv", "summary.json", "SUMMARY.md",
                 "versions.txt"):
        assert (base_t8 / name).is_file(), f"T8 缺 {name}"
    splits = list(base_t8.glob("split_*_s0.mp4"))
    assert len(splits) == 2, f"两张分屏视频缺失：{splits}"
    q2 = PACK / "runs/task8_track2/q2_carla"
    for rel in ("analysis/SUMMARY.md", "analysis/q2_overlay.mp4",
                "analysis/invariance_checks.json", "q2_closeout.json",
                "pull_result.txt",
                "human_gate/decision.json", "human_gate/q2_gate.log"):
        assert (q2 / rel).is_file(), f"Q2 缺 {rel}"
    # 事后补的真人 Gate2 必须是真人 card_form approve、0 付费（D9 闭环）
    hg = json.loads((q2 / "human_gate/decision.json").read_text(encoding="utf-8"))
    assert hg["schema"] == "simpraxis.q2_human_gate.v1"
    assert hg["human_decision"]["decision"] == "approve"
    assert hg["human_decision"]["source"] == "card_form"
    assert hg["human_decision"]["choice"] is None
    assert hg["paid_llm_calls"]["delta"] == 0
    assert hg["metrics"]["metrics_kind"] == "q2_invariance_report"
    for run in ("run_t7-mujoco-20261015_2", "run_t7-mujoco-20261015b_1",
                "run_t7-maniskill-20261015a_1"):
        d = PACK / "runs/task7_track1" / run
        assert (d / "video.mp4").is_file() and (d / "manifest.json").is_file()


# ---------------------------------------------------------------------------
# 4. ledger 上限与对账
# ---------------------------------------------------------------------------
def _ledger_rows():
    return [json.loads(l) for l in
            (PACK / "quota/ledger.jsonl").read_text(encoding="utf-8")
            .splitlines() if l.strip()]


def test_06_ledger_caps_sequence_and_reconciliation(manifest):
    rows = _ledger_rows()
    n = len(rows)
    assert [r["seq"] for r in rows] == list(range(1, n + 1)), "seq 不连续"
    assert n <= CAP_HARD, f"硬顶 {CAP_HARD} 被突破：{n}"
    assert n <= CAP_NORMAL, (
        f"常态 {CAP_NORMAL} 被突破：{n}；36→48 每批需单独书面批准并扩展本测试")
    for r in rows:
        assert r["batch_used_after"] <= r["batch_approved"]
        assert r["global_used_after"] == r["seq"]
    # 包内账实一致：包内 ledger == 宿主原件（禁止打包后单边改动）
    assert _sha(PACK / "quota/ledger.jsonl") == _sha(
        PHASE1_ROOT / "quota/ledger.jsonl")
    index = json.loads(
        (PACK / "quota/approvals_index.json").read_text(encoding="utf-8"))
    batches = {r["batch_id"] for r in rows}
    assert batches <= set(index), f"批次无凭据：{batches - set(index)}"
    used: dict[str, int] = {}
    for r in rows:
        used[r["batch_id"]] = used.get(r["batch_id"], 0) + 1
        # approval_ref 逐行回指索引条目
        ref = r["approval_ref"].removeprefix("quota/approvals/").removesuffix(".json")
        assert ref in index and index[ref]["file"] == r["approval_ref"]
    for bid, n_used in used.items():
        assert n_used <= index[bid]["metadata"]["approved"]
        # 索引哈希必须与宿主原件一致（原件不打包，但可凭哈希对账）
        live = PHASE1_ROOT / index[bid]["file"]
        assert _sha(live) == index[bid]["sha256"], f"凭据原件被改动：{bid}"


# ---------------------------------------------------------------------------
# 5. FAILURE.json 不可变
# ---------------------------------------------------------------------------
def test_07_failure_json_immutable(manifest):
    frozen = {f["thread"]: f for f in manifest["failures"]}
    assert set(frozen) == {"t7-mujoco-20261015"}
    for thread, rec in frozen.items():
        packed = PACK / rec["pack_path"]
        doc = json.loads(packed.read_text(encoding="utf-8"))
        assert doc["status"] == "human_failure"
        assert doc["gate2_rejects"] == 2 and doc["gate1_rejects"] == 0
        assert doc["thread_id"] == thread and doc["reason"]
        # 三处哈希一致：manifest 冻结值 / 包内件 / 宿主原件
        assert rec["sha256"] == _sha(packed)
        assert _sha(packed) == _sha(
            PHASE1_ROOT / "runs" / thread / "FAILURE.json")
    # latest 软链永不指向 FAILURE 目录（包内清单 + 现场实物双重断言）
    syms = json.loads((PACK / "symlinks.json").read_text(encoding="utf-8"))
    assert not any(s["points_to_failure"] for s in syms)
    # H-11：清单必须标注相对/绝对与文件/目录；现场两个 latest 为相对目录链接
    for s in syms:
        assert isinstance(s["relative"], bool)
        assert s["kind"] in ("dir", "file")
    latest = [s for s in syms if Path(s["path"]).name == "latest"]
    assert latest and all(s["relative"] and s["kind"] == "dir" for s in latest)
    for base in (PHASE1_ROOT, RUNS_PHASE1):
        for link in base.rglob("*"):
            if link.is_symlink() and link.name == "latest":
                assert not (link.resolve() / "FAILURE.json").exists()


# ---------------------------------------------------------------------------
# 6. 无凭据 + secret scan
# ---------------------------------------------------------------------------
def test_08_no_credentials_and_secret_scan_clean(manifest):
    for p in PACK.rglob("*"):
        if p.is_file():
            assert "approvals" not in p.relative_to(PACK).parts \
                or p.name == "approvals_index.json", f"凭据原件混入：{p}"
    for p in PACK.rglob("*"):
        if not p.is_file():
            continue
        text = p.read_text(encoding="utf-8", errors="ignore")
        # H-12：测试不再自持正则副本，直接用单一事实源（10 条严格口径）。
        hits = scan_text(text)
        assert not hits, (
            f"secret 命中 {p.relative_to(PACK)}：{hits}")


# ---------------------------------------------------------------------------
# 7. 追溯矩阵
# ---------------------------------------------------------------------------
def test_09_traceability_matrix_seven_items(manifest):
    m = json.loads((PACK / "traceability_matrix.json").read_text(encoding="utf-8"))
    rows = {r["item"]: r for r in m["rows"]}
    for i in range(1, 8):
        assert i in rows and rows[i]["evidence_ids"] and rows[i]["tests"]
    ids = set(_by_id(manifest))
    for r in m["rows"]:
        assert set(r["evidence_ids"]) <= ids, (
            f"矩阵 {r['item']} 引用了不存在的证据编号")
    # 第 2/6 项的 Gate 证据：四线程 card_form 决策行
    gates = json.loads((PACK / "state/gates_export.json").read_text(encoding="utf-8"))
    decided = [(g["thread_id"], g["gate"]) for g in gates
               if g["status"] == "DECIDED" and g["source"] == "card_form"]
    for tid in ("t7-mujoco-20261015b", "t7-maniskill-20261015a",
                "t8-genesis-20261016a"):
        assert (tid, 1) in decided and (tid, 2) in decided
    # Q2（D9 条件闸门）无 Gate1，事后补的 Gate2 必须有真人 card_form 签收
    assert ("t8-q2carla-hitl-20261016", 2) in decided
    q2_gate = next(g for g in gates
                   if g["thread_id"] == "t8-q2carla-hitl-20261016"
                   and g["gate"] == 2)
    assert q2_gate["decision"] == "approve" and q2_gate["choice"] is None
    assert not any(g["thread_id"] == "t8-q2carla-hitl-20261016"
                   and g["gate"] == 1 for g in gates), "D9 路径不应存在 Gate1 行"


# ---------------------------------------------------------------------------
# 8. 看板
# ---------------------------------------------------------------------------
def test_10_rsi_dashboard_schema_and_counts(manifest):
    d = json.loads((PACK / "rsi_dashboard.json").read_text(encoding="utf-8"))
    assert d["schema"] == "simpraxis.rsi_dashboard.v1"
    assert d["positioning"]["rsi_level_axis"]["level"] == 2
    assert {r["thread_id"] for r in d["runs"]} == {
        "t7-mujoco-20261015", "t7-mujoco-20261015b",
        "t7-maniskill-20261015a", "t8-genesis-20261016a"}
    for r in d["runs"]:
        assert isinstance(r.get("proxy_decoupling_alarms"), list)
    # H-18：total 只认真实触网行（call_lines：seq 整数），preflight 拦截行
    # （seq=null）单列 blocked_preflight；今日无拦截行，两数与 ledger 行数相等。
    rows = _ledger_rows()
    calls = call_lines(rows)
    pc = d["paid_calls"]
    assert pc["total"] == len(calls)
    assert pc["blocked_preflight"] == len(rows) - len(calls) == 0
    assert pc["ledger_lines"] == len(rows)
    assert pc["hard_cap_ok"] is True
    assert pc["normal_cap_used"] is True
    # H-18：T8 结案块从 DONE 信封实读（不再硬编码 done/0）
    t8row = next(r for r in d["runs"]
                 if r["thread_id"] == "t8-genesis-20261016a")
    env = json.loads((PHASE1_ROOT / "runs" / "t8-genesis-20261016a"
                      / "DONE.json").read_text(encoding="utf-8"))
    assert t8row["closeout"] == env["status"] == "done"
    assert t8row["closeout_source"] == "DONE.json"
    assert t8row["n_agent_runs"] == env["n_agent_runs"]
    assert t8row["gate1_rejects"] == env["gate1_rejects"]
    assert t8row["gate2"]["rejects"] == env["gate2_rejects"]
    assert d["loop_distribution"]["per_thread"][
        "t8-genesis-20261016a"]["agent_runs"] == env["n_agent_runs"]
    # H-18：pending 演练块的配额键改名（已 passed 时该键不存在）
    assert "approved" not in d["hitl_drill"]
    assert d["learnings"]["rows_new"] == 4
    assert d["learnings"]["production_recall_hits"] == 0  # 诚实口径
    # L580 HITL 真人点验：代理重验复现 + card_form 真人签字
    h = d["hitl_drill"]
    assert h["status"] == "passed" and h["batch_id"] == "T9-B1"
    assert h["rung4_reproducible"] is True
    assert h["human_decision"]["decision"] == "approve"
    assert h["human_decision"]["source"] == "card_form"
    assert h["spotchecked_run"] == "t7-maniskill-20261015a"
    # 人类 Oracle 分歧告警必须在看板上（§6.2 proxy decoupling）
    failed = next(r for r in d["runs"]
                  if r["thread_id"] == "t7-mujoco-20261015")
    assert any(a["type"] == "human_oracle_vs_proxies"
               for a in failed["proxy_decoupling_alarms"])
    # Q2（D9 条件闸门）事后补的真人 Gate2：passed/card_form/0 付费
    q = d["q2_human_gate"]
    assert q["status"] == "passed"
    assert q["thread_id"] == "t8-q2carla-hitl-20261016"
    assert q["paid_llm_calls_delta"] == 0
    assert q["human_decision"]["decision"] == "approve"
    assert q["human_decision"]["source"] == "card_form"


def test_11_hitl_drill_log_present():
    log = PACK / "hitl_drill.log"
    assert log.is_file() and log.stat().st_size > 0, (
        "HITL 演练日志缺失：T9-B1 批次经用户书面批准后跑 hitl_drill")
    text = log.read_text(encoding="utf-8")
    assert "T9-B1" in text and "SIMPRAXIS_ALLOW_PAID=1" in text


# ---------------------------------------------------------------------------
# H-11/H-12/H-13：合成树单测（不依赖 /mnt 现场包，全部 tmp_path）
# ---------------------------------------------------------------------------

def test_h11_symlink_file_is_fatal(tmp_path, monkeypatch):
    """符号链接文件绝不复制（会把链接目标实体打进包）。"""
    monkeypatch.setattr(bep, "PACK", tmp_path / "pack")
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_text("real", encoding="utf-8")
    link = src / "b.txt"
    link.symlink_to(src / "a.txt")
    dst = tmp_path / "pack" / "b.txt"
    with pytest.raises(SystemExit, match="H-11"):
        bep._copy_file(link, dst, "E9.9", True, "host", [])
    assert not dst.exists()
    # 普通文件复制正常，manifest 落账
    manifest: list = []
    bep._copy_file(src / "a.txt", tmp_path / "pack" / "a.txt", "E9.9",
                   True, "host", manifest)
    assert manifest and manifest[0]["path"].endswith("a.txt")


def test_h11_walk_relative_dir_link_not_descended(tmp_path):
    """相对目录符号链接：白名单允许、实体不下降复制、不 FATAL。"""
    root = tmp_path / "tree"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "secret_target.txt").write_text("via link", encoding="utf-8")
    (root / "real").mkdir()
    (root / "real" / "f.txt").write_text("real", encoding="utf-8")
    (root / "link_out").symlink_to(Path("..") / "outside")  # 相对链接
    got = {p.relative_to(root).as_posix()
           for p in bep._iter_copy_files(root)}
    assert "real/f.txt" in got
    assert "link_out/secret_target.txt" not in got
    assert not any(p.as_posix().startswith("link_out") for p in
                   bep._iter_copy_files(root))


def test_h11_walk_absolute_dir_link_is_fatal(tmp_path):
    """绝对目标的符号链接目录命中 FATAL（白名单只放相对链接）。"""
    root = tmp_path / "tree"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "abs_link").symlink_to(outside)
    with pytest.raises(SystemExit, match="H-11"):
        list(bep._iter_copy_files(root))


def test_h12_scanner_uses_shared_patterns(tmp_path):
    """扫描器走单一事实源：cli_ app_id 与 app_secret 赋值都能命中。"""
    d = tmp_path / "p"
    d.mkdir()
    (d / "evil.txt").write_text(
        "APP_ID=cli_" + "a" * 20 + "\n"
        "FEISHU_APP_SECRET=\"" + "j" * 32 + "\"\n", encoding="utf-8")
    (d / "clean.txt").write_text("nothing here\n", encoding="utf-8")
    hits = bep._scan_secrets(d)
    joined = "\n".join(hits)
    assert "lark_cli_app_id" in joined
    assert "assigned_secret_kv" in joined
    assert "evil.txt" in joined
    # 干净目录零命中
    clean = tmp_path / "c"
    clean.mkdir()
    (clean / "ok.txt").write_text("normal log line\n", encoding="utf-8")
    assert bep._scan_secrets(clean) == []


def test_h13_sqlite_backup_snapshot_merges_live_wal(tmp_path, monkeypatch):
    """存活 WAL 库经 backup 快照：未 checkpoint 的写入必须进包且自洽。"""
    monkeypatch.setattr(bep, "PACK", tmp_path / "pack")
    live = tmp_path / "live.db"
    writer = sqlite3.connect(str(live))
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("CREATE TABLE t(x INTEGER)")
    writer.execute("INSERT INTO t VALUES (1)")
    writer.commit()  # 不 checkpoint：值只在 WAL 里
    assert live.with_name("live.db-wal").exists()

    dst = tmp_path / "pack" / "state" / "live.db"
    manifest: list = []
    try:
        bep._snapshot_sqlite(live, dst, "E1.5", True, "host", manifest)
    finally:
        writer.close()
    ro = sqlite3.connect(f"file:{dst}?mode=ro", uri=True)
    try:
        assert ro.execute("SELECT x FROM t").fetchall() == [(1,)]
        assert ro.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        ro.close()
    assert not dst.with_name("live.db-wal").exists()
    assert not dst.with_name("live.db-shm").exists()
    assert manifest[0]["snapshot"] == "sqlite3_backup_api"


def test_h13_snapshot_missing_required_fatal(tmp_path, monkeypatch):
    monkeypatch.setattr(bep, "PACK", tmp_path / "pack")
    with pytest.raises(FileNotFoundError):
        bep._snapshot_sqlite(tmp_path / "nope.db",
                             tmp_path / "pack" / "nope.db",
                             "E1.5", True, "host", [])
