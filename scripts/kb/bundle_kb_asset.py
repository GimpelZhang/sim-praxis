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
打包时 select_embedding() 的 embedding_model/vector_dim、每表行数。

封存注（Phase 2.5 Task6 Stage A 换嵌入，2026-10-31）：2026-10-17 的六份
历史资产包为 mpnet/768（embedding_model=sentence-transformers/
paraphrase-multilingual-mpnet-base-v2，vector_dim=768），其 manifest 自
描述、verify_bundle 按 manifest 值对账，永久可验；换嵌入 Qwen3-0.6B/1024
后新包记录新模型/新维度，代码不再硬钉 mpnet/768，混维度守卫改为「每表维度
== 打包时当前嵌入 config dim」。

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
CORE_TABLES = ("kb_chunks", "phase1_learnings")
DEFAULT_OUT = Path("/mnt/artifacts/embodied-kb/data/bundles")

# —— ProductBundle v1（S0-8 / WP-P）：闭源三 tier + 公开内容快照 ——
PRODUCT_TIERS = ("core", "extended", "product")
PRODUCT_CLOSED_TIERS = ("research_memory", "archives", "promoted_params")
CONTENT_TREES = ("knowledge", "solutions")   # KB 公开面（脚本+知识文档）
DEFAULT_CLOSED_ROOT = Path("/mnt/artifacts/simpraxis/s0_closed")


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


def copy_tree_private(src: Path, dst: Path, *, label: str,
                      allow_empty: bool = True) -> list[dict]:
    """复制闭源/内容树（拒绝软链）；返回 relpath/size/sha256 清单。

    与 copy_table 的区别：允许空目录（S0 闭源 archive 尚无内容时如实记 0 件），
    但绝不跟随软链——软链既可能越界，也可能把闭源实体带进公开面。
    """
    files: list[dict] = []
    if src.is_symlink():
        raise BundleError(f"{label}: 源目录是软链（拒绝穿透）：{src}")
    if not src.is_dir():
        raise BundleError(f"{label}: 源目录不存在：{src}")
    for p in sorted(src.rglob("*")):
        rel = p.relative_to(src).as_posix()
        if p.is_symlink():
            raise BundleError(f"{label}: 拒绝把软链打入资产包：{rel}")
        if any(part in PRODUCT_CLOSED_TIERS for part in Path(rel).parts):
            raise BundleError(f"{label}: 路径含闭源 tier 名：{rel}")
        if not p.is_file():
            continue
        target = dst / rel
        ensure_private_dir(target.parent, mode=0o700)
        shutil.copyfile(p, target)
        os.chmod(target, 0o600)
        digest = _sha256_file(target)
        if _sha256_file(p) != digest:
            raise BundleError(f"{label}: 复制字节不一致：{rel}")
        files.append({"relpath": rel, "size": p.stat().st_size, "sha256": digest})
    if not files and not allow_empty:
        raise BundleError(f"{label}: 空目录拒绝打包：{src}")
    return files


def _closed_file_shas(closed: dict) -> set[str]:
    return {f["sha256"] for meta in closed.values() for f in meta.get("files", [])}


def assert_content_clean(content_files: list[dict], closed_shas: set[str]) -> None:
    """防泄机判：公开内容面不得含闭源 tier 路径或闭源文件字节（sha 撞库即拒）。"""
    for entry in content_files:
        rel = entry["relpath"]
        if any(part in PRODUCT_CLOSED_TIERS for part in Path(rel).parts):
            raise BundleError(f"内容面出现闭源 tier 路径：{rel}")
        if entry["sha256"] in closed_shas:
            raise BundleError(f"内容面出现闭源文件字节（sha256 相同）：{rel}")


def build_bundle(*, tier: str, uri: Path, out_root: Path,
                 kb_root: Path, sim_root: Path,
                 closed_root: Path | None = None) -> Path:
    if tier not in PRODUCT_TIERS:
        raise BundleError(f"tier 必须是 core|extended|product，收到 {tier!r}")
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
            raise BundleError(f"{tier} 必须包含 core 全部表，缺：{missing}")
        wanted = present

    # 锁内适配（Phase 2.5 换嵌入）：不再硬钉 mpnet/768；manifest 记录打包时
    # 当前 config 的模型/维度，混维度守卫＝每表维度 == config dim（见下）。
    cfg = select_embedding()
    if not isinstance(cfg.get("model_name"), str) or not cfg["model_name"]:
        raise BundleError(f"嵌入 config 缺 model_name：{cfg!r}")
    config_dim = int(cfg["dim"])
    if config_dim <= 0:
        raise BundleError(f"嵌入 config dim 非法：{cfg.get('dim')!r}")

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
    try:
        return _fill_bundle(bundle=bundle, tier=tier, uri=uri, wanted=wanted,
                            kb_root=kb_root, kb_sha=kb_sha, engine_sha=engine_sha,
                            dirty=dirty, cfg=cfg, config_dim=config_dim,
                            closed_root=closed_root)
    except BundleError:
        # 失败即删本函数刚创建的自有目录，拒绝留下半成品包（评审 LOW）
        shutil.rmtree(bundle, ignore_errors=True)
        raise


def _fill_bundle(*, bundle: Path, tier: str, uri: Path, wanted: list[str],
                 kb_root: Path, kb_sha: str, engine_sha: str, dirty: bool,
                 cfg: dict, config_dim: int,
                 closed_root: Path | None) -> Path:
    db = lancedb.connect(str(uri))
    tables_meta = {}
    for table in wanted:
        src_dir = uri / f"{table}.lance"
        dst_dir = bundle / "tables" / f"{table}.lance"
        files = copy_table(src_dir, dst_dir)
        rows = db.open_table(table).count_rows()
        dim = table_vector_dim(uri, table)
        if dim is not None and dim != config_dim:
            raise BundleError(
                f"{table} 维度 {dim} != 当前嵌入 config dim {config_dim}"
                f"（混维度守卫；manifest 只记录打包时活 config）")
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
        "vector_dim": config_dim,
        "source_uri": str(uri),
        "tables": tables_meta,
    }

    # —— ProductBundle v1（WP-P）：闭源三 tier + 公开内容快照（仅 product tier）——
    if tier == "product":
        closed_root = (Path(closed_root) if closed_root is not None else Path(
            os.environ.get("SIMPRAXIS_S0_CLOSED_ROOT", str(DEFAULT_CLOSED_ROOT))))
        closed_meta: dict[str, dict] = {}
        for name in PRODUCT_CLOSED_TIERS:
            source = closed_root / name
            ensure_private_dir(bundle / "closed" / name, mode=0o700)  # 空 tier 也留目录
            files = copy_tree_private(source, bundle / "closed" / name,
                                      label=f"closed/{name}")
            closed_meta[name] = {"files": files}
            print(f"[bundle] product/closed/{name}: {len(files)} 文件")
        closed_shas = _closed_file_shas(closed_meta)

        content_meta: dict[str, dict] = {}
        for name in CONTENT_TREES:
            source = kb_root / name
            files = copy_tree_private(source, bundle / "content" / name,
                                      label=f"content/{name}", allow_empty=False)
            assert_content_clean(files, closed_shas)
            content_meta[name] = {"files": files}
            print(f"[bundle] product/content/{name}: {len(files)} 文件")

        manifest["closed_root"] = str(closed_root)
        manifest["closed_tiers"] = closed_meta
        manifest["content"] = content_meta

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
    ap.add_argument("--tier", choices=("core", "extended", "product", "both"),
                    default="both")
    ap.add_argument("--uri", type=Path, default=None)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--kb-root", type=Path,
                    default=Path(os.environ.get("KB_ROOT", str(DEFAULT_KB_ROOT))))
    ap.add_argument("--sim-praxis-root", type=Path, default=_REPO_ROOT)
    ap.add_argument("--closed-root", type=Path, default=None,
                    help="product tier 的闭源数据层根（缺省 $SIMPRAXIS_S0_CLOSED_ROOT）")
    args = ap.parse_args()
    uri = args.uri or Path(default_uri())
    tiers = ("core", "extended") if args.tier == "both" else (args.tier,)
    try:
        for tier in tiers:
            build_bundle(tier=tier, uri=uri, out_root=args.out,
                         kb_root=args.kb_root, sim_root=args.sim_praxis_root,
                         closed_root=args.closed_root)
    except BundleError as exc:
        print(f"[BUNDLE STOP] {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
