"""G1 B 层：**独立**评分参照（不复用 g1_probe 的实现）。

按案例公开数据与预登记 expected 直接算；用于与 ``g1_probe.score_slot``
交叉核对——两者不一致即停止封存，不发模型请求。
"""
from __future__ import annotations

ABS = 1e-8
REL = 1e-6


def _close(got, want) -> bool:
    got, want = float(got), float(want)
    return abs(got - want) <= ABS + REL * abs(want)


def judge(case: dict, interface: str, doc: dict) -> dict:
    exp = case["expected"]
    claim = doc.get("claim") if isinstance(doc, dict) else None
    ref = doc.get("result_ref") if isinstance(doc, dict) else None
    if not isinstance(claim, dict) or not isinstance(ref, dict):
        return {"mechanical_ok": False, "semantic_ok": False}
    if ref.get("artifact_id") != "analysis/output.json":
        return {"mechanical_ok": False, "semantic_ok": False}
    pointer = str(ref.get("json_pointer") or "")
    if not pointer.startswith("/"):
        return {"mechanical_ok": False, "semantic_ok": False}
    node = case["analysis"]["doc"]
    for token in pointer.split("/")[1:]:
        if isinstance(node, dict):
            node = node.get(token)
        elif isinstance(node, list):
            try:
                node = node[int(token)]
            except (ValueError, IndexError):
                node = None
        else:
            node = None
    if not isinstance(node, dict):
        return {"mechanical_ok": False, "semantic_ok": False}
    contrast_ok = str(node.get("contrast_id")) == exp["contrast_id"]
    if interface == "verbatim":
        numeric = all(_close(claim.get(k), node.get(k))
                      for k in ("estimate", "lower", "upper"))
    else:
        numeric = True
    mechanical = bool(contrast_ok and numeric)
    semantic = (str(claim.get("scope")) == exp["scope"]
                and str(claim.get("semantic")) == exp["semantic"])
    return {"mechanical_ok": mechanical, "semantic_ok": semantic}
