#!/usr/bin/env python3
"""Task 3 预览探针（D3-5 / D3-8 / AC-T3-2）：容器内建场、渲一帧 256×256、回 BBox 表。

为什么是探针而不是宿主模块：宿主 ``import genesis`` 段错误（A-06），渲染只能
在容器里做；本脚本与 ``probe_scene_build.py`` 同镜像（``SCENE_BUILD_IMAGE``，
系统解释器 ``/usr/bin/python`` 上装 genesis 1.3.3 + trimesh 5.1.1），但只做
**只读**装配 + 离屏渲染，不写引擎状态，不触网。

回执**恰好一行** JSON（引擎横幅全转 stderr，机制保证而非事后过滤）：

    {"schema":"simpraxis.preview_probe.v1","ok":true,"camera":"iso",
     "png":".preview/preview.png","size":[256,256],
     "bbox_table":[{"asset":"vessel/pitcher_standard","center":[…],
                    "extents":[…],"min":[…],"max":[…]}, …],
     "render_seconds_first":34.8,"render_seconds_second":0.9,
     "wall_s":36.1,"unique_colors":1,"std":0.0,"light_added":false}

同时把同一份 JSON 写到 ``<out>.json`` 旁侧文件：宿主侧工具读它来构造
``bbox_table`` / ``verdict`` / ``spatial_relation``，**图字节永不进工具回执**
（硬规则 1：视觉唯一入口 ``call_visual``）。

冷/热口径（D3-8，Plan §T3.4-D 的 ``PREVIEW_COLD/WARM_TIMEOUT_S``）：
``wall_s`` 是整进程冷启（含引擎创建与建场），``render_seconds_second`` 是
建场后第二帧的渲染耗时——即「同一会话内再渲一帧」的热路径，
**不是**再起一个容器的耗时（那必然又是冷启）。AC-T3-2 按这两项断言。

纪律：0 联网 / 0 付费；只读挂载 ``/assets``；输出只写 ``--out`` 与
``<out>.json``（容器 workdir 的 rw 挂载内）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

RECEIPT_SCHEMA = "simpraxis.preview_probe.v1"
SPEC_SCHEMA = "simpraxis.scene_spec.v1"
MANIFEST_SCHEMA = "simpraxis.asset_zoo.v1"
DEFAULT_SPEC = "/work/scene_spec.json"
DEFAULT_ASSETS = "/assets"
PREVIEW_MAX_PX = 256

# 与 probe_scene_build.py 同源的三处只读根规避（L-16）：numba / genesis /
# quadrants 都要写缓存，而兄弟容器根文件系统只读。必须在 import genesis 前设。
#
# 缓存位置（AC-T3-2 的 G0-12 裁决物，2026-11-04 用户授权）：后端若挂了
# `<phase27>/cache/genesis → /cache:rw`（`run.py::_validate_cache_mount`），
# 就用它——taichi 内核 + 着色器/numba 编译在冷缓存下占冷启 ~76 s（总 96.6 s），
# 同镜像第二次实测 15.1 s。没挂 `/cache` 时退回 tmpfs（正确但慢，回执的
# `cache_dir` 会如实说走了哪条）。
def _cache_root() -> str:
    persistent = "/cache"
    if os.path.isdir(persistent) and os.access(persistent, os.W_OK):
        return persistent
    return "/tmp/simpraxis-cache"


CACHE_ROOT = _cache_root()
for _sub in ("gs", "xdg", "numba"):
    os.makedirs(os.path.join(CACHE_ROOT, _sub), exist_ok=True)
os.environ.setdefault("NUMBA_CACHE_DIR", os.path.join(CACHE_ROOT, "numba"))
os.environ.setdefault("GS_CACHE_FILE_PATH", os.path.join(CACHE_ROOT, "gs"))
os.environ["HOME"] = "/tmp/simpraxis-home"
os.environ.setdefault("XDG_CACHE_HOME", os.path.join(CACHE_ROOT, "xdg"))

_STDOUT_FD = os.dup(1)


class ProbeError(RuntimeError):
    pass


def _emit(payload: dict) -> None:
    os.write(_STDOUT_FD, (json.dumps(payload, ensure_ascii=False) + "\n").encode())


def _mute_engine_output() -> None:
    os.dup2(2, 1)


def _load_spec(path: Path) -> dict:
    if not path.is_file():
        raise ProbeError(f"scene spec 不存在：{path}")
    spec = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(spec, dict) or spec.get("schema") != SPEC_SCHEMA:
        raise ProbeError(f"scene spec schema 必须是 {SPEC_SCHEMA}")
    bodies = spec.get("bodies")
    if not isinstance(bodies, list) or not bodies:
        raise ProbeError("scene spec bodies 必须是非空数组（预览要有可看的件）")
    for body in bodies:
        for key in ("asset", "pos", "quat", "fixed"):
            if key not in body:
                raise ProbeError(f"body 缺字段 {key}：{body!r}")
    return spec


def _load_manifest(assets_root: Path) -> "dict[str, dict]":
    path = assets_root / "zoo_manifest.json"
    if not path.is_file():
        raise ProbeError(f"Asset Zoo manifest 不存在：{path}")
    doc = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or doc.get("schema") != MANIFEST_SCHEMA:
        raise ProbeError(f"manifest schema 必须是 {MANIFEST_SCHEMA}")
    out = {}
    for item in doc.get("assets") or []:
        if isinstance(item, dict) and isinstance(item.get("name"), str):
            out[item["name"]] = item
    if not out:
        raise ProbeError("manifest 没有任何资产")
    return out


def _resolve(assets_root: Path, manifest: "dict[str, dict]", name: str):
    entry = manifest.get(name)
    if entry is None:
        raise ProbeError(f"资产未登记：{name}")
    rel = Path(str(entry["relpath"]))
    if rel.is_absolute() or ".." in rel.parts:
        raise ProbeError(f"资产 relpath 非法：{rel}")
    path = assets_root / rel
    if not path.is_file():
        raise ProbeError(f"资产缺件：{path}")
    return path, entry


def _quat_matrix(quat) -> "list[list[float]]":
    """w,x,y,z → 4×4 旋转矩阵（不依赖 scipy/tf，纯算术，确定性）。"""
    w, x, y, z = (float(v) for v in quat)
    n = (w * w + x * x + y * y + z * z) ** 0.5
    if n == 0.0:
        raise ProbeError("quat 不能全零")
    w, x, y, z = w / n, x / n, y / n, z / n
    return [
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ]


def _apply(rot, vec) -> "list[float]":
    return [sum(rot[i][j] * vec[j] for j in range(3)) for i in range(3)]


def _bbox_table(spec: dict, assets_root: Path, manifest) -> "list[dict]":
    """从网格本身算世界系 AABB（不依赖 Genesis 的 AABB API，确定性）。"""
    import trimesh

    rows = []
    for body in spec["bodies"]:
        path, _entry = _resolve(assets_root, manifest, body["asset"])
        mesh = trimesh.load(str(path), process=False, force="mesh")
        lo, hi = mesh.bounds
        rot = _quat_matrix(body["quat"])
        pos = [float(v) for v in body["pos"]]
        corners = []
        for sx in (lo[0], hi[0]):
            for sy in (lo[1], hi[1]):
                for sz in (lo[2], hi[2]):
                    rotated = _apply(rot, [float(sx), float(sy), float(sz)])
                    corners.append([rotated[i] + pos[i] for i in range(3)])
        wmin = [min(c[i] for c in corners) for i in range(3)]
        wmax = [max(c[i] for c in corners) for i in range(3)]
        rows.append({
            "asset": body["asset"],
            "min": [round(v, 6) for v in wmin],
            "max": [round(v, 6) for v in wmax],
            "center": [round((wmin[i] + wmax[i]) / 2.0, 6) for i in range(3)],
            "extents": [round(wmax[i] - wmin[i], 6) for i in range(3)],
        })
    rows.sort(key=lambda r: r["asset"])
    return rows


def _camera_pose(camera: str, center: "list[float]"):
    cx, cy, cz = center
    if camera == "iso":
        return (cx + 0.30, cy + 0.30, cz + 0.55), (cx, cy, cz)
    if camera == "front":
        return (cx, cy - 0.55, cz + 0.25), (cx, cy, cz)
    if camera == "ortho_top":
        return (cx, cy, cz + 0.60), (cx, cy, cz)
    raise ProbeError(f"未知相机：{camera}")


def _rgb_array(out):
    """把 ``Camera.render()`` 的返回值归一成 H×W×3 的 uint8-ish RGB 数组。

    **Genesis 1.3.3 的 ``Camera.render()`` 返回 tuple（RGB 在首位）**——与
    ``probe_cpu_backend.py::_s3_offscreen_render`` 同一条实测事实
    （``probe_cpu_backend_check.log`` 的 S3 行）。直接 ``np.asarray(tuple)``
    会炸 inhomogeneous shape（AC-T3-2 首次实跑的真错因，非环境问题）。
    """
    import numpy as np

    arr = np.asarray(out[0] if isinstance(out, tuple) else out)
    if arr.ndim == 4:
        arr = arr[0]
    if arr.ndim != 3 or arr.shape[2] != 3:
        raise ProbeError(f"渲染帧形状异常：{list(arr.shape)}（期望 H×W×3）")
    return arr


def _frame_stats(rgb) -> dict:
    import numpy as np

    arr = _rgb_array(rgb)
    flat = arr.reshape(-1, 3)
    return {"shape": list(arr.shape), "dtype": str(arr.dtype),
            "std": round(float(arr.std()), 4),
            "unique_colors": int(len(np.unique(flat, axis=0)))}


def _add_light(scene, gs) -> bool:
    """尽量加一盏方向光（无光时 Rasterizer 出纯色帧，视觉批评拿不到信息）。

    Genesis 各版本的灯光 API 不完全一致，故三层试探 + 如实回报；失败**不**
    影响回执 ok（AC-T3-2 不要求有光），只让 ``light_added`` 说真话。
    """
    attempts = (
        lambda: scene.add_light(directional=True, dir=(-0.3, -0.4, -1.0)),
        lambda: scene.add_light(gs.LightOptions(directional=True)),
        lambda: scene.add_light(),
    )
    for attempt in attempts:
        try:
            attempt()
            return True
        except Exception:  # noqa: BLE001 - 探针只回报能力，不改判定
            continue
    return False


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--spec", default=DEFAULT_SPEC)
    parser.add_argument("--assets", default=DEFAULT_ASSETS)
    parser.add_argument("--out", required=True,
                        help="PNG 输出相对路径（相对 cwd），旁侧 JSON 为 <out>.json")
    parser.add_argument("--camera", default="iso",
                        choices=("iso", "front", "ortho_top"))
    parser.add_argument("--size", type=int, default=PREVIEW_MAX_PX)
    args = parser.parse_args(argv)

    receipt: dict = {"schema": RECEIPT_SCHEMA, "ok": False,
                     "camera": args.camera, "cache_dir": CACHE_ROOT}
    started = time.monotonic()
    # 分段耗时（AC-T3-2 的「实测值落证据」）：cold 预算究竟花在
    # import / init / build / 首帧哪一段，回执自己说清楚，不靠猜。
    phases: "dict[str, float]" = {}
    mark = started
    sidecar_path: "Path | None" = None
    _mute_engine_output()

    def _phase(name: str) -> None:
        nonlocal mark
        now = time.monotonic()
        phases[name] = round(now - mark, 3)
        mark = now

    try:
        if args.size != PREVIEW_MAX_PX:
            raise ProbeError(f"预览尺寸固定 {PREVIEW_MAX_PX}，收到 {args.size}")
        spec = _load_spec(Path(args.spec))
        assets_root = Path(args.assets)
        manifest = _load_manifest(assets_root)
        rows = _bbox_table(spec, assets_root, manifest)
        center = [sum(r["center"][i] for r in rows) / len(rows) for i in range(3)]
        _phase("bbox_table")

        import genesis as gs  # noqa: E402 - 静音之后 import

        _phase("import_genesis")
        gs.init(backend=gs.cpu, seed=0)
        _phase("gs_init")
        scene = gs.Scene(show_viewer=False,
                         renderer=gs.options.renderers.Rasterizer())
        for body in spec["bodies"]:
            path, _entry = _resolve(assets_root, manifest, body["asset"])
            # 预览**只渲不撞**：关掉 convexify，避免 CoACD 凸分解白烧
            # （实测 ~15 s 冷启；预览帧与碰撞体无关）。位置/姿态照旧由
            # spec 决定，不改任何判据。
            morph = gs.morphs.Mesh(file=str(path), pos=body["pos"],
                                   quat=body["quat"], fixed=bool(body["fixed"]),
                                   convexify=False)
            scene.add_entity(morph)
        receipt["light_added"] = _add_light(scene, gs)
        cam_pos, lookat = _camera_pose(args.camera, center)
        cam = scene.add_camera(model="pinhole", res=(args.size, args.size),
                               pos=tuple(cam_pos), lookat=tuple(lookat),
                               fov=40, GUI=False)
        _phase("entities_camera")
        scene.build()
        _phase("scene_build")
        scene.step()
        _phase("scene_step")
        t0 = time.monotonic()
        rgb = cam.render(rgb=True)
        receipt["render_seconds_first"] = round(time.monotonic() - t0, 3)
        _phase("render_first")
        stats = _frame_stats(rgb)

        arr = _rgb_array(rgb)
        out_path = Path(args.out)
        if out_path.is_absolute():
            raise ProbeError("--out 必须是相对 cwd 的路径")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        # 无 PIL 依赖：PNG 由 numpy → 简易编码（Pillow 在镜像里未必有）。
        _write_png(out_path, arr.astype("uint8"))
        t1 = time.monotonic()
        cam.render(rgb=True)     # 热路径：同一会话内的第二帧
        receipt["render_seconds_second"] = round(time.monotonic() - t1, 3)

        receipt.update({
            "png": str(out_path).replace("\\", "/"),
            "size": [int(arr.shape[0]), int(arr.shape[1])],
            "bbox_table": rows,
            "unique_colors": stats["unique_colors"],
            "std": stats["std"],
        })
        # 旁侧回执**在 finally 里落盘**：ok / wall_s / phase_seconds 都在
        # finally 才定稿，提前写会把「ok=true 但回执写 ok=false、无 wall_s」
        # 的假状态留在盘上（AC-T3-2 实跑抓到的真缺陷）。
        sidecar_path = out_path.with_suffix(".json")
        receipt["ok"] = True
    except Exception as exc:  # noqa: BLE001 - 回执要的是 ok/err，不是栈
        receipt["err"] = f"{type(exc).__name__}: {exc}"
    finally:
        _phase("post_render")
        receipt["phase_seconds"] = phases
        receipt["wall_s"] = round(time.monotonic() - started, 2)
        if sidecar_path is not None:
            try:
                sidecar_path.write_text(
                    json.dumps(receipt, ensure_ascii=False), encoding="utf-8")
            except OSError:   # noqa: BLE001 - 写不了旁侧件不影响 stdout 回执
                pass
        try:
            import genesis as gs  # noqa: F401

            gs.destroy()
        except Exception:  # noqa: BLE001 - 拆不掉不影响判定
            pass
        _emit(receipt)
    return 0 if receipt["ok"] else 1


def _write_png(path: Path, arr) -> None:
    """写 PNG：优先 Pillow，缺则用 matplotlib（镜像里两者之一必有）。"""
    try:
        from PIL import Image

        Image.fromarray(arr).save(str(path))
        return
    except Exception:  # noqa: BLE001
        pass
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig = plt.figure(figsize=(arr.shape[1] / 100, arr.shape[0] / 100), dpi=100)
    ax = fig.add_axes((0, 0, 1, 1))
    ax.axis("off")
    ax.imshow(arr)
    fig.savefig(str(path), dpi=100, format="png")
    plt.close(fig)


if __name__ == "__main__":
    raise SystemExit(main())
