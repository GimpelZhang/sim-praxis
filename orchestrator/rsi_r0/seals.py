"""旧资产封存集核对（G-M 的 ``seals_verified`` 条件，AC05 的运行时一半）。

T0 的 ``baseline_manifest.json`` 把旧六本账、旧批准件目录、旧网关暂停态目录的
内容哈希钉死在开账那一刻。本模块在**每次判门时**重新算一遍并与基线逐项比对：

* 旧账：行数 + sha256 必须逐字不变（旧账自 R0 起一律封存只读）；
* 旧批准件/暂停态：目录「名单 + 逐件哈希」摘要必须不变。

摘要算法与产出方 ``scripts/rsi_r0/admission.py::dir_digest`` **逐字节一致**
（``<相对路径>:<sha256>`` 按路径排序后以换行连接再取 sha256）——两边算法不同
会让基线永远对不上，那等于校验收不到东西。

本模块只读，不修基线：对不上就是对不上，票给你看，怎么处置是人的事。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

MANIFEST_SCHEMA = "rsi_r0.baseline_manifest.v1"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def dir_digest(path: Path) -> dict[str, Any]:
    """目录的「名单 + 逐件哈希」摘要（与 admission.dir_digest 同算法）。"""
    if not path.exists():
        return {"path": str(path), "exists": False, "files": 0, "digest": None}
    entries: list[str] = []
    for p in sorted(path.rglob("*")):
        if p.is_file():
            entries.append(f"{p.relative_to(path)}:{sha256_file(p)}")
    return {
        "path": str(path),
        "exists": True,
        "files": len(entries),
        "digest": sha256_text("\n".join(entries)),
    }


def _ledger_now(entry: Mapping[str, Any]) -> dict[str, Any]:
    p = Path(str(entry.get("path") or ""))
    if not p.is_file():
        return {"path": str(p), "exists": False}
    return {"path": str(p), "exists": True,
            "lines": sum(1 for _ in p.open("rb")),
            "sha256": sha256_file(p)}


def verify_baseline(manifest_path: Path) -> dict[str, Any]:
    """逐项重算并与基线比对。返回 ``{ok, drift, manifest_sha256, ...}``。"""
    if not manifest_path.exists():
        return {"ok": False, "why": f"缺基线清单：{manifest_path}", "drift": {}}
    raw = manifest_path.read_text(encoding="utf-8")
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as exc:
        return {"ok": False, "why": f"基线清单不可解析：{exc}", "drift": {}}
    if doc.get("schema") != MANIFEST_SCHEMA:
        return {"ok": False, "why": f"baseline schema 不符：{doc.get('schema')!r}",
                "drift": {}}

    drift: dict[str, Any] = {}
    checked = 0

    for name, entry in (doc.get("ledgers") or {}).items():
        if not isinstance(entry, Mapping):
            drift[f"ledger:{name}"] = {"why": "基线项不是对象"}
            continue
        if not entry.get("exists", True):
            continue
        now = _ledger_now(entry)
        checked += 1
        for key in ("lines", "sha256"):
            if entry.get(key) != now.get(key):
                drift[f"ledger:{name}.{key}"] = {"locked": entry.get(key),
                                                 "now": now.get(key)}

    for family in ("approvals", "gateway_state"):
        for name, entry in (doc.get(family) or {}).items():
            if not isinstance(entry, Mapping):
                drift[f"{family}:{name}"] = {"why": "基线项不是对象"}
                continue
            now = dir_digest(Path(str(entry.get("path") or "")))
            checked += 1
            for key in ("exists", "files", "digest"):
                if entry.get(key) != now.get(key):
                    drift[f"{family}:{name}.{key}"] = {"locked": entry.get(key),
                                                       "now": now.get(key)}

    return {
        "ok": not drift and checked > 0,
        "checked": checked,
        "why": "" if (not drift and checked > 0) else (
            "基线清单里没有任何旧资产项（空清单不等于核对过）" if checked == 0
            else f"{len(drift)} 项漂移"),
        "drift": drift,
        "manifest_path": str(manifest_path),
        "manifest_sha256": sha256_text(raw),
        "captured_at_utc": doc.get("captured_at_utc"),
    }
