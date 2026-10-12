#!/usr/bin/env python3
"""Qxx 原子完整性：源文件计数 == 库内整块 chunk 计数（期望 104），双向不缺不重。"""
from __future__ import annotations

import argparse
import re
from collections import Counter
from pathlib import Path

from memory_flywheel.scripts.ingest_kb import (
    HEADING_RE, Q_ENTRY_RE, classify, ingest_file, parse_sections, KB_ROOT,
)
from memory_flywheel.vector_engine import VectorEngine

# B4：不在本文件复制 Q 识别规则，也不做 path.split(">") 叶匹配——Q12 标题含
# "transformers>=4.40,<4.50"，标题内 ">" 会把叶子切成 "=4.40,..." 导致漏计。
# 源侧与入库共用同一判定 Q_ENTRY_RE.search(path)，qid 用正则从整路径提取。


def source_q_sections(kb_root: Path):
    """返回 [(rel, qid, section_path, 全部正文行拼接)]，源文件侧真相。"""
    out = []
    for p in sorted((kb_root / "knowledge").rglob("*.md")):
        rel = str(p.relative_to(kb_root))
        lines = p.read_text(encoding="utf-8").splitlines()
        for path, sec_lines in parse_sections(lines):
            if Q_ENTRY_RE.search(path):
                qid = re.search(r"(Q\d{2})\b", path).group(1)
                out.append((rel, qid, path, "\n".join(sec_lines).strip()))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kb-root", default=str(KB_ROOT))
    ap.add_argument("--expect", type=int, default=104)
    args = ap.parse_args()
    kb_root = Path(args.kb_root)

    src = source_q_sections(kb_root)
    print(f"[integrity] 源文件 Q 条目：{len(src)}（{Counter(r for r, *_ in src)}）")
    by_project = Counter(classify(r)[1] for r, *_ in src)
    print("[integrity] 按 project：", dict(by_project))
    assert len(src) == args.expect, f"源文件 Q 数 {len(src)} != 期望 {args.expect}，先核对 KB"

    eng = VectorEngine()
    tbl = eng.table().to_pandas()
    qrows = tbl[tbl["section_path"].astype(str).str.contains(Q_ENTRY_RE)]
    print(f"[integrity] 库内 Q chunk：{len(qrows)}")
    assert len(qrows) == args.expect, f"库内 Q chunk {len(qrows)} != {args.expect}（M2 回归？）"

    # 每个 (文件, Qxx) 恰好一个 chunk，且 chunk 正文完整包含源节正文
    keyed = Counter(zip(qrows["kb_relpath"], qrows["section_path"].str.extract(r"(Q\d{2})\b")[0]))
    assert all(v == 1 for v in keyed.values()), f"存在重复 Q chunk：{[k for k, v in keyed.items() if v != 1]}"
    index = {(r.kb_relpath, m.group(1)): r.text
             for r in qrows.itertuples() for m in [re.search(r"(Q\d{2})\b", r.section_path)]}
    missing = []
    for rel, qid, _path, body in src:
        text = index.get((rel, qid), "")
        probe_lines = [ln.strip() for ln in body.splitlines() if ln.strip()
                       and not ln.lstrip().startswith("#")]
        # 抽查首/中/尾各一行必须整块出现（围栏内长代码也不得被切断）
        for ln in (probe_lines[0], probe_lines[len(probe_lines) // 2], probe_lines[-1]):
            if ln and ln not in text:
                missing.append((rel, qid, ln[:60]))
    assert not missing, f"以下 Q 条目未整块落库：{missing[:5]}"
    print(f"[integrity OK] {args.expect} 个 Q 条目均整块且仅在一个 chunk 中")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
