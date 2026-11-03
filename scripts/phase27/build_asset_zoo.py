#!/usr/bin/env python3
"""确定性构建 Asset Zoo（Phase 2.7 Task 1 / T1.4-A；离线一次性，0 联网 / 0 付费 / 0 新依赖）。

两段式（D1-1，v1.4）：

  STAGE-1 CURATE    只读探测本机成熟库（mani_skill / genesis）里的候选网格，
                    逐件过验收闸，**按序命中即止**；路径不存在直接跳过且不报错。
  STAGE-2 FALLBACK  某件探测不到或过不了闸 → 单次 `trimesh.creation.revolve`
                    闭合剖面（外壁→底→内壁→口沿），**不带把手、不拼接、不布尔**。

两段产物在 manifest 用 ``origin`` 如实区分（``vendored`` / ``revolved``）；
``cube_standard`` 无外部候选，恒走 ``trimesh.creation.box``（``primitive``，见 L-14）。
``probe_report.json`` 记录探测过的每条路径、命中/未命中与原因（审计用），
并登记本脚本自身对 ``trimesh.creation.*`` 的使用点（L-12 ③：引擎层允许原语，
但必须可审计；拼接类调用在**任何**层级都被 ``cad_guard`` 拒绝）。

为什么不能拼接（D1-1 / D1-2 的实测理由）：

- ``trimesh.util.concatenate`` **不是布尔并**，只是把两组顶点/面数组 append
  到一起；把手与壶壁交叉处留下内部重叠面与自相交。两个壳各自水密时
  ``is_watertight`` 仍返回 True（**假水密**），但 CoACD 会切出碎片包围盒、
  SPH 边界粒子会卡在夹缝里。
- 本机宿主**没装** ``manifold3d``，``trimesh.boolean.difference`` 缺布尔引擎时
  会退化成 ``Scene`` 并静默给出非水密结果——同为假水密陷阱。
- 单次回转在拓扑上**保证单连通水密**，所以它当保底、不当主选。

纪律（AC-T1-1 / AC-T1-4 / AC-T1-9 / AC-T1-10）：

- 每个 mesh 必须 ``process=False`` 载入后 ``is_watertight`` 且
  ``is_winding_consistent``；体积 > 0、面数 <= 5000、单连通（恰 1 块）、
  boundary=0、nonmanifold=0、bbox 三轴尺寸落在各自 [lo, hi] 区间；
- 同一脚本连跑两次，逐件 sha256 必须相同（确定性）；build 内**不带时间戳**、
  不写随机数、不 ``merge_vertices``、不重排顶点；
- 归一化（计划 T1.5）：``s = target_longest_edge / max(extents)`` →
  ``apply_scale(s)`` → ``apply_translation(-bounds[0])``（底面落到 z=0）。
- 关键点**先定 schema 再算**（D1-7 / RV-05 / RV-10）：``rigid`` 件只算
  ``bottom_center``；``vessel`` 件算全套，但口沿绕 z 12/16 分箱后各箱最高 z
  极差 < 1e-3 m 即判轴对称 ⇒ ``spout_defined=False``、``pour_edge=None``、
  ``handle_grip=None``（对称体没有唯一出口，硬挑顶点是编造几何真值）。

用法：

    /mnt/venvs/simpraxis/bin/python scripts/phase27/build_asset_zoo.py \\
        --evidence-dir "$SIMPRAXIS_PHASE27_ROOT/task1_asset_zoo" \\
        --hash-out "$SIMPRAXIS_PHASE27_ROOT/task1_asset_zoo/build_run1.sha256"

    # Step 0 容器探针跑完后，把三段回执并进 probe_report.json（T1.5 Step 0 / RV-08）：
    ... --record-step0 "$SIMPRAXIS_PHASE27_ROOT/task1_asset_zoo/probe_cpu_backend_check.log"
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import trimesh
from trimesh.grouping import group_rows

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from orchestrator.agentic.tools import cad_guard  # noqa: E402
from orchestrator.assets.manifest import (  # noqa: E402
    ASSET_SCHEMA_ID,
    MANIFEST_NAME,
    load_zoo_manifest,
    zoo_root,
)
from orchestrator.common.fsutil import (  # noqa: E402
    atomic_write_bytes,
    atomic_write_text,
    ensure_private_dir,
)

BUILD_SCHEMA = "simpraxis.asset_build_report.v1"
CONVEX_SCHEMA = "simpraxis.convex_parts.v1"

MAX_FACES = 5000
COACD_THRESHOLD = 0.05
REVOLVE_SECTIONS = 48

# 关键点几何常量（米制，归一化之后）。
RIM_BAND_M = 1e-3          # 口沿判定带：z >= z_max - 1e-3
SPOUT_ZRANGE_M = 1e-3      # 各角度分箱最高 z 的极差 < 此值 ⇒ 轴对称口沿
SPOUT_BINS = 16            # 绕 z 的角度分箱数（确定性、不随网格密度变）
BOTTOM_Z_TOL = 1e-6        # 「最低面片」判定：面上三顶点 z 都 <= z_min + tol

_MODEL_DIR = "mani_skill/assets/deformable_manipulation"
_BEAKER = f"{_MODEL_DIR}/beaker.glb"
_BOTTLE = f"{_MODEL_DIR}/bottle.glb"
_GENESIS_GLOB = "genesis/assets/meshes/*.obj"


class BuildError(RuntimeError):
    """构建期硬失败（过不了闸且保底也过不了、源目录异常、写出后校验不符）。"""


@dataclass(frozen=True)
class AssetSpec:
    name: str                                  # "vessel/pitcher_standard"
    keypoint_schema: str                       # "vessel" | "rigid"
    target_longest: float                      # 归一化目标最长边（米）
    bbox_ranges: "tuple[tuple[str, float, float], ...]"
    candidates: "tuple[str, ...]"              # STAGE-1 候选（相对 venv 的路径或 glob）
    fallback: "str | None"                     # STAGE-2 剖面名；None = 走原语

    @property
    def relpath(self) -> str:
        return f"{self.name}.obj"


# bbox 区间不是装饰：它让「按序取第一个过闸的候选」语义诚实——
# 候选 1（beaker）归一化到杯的 0.10 后 x=0.100 > 0.09 被拒，候选 2（bottle）
# 归一化到壶的 0.12 后 x=y=0.0478 < 0.06 被拒。四件的区间互斥，
# 因此不需要「某候选已被别的资产占用」这种额外规则。
ASSET_SPECS: "tuple[AssetSpec, ...]" = (
    AssetSpec(
        name="vessel/pitcher_standard", keypoint_schema="vessel",
        target_longest=0.12,
        bbox_ranges=(("x", 0.06, 0.15), ("y", 0.06, 0.15), ("z", 0.06, 0.15)),
        candidates=(_BEAKER, _BOTTLE, _GENESIS_GLOB), fallback="pitcher"),
    AssetSpec(
        name="vessel/cup_standard", keypoint_schema="vessel",
        target_longest=0.10,
        bbox_ranges=(("x", 0.02, 0.09), ("y", 0.02, 0.09), ("z", 0.06, 0.13)),
        candidates=(_BEAKER, _BOTTLE, _GENESIS_GLOB), fallback="cup"),
    AssetSpec(
        name="tableware/plate_standard", keypoint_schema="vessel",
        target_longest=0.16,
        bbox_ranges=(("x", 0.14, 0.18), ("y", 0.14, 0.18), ("z", 0.004, 0.05)),
        candidates=(_BEAKER, _BOTTLE, _GENESIS_GLOB), fallback="plate"),
    AssetSpec(
        name="blocks/cube_standard", keypoint_schema="rigid",
        target_longest=0.05,
        bbox_ranges=(("x", 0.045, 0.055), ("y", 0.045, 0.055),
                     ("z", 0.045, 0.055)),
        candidates=(), fallback=None),
)

# STAGE-2 闭合剖面（(半径 r, 高度 h) 序列；逆时针闭合 ⇒ 法线朝外）。
# 每件都是「外壁→底→内壁→口沿」的单一闭合环，**不做任何拼接**。
REVOLVE_PROFILES: "dict[str, tuple[tuple[float, float], ...]]" = {
    # 壶：外翻口沿的烧杯形（计划 T1.4-A 给定尺寸：外壁 0.06→0.055、高 0.16、
    # 底厚 0.008、内壁内缩 0.004）。归一化会把它缩到 target_longest=0.12。
    "pitcher": ((0.0, 0.0), (0.060, 0.0), (0.055, 0.160), (0.051, 0.160),
                (0.056, 0.008), (0.0, 0.008), (0.0, 0.0)),
    # 杯：半径 0.04 / 高 0.10 / 壁厚 0.004（计划 T1.4-A）。
    "cup": ((0.0, 0.0), (0.040, 0.0), (0.040, 0.100), (0.036, 0.100),
            (0.036, 0.004), (0.0, 0.004), (0.0, 0.0)),
    # 盘：浅盘（外径 0.16、高 0.02、壁厚 0.008、底厚 0.008）。
    "plate": ((0.0, 0.0), (0.080, 0.0), (0.080, 0.020), (0.072, 0.020),
              (0.072, 0.008), (0.0, 0.008), (0.0, 0.0)),
}

CUBE_EXTENTS = (0.05, 0.05, 0.05)


# ── 小工具 ────────────────────────────────────────────────────────────────
def venv_root() -> Path:
    """候选源的根：``$SIMPRAXIS_VENV`` 优先，缺省当前解释器前缀。"""
    return Path(os.environ.get("SIMPRAXIS_VENV") or sys.prefix)


def site_packages() -> Path:
    parts = venv_root() / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}"
    return parts / "site-packages"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _q(value: float) -> float:
    """定点到 9 位小数，抹掉 -0.0（manifest 可读且逐位稳定）。"""
    out = round(float(value), 9)
    return 0.0 if out == 0.0 else out


def _vec(seq) -> "list[float]":
    return [_q(v) for v in seq]


def _obj_bytes(mesh: trimesh.Trimesh) -> bytes:
    data = mesh.export(file_type="obj")
    return data.encode("utf-8") if isinstance(data, str) else data


def _edge_counts(mesh: trimesh.Trimesh) -> "tuple[int, int]":
    """边界边 / 非流形边计数。

    trimesh 5.1.1 删掉了 ``edges_boundary`` / ``edges_nonmanifold``，
    改用 ``grouping.group_rows`` 直接数只出现一次/三次的边。
    """
    edges = mesh.edges_sorted
    boundary = len(group_rows(edges, require_count=1))
    nonmanifold = len(group_rows(edges, require_count=3))
    return boundary, nonmanifold


# ── STAGE-1：候选探测与验收闸 ─────────────────────────────────────────────
def _expand_candidates(spec: AssetSpec) -> "list[Path]":
    out: list[Path] = []
    for entry in spec.candidates:
        if any(ch in entry for ch in "*?["):
            out.extend(sorted(site_packages().glob(entry)))
        else:
            out.append(site_packages() / entry)
    return out


def _load_single_mesh(path: Path) -> trimesh.Trimesh:
    loaded = trimesh.load(str(path), process=False)
    if isinstance(loaded, trimesh.Scene):
        geoms = list(loaded.geometry.values())
        if len(geoms) != 1:
            raise BuildError(f"Scene 含 {len(geoms)} 个几何体（要求恰 1 个）")
        loaded = geoms[0]
    if not isinstance(loaded, trimesh.Trimesh):
        raise BuildError(f"载入结果不是 Trimesh：{type(loaded).__name__}")
    return loaded


def _normalize(mesh: trimesh.Trimesh, target_longest: float) -> trimesh.Trimesh:
    """确定性命中归一化：最长边缩放到目标、bbox 最小角平移到原点。"""
    out = mesh.copy()
    extents = np.asarray(out.extents, dtype=np.float64)
    longest = float(extents.max())
    if not np.isfinite(longest) or longest <= 0.0:
        raise BuildError(f"bbox 最长边非法：{extents.tolist()}")
    out.apply_scale(target_longest / longest)
    out.apply_translation(-np.asarray(out.bounds[0], dtype=np.float64))
    return out


def _metrics(mesh: trimesh.Trimesh) -> dict:
    boundary, nonmanifold = _edge_counts(mesh)
    return {
        "faces": int(len(mesh.faces)),
        "vertices": int(len(mesh.vertices)),
        "watertight": bool(mesh.is_watertight),
        "winding_consistent": bool(mesh.is_winding_consistent),
        "volume": _q(mesh.volume),
        "parts": int(len(mesh.split(only_watertight=False))),
        "boundary_edges": int(boundary),
        "nonmanifold_edges": int(nonmanifold),
        "extents": _vec(mesh.extents),
        "bounds_min": _vec(mesh.bounds[0]),
    }


def _gate(mesh: trimesh.Trimesh) -> "list[str]":
    """结构验收闸（顺序即短路顺序：大网格先被面数挡掉，不白算拓扑）。"""
    failures: "list[str]" = []
    face_count = int(len(mesh.faces))
    if face_count > MAX_FACES:
        return [f"faces={face_count} > {MAX_FACES}"]
    if not bool(mesh.is_watertight):
        failures.append("watertight=False")
    if not bool(mesh.is_winding_consistent):
        failures.append("winding_consistent=False")
    if not (float(mesh.volume) > 0.0):
        failures.append(f"volume={_q(mesh.volume)} <= 0")
    if failures:
        return failures
    parts = len(mesh.split(only_watertight=False))
    if parts != 1:
        failures.append(f"parts={parts} != 1（多壳拼接/多连通，假水密陷阱）")
    boundary, nonmanifold = _edge_counts(mesh)
    if boundary != 0:
        failures.append(f"boundary_edges={boundary} != 0")
    if nonmanifold != 0:
        failures.append(f"nonmanifold_edges={nonmanifold} != 0")
    return failures


def _bbox_failures(mesh: trimesh.Trimesh,
                   ranges) -> "list[str]":
    extents = np.asarray(mesh.extents, dtype=np.float64)
    failures: "list[str]" = []
    for idx, (axis, lo, hi) in enumerate(ranges):
        value = float(extents[idx])
        if not (lo <= value <= hi):
            failures.append(f"bbox.{axis}={value:.6f} 不在 [{lo}, {hi}]")
    return failures


def _probe_candidate(path: Path, spec: AssetSpec) -> "tuple[trimesh.Trimesh | None, dict]":
    """探测一个候选。返回 (归一化后的 mesh 或 None, 审计记录)。"""
    record = {"path": str(path), "exists": path.is_file()}
    if not record["exists"]:
        record["result"] = "missing"
        return None, record
    record["source_sha256"] = _sha256_file(path)
    try:
        raw = _load_single_mesh(path)
    except Exception as exc:  # 载入失败只是「这个候选不行」，不是构建失败
        record["result"] = "load_error"
        record["reason"] = f"{type(exc).__name__}: {exc}"
        return None, record
    record["raw_metrics"] = {
        "faces": int(len(raw.faces)),
        "watertight": bool(raw.is_watertight),
        "winding_consistent": bool(raw.is_winding_consistent),
        "extents": _vec(raw.extents),
    }
    failures = _gate(raw)
    if failures:
        record["result"] = "gate_reject"
        record["reason"] = "; ".join(failures)
        return None, record
    candidate = _normalize(raw, spec.target_longest)
    failures = _bbox_failures(candidate, spec.bbox_ranges)
    if failures:
        record["result"] = "bbox_reject"
        record["reason"] = "; ".join(failures)
        record["normalized_extents"] = _vec(candidate.extents)
        return None, record
    record["result"] = "hit"
    record["normalized_extents"] = _vec(candidate.extents)
    return candidate, record


# ── STAGE-2：单次回转 / 原语 ───────────────────────────────────────────────
def _revolve(profile_name: str) -> trimesh.Trimesh:
    profile = np.asarray(REVOLVE_PROFILES[profile_name], dtype=np.float64)
    return trimesh.creation.revolve(profile, sections=REVOLVE_SECTIONS)


def _cube() -> trimesh.Trimesh:
    return trimesh.creation.box(extents=CUBE_EXTENTS)


# ── 关键点（D1-7 / RV-05 / RV-10）───────────────────────────────────────────
def _axis_xy(mesh: trimesh.Trimesh) -> "tuple[float, float]":
    bounds = np.asarray(mesh.bounds, dtype=np.float64)
    return (float(bounds[0][0] + bounds[1][0]) / 2.0,
            float(bounds[0][1] + bounds[1][1]) / 2.0)


def _bottom_center(mesh: trimesh.Trimesh) -> "tuple[float, float, float]":
    """最低面片聚类的顶点中心（两种 schema 都有的语义）。"""
    verts = np.asarray(mesh.vertices, dtype=np.float64)
    faces = np.asarray(mesh.faces, dtype=np.int64)
    z_min = float(verts[:, 2].min())
    face_z = verts[faces][:, :, 2]
    mask = np.all(face_z <= z_min + BOTTOM_Z_TOL, axis=1)
    if not mask.any():
        # 兜底：取 global z 最小值所在面（理论上不可达，闭合体总有水平底）
        idx = int(np.argmin(face_z.min(axis=1)))
        mask = np.zeros(len(faces), dtype=bool)
        mask[idx] = True
    used = np.unique(faces[mask])
    center = verts[used].mean(axis=0)
    return (_q(center[0]), _q(center[1]), _q(center[2]))


def _vessel_keypoints(mesh: trimesh.Trimesh) -> dict:
    """口沿中心 / 轴对称判定 / （仅在真出口时）出口点与把手。"""
    verts = np.asarray(mesh.vertices, dtype=np.float64)
    z_max = float(verts[:, 2].max())
    rim = verts[verts[:, 2] >= z_max - RIM_BAND_M]
    if len(rim) == 0:                      # 闭合体不可能发生；防御性失败
        raise BuildError("口沿带为空：网格没有可识别的顶部顶点")
    rim_lo = rim.min(axis=0)
    rim_hi = rim.max(axis=0)
    rim_center = (rim_lo + rim_hi) / 2.0

    cx, cy = _axis_xy(mesh)
    angle = np.arctan2(rim[:, 1] - cy, rim[:, 0] - cx)
    bins = ((angle + np.pi) / (2.0 * np.pi) * SPOUT_BINS).astype(np.int64) % SPOUT_BINS
    bin_tops = [float(rim[bins == b][:, 2].max()) for b in np.unique(bins)]
    z_spread = float(max(bin_tops) - min(bin_tops))
    spout_defined = z_spread >= SPOUT_ZRANGE_M

    pour_edge = None
    handle_grip = None                     # 收割件与回转件都不带把手
    if spout_defined:
        top = rim[rim[:, 2] >= float(rim[:, 2].max()) - BOTTOM_Z_TOL]
        center = top.mean(axis=0)
        pour_edge = (_q(center[0]), _q(center[1]), _q(center[2]))
    return {
        "schema": "vessel",
        "bottom_center": _bottom_center(mesh),
        "rim_center": (_q(rim_center[0]), _q(rim_center[1]), _q(rim_center[2])),
        "spout_defined": bool(spout_defined),
        "pour_edge": pour_edge,
        "handle_grip": handle_grip,
        "rim_z_spread_m": _q(z_spread),
        "rim_vertex_count": int(len(rim)),
    }


def _rigid_keypoints(mesh: trimesh.Trimesh) -> dict:
    """方块没有口沿：只登记 bottom_center，其余一律 null（RV-10）。"""
    return {
        "schema": "rigid",
        "bottom_center": _bottom_center(mesh),
        "rim_center": None,
        "spout_defined": False,
        "pour_edge": None,
        "handle_grip": None,
    }


# ── 凸分解 ────────────────────────────────────────────────────────────────
def _convex_parts(mesh: trimesh.Trimesh) -> "tuple[list[dict], str | None]":
    """CoACD 凸分解；失败则降级 convex_hull 并**如实**返回原因（D1-3）。"""
    try:
        import coacd

        coacd.set_log_level("error")
        parts = coacd.run_coacd(
            coacd.Mesh(np.asarray(mesh.vertices, dtype=np.float64),
                       np.asarray(mesh.faces, dtype=np.int32)),
            threshold=COACD_THRESHOLD)
        encoded = [{"vertices": np.asarray(v, dtype=np.float64).tolist(),
                    "faces": np.asarray(f, dtype=np.int32).tolist()}
                   for v, f in parts]
        if not encoded:
            return [], "coacd 返回 0 个凸块"
        return encoded, None
    except Exception as exc:  # 分解失败不阻塞整库，但必须可见
        return [], f"{type(exc).__name__}: {exc}"


# ── 主流程 ────────────────────────────────────────────────────────────────
def build(zoo: Path) -> dict:
    """构建四件资产 + manifest；返回 probe_report 的 assets 段与校验段。"""
    ensure_private_dir(zoo)
    assets_report: "list[dict]" = []
    validate_report: "list[dict]" = []
    manifest_assets: "list[dict]" = []

    for spec in ASSET_SPECS:
        target = zoo / spec.relpath
        ensure_private_dir(target.parent)

        mesh: "trimesh.Trimesh | None" = None
        probes: "list[dict]" = []
        origin = ""
        source = ""
        source_sha256 = None
        stage = ""
        fallback_reason = None

        for candidate in _expand_candidates(spec):
            found, record = _probe_candidate(candidate, spec)
            probes.append(record)
            if found is not None:
                mesh, origin, stage = found, "vendored", "STAGE-1_CURATE"
                source = str(candidate.relative_to(venv_root()))
                source_sha256 = record["source_sha256"]
                break

        if mesh is None:
            if spec.fallback is None:
                mesh, origin, stage = _cube(), "primitive", "PRIMITIVE"
                source = f"trimesh.creation.box(extents={CUBE_EXTENTS})"
            else:
                mesh, origin, stage = _revolve(spec.fallback), "revolved", "STAGE-2_FALLBACK"
                source = "revolve"
                fallback_reason = (
                    "STAGE-1 无可命中的候选"
                    if not probes else
                    "STAGE-1 候选全部未过闸：" + " | ".join(
                        f"{Path(p['path']).name}:{p['result']}"
                        for p in probes if p["result"] != "hit"))
            mesh = _normalize(mesh, spec.target_longest)

        # 落到磁盘前的最终校验（STAGE-2 也要过同一套闸——保底不是免检）
        failures = _gate(mesh) + _bbox_failures(mesh, spec.bbox_ranges)
        if failures:
            raise BuildError(
                f"{spec.name}（{stage}）未过验收闸：{'; '.join(failures)}；"
                "禁止放宽闸门去迁就产物（硬规则 7）")

        payload = _obj_bytes(mesh)
        atomic_write_bytes(target, payload, mode=0o600)

        # 重载复验：磁盘上的 OBJ 必须与内存 mesh 同口径（导出不丢拓扑）
        reloaded = _load_single_mesh(target)
        reload_failures = _gate(reloaded)
        if reload_failures:
            raise BuildError(f"{spec.name} 导出后复验失败：{'; '.join(reload_failures)}")

        parts, convex_error = _convex_parts(reloaded)
        collision_proxy = "coacd" if parts else "convex_hull"
        convex_path = target.with_name(target.stem + ".convex.json")
        convex_doc = {
            "schema": CONVEX_SCHEMA,
            "asset": spec.name,
            "source_relpath": spec.relpath,
            "source_sha256": _sha256_bytes(payload),
            "method": collision_proxy,
            "coacd_threshold": COACD_THRESHOLD if collision_proxy == "coacd" else None,
            "note": convex_error,
            "parts": parts,
        }
        atomic_write_bytes(
            convex_path,
            (json.dumps(convex_doc, ensure_ascii=False, sort_keys=True,
                        indent=1) + "\n").encode("utf-8"),
            mode=0o600)

        if spec.keypoint_schema == "vessel":
            keypoints = _vessel_keypoints(reloaded)
        else:
            keypoints = _rigid_keypoints(reloaded)
        keypoints_doc = {k: v for k, v in keypoints.items()
                         if k in ("schema", "bottom_center", "rim_center",
                                  "spout_defined", "pour_edge", "handle_grip")}

        entry = {
            "name": spec.name,
            "relpath": spec.relpath,
            "sha256": _sha256_bytes(payload),
            "size_bytes": len(payload),
            "faces": int(len(reloaded.faces)),
            "watertight": bool(reloaded.is_watertight),
            "collision_proxy": collision_proxy,
            "origin": origin,
            "source": source,
            "source_sha256": source_sha256,
            "keypoint_schema": spec.keypoint_schema,
            "keypoints": keypoints_doc,
            "convex_relpath": convex_path.name,
        }
        manifest_assets.append(entry)
        validate_report.append({
            "name": spec.name,
            "origin": origin,
            "stage": stage,
            "source": source,
            "metrics": _metrics(reloaded),
            "bbox_ranges": [list(r) for r in spec.bbox_ranges],
            "bbox_ok": True,
            "keypoints": keypoints,
            "collision_proxy": collision_proxy,
            "convex_parts": len(parts),
            "convex_error": convex_error,
        })
        assets_report.append({
            "name": spec.name,
            "origin": origin,
            "stage": stage,
            "source": source,
            "source_sha256": source_sha256,
            "fallback_reason": fallback_reason,
            "probes": probes,
        })

    manifest = {
        "schema": ASSET_SCHEMA_ID,
        "assets": sorted(manifest_assets, key=lambda item: item["name"]),
    }
    atomic_write_text(
        zoo / MANIFEST_NAME,
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=1) + "\n",
        mode=0o600)

    # 写出后立刻用**消费侧解析器**复验（单真值：manifest.py 的 fail-closed 入口）。
    load_zoo_manifest()

    return {"assets": assets_report, "validate": validate_report}


def _hash_listing(zoo: Path) -> str:
    """逐件 sha256 清单（相对 zoo 根，排序后，确定性文本）。"""
    lines = []
    for path in sorted(p for p in zoo.rglob("*") if p.is_file()):
        lines.append(f"{_sha256_file(path)}  {path.relative_to(zoo).as_posix()}")
    return "\n".join(lines) + "\n"


def _cad_guard_selftest() -> dict:
    """L-12：脚本自身过引擎层规则，并登记允许的 trimesh.creation.* 使用点。"""
    source_path = Path(__file__).resolve()
    text = source_path.read_text(encoding="utf-8")
    engine_findings = cad_guard.scan_source(text, tier=cad_guard.TIER_ENGINE)
    model_findings = cad_guard.scan_source(text, tier=cad_guard.TIER_MODEL)
    return {
        "path": str(source_path),
        "engine_tier": [{"rule": f.rule, "lineno": f.lineno, "snippet": f.snippet}
                        for f in engine_findings],
        "engine_tier_violations": len(engine_findings),
        "primitives_used": [{"rule": f.rule, "lineno": f.lineno, "snippet": f.snippet}
                            for f in model_findings if f.rule == "trimesh_creation"],
    }


def _read_step0(path: Path) -> "list[dict]":
    """把 Step 0 的三行 JSON 回执读进来（一段一行，互不掩盖）。"""
    lines = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw.startswith("{"):
            continue
        try:
            lines.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
    return lines


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--zoo-root", default=None,
                        help="资产落盘根（缺省 $SIMPRAXIS_ASSET_ZOO_ROOT 或 /mnt/assets/simpraxis/zoo）")
    parser.add_argument("--evidence-dir", default=None,
                        help="证据目录（probe_report.json / validate_report.json）")
    parser.add_argument("--hash-out", default=None,
                        help="写出逐件 sha256 清单（用于两次构建比对）")
    parser.add_argument("--record-step0", default=None,
                        help="把 Step 0 容器探针日志的三行 JSON 并入 probe_report.json")
    parser.add_argument("--report-only", action="store_true",
                        help="只重建证据文件（不重跑构建）")
    args = parser.parse_args(argv)

    if args.zoo_root:
        # 让「写」与「读」走同一个解析入口（manifest.py 的 zoo_root()），
        # 否则 --zoo-root 只影响构建、复验会去打环境变量指的那个目录。
        os.environ["SIMPRAXIS_ASSET_ZOO_ROOT"] = args.zoo_root
    zoo = zoo_root()
    evidence = Path(args.evidence_dir) if args.evidence_dir else None
    report_path = evidence / "probe_report.json" if evidence else None

    result = None
    if args.report_only:
        if report_path is None or not report_path.is_file():
            raise SystemExit("--report-only 需要既有的 --evidence-dir/probe_report.json")
        report = json.loads(report_path.read_text(encoding="utf-8"))
    else:
        result = build(zoo)
        report = {
            "schema": BUILD_SCHEMA,
            "zoo_root": str(zoo),
            "venv_root": str(venv_root()),
            "site_packages": str(site_packages()),
            "interpreter": sys.version.split()[0],
            "trimesh_version": trimesh.__version__,
            "numpy_version": np.__version__,
            "revolve_sections": REVOLVE_SECTIONS,
            "coacd_threshold": COACD_THRESHOLD,
            "cad_guard": _cad_guard_selftest(),
            "assets": result["assets"],
        }
        if args.hash_out:
            out = Path(args.hash_out)
            ensure_private_dir(out.parent)
            atomic_write_text(out, _hash_listing(zoo), mode=0o600)

    if args.record_step0:
        report["step0_container_probe"] = _read_step0(Path(args.record_step0))

    if report_path is not None:
        ensure_private_dir(report_path.parent)
        atomic_write_text(
            report_path,
            json.dumps(report, ensure_ascii=False, sort_keys=True, indent=1) + "\n",
            mode=0o600)

    if result is not None and evidence is not None:
        validate_path = evidence / "validate_report.json"
        ensure_private_dir(validate_path.parent)
        atomic_write_text(
            validate_path,
            json.dumps({"schema": "simpraxis.asset_validate.v1",
                        "zoo_root": str(zoo),
                        "assets": result["validate"]},
                       ensure_ascii=False, sort_keys=True, indent=1) + "\n",
            mode=0o600)

    print(json.dumps({"ok": True, "zoo_root": str(zoo),
                      "assets": [a["name"] for a in report["assets"]]},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
