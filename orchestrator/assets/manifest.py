"""Asset Zoo 解析层（D1-6）：只认 manifest 登记的件，其余一律 AssetError。

为什么不直接拼路径：``zoo_root() / (name + ".obj")`` 会被 ``../`` 穿越，
也会在缺件时静默给出一个不存在的路径、错误推迟到下游引擎才炸。manifest
解析天然 fail-closed：名字不在表里就没有条目，路径不由调用方拼。

三层纪律（CLAUDE.md 硬规则 3 的同构落地）：网格实体与 ``zoo_manifest.json``
都落 ``/mnt/assets/simpraxis/zoo``（0700 本地数据层，不入 git）；仓内只有
生成脚本 ``scripts/phase27/build_asset_zoo.py`` 与解析层本文件（小文件、可评审）。
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

ASSET_SCHEMA_ID = "simpraxis.asset_zoo.v1"
DEFAULT_ZOO_ROOT = "/mnt/assets/simpraxis/zoo"
ZOO_ROOT_ENV = "SIMPRAXIS_ASSET_ZOO_ROOT"
MANIFEST_NAME = "zoo_manifest.json"

KEYPOINT_SCHEMAS = ("vessel", "rigid")
# L-14（2026-11-03 实测登记）：AC-T1-9 ① 原文写 origin ∈ {"vendored","revolved"}，
# 而 T1.4-A 又规定 cube_standard 恒由 trimesh.creation.box 生成——它既非收割
# （无外部源文件）也非回转。把方块标成 "revolved" 会让「来源可核」字段本身说谎，
# 故枚举增 "primitive"，并按 origin 分支 fail-closed（vendored 必验源文件 sha256；
# revolved/primitive 的 source_sha256 必须为 None 且 source 必填生成器描述）。
ORIGINS = ("vendored", "revolved", "primitive")
COLLISION_PROXIES = ("coacd", "convex_hull")


class AssetError(RuntimeError):
    """资产缺件 / 哈希不符 / 未登记 / manifest 结构非法（一律 fail-closed）。"""


def zoo_root() -> Path:
    """``$SIMPRAXIS_ASSET_ZOO_ROOT`` 优先，缺省 ``/mnt/assets/simpraxis/zoo``。"""
    return Path(os.environ.get(ZOO_ROOT_ENV) or DEFAULT_ZOO_ROOT)


def manifest_path() -> Path:
    return zoo_root() / MANIFEST_NAME


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _vec3(value, field: str) -> "tuple[float, float, float] | None":
    if value is None:
        return None
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise AssetError(f"keypoints.{field} 必须是 3 元数组或 null：{value!r}")
    out = []
    for item in value:
        if not isinstance(item, (int, float)) or isinstance(item, bool):
            raise AssetError(f"keypoints.{field} 分量必须是数字：{value!r}")
        out.append(float(item))
    return (out[0], out[1], out[2])


@dataclass(frozen=True)
class Keypoints:
    """语义关键点（D1-7 / RV-05 / RV-10）。米制、资产本地系、确定性计算。

    两条诚实性闸：

    - ``spout_defined``：收割来的 beaker/bottle 是 360° 轴对称体，整圈口沿
      z 相同，「沿 +x 最远的口沿点」只是坐标轴上的任意一点，与倒水方向
      无物理关系。此时 ``spout_defined=False``，``pour_edge`` /
      ``handle_grip`` 必须为 ``None``——硬挑一个出口点等于编造几何真值。
    - ``schema``：方块没有口沿。``schema="rigid"`` 时 ``rim_center`` 必须为
      ``None``（在无该语义的形体上硬造口沿 = 同类错误），只有
      ``bottom_center`` 有意义。
    """

    schema: str                                  # "vessel" | "rigid"
    bottom_center: "tuple[float, float, float] | None"
    spout_defined: bool
    rim_center: "tuple[float, float, float] | None" = None
    pour_edge: "tuple[float, float, float] | None" = None
    handle_grip: "tuple[float, float, float] | None" = None

    def __post_init__(self) -> None:
        if self.schema not in KEYPOINT_SCHEMAS:
            raise AssetError(
                f"keypoints.schema 必须是 {KEYPOINT_SCHEMAS} 之一：{self.schema!r}")
        if not isinstance(self.spout_defined, bool):
            raise AssetError("keypoints.spout_defined 必须是 bool")
        if self.bottom_center is None:
            raise AssetError("keypoints.bottom_center 必填（两种 schema 都要）")
        if self.schema == "rigid":
            if self.rim_center is not None or self.pour_edge is not None \
                    or self.handle_grip is not None:
                raise AssetError(
                    "keypoints.schema='rigid' 时 rim_center/pour_edge/"
                    "handle_grip 必须为 null（方块没有口沿，硬造即假信号）")
            if self.spout_defined:
                raise AssetError("keypoints.schema='rigid' 时 spout_defined 必为 False")
            return
        # vessel
        if self.rim_center is None:
            raise AssetError("keypoints.schema='vessel' 时 rim_center 必填")
        if not self.spout_defined and (self.pour_edge is not None
                                       or self.handle_grip is not None):
            raise AssetError(
                "keypoints.spout_defined=False 时 pour_edge/handle_grip 必须为 null"
                "（轴对称口沿没有唯一出口，硬挑顶点即为假信号）")

    def to_json(self) -> dict:
        def enc(v):
            return None if v is None else [float(x) for x in v]
        return {"schema": self.schema,
                "bottom_center": enc(self.bottom_center),
                "rim_center": enc(self.rim_center),
                "pour_edge": enc(self.pour_edge),
                "handle_grip": enc(self.handle_grip),
                "spout_defined": bool(self.spout_defined)}

    @classmethod
    def from_json(cls, doc: dict) -> "Keypoints":
        if not isinstance(doc, dict):
            raise AssetError(f"keypoints 必须是对象：{doc!r}")
        if "schema" not in doc:
            raise AssetError("keypoints 缺 schema（v1.4b 起必填）")
        return cls(
            schema=doc["schema"],
            bottom_center=_vec3(doc.get("bottom_center"), "bottom_center"),
            spout_defined=doc.get("spout_defined"),
            rim_center=_vec3(doc.get("rim_center"), "rim_center"),
            pour_edge=_vec3(doc.get("pour_edge"), "pour_edge"),
            handle_grip=_vec3(doc.get("handle_grip"), "handle_grip"),
        )


@dataclass(frozen=True)
class AssetEntry:
    name: str                    # "vessel/pitcher_standard"
    relpath: str                 # "vessel/pitcher_standard.obj"（相对 zoo 根）
    sha256: str
    size_bytes: int
    faces: int
    watertight: bool
    collision_proxy: str         # "coacd" | "convex_hull"
    origin: str                  # "vendored" | "revolved" | "primitive"（L-14）
    source: str                  # vendored：相对 venv 的源路径；其余：生成器描述
    source_sha256: "str | None"  # vendored 必填，revolved 为 None
    keypoints: Keypoints
    path: Path                   # 解析后的绝对路径（不入 manifest，仅供读取）

    def to_json(self) -> dict:
        return {
            "name": self.name, "relpath": self.relpath,
            "sha256": self.sha256, "size_bytes": self.size_bytes,
            "faces": self.faces, "watertight": self.watertight,
            "collision_proxy": self.collision_proxy,
            "origin": self.origin, "source": self.source,
            "source_sha256": self.source_sha256,
            "keypoints": self.keypoints.to_json(),
        }


def _entry_from_json(name: str, doc: dict, root: Path) -> AssetEntry:
    if not isinstance(doc, dict):
        raise AssetError(f"asset {name} 条目必须是对象")
    for field in ("relpath", "sha256", "size_bytes", "faces", "watertight",
                  "collision_proxy", "origin", "source", "keypoints"):
        if field not in doc:
            raise AssetError(f"asset {name} 缺字段 {field}")
    relpath = doc["relpath"]
    if not isinstance(relpath, str) or not relpath:
        raise AssetError(f"asset {name} relpath 非法：{relpath!r}")
    rel = Path(relpath)
    if rel.is_absolute() or ".." in rel.parts:
        raise AssetError(f"asset {name} relpath 不得为绝对路径或含 ..：{relpath}")
    target = (root / rel).resolve(strict=False)
    root_resolved = root.resolve(strict=False)
    if target != root_resolved and root_resolved not in target.parents:
        raise AssetError(f"asset {name} relpath 逃出 zoo 根：{relpath}")
    if doc["origin"] not in ORIGINS:
        raise AssetError(f"asset {name} origin 必须是 {ORIGINS}：{doc['origin']!r}")
    if doc["collision_proxy"] not in COLLISION_PROXIES:
        raise AssetError(
            f"asset {name} collision_proxy 必须是 {COLLISION_PROXIES}："
            f"{doc['collision_proxy']!r}")
    if doc["origin"] == "vendored" and not doc.get("source_sha256"):
        raise AssetError(f"asset {name} origin=vendored 必须登记 source_sha256（来源不可考）")
    if doc["origin"] != "vendored" and doc.get("source_sha256") is not None:
        raise AssetError(
            f"asset {name} origin={doc['origin']} 时 source_sha256 必须为 null"
            "（revolved/primitive 无外部源文件）")
    if not isinstance(doc.get("source"), str) or not doc["source"]:
        raise AssetError(
            f"asset {name} source 必填（vendored=相对 venv 的源路径；"
            "revolved/primitive=生成器描述）")
    return AssetEntry(
        name=name, relpath=relpath, sha256=str(doc["sha256"]),
        size_bytes=int(doc["size_bytes"]), faces=int(doc["faces"]),
        watertight=bool(doc["watertight"]),
        collision_proxy=doc["collision_proxy"], origin=doc["origin"],
        source=str(doc["source"]), source_sha256=doc.get("source_sha256"),
        keypoints=Keypoints.from_json(doc["keypoints"]), path=target)


def load_zoo_manifest() -> dict:
    """读 manifest 并**逐件**校验磁盘 sha256 与 size；任何不符即 AssetError。"""
    path = manifest_path()
    if not path.is_file():
        raise AssetError(
            f"Asset Zoo manifest 不存在：{path}"
            "（先跑 scripts/phase27/build_asset_zoo.py）")
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise AssetError(f"Asset Zoo manifest 无法解析：{path}：{exc}") from None
    if not isinstance(doc, dict) or doc.get("schema") != ASSET_SCHEMA_ID:
        raise AssetError(
            f"Asset Zoo manifest schema 不符：期望 {ASSET_SCHEMA_ID}，"
            f"实际 {doc.get('schema') if isinstance(doc, dict) else type(doc)!r}")
    assets = doc.get("assets")
    if not isinstance(assets, list) or not assets:
        raise AssetError("Asset Zoo manifest 的 assets 必须是非空数组")
    root = zoo_root()
    entries: dict[str, AssetEntry] = {}
    for item in assets:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            raise AssetError(f"manifest asset 条目非法：{item!r}")
        name = item["name"]
        if name in entries:
            raise AssetError(f"manifest asset 重名：{name}")
        entry = _entry_from_json(name, item, root)
        if not entry.path.is_file():
            raise AssetError(f"asset {name} 磁盘缺件：{entry.path}")
        size = entry.path.stat().st_size
        if size != entry.size_bytes:
            raise AssetError(
                f"asset {name} size 不符：manifest {entry.size_bytes} "
                f"实际 {size}（{entry.path}）")
        actual = _sha256_file(entry.path)
        if actual != entry.sha256:
            raise AssetError(
                f"asset {name} sha256 不符：manifest {entry.sha256[:12]}… "
                f"实际 {actual[:12]}…（{entry.path}）")
        entries[name] = entry
    return {"schema": ASSET_SCHEMA_ID, "root": root,
            "manifest_path": path, "assets": entries}


def resolve_asset(name: str) -> AssetEntry:
    """按名取资产条目；未登记 / 结构非法 / 磁盘不符一律 AssetError。"""
    if not isinstance(name, str) or not name:
        raise AssetError(f"资产名必须是非空字符串：{name!r}")
    entries = load_zoo_manifest()["assets"]
    if name not in entries:
        raise AssetError(
            f"资产 {name!r} 未在 manifest 登记（可用：{sorted(entries)}）")
    return entries[name]


def list_assets() -> list[str]:
    return sorted(load_zoo_manifest()["assets"])
