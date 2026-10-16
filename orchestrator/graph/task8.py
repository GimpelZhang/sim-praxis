#!/usr/bin/env python3
"""Task8 Track2 生产装配：Genesis 结冰/油污 DR 对照 batch 的图接缝。

计划锚点（Phase_1_Plan_detailed.md L561-572）：direction→Gate1（真人）→
agent_run（batch：三臂 normal/ice_nominal/ice_dr ×3 种子）→evaluate
（gait_rungs 三级文件判定 + 两个对照分屏视频的 Rung4）→Gate2（真人看分屏
视频 + CSV 对照裁决）。与 task7.py 的差异：

- RunSpec 的成功产物是**聚合 batch**（summary.json + CSV + 分屏 mp4 + 拼图），
  不是单 rollout telemetry/video；coding 补丁的目标仍是 workdir 内 stage 的
  genesis_humanoid_dr.py，≤5 轮/run（D10）。
- 规则判定走 gait_rungs.evaluate_batch（不信脚本 outcomes，从 series 独立
  重算；只判证据可信度，科学结论放 contrast），Rung4 用步态专用 rubric +
  独立标定件（未标定时零实呼）。
- 物理矛盾（对照组不稳健 / DR 未改善）按 L571「矛盾本身是结论」原样进卡，
  不存在「实验组必须更好」的通过阈值。

本模块零网络、零付费：实呼只发生在 ModelGateway（macro 在外层账套，
coding/visual 在各自接缝内 task=8 账套），单测全 monkeypatch。
"""
from __future__ import annotations

import dataclasses
import json
import os
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import Any, Callable

from orchestrator.agent_runner.runner import RunSpec
from orchestrator.common.callcache import CallReplay, request_fingerprint
from orchestrator.common.clock import utc_now_iso_z
from orchestrator.common.fsutil import atomic_write_bytes, atomic_write_text
from orchestrator.gateway.quota_ledger import (
    billing_context,
    phase1_root,
    set_current_evidence,
)
from orchestrator.gateway.router import redact
from orchestrator.graph.nodes import GraphStateError
from orchestrator.judge.collage import (
    build_collage_png,
    collage_b64,
    extract_frames_mp4,
    select_frame_indices,
)
from orchestrator.judge.gait_calibrate import (
    gait_calibration_file,
    load_gait_rubric,
)
from orchestrator.judge.gait_rungs import evaluate_batch
from orchestrator.judge.gait_thresholds import (
    CONTRAST_RATIO,
    FALL_TILT_DEG,
    FALL_Z_M,
    MIN_SEEDS,
)
from orchestrator.judge.rung4 import Rung4Judge
from orchestrator.solutions import SolutionError, resolve_solution

TASK = 8

# Gate1 批准的 sim_engine slug（能力矩阵 slug）→ Track2 DR 解题方案 id。
# Task0b 三层分离：题目脚本正本（v1 run 执行副本）已迁 KB 公开仓 solutions/，
# 引擎仓只经 orchestrator.solutions.resolver 按 manifest 校验后 stage。
# Track2 Q1 只接 genesis；Q2 Carla 是条件闸门（D9），不经本图运行。
ENGINE_SCENARIOS: dict[str, dict] = {
    "genesis": {
        "solution_id": "track2_questions/01_ice_floor_humanoid_dr",
        "env": {"GS_ENABLE_ZEROCOPY": "0", "PYOPENGL_PLATFORM": "egl"},
    },
}

# 35N×0.2s 选窗标定件 v2（sw2–sw4 证据 + 四格跨进程逐位复验），batch 运行前
# stage 进 workdir；rung1 对批内副本做 schema/选窗/bit-exact 校验。
CALIBRATION_SOURCE = Path(
    "/mnt/caches/simpraxis/genesis_probe/push_window_calibration.json")
CALIBRATION_NAME = "push_window_calibration.json"
CALIBRATION_SCHEMA_ID = "simpraxis.task8.track2.push_window_calibration.v2"

SUMMARY_NAME = "summary.json"
COVER_NAME = "cover.png"
MANIFEST_NAME = "manifest.json"
CSV_NAME = "dr_controls_vs_experimental.csv"
SEEDS = MIN_SEEDS

# (卡片/Gate2 用键, 文件名)：两张分屏各做一次 Rung4；第一张是 Gate2 主视频
# （Case A 核心对照：正常地面策略 vs 同名臂在冰面）。
SPLITS: tuple[tuple[str, str], ...] = (
    ("normal_vs_ice_nominal", "split_normal_vs_ice_nominal_s0.mp4"),
    ("ice_nominal_vs_ice_dr", "split_ice_nominal_vs_ice_dr_s0.mp4"),
)
PRIMARY_VIDEO = SPLITS[0][1]

VENV_PY = "/mnt/venvs/simpraxis/bin/python"
# batch = 9 rollout × ~8 s 仿真 + 容器内 GS 内核编译/init；pregate 实测
# ~12-16 min；2400 s 含余量（coding 重跑每轮都是完整 batch）。
BATCH_TIMEOUT_S = 2400


def runs_root() -> Path:
    return Path(os.environ.get("SIMPRAXIS_RUN_DIR",
                               "/mnt/artifacts/simpraxis/runs"))


def task8_run_dir(thread_id: str, run_idx: int) -> Path:
    """真实 batch 工作目录：$SIMPRAXIS_RUN_DIR/phase1/task8_track2/q1_genesis/。"""
    d = runs_root() / "phase1" / "task8_track2" / "q1_genesis" \
        / f"run_{thread_id}_{run_idx}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _last_event(state: dict, kind: str) -> "dict | None":
    for ev in reversed(state.get("events", [])):
        if ev.get("type") == kind:
            return ev
    return None


def _installed_versions() -> str:
    pkgs = sorted((f"{d.metadata['Name']}=={d.version}"
                   for d in importlib_metadata.distributions()
                   if d.metadata and d.metadata.get("Name")),
                  key=str.lower)
    return "\n".join(pkgs) + "\n"


def _stage_calibration(dst: Path) -> None:
    """拷入 v2 标定件并做最小源头校验（rung1 在批内副本上做完整校验）。"""
    if not CALIBRATION_SOURCE.is_file():
        raise GraphStateError(
            f"Task8 选窗标定件不存在：{CALIBRATION_SOURCE}；"
            "需先在 /mnt/caches/simpraxis/genesis_probe 生成 v2 标定")
    try:
        cal = json.loads(CALIBRATION_SOURCE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise GraphStateError(f"Task8 标定件不可读/非法 JSON：{exc}") from exc
    if cal.get("schema") != CALIBRATION_SCHEMA_ID:
        raise GraphStateError(
            f"Task8 标定件 schema 不符：{cal.get('schema')!r}，"
            f"要求 {CALIBRATION_SCHEMA_ID}")
    # S-6：copy→chmod 收口 fsutil 原子写（读字节→0600 tmp+replace）。
    atomic_write_bytes(dst, CALIBRATION_SOURCE.read_bytes())


def make_spec_factory(thread_id: str, coding_batch: str,
                      task_title: str = "") -> Callable[[dict], RunSpec]:
    """agent_run 接缝的 spec 工厂：Gate1 必须批准 genesis，否则协议外即报错。

    Gate2 打回后的再跑使用全新 run 目录（run_idx 递增），人工意见经
    gate2_notes 注入，脚本是干净新副本，前一 run 的 coding 补丁不继承。
    """
    def factory(state: dict) -> RunSpec:
        chosen = state.get("chosen") or {}
        engine = chosen.get("sim_engine")
        scenario = ENGINE_SCENARIOS.get(engine)
        if scenario is None:
            raise GraphStateError(
                f"Task8 Track2 Q1 场景映射缺 sim_engine={engine!r}；"
                f"Q1 只支持 {sorted(ENGINE_SCENARIOS)}（genesis），"
                "Carla 属 Q2 条件闸门（D9），不经本图")
        # L-12 编号钉死（两阶段索引关系，禁止改号）：工厂在 agent_runs 追加
        # 之前预占 len(agent_runs)+1；evaluate 接缝在追加之后用 len(agent_runs)
        # 复核（见 make_evaluate_seam 注释）——同一 run 两处同号（首 run=r1）。
        run_idx = len(state.get("agent_runs", [])) + 1
        workdir = task8_run_dir(thread_id, run_idx)

        # Task0b：方案脚本在内容层（$KB_ROOT/solutions，正本=v1 run 副本），
        # resolver 做 manifest/sha256/C4 全量校验后 stage（已存在落件不覆盖，
        # coding 补丁跨 spec 重建保留）；违规 fail-closed 成 GraphStateError。
        try:
            staged = resolve_solution(
                scenario["solution_id"]).stage(workdir)
        except SolutionError as exc:
            raise GraphStateError(
                f"Task8 方案 stage 失败（{scenario['solution_id']}）：{exc}"
            ) from exc
        target = staged.entrypoint
        versions = workdir / "versions.txt"
        if not versions.exists():
            atomic_write_text(versions, _installed_versions())
        cal = workdir / CALIBRATION_NAME
        if not cal.exists():
            _stage_calibration(cal)

        ev = _last_event(state, "gate2_rejected")
        notes = ev["human_notes"] if ev else None
        env_extra = {
            # GS_ENABLE_ZEROCOPY=0：默认 zerocopy 路径 int32 gather 抛错的
            # 必需正确性开关（根因堆栈见生产脚本 docstring(3)）；HOME 指向
            # 持久缓存（大文件只落 /mnt）；HF 走镜像，构建/运行直连。
            "HOME": "/mnt/caches/home",
            "HF_HOME": "/mnt/caches/hf",
            "HF_ENDPOINT": "https://hf-mirror.com",
            "SIMPRAXIS_USE_PROXY": "0",
        }
        env_extra.update(scenario["env"])

        return RunSpec(
            thread_id=thread_id,
            workdir=workdir,
            cmd=[VENV_PY, str(target), "batch",
                 "--out-dir", str(workdir),
                 "--seeds", str(SEEDS),
                 "--calibration-json", str(cal)],
            telemetry_relpath=SUMMARY_NAME,
            run_idx=run_idx,             # L-1：coding 重放 key 稳定序号
            batch_id=coding_batch,
            billing_task=TASK,
            gate2_notes=notes,
            env_extra=env_extra,
            run_timeout_s=BATCH_TIMEOUT_S,
            task_title=task_title or "Task8 Track2 Genesis 结冰/油污 DR 对照",
            video_relpath=PRIMARY_VIDEO,
            cover_relpath=COVER_NAME,
            manifest_relpath=MANIFEST_NAME,
        )

    return factory


# ---------------------------------------------------------------------------
# evaluate 接缝：gait_rungs 三级文件判定 + 两张分屏的 Rung4
# ---------------------------------------------------------------------------

def _evidence_dir() -> Path:
    d = phase1_root() / "task8_track2" / "evidence"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_evidence(stem: str, kind: str, payload: dict) -> str:
    p = _evidence_dir() / f"{stem}.{kind}.json"
    # S-6：原子写 0600 收口 fsutil。
    atomic_write_text(p, json.dumps(payload, ensure_ascii=False, indent=2))
    return f"phase1/task8_track2/evidence/{p.name}"


class _VisualEvidenceGateway:
    """同 task7._VisualEvidenceGateway：call_visual 前后落脱敏证据，
    evidence 在 billing_context 内挂接（task=8 visual 批次）。

    L-1：稳定 key task8_rung4_{run_id}_{split} + 请求指纹的调用级重放；
    命中零付费零证据写。L-7：实呼异常在 except 内补脱敏失败 resp 后抛出。
    """

    KIND = "task8_split_rung4"
    REF_PREFIX = "phase1/task8_track2/evidence"

    def __init__(self, gateway, *, thread: str, run_id: str, batch_id: str,
                 split_key: str, collage_path: str,
                 cache: "CallReplay | None" = None,
                 cache_key: "str | None" = None):
        self._g = gateway
        self._thread = thread
        self._run_id = run_id
        self._batch = batch_id
        self._split = split_key
        self._collage = collage_path
        self._cache = cache
        self._cache_key = cache_key
        self.req_ref: "str | None" = None

    def model_id(self, stage: str) -> str:
        return self._g.model_id(stage)

    @staticmethod
    def _usage_ints(usage: Any) -> "tuple[int, int] | None":
        if (isinstance(usage, (list, tuple)) and len(usage) == 2
                and all(isinstance(v, int) and not isinstance(v, bool)
                        and v >= 0 for v in usage)):
            return int(usage[0]), int(usage[1])
        return None

    def call_visual(self, prompt: str, image_b64: str, **kwargs):
        stem = f"rung4_{self._run_id}_{self._split}"
        model = self._g.model_id("visual_rollout")
        # S-8：指纹并入 stage/model，同 prompt 跨阶段/跨模型不得错配重放。
        fp = request_fingerprint(prompt, image_b64, dict(kwargs),
                                 stage="visual_rollout", model=model)
        if self._cache is not None and self._cache_key is not None:
            hit = self._cache.get(self._cache_key, fp)
        else:
            hit = None
        if hit is not None:
            # 重放命中：零付费、零证据写；req_ref 用首调确定性证据名补戳。
            self.req_ref = f"{self.REF_PREFIX}/{stem}.req.json"
            return hit["raw"], tuple(hit["usage"])

        req_ref = _write_evidence(stem, "req", {
            # P2（Task0）：Phase 2 新证据行统一严格 UTC Z。
            "ts_iso": utc_now_iso_z(),
            "stage": "visual_rollout",
            "model": model,
            "thread_id": self._thread, "batch_id": self._batch,
            "run_id": self._run_id, "split": self._split,
            "kind": self.KIND,
            "collage_png": self._collage, "image_b64_len": len(image_b64),
            "max_tokens": kwargs.get("max_tokens", 512),
            "prompt": redact(prompt),
        })
        self.req_ref = req_ref
        with billing_context(task=TASK, batch_id=self._batch,
                             thread_id=self._thread):
            set_current_evidence(req_ref)
            try:
                raw, usage = self._g.call_visual(prompt, image_b64, **kwargs)
            except Exception as exc:
                # L-7：账行已由网关记账；补脱敏失败 resp 证据后原样抛出。
                _write_evidence(stem, "resp",
                                {"ok": False, "error": redact(str(exc))[:1000]})
                raise
        _write_evidence(stem, "resp",
                        {"ok": True, "text": redact(raw),
                         "tokens_in": usage[0], "tokens_out": usage[1]})
        if self._cache is not None and self._cache_key is not None:
            usage_ints = self._usage_ints(usage)
            if usage_ints is not None:
                self._cache.put(self._cache_key, kind=self.KIND, model=model,
                                raw=raw, usage=usage_ints, fingerprint=fp)
        return raw, usage


def _json_safe(obj: Any) -> Any:
    """checkpointer/JSON 卡只吃可序列化值：兜底 numpy 标量等。"""
    return json.loads(json.dumps(obj, default=_coerce_scalar))


def _coerce_scalar(o: Any) -> Any:
    # 不 import numpy：按类名 duck-typing（np.bool_/np.floating/np.integer）。
    name = getattr(getattr(o, "__class__", None), "__name__", "")
    if name == "bool_":
        return bool(o)
    if name in ("floating", "float32", "float64"):
        return float(o)
    if name in ("integer", "int32", "int64"):
        return int(o)
    if name == "ndarray":
        return o.tolist()
    return float(o)


def _aggregate_rung4(split_results: list[dict]) -> dict:
    """两次分屏判定的卡片级聚合（Rung4 永不单独放行）。

    mode/status 取最保守者：任一 uncalibrated→uncalibrated（实际两调用共用
    同一标定件，mode 必相同），任一 vlm_error→vlm_error；缺陷数/低置信/分歧
    取并；active 下的分歧/低置信/错误驱动 escalate_human（D8）。
    """
    items: list[dict] = []
    n_defects = 0
    low = False
    disagreement = None
    mode = "uncalibrated"
    active = True
    any_error = False
    all_ok = True
    for sr in split_results:
        d = sr["result"].as_dict()
        if d["mode"] != "uncalibrated":
            mode = d["mode"]
        if d["status"] == "vlm_error":
            any_error = True
        if d["status"] != "ok":
            all_ok = False
        active = active and d["mode"] == "active"
        low = low or bool(d["low_confidence"])
        if d.get("disagreement") and disagreement is None:
            disagreement = f"{sr['split']}: {d['disagreement']}"
        for it in d.get("items", []):
            items.append({"split": sr["split"], **it})
            n_defects += 1 if it.get("defect") else 0
    # judge 在标定存在时只产出 ok/vlm_error 两种 status；mixed 仅为防御性聚合。
    if mode == "uncalibrated":
        status = "uncalibrated"
    elif any_error:
        status = "vlm_error"
    else:
        status = "ok" if all_ok else "mixed"
    return {
        "status": status, "mode": mode, "items": items,
        "n_defects": n_defects, "n_items": len(items),
        "low_confidence": low, "disagreement": disagreement,
        # 两分屏各有自己的 req 引用（见 splits[].raw_text_ref）；聚合级无单引用。
        "raw_text_ref": None,
        "splits": [{"split": sr["split"], "collage_png": sr["collage_png"],
                    **sr["result"].as_dict()} for sr in split_results],
    }


def make_evaluate_seam(gateway, visual_batch: str, thread_id: str):
    """evaluate 节点接缝：只读 batch 真实产物；缺 CSV/分屏即协议违规。"""
    def seam(state: dict) -> dict:
        uri = state.get("telemetry_uri")
        if not uri:
            raise GraphStateError("task8 evaluate：state 无 telemetry_uri")
        summary = Path(uri)
        if not summary.is_file():
            raise GraphStateError(f"task8 evaluate：summary 不存在：{uri}")
        workdir = summary.parent

        result = evaluate_batch(workdir)
        # L-6：telemetry/CSV 结构错误必须在任何 Rung4 实呼之前熔断（未标定
        # 模式虽不发网，也不得继续装配）；同时避免下面 result["vector"] 等键
        # 在 evaluate_batch 短路返回（无 vector）上抛裸 KeyError。
        if result.get("schema_errors"):
            raise GraphStateError(
                "task8 evaluate：telemetry/CSV 结构校验失败，"
                "禁止发 Rung4 视觉调用："
                + "；".join(map(str, result["schema_errors"][:10])))

        # 两张对照分屏是 Gate2 的核心证据：缺一是协议违规，不许降级放行。
        missing = [name for _, name in SPLITS
                   if not (workdir / name).is_file()]
        if missing:
            raise GraphStateError(
                f"task8 evaluate：对照分屏视频缺失 {missing}（目录 {workdir}）")

        runs = state.get("agent_runs", [])
        # L-12：节点在 agent_run 追加之后执行，len(runs) 与工厂预占 +1 同号
        # （首 run=r1）；与 make_spec_factory 注释配对，禁止改号。
        run_idx = len(runs)
        run_id = f"{thread_id}-r{run_idx}"

        # L-1：两张分屏各有稳定 key（task8_rung4_{run_id}_{split}），共用
        # 一个 0700 点目录缓存；kill-9 后整节点重放零二次付费。
        call_cache = CallReplay(phase1_root() / ".call_cache")
        rubric = load_gait_rubric()
        split_results: list[dict] = []
        for key, name in SPLITS:
            frames = extract_frames_mp4(str(workdir / name))
            # 分屏无单一「接触帧」：contact_index=None → 以片中段为中心六槽。
            idx = select_frame_indices(len(frames), None)
            labels = [f"frame {i:03d}" for i in idx]
            chosen = [frames[i] for i in idx]
            collage_png = workdir / f"rung4_collage_{key}.png"
            # S-6：原子写 0600 收口 fsutil。
            atomic_write_bytes(collage_png, build_collage_png(chosen, labels))
            image_b64 = collage_b64(chosen, labels)

            ev_gateway = _VisualEvidenceGateway(
                gateway, thread=thread_id, run_id=run_id,
                batch_id=visual_batch, split_key=key,
                collage_path=str(collage_png), cache=call_cache,
                cache_key=f"task8_rung4_{run_id}_{key}")
            judge = Rung4Judge(
                ev_gateway, rubric=rubric,
                calibration_file=gait_calibration_file())
            # 未标定 → judge 内部零实呼；record_only 照实呼、卡片照显但分歧
            # 标志强制清空；active 才进自动升级逻辑。
            r4 = judge.judge(image_b64, bool(result.get("rules_pass")))
            if r4.raw_text_ref is None and ev_gateway.req_ref is not None:
                r4 = dataclasses.replace(r4, raw_text_ref=ev_gateway.req_ref)
            split_results.append({"split": name, "result": r4,
                                  "collage_png": str(collage_png)})

        r4_agg = _aggregate_rung4(split_results)
        active = r4_agg["mode"] == "active"
        escalate = bool(
            not result.get("rules_pass")
            or (active and (r4_agg["disagreement"]
                            or r4_agg["low_confidence"]
                            or r4_agg["status"] == "vlm_error")))

        metrics = {
            "metrics_kind": "task8_gait_batch",
            "engine": result.get("engine", "genesis"),
            "run_id": run_id,
            "workdir": str(workdir),
            "rungs": {"rung1": result["rung1"],
                      "rung2": result["rung2"],
                      "rung3": result["rung3"],
                      "rung4": r4_agg},
            "vector": result["vector"],
            "rules_pass": result["rules_pass"],
            "auto_release": result["rules_pass"],
            "escalate_human": escalate,
            "schema_errors": result.get("schema_errors", []),
            "seeds": result.get("seeds"),
            "groups": result.get("groups"),
            "contrast": result.get("contrast"),
            "disturbance": result.get("disturbance"),
            "dr_envelope": result.get("dr_envelope"),
            "rung4": r4_agg,
            "provenance": result.get("provenance"),
            "artifacts": {
                "csv": CSV_NAME,
                "summary": SUMMARY_NAME,
                "calibration": CALIBRATION_NAME,
                "collage": "collage.png",
                "splits": [name for _, name in SPLITS],
            },
        }
        if r4_agg.get("disagreement"):
            metrics["banner"] = "规则/VLM 分歧：以 Rung1–3 规则为准，由人裁决（D8）"
        return _json_safe(metrics)

    return seam


# ---------------------------------------------------------------------------
# Gate2 卡片三问说明（口径同 task7：只写实验事实与判定口径，不读结果倒推；
# 阈值只引常量；具体 outcome 数字由卡片指标段从真实 metrics 渲染）
# ---------------------------------------------------------------------------

def build_gate2_explanation(state: dict, rec: dict) -> "dict[str, str] | None":
    chosen = state.get("chosen") or {}
    engine = chosen.get("sim_engine")
    if engine != "genesis":
        return None
    scheme = (chosen.get("name") or "（未命名方案）").strip()

    task = (
        f"Gate1 批准方案：{scheme}\n"
        "面试题 Case A：人形机器人在结冰/油污地面行走，RL 策略端不改，只在仿真端"
        "做接触域随机化（DR）。本 batch 在 Genesis 1.3.3 用同一反应踏步控制器"
        "（无策略学习；DR 只是环境鲁棒性包络）跑三臂各 3 个随机种子：\n"
        f"· normal：正常地面 μ=0.7；\n"
        "· ice_nominal：低摩擦 μ=0.05 的结冰地面（同名控制器直入）；\n"
        "· ice_dr：μ∈[0.05,0.4] 对数均匀、接触刚度 Kp/阻尼 Kd 非对称对数随机化、"
        "油膜微扰动力/力矩的 DR 包络。\n"
        "每 run 8 s、50 Hz telemetry，并在规定时刻施加 35 N×0.2 s 标准化水平推力"
        "（该推力窗口由 12 格预实验 + 跨进程逐位复验选定，标定件随批交付）。"
    )

    problem = (
        "它要回答的不是「脚本有没有跑通」，而是：结冰地面是否让同一控制器发生"
        "明显滑移/倾覆、DR 包络是否改变这个结果，以及这些证据本身可不可信。判据"
        "分四级：\n"
        f"· Rung1：九 run 产物齐套、全部数值有限、标定件选窗与逐位复验真实；\n"
        "· Rung2：不信脚本自报——从 50 Hz 时序独立重算跌倒帧/滑移距离/峰速/"
        "接触力，与 telemetry outcomes 和 CSV 逐 run 对质；\n"
        f"· Rung3：物理界与扰动交付核验（跌倒 z<{FALL_Z_M:g} m 或倾角"
        f">{FALL_TILT_DEG:g}°、滑移积分力门 2 N、推力必须确为 35 N×0.2 s）；\n"
        "· Rung4：视觉模型对两张对照分屏各做 6 帧几何/时序核片（冰面打滑与倾覆"
        "是预期物理、不算缺陷；只查穿地/瞬移/抖动/畸变）。\n"
        f"对照有序性是**预注册**判据（冰/干推力后峰速组均比 ≥{CONTRAST_RATIO:g}），"
        "写于正式 batch 之前。若结果与预期矛盾（对照组本身不稳健、或 DR 未改善"
        "甚至更差），按计划熔断口径：**禁止调参到好看为止，outcome 与 mechanism "
        "分开报告，矛盾本身就是结论**，由你裁决。Rung4 永不单独放行。"
    )

    video = (
        "发来的主视频是 split_normal_vs_ice_nominal_s0.mp4（seed 0 的左右分屏"
        "对照：左 normal（μ=0.7）、右 ice_nominal（μ=0.05），同一时刻并排，"
        "8 s）。第二张分屏 split_ice_nominal_vs_ice_dr_s0.mp4（ice_nominal vs "
        "ice_dr）、三臂×六帧拼图、接触力/滑移曲线图、九 run 指标 CSV 都在 batch "
        "证据目录（卡片指标段有目录与文件名）。请重点看：冰面侧足部是否明显滑移、"
        "倾覆过程是否连续可信，DR 侧与名义冰面侧有无可见差异；有没有穿地、瞬移、"
        "高频抖动等几何/时序不自洽。下方 Rung1–4 是对同一批数据的量化判定。"
    )
    dur_ms = rec.get("duration_ms")
    if isinstance(dur_ms, (int, float)) and dur_ms > 0:
        video += f"（主分屏约 {dur_ms / 1000:.0f} s）"
    return {"task": task, "problem": problem, "video": video}
