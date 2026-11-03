#!/usr/bin/env python3
"""Task 1 Step 0：容器内「无 GPU 能做什么」三段可行性探针（0 付费、`--network none`、不带 `--gpus`）。

为什么要分段：空场景能建**不等于**可用——Task 1 步骤 5 要加真实网格 + 凸分解 +
一步物理，Task 3 要离屏渲染。三段各测各的负载、一段一行、互不掩盖。

用法（宿主侧；镜像与解释器见 orchestrator/agentic/image_pin.py 的 L-13 登记）：

    docker run --rm -i --network none \
      -v /mnt/assets/simpraxis/zoo:/assets:ro \
      sim-praxis/sandbox:phase1-task8@sha256:78be93b9… \
      /usr/bin/python - < sandbox/probes/probe_cpu_backend.py \
      > probe_cpu_backend_check.log 2> probe_cpu_backend_check.stderr.log

输出契约：**stdout 恰好三行 JSON**，每行一个 stage（`S1_engine_empty_scene` /
`S2_mesh_entity_step` / `S3_offscreen_render`），失败段 `ok=false` 且带 `err`。

为了让 stdout 只有这三行，脚本在进入 Genesis 之前把 fd 1 指向 fd 2：引擎
（含 C++ 层）的横幅、警告、进度全部落 stderr，JSON 行直接写回保存下来的
原 stdout fd。这样「日志三行」是**机制保证**，不是事后过滤。

预注册的分叉规则（计划 T1.5 Step 0，执行期不得改）：
  S1 红 → Task 1 阻塞，STOP 上报（禁止自行改调用点为 "all"）；
  S2 红 → Task 1 阻塞，STOP 上报（同上）；
  S3 红 → Task 3 阻塞，按 RV-12 的五项后果 + 条件式建议呈报用户；
  S1/S2 绿而 S3 红是**允许的中间态**：Task 1 继续，但必须如实登记进 probe_report.json。
"""
from __future__ import annotations

import json
import os
import sys

_STDOUT_FD = os.dup(1)


def _line(payload: "dict") -> None:
    os.write(_STDOUT_FD, (json.dumps(payload, ensure_ascii=False) + "\n").encode())


def _mute_engine_output() -> None:
    """引擎的一切输出转向 stderr，stdout 只留 JSON 行。"""
    os.dup2(2, 1)


def _probe_environment() -> "dict":
    info: "dict" = {"python": sys.version.split()[0], "image_python": sys.executable,
                    "gpu_env_visible": False, "cuda_visible_devices": None}
    info["cuda_visible_devices"] = os.environ.get("CUDA_VISIBLE_DEVICES")
    try:
        import torch  # 镜像内已装；仅用于报告可见性，不初始化 CUDA

        info["torch"] = torch.__version__
        info["torch_cuda_available"] = bool(torch.cuda.is_available())
        info["gpu_env_visible"] = bool(torch.cuda.device_count() > 0)
    except Exception as exc:  # noqa: BLE001 - 探针只报告，不改变判定
        info["torch_error"] = f"{type(exc).__name__}: {exc}"
    return info


def _segment(name: str, body, env: "dict") -> None:
    """跑一段；异常只记录不抛出（一段红不掩盖其他段）。"""
    import genesis as gs

    try:
        result = body(gs, env)
        payload = {"stage": name, "ok": True}
        if result:
            payload.update(result)
        _line(payload)
    except Exception as exc:  # noqa: BLE001 - 探针要的是「红/绿」而非栈
        _line({"stage": name, "ok": False,
               "err": f"{type(exc).__name__}: {exc}"})
    finally:
        try:
            gs.destroy()
        except Exception:  # noqa: BLE001 - 拆不掉不影响本段判定
            pass


def _s1_engine_empty_scene(gs, env: "dict") -> "dict":
    gs.init(backend=gs.cpu)
    scene = gs.Scene()
    scene.build()
    return {"version": gs.__version__, "backend": "cpu", **env}


def _s2_mesh_entity_step(gs, env: "dict") -> "dict":
    gs.init(backend=gs.cpu)
    scene = gs.Scene()
    # convexify 必须**显式传**并**读回**：1.3.3 的 gs.morphs.Mesh 缺省是 None
    # （建场时由引擎自决），照字面写 `convexify: True` 是把未发生的事写成事实。
    morph = gs.morphs.Mesh(file="/assets/vessel/pitcher_standard.obj",
                           convexify=True)
    scene.add_entity(morph)
    scene.build()
    scene.step()
    # AC-T1-2 要用这两个字段判「探针回执与 Step 0 判定一致」：CPU 路径下
    # gpu_visible=false 且 gpu_mib=null（无 --gpus ⇒ 容器内看不到设备）。
    return {"mesh": "/assets/vessel/pitcher_standard.obj",
            "convexify": bool(morph.convexify), "steps": 1, "backend": "cpu",
            "gpu_visible": bool(env.get("gpu_env_visible", False)),
            "gpu_mib": None if not env.get("gpu_env_visible") else 0}


def _s3_offscreen_render(gs, env: "dict") -> "dict":
    import time

    import numpy as np

    gs.init(backend=gs.cpu)
    scene = gs.Scene()
    cam = scene.add_camera(res=(256, 256))
    scene.build()
    started = time.monotonic()
    out = cam.render()
    elapsed = round(time.monotonic() - started, 3)
    # Genesis 1.3.3 的 Camera.render 返回 **tuple**（rgb 在首位，其余按需）。
    # 这里必须真的取到图并验形状：`getattr(x, "shape", ())` 对 tuple 恒为 ()
    # ——那会把「没渲染出图」读成绿（假绿，硬规则 7 禁区）。
    rgb = out[0] if isinstance(out, tuple) else out
    shape = list(np.shape(rgb))
    if shape != [256, 256, 3]:
        raise RuntimeError(f"render 返回形状 {shape} != [256, 256, 3]（未真正渲染出图）")
    return {"shape": shape, "dtype": str(np.asarray(rgb).dtype),
            "unique_colors": int(len(np.unique(np.asarray(rgb).reshape(-1, 3),
                                               axis=0))),
            "render_seconds": elapsed}


def main() -> int:
    env = _probe_environment()
    _mute_engine_output()
    for name, body in (("S1_engine_empty_scene", _s1_engine_empty_scene),
                       ("S2_mesh_entity_step", _s2_mesh_entity_step),
                       ("S3_offscreen_render", _s3_offscreen_render)):
        _segment(name, body, env)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
