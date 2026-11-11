#!/usr/bin/env python3
"""RSI-R1 T1b 可信实验服务（容器内固定脚本；Plan §3.1/§5.4）。

- 入口：``/data/spec.json``（宿主用 ``orchestrator.rsi_r1.physics`` 生成）。
- 出口：``/out/raw.npz``（数组）+ ``/out/reply.json``（执行事实）。
- 本脚本**不读仓库、不读环境变量、不联网**；模型来自 spec 里的 MJCF 文本，
  该文本由宿主构造，候选无法触及。
- 记录只含物理时间步进后的状态；观测记录（JSONL）由宿主组装并校验，
  本脚本自报的任何数字都不构成证据。

拒绝路径写 ``reply.status="rejected"`` 并退 2；宿主据此报基础设施失败。
"""

import hashlib
import json
import os
import sys

import numpy as np

import mujoco

SPEC_PATH = "/data/spec.json"
OUT_DIR = "/out"
SPEC_SCHEMA = "simpraxis.rsi_r1.experiment_spec.v1"
REPLY_SCHEMA = "simpraxis.rsi_r1.experiment_reply.v1"
EXPECTED_MUJOCO = "3.15.0"
DUMP_SIZE_CAP = 48 << 20          # 服务端原始数组上限（宿主另按字节帽复核）


class Rejected(Exception):
    def __init__(self, code, detail=""):
        self.code = code
        self.detail = detail
        super().__init__(code)


def _require(cond, code, detail=""):
    if not cond:
        raise Rejected(code, detail)


def load_spec():
    with open(SPEC_PATH, "r", encoding="utf-8") as fh:
        spec = json.load(fh)
    _require(isinstance(spec, dict), "SPEC_SHAPE", "spec 不是对象")
    _require(spec.get("schema") == SPEC_SCHEMA, "SPEC_SCHEMA", "schema 不符")
    _require(spec.get("schema_version") == 1, "SPEC_VERSION", "schema_version")
    _require(spec.get("mujoco_version") == EXPECTED_MUJOCO, "ENGINE_DRIFT",
             "宿主期望 MuJoCo %s" % EXPECTED_MUJOCO)
    _require(mujoco.__version__ == EXPECTED_MUJOCO, "ENGINE_DRIFT",
             "镜像 MuJoCo %s" % mujoco.__version__)
    for key in ("run_id", "spec_hash", "study_id", "scene_xml", "run"):
        _require(key in spec, "SPEC_FIELD", "缺 %s" % key)
    _require(isinstance(spec["scene_xml"], str) and spec["scene_xml"],
             "SPEC_FIELD", "scene_xml 为空")
    return spec


def _warnings_total(data):
    total = 0
    try:
        for entry in data.warning:
            total += int(entry["number"])
    except Exception:      # noqa: BLE001 - 旧/新版本 warning 结构差异
        return 0
    return total


def run_a(spec, model, data):
    run = spec["run"]
    nsteps = int(spec["nsteps"])
    dt = float(model.opt.timestep)
    settle = run.get("settle_steps")
    _require(isinstance(settle, int) and 0 <= settle <= 20000,
             "SPEC_FIELD", "settle_steps 非法")
    v0 = float(run["initial_vx_mps"])
    _require(np.isfinite(v0), "SPEC_FIELD", "初始速度非有限")

    for _ in range(settle):
        mujoco.mj_step(model, data)

    data.qvel[:] = 0.0
    data.qvel[0] = v0
    data.qacc[:] = 0.0
    x0 = float(data.qpos[0])
    z0 = float(data.qpos[2])

    n_alloc = nsteps
    time_s = np.empty(n_alloc, dtype=np.float64)
    qpos = np.empty((n_alloc, model.nq), dtype=np.float32)
    qvel = np.empty((n_alloc, model.nv), dtype=np.float32)
    dim = np.empty(n_alloc, dtype=np.int8)
    mu_s = np.empty(n_alloc, dtype=np.float32)
    mu_t = np.empty(n_alloc, dtype=np.float32)
    force = np.empty(n_alloc, dtype=np.float32)

    pair_mu = float(model.pair_friction[0][0])
    pair_tors = float(model.pair_friction[0][2])
    res6 = np.zeros(6, dtype=np.float64)
    status, stop_index, used = "ok", None, 0
    for i in range(nsteps):
        mujoco.mj_step(model, data)
        ncon = int(data.ncon)
        if ncon == 0:
            dim_i, f_i = 0, 0.0
        else:
            dims = [int(data.contact[j].dim) for j in range(ncon)]
            dim_i = min(dims)
            f_sum = 0.0
            for j in range(ncon):
                mujoco.mj_contactForce(model, data, j, res6)
                f_sum += float(res6[0])
            f_i = f_sum if f_sum > 0.0 else 0.0
        if not (np.isfinite(data.qpos).all() and np.isfinite(data.qvel).all()):
            status = "nonfinite"
            break
        time_s[used] = (i + 1) * dt
        qpos[used] = data.qpos
        qvel[used] = data.qvel
        dim[used] = dim_i
        mu_s[used] = pair_mu
        mu_t[used] = pair_tors
        force[used] = f_i
        used += 1

    arrays = {
        "time_s": time_s[:used],
        "qpos": qpos[:used],
        "qvel": qvel[:used],
        "contact_dim": dim[:used],
        "friction_sliding": mu_s[:used],
        "friction_torsional": mu_t[:used],
        "contact_force_n": force[:used],
        # 起滑参考位姿随**数据**通道返回（不只在 reply 自述里）：宿主用它算位移，
        # 并与 reply.extra 的同名值互证。
        "initial_x_m": np.asarray([x0], dtype=np.float64),
        "initial_z_m": np.asarray([z0], dtype=np.float64),
    }
    extra = {"x0_m": x0, "z0_m": z0, "displacement_note": "x - x0, host recomputes"}
    return status, stop_index, arrays, extra, _warnings_total(data)


def run_b(spec, model, data):
    run = spec["run"]
    nsteps = int(spec["nsteps"])
    dt = float(model.opt.timestep)
    dt_us = int(run["dt_ticks_us"])
    tc_us = int(run["tc_ticks_us"])
    _require(dt_us > 0 and tc_us % dt_us == 0, "SPEC_FIELD", "Tc/dt 不整除")
    stride = tc_us // dt_us
    amp = float(run["target_amplitude_rad"])
    freq = float(run["target_freq_hz"])
    kp = float(run["kp"])
    kd = float(run["kd"])
    limit = float(run["torque_limit_nm"])
    two_pi = 2.0 * np.pi

    n_alloc = nsteps
    time_s = np.empty(n_alloc, dtype=np.float64)
    qpos = np.empty((n_alloc, model.nq), dtype=np.float32)
    qvel = np.empty((n_alloc, model.nv), dtype=np.float32)
    target = np.empty(n_alloc, dtype=np.float32)
    command = np.empty(n_alloc, dtype=np.float32)
    applied = np.empty(n_alloc, dtype=np.float32)
    index = np.empty(n_alloc, dtype=np.int32)

    # t=0 的第一次更新（零阶保持；之后每 stride 步更新一次）
    k = 0
    q = float(data.qpos[0])
    w = float(data.qvel[0])
    tgt_now = amp * np.sin(two_pi * freq * 0.0)
    cmd = kp * (tgt_now - q) - kd * w
    app = float(np.clip(cmd, -limit, limit))
    data.qfrc_applied[:] = 0.0
    data.qfrc_applied[0] = app
    saturation = 1 if cmd != app else 0
    status, stop_index, used = "ok", None, 0
    events = []
    for i in range(nsteps):
        mujoco.mj_step(model, data)
        t_end = (i + 1) * dt
        tgt_end = amp * np.sin(two_pi * freq * t_end)
        if not (np.isfinite(data.qpos).all() and np.isfinite(data.qvel).all()):
            status = "nonfinite"
            events.append({"time_s": t_end, "reason": "NONFINITE"})
            break
        time_s[used] = t_end
        qpos[used] = data.qpos
        qvel[used] = data.qvel
        target[used] = tgt_end
        command[used] = cmd
        applied[used] = app
        index[used] = k
        used += 1
        if abs(float(data.qpos[0])) > 4.0:
            status = "instability"
            stop_index = i
            events.append({"time_s": t_end, "reason": "ABS_Q_GT_4"})
            break
        if (i + 1) % stride == 0 and (i + 1) < nsteps:
            k = (i + 1) // stride
            q = float(data.qpos[0])
            w = float(data.qvel[0])
            tgt_now = amp * np.sin(two_pi * freq * t_end)
            cmd = kp * (tgt_now - q) - kd * w
            app = float(np.clip(cmd, -limit, limit))
            data.qfrc_applied[:] = 0.0
            data.qfrc_applied[0] = app
            saturation += 1 if cmd != app else 0

    arrays = {
        "time_s": time_s[:used],
        "qpos": qpos[:used],
        "qvel": qvel[:used],
        "target_q": target[:used],
        "command_torque": command[:used],
        "applied_torque": applied[:used],
        "control_update_index": index[:used],
    }
    extra = {"saturation_count": int(saturation), "instability_events": events}
    return status, stop_index, arrays, extra, _warnings_total(data)


def write_outputs(spec, status, stop_index, arrays, extra, warnings):
    total = sum(int(a.nbytes) for a in arrays.values())
    _require(total <= DUMP_SIZE_CAP, "OUTPUT_TOO_LARGE", "数组 %d B" % total)
    raw_path = os.path.join(OUT_DIR, "raw.npz")
    np.savez_compressed(raw_path, **arrays)
    with open(raw_path, "rb") as fh:
        raw_sha = hashlib.sha256(fh.read()).hexdigest()
    reply = {
        "schema": REPLY_SCHEMA,
        "schema_version": 1,
        "run_id": spec["run_id"],
        "spec_hash": spec["spec_hash"],
        "status": status,
        "code": None,
        "nsteps": int(len(arrays["time_s"])),
        "nsteps_requested": int(spec["nsteps"]),
        "stop_index": stop_index,
        "solver_warning_count": int(warnings),
        "mujoco_version": mujoco.__version__,
        "raw_file": "raw.npz",
        "raw_sha256": raw_sha,
        "extra": extra,
        "arrays": {k: [list(v.shape), str(v.dtype)] for k, v in arrays.items()},
    }
    path = os.path.join(OUT_DIR, "reply.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(reply, fh, ensure_ascii=False, sort_keys=True,
                  separators=(",", ":"), allow_nan=False)
    return reply


def write_rejected(code, detail):
    reply = {"schema": REPLY_SCHEMA, "schema_version": 1, "status": "rejected",
             "code": code, "detail": str(detail)[:500]}
    try:
        with open(os.path.join(OUT_DIR, "reply.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(reply, fh, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)
    except OSError:
        pass


def main():
    try:
        spec = load_spec()
        model = mujoco.MjModel.from_xml_string(spec["scene_xml"])
        data = mujoco.MjData(model)
        dt = float(model.opt.timestep)
        _require(abs(dt - float(spec["dt_s"])) <= 1e-15, "DT_MISMATCH",
                 "XML timestep %r != spec dt %r" % (dt, spec["dt_s"]))
        kind = spec["run"].get("kind")
        if kind == "A":
            _require(model.nq == 7 and model.nv == 6, "SCENE_SHAPE",
                     "A 期望 free joint 7/6，实为 %d/%d" % (model.nq, model.nv))
            out = run_a(spec, model, data)
        elif kind == "B":
            _require(model.nq == 1 and model.nv == 1, "SCENE_SHAPE",
                     "B 期望单 hinge 1/1，实为 %d/%d" % (model.nq, model.nv))
            out = run_b(spec, model, data)
        else:
            raise Rejected("STUDY_UNKNOWN", repr(kind))
        write_outputs(spec, *out)
        return 0
    except Rejected as exc:
        write_rejected(exc.code, exc.detail)
        return 2
    except Exception as exc:      # noqa: BLE001 - 容器内最后一层兜底必须留证
        write_rejected("SERVICE_CRASH", "%s: %s" % (type(exc).__name__, exc))
        return 3


if __name__ == "__main__":
    sys.exit(main())
