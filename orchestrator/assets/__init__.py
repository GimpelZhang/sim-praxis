"""Asset Zoo 解析与声明式场景装配（Phase 2.7 Task 1，D1-4/D1-6）。

- ``manifest``：只读解析 ``zoo_manifest.json``，逐件 sha256 校验，fail-closed；
- ``scene_spec``：**数据**（不 import 任何物理引擎，也不执行任何东西）。
  场景由 ``sandbox/probes/probe_scene_build.py`` 在容器内消费。
"""
from __future__ import annotations

from .manifest import (  # noqa: F401
    ASSET_SCHEMA_ID,
    AssetEntry,
    AssetError,
    Keypoints,
    list_assets,
    load_zoo_manifest,
    resolve_asset,
    zoo_root,
)
from .scene_spec import SCENE_SPEC_SCHEMA, Body, SceneSpec  # noqa: F401

__all__ = [
    "ASSET_SCHEMA_ID", "AssetEntry", "AssetError", "Keypoints", "Body",
    "SCENE_SPEC_SCHEMA", "SceneSpec", "list_assets", "load_zoo_manifest",
    "resolve_asset", "zoo_root",
]
