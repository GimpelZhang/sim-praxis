#!/usr/bin/env python3
"""Task7 Track1 生产装配：真实 run 的 spec_factory + Rung4 视频 evaluate 接缝。

计划锚点（Phase_1_Plan_detailed.md L546-559）：图整体在 sim-sandbox 容器内
跑（同路径 bind mount，ledger/批次凭据/检查点/证据宿主单份），direction→
Gate1→agent_run→evaluate→Gate2 全真实；Track 运行器命令由 Task7 提供
（L502「Track 运行器在 Task7/8 提供命令」）。

本模块零网络、零付费：实呼只发生在 ModelGateway（Rung4 唯一入口
call_visual，硬绑 visual_rollout），且要求 task=7 的批次凭据；单测全
monkeypatch。
"""
from __future__ import annotations

import dataclasses
import json
import os
import shutil
import time
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import Any, Callable

from orchestrator.agent_runner.runner import RunSpec, agent_run_seam
from orchestrator.common.callcache import CallReplay, request_fingerprint
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
from orchestrator.judge.rung4 import Rung4Judge, Rung4Result, combine_vector
from orchestrator.judge.rungs import evaluate_rollout
from orchestrator.judge.seam import assemble_file_metrics
from orchestrator.judge.thresholds import (
    CONTACT_DURATION_S,
    CONTACT_FORCE_CV_MAX,
    GOAL_WINDOW_S,
    MAX_PENETRATION_M,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
TASK = 7

# Gate1 批准的 sim_engine slug（能力矩阵 slug）→ Track1 场景模板 + 额外环境。
# 只接 Track1 两引擎；genesis/physx_isaac/newton/bullet 在 Task7 是协议外选择
# （Track2/后续任务），命中即 GraphStateError，不静默跑错脚本。
ENGINE_SCENARIOS: dict[str, dict] = {
    "mujoco": {
        "template": REPO_ROOT / "sandbox" / "track1" / "mujoco_contact_push.py",
        "script": "mujoco_contact_push.py",
        "env": {"MUJOCO_GL": "egl", "PYOPENGL_PLATFORM": "egl"},
    },
    "sapien": {
        "template": REPO_ROOT / "sandbox" / "track1" / "maniskill_pushcube.py",
        "script": "maniskill_pushcube.py",
        "env": {"PYOPENGL_PLATFORM": "egl"},
    },
}

TELEMETRY_NAME = "telemetry.json"
VIDEO_NAME = "video.mp4"
COVER_NAME = "cover.png"
MANIFEST_NAME = "manifest.json"
VENV_PY = "/mnt/venvs/simpraxis/bin/python"


def runs_root() -> Path:
    return Path(os.environ.get("SIMPRAXIS_RUN_DIR",
                               "/mnt/artifacts/simpraxis/runs"))


def task7_run_dir(thread_id: str, run_idx: int) -> Path:
    """真实 rollout 工作目录：$SIMPRAXIS_RUN_DIR/phase1/task7_track1/run_<id>。"""
    d = runs_root() / "phase1" / "task7_track1" \
        / f"run_{thread_id}_{run_idx}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _last_event(state: dict, kind: str) -> "dict | None":
    for ev in reversed(state.get("events", [])):
        if ev.get("type") == kind:
            return ev
    return None


def _installed_versions() -> str:
    """本解释器实际可导入分发清单（= run 真实环境；不另起 pip 子进程）。"""
    pkgs = sorted((f"{d.metadata['Name']}=={d.version}"
                   for d in importlib_metadata.distributions()
                   if d.metadata and d.metadata.get("Name")),
                  key=str.lower)
    return "\n".join(pkgs) + "\n"


def make_spec_factory(thread_id: str, coding_batch: str,
                      task_title: str = "") -> Callable[[dict], RunSpec]:
    """agent_run 接缝的 spec 工厂：按 Gate1 批准引擎选场景，stage 模板后执行。

    Gate2 打回后的再跑（route_after_gate2→agent_run）使用全新 run 目录与新
    run_idx：人工意见经 gate2_notes 注入 prompt，脚本是干净的新副本，前一
    run 的 coding 补丁不继承（每次 run 独立 ≤5 轮，计划 L502/D10）。
    """
    def factory(state: dict) -> RunSpec:
        chosen = state.get("chosen") or {}
        engine = chosen.get("sim_engine")
        scenario = ENGINE_SCENARIOS.get(engine)
        if scenario is None:
            raise GraphStateError(
                f"Task7 Track1 场景映射缺 sim_engine={engine!r}；"
                f"支持 {sorted(ENGINE_SCENARIOS)}，其余引擎属 Track2/后续任务")
        template: Path = scenario["template"]
        if not template.is_file():
            raise GraphStateError(
                f"Task7 场景模板不存在：{template}（引擎 {engine} 探针/脚本未就位）")

        # L-12 编号钉死（两阶段索引关系，禁止改号）：工厂在 agent_runs 追加
        # 之前预占序号 len(agent_runs)+1；evaluate 接缝在追加之后用
        # len(agent_runs) 复核（见 make_evaluate_seam 注释）——同一 run 两处
        # 同号（首个 run 都是 r1）。run_id/证据文件名/callcache key 以此为准。
        run_idx = len(state.get("agent_runs", [])) + 1
        workdir = task7_run_dir(thread_id, run_idx)

        # 仅在新目录首见时 stage：coding 补丁只改 workdir 内文件，重跑保留。
        target = workdir / scenario["script"]
        if not target.exists():
            shutil.copyfile(template, target)
            os.chmod(target, 0o700)
        versions = workdir / "versions.txt"
        if not versions.exists():
            versions.write_text(_installed_versions(), encoding="utf-8")
            os.chmod(versions, 0o600)

        ev = _last_event(state, "gate2_rejected")
        notes = ev["human_notes"] if ev else None
        # 与 --run-id、证据 stem、callcache key task7_rung4_{run_id} 同一字符串。
        run_id = f"{thread_id}-r{run_idx}"
        env_extra = {
            # SAPIEN PhysX GPU 库按 ~/.sapien/physx 下载（无 env 覆盖，只认 HOME）：
            # 指向持久缓存，避免随容器销毁每次重下（大文件只落 /mnt）。
            "HOME": "/mnt/caches/home",
            "HF_HOME": "/mnt/caches/hf",
            "HF_ENDPOINT": "https://hf-mirror.com",
            "SIMPRAXIS_USE_PROXY": "0",
        }
        env_extra.update(scenario["env"])

        return RunSpec(
            thread_id=thread_id,
            workdir=workdir,
            cmd=[VENV_PY, str(target), "--out-dir", str(workdir),
                 "--run-id", run_id],
            telemetry_relpath=TELEMETRY_NAME,
            run_idx=run_idx,             # L-1：coding 重放 key 稳定序号
            batch_id=coding_batch,
            billing_task=TASK,
            gate2_notes=notes,
            env_extra=env_extra,
            task_title=task_title or f"Task7 Track1 {engine} 真实运行",
            video_relpath=VIDEO_NAME,
            cover_relpath=COVER_NAME,
            manifest_relpath=MANIFEST_NAME,
        )

    return factory


# ---------------------------------------------------------------------------
# evaluate 接缝：Rung1–3 文件判定 + Rung4 真实视频视觉判定
# ---------------------------------------------------------------------------

def _evidence_dir() -> Path:
    d = phase1_root() / "task7_track1" / "evidence"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_evidence(stem: str, kind: str, payload: dict) -> str:
    p = _evidence_dir() / f"{stem}.{kind}.json"
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                 encoding="utf-8")
    os.chmod(p, 0o600)
    return f"phase1/task7_track1/evidence/{p.name}"


class _VisualEvidenceGateway:
    """包一层 Rung4Judge.gateway：call_visual 前后落脱敏证据并在 billing_context
    内挂接 evidence（同 Task4/5 时序：新 BillingContext 会重置 evidence）。

    对外只暴露 call_visual/model_id（judge 仅需这两个）。req 证据在实呼前
    落盘并记到 self.req_ref，供接缝把 raw_text_ref 补戳进 Rung4Result。

    L-1 调用级幂等：实呼前按稳定 key task7_rung4_{run_id} + 请求指纹查
    CallReplay。命中直接返回缓存 raw/usage——不发网、不进 billing_context、
    不写 ledger、不重写 req/resp 证据（首调 req 仍在盘上，req_ref 用同一
    确定性证据引用补戳）；未命中才实呼，成功后才 put。L-7：实呼异常在
    except 内补脱敏失败 resp（先例 gait_calibrate.py），随后原样抛出，
    异常路径绝不写缓存。
    """

    KIND = "task7_real_rollout_rung4"
    REF_PREFIX = "phase1/task7_track1/evidence"

    def __init__(self, gateway, *, thread: str, run_id: str, batch_id: str,
                 collage_path: str, cache: "CallReplay | None" = None,
                 cache_key: "str | None" = None):
        self._g = gateway
        self._thread = thread
        self._run_id = run_id
        self._batch = batch_id
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
        stem = f"rung4_{self._run_id}"
        fp = request_fingerprint(prompt, image_b64, dict(kwargs))
        if self._cache is not None and self._cache_key is not None:
            hit = self._cache.get(self._cache_key, fp)
        else:
            hit = None
        if hit is not None:
            # 重放命中：零付费、零证据写；req_ref 与首调确定性同名，保证
            # raw_text_ref 补戳结果与首调逐字一致。
            self.req_ref = f"{self.REF_PREFIX}/{stem}.req.json"
            return hit["raw"], tuple(hit["usage"])

        model = self._g.model_id("visual_rollout")
        req_ref = _write_evidence(stem, "req", {
            "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "stage": "visual_rollout",
            "model": model,
            "thread_id": self._thread, "batch_id": self._batch,
            "run_id": self._run_id, "kind": self.KIND,
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
        # 成功（含证据落盘）后才入缓存；fake/无计量 usage 不入。
        if self._cache is not None and self._cache_key is not None:
            usage_ints = self._usage_ints(usage)
            if usage_ints is not None:
                self._cache.put(self._cache_key, kind=self.KIND, model=model,
                                raw=raw, usage=usage_ints, fingerprint=fp)
        return raw, usage


def make_evaluate_seam(gateway, visual_batch: str, thread_id: str):
    """evaluate 节点接缝：只读真实 telemetry 与真实 mp4，不出任何指标字面量。"""
    def seam(state: dict) -> dict:
        uri = state.get("telemetry_uri")
        if not uri:
            raise GraphStateError("task7 evaluate：state 无 telemetry_uri")
        tele_path = Path(uri)
        if not tele_path.is_file():
            raise GraphStateError(f"task7 evaluate：telemetry 不存在：{uri}")
        data = json.loads(tele_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise GraphStateError("task7 evaluate：telemetry 顶层必须是对象")

        # Task5 标准装配（含 schema 失败以 rules_pass=False 进 Gate2 的语义），
        # 再用真实视频的 Rung4 结果覆盖其恒 None 占位。
        metrics = assemble_file_metrics(uri, data)
        result = evaluate_rollout(data)

        runs = state.get("agent_runs", [])
        rec = runs[-1] if runs else {}
        # L-12：evaluate 节点在 agent_run 追加之后运行，故这里 len(runs) 与
        # 工厂预占的 len(agent_runs)+1 同号（首个 run 即 r1）；两阶段索引关系
        # 钉死，禁止改号（run_id 同时进 --run-id/证据名/callcache key）。
        run_idx = len(runs)
        run_id = f"{thread_id}-r{run_idx}"
        video = rec.get("video_path")
        if not video or not Path(video).is_file():
            # Task7 真实 run 无视频即协议违规：不得静默以 Rung4=None 放行
            raise GraphStateError(
                f"task7 evaluate：真实视频缺失（video_path={video!r}）")

        frames = extract_frames_mp4(video)
        contact_active = data.get("contact_active") or []
        contact_idx = next((i for i, v in enumerate(contact_active) if v), None)
        idx = select_frame_indices(len(frames), contact_idx)
        labels = [f"frame {i:03d}" for i in idx]
        chosen = [frames[i] for i in idx]

        collage_png = Path(video).parent / "rung4_collage.png"
        collage_png.write_bytes(build_collage_png(chosen, labels))
        os.chmod(collage_png, 0o600)
        image_b64 = collage_b64(chosen, labels)

        # L-1：Rung4 实呼级重放缓存（0700 点目录，证据包不收录），key 按
        # task7_rung4_{run_id}，kill-9 后整节点重放零二次付费。
        call_cache = CallReplay(phase1_root() / ".call_cache")
        ev_gateway = _VisualEvidenceGateway(
            gateway, thread=thread_id, run_id=run_id, batch_id=visual_batch,
            collage_path=str(collage_png), cache=call_cache,
            cache_key=f"task7_rung4_{run_id}")
        judge = Rung4Judge(ev_gateway)
        # uncalibrated 时 judge 内部不发网（Task5 闸门语义）；record_only 照实呼、
        # 卡片照显 items 但分歧标志强制清空。
        r4 = judge.judge(image_b64, bool(result.get("rules_pass")))
        if r4.raw_text_ref is None and ev_gateway.req_ref is not None:
            r4 = dataclasses.replace(r4, raw_text_ref=ev_gateway.req_ref)

        combined = combine_vector(result, r4)
        r4_dict = r4.as_dict()
        metrics["provenance"] = "file:telemetry_uri+video:rung4_collage"
        metrics["rungs"]["rung4"] = r4_dict
        metrics["rung4"] = r4_dict
        metrics["vector"] = combined["vector"]
        metrics["rules_pass"] = combined["rules_pass"]
        metrics["auto_release"] = combined["auto_release"]
        metrics["escalate_human"] = combined["escalate_human"]
        if "banner" in combined:
            metrics["banner"] = combined["banner"]
        return metrics

    return seam


# ---------------------------------------------------------------------------
# Gate2 卡片自解释说明（真人 Gate2 2026-10-15 打回逐字要求）：
# 「说明这个仿真任务是做什么的，解决什么问题，发来的视频是什么意思」
# 文案只描述场景脚本 docstring 的物理事实与 judge 的判定口径，不读结果倒推；
# 阈值数字一律取自 thresholds 常量，避免卡片与判定器口径漂移。
# ---------------------------------------------------------------------------

_ENGINE_EXPLANATION: dict[str, dict[str, str]] = {
    "mujoco": {
        "task": "MuJoCo 原生物理（mj_step 3.15.0）+ EGL 无头渲染：一个平面推板沿 "
                "min-jerk 平滑轨迹，把桌面上 5 cm、0.1 kg 的立方块推入目标公差盒、"
                "压向末端固定挡块，并保持约 0.8 s。每帧记录推板与方块的真实位姿，"
                "每个物理子步记录全部接触对的最大穿模深度，以及推板–方块接触力的"
                "合力模（等价腕部力/扭矩读数）。",
        "video": "画面是这次真实 rollout 的 EGL 离屏渲染：推板从方块一侧平滑靠近、"
                 "接触并把它沿平面推到目标位置（是否进目标盒按坐标与公差盒判定，"
                 "画面中不一定画出盒线），最后顶住挡块保持。",
    },
    "sapien": {
        "task": "ManiSkill3（SAPIEN；PhysX CPU 物理 + Vulkan GPU 离屏渲染）跑官方 "
                "Panda PushCube-v1：Panda 机械臂用官方运动规划求解器，把桌面上 "
                "4 cm 方块推入前方圆形目标，到位后冻结末动作保持接触约 1.3 s。按控制"
                "帧记录机械臂 tcp 与方块位姿、手爪对方块接触力的三维合力模（与 "
                "MuJoCo 场景同口径），以及每物理步的最大穿模深度。",
        "video": "画面是这次真实 rollout 的 Vulkan 离屏渲染：Panda 机械臂从起始位姿"
                 "靠近、用手指接触方块，沿桌面把它推到前方圆形目标区，到位后冻结"
                 "末动作保持。",
    },
}

_VIDEO_WATCH = (
    "请重点看：接触是否真实发生、方块是否平滑到位并停住，有没有穿透、瞬移、"
    "悬空或可疑抖动。下方 Rung1–4 是对同一条过程的量化判定。"
)


def build_gate2_explanation(state: dict, rec: dict) -> "dict[str, str] | None":
    """组装 Gate2 卡片顶部三问说明；协议外引擎返回 None（卡片退回纯指标，不编故事）。"""
    chosen = state.get("chosen") or {}
    engine = chosen.get("sim_engine")
    narrative = _ENGINE_EXPLANATION.get(engine) if isinstance(engine, str) else None
    if narrative is None:
        return None

    scheme = (chosen.get("name") or "（未命名方案）").strip()
    task = (f"Gate1 批准方案：{scheme}\n"
            f"本次是该方案在 Track1 的最小物理实证——{narrative['task']}")

    problem = (
        "它要回答的不是「脚本有没有跑通」，而是「这次仿真在物理上是否可信、"
        "能否作为后续 RSI / 训练的证据」，分四级：\n"
        f"· Rung1：全部数值有限（无 NaN/Inf）、逐物理子步穿模 ≤ "
        f"{MAX_PENETRATION_M * 1000:g} mm、关节力矩不越限；\n"
        f"· Rung2：只按物体位姿时序重算是否真进目标区（末 {GOAL_WINDOW_S:g} s "
        "全程在盒内），且最长连续接触 ≥ "
        f"{CONTACT_DURATION_S:g} s、窗口内接触力波动 CV ≤ {CONTACT_FORCE_CV_MAX:g}；\n"
        "· Rung3：对末端轨迹做三阶差分查 jerk 尖峰（阈值随帧 dt 锚定），拦瞬移/"
        "可疑抖动；\n"
        "· Rung4：视觉模型对视频 2×3 抽帧独立挑物理缺陷。规则与视觉不一致时标"
        "「分歧」，以 Rung1–3 规则为准并升级到你裁决；Rung4 永不单独放行。"
    )

    length = ""
    manifest: dict[str, Any] = {}
    tele = rec.get("telemetry_uri") or state.get("telemetry_uri")
    if tele:
        mp = Path(tele).parent / MANIFEST_NAME
        if mp.is_file():
            try:
                loaded = json.loads(mp.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    manifest = loaded
            except (ValueError, OSError):
                manifest = {}
    n_frames = manifest.get("n_frames")
    dur_ms = rec.get("duration_ms")
    parts: list[str] = []
    if isinstance(n_frames, int) and n_frames > 0:
        parts.append(f"{n_frames} 帧")
    if isinstance(dur_ms, (int, float)) and dur_ms > 0:
        parts.append(f"{dur_ms / 1000:.1f} s")
    length = ("本段约 " + "、".join(parts)) if parts else ""
    video = narrative["video"] + (f"（{length}）。" if length else "。") + _VIDEO_WATCH

    return {"task": task, "problem": problem, "video": video}
