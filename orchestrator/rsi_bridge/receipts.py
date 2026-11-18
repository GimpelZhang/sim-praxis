"""证据接缝：`experiment-receipt/v2` ↔ 证据包 bundle manifest 逐件 sha 互证。

互证规则（fail-closed）：
- receipt 声明的每个文件（relpath→sha256）必须在 manifest 中出现且哈希相等；
- manifest 中 receipt 未声明的文件不参与互证（bundle 可含表/文档等外围件）；
- 任一不一致 ⇒ `ReceiptMismatch`（改一字节即 fail，负控见测试）。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

__all__ = ["ReceiptMismatch", "crosscheck_receipt", "file_sha256", "load_manifest_files"]


class ReceiptMismatch(RuntimeError):
    """receipt 与 bundle manifest 互证失败。"""


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _manifest_file_index(manifest: Mapping[str, Any]) -> dict[str, str]:
    """把 bundle_manifest v1 的 {tables: {name: {files: [{relpath, sha256}]}}} 拍平。"""
    index: dict[str, str] = {}
    tables = manifest.get("tables")
    if not isinstance(tables, Mapping):
        raise ReceiptMismatch("manifest 缺 tables 段，无法互证")
    for table, meta in tables.items():
        files = (meta or {}).get("files") if isinstance(meta, Mapping) else None
        if not isinstance(files, list):
            raise ReceiptMismatch(f"manifest.tables[{table}] 缺 files 清单")
        for entry in files:
            relpath = str(entry.get("relpath"))
            index[f"tables/{table}/{relpath}"] = str(entry.get("sha256"))
    return index


def load_manifest_files(manifest_path: Path) -> dict[str, str]:
    document = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    if document.get("schema") != "simpraxis.bundle_manifest.v1":
        raise ReceiptMismatch(f"{manifest_path} 不是 bundle_manifest.v1")
    return _manifest_file_index(document)


def crosscheck_receipt(receipt: Mapping[str, Any],
                       manifest_files: Mapping[str, str]) -> dict:
    """逐件互证；返回匹配清单（供证据包记录），任一不符即抛。"""
    if receipt.get("schema") != "physicalrsi.experiment-receipt/v2":
        raise ReceiptMismatch(f"receipt schema 不是 experiment-receipt/v2：{receipt.get('schema')!r}")
    files = receipt.get("files")
    if not isinstance(files, Mapping) or not files:
        raise ReceiptMismatch("receipt.files 必须是非空 relpath→sha256 映射")
    matched = {}
    for relpath, digest in files.items():
        if relpath not in manifest_files:
            raise ReceiptMismatch(f"receipt 文件 {relpath!r} 不在 manifest 中")
        if manifest_files[relpath] != digest:
            raise ReceiptMismatch(
                f"{relpath!r} 哈希不符：receipt {digest} != manifest {manifest_files[relpath]}")
        matched[relpath] = digest
    return {"matched": matched, "count": len(matched)}
