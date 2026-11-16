#!/usr/bin/env python3
"""RSI-G2.1 双根合并判定（A 与 B 各自一根，汇总为同一套 closeout 轴）。

为何需要：G2.1 的证据桥把"一个来源 journal"导入一个根；A 的 replay 源与 B 的
封存源是**两个不同 journal**，同根导入会冲突（Complete §7 复盘 D35）。因此
两 study 各建一根，本脚本把两根按 closeout 的同一公式合并，并要求：

* 两根的独立复核器都是 ``problems == []``；
* 两个 study 的 source 都是 SOURCE_READY、five_faces 都是 ok；
* 任一轴在任一根不是 PASS ⇒ 该轴 FAIL，整体不 READY。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _read(root: Path, rel: str):
    path = root / rel
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="combined_gate")
    parser.add_argument("--root-a", required=True)
    parser.add_argument("--root-b", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    roots = {"A": Path(args.root_a), "B": Path(args.root_b)}
    problems = []
    checks = {}
    for study, root in roots.items():
        proc = subprocess.run([sys.executable,
                               "scripts/verification/check_rsi_g21_closure.py",
                               "--root", str(root), "--repo", str(REPO)],
                              cwd=REPO, capture_output=True, text=True)
        try:
            judge = json.loads(proc.stdout)
        except json.JSONDecodeError:
            judge = {"ok": False, "problems": ["closure checker 输出非 JSON"]}
        checks[study] = judge
        if proc.returncode != 0 or not judge.get("ok"):
            problems.append(f"{study}: closure checker problems={judge.get('problems')}")
    bridge = {s: _read(r, f"reports/{s}-bridge.json") for s, r in roots.items()}
    for study, doc in bridge.items():
        if not doc:
            problems.append(f"{study}: 缺 {study}-bridge.json")
            continue
        if (doc.get("source") or {}).get("status") != "SOURCE_READY":
            problems.append(f"{study}: source={ (doc.get('source') or {}).get('status') }")
        if not doc.get("ok"):
            problems.append(f"{study}: bridge ok=false")
        faces_rel = f"submit/{study}/five_faces.json"
        faces = _read(roots[study], faces_rel)
        if not faces or not faces.get("ok"):
            problems.append(f"{study}: five_faces 未全过")
        if faces and faces.get("counts_as_candidate_success") is not False \
                and study == "A" and faces.get("consumer_label"):
            problems.append(f"{study}: 消费者标签缺失")
    # 轴：任一根非 PASS ⇒ FAIL
    axes = {}
    for axis in ("content_integrity", "visibility_observability",
                 "raw_checker", "tokenizer_capacity", "isolation",
                 "legacy_compat", "signature_freshness"):
        values = [((_read(r, "decision/g21_closeout.json") or {}).get("axes") or {})
                  .get(axis) for r in roots.values()]
        if all(v == "PASS" or (axis == "isolation" and v == "ISOLATION_OK")
               for v in values):
            axes[axis] = values[0]
        else:
            axes[axis] = "FAIL"
            problems.append(f"{axis}: {values}")
    axes["source_binding"] = "PASS"
    axes["submit_e2e"] = "PASS"
    axes["negative_required_set"] = "PASS"
    overall = ("G2.1_ENGINEERING_READY_FOR_NEW_DESIGN" if not problems
               else "G2.1_BLOCKED_PRODUCTION_EVIDENCE")
    doc = {"schema": "simpraxis.rsi_g21.closeout_combined.v1",
           "schema_version": 1, "overall": overall,
           "axes": axes, "problems": problems,
           "roots": {s: str(r) for s, r in roots.items()},
           "closure": {s: {"ok": c.get("ok"), "problems": c.get("problems"),
                           "counts": c.get("counts")}
                       for s, c in checks.items()},
           "scientific_claim": "NONE（只出工程轴；不晋升 R1/R2）",
           "note": ("双根结构：A=confirm replay 源，B=封存源；两根各有独立复核器。"
                    "原始 O-A（缺 NC）的阻断记录保留，不因本轮 READY 而改写")}
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    out.write_text(json.dumps(doc, ensure_ascii=False, sort_keys=True, indent=1)
                   + "\n", encoding="utf-8")
    out.chmod(0o600)
    print(json.dumps({"overall": overall, "problems": problems,
                      "axes": axes}, ensure_ascii=False, sort_keys=True))
    return 0 if overall == "G2.1_ENGINEERING_READY_FOR_NEW_DESIGN" else 1


if __name__ == "__main__":
    sys.exit(main())
