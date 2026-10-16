#!/usr/bin/env python3
"""Task0b 数据层迁移：lancedb 从 simpraxis 数据根同盘 mv 到 KB data/ 实体树。

默认（实跑，lead 在引擎仓提交后执行）：
  source scripts/00_env.sh
  /mnt/venvs/simpraxis/bin/python scripts/kb/migrate_data_layer.py --apply

语义（计划 §Task0b 数据迁移侧，逐条对应）：
  1. 源默认 /mnt/artifacts/simpraxis/lancedb，目的实体树
     /mnt/artifacts/embodied-kb/data（lancedb/blobs/bundles，0700），
     另在 $KB_ROOT/data 建软链；data/ 已被 KB .gitignore 忽略；
  2. stat 设备号断言源/目同盘（st_dev 相等，等价 `stat -c %d`）；
     跨设备拒绝 mv 并 STOP（禁止隐形跨盘复制）；
  3. 迁移前清单：kb_chunks=1542、phase1_learnings=5、文件列表+大小、
     3 条固定查询的召回 id 快照（mpnet，复用缓存不二次下载）；
  4. 同盘 rename（os.rename，秒级，不留旧路径双写）；迁移后行数与召回
     id 快照逐项一致才写 data/manifest.json（kb_sha、sim_praxis_sha、
     嵌入模型、768 维、每表行数、召回快照）；
  5. 幂等重入：源已不存在而目的有 manifest 时进入只验模式（重新对账）。

无 --apply 为 dry-run（只做清单与设备检查，不动文件系统布局）。
本脚本只允许操作调用方显式给出的路径；实跑红线见项目 CLAUDE.md 规则 5/6。
"""
from __future__ import annotations

import argparse
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

DEFAULT_SRC = Path("/mnt/artifacts/simpraxis/lancedb")
DEFAULT_DATA_ROOT = Path("/mnt/artifacts/embodied-kb/data")
MANIFEST_NAME = "manifest.json"
INVENTORY_NAME = "migration_pre_inventory.json"

# 3 条固定召回快照查询（与 tests/milestone1/test_learning_isolation.py 同源，
# 迁移前后 id 必须逐项一致；过滤器也同 test_04）。
SNAPSHOT_QUERIES = [
    ("q1_yaml_override",
     ("YAML 配置文件为什么会静默覆盖命令行参数？我在命令行传 --device cuda:0 "
      "为什么还是跑在 CPU 上？"),
     {"namespace": "capabilities", "project": "lw_benchhub", "top_k": 3}),
    ("q2_egl_black_screen",
     "headless 无显示器，EGL 离屏渲染初始化失败黑屏怎么排查？",
     {"namespace": "troubleshooting", "top_k": 5}),
    ("q3_cuda_illegal_address",
     "Isaac Sim 里 cudaErrorIllegalAddress 非法显存地址在什么情况下发生？",
     {"namespace": "capabilities", "top_k": 5}),
]


class MigrationError(RuntimeError):
    """迁移前置/对账失败：必须 STOP，不允许自动回滚之外的处置。"""


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True)


def engine_head_and_dirty() -> tuple[str, bool]:
    sha = _git(_REPO_ROOT, "rev-parse", "HEAD").stdout.strip()
    dirty = bool(_git(_REPO_ROOT, "status", "--porcelain").stdout.strip())
    return sha, dirty


def table_rows(uri: Path, table: str) -> int | None:
    db = lancedb.connect(str(uri))
    if table not in db.table_names():
        return None
    return db.open_table(table).count_rows()


def vector_dim(uri: Path, table: str = "kb_chunks") -> int:
    db = lancedb.connect(str(uri))
    return db.open_table(table).schema.field("vector").type.list_size


def recall_snapshot(uri: Path) -> dict:
    """3 条固定查询的命中 id 列表（延迟导入 VectorEngine：构造即加载嵌入配置）。"""
    from memory_flywheel.vector_engine import VectorEngine
    eng = VectorEngine(uri=str(uri))
    snap: dict[str, list[str]] = {}
    for name, query, kwargs in SNAPSHOT_QUERIES:
        hits = eng.query_kb(query, **kwargs)
        snap[name] = [h["id"] for h in hits]
    return snap


def file_inventory(uri: Path) -> dict:
    files = []
    total = 0
    for p in sorted(uri.rglob("*")):
        if p.is_file():
            size = p.stat().st_size
            total += size
            files.append({"relpath": p.relative_to(uri).as_posix(), "size": size})
    return {"file_count": len(files), "total_bytes": total, "files": files}


def collect_state(uri: Path, *, with_recall: bool) -> dict:
    rows = {
        "kb_chunks": table_rows(uri, "kb_chunks"),
        "phase1_learnings": table_rows(uri, "phase1_learnings"),
        "solution_chunks": table_rows(uri, "solution_chunks"),
    }
    state = {"rows": rows, "vector_dim": vector_dim(uri),
             "inventory": file_inventory(uri)}
    if with_recall:
        state["recall_snapshot"] = recall_snapshot(uri)
    return state


def assert_expected_rows(state: dict, *, expect_kb: int, expect_learn: int) -> None:
    rows = state["rows"]
    problems = []
    if rows["kb_chunks"] != expect_kb:
        problems.append(f"kb_chunks={rows['kb_chunks']} != 期望 {expect_kb}")
    if rows["phase1_learnings"] != expect_learn:
        problems.append(
            f"phase1_learnings={rows['phase1_learnings']} != 期望 {expect_learn}")
    if state["vector_dim"] != int(select_embedding()["dim"]):
        problems.append(
            f"vector_dim={state['vector_dim']} != 嵌入配置 {select_embedding()['dim']}")
    if problems:
        raise MigrationError("迁移前清单不符，STOP：" + "；".join(problems))


def same_device_required(src: Path, data_root: Path) -> None:
    ensure_private_dir(data_root, mode=0o700)
    if src.stat().st_dev != data_root.stat().st_dev:
        raise MigrationError(
            f"跨设备拒绝 mv：src dev={src.stat().st_dev} "
            f"dst dev={data_root.stat().st_dev}（计划要求同盘 rename；"
            "确认 --src/--data-root 均在 /mnt 同一块设备后再跑）")


def link_data_root(kb_root: Path, data_root: Path) -> None:
    link = kb_root / "data"
    if link.is_symlink():
        cur = os.readlink(link)
        if Path(cur).resolve() == data_root.resolve():
            print(f"[migrate] 软链已存在且指向正确：{link} -> {cur}")
            return
        raise MigrationError(f"{link} 是软链但指向 {cur}（期望 {data_root}）")
    if link.exists():
        raise MigrationError(f"{link} 已作为实体存在（必须是软链；请人工核对后处置）")
    kb_root.mkdir(parents=True, exist_ok=True)
    os.symlink(data_root, link)
    # 锁内适配（Task0b 彩排实证）：git 拒绝穿越软链的 pathspec（check-ignore
    # data/lancedb 对软链直接 fatal），且尾斜杠 data/ 规则不匹配软链本身；
    # 正确闸口是对软链路径 data 本身 check-ignore（.gitignore 须为锚定 /data）。
    ci = _git(kb_root, "check-ignore", "data")
    if ci.returncode != 0:
        raise MigrationError("data 软链未被 .gitignore 命中（拒绝留下被跟踪风险）")
    print(f"[migrate] 软链：{link} -> {data_root}（check-ignore data 命中）")


def _write_manifest(data_root: Path, payload: dict) -> Path:
    path = data_root / MANIFEST_NAME
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2,
                                       sort_keys=True) + "\n", mode=0o600)
    return path


def migrate(*, src: Path, data_root: Path, kb_root: Path,
            expect_kb: int, expect_learn: int, do_link: bool) -> dict:
    if not src.is_dir():
        raise MigrationError(f"源库不存在：{src}")
    dst = data_root / "lancedb"
    if dst.exists():
        # 锁内适配（Task0b）：lancedb.connect() 对不存在的 uri 会自动 mkdir（已实测），
        # 迁移前任何默认 URI 构造（如全量收集时的模块级 VectorEngine()）都会在内容层
        # 留下空 data/lancedb 残留。仍不代删（fail-closed），只对空目录残留给出精确处置。
        empty_residue = dst.is_dir() and not any(dst.iterdir())
        hint = ("；空目录是 lancedb.connect() 预建残留（0 字节），核对后执行 "
                f"rmdir {dst} {data_root}（仅当两者皆空）即可"
                if empty_residue else "（拒绝合并/覆盖；人工核对后处置）")
        raise MigrationError(f"目的已存在：{dst}{hint}")

    same_device_required(src, data_root)
    print(f"[migrate] 同盘确认（st_dev={src.stat().st_dev}）")
    pre = collect_state(src, with_recall=True)
    assert_expected_rows(pre, expect_kb=expect_kb, expect_learn=expect_learn)
    for name, ids in pre["recall_snapshot"].items():
        print(f"[migrate] 快照 {name}: {len(ids)} 命中")
    pre_path = data_root / INVENTORY_NAME
    atomic_write_text(
        pre_path,
        json.dumps({"captured_at": utc_now_iso_z(), "src": str(src),
                    **pre}, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        mode=0o600)

    # blobs/bundles 0700（lancedb 子目录由 mv 产生，不能预建，否则 mv 落进空目录）
    ensure_private_dir(data_root / "blobs", mode=0o700)
    ensure_private_dir(data_root / "bundles", mode=0o700)

    try:
        os.rename(src, dst)
    except OSError as exc:
        raise MigrationError(f"rename 失败：{exc}（源未移动，无残留）") from exc
    print(f"[migrate] rename：{src} -> {dst}")
    if src.exists():
        raise MigrationError(f"旧路径仍存在，疑似双写：{src}")

    post = collect_state(dst, with_recall=True)
    problems = []
    if post["rows"] != pre["rows"]:
        problems.append(f"行数变化：{pre['rows']} -> {post['rows']}")
    if post["recall_snapshot"] != pre["recall_snapshot"]:
        problems.append("召回 id 快照迁移前后不一致")
    if post["inventory"]["file_count"] != pre["inventory"]["file_count"] \
            or post["inventory"]["total_bytes"] != pre["inventory"]["total_bytes"]:
        problems.append("文件数/总字节迁移前后不一致")
    if problems:
        raise MigrationError("迁移后对账失败，STOP（同盘 mv 回滚命令见脚本 docstring）："
                             + "；".join(problems))

    cfg = select_embedding()
    engine_sha, dirty = engine_head_and_dirty()
    manifest = {
        "schema": "simpraxis.data_manifest.v1",
        "migrated_at": utc_now_iso_z(),
        "kb_sha": kb_head(kb_root),
        "sim_praxis_sha": engine_sha,
        "sim_praxis_dirty_at_migration": dirty,
        "embedding_model": cfg["model_name"],
        "vector_dim": post["vector_dim"],
        "tables": post["rows"],
        "recall_snapshot": post["recall_snapshot"],
        "file_inventory": {"file_count": post["inventory"]["file_count"],
                           "total_bytes": post["inventory"]["total_bytes"]},
        "src": str(src),
        "dst": str(dst),
    }
    _write_manifest(data_root, manifest)
    print(f"[migrate] manifest：{data_root / MANIFEST_NAME}（0600）")
    if dirty:
        print("[migrate] WARN：迁移时引擎仓有未提交改动，sim_praxis_sha 非最终提交 SHA")
    if do_link:
        link_data_root(kb_root, data_root)
    print("[migrate OK] 行数/召回/文件清单全部一致")
    return manifest


def verify_only(*, src: Path, data_root: Path) -> dict:
    dst = data_root / "lancedb"
    manifest_path = data_root / MANIFEST_NAME
    if src.exists():
        raise MigrationError(f"重入但旧源仍在：{src}（拒绝双写状态）")
    if not dst.is_dir() or not manifest_path.is_file():
        raise MigrationError(f"重入验证缺目的库或 manifest：{dst} / {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    state = collect_state(dst, with_recall=True)
    problems = []
    if state["rows"] != manifest["tables"]:
        problems.append(f"行数偏离 manifest：{state['rows']} != {manifest['tables']}")
    if state["recall_snapshot"] != manifest["recall_snapshot"]:
        problems.append("召回 id 快照偏离 manifest")
    if state["vector_dim"] != manifest["vector_dim"]:
        problems.append("vector_dim 偏离 manifest")
    if problems:
        raise MigrationError("重入对账失败：" + "；".join(problems))
    print("[migrate VERIFY OK] 目的库行数/召回/维度与 manifest 一致")
    return manifest


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--src", type=Path, default=DEFAULT_SRC)
    ap.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    ap.add_argument("--kb-root", type=Path, default=DEFAULT_KB_ROOT)
    ap.add_argument("--expect-kb-rows", type=int, default=1542)
    ap.add_argument("--expect-learnings", type=int, default=5)
    ap.add_argument("--apply", action="store_true", help="实际执行迁移；缺省 dry-run")
    ap.add_argument("--no-link", action="store_true", help="不建 $KB_ROOT/data 软链")
    args = ap.parse_args()

    try:
        if args.src.exists():
            if not args.apply:
                same_device_required(args.src, args.data_root)
                state = collect_state(args.src, with_recall=True)
                assert_expected_rows(state, expect_kb=args.expect_kb_rows,
                                     expect_learn=args.expect_learnings)
                print(f"[dry-run] 清单通过：{state['rows']} dim={state['vector_dim']} "
                      f"files={state['inventory']['file_count']}；"
                      f"将 rename {args.src} -> {args.data_root / 'lancedb'}")
                return 0
            migrate(src=args.src, data_root=args.data_root, kb_root=args.kb_root,
                    expect_kb=args.expect_kb_rows, expect_learn=args.expect_learnings,
                    do_link=not args.no_link)
        else:
            verify_only(src=args.src, data_root=args.data_root)
    except MigrationError as exc:
        print(f"[MIGRATION STOP] {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
