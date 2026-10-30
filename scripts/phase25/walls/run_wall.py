#!/usr/bin/env python3
"""Task 5 历史墙唯一付费入口（coding_debug 文本阶段，0 image block）。

用法：
  # 0 付费：冻结/挂载/镜像/票据/白名单全预检 + 容器管道冒烟（不触网）
  python run_wall.py --wall wallA --preflight

  # 真跑（必须 SIMPRAXIS_ALLOW_PAID=1，裸跑 exit 2；经 run_watched.sh 外包）
  SIMPRAXIS_ALLOW_PAID=1 python run_wall.py --wall wallA

  # 0 付费：两墙结果齐了以后聚合三态判决 + B-cap 独立轴
  python run_wall.py --combine

纪律：首呼前比对 freeze_manifest 九件闸哈希并写 LOCK；call_cache=None
真出生；模型容器永不挂 /opt/inst 与冻结 OBJ；loop 自终局且容器清零后才
一次性评分；到子帽未过即熔断（不抬帽/不换种/不放宽）。负结果照发即结论。
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from orchestrator.agentic.caps import BudgetTracker
from orchestrator.agentic.forensics import TraceStore
from orchestrator.agentic.loop import run_agentic_inner_loop
from orchestrator.agentic.wiring import (
    build_seq_provider, parse_grant_strings,
)
from orchestrator.gateway.quota_ledger import (
    QuotaLedger, billing_context, call_lines, read_lines,
)
from orchestrator.gateway.router import ModelGateway

from scripts.phase25.walls import freeze_common as fc
from scripts.phase25.walls.spike_backend import (
    IMAGE_ID, IMAGE_REPODIGEST, SpikeWallBackend, score_wall_a, score_wall_b,
)
from scripts.phase25.walls.verdict import (
    WALL_A, WALL_B, WallResult, b_cap_axis, deny_records_from_trace,
    engineering_verdict,
)

WALLS = (WALL_A, WALL_B)
BASELINES = {WALL_A: 60, WALL_B: 30}
# 实验批次：g2＝Task 5 原始批次（历史默认，票据/线程前缀冻结不动）；
# 5b＝协议加固新批次（task5b_spec.md 攢批115 af2df49 预注册；九件冻结墙、
# 同帽 16/9、模型不换；G2「不成立」不翻案）。
EXPERIMENTS = ("g2", "5b")
BATCH_IDS = {WALL_A: "P25-B2", WALL_B: "P25-B3"}
BATCH_IDS_5B = {WALL_A: "P25-B4", WALL_B: "P25-B5"}
BATCH_BY_EXPERIMENT = {"g2": BATCH_IDS, "5b": BATCH_IDS_5B}
THREAD_PREFIX = {"g2": "t5", "5b": "t5b"}
PRIOR_G2_VERDICT = {
    "file": "verdict-20261030T092635Z.json",
    "verdict": "不成立",
    "note": ("Task 5 G2 三态结论（硬规则 7 不翻案）；5b 是用户 D-4 裁决"
             "（2026-10-30）授权的独立新实验，见 task5b_spec.md"),
}
SUBCAPS = {WALL_A: 16, WALL_B: 9}      # compute_subcaps(60,30) 冻结值
GPU_CLOSEOUT_MIB = 200                 # 硬规则 5：收尾显存 ≤200 MiB
FENCE_RE = re.compile(r"```\n(.*?)```", re.DOTALL)

# 冻结授予串（运行时与 GATE.md 围栏块逐字互证；两墙同一份）。
FROZEN_GRANTS = (
    "read_file", "list_dir", "grep", "write:",
    "run:python", "run:ls", "run:cat", "run:grep", "run:find",
)

# G-15 关键词（TASK.md 出生契约 distinctive tokens；真出生的模型侧文本
# 应在 trace/full/raw 中可检——只做计数留证，不参与过墙判据）。
FORENSIC_KEYWORDS = {
    WALL_A: ["ControlEnv", "libero_spatial", "OSC_POSE",
             "use_camera_obs", "build_env"],
    WALL_B: ["edge_manifold_counts", "make_vessel_mesh",
             "kinematic_witness", "evaluate_precheck", "watertight"],
}

OPERATIONAL_FOOTER = """\

## 执行约定

- 工作目录就是沙盒内的 /work；只在其中写 `agent_solution.py` 与你的临时
  自检脚本；可用的工具只有文件读写/检索与白名单命令（python/ls/cat/grep/find），
  无网络、无包安装、无 GPU。
- 可以也应当反复运行你自己的临时脚本自检（例如
  `python my_check.py`）；但最终交付件必须是 /work/agent_solution.py，
  导入它不得有副作用。
- 自检通过后调用 finish 结束；确认无法完成时调用 fail 并说明原因。
  终局判定由独立隐藏评分器在另一只读环境一次性完成。
"""


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def nvidia_used_mib() -> int:
    out = subprocess.run(
        ["nvidia-smi", "--query-gpu=memory.used",
         "--format=csv,noheader,nounits"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=15)
    return int(out.stdout.decode().strip().splitlines()[0])


# ------------------------------------------------------------ 冻结面校验

def verify_freeze(root: Path) -> dict:
    manifest_p = root / "manifests" / "freeze_manifest.json"
    if not manifest_p.is_file():
        raise SystemExit(f"冻结清单不存在：{manifest_p}（先跑两个 freeze 脚本）")
    manifest = json.loads(manifest_p.read_text(encoding="utf-8"))
    current = fc.gate_hashes()
    if manifest["gate_hashes"] != current:
        raise SystemExit("闸件哈希与冻结清单不符，拒呼（fail-closed）")
    fc.verify_embedded_hashes()
    for section in ("wall_a", "wall_b"):
        if section not in manifest["sections"]:
            raise SystemExit(f"冻结清单缺 {section} 段，两墙冻结齐了才许开跑")
    proc = subprocess.run(
        ["docker", "inspect", IMAGE_ID, "--format",
         "{{json .RepoDigests}}|{{json .RepoTags}}"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    out = proc.stdout.decode("utf-8", "replace")
    if proc.returncode != 0 or IMAGE_REPODIGEST.split(":", 1)[1] not in out:
        raise SystemExit("冻结镜像 digest 不符/缺失，拒呼")
    return manifest


def grants_from_gate(wall: str) -> tuple[str, ...]:
    gate_dir = fc.GATES_DIR / ("wall_a" if wall == WALL_A else "wall_b")
    text = (gate_dir / "GATE.md").read_text(encoding="utf-8")
    blocks = [b for b in FENCE_RE.findall(text) if "run:python" in b]
    if len(blocks) != 1:
        raise SystemExit(f"GATE.md 白名单围栏不唯一：{gate_dir}")
    lines = tuple(ln.strip() for ln in blocks[0].splitlines() if ln.strip())
    return lines


def write_lock(root: Path, wall: str, batch_id: str, thread_id: str) -> None:
    lock = root / "manifests" / "LOCK.json"
    payload = {
        "schema": "simpraxis.phase25.t5_first_call_lock.v1",
        "locked_iso": utc_iso(),
        "first_wall": wall, "batch_id": batch_id, "thread_id": thread_id,
        "gate_hashes": fc.gate_hashes(),
        "note": "首呼锁：此后闸件/冻结件一字不得改；freeze 脚本见锁拒跑",
    }
    if lock.exists():
        old = json.loads(lock.read_text(encoding="utf-8"))
        if old.get("gate_hashes") != payload["gate_hashes"]:
            raise SystemExit("LOCK 闸哈希与当前闸件不符，拒呼")
        return
    fc.write_json(lock, payload)


# ------------------------------------------------------------ 环境取证

class GpuSampler:
    def __init__(self, csv_path: Path, interval_s: float = 10.0):
        self.csv_path = csv_path
        self.interval_s = interval_s
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self.samples: list[tuple[str, int]] = []

    def _loop(self) -> None:
        with open(self.csv_path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["ts_iso", "memory_used_mib"])
            f.flush()
            while not self._stop.is_set():
                try:
                    mib = nvidia_used_mib()
                except Exception:  # noqa: BLE001 - 采样失败不顶主流程
                    mib = -1
                ts = utc_iso()
                self.samples.append((ts, mib))
                w.writerow([ts, mib])
                f.flush()
                self._stop.wait(self.interval_s)

    def __enter__(self) -> "GpuSampler":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self._thread.join(timeout=20)

    @property
    def max_mib(self) -> int | None:
        vals = [m for _t, m in self.samples if m >= 0]
        return max(vals) if vals else None


def docker_label_residue() -> list[str]:
    proc = subprocess.run(
        ["docker", "ps", "-aq", "--filter", "label=simpraxis-agentic-run"],
        stdout=subprocess.PIPE, timeout=30)
    return [c for c in proc.stdout.decode().split() if c]


def ledger_identity_rows(ledger: QuotaLedger, *, task: int,
                         batch_id: str, thread_id: str) -> list[dict]:
    rows = call_lines(read_lines(ledger.ns.ledger_path_fn()))
    return [r for r in rows
            if r.get("task") == task and r.get("batch_id") == batch_id
            and (r.get("thread_id") or "") == thread_id]


# ------------------------------------------------------------ 子命令

def cmd_preflight(wall: str, experiment: str = "g2") -> int:
    root = fc.freeze_root()
    manifest = verify_freeze(root)
    grants = grants_from_gate(wall)
    if grants != FROZEN_GRANTS:
        raise SystemExit(f"GATE.md 白名单与冻结授予串不符：\n{grants}")
    parse_grant_strings(grants)          # 形态闸：偷渡授予串直接抛
    batch_id = BATCH_BY_EXPERIMENT[experiment][wall]
    ledger = QuotaLedger.phase25()
    ledger_p = ledger.ns.ledger_path_fn()
    before = len(call_lines(read_lines(ledger_p)))
    try:
        pf = ledger.preflight("coding_debug", task=5,
                              batch_id=batch_id)
    except Exception as exc:  # noqa: BLE001 - 任何拦截都是预检失败
        raise SystemExit(f"票据 preflight 被拦：{type(exc).__name__}: {exc}")
    after = len(call_lines(read_lines(ledger_p)))
    if after != before:
        raise SystemExit("preflight 意外写账（票据可能被判 blocked）")
    # 容器管道冒烟：直调后端跑一句 import（绕过模型/policy，纯管道验证）。
    backend = SpikeWallBackend(wall, freeze_dir=root)
    smoke = "/opt/venv/bin/python"
    if wall == WALL_A:
        code = (
            "import numpy, mujoco, robosuite; "
            "from libero.libero.benchmark import get_benchmark; "
            "from libero.libero.envs.env_wrapper import ControlEnv; "
            "print('SMOKE_OK', ControlEnv.__name__)")
    else:
        code = ("import numpy, trimesh; "
                "print('SMOKE_OK', trimesh.__version__)")
    with tempfile.TemporaryDirectory(prefix="t5preflight-") as td:
        res = backend.run([smoke, "-c", code], cwd=Path(td), timeout_s=180)
    if res.rc != 0 or b"SMOKE_OK" not in res.stdout:
        raise SystemExit(
            f"管道冒烟失败 rc={res.rc}\nstdout={res.stdout[-2000:]!r}\n"
            f"stderr={res.stderr[-2000:]!r}")
    residue = docker_label_residue()
    if residue:
        raise SystemExit(f"冒烟后容器残留：{residue}")
    print(json.dumps({
        "preflight": "ok", "wall": wall,
        "manifest_sections": list(manifest["sections"]),
        "grants": list(grants), "ticket": batch_id,
        "experiment": experiment,
        "preflight_usage": pf,
        "smoke_stdout": res.stdout.decode().strip(),
        "ledger_call_rows_before_after": [before, after],
        "container_residue": [], "paid_calls": 0,
    }, indent=1, ensure_ascii=False))
    return 0


def _task_text(wall: str) -> str:
    gate_dir = fc.GATES_DIR / ("wall_a" if wall == WALL_A else "wall_b")
    return ((gate_dir / "TASK.md").read_text(encoding="utf-8")
            + OPERATIONAL_FOOTER)


def _score_once(wall: str, root: Path, work: Path) -> dict:
    score_dir = work / ".score"
    if score_dir.exists():
        print(f"[run_wall] ls 后清理旧 .score：",
              sorted(p.name for p in score_dir.iterdir()))
        shutil.rmtree(score_dir)
    if wall == WALL_A:
        res = score_wall_a(
            work, fc.GATES_DIR / "wall_a", root, timeout_s=240)
    else:
        res = score_wall_b(
            work, fc.GATES_DIR / "wall_b" / "wall_b_checker.py",
            root / "evidence_wall_b" / "pitcher_open_watertight.obj",
            timeout_s=180)
    files = sorted(score_dir.glob("wall_*-*.json"))
    score = json.loads(files[-1].read_text(encoding="utf-8")) if files else None
    return {"rc": res.rc, "timed_out": res.timed_out,
            "stdout": res.stdout.decode("utf-8", "replace"),
            "stderr": res.stderr.decode("utf-8", "replace"),
            "score": score, "score_file": files[-1].name if files else None}


def _read_trace_events(trace_jsonl: Path) -> list[dict]:
    events = []
    for line in trace_jsonl.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            events.append(json.loads(line))
    return events


def _forensics(wall: str, trace_dir: Path,
               seq_rows: list[dict]) -> dict:
    trace_jsonl = trace_dir / "trace.jsonl"
    blobs: list[Path] = [trace_jsonl]
    for sub in ("full", "raw"):
        blobs.extend(sorted((trace_dir / sub).glob("*.txt")))
    keyword_hits = {}
    for kw in FORENSIC_KEYWORDS[wall]:
        docs = 0
        for p in blobs:
            try:
                if kw in p.read_text(encoding="utf-8", errors="replace"):
                    docs += 1
            except OSError:
                pass
        keyword_hits[kw] = docs
    seqs = [int(r["seq"]) for r in seq_rows if r.get("seq") is not None]
    ts = [r.get("ts_iso") for r in seq_rows if r.get("ts_iso")]
    return {
        "schema": "simpraxis.phase25.t5_g15_forensics.v1",
        "seq_list": seqs, "seq_advance_monotonic": seqs == sorted(set(seqs)),
        "call_count": len(seqs),
        "first_ts_iso": ts[0] if ts else None,
        "last_ts_iso": ts[-1] if ts else None,
        "keyword_doc_hits": keyword_hits,
        "keyword_docs_searched": len(blobs),
        "callcache_enabled": False,
        "note": ("真出生三证：seq 推进见 ledger 行；新鲜时间戳比对冻结/首呼"
                 "时刻；关键词在 trace/full/raw 实文档计数。证伪面：0 命中"
                 "即出生契约未到达模型侧，须人工复查（不自动改判）。"),
    }


def cmd_run(wall: str, experiment: str = "g2") -> int:
    if os.environ.get("SIMPRAXIS_ALLOW_PAID") != "1":
        print("拒绝裸跑付费墙：需要 SIMPRAXIS_ALLOW_PAID=1（见硬规则 2 双守卫）",
              file=sys.stderr)
        return 2
    batch_id = BATCH_BY_EXPERIMENT[experiment][wall]
    root = fc.freeze_root()
    verify_freeze(root)
    grants = grants_from_gate(wall)
    if grants != FROZEN_GRANTS:
        raise SystemExit("GATE.md 白名单与冻结授予串不符，拒呼")
    grant_table = parse_grant_strings(grants)

    stamp = utc_stamp()
    thread_id = f"{THREAD_PREFIX[experiment]}-{wall.lower()}-{stamp}"
    run_dir = fc.ensure_dir(root / "runs" / wall / stamp)
    work = fc.ensure_dir(run_dir / "work")
    write_lock(root, wall, batch_id, thread_id)

    task_text = _task_text(wall)
    fc.atomic_write_bytes(run_dir / "task_prompt.txt",
                          task_text.encode("utf-8"))
    meta = {
        "schema": "simpraxis.phase25.t5_run_meta.v1",
        "wall": wall, "experiment": experiment,
        "batch_id": batch_id, "thread_id": thread_id,
        "started_iso": utc_iso(), "subcap": SUBCAPS[wall],
        "baseline_s": BASELINES[wall],
        "task_prompt_sha256": fc.sha256_file(run_dir / "task_prompt.txt"),
        "task_prompt_bytes": len(task_text.encode("utf-8")),
        "grants": list(grants),
        "image": {"id": IMAGE_ID, "repo_digest": IMAGE_REPODIGEST},
        "gate_hashes": fc.gate_hashes(),
        "callcache_enabled": False,
    }
    fc.write_json(run_dir / "run_meta.json", meta)
    print(f"[run_wall] {wall} 开跑 batch={batch_id} thread={thread_id} "
          f"subcap={SUBCAPS[wall]} run_dir={run_dir}", flush=True)

    # 账套必须显式 phase25 命名空间（billing_context 不路由命名空间）。
    ledger = QuotaLedger.phase25()
    gateway = ModelGateway(ledger=ledger)
    budget = BudgetTracker(step_cap=0, token_cap=0,
                           round_cap=SUBCAPS[wall])
    tracer = TraceStore(run_dir / "traces", thread_id)
    backend = SpikeWallBackend(wall, freeze_dir=root)

    with GpuSampler(run_dir / "gpu_mib.csv") as gpu_sampler, \
            billing_context(task=5, batch_id=batch_id,
                            thread_id=thread_id):
        outcome = run_agentic_inner_loop(
            workdir=work, task=task_text, grants=grant_table,
            gateway=gateway, budget=budget, tracer=tracer,
            thread_id=thread_id, run_backend=backend,
            probe_registry=None, recall_port=None,
            call_cache=None, nonprod=False,
            seq_provider=build_seq_provider())

    residue = docker_label_residue()
    if residue:
        raise SystemExit(f"loop 终局后模型容器残留：{residue}（人工清理前"
                         "不评分；硬规则 5）")

    outcome_rec = {
        "kind": outcome.kind, "value": outcome.value, "reason": outcome.reason,
        "smolagents_state": outcome.smolagents_state,
        "rounds_used": outcome.rounds_used, "steps_used": outcome.steps_used,
        "tokens_used": outcome.tokens_used, "cache_hits": outcome.cache_hits,
        "empty_responses": outcome.empty_responses,
        "supplier_errors": outcome.supplier_errors,
        "trace_path": str(outcome.trace_path),
        "is_failure_artifact": outcome.is_failure_artifact,
    }
    fc.write_json(run_dir / "outcome.json", outcome_rec)
    print(f"[run_wall] loop 终局 kind={outcome.kind} "
          f"rounds={outcome.rounds_used} steps={outcome.steps_used} "
          f"supplier_errors={outcome.supplier_errors}", flush=True)

    # 一次性评分（loop 自终局 + 容器清零之后）。
    scored = _score_once(wall, root, work)
    score_dir = fc.ensure_dir(run_dir / "score")
    fc.atomic_write_bytes(score_dir / "stdout.log",
                          scored["stdout"].encode("utf-8"))
    fc.atomic_write_bytes(score_dir / "stderr.log",
                          scored["stderr"].encode("utf-8"))
    if scored["score"] is not None:
        fc.write_json(score_dir / "score.json", scored["score"])

    residue_after = docker_label_residue()
    if residue_after:
        raise SystemExit(f"评分后容器残留：{residue_after}（硬规则 5）")
    gpu_final = nvidia_used_mib()

    seq_rows = ledger_identity_rows(
        ledger, task=5, batch_id=batch_id, thread_id=thread_id)
    n_calls = len(seq_rows)
    score_json = scored["score"] or {}
    passed = (scored["rc"] == 0 and score_json.get("verdict") == "pass")

    trace_jsonl = Path(outcome.trace_path)
    events = _read_trace_events(trace_jsonl)
    denies = deny_records_from_trace([
        {"tool": e.get("tool"),
         "policy": e.get("policy_decision")}
        for e in events])
    forensics = _forensics(wall, trace_jsonl.parent, seq_rows)
    forensics["ledger_rounds_match"] = (
        n_calls == outcome.rounds_used - outcome.cache_hits)
    forensics["trace_events"] = len(events)
    forensics["policy_denies"] = denies
    fc.write_json(run_dir / "forensics.json", forensics)

    cap_exhausted = (not passed) and n_calls >= SUBCAPS[wall]
    bcap = score_json.get("b_cap") if wall == WALL_B else None
    b_cap_passed = None
    if wall == WALL_B and passed and isinstance(bcap, dict):
        b_cap_passed = bool(bcap.get("speed_pass")) and \
            bool(bcap.get("clearance_pass"))
    wall_result = WallResult(
        wall=wall, passed=passed,
        calls_to_rc0=n_calls if passed else None,
        escalated=False, human=False, cap_exhausted=cap_exhausted,
        b_cap_passed=b_cap_passed)
    result_rec = {
        "schema": "simpraxis.phase25.t5_wall_result.v1",
        "wall": wall, "experiment": experiment, "ts_iso": utc_iso(),
        "passed": passed, "rc": scored["rc"],
        "scorer_timed_out": scored["timed_out"],
        "verdict": score_json.get("verdict"),
        "calls_to_rc0": wall_result.calls_to_rc0,
        "paid_calls": n_calls, "subcap": SUBCAPS[wall],
        "baseline_s": BASELINES[wall],
        "within_50pct_threshold": BASELINES[wall] // 2,
        "cap_exhausted": cap_exhausted,
        "loop_kind": outcome.kind,
        "b_cap": bcap,
        "b_cap_passed": wall_result.b_cap_passed,
        "escalated": False, "human": False,
        "policy_denies": denies,
        "gpu_max_mib_while_running": gpu_sampler.max_mib,
        "gpu_final_mib": gpu_final,
        "gpu_closeout_ok": gpu_final <= GPU_CLOSEOUT_MIB,
        "forensics": forensics,
        "run_dir": str(run_dir),
    }
    fc.write_json(run_dir / "wall_result.json", result_rec)

    print(json.dumps(result_rec, indent=1, ensure_ascii=False))
    print(f"[run_wall] {wall} 完成：passed={passed} calls={n_calls}/"
          f"{SUBCAPS[wall]} cap_exhausted={cap_exhausted} "
          f"gpu_final={gpu_final}MiB", flush=True)
    if gpu_final > GPU_CLOSEOUT_MIB:
        raise SystemExit(
            f"收尾显存 {gpu_final} > {GPU_CLOSEOUT_MIB} MiB（硬规则 5）；"
            "不擅杀他方进程，STOP 人工核")
    return 0


def _latest_result(root: Path, wall: str) -> dict | None:
    base = root / "runs" / wall
    if not base.is_dir():
        return None
    candidates = sorted(base.glob("*/wall_result.json"))
    if not candidates:
        return None
    return json.loads(candidates[-1].read_text(encoding="utf-8"))


def cmd_combine(root: Path) -> int:
    ra = _latest_result(root, WALL_A)
    rb = _latest_result(root, WALL_B)
    missing = [w for w, r in ((WALL_A, ra), (WALL_B, rb)) if r is None]
    if missing:
        raise SystemExit(f"缺墙结果，无法聚合：{missing}")

    def to_wall(r: dict) -> WallResult:
        return WallResult(
            wall=r["wall"], passed=r["passed"],
            calls_to_rc0=r.get("calls_to_rc0"),
            escalated=r.get("escalated", False),
            human=r.get("human", False),
            cap_exhausted=r.get("cap_exhausted", False),
            b_cap_passed=r.get("b_cap_passed"))

    exp_a = ra.get("experiment", "g2")
    exp_b = rb.get("experiment", "g2")
    if exp_a != exp_b:
        raise SystemExit(
            f"两墙结果分属不同实验：{exp_a} vs {exp_b}，拒绝跨实验聚合")
    wa, wb = to_wall(ra), to_wall(rb)
    verdict = engineering_verdict(
        wa, wb, baselines={WALL_A: BASELINES[WALL_A],
                           WALL_B: BASELINES[WALL_B]})
    bcap = b_cap_axis(wb)
    out = {
        "schema": "simpraxis.phase25.t5_combined_verdict.v1",
        "ts_iso": utc_iso(),
        "experiment": exp_a,
        "wall_a_run": ra["run_dir"], "wall_b_run": rb["run_dir"],
        "wall_a": {"passed": ra["passed"], "calls": ra["paid_calls"],
                   "subcap": ra["subcap"], "kind": ra["loop_kind"]},
        "wall_b": {"passed": rb["passed"], "calls": rb["paid_calls"],
                   "subcap": rb["subcap"], "kind": rb["loop_kind"],
                   "b_cap_passed": rb.get("b_cap_passed")},
        "engineering_verdict": verdict,
        "b_cap_axis": bcap,
        "d4_stop_required": (
            verdict["verdict"] == "不成立" or bcap["d4_stop"]),
        # 5b：只读指针，不参与 engineering_verdict 计算（报告连续性）。
        "prior_g2_verdict": PRIOR_G2_VERDICT if exp_a == "5b" else None,
    }
    p = root / "runs" / f"verdict-{utc_stamp()}.json"
    fc.write_json(p, out)
    print(json.dumps(out, indent=1, ensure_ascii=False))
    print(f"[run_wall] 聚合判决：{p}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wall", choices=list(WALLS))
    ap.add_argument("--experiment", choices=list(EXPERIMENTS),
                    default="g2", help="g2=Task5 历史批次（默认）；5b=协议加固批次")
    ap.add_argument("--preflight", action="store_true")
    ap.add_argument("--combine", action="store_true")
    args = ap.parse_args(argv)
    root = fc.freeze_root()
    if args.combine:
        return cmd_combine(root)
    if not args.wall:
        ap.error("非 --combine 模式必须给 --wall")
    if args.preflight:
        return cmd_preflight(args.wall, args.experiment)
    return cmd_run(args.wall, args.experiment)


if __name__ == "__main__":
    raise SystemExit(main())
