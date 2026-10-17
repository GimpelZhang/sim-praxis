#!/usr/bin/env python3
"""Task0b 数据资产打包：core/extended 两档，产物落 /mnt（不入库，0 付费 0 触网）。

档位：
- core     = kb_chunks + phase1_learnings（生产召回热路径最小集）
- extended = 数据层全部表（含 solution_chunks；未来新增表自动纳入）

产物布局（root 0700、文件 0600）：
  <out>/simpraxis-<tier>-<kb8>-<UTC>/
    bundle_manifest.json      schema simpraxis.bundle_manifest.v1
    tables/<table>.lance/...  逐文件字节复制（拒绝软链），sha256+size 入清单

manifest 对账项（verify_bundle.py 复算）：kb_sha、引擎仓 sim_praxis_sha、
嵌入模型 paraphrase-multilingual-mpnet-base-v2、vector_dim=768、每表行数。

实跑（lead 在引擎仓提交 + 数据迁移 + solution_chunks 入库之后）：
  python scripts/kb/bundle_kb_asset.py --tier both
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import lancedb  # noqa: E402

from memory_flywheel.embedding_selector import select_embedding  # noqa: E402
from memory_flywheel.kb_pins import DEFAULT_KB_ROOT, head as kb_head  # noqa: E402
from orchestrator.common.clock import utc_now_iso_z  # noqa: E402
from orchestrator.common.fsutil import (  # noqa: E402
    atomic_write_text, ensure_private_dir)

BUNDLE_SCHEMA = "simpraxis.bundle_manifest.v1"
EMBEDDING_MODEL_EXPECTED = "sentence-transformers/paraphrase-multilingual-mpnet-base-v2"
CORE_TABLES = ("kb_chunks", "phase1_learnings")
DEFAULT_OUT = Path("/mnt/artifacts/embodied-kb/data/bundles")


class BundleError(RuntimeError):
    """打包前置失败：拒绝产生半成品包。"""


def _git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise BundleError(f"git -C {repo} {' '.join(args)} 失败：{r.stderr.strip()}")
    return r.stdout.strip()


def default_uri() -> str:
    return os.environ.get(
        "SIMPRAXIS_LANCEDB_URI",
        str(Path(os.environ.get("KB_ROOT", str(DEFAULT_KB_ROOT)))
            / "data" / "lancedb"))


def _sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def table_names(uri: Path) -> list[str]:
    return sorted(
        n[:-6] for n in os.listdir(uri)
        if n.endswith(".lance") and (uri / n).is_dir())


def table_vector_dim(uri: Path, table: str) -> int | None:
    schema = lancedb.connect(str(uri)).open_table(table).schema
    if "vector" not in schema.names:
        return None
    return int(schema.field("vector").type.list_size)


def copy_table(src_dir: Path, dst_dir: Path) -> list[dict]:
    """字节复制一张 .lance 表；返回文件清单（relpath/size/sha256）。拒绝软链。"""
    files: list[dict] = []
    for p in sorted(src_dir.rglob("*")):
        rel = p.relative_to(src_dir).as_posix()
        if p.is_symlink():
            raise BundleError(f"拒绝把软链打入资产包：{src_dir.name}/{rel}")
        if not p.is_file():
            continue
        target = dst_dir / rel
        ensure_private_dir(target.parent, mode=0o700)
        shutil.copyfile(p, target)
        os.chmod(target, 0o600)
        size = p.stat().st_size
        digest = _sha256_file(target)
        if _sha256_file(p) != digest:  # 复制后源/目双算
            raise BundleError(f"复制字节不一致：{src_dir.name}/{rel}")
        files.append({"relpath": rel, "size": size, "sha256": digest})
    if not files:
        raise BundleError(f"表目录为空，拒绝空包：{src_dir}")
    return files


def build_bundle(*, tier: str, uri: Path, out_root: Path,
                 kb_root: Path, sim_root: Path) -> Path:
    if tier not in ("core", "extended"):
        raise BundleError(f"tier 必须是 core|extended，收到 {tier!r}")
    if not uri.is_dir():
        raise BundleError(f"数据层不存在：{uri}（先跑 migrate_data_layer.py --apply）")

    present = table_names(uri)
    if tier == "core":
        wanted = list(CORE_TABLES)
        missing = [t for t in CORE_TABLES if t not in present]
        if missing:
            raise BundleError(f"core 缺表：{missing}（实际 {present}）")
    else:
        missing = [t for t in CORE_TABLES if t not in present]
        if missing:
            raise BundleError(f"extended 必须包含 core 全部表，缺：{missing}")
        wanted = present

    cfg = select_embedding()
    if cfg["model_name"] != EMBEDDING_MODEL_EXPECTED:
        raise BundleError(
            f"嵌入模型口径偏移：{cfg['model_name']} != {EMBEDDING_MODEL_EXPECTED}")
    if int(cfg["dim"]) != 768:
        raise BundleError(f"嵌入维度口径偏移：{cfg['dim']} != 768")

    kb_sha = kb_head(kb_root)
    engine_sha = _git(sim_root, "rev-parse", "HEAD")
    dirty = bool(_git(sim_root, "status", "--porcelain"))
    stamp = utc_now_iso_z().replace(":", "").replace("-", "")
    name = f"simpraxis-{tier}-{kb_sha[:8]}-{stamp.replace('Z', '')}Z"
    bundle = out_root / name
    if bundle.exists():
        raise BundleError(f"资产包已存在（拒绝覆盖）：{bundle}")
    # L6：存在性检查后立即把包根建成 0700，再建 tables 子目录，消除 bundle 根
    # 以 mkdir 默认权限瞬态存在的窗口。
    ensure_private_dir(bundle, mode=0o700)
    ensure_private_dir(bundle / "tables", mode=0o700)

    db = lancedb.connect(str(uri))
    tables_meta = {}
    for table in wanted:
        src_dir = uri / f"{table}.lance"
        dst_dir = bundle / "tables" / f"{table}.lance"
        files = copy_table(src_dir, dst_dir)
        rows = db.open_table(table).count_rows()
        dim = table_vector_dim(uri, table)
        if dim is not None and dim != 768:
            raise BundleError(f"{table} 维度 {dim} != 768（混维度守卫）")
        tables_meta[table] = {"rows": rows, "vector_dim": dim, "files": files}
        print(f"[bundle] {tier}/{table}: {rows} 行，{len(files)} 文件")

    manifest = {
        "schema": BUNDLE_SCHEMA,
        "tier": tier,
        "created_at": utc_now_iso_z(),
        "kb_sha": kb_sha,
        "sim_praxis_sha": engine_sha,
        "sim_praxis_dirty_at_bundle": dirty,
        "embedding_model": cfg["model_name"],
        "vector_dim": int(cfg["dim"]),
        "source_uri": str(uri),
        "tables": tables_meta,
    }
    atomic_write_text(
        bundle / "bundle_manifest.json",
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        mode=0o600)
    os.chmod(bundle, 0o700)
    if dirty:
        print("[bundle] WARN：打包时引擎仓有未提交改动，sim_praxis_sha 非最终提交 SHA")
    print(f"[bundle OK] {tier} -> {bundle}")
    return bundle


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--tier", choices=("core", "extended", "both"),
                    default="both")
    ap.add_argument("--uri", type=Path, default=None)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--kb-root", type=Path,
                    default=Path(os.environ.get("KB_ROOT", str(DEFAULT_KB_ROOT))))
    ap.add_argument("--sim-praxis-root", type=Path, default=_REPO_ROOT)
    args = ap.parse_args()
    uri = args.uri or Path(default_uri())
    tiers = ("core", "extended") if args.tier == "both" else (args.tier,)
    try:
        for tier in tiers:
            build_bundle(tier=tier, uri=uri, out_root=args.out,
                         kb_root=args.kb_root, sim_root=args.sim_praxis_root)
    except BundleError as exc:
        print(f"[BUNDLE STOP] {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
