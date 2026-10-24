#!/usr/bin/env python3
"""Task9：组装 Milestone 2 证据包（计划 L618-635，0 付费）。

复用（不 fork）``scripts/milestone1/build_evidence_pack.py`` 的全部安全机制：
- ``_sha256/_copy_file/_iter_copy_files/_snapshot_sqlite/_scan_secrets/
  _add_manifest/_gates_export/_git_head`` 与 SKIP_NAMES/_WAL_SUFFIXES 常量
  经 importlib 按文件加载，并把其模块全局 PACK/PHASE1_ROOT 重绑到本里程碑根
  （老文件冻结，一行不改；复用钩子全部走显式参数）；
- SQLite 存活库一律 online backup API 快照、无 -wal/-shm 侧车、入包后
  integrity_check；H-11 符号链接策略与 H-12 单一密钥正则（scan_text）；
- manifest（schema simpraxis.evidence_pack.v1，加 milestone=2）逐件
  sha256/size/origin/logical_ids；traceability_matrix.json；ADAPTATIONS.md；
  approvals 原件永不入包（只入 metadata+sha256 索引）；终态 FATAL 自检。

Phase 2 自己的事实：
- 根：$SIMPRAXIS_PHASE2_ROOT（默认 /mnt/artifacts/simpraxis/phase2）；
  容器/run 侧根：$SIMPRAXIS_RUN_DIR/phase2；图存活库物理上仍在 phase1 根
  （phase2/state 为空，图复用 phase1 两库），快照按真实位置取，ADAPTATIONS
  留痕；
- 活 LanceDB 位置只取 ``kb_pins.default_lancedb_uri()``（env/KB_ROOT 单一
  源），不硬编码；phase2_learnings 表不存在即如实记录，不造表；
- Task8 Stage0 报告 runs/phase2/task8/stage0/stage0_fluid_report.json 与
  Task7 计划路径 runs/phase2/task7/stage0/stage0_compat_report.json 为
  required，缺件 fail-closed 点件名（LEAD 在 Task7 容器实跑后做最终构建）；
- ``--clean`` 先扫 /proc/*/fd：有进程持有活 LanceDB / 存活 SQLite / 包目录
  句柄即拒建并给出处置指引，绝不强删、绝不跳过（评审教训：重建期不与写入者
  抢活库）。

用法：
  python scripts/milestone2/build_dashboard_phase2.py [--tests-result F]
  python scripts/milestone2/build_evidence_pack_phase2.py [--clean]
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))

from memory_flywheel.kb_pins import default_lancedb_uri  # noqa: E402
from orchestrator.common.runtime_paths import phase1_root, phase2_root  # noqa: E402

SCHEMA = "simpraxis.evidence_pack.v1"
MILESTONE = 2

# 源根键
P2, P1, R2, C = "p2", "p1", "runs2", "repo"

# ---------------------------------------------------------------------------
# Phase 2 复制清单：(pack 内相对路径, 根, 源相对路径, 逻辑编号, 必需)
# ---------------------------------------------------------------------------
FILES: list[tuple[str, str, str, str, bool]] = [
    # 账与配额（approvals 原件排除，只入索引）
    ("quota/ledger.jsonl", P2, "quota/ledger.jsonl", "M2.$.1", True),
    ("quota/counters.json", P2, "quota/counters.json", "M2.$.1", True),
    ("quota/quota_pause_schema_v2.signed.json", P2,
     "quota/quota_pause_schema_v2.signed.json", "M2.$.1", False),
    ("quota/resume_audit.jsonl", P2, "quota/resume_audit.jsonl",
     "M2.$.1", False),
    # 生产采纳遥测（飞轮生产召回的唯一活证据）
    ("recall/production_recall.jsonl", P2,
     "recall/production_recall.jsonl", "M2.$.3", True),
    ("recall/offgraph_writes.jsonl", P2, "recall/offgraph_writes.jsonl",
     "M2.$.3", False),
    ("recall/pre_task0b_migration_snapshot.json", P2,
     "recall/pre_task0b_migration_snapshot.json", "M2.T0.2", False),
    ("recall/post_ingest_inventory_delta.txt", P2,
     "recall/post_ingest_inventory_delta.txt", "M2.T0.2", False),
    # Task0b 三层分离结案
    ("task0b_closeout_evidence.md", P2, "task0b_closeout_evidence.md",
     "M2.T0.1", True),
    # Task4 Rung4 BinEval 标定（record_only 终态原件）
    ("task4_judge/calibration_bineval.json", P2,
     "task4_judge/calibration_bineval.json", "M2.T4.1", True),
    ("task4_judge/bineval_failure_report.json", P2,
     "task4_judge/bineval_failure_report.json", "M2.T4.1", True),
    # Task7 tour 结构清单（Task7 提交后存在；缺失不阻断当前构建）
    ("task7/libero_tour_structure_manifest.json", P2,
     "task7/libero_tour_structure_manifest.json", "M2.T7.1", False),
    ("v1_index.json", P2, "v1_index.json", "M2.T6.1", False),
    # Task9 看板与缓存清单（须先跑 build_dashboard_phase2.py）
    ("rsi_dashboard_phase2.json", P2,
     "task9_milestone/rsi_dashboard_phase2.json", "M2.T9.1", True),
    ("call_cache_inventory.json", P2,
     "task9_milestone/call_cache_inventory.json", "M2.T9.2", True),
    # run 侧：Task8/Task7 Stage0 机判报告（LEAD 指定路径，required）
    ("runs2/task8/stage0/stage0_fluid_report.json", R2,
     "task8/stage0/stage0_fluid_report.json", "M2.T8.1", True),
    ("runs2/task7/stage0/stage0_compat_report.json", R2,
     "task7/stage0/stage0_compat_report.json", "M2.T7.2", True),
    ("runs2/task0/uid_check.log", R2, "task0/uid_check.log",
     "M2.T0.1", False),
]

# 目录复制（phase2 安全 walker：链接实体永不复制，越界链接 FATAL）
DIR_COPIES: list[tuple[str, str, str, str, bool]] = [
    ("evolutions", P2, "evolutions", "M2.T6.2", True),
    ("evolution_sources/t6-genesis-ascent-20261016a", P2,
     "evolution_sources/t6-genesis-ascent-20261016a", "M2.T6.1", True),
    ("task4_judge/evidence", P2, "task4_judge/evidence", "M2.T4.2", True),
    ("task4_judge/batch_logs", P2, "task4_judge/batch_logs",
     "M2.T4.4", True),
    ("task4_judge/synth", P2, "task4_judge/synth", "M2.T4.3", True),
    ("task4_judge_retry_archive", P2, "task4_judge_retry_archive",
     "M2.T4.5", True),
    ("recall/backups", P2, "recall/backups", "M2.$.3", False),
    ("runs/task6_ascent", P2, "runs/task6_ascent", "M2.T6.3", True),
    ("runs/task6_smoke_20261019", P2, "runs/task6_smoke_20261019",
     "M2.T6.4", False),
    # Task7 容器第五平台产物：LEAD 最终构建时存在，当前缺失不阻断
    ("runs2/task7_libero", R2, "task7_libero", "M2.T7.3", False),
    # Task7 Stage0 六步实证机产原件（constraints.txt/logs/config.yaml）：
    # 机产件，合成 fixture 与 conflict 路径允许缺席，活构建实际命中
    ("runs2/task7/stage0/stage0_compat_report_assets", R2,
     "task7/stage0/stage0_compat_report_assets", "M2.T7.2", False),
]

# 图存活 SQLite（物理位置在 phase1 根；phase2/state 空——见 ADAPTATIONS）
SQLITE_DBS: list[tuple[str, str, str, str, bool]] = [
    ("state/pending_gates.db", P1, "state/pending_gates.db",
     "M2.T6.5", True),
    ("state/langgraph_checkpoints.db", P1, "state/langgraph_checkpoints.db",
     "M2.T6.5", True),
]

# required 活件缺件时的点名提示（fail-closed，给 LEAD 的处置语）
REQUIRED_ARTIFACT_HINTS = {
    "task8/stage0/stage0_fluid_report.json":
        "Task8 Stage0 流体能力探针报告缺失；先在容器内跑 Stage0（0 付费），"
        "再重跑本构建器。",
    "task7/stage0/stage0_compat_report.json":
        "Task7 Stage0 兼容报告缺失（LEAD 指定计划路径）；待 Task7 容器实跑"
        "产出后由 LEAD 重跑最终构建。",
    "task9_milestone/rsi_dashboard_phase2.json":
        "先跑 scripts/milestone2/build_dashboard_phase2.py 再打证据包。",
    "task9_milestone/call_cache_inventory.json":
        "先跑 scripts/milestone2/build_dashboard_phase2.py（随看板导出缓存清单）。",
}

ADAPTATIONS = """# Milestone 2 证据树锁内适配（规则 7 留痕；与 Phase 2 计划 Task9 偏差总表）

| 项 | 计划/名义 | 实际落点与理由 |
|---|---|---|
| phase1_learnings 行数（锁内适配 1） | 计划 L628 机判清单写 ``phase1_learnings=5`` | Task0a 时点旧值；活表现为 12（Task6 run1–6 经 gate1 批准运行后诚实 human_failure 回写 6 行 + run7a 回写 1 行；run7c 失败文本与 run5 逐字相同，内容寻址 upsert 塌到既有 24681e47 不增行）。看板四表段读活表，不手抄；测试不锁 12。 |
| 里程碑推送（锁内适配 2） | 计划 L635「Task9 做最终里程碑推送 origin/main」 | 用户常设指令「暂不推送，攒批」覆盖：本里程碑不 push；看板 git 段只只读记录 ahead 计数与 ``local_batch_held_not_pushed``，构建器/测试/产物中不含也不建议任何 push 动作。 |
| phase2/state 存活库 | 名义 ``$SIMPRAXIS_PHASE2_ROOT/state/*.db`` | Phase 2 图复用 phase1 两库（``$SIMPRAXIS_PHASE1_ROOT/state/pending_gates.db``、``langgraph_checkpoints.db``），phase2/state 为空目录。快照按真实位置经 backup API 取，manifest origin 记 p1；state/gates_export.json 同源只读导出。 |
| .call_cache 物理位置 | 名义 phase2 根 | setter._replay_cache() 默认 phase1_root()/.call_cache，Phase 2 沿用；缓存条目不入包（含模型响应），只入派生 call_cache_inventory.json（ts/key→整数 seq 配对，方法注记齐全）。 |
| task6_ascent 内绝对符号链接 | H-11 对绝对链接目录 FATAL | 该树有两个**树内**绝对链接 ``g6_watch_run6.latest.log``、``graph_resume_run6.latest.log``（目标在同一证据树内）。Phase2 walker：链接本身永不复制、登记进 symlinks.json，目标以真名正常入包；任何解析到证据根之外的链接一律 FATAL（wheelhouse 类越界链接即由此挡在包外）。 |
| runs/phase2/task8/stage0_wheels/ | 名义可入包的 wheel 实体 | 实为指向 /mnt/caches/simpraxis/wheelhouse_task8* 的绝对符号链接集（他方共享缓存），整目录排除；版本证据以 stage0_fluid_report.json 与容器内 pip freeze 为准。 |
| Task7 Stage0 报告 | LEAD 任务书给的计划路径 task7/stage0/stage0_compat_report.json | 最终 compatible 报告按 required 登记（schema=``simpraxis.task7.stage0_compat.v1``，``ok=true``/``outcome=compatible``，``ts_utc=2026-10-24T06:50:06Z``；六步 ``venv_create``/``robosuite_bddl``/``isolated_leaf_deps``/``libero_src_copy``/``libero_pkg_init_shim``/``libero_editable`` 全 rc=0，``import_chain`` numpy/torch/mujoco/robosuite/bddl/libero.libero 全 ok；三 pin 不漂移：numpy 1.26.4、torch 2.4.1+cu124（共享复用）、mujoco 3.15.0（pin_facts 全 true、``mujoco_diverged=false``），``mujoco-py=null``，另 robosuite 1.4.0、bddl 1.0.1、libero 0.1.0）。五轮 QA 锁内适配（每轮留快照，assets 只读树零改动）：轮 1–2 镜像缺 ensurepip → ``--without-pip``+``--system-site-packages`` 建隔离 venv、pip 自发现；轮 3 robosuite wheel 无 Requires-Dist → termcolor/future 两纯 Py 叶子在隔离 venv 内 ``-c`` 约束补装；轮 4 roro 只读挂载致 legacy setup.py egg_info EROFS → ``shutil.copytree(symlinks=False)`` 实体复制到 runs 可写副本再对副本 ``-e``；轮 5 上游 commit ``8f1084e3132a39270c3a13ebe37270a43ece2a01`` 缺顶层 ``libero/__init__.py``（find_packages 0 包、editable finder ``MAPPING={}``）→ 仅在可写副本补 0 字节标记（realpath 守卫，非空已存 fail-closed）。三张历史快照（``pre_adaptation``/``pre_copy_adapt``/``pre_init_shim``）同目录随 glob 真名附入。 |
| Task7 A 线结构清单 | 计划附录 B L981 结构清单（无 LFS 实体声明） | FILES 条目 ``required=False``（Task7 提交前缺失不阻断），最终构建时实际命中 ``task7/libero_tour_structure_manifest.json``；该件 2026-10-24T07:01Z 经 LEAD 复核带 4 条 corrections：``submodules_present`` 笔误 true→false、``clone.command`` 占位 ``<url>`` 补全为公开仓库逐字命令、B 线 venv 路径 /root→/home/agent 非 root 勘误、``.write_probe`` 遗留清理后 ``working_tree_clean`` 维持 true。 |
| phase2_learnings 表 | 计划机判「phase2_learnings ≥ 实际写入数」 | gate2 accept 从未发生，活表无此表；table_counts 派生件如实 exists=false/rows=0，不造表、不占位。 |
| V2 Pareto 判定 | 名义 V1/V2 Pareto 段 | v2_summary.json 从未生成（Gate2 全程未建行），看板 pareto_verdict=null 并写明 no_verdict_reason；run 目录原始观察值仅作 non-authoritative 旁证。Q1 矛盾 0.824<界 2.0 原样冻结，矛盾即结论，未翻案。 |
| tests 全绿机判 | 计划「341 旧 + 新增全绿」 | Task7/8 新文件未提交期间 test_milestone0_e2e::test_05_repos_clean_and_author 必红；看板把 collect-only 清单口径与 terminal 全绿机判分两字段，终态结果由 LEAD 提交后以 --tests-result 喂入真实全跑 JSON，不用收集数冒充全绿、不弱化该测试。 |
| Rung4 | 名义 active 需 D7+BinEval≥0.7 | 实测 min_agreement=0.8 但 defect_recall=0.0，D7_NOT_ACHIEVED，终态 record_only（用户签收）；看板/证据包禁止声称 active。 |
"""

REPLAY = [
    "source scripts/00_env.sh",
    "$PY -m pytest tests -q -k 'not test_04' -m 'not paid and not kb_integration'",
    "bash scripts/verification/gate_static.sh",
    "$PY scripts/kb/check_solutions.py",
    "$PY -m memory_flywheel.scripts.validate_capability_matrix",
    "$PY scripts/verification/check_blueprint_evidence.py",
    "$PY scripts/milestone2/build_dashboard_phase2.py",
    "$PY scripts/milestone2/build_evidence_pack_phase2.py --clean",
]


# ---------------------------------------------------------------------------
# 复用 milestone1 构建器（冻结老文件：只按文件加载并重绑全局，不改一行）
# ---------------------------------------------------------------------------

def _load_m1():
    path = _REPO / "scripts" / "milestone1" / "build_evidence_pack.py"
    spec = importlib.util.spec_from_file_location(
        "simpraxis_m1_evidence_pack", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载 milestone1 构建器：{path}")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


M1 = _load_m1()


# ---------------------------------------------------------------------------
# /proc 句柄守卫（--clean 前 fail-closed；纯函数可注入 proc_root 做 hermetic）
# ---------------------------------------------------------------------------

def _processes_holding_paths(paths: list[Path], *, proc_root: str = "/proc",
                             exclude_pid: int | None = None) -> list[dict]:
    """列出在 ``paths``（文件或目录前缀）上持有打开 fd 的进程。

    扫 /proc/<pid>/fd 的 readlink（不需 root、不依赖 lsof）；目标解析后等于
    受护路径或落在其目录前缀内即记一条。socket/pipe/anon_inode/dev 忽略。
    """
    targets = []
    for p in paths:
        try:
            rp = p.resolve(strict=False)
        except OSError:
            continue
        if rp.exists():
            targets.append(rp)
    if not targets:
        return []
    holders: list[dict] = []
    seen: set[tuple[int, str, str]] = set()
    for pid_dir in Path(proc_root).glob("[0-9]*"):
        try:
            pid = int(pid_dir.name)
        except ValueError:
            continue
        if exclude_pid is not None and pid == exclude_pid:
            continue
        fd_dir = pid_dir / "fd"
        try:
            entries = list(fd_dir.iterdir())
        except OSError:
            continue  # 内核/他人进程或已退出
        try:
            cmdline = (pid_dir / "cmdline").read_bytes().replace(
                b"\0", b" ").decode("utf-8", "ignore").strip()[:300]
        except OSError:
            cmdline = ""
        for fd in entries:
            try:
                link = os.readlink(fd)
            except OSError:
                continue
            if link.startswith(("pipe:", "socket:", "anon_inode:", "/dev/")):
                continue
            if link.endswith(" (deleted)"):
                link = link[: -len(" (deleted)")]
            resolved = Path(link).resolve(strict=False)
            s = str(resolved)
            for tgt in targets:
                hit = resolved == tgt or s.startswith(str(tgt) + os.sep)
                if not hit:
                    continue
                key = (pid, link, str(tgt))
                if key in seen:
                    continue
                seen.add(key)
                holders.append({"pid": pid, "fd": fd.name,
                                "open_path": link, "guard_root": str(tgt),
                                "cmdline": cmdline})
    return holders


def _guard_no_handles(paths: list[Path], *, proc_root: str) -> None:
    holders = _processes_holding_paths(
        paths, proc_root=proc_root, exclude_pid=os.getpid())
    if not holders:
        return
    lines = ["", "检测到进程持有活库/证据目录句柄，拒绝 --clean 重建"
                 "（重建期不与写入者抢活库）："]
    for h in holders:
        lines.append(
            f"  pid={h['pid']} fd={h['fd']} path={h['open_path']}"
            f"（守护根 {h['guard_root']}）cmd={h['cmdline'] or '?'}")
    lines += [
        "处置：先停掉持句柄的 graph/gateway/feishu 长驻进程（或等其正常"
        "退出；只停 simpraxis 自有进程，不擅杀他方 GPU 进程），确认后重跑",
        "  python scripts/milestone2/build_evidence_pack_phase2.py --clean",
        "本命令不会强制删除、不会跳过句柄检查。",
    ]
    raise SystemExit("\n".join(lines))


# ---------------------------------------------------------------------------
# Phase 2 安全 walker：链接实体永不复制；树内链接登记跳过，越界链接 FATAL
# ---------------------------------------------------------------------------

def _iter_phase2_files(src_base: Path, allowed_roots: list[Path],
                       symlink_index: list[dict], root_label: str):
    skip_names = M1.SKIP_NAMES
    allowed_resolved = [p.resolve() for p in allowed_roots]
    for dirpath, dirnames, filenames in os.walk(
            src_base, followlinks=False):
        dpath = Path(dirpath)
        kept: list[str] = []
        for d in dirnames:
            if d in skip_names:
                continue
            p = dpath / d
            if p.is_symlink():
                tgt = os.readlink(p)
                resolved = p.resolve(strict=False)
                inside = any(resolved == r or str(resolved).startswith(
                    str(r) + os.sep) for r in allowed_resolved)
                symlink_index.append({
                    "path": str(p.relative_to(src_base)), "root": root_label,
                    "target": tgt, "relative": not os.path.isabs(tgt),
                    "kind": "dir", "in_tree": inside,
                    "broken": not resolved.exists(), "copied": False})
                if not inside:
                    raise SystemExit(
                        f"证据树含越界符号链接目录，拒绝打包（H-11）：{p} -> {tgt}")
                continue  # 树内链接目录也不下降
            kept.append(d)
        dirnames[:] = kept
        for name in filenames:
            if name in skip_names:
                continue
            p = dpath / name
            if p.is_symlink():
                tgt = os.readlink(p)
                resolved = p.resolve(strict=False)
                inside = any(resolved == r or str(resolved).startswith(
                    str(r) + os.sep) for r in allowed_resolved)
                symlink_index.append({
                    "path": str(p.relative_to(src_base)), "root": root_label,
                    "target": tgt, "relative": not os.path.isabs(tgt),
                    "kind": "file", "in_tree": inside,
                    "broken": not resolved.exists(), "copied": False})
                if not inside:
                    raise SystemExit(
                        f"证据源为越界符号链接文件，拒绝打包（H-11）：{p} -> {tgt}")
                continue  # 树内链接文件：只登记不复制（目标以真名入包）
            yield p


# ---------------------------------------------------------------------------
# Phase 2 派生件
# ---------------------------------------------------------------------------

def _approvals_index(p2: Path, sha256) -> dict:
    out = {}
    adir = p2 / "quota" / "approvals"
    if not adir.is_dir():
        return out
    for f in sorted(adir.glob("*.json")):
        meta = json.loads(f.read_text(encoding="utf-8"))
        out[f.stem] = {"file": f"quota/approvals/{f.name}",
                       "sha256": sha256(f), "metadata": meta}
    return out


def _lance_table_counts(uri: str) -> dict:
    # 只读连接，短读即关；表行数与看板同源（复用 dashboard 段函数，不重抄）。
    here = Path(__file__).resolve()
    spec = importlib.util.spec_from_file_location(
        "simpraxis_m2_dashboard",
        here.parent / "build_dashboard_phase2.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod.lance_tables_block(uri)


def _ledger_summary(p2: Path) -> dict:
    from orchestrator.gateway.quota_ledger import call_lines
    path = p2 / "quota" / "ledger.jsonl"
    rows = [json.loads(l) for l in
            path.read_text(encoding="utf-8").splitlines() if l.strip()]
    calls = call_lines(rows)
    blocked = [r for r in rows if r.get("event") == "preflight_blocked"]
    seqs = sorted(r["seq"] for r in calls)
    by_batch: dict[str, dict] = {}
    for r in calls:
        b = by_batch.setdefault(r["batch_id"], {
            "used": 0, "ok": 0, "failed": 0,
            "approval_ref": r.get("approval_ref"),
            "approved": r.get("batch_approved"), "task": r.get("task")})
        b["used"] += 1
        b["ok" if r["ok"] else "failed"] += 1
    return {
        "path": str(path), "lines": len(rows), "calls_real": len(calls),
        "seq_min": seqs[0] if seqs else None,
        "seq_max": seqs[-1] if seqs else None,
        "seq_contiguous": seqs == list(range(1, len(seqs) + 1)),
        "ok": sum(1 for r in calls if r["ok"]),
        "failed": sum(1 for r in calls if not r["ok"]),
        "blocked_preflight": len(blocked),
        "all_ts_utc_z": all(
            str(r.get("ts_iso", "")).endswith("Z") for r in rows),
        "by_batch": by_batch,
        "semantics": "整数 seq 行=实呼；preflight_blocked(seq=null) 单列；"
                     "0 账行调用=call_cache 命中（见 call_cache_inventory）",
    }


def _phase1_sealed(p1: Path) -> dict:
    path = p1 / "quota" / "ledger.jsonl"
    n = sum(1 for l in path.read_text(encoding="utf-8").splitlines()
            if l.strip()) if path.is_file() else None
    return {"path": str(path), "lines": n,
            "access": "read_only：Phase 2 任何路径不得追加（§6.6）"}


def _symlinks_roots(p2: Path, runs2: Path) -> list[dict]:
    out: list[dict] = []
    for base, label in ((p2, "p2"), (runs2, "runs2")):
        if not base.exists():
            continue
        for link in base.rglob("*"):
            if link.is_symlink():
                tgt = os.readlink(link)
                out.append({
                    "path": str(link.relative_to(base)), "root": label,
                    "target": tgt, "relative": not os.path.isabs(tgt),
                    "kind": "dir" if link.is_dir() else "file",
                    "broken": not link.resolve(strict=False).exists()})
    return out


TRACEABILITY = [
    {"item": "T0a", "name": "Q2 补沉淀/图外回写通路",
     "evidence_ids": ["M2.$.3"],
     "tests": "tests/milestone2/test_task0a_q2_backfill.py、test_task1_recall.py"},
    {"item": "T0b", "name": "KB 三层分离（引擎/内容/数据）",
     "evidence_ids": ["M2.T0.1", "M2.T0.2"],
     "tests": "tests/milestone2/test_task0b_layers.py、test_task0_paths.py；"
              "scripts/kb/check_solutions.py"},
    {"item": "T4", "name": "Rung4 BinEval 标定 record_only 终态",
     "evidence_ids": ["M2.T4.1", "M2.T4.2", "M2.T4.3", "M2.T4.4", "M2.T4.5"],
     "tests": "test_task4_bineval_calibration.py、test_task4_rung4_probes.py"},
    {"item": "T6", "name": "升阶接缝：V1 冻结/Q1 矛盾/V2 五次失败出生",
     "evidence_ids": ["M2.T6.1", "M2.T6.2", "M2.T6.3", "M2.T6.4", "M2.T6.5"],
     "tests": "test_task2_ascent.py、test_task5_evolution.py、"
              "test_task6_ascent_flow.py、test_task6_entry_guards.py、"
              "test_task6_sentinel_seam.py"},
    {"item": "T7", "name": "LIBERO tour 结构 + 容器兼容 Stage0",
     "evidence_ids": ["M2.T7.1", "M2.T7.2", "M2.T7.3"],
     "tests": "tests/milestone2/test_task7_libero_seam.py（tour 本体永不实跑）"},
    {"item": "T8", "name": "Genesis 流固 Stage0（sph_ok，A800 实证）",
     "evidence_ids": ["M2.T8.1"],
     "tests": "tests/milestone2/test_task8_fluid_seam.py"},
    {"item": "recall", "name": "飞轮生产采纳遥测（接线后现状）",
     "evidence_ids": ["M2.$.3"],
     "tests": "test_task1_recall.py；production_recall.jsonl 六字段严格计数"},
    {"item": "paid", "name": "付费双账纪律（phase2 新账/phase1 封存/缓存口径）",
     "evidence_ids": ["M2.$.1", "M2.$.2", "M2.T9.2"],
     "tests": "全套零联网单测；ledger 整数 seq 连续性自校验"},
    {"item": "T9", "name": "看板/证据包/容器 GPU 收尾总验收",
     "evidence_ids": ["M2.T9.1", "M2.T9.2", "M2.T9.3"],
     "tests": "tests/milestone2/test_task9_closeout.py"},
    {"item": "env", "name": "环境与收尾纪律",
     "evidence_ids": ["M2.T8.1", "M2.T0.1"],
     "tests": "看板 closeout 段：docker ps -a 零 sim_praxis 容器、"
              "nvidia-smi ≤200MiB、df /mnt"},
]


# ---------------------------------------------------------------------------
# 主装包流程（根全部显式传参，便于 hermetic 自测）
# ---------------------------------------------------------------------------

def build_pack(*, p2: Path, p1: Path, runs2: Path, repo: Path,
               pack: Path, lance_uri: str, clean: bool = False,
               proc_root: str = "/proc") -> dict:
    m = M1
    # 重绑老构建器全局（其所有 helper 经全局取根/PACK；老文件本身不改）。
    m.PACK = pack
    m.PHASE1_ROOT = p1
    m.RUNS_PHASE1 = runs2  # 老 _symlinks_index 等不被调用，仅保持一致
    sha256 = m._sha256

    roots = {P2: p2, P1: p1, R2: runs2, C: repo}

    # required 活件点名预检（缺件信息要带 LEAD 处置语）
    for spec in FILES:
        pack_rel, root_key, src_rel, _logical, required = spec
        if not required:
            continue
        src = roots[root_key] / src_rel
        if not src.is_file():
            hint = REQUIRED_ARTIFACT_HINTS.get(src_rel)
            raise SystemExit(
                f"必需证据缺失：{src}\n  逻辑编号 {spec[3]}。"
                + (f"\n  处置：{hint}" if hint else ""))

    if clean:
        # 重建窗口守卫：活 LanceDB（解析软链后的实体）、两存活 SQLite、
        # 旧包目录均不得被他进程持有。
        guarded = [Path(lance_uri),
                   p1 / "state" / "pending_gates.db",
                   p1 / "state" / "langgraph_checkpoints.db"]
        if pack.exists():
            guarded.append(pack)
        _guard_no_handles(guarded, proc_root=proc_root)
        # 删除守卫（规则 6）：pack 必须在 phase2 根字面路径内。
        pack_res = pack.resolve(strict=False)
        p2_res = p2.resolve()
        if pack_res != p2_res and not str(pack_res).startswith(
                str(p2_res) + os.sep):
            raise SystemExit(f"拒绝删除：包目录不在 phase2 根内：{pack}")
        # 首建（包目录缺席）也是合法 --clean 路径：缺席则跳过删除，
        # 裸 rmtree 在首建时会抛 FileNotFoundError。
        if pack.exists():
            shutil.rmtree(pack)
    pack.mkdir(parents=True, exist_ok=True)

    manifest_files: list[dict] = []

    def cp(pack_rel: str, root_key: str, src_rel: str, logical: str,
           required: bool) -> None:
        m._copy_file(roots[root_key] / src_rel, pack / pack_rel, logical,
                     required, root_key, manifest_files)

    for spec in FILES:
        cp(*spec)

    # Task7 stage0 目录的其余机产件（pre_adaptation 快照等，随真名复制）
    t7_stage0 = runs2 / "task7" / "stage0"
    if t7_stage0.is_dir():
        for f in sorted(t7_stage0.glob("*.json")):
            if f.name == "stage0_compat_report.json" or f.is_symlink():
                continue
            m._copy_file(f, pack / "runs2" / "task7" / "stage0" / f.name,
                         "M2.T7.2", False, R2, manifest_files)

    # 存活 SQLite：backup API 一致性快照
    for pack_rel, root_key, src_rel, logical, required in SQLITE_DBS:
        m._snapshot_sqlite(roots[root_key] / src_rel, pack / pack_rel,
                           logical, required, root_key, manifest_files)

    # 目录复制（Phase2 安全 walker）
    syms_seen: list[dict] = []
    for pack_prefix, root_key, src_dir, logical, required in DIR_COPIES:
        src_base = roots[root_key] / src_dir
        if not src_base.exists():
            if required:
                raise FileNotFoundError(f"必需证据目录缺失：{src_base}")
            continue
        if src_base.is_symlink():
            raise SystemExit(
                f"证据清单根为符号链接，拒绝打包（H-11）：{src_base} -> "
                f"{os.readlink(src_base)}")
        allowed = [roots[root_key]] + [p2, p1, runs2]
        for f in sorted(_iter_phase2_files(
                src_base, allowed, syms_seen, root_key)):
            rel = f.relative_to(src_base)
            m._copy_file(f, pack / pack_prefix / rel, logical, True,
                         root_key, manifest_files)

    # 包内兜底：任何符号链接都不得存在（walker 已保证，双保险）
    leaked_links = [str(p.relative_to(pack)) for p in pack.rglob("*")
                    if p.is_symlink()]
    if leaked_links:
        raise SystemExit(f"证据包混入符号链接：{leaked_links}")

    # ---- 派生只读视图 -----------------------------------------------------
    gates = m._gates_export()  # 重绑后读 p1/state/pending_gates.db
    gp = pack / "state" / "gates_export.json"
    gp.parent.mkdir(parents=True, exist_ok=True)
    gp.write_text(json.dumps(gates, ensure_ascii=False, indent=2),
                  encoding="utf-8")
    m._add_manifest(manifest_files, gp, p1 / "state" / "pending_gates.db",
                    "derived", "M2.T6.5", True)

    aidx = _approvals_index(p2, sha256)
    ap_path = pack / "quota" / "approvals_index.json"
    ap_path.write_text(json.dumps(aidx, ensure_ascii=False, indent=2),
                       encoding="utf-8")
    manifest_files.append({
        "path": "quota/approvals_index.json", "origin": "derived",
        "origin_rel": "quota/approvals/*.json",
        "size": ap_path.stat().st_size, "sha256": sha256(ap_path),
        "logical_ids": ["M2.$.2"], "required": True})

    counts = _lance_table_counts(lance_uri)
    lc_path = pack / "lance" / "table_counts.json"
    lc_path.parent.mkdir(parents=True, exist_ok=True)
    lc_path.write_text(json.dumps(counts, ensure_ascii=False, indent=2),
                       encoding="utf-8")
    manifest_files.append({
        "path": "lance/table_counts.json", "origin": "derived",
        "origin_rel": f"lancedb uri={lance_uri}（kb_pins.default_lancedb_uri）",
        "size": lc_path.stat().st_size, "sha256": sha256(lc_path),
        "logical_ids": ["M2.T0.2", "M2.T5.1"], "required": True})

    syms = _symlinks_roots(p2, runs2)
    sp = pack / "symlinks.json"
    sp.write_text(json.dumps(syms, ensure_ascii=False, indent=2),
                  encoding="utf-8")
    manifest_files.append({
        "path": "symlinks.json", "origin": "derived",
        "origin_rel": "phase2+runs2 symlink walk",
        "size": sp.stat().st_size, "sha256": sha256(sp),
        "logical_ids": ["M2.T9.3"], "required": True})

    (pack / "ADAPTATIONS.md").write_text(ADAPTATIONS, encoding="utf-8")
    manifest_files.append({
        "path": "ADAPTATIONS.md", "origin": "derived",
        "origin_rel": "Phase 2 Task9 偏差映射",
        "size": (pack / "ADAPTATIONS.md").stat().st_size,
        "sha256": sha256(pack / "ADAPTATIONS.md"),
        "logical_ids": ["M2.T9.3"], "required": True})

    (pack / "traceability_matrix.json").write_text(
        json.dumps({"schema": "simpraxis.traceability.v1",
                    "plan_ref": "Phase_2_Plan_detailed.md Task9（L618-635）",
                    "rows": TRACEABILITY}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    manifest_files.append({
        "path": "traceability_matrix.json", "origin": "derived",
        "origin_rel": "Phase_2_Plan_detailed.md Task9",
        "size": (pack / "traceability_matrix.json").stat().st_size,
        "sha256": sha256(pack / "traceability_matrix.json"),
        "logical_ids": ["M2.T9.3"], "required": True})

    # ---- 终态 FATAL 自检（沿用 milestone1 三件套）------------------------
    forbidden = [str(p.relative_to(pack)) for p in pack.rglob("*")
                 if p.is_file() and "approvals" in p.parts
                 and p.name != "approvals_index.json"]
    if forbidden:
        raise SystemExit(f"证据包混入凭据/批准原件：{forbidden}")
    wal_packed = [str(p.relative_to(pack)) for p in pack.rglob("*")
                  if p.is_file() and p.name.endswith(m._WAL_SUFFIXES)]
    if wal_packed:
        raise SystemExit(f"证据包混入 SQLite -wal/-shm 伴生件：{wal_packed}")
    hits = m._scan_secrets(pack)
    if hits:
        raise SystemExit("证据包 secret scan 命中：\n" + "\n".join(hits))

    failures = [{"pack_path": str(f.relative_to(pack)),
                 "sha256": sha256(f), "immutable": True}
                for f in pack.rglob("FAILURE.json")]

    manifest = {
        "schema": SCHEMA, "milestone": MILESTONE,
        "built_at_iso": datetime.now(timezone.utc).astimezone().isoformat(),
        "git_commit": m._git_head(),
        "plan": "Phase_2_Plan_detailed.md Task9（L618-635）",
        "roots": {"p2": str(p2), "p1": str(p1), "runs2": str(runs2),
                  "repo": str(repo)},
        "lance_uri": lance_uri,
        "replay_commands": REPLAY,
        "push_state": "local_batch_held_not_pushed（锁内适配 2：用户常设"
                      "「暂不推送，攒批」覆盖计划 L635）",
        "ledger": _ledger_summary(p2),
        "phase1_sealed_ledger": _phase1_sealed(p1),
        "failures": failures,
        "exclusions": [
            {"path": "quota/approvals/*.json",
             "reason": "批准凭据原件不打包；索引见 approvals_index.json"},
            {"path": "runs2/task8/stage0_wheels/",
             "reason": "指向 /mnt/caches 他方共享缓存的绝对符号链接集；"
                       "版本证据以 stage0 报告为准（见 ADAPTATIONS）"},
            {"path": "*.latest.log（树内绝对符号链接）",
             "reason": "链接本身不复制，登记 symlinks.json；目标以真名入包"},
        ],
        "files": sorted(manifest_files, key=lambda x: x["path"]),
    }
    (pack / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    n_bytes = sum(f["size"] for f in manifest["files"])
    return {"ok": True, "pack": str(pack),
            "files": len(manifest["files"]),
            "mebibytes": round(n_bytes / 2 ** 20, 1),
            "failures_frozen": len(failures),
            "calls_real": manifest["ledger"]["calls_real"],
            "blocked": manifest["ledger"]["blocked_preflight"],
            "symlinks_indexed": len(syms)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--clean", action="store_true", help="先清空包目录重建")
    ap.add_argument("--proc-root", default="/proc",
                    help="/proc 根（测试注入用）")
    args = ap.parse_args()

    p2 = phase2_root()
    p1 = phase1_root()
    runs2 = Path(os.environ.get("SIMPRAXIS_RUN_DIR",
                                "/mnt/artifacts/simpraxis/runs")) / "phase2"
    pack = p2 / "task9_milestone" / "evidence_pack"
    summary = build_pack(p2=p2, p1=p1, runs2=runs2, repo=_REPO, pack=pack,
                         lance_uri=default_lancedb_uri(), clean=args.clean,
                         proc_root=args.proc_root)
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
