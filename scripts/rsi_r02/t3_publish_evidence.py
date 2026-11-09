#!/usr/bin/env python3
"""RSI-R0.2 T3：把控制 campaign 的结论件逐字节发布进生产 campaign（零 POST）。

T3 的同路径控制在**独立**控制 campaign 里跑（不与生产样本混账）；最终 LOCK
却必须绑定这些结论件。本脚本只做一件事，并且做得可核对：

1. 从控制 campaign 取 4 件（控制报告、结案预演、两条协议记录）；
2. 复制进生产 campaign 的 ``t3_control/``（0700 目录 / 0600 文件）；
3. **读回复核逐字节相等**，不等即 fail-closed；
4. 写 ``t3_control/provenance.json``（来源 campaign、逐件 sha256/字节数），
   让「生产 campaign 里这 4 件从哪来」永远可追。

不生成、不改写任何报告内容；重跑幂等（同名覆盖后重新核对）。

用法::

    /mnt/venvs/simpraxis/bin/python scripts/rsi_r02/t3_publish_evidence.py \\
        --control-campaign r02_t3_control_<UTC> \\
        --campaign-id r02_<UTC>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(REPO_ROOT), str(REPO_ROOT / "scripts" / "rsi_r02")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from orchestrator.common.clock import utc_now_iso_z  # noqa: E402
from orchestrator.common.fsutil import ensure_private_dir  # noqa: E402
from orchestrator.rsi_r02 import campaign as C  # noqa: E402

SCHEMA = "simpraxis.rsi_r02.t3_control_provenance.v1"
FILES = ("report.json", "closeout_rehearsal.json",
         "records/proto_P1.json", "records/proto_P2.json")


class PublishError(RuntimeError):
    """结论件缺失或复制后不一致（不静默、不补写）。"""


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def publish(*, control: C.CampaignContext, prod: C.CampaignContext,
            note: str = "") -> "dict[str, Any]":
    out: "dict[str, Any]" = {
        "schema": SCHEMA,
        "source_campaign": control.campaign_id,
        "source_root": str(control.root),
        "published_at_utc": utc_now_iso_z(),
        "note": note,
        "files": {},
    }
    for rel in FILES:
        src = control.relative(f"t3_control/{rel}")
        if not src.is_file():
            raise PublishError(f"控制 campaign 缺结论件：t3_control/{rel}")
        data = src.read_bytes()
        dst = prod.relative(f"t3_control/{rel}")
        ensure_private_dir(dst.parent)
        dst.write_bytes(data)
        os.chmod(dst, 0o600)
        back = dst.read_bytes()
        out["files"][rel] = {
            "sha256": _sha(data), "bytes": len(data),
            "identical": back == data,
            "source_sha256": _sha(src.read_bytes()),
        }
        if back != data:
            raise PublishError(f"复制后不一致：t3_control/{rel}")
    C.write_json(prod, "t3_control/provenance.json", out)
    return out


def main(argv: "list[str] | None" = None) -> int:
    from orchestrator.common.runtime_paths import rsi_r02_campaign_id, rsi_r02_root

    ap = argparse.ArgumentParser(description="T3 结论件发布（零 POST）")
    ap.add_argument("--control-campaign", required=True)
    ap.add_argument("--campaign-id", default=None)
    ap.add_argument("--root", default=None)
    ap.add_argument("--note", default="")
    args = ap.parse_args(argv)

    base = Path(args.root) if args.root else rsi_r02_root()
    control = C.load_campaign(campaign_id=args.control_campaign, root=base)
    prod = C.load_campaign(campaign_id=args.campaign_id or rsi_r02_campaign_id(),
                           root=base)
    out = publish(control=control, prod=prod, note=args.note)
    print(json.dumps({"source": out["source_campaign"],
                      "files": {k: v["identical"]
                                for k, v in out["files"].items()}},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
