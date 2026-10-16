#!/usr/bin/env python3
"""Task9：组装 Milestone 1 证据包（计划 L582-583，§1.8 目录，零付费）。

输出 $SIMPRAXIS_PHASE1_ROOT/task9_milestone/evidence_pack/：
- 按 §1.8 收齐 Task0–9 全部机器证据（宿主侧 + 容器 run 侧两路，见 §1.8
  挂载约定），大文件只在 /mnt 内复制，仓内不落任何证据；
- manifest.json：逐文件 sha256/size/来源/逻辑编号/类型，可重放命令，
  ledger 快照与对账，FAILURE.json 哈希冻结，排除清单（凭据原件不打包）；
- traceability_matrix.json：§4 七项 + 付费/环境纪律 → 证据编号；
- 衍生件（不新增事实，只做只读视图）：state/gates_export.json、
  task6_learnings/learnings_export.jsonl（去 vector）、
  quota/approvals_index.json（只取批次元数据，文件哈希可对账）、
  symlinks.json；
- 打包前对所有文本件跑密钥扫描，正则单一事实源
  orchestrator/common/secrets.py（H-12：10 条严格口径，含飞书 cli_ app_id
  与 app_secret 赋值形态；与 scan_secrets.sh 同源）。

与 §1.8 树不一致处一律写进 ADAPTATIONS.md（锁内适配留痕，规则 7）。
用法：
  python scripts/milestone1/build_evidence_pack.py [--clean]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))

# H-12：密钥正则只允许单一事实源（router 日志脱敏也用它），本脚本不再自持副本。
from orchestrator.common.secrets import scan_text  # noqa: E402

PHASE1_ROOT = Path(os.environ.get(
    "SIMPRAXIS_PHASE1_ROOT", "/mnt/artifacts/simpraxis/phase1"))
RUNS_PHASE1 = Path(os.environ.get(
    "SIMPRAXIS_RUN_DIR", "/mnt/artifacts/simpraxis/runs")) / "phase1"
PACK = PHASE1_ROOT / "task9_milestone" / "evidence_pack"

SCHEMA = "simpraxis.evidence_pack.v1"

# ---------------------------------------------------------------------------
# 复制清单：(pack 内相对路径, 源根, 源相对路径, 逻辑编号, 必需性)
# 源根 H=宿主 phase1，R=runs/phase1，C=repo
# ---------------------------------------------------------------------------
H, R, C = "host", "runs", "repo"

FILES: list[tuple[str, str, str, str, bool]] = [
    # Task0 环境
    ("task0_env/pip_freeze.txt", H, "task0_env/pip_freeze.txt", "E0.2", True),
    ("task0_env/admission.log", H, "task0_env/admission.log", "E0.3", True),
    ("task0_env/inputs/track_1_samples.txt", C, "inputs/tracks/track_1_samples.txt", "E0.4", True),
    ("task0_env/inputs/track_2_samples.txt", C, "inputs/tracks/track_2_samples.txt", "E0.4", True),
    # Task1 飞书
    ("task1_feishu/probe_b_p2p.json", H, "task1_feishu/probe_b_p2p.json", "E1.1", True),
    ("task1_feishu/probe_c_form.json", H, "task1_feishu/probe_c_form.json", "E1.2", True),
    ("task1_feishu/probe_d_media.json", H, "task1_feishu/probe_d_media.json", "E1.3", True),
    ("task1_feishu/probe_c_card_adjudication.md", H, "task1_feishu/probe_c_card_adjudication.md", "E1.2", True),
    ("task1_feishu/ws_run.log", H, "task1_feishu/ws_run.log", "E1.4", True),
    ("task1_feishu/probe_d/probe_d_synthetic.mp4", R, "task1_feishu/probe_d/probe_d_synthetic.mp4", "E1.3", True),
    ("task1_feishu/probe_d/probe_d_cover.png", R, "task1_feishu/probe_d/probe_d_cover.png", "E1.3", True),
    ("task1_feishu/probe_d/commands.txt", R, "task1_feishu/probe_d/commands.txt", "E1.3", True),
    # Task2 方向（Q1/Q2/T7/T8 各 run 的 macro 原件由 glob 补；这里钉最小必需）
    ("task2_direction/proposals_t8-genesis-20261016a_r0.json",
     H, "task2_direction/proposals_t8-genesis-20261016a_r0.json", "E2.1", True),
    ("task2_direction/recall_baseline_q1q2q3.json",
     H, "task2_direction/recall_baseline_q1q2q3.json", "E2.2", True),
    # Task3 图（graph_diagram.svg 适配 → topology.txt + 仓内 export_svg 函数）
    ("task3_graph/topology.txt", H, "task3_graph/topology.txt", "E3.1", True),
    ("task3_graph/resume_drill.log", H, "task3_graph/resume_drill.log", "E3.2", True),
    # Task4 runner
    ("task4_runner/unit_tests.log", H, "task4_runner/unit_tests.log", "E4.2", True),
    ("task4_runner/t8-genesis-20261016a/coding_round1.req.json",
     H, "task4_runner/t8-genesis-20261016a/coding_round1.req.json", "E4.1", True),
    ("task4_runner/t8-genesis-20261016a/coding_round1.resp.json",
     H, "task4_runner/t8-genesis-20261016a/coding_round1.resp.json", "E4.1", True),
    ("task4_runner/t8-genesis-20261016a/coding_round1.decision.json",
     H, "task4_runner/t8-genesis-20261016a/coding_round1.decision.json", "E4.1", True),
    # Task5 judge
    ("task5_judge/calibration_6q.json", H, "task5_judge/calibration_6q.json", "E5.1", True),
    ("task5_judge/telemetry_schema.json", H, "task5_judge/telemetry_schema.json", "E5.2", True),
    ("task5_judge/rung4_rubric.json", H, "task5_judge/rung4_rubric.json", "E5.3", True),
    ("task5_judge/threshold_provenance.md", H, "task5_judge/threshold_provenance.md", "E5.2", False),
    # Task6 学习隔离（rows.jsonl/kb_invariants.json 适配 → 衍生导出）
    ("task6_learnings/isolation_evidence.json",
     H, "task6_learnings/isolation_evidence.json", "E6.2", True),
    ("task6_learnings/unit_tests.log", H, "task6_learnings/unit_tests.log", "E6.2", True),
    # Task8 Track2 judge（步态 Rung4 标定）
    ("task8_track2_judge/calibration_gait.json",
     H, "task8_track2_judge/calibration_gait.json", "E5.4", True),
    ("task8_track2_judge/gait_cal_good.incident.json",
     H, "task8_track2_judge/gait_cal_good.incident.json", "E5.4", False),
    # 决策签收
    ("decision_signoff.json", H, "decision_signoff.json", "E0.1", True),
    # state：两个存活 SQLite 不走 FILES 的 copy2，由 SQLITE_DBS 经 backup
    # API 取一致性快照（H-13，见 _snapshot_sqlite）。
    # quota（approvals/ 原件排除，只入索引）
    ("quota/ledger.jsonl", H, "quota/ledger.jsonl", "E$.1", True),
    ("quota/counters.json", H, "quota/counters.json", "E$.1", True),
]

# 目录整体复制（glob 展开）：(pack 前缀, 源根, 源目录 glob, 逻辑编号)
DIR_COPIES = [
    ("task2_direction", H, "task2_direction", "E2.1"),
    ("task5_judge", H, "task5_judge", "E5.1"),
    ("task7_track1/evidence", H, "task7_track1/evidence", "E7.4"),
    ("task8_track2/evidence", H, "task8_track2/evidence", "E8.4"),
    ("task8_track2/install_compat", H, "task8_track2/install_compat", "E8.0"),
    ("task8_track2_judge", H, "task8_track2_judge", "E5.4"),
    ("task0_env", H, "task0_env", "E0.2"),
    # run 族
    ("runs/task7_track1/run_t7-mujoco-20261015_1",
     R, "task7_track1/run_t7-mujoco-20261015_1", "E7.1"),
    ("runs/task7_track1/run_t7-mujoco-20261015_2",
     R, "task7_track1/run_t7-mujoco-20261015_2", "E7.1"),
    ("runs/task7_track1/run_t7-mujoco-20261015b_1",
     R, "task7_track1/run_t7-mujoco-20261015b_1", "E7.1"),
    ("runs/task7_track1/run_t7-maniskill-20261015a_1",
     R, "task7_track1/run_t7-maniskill-20261015a_1", "E7.1"),
    ("runs/task5_calibration/20261014-175423",
     R, "task5_calibration/20261014-175423", "E5.1"),
    ("runs/task5_calibration/20261014-183451",
     R, "task5_calibration/20261014-183451", "E5.1"),
    ("runs/task8_gait_calibration/20261015-202909",
     R, "task8_gait_calibration/20261015-202909", "E5.4"),
    ("runs/task8_track2/q1_genesis/run_t8-genesis-20261016a_1",
     R, "task8_track2/q1_genesis/run_t8-genesis-20261016a_1", "E8.1"),
    ("runs/task8_track2/q2_carla", R, "task8_track2/q2_carla", "E8.2"),
]

# 单文件 run 侧补充（图日志等）
RUN_SINGLES = [
    ("runs/task7_track1/graph_t7_mujoco_20261015.log",
     "task7_track1/graph_t7_mujoco_20261015.log", "E7.3"),
    ("runs/task7_track1/graph_t7_mujoco_b.log",
     "task7_track1/graph_t7_mujoco_b.log", "E7.3"),
    ("runs/task7_track1/graph_t7_maniskill_a.log",
     "task7_track1/graph_t7_maniskill_a.log", "E7.3"),
    ("runs/task7_track1/resume_t7-mujoco-b_111403.log",
     "task7_track1/resume_t7-mujoco-b_111403.log", "E7.3"),
    ("runs/task8_track2/graph_t8_q1_a.log",
     "task8_track2/graph_t8_q1_a.log", "E8.3"),
    ("runs/task8_track2/q2_df_before_pull_20261016-094550.txt",
     "task8_track2/q2_df_before_pull_20261016-094550.txt", "E8.2"),
    ("runs/task8_track2/logs", "task8_track2/logs", "E8.3"),
    ("runs/task8_track2/probe", "task8_track2/probe", "E8.0"),
]

# 存活 SQLite：禁止 copy2（有写入者时主库可能落后 WAL）。走 sqlite3 online
# backup API 导出事务一致快照（WAL 在导出时合并），入包后再跑 integrity_check。
SQLITE_DBS = [
    ("state/pending_gates.db", H, "state/pending_gates.db", "E1.5", True),
    ("state/langgraph_checkpoints.db", H, "state/langgraph_checkpoints.db",
     "E3.3", True),
]

SKIP_NAMES = {"__pycache__", ".ledger.lock"}

# SQLite 伴生文件：只允许主库快照入包，-wal/-shm 永不打包（H-13）。
_WAL_SUFFIXES = ("-wal", "-shm")


def _git_head() -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=_REPO,
                          capture_output=True, text=True).stdout.strip()


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _origin(root: str, rel: str) -> Path:
    return {H: PHASE1_ROOT, R: RUNS_PHASE1, C: _REPO}[root] / rel


def _scan_secrets(root: Path) -> list[str]:
    # H-12：正则单一事实源在 orchestrator.common.secrets（严格口径，
    # 含飞书 cli_ app_id 与 app_secret 赋值形态）。
    hits = []
    for f in root.rglob("*"):
        if not f.is_file():
            continue
        try:
            data = f.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for name, matched in scan_text(data):
            hits.append(f"{f.relative_to(root)} :: {name} :: {matched[:40]}")
    return hits


def _gates_export() -> list[dict]:
    con = sqlite3.connect(f"file:{PHASE1_ROOT/'state'/'pending_gates.db'}?mode=ro",
                          uri=True)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(
        "SELECT thread_id,gate,status,choice,decision,human_notes,source,"
        "card_message_id,media_message_id,created_at,decided_at "
        "FROM pending_gates ORDER BY created_at")]
    con.close()
    return rows


def _approvals_index() -> dict:
    out = {}
    adir = PHASE1_ROOT / "quota" / "approvals"
    for f in sorted(adir.glob("*.json")):
        meta = json.loads(f.read_text(encoding="utf-8"))
        out[f.stem] = {"file": f"quota/approvals/{f.name}",
                       "sha256": _sha256(f), "metadata": meta}
    return out


def _learnings_export() -> list[dict]:
    from memory_flywheel.phase1.learning_store import LearningStore
    tbl = LearningStore().table()
    df = tbl.head(1000).to_pandas()
    rows = []
    for rec in df.drop(columns=["vector"]).to_dict(orient="records"):
        rows.append(json.loads(json.dumps(rec, ensure_ascii=False, default=str)))
    return rows


def _symlinks_index() -> list[dict]:
    # H-11：全量清点（不论是否在复制树内），标注相对/绝对与文件/目录；
    # 复制树内的绝对链接/链接文件由 _iter_copy_files/_copy_file 直接 FATAL。
    out = []
    for base in (PHASE1_ROOT, RUNS_PHASE1):
        for link in base.rglob("*"):
            if link.is_symlink():
                tgt = os.readlink(link)
                resolved = (link.parent / tgt).resolve()
                out.append({
                    "path": str(link.relative_to(base)),
                    "root": "host" if base == PHASE1_ROOT else "runs",
                    "target": tgt,
                    "relative": not os.path.isabs(tgt),
                    "kind": "dir" if link.is_dir() else "file",
                    "broken": not resolved.exists(),
                    "points_to_failure": (resolved.name.startswith("FAILURE")
                                          or (resolved / "FAILURE.json").exists()),
                })
    return out


def _add_manifest(manifest: list, dst: Path, src: Path, origin: str,
                  logical: str, required: bool) -> dict:
    """登记 manifest；同包路径（FILES 与 DIR_COPIES 重叠时）合并逻辑编号。"""
    rel = str(dst.relative_to(PACK))
    rec = next((r for r in manifest if r["path"] == rel), None)
    if rec is None:
        rec = {
            "path": rel, "origin": origin, "origin_rel": str(src),
            "size": dst.stat().st_size, "sha256": _sha256(dst),
            "logical_ids": [logical], "required": required}
        manifest.append(rec)
    elif logical not in rec["logical_ids"]:
        rec["logical_ids"].append(logical)
        rec["required"] = rec["required"] or required
    return rec


def _iter_copy_files(src_base: Path):
    """展开目录复制清单（H-11 安全 walk）。

    - os.walk(followlinks=False)：符号链接目录永不下降（含相对白名单链接，
      其目标实体不打包，链接本身登记进 symlinks.json）；
    - 符号链接目录若为绝对目标 → FATAL（白名单只允许相对链接）；
    - 符号链接**文件**一律 FATAL（is_file() 会跟随链接把 ~/access 等
      链接目标实体打进包，破坏清单自证）。
    """
    for dirpath, dirnames, filenames in os.walk(src_base, followlinks=False):
        dpath = Path(dirpath)
        kept: list[str] = []
        for d in dirnames:
            p = dpath / d
            if d in SKIP_NAMES:
                continue
            if p.is_symlink():
                tgt = os.readlink(p)
                if os.path.isabs(tgt):
                    raise SystemExit(
                        f"证据树含绝对符号链接目录，拒绝打包（H-11）：{p} -> {tgt}")
                # 相对目录链接：白名单允许但不下降（followlinks=False 已保证，
                # 从 dirnames 摘除为双保险），由 symlinks.json 登记。
                continue
            kept.append(d)
        dirnames[:] = kept
        for name in filenames:
            if name in SKIP_NAMES:
                continue
            p = dpath / name
            if p.is_symlink():
                raise SystemExit(
                    f"证据源为符号链接文件，拒绝打包（H-11）：{p} -> "
                    f"{os.readlink(p)}")
            yield p


def _copy_file(src: Path, dst: Path, logical: str, required: bool,
               origin: str, manifest: list) -> None:
    if not src.exists():
        if required:
            raise FileNotFoundError(f"必需证据缺失：{src}")
        return
    # H-11：单文件复制同样拒绝符号链接（FILES/RUN_SINGLES/信封/hitl_evidence
    # 全部经本函数，无旁路）。
    if src.is_symlink():
        raise SystemExit(
            f"证据源为符号链接文件，拒绝打包（H-11）：{src} -> "
            f"{os.readlink(src)}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    _add_manifest(manifest, dst, src, origin, logical, required)


def _snapshot_sqlite(src: Path, dst: Path, logical: str, required: bool,
                     origin: str, manifest: list) -> None:
    """H-13：存活 SQLite 经 online backup API 取一致性快照入包。

    copy2 存活库在有写入者时主库可能落后未 checkpoint 的 WAL；backup API
    导出时合并 WAL，得到事务一致快照。导出后对包内副本跑 integrity_check，
    且目标侧不得残留 -wal/-shm。
    """
    if not src.exists():
        if required:
            raise FileNotFoundError(f"必需证据缺失：{src}")
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():  # 非 --clean 重建：backup 不能覆盖已含表的库
        dst.unlink()
    for suf in _WAL_SUFFIXES:
        side = dst.with_name(dst.name + suf)
        if side.exists():
            side.unlink()
    src_con = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        out_con = sqlite3.connect(str(dst))
        try:
            src_con.backup(out_con)
            # backup 会把源库文件头的 journal_mode=WAL 带到目标；关闭前改回
            # DELETE，使快照关闭即清掉 -wal/-shm（证据包只收主库文件）。
            out_con.execute("PRAGMA journal_mode=DELETE")
        finally:
            out_con.close()
    finally:
        src_con.close()
    chk_con = sqlite3.connect(f"file:{dst}?mode=ro", uri=True)
    try:
        verdict = chk_con.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        chk_con.close()
    if verdict != "ok":
        raise SystemExit(f"SQLite 快照完整性失败：{src} -> {verdict}")
    sidecars = [dst.with_name(dst.name + suf) for suf in _WAL_SUFFIXES]
    if any(p.exists() for p in sidecars):
        raise SystemExit(f"SQLite 快照残留 {[p.name for p in sidecars if p.exists()]}")
    rec = _add_manifest(manifest, dst, src, origin, logical, required)
    rec["snapshot"] = "sqlite3_backup_api"


def _ledger_snapshot() -> dict:
    rows = [json.loads(l) for l in
            (PHASE1_ROOT / "quota" / "ledger.jsonl").read_text(encoding="utf-8")
            .splitlines() if l.strip()]
    by_batch: dict[str, dict] = {}
    for r in rows:
        b = by_batch.setdefault(r["batch_id"], {"used": 0, "ok": 0, "failed": 0,
                                               "approval_ref": r["approval_ref"],
                                               "approved": r["batch_approved"]})
        b["used"] += 1
        b["ok" if r["ok"] else "failed"] += 1
    return {"n_total": len(rows), "n_ok": sum(1 for r in rows if r["ok"]),
            "n_failed": sum(1 for r in rows if not r["ok"]),
            "seq_min": rows[0]["seq"], "seq_max": rows[-1]["seq"],
            "cap_normal": 36, "cap_hard": 48,
            "normal_ok": len(rows) <= 36, "hard_ok": len(rows) <= 48,
            "by_batch": by_batch}


ADAPTATIONS = """# §1.8 证据树锁内适配（计划差异总表 #24：证据闭环替代假数据测试）

> 仓内偏差规范登记位置：`docs/ADAPTATIONS.md`（日期/条目/计划原文/实际/理由，
> 评审 H-11/H-12/H-13/H-14 已登记）；本表只列 §1.8 证据树的落点映射。

| §1.8 名义件 | 实际落点 | 原因 |
|---|---|---|
| `task3_graph/graph_diagram.svg` | `task3_graph/topology.txt` + 仓内 `orchestrator/graph/topology.py::export_svg` | 生产拓扑文本导出已固化；SVG 渲染为可选视图，未生成不影响拓扑单测（test_graph 断言节点/边） |
| run 族 `gates.json` | `state/gates_export.json`（pending_gates.db 只读导出）+ `runs_envelopes/<thread>/{DONE,FAILURE}.json` | 实现上 Gate 决策落 SQLite（主表+history），run 信封每线程一个 DONE/FAILURE；导出 JSON 即 gates.json 等价视图 |
| `task6_learnings/rows.jsonl` | `task6_learnings/learnings_export.jsonl` | 行数据在 LanceDB `phase1_learnings` 表（12 列含 vector）；导出去嵌入向量，行内容不增不减 |
| `task6_learnings/kb_invariants.json` | `task6_learnings/isolation_evidence.json` | Task6 取证时实际文件名；内容覆盖 1542/104/Q1Q2Q3 四件断言 |
| `task1_feishu/gate1_card.json`、`gate2_card.json` | `state/gates_export.json` 的 card_message_id + 仓内 `orchestrator/feishu_gateway/cards.py`（本 commit）+ probe_c_card_adjudication.md | 卡片模板在源码版本化，实发卡由 message_id 与回调行留痕 |
| `task0_env/inputs/` | `task0_env/inputs/track_{1,2}_samples.txt`（自仓内 `inputs/tracks/` 复制） | 面试题原件仓内版本化，证据包内固定一份对应 commit |
| `quota/approvals/` | `quota/approvals_index.json`（元数据+sha256，无凭据） | 凭据/批准原件不打包（计划 L582）；ledger 每行 approval_ref 可凭索引哈希回原文件对账 |
| 失败线程第二轮 Gate2 意见 | 无落库件（见 rsi_dashboard reject_topic_clusters.evidence_gap） | 重发卡未走 archive_decided，history 表空；不猜测按钮/文字，事件序列与 FAILURE.json 为证 |
| `state/pending_gates.db`、`state/langgraph_checkpoints.db` 存活库原件复制（H-13） | sqlite3 online backup API 一致性快照（导出时合并 WAL），manifest 标 `snapshot=sqlite3_backup_api`，包内副本过 `PRAGMA integrity_check`；`-wal/-shm` 永不入包 | copy2 存活库在有写入者时主库可能落后 WAL，快照不事务一致；备份 API 取在线一致视图（2026-10-16 评审 H-13） |
| 目录树 rglob 复制（H-11） | os.walk(followlinks=False) 安全展开：符号链接**文件**/绝对目标符号链接目录命中即 FATAL；相对符号链接目录不下降、仅登记 symlinks.json（新增 relative/kind 字段） | is_file() 跟随链接文件，会把指向 ~/access 或超大件的目标实体打进包，破坏清单自证；现场两个 `latest` 为相对目录链接，白名单保留（2026-10-16 评审 H-11） |
| Gate2 视频体积分档（H-14，`orchestrator/feishu_gateway/video.py`） | MB=1_000_000 十进制（飞书 files 接口口径）；≤28MB step1 直传，>28MB 一律从 step2 起，按压缩后实测体积逐级 2→3→4→5；旧 64/128/256MB 粗分档常量仅留作文档锚点不再选级 | 旧 1024² 口径下 HARD_LIMIT=30MiB 越过接口十进制硬顶 30,000,000（fail-safe 但白丢一次上传），粗分档会越级直跳截断档，与 H-2 接触时刻未接线叠加放大误裁剪（2026-10-16 评审 H-14） |
"""

REPLAY = [
    "source scripts/00_env.sh",
    "$PY -m pytest tests -q -k 'not test_04' -m 'not paid'",
    "bash scripts/verification/gate_static.sh",
    "$PY scripts/milestone1/build_dashboard.py",
    "$PY scripts/milestone1/build_evidence_pack.py --clean",
    "$PY -m memory_flywheel.scripts.check_chunk_integrity --expect 104",
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--clean", action="store_true", help="先清空包目录重建")
    args = ap.parse_args()

    if args.clean and PACK.exists():
        shutil.rmtree(PACK)
    PACK.mkdir(parents=True, exist_ok=True)

    manifest_files: list[dict] = []

    def cp(pack_rel: str, root: str, src_rel: str, logical: str,
           required: bool = True) -> None:
        _copy_file(_origin(root, src_rel), PACK / pack_rel, logical,
                   required, root, manifest_files)

    for spec in FILES:
        cp(*spec)

    # H-13：存活 SQLite 经 backup API 快照（不走 copy2）。
    for pack_rel, root, src_rel, logical, required in SQLITE_DBS:
        _snapshot_sqlite(_origin(root, src_rel), PACK / pack_rel, logical,
                         required, root, manifest_files)

    # 目录复制（含 q2 npz 等二进制；H-11 安全 walk：跳过 __pycache/锁、
    # 不下降任何符号链接目录、链接文件 FATAL）
    for pack_prefix, root, src_dir, logical in DIR_COPIES:
        src_base = _origin(root, src_dir)
        if not src_base.exists():
            raise FileNotFoundError(f"必需证据目录缺失：{src_base}")
        for f in sorted(_iter_copy_files(src_base)):
            rel = f.relative_to(src_base)
            _copy_file(f, PACK / pack_prefix / rel, logical, True, root,
                       manifest_files)

    for pack_rel, src_rel, logical in RUN_SINGLES:
        src = RUNS_PHASE1 / src_rel
        if src.is_symlink():
            # 清单根本身是链接：绝对目标 FATAL；相对目录链接不复制实体（H-11，
            # symlinks.json 全量登记）。
            tgt = os.readlink(src)
            if os.path.isabs(tgt):
                raise SystemExit(
                    f"证据根为绝对符号链接，拒绝打包（H-11）：{src} -> {tgt}")
            continue
        if src.is_dir():
            for f in sorted(_iter_copy_files(src)):
                _copy_file(f, PACK / pack_rel / f.relative_to(src),
                           logical, True, R, manifest_files)
        else:
            _copy_file(src, PACK / pack_rel, logical, True, R, manifest_files)

    # run 结案信封（DONE/FAILURE）
    env_dir_src = PHASE1_ROOT / "runs"
    for env_thread in sorted(p for p in env_dir_src.iterdir() if p.is_dir()):
        for name in ("DONE.json", "FAILURE.json"):
            f = env_thread / name
            if f.exists():
                _copy_file(f, PACK / "runs_envelopes" / env_thread.name / name,
                           "E7.2", True, H, manifest_files)

    # 衍生只读视图
    derived = PACK / "state" / "gates_export.json"
    derived.parent.mkdir(parents=True, exist_ok=True)
    gates = _gates_export()
    derived.write_text(json.dumps(gates, ensure_ascii=False, indent=2),
                       encoding="utf-8")
    manifest_files.append({"path": str(derived.relative_to(PACK)),
                           "origin": "derived", "origin_rel": "state/pending_gates.db",
                           "size": derived.stat().st_size,
                           "sha256": _sha256(derived),
                           "logical_ids": ["E1.5"], "required": True})

    (PACK / "task6_learnings").mkdir(exist_ok=True)
    learn = _learnings_export()
    lp = PACK / "task6_learnings" / "learnings_export.jsonl"
    lp.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in learn)
                  + "\n", encoding="utf-8")
    # Task0b：origin_rel 跟随 SIMPRAXIS_LANCEDB_URI 实体位置（迁移后为
    # data/lancedb/phase1_learnings.lance），不再写死 simpraxis 数据根。
    _lance_uri = Path(os.environ.get(
        "SIMPRAXIS_LANCEDB_URI",
        str(Path(os.environ.get(
            "KB_ROOT", "/home/vipuser/embodied-ai-sim-knowledge-base"))
            / "data" / "lancedb")))
    _lance_origin_rel = str(Path(
        _lance_uri.parent.name, _lance_uri.name, "phase1_learnings.lance"))
    manifest_files.append({"path": str(lp.relative_to(PACK)),
                           "origin": "derived",
                           "origin_rel": _lance_origin_rel,
                           "size": lp.stat().st_size, "sha256": _sha256(lp),
                           "logical_ids": ["E6.1"], "required": True})

    aidx = _approvals_index()
    ap_path = PACK / "quota" / "approvals_index.json"
    ap_path.write_text(json.dumps(aidx, ensure_ascii=False, indent=2),
                       encoding="utf-8")
    manifest_files.append({"path": str(ap_path.relative_to(PACK)),
                           "origin": "derived",
                           "origin_rel": "quota/approvals/*.json",
                           "size": ap_path.stat().st_size,
                           "sha256": _sha256(ap_path),
                           "logical_ids": ["E$.2"], "required": True})

    syms = _symlinks_index()
    sp = PACK / "symlinks.json"
    sp.write_text(json.dumps(syms, ensure_ascii=False, indent=2),
                  encoding="utf-8")
    manifest_files.append({"path": "symlinks.json", "origin": "derived",
                           "origin_rel": "phase1+runs symlink walk",
                           "size": sp.stat().st_size, "sha256": _sha256(sp),
                           "logical_ids": ["E$.3"], "required": True})

    (PACK / "ADAPTATIONS.md").write_text(ADAPTATIONS, encoding="utf-8")

    # §4 追溯矩阵：7 项 + 付费/环境纪律 → 逻辑证据编号（编号须在 manifest 中）
    matrix = [
        {"item": 1, "name": "双轨状态机可运行",
         "evidence_ids": ["E3.1", "E3.2", "E3.3"],
         "tests": "tests/milestone1/test_graph.py（含 kill-9 重启）"},
        {"item": 2, "name": "Gate1/Gate2 真实阻断（No Fake Approval）",
         "evidence_ids": ["E1.1", "E1.2", "E1.3", "E1.4", "E1.5", "E7.2", "E8.2"],
         "tests": "tests/milestone1/test_feishu_gateway.py + 本包 state/gates_export.json"
                  "；Q2（D9 条件闸门无 Gate1）由 scripts/milestone1/q2_human_gate.py "
                  "事后补真人 Gate2（thread=t8-q2carla-hitl-20261016，0 付费，"
                  "decision.json 在 E8.2 human_gate/）"},
        {"item": 3, "name": "物理真值（No Fake Physics）",
         "evidence_ids": ["E5.2", "E7.1", "E8.1"],
         "tests": "tests/milestone1/test_judge.py（五夹具）"},
        {"item": 4, "name": "Rung1–4 分级",
         "evidence_ids": ["E5.1", "E5.3", "E5.4", "E7.4", "E8.4"],
         "tests": "test_judge.py / test_task8_seam.py（五夹具 + 步态 record_only）"},
        {"item": 5, "name": "知识飞轮自增长且不毁基线",
         "evidence_ids": ["E6.1", "E6.2"],
         "tests": "test_learning_isolation.py（1542/104/Q1Q2Q3 不变）"},
        {"item": 6, "name": "两条 track 实证视频与对照",
         "evidence_ids": ["E7.1", "E7.2", "E7.3", "E7.4", "E8.1", "E8.2", "E8.4"],
         "tests": "Gate2 真人签字（gates_export: decision=approve, source=card_form）"},
        {"item": 7, "name": "交付物树",
         "evidence_ids": ["E0.1", "E9.1", "E9.2"],
         "tests": "test_milestone_evidence_pack.py（本包 manifest 全量校验）+ "
                  "test_hitl_drill.py（paid：真人 card_form 点验 Gate2 重放，"
                  "SIMPRAXIS_ALLOW_PAID=1，批次 T9-B1）"},
        {"item": "paid", "name": "付费纪律",
         "evidence_ids": ["E$.1", "E$.2"],
         "tests": "ledger ≤ 36 常态 / ≤48 硬顶；batch_id 逐行对账；单测零联网"},
        {"item": "env", "name": "环境纪律",
         "evidence_ids": ["E0.2", "E0.3", "E8.0", "E8.2"],
         "tests": "admission/pip_freeze/df+镜像拉取记录/容器与 GPU 收尾证据"},
    ]
    (PACK / "traceability_matrix.json").write_text(
        json.dumps({"schema": "simpraxis.traceability.v1",
                    "plan_ref": "Phase_1_Plan_detailed.md §4",
                    "rows": matrix}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    manifest_files.append({
        "path": "traceability_matrix.json", "origin": "derived",
        "origin_rel": "Phase_1_Plan_detailed.md §4",
        "size": (PACK / "traceability_matrix.json").stat().st_size,
        "sha256": _sha256(PACK / "traceability_matrix.json"),
        "logical_ids": ["E9.3"], "required": True})
    manifest_files.append({
        "path": "ADAPTATIONS.md", "origin": "derived",
        "origin_rel": "§1.8 差异映射",
        "size": (PACK / "ADAPTATIONS.md").stat().st_size,
        "sha256": _sha256(PACK / "ADAPTATIONS.md"),
        "logical_ids": ["E9.3"], "required": True})

    # 看板复制进包
    dash_src = PHASE1_ROOT / "task9_milestone" / "rsi_dashboard.json"
    if dash_src.exists():
        _copy_file(dash_src, PACK / "rsi_dashboard.json", "E9.1", True, H,
                   manifest_files)
    drill = PHASE1_ROOT / "task9_milestone" / "hitl_drill.log"
    if drill.exists():
        _copy_file(drill, PACK / "hitl_drill.log", "E9.2", True, H,
                   manifest_files)
    # HITL 演练的脱敏 req/resp 与结构化结果（与日志同编号 E9.2；演练前目录可缺）
    he = PHASE1_ROOT / "task9_milestone" / "hitl_evidence"
    if he.is_dir() and not he.is_symlink():
        for f in sorted(_iter_copy_files(he)):
            _copy_file(f, PACK / "hitl_evidence" / f.relative_to(he),
                       "E9.2", True, H, manifest_files)

    # 凭据排除自检：包内不得出现 approvals 原件或任何凭据文件名
    forbidden = [str(p.relative_to(PACK)) for p in PACK.rglob("*")
                 if p.is_file() and ("approvals" in p.parts
                                     and p.name != "approvals_index.json")]
    if forbidden:
        raise SystemExit(f"证据包混入凭据原件：{forbidden}")

    # H-13：-wal/-shm 永不入包（存活库只经 backup 快照；DIR_COPIES 内若有
    # SQLite 伴生件也在此兜底拦截）。
    wal_packed = [str(p.relative_to(PACK)) for p in PACK.rglob("*")
                  if p.is_file() and p.name.endswith(_WAL_SUFFIXES)]
    if wal_packed:
        raise SystemExit(f"证据包混入 SQLite -wal/-shm 伴生件：{wal_packed}")

    hits = _scan_secrets(PACK)
    if hits:
        raise SystemExit("证据包 secret scan 命中：\n" + "\n".join(hits))

    ledger_snap = _ledger_snapshot()
    failures = []
    for f in (PACK / "runs_envelopes").rglob("FAILURE.json"):
        failures.append({"thread": f.parent.name,
                         "pack_path": str(f.relative_to(PACK)),
                         "sha256": _sha256(f), "immutable": True})

    manifest = {
        "schema": SCHEMA,
        "built_at_iso": datetime.now(timezone.utc).astimezone().isoformat(),
        "git_commit": _git_head(),
        "plan": "Phase_1_Plan_detailed.md Task9（L574-586）+ §1.8 + §4",
        "roots": {"host": str(PHASE1_ROOT), "runs": str(RUNS_PHASE1),
                  "repo": str(_REPO)},
        "replay_commands": REPLAY,
        "ledger": ledger_snap,
        "failures": failures,
        "exclusions": [
            {"path": "quota/approvals/*.json",
             "reason": "批准凭据原件不打包（L582）；索引见 approvals_index.json"},
            {"path": "task1_feishu/proc/", "reason": "运行时 pid/fifo 临时件"},
        ],
        "files": sorted(manifest_files, key=lambda x: x["path"]),
    }
    (PACK / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    n_bytes = sum(f["size"] for f in manifest["files"])
    print(json.dumps({"ok": True, "pack": str(PACK),
                      "files": len(manifest["files"]),
                      "mebibytes": round(n_bytes / 2 ** 20, 1),
                      "failures_frozen": len(failures),
                      "ledger_total": ledger_snap["n_total"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
