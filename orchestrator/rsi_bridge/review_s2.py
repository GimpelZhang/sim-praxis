"""S2 双审仪器 v2（六维 + 六段报告 + 盲审 + 通道互异；无第三票）。

在 S1 仪器（``review_s1``）之上只加三件（计划 §2.1 第 4/5 行、§3 S2-5）：

1. **六段报告入包**：研究包必须带报告六段（``report_contract``），缺段在包里显式
   标注 ``missing_sections``（"缺段按 0 分且结构标注"）；
2. **盲审**：包里**不出现** ``v0``/``v1`` 臂标签（``pack_id`` 是实例×臂×盐的哈希，
   映射只留在宿主侧报告）；自足负控沿用 S1（禁止键 + 哨兵）；
3. **通道互异**：channel_a 温度 0.0 / 盐 A、channel_b 温度 0.7 / 盐 B；每条审查
   记录实际使用的加盐包（``_used_packs``），判件复算 ``pack_sha256``。

真值表/校准/冻结复用 S1 单一真值（不复制实现）。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from orchestrator.hk import report_contract as RC
from orchestrator.rsi_bridge import review_s1 as R1

__all__ = ["SCHEMA", "CHANNELS", "ReviewS2Error", "channel_parameters",
           "build_pack_v2", "pack_id", "run_channel", "merge_channels",
           "freeze_report", "verify_frozen_report", "calibration_cases",
           "calibration_expectations", "report_floor"]

SCHEMA = "simpraxis.rsi_s2.review.v2"

#: 通道参数（互异盐 + 温度；记录进冻结报告，判件复算）。
CHANNELS = {
    "channel_a": {"temperature": "0.0", "salt": "s2-channel-a"},
    "channel_b": {"temperature": "0.7", "salt": "s2-channel-b"},
}

#: 盲审：包里不得出现臂标签。
_ARM_LABELS = ("\"v0\"", "\"v1\"", "arm=v0", "arm=v1", "harness=v0",
               "harness=v1", "结构停止码", "STAGE_RUNTIME_ERROR", "NC_DEGENERATE",
               "NC_UNREGISTERED")


class ReviewS2Error(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


def channel_parameters(channel: str) -> "dict[str, str]":
    try:
        return dict(CHANNELS[channel])
    except KeyError:
        raise ReviewS2Error("CHANNEL_UNKNOWN", channel) from None


def pack_id(*, instance_id: str, arm: str, channel: str) -> str:
    """盲审包 id（实例×臂×通道盐的哈希；臂映射只留宿主侧）。"""
    salt = channel_parameters(channel)["salt"]
    return hashlib.sha256(f"{instance_id}|{arm}|{salt}".encode("utf-8")).hexdigest()[:16]


def _canonical(doc: Any) -> bytes:
    return json.dumps(doc, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def report_floor(report: Any) -> "dict[str, float]":
    """结构分地板（缺段→0）；透传 S1 六维名。"""
    return RC.dimension_floor(report)


def build_pack_v2(*, instance_id: str, arm: str, channel: str,
                  question: str, methods: str,
                  results: "list[Mapping[str, Any]]",
                  claim: Mapping[str, Any], report: Mapping[str, Any],
                  negative_control: str) -> "dict[str, Any]":
    """构造盲审包（六段报告 + 自足负控 + 盲 id；不含臂标签）。"""
    channel_parameters(channel)  # 未知通道 fail-closed
    missing = RC.missing_sections(report)
    pack = {
        "schema": f"{SCHEMA}.pack",
        "pack_id": pack_id(instance_id=instance_id, arm=arm, channel=channel),
        "study": instance_id,
        "question": question,
        "methods": methods,
        "results": [dict(row) for row in results],
        "claim": dict(claim),
        "report": {name: str((report or {}).get(name, ""))
                   for name in RC.SECTIONS},
        "missing_sections": missing,
        "negative_control": negative_control,
        "nonce": R1._sha(  # noqa: SLF001 - 单一真值
            [instance_id, question, methods, list(results), dict(claim),
             {name: str((report or {}).get(name, "")) for name in RC.SECTIONS},
             negative_control]),
    }
    R1.assert_pack_self_contained(pack)
    text = json.dumps(pack, ensure_ascii=False)
    for label in _ARM_LABELS:
        if label in text:
            raise ReviewS2Error("PACK_NOT_BLIND", f"包含臂标签 {label!r}")
    pack["pack_sha256"] = R1._sha(  # noqa: SLF001
        {k: v for k, v in pack.items() if k != "pack_sha256"})
    return pack


def _first_json_object(text: str) -> str:
    """提取**首个完整平衡的 JSON 对象**（reviewer 偶发输出多对象时避免 Extra data）。

    只做括号配对（尊重字符串与转义），不做任何语义宽松：提取出的对象仍必须通过
    ``review_s1.parse_review`` 的六维 + verdict 全 schema 校验，否则照常拒绝。
    """
    source = text or ""
    start = source.find("{")
    if start < 0:
        return source
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(source)):
        char = source[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == "\"":
                in_string = False
            continue
        if char == "\"":
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
    return source[start:]


def run_channel(call, *, pack: Mapping[str, Any], channel: str) -> "dict[str, Any]":
    """单通道审查（call 由战役注入网关调用；记录实际包）。"""
    params = channel_parameters(channel)
    raw_text, tokens = call(R1.render_prompt(pack))
    parsed = R1.parse_review(_first_json_object(raw_text),
                             pack_sha256=str(pack.get("pack_sha256") or ""))
    run = R1.ReviewRun(channel=channel,
                       pack_sha256=str(pack.get("pack_sha256") or ""),
                       raw_text=raw_text, parsed=parsed, tokens=tokens)
    return {"channel": channel, "pack_sha256": run.pack_sha256,
            "pack_id": pack.get("pack_id"),
            "temperature": params["temperature"], "salt": params["salt"],
            "raw_text": run.raw_text, "parsed": dict(run.parsed),
            "tokens": list(run.tokens),
            "missing_sections": list(pack.get("missing_sections") or []),
            "report_floor": report_floor(pack.get("report")),
            "_used_packs": {channel: dict(pack)}}


def merge_channels(*, instance_id: str, arm: str,
                   channel_a: Mapping[str, Any],
                   channel_b: Mapping[str, Any]) -> "dict[str, Any]":
    """两票合并（无第三票）；核对两通道确实互异且包可复算。"""
    # 通道互异 = **审查参数**互异（温度/盐）+ 各自送审包 sha 可复算。
    # 两通道共享同一盲包内容是**设计本意**（盲审），因此不能比内容面
    # （2026-11-19 复评 CRITICAL：比去盐内容面恒相等 → 恒抛，双审阶段不可复现）。
    params_a = (str(channel_a.get("temperature")), str(channel_a.get("salt")))
    params_b = (str(channel_b.get("temperature")), str(channel_b.get("salt")))
    if params_a == params_b:
        raise ReviewS2Error("CHANNELS_NOT_DISTINCT",
                            f"两通道参数相同 {params_a}（同源偏差）")
    for row in (channel_a, channel_b):
        pack = (row.get("_used_packs") or {}).get(row["channel"])
        if not isinstance(pack, Mapping):
            raise ReviewS2Error("PACK_PROVENANCE_MISSING", row["channel"])
        claimed = str(pack.get("pack_sha256") or "")
        actual = R1._sha({k: v for k, v in pack.items()  # noqa: SLF001
                          if k != "pack_sha256"})
        if claimed != actual:
            raise ReviewS2Error("DUAL_REVIEW_PACK_MISMATCH",
                                f"{row['channel']} 包 sha 复算不符")
        if row["pack_sha256"] != claimed:
            raise ReviewS2Error("DUAL_REVIEW_PACK_MISMATCH",
                                f"{row['channel']} 记录 sha 与包不符")
    merged = R1.merge_truth_table(channel_a["parsed"], channel_b["parsed"])
    return {"schema": f"{SCHEMA}.merged", "instance_id": instance_id,
            "arm": arm, **merged,
            "channels": {"channel_a": channel_a, "channel_b": channel_b}}


def freeze_report(document: Mapping[str, Any]) -> "dict[str, Any]":
    return R1.freeze_report(document)


def verify_frozen_report(document: Mapping[str, Any]) -> str:
    return R1.verify_frozen_report(document)


def calibration_cases():
    return R1.calibration_cases()


def calibration_expectations():
    return R1.calibration_expectations()
