#!/usr/bin/env python3
"""RSI-R0 可信 MuJoCo 观测服务（**容器内**运行，独立进程）。

信任边界（计划 §4.4）：候选只经本进程的**有界数值白名单**发起实验；本进程自己
生成 run_id、加载固定场景、真实 step、逐 step 记录事件并回传只读摘要。进程内
不接受代码、路径、动态 import、任意命令，也不读候选目录。

输入：stdin 一行 JSON（``rsi_r0.service.v1``）。
输出：stdout 一行 ``RESULT_JSON {...}``；任何失败同样单行回传并 rc=2。

本文件**不 import 仓库任何模块**：它在镜像里跑，只有 mujoco/numpy 可用。
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import uuid

REQUEST_SCHEMA = "rsi_r0.service.v1"

#: 渲染后端必须**显式**钉 EGL：2026-11-08 实测不设此项时 MuJoCo 走默认 GLFW/OSMesa
#: 软件路径，即使容器有 `--gpus all` 也会拿 llvmpipe 出图（pixel_std≈2.96 的
#: "成功"渲染）。GL 厂商策略由宿主侧 :mod:`orchestrator.rsi_r0.trusted` 判负。
os.environ.setdefault("MUJOCO_GL", "egl")

#: 硬界（越界拒，不夹取）：数值白名单的唯一真值在可信侧。
MAX_STEPS = 1_000_000
MAX_FORCE = 500.0
MAX_SERIES_POINTS = 2000
MAX_SCENE_BYTES = 200_000
ALLOWED_KINDS = ("push_control", "free_run", "render_probe", "env_probe")


def _fail(msg: str, **extra) -> None:
    payload = {"schema": REQUEST_SCHEMA, "ok": False, "error": msg[:500]}
    payload.update(extra)
    print("RESULT_JSON " + json.dumps(payload, ensure_ascii=False, sort_keys=True))
    sys.exit(2)


def _num(req: dict, key: str, lo: float, hi: float) -> float:
    v = req.get(key)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        _fail(f"字段 {key} 必须是数值")
    v = float(v)
    if not (lo <= v <= hi):
        _fail(f"字段 {key}={v} 越界 [{lo}, {hi}]")
    return v


def _hash_events(rows: list[tuple]) -> str:
    h = hashlib.sha256()
    for row in rows:
        h.update(repr(row).encode("utf-8"))
    return h.hexdigest()


def main() -> int:
    raw = sys.stdin.read(MAX_SCENE_BYTES * 4)
    try:
        req = json.loads(raw)
    except json.JSONDecodeError as exc:
        _fail(f"请求不是 JSON：{exc}")
    if not isinstance(req, dict) or req.get("schema") != REQUEST_SCHEMA:
        _fail(f"schema 必须是 {REQUEST_SCHEMA}")
    kind = req.get("kind")
    if kind not in ALLOWED_KINDS:
        _fail(f"未知 kind={kind!r}（可选 {ALLOWED_KINDS}）")
    scene_xml = req.get("scene_xml")
    if not isinstance(scene_xml, str) or not scene_xml.strip():
        _fail("缺少 scene_xml 字符串")
    if len(scene_xml.encode("utf-8")) > MAX_SCENE_BYTES:
        _fail("scene_xml 超过 200 KB")
    steps = req.get("steps")
    if isinstance(steps, bool) or not isinstance(steps, int) or not (1 <= steps <= MAX_STEPS):
        _fail(f"steps 必须是 1..{MAX_STEPS} 的整数")

    import mujoco
    import numpy as np

    try:
        model = mujoco.MjModel.from_xml_string(scene_xml)
    except Exception as exc:  # noqa: BLE001
        _fail(f"场景加载失败：{type(exc).__name__}: {exc}")
    data = mujoco.MjData(model)

    run_id = "run_" + uuid.uuid4().hex
    events: list[tuple] = []
    series: list[list[float]] = []
    sample_every = max(1, steps // MAX_SERIES_POINTS)

    out: dict = {
        "schema": REQUEST_SCHEMA, "ok": True, "run_id": run_id, "kind": kind,
        "steps_requested": steps,
        "mujoco_version": mujoco.__version__,
        "nq": int(model.nq), "nv": int(model.nv),
    }

    if kind == "env_probe":
        # 镜像指纹：证明容器里真有一套可用的 mujoco/numpy/GL，而不是宿主代跑。
        out["python_version"] = sys.version.split()[0]
        out["numpy_version"] = np.__version__
        out["mujoco_gl_env"] = os.environ.get("MUJOCO_GL")
        # GL 字符串必须在**活着的上下文**里取：先建 Renderer 再查询，最后才 close。
        renderer = mujoco.Renderer(model, 8, 8)
        try:
            import OpenGL.GL as gl
            def _g(name):
                val = gl.glGetString(name)
                return val.decode() if isinstance(val, bytes) else str(val)
            out["gl_renderer"] = _g(gl.GL_RENDERER)
            out["gl_vendor"] = _g(gl.GL_VENDOR)
        except Exception as exc:  # noqa: BLE001
            out["gl_error"] = f"{type(exc).__name__}: {exc}"
        finally:
            renderer.close()
        print("RESULT_JSON " + json.dumps(out, ensure_ascii=False, sort_keys=True))
        return 0

    if kind == "render_probe":
        # 真 GPU/EGL 取证：渲染必须非全黑，且 GL 厂商由调用方按策略校验。
        try:
            renderer = mujoco.Renderer(model, 64, 64)
            mujoco.mj_forward(model, data)
            renderer.update_scene(data)
            img = renderer.render()
            import OpenGL.GL as gl
            def _glstr(name):
                val = gl.glGetString(name)
                return val.decode() if isinstance(val, bytes) else str(val)
            out["gl_renderer"] = _glstr(gl.GL_RENDERER)
            out["gl_vendor"] = _glstr(gl.GL_VENDOR)
            renderer.close()
            out["pixel_sum"] = int(img.sum())
            out["pixel_std"] = float(np.std(img))
            out["render_ok"] = bool(out["pixel_sum"] > 0 and out["pixel_std"] > 0)
        except Exception as exc:  # noqa: BLE001
            _fail(f"渲染失败：{type(exc).__name__}: {exc}")
        print("RESULT_JSON " + json.dumps(out, ensure_ascii=False, sort_keys=True))
        return 0

    # ---- 自由刚体箱体：位置闭环（候选只给三个数值增益） ----
    body_id = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_BODY, "box")
    hinge_id = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_JOINT, "hinge")
    box_geom = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_GEOM, "box")
    #: 推力施加在**箱底贴地**处而不是质心：下式给出等效的补偿力矩。
    #: 若直接推质心，10 N 级推力就会把立方箱掀翻（F_tip = m·g），翻倒后 a(F) 不再
    #: 线性，D 族的参数辨识会整段失效（2026-11-08 实测 A 拟合到 ≈0）。
    lever = float(model.geom_size[box_geom][2]) if box_geom >= 0 else 0.0

    if kind == "push_control":
        if body_id < 0:
            _fail("push_control 需要场景含名为 box 的自由刚体")
        tx = _num(req, "target_x", -50.0, 50.0)
        ty = _num(req, "target_y", -50.0, 50.0)
        kp = _num(req, "kp", 0.0, 5000.0)
        kd = _num(req, "kd", 0.0, 500.0)
        fmax = _num(req, "max_force", 0.0, MAX_FORCE)
        # 宿主侧注入的执行器增益（D 族 force_scale 故障载体）；候选请求里没有该项。
        force_scale = _num(req, "force_scale", 0.2, 10.0) if "force_scale" in req else 1.0
        for i in range(steps):
            mujoco.mj_forward(model, data)
            pos = data.xpos[body_id]
            # 只取该自由体在**世界系**的线速度（自由度索引由 joint 的 dofadr 给出）。
            qadr = int(model.body_dofadr[body_id])
            vel = data.qvel[qadr:qadr + 3]
            err = np.array([tx - pos[0], ty - pos[1], 0.0])
            cmd = kp * err - kd * vel
            norm = float(np.linalg.norm(cmd))
            if norm > fmax and norm > 0:
                cmd = cmd * (fmax / norm)
            # 执行器增益必须在**夹取之后**施加：故障语义是"你给 fmax，实际只出
            # s·fmax"。2026-11-08 实测：先乘后夹会被夹回 fmax，force_scale 变成
            # 完全无效的空操作（D-v2 拟合出的 A 与无故障逐位相同）。
            cmd = cmd * force_scale
            data.xfrc_applied[body_id, :3] = cmd
            # τ = r × F，r = (0,0,−lever) ⇒ τ = (lever·Fy, −lever·Fx, 0)
            data.xfrc_applied[body_id, 3:6] = (lever * cmd[1], -lever * cmd[0], 0.0)
            mujoco.mj_step(model, data)
            events.append((i, round(float(data.qpos[0]), 6), round(float(data.qpos[1]), 6),
                           round(float(vel[0]), 6), round(float(vel[1]), 6)))
            if i % sample_every == 0:
                series.append([int(i), float(data.qpos[0]), float(data.qpos[1]),
                               float(vel[0]), float(vel[1])])
        pos = data.xpos[body_id]
        qadr = int(model.body_dofadr[body_id])
        vel = data.qvel[qadr:qadr + 3]
        out.update({
            "final_pos": [float(pos[0]), float(pos[1])],
            "final_speed": float(np.linalg.norm(vel[:2])),
            "target": [tx, ty],
            "distance": float(np.hypot(pos[0] - tx, pos[1] - ty)),
            "series": series,          # [step, x, y, vx, vy]（下采样 ≤2000 点）
        })

    # ---- 自由运行：给定初始关节位形，记录时间序列（R 族实验） ----
    elif kind == "free_run":
        if hinge_id < 0:
            _fail("free_run 需要场景含名为 hinge 的关节")
        q0 = req.get("initial_angle")
        if isinstance(q0, bool) or not isinstance(q0, (int, float)):
            _fail("initial_angle 必须是数值")
        q0 = float(q0)
        if not (-3.2 <= q0 <= 3.2):
            _fail("initial_angle 越界 [-3.2, 3.2]")
        adr = int(model.jnt_qposadr[hinge_id])
        dadr = int(model.jnt_dofadr[hinge_id])
        data.qpos[adr] = q0
        mujoco.mj_forward(model, data)
        energy0 = float(data.energy[0] + data.energy[1])
        for i in range(steps):
            mujoco.mj_step(model, data)
            ang = float(data.qpos[adr])
            vel = float(data.qvel[dadr])
            events.append((i, round(ang, 6), round(vel, 6)))
            if i % sample_every == 0:
                series.append([int(i), ang, vel])
        energy1 = float(data.energy[0] + data.energy[1])
        out.update({
            "initial_angle": q0,
            "angle_series": series,
            "energy_initial": energy0,
            "energy_final": energy1,
            "final_angle": float(data.qpos[adr]),
            "final_angular_speed": float(data.qvel[dadr]),
        })

    out["steps_run"] = len(events)
    out["events_sha256"] = _hash_events(events)
    print("RESULT_JSON " + json.dumps(out, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
