"""RSI-R0 可信服务的宿主侧调用面（薄适配，不重实现物理）。

职责边界（计划 §4.4）：

* 用**固定 digest 镜像**起一次性容器跑 :mod:`orchestrator.rsi_r0.service`；
* 只经 stdin 传**有界数值白名单**请求，绝不把候选代码/路径/命令塞进容器；
* 解析容器回来的单行 ``RESULT_JSON``，核对 ``steps_run`` 与积分步数，落证据
  （请求/响应/事件哈希）；
* GPU 车道：渲染必须来自 NVIDIA GL，llvmpipe/OSMesa 软件渲染**判负**——
  2026-11-08 实测无 ``--gpus`` 时 llvmpipe 能出图（pixel_sum≈87381 vs GPU
  87429），"能渲染" 不构成 GPU 证据。

任何宿主侧回退（在自己进程里 import mujoco 跑参考）都被 :func:`assert_no_host_fallback`
拒绝：host 成功不得冒充容器成功（AC12）。
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Mapping

from orchestrator.rsi_r0.contracts import sha256_of


#: 容器内执行的唯一服务脚本（随仓库走，只读挂载进容器）。
SERVICE_SCRIPT = Path(__file__).with_name("service.py")

#: AC15 策略：渲染证据必须来自真 GPU（厂商串含 NVIDIA）。
REQUIRED_GL_VENDOR = "NVIDIA"

#: 单次可信调用的硬墙钟（计划 §6.2 reference_run_s=180，留 20s 容器启动余量）。
DEFAULT_TIMEOUT_S = 180


class TrustedServiceError(RuntimeError):
    """可信服务不可用或返回不自洽（阻断试次，绝不降级到宿主）。"""


class HostFallbackRejected(RuntimeError):
    """尝试用宿主进程代替固定镜像容器（AC12 明令禁止）。"""


def assert_no_host_fallback(where: str) -> None:
    raise HostFallbackRejected(
        f"{where}：禁止在宿主进程里跑参考/场景（host 成功不得冒充容器成功，AC12）")


def _docker_argv(image_digest: str, *, gpus: bool, extra: list[str] | None = None) -> list[str]:
    argv = [
        "docker", "run", "--rm", "-i",
        "--network=none",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--pids-limit=256",
        "--memory=8g",
        "--cpus=4",
        "--read-only",
        "--tmpfs=/tmp:rw,size=512m",
        "--user=1000:1000",
        "-v", f"{SERVICE_SCRIPT}:/svc/service.py:ro",
    ]
    if gpus:
        argv.append("--gpus=all")
    if extra:
        argv.extend(extra)
    argv.extend([image_digest, "python3", "/svc/service.py"])
    return argv


def _parse_result(stdout: str) -> dict[str, Any]:
    for line in stdout.splitlines():
        if line.startswith("RESULT_JSON "):
            return json.loads(line[len("RESULT_JSON "):])
    raise TrustedServiceError(f"容器未返回 RESULT_JSON（stdout 前 300 字：{stdout[:300]!r}）")


def call_service(request: Mapping[str, Any], *, image_digest: str, gpus: bool = False,
                 timeout_s: int = DEFAULT_TIMEOUT_S) -> dict[str, Any]:
    """起一次性容器执行一次可信调用；返回容器回传的 dict（含事件哈希）。"""
    if shutil.which("docker") is None:
        raise TrustedServiceError("docker 不可用：可信服务必须跑在固定镜像容器里")
    argv = _docker_argv(image_digest, gpus=gpus)
    payload = json.dumps(request, ensure_ascii=False)
    try:
        proc = subprocess.run(argv, input=payload, capture_output=True, text=True,
                              timeout=timeout_s)
    except subprocess.TimeoutExpired as exc:
        raise TrustedServiceError(f"可信服务超时 {timeout_s}s（run 作废，不重试）") from exc
    result = _parse_result(proc.stdout)
    if not result.get("ok"):
        raise TrustedServiceError(
            f"可信服务拒绝：{result.get('error')}（rc={proc.returncode}）")
    if proc.returncode != 0:
        raise TrustedServiceError(f"可信服务 rc={proc.returncode} 但回报 ok=true，自相矛盾")
    return result


def render_fingerprint(image_digest: str, scene_xml: str) -> dict[str, Any]:
    """GPU/EGL 真验（AC15）：渲染 + GL 厂商策略。软件渲染一律判负。"""
    result = call_service({"schema": "rsi_r0.service.v1", "kind": "render_probe",
                           "scene_xml": scene_xml, "steps": 1},
                          image_digest=image_digest, gpus=True)
    vendor = str(result.get("gl_vendor", ""))
    renderer = str(result.get("gl_renderer", ""))
    verdict = {
        "gl_vendor": vendor,
        "gl_renderer": renderer,
        "pixel_sum": result.get("pixel_sum"),
        "pixel_std": result.get("pixel_std"),
        "render_ok": bool(result.get("render_ok")),
        "hardware": REQUIRED_GL_VENDOR in vendor,
    }
    verdict["accepted"] = bool(verdict["render_ok"] and verdict["hardware"])
    if not verdict["accepted"]:
        verdict["reason"] = (
            "render_failed" if not verdict["render_ok"]
            else f"software_renderer_rejected:{vendor or 'unknown'}")
    verdict["mujoco_version"] = result.get("mujoco_version")
    return verdict


def container_argv_template(image_digest: str, *, gpus: bool = False) -> list[str]:
    """证据包里复现一次调用所需的 argv（不含请求体）。"""
    return _docker_argv(image_digest, gpus=gpus)


# ---------------------------------------------------------------------------
# 候选面闸口：scene_xml **永不**由候选提供
# ---------------------------------------------------------------------------

#: 候选可提交的数值键白名单（唯一真值）。场景、故障与宿主注入字段都不在此列——
#: 候选若提交 ``scene_xml`` / ``force_scale`` / ``fault`` 等一律 fail-closed。
CANDIDATE_PARAM_BOUNDS: Mapping[str, Mapping[str, tuple[float, float]]] = {
    "push_control": {
        "target_x": (-50.0, 50.0), "target_y": (-50.0, 50.0),
        "kp": (0.0, 5000.0), "kd": (0.0, 500.0), "max_force": (0.0, 500.0),
        "steps": (1.0, 1_000_000.0),
    },
    "free_run": {
        "initial_angle": (-3.2, 3.2),
        "damping": (0.0, 5.0),          # 只对 R 族开放；E/D 的 builder 会拒收
        "steps": (1.0, 1_000_000.0),
    },
}

#: 宿主独占字段：只由维护侧写入请求，候选提交即判非法（防止候选自造故障）。
HOST_ONLY_FIELDS = frozenset({"force_scale", "scene_xml", "fault"})


class CandidateRequestError(ValueError):
    """候选提交了未授权字段或越界数值（fail-closed，不夹取）。"""


class ExperimentSession:
    """一个实例的可信实验会话：候选只能提交有界数值，场景由宿主侧构造。

    信任链：候选 → **本对象**（白名单校验 + 场景构造）→ :func:`call_service`
    → 固定镜像容器里的 :mod:`orchestrator.rsi_r0.service`。候选请求体里不可能
    出现 ``scene_xml``，因此"改场景/读真值/注入故障"在协议层就不可达（计划 §4.4）。
    """

    def __init__(self, *, kind: str, image_digest: str, scene_builder,
                 max_steps: int, host_fields: Mapping[str, float] | None = None,
                 gpus: bool = False, timeout_s: int = DEFAULT_TIMEOUT_S):
        if kind not in CANDIDATE_PARAM_BOUNDS:
            raise CandidateRequestError(f"未知实验类型：{kind!r}")
        self.kind = kind
        self.image_digest = image_digest
        self._build_scene = scene_builder
        self.max_steps = int(max_steps)
        self._host_fields = dict(host_fields or {})
        bad = HOST_ONLY_FIELDS & set(self._host_fields) - {"force_scale"}
        if bad:
            raise CandidateRequestError(f"宿主字段只能注入 force_scale，收到 {sorted(bad)}")
        self.gpus = gpus
        self.timeout_s = timeout_s
        self.calls: list[dict[str, Any]] = []

    # -- 校验 ---------------------------------------------------------------
    def validate(self, params: Mapping[str, Any]) -> dict[str, float]:
        bounds = CANDIDATE_PARAM_BOUNDS[self.kind]
        illegal = set(params) - set(bounds)
        if illegal:
            raise CandidateRequestError(
                f"未授权字段：{sorted(illegal)}（本实例可选 {sorted(bounds)}）")
        out: dict[str, float] = {}
        for key, value in params.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise CandidateRequestError(f"{key} 必须是数值，收到 {type(value).__name__}")
            lo, hi = bounds[key]
            if key == "steps":
                hi = min(hi, float(self.max_steps))
            if not (lo <= float(value) <= hi):
                raise CandidateRequestError(f"{key}={value} 越界 [{lo}, {hi}]")
            out[key] = float(value)
        if "steps" not in out:
            out["steps"] = float(self.max_steps)
        if out["steps"] != int(out["steps"]):
            raise CandidateRequestError("steps 必须是整数")
        out["steps"] = int(out["steps"])
        return out

    # -- 执行 ---------------------------------------------------------------
    def run(self, params: Mapping[str, Any]) -> dict[str, Any]:
        clean = self.validate(params)
        scene_xml = self._build_scene(clean)
        if not isinstance(scene_xml, str) or not scene_xml.strip():
            raise TrustedServiceError("场景构造器未返回 XML（宿主侧 bug，不是候选的错）")
        request = {"schema": "rsi_r0.service.v1", "kind": self.kind,
                   "scene_xml": scene_xml, **clean, **self._host_fields}
        result = call_service(request, image_digest=self.image_digest,
                              gpus=self.gpus, timeout_s=self.timeout_s)
        if int(result.get("steps_run", -1)) != int(clean["steps"]):
            raise TrustedServiceError(
                f"steps_run={result.get('steps_run')} 与请求 {clean['steps']} 不符")
        self.calls.append({"params": clean, "run_id": result.get("run_id"),
                           "events_sha256": result.get("events_sha256"),
                           # 场景身份必须随事件一起留档：评测器要用它证明"跑的是本题的场景"
                           "scene_sha256": sha256_of(scene_xml),
                           "steps_run": int(result.get("steps_run", -1))})
        return result
