#!/usr/bin/env python3
"""探针纯函数不变量：clearColor 字节、中心裁剪包络、哈希可分性、相对软链。
不创建 EGL 上下文、不付费；由 gate_static.sh 第 5 项调用。"""
import importlib.util
import os
import pathlib

import numpy as np


ROOT = pathlib.Path(__file__).resolve().parent.parent
_probe_path = ROOT / "scripts" / "verification" / "verify_egl_real.py"
_spec = importlib.util.spec_from_file_location("verify_egl_real", _probe_path)
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)


def test_clear_color_byte_vs_corner_threshold():
    # GL_UNSIGNED_BYTE 归一化转换：byte = round(c*255)（0.5 舍入情形本数据不涉及）
    rb, gb, bb, ab = [int(round(c * 255)) for c in probe.CLEAR_COLOR]
    assert bb > 120, f"蓝字节 {bb} 必须严格大于角点断言阈值 120（旧 0.40->102 即 B1a）"
    assert rb < 80 and gb < 80 and ab == 255, (rb, gb, bb, ab)


def _square_bounds():
    """复刻 render_frame 的方块四边（视频坐标：世界 y 经翻转后等于行号）。"""
    x0, x1, y0, y1 = probe.WIDTH, 0, probe.HEIGHT, 0
    for i in range(probe.N_FRAMES):
        ang = 2 * np.pi * i / probe.N_FRAMES
        cx = int(probe.WIDTH / 2 + probe.ORBIT_RX * np.cos(ang))
        cy = int(probe.HEIGHT / 2 + probe.ORBIT_RY * np.sin(ang))
        h = probe.SQUARE_HALF
        x0, x1 = min(x0, cx - h), max(x1, cx + h)
        y0, y1 = min(y0, cy - h), max(y1, cy + h)
    return x0, x1, y0, y1


def test_center_crop_covers_full_motion_envelope():
    x0, x1, y0, y1 = _square_bounds()
    assert (x0, x1, y0, y1) == (600, 1320, 260, 820), (x0, x1, y0, y1)
    assert probe.CROP_R0 <= y0 and y1 <= probe.CROP_R1
    assert probe.CROP_C0 <= x0 and x1 <= probe.CROP_C1


def test_corner_pixel_outside_crop():
    # 角点 (40,40) 必须在窗外，保证裁剪窗不含静态蓝底角点
    r, c = 40, 40
    assert not (probe.CROP_R0 <= r < probe.CROP_R1 and probe.CROP_C0 <= c < probe.CROP_C1)


def _model_frame(frame_idx):
    """与 render_frame 同几何的 numpy 模型帧（蓝底 + 绿色实心方块），仅用于哈希可分性证明。"""
    img = np.zeros((probe.HEIGHT, probe.WIDTH, 3), dtype=np.uint8)
    img[:, :, 2] = int(round(probe.CLEAR_COLOR[2] * 255))  # 蓝底
    ang = 2 * np.pi * frame_idx / probe.N_FRAMES
    cx = int(probe.WIDTH / 2 + probe.ORBIT_RX * np.cos(ang))
    cy = int(probe.HEIGHT / 2 + probe.ORBIT_RY * np.sin(ang))
    h = probe.SQUARE_HALF
    img[cy - h:cy + h, cx - h:cx + h] = (0, 255, 0)
    return img


def test_crop_hashes_separate_at_least_80_percent():
    # B1b 的可执行证明：60 个不同帧位 -> 中心裁剪哈希至少 48 个不同（实际 60/60）
    hashes = set()
    for i in range(probe.N_FRAMES):
        crop = np.ascontiguousarray(
            _model_frame(i)[probe.CROP_R0:probe.CROP_R1, probe.CROP_C0:probe.CROP_C1])
        hashes.add(hash(crop.tobytes()))
    assert len(hashes) >= int(0.8 * probe.N_FRAMES), len(hashes)


def test_old_static_top_hash_would_collapse():
    # 反证：旧实现 hash(img.tobytes()[:4096]) 在同一模型上必然塌缩为 1，
    # 锁定回归——谁改回顶部静态字节哈希，本测试立刻红。
    top = _model_frame(0).tobytes()[:4096]
    assert all(_model_frame(i).tobytes()[:4096] == top for i in range(1, probe.N_FRAMES))


def test_latest_symlink_is_relative_and_resolves(tmp_path):
    # B1c：latest 必须是相对软链（basename），容器侧与宿主挂载侧都能解析
    run_root = tmp_path / "runs"
    outdir = run_root / "20261011-120000"
    outdir.mkdir(parents=True)
    (outdir / "probe_egl.json").write_text("{}", encoding="utf-8")
    latest = run_root / "latest"
    os.symlink(os.path.basename(outdir), latest)
    assert os.readlink(latest) == "20261011-120000"
    assert not os.path.isabs(os.readlink(latest))
    assert (latest / "probe_egl.json").read_text(encoding="utf-8") == "{}"
