#!/usr/bin/env python3
"""能力矩阵机器校验：引擎齐全、证据文件存在、证据等级合法、absent 项必须 KB 零命中。"""
import os
import re
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
KB = Path(os.environ.get("KB_ROOT", "/home/vipuser/embodied-ai-sim-knowledge-base"))
m = yaml.safe_load((ROOT / "memory_flywheel/capability_matrix.yaml").read_text(encoding="utf-8"))

LEVELS = {"[CODE]", "[PAPER]", "[README]", "[官网]", "[文章]", "[实践]", "[推断]"}
REQUIRED = {"mujoco", "physx_isaac", "genesis", "newton", "sapien", "bullet"}
# m7：absent 判定必须用词边界在 KB knowledge/ 目录实测零命中（范围与入库/纪律口径
# 一致——只扫 knowledge/，不含仓库根的 README/索引等 knowledge/ 外文件）；
# 例如 panda_bullet 中的 bullet 不得算命中（前导 _ 被边界排除），bulletproof 同理。
ENGINE_KEYWORDS = {
    "mujoco": ["mujoco"],
    "physx_isaac": [r"physx", r"isaac\s*sim"],
    "newton": ["newton"],
    "sapien": ["sapien"],
    "bullet": ["bullet"],
}


def kb_keyword_hits(slug: str) -> int:
    alts = "|".join(ENGINE_KEYWORDS[slug])
    rx = re.compile(rf"(?<![A-Za-z0-9_])(?:{alts})(?![A-Za-z0-9_])", re.IGNORECASE)
    total = 0
    for p in (KB / "knowledge").rglob("*.md"):  # m7：只扫 knowledge/，不扫仓内其他 md
        total += len(rx.findall(p.read_text(encoding="utf-8", errors="replace")))
    return total


errors = []
kb_commit = m.get("meta", {}).get("kb_commit", "")
if kb_commit in ("", "__FILL__"):
    errors.append("meta.kb_commit 未填写")
else:
    real = subprocess.run(["git", "-C", str(KB), "rev-parse", "HEAD"],
                          capture_output=True, text=True)
    if real.returncode != 0 or kb_commit.strip() != real.stdout.strip():
        errors.append(f"meta.kb_commit {kb_commit} 与 KB HEAD 不一致")
slugs = set()
for eng in m.get("engines", []):
    slug = eng.get("slug")
    slugs.add(slug)
    if eng.get("coverage") == "absent":
        if not any("未提及" in g for g in eng.get("gaps", [])):
            errors.append(f"{slug} absent 但 gaps 未标 未提及/待 Phase1 补")
        hits = kb_keyword_hits(slug)
        if hits != 0:
            errors.append(f"{slug} 标为 absent 但 KB 词边界 grep 命中 {hits} 处（m7：不允许假 absent）")
        continue
    facts = eng.get("facts", [])
    if not facts:
        errors.append(f"{slug} 非 absent 却没有 facts")
    for f in facts:
        ev = f.get("evidence") or {}
        rp = ev.get("kb_relpath", "")
        if not (KB / rp).is_file():
            errors.append(f"{slug} 证据文件不存在：{rp}")
        if ev.get("level") not in LEVELS:
            errors.append(f"{slug} 非法证据等级：{ev.get('level')}")
        if not ev.get("locator"):
            errors.append(f"{slug} fact 缺 locator：{f.get('claim','')[:30]}")
if slugs != REQUIRED:
    errors.append(f"引擎集合不符：缺 {REQUIRED - slugs}，多 {slugs - REQUIRED}")
# 覆盖度下限（防止用一两条事实凑数）
counts = {e["slug"]: len(e.get("facts", [])) for e in m["engines"]}
if counts.get("genesis", 0) < 8:
    errors.append("genesis facts 少于 8 条，提炼不充分")
if counts.get("physx_isaac", 0) < 5:
    errors.append("physx_isaac facts 少于 5 条")
if counts.get("mujoco", 0) < 2:
    errors.append("mujoco facts 少于 2 条")
if errors:
    print("[matrix FAIL]")
    print("\n".join(" - " + e for e in errors))
    sys.exit(1)
print(f"[matrix OK] {len(slugs)} 个引擎，{sum(counts.values())} 条带证据事实")
