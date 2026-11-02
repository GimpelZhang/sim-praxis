#!/usr/bin/env python3
"""Task10：组装 Phase 2.5 里程碑证据包（AC-T10-4，0 付费）。

复用（不 fork）既有两段安全机制：
- ``scripts/milestone1/build_evidence_pack.py`` 的 ``_sha256/_copy_file/
  _iter_copy_files/_snapshot_sqlite/_scan_secrets/_add_manifest/_git_head``
  与 SKIP_NAMES/_WAL_SUFFIXES（``_iter_copy_files`` 为 H-11 安全 walk：符号
  链接文件 FATAL、绝对链接目录 FATAL、相对目录链接不下降并登记）；
- ``scripts/milestone2/build_evidence_pack_phase2.py`` 的
  ``_processes_holding_paths`` 句柄守卫（``--clean`` 前扫 /proc/*/fd，
  有持句柄进程即拒建，绝不强删）。
老文件一行不改：全部经 importlib 按文件加载，只重绑本包的 PACK/P1 全局。

Phase 2.5 自己的事实：
- 根：``$SIMPRAXIS_PHASE25_ROOT``（默认 /mnt/artifacts/simpraxis/phase25）；
  run 侧根：``$SIMPRAXIS_RUN_DIR/phase25``；
- 证据根登记区（AC-T10-4）：``evidence_roots`` 显式注册
  ``@runs/phase25/<thread>/stage_<id>/`` 标签，指明其解析基准
  （``@runs/`` 前缀 → ``$SIMPRAXIS_RUN_DIR``，子目录 ``phase25/``）与当前
  使用行数（Phase 2.5 未产生 Gate2 accept，故为 0——登记的是白名单来源，
  不是伪造行）；
- 数据层不打包：``task6_r4_window/<ts>/lancedb_prewindow_backup`` 是窗前
  LanceDB 实体备份（>200MB，数据层闭源），只入 sha256 指针与说明，不入包；
- approvals 原件永不入包（沿用 m1 规则），只入 metadata+sha256 索引。

用法：
  python scripts/phase25/build_evidence_pack_phase25.py [--clean]
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))

from memory_flywheel.kb_pins import default_lancedb_uri  # noqa: E402
from memory_flywheel.phase1.learning_store import (  # noqa: E402
    runs_root)  # 证据根 @runs/ 的单一源（与 persist 接缝同函数）
from orchestrator.common.runtime_paths import (  # noqa: E402
    phase1_root, phase25_root, phase2_root)

SCHEMA = "simpraxis.evidence_pack.v1"
MILESTONE = "2.5"

# 源根键
P25, R25, REPO, P1 = "p25", "r25", "repo", "p1"

WINDOW = "task6_r4_window/2026-11-02T031952Z"

# 仓库/单件硬上限（测试可 monkeypatch 以构造小额超限件）
MAX_FILE_BYTES = 10 * 1024 * 1024


class PackError(RuntimeError):
    """缺必需证据/越界删除；CLI exit 2，绝不产出"半包"充当完整。"""


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


M1 = _load(_REPO / "scripts/milestone1/build_evidence_pack.py", "m1_pack")
M2 = _load(_REPO / "scripts/milestone2/build_evidence_pack_phase2.py",
           "m2_pack")

# ---------------------------------------------------------------------------
# 单文件清单：(pack 内相对路径, 根, 源相对路径, 逻辑编号, 必需)
# ---------------------------------------------------------------------------
FILES: list[tuple[str, str, str, str, bool]] = [
    # ---- Task 0 基座 ----
    ("task0_baseline/t0_dependency_verdict.json", P25,
     "estimates/t0_dependency_verdict.json", "M25.T0.1", True),
    ("task0_baseline/t0_test_baseline.json", P25,
     "estimates/t0_test_baseline.json", "M25.T0.2", True),
    ("task0_baseline/t0_baseline_pytest.raw.log", P25,
     "estimates/t0_baseline_pytest.raw.log", "M25.T0.3", False),
    # ---- Task 1 方法学 v2 ----
    ("task1_methodology/methodology_v2.md", P25,
     "estimates/methodology_v2.md", "M25.T1.1", True),
    ("task1_methodology/hindcast_backtest.json", P25,
     "estimates/hindcast_backtest.json", "M25.T1.2", True),
    ("task1_methodology/burn_rules.json", P25,
     "estimates/burn_rules.json", "M25.T1.3", True),
    ("task1_methodology/hindcast_dataset.json", P25,
     "estimates/hindcast_dataset.json", "M25.T1.4", True),
    ("task1_methodology/interval_t3.json", P25,
     "estimates/interval_t3.json", "M25.T1.5", True),
    ("task1_methodology/interval_t5.json", P25,
     "estimates/interval_t5.json", "M25.T1.5", True),
    ("task1_methodology/interval_t9.json", P25,
     "estimates/interval_t9.json", "M25.T1.5", True),
    ("task1_methodology/interval_t10.json", P25,
     "estimates/interval_t10.json", "M25.T1.5", True),
    ("task1_methodology/rebaselines/pre_fix_sha256.json", P25,
     "estimates/rebaselines/pre_fix_sha256.json", "M25.T1.6", True),
    # ---- Task 5 墙冻结清单 ----
    ("task5_walls/SEQ_SPLIT.json", P25, "walls/wall_b/SEQ_SPLIT.json",
     "M25.T5.1", True),
    ("task5_walls/freeze_manifest.json", P25,
     "walls/t5_freeze/manifests/freeze_manifest.json", "M25.T5.2", True),
    ("task5_walls/LOCK.json", P25, "walls/t5_freeze/manifests/LOCK.json",
     "M25.T5.2", True),
    ("task5_walls/snapshots/secret_scan.json", P25,
     "walls/t5_freeze/snapshots/secret_scan.json", "M25.T5.3", True),
    # ---- Task 6 召回 ----
    ("task6_recall/gate1_freeze/gate1_freeze_registration_2026-10-31.json",
     P25, "task6_recall/gate1_freeze/"
          "gate1_freeze_registration_2026-10-31.json", "M25.T6.1", True),
    ("task6_stage_b/prereg.md", REPO,
     "scripts/phase25/task6_r4_prereg.md", "M25.T6.2", True),
    ("task6_stage_b/point_predictions.json", REPO,
     "scripts/phase25/snapshots/task6_r4_point_predictions.json",
     "M25.T6.3", True),
    ("task6_stage_b/reference_sim.py", REPO,
     "scripts/phase25/task6_r4_reference_sim.py", "M25.T6.3", True),
    # ---- Task 6 Stage B 窗口（精挑；数据层备份不打包）----
    (f"task6_stage_b/window/{WINDOW.split('/')[-1]}/task6_r4_window.log", P25,
     f"{WINDOW}/task6_r4_window.log", "M25.T6.4", True),
    (f"task6_stage_b/window/{WINDOW.split('/')[-1]}/hermetic_gate.log", P25,
     f"{WINDOW}/hermetic_gate.log", "M25.T6.5", True),
    (f"task6_stage_b/window/{WINDOW.split('/')[-1]}/kbint_gate.log", P25,
     f"{WINDOW}/kbint_gate.log", "M25.T6.5", True),
    (f"task6_stage_b/window/{WINDOW.split('/')[-1]}/gate_static.log", P25,
     f"{WINDOW}/gate_static.log", "M25.T6.5", True),
    (f"task6_stage_b/window/{WINDOW.split('/')[-1]}/iter5_binding.log", P25,
     f"{WINDOW}/iter5_binding.log", "M25.T6.6", True),
    (f"task6_stage_b/window/{WINDOW.split('/')[-1]}/window_evidence.json",
     P25, f"{WINDOW}/window_evidence.json", "M25.T6.6", True),
    (f"task6_stage_b/window/{WINDOW.split('/')[-1]}/task6_r4_verdict.json",
     P25, f"{WINDOW}/task6_r4_verdict.json", "M25.T6.7", True),
    (f"task6_stage_b/window/{WINDOW.split('/')[-1]}/"
     "pre_vector_identity.json", P25,
     f"{WINDOW}/pre_vector_identity.json", "M25.T6.8", True),
    (f"task6_stage_b/window/{WINDOW.split('/')[-1]}/"
     "snapshot_contemporaneity_check.json", P25,
     f"{WINDOW}/snapshot_contemporaneity_check.json", "M25.T6.8", True),
    (f"task6_stage_b/window/{WINDOW.split('/')[-1]}/prewindow_snapshot.json",
     P25, f"{WINDOW}/prewindow_snapshot.json", "M25.T6.9", True),
    # ---- 账 ----
    ("quota/ledger.jsonl", P25, "quota/ledger.jsonl", "M25.$ .1", True),
    ("quota/counters.json", P25, "quota/counters.json", "M25.$ .1", False),
]

# ---------------------------------------------------------------------------
# 目录清单：(pack 前缀, 根, 源相对目录, 逻辑编号, 必需)
# ---------------------------------------------------------------------------
# 第 5 位＝显式排除子树（相对源目录的 POSIX 路径）。仅用于「环境接线」类
# 符号链接：`gate_env/pyshim/libero -> /opt/libero_tour/libero` 是墙 A 只读
# tour 源的绝对链接（H-11 禁打包实体），摘除事实记入 `symlinks_excluded.json`。
DIR_COPIES: list[tuple[str, str, str, str, bool, tuple[str, ...]]] = [
    ("task0_baseline/t0_deps", P25, "probes/t0_deps", "M25.T0.1", True, ()),
    ("task2_watchdog", P25, "watchdog", "M25.T2.1", True, ()),
    ("task3_stage0", P25, "probes/stage0", "M25.T3.1", True, ()),
    ("task5_walls/freeze", P25, "walls/t5_freeze", "M25.T5.2", True,
     ("gate_env/pyshim",)),
    ("task6_recall", P25, "task6_recall", "M25.T6.1", True, ()),
    # 三签件（AC-T10-4：「三签件全入」）。可选：收尾链顺序为 对账→看板→
    # 机判→三签→终建包，故首建包时该目录可以尚不存在，缺则如实由
    # ``signs_MISSING`` 标记承担（见下方派生写）。
    ("task10_closeout/signs", P25, "task10_milestone/signs", "M25.T10.3",
     False, ()),
]

# 证据根登记区（AC-T10-4）：标签 → 解析基准与当前使用行数。
EVIDENCE_ROOTS = {
    "@runs/phase25/<thread>/stage_<id>/": {
        "tag": "@runs/",
        "root_env": "SIMPRAXIS_RUN_DIR",
        "root_default": str(runs_root()),
        "subdir": "phase25",
        "resolves_to": str(runs_root() / "phase25"),
        "rows_using": 0,
        "note": ("Phase 2.5 未产生 Gate2 accept/学习回写行，故 0 行使用；"
                 "标签按 LearningStore 既有 @runs/ 分支解析（不新增第四根、"
                 "不改 learning_store），在此登记以便未来 VLA 多阶段战役的"
                 "证据路径可机判白名单。"),
    },
    "@runs/": {"tag": "@runs/", "root_env": "SIMPRAXIS_RUN_DIR",
               "root_default": str(runs_root()),
               "note": "Task7 起真实 rollout 产物根（Phase 1 既有两分支之一）"},
    "@phase2/": {"tag": "@phase2/", "root_env": "SIMPRAXIS_PHASE2_ROOT",
                 "root_default": str(phase2_root()),
                 "note": "锁内适配第三根（Phase 2 Task6 升阶），本里程碑不改"},
    "bare": {"tag": "", "root_env": "SIMPRAXIS_PHASE1_ROOT",
             "root_default": str(phase1_root()),
             "note": "裸 relpath＝phase1 证据根（2026-10-28 活表 44 行沿用）"},
}

TRACEABILITY = [
    {"item": "T0", "name": "开工基座（分账/封存/依赖裁决/基线快照）",
     "evidence_ids": ["M25.T0.1", "M25.T0.2", "M25.T0.3"],
     "tests": "tests/milestone25/test_task0_base.py"},
    {"item": "T1", "name": "呼量估算方法论 v2（错误墙报价/区间件/燃尽）",
     "evidence_ids": ["M25.T1.1", "M25.T1.2", "M25.T1.3", "M25.T1.4",
                      "M25.T1.5"],
     "tests": "tests/milestone25/test_task1_estimate.py"},
    {"item": "T2", "name": "看门狗 SOP（双签名/PDEATHSIG/元告警/注入）",
     "evidence_ids": ["M25.T2.1"],
     "tests": "tests/milestone25/test_task2_watchdog.py"},
    {"item": "T3", "name": "Stage0 传输探针 P0–P5（六帽三态）",
     "evidence_ids": ["M25.T3.1"],
     "tests": "tests/milestone25/test_task3_stage0.py"},
    {"item": "T4", "name": "agentic 内环产品化（六工具/三闸/侧车）",
     "evidence_ids": ["M25.T4.1"],
     "tests": "tests/milestone25/test_task4_agentic.py（五轮对抗 C1–C4+M1–M5）"},
    {"item": "T5", "name": "历史墙 A/B 三态实证（含 5b 重测）",
     "evidence_ids": ["M25.T5.1", "M25.T5.2", "M25.T5.3"],
     "tests": "tests/milestone25/test_task5_wall_contracts.py"},
    {"item": "T6", "name": "Gate1 四路召回（r3→Stage A 负结果→Stage B 保留 r4）",
     "evidence_ids": ["M25.T6.1", "M25.T6.2", "M25.T6.3", "M25.T6.4",
                      "M25.T6.5", "M25.T6.6", "M25.T6.7", "M25.T6.8",
                      "M25.T6.9"],
     "tests": "tests/milestone25/test_task6_*.py"},
    {"item": "T7/T8/T9", "name": "用户 2026-11-02 裁决不再进行（无产出，不虚报）",
     "evidence_ids": [], "tests": "—"},
    {"item": "T10", "name": "收尾：对账/看板/证据包/机判/Complete/三签",
     "evidence_ids": ["M25.T10.1", "M25.T10.2"],
     "tests": "tests/milestone25/test_task10_closeout.py；"
              "scripts/verification/check_phase25_closure.py"},
    {"item": "env", "name": "环境与收尾纪律",
     "evidence_ids": ["M25.T10.2"],
     "tests": "看板 closeout 段：docker ps -a 零容器、nvidia-smi ≤200MiB、"
              "df /mnt"},
]


def _symlinks_under(path: Path) -> list[dict]:
    """列出被排除子树内的全部符号链接（只登记，目标实体永不入包）。"""
    out: list[dict] = []
    for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
        d = Path(dirpath)
        for name in list(dirnames) + list(filenames):
            p = d / name
            if p.is_symlink():
                out.append({
                    "path": str(p),
                    "relative_path": p.relative_to(path).as_posix(),
                    "target": os.readlink(p),
                    "absolute": os.path.isabs(os.readlink(p)),
                    "kind": "dir" if name in dirnames else "file",
                    "broken": not p.exists(),
                    "copied": False})
    return out


def _normalize_modes(pack: Path) -> dict:
    """AC-T10-4：包内目录 0700、文件 0600（不收来源处 0664/0755 的余数）。"""
    dirs = files = 0
    for dirpath, dirnames, filenames in os.walk(pack):
        os.chmod(dirpath, 0o700)
        dirs += 1
        for name in filenames:
            os.chmod(Path(dirpath) / name, 0o600)
            files += 1
    return {"dirs_0700": dirs, "files_0600": files}


def _push_state(repo: Path) -> dict:
    """只读 git 远端状态（推送永远是人工 STOP 门，本脚本只记录不推送）。"""
    def run(args: list[str]) -> str:
        out = subprocess.run(args, cwd=repo, capture_output=True, text=True)
        return out.stdout.strip() if out.returncode == 0 else ""

    head = run(["git", "rev-parse", "HEAD"])
    origin = run(["git", "rev-parse", "origin/main"])
    ahead_raw = run(["git", "rev-list", "--count", "origin/main..HEAD"]) \
        if origin else ""
    return {
        "head": head,
        "origin_main": origin,
        "ahead": int(ahead_raw) if ahead_raw.isdigit() else None,
        "policy": "本地攒批未推送；推送仅经用户显式窗口指示（人工 STOP 门）",
    }


def _iter_dir_files(src_base: Path, exclude: tuple[str, ...] = ()):
    """H-11 同口径安全 walk + 显式排除子树（锁内适配，仅此一处）。

    - os.walk(followlinks=False)：任何符号链接目录都不下降；
    - **未声明排除**的符号链接（目录或文件）一律 FATAL——与
      ``M1._iter_copy_files`` 完全同口径，快照/清单不得混入链接实体；
    - 声明在 ``exclude`` 里的相对子树直接摘除、其链接只登记（返回第二项），
      用于 ``walls/t5_freeze/gate_env/pyshim``（指向只读 tour 源的绝对链接，
      属环境接线而非证据实体）。
    """
    files: list[Path] = []
    excluded: list[dict] = []
    exclude_set = set(exclude)
    for dirpath, dirnames, filenames in os.walk(src_base, followlinks=False):
        dpath = Path(dirpath)
        rel_dir = dpath.relative_to(src_base)
        kept: list[str] = []
        for d in dirnames:
            if d in M1.SKIP_NAMES:
                continue
            p = dpath / d
            rel = (rel_dir / d).as_posix() if rel_dir.parts else d
            if rel in exclude_set:
                nested = _symlinks_under(p)
                for item in nested:
                    item["under_excluded"] = rel
                excluded.append({
                    "path": str(p), "relative_path": rel,
                    "target": os.readlink(p) if p.is_symlink() else None,
                    "kind": "dir", "copied": False,
                    "reason": "显式排除的环境接线子树（非证据实体）",
                    "symlinks_inside": nested})
                continue
            if p.is_symlink():
                raise SystemExit(
                    f"证据树含符号链接目录，拒绝打包（H-11）：{p} -> "
                    f"{os.readlink(p)}")
            kept.append(d)
        dirnames[:] = kept
        for name in filenames:
            if name in M1.SKIP_NAMES:
                continue
            p = dpath / name
            if p.is_symlink():
                raise SystemExit(
                    f"证据源为符号链接文件，拒绝打包（H-11）：{p} -> "
                    f"{os.readlink(p)}")
            files.append(p)
    return files, excluded


def build_pack(*, p25: Path, runs25: Path, repo: Path, p1: Path, pack: Path,
               lance_uri: str, clean: bool = False,
               proc_root: str = "/proc") -> dict:
    m = M1
    m.PACK = pack          # 老 helper 经全局取包根（重绑，不改文件）
    m.PHASE1_ROOT = p1
    roots = {P25: p25, R25: runs25, REPO: repo, P1: p1}

    # 必需件预检（缺件带点名，fail-closed）
    for pack_rel, root_key, src_rel, logical, required in FILES:
        if not required:
            continue
        src = roots[root_key] / src_rel
        if not src.is_file():
            raise PackError(f"必需证据缺失：{src}（逻辑编号 {logical}）")
    # 必需目录同样在 --clean 删旧包之前预检（评审 LOW-1：不留「旧包已删、
    # 新包未成」的空窗）；符号链接/密钥/超限只能在复制后判，属已知残余风险。
    for (_pfx, root_key, src_dir, logical, required, _ex) in DIR_COPIES:
        if not required:
            continue
        src_base = roots[root_key] / src_dir
        if not src_base.exists():
            raise PackError(f"必需证据目录缺失：{src_base}（逻辑编号 {logical}）")

    if clean:
        guarded = [Path(lance_uri), pack]
        holders = M2._processes_holding_paths(
            guarded, proc_root=proc_root, exclude_pid=os.getpid())
        if holders:
            lines = ["检测到进程持有活库/证据目录句柄，拒绝 --clean 重建"
                     "（重建期不与写入者抢活库）："]
            for h in holders:
                lines.append(f"  pid={h['pid']} fd={h['fd']} "
                             f"path={h['open_path']} cmd={h['cmdline'] or '?'}")
            lines += [
                "处置：先停掉持句柄的 simpraxis 自有进程（或等其正常退出；"
                "不擅杀他方 GPU 进程），确认后重跑：",
                "  python scripts/phase25/build_evidence_pack_phase25.py --clean",
                "本命令不会强制删除、不会跳过句柄检查。"]
            raise PackError("\n".join(lines))
        pack_res = pack.resolve(strict=False)
        p25_res = p25.resolve()
        if pack_res != p25_res and not str(pack_res).startswith(
                str(p25_res) + os.sep):
            raise PackError(f"拒绝删除：包目录不在 phase25 根内：{pack}")
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

    # 目录复制（H-11 安全 walk；显式排除子树只登记不复制）
    symlinks_excluded: list[dict] = []
    for (pack_prefix, root_key, src_dir, logical, required,
         exclude) in DIR_COPIES:
        src_base = roots[root_key] / src_dir
        if not src_base.exists():
            if required:
                raise PackError(f"必需证据目录缺失：{src_base}")
            continue
        if src_base.is_symlink():
            raise PackError(f"证据清单根为符号链接，拒绝打包：{src_base}")
        files, skipped = _iter_dir_files(src_base, exclude)
        symlinks_excluded.extend(skipped)
        for f in sorted(files):
            rel = f.relative_to(src_base)
            m._copy_file(f, pack / pack_prefix / rel, logical, True,
                         root_key, manifest_files)

    leaked = [str(p.relative_to(pack)) for p in pack.rglob("*")
              if p.is_symlink()]
    if leaked:
        raise PackError(f"证据包混入符号链接：{leaked}")

    # ---- 派生只读视图（写包后登记）------------------------------------
    def write_derived(rel: str, obj, logical: str, origin: Path) -> None:
        dst = pack / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8")
        m._add_manifest(manifest_files, dst, origin, "derived", logical, True)

    # approvals：原件不入包，只入 metadata+sha256 索引（沿用 m1 规则）
    idx = []
    appr_dir = p25 / "quota" / "approvals"
    for f in sorted(appr_dir.glob("*.json")):
        rec = json.loads(f.read_text(encoding="utf-8"))
        idx.append({"file": f.name,
                    "sha256": m._sha256(f),
                    "batch_id": rec.get("batch_id"),
                    "approved": rec.get("approved"),
                    "task": rec.get("task"),
                    "ledger_phase": rec.get("ledger_phase")})
    write_derived("quota/approvals_index.json",
                  {"note": "批准件原件不入包（m1 规则），此处为 sha256 索引",
                   "batches": idx}, "M25.$ .2",
                  appr_dir)

    # 数据层备份指针（不打包，>10MB 且属闭源数据层）
    backup = p25 / WINDOW / "lancedb_prewindow_backup"
    write_derived("task6_stage_b/window/data_layer_backup_pointer.json",
                  {"path": str(backup),
                   "in_pack": False,
                   "reason": ("窗前 LanceDB 实体备份（数据层闭源、>200MB）；"
                              "证据口径由 prewindow_snapshot.json 与 "
                              "pre_vector_identity.json 承担，实体不入包。"),
                   "exists": backup.exists()}, "M25.T6.9", backup)

    # 显式排除的符号链接（只登记不复制；H-11 事实留痕）
    write_derived("symlinks_excluded.json",
                  {"note": ("H-11：符号链接实体永不入包。此处为按 DIR_COPIES "
                            "显式排除的环境接线子树——"
                            "walls/t5_freeze/gate_env/pyshim/libero 是指向 "
                            "/opt/libero_tour/libero 的绝对链接（墙 A 只读 "
                            "tour 源；收尾时该挂载已不存在，链接为 broken），"
                            "属环境接线而非证据实体；墙 A 的证据在 "
                            "walls/t5_freeze/runs 与 lead_only 内。"),
                   "items": symlinks_excluded}, "M25.T5.2", p25 / "walls")

    # 收尾三件（对账/看板/机判）——若已生成则入包，缺则如实登记
    derived_specs = [
        ("closeout/closeout_reconcile.json", "closeout_reconcile.json",
         "M25.T10.1"),
        ("closeout/dashboard_phase25.json", "dashboard_phase25.json",
         "M25.T10.2"),
        ("closeout/closure_judge.json", "closure_judge.json", "M25.T10.2"),
    ]
    for pack_rel, name, logical in derived_specs:
        src = p25 / "task10_milestone" / name
        if src.is_file():
            m._copy_file(src, pack / pack_rel, logical, True, P25,
                         manifest_files)
        else:
            write_derived(pack_rel.replace(".json", "_MISSING.json"),
                          {"missing": str(src),
                           "note": "收尾件尚未生成（构建顺序：对账→看板→"
                                   "机判→证据包终建）"},
                          logical, src)

    # AC-T10-1：收尾全套冷跑日志随包（命令口径/摘要/rc 三处可审，机判另读一遍
    # 冻结副本，不靠看板的二手转述）。缺则如实挂牌，不伪造绿件。
    suite_log = p25 / "task10_milestone" / "task10_fullsuite.log"
    if suite_log.is_file():
        m._copy_file(suite_log,
                     pack / "task10_closeout/task10_fullsuite.log",
                     "M25.T10.1", True, P25, manifest_files)
    else:
        write_derived("task10_closeout/task10_fullsuite_MISSING.json",
                      {"missing": str(suite_log),
                       "note": ("收尾两排除全套冷跑日志尚未生成；机判要求包内"
                                "存在该日志且 `# cmd:` 含两排除项、rc=0。")},
                      "M25.T10.1", suite_log)

    # T7/T8/T9：用户 2026-11-02 裁决不再进行 → 无证据实体。AC-T10-4 点名的
    # 「drill 报告 / T9 战役证据」因此不存在；此处如实挂牌，不用空目录或
    # 占位产物冒充已执行（硬规则 7）。
    not_executed = {
        "task7_stages": ("Task 7 stages[] 多阶段状态机",
                         ["roadmap 契约测试件", "kill/resume 录证",
                          "历史 golden（T7-2）", "脱敏 fixture"]),
        "task8_drill": ("Task 8 飞书富卡 + 真人 drill",
                        ["drill_report", "最大卡快照（字节帽 T8-5/9）",
                         "P2P 降级法录证（T8-8）"]),
        "task9_vla": ("Task 9 VLA LIBERO 多阶段战役",
                      ["三档 venv 判决（T9-1/11）", "战役证据包 + Gate 记录",
                       "负结果落库行（T9-12）"]),
    }
    for slug, (title, would_include) in not_executed.items():
        write_derived(
            f"{slug}/NOT_EXECUTED.json",
            {"task": title,
             "status": "stopped_by_user",
             "adjudication": ("用户 2026-11-02 裁决「Task 6 Stage B确认立项，"
                              "Task7/8/9不再继续进行」"),
             "evidence_present": False,
             "would_have_included": would_include,
             "note": ("AC-T10-4 点名该证据项；任务未执行故无实体。本件替代"
                      "空目录/占位产物，机判据此登记 provenance gap 而非"
                      "记为通过。")},
            "M25.T10.3", p25 / "task10_milestone")

    # 三签件若尚未产出，同样如实挂牌（不伪装已签）。
    signs_dir = p25 / "task10_milestone" / "signs"
    if not signs_dir.is_dir():
        write_derived("task10_closeout/signs_MISSING.json",
                      {"missing": str(signs_dir),
                       "note": ("三签（executor/reviewer/verifier）尚未落件；"
                                "构建顺序为 机判→三签→终建包，终建包时本件"
                                "应消失并被 signs/*.json 取代。")},
                      "M25.T10.3", signs_dir)

    # 追溯矩阵与适配说明（派生件，逐件登记进 manifest）
    write_derived("traceability_matrix.json",
                  {"schema": "simpraxis.traceability_matrix.v1",
                   "milestone": MILESTONE, "items": TRACEABILITY},
                  "M25.T10.1", pack)
    (pack / "ADAPTATIONS.md").write_text(ADAPTATIONS, encoding="utf-8")
    manifest_files.append({
        "path": "ADAPTATIONS.md",
        "sha256": M1._sha256(pack / "ADAPTATIONS.md"),
        "size": (pack / "ADAPTATIONS.md").stat().st_size,
        "origin": "derived", "logical_ids": ["M25.T10.1"],
    })

    manifest = {
        "schema": SCHEMA,
        "milestone": MILESTONE,
        "git_commit": M1._git_head(),
        "generated_at_utc": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"),
        "roots": {k: str(v) for k, v in roots.items()},
        "evidence_roots": EVIDENCE_ROOTS,
        "replay_commands": [
            "python scripts/phase25/closeout_reconcile.py",
            "python scripts/phase25/build_dashboard_phase25.py "
            "--tests-result <cold-run.log>",
            "python scripts/phase25/build_evidence_pack_phase25.py --clean",
            "python scripts/verification/check_phase25_closure.py "
            "--report <root>/task10_milestone/closure_judge.json",
        ],
        "push_state": _push_state(repo),
        "files": sorted(manifest_files, key=lambda r: r["path"]),
        "n_files": len(manifest_files),
        "total_bytes": sum(int(r["size"]) for r in manifest_files),
        "secrets_scan_hits": M1._scan_secrets(pack),
        "policy": {
            "approvals_originals_in_pack": False,
            "symlinks_in_pack": False,
            "symlinks_excluded_registered":
                len(symlinks_excluded),
            "data_layer_in_pack": False,
            "max_file_bytes": MAX_FILE_BYTES,
        },
    }
    if manifest["secrets_scan_hits"]:
        raise PackError(
            f"证据包命中密钥正则：{manifest['secrets_scan_hits'][:5]}")
    big = [r["path"] for r in manifest["files"]
           if int(r["size"]) > MAX_FILE_BYTES]
    if big:
        raise PackError(f"包内 >10MB 件：{big}")
    # 权限归一（AC-T10-4）在全部写入之后执行，再把计数回填 manifest。
    modes = _normalize_modes(pack)
    manifest["policy"]["modes"] = {"dirs": "0700", "files": "0600", **modes}
    manifest["n_files"] = len(manifest["files"])
    manifest["total_bytes"] = sum(int(r["size"]) for r in manifest["files"])
    (pack / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    os.chmod(pack / "manifest.json", 0o600)
    return manifest


ADAPTATIONS = """# Phase 2.5 证据包适配说明

1. **approvals 原件不入包**（沿用 Milestone 1 规则）：`quota/approvals_index.json`
   记 sha256 与批准数；原件在 `$SIMPRAXIS_PHASE25_ROOT/quota/approvals/`。
2. **数据层不打包**：`task6_r4_window/<ts>/lancedb_prewindow_backup` 为窗前
   LanceDB 实体备份（>200MB、数据层闭源），只入指针件；四表身份由
   `prewindow_snapshot.json` + `pre_vector_identity.json` 承担。
3. **窗口快照去重**：`postwindow_snapshot.json` 与 `restore_verify_snapshot.json`
   未入包——前者是绑定跑写入现场（已在旁置目录
   `data.postwindow_20261102T051251Z` 保全），后者与 `prewindow_snapshot.json`
   **逐字节相同**（sha `9228269a…`，见窗口日志 [14]），重复入包只增加体积。
4. **T6 Stage A（换嵌入）**：其窗口证据在 `task6_recall/` 下按原文件名保留；
   回滚后的嵌入配置以仓库 `memory_flywheel/embedding_config.json` 为准（mpnet/
   768），包内不含活库实体。
5. **T7/T8/T9 未执行**：用户 2026-11-02 裁决「Task7/8/9 不再继续进行」，故
   本包无其实跑证据；`interval_t9.json` 原样保留，不回收、不据此改区间。
6. **证据根登记**：`@runs/phase25/<thread>/stage_<id>/` 已在 `manifest.json`
   的 `evidence_roots` 注册（解析基准 `$SIMPRAXIS_RUN_DIR` + 子目录
   `phase25/`，当前 0 行使用）；本里程碑不改 `learning_store` 的白名单。
7. **符号链接只登记不复制**（H-11）：`walls/t5_freeze/gate_env/pyshim/libero`
   是指向 `/opt/libero_tour/libero` 的**绝对且已 broken** 链接（墙 A 只读
   tour 源，收尾时挂载已不在），按 DIR_COPIES 显式排除并全量登记在
   `symlinks_excluded.json`；包内符号链接恒 0。
8. **权限归一**：全部复制完成后统一目录 0700 / 文件 0600（不继承来源处
   0664/0755 的余数），计数回填 `manifest.policy.modes`。
9. **包内无 >10MB 件**、密钥双扫 0 命中；活 LanceDB 只以快照与指针件入包。
"""


def main(argv: "list[str] | None" = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--clean", action="store_true")
    ap.add_argument("--pack", type=Path, default=None)
    ap.add_argument("--proc-root", default="/proc")
    args = ap.parse_args(argv)
    p25 = phase25_root()
    pack = args.pack or (p25 / "task10_milestone" / "evidence_pack")
    try:
        man = build_pack(p25=p25, runs25=runs_root() / "phase25",
                         repo=_REPO, p1=phase1_root(), pack=pack,
                         lance_uri=default_lancedb_uri(), clean=args.clean,
                         proc_root=args.proc_root)
    except PackError as exc:
        print(f"[PACK-FAIL] {exc}", file=sys.stderr)
        return 2
    print(f"pack {pack} files={man['n_files']} "
          f"bytes={man['total_bytes']} commit={man['git_commit'][:12]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
