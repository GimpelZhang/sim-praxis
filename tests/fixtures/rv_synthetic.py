"""RSI-R1-RV 合成来源 fixture（**fixture_only**；不产生可引用证据）。

复用 ``tests.fixtures.g21_synthetic`` 的两个封存 study，按 G2.1 终态根的形状
组装两根（``freeze/replay_manifest.json`` + 重建 scope + 公开 task），供
``rv_contract.build_source_manifest`` 走真实读路径。产物只写测试临时目录。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from orchestrator.rsi_r1 import g21_bridge as G21
from orchestrator.rsi_r1 import rv_contract as RV
from tests.fixtures import g21_synthetic as G21SYN


def build_rv_source_roots(tmp_path: "str | Path") -> "tuple[Path, dict[str, Any]]":
    """返回 (source_map_path, source_map_doc)；两根在 tmp_path/g21-<S>。"""
    tmp = Path(tmp_path)
    sealed = tmp / "sealed"
    manifest = G21SYN.replay_manifest_doc(sealed)
    studies: "dict[str, Any]" = {}
    for study in ("A", "B"):
        root = tmp / f"g21-{study}"
        G21.freeze_inputs(root, manifest)
        G21.build_scopes(root, manifest)
        # 真实根同时保留 replay_manifest.json（_study_entry_from_g21 读取它）
        RV.write_json_private(root / "freeze" / "replay_manifest.json", manifest)
        task_src = Path(manifest["studies"][study]["task"])
        RV.write_json_private(root / "public" / study / "task.json",
                              RV.read_json(task_src))
        RV.write_json_private(
            root / "contract" / f"scope_{study}.json",
            RV.read_json(root / "contract" / f"scope_{study}.json"))
        studies[study] = {"g21_root": str(root),
                          "source_campaign_id": f"fixture-{study}"}
    doc = {"schema": RV.SOURCE_MAP_SCHEMA, "schema_version": 1,
           "studies": studies}
    path = tmp / "source_map.json"
    RV.write_json_private(path, doc)
    return path, doc


def build_initialized_root(tmp_path: "str | Path") -> Path:
    """初始化一个完整 RV root（两源、公开包、预注册、合同快照）。"""
    from orchestrator.rsi_r1 import rv_cli as CLI
    _, doc = build_rv_source_roots(tmp_path)
    root = Path(tmp_path) / "round"
    source_map = Path(tmp_path) / "source_map.json"
    assert CLI.main(["init", "--root", str(root),
                     "--source-map", str(source_map)]) == 0
    return root
