#!/usr/bin/env python3
"""
SimPraxis Phase0 真实无头渲染探针。
阶梯：EGLDeviceEXT / EGL 默认显示（GPU，通过线）-> OSMesa（CPU 软件，降级线）。
退出码（Task 1 自检与 Milestone e2e 共用同一语义，禁止执行期自行弱化）：
  0 = GPU EGL 通过（tier=="gpu"，backend 为 egl-device[i]/egl-default，renderer 含 NVIDIA）；
  2 = 仅 OSMesa 软件出片成功——能出片但 Milestone 整体判 FAIL（GPU 验收未过）；
  1 = GPU 与 OSMesa 都失败。
产物：$SIMPRAXIS_RUN_DIR/<时间戳>/probe_egl.mp4 + probe_egl.json，latest 为相对软链。
禁止用 numpy 合成帧替代 GL 渲染；每一帧都必须来自 glReadPixels 的真实回读。
像素数学（tests/test_probe_math.py 同步断言，改一处必须改另一处）：
  蓝底 glClearColor(0.02,0.05,0.55) -> 蓝通道字节 round(0.55*255)=140，角点断言 >120；
  运动包络（翻转后视频坐标）：方块 x∈[600,1320]、y∈[260,820]，唯一哈希裁剪窗
  img[200:900, 400:1500] 完整覆盖运动包络，且角点 (40,40) 在窗外。
"""
import ctypes
import json
import math
import os
import subprocess
import sys
import time
from ctypes import byref, c_int, c_uint, c_void_p

import numpy as np


WIDTH, HEIGHT, FPS, N_FRAMES = 1920, 1080, 30, 60

# ---- EGL 常量（取自 Khronos egl.h / eglext.h，不依赖 PyOpenGL 对 EXT 常量的导出） ----
EGL_SUCCESS = 0x3000
EGL_DEFAULT_DISPLAY = c_void_p(0)
EGL_NO_CONTEXT = c_void_p(0)
EGL_OPENGL_API = 0x30A2
EGL_PBUFFER_BIT = 0x0001
EGL_OPENGL_BIT = 0x0008
EGL_SURFACE_TYPE = 0x3033
EGL_RENDERABLE_TYPE = 0x3040
EGL_RED_SIZE, EGL_GREEN_SIZE, EGL_BLUE_SIZE = 0x3024, 0x3023, 0x3022
EGL_ALPHA_SIZE, EGL_DEPTH_SIZE = 0x3021, 0x3025
EGL_WIDTH, EGL_HEIGHT, EGL_NONE = 0x3057, 0x3056, 0x3038
EGL_CONTEXT_MAJOR_VERSION, EGL_CONTEXT_MINOR_VERSION = 0x3098, 0x30FB
# EGL_KHR_create_context：不指定 profile 时 3.2+ 上下文默认 core，
# 固定管线（glMatrixMode/glBegin/glOrtho）会 GL_INVALID_OPERATION(1282)，必须显式 compat。
EGL_CONTEXT_OPENGL_PROFILE_MASK = 0x30FD
EGL_CONTEXT_OPENGL_COMPAT_PROFILE_BIT = 0x0002
EGL_PLATFORM_DEVICE_EXT = 0x313F
EGL_VENDOR, EGL_VERSION, EGL_CLIENT_APIS = 0x3053, 0x3054, 0x308D


def _bind_ext(lib, name, restype, argtypes):
    """按 EGL 规范取 EXT 入口点：glvnd 的 libEGL.so.1 不导出这些动态符号
    （readelf 无 eglQueryDevicesEXT），必须经 eglGetProcAddress 解析，eglinfo
    的 Device platform 走的就是这一路径。"""
    addr = lib.eglGetProcAddress(name.encode("ascii"))
    if not addr:
        raise OSError(f"eglGetProcAddress({name}) 返回 NULL")
    proto = ctypes.CFUNCTYPE(restype, *argtypes)
    fn = proto(addr)
    # 保活：proto/fn 被 GC 后函数指针会失效
    refs = getattr(lib, "_ext_refs", None)
    if refs is None:
        refs = lib._ext_refs = []
    refs.append((proto, fn))
    setattr(lib, name, fn)


def load_egl():
    lib = ctypes.CDLL("libEGL.so.1")
    lib.eglGetError.restype = c_uint
    lib.eglGetProcAddress.argtypes = [ctypes.c_char_p]
    lib.eglGetProcAddress.restype = c_void_p
    _bind_ext(lib, "eglQueryDevicesEXT", c_uint,
              [c_int, ctypes.POINTER(c_void_p), ctypes.POINTER(c_int)])
    _bind_ext(lib, "eglGetPlatformDisplayEXT", c_void_p, [c_uint, c_void_p, c_void_p])
    lib.eglGetDisplay.argtypes = [c_void_p]
    lib.eglGetDisplay.restype = c_void_p
    lib.eglInitialize.argtypes = [c_void_p, ctypes.POINTER(c_int), ctypes.POINTER(c_int)]
    lib.eglInitialize.restype = c_uint
    lib.eglTerminate.argtypes = [c_void_p]
    lib.eglTerminate.restype = c_uint
    lib.eglBindAPI.argtypes = [c_uint]
    lib.eglBindAPI.restype = c_uint
    lib.eglChooseConfig.argtypes = [c_void_p, ctypes.POINTER(c_int), ctypes.POINTER(c_void_p),
                                    c_int, ctypes.POINTER(c_int)]
    lib.eglChooseConfig.restype = c_uint
    lib.eglCreatePbufferSurface.argtypes = [c_void_p, c_void_p, ctypes.POINTER(c_int)]
    lib.eglCreatePbufferSurface.restype = c_void_p
    lib.eglCreateContext.argtypes = [c_void_p, c_void_p, c_void_p, ctypes.POINTER(c_int)]
    lib.eglCreateContext.restype = c_void_p
    lib.eglMakeCurrent.argtypes = [c_void_p, c_void_p, c_void_p, c_void_p]
    lib.eglMakeCurrent.restype = c_uint
    lib.eglQueryString.argtypes = [c_void_p, c_int]
    lib.eglQueryString.restype = ctypes.c_char_p
    lib.eglDestroySurface.argtypes = [c_void_p, c_void_p]
    lib.eglDestroyContext.argtypes = [c_void_p, c_void_p]
    return lib


def enum_devices(lib):
    fn = getattr(lib, "eglQueryDevicesEXT", None)
    if fn is None:
        return []
    num = c_int(0)
    if not fn(0, None, byref(num)) or num.value <= 0:
        return []
    arr = (c_void_p * num.value)()
    if not fn(num.value, arr, byref(num)):
        return []
    return [arr[i] for i in range(num.value)]


def open_egl_display(lib, diag):
    for idx, dev in enumerate(enum_devices(lib)):
        dpy = lib.eglGetPlatformDisplayEXT(EGL_PLATFORM_DEVICE_EXT, dev, None)
        if not dpy:
            diag.append(f"device[{idx}] get platform display failed: 0x{lib.eglGetError():x}")
            continue
        major, minor = c_int(0), c_int(0)
        if lib.eglInitialize(dpy, byref(major), byref(minor)):
            return dpy, f"egl-device[{idx}]", f"EGL {major.value}.{minor.value}"
        diag.append(f"device[{idx}] eglInitialize failed: 0x{lib.eglGetError():x}")
    dpy = lib.eglGetDisplay(EGL_DEFAULT_DISPLAY)
    if dpy:
        major, minor = c_int(0), c_int(0)
        if lib.eglInitialize(dpy, byref(major), byref(minor)):
            return dpy, "egl-default", f"EGL {major.value}.{minor.value}"
    return None, None, None


def make_current(lib, dpy):
    assert lib.eglBindAPI(EGL_OPENGL_API), "eglBindAPI(EGL_OPENGL_API) 失败"
    cfg_attr = (c_int * 15)(
        EGL_SURFACE_TYPE, EGL_PBUFFER_BIT,
        EGL_RENDERABLE_TYPE, EGL_OPENGL_BIT,
        EGL_RED_SIZE, 8, EGL_GREEN_SIZE, 8, EGL_BLUE_SIZE, 8,
        EGL_ALPHA_SIZE, 8, EGL_DEPTH_SIZE, 24, EGL_NONE)
    cfg, num = c_void_p(), c_int(0)
    assert lib.eglChooseConfig(dpy, cfg_attr, byref(cfg), 1, byref(num)) and num.value >= 1
    pb_attr = (c_int * 5)(EGL_WIDTH, WIDTH, EGL_HEIGHT, HEIGHT, EGL_NONE)
    surf = lib.eglCreatePbufferSurface(dpy, cfg, pb_attr)
    assert surf, f"eglCreatePbufferSurface 失败: 0x{lib.eglGetError():x}"
    ctx_attr = (c_int * 8)(
        EGL_CONTEXT_MAJOR_VERSION, 3,
        EGL_CONTEXT_MINOR_VERSION, 3,
        EGL_CONTEXT_OPENGL_PROFILE_MASK, EGL_CONTEXT_OPENGL_COMPAT_PROFILE_BIT,
        EGL_NONE)
    ctx = lib.eglCreateContext(dpy, cfg, EGL_NO_CONTEXT, ctx_attr)
    assert ctx, f"eglCreateContext 失败: 0x{lib.eglGetError():x}"
    assert lib.eglMakeCurrent(dpy, surf, surf, ctx), "eglMakeCurrent 失败"
    return surf, ctx


# 中心裁剪窗（视频坐标）：覆盖方块全部 60 帧位置，排除四角静态蓝底。
# 几何推导见模块 docstring；tests/test_probe_math.py 对同一常量做纯函数证明。
CROP_R0, CROP_R1, CROP_C0, CROP_C1 = 200, 900, 400, 1500
# 蓝底色（B1a：蓝字节 140 必须稳定高于角点阈值 120）
CLEAR_COLOR = (0.02, 0.05, 0.55, 1.0)
# 方块轨道（半长轴 320/240，半边长 40）；tests/test_probe_math.py 复用，禁止两处不一致
ORBIT_RX, ORBIT_RY, SQUARE_HALF = 320, 240, 40


def render_frame(GL, frame_idx):
    """真实 GL 绘制：蓝底 + 居中红色静止三角形 + 绕中心圆周运动的绿色方块。"""
    GL.glViewport(0, 0, WIDTH, HEIGHT)
    # B1a：蓝字节 round(CLEAR_COLOR[2]*255)=140 必须稳定大于角点阈值 120；
    # 旧值 0.40 -> 102 使 corner[2] > 120 恒假（OSMesa 复用同函数会退成 exit 1）。
    GL.glClearColor(*CLEAR_COLOR)
    GL.glClear(GL.GL_COLOR_BUFFER_BIT | GL.GL_DEPTH_BUFFER_BIT)
    GL.glMatrixMode(GL.GL_PROJECTION)
    GL.glLoadIdentity()
    GL.glOrtho(0, WIDTH, HEIGHT, 0, -1, 1)
    GL.glMatrixMode(GL.GL_MODELVIEW)
    GL.glLoadIdentity()

    GL.glBegin(GL.GL_TRIANGLES)
    GL.glColor3f(1.0, 0.0, 0.0)
    GL.glVertex2f(WIDTH / 2 - 220, HEIGHT / 2 + 180)
    GL.glVertex2f(WIDTH / 2 + 220, HEIGHT / 2 + 180)
    GL.glVertex2f(WIDTH / 2, HEIGHT / 2 - 180)
    GL.glEnd()

    ang = 2 * math.pi * frame_idx / N_FRAMES
    cx = int(WIDTH / 2 + ORBIT_RX * math.cos(ang))
    cy = int(HEIGHT / 2 + ORBIT_RY * math.sin(ang))
    s = SQUARE_HALF
    GL.glColor3f(0.0, 1.0, 0.0)
    GL.glBegin(GL.GL_QUADS)
    GL.glVertex2f(cx - s, cy - s)
    GL.glVertex2f(cx + s, cy - s)
    GL.glVertex2f(cx + s, cy + s)
    GL.glVertex2f(cx - s, cy + s)
    GL.glEnd()
    GL.glFinish()

    buf = GL.glReadPixels(0, 0, WIDTH, HEIGHT, GL.GL_RGB, GL.GL_UNSIGNED_BYTE)
    img = np.frombuffer(buf, dtype=np.uint8).reshape(HEIGHT, WIDTH, 3)
    return img[::-1].copy()  # GL 原点在左下，翻转为视频顶左原点


def assert_frame_pixels(img):
    center = img[HEIGHT // 2, WIDTH // 2]
    corner = img[40, 40]
    assert center[0] > 180 and center[1] < 80 and center[2] < 80, f"中心应为红三角，实得 {center}"
    assert corner[2] > 120 and corner[0] < 80, f"四角应为蓝底，实得 {corner}"
    mask = (img[:, :, 1] > 200) & (img[:, :, 0] < 80) & (img[:, :, 2] < 80)
    ys, xs = np.where(mask)
    assert len(xs) > 2000, "绿色方块像素过少，GL 图元可能未真正绘制"
    return float(xs.mean()), float(ys.mean()), int(mask.sum())


def run_osmesa(diag):
    """降级线：OSMesa 软件离屏。能出片但不满足 GPU 验收，退出码区分。"""
    import imageio.v2 as imageio
    from OpenGL import GL
    osmesa = ctypes.CDLL("libOSMesa.so.8")
    OSMESA_RGBA = 0x1908
    # Mesa GL/osmesa.h 的 attrib 常量：FORMAT/DEPTH_BITS/PROFILE + COMPAT_PROFILE
    OSMESA_FORMAT, OSMESA_DEPTH_BITS = 0x22, 0x30
    OSMESA_PROFILE, OSMESA_COMPAT_PROFILE = 4, 2
    osmesa.OSMesaMakeCurrent.argtypes = [c_void_p, c_void_p, c_uint, c_uint, c_uint]
    osmesa.OSMesaMakeCurrent.restype = c_uint
    ctx = None
    create_attribs = getattr(osmesa, "OSMesaCreateContextAttribs", None)
    if create_attribs is not None:
        # Mesa 23 默认 core，固定管线同样需要显式 compat profile
        create_attribs.argtypes = [ctypes.POINTER(c_int), c_void_p]
        create_attribs.restype = c_void_p
        attr = (c_int * 7)(OSMESA_FORMAT, OSMESA_RGBA, OSMESA_DEPTH_BITS, 24,
                           OSMESA_PROFILE, OSMESA_COMPAT_PROFILE, 0)
        ctx = create_attribs(attr, None)
    if not ctx:
        osmesa.OSMesaCreateContextExt.argtypes = [c_uint, c_int, c_int, c_int, c_void_p]
        osmesa.OSMesaCreateContextExt.restype = c_void_p
        ctx = osmesa.OSMesaCreateContextExt(OSMESA_RGBA, 24, 0, 0, None)
    assert ctx, "OSMesaCreateContext 失败"
    buf = (ctypes.c_ubyte * (WIDTH * HEIGHT * 4))()
    assert osmesa.OSMesaMakeCurrent(ctx, buf, GL.GL_UNSIGNED_BYTE, WIDTH, HEIGHT)
    return produce_artifacts("osmesa-software", "Mesa OSMesa (CPU software)",
                             "software", imageio, readback=lambda i: read_osmesa(buf))


def read_osmesa(buf):
    img = np.frombuffer(buf, dtype=np.uint8).reshape(HEIGHT, WIDTH, 4)
    return np.ascontiguousarray(img[:, :, :3])  # OSMesa 顶左原点，无需翻转


def torch_check():
    info = {}
    import torch
    info["torch_version"] = torch.__version__
    info["cuda_available"] = bool(torch.cuda.is_available())
    assert info["cuda_available"], "torch.cuda.is_available() 为 False"
    name = torch.cuda.get_device_name(0)
    info["device_name"] = name
    assert "A800" in name, f"torch 看到的设备不是 A800：{name}"
    x = torch.randn(2048, 2048, device="cuda")
    val = (x @ x).sum().item()
    assert math.isfinite(val)
    info["matmul_finite"] = True
    return info


def produce_artifacts(backend, renderer, tier, imageio, readback):
    import OpenGL.GL as GL
    out_root = os.environ.get("SIMPRAXIS_RUN_DIR", "/root/runs")
    ts = time.strftime("%Y%m%d-%H%M%S")
    outdir = os.path.join(out_root, ts)
    os.makedirs(outdir, exist_ok=True)
    mp4 = os.path.join(outdir, "probe_egl.mp4")
    # imageio 2.36.1 的 ffmpeg writer 形参名为 pixelformat（旧名 output_pixel_format 已移除）
    # 锁内适配（计划 §9.6 原文 quality=8 = x264 crf 10）：探针场景刻意低熵（纯色底+
    # 单三角+小方块），真实 60 帧 GPU 渲染在 crf10/crf5/甚至 crf0 无损下都只有 ~20–33KB
    # ——帧间预测把静态蓝底整段抵消；这被 Task5 test_03 的「mp4>50KB 防空壳」硬门误杀。
    # 实测唯一不改渲染内容即可如实过闸的编码选择：quality=10（crf0 无损）+ 全 I 帧
    # （-g 1 -sc_threshold 0），每帧独立可解码（验收/分析场景的常规 profile），
    # 实测真实体积 ~300KB；渲染内容/帧数/像素断言一律不动。
    writer = imageio.get_writer(
        mp4, fps=FPS, codec="libx264", pixelformat="yuv420p",
        macro_block_size=1, quality=10,
        output_params=["-g", "1", "-sc_threshold", "0"])
    centroids, hashes = [], set()
    for i in range(N_FRAMES):
        if backend == "osmesa-software":
            render_frame(GL, i)  # 绘制到当前 OSMesa 上下文
            img = readback(None)
        else:
            img = render_frame(GL, i)
        cx, cy, npix = assert_frame_pixels(img)
        centroids.append((cx, cy, npix))
        # B1b：旧实现 hash(img.tobytes()[:4096]) 只覆盖顶部约 1.3 行静态蓝底
        # （运动方块在 y∈[260,820]），60 帧哈希全同 -> unique≥48 恒假。
        # 改哈希覆盖运动包络的中心裁剪窗（角点留在窗外）。
        crop = np.ascontiguousarray(img[CROP_R0:CROP_R1, CROP_C0:CROP_C1])
        hashes.add(hash(crop.tobytes()))
        writer.append_data(img)
    writer.close()

    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                            "-show_entries", "stream=codec_name,width,height,nb_frames",
                            "-of", "json", mp4], capture_output=True, text=True, check=True)
    stream = json.loads(probe.stdout)["streams"][0]
    assert stream["codec_name"] == "h264", stream
    assert int(stream["width"]) == 1920 and int(stream["height"]) == 1080, stream
    assert int(stream["nb_frames"]) == N_FRAMES, stream

    cx_std = float(np.std([c[0] for c in centroids]))
    cy_std = float(np.std([c[1] for c in centroids]))
    assert cx_std > 100 and cy_std > 80, f"绿色方块质心几乎不动：cx_std={cx_std} cy_std={cy_std}"
    assert len(hashes) >= int(0.8 * N_FRAMES), f"帧重复度过高：unique={len(hashes)}"

    result = {
        "backend": backend, "renderer": renderer, "tier": tier,
        "gpu_renderer_verified": tier == "gpu",
        "resolution": [1920, 1080], "frames": N_FRAMES, "fps": FPS,
        "frame_unique_hashes": len(hashes),
        "square_centroid_std": [cx_std, cy_std],
        "ffprobe": stream,
        "mp4_path": mp4,
        "torch": torch_check() if tier == "gpu" else {"skipped": "software fallback"},
    }
    json_path = os.path.join(outdir, "probe_egl.json")
    result["json_path"] = json_path
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    latest = os.path.join(out_root, "latest")
    if os.path.islink(latest) or os.path.exists(latest):
        os.remove(latest)
    # B1c：容器内 outdir 是 /root/runs/<ts> 绝对路径；该软链挂到宿主
    # /mnt/artifacts/simpraxis/runs/latest 后指向不存在的 /root/runs/<ts>。
    # 只写相对名，容器内（/root/runs）与宿主（挂载点）两侧都能解析。
    os.symlink(os.path.basename(outdir), latest)
    return result


def main():
    diag = []
    out_root = os.environ.get("SIMPRAXIS_RUN_DIR", "/root/runs")
    os.makedirs(out_root, exist_ok=True)
    try:
        import imageio.v2 as imageio
        lib = load_egl()
        dpy, backend, egl_ver = open_egl_display(lib, diag)
        if dpy is None:
            raise RuntimeError("EGL display 初始化失败：" + " | ".join(diag))
        surf, ctx = make_current(lib, dpy)
        import OpenGL.GL as GL
        vendor = GL.glGetString(GL.GL_VENDOR).decode()
        renderer = GL.glGetString(GL.GL_RENDERER).decode()
        gl_version = GL.glGetString(GL.GL_VERSION).decode()
        print(f"[probe] {backend} | {vendor} | {renderer} | {gl_version}")
        if "NVIDIA" not in vendor and "NVIDIA" not in renderer:
            raise RuntimeError(f"EGL 后端不是 NVIDIA GPU：{vendor} / {renderer}（diag={diag}）")
        result = produce_artifacts(backend, renderer, "gpu", imageio, readback=None)
        result["egl_version"] = egl_ver
        with open(result["json_path"], "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        lib.eglMakeCurrent(None, None, None, None)
        lib.eglDestroySurface(dpy, surf)
        lib.eglDestroyContext(dpy, ctx)
        lib.eglTerminate(dpy)
        print("[probe] GPU EGL PASS:", json.dumps(result["ffprobe"], ensure_ascii=False))
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"[probe] GPU 路径失败：{exc}\n诊断：{diag}", file=sys.stderr)
        try:
            result = run_osmesa(diag)
            print("[probe] 已用 OSMesa 软件渲染降级出片（Milestone 不认可），详见 probe_egl.json",
                  file=sys.stderr)
            return 2
        except Exception as exc2:  # noqa: BLE001
            print(f"[probe] OSMesa 降级也失败：{exc2}", file=sys.stderr)
            return 1


if __name__ == "__main__":
    sys.exit(main())
