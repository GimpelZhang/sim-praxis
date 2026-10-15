#!/usr/bin/env python3
"""Task7 安装探针（1/2）：MuJoCo 原生 scene 无渲染步进 + EGL 离屏渲染复核。

计划 L542-544：先装 MuJoCo（pip，轻）跑一个原生 scene 的无渲染步进；
版本探针后钉死回填 requirements-container.txt + lock。

严格两阶段（分别由 --phase 选择，确保 MUJOCO_GL 只影响渲染阶段）：
  step     : 最小 MJCF（平面 + 自由落体球），纯物理步进，断言下落、接触、
             数值 finite；不初始化任何 GL 上下文。
  egl      : MUJOCO_GL=egl 下用 mujoco.Renderer 离屏渲染一帧，断言真实出片
             （像素方差 > 0；是否真 GPU 由 verify_egl_real.py 同容器复核，
             本脚本不替它下结论）。

产物：--out 指定 JSON 探针结果（容器内只写挂载卷）。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time


def _model():
    import mujoco

    # 原生 scene：平面 + 半径 5cm 小球自 1m 高自由落体；timestep 用默认 2ms。
    xml = """
<mujoco model="simpraxis_t7_probe">
  <option timestep="0.002"/>
  <worldbody>
    <light pos="0 0 3"/>
    <geom name="floor" type="plane" size="5 5 0.1"/>
    <body name="ball" pos="0 0 1">
      <freejoint/>
      <geom type="sphere" size="0.05" mass="0.1"/>
    </body>
  </worldbody>
</mujoco>
"""
    model = mujoco.MjModel.from_xml_string(xml)
    data = mujoco.MjData(model)
    return mujoco, model, data


def phase_step(out_path: str) -> None:
    mujoco, model, data = _model()
    t0 = time.time()
    z_trace, ncon_trace, force_trace = [], [], []
    contact_first_step = None
    n_steps = 1000  # 2s 物理
    for i in range(n_steps):
        mujoco.mj_step(model, data)
        z = float(data.qpos[2])
        ncon = int(data.ncon)
        # 接触法向力：cfrc_ext 在 mj_step 路径不被更新（3.15 实测恒零），
        # 直接读约束求解器结果 efc_force[contact.efc_address] 求和。
        force = 0.0
        for ci in range(data.ncon):
            force += float(data.efc_force[data.contact[ci].efc_address])
        if not math.isfinite(z) or not math.isfinite(force):
            raise AssertionError(f"step {i}: 非有限数值 z={z} force={force}")
        if i % 25 == 0:
            z_trace.append(round(z, 6))
            ncon_trace.append(ncon)
            force_trace.append(round(force, 6))
        if ncon > 0 and contact_first_step is None:
            contact_first_step = i
    elapsed = time.time() - t0

    z_start, z_end = z_trace[0], z_trace[-1]
    # 物理断言（真实仿真，不允许 numpy 假数据）：
    # ① 1m 高自由落体 2s 内必须发生球-平面接触；
    assert contact_first_step is not None, "1000 步内无接触（物理异常）"
    # ② 球必须真实下落（首末高度差 > 0.5m）；
    assert z_start - z_end > 0.5, f"下落不足：{z_start} -> {z_end}"
    # ③ 接触后应出现非零接触力样本。
    assert max(force_trace) > 0.0, "全程零接触力"

    result = {
        "phase": "step",
        "mujoco_version": mujoco.__version__,
        "python": sys.version.split()[0],
        "timestep_s": float(model.opt.timestep),
        "n_steps": n_steps,
        "contact_first_step": contact_first_step,
        "z_start": z_start,
        "z_end": z_end,
        "z_trace_sample": z_trace,
        "ncon_trace_sample": ncon_trace,
        "force_trace_sample": force_trace,
        "max_contact_force": max(force_trace),
        "wall_s": round(elapsed, 4),
        "status": "ok",
    }
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)
    print(json.dumps({"phase": "step", "status": "ok",
                      "mujoco_version": mujoco.__version__,
                      "contact_first_step": contact_first_step},
                     ensure_ascii=False))


def phase_egl(out_path: str) -> None:
    os.environ["MUJOCO_GL"] = "egl"          # 必须在 import mujoco 前设定
    os.environ.setdefault("PYOPENGL_PLATFORM", "egl")
    mujoco, model, data = _model()
    # 步进若干步让球离开初始位置再出片
    for _ in range(120):
        mujoco.mj_step(model, data)
    mujoco.mj_forward(model, data)

    import numpy as np

    renderer = mujoco.Renderer(model, height=240, width=320)
    renderer.update_scene(data)
    img = renderer.render()
    assert img.shape == (240, 320, 3), img.shape
    assert float(np.isfinite(img).all())
    fimg = img.astype("float32")
    var = float(fimg.var())
    mean = float(img.mean())
    # 真实出片断言：像素必须有结构——整体非纯色 + 存在空间边缘（相邻像素差异）。
    # 不要求三通道不等（默认材质场景合法地接近灰度）；真 GPU 结论由
    # verify_egl_real.py 在同一容器内另跑（其退出码语义不在这里替代）。
    assert var > 10.0, f"渲染帧疑似纯色：var={var}"
    dx = float(np.abs(np.diff(fimg, axis=1)).max())
    dy = float(np.abs(np.diff(fimg, axis=0)).max())
    assert dx > 5.0 and dy > 5.0, f"帧无空间结构：dx_max={dx} dy_max={dy}"
    uniq = int(np.unique(img.reshape(-1, 3), axis=0).shape[0])
    assert uniq > 100, f"帧颜色种类过少：{uniq}"
    result = {
        "phase": "egl",
        "mujoco_version": mujoco.__version__,
        "MUJOCO_GL": os.environ.get("MUJOCO_GL"),
        "frame_shape": list(img.shape),
        "pixel_var": round(var, 4),
        "pixel_mean": round(mean, 4),
        "edge_max_dx": round(dx, 3),
        "edge_max_dy": round(dy, 3),
        "unique_colors": uniq,
        "status": "ok",
    }
    # mujoco 3.15 EGL 上下文在 GC 析构期会打 EGL_NOT_INITIALIZED 的 ignored 异常；
    # 显式 close 并吞掉该已知收尾噪音（帧已在此前真实取回）。
    try:
        renderer.close()
    except Exception as exc:  # noqa: BLE001 - 仅收尾，不影响出片事实
        result["close_warning"] = str(exc)[:200]
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)
    print(json.dumps({"phase": "egl", "status": "ok", "pixel_var": round(var, 2)},
                     ensure_ascii=False))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", required=True, choices=("step", "egl"))
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    if args.phase == "step":
        phase_step(args.out)
    else:
        phase_egl(args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
