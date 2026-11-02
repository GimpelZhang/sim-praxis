#!/usr/bin/env python3
"""Task 6 Stage B（r4）：四表**向量身份**机产器（只读、0 付费、确定性）。

为什么需要单独一件（GATE1-LANE-A C-1）：窗口前/后快照的逐行归约哈希只覆盖
``text`` / 元数据 / 戳记，**不覆盖 ``vector`` 列**。因此「文本与 id 全等、只有
向量被重算」这种写入在写入面比对里看起来是干净的——§8 条件 4 的「嵌入身份
不变」就成了空断言。本脚本按**同一个可复算口径**给四张表各产出一个
``vector_set_sha256``：

    对每张表：按 ``str(id)`` 升序排列全部行，
    对每行依次 update ``str(id).encode("utf-8")`` 与
    ``numpy.asarray(vector, dtype="<f4").tobytes()``（Little-Endian float32，
    与 LanceDB 内部存储同宽），整个表一个 sha256。

口径冻结、不含时间戳，因此同一活库连续两次运行必须逐字节相同；窗口前取一次
作基线，取证时再算一次比对——任何向量变化（含同维重嵌入）都会改变集合哈希。

0 下载（离线三开关 setdefault）、只读（不建表/不写库/不调网关）。
用法：
  python scripts/phase25/gen_task6_r4_vector_identity.py [--out PATH] [--check PATH]
退出码：0=成功；2=前置不满足（缺表/维度不一致/缺 id/重复 id）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

for _var in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_DATASETS_OFFLINE"):
    os.environ.setdefault(_var, "1")

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import numpy as np  # noqa: E402

TABLES = ("kb_chunks", "solution_chunks", "phase1_learnings",
          "phase2_learnings")
SCHEMA = "simpraxis.phase25.task6_r4_vector_identity.v1"
EXPECTED_DIM = 768


class IdentityError(RuntimeError):
    """活库不满足身份前置（判据前失败，exit 2）。"""


def vector_set_sha256(pairs: "list[tuple[str, list[float]]]") -> str:
    """按冻结口径给一组 ``(id, vector)`` 计算集合哈希（纯函数，可单测）。"""
    h = hashlib.sha256()
    for rid, vec in sorted(pairs, key=lambda p: str(p[0])):
        h.update(str(rid).encode("utf-8"))
        h.update(np.asarray(vec, dtype="<f4").tobytes())
    return h.hexdigest()


def table_identity(tbl, name: str) -> dict:
    rows = (tbl.search(None).select(["id", "vector"]).limit(10 ** 6).to_list())
    if not rows:
        raise IdentityError(f"{name} 零行（无法作身份断言）")
    pairs: list[tuple[str, list[float]]] = []
    dims = set()
    for r in rows:
        if r.get("id") is None:
            raise IdentityError(f"{name} 存在无 id 的行（身份口径要求 id 非空）")
        vec = r.get("vector")
        if vec is None:
            raise IdentityError(f"{name} 存在无向量的行（{r.get('id')}）")
        dims.add(len(vec))
        pairs.append((str(r["id"]), vec))
    if len(dims) != 1:
        raise IdentityError(f"{name} 行间维度不一致：{sorted(dims)}")
    # 重复 id 会让集合哈希依赖行序：`sorted` 对同 id 是稳定的，但活库扫描
    # 顺序一旦变化，两行同 id 的先后就换了，哈希帧随之漂移——既可能假阳
    # （同一数据两次不同）也可能掩盖写入（GATE1-LANE-A 第二轮 LOW-5）。
    seen: set = set()
    dup = []
    for rid, _vec in pairs:
        if rid in seen:
            dup.append(rid)
        else:
            seen.add(rid)
    if dup:
        raise IdentityError(
            f"{name} 存在重复 id（集合哈希口径无法区分同 id 多行）：{dup[:5]}")
    dim = dims.pop()
    if dim != EXPECTED_DIM:
        raise IdentityError(f"{name} 维度 {dim} ≠ 冻结值 {EXPECTED_DIM}")
    return {"rows": len(pairs), "dim": dim,
            "vector_set_sha256": vector_set_sha256(pairs)}


def build(uri: "str | None" = None) -> dict:
    import lancedb
    from memory_flywheel.kb_pins import default_lancedb_uri
    db = lancedb.connect(uri or default_lancedb_uri())
    present = set(db.table_names())
    missing = [n for n in TABLES if n not in present]
    if missing:
        raise IdentityError(f"活库缺表：{missing}")
    tables = {n: table_identity(db.open_table(n), n) for n in TABLES}
    return {"schema": SCHEMA,
            "note": ("四表向量集合哈希（id + LE float32 字节），窗口前基线与"
                     "取证时比对；口径见模块头，无时间戳、可逐字节复算。"),
            "tables": tables,
            "total_rows": sum(t["rows"] for t in tables.values())}


def _dump(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=None, help="输出 json 路径（不写则只打印）")
    ap.add_argument("--check", default=None,
                    help="对基线件逐字节复核（不出新文件）")
    args = ap.parse_args(argv)
    try:
        payload = build()
    except IdentityError as exc:
        print(f"[T6R4-VEC][STOP] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    text = _dump(payload)
    if args.check:
        target = Path(args.check)
        want = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if _sha256(target) != want:
            print(f"[T6R4-VEC][STOP] 基线件逐字节不符：{target}", file=sys.stderr)
            return 2
        print(f"[T6R4-VEC] check ok {target}")
        return 0
    for name, node in payload["tables"].items():
        print(f"{name:18s} rows={node['rows']:5d} dim={node['dim']} "
              f"vector_set_sha256={node['vector_set_sha256']}")
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(f"out={out} sha256={_sha256(out)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
