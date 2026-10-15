#!/usr/bin/env python3
"""能力矩阵机器校验：引擎齐全、证据文件存在、证据等级合法、absent 项必须 KB 零命中。

锁内适配（Task7，2026-10-15 用户书面批准「[实践]证据扩展」）：新增 coverage
值 phase1_probed——KB 零提及但已有 Phase1 0 付费一手探针证据的引擎（sapien）。
其 fact 证据规则与 KB 二手证据互斥：
- level [实践]：必须用 repo_relpath 指仓内证据索引（文件真实存在且不得越出
  仓库根），禁止再配 kb_relpath（不允许把一手探针伪装成 KB 引用）；
- 其余 level（[CODE]/[PAPER]/…）：规则不变，kb_relpath 必须落只读 KB 内，
  禁止配 repo_relpath（二手知识不允许用仓内文件冒充出处）。
KB 只读钉死（meta.kb_commit == KB HEAD）与 absent 词边界零命中规则不变。
"""
import os
import re
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MATRIX = ROOT / "memory_flywheel" / "capability_matrix.yaml"
KB = Path(os.environ.get("KB_ROOT", "/home/vipuser/embodied-ai-sim-knowledge-base"))

LEVELS = {"[CODE]", "[PAPER]", "[README]", "[官网]", "[文章]", "[实践]", "[推断]"}
PRACTICE_LEVEL = "[实践]"
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


def kb_keyword_hits(slug: str, kb_root: Path = KB) -> int:
    alts = "|".join(ENGINE_KEYWORDS[slug])
    rx = re.compile(rf"(?<![A-Za-z0-9_])(?:{alts})(?![A-Za-z0-9_])", re.IGNORECASE)
    total = 0
    for p in (kb_root / "knowledge").rglob("*.md"):  # m7：只扫 knowledge/，不扫仓内其他 md
        total += len(rx.findall(p.read_text(encoding="utf-8", errors="replace")))
    return total


def check_fact_evidence(ev: dict, kb_root: Path = KB) -> "str | None":
    """校验单条 fact 的证据出处；返回错误字符串，None 表示通过。

    [实践] 与 KB 二手证据互斥（见模块 docstring），两路都做真实文件存在性
    与越界检查——无证据文件的 fact 一律不允许进矩阵。
    """
    level = ev.get("level")
    rp_repo = (ev.get("repo_relpath") or "").strip()
    rp_kb = (ev.get("kb_relpath") or "").strip()
    if level == PRACTICE_LEVEL:
        if rp_kb:
            return f"[实践] 一手证据禁止配 kb_relpath（不得伪装 KB 引用）：{rp_kb}"
        if not rp_repo:
            return "[实践] fact 必须提供 repo_relpath（仓内一手证据索引）"
        target = (ROOT / rp_repo).resolve()
        try:
            target.relative_to(ROOT.resolve())
        except ValueError:
            return f"[实践] repo_relpath 越出仓库根：{rp_repo} → {target}"
        if not target.is_file():
            return f"[实践] 证据文件不存在：{rp_repo}"
        return None
    if rp_repo:
        return (f"level {level} 属 KB 二手证据，禁止配 repo_relpath（二手知识"
                f"不得用仓内文件冒充出处）：{rp_repo}")
    if not rp_kb:
        return f"level {level} fact 必须提供 kb_relpath"
    if not (kb_root / rp_kb).is_file():
        return f"证据文件不存在：{rp_kb}"
    return None


def validate(matrix_path: "str | Path" = DEFAULT_MATRIX,
             kb_root: "str | Path" = KB) -> "list[str]":
    """机器校验矩阵，返回错误列表（空列表 = 通过）。"""
    matrix_path = Path(matrix_path)
    kb_root = Path(kb_root)
    m = yaml.safe_load(matrix_path.read_text(encoding="utf-8"))
    errors: list[str] = []
    kb_commit = m.get("meta", {}).get("kb_commit", "")
    if kb_commit in ("", "__FILL__"):
        errors.append("meta.kb_commit 未填写")
    else:
        real = subprocess.run(["git", "-C", str(kb_root), "rev-parse", "HEAD"],
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
            hits = kb_keyword_hits(slug, kb_root)
            if hits != 0:
                errors.append(f"{slug} 标为 absent 但 KB 词边界 grep 命中 {hits} 处（m7：不允许假 absent）")
            continue
        facts = eng.get("facts", [])
        if not facts:
            errors.append(f"{slug} 非 absent 却没有 facts")
        for f in facts:
            ev = f.get("evidence") or {}
            if ev.get("level") not in LEVELS:
                errors.append(f"{slug} 非法证据等级：{ev.get('level')}")
                continue
            if not ev.get("locator"):
                errors.append(f"{slug} fact 缺 locator：{f.get('claim', '')[:30]}")
            err = check_fact_evidence(ev, kb_root)
            if err:
                errors.append(f"{slug} {err}")
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
    return errors


def main() -> int:
    errors = validate()
    if errors:
        print("[matrix FAIL]")
        print("\n".join(" - " + e for e in errors))
        return 1
    m = yaml.safe_load(DEFAULT_MATRIX.read_text(encoding="utf-8"))
    n = sum(len(e.get("facts", [])) for e in m["engines"])
    print(f"[matrix OK] {len(m['engines'])} 个引擎，{n} 条带证据事实")
    return 0


if __name__ == "__main__":
    sys.exit(main())
