#!/usr/bin/env python3
"""RSI-R0.1 可信实验服务（**容器内**运行，独立进程，只写 /out）。

与 R0 的 ``rsi_r0.service`` 的关键差异（计划 §4.2）：这里**逐 simulaton step 落盘
完整观测**（``t=0`` 初态 + 2000 步 ⇒ 2001 行，dt=0.002 s，末点恰 T=4 s），而不是
只回传下采样摘要。摘要会丢掉决定沉降的末段/越界点，本轮合同明确不接受。

信任边界：

* 只接受有界数值白名单 + 宿主构造的 ``scene_xml``；不接受代码/路径/动态 import；
* 场景/故障/宿主注入字段永不来自候选；
* 回传的 ``applied`` 是**从模型读回**的实际生效值（AC05：请求值=应用值）；
* ``observation_sha256`` 由容器自算**仅供交叉核对**，宿主必须自己重算并以宿主
  值为准（容器自报不构成证据）。

本文件**不 import 仓库任何模块**：它在镜像里跑，只有 mujoco/numpy 可用。
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import uuid

SCHEMA = "simpraxis.rsi_r01.service.v1"

os.environ.setdefault("MUJOCO_GL", "egl")

MAX_STEPS = 100_000
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_SCENE_BYTES = 200_000
OUT_DIR = "/out"
OUT_NAME = "observation.json"
ALLOWED_KINDS = ("pendulum_run", "push_control", "env_probe")


def _fail(msg: str, **extra) -> None:
    payload = {"schema": SCHEMA, "ok": False, "error": str(msg)[:500]}
    payload.update(extra)
    print("RESULT_JSON " + json.dumps(payload, ensure_ascii=False, sort_keys=True))
    sys.exit(2)


def _num(req: dict, key: str, lo: float, hi: float):
    v = req.get(key)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        _fail(f"字段 {key} 必须是数值")
    v = float(v)
    if not (lo <= v <= hi):
        _fail(f"字段 {key}={v} 越界 [{lo}, {hi}]")
    return v


def _canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False)


def _write_observation(doc: dict, *, out_extra: dict) -> int:
    """把完整观测原子落盘，并回传**宿主可交叉核对**的元数据。

    ``observation_sha256`` 是容器自算值，宿主必须重新计算并以宿主值为准
    （容器自报不构成证据，§4.2）。
    """
    text = _canon(doc) + "\n"
    blob = text.encode("utf-8")
    if len(blob) > MAX_FILE_BYTES:
        _fail(f"观测文件 {len(blob)} B 超过 {MAX_FILE_BYTES} B 上限")
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, OUT_NAME)
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(blob)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    out = {"schema": SCHEMA, "ok": True, "observation_sha256":
           hashlib.sha256(blob).hexdigest(), "rows": len(doc["t"]),
           "t_start": doc["t"][0], "t_end": doc["t"][-1], "bytes": len(blob),
           "out_name": OUT_NAME, "params": doc["params"]}
    out.update(out_extra)
    print("RESULT_JSON " + _canon(out))
    return 0


def main() -> int:
    raw = sys.stdin.read(MAX_SCENE_BYTES * 4)
    try:
        req = json.loads(raw)
    except json.JSONDecodeError as exc:
        _fail(f"请求不是 JSON：{exc}")
    if not isinstance(req, dict) or req.get("schema") != SCHEMA:
        _fail(f"schema 必须是 {SCHEMA}")
    kind = req.get("kind")
    if kind not in ALLOWED_KINDS:
        _fail(f"未知 kind={kind!r}（可选 {ALLOWED_KINDS}）")
    scene_xml = req.get("scene_xml")
    if not isinstance(scene_xml, str) or not scene_xml.strip():
        _fail("缺少 scene_xml 字符串")
    if len(scene_xml.encode("utf-8")) > MAX_SCENE_BYTES:
        _fail("scene_xml 超过 200 KB")

    import mujoco
    import numpy as np

    import numpy as _np  # noqa: F401  （显式证明 numpy 在场）

    try:
        model = mujoco.MjModel.from_xml_string(scene_xml)
    except Exception as exc:  # noqa: BLE001
        _fail(f"场景加载失败：{type(exc).__name__}: {exc}")
    data = mujoco.MjData(model)
    scene_sha = hashlib.sha256(scene_xml.encode("utf-8")).hexdigest()
    run_id = "run_" + uuid.uuid4().hex

    if kind == "env_probe":
        out = {"schema": SCHEMA, "ok": True, "kind": kind, "run_id": run_id,
               "python_version": sys.version.split()[0],
               "numpy_version": np.__version__,
               "mujoco_version": mujoco.__version__,
               "mujoco_gl_env": os.environ.get("MUJOCO_GL")}
        print("RESULT_JSON " + _canon(out))
        return 0

    # ---- push_control：E/D 族的真实执行（逐步记录世界系位置/速度） ----
    if kind == "push_control":
        body = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_BODY, "box")
        if body < 0:
            _fail("push_control 需要场景含名为 box 的自由刚体")
        tx = _num(req, "target_x", -50.0, 50.0)
        ty = _num(req, "target_y", -50.0, 50.0)
        kp = _num(req, "kp", 0.0, 5000.0)
        kd = _num(req, "kd", 0.0, 500.0)
        fmax = _num(req, "max_force", 0.0, 500.0)
        timestep = _num(req, "timestep", 0.0005, 0.01)
        steps = req.get("steps")
        if isinstance(steps, bool) or not isinstance(steps, int) or not (1 <= steps <= MAX_STEPS):
            _fail(f"steps 必须是 1..{MAX_STEPS} 的整数")
        # 宿主私有注入项（D v2 故障载体：恒定外力偏置 / 执行器死区）；
        # 候选请求里没有这些键。场景级故障（friction_scale）走 scene_xml。
        force_bias_x = _num(req, "force_bias_x", -100.0, 100.0) \
            if "force_bias_x" in req else 0.0
        force_bias_y = _num(req, "force_bias_y", -100.0, 100.0) \
            if "force_bias_y" in req else 0.0
        deadzone = _num(req, "deadzone", 0.0, 100.0) if "deadzone" in req else 0.0
        scene_sha = hashlib.sha256(scene_xml.encode("utf-8")).hexdigest()
        run_id = "run_" + uuid.uuid4().hex
        trial_id = req.get("trial_id")
        instance_id = req.get("instance_id")
        if not isinstance(trial_id, str) or not trial_id:
            _fail("缺 trial_id 字符串")
        if not isinstance(instance_id, str) or not instance_id:
            _fail("缺 instance_id 字符串")

        model.opt.timestep = timestep
        mujoco.mj_forward(model, data)
        # 场景 XML 若只建了 z 向长条 box，绕质心的力臂就是它的半高（R0 已校准）。
        box_geom = -1
        for g in range(model.ngeom):
            if model.geom_bodyid[g] == body:
                box_geom = g
                break
        lever = float(model.geom_size[box_geom][2]) if box_geom >= 0 else 0.0

        t_list = [0.0]
        x_list = [float(data.xpos[body][0])]
        y_list = [float(data.xpos[body][1])]
        vx_list = [float(data.qvel[model.body_dofadr[body]])]
        vy_list = [float(data.qvel[model.body_dofadr[body] + 1])]
        for i in range(1, steps + 1):
            mujoco.mj_forward(model, data)
            pos = data.xpos[body]
            qadr = int(model.body_dofadr[body])
            vel = data.qvel[qadr:qadr + 3]
            err = np.array([tx - pos[0], ty - pos[1], 0.0])
            cmd = kp * err - kd * vel
            norm = float(np.linalg.norm(cmd))
            if norm > fmax and norm > 0:
                cmd = cmd * (fmax / norm)
            # 故障语义：先出 fmax，再判死区，最后叠加恒定外力偏置。
            if deadzone > 0.0 and float(np.linalg.norm(cmd)) < deadzone:
                cmd = np.zeros(3)
            cmd = cmd + np.array([force_bias_x, force_bias_y, 0.0])
            data.xfrc_applied[body, :3] = cmd
            data.xfrc_applied[body, 3:6] = (lever * cmd[1], -lever * cmd[0], 0.0)
            mujoco.mj_step(model, data)
            t_list.append(round(i * timestep, 12))
            x_list.append(float(data.xpos[body][0]))
            y_list.append(float(data.xpos[body][1]))
            vx_list.append(float(data.qvel[qadr]))
            vy_list.append(float(data.qvel[qadr + 1]))

        final_speed = float(np.hypot(vx_list[-1], vy_list[-1]))
        distance = float(np.hypot(x_list[-1] - tx, y_list[-1] - ty))
        doc = {"schema": "simpraxis.rsi_r01.observation.v1", "kind": kind,
               "trial_id": trial_id, "run_id": run_id, "instance_id": instance_id,
               "params": {"kp": kp, "kd": kd, "max_force": fmax,
                          "target_x": tx, "target_y": ty,
                          "timestep": timestep, "steps": steps},
               "t": t_list, "x": x_list, "y": y_list,
               "vx": vx_list, "vy": vy_list}
        extra = {
            "kind": kind, "run_id": run_id, "trial_id": trial_id,
            "instance_id": instance_id, "scene_sha256": scene_sha,
            "final_distance": distance, "final_speed": final_speed,
            "target": [tx, ty], "applied": dict(doc["params"]),
            "mujoco_version": mujoco.__version__, "steps_run": steps}
        if force_bias_x or force_bias_y or deadzone:
            extra["host_injected"] = {"force_bias_x": force_bias_x,
                                      "force_bias_y": force_bias_y,
                                      "deadzone": deadzone}
        return _write_observation(doc, out_extra=extra)

    # ---- pendulum_run：完整逐步观测落盘 ----
    hinge = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_JOINT, "hinge")
    if hinge < 0:
        _fail("pendulum_run 需要场景含名为 hinge 的关节")
    damping = _num(req, "damping", 0.0, 5.0)
    q0 = _num(req, "initial_angle", -3.2, 3.2)
    timestep = _num(req, "timestep", 0.0005, 0.01)
    steps = req.get("steps")
    if isinstance(steps, bool) or not isinstance(steps, int) or not (1 <= steps <= MAX_STEPS):
        _fail(f"steps 必须是 1..{MAX_STEPS} 的整数")
    trial_id = req.get("trial_id")
    if not isinstance(trial_id, str) or not trial_id:
        _fail("缺 trial_id 字符串")
    instance_id = req.get("instance_id")
    if not isinstance(instance_id, str) or not instance_id:
        _fail("缺 instance_id 字符串")

    dadr = int(model.jnt_dofadr[hinge])
    adr = int(model.jnt_qposadr[hinge])
    # 宿主注入的阻尼**写到模型上**，再从模型读回作为 applied 证据。
    model.dof_damping[dadr] = damping
    model.opt.timestep = timestep
    data.qpos[adr] = q0
    data.qvel[dadr] = 0.0
    mujoco.mj_forward(model, data)

    applied = {"damping": float(model.dof_damping[dadr]),
               "initial_angle": float(data.qpos[adr]),
               "timestep": float(model.opt.timestep)}
    for key in ("damping", "initial_angle", "timestep"):
        if abs(applied[key] - {"damping": damping, "initial_angle": q0,
                               "timestep": timestep}[key]) > 0.0:
            _fail(f"参数未按请求生效：{key}")

    t_list = [0.0]
    th_list = [float(data.qpos[adr])]
    om_list = [float(data.qvel[dadr])]
    for i in range(1, steps + 1):
        mujoco.mj_step(model, data)
        t_list.append(round(i * timestep, 12))
        th_list.append(float(data.qpos[adr]))
        om_list.append(float(data.qvel[dadr]))

    doc = {"schema": "simpraxis.rsi_r01.observation.v1", "kind": kind,
           "trial_id": trial_id, "run_id": run_id, "instance_id": instance_id,
           "params": {"damping": damping, "initial_angle": q0,
                      "timestep": timestep, "steps": steps},
           "t": t_list, "theta": th_list, "omega": om_list}
    return _write_observation(doc, out_extra={
        "kind": kind, "run_id": run_id, "trial_id": trial_id,
        "instance_id": instance_id, "scene_sha256": scene_sha, "applied": applied,
        "mujoco_version": mujoco.__version__, "steps_run": steps})


if __name__ == "__main__":
    sys.exit(main())
