#!/usr/bin/env python3
"""把 embodied-ai-sim-knowledge-base 的 knowledge/**/*.md 全量切块写入 LanceDB。

用法：
  python -m memory_flywheel.scripts.ingest_kb --rebuild   # 首次/换模型
  python -m memory_flywheel.scripts.ingest_kb             # 幂等：文件级替换
  python -m memory_flywheel.scripts.ingest_kb --dry-run   # 只统计不写库
只读 KB 仓库，绝不修改它。
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

from memory_flywheel.kb_pins import KNOWLEDGE_BASELINE_SHA
from memory_flywheel.vector_engine import VectorEngine, make_record

KB_ROOT = Path(os.environ.get("KB_ROOT", "/home/vipuser/embodied-ai-sim-knowledge-base"))
# Task0b 回填：旧单点字面量已迁为 memory_flywheel.kb_pins 的分层钉点
# （knowledge/ 32-md 哈希集 + HEAD≥迁移提交）。保留别名供历史调用方导入，
# 值仍是 knowledge 树基线提交 aac53313；单真值在 kb_pins，禁止再抄字面量。
KB_BASELINE_SHA = KNOWLEDGE_BASELINE_SHA
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
FENCE_RE = re.compile(r"^\s*(?:```|~~~)")
EVID_RE = re.compile(r"(?<![A-Za-z0-9])([PDLQ])(\d{2})(?!\d)")
# Qxx 原子节：匹配拼接 section_path 的首个标题或任意 "> " 之后的标题（M2）
Q_ENTRY_RE = re.compile(r"(?:^|>\s*)Q\d{2}\b")
Q_HEADING_RE = re.compile(r"^Q\d{2}\b")
TARGET_CHARS, HARD_CHARS = 1200, 2000

GLOBAL_DOC_TYPES = {
    "common_reproduction_guide": "guide",
    "common_issues_solutions": "troubleshooting",
    "toolchain_comparison": "comparison",
    "best_practices": "practices",
    "terminology_mapping": "terminology",
}
PROJECT_DOC_TYPES = {
    "quickstart": "quickstart",
    "background_knowledge": "background",
    "ai_knowledge": "ai",
    "troubleshooting": "troubleshooting",
    "code_knowledge": "code",
    "00-index": "index",
}


def classify(rel: str):
    # B3：两分支统一以无后缀 stem 查键——旧版 global 分支用带 .md 的 name
    # 去查无后缀字典键，5 个全局文件全部落到 fallback，DOC_TYPES 覆盖断言必红。
    parts = Path(rel).parts
    name = Path(rel).name
    stem = name[:-3] if name.endswith(".md") else name
    if rel == "knowledge/00-index.md":
        return "root", "global", "root_index"
    if parts[1] == "global":
        if stem == "00-index":
            return "global", "global", "index"
        return "global", "global", GLOBAL_DOC_TYPES.get(stem, stem)
    if parts[1] == "projects":
        if rel == "knowledge/projects/00-index.md":
            return "project", "global", "index"
        slug = parts[2]
        return "project", slug, PROJECT_DOC_TYPES.get(stem, stem)
    raise ValueError(f"未分类路径：{rel}")


def is_troubleshooting(name: str) -> bool:
    return name in ("troubleshooting.md", "common_issues_solutions.md")


def parse_sections(lines: list[str]):
    """按 h1~h3 切节；代码围栏内不识别标题。返回 [(section_path, [行...]), ...]"""
    sections, stack, current = [], [], None
    in_fence = False

    def push():
        nonlocal current
        if current is not None and any(x.strip() for x in current["lines"]):
            sections.append((current["path"], current["lines"]))
        current = None

    for line in lines:
        m = None if in_fence else HEADING_RE.match(line)
        if m and len(m.group(1)) <= 3:
            push()
            level = len(m.group(1))
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, m.group(2).strip()))
            current = {"path": " > ".join(t for _, t in stack), "lines": []}
            continue
        if FENCE_RE.match(line):
            in_fence = not in_fence
        if current is None:
            current = {"path": "(文首)", "lines": []}
        current["lines"].append(line)
    push()
    return sections


def split_paragraphs(lines: list[str]):
    groups, buf, in_fence = [], [], False
    for line in lines:
        if FENCE_RE.match(line):
            in_fence = not in_fence
            buf.append(line)
            continue
        if not line.strip() and not in_fence:
            if buf:
                groups.append("\n".join(buf).strip("\n"))
                buf = []
        else:
            buf.append(line)
    if buf:
        groups.append("\n".join(buf).strip("\n"))
    return [g for g in groups if g.strip()]


_TABLE_SEP_RE = re.compile(r"^\s*\|?[\s:|-]*-[\s:|-]*$")


def _table_header_above(lines: list[str], idx: int):
    """idx 行处于某张 markdown 表内时，返回该表的（表头, 分隔）两行，否则 None。"""
    j = idx - 1
    while j >= 0 and lines[j].lstrip().startswith("|"):
        if j - 1 >= 0 and lines[j - 1].lstrip().startswith("|") and _TABLE_SEP_RE.match(lines[j]):
            return lines[j - 1], lines[j]
        j -= 1
    return None


def _split_oversized(para: str, head_budget: int = HARD_CHARS) -> list[str]:
    """单个"无空行段落"超过可用预算时的硬切（长表/长代码树/连续行）。

    split_paragraphs 只按空行分段，连续的 markdown 表/目录树是一整段，旧逻辑在
    cur 为空时无法触发 HARD_CHARS，4186 字的 P01–P16 表会整块入库、语义被稀释。
    这里按行向 TARGET_CHARS 打包：表格续片重复表头两行（保持列含义），围栏续片
    补开/闭围栏（保持代码块完整）；任何产出片加上章节头后都不得超过 HARD_CHARS。
    head_budget = HARD_CHARS - len(章节头)，让 ~1990 字的段落也提前切开。
    """
    if len(para) <= head_budget:
        return [para]
    lines = para.split("\n")
    pack_target = min(TARGET_CHARS, head_budget)
    pieces: list[str] = []
    buf: list[str] = []
    buf_len = 0
    in_fence = False
    fence_open = ""

    def flush():
        nonlocal buf, buf_len
        if not buf:
            return
        pieces.append("\n".join(buf))
        buf, buf_len = [], 0

    for i, line in enumerate(lines):
        stripped = line.lstrip()
        is_fence_line = bool(FENCE_RE.match(line))
        if is_fence_line and not in_fence:
            # 开围栏：前面若已攒散文行且超限，先成片；开围栏本身属于新片
            if buf and buf_len + len(line) + 1 > pack_target:
                flush()
            buf.append(line)
            buf_len += len(line) + 1
            in_fence, fence_open = True, line
            continue
        if is_fence_line and in_fence:
            # 闭围栏始终留在当前片尾部
            buf.append(line)
            buf_len += len(line) + 1
            in_fence, fence_open = False, ""
            continue
        if len(line) > head_budget and not buf:
            # 没有空白可切的超长单行（当前 KB 不存在）：字符级兜底，保证加上章节头不超硬上限
            for k in range(0, len(line), head_budget):
                pieces.append(line[k:k + head_budget])
            continue
        if buf and buf_len + len(line) + 1 > pack_target:
            if in_fence:
                buf.append("```")        # 续片前先闭合当前围栏
                flush()
                buf.append(fence_open)   # 续片以同一围栏重开
                buf_len = len(fence_open) + 1
            else:
                flush()
                if stripped.startswith("|"):
                    hdr = _table_header_above(lines, i)
                    if hdr:
                        buf.extend(hdr)
                        buf_len = len(hdr[0]) + len(hdr[1]) + 2
        buf.append(line)
        buf_len += len(line) + 1
    if in_fence:
        buf.append("```")
    flush()
    assert all(len(piece) <= head_budget for piece in pieces), "硬切后仍超 head_budget"
    return pieces


def chunk_section(path: str, lines: list[str], atomic: bool):
    text = "\n".join(lines).strip()
    if not text:
        return []
    header = f"【章节】{path}\n"
    if atomic:
        return [header + text]
    chunks, cur = [], ""
    for para in split_paragraphs(lines):
        for piece in _split_oversized(para, HARD_CHARS - len(header)):
            if not cur:
                cand = header + piece
            else:
                cand = cur + "\n\n" + piece
            if len(cand) <= TARGET_CHARS or not cur:
                cur = cand
            else:
                chunks.append(cur)
                cur = header + piece
            if len(cur) > HARD_CHARS:
                chunks.append(cur)
                cur = ""
    if cur:
        chunks.append(cur)
    return chunks


def ingest_file(kb_root: Path, rel: str, source_commit: str):
    layer, project, doc_type = classify(rel)
    namespace = "troubleshooting" if is_troubleshooting(Path(rel).name) else "capabilities"
    lines = (kb_root / rel).read_text(encoding="utf-8").splitlines()
    records = []
    idx = 0
    for section_path, sec_lines in parse_sections(lines):
        # M2：section_path 是标题栈拼接串（如 "... > lw_benchhub > Q02 EGL..."），
        # 旧实现 re.match(r"^Q\d{2}\b") 对拼接串永不命中 -> Q 条目被错误二次切分。
        atomic = bool(Q_ENTRY_RE.search(section_path))
        for chunk in chunk_section(section_path, sec_lines, atomic):
            refs = sorted({f"{a}{b}" for a, b in EVID_RE.findall(chunk)})
            records.append(make_record(
                kb_relpath=rel, chunk_index=idx, text=chunk, namespace=namespace,
                project=project, layer=layer, doc_type=doc_type,
                section_path=section_path, evidence_refs=refs,
                source_commit=source_commit))
            idx += 1
    return records


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kb-root", default=str(KB_ROOT))
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    kb_root = Path(args.kb_root)
    assert (kb_root / "knowledge").is_dir(), f"KB 路径异常：{kb_root}"
    # 只读纪律：KB 必须干净
    dirty = subprocess.run(["git", "-C", str(kb_root), "status", "--porcelain"],
                           capture_output=True, text=True, check=True).stdout.strip()
    assert not dirty, "KB 仓库不干净，拒绝在只读阶段继续"
    commit = subprocess.run(["git", "-C", str(kb_root), "rev-parse", "HEAD"],
                            capture_output=True, text=True, check=True).stdout.strip()
    # Task0b 回填：v4.1 M3 旧断言「HEAD 全 SHA == aac53313」在三层分离后改为
    # 分层不变量——HEAD 必须是迁移提交（solutions/ 首次写入）的后代，且
    # knowledge/ 32 个 md 的 sha256 集仍逐字等于 aac53313 基线（只增不改，
    # 新增 md 须在 kb_pins.KNOWLEDGE_ADDITIONS 显式登记）。
    from memory_flywheel import kb_pins
    pin_errors = kb_pins.check_layered_pins(kb_root)
    assert not pin_errors, "KB 分层不变量校验失败，拒绝入库：\n" + "\n".join(pin_errors)

    files = sorted(str(p.relative_to(kb_root))
                   for p in (kb_root / "knowledge").rglob("*.md"))
    if args.limit:
        files = files[: args.limit]
    print(f"[ingest] {len(files)} 个文件，KB commit={commit}")

    # 适配（计划 §12.3 main 原文为先构造后 drop）：跨维度换模型（512→768）时
    # VectorEngine.__init__ 的维度守卫会直接 RuntimeError，使 --rebuild 无法执行；
    # 故 rebuild 在构造引擎前用不加载嵌入的静态方法删旧表，守卫对其他路径不变。
    if args.rebuild:
        dropped = VectorEngine.drop_existing()
        print(f"[ingest] rebuild：旧表已删除={dropped}")
    eng = VectorEngine()
    if args.rebuild:
        print("[ingest] 已整表重建")
    if not args.dry_run:
        eng.table(create=True)

    total = 0
    ns_counter, type_counter, proj_counter = Counter(), Counter(), Counter()
    for rel in files:
        records = ingest_file(kb_root, rel, commit)
        if not records:
            print(f"[ingest WARN] {rel} 未产出任何 chunk")
        if not args.dry_run:
            eng.replace_by_relpaths(records, [rel])
        for r in records:
            total += 1
            ns_counter[r["namespace"]] += 1
            type_counter[r["doc_type"]] += 1
            proj_counter[r["project"]] += 1
        print(f"  {rel}: {len(records)} chunks")

    print(f"[ingest] 合计 {total} chunks")
    print("  namespace:", dict(ns_counter))
    print("  doc_type :", dict(type_counter))
    print("  project  :", dict(proj_counter))
    if not args.dry_run:
        assert eng.count() >= total or args.limit  # 库里可能还有未涉及文件
    return 0


if __name__ == "__main__":
    sys.exit(main())
