"""RSI-R0.2 证据包构建与独立复验（计划 §10 / T8，规则在 LOCK 内）。

规则写死在这里，T8 只运行、不改语义：

* **白名单**入包：合同/公开件/观测/分析/协议/journal/槽账/预留/控制预演/gather；
  ``private/``（私有真值）与 ``quota/ledger.jsonl``（旧账族）**永不入包**。
* **判决在包外**：``closeout/judgement.json`` 不复制，manifest 也不自指
  （``manifest.json`` 只列其他件）——避免 hash 环与「最终判决被包重写」。
* 逐件 sha256、单一 manifest；拒绝符号链接、单件 >10MB、密钥正则命中。
* ``verify()`` 独立复算：逐件 sha 相符、无多余/缺失件、无符号链接、
  manifest 不包含自身。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from orchestrator.common import fsutil, secrets
from orchestrator.common.clock import utc_now_iso_z
from orchestrator.rsi_r02 import campaign as C

SCHEMA = "simpraxis.rsi_r02.evidence_pack.v1"
MANIFEST = "manifest.json"
MAX_FILE_BYTES = 10 * 1024 * 1024

INCLUDE_PREFIXES = ("contract/", "public/", "observations/", "analysis/",
                    "protocol/", "trials/", "quota/slots.json",
                    "quota/approvals/", "quota/reservations/", "t3_control/",
                    "gather/", "closeout/facts.json")
EXCLUDE_EXACT = ("closeout/judgement.json", "quota/ledger.jsonl")
EXCLUDE_PREFIXES = ("evidence_pack/", "private/", "__pycache__/")


class PackError(RuntimeError):
    """入包规则被违反（不静默跳过）。"""


def _sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _included(rel: str) -> bool:
    if rel in EXCLUDE_EXACT or rel == MANIFEST:
        return False
    if any(rel.startswith(p) for p in EXCLUDE_PREFIXES):
        return False
    if Path(rel).name.endswith((".lock", ".tmp")):
        return False
    return any(rel == p or rel.startswith(p) for p in INCLUDE_PREFIXES)


def select(ctx: C.CampaignContext,
           problems: "list[str] | None" = None) -> "list[str]":
    """白名单选件。评审 m2：符号链接**拒绝**而不是静默跳过——文档承诺「拒绝
    符号链接、不静默跳过」，静默跳过会让「包内每件都能溯源到实件」的断言
    在链上悄悄失效。给 ``problems`` 时记问题（verify 用），否则直接抛。"""
    rels: "list[str]" = []
    for path in sorted(ctx.root.rglob("*")):
        rel = str(path.relative_to(ctx.root))
        if path.is_symlink():
            if problems is None:
                raise PackError(f"campaign 内存在符号链接，拒绝打包：{rel}")
            problems.append(f"SYMLINK_IN_LIVE: {rel}")
            continue
        if not path.is_file():
            continue
        if _included(rel):
            rels.append(rel)
    return rels


def build(ctx: C.CampaignContext, *, out_rel: str = "evidence_pack"
          ) -> "dict[str, Any]":
    out_root = ctx.relative(out_rel)
    ctx.assert_write_allowed(out_root)
    problems: "list[str]" = []
    files: "dict[str, str]" = {}
    for rel in select(ctx):
        if rel == out_rel or rel.startswith(f"{out_rel}/"):
            continue
        src = ctx.root / rel
        data = src.read_bytes()
        if len(data) > MAX_FILE_BYTES:
            problems.append(f"OVERSIZE: {rel} ({len(data)} B)")
            continue
        text = data.decode("utf-8", errors="ignore")
        hits = secrets.scan_text(text)
        if hits:
            problems.append(f"SECRET: {rel} ({hits[:1]})")
            continue
        dst = out_root / rel
        fsutil.ensure_private_dir(dst.parent)
        fsutil.atomic_write_bytes(dst, data, 0o600)
        files[rel] = _sha_bytes(data)
    if problems:
        raise PackError("；".join(problems[:5]))
    manifest = {"schema": SCHEMA, "recorded_at_utc": utc_now_iso_z(),
                "campaign_id": ctx.campaign_id, "out_rel": out_rel,
                "n_files": len(files), "files": files,
                "excludes": {"exact": list(EXCLUDE_EXACT),
                             "prefixes": list(EXCLUDE_PREFIXES)},
                "note": "manifest 不自指；最终判决写在包外 closeout/judgement.json"}
    fsutil.ensure_private_dir(out_root)
    fsutil.atomic_write_text(out_root / MANIFEST,
                             json.dumps(manifest, ensure_ascii=False,
                                        indent=2, sort_keys=True) + "\n",
                             0o600)
    return manifest


def verify(ctx: C.CampaignContext, *, out_rel: str = "evidence_pack"
           ) -> "dict[str, Any]":
    out_root = ctx.relative(out_rel)
    problems: "list[str]" = []
    if not out_root.is_dir():
        return {"ok": False, "problems": [f"包目录缺失：{out_rel}"], "n_files": 0}
    for path in out_root.rglob("*"):
        if path.is_symlink():
            problems.append(f"SYMLINK: {path.relative_to(out_root)}")
        elif path.is_file() and path.stat().st_size > MAX_FILE_BYTES:
            problems.append(f"OVERSIZE: {path.relative_to(out_root)}")
    mpath = out_root / MANIFEST
    if not mpath.is_file():
        return {"ok": False, "problems": problems + ["manifest 缺失"], "n_files": 0}
    manifest = json.loads(mpath.read_text(encoding="utf-8"))
    files: Mapping[str, str] = manifest.get("files") or {}
    if MANIFEST in files:
        problems.append("manifest 自指")
    actual: "set[str]" = set()
    for path in out_root.rglob("*"):
        if path.is_file() and not path.is_symlink():
            rel = str(path.relative_to(out_root))
            if rel != MANIFEST:
                actual.add(rel)
    for rel, sha in sorted(files.items()):
        target = out_root / rel
        if not target.is_file():
            problems.append(f"MISSING: {rel}")
            continue
        if _sha_bytes(target.read_bytes()) != sha:
            problems.append(f"SHA_MISMATCH: {rel}")
    for rel in sorted(actual - set(files)):
        problems.append(f"UNLISTED: {rel}")
    for rel in sorted(files):
        src = ctx.root / rel
        if src.is_file() and _sha_bytes(src.read_bytes()) != files[rel]:
            problems.append(f"PACK_DIFFERS_FROM_LIVE: {rel}")
    # 评审 m3：包建完之后 live 侧又增删了件，逐件比对是看不出来的（多出来的件
    # 没人列、少掉的件仍按 manifest 命中「MISSING」但语义不同）——用同一套选件
    # 规则重选一次求差，包与活树必须集合相等。
    live = {rel for rel in select(ctx, problems=problems)
            if rel != out_rel and not rel.startswith(f"{out_rel}/")}
    for rel in sorted(live - set(files)):
        problems.append(f"LIVE_ADDED: {rel}")
    for rel in sorted(set(files) - live):
        problems.append(f"LIVE_REMOVED: {rel}")
    if (out_root / "closeout" / "judgement.json").exists():
        problems.append("判决件混入包内")
    return {"ok": not problems, "problems": problems, "n_files": len(files),
            "manifest_sha256": _sha_bytes(mpath.read_bytes())}


def main(argv: "list[str] | None" = None) -> int:
    import argparse

    from orchestrator.common.runtime_paths import rsi_r02_campaign_id, rsi_r02_root

    ap = argparse.ArgumentParser(description="RSI-R0.2 证据包（零 POST）")
    ap.add_argument("--campaign-id", default=None)
    ap.add_argument("--root", default=None)
    ap.add_argument("--pack", default="evidence_pack")
    ap.add_argument("--verify", action="store_true", help="只复验不重建")
    args = ap.parse_args(list(argv) if argv is not None else None)
    cid = args.campaign_id or rsi_r02_campaign_id()
    base = Path(args.root) if args.root else rsi_r02_root()
    ctx = C.load_campaign(campaign_id=cid, root=base)
    if args.verify:
        out = verify(ctx, out_rel=args.pack)
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0 if out["ok"] else 1
    manifest = build(ctx, out_rel=args.pack)
    print(json.dumps({"n_files": manifest["n_files"],
                      "out_rel": manifest["out_rel"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
