#!/usr/bin/env python3
"""Task8 Q2（Carla 固态 LiDAR 雨雪雾）事后补真人 Gate2 点验卡片。

背景：Q2 按详细计划 D9 走「条件闸门」——不经 LangGraph 图、无 Gate1 方案卡，
凭用户 2026-10-16 书面 standing authorization 执行，因此四任务中唯独 Q2
没有飞书 card_form 真人签字。本脚本按用户 2026-10-16 指示补发一张真实
Gate2 卡（原生 q2_overlay.mp4 + outcome/mechanism 双结论），真实阻塞等待
真人按钮裁决，禁止任何自动批准。

与 tests/milestone1/test_hitl_drill.py 的区别：本流程 0 次方舟付费调用
（不发 Rung4 视觉实呼；Q2 结论是几何/物理对照实验，不是 rollout 视频核片），
只有飞书消息发送；脚本在发卡前后各读一次 quota ledger，断言实呼行数不变。

唯一合法运行方式（fail-closed 环境守卫）：
    source scripts/00_env.sh
    SIMPRAXIS_Q2_HUMAN_GATE=1 \
        /mnt/venvs/simpraxis/bin/python scripts/milestone1/q2_human_gate.py

产物（全部在 Q2 证据目录内，证据包按 E8.2 递归收录）：
    <q2_carla>/human_gate/q2_gate.log     过程日志
    <q2_carla>/human_gate/decision.json   结构化裁决（simpraxis.q2_human_gate.v1，0600）

Phase 2 Task0a（纪律 12）：真人 approve/card_form 落 decision.json 后，经
memory_flywheel.phase1.offgraph_learning 图外通路向 phase1_learnings 补沉淀
一行（gate2_accepted_supplementary, rung=1）；reject/异常不写。回写失败
FATAL 退出（裁决件已保留），人工可对既有 decision.json 经该模块 CLI 补跑。

退出码：0=真人 approve；3=真人 reject_with_comments（如实失败，不掩盖）；
1=环境/证据缺失或等待异常。

H-5/H-9/H-10/H-19（2026-10-16 评审修复）：
  - 幂等：decision.json 已存在即拒；线程在发卡前经 store.claim_pending 事务
    占位，第二个并发调用方直接退出；终态守卫查主表 UNION history；
  - decision.json 经 fsutil.atomic_write_text 原子写且 0600；
  - 卡片不再写死任何事实：16 线/120°/50m/300kpps 读 meta_rep1.json 并与
    SUMMARY.md 交叉核对，Qback 读 optics_model.py 源码常量并与 SUMMARY 核对，
    时长 ffprobe 实读，engine_has_snow 实时 hasattr carla.WeatherParameters；
    任一来源缺失或不一致 -> Q2FactError -> FATAL exit 1；
  - 事实读取/指标构建/退出码裁决均为纯函数（见 Q2Facts/build_*/*），
    tests/milestone1/test_q2_human_gate.py 覆盖退出矩阵。
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
PHASE1_ROOT = Path(os.environ.get(
    "SIMPRAXIS_PHASE1_ROOT", "/mnt/artifacts/simpraxis/phase1"))
RUNS_ROOT = Path(os.environ.get(
    "SIMPRAXIS_RUN_DIR", "/mnt/artifacts/simpraxis/runs")) / "phase1"
Q2_DIR = RUNS_ROOT / "task8_track2" / "q2_carla"
ANALYSIS = Q2_DIR / "analysis"
CONTROL_INV = Q2_DIR / "control" / "analysis" / "invariance_checks.json"
GATES_DB = PHASE1_ROOT / "state" / "pending_gates.db"
OPTICS_MODEL = (_REPO_ROOT / "sandbox" / "track2" / "carla_lidar_weather"
                / "optics_model.py")

THREAD_ID = "t8-q2carla-hitl-20261016"
TITLE = ("[Q2 补真人点验] Track2 工程题：固态 LiDAR 雨雪雾"
         "物理光学材质模型（Carla 0.10.0）")

# optics_sweep.csv 三档 condition -> 卡片标签（与 SUMMARY 措辞一致）
_WANTED_CONDITIONS = {
    "rain_100": "rain 100 mm/h（Marshall–Palmer 雨谱）",
    "snow_100": "snow 100 mm/h 融化当量（Gunn–Marshall 雪谱）",
    "fog_v6": "fog V=6 m（Koschmieder）",
}


class Q2FactError(RuntimeError):
    """事实来源缺失/不一致：卡片不得带未核验字面量发卡（H-10）。"""


@dataclass(frozen=True)
class Q2Paths:
    """run() 的全部现场路径（测试注入临时目录用；默认值取现场常量）。"""
    q2_dir: Path
    analysis: Path
    control_invariance: Path
    gates_db: Path
    meta_rep1: Path
    summary_md: Path
    optics_model: Path
    video: Path

    @property
    def decision_path(self) -> Path:
        return self.q2_dir / "human_gate" / "decision.json"

    @property
    def log_path(self) -> Path:
        return self.q2_dir / "human_gate" / "q2_gate.log"


@dataclass(frozen=True)
class Q2Facts:
    """卡片事实，全部实读，不允许在模板里写字面量（H-10）。"""
    channels: int
    horizontal_fov_deg: int
    range_m: int
    points_per_second: int
    rotation_hz: int
    video_duration_s: float
    qback: float
    engine_has_snow: bool


def default_paths() -> Q2Paths:
    return Q2Paths(
        q2_dir=Q2_DIR,
        analysis=ANALYSIS,
        control_invariance=CONTROL_INV,
        gates_db=GATES_DB,
        meta_rep1=Q2_DIR / "collect_rep1" / "meta_rep1.json",
        summary_md=ANALYSIS / "SUMMARY.md",
        optics_model=OPTICS_MODEL,
        video=ANALYSIS / "q2_overlay.mp4",
    )


# ---------------- H-10：事实读取（全部纯函数/可注入） ----------------

def probe_engine_has_snow() -> bool:
    """实时探测 carla.WeatherParameters 是否有 snow 字段（引擎能力事实）。

    carla 不可导入即无法为「引擎无 snow 通道」举证——按 H-10 fail FATAL，
    不允许退回写死 False。
    """
    try:
        import carla  # type: ignore
    except Exception as exc:  # noqa: BLE001
        raise Q2FactError(f"carla 不可导入，无法核验 snow 字段：{exc}") from exc
    return hasattr(carla.WeatherParameters(), "snow")


def read_qback_default(optics_path: Path) -> float:
    """从 optics_model.py 源码读 QBACK_DEFAULT 常量（不 import sandbox 包——
    track2 目录无 __init__.py，且读源码常量能钉住「卡片数字 == 模型数字」）。"""
    try:
        text = optics_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise Q2FactError(f"Qback 来源缺失 {optics_path}: {exc}") from exc
    m = re.search(r"^QBACK_DEFAULT\s*=\s*([0-9]+(?:\.[0-9]+)?)", text, re.M)
    if m is None:
        raise Q2FactError(f"{optics_path} 未找到 QBACK_DEFAULT 常量定义")
    return float(m.group(1))


def ffprobe_duration(video: Path, runner=None) -> float:
    """ffprobe 实读时长（秒）；runner 可注入（测试不触网不起进程）。"""
    def _run(argv):
        return subprocess.run(argv, check=True, capture_output=True,
                              text=True, timeout=60)

    run = runner or _run
    ffprobe = shutil.which("ffprobe") or "/usr/bin/ffprobe"
    try:
        proc = run([ffprobe, "-v", "error", "-show_entries",
                    "format=duration", "-of", "default=nw=1:nk=1",
                    str(video)])
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) \
            as exc:
        raise Q2FactError(f"ffprobe 读时长失败 {video}: {exc}") from exc
    raw = (proc.stdout if hasattr(proc, "stdout") else proc[0]).strip()
    try:
        dur = float(raw)
    except ValueError as exc:
        raise Q2FactError(f"ffprobe 时长无法解析：{raw!r}") from exc
    if not (dur > 0):
        raise Q2FactError(f"ffprobe 时长非正：{dur}")
    return dur


_SUMMARY_SPEC_RE = re.compile(
    r"(\d+)\s*线\s*/\s*(\d+)\s*°\s*水平\s*FOV\s*/\s*(\d+)\s*m\s*/\s*"
    r"(\d+)\s*kpps\s*/\s*(\d+)\s*Hz"
)
_SUMMARY_QBACK_RE = re.compile(r"Qback=([0-9]+(?:\.[0-9]+)?)")


def read_q2_facts(paths: Q2Paths, *, ffprobe_runner=None,
                  snow_probe=probe_engine_has_snow) -> Q2Facts:
    """三源交叉读取卡片事实；任何缺失/不一致即 Q2FactError（H-10 FATAL）。"""
    try:
        meta = json.loads(paths.meta_rep1.read_text(encoding="utf-8"))
        summary = paths.summary_md.read_text(encoding="utf-8")
    except (OSError, json.JSONDecodeError) as exc:
        raise Q2FactError(f"meta/SUMMARY 读取失败：{exc}") from exc

    geo = meta.get("lidar_attrs", {}).get("geo")
    sem = meta.get("lidar_attrs", {}).get("sem")
    if not geo or not sem:
        raise Q2FactError("meta_rep1.json 缺 lidar_attrs.geo/sem")
    # 双雷达同位：两通道规格必须逐字段一致，否则「同位双雷达」叙述不成立。
    for key in ("channels", "horizontal_fov", "range",
                "points_per_second", "rotation_frequency"):
        if geo.get(key) != sem.get(key):
            raise Q2FactError(
                f"geo/sem 雷达规格不一致：{key} {geo.get(key)!r}!={sem.get(key)!r}")
    try:
        facts_core = {
            "channels": int(geo["channels"]),
            "horizontal_fov_deg": int(geo["horizontal_fov"]),
            "range_m": int(float(geo["range"])),
            "points_per_second": int(float(geo["points_per_second"])),
            "rotation_hz": int(float(geo["rotation_frequency"])),
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise Q2FactError(f"meta 雷达规格字段无法解析：{exc}") from exc

    # SUMMARY.md 是随实验产出的人工可读结论：与 meta 交叉核对规格行。
    mspec = _SUMMARY_SPEC_RE.search(summary)
    if mspec is None:
        raise Q2FactError("SUMMARY.md 未找到雷达规格行（线/FOV/range/kpps/Hz）")
    ch, fov, rng, kpps, hz = (int(x) for x in mspec.groups())
    if (ch, fov, rng, kpps * 1000, hz) != (
            facts_core["channels"], facts_core["horizontal_fov_deg"],
            facts_core["range_m"], facts_core["points_per_second"],
            facts_core["rotation_hz"]):
        raise Q2FactError(
            f"SUMMARY 规格 ({ch}线/{fov}°/{rng}m/{kpps}kpps/{hz}Hz) 与 meta "
            f"({facts_core}) 不一致")

    qback = read_qback_default(paths.optics_model)
    mq = _SUMMARY_QBACK_RE.search(summary)
    if mq is None:
        raise Q2FactError("SUMMARY.md 未找到 Qback= 字面量，无法交叉核对")
    qback_summary = float(mq.group(1))
    if abs(qback - qback_summary) > 1e-12:
        raise Q2FactError(
            f"Qback 不一致：optics_model={qback} vs SUMMARY={qback_summary}")

    duration = ffprobe_duration(paths.video, ffprobe_runner)
    return Q2Facts(
        **facts_core,
        video_duration_s=duration,
        qback=qback,
        engine_has_snow=bool(snow_probe()),
    )


# ---------------- H-19：卡片内容纯函数 ----------------

def build_explanation(facts: Q2Facts) -> dict[str, str]:
    dur_n = int(round(facts.video_duration_s))
    return {
        "task": (f"面试题第 2 题：无人物流车 {facts.channels} 线固态式 LiDAR "
                 f"在雨/雪/雾天的点云噪声。真实 Carla 0.10.0 服务端实验：静态 "
                 f"MKZ、Town10HD_Opt、ray_cast 与 ray_cast_semantic 同位双雷达"
                 f"（{facts.horizontal_fov_deg}°/{facts.range_m}m/"
                 f"{facts.points_per_second // 1000}kpps），clear/rain_hard/"
                 f"fog_dense 三天气，同步模式、独立进程 3 重复。"),
        "problem": ("分两层验收，分开报告不调参：①引擎原生 LiDAR 到底有没有"
                    "天气物理通道（outcome，用 bit 级对照回答）；②若没有，"
                    "引擎外物理光学材质层（双程 Beer–Lambert、Marshall–Palmer "
                    "雨谱、Gunn–Marshall 雪谱、Koschmieder 雾）给出的天气"
                    "效应量级与单调性是否合理（mechanism）。"),
        "video": (f"q2_overlay.mp4，约 {dur_n} 秒（1fps）俯视点云三分屏：左＝Carla "
                  "clear 原生基线；中＝叠加 rain 100mm/h 光学层（灰＝保留的"
                  "表面点，红＝is_weather 标签的天气伪回波，另有近场盲丢）；"
                  "右＝fog V=6m，表面点近盲。核心看点：三天气下左侧原生几何"
                  "应当逐点重合（80 组语义射线逐 bit 相等），天气效应只来自"
                  "中间叠加的物理光学层。"),
    }


def _mechanism_rows(sweep_path: Path) -> list[dict]:
    rows: list[dict] = []
    with sweep_path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            label = _WANTED_CONDITIONS.get(row["condition"])
            if label is None:
                continue
            rows.append({
                "label": label,
                "surface_kept_pct": float(row["frac_surface_kept_mean"]) * 100,
                "weather_false": float(row["n_weather_false_mean"]),
                "near_blind": (float(row["n_near_blind_mean"])
                               if float(row["n_near_blind_mean"]) > 0 else None),
                "transmission": float(row["transmission_mean_mean"]),
            })
    if len(rows) != 3:
        raise Q2FactError(f"optics 三档缺：{[r['label'] for r in rows]}")
    return rows


def build_metrics(inv: dict, ctl: dict, mech: list[dict],
                  facts: Q2Facts) -> dict:
    """数字全部从 Q2 真实产物实读 + H-10 事实注入；模板内不写字面量。"""
    geo = inv["semantic_geometry_bit_identical"]
    cross = inv["geo_cross_repeat_bit_identical"]
    z = inv["geo_count_z_vs_clear"]
    zc = ctl["geo_count_z_vs_clear"]
    di = max(x["max_abs_dI"] for x in inv["survivor_intensity_identity"])
    return {
        "metrics_kind": "q2_invariance_report",
        "outcome": {
            "bit_identical": {
                "comparisons": geo["comparisons"], "failures": len(geo["failures"]),
                "cross_repeat": cross["comparisons"]},
            "z_vs_clear": {"rain_hard": z["rain_hard"],
                           "fog_dense": z["fog_dense"]},
            "z_negative_control": {"rain_hard": zc["rain_hard"],
                                   "fog_dense": zc["fog_dense"]},
            "survivor_max_abs_dI": f"{di:.2e}",
            # H-10：实时探测，不再写死 False。
            "engine_has_snow": facts.engine_has_snow,
        },
        "mechanism": mech,
        "residual": (
            f"Qback={facts.qback:g}、湿沥青 BRDF 0.7、905nm 雾消光等参数未用"
            "真实天气 LiDAR 数据标定，模型只断言量级与单调性；静态单车/单地图/"
            "单 spawn，未引入交通流与多径。"),
        "d9_note": ("Q2 按计划 D9 走条件闸门（不经 LangGraph 图、无 Gate1 方案卡），"
                    "原仅凭用户 2026-10-16 书面授权执行；本卡是应要求补的真人 "
                    "Gate2 点验，全程 0 次方舟付费调用。"),
    }


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def evaluate_outcome(status: str, decision, source, paid_delta: int) -> int:
    """H-19 退出矩阵纯函数：0 approve / 3 reject / 1 任何异常态。

    ledger 增量非 0 永远优先判 1（声称 0 付费不可被后续成功掩盖）。
    """
    if paid_delta != 0:
        return 1
    if status != "DECIDED" or source != "card_form":
        return 1
    return 0 if decision == "approve" else 3


# ---------------- 主流程 ----------------

class GateLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lines: list[str] = []

    def log(self, line: str) -> None:
        stamped = f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {line}"
        self.lines.append(stamped)
        print(stamped, flush=True)

    def flush(self) -> None:
        # H-9：日志同样原子写、0600。
        from orchestrator.common.fsutil import atomic_write_text
        atomic_write_text(self.path, "\n".join(self.lines) + "\n", mode=0o600)


def _offgraph_learning_writer(decision_path: Path) -> dict:
    """纪律 12 默认通路：真人 approve 落 decision.json 后图外补沉淀一行。

    evidence_relpath=None 让 LearningStore 按双根自动换算（生产 decision.json
    在 $SIMPRAXIS_RUN_DIR 下 → @runs/ 前缀 relpath）；测试注入 offgraph_writer
    旁路，绝不误写真实学习表。
    """
    from memory_flywheel.phase1.offgraph_learning import write_offgraph_learning
    return write_offgraph_learning(
        decision_json=decision_path, evidence_relpath=None)


def run(
    paths: Q2Paths,
    *,
    gateway_factory=None,
    count_paid=None,
    ffprobe_runner=None,
    snow_probe=probe_engine_has_snow,
    offgraph_writer=_offgraph_learning_writer,
) -> int:
    """发卡主流程（环境守卫在 main；测试直接调本函数注入依赖）。"""
    from orchestrator.common.fsutil import atomic_write_text, ensure_private_dir
    from orchestrator.feishu_gateway.client import gateway_from_env
    from orchestrator.gateway.quota_ledger import call_lines, read_lines

    gateway_factory = gateway_factory or gateway_from_env
    # 日志/裁决落盘目录：0700 预建（原子写不会替我们 mkdir）。
    ensure_private_dir(paths.log_path.parent)
    if count_paid is None:
        def count_paid() -> int:  # noqa: E306
            return len(call_lines(read_lines()))

    glog = GateLog(paths.log_path)
    required = [
        paths.video, paths.summary_md,
        paths.analysis / "invariance_checks.json", paths.control_invariance,
        paths.analysis / "optics_sweep.csv", paths.gates_db,
        paths.meta_rep1, paths.optics_model,
    ]
    missing = [p for p in required if not p.is_file()]
    if missing:
        for p in missing:
            print(f"refuse: 证据缺失 {p}", file=sys.stderr)
        return 1

    # H-5：decision.json 已存在即拒（重跑必须走新一轮线程，不覆盖真人结论）。
    if paths.decision_path.is_file():
        print(f"refuse: {paths.decision_path} 已存在：{THREAD_ID} 已有点验结论，"
              "禁止重发覆盖", file=sys.stderr)
        return 1

    # H-10：事实先读、交叉核对；任何不一致 FATAL，绝不带写死字面量发卡。
    try:
        facts = read_q2_facts(paths, ffprobe_runner=ffprobe_runner,
                              snow_probe=snow_probe)
        inv = json.loads((paths.analysis / "invariance_checks.json")
                         .read_text(encoding="utf-8"))
        ctl = json.loads(paths.control_invariance.read_text(encoding="utf-8"))
        mech = _mechanism_rows(paths.analysis / "optics_sweep.csv")
        metrics = build_metrics(inv, ctl, mech, facts)
    except Q2FactError as exc:
        glog.log(f"[q2-gate] FATAL：事实核验失败，不发卡：{exc}")
        glog.flush()
        return 1

    snapshot = {
        "invariance_checks_sha256":
            sha256(paths.analysis / "invariance_checks.json"),
        "control_invariance_sha256": sha256(paths.control_invariance),
        "optics_sweep_sha256": sha256(paths.analysis / "optics_sweep.csv"),
        "summary_md_sha256": sha256(paths.summary_md),
    }
    paid_before = count_paid()
    glog.log(f"[q2-gate] 开始：thread={THREAD_ID}；ledger 实呼行数={paid_before}"
             f"（本流程必须保持不变）")
    glog.log(f"[q2-gate] 事实核验通过：{facts.channels}线/"
             f"{facts.horizontal_fov_deg}°/{facts.range_m}m/"
             f"{facts.points_per_second // 1000}kpps/"
             f"{facts.rotation_hz}Hz，时长={facts.video_duration_s:g}s，"
             f"Qback={facts.qback:g}，engine_has_snow={facts.engine_has_snow}")
    glog.log(f"[q2-gate] 证据 sha256：video={sha256(paths.video)[:16]} "
             f"summary={snapshot['summary_md_sha256'][:16]} "
             f"invariance={snapshot['invariance_checks_sha256'][:16]} "
             f"control={snapshot['control_invariance_sha256'][:16]}")
    glog.flush()

    feishu = gateway_factory()
    try:
        # H-5：先查主表 UNION history 的终态裁决（必须在 claim 之前——否则
        # 主表为空时 claim 会越过已归档到 history 的 approve 插入新 PENDING）。
        if (feishu.store.decision_exists(THREAD_ID, 2, "approve")
                or feishu.store.decision_exists(
                    THREAD_ID, 2, "reject_with_comments")):
            print(f"refuse: {THREAD_ID} 已有终态裁决（主表或 history），"
                  "禁止重发覆盖", file=sys.stderr)
            return 1
        # 再做发卡前事务占位；已有 PENDING 行（并发调用方/崩溃遗留）直接退出，
        # 第二个调用方绝不重推视频。
        if not feishu.store.claim_pending(THREAD_ID, 2, task_title=TITLE):
            print(f"refuse: {THREAD_ID} 已有 PENDING 占位（另一并发调用方"
                  "或崩溃遗留；请用 CLI decide 裁决后再处理）", file=sys.stderr)
            return 1
        glog.log("[q2-gate] 占位成功；发送原生视频 + Gate2 卡片到真人飞书，"
                 "等待按钮裁决（approve / reject_with_comments；系统绝不代决）…")
        glog.flush()
        # H-2：三分屏无单一接触时刻（contact_moment_s=None）；原片时长 ffprobe
        # 实读后透传，未走到 step4 时卡片只显「原片 Ns」。
        trim = {
            "trim_start": 0.0,
            "trim_seconds": 30.0,
            "contact_moment_s": None,
            "source_duration_s": facts.video_duration_s,
        }
        decision = feishu.request_gate2_approval(
            THREAD_ID, TITLE, metrics,
            video_path=str(paths.video),
            explanation=build_explanation(facts),
            trim=trim)
        # 连接关闭前取落库行
        grow = feishu.store.get_pending(THREAD_ID, 2)
    finally:
        feishu.close()

    glog.log(f"[q2-gate] 真人裁决返回：{json.dumps(decision, ensure_ascii=False)}")
    if grow is None:
        glog.log("[q2-gate] FATAL：裁决返回但 DB 无落库行")
        glog.flush()
        return 1
    paid_after = count_paid()
    glog.log(f"[q2-gate] DB：status={grow['status']} decision={grow['decision']} "
             f"source={grow['source']} notes={grow['human_notes']!r} "
             f"card={grow['card_message_id']} media={grow['media_message_id']}；"
             f"ledger 实呼 {paid_before}→{paid_after}")

    result = {
        "schema": "simpraxis.q2_human_gate.v1",
        "thread_id": THREAD_ID,
        "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "gate_path": "D9 条件闸门补 Gate2 点验（无 Gate1；用户 2026-10-16 指示）",
        "task_title": TITLE,
        "paid_llm_calls": {"before": paid_before, "after": paid_after,
                           "delta": paid_after - paid_before},
        "verified_facts": {
            "channels": facts.channels,
            "horizontal_fov_deg": facts.horizontal_fov_deg,
            "range_m": facts.range_m,
            "points_per_second": facts.points_per_second,
            "rotation_hz": facts.rotation_hz,
            "video_duration_s": facts.video_duration_s,
            "qback": facts.qback,
            "engine_has_snow": facts.engine_has_snow,
        },
        "metrics": metrics,
        "evidence_sha256": {"q2_overlay.mp4": sha256(paths.video), **snapshot},
        "human_decision": {
            "decision": grow["decision"], "choice": grow["choice"],
            "human_notes": grow["human_notes"], "source": grow["source"],
            "card_message_id": grow["card_message_id"],
            "media_message_id": grow["media_message_id"],
            "open_chat_id": grow["open_chat_id"]},
    }
    # H-9：0600 tmp+replace 原子写（fsync 后 os.replace），中途崩溃不留半截 JSON。
    atomic_write_text(paths.decision_path,
                      json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                      mode=0o600)
    code = evaluate_outcome(grow["status"], grow["decision"], grow["source"],
                            paid_after - paid_before)
    if code == 1:
        if paid_after != paid_before:
            glog.log("[q2-gate] FATAL：本流程声称 0 付费但 ledger 行数变化，"
                     "结果如实保留，退出码 1")
        else:
            glog.log("[q2-gate] FATAL：非 card_form 终态，结果如实保留")
    elif code == 0:
        glog.log(f"[q2-gate] 真人 approve/card_form，结果落 {paths.decision_path}")
    else:
        glog.log(f"[q2-gate] 真人打回（{grow['decision']}），意见："
                 f"{grow['human_notes']!r}；不掩盖、不重发")
    if code == 0:
        # Phase 2 Task0a（纪律 12）：图外补点验同责沉淀——真人 approve/card_form
        # 后经 offgraph 通路补一行学习（gate2_accepted_supplementary, rung=1），
        # 今后不允许再出现 0 沉淀。decision.json 已原子落盘，回写失败不得假装
        # 成功：FATAL 退出并保留裁决件，由人工经 CLI 对既有 decision.json 补跑。
        try:
            learning = offgraph_writer(paths.decision_path)
        except Exception as exc:  # noqa: BLE001
            glog.log(f"[q2-gate] FATAL：图外学习回写失败（decision.json 已保留，"
                     f"需手工 CLI 补沉淀：python -m memory_flywheel.phase1."
                     f"offgraph_learning --decision {paths.decision_path}"
                     f" --relpath <@runs/.../decision.json>）：{exc}")
            glog.flush()
            return 1
        glog.log(f"[q2-gate] 图外学习回写完成（纪律 12）：id={learning['id']} "
                 f"gate_source={learning['gate_source']} rung={learning['rung']}")
    glog.flush()
    return code


def main() -> int:
    if os.environ.get("SIMPRAXIS_Q2_HUMAN_GATE") != "1":
        print("refuse: 必须显式 SIMPRAXIS_Q2_HUMAN_GATE=1（真实飞书发卡+真人"
              "阻塞，禁止误触发与任何自动批准）", file=sys.stderr)
        return 1
    for name in ("FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_RECEIVE_ID"):
        if not os.environ.get(name):
            print(f"refuse: {name} 未配置（先 source scripts/00_env.sh）",
                  file=sys.stderr)
            return 1
    sys.path.insert(0, str(_REPO_ROOT))
    return run(default_paths())


if __name__ == "__main__":
    sys.exit(main())
