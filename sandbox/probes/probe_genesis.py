#!/usr/bin/env python3
"""Task8 零付费 Genesis 安装/运行探针（容器内执行）。

只验证「装上能不能真跑」，不做 DR 实验（实验在 sandbox/track2 脚本）：
  1. import genesis + 版本/后端：必须真 CUDA 后端（A800），CPU 回退判失败；
  2. 低摩擦地面（μ=0.1）+ 内置 MJCF humanoid 建场，PD 关节目标驱动若干步；
  3. 接触 API：humanoid.get_contacts(with_entity=ground) 与底层
     rigid_solver.collider.get_contacts() 的键（必须含 penetration/force）；
  4. Rasterizer 离屏相机：cam.render() 返回元组，[0] 为 RGB；真帧自检
     （非纯色 std、前后帧有差异），非 numpy 假帧；
  5. 全程数值有限；结果写 JSON。

兼容 genesis-world 1.3.3（render 4 元组）/ 1.4.4（6 元组）：RGB 恒为 [0]。
用法：probe_genesis.py --out <probe.json> [--steps 150]
"""
from __future__ import annotations

import argparse
import json
import platform
import sys
import time
from pathlib import Path

import numpy as np


def _to_np(t):
    """锁内适配（Task8 容器实测 2026-10-15）：gs.gpu 后端下 get_dofs_position/
    get_pos/get_contacts 等状态读取返回 cuda:0 torch tensor，不能直接 np.asarray
    （TypeError: can't convert cuda:0 device type tensor to numpy）；统一先
    detach().cpu()。Rasterizer 渲染回读本身是 numpy ndarray（无 detach 属性），
    走 asarray 分支。"""
    if hasattr(t, "detach"):
        return t.detach().cpu().numpy()
    return np.asarray(t)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--steps", type=int, default=150)
    args = ap.parse_args()

    result: dict = {"py": platform.python_version(), "steps": args.steps}

    import genesis as gs  # noqa: E402

    result["genesis_version"] = gs.__version__

    # gs_madrona 加载取证（Task8 CUDA 库版本冲突 D 决策用）：
    # gs_madrona 0.0.10 全树唯一导入点是 vis/batch_renderer.py 的 try/except
    # （MadronaBatchRendererAdapter，可选）。Q1 走 Rasterizer，不应加载它。
    # 在「同一进程」内分别于 import 后/建场渲染后记录 sys.modules 实况。
    def _madrona_modules():
        return sorted(k for k in sys.modules if "madrona" in k.lower())

    result["madrona_modules_after_import_genesis"] = _madrona_modules()
    gs.init(backend=gs.gpu, seed=0)
    # 接触数据路径取证：use_zerocopy=True 时 1.3.3 在「非对齐静态配置」下用
    # contact_sort_idx（Quadrants int32 零拷贝视图）做 torch.gather 索引，
    # torch 2.4 要求 int64 → RuntimeError。官方环境开关 GS_ENABLE_ZEROCOPY=0
    # （__init__.py:141）回传统 collider_kernel_get_contacts 路径。
    result["use_zerocopy"] = bool(gs.use_zerocopy)
    backend_name = str(getattr(gs, "backend", "?"))
    result["backend"] = backend_name
    if "cuda" not in backend_name.lower():
        raise RuntimeError(
            f"Genesis 未取得 CUDA 后端（{backend_name}）：A800 沙盒不允许 CPU 回退算通过")

    asset_xml = Path(gs.__file__).resolve().parent / "assets" / "xml" / "humanoid.xml"
    if not asset_xml.is_file():
        raise RuntimeError(f"内置 humanoid.xml 缺失：{asset_xml}")
    result["humanoid_xml"] = str(asset_xml)

    scene = gs.Scene(
        sim_options=gs.options.SimOptions(dt=0.01, substeps=2),
        show_viewer=False,
        renderer=gs.options.renderers.Rasterizer(),
    )
    # 低摩擦地面（油污极值之一）：地面是 Task8 DR 的摩擦杠杆
    ground = scene.add_entity(
        gs.morphs.Plane(),
        material=gs.materials.Rigid(friction=0.1),
    )
    humanoid = scene.add_entity(
        gs.morphs.MJCF(file=str(asset_xml)),
    )
    cam = scene.add_camera(
        model="pinhole", res=(320, 240),
        pos=(2.6, 0.0, 1.3), lookat=(0.0, 0.0, 1.0), fov=42, GUI=False,
    )
    scene.build()

    # 锁内适配（Task8 容器实测 2026-10-15）：genesis-world 1.3.3 的 RigidEntity
    # 没有 get_links()（1.4.x 文档写法），1.3.3 用 .links property（源码
    # rigid_entity.py:2473 -> list[RigidLink]，RigidLink.name 见 rigid_link.py:416）。
    link_names = [lc.name for lc in humanoid.links]
    result["n_links"] = len(link_names)
    result["has_torso_link"] = any("torso" in n for n in link_names)
    result["has_foot_links"] = [n for n in link_names if "foot" in n]
    n_dofs = humanoid.n_dofs
    result["n_dofs"] = int(n_dofs)
    if n_dofs <= 0 or not result["has_torso_link"] or len(result["has_foot_links"]) < 2:
        raise RuntimeError(f"humanoid 结构异常：{link_names}")
    torso = humanoid.get_link(
        next(n for n in link_names if n.startswith("torso")))

    # 中等 PD 增益保持初始站姿（位置目标=初始关节角）
    kp = np.full(n_dofs, 200.0, dtype=np.float64)
    kv = np.full(n_dofs, 20.0, dtype=np.float64)
    humanoid.set_dofs_kp(kp)
    humanoid.set_dofs_kv(kv)
    q0 = np.asarray(_to_np(humanoid.get_dofs_position()),
                    dtype=np.float64).reshape(-1)
    if q0.shape[0] != n_dofs:
        raise RuntimeError(f"初始 dof 形状异常：{q0.shape} vs n_dofs={n_dofs}")

    # 探针运动激励：PD 保持静立时画面近乎不变（i=1 vs i=75 mean|Δ|=0.38，
    # 过不了自己的反静态假渲染阈值 1.0）。阈值不放宽——改为命令肩肘关节
    # 做持续正弦摆动，让「真物理→真渲染」在帧间产生可见差异。dof 索引经
    # joint API 解析，不按 motor 序号硬编码（root freejoint 占 7 个位置维）。
    arm_names = ["shoulder1_right", "shoulder2_right", "elbow_right",
                 "shoulder1_left", "shoulder2_left", "elbow_left"]
    arm_idx = []
    for jn in arm_names:
        j = humanoid.get_joint(jn)
        jidx = np.asarray(_to_np(j.dofs_idx_local), dtype=np.int64).reshape(-1)
        if jidx.size != 1:
            raise RuntimeError(f"关节 {jn} dof 索引异常：{jidx}")
        arm_idx.append(int(jidx[0]))
    result["arm_dof_idx_local"] = arm_idx
    if len(arm_idx) != 6:
        raise RuntimeError(f"肩肘关节解析不完整：{arm_idx}")

    pelvis_z = []
    contact_n = []
    raw_keys_seen: set[str] = set()
    pen_seen = False
    rgb_first = rgb_mid = None
    t0 = time.time()
    arm_phase = np.linspace(0.0, np.pi, len(arm_idx), endpoint=False)
    for i in range(args.steps):
        target = q0.copy()
        target[arm_idx] = q0[arm_idx] + 0.35 * np.sin(0.08 * i + arm_phase)
        humanoid.control_dofs_position(target)
        scene.step()
        pelvis_z.append(float(_to_np(torso.get_pos()).reshape(-1)[:3][2]))
        ci = humanoid.get_contacts(with_entity=ground)
        contact_n.append(int(_to_np(ci["force_a"]).reshape(-1).shape[0] // 3)
                         if "force_a" in ci else 0)
        raw = humanoid._solver.collider.get_contacts(as_tensor=True, to_torch=True)
        raw_keys_seen.update(raw.keys())
        if "penetration" in raw and _to_np(raw["penetration"]).size:
            pen_seen = True
        if i == 1 or i == args.steps // 2:
            out = cam.render(rgb=True)
            rgb = _to_np(out[0])
            if rgb.ndim == 4:  # env 维
                rgb = rgb[0]
            if i == 1:
                rgb_first = rgb
            else:
                rgb_mid = rgb
    result["sim_wall_s"] = round(time.time() - t0, 3)
    result["raw_contact_keys"] = sorted(raw_keys_seen)
    result["penetration_field_seen"] = pen_seen
    result["contact_frames"] = int(sum(1 for n in contact_n if n > 0))
    result["pelvis_z_first"] = round(pelvis_z[0], 5)
    result["pelvis_z_last"] = round(pelvis_z[-1], 5)

    # ---- 硬自检（任何不满足直接非零退出，不写「成功」） ----
    if not all(np.isfinite(z) for z in pelvis_z):
        raise RuntimeError("pelvis z 出现 NaN/Inf")
    need = {"geom_a", "geom_b", "position", "force_a", "force_b"}
    # 实体级接触 dict 键以首次实际接触帧的 raw 键为准（raw 全量含 force/normal）
    if not need <= raw_keys_seen and not {"force", "position"} <= raw_keys_seen:
        raise RuntimeError(f"raw contact 键不足：{sorted(raw_keys_seen)}")
    if not pen_seen:
        raise RuntimeError("collider 原始接触中未见 penetration 字段（telemetry 穿模源缺失）")
    for tag, rgb in (("first", rgb_first), ("mid", rgb_mid)):
        if rgb is None or rgb.ndim != 3 or rgb.shape[2] != 3:
            raise RuntimeError(f"RGB 帧形状异常（{tag}）：{None if rgb is None else rgb.shape}")
        if float(rgb.std()) < 5.0:
            raise RuntimeError(f"RGB 帧近似纯色（std={float(rgb.std()):.2f}），拒绝假帧")
    diff = float(np.abs(
        rgb_mid.astype(np.float32) - rgb_first.astype(np.float32)).mean())
    result["rgb_mean_abs_diff"] = round(diff, 4)
    if diff < 1.0:
        raise RuntimeError(f"前后帧无可见差异（mean|Δ|={diff:.3f}），拒绝静态假渲染")

    # 建场+步进+Rasterizer 渲染之后，gs_madrona 仍不得被加载
    result["madrona_modules_after_probe"] = _madrona_modules()
    result["gs_madrona_loaded_after_probe"] = any(
        k == "gs_madrona" or k.startswith("gs_madrona.")
        for k in result["madrona_modules_after_probe"])
    result["batch_renderer_imported"] = (
        "genesis.vis.batch_renderer" in sys.modules)

    result["ok"] = True
    Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    print("GENESIS_PROBE_OK", json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
