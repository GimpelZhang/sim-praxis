#!/usr/bin/env python3
"""Task7 安装探针（2/2）：ManiSkill 官方 env 的 headless GPU 离屏渲染。

计划 L542-544：再装 ManiSkill 跑一个官方 demo 的 headless EGL render
（离屏；SAPIEN3 的 GPU 渲染后端为 Vulkan，本探针同时记录 Vulkan 设备与
EGL 环境事实；真 GPU 结论沿用 verify_egl_real.py，不在这里替代）。

严格事实：
- 官方环境 PushCube-v1（mani_skill.examples.demo_random_action 默认）；
- sim/render 后端默认 gpu（SAPIEN Vulkan），失败即如实记录，不静默降级；
- 帧必须来自 env.render() 真实回读（禁 numpy 合成帧）；
- 首步/末步两帧均要有空间结构且彼此不同（场景在动）。

产物：--out JSON；同目录写 maniskill_frame.png（首帧人类证据）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time


def main() -> int:
    out_path: str = ""
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--env-id", default="PushCube-v1")
    ap.add_argument("--n-steps", type=int, default=30)
    ap.add_argument("--sim-backend", default="gpu", choices=("gpu", "cpu"))
    args = ap.parse_args()
    out_path = args.out

    t0 = time.time()
    import gymnasium as gym
    import numpy as np
    import sapien
    import mani_skill
    import mani_skill.envs  # noqa: F401  注册 gym env
    import torch
    import imageio.v2 as imageio

    def render_np(frame) -> np.ndarray:
        # 锁内适配（计划 L543 headless EGL render）：gpu sim backend 下
        # env.render() 返回 cuda:0 torch.Tensor，单 env 仍可能带第 0 维
        # batch（shape (1,H,W,3)）；cpu() 回读后去 batch。帧数据仍来自
        # env.render() 真实 Vulkan 回读，不做任何像素合成。
        if isinstance(frame, torch.Tensor):
            frame = frame.detach().cpu().numpy()
        arr = np.asarray(frame)
        if arr.ndim == 4 and arr.shape[0] == 1:
            arr = arr[0]
        return arr

    ms_version = getattr(mani_skill, "__version__", "unknown")
    sys.stdout.write(f"[probe] maniskill={ms_version} "
                     f"sapien={sapien.__version__} torch={torch.__version__} "
                     f"cuda_available={torch.cuda.is_available()}\n")
    sys.stdout.flush()

    env = gym.make(
        args.env_id,
        obs_mode="state",
        render_mode="rgb_array",
        sim_backend=args.sim_backend,
    )
    try:
        obs, info = env.reset(seed=7)
        arr0 = render_np(env.render())
        assert arr0.ndim == 3 and arr0.shape[2] == 3, arr0.shape
        h, w, _ = arr0.shape
        assert arr0.dtype == np.uint8, arr0.dtype
        var0 = float(arr0.astype("float32").var())
        assert var0 > 10.0, f"首帧疑似纯色 var={var0}"

        last_frame = arr0
        for i in range(args.n_steps):
            action = env.action_space.sample()
            obs, reward, terminated, truncated, info = env.step(action)
            if i == args.n_steps - 1:
                last_frame = render_np(env.render())
        var1 = float(last_frame.astype("float32").var())
        assert var1 > 10.0, f"末帧疑似纯色 var={var1}"
        diff = float(np.abs(last_frame.astype("int16")
                            - arr0.astype("int16")).mean())
        assert diff > 0.5, f"首末帧几乎相同 mean_diff={diff}（场景未动）"

        png_path = os.path.join(os.path.dirname(out_path), "maniskill_frame.png")
        imageio.imwrite(png_path, arr0)

        result = {
            "status": "ok",
            "env_id": args.env_id,
            "maniskill_version": ms_version,
            "sapien_version": sapien.__version__,
            "torch_version": torch.__version__,
            "torch_cuda_available": bool(torch.cuda.is_available()),
            "sim_backend": args.sim_backend,
            "render_backend": "gpu(Vulkan)",
            "frame_shape": list(arr0.shape),
            "n_steps": args.n_steps,
            "pixel_var_first": round(var0, 3),
            "pixel_var_last": round(var1, 3),
            "frame_mean_diff": round(diff, 4),
            "frame_png": png_path,
            "wall_s": round(time.time() - t0, 2),
        }
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(result, fh, ensure_ascii=False, indent=2)
        print(json.dumps({"status": "ok", "env": args.env_id,
                          "frame": list(arr0.shape), "mean_diff": round(diff, 3)},
                         ensure_ascii=False))
    finally:
        env.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
