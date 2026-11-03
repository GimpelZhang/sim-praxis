#!/usr/bin/env python3
"""Task 1 场景装配探针（D1-4 / AC-T1-2）：在容器内把声明式 SceneSpec 变成 Genesis 场景。

为什么是探针而不是宿主模块：宿主进程 ``import genesis`` **段错误**（A-06），
所以场景装配只能在容器里做。本脚本只读挂载点 ``/assets``（宿主
``/mnt/assets/simpraxis/zoo`` 的 ``:ro``）读资产，**逐件核 sha256** 后实例化，
把结果写成**恰好一行** JSON 回执；引擎（含 C++ 层）的全部横幅/警告转 stderr，
所以「一行回执」是机制保证而不是事后过滤。

用法（容器内，``--network none``，默认不带 ``--gpus``）：

    /usr/bin/python /probes/probe_scene_build.py --spec /work/scene_spec.json
    /usr/bin/python /probes/probe_scene_build.py --empty
    # 可选：--render 再离屏渲一帧（Task 3 预演；默认关，软件渲染很慢）

回执 schema：``simpraxis.scene_build.v1``

    {"schema":"simpraxis.scene_build.v1","ok":true,
     "bodies":[{"asset":"vessel/pitcher_standard","pos":[0,0,1.05],"rigid":true}],
     "engine":"genesis","gpu_visible":false,"gpu_mib":null,"backend":"cpu"}

``gpu_mib`` 是 ``int | null``（无 GPU 容器里恒为 null）；``gpu_visible`` 与
``backend`` 由 T1.5 Step 0 的三段探针定档，本探针只**如实回报**,不自行升档
（BR-11：``--gpus`` 未获授权）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

RECEIPT_SCHEMA = "simpraxis.scene_build.v1"
SPEC_SCHEMA = "simpraxis.scene_spec.v1"
MANIFEST_SCHEMA = "simpraxis.asset_zoo.v1"
DEFAULT_SPEC = "/work/scene_spec.json"
DEFAULT_ASSETS = "/assets"
IDENTITY_QUAT = (1.0, 0.0, 0.0, 0.0)   # w,x,y,z

# L-16（2026-11-03 实测）：兄弟容器根文件系统只读（--read-only），而 Genesis
# 的 geom 内核用 numba ``@nb.jit(cache=True)``，默认要在
# ``<site-packages>/genesis/utils/__pycache__`` 落缓存；只读根下抛
# ``RuntimeError: cannot cache function '_np_xyz_to_quat': no locator available``。
# 把 NUMBA_CACHE_DIR 指到 /tmp（tmpfs 可写）即可——必须在 import genesis 之前。
os.environ.setdefault("NUMBA_CACHE_DIR", "/tmp/simpraxis-numba-cache")
# 同一只读根问题的第二个落点：Genesis 退出时会写编译内核缓存到
# ``get_cache_dir()``（首选 ``$GS_CACHE_FILE_PATH``，否则 ``~/.cache/genesis``），
# 容器 HOME 只读 ⇒ OSError Errno 30。显式指到 /tmp。
os.environ.setdefault("GS_CACHE_FILE_PATH", "/tmp/simpraxis-genesis-cache")
# 第三个落点：quadrants（Genesis 的 Taichi 分叉）自己写 ``~/.cache/quadrants``。
# 逐个库找环境变量既易漏又脆（还有别的库写法），直接把这个只读根容器里的
# HOME 重定向到 tmpfs——HOME 不可写是环境事实，不是本探针的语义。
os.environ["HOME"] = "/tmp/simpraxis-home"
os.environ.setdefault("XDG_CACHE_HOME", "/tmp/simpraxis-home/.cache")

_STDOUT_FD = os.dup(1)


def _emit(payload: dict) -> None:
    os.write(_STDOUT_FD, (json.dumps(payload, ensure_ascii=False) + "\n").encode())


def _mute_engine_output() -> None:
    """引擎的一切输出转向 stderr，stdout 只留一行回执。"""
    os.dup2(2, 1)


class ProbeError(RuntimeError):
    """spec/manifest/资产任一环节不符契约（一律 ok=false，不静默兜底）。"""


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_manifest(assets_root: Path) -> "dict[str, dict]":
    """读挂载点里的 zoo_manifest.json；结构非法即 fail-closed。"""
    path = assets_root / "zoo_manifest.json"
    if not path.is_file():
        raise ProbeError(f"资产 manifest 缺失：{path}（/assets 挂载不全）")
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ProbeError(f"资产 manifest 无法解析：{path}：{exc}") from None
    if not isinstance(doc, dict) or doc.get("schema") != MANIFEST_SCHEMA:
        raise ProbeError(
            f"资产 manifest schema 不符：期望 {MANIFEST_SCHEMA}，"
            f"实际 {doc.get('schema') if isinstance(doc, dict) else type(doc)!r}")
    entries = doc.get("assets")
    if not isinstance(entries, list) or not entries:
        raise ProbeError("资产 manifest 的 assets 必须是非空数组")
    table: "dict[str, dict]" = {}
    for item in entries:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            raise ProbeError(f"manifest 条目非法：{item!r}")
        if item["name"] in table:
            raise ProbeError(f"manifest 资产重名：{item['name']}")
        table[item["name"]] = item
    return table


def _resolve(assets_root: Path, manifest: "dict[str, dict]",
             name: str) -> "tuple[Path, dict]":
    """按 manifest 取资产并核 sha256；未登记 / 越界 / 不符一律 ProbeError。"""
    entry = manifest.get(name)
    if entry is None:
        raise ProbeError(
            f"资产 {name!r} 未在 manifest 登记（可用：{sorted(manifest)}）")
    relpath = entry.get("relpath")
    if not isinstance(relpath, str) or not relpath:
        raise ProbeError(f"资产 {name} relpath 非法：{relpath!r}")
    rel = Path(relpath)
    if rel.is_absolute() or ".." in rel.parts:
        raise ProbeError(f"资产 {name} relpath 不得为绝对路径或含 ..：{relpath}")
    path = (assets_root / rel).resolve()
    root = assets_root.resolve()
    if root not in path.parents:
        raise ProbeError(f"资产 {name} relpath 逃出资产根：{relpath}")
    if not path.is_file():
        raise ProbeError(f"资产 {name} 磁盘缺件：{path}")
    expect = entry.get("sha256")
    actual = _sha256_file(path)
    if actual != expect:
        raise ProbeError(
            f"资产 {name} sha256 不符：manifest {str(expect)[:12]}… "
            f"实际 {actual[:12]}…（{path}）")
    return path, entry


def _triple(value, field_name: str) -> "tuple[float, float, float]":
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ProbeError(f"scene_spec {field_name} 必须是 3 元数组：{value!r}")
    out = []
    for item in value:
        if not isinstance(item, (int, float)) or isinstance(item, bool):
            raise ProbeError(f"scene_spec {field_name} 分量必须是数字：{value!r}")
        out.append(float(item))
    return (out[0], out[1], out[2])


def _quat(value, field_name: str) -> "tuple[float, float, float, float]":
    if value is None:
        return IDENTITY_QUAT
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ProbeError(f"scene_spec {field_name} 必须是 4 元数组（w,x,y,z）：{value!r}")
    out = []
    for item in value:
        if not isinstance(item, (int, float)) or isinstance(item, bool):
            raise ProbeError(f"scene_spec {field_name} 分量必须是数字：{value!r}")
        out.append(float(item))
    norm = sum(v * v for v in out) ** 0.5
    if abs(norm - 1.0) > 1e-6:
        raise ProbeError(
            f"scene_spec {field_name} 必须是单位四元数（当前模长 {norm:.9f}）")
    return (out[0], out[1], out[2], out[3])


def _load_spec(path: Path) -> dict:
    """自校验 JSON（探针不 import 引擎仓：容器里只有 /work、/assets、/probes）。"""
    if not path.is_file():
        raise ProbeError(f"scene_spec 不存在：{path}")
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ProbeError(f"scene_spec 不是合法 JSON：{exc}") from None
    if not isinstance(doc, dict) or doc.get("schema") != SPEC_SCHEMA:
        raise ProbeError(
            f"scene_spec.schema 必须是 {SPEC_SCHEMA}："
            f"{doc.get('schema') if isinstance(doc, dict) else type(doc)!r}")
    if doc.get("engine") != "genesis":
        raise ProbeError(
            f"scene_build 探针只装配 engine='genesis'：{doc.get('engine')!r}")
    raw_bodies = doc.get("bodies")
    if not isinstance(raw_bodies, list) or not raw_bodies:
        raise ProbeError("scene_spec.bodies 必须是非空数组")
    bodies = []
    for item in raw_bodies:
        if not isinstance(item, dict):
            raise ProbeError(f"scene_spec.bodies 元素必须是对象：{item!r}")
        asset = item.get("asset")
        if not isinstance(asset, str) or not asset:
            raise ProbeError(f"scene_spec body.asset 必须是非空字符串：{asset!r}")
        fixed = item.get("fixed", True)
        if not isinstance(fixed, bool):
            raise ProbeError(f"scene_spec body.fixed 必须是 bool：{fixed!r}")
        bodies.append({"asset": asset,
                       "pos": _triple(item.get("pos"), f"bodies[{asset}].pos"),
                       "quat": _quat(item.get("quat"), f"bodies[{asset}].quat"),
                       "fixed": fixed})
    return {"engine": doc["engine"], "bodies": bodies,
            "fluid": doc.get("fluid")}


def _gpu_state() -> dict:
    """与 Step 0 同口径：仅报告可见性，不初始化 CUDA。"""
    state = {"gpu_visible": False, "gpu_mib": None}
    try:
        import torch

        if torch.cuda.is_available() and torch.cuda.device_count() > 0:
            free, total = torch.cuda.mem_get_info(0)
            state["gpu_visible"] = True
            state["gpu_mib"] = int((total - free) // (1 << 20))
    except Exception as exc:  # noqa: BLE001 - 探针只报告，不改变判定
        state["gpu_error"] = f"{type(exc).__name__}: {exc}"
    return state


def _frame_stats(rgb) -> dict:
    import numpy as np

    arr = np.asarray(rgb)
    if arr.ndim == 4:      # 环境维（多 env 渲染）
        arr = arr[0]
    if arr.ndim != 3 or arr.shape[2] != 3:
        raise ProbeError(f"渲染帧形状异常：{list(arr.shape)}（期望 H×W×3）")
    flat = arr.reshape(-1, 3)
    return {"shape": list(arr.shape), "dtype": str(arr.dtype),
            "std": round(float(arr.std()), 4),
            "unique_colors": int(len(np.unique(flat, axis=0)))}


def _build_scene(gs, bodies, assets_root: Path, manifest: "dict[str, dict]",
                 render: bool):
    gs.init(backend=gs.cpu, seed=0)
    scene = gs.Scene(show_viewer=False,
                     renderer=gs.options.renderers.Rasterizer() if render else None)
    placed = []
    for body in bodies:
        path, entry = _resolve(assets_root, manifest, body["asset"])
        morph = gs.morphs.Mesh(file=str(path), pos=body["pos"],
                              quat=body["quat"], fixed=body["fixed"],
                              convexify=True)
        entity = scene.add_entity(morph)
        placed.append({"body": body, "entry": entry, "entity": entity,
                       "path": str(path)})
    cam = None
    if render:
        # 俯视（+z 向下）+ 前视（+y 向后）都覆盖不到全部件时，取斜俯视。
        cam = scene.add_camera(model="pinhole", res=(256, 256),
                               pos=(0.55, 0.55, 0.75), lookat=(0.25, 0.25, 1.03),
                               fov=40, GUI=False)
    scene.build()
    return scene, placed, cam


def _receipt_bodies(placed) -> "list[dict]":
    import numpy as np

    out = []
    for item in placed:
        body = item["body"]
        entity = item["entity"]
        raw = entity.get_pos()
        if hasattr(raw, "detach"):          # gs.gpu 后端返回 torch tensor
            raw = raw.detach().cpu().numpy()
        pos = [round(float(x), 9) for x in np.asarray(raw).reshape(-1)[:3]]
        if len(pos) != 3:
            raise ProbeError(f"资产 {body['asset']} 读回的位姿分量不足 3：{pos}")
        drift = max(abs(pos[i] - body["pos"][i]) for i in range(3))
        if drift > 1e-6:
            raise ProbeError(
                f"资产 {body['asset']} 实例化后位姿漂移 {drift:.3e} m "
                f"（请求 {body['pos']} 实际 {pos}）")
        out.append({"asset": body["asset"], "pos": pos,
                    "quat": list(body["quat"]),
                    "fixed": body["fixed"],
                    "rigid": not body["fixed"],
                    "origin": item["entry"].get("origin")})
    return out


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--spec", default=DEFAULT_SPEC)
    parser.add_argument("--assets", default=DEFAULT_ASSETS)
    parser.add_argument("--empty", action="store_true",
                        help="空场景形态（不读 spec、不挂资产）")
    parser.add_argument("--render", action="store_true",
                        help="额外离屏渲一帧 256×256（软件渲染，约 30 s）")
    parser.add_argument("--steps", type=int, default=1,
                        help="建场后步进的物理步数（默认 1）")
    args = parser.parse_args(argv)

    receipt: dict = {"schema": RECEIPT_SCHEMA, "ok": False,
                     "engine": "genesis", "backend": "cpu",
                     "empty": bool(args.empty), "steps": args.steps}
    started = time.monotonic()
    _mute_engine_output()
    try:
        import genesis as gs  # noqa: E402 - 静音之后再 import

        receipt["genesis_version"] = gs.__version__
        receipt.update(_gpu_state())
        assets_root = Path(args.assets)
        if args.empty:
            spec = {"engine": "genesis", "bodies": [], "fluid": None}
            manifest: "dict[str, dict]" = {}
        else:
            spec = _load_spec(Path(args.spec))
            manifest = _load_manifest(assets_root)
        receipt["spec"] = {"engine": spec["engine"],
                           "bodies": [b["asset"] for b in spec["bodies"]],
                           "fluid": spec["fluid"]}
        scene, placed, cam = _build_scene(gs, spec["bodies"], assets_root,
                                          manifest, args.render)
        for _ in range(max(0, args.steps)):
            scene.step()
        receipt["bodies"] = _receipt_bodies(placed)
        if len(receipt["bodies"]) != len(spec["bodies"]):
            raise ProbeError(
                f"实例化件数与 spec 不符：{len(receipt['bodies'])} vs "
                f"{len(spec['bodies'])}")
        if cam is not None:
            out = cam.render(rgb=True)
            if isinstance(out, tuple):
                if isinstance(out[0], (list, tuple)):   # Genesis 1.4 多相机形态
                    out = out[0]
                rgb = out[0]
            else:
                rgb = out
            receipt["render"] = _frame_stats(rgb)
        receipt["ok"] = True
    except Exception as exc:  # noqa: BLE001 - 回执要的是 ok/err，不是栈
        receipt["err"] = f"{type(exc).__name__}: {exc}"
    finally:
        receipt["wall_s"] = round(time.monotonic() - started, 2)
        try:
            import genesis as gs  # noqa: F401 - 仅在已 import 时有效

            gs.destroy()
        except Exception:  # noqa: BLE001 - 拆不掉不影响判定
            pass
        _emit(receipt)
    return 0 if receipt["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
