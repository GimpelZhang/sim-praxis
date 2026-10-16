#!/usr/bin/env python3
"""Task8 Q2（Carla 固态 LiDAR 雨雪雾）事后补真人 Gate2 点验卡片。

背景：Q2 按详细计划 D9 走「条件闸门」——不经 LangGraph 图、无 Gate1 方案卡，
凭用户 2026-10-16 书面 standing authorization 执行，因此四任务中唯独 Q2
没有飞书 card_form 真人签字。本脚本按用户 2026-10-16 指示补发一张真实
Gate2 卡（原生 q2_overlay.mp4 + outcome/mechanism 双结论），真实阻塞等待
真人按钮裁决，禁止任何自动批准。

与 tests/milestone1/test_hitl_drill.py 的区别：本流程 0 次方舟付费调用
（不发 Rung4 视觉实呼；Q2 结论是几何/物理对照实验，不是 rollout 视频核片），
只有飞书消息发送；脚本在发卡前后各读一次 quota ledger，断言实呼行数不变。

唯一合法运行方式（fail-closed 环境守卫）：
    source scripts/00_env.sh
    SIMPRAXIS_Q2_HUMAN_GATE=1 \
        /mnt/venvs/simpraxis/bin/python scripts/milestone1/q2_human_gate.py

产物（全部在 Q2 证据目录内，证据包按 E8.2 递归收录）：
    <q2_carla>/human_gate/q2_gate.log     过程日志
    <q2_carla>/human_gate/decision.json   结构化裁决（simpraxis.q2_human_gate.v1，0600）

退出码：0=真人 approve；3=真人 reject_with_comments（如实失败，不掩盖）；
1=环境/证据缺失或等待异常。
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

PHASE1_ROOT = Path(os.environ.get(
    "SIMPRAXIS_PHASE1_ROOT", "/mnt/artifacts/simpraxis/phase1"))
RUNS_ROOT = Path(os.environ.get(
    "SIMPRAXIS_RUN_DIR", "/mnt/artifacts/simpraxis/runs")) / "phase1"
Q2_DIR = RUNS_ROOT / "task8_track2" / "q2_carla"
ANALYSIS = Q2_DIR / "analysis"
CONTROL_INV = Q2_DIR / "control" / "analysis" / "invariance_checks.json"
GATES_DB = PHASE1_ROOT / "state" / "pending_gates.db"

THREAD_ID = "t8-q2carla-hitl-20261016"
TITLE = ("[Q2 补真人点验] Track2 工程题：固态 LiDAR 雨雪雾"
         "物理光学材质模型（Carla 0.10.0）")

_log_lines: list[str] = []


def _log(line: str) -> None:
    stamped = f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {line}"
    _log_lines.append(stamped)
    print(stamped, flush=True)


def _flush_log() -> None:
    ld = Q2_DIR / "human_gate"
    ld.mkdir(parents=True, exist_ok=True)
    (ld / "q2_gate.log").write_text("\n".join(_log_lines) + "\n",
                                    encoding="utf-8")


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _build_metrics() -> tuple[dict, dict]:
    """数字全部从 Q2 真实产物实读，模板内不写字面量。返回 (metrics, 原件快照)。"""
    inv = _load_json(ANALYSIS / "invariance_checks.json")
    ctl = _load_json(CONTROL_INV)
    geo = inv["semantic_geometry_bit_identical"]
    cross = inv["geo_cross_repeat_bit_identical"]
    z = inv["geo_count_z_vs_clear"]
    zc = ctl["geo_count_z_vs_clear"]
    di = max(x["max_abs_dI"] for x in inv["survivor_intensity_identity"])

    # optics_sweep.csv 取 SUMMARY 引用的三档（condition 名以实际表头为准）
    wanted = {"rain_100": "rain 100 mm/h（Marshall–Palmer 雨谱）",
              "snow_100": "snow 100 mm/h 融化当量（Gunn–Marshall 雪谱）",
              "fog_v6": "fog V=6 m（Koschmieder）"}
    mech: list[dict] = []
    with (ANALYSIS / "optics_sweep.csv").open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            label = wanted.get(row["condition"])
            if label is None:
                continue
            mech.append({
                "label": label,
                "surface_kept_pct": float(row["frac_surface_kept_mean"]) * 100,
                "weather_false": float(row["n_weather_false_mean"]),
                "near_blind": (float(row["n_near_blind_mean"])
                               if float(row["n_near_blind_mean"]) > 0 else None),
                "transmission": float(row["transmission_mean_mean"]),
            })
    assert len(mech) == 3, f"optics 三档缺：{[m['label'] for m in mech]}"

    metrics = {
        "metrics_kind": "q2_invariance_report",
        "outcome": {
            "bit_identical": {
                "comparisons": geo["comparisons"], "failures": len(geo["failures"]),
                "cross_repeat": cross["comparisons"]},
            "z_vs_clear": {"rain_hard": z["rain_hard"], "fog_dense": z["fog_dense"]},
            "z_negative_control": {"rain_hard": zc["rain_hard"],
                                   "fog_dense": zc["fog_dense"]},
            "survivor_max_abs_dI": f"{di:.2e}",
            "engine_has_snow": False,
        },
        "mechanism": mech,
        "residual": ("Qback=0.4、湿沥青 BRDF 0.7、905nm 雾消光等参数未用真实天气 "
                     "LiDAR 数据标定，模型只断言量级与单调性；静态单车/单地图/"
                     "单 spawn，未引入交通流与多径。"),
        "d9_note": ("Q2 按计划 D9 走条件闸门（不经 LangGraph 图、无 Gate1 方案卡），"
                    "原仅凭用户 2026-10-16 书面授权执行；本卡是应要求补的真人 "
                    "Gate2 点验，全程 0 次方舟付费调用。"),
    }
    snapshot = {
        "invariance_checks_sha256": _sha(ANALYSIS / "invariance_checks.json"),
        "control_invariance_sha256": _sha(CONTROL_INV),
        "optics_sweep_sha256": _sha(ANALYSIS / "optics_sweep.csv"),
        "summary_md_sha256": _sha(ANALYSIS / "SUMMARY.md"),
    }
    return metrics, snapshot


def _explanation() -> dict[str, str]:
    return {
        "task": ("面试题第 2 题：无人物流车 16 线固态式 LiDAR 在雨/雪/雾天的"
                 "点云噪声。真实 Carla 0.10.0 服务端实验：静态 MKZ、"
                 "Town10HD_Opt、ray_cast 与 ray_cast_semantic 同位双雷达"
                 "（120°/50m/300kpps），clear/rain_hard/fog_dense 三天气，"
                 "同步模式、独立进程 3 重复。"),
        "problem": ("分两层验收，分开报告不调参：①引擎原生 LiDAR 到底有没有"
                    "天气物理通道（outcome，用 bit 级对照回答）；②若没有，"
                    "引擎外物理光学材质层（双程 Beer–Lambert、Marshall–Palmer "
                    "雨谱、Gunn–Marshall 雪谱、Koschmieder 雾）给出的天气"
                    "效应量级与单调性是否合理（mechanism）。"),
        "video": ("q2_overlay.mp4，约 10 秒（1fps）俯视点云三分屏：左＝Carla "
                  "clear 原生基线；中＝叠加 rain 100mm/h 光学层（灰＝保留的"
                  "表面点，红＝is_weather 标签的天气伪回波，另有近场盲丢）；"
                  "右＝fog V=6m，表面点近盲。核心看点：三天气下左侧原生几何"
                  "应当逐点重合（80 组语义射线逐 bit 相等），天气效应只来自"
                  "中间叠加的物理光学层。"),
    }


def main() -> int:
    if os.environ.get("SIMPRAXIS_Q2_HUMAN_GATE") != "1":
        print("refuse: 必须显式 SIMPRAXIS_Q2_HUMAN_GATE=1（真实飞书发卡+真人"
              "阻塞，禁止误触发与任何自动批准）", file=sys.stderr)
        return 1
    for name in ("FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_RECEIVE_ID"):
        if not os.environ.get(name):
            print(f"refuse: {name} 未配置（先 source scripts/00_env.sh）",
                  file=sys.stderr)
            return 1

    video = ANALYSIS / "q2_overlay.mp4"
    for p in (video, ANALYSIS / "SUMMARY.md", ANALYSIS / "invariance_checks.json",
              CONTROL_INV, ANALYSIS / "optics_sweep.csv", GATES_DB):
        if not p.is_file():
            print(f"refuse: 证据缺失 {p}", file=sys.stderr)
            return 1

    # 幂等守卫：该线程已有结论（无论 approve/reject）不允许重发覆盖
    con = sqlite3.connect(f"file:{GATES_DB}?mode=ro", uri=True)
    try:
        existing = con.execute(
            "SELECT status,decision,source FROM pending_gates "
            "WHERE thread_id=? AND gate=2 ORDER BY created_at",
            (THREAD_ID,)).fetchall()
    finally:
        con.close()
    decided = [r for r in existing if r[0] == "DECIDED"]
    if decided:
        print(f"refuse: {THREAD_ID} 已有终态裁决 {decided}，禁止重发覆盖",
              file=sys.stderr)
        return 1

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from orchestrator.feishu_gateway.client import gateway_from_env
    from orchestrator.gateway.quota_ledger import call_lines, read_lines

    metrics, snapshot = _build_metrics()
    paid_before = len(call_lines(read_lines()))
    _log(f"[q2-gate] 开始：thread={THREAD_ID}；ledger 实呼行数={paid_before}"
         f"（本流程必须保持不变）")
    _log(f"[q2-gate] 证据 sha256：video={_sha(video)[:16]} "
         f"summary={snapshot['summary_md_sha256'][:16]} "
         f"invariance={snapshot['invariance_checks_sha256'][:16]} "
         f"control={snapshot['control_invariance_sha256'][:16]}")
    _flush_log()

    feishu = gateway_from_env()
    try:
        _log("[q2-gate] 发送原生视频 + Gate2 卡片到真人飞书，等待按钮裁决"
             "（approve / reject_with_comments；系统绝不代决）…")
        _flush_log()
        decision = feishu.request_gate2_approval(
            THREAD_ID, TITLE, metrics,
            video_path=str(video),
            explanation=_explanation())
    finally:
        feishu.close()
    _log(f"[q2-gate] 真人裁决返回：{json.dumps(decision, ensure_ascii=False)}")

    con = sqlite3.connect(f"file:{GATES_DB}?mode=ro", uri=True)
    try:
        grow = con.execute(
            "SELECT status,choice,decision,human_notes,source,card_message_id,"
            "media_message_id,open_chat_id FROM pending_gates "
            "WHERE thread_id=? AND gate=2", (THREAD_ID,)).fetchone()
    finally:
        con.close()
    if grow is None:
        _log("[q2-gate] FATAL：裁决返回但 DB 无落库行")
        _flush_log()
        return 1
    status, choice, dec, notes, source, card_id, media_id, chat_id = grow
    paid_after = len(call_lines(read_lines()))
    _log(f"[q2-gate] DB：status={status} decision={dec} source={source} "
         f"notes={notes!r} card={card_id} media={media_id}；"
         f"ledger 实呼 {paid_before}→{paid_after}")

    result = {
        "schema": "simpraxis.q2_human_gate.v1",
        "thread_id": THREAD_ID,
        "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "gate_path": "D9 条件闸门补 Gate2 点验（无 Gate1；用户 2026-10-16 指示）",
        "task_title": TITLE,
        "paid_llm_calls": {"before": paid_before, "after": paid_after,
                           "delta": paid_after - paid_before},
        "metrics": metrics,
        "evidence_sha256": {"q2_overlay.mp4": _sha(video), **snapshot},
        "human_decision": {"decision": dec, "choice": choice,
                           "human_notes": notes, "source": source,
                           "card_message_id": card_id,
                           "media_message_id": media_id,
                           "open_chat_id": chat_id},
    }
    out = Q2_DIR / "human_gate" / "decision.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    os.chmod(out, 0o600)
    if paid_after != paid_before:
        _log("[q2-gate] FATAL：本流程声称 0 付费但 ledger 行数变化，"
             "结果如实保留，退出码 1")
        _flush_log()
        return 1
    if status != "DECIDED" or source != "card_form":
        _log("[q2-gate] FATAL：非 card_form 终态")
        _flush_log()
        return 1
    if dec == "approve":
        _log(f"[q2-gate] 真人 approve/card_form，结果落 {out}")
        _flush_log()
        return 0
    _log(f"[q2-gate] 真人打回（{dec}），意见：{notes!r}；不掩盖、不重发")
    _flush_log()
    return 3


if __name__ == "__main__":
    sys.exit(main())
