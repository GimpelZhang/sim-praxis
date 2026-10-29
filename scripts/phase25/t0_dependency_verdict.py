#!/usr/bin/env python3
"""Phase 2.5 Task0 依赖探针裁决构造器（AC-T0-3，0 付费、纯本地解析）。

本模块【不含任何网络代码】：只解析 ``pip install --dry-run --report`` 的 JSON
报告与 freeze/pip-check 快照，构造裁决件 t0_dependency_verdict.json。
实际 pip 调用（清华源 dry-run/真装）由同目录 t0_dependency_probe.sh 执行。

裁决规则（Plan Task0 / 交接件原文）：
- smolagents==1.26.0 全依赖 dry-run 的 would-install 集合中，若五个关键 pin
  （numpy/torch/pydantic/langgraph/lark-oapi）一个都不动 → 主 venv 零 extra 真装；
- 任一关键 pin 会被安装/升降级 → 独立 /mnt/venvs/simpraxis-agentic（py3.10）；
- 六个轻包（huggingface-hub/requests/rich/jinja2/pillow/python-dotenv）逐项记录
  其 requires_dist 约束与现装版本、是否被动；轻包变动只告警，不构成换轨条件；
- openai-agents==0.24.0 永远只做 dry-run，绝不安装，冲突包明细留档。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

# 仓内模块（utc_now_iso_z）：脚本位于 scripts/phase25/，仓根在上两级。
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from orchestrator.common.clock import utc_now_iso_z  # noqa: E402
from orchestrator.common.fsutil import atomic_write_text  # noqa: E402 L6：0600 单一源

TARGET_PACKAGE = "smolagents"
TARGET_VERSION = "1.26.0"
OPENAI_AGENTS_SPEC = "openai-agents==0.24.0"
TSINGHUA_INDEX = "https://pypi.tuna.tsinghua.edu.cn/simple"
VERDICT_SCHEMA = "simpraxis.phase25.t0_dependency_verdict.v1"

# 五个关键 pin（硬规则 8：不松版本；任一被动即必须换独立 venv）。
FIVE_PINS: dict[str, str] = {
    "numpy": "1.26.4",
    "torch": "2.4.1+cu124",
    "pydantic": "2.11.10",
    "langgraph": "0.2.28",
    "lark-oapi": "1.7.4",
}
# 六个轻包（smolagents 直接依赖；逐项记录，不作为换轨判据）。
SIX_LIGHTS: tuple[str, ...] = (
    "huggingface-hub", "requests", "rich", "jinja2", "pillow", "python-dotenv",
)


def canonical(name: str) -> str:
    """PEP 503 归一化：小写，-_. 连续串统一为 '-'。"""
    return re.sub(r"[-_.]+", "-", name).lower()


FIVE_PINS_NORM = {canonical(k): k for k in FIVE_PINS}
SIX_LIGHTS_NORM = {canonical(k): k for k in SIX_LIGHTS}


def load_report(path: str | Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def would_install(report: dict[str, Any]) -> list[dict[str, str]]:
    """pip --report JSON 的 install 数组 → 排序后的 {name, canonical, version}。"""
    out: list[dict[str, str]] = []
    for item in report.get("install", []):
        meta = item.get("metadata", {})
        name = str(meta.get("name", ""))
        out.append({"name": name, "canonical": canonical(name),
                    "version": str(meta.get("version", ""))})
    return sorted(out, key=lambda d: d["canonical"])


def requires_dist_for(report: dict[str, Any], canon: str) -> list[str]:
    """从报告内【各安装项】元数据取 requires_dist 中指向某依赖的原始约束串。

    pip --report 的 install[] 只列将安装的包；--no-deps 时只有目标包
    smolagents 一项，其 requires_dist 里才写着对六轻包的约束——不能按安装项
    自身名字过滤（那会永远匹配不到依赖项本身）。
    """
    specs: list[str] = []
    for item in report.get("install", []):
        meta = item.get("metadata", {})
        for req in meta.get("requires_dist", []) or []:
            # 只取无环境标记的主依赖（extra == "..." 条件不计入主解析面）。
            if ";" not in req and canonical(
                    re.split(r"[<>=!~\[;( ]", req, 1)[0]) == canon:
                specs.append(req.strip())
    return sorted(set(specs))


def parse_freeze(path: str | Path) -> dict[str, str]:
    """pip freeze 文本 → {canonical_name: 原始版本串}（忽略 -e / 注释/空行）。"""
    out: dict[str, str] = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("-e ") or "==" not in line:
            continue
        name, _, ver = line.partition("==")
        out[canonical(name)] = ver.strip()
    return out


def parse_pip_check(path: str | Path) -> tuple[int, list[str]]:
    """pip check 输出 → (rc 由调用方另行传入, 去重排序的告警行)。

    rc 从文件名旁侧参数给（pip check 无 rc 信息于输出中）；本函数只解析文本。
    """
    lines = [ln.strip() for ln in Path(path).read_text(encoding="utf-8").splitlines()
             if ln.strip()]
    return 0, sorted(set(lines))


def _freeze_diff(before: dict[str, str], after: dict[str, str]) -> dict[str, list[str]]:
    # L3（2026-10-29 评审修单）：added 必须显式 k not in before——版本变更的
    # 包在 before.get(k) != v 下会被 added/changed 双列。
    added = sorted(f"{FIVE_PINS_NORM.get(k, k)}=={v}" for k, v in after.items()
                   if k not in before)
    removed = sorted(f"{FIVE_PINS_NORM.get(k, k)}=={v}" for k, v in before.items()
                     if k not in after)
    changed = sorted(f"{FIVE_PINS_NORM.get(k, k)}: {before.get(k)} -> {after.get(k)}"
                     for k in before.keys() & after.keys()
                     if before[k] != after[k])
    return {"added": added, "removed": removed, "changed": changed}


def decide(full_report: dict[str, Any]) -> dict[str, Any]:
    """关键 pin 漂移裁决：would-install 集合 ∩ 五 pin 为空 → main，否则 isolated。"""
    drift = [d for d in would_install(full_report) if d["canonical"] in FIVE_PINS_NORM]
    return {"branch": "isolated" if drift else "main_venv", "pin_drift": drift}


def build_verdict(*, nodeps_report_path: str | Path, full_report_path: str | Path,
                  openai_report_path: str | Path, freeze_before_path: str | Path,
                  freeze_after_path: str | Path, pipcheck_before_path: str | Path,
                  pipcheck_after_path: str | Path, pipcheck_before_rc: int,
                  pipcheck_after_rc: int, install: dict[str, Any],
                  interpreter: str, probe_dir: str | Path) -> dict[str, Any]:
    nodeps = load_report(nodeps_report_path)
    full = load_report(full_report_path)
    openai = load_report(openai_report_path)
    before = parse_freeze(freeze_before_path)
    after = parse_freeze(freeze_after_path)

    nodeps_set = would_install(nodeps)
    full_set = would_install(full)
    oa_set = would_install(openai)
    full_by_canon = {d["canonical"]: d for d in full_set}

    decision = decide(full)
    fdiff = _freeze_diff(before, after)

    pins: dict[str, dict[str, Any]] = {}
    for canon, display in FIVE_PINS_NORM.items():
        would = full_by_canon.get(canon)
        pins[display] = {
            "pinned": FIVE_PINS[display],
            "installed_before": before.get(canon),
            "installed_after": after.get(canon),
            "in_full_dry_run_would_install": would is not None,
            "would_install_version": would["version"] if would else None,
            "unchanged": before.get(canon) == after.get(canon) == FIVE_PINS[display],
        }

    lights: dict[str, dict[str, Any]] = {}
    for canon, display in SIX_LIGHTS_NORM.items():
        would = full_by_canon.get(canon)
        lights[display] = {
            "requires_dist": requires_dist_for(nodeps, canon),
            "installed_before": before.get(canon),
            "installed_after": after.get(canon),
            "in_full_dry_run_would_install": would is not None,
            "would_install_version": would["version"] if would else None,
            "unchanged": before.get(canon) == after.get(canon),
        }

    _, check_before_lines = parse_pip_check(pipcheck_before_path)
    _, check_after_lines = parse_pip_check(pipcheck_after_path)
    before_set, after_set = set(check_before_lines), set(check_after_lines)

    oa_conflicts = []
    for d in oa_set:
        if d["canonical"] in FIVE_PINS_NORM:
            display = FIVE_PINS_NORM[d["canonical"]]
            oa_conflicts.append({
                "package": display, "pinned": FIVE_PINS[display],
                "installed_before": before.get(d["canonical"]),
                "would_install": d["version"],
                "breaks_pin": d["version"] != FIVE_PINS[display],
            })

    target_present = canonical(TARGET_PACKAGE) in after
    return {
        "schema": VERDICT_SCHEMA,
        "ts_iso": utc_now_iso_z(),
        "ark_paid_calls": 0,
        "index_url": TSINGHUA_INDEX,
        "interpreter": interpreter,
        "probe_dir": str(probe_dir),
        "target": {"name": TARGET_PACKAGE, "version": TARGET_VERSION,
                   "install_mode": "zero-extras (--no-deps)",
                   "present_after": target_present},
        "dry_run": {
            "smolagents_no_deps": {"would_install": nodeps_set},
            "smolagents_full": {"would_install": full_set},
            "openai_agents": {"spec": OPENAI_AGENTS_SPEC,
                              "would_install": oa_set,
                              "installed": False,
                              "note": "仅 dry-run 留档，按 Task0 合同永不安装"},
        },
        "six_lights": lights,
        "five_pins": pins,
        "decision": {
            "branch": decision["branch"],
            "reason": ("全依赖 dry-run 的 would-install 不含任何关键 pin"
                       if decision["branch"] == "main_venv"
                       else "全依赖 dry-run 将改动关键 pin，主 venv 禁止，换独立 venv"),
            "pin_drift": decision["pin_drift"],
        },
        "install": install,
        "freeze_diff": fdiff,
        "pip_check": {
            "before_rc": pipcheck_before_rc,
            "after_rc": pipcheck_after_rc,
            "before_lines": check_before_lines,
            "after_lines": check_after_lines,
            "new_issues": sorted(after_set - before_set),
            "resolved": sorted(before_set - after_set),
            "identical": before_set == after_set,
        },
        "openai_agents_conflicts": oa_conflicts,
    }


def _write_atomic_0600(path: Path, doc: dict[str, Any]) -> None:
    # L6：统一走 fsutil.atomic_write_text（mkstemp 0600+fsync+replace 的 G-7
    # 单一源），不再自造 tmp/chmod/replace。
    atomic_write_text(
        path, json.dumps(doc, ensure_ascii=False, indent=2) + "\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="构造 Task0 依赖探针裁决件（纯本地解析）")
    ap.add_argument("--nodeps-report", required=True)
    ap.add_argument("--full-report", required=True)
    ap.add_argument("--openai-report", required=True)
    ap.add_argument("--freeze-before", required=True)
    ap.add_argument("--freeze-after", required=True)
    ap.add_argument("--pipcheck-before", required=True)
    ap.add_argument("--pipcheck-after", required=True)
    ap.add_argument("--pipcheck-before-rc", type=int, required=True)
    ap.add_argument("--pipcheck-after-rc", type=int, required=True)
    ap.add_argument("--interpreter", required=True)
    ap.add_argument("--probe-dir", required=True)
    ap.add_argument("--install-performed", action="store_true")
    ap.add_argument("--install-already-present", action="store_true")
    ap.add_argument("--install-cmd", default="")
    ap.add_argument("--install-rc", type=int)
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)

    install = {
        "performed": bool(args.install_performed),
        "already_present_correct_version": bool(args.install_already_present),
        "cmd": args.install_cmd,
        "rc": args.install_rc,
    }
    verdict = build_verdict(
        nodeps_report_path=args.nodeps_report,
        full_report_path=args.full_report,
        openai_report_path=args.openai_report,
        freeze_before_path=args.freeze_before,
        freeze_after_path=args.freeze_after,
        pipcheck_before_path=args.pipcheck_before,
        pipcheck_after_path=args.pipcheck_after,
        pipcheck_before_rc=args.pipcheck_before_rc,
        pipcheck_after_rc=args.pipcheck_after_rc,
        install=install, interpreter=args.interpreter,
        probe_dir=args.probe_dir)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    _write_atomic_0600(out, verdict)
    print(json.dumps({"branch": verdict["decision"]["branch"],
                      "pin_drift": verdict["decision"]["pin_drift"],
                      "target_present": verdict["target"]["present_after"],
                      "pip_check_identical": verdict["pip_check"]["identical"],
                      "out": str(out)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
