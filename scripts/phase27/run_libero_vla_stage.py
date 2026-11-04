#!/usr/bin/env python3
"""LIBERO VLA Stage 执行器（S1/S2/S3）——Phase 2.7 Task 6 / 票 `P27-B2`（≤85 呼）。

三个 Stage 走**同一条路线图**（`GraphDeps` 三接缝 + 真飞书双 Gate）：

  S1 `s1_baseline`     官方环境 + 零动作基线 → 1080P MP4 + telemetry + Rung1~3 读数
  S2 `s2_main_policy`  预注册顺序里**首个可达** VLA → n=10 成功率 + Wilson 95%
  S3 `s3_control_policy` 对照策略（不同形态的另一个 VLA）→ n=10 同上

用法（计划 §T6.4-B 的字面形态；`--stage` 取 `s1|s2|s3|all`，缺省 `all`）：

    python -m scripts.phase27.run_libero_vla_stage --stage s1 --batch P27-B2
    python -m scripts.phase27.run_libero_vla_stage --stage s2 --policy octo-small --batch P27-B2
    python -m scripts.phase27.run_libero_vla_stage --stage s3 --policy openvla-7b --batch P27-B2

锁内适配（登记于 `~/文档/Phase_2.7_Plan_Complete.md` §7，判据强度不降）：

1. **单进程跑完 s1→s3 的整条路线图**（计划 CLI 示例是三次独立调用）。原因：
   LangGraph 0.2.28 的「跨进程断点续跑」是计划 B.3 明列的已知未知；一次进程内
   走完整条路，Gate 1 只批一次、Stage 推进只发生一次，证据面反而更干净。
   `--stage` 仍可用来只跑到某个 Stage（调试/分次），语义是「跑到该 Stage 为止」。
2. **Rung3 在 LIBERO 上记 `not_applicable`**：LIBERO 官方 `control_freq=20`
   ⇒ `dt=0.05 s`，落在冻结判据的锚定带 `[1/120, 1/30]` **之外**；
   `orchestrator/judge/rungs.py::rung3_check` 自己声明带外「保守判负」。
   本脚本照实记 `dt_band_ok=False` → `not_applicable`，并把原始
   `jerk_max_mps3` 逐 episode 报出；**任何文档不得声称 Rung3 通过**。
3. **Rung2 的目标子项**：LIBERO 没有 `goal_box`，按预注册 §8 改由官方
   `env.check_success()` 判定。零动作基线（S1）按构造不可能达成目标 ⇒ 目标子项
   对 S1 **报数不判分**（计分等于要求基线成功）；S2/S3 计分。
4. **一次容器进程跑完整批 episode**：模型（Octo/OpenVLA）整批只加载一次，
   否则 10 episode × 冷启动会把时间全烧在模型加载上（0 付费但是小时级）。

纪律：
- **No Fake Approval**：Gate 1（路线图核准）与 Gate 2（逐 Stage 终审）都是真飞书
  卡片真人点击；脚本里没有任何「等太久就代点」的分支，超时只抛 `TimeoutError`。
- **付费只走网关**：每 Stage 采集完后用 `coding_debug` 做**失败归因 / 报告叙述**
  （每 Stage 1–3 次），全部落在 `QuotaLedger.phase27()` 的 `P27-B2` 票下；
  容器内 VLA 推理**不产生**网关付费调用。
- **证据与视频分离**（D6-6）：MP4 落本 Stage 的 `videos/`（`/mnt`），
  证据包只收 `video_manifest.json`（每视频的 sha256/字节数/分辨率/帧数/抽帧哈希）。
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
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from langgraph.checkpoint.sqlite import SqliteSaver  # noqa: E402

from orchestrator.common.clock import utc_now_iso_z  # noqa: E402
from orchestrator.common.fsutil import atomic_write_text  # noqa: E402
from orchestrator.common.runtime_paths import phase27_root  # noqa: E402
from orchestrator.feishu_gateway.cards import (  # noqa: E402
    build_roadmap_card,
    build_stage_gate_card,
)
from orchestrator.feishu_gateway.client import gateway_from_env  # noqa: E402
from orchestrator.gateway.quota_ledger import (  # noqa: E402
    PHASE27_TASK_CAPS,
    QuotaLedger,
    billing_context,
    read_lines,
    write_approval,
)
from orchestrator.gateway.router import ModelGateway  # noqa: E402
from orchestrator.graph.nodes import GraphDeps, build_graph  # noqa: E402
from orchestrator.graph.roadmap import (  # noqa: E402
    ROADMAP_SCHEMA,
    stage_statuses,
    stage_to_mapping,
    validate_roadmap,
)
from orchestrator.judge.rungs import rung1_check, rung3_check  # noqa: E402

TASK_ID = 6
BATCH = "P27-B2"
TASK_CAP = int(PHASE27_TASK_CAPS[TASK_ID])
# 计划 §T6.5 的三段子帽（只由本脚本强制；**不进票**——拆票会
# BLOCKED_BATCH_APPROVAL_MISMATCH，与 P27-B1/B1b 的既有口径一致）。
SUBCAP = {"s1": 15, "s2": 40, "s3": 30}
THREAD_ID = "p27-t6-vla-001"
TASK_TITLE = "Phase 2.7 Task 6 LIBERO VLA 多 Stage 战役（P27-B2，≤85 呼）"

IMAGE = "sim-praxis/libero:task6"
HF_CACHE = Path("/mnt/caches/hf")
GLOBAL_GRANTS = ("run:docker", "read_file", "list_dir", "write:")

# 预注册 §5：策略选择顺序（钉死，不得开跑后改序）。
STRATEGY_ORDER = ["octo-small", "openvla-7b-int4", "openvla-7b-fp16"]
# 顺序项 → 容器内 `--policy` 形态（openvla 的 4bit/fp16 是同一形态 + 环境开关）。
POLICY_FORM = {"octo-small": "octo-small", "openvla-7b-int4": "openvla-7b",
               "openvla-7b-fp16": "openvla-7b"}
POLICY_4BIT = {"openvla-7b-int4": "1", "openvla-7b-fp16": "0"}

STAGE_IDS = ("s1_baseline", "s2_main_policy", "s3_control_policy")
STAGE_OF = {"s1": "s1_baseline", "s2": "s2_main_policy", "s3": "s3_control_policy"}

# 预注册 §1/§2：5 任务 × 2 初态 = 10 episode；seed = 1000 + 10*task + init。
EPISODES = [{"task_index": t, "init_index": i, "seed": 1000 + 10 * t + i}
            for t in range(5) for i in (0, 1)]

ROADMAP_DOC: dict[str, Any] = {
    "schema": ROADMAP_SCHEMA,
    "title": "LIBERO VLA 三 Stage 战役：官方基线 → 主策略 → 对照策略",
    "stages": [
        {
            "stage_id": "s1_baseline",
            "title": "官方基线：环境出生 + 离屏 1080P + Rung 读数",
            "objective": ("在官方 LIBERO 环境上跑零动作基线，证明环境/渲染/遥测/判定"
                          "链路可用；不追求任务成功（D6-5：不写新策略）"),
            "exit_ac": ["AC-S1-1 10 个 episode 的 MP4 可解码且为 1920×1080",
                        "AC-S1-2 telemetry 行数 > 0 且逐行可解析、数值有限",
                        "AC-S1-3 Rung1 全绿、Rung2 接触子项绿、Rung3 出读数（带外 not_applicable）"],
            "tool_grants": ["run:docker", "read_file", "write:"],
            "max_rounds": 8,
        },
        {
            "stage_id": "s2_main_policy",
            "title": "主策略：预注册顺序里首个可达 VLA",
            "objective": ("按 s0/vla_candidates.json 的候选与预注册顺序，取首个**可达**的 VLA "
                          "跑 n=10，给出成功率 + Wilson 95% 区间"),
            "exit_ac": ["AC-S2-1 主策略启动成功（权重加载 + 首帧推理）",
                        "AC-S2-2 10 个 episode 完整记录（seed/success/steps/vram_mib）",
                        "AC-S2-3 至少 1 次成功，或给出逐条失败归因"],
            "tool_grants": ["run:docker", "read_file", "write:"],
            "max_rounds": 8,
        },
        {
            "stage_id": "s3_control_policy",
            "title": "对照策略：另一形态 VLA（同任务同种子）",
            "objective": "同任务同种子跑对照策略，形成成功率对照矩阵；降级必须留 degradation 条目",
            "exit_ac": ["AC-S3-1 对照策略启动成功",
                        "AC-S3-2 10 个 episode 完整记录",
                        "AC-S3-3 与主策略同任务同种子可比（差异仅策略形态）"],
            "tool_grants": ["run:docker", "read_file", "list_dir"],
            "max_rounds": 8,
        },
    ],
}


# ------------------------------------------------------------------ 基础工具

def campaign_root() -> Path:
    root = phase27_root() / "task6_vla"
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    return root


def _write_json(path: Path, payload: Any, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))
    os.chmod(path, mode)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ensure_batch_approval() -> Path:
    """`P27-B2` 批次批准件（硬规则 2：批次件落 `quota/approvals/`）。

    幂等：已存在则原样返回并核对任务/额度；不存在则按计划 §T6.5 的 85 呼信封写。
    """
    path = phase27_root() / "quota" / "approvals" / f"{BATCH}.json"
    if path.is_file():
        cur = json.loads(path.read_text(encoding="utf-8"))
        if int(cur.get("task", -1)) != TASK_ID or int(cur.get("approved", -1)) != TASK_CAP:
            raise SystemExit(f"{path} 与本次任务/额度不符：{cur}")
        return path
    return write_approval(
        batch_id=BATCH, task=TASK_ID, stage="coding_debug", approved=TASK_CAP,
        beyond_normal=False,
        note=("Phase 2.7 Task6 LIBERO VLA 三 Stage 信封：S1 基线 15 / S2 主策略 40 / "
              "S3 对照 30 = 85（子帽只由 run_libero_vla_stage.py 强制，不进票）。"),
        namespace=QuotaLedger.phase27().ns,
    )


def batch_spend() -> dict[str, int]:
    """本票已花实呼（`event="call"` 行；`seq=null` 的 blocked 行不占实呼）。"""
    rows = read_lines(QuotaLedger.phase27().ns.ledger_path_fn())
    mine = [r for r in rows if r.get("batch_id") == BATCH]
    by_stage: dict[str, int] = {}
    for r in mine:
        if r.get("event") != "call":
            continue
        key = str(r.get("thread_id") or "")
        for sid, st in STAGE_OF.items():
            if sid in key:
                by_stage[st] = by_stage.get(st, 0) + 1
    return {"total": sum(1 for r in mine if r.get("event") == "call"),
            **by_stage}


# ------------------------------------------------------------------ 容器执行

RUNNER_IN_CONTAINER = "/workspace/scripts/phase27/libero_episode_runner.py"


def _docker_common(stage_dir: Path) -> list[str]:
    return [
        "docker", "run", "--rm", "--gpus", "all", "--ipc=host",
        "--user", f"{os.getuid()}:{os.getgid()}",
        "-e", "MUJOCO_GL=egl", "-e", "PYOPENGL_PLATFORM=egl",
        # 适配（2026-11-04）：HF_HOME 必须是**容器内**路径（挂载点 /hf-cache），
        # 不能直传宿主路径 /mnt/caches/hf——容器里没有这棵目录树，且 --user 下
        # 建不了 /mnt，会把权重下载打成 PermissionError（基础设施假失败）。
        "-e", "HF_HOME=/hf-cache", "-e", "HF_HUB_CACHE=/hf-cache/hub",
        "-e", "HF_ENDPOINT=https://hf-mirror.com",
        # 适配（2026-11-04）：关掉 Xet 传输。实测容器**直连**外网会被
        # `hf-mirror.com` 限流（429），带宽从 10 MB/s 塌到 ~65 KB/s 并长时间
        # 零增长（xet 客户端日志反复 "connection struggling"、37 分钟只落
        # 6 GB / 15 GB）。容器内到不了宿主的本地代理（代理只监听
        # 127.0.0.1:10900，容器里那个地址是容器自己的回环），所以权重改为
        # **宿主机侧预热**进 /mnt/caches/hf（宿主走代理 ~16 MB/s，见
        # rerun 记录），容器只读缓存；这里同时关掉 Xet，避免缓存未命中时
        # 又退回慢路径。
        "-e", "HF_HUB_DISABLE_XET=1",
        "-e", "PYTHONUNBUFFERED=1",
        "-v", f"{REPO_ROOT}:/workspace:ro",
        "-v", f"{stage_dir}:/runs",
        "-v", f"{HF_CACHE}:/hf-cache",
    ]


def _docker_cmd(policy_form: str, spec_path: Path, stage_dir: Path,
                extra_env: dict[str, str]) -> list[str]:
    cmd = _docker_common(stage_dir)
    for k, v in extra_env.items():
        cmd += ["-e", f"{k}={v}"]
    cmd += [
        IMAGE,
        "python3", RUNNER_IN_CONTAINER,
        "--policy", policy_form,
        "--spec", f"/runs/{spec_path.name}",
        "--out-dir", "/runs",
    ]
    return cmd


def run_smoke(stage_dir: Path, *, steps: int = 3,
              timeout_s: float = 1800.0) -> dict[str, Any]:
    """S1 预检：单 episode / 3 控制步 / 零动作，证明镜像在**运行期**真能起
    环境 + 渲染 + 落遥测。失败 ⇒ S1 判 blocked，不再烧 10 个 episode
    （2026-11-04 首轮就是「Gate 已点、批量全废」，预检把这类浪费挡住）。"""
    d = stage_dir / "_smoke"
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    cmd = _docker_common(d) + [
        IMAGE, "python3", RUNNER_IN_CONTAINER,
        "--policy", "zero", "--task-index", "0", "--init-index", "0",
        "--seed", "1000", "--max-steps", str(steps), "--out-dir", "/runs"]
    log_path = d / "smoke.log"
    t0 = time.time()
    with log_path.open("w", encoding="utf-8") as log:
        try:
            rc = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT,
                                timeout=timeout_s, check=False).returncode
        except subprocess.TimeoutExpired:
            rc = -9
            log.write(f"\n[TIMEOUT after {timeout_s}s]\n")
    out_json = d / "episodes_zero.json"
    results = (json.loads(out_json.read_text(encoding="utf-8"))
               if out_json.is_file() else [])
    ok = bool(rc == 0 and results and all(
        ((it.get("summary") or {}).get("steps") or 0) == steps
        and (it.get("summary") or {}).get("error") is None for it in results))
    return {"ok": ok, "returncode": rc, "log": str(log_path),
            "dir": str(d), "steps": steps,
            "wallclock_s": round(time.time() - t0, 3),
            "results": results, "cmd": cmd}


def run_batch(stage_dir: Path, policy_form: str, *,
              extra_env: dict[str, str] | None = None,
              timeout_s: float = 7200.0) -> dict[str, Any]:
    """跑一批（10 episode）。返回 {ok, returncode, results, log, cmd}。"""
    stage_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(stage_dir, 0o700)
    spec_path = stage_dir / "episodes_spec.json"
    _write_json(spec_path, EPISODES)
    env = dict(extra_env or {})
    cmd = _docker_cmd(policy_form, spec_path, stage_dir, env)
    log_path = stage_dir / f"container_{policy_form}.log"
    t0 = time.time()
    with log_path.open("w", encoding="utf-8") as log:
        try:
            proc = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT,
                                  timeout=timeout_s, check=False)
            rc = proc.returncode
        except subprocess.TimeoutExpired:
            rc = -9
            log.write(f"\n[TIMEOUT after {timeout_s}s]\n")
    results: list[dict[str, Any]] = []
    out_json = stage_dir / f"episodes_{policy_form}.json"
    if out_json.is_file():
        results = json.loads(out_json.read_text(encoding="utf-8"))
    return {"ok": rc == 0 and bool(results), "returncode": rc,
            "results": results, "log": str(log_path),
            "wallclock_s": round(time.time() - t0, 3),
            "cmd": cmd, "spec": str(spec_path)}


# ------------------------------------------------------------------ 判定

def wilson(k: int, n: int, z: float = 1.959963984540054) -> dict[str, float]:
    """Wilson score 95% 区间（预注册 §4；端点与机判重算一致 < 1e-6）。"""
    if n <= 0:
        raise ValueError("n 必须为正")
    p = k / n
    denom = 1.0 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = (z / denom) * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5)
    return {"k": k, "n": n, "p_hat": round(p, 9),
            "lower": round(max(0.0, center - half), 9),
            "upper": round(min(1.0, center + half), 9),
            "method": "wilson", "z": z}


def judge_tracks(rec: dict[str, Any], control_dt: float) -> dict[str, Any]:
    """把一批容器 episode 的原始时序送进**冻结**判据（Rung1/Rung3 直接调用）。

    Rung2 的 LIBERO 映射按预注册 §8：目标子项 = 官方 `check_success()`（容器侧
    `success` 字段，逐帧视作末帧读数），接触子项 = `ncon>0` 的最长连续段。
    """
    out: list[dict[str, Any]] = []
    for item in rec.get("results") or []:
        s = item.get("summary") or {}
        tr = item.get("tracks") or {}
        pen = tr.get("pen") or []
        ee = tr.get("ee") or []
        active = tr.get("active") or []
        data = {
            "penetrations": pen,
            "ee_positions": ee,
            "contact_active": active,
            "contact_forces": tr.get("force") or [],
            "object_poses": tr.get("obj") or [],
            "dt": control_dt,
            "engine": "libero",
            "run_id": f"{s.get('policy')}-T{(s.get('task_index') or 0) + 1}-{s.get('init_index')}",
        }
        r1 = rung1_check(data) if pen else {"pass": False,
                                            "checks": {"reason": "无 telemetry（容器失败）"}}
        r3 = rung3_check(data) if len(ee) >= 6 else {
            "pass": False, "checks": {"reason": "帧数不足 6（无法三阶差分）"}}
        # Rung2（LIBERO 映射）
        run = 0
        best = 0
        for v in active:
            run = run + 1 if v else 0
            best = max(best, run)
        contact_s = round(best * control_dt, 6)
        goal = bool(s.get("success"))
        dt_ok = bool((r3.get("checks") or {}).get("dt_band_ok"))
        # 锁内适配 #2：`rung3_check` 带外直接判负、**不计算** jerk，因此冻结判据
        # 给不出 `jerk_max_mps3`。预注册 §8 要求「逐 episode 报原始值」，这里用
        # 同一三阶差分公式在带外**报数**（`_reported` 后缀 + 明确标注不计分），
        # 不冒充判据输出、不据此判通过。
        jerk_reported = None
        if not dt_ok and len(ee) >= 4:
            import numpy as np
            arr = np.asarray([row[:3] for row in ee if len(row) >= 3], dtype=float)
            if arr.shape[0] >= 4:
                n = arr.shape[0]
                d3 = np.zeros_like(arr)
                i = np.arange(2, n - 2)
                d3[2:n - 2] = (arr[i + 2] - 2 * arr[i + 1] + 2 * arr[i - 1]
                               - arr[i - 2]) / (2.0 * control_dt ** 3)
                d3[0] = (arr[3] - 3 * arr[2] + 3 * arr[1] - arr[0]) / control_dt ** 3
                if n >= 5:
                    d3[1] = (arr[4] - 3 * arr[3] + 3 * arr[2] - arr[1]) / control_dt ** 3
                    d3[n - 2] = (arr[n - 2] - 3 * arr[n - 3] + 3 * arr[n - 4]
                                 - arr[n - 5]) / control_dt ** 3
                d3[n - 1] = (arr[n - 1] - 3 * arr[n - 2] + 3 * arr[n - 3]
                             - arr[n - 4]) / control_dt ** 3
                jerk_reported = round(float(np.linalg.norm(d3, axis=1).max()), 6)
        out.append({
            "run_id": data["run_id"],
            "policy": s.get("policy"),
            "task_index": s.get("task_index"),
            "init_index": s.get("init_index"),
            "seed": s.get("seed"),
            "steps": s.get("steps"),
            "success": goal,
            "vram_mib": s.get("vram_mib"),
            "unnorm_key_used": s.get("unnorm_key_used"),
            "unnorm_key_note": s.get("unnorm_key_note"),
            "error": s.get("error"),
            "video": s.get("video"),
            "telemetry": s.get("telemetry"),
            "rung1": r1,
            "rung2": {
                "goal_via_official_check_success": goal,
                "contact_longest_run_s": contact_s,
                "contact_active_ratio": round(sum(1 for v in active if v) / len(active), 6)
                if active else 0.0,
                "pass": contact_s >= 1.0,
            },
            "rung3": {
                "pass": r3.get("pass") if dt_ok else None,
                "not_applicable": (not dt_ok),
                "dt": control_dt,
                "detail": r3.get("checks"),
                # 带外报数，不计分（见上方锁内适配 #2 注释）
                "jerk_max_mps3_reported": jerk_reported,
                "jerk_reported_scored": False,
            },
        })
    return {"episodes": out,
            "rung1_all_pass": all(e["rung1"].get("pass") for e in out) if out else False,
            "rung2_contact_all_pass": all(e["rung2"]["pass"] for e in out) if out else False,
            "rung3_dt_band_ok": all(
                not e["rung3"]["not_applicable"] for e in out) if out else False,
            # 带外时冻结判据不产 `over_threshold_count`，记 None 而不是 0——
            # 0 会被误读成「零次超阈」，那是假读数（No Fake Numbers）。
            "rung3_over_threshold_total": (
                None if any(e["rung3"]["not_applicable"] for e in out) else
                sum(int(((e["rung3"].get("detail") or {})
                         .get("over_threshold_count") or 0)) for e in out)),
            "control_dt": control_dt}


def video_manifest(stage_dir: Path) -> dict[str, Any]:
    """视频清单：sha256/字节/分辨率/帧数（用 ffprobe，可独立复算）。"""
    rows = []
    vids = sorted((stage_dir / "videos").glob("*.mp4")) if (stage_dir / "videos").is_dir() else []
    for v in vids:
        info: dict[str, Any] = {"name": v.name, "bytes": v.stat().st_size,
                                "sha256": _sha256_file(v)}
        try:
            proc = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=width,height,nb_frames,duration",
                 "-of", "json", str(v)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
            if proc.returncode == 0:
                st = (json.loads(proc.stdout.decode() or "{}").get("streams") or [{}])[0]
                nb = int(st.get("nb_frames") or 0) \
                    if str(st.get("nb_frames") or "").isdigit() else None
                if not nb:
                    # 有些封装不写 nb_frames，逐帧实数是唯一可信口径（不拿时长估算）
                    try:
                        cnt = subprocess.run(
                            ["ffprobe", "-v", "error", "-select_streams", "v:0",
                             "-count_frames", "-show_entries", "stream=nb_read_frames",
                             "-of", "json", str(v)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            timeout=300)
                        if cnt.returncode == 0:
                            cs = (json.loads(cnt.stdout.decode() or "{}")
                                  .get("streams") or [{}])[0]
                            nbr = str(cs.get("nb_read_frames") or "")
                            nb = int(nbr) if nbr.isdigit() else None
                    except (FileNotFoundError, subprocess.TimeoutExpired):
                        nb = None
                info.update({"width": st.get("width"), "height": st.get("height"),
                             "nb_frames": nb,
                             "duration_s": float(st.get("duration") or 0.0) or None,
                             "decodable": True})
            else:
                info.update({"decodable": False,
                             "ffprobe_error": proc.stderr.decode()[:400]})
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            info.update({"decodable": False, "ffprobe_error": str(exc)})
        rows.append(info)
    return {"videos": rows, "count": len(rows)}


# ------------------------------------------------------------------ 付费分析

class PaidAnalyst:
    """每 Stage 的失败归因 / 报告叙述（唯一付费面，逐次记账）。"""

    def __init__(self, *, thread: str, evidence_dir: Path) -> None:
        self.ledger = QuotaLedger.phase27()
        self.gateway = ModelGateway(ledger=self.ledger)
        self.thread = thread
        self.evidence_dir = evidence_dir
        self.calls: list[dict[str, Any]] = []

    def ask(self, stage_key: str, system: str, user: str, *,
            max_tokens: int = 900, tag: str = "analysis") -> str:
        spend = batch_spend()
        used = spend.get("total", 0)
        if used >= TASK_CAP:
            raise RuntimeError(f"{BATCH} 已用 {used}/{TASK_CAP}，拒绝继续实呼（fail-closed）")
        # 子帽（预注册 §10：S1 15 / S2 40 / S3 30，只由本脚本强制，不进票）
        cap = SUBCAP.get(stage_key)
        if cap is not None and spend.get(STAGE_OF[stage_key], 0) >= cap:
            raise RuntimeError(
                f"{BATCH}/{stage_key} 已用 {spend.get(STAGE_OF[stage_key], 0)}/{cap}，"
                "拒绝继续实呼（子帽 fail-closed）")
        ref = f"@runs/phase27/{THREAD_ID}/{stage_key}/{tag}"
        with billing_context(task=TASK_ID, batch_id=BATCH,
                             thread_id=f"{THREAD_ID}/{stage_key}"):
            try:
                from orchestrator.gateway.quota_ledger import set_current_evidence
                set_current_evidence(ref)
            except Exception:  # noqa: BLE001 - 证据引用是可选装饰
                pass
            text, tokens = self.gateway.call(
                "coding_debug",
                [{"role": "system", "content": system},
                 {"role": "user", "content": user}],
                max_tokens=max_tokens)
        self.calls.append({"stage": stage_key, "tag": tag, "chars": len(text),
                           "tokens": list(tokens), "evidence": ref,
                           "at_utc": utc_now_iso_z()})
        # 文件名按 tag 命名（tag 自带 stage 前缀）；此前写成 `{stage}_{tag}`
        # 会产出 `s2_s2_attribution.json` 这种双前缀，机判按约定名取件。
        _write_json(self.evidence_dir / f"{tag}.json",
                    {"stage": stage_key, "tag": tag, "system": system,
                     "user": user, "response": text,
                     "tokens": list(tokens), "evidence": ref,
                     "at_utc": utc_now_iso_z()})
        return text


# ------------------------------------------------------------------ 飞书 Gate

class StageGate:
    """发卡 → 等真人点击 → 落三件互证（message_id / 回调时刻 / 检查点决策）。"""

    def __init__(self, gw, evidence: Path, *, max_wait: float) -> None:
        self.gw = gw
        self.evidence = evidence
        self.max_wait = max_wait
        self.records: list[dict[str, Any]] = []
        self.callbacks: list[dict[str, Any]] = []
        self.snapshots: list[dict[str, Any]] = []

    def on_card_action(self, info: dict[str, Any]) -> None:
        self.callbacks.append({"received_at_utc": utc_now_iso_z(),
                               "message_id": info.get("message_id"),
                               "gate": info.get("gate"),
                               "decided": info.get("decided"),
                               "decision": info.get("decision"),
                               "choice": info.get("choice"),
                               "reason": info.get("reason")})

    def ask(self, gate: int, card: dict, label: str) -> dict:
        snap = self.evidence / "card_snapshots" / f"gate{gate}_{label}_{int(time.time())}.json"
        snap.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(snap.parent, 0o700)
        _write_json(snap, card)
        self.gw._reopen_round(THREAD_ID, gate)
        sent = self.gw._send_interactive(self.gw.owner_open_id, card, card,
                                         form_required=True)
        if not sent.get("message_id"):
            raise RuntimeError(f"Gate{gate} 卡片发送失败：{sent}")
        self.gw.store.upsert_pending(THREAD_ID, gate,
                                     card_message_id=sent["message_id"],
                                     open_chat_id=sent.get("chat_id"),
                                     task_title=TASK_TITLE)
        row = {"gate": gate, "label": label, "message_id": sent["message_id"],
               "chat_id": sent.get("chat_id"), "sent_at_utc": utc_now_iso_z()}
        self.records.append(row)
        self.gw._event_for(THREAD_ID, gate)
        decision = self.gw._await_decision(THREAD_ID, gate, max_wait=self.max_wait)
        pend = self.gw.store.get_pending(THREAD_ID, gate) or {}
        row.update({"decision": decision.get("decision"),
                    "human_notes": decision.get("human_notes"),
                    "choice": decision.get("choice"),
                    "decided_at_utc": utc_now_iso_z(),
                    "pending_status": pend.get("status"),
                    "pending_source": pend.get("source")})
        self.snapshots.append(dict(row))
        _write_json(self.evidence / f"gate{gate}_{label}_record.json", row, 0o600)
        return decision


# ------------------------------------------------------------------ 图的三个接缝

def _checkpoint_state(db_path: Path, thread_id: str) -> dict:
    con = sqlite3.connect(str(db_path))
    try:
        rows = con.execute(
            "SELECT type, checkpoint FROM checkpoints "
            "WHERE thread_id = ? AND checkpoint_ns = '' "
            "ORDER BY checkpoint_id DESC LIMIT 1", (thread_id,)).fetchall()
    finally:
        con.close()
    if not rows:
        return {}
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
    type_, raw = rows[0]
    if isinstance(raw, str):
        raw = raw.encode()
    ckpt = JsonPlusSerializer().loads_typed((type_, raw))
    return dict(ckpt.get("channel_values") or {})


def _stream_campaign(compiled, up_to: str) -> dict:
    """驱动整条路线图；`up_to` 决定在哪个 Stage 的终审后收手。"""
    state = {
        "thread_id": THREAD_ID, "track": "track1",
        "question": "LIBERO VLA 三 Stage 战役（P27-B2）",
        "proposals": [], "chosen": {}, "gate1_rejects": 0, "agent_runs": [],
        "gate2_rejects": 0, "coding_round": 0, "events": [],
        "stages": [stage_to_mapping(s) for s in validate_roadmap(ROADMAP_DOC)],
        "stage_index": 0,
        "stage_status": stage_statuses(validate_roadmap(ROADMAP_DOC)),
        "stage_frame": {},
    }
    config = {"configurable": {"thread_id": THREAD_ID}, "recursion_limit": 40}
    last: dict = {}
    stream = compiled.stream(state, config=config, stream_mode="values")
    try:
        for snapshot in stream:
            last = snapshot
            frame = snapshot.get("stage_frame") or {}
            events = snapshot.get("events") or []
            sid = frame.get("stage_id")
            if up_to != "all" and sid and frame.get("stage_index") is not None:
                if STAGE_OF[up_to] == sid and events and events[-1].get("type") == "stage_frame":
                    # 目标 Stage 已经走完一个 frame（含其 Gate2 终审）
                    if snapshot.get("stage_status", {}).get(sid) in (
                            "approved", "rejected"):
                        break
    finally:
        stream.close()
    return last


# ------------------------------------------------------------------ Stage 实质工作

def _control_dt(_stage_dir: Path | None = None) -> float:
    """LIBERO 官方 `control_freq=20`（`libero/envs/env_wrapper.py:27`）⇒ dt = 0.05 s。

    dt 由官方构造默认值决定，逐 episode 不变；传参只为调用点对称。
    """
    return 1.0 / 20.0


class StageWork:
    """逐 Stage 的实质动作（容器 + 判定 + 付费归因），带重入缓存。"""

    def __init__(self, root: Path, analyst: PaidAnalyst | None, *,
                 max_wait: float) -> None:
        self.root = root
        self.analyst = analyst
        self.max_wait = max_wait
        self.done: dict[str, dict[str, Any]] = {}
        self.chosen: dict[str, str] = {}      # s2/s3 -> 选中的策略
        self.degradations: list[dict[str, Any]] = []

    # -- 单个策略形态的整批 ------------------------------------------------
    def _try_policy(self, stage_key: str, stage_dir: Path,
                    cand: str) -> tuple[bool, dict[str, Any]]:
        form = POLICY_FORM[cand]
        env: dict[str, str] = {}
        if cand in POLICY_4BIT:
            env["OPENVLA_4BIT"] = POLICY_4BIT[cand]
            env["OPENVLA_MODEL"] = "openvla/openvla-7b"
            env["OPENVLA_UNNORM_KEY"] = "libero_object"
        rec = run_batch(stage_dir / cand, form, extra_env=env)
        ok = rec["ok"] and all(
            ((it.get("summary") or {}).get("steps") or 0) > 0
            for it in rec["results"])
        self.degradations_or_ok(stage_key, cand, ok, rec, stage_dir / cand)
        # 动作反归一化键不匹配（base 检查点无 libero 键）也必须披露：
        # 这是「策略被跑起来了但动作分布与训练分布不同」，不是不可达，也不是通过。
        note = next((((it.get("summary") or {}).get("unnorm_key_note"))
                     for it in rec["results"]
                     if (it.get("summary") or {}).get("unnorm_key_note")), None)
        if ok and note:
            self.degradations.append({
                "stage": stage_key, "policy": cand, "kind": "other",
                "reason": note,
                # 适配（2026-11-04）：运算符优先级。`/` 与 `%` 同级左结合，
                # 原来写成 `stage_dir / cand / "episodes_%s.json" % form` 会先拼
                # 路径再 `PosixPath % str` ⇒ TypeError，且崩在 S2 **跑完之后**，
                # 白烧 21 分钟 GPU（round 6 事故）。这里显式加括号。
                "evidence": str(stage_dir / cand / ("episodes_%s.json" % form))
                            + "（逐 episode summary.unnorm_key_used/_note）",
            })
        return ok, rec

    def degradations_or_ok(self, stage_key: str, cand: str, ok: bool,
                           rec: dict[str, Any], cdir: Path) -> None:
        """记录可达/不可达（不可达必须留 `degradations` 条目，禁止一句话了事）。"""
        _write_json(cdir / "reachability.json",
                    {"candidate": cand, "ok": ok,
                     "returncode": rec.get("returncode"),
                     "wallclock_s": rec.get("wallclock_s"),
                     "log": rec.get("log"), "cmd": rec.get("cmd")})
        if ok:
            return
        tail = ""
        logf = Path(str(rec.get("log") or ""))
        if logf.is_file():
            tail = logf.read_text(encoding="utf-8", errors="replace")[-2000:]
        self.degradations.append({
            "stage": stage_key, "policy": cand, "kind": "policy_unreachable",
            "reason": f"容器退出码 {rec.get('returncode')}；结果条数 "
                      f"{len(rec.get('results') or [])}；末 2000 字符日志见 evidence",
            "evidence": str(cdir / "reachability.json") + " + "
                        + str(rec.get("log")),
            "log_tail": tail,
        })

    # -- S1 ---------------------------------------------------------------
    def s1(self, stage_dir: Path) -> dict[str, Any]:
        smoke = run_smoke(stage_dir)
        _write_json(stage_dir / "smoke.json",
                    {k: v for k, v in smoke.items() if k != "results"})
        if not smoke["ok"]:
            # 预检不过 ⇒ 镜像/环境层面不通，**不伪造 episode、不跑批量**。
            tail = ""
            logf = Path(str(smoke.get("log") or ""))
            if logf.is_file():
                tail = logf.read_text(encoding="utf-8", errors="replace")[-2000:]
            deg = {"stage": "s1", "policy": "zero", "kind": "other",
                   "reason": "S1 预检（3 步单 episode）失败：容器退出码 "
                             f"{smoke.get('returncode')}",
                   "evidence": str(smoke.get("log")), "log_tail": tail}
            self.degradations.append(deg)
            return {"stage": "s1_baseline", "pass": False,
                    "reason": "S1 预检失败（镜像/环境在运行期不通，未进入批量）",
                    "smoke": {k: v for k, v in smoke.items() if k != "results"},
                    "smoke_log_tail": tail,
                    "judge": {"episodes": [], "rung1_all_pass": False,
                              "rung2_contact_all_pass": False,
                              "rung3_dt_band_ok": False,
                              "rung3_over_threshold_total": None,
                              "control_dt": _control_dt(stage_dir)},
                    "video_manifest": {"count": 0, "videos": []},
                    "video_ok": False, "telemetry_ok": False,
                    "degradations": [deg]}
        rec = run_batch(stage_dir, "zero")
        judged = judge_tracks(rec, _control_dt(stage_dir))
        man = video_manifest(stage_dir)
        ok_video = bool(man["count"]) and all(
            v.get("decodable") and v.get("width") == 1920
            and v.get("height") == 1080 for v in man["videos"])
        out = {"stage": "s1_baseline", "container": {
            "returncode": rec["returncode"], "wallclock_s": rec["wallclock_s"],
            "log": rec["log"]}, "judge": judged, "video_manifest": man,
            "video_ok": ok_video,
            # 注意：这里曾经写成 `all(A and all(...))`——`all()` 收到 bool 会
            # TypeError（2026-11-04 第二轮 S1 跑到这里崩过一次，批量已跑完）。
            # 现在显式取 episode 列表再判，不做隐式真值短路。
            "telemetry_ok": bool(judged["episodes"]) and all(
                (e.get("steps") or 0) > 0 for e in (judged["episodes"] or []))}
        out["pass"] = bool(ok_video and out["telemetry_ok"]
                           and judged["rung1_all_pass"]
                           and judged["rung2_contact_all_pass"])
        return out

    # -- S2 / S3 ----------------------------------------------------------
    def vla(self, stage_key: str, stage_dir: Path,
            exclude: str | None) -> dict[str, Any]:
        order = [c for c in STRATEGY_ORDER if c != exclude]
        if exclude:
            # 对照优先换模型形态（预注册 §5），换不了再用同模型另一量化形态
            head = [c for c in order if POLICY_FORM[c] != POLICY_FORM[exclude]]
            tail = [c for c in order if c not in head]
            order = head + tail
        picked: str | None = None
        rec: dict[str, Any] | None = None
        for cand in order:
            ok, r = self._try_policy(stage_key, stage_dir, cand)
            if ok:
                picked, rec = cand, r
                break
        if picked is None or rec is None:
            return {"stage": stage_key, "pass": False, "picked": None,
                    "reason": "预注册顺序内无任何可达策略（逐条见 degradations）",
                    "degradations": list(self.degradations)}
        judged = judge_tracks(rec, _control_dt(stage_dir))
        k = sum(1 for e in judged["episodes"] if e["success"])
        n = len(judged["episodes"])
        out = {"stage": stage_key, "picked": picked, "container": {
            "returncode": rec["returncode"], "wallclock_s": rec["wallclock_s"],
            "log": rec["log"]}, "judge": judged,
            "wilson": wilson(k, n), "successes": k, "n": n,
            "video_manifest": video_manifest(stage_dir / picked),
            "degradations": list(self.degradations)}
        out["pass"] = bool(n == 10 and all(e.get("error") is None
                                           for e in judged["episodes"])
                           and all(e.get("steps") for e in judged["episodes"]))
        return out

    # -- 付费归因 ---------------------------------------------------------
    def attribute(self, stage_key: str, payload: dict[str, Any]) -> str:
        if self.analyst is None:
            return ""
        compact = json.dumps(payload, ensure_ascii=False)[:12000]
        # 适配（2026-11-04）：归因额定输出从 900 提到 2500。实测 900 会把这段
        # 三段式答案**截断在 max_tokens 上**（round 3 那份 115 字符的证据就是
        # 截断件，结尾停在半个 JSON 字段名），而本供应商在被截断时会返回一个
        # 缺 `text` 键的尾块，litellm 的 anthropic 转换器直接 `KeyError: 'text'`
        # → 5 次重试全废 → 驱动在 agent_run 里抛 GatewayError 整个崩掉
        # （round 4 事故，ledger seq 58 ok=false）。同一提示词改 2500 后
        # 一次成功（1243 输出 token，1351 字符，首尾完整）。
        return self.analyst.ask(
            stage_key,
            "你是具身智能仿真实习评审。只依据给定 JSON 事实作答；"
            "不臆测未给出的数值，不把失败改写成成功。",
            ("以下是 LIBERO VLA 战役一个 Stage 的机读结果（预注册判据见 "
             "VERDICT_CRITERIA_VLA.md）。请用中文给三段：\n"
             "1) 事实摘要（成功率/步数/Rung 读数，逐条引用 JSON 里的数值）；\n"
             "2) 失败归因（若失败：动作空间/观测预处理/控制器/权重不匹配，"
             "每条必须指向 JSON 里的字段作为证据；成功则写「无失败」）；\n"
             "3) 给下一 Stage 的一条可执行建议（不得要求改预注册判据）。\n"
             f"JSON：{compact}"),
            max_tokens=2500,
            tag=f"{stage_key}_attribution")


def _build_deps(gate: StageGate, work: StageWork, run_root: Path,
                stage_log: list[dict[str, Any]]):
    """三接缝装配：direction → gate1 → agent_run/evaluate/gate2 → persist。"""

    def direction(state: dict) -> dict:
        return {"proposals": [{
            "name": "LIBERO VLA 三 Stage 战役（P27-B2）",
            "sim_engine": "libero",
            "first_principles": ("官方基线先行（证明环境/渲染/遥测），再让预注册顺序里"
                                 "首个可达的 VLA 接管动作，最后换形态做对照"),
            "expected_artifacts": ["videos/*.mp4", "episodes_*.json",
                                   "vla_report.md"],
            "tool_grants": list(GLOBAL_GRANTS),
        }], "question": state.get("question", "")}

    def gate1(state: dict) -> dict:
        cands = json.loads((campaign_root() / "s0" / "vla_candidates.json")
                           .read_text(encoding="utf-8"))
        names = [c["name"] for c in cands.get("candidates", [])]
        order = cands.get("strategy_order") or STRATEGY_ORDER
        note = ("【Task6 第 1 张】请点「批准方案 1」——核准整条三 Stage 路线图。"
                f"S0 候选={names}，预注册顺序={order}")
        card = build_roadmap_card(ROADMAP_DOC, note=note)
        return gate.ask(1, card, "roadmap")

    def agent_run(state: dict) -> dict:
        frame = state.get("stage_frame") or {}
        sid = frame.get("stage_id") or STAGE_IDS[0]
        stage_key = next(k for k, v in STAGE_OF.items() if v == sid)
        stage_dir = run_root / stage_key
        if sid in work.done:
            res = work.done[sid]
        else:
            if stage_key == "s1":
                res = work.s1(stage_dir)
            elif stage_key == "s2":
                res = work.vla(stage_key, stage_dir, exclude=None)
                if res.get("picked"):
                    work.chosen["s2"] = res["picked"]
            else:
                res = work.vla(stage_key, stage_dir, exclude=work.chosen.get("s2"))
                if res.get("picked"):
                    work.chosen["s3"] = res["picked"]
            res["attribution"] = work.attribute(stage_key, {
                "stage": stage_key,
                "pass": res.get("pass"),
                "picked": res.get("picked"),
                "successes": res.get("successes"),
                "n": res.get("n"),
                "wilson": res.get("wilson"),
                "judge": {k: v for k, v in (res.get("judge") or {}).items()
                          if k != "episodes"},
                "episodes": [{kk: e.get(kk) for kk in
                              ("run_id", "steps", "success", "vram_mib",
                               "error", "seed")}
                             for e in ((res.get("judge") or {}).get("episodes") or [])],
                "degradations": [{kk: d.get(kk) for kk in ("policy", "kind", "reason")}
                                 for d in res.get("degradations", [])],
            })
            _write_json(stage_dir / "stage_result.json", res)
            work.done[sid] = res
        uri = stage_dir / "metrics.json"
        _write_json(uri, {"engine": "libero", "run_id": f"{THREAD_ID}/{sid}",
                          "stage": stage_key, "pass": res.get("pass"),
                          "picked": res.get("picked"),
                          "successes": res.get("successes"),
                          "n": res.get("n"),
                          "wilson": res.get("wilson"),
                          "judge": {k: v for k, v in (res.get("judge") or {}).items()
                                    if k != "episodes"}})
        stage_log.append({"stage_id": sid, "stage_key": stage_key,
                          "metrics": str(uri), "pass": res.get("pass"),
                          "picked": res.get("picked")})
        return {"telemetry_uri": str(uri), "coding_round": 0}

    def evaluate(state: dict) -> dict:
        uri = state.get("telemetry_uri")
        if not uri or not Path(uri).is_file():
            raise RuntimeError(f"evaluate：metrics 件不存在：{uri!r}")
        m = json.loads(Path(uri).read_text(encoding="utf-8"))
        j = m.get("judge") or {}
        return {
            "fixture": False,
            "stage": m.get("stage"),
            "pass": m.get("pass"),
            "picked": m.get("picked"),
            "successes": m.get("successes"),
            "n": m.get("n"),
            "wilson": m.get("wilson"),
            "vector": {"stage_pass": bool(m.get("pass")),
                       "rung1": bool(j.get("rung1_all_pass")),
                       "rung2_contact": bool(j.get("rung2_contact_all_pass"))},
            "rules_pass": bool(m.get("pass")),
            "telemetry_uri": uri,
        }

    def gate2(state: dict) -> dict:
        frame = state.get("stage_frame") or {}
        index = int(state.get("stage_index", 0) or 0)
        sid = frame.get("stage_id")
        rec = work.done.get(sid or "", {})
        arts: list[dict[str, Any]] = []
        j = rec.get("judge") or {}
        if rec.get("picked"):
            arts.append({"name": f"策略：{rec['picked']}",
                         "detail": f"成功率 {rec.get('successes')}/{rec.get('n')} "
                                   f"Wilson={rec.get('wilson')}"})
        if j:
            arts.append({"name": "Rung 向量",
                         "detail": f"rung1_all={j.get('rung1_all_pass')} "
                                   f"rung2_contact_all={j.get('rung2_contact_all_pass')} "
                                   f"rung3_dt_band_ok={j.get('rung3_dt_band_ok')}"})
        vm = rec.get("video_manifest") or {}
        if vm.get("count"):
            arts.append({"name": f"视频 {vm['count']} 条（1920×1080/20fps）",
                         "detail": "MP4 在 /mnt，证据包只收 video_manifest.json"})
        if rec.get("degradations"):
            arts.append({"name": f"降级 {len(rec['degradations'])} 条",
                         "detail": "；".join(d["policy"] for d in rec["degradations"])})
        note = (f"【Task6 第 {index + 2}/{len(STAGE_IDS) + 1} 张】"
                f"Stage {index + 1}《{frame.get('title', sid)}》终审："
                f"通过点「批准」，否则「打回并提出意见」")
        card = build_stage_gate_card(frame, arts, stage_index=index,
                                     total_stages=len(STAGE_IDS), note=note)
        return gate.ask(2, card, f"stage{index + 1}_{sid}")

    def persist(state: dict) -> dict:
        # Task6 不写任何库表（AC-T6-5 要求回收后 phase1_learnings 仍 44 行）。
        return {"written": 0,
                "note": "Task6：accept 结果只落证据包，不写 phase1/phase2_learnings"}

    return GraphDeps(direction=direction, gate1=gate1, agent_run=agent_run,
                     evaluate=evaluate, gate2=gate2, persist=persist,
                     task_title=TASK_TITLE, run_dir=run_root), gate


# ------------------------------------------------------------------ 报告

def render_report(root: Path, work: StageWork, gate: StageGate,
                  ckpt: dict) -> str:
    lines = ["# Phase 2.7 Task 6 — LIBERO VLA 多 Stage 战役报告", ""]
    lines.append(f"- 票号：`{BATCH}`（帽 {TASK_CAP}；子帽 S1={SUBCAP['s1']} / "
                 f"S2={SUBCAP['s2']} / S3={SUBCAP['s3']}）")
    lines.append(f"- 生成时刻（UTC）：{utc_now_iso_z()}")
    lines.append(f"- 线程：`{THREAD_ID}`")
    lines.append("")
    lines.append("## 1. 三 Stage 终态")
    lines.append("")
    lines.append("| Stage | 通过 | 策略 | 成功率 | Wilson 95% | 备注 |")
    lines.append("|---|---|---|---|---|---|")
    for sid in STAGE_IDS:
        r = work.done.get(sid) or {}
        w = r.get("wilson") or {}
        note = "—"
        if sid == "s1_baseline":
            j = r.get("judge") or {}
            note = (f"rung1={j.get('rung1_all_pass')} "
                    f"rung2_contact={j.get('rung2_contact_all_pass')} "
                    f"rung3_band={j.get('rung3_dt_band_ok')}")
        rate = (f"{r.get('successes')}/{r.get('n')}") if r.get("n") else "—"
        ci = ("[%s, %s]" % (w["lower"], w["upper"])) if w else "—"
        # 适配（2026-11-04）：策略列的空值兜底原来一律写 `zero`，把
        # 「S3 没选出任何可达策略」（picked=None ⇒ 根本没跑）显示成「跑了
        # zero 基线」——round 7 的报告因此误导读者。只有 S1 的 None 才
        # 真的是 zero 基线；其余 Stage 的 None 如实写「（无）」并注明原因在
        # §2 降级披露里。判据不变，只修行文。
        if r.get("picked"):
            shown = r["picked"]
        elif sid == "s1_baseline":
            shown = "zero"
        else:
            shown = "（无：见 §2 降级披露）"
        lines.append(f"| `{sid}` | {r.get('pass')} | {shown} | "
                     f"{rate} | {ci} | {note} |")
    lines.append("")
    lines.append("## 2. 降级披露（`degradations`）")
    lines.append("")
    degs = work.degradations
    if not degs:
        lines.append("```json")
        lines.append("[]")
        lines.append("```")
    else:
        lines.append("```json")
        lines.append(json.dumps([
            {k: d.get(k) for k in ("stage", "policy", "kind", "reason", "evidence")}
            for d in degs], ensure_ascii=False, indent=2))
        lines.append("```")
    lines.append("")
    lines.append("## 3. 真人 Gate 记录")
    lines.append("")
    lines.append("| 卡 | gate | message_id | 发出（UTC） | 决策 | 意见 |")
    lines.append("|---|---|---|---|---|---|")
    for i, row in enumerate(gate.snapshots, 1):
        lines.append(f"| {i} {row['label']} | {row['gate']} | `{row['message_id']}` | "
                     f"{row['sent_at_utc']} | {row.get('decision')} | "
                     f"{row.get('human_notes') or '-'} |")
    lines.append("")
    lines.append("## 4. 检查点侧互证")
    lines.append("")
    events = ckpt.get("events") or []
    lines.append(f"- `stage_index` = {ckpt.get('stage_index')}")
    lines.append(f"- `stage_status` = {json.dumps(ckpt.get('stage_status'), ensure_ascii=False)}")
    lines.append(f"- gate 事件：{json.dumps([e.get('type') for e in events if str(e.get('type','')).startswith('gate')], ensure_ascii=False)}")
    lines.append("")
    lines.append("## 5. 锁内适配（判据强度不降）")
    lines.append("")
    lines.append("1. 单进程跑完 s1→s3（计划 CLI 示例是三次独立调用）：LangGraph 0.2.28 的"
                 "跨进程续跑是计划 B.3 明列的已知未知，一次进程内走完更干净。")
    lines.append("2. Rung3 记 `not_applicable`：LIBERO 官方 `control_freq=20` ⇒ "
                 "`dt=0.05 s` 落在冻结判据锚定带 `[1/120, 1/30]` 之外，"
                 "`rung3_check` 自己声明带外保守判负；原始 `jerk_max_mps3` 已逐 episode 报出，"
                 "**本文不声称 Rung3 通过**。")
    lines.append("3. Rung2 目标子项：LIBERO 无 `goal_box`，改由官方 `env.check_success()` 判定；"
                 "零动作基线（S1）报数不判分，S2/S3 计分。")
    lines.append("4. 一批 episode 在**一个容器进程**内跑完（模型只加载一次）。")
    lines.append("5. 镜像侧 import 链（2026-11-04 首轮基础设施故障后修复）：LIBERO 仓根的 "
                 "`libero/` 是命名空间目录（真正的包是 `libero/libero/`），editable 安装"
                 "只映射 `libero.libero`，故把仓根挂 `PYTHONPATH`；`libero/libero/__init__.py` "
                 "在 `~/.libero/config.yaml` 缺失时调 `input()`，故构建期预生成配置；"
                 "构建断言升级为**真 import + benchmark 字典 + bddl 目录 + 渲染类**，"
                 "镜像缺陷不允许再带到运行期。")
    lines.append("6. 动作反归一化键：S0 钉死的检查点是 `openvla/openvla-7b`（base，OpenX 混训），"
                 "其 `norm_stats` 无 LIBERO 的 `libero_object` 键。不偷换检查点，"
                 "改用检查点自带键并逐 episode 记 `unnorm_key_used/_note`，"
                 "同时在 `degradations` 记 `kind=\"other\"`（动作分布不匹配，成功率预期偏低）。")
    lines.append("")
    return "\n".join(lines)


# ------------------------------------------------------------------ 主流程

def run(*, up_to: str, max_wait: float, start_ws: bool = True) -> dict[str, Any]:
    root = campaign_root()
    run_root = root / "runs" / THREAD_ID
    # 上一轮已跑完（有 campaign_record）⇒ **整目录归档**而不是原地覆盖：
    # 每次尝试的 gate 点击、证据、付费归因都要留档，公开报告里逐轮交代。
    if (run_root / "campaign_record.json").is_file():
        ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        dest = root / "runs" / f"_archive_{THREAD_ID}_{ts}"
        shutil.move(str(run_root), str(dest))
        print(f"[t6] 上一轮已结束，归档到：{dest}")
    run_root.mkdir(parents=True, exist_ok=True)
    os.chmod(root / "runs", 0o700)
    os.chmod(run_root, 0o700)
    ensure_batch_approval()

    analyst = PaidAnalyst(thread=THREAD_ID, evidence_dir=run_root / "paid")
    gate = StageGate(None, run_root / "gates", max_wait=max_wait)  # gw 稍后注入
    work = StageWork(root, analyst, max_wait=max_wait)
    stage_log: list[dict[str, Any]] = []

    gw = gateway_from_env(db_path=run_root / "pending.sqlite",
                          evidence_dir=run_root / "feishu",
                          start_ws=start_ws,
                          on_card_action=gate.on_card_action)
    gate.gw = gw
    db_path = run_root / "checkpoints.db"
    if db_path.exists():
        print(f"[t6] 清理上次检查点：{db_path}")
        db_path.unlink()
    deps, _ = _build_deps(gate, work, run_root, stage_log)
    try:
        with SqliteSaver.from_conn_string(str(db_path)) as saver:
            compiled = build_graph(deps).compile(checkpointer=saver)
            last = _stream_campaign(compiled, up_to)
    finally:
        gw.close()

    ckpt = _checkpoint_state(db_path, THREAD_ID) or last
    report = render_report(root, work, gate, ckpt)
    atomic_write_text(root / "vla_report.md", report)
    os.chmod(root / "vla_report.md", 0o600)
    _write_json(run_root / "campaign_record.json", {
        "thread_id": THREAD_ID, "batch": BATCH,
        "gates": gate.snapshots, "callbacks": gate.callbacks,
        "stage_log": stage_log, "chosen": work.chosen,
        "degradations": work.degradations,
        "spend": batch_spend(),
        "paid_calls": analyst.calls,
        "stage_index": ckpt.get("stage_index"),
        "stage_status": ckpt.get("stage_status"),
        "events": [e.get("type") for e in (ckpt.get("events") or [])],
        "at_utc": utc_now_iso_z(),
    })
    return {"report": str(root / "vla_report.md"),
            "stages": list(work.done),
            "chosen": work.chosen,
            "degradations": len(work.degradations),
            "spend": batch_spend(),
            "gates": len(gate.snapshots)}


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description="Phase 2.7 Task6 LIBERO VLA Stage 执行器")
    ap.add_argument("--stage", default="all", choices=["s1", "s2", "s3", "all"])
    ap.add_argument("--batch", default=BATCH)
    ap.add_argument("--policy", default=None,
                    help="仅记录用；实际策略由预注册顺序 + 可达性决定")
    ap.add_argument("--max-wait", type=float, default=7200.0,
                    help="单张卡最长等待秒数（超时抛错，绝不代点）")
    ap.add_argument("--no-ws", action="store_true", help="调试用：不建飞书长连接")
    args = ap.parse_args(argv)
    if args.batch != BATCH:
        raise SystemExit(f"票号不符：{args.batch} != {BATCH}")
    if args.policy:
        print(f"[t6] 提示：--policy={args.policy} 仅记录；"
              f"实际按预注册顺序 {STRATEGY_ORDER} 取首个可达者")
    out = run(up_to=args.stage, max_wait=args.max_wait, start_ws=not args.no_ws)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
