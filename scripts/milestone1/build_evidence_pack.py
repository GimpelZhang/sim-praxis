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
- 打包前对所有文本件跑 scan_secrets 同款 9 条正则（Python 移植）。

与 §1.8 树不一致处一律写进 ADAPTATIONS.md（锁内适配留痕，规则 7）。
用法：
  python scripts/milestone1/build_evidence_pack.py [--clean]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))

PHASE1_ROOT = Path(os.environ.get(
    "SIMPRAXIS_PHASE1_ROOT", "/mnt/artifacts/simpraxis/phase1"))
RUNS_PHASE1 = Path(os.environ.get(
    "SIMPRAXIS_RUN_DIR", "/mnt/artifacts/simpraxis/runs")) / "phase1"
PACK = PHASE1_ROOT / "task9_milestone" / "evidence_pack"

SCHEMA = "simpraxis.evidence_pack.v1"

# scan_secrets.sh 同款 9 条（第 9 条大小写不敏感）
SECRET_PATTERNS = [
    (r"hf_[A-Za-z0-9]{20,}", False),
    (r"gh[pousr]_[A-Za-z0-9]{20,}", False),
    (r"nvapi-[A-Za-z0-9_-]{20,}", False),
    (r"ark-[A-Za-z0-9][A-Za-z0-9-]{12,}", False),
    (r"sk-[A-Za-z0-9_-]{24,}", False),
    (r"AKIA[0-9A-F]{16}", False),
    (r"xox[baprs]-[A-Za-z0-9-]{10,}", False),
    (r"-----BEGIN (RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----", False),
    (r"(api[_-]?key|access[_-]?token|auth[_-]?token|authorization)"
     r"([\s]{0,5})[:=]([\s]{0,5})['\"][A-Za-z0-9_./+-]{24,}['\"]", True),
]

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
    # state
    ("state/pending_gates.db", H, "state/pending_gates.db", "E1.5", True),
    ("state/langgraph_checkpoints.db", H, "state/langgraph_checkpoints.db", "E3.3", True),
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

SKIP_NAMES = {"__pycache__", ".ledger.lock"}


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
    hits = []
    for f in root.rglob("*"):
        if not f.is_file():
            continue
        try:
            data = f.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for pat, ignore_case in SECRET_PATTERNS:
            flags = re.IGNORECASE if ignore_case else 0
            m = re.search(pat, data, flags)
            if m:
                hits.append(f"{f.relative_to(root)} :: /{pat}/ :: {m.group(0)[:40]}")
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
                    "broken": not resolved.exists(),
                    "points_to_failure": (resolved.name.startswith("FAILURE")
                                          or (resolved / "FAILURE.json").exists()),
                })
    return out


def _add_manifest(manifest: list, dst: Path, src: Path, origin: str,
                  logical: str, required: bool) -> None:
    """登记 manifest；同包路径（FILES 与 DIR_COPIES 重叠时）合并逻辑编号。"""
    rel = str(dst.relative_to(PACK))
    rec = next((r for r in manifest if r["path"] == rel), None)
    if rec is None:
        manifest.append({
            "path": rel, "origin": origin, "origin_rel": str(src),
            "size": dst.stat().st_size, "sha256": _sha256(dst),
            "logical_ids": [logical], "required": required})
    elif logical not in rec["logical_ids"]:
        rec["logical_ids"].append(logical)
        rec["required"] = rec["required"] or required


def _copy_file(src: Path, dst: Path, logical: str, required: bool,
               origin: str, manifest: list) -> None:
    if not src.exists():
        if required:
            raise FileNotFoundError(f"必需证据缺失：{src}")
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    _add_manifest(manifest, dst, src, origin, logical, required)


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

    # 目录复制（含 q2 npz 等二进制；跳过 __pycache/锁）
    for pack_prefix, root, src_dir, logical in DIR_COPIES:
        src_base = _origin(root, src_dir)
        if not src_base.exists():
            raise FileNotFoundError(f"必需证据目录缺失：{src_base}")
        for f in sorted(src_base.rglob("*")):
            if f.is_dir() or f.name in SKIP_NAMES \
                    or any(p.name in SKIP_NAMES for p in f.parents):
                continue
            rel = f.relative_to(src_base)
            _copy_file(f, PACK / pack_prefix / rel, logical, True, root,
                       manifest_files)

    for pack_rel, src_rel, logical in RUN_SINGLES:
        src = RUNS_PHASE1 / src_rel
        if src.is_dir():
            for f in sorted(src.rglob("*")):
                if f.is_dir() or f.name in SKIP_NAMES:
                    continue
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
    manifest_files.append({"path": str(lp.relative_to(PACK)),
                           "origin": "derived",
                           "origin_rel": "lancedb/phase1_learnings.lance",
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
         "evidence_ids": ["E1.1", "E1.2", "E1.3", "E1.4", "E1.5", "E7.2"],
         "tests": "tests/milestone1/test_feishu_gateway.py + 本包 state/gates_export.json"},
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
    if he.is_dir():
        for f in sorted(he.rglob("*")):
            if f.is_file() and f.name not in SKIP_NAMES:
                _copy_file(f, PACK / "hitl_evidence" / f.relative_to(he),
                           "E9.2", True, H, manifest_files)

    # 凭据排除自检：包内不得出现 approvals 原件或任何凭据文件名
    forbidden = [str(p.relative_to(PACK)) for p in PACK.rglob("*")
                 if p.is_file() and ("approvals" in p.parts
                                     and p.name != "approvals_index.json")]
    if forbidden:
        raise SystemExit(f"证据包混入凭据原件：{forbidden}")

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
