#!/usr/bin/env python3
"""核验 tour_blueprint.json：canonical 4 段；observed 4 项目；每条 evidence 文件真实存在；
sha 与本地克隆一致。任何臆造路径都会在这里失败。"""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOURS = Path(os.environ.get("SIMPRAXIS_TOURS_DIR", "/mnt/assets/simpraxis/tours"))
bp = json.loads((ROOT / "templates/tour_blueprint.json").read_text(encoding="utf-8"))

errors = []

# 全 JSON 递归扫描：任何遗留的 __...__ 占位符（含中文提示）都判失败
import re as _re
_PLACEHOLDER = _re.compile(r"__\S.*?__")

def _walk(node, path="$"):
    if isinstance(node, dict):
        for k, v in node.items():
            _walk(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            _walk(v, f"{path}[{i}]")
    elif isinstance(node, str) and _PLACEHOLDER.search(node):
        errors.append(f"blueprint 残留占位符 {path}: {node[:50]}")

_walk(bp)

spine = bp["canonical_spine"]
if [s["stage_id"] for s in spine] != ["Stage 0", "Stage 1", "Stage 2", "Stage 3"]:
    errors.append("canonical_spine 必须恰好是 Stage 0~3")
for s in spine:
    if not s.get("exit_criteria") or not s.get("required_artifacts"):
        errors.append(f"{s['stage_id']} 缺 exit_criteria/required_artifacts")

maps = bp["observed_stage_maps"]
if set(maps) != {"ge_sim_v2", "genesis_world", "lw_benchhub", "genie_sim_v3"}:
    errors.append(f"observed_stage_maps 键集合错误：{set(maps)}")

for slug, info in maps.items():
    if info.get("clone_status") != "ok":
        errors.append(f"{slug} clone_status 非 ok：禁止在未核实情况下产出范式")
        continue
    local = TOURS / info["repo"].split("/")[-1]
    if not local.exists():
        errors.append(f"{slug} 本地克隆不存在：{local}")
        continue
    sha = subprocess.run(["git", "-C", str(local), "rev-parse", "HEAD"],
                         capture_output=True, text=True, check=True).stdout.strip()
    if info.get("commit") in (None, "", "__FILL__"):
        errors.append(f"{slug} blueprint commit 仍是占位符，必须填克隆真实 sha")
    elif info["commit"] != sha:
        errors.append(f"{slug} blueprint commit {info['commit']} 与克隆 {sha} 不一致")
    stages = info.get("stages", [])
    if not stages:
        errors.append(f"{slug} 没有 stages 映射")
    for st in stages:
        if not isinstance(st, dict):
            errors.append(f"{slug} stage 必须是对象（疑似未替换的模板字符串）：{str(st)[:40]}")
            continue
        for ev in st.get("evidence", []):
            # README 锚点形式 README.md#xxx 只校验文件部分
            rel = ev.split("#", 1)[0]
            if rel and not (local / rel).exists():
                errors.append(f"{slug} 证据文件不存在：{rel}（stage={st.get('observed_id')})")

if errors:
    print("[blueprint FAIL]")
    for e in errors:
        print(" -", e)
    sys.exit(1)
print(f"[blueprint OK] 4 个项目、{sum(len(m['stages']) for m in maps.values())} 个 stage 证据全部存在")
