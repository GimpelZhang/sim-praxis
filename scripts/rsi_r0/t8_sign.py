#!/usr/bin/env python3
"""T8 三签写件器（零模型调用）。

用途：把某条车道的独立评审结论落成签名件 ``closeout/signs/<role>.json``，
并把它绑死在同一组活值上：报告 sha256、证据包 manifest sha256、git commit。

纪律：
- 本工具**不替车道做判断**。``--verdict`` 由车道自己给出；给 REJECT 也照写，
  只是机判会因此判不通过（这正是不许伪造签名的意思）。
- 三车道（executor / reviewer / verifier）的 signer 必须互异；reviewer 与
  verifier 的 lane.kind 必须是 ``independent_readonly``，机判逐件核对。
- 报告不入包、签名不入包（AC33）；本工具只写 ``closeout/signs/`` 下的文件。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

ROLE_LANE = {
    "executor": {"kind": "author", "readonly": False},
    "reviewer": {"kind": "independent_readonly", "readonly": True},
    "verifier": {"kind": "independent_readonly", "readonly": True},
}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def git_commit() -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(Path(__file__).resolve().parents[2]),
                          check=True, capture_output=True, text=True).stdout.strip()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="T8 三签写件器")
    ap.add_argument("--campaign", required=True)
    ap.add_argument("--role", required=True, choices=sorted(ROLE_LANE))
    ap.add_argument("--signer", required=True,
                    help="签名主体标识（三签必须互异；不得写成本仓作者冒充独立车道）")
    ap.add_argument("--verdict", required=True, choices=["APPROVE", "REJECT"])
    ap.add_argument("--findings", required=True,
                    help="该车道自己的评审件（写在 closeout/reviews/ 下），本件只记其 sha256")
    ap.add_argument("--note", default="")
    args = ap.parse_args(argv)

    campaign = Path(args.campaign).resolve()
    closeout = campaign / "closeout"
    report = closeout / "human_report.md"
    pack_manifest = closeout / "evidence_pack" / "manifest.json"
    findings = Path(args.findings).resolve()

    if not report.is_file():
        print(f"[T8-SIGN-FAIL] 报告不在场：{report}", file=sys.stderr)
        return 2
    if not pack_manifest.is_file():
        print(f"[T8-SIGN-FAIL] 包 manifest 不在场（先 --pack 再 --build）：{pack_manifest}",
              file=sys.stderr)
        return 2
    if not findings.is_file():
        print(f"[T8-SIGN-FAIL] 评审件不在场：{findings}", file=sys.stderr)
        return 2
    # 评审件必须在包外（包内不得出现判决件族）
    try:
        findings.relative_to(closeout / "evidence_pack")
        print("[T8-SIGN-FAIL] 评审件不得放进证据包（AC33 无循环）", file=sys.stderr)
        return 2
    except ValueError:
        pass

    manifest = json.loads(pack_manifest.read_text(encoding="utf-8"))
    doc = {
        "schema": "rsi_r0.sign.v1",
        "role": args.role,
        "verdict": args.verdict,
        "signer": args.signer,
        "lane": ROLE_LANE[args.role],
        "report_sha256": sha256_file(report),
        "pack_manifest_sha256": sha256_file(pack_manifest),
        "pack_n_files": manifest.get("n_files"),
        "pack_git_commit": manifest.get("git_commit"),
        "git_commit": git_commit(),
        "findings": {
            "rel": str(findings.relative_to(closeout)) if str(findings).startswith(str(closeout))
                   else str(findings),
            "sha256": sha256_file(findings),
        },
        "note": args.note,
    }
    out_dir = closeout / "signs"
    out_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    out = out_dir / f"{args.role}.json"
    tmp = out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(out)
    print(json.dumps({"written": str(out), "role": args.role, "verdict": args.verdict,
                      "report_sha256": doc["report_sha256"],
                      "pack_manifest_sha256": doc["pack_manifest_sha256"],
                      "git_commit": doc["git_commit"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
