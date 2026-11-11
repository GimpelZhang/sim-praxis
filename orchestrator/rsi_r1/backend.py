"""RSI-R1 真物理 backend：``ExperimentSpec`` → 容器 → 宿主规范化 raw（T1b）。

分工（Plan §5.4）：

* MJCF 与 spec 文档由 :mod:`orchestrator.rsi_r1.physics` 在宿主构造；
* 容器只跑固定脚本 ``experiment_service.py``（``sandbox.run_experiment`` 同一
  §5.4 安全面），输出数组以**字节**回到宿主（``evidence.blobs``）；
* 观测记录由**本模块**从数组重建 + 校验（``observations.validate_record``），
  容器自报的每个数字都要与数组互证，对不上就是基础设施失败；
* raw 落盘（JSONL + 容器原始 npz + meta），每件进 manifest。

失败语义：

* 基础设施失败（镜像/沙箱/协议/rejected/形状不符/自报与数组不符、A 非有限）
  → :class:`BackendInfraError`，``execute_batch`` 整批 BLOCKED，不补样本；
* B 的 |q|>4 或非有限是**有效科学数据**（``LOSS_CAP``），以 instability_events
  记入记录，不当崩溃。
"""
from __future__ import annotations

import hashlib
import io
import json
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from orchestrator.common.clock import utc_now_iso_z
from orchestrator.rsi_r1 import observations as O
from orchestrator.rsi_r1 import physics as P
from orchestrator.rsi_r1 import sandbox as SB
from orchestrator.rsi_r1.experiments import (BackendInfraError, ExperimentSpec)

META_SCHEMA = "simpraxis.rsi_r1.run_meta.v1"
RECORD_SCHEMA = O.RAW_SCHEMA

#: 容器 reply 允许的状态；``rejected`` 之外的任何值都是协议违约。
REPLY_STATUSES = ("ok", "instability", "nonfinite")

#: 数组名 → 记录字段（两 study 共用部分）。
_COMMON_ARRAYS = ("time_s", "qpos", "qvel")
_A_ARRAYS = _COMMON_ARRAYS + ("contact_dim", "friction_sliding",
                              "friction_torsional", "contact_force_n",
                              "initial_x_m", "initial_z_m")
_B_ARRAYS = _COMMON_ARRAYS + ("target_q", "command_torque", "applied_torque",
                              "control_update_index")


def _arr(arrays: Mapping[str, Any], name: str) -> list:
    try:
        return arrays[name]
    except KeyError:
        raise BackendInfraError("RAW_FIELD") from None


def _flat(values: Any) -> "list[float]":
    out: "list[float]" = []
    for v in values:
        if isinstance(v, list):
            out.extend(float(x) for x in v)
        else:
            out.append(float(v))
    return out


def _finite_count(arrays: Mapping[str, Any]) -> int:
    bad = 0
    for name in arrays:
        for v in _flat(arrays[name]):
            if not math.isfinite(v):
                bad += 1
    return bad


@dataclass
class PhysicsBackend:
    """``execute_batch`` 的 ``backend`` 参数实现（``run(spec) -> record``）。"""

    observations_root: Path
    evidence_root: Path
    work_root: Path
    runner: Any
    image: str
    batch_id: str
    service_source: bytes = field(default_factory=P.service_source)
    entries: "list[O.ManifestEntry]" = field(default_factory=list)
    metas: "dict[str, dict[str, Any]]" = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.observations_root = Path(self.observations_root)
        self.evidence_root = Path(self.evidence_root)
        self.work_root = Path(self.work_root)

    # -- 主路径 ---------------------------------------------------------
    def run(self, spec: ExperimentSpec) -> "dict[str, Any]":
        if not isinstance(spec, ExperimentSpec):
            raise BackendInfraError(f"spec 类型 {type(spec).__name__} 不接受")
        doc = P.build_spec_document(spec)
        started_at_utc = utc_now_iso_z()
        try:
            ev = SB.run_experiment(self.service_source, doc,
                                   evidence_root=self.evidence_root,
                                   work_root=self.work_root,
                                   runner=self.runner, image=self.image)
        except SB.SandboxError as exc:
            raise BackendInfraError(f"sandbox {exc.code}: {exc}") from None
        reply = self._check_reply(ev, spec)
        arrays = self._read_arrays(ev, reply, spec)
        record = self._build_record(spec, reply, arrays, envelope=doc)
        target = self._paths(spec.run_id)
        if target["jsonl"].exists() or target["npz"].exists():
            raise BackendInfraError(f"RAW_EXISTS: {target['jsonl']} 已存在，拒绝覆盖")
        O.write_record_jsonl(target["jsonl"], record)
        npz_bytes = ev.blobs["raw.npz"]
        with open(target["npz"], "wb") as fh:
            fh.write(npz_bytes)
        os.chmod(target["npz"], 0o600)
        meta = self._meta(spec, reply, ev, arrays, doc, record=record,
                          started_at_utc=started_at_utc)
        target["meta"].write_text(
            json.dumps(meta, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":"), allow_nan=False) + "\n",
            encoding="utf-8")
        os.chmod(target["meta"], 0o600)
        self.metas[spec.run_id] = meta
        self.entries.append(O.manifest_entry(
            spec.run_id, target["jsonl"].name, target["jsonl"],
            nsteps=len(record["time_s"])))
        return record

    # -- 校验 -----------------------------------------------------------
    def _check_reply(self, ev: SB.SandboxEvidence,
                     spec: ExperimentSpec) -> "dict[str, Any]":
        if ev.timed_out:
            raise BackendInfraError("SANDBOX_TIMEOUT")
        if ev.exit_code != 0:
            detail = (ev.reply or {}).get("code") or ev.stderr_tail or ""
            raise BackendInfraError(f"SANDBOX_EXIT {ev.exit_code}: {detail}")
        reply = ev.reply
        if not isinstance(reply, dict):
            raise BackendInfraError("REPLY_MISSING")
        if reply.get("schema") != "simpraxis.rsi_r1.experiment_reply.v1":
            raise BackendInfraError(f"REPLY_SCHEMA {reply.get('schema')!r}")
        if reply.get("run_id") != spec.run_id:
            raise BackendInfraError("REPLY_RUN_ID")
        if reply.get("spec_hash") != spec.spec_hash:
            raise BackendInfraError("REPLY_SPEC_HASH")
        if reply.get("mujoco_version") != P.MUJOCO_VERSION:
            raise BackendInfraError(f"ENGINE_DRIFT {reply.get('mujoco_version')!r}")
        status = reply.get("status")
        if status not in REPLY_STATUSES:
            raise BackendInfraError(f"REPLY_STATUS {status!r}")
        if "raw.npz" not in ev.blobs:
            raise BackendInfraError("RAW_MISSING")
        digest = hashlib.sha256(ev.blobs["raw.npz"]).hexdigest()
        if reply.get("raw_sha256") != digest:
            raise BackendInfraError("RAW_SHA_MISMATCH")
        return reply

    def _read_arrays(self, ev: SB.SandboxEvidence, reply: Mapping[str, Any],
                     spec: ExperimentSpec) -> "dict[str, Any]":
        import numpy as np  # 局部 import：只有真跑物理时才需要

        with np.load(io.BytesIO(ev.blobs["raw.npz"]), allow_pickle=False) as data:
            arrays = {k: data[k].tolist() for k in data.files}
        listed = reply.get("arrays")
        if not isinstance(listed, Mapping):
            raise BackendInfraError("REPLY_ARRAYS")
        missing = sorted(set(listed) - set(arrays))
        extra = sorted(set(arrays) - set(listed))
        if missing or extra:
            raise BackendInfraError(f"ARRAY_SET missing={missing} extra={extra}")
        for name, meta in listed.items():
            shape = list(np.asarray(arrays[name]).shape)
            if not isinstance(meta, list) or len(meta) != 2:
                raise BackendInfraError(f"ARRAY_META {name}")
            if list(meta[0]) != shape:
                raise BackendInfraError(f"ARRAY_SHAPE {name}: {meta[0]} != {shape}")
        return arrays

    def _build_record(self, spec: ExperimentSpec, reply: Mapping[str, Any],
                      arrays: Mapping[str, Any], *,
                      envelope: Mapping[str, Any]) -> "dict[str, Any]":
        status = reply["status"]
        n = len(_arr(arrays, "time_s"))
        if status == "ok" and n != spec.nsteps:
            raise BackendInfraError(f"STEP_SHORT {n} != {spec.nsteps}")
        if status != "ok" and n >= spec.nsteps:
            raise BackendInfraError(f"STEP_LONG status={status}")
        if n < 2:
            raise BackendInfraError("TOO_SHORT")
        params = {**dict(spec.physical_params), **dict(spec.controller_params)}
        record: "dict[str, Any]" = {
            "time_s": _arr(arrays, "time_s"),
            "qpos": _arr(arrays, "qpos"),
            "qvel": _arr(arrays, "qvel"),
            "params_applied": params,
            "engine": {"name": "mujoco", "version": reply["mujoco_version"]},
            "run_id": spec.run_id,
            "spec_hash": spec.spec_hash,
            "block_id": spec.block_id,
            "nsteps": n,
            "nonfinite_count": _finite_count(arrays),
            "solver_warnings": ([] if not reply.get("solver_warning_count")
                                else [f"mujoco_warning_count={int(reply['solver_warning_count'])}"]),
        }
        if spec.study_id == "A":
            self._fill_a(record, arrays, reply, params)
        else:
            self._fill_b(record, arrays, reply, params)
        if record["nonfinite_count"] != 0:
            raise BackendInfraError(
                f"NONFINITE_RAW {record['nonfinite_count']}")
        return O.validate_record(record, scene_id=spec.scene_id)

    def _fill_a(self, record: "dict[str, Any]", arrays: Mapping[str, Any],
                reply: Mapping[str, Any], params: Mapping[str, str]) -> None:
        if reply["status"] != "ok":
            # A 的非有限状态无法构成有效观测：整批基础设施失败（不补样本）。
            raise BackendInfraError(f"A_STATUS status={reply['status']}")
        mu = float(params["sliding_friction"])
        tors = float(params["torsional_friction_m"])
        sliding = _flat(_arr(arrays, "friction_sliding"))
        twist = _flat(_arr(arrays, "friction_torsional"))
        if any(abs(v - mu) > 1e-6 for v in sliding):
            raise BackendInfraError("FRICTION_MISMATCH sliding")
        if any(abs(v - tors) > 1e-6 for v in twist):
            raise BackendInfraError("FRICTION_MISMATCH torsional")
        x0 = _arr(arrays, "initial_x_m")[0]
        if float(reply.get("extra", {}).get("x0_m", float("nan"))) != float(x0):
            raise BackendInfraError("X0_CHANNEL_MISMATCH")
        record["contact_pair"] = "box_floor"
        record["contact_dim_per_step"] = [int(d) for d in _arr(arrays, "contact_dim")]
        record["friction_sliding_per_step"] = _arr(arrays, "friction_sliding")
        record["friction_torsional_per_step"] = _arr(arrays, "friction_torsional")
        record["contact_force_n"] = _arr(arrays, "contact_force_n")

    def _fill_b(self, record: "dict[str, Any]", arrays: Mapping[str, Any],
                reply: Mapping[str, Any], params: Mapping[str, str]) -> None:
        status = reply["status"]
        index = [int(k) for k in _arr(arrays, "control_update_index")]
        for i in range(1, len(index)):
            if index[i] < index[i - 1]:
                raise BackendInfraError("CONTROL_INDEX_BACKWARDS")
        limit = float(P.B_TORQUE_LIMIT)
        command = _flat(_arr(arrays, "command_torque"))
        applied = _flat(_arr(arrays, "applied_torque"))
        for c, a in zip(command, applied):
            expect = max(-limit, min(limit, c))
            if abs(expect - a) > 1e-9:
                raise BackendInfraError("TORQUE_CLIP_MISMATCH")
        # 饱和度由宿主从数组重算（每个更新序号第一次出现的 command 判一次）
        seen: "set[int]" = set()
        sat = 0
        for k, c in zip(index, command):
            if k in seen:
                continue
            seen.add(k)
            if abs(c) > limit:
                sat += 1
        extra = reply.get("extra") or {}
        if int(extra.get("saturation_count", -1)) != sat:
            raise BackendInfraError(
                f"SATURATION_MISMATCH reply={extra.get('saturation_count')} host={sat}")
        events = extra.get("instability_events") or []
        if not isinstance(events, list):
            raise BackendInfraError("INSTABILITY_EVENTS")
        reasons = {str(e.get("reason")) for e in events if isinstance(e, Mapping)}
        if status == "ok" and events:
            raise BackendInfraError("STATUS_EVENT_MISMATCH")
        if status == "instability":
            if reasons != {"ABS_Q_GT_4"}:
                raise BackendInfraError(f"EVENT_REASON {sorted(reasons)}")
            q0 = [row[0] if isinstance(row, list) else row
                  for row in _arr(arrays, "qpos")]
            if max(abs(float(v)) for v in q0) <= 4.0:
                raise BackendInfraError("INSTABILITY_UNOBSERVED")
        if status == "nonfinite" and reasons != {"NONFINITE"}:
            raise BackendInfraError(f"EVENT_REASON {sorted(reasons)}")
        record["target_q"] = _arr(arrays, "target_q")
        record["command_torque"] = _arr(arrays, "command_torque")
        record["applied_torque"] = _arr(arrays, "applied_torque")
        record["control_update_index"] = index
        record["saturation_count"] = sat
        record["instability_events"] = [dict(e) for e in events]

    # -- 落盘 -----------------------------------------------------------
    def _paths(self, run_id: str) -> "dict[str, Path]":
        base = self.observations_root / self.batch_id
        base.mkdir(parents=True, exist_ok=True, mode=0o700)
        return {"jsonl": base / f"{run_id}.jsonl",
                "npz": base / f"{run_id}.npz",
                "meta": base / f"{run_id}.meta.json"}

    def _meta(self, spec: ExperimentSpec, reply: Mapping[str, Any],
              ev: SB.SandboxEvidence, arrays: Mapping[str, Any],
              envelope: Mapping[str, Any], *,
              record: "Mapping[str, Any] | None" = None,
              started_at_utc: "str | None" = None) -> "dict[str, Any]":
        extra = reply.get("extra") or {}
        initial: "dict[str, Any]" = {}
        if spec.study_id == "A":
            initial = {"initial_x_m": float(arrays["initial_x_m"][0]),
                       "initial_z_m": float(arrays["initial_z_m"][0])}
        # 复跑独立性所需的执行身份（``replay.EXECUTION_KEYS``）：
        # execution/container 来自本轮**自有**容器名；started_at 是宿主发车时间；
        # raw_sha256 是**搬运身份**（记录字节 + 执行信封），不是内容哈希——
        # 确定性物理在同输入下内容逐位相同是合法事实（``record_identical``），
        # 而执行身份必须不同，否则 inspector 无法区分「复跑」与「抄原件」。
        execution_id = ev.container
        raw_identity = hashlib.sha256(json.dumps(
            {"record": record, "envelope": dict(envelope),
             "execution_id": execution_id, "started_at_utc": started_at_utc},
            ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
        return {
            "schema": META_SCHEMA, "schema_version": 1,
            "batch_id": self.batch_id, "run_id": spec.run_id,
            "spec_hash": spec.spec_hash, "study_id": spec.study_id,
            "scene_id": spec.scene_id, "condition_id": spec.condition_id,
            "block_id": spec.block_id, "status": reply["status"],
            "nsteps_requested": spec.nsteps,
            "nsteps_recorded": len(arrays["time_s"]),
            "stop_index": reply.get("stop_index"),
            "engine": {"name": "mujoco", "version": reply["mujoco_version"]},
            "sandbox": ev.to_dict(),
            "spec_envelope": dict(envelope),
            "reply_extra": {k: v for k, v in extra.items() if k != "displacement_note"},
            "execution_id": execution_id,
            "container_id": ev.container,
            "started_at_utc": started_at_utc,
            "raw_sha256": raw_identity,
            "root": str(self.evidence_root),
            "cache_hit": False,
            **initial,
        }

    def write_manifest(self) -> "dict[str, Any]" | None:
        if not self.entries:
            return None
        studies = set()
        for run_id, meta in self.metas.items():
            studies.add(meta["study_id"])
        if len(studies) != 1:
            raise BackendInfraError(f"MANIFEST_MULTI_STUDY {sorted(studies)}")
        study = studies.pop()
        manifest = O.build_manifest(self.entries, study=study,
                                    batch_id=self.batch_id)
        target = self.observations_root / self.batch_id / "manifest.json"
        target.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False) + "\n",
                          encoding="utf-8")
        os.chmod(target, 0o600)
        O.verify_manifest(manifest, target.parent)
        return manifest
