#!/usr/bin/env python3
"""Task0b 资产包对账：复算文件 sha256/size + 开表行数/维度 + kb/引擎 SHA 交叉核对。

用法：
  python scripts/kb/verify_bundle.py <bundle 目录> [--kb-root ...]
                                     [--sim-praxis-root ...]
                                     [--engine-allow-ancestor]

校验项（全过 exit 0；内容不符 exit 1；用法/缺件硬错 exit 2）：
  1. manifest schema/tier/嵌入模型/768 维；core 表集恰为 kb_chunks+
     phase1_learnings，extended 必须包含 core；
  2. 包内文件集与 manifest 逐项 sha256/size 一致，无多无缺，无未登记软链；
  3. 直接打开包里的 .lance 表：行数、vector 维度逐项等于 manifest；
  4. --kb-root 给定时：记录的 kb_sha 是当前 HEAD 祖先（内容层只增不改），
     且 knowledge/ 哈希集通过 kb_pins；
  5. --sim-praxis-root 给定时：sim_praxis_sha == 当前 HEAD（默认严格）；
     引擎后续提交后可用 --engine-allow-ancestor 降为祖先关系校验。

只读校验，不触网、0 付费。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import lancedb  # noqa: E402

from memory_flywheel import kb_pins  # noqa: E402

BUNDLE_SCHEMA = "simpraxis.bundle_manifest.v1"
EMBEDDING_MODEL_EXPECTED = "sentence-transformers/paraphrase-multilingual-mpnet-base-v2"
CORE_TABLES = ("kb_chunks", "phase1_learnings")


class BundleVerifyError(RuntimeError):
    """硬错（缺件/用法）：exit 2。"""


def _sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True)


def verify_bundle(bundle: Path, *, kb_root: Path | None = None,
                  sim_root: Path | None = None,
                  engine_allow_ancestor: bool = False) -> list[str]:
    """返回内容不符项列表（空=全过）；缺件/非法包直接 raise BundleVerifyError。"""
    problems: list[str] = []
    manifest_path = bundle / "bundle_manifest.json"
    if not bundle.is_dir():
        raise BundleVerifyError(f"资产包目录不存在：{bundle}")
    if not manifest_path.is_file():
        raise BundleVerifyError(f"缺 bundle_manifest.json：{manifest_path}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BundleVerifyError(f"manifest 非法 JSON：{exc}") from exc

    if manifest.get("schema") != BUNDLE_SCHEMA:
        problems.append(f"schema != {BUNDLE_SCHEMA}：{manifest.get('schema')!r}")
    tier = manifest.get("tier")
    if tier not in ("core", "extended"):
        problems.append(f"tier 非法：{tier!r}")
    if manifest.get("embedding_model") != EMBEDDING_MODEL_EXPECTED:
        problems.append(f"embedding_model != mpnet：{manifest.get('embedding_model')!r}")
    if manifest.get("vector_dim") != 768:
        problems.append(f"vector_dim != 768：{manifest.get('vector_dim')!r}")

    tables = manifest.get("tables")
    if not isinstance(tables, dict) or not tables:
        raise BundleVerifyError("manifest tables 缺失或为空")
    table_names = sorted(tables)
    if tier == "core" and table_names != sorted(CORE_TABLES):
        problems.append(f"core 包表集必须恰为 {sorted(CORE_TABLES)}，实际 {table_names}")
    if tier == "extended" and any(t not in table_names for t in CORE_TABLES):
        problems.append(f"extended 包缺 core 表：{sorted(set(CORE_TABLES) - set(table_names))}")

    # --- 文件集 + sha256/size 复算 ---
    tables_dir = bundle / "tables"
    for table, meta in tables.items():
        tdir = tables_dir / f"{table}.lance"
        if not tdir.is_dir():
            problems.append(f"缺表目录：{tdir.relative_to(bundle)}")
            continue
        listed = {f["relpath"]: f for f in meta.get("files", [])}
        actual = {}
        for p in sorted(tdir.rglob("*")):
            if p.is_symlink():
                problems.append(f"出现未登记软链：{p.relative_to(bundle)}")
                continue
            if p.is_file():
                actual[p.relative_to(tdir).as_posix()] = p
        for rel in sorted(set(listed) - set(actual)):
            problems.append(f"{table}: manifest 登记文件缺失：{rel}")
        for rel in sorted(set(actual) - set(listed)):
            problems.append(f"{table}: 出现未登记文件：{rel}")
        for rel, p in actual.items():
            item = listed.get(rel)
            if item is None:
                continue
            size = p.stat().st_size
            if item.get("size") != size:
                problems.append(f"{table}/{rel}: size {size} != {item.get('size')}")
            digest = _sha256_file(p)
            if item.get("sha256") != digest:
                problems.append(f"{table}/{rel}: sha256 不符（{digest[:12]}）")

    # --- 直接开表：行数 + 维度 ---
    if tables_dir.is_dir():
        db = lancedb.connect(str(tables_dir))
        for table, meta in tables.items():
            if table not in db.table_names():
                continue
            opened = db.open_table(table)
            rows = opened.count_rows()
            if rows != meta.get("rows"):
                problems.append(f"{table}: 行数 {rows} != manifest {meta.get('rows')}")
            schema = opened.schema
            if "vector" in schema.names:
                dim = int(schema.field("vector").type.list_size)
                if dim != meta.get("vector_dim"):
                    problems.append(
                        f"{table}: 维度 {dim} != manifest {meta.get('vector_dim')}")
                if dim != 768:
                    problems.append(f"{table}: 维度 {dim} != 768（混维度）")

    # --- kb_sha 交叉核对：记录点必须是当前 HEAD 祖先（只增不改）---
    recorded_kb = manifest.get("kb_sha")
    if kb_root is not None:
        if not isinstance(recorded_kb, str) or len(recorded_kb) != 40:
            problems.append(f"kb_sha 非法：{recorded_kb!r}")
        elif not (kb_root / ".git").exists():
            raise BundleVerifyError(f"--kb-root 不是 git 仓：{kb_root}")
        elif not kb_pins.is_descendant(recorded_kb, kb_root):
            problems.append(
                f"kb_sha {recorded_kb[:12]} 不是当前 HEAD 的祖先（内容层历史分叉？）")
        else:
            problems.extend(kb_pins.check_knowledge_hash_set(kb_root))

    # --- 引擎 SHA：默认严格等于当前 HEAD ---
    recorded_engine = manifest.get("sim_praxis_sha")
    if sim_root is not None:
        if not isinstance(recorded_engine, str) or len(recorded_engine) != 40:
            problems.append(f"sim_praxis_sha 非法：{recorded_engine!r}")
        elif not (sim_root / ".git").exists():
            raise BundleVerifyError(f"--sim-praxis-root 不是 git 仓：{sim_root}")
        else:
            cur = _git(sim_root, "rev-parse", "HEAD").stdout.strip()
            if cur == recorded_engine:
                pass
            elif engine_allow_ancestor:
                if _git(sim_root, "merge-base", "--is-ancestor",
                        recorded_engine, "HEAD").returncode != 0:
                    problems.append(
                        f"sim_praxis_sha {recorded_engine[:12]} 不是当前 HEAD 祖先")
            else:
                problems.append(
                    f"sim_praxis_sha {recorded_engine[:12]} != 当前 HEAD {cur[:12]}"
                    "（引擎已前进；确认无误可 --engine-allow-ancestor）")

    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("bundle", type=Path)
    ap.add_argument("--kb-root", type=Path, default=None)
    ap.add_argument("--sim-praxis-root", type=Path, default=None)
    ap.add_argument("--engine-allow-ancestor", action="store_true")
    args = ap.parse_args()
    try:
        problems = verify_bundle(
            args.bundle, kb_root=args.kb_root, sim_root=args.sim_praxis_root,
            engine_allow_ancestor=args.engine_allow_ancestor)
    except BundleVerifyError as exc:
        print(f"[VERIFY HARD ERROR] {exc}", file=sys.stderr)
        return 2
    if problems:
        print(f"[verify FAIL] {args.bundle}：{len(problems)} 项不符：")
        for p in problems:
            print(f"  - {p}")
        return 1
    print(f"[verify OK] {args.bundle}：文件/行数/维度/SHA 对账全过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
