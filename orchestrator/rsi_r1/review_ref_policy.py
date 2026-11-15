"""R1.3 审查引用粒度策略（**预注册**，2026-11-15 轮 D）。

背景（R1.3 轮 B/C 实证）：模型能给出合理的语义判断，但在严格字段级
JSON Pointer 下引用格式不稳定（键名 `pointer/path/value` 混用、层级偶缺）。
继续在同一轮内放宽解析属于事后追绿；因此这里把**引用粒度**本身预注册：

* 校准审查（calibration）：引用单位是 **case**。接受
  - 字符串 ``"case_id"``；
  - ``{"case_id": ...}`` / ``{"type":"case","case_id":...}``；
  - 任意 ``pointer/path/value/json_pointer``，只要其**最长存在前缀**落在
    ``/cases/<i>``；字段层级对错不影响引用有效性（case 是证据单位）。
* 正式审查（formal）：引用单位是 **包内节点**。指针必须解析到包内实际存在
  的节点（允许最长存在前缀，且深度 ≥1）；不存在的顶层键仍拒绝。

策略以常量文档形式随模块进 LOCK 闭包；请求 prompt 明示该策略。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

POLICY_SCHEMA = "simpraxis.rsi_r13.review_ref_policy.v1"
ALIASES = ("pointer", "path", "json_pointer", "value")


class RefPolicyError(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"[{code}] {detail}" if detail else code)


def policy_document() -> "dict[str, Any]":
    return {
        "schema": POLICY_SCHEMA,
        "schema_version": 1,
        "calibration": {
            "unit": "case",
            "accepted": ["case_id string", "{case_id}",
                         "{type: case, case_id}", "pointer under /cases/<i>"],
            "resolution": "longest_existing_prefix; case index must exist; "
                          "field depth is not validated",
        },
        "formal": {
            "unit": "packet node",
            "accepted": ["pointer/path/json_pointer/value"],
            "resolution": "longest_existing_prefix; depth >= 1",
        },
        "aliases": list(ALIASES),
        "packet_sha_field": "packet_sha256",
    }


def policy_sha256() -> str:
    return hashlib.sha256(json.dumps(
        policy_document(), sort_keys=True, separators=(",", ":"),
        ensure_ascii=False).encode("utf-8")).hexdigest()


class RefResolver:
    """按预注册策略解析一个审查引用。"""

    def __init__(self, packet_bytes: bytes, *, mode: str) -> None:
        if mode not in ("calibration", "formal"):
            raise RefPolicyError("MODE_INVALID", mode)
        try:
            self.packet = json.loads(packet_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise RefPolicyError("PACKET_INVALID", "packet 不是 JSON") from None
        self.mode = mode

    # -- JSON pointer 前缀 -------------------------------------------------
    def _longest_prefix(self, pointer: str) -> "tuple[bool, list[str]]":
        if not isinstance(pointer, str) or not pointer.startswith("/"):
            return False, []
        tokens = [t.replace("~1", "/").replace("~0", "~")
                  for t in pointer.split("/")[1:]]
        node: Any = self.packet
        good: "list[str]" = []
        for token in tokens:
            if isinstance(node, Mapping) and token in node:
                good.append(token)
                node = node[token]
            elif isinstance(node, list):
                try:
                    idx = int(token)
                    node = node[idx]
                except (ValueError, IndexError):
                    break
                good.append(token)
            else:
                break
        return bool(good), good

    def resolve(self, locator: Any) -> "dict[str, Any]":
        if isinstance(locator, str):
            if self.mode == "calibration" and not locator.startswith("/"):
                return self._case(locator)
            pointer: "str | None" = locator
        elif isinstance(locator, Mapping):
            if self.mode == "calibration":
                if "case_id" in locator:
                    return self._case(str(locator["case_id"]))
                if str(locator.get("type")) in ("case", "case_id"):
                    return self._case(str(locator.get("case_id") or
                                          locator.get("value") or ""))
            pointer = next((locator.get(k) for k in ALIASES
                            if isinstance(locator.get(k), str)), None)
        else:
            raise RefPolicyError("REF_INVALID", str(locator))
        if pointer is None:
            raise RefPolicyError("REF_INVALID", "缺 pointer/path/value/case_id")
        ok, prefix = self._longest_prefix(pointer)
        if not ok:
            raise RefPolicyError("REF_UNRESOLVED", pointer)
        if self.mode == "calibration":
            if len(prefix) < 2 or prefix[0] != "cases":
                raise RefPolicyError("REF_NOT_CASE", pointer)
            try:
                idx = int(prefix[1])
            except ValueError:
                raise RefPolicyError("REF_NOT_CASE", pointer) from None
            cases = self.packet.get("cases") or []
            if not (0 <= idx < len(cases)):
                raise RefPolicyError("REF_NOT_CASE", pointer)
            return {"case_id": str(cases[idx].get("case_id") or idx),
                    "index": idx,
                    "pointer_prefix": "/" + "/".join(prefix)}
        return {"pointer_prefix": "/" + "/".join(prefix)}

    def _case(self, case_id: str) -> "dict[str, Any]":
        cases = self.packet.get("cases") or []
        for idx, case in enumerate(cases):
            if str(case.get("case_id")) == str(case_id):
                return {"case_id": str(case_id), "index": idx,
                        "pointer_prefix": f"/cases/{idx}"}
        raise RefPolicyError("REF_CASE_UNKNOWN", case_id)
