"""RSI-R0.1 完整逐步观测：可信容器调用、宿主重算、身份绑定与只读副本。

计划 §4.2 的落地：

* 观测件是**不可变规范化 JSON**（``t``/``theta``/``omega`` 三条等长数组），
  2001 行、dt=0.002 s、末点恰 4 s；
* 容器自报的 ``observation_sha256`` 只作交叉核对，**宿主必须重算并以宿主值
  为准**——容器自报不构成证据；
* manifest 带 trial/run/实例/请求参数/**实际应用参数**/scene hash/行数/时间范围；
* 宿主可信原件单独保存，候选只拿该 trial 的只读副本；
* 摘要可降采样但必须标 ``preview_only`` 且含首末点，计算唯一依据是完整文件。

本模块**不**在宿主 import mujoco：物理只在固定镜像容器里发生（AC16/AC19 的
「host 成功不得冒充容器成功」）。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from orchestrator.rsi_r01 import schemas as S

SERVICE_SCRIPT = Path(__file__).with_name("service.py")

#: 与 R0 一致的固定镜像单一真值（按 digest，不按 tag）。
IMAGE_REPO = "sim-praxis/sandbox"
IMAGE_DIGEST = "sha256:50b6e10df854db40ea41e33f3ed9731a9d5d35b2d9d2ccf9b9f178d8565e4bfe"
IMAGE_REF = f"{IMAGE_REPO}@{IMAGE_DIGEST}"

#: 单次实验的硬墙钟（计划 §6.3：每 trial ≤45 min，单次留足容器启动余量）。
DEFAULT_TIMEOUT_S = 180


class ObservationError(RuntimeError):
    """仪器侧失败（不是候选的错 ⇒ 该 trial INVALID / INVALID_INSTRUMENT）。"""

    code = "OBSERVATION_ERROR"


class ObservationIntegrityError(ObservationError):
    """观测件与 manifest 不自洽（篡改/串用/缺末点）——判 INVALID。"""

    def __init__(self, code: str, msg: str):
        super().__init__(f"{code}: {msg}")
        self.code = code
        self.detail = msg


class TrustedServiceUnavailable(ObservationError):
    code = "TRUSTED_SERVICE_UNAVAILABLE"


@dataclass(frozen=True)
class ExperimentRequest:
    """一次可信实验请求（宿主构造 scene_xml，候选只见数值参数）。

    ``host_injected`` 是**宿主私有注入项**（目前只有 D 族 ``force_scale``）：
    它由宿主按私有故障表写进请求，候选的提交体里不存在这个键。
    """

    trial_id: str
    instance_id: str
    kind: str
    scene_xml: str
    params: Mapping[str, float]
    steps: int
    host_injected: Mapping[str, float] = field(default_factory=dict)

    def service_payload(self) -> dict[str, Any]:
        payload = {"schema": S.SCHEMA_SERVICE_REQUEST, "kind": self.kind,
                   "trial_id": self.trial_id, "instance_id": self.instance_id,
                   "scene_xml": self.scene_xml, "steps": int(self.steps)}
        for key, value in self.params.items():
            payload[str(key)] = float(value)
        for key, value in self.host_injected.items():
            payload[str(key)] = float(value)
        return payload

    @classmethod
    def pendulum(cls, *, trial_id: str, instance_id: str, scene_xml: str,
                 damping: float, initial_angle: float,
                 timestep: float = S.DT_S, steps: int = S.N_STEPS
                 ) -> "ExperimentRequest":
        return cls(trial_id=trial_id, instance_id=instance_id,
                   kind=S.OBS_KIND_PENDULUM, scene_xml=scene_xml,
                   params={"damping": float(damping),
                           "initial_angle": float(initial_angle),
                           "timestep": float(timestep)},
                   steps=int(steps))

    @classmethod
    def push(cls, *, trial_id: str, instance_id: str, scene_xml: str,
             kp: float, kd: float, max_force: float, target: tuple[float, float],
             timestep: float, steps: int,
             force_scale: float | None = None) -> "ExperimentRequest":
        injected = {} if force_scale is None else {"force_scale": float(force_scale)}
        return cls(trial_id=trial_id, instance_id=instance_id,
                   kind=S.OBS_KIND_PUSH, scene_xml=scene_xml,
                   params={"kp": float(kp), "kd": float(kd),
                           "max_force": float(max_force),
                           "target_x": float(target[0]),
                           "target_y": float(target[1]),
                           "timestep": float(timestep)},
                   steps=int(steps), host_injected=injected)


def container_argv(*, host_out_dir: Path, gpus: bool = False) -> list[str]:
    """实验容器 argv：非 root、无网络、只读根、cap-drop ALL、pids/内存/CPU 硬界。"""
    argv = [
        "docker", "run", "--rm", "-i",
        "--network=none",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--pids-limit=256",
        "--memory=4g",
        "--cpus=2",
        "--read-only",
        "--tmpfs=/tmp:rw,size=256m",
        "--user=1000:1000",
        "-v", f"{SERVICE_SCRIPT}:/svc/service.py:ro",
        "-v", f"{host_out_dir}:/out:rw",
    ]
    if gpus:
        argv.append("--gpus=all")
    argv.extend([IMAGE_REF, "python3", "/svc/service.py"])
    return argv


def _parse_result(stdout: str) -> dict[str, Any]:
    for line in stdout.splitlines():
        if line.startswith("RESULT_JSON "):
            return json.loads(line[len("RESULT_JSON "):])
    raise TrustedServiceUnavailable(f"容器未返回 RESULT_JSON（stdout 前 300 字：{stdout[:300]!r}）")


def _run_container(payload: Mapping[str, Any], *, out_dir: Path, gpus: bool,
                   timeout_s: int) -> dict[str, Any]:
    if shutil.which("docker") is None:
        raise TrustedServiceUnavailable("docker 不可用：可信实验必须跑在固定镜像容器里")
    argv = container_argv(host_out_dir=out_dir, gpus=gpus)
    try:
        proc = subprocess.run(argv, input=json.dumps(payload, ensure_ascii=False),
                              capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired as exc:
        raise TrustedServiceUnavailable(f"可信实验超时 {timeout_s}s（run 作废，不重试）") from exc
    result = _parse_result(proc.stdout)
    if not result.get("ok"):
        raise TrustedServiceUnavailable(f"可信实验失败：{result.get('error')}（rc={proc.returncode}）")
    if proc.returncode != 0:
        raise TrustedServiceUnavailable(f"容器 rc={proc.returncode} 却回报 ok=true")
    return result


# ---------------------------------------------------------------------------
# manifest 与完整性
# ---------------------------------------------------------------------------


def build_manifest(*, trial_id: str, instance_id: str, run_id: str,
                   scene_sha256: str, observation_sha256: str, rows: int,
                   t_start: float, t_end: float, params: Mapping[str, Any],
                   applied: Mapping[str, Any], bytes_: int,
                   kind: str = S.OBS_KIND_PENDULUM,
                   image_digest: str = IMAGE_DIGEST) -> dict[str, Any]:
    units = ({"t": "s", "theta": "rad", "omega": "rad/s"}
             if kind == S.OBS_KIND_PENDULUM
             else {"t": "s", "x": "m", "y": "m", "vx": "m/s", "vy": "m/s"})
    dt = params.get("timestep") if isinstance(params, Mapping) else None
    return {
        "schema": S.SCHEMA_OBSERVATION_MANIFEST,
        "kind": kind,
        "trial_id": trial_id,
        "instance_id": instance_id,
        "run_id": run_id,
        "scene_sha256": scene_sha256,
        "observation_sha256": observation_sha256,
        "rows": int(rows),
        "t_start": float(t_start),
        "t_end": float(t_end),
        "dt_s": float(dt) if isinstance(dt, (int, float)) else S.DT_S,
        "params": dict(params),
        "applied": dict(applied),
        "bytes": int(bytes_),
        "image_digest": image_digest,
        "units": units,
    }


def _sha256_file(path: Path) -> str:
    h = __import__("hashlib").sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_observation_file(path: Path, manifest: Mapping[str, Any], *,
                            expected_trial: str | None = None,
                            expected_instance: str | None = None,
                            expected_run: str | None = None) -> dict[str, Any]:
    """完整性与身份绑定：hash/行数/时间范围/schema/跨 trial 串用都拒。

    返回观测文档；任何不符抛 :class:`ObservationIntegrityError`（带稳定 code）。
    """
    p = Path(path)
    if not p.is_file():
        raise ObservationIntegrityError("OBSERVATION_MISSING", f"{p} 不存在")
    size = p.stat().st_size
    if size > S.MAX_OBSERVATION_BYTES:
        raise ObservationIntegrityError(
            "OBSERVATION_TOO_LARGE", f"{size} B > {S.MAX_OBSERVATION_BYTES} B")
    actual = _sha256_file(p)
    if actual != manifest.get("observation_sha256"):
        raise ObservationIntegrityError(
            "HASH_MISMATCH", f"文件 {actual} ≠ manifest {manifest.get('observation_sha256')}")
    problems = S.check_manifest_doc(dict(manifest))
    if problems:
        raise ObservationIntegrityError("MANIFEST_INVALID", str(problems[:4]))
    if expected_trial is not None and manifest.get("trial_id") != expected_trial:
        raise ObservationIntegrityError(
            "TRIAL_MISMATCH", f"{manifest.get('trial_id')} ≠ {expected_trial}")
    if expected_instance is not None and manifest.get("instance_id") != expected_instance:
        raise ObservationIntegrityError(
            "INSTANCE_MISMATCH", f"{manifest.get('instance_id')} ≠ {expected_instance}")
    if expected_run is not None and manifest.get("run_id") != expected_run:
        raise ObservationIntegrityError(
            "RUN_MISMATCH", f"{manifest.get('run_id')} ≠ {expected_run}")
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ObservationIntegrityError("NOT_JSON", str(exc)) from exc
    kind = manifest.get("kind") or S.OBS_KIND_PENDULUM
    problems = S.check_observation_doc(doc, kind=kind)
    if problems:
        raise ObservationIntegrityError("OBSERVATION_INVALID", str(problems[:4]))
    if len(doc["t"]) != int(manifest["rows"]):
        raise ObservationIntegrityError(
            "ROW_MISMATCH", f"文件 {len(doc['t'])} 行 ≠ manifest {manifest['rows']}")
    if abs(float(doc["t"][0]) - float(manifest["t_start"])) > 1e-12 or \
            abs(float(doc["t"][-1]) - float(manifest["t_end"])) > 1e-9:
        raise ObservationIntegrityError(
            "TIME_RANGE_MISMATCH",
            f"[{doc['t'][0]}, {doc['t'][-1]}] ≠ [{manifest['t_start']}, {manifest['t_end']}]")
    # 文档自带的身份字段也必须与 manifest 一致（防同文件换壳串用）
    for key in ("trial_id", "run_id", "instance_id"):
        if doc.get(key) != manifest.get(key):
            raise ObservationIntegrityError(
                f"DOC_{key.upper()}_MISMATCH", f"{doc.get(key)!r} ≠ {manifest.get(key)!r}")
    if isinstance(doc.get("kind"), str) and doc["kind"] != kind:
        raise ObservationIntegrityError(
            "DOC_KIND_MISMATCH", f"{doc['kind']!r} ≠ manifest {kind!r}")
    return doc


def preview(doc: Mapping[str, Any], *, max_bytes: int = S.MAX_PREVIEW_BYTES) -> dict[str, Any]:
    """工具回包摘要：含首末点、标 preview_only；超 4 KiB 则减少中段采样。"""
    kind = str(doc.get("kind") or S.OBS_KIND_PENDULUM)
    names = S.OBS_SERIES.get(kind, S.OBS_SERIES[S.OBS_KIND_PENDULUM])
    t = doc["t"]
    n = len(t)

    def _point(i: int) -> dict[str, float]:
        point = {"t": t[i]}
        for name in names:
            point[name] = doc[name][i]
        return point

    out = {"preview_only": True, "kind": kind, "rows": n,
           "first": _point(0), "last": _point(n - 1),
           "note": "摘要仅供预览；计算唯一依据是完整观测文件"}
    step = max(1, n // 8)
    while True:
        sample = [_point(i) for i in range(0, n, step)]
        candidate = dict(out, sample=sample)
        if len(S.canonical_json(candidate).encode("utf-8")) <= max_bytes or step >= n:
            return candidate
        step *= 2


# ---------------------------------------------------------------------------
# 存储：可信原件 + 候选只读副本
# ---------------------------------------------------------------------------


class TrialObservationStore:
    """一个 trial 的观测存储：可信原件与候选副本物理分离。"""

    def __init__(self, *, trusted_dir: Path, public_dir: Path):
        self.trusted_dir = Path(trusted_dir)
        self.public_dir = Path(public_dir)
        for d in (self.trusted_dir, self.public_dir):
            d.mkdir(parents=True, exist_ok=True)
            os.chmod(d, 0o700)
        self.total_bytes = sum(p.stat().st_size for p in self.trusted_dir.glob("*.json"))

    def run(self, request: ExperimentRequest, *, gpus: bool = False,
            timeout_s: int = DEFAULT_TIMEOUT_S) -> tuple[dict[str, Any], dict[str, Any]]:
        """跑一次可信实验，落可信原件与只读副本，返回 (manifest, 观测文档)。"""
        with tempfile.TemporaryDirectory(prefix="rsi_r01_exp_") as tmp:
            out_dir = Path(tmp)
            result = _run_container(request.service_payload(), out_dir=out_dir,
                                    gpus=gpus, timeout_s=timeout_s)
            produced = out_dir / str(result.get("out_name") or "observation.json")
            if not produced.is_file():
                raise TrustedServiceUnavailable("容器未产出观测文件")
            blob_size = produced.stat().st_size
            if self.total_bytes + blob_size > S.MAX_TRIAL_OBSERVATION_BYTES:
                raise ObservationIntegrityError(
                    "TRIAL_OBSERVATION_OVER_CAP",
                    f"trial 观测累计将达 {self.total_bytes + blob_size} B > "
                    f"{S.MAX_TRIAL_OBSERVATION_BYTES} B")
            sha = _sha256_file(produced)          # 宿主重算，容器值仅交叉核对
            if result.get("observation_sha256") and result["observation_sha256"] != sha:
                raise ObservationError(
                    f"容器自报 hash {result['observation_sha256']} ≠ 宿主重算 {sha}"
                    "（仪器不一致，判 INVALID_INSTRUMENT）")
            doc = json.loads(produced.read_text(encoding="utf-8"))
            problems = S.check_observation_doc(doc, kind=request.kind)
            if problems:
                raise ObservationIntegrityError("OBSERVATION_INVALID", str(problems[:4]))
            manifest = build_manifest(
                trial_id=request.trial_id, instance_id=request.instance_id,
                run_id=str(result["run_id"]),
                scene_sha256=str(result.get("scene_sha256") or ""),
                observation_sha256=sha, rows=len(doc["t"]),
                t_start=float(doc["t"][0]), t_end=float(doc["t"][-1]),
                params=doc.get("params", {}), applied=result.get("applied", {}),
                bytes_=blob_size, kind=request.kind)
            problems = S.check_manifest_doc(manifest)
            if problems:
                raise ObservationIntegrityError("MANIFEST_INVALID", str(problems[:4]))
            # 可信原件
            original = self.trusted_dir / f"{manifest['run_id']}.json"
            shutil.copyfile(produced, original)
            os.chmod(original, 0o600)
            (self.trusted_dir / f"{manifest['run_id']}.manifest.json").write_text(
                S.canonical_json(manifest) + "\n", encoding="utf-8")
            os.chmod(self.trusted_dir / f"{manifest['run_id']}.manifest.json", 0o600)
            # 候选只读副本（内容相同，物理分离）
            copy = self.public_dir / f"{manifest['run_id']}.json"
            shutil.copyfile(original, copy)
            os.chmod(copy, 0o400)
        self.total_bytes += blob_size
        return manifest, doc

    def load_trusted(self, run_id: str, manifest: Mapping[str, Any], *,
                     expected_trial: str | None = None,
                     expected_instance: str | None = None) -> dict[str, Any]:
        return verify_observation_file(
            self.trusted_dir / f"{run_id}.json", manifest,
            expected_trial=expected_trial, expected_instance=expected_instance,
            expected_run=run_id)


def run_unchecked(request: ExperimentRequest, *, out_dir: Path,
                  gpus: bool = False, timeout_s: int = DEFAULT_TIMEOUT_S
                  ) -> tuple[dict[str, Any], dict[str, Any]]:
    """**校准专用**：真实容器执行 + 宿主重算 hash，但不套用试次观测合同。

    T2 的收敛检查要在 dt 与 dt/2 上各跑一次，行数随之不同（2001 vs 4001），
    不能走 :func:`check_observation_doc` 的固定 2001 行门。这里仍做三件事：
    容器自报 hash 与宿主重算必须一致、文件必须可解析、形状由调用方校验。
    """
    out_dir = Path(out_dir)
    result = _run_container(request.service_payload(), out_dir=out_dir,
                            gpus=gpus, timeout_s=timeout_s)
    produced = out_dir / str(result.get("out_name") or "observation.json")
    if not produced.is_file():
        raise TrustedServiceUnavailable("容器未产出观测文件")
    sha = _sha256_file(produced)
    if result.get("observation_sha256") and result["observation_sha256"] != sha:
        raise ObservationError(
            f"容器自报 hash {result['observation_sha256']} ≠ 宿主重算 {sha}"
            "（仪器不一致，判 INVALID_INSTRUMENT）")
    doc = json.loads(produced.read_text(encoding="utf-8"))
    return result, doc


def probe_environment(*, gpus: bool = False, timeout_s: int = DEFAULT_TIMEOUT_S
                      ) -> dict[str, Any]:
    """固定镜像版本探针（零付费、真实容器；AC12/AC26 的镜像身份证据）。"""
    scene = ("<mujoco model=\"rsi_r01_env\"><worldbody>"
             "<geom name=\"floor\" type=\"plane\" size=\"1 1 0.1\"/>"
             "</worldbody></mujoco>\n")
    payload = {"schema": S.SCHEMA_SERVICE_REQUEST, "kind": "env_probe",
               "scene_xml": scene}
    with tempfile.TemporaryDirectory(prefix="rsi_r01_probe_") as tmp:
        return _run_container(payload, out_dir=Path(tmp), gpus=gpus,
                              timeout_s=timeout_s)
