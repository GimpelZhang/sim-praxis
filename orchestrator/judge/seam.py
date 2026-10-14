#!/usr/bin/env python3
"""Task5 evaluate 接缝：把 telemetry 文件装配成图 metrics（详细计划 L515-524）。

Task5 范围：Rung1–3 规则判定在本接缝全部落地；Rung4 在此恒为 None（state 尚无
视频字段，Task7 真实 rollout 产出 mp4 后由 Rung4Judge + combine_vector 接入，
视频抽帧走 collage.extract_frames_mp4 容器路径）。

保留 Task3 占位件的全部既有键（telemetry_uri/fixture/engine/run_id/data/
provenance），新增 schema_errors/rungs/vector/rules_pass/auto_release/
escalate_human。schema 不合法不抛异常（图不中断），以 rules_pass=False 进 Gate2。
"""
from __future__ import annotations

from typing import Any

from .rung4 import combine_vector
from .rungs import evaluate_rollout


def assemble_file_metrics(uri: "str | Any", data: dict) -> dict:
    """读文件与顶层对象检查由 nodes.file_backed_evaluate 完成；此处纯装配。"""
    result = evaluate_rollout(data)
    combined = combine_vector(result, None)
    return {
        "telemetry_uri": str(uri),
        "fixture": bool(data.get("_fixture", False)),
        "engine": data.get("engine"),
        "run_id": data.get("run_id"),
        "data": data,
        "provenance": "file:telemetry_uri",
        "ok": bool(result.get("ok")),
        "schema_errors": result.get("schema_errors", []),
        "rungs": {"rung1": result["rung1"], "rung2": result["rung2"],
                  "rung3": result["rung3"], "rung4": None},
        "vector": combined["vector"],
        "rules_pass": combined["rules_pass"],
        "rung4": None,
        "auto_release": combined["auto_release"],
        "escalate_human": combined["escalate_human"],
    }
