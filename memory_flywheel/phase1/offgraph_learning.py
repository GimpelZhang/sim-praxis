"""图外运行学习回写唯一通路（Phase 2 Task0a，计划纪律 12；0 付费/0 触网）。

补点验/点验/烟测等不经 LangGraph 的运行，其结论与图内结案**同责沉淀**：
一律经本模块写入 ``phase1_learnings``，默认通路不得绕过，不允许再出现
「Q2 补点验 0 沉淀」。accepted 行必须有**真实人类裁决件**：

  - decision.json 可解析、schema == ``simpraxis.q2_human_gate.v1``；
  - human_decision.decision == "approve"（非 approve 一律拒写 accepted）；
  - 证据 relpath 必填、真实存在、禁越界（复用 LearningStore 双根校验：
    "@runs/" 前缀 → $SIMPRAXIS_RUN_DIR，裸相对 → $SIMPRAXIS_PHASE1_ROOT）；
  - 行内事实只许抄证据（数值全部从 decision.json metrics 实读格式化），
    空 human_notes 不得代笔——human_opinion 只放真人逐字 notes，空串时
    compose_text 渲染「（无）」。

白名单：gate_source 固定为新增值 ``gate2_accepted_supplementary``
（learning_store.GATE_SOURCES 枚举内、非 failure 正则、rung 必须 >=1）。
Q2 不经 Rung1-4 阶梯、无 VLM，按「已过最低自动化核验级」如实记 rung=1，
不得记 4（Rung4 是 VLM 级）。

操作留痕：每次成功回写以**追加**方式写 0600 jsonl
``$SIMPRAXIS_PHASE2_ROOT/recall/offgraph_writes.jsonl``（含行 id 与裁决
元数据，decision 原件经 evidence_relpath 指针留痕）。确定性主键「先删后写」
复用 LearningStore._id_for，同参重跑幂等、不产生第二行（追加日志如实记
两次操作）。

CLI（历史 Q2 对既有 decision.json 的一次性补跑由人工经此执行）::

    python -m memory_flywheel.phase1.offgraph_learning \\
        --decision /path/to/decision.json \\
        --relpath @runs/phase1/task8_track2/q2_carla/human_gate/decision.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from orchestrator.common.clock import utc_now_iso_z
from orchestrator.common.fsutil import atomic_write_text, ensure_private_dir
from orchestrator.common.runtime_paths import phase2_root
from memory_flywheel.phase1.learning_store import (
    GATE_ACCEPTED_SUPPLEMENTARY,
    LearningStore,
)

DECISION_SCHEMA = "simpraxis.q2_human_gate.v1"

# Q2 补沉淀固定字段（计划 Task0a L292-293）：track/sim_engine 来自任务归属，
# run_id 必须取 decision.json 真实 thread_id（不新编）；rung=1 理由见模块
# docstring 与计划 L293。
TRACK = "track2"
SIM_ENGINE = "carla"
SUPPLEMENTARY_RUNG = 1

WRITE_LOG_RELPATH = Path("recall") / "offgraph_writes.jsonl"


class OffgraphDecisionError(ValueError):
    """decision.json 缺失/不可解析/schema 不符/非真人 approve：拒写 accepted。"""


# ---------------- 裁决件校验（四种拒绝情形全部 fail-closed，零残行） ----------------

def load_approved_decision(decision_json: "str | Path | dict") -> dict[str, Any]:
    """读 decision.json 并校验真人 approve；任一不符抛 OffgraphDecisionError。

    接受路径（str/Path，必须是真实文件）或已解析的 dict（测试用；仍过全套
    结构校验）。校验只读取裁决件，不触碰学习表。
    """
    if isinstance(decision_json, dict):
        data = decision_json
    else:
        path = Path(decision_json)
        if not path.is_file():
            raise OffgraphDecisionError(
                f"decision.json 缺失或不可读：{path}（无真人裁决件不得写 "
                "accepted；纪律 12）")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise OffgraphDecisionError(
                f"decision.json 无法解析：{path}: {exc}（拒写 accepted）"
            ) from exc
    if not isinstance(data, dict) or data.get("schema") != DECISION_SCHEMA:
        got = data.get("schema") if isinstance(data, dict) else type(data).__name__
        raise OffgraphDecisionError(
            f"decision.json schema 不符：期望 {DECISION_SCHEMA}，收到 {got!r}")
    hd = data.get("human_decision")
    if not isinstance(hd, dict) or hd.get("decision") != "approve":
        got = hd.get("decision") if isinstance(hd, dict) else None
        raise OffgraphDecisionError(
            f"human_decision.decision 必须为 approve，收到 {got!r}：拒写 "
            "supplementary accepted 行（无真人 approve 不得沉淀为通过结论）")
    run_id = data.get("thread_id")
    if not isinstance(run_id, str) or not run_id.strip():
        raise OffgraphDecisionError(
            "decision.json 缺 thread_id：run_id 必须取真实裁决线程 id，"
            "不允许新编")
    if not isinstance(data.get("ts_iso"), str) or not data["ts_iso"].strip():
        raise OffgraphDecisionError("decision.json 缺 ts_iso（裁决时间留痕）")
    source = hd.get("source")
    if not isinstance(source, str) or not source.strip():
        raise OffgraphDecisionError(
            "human_decision.source 缺失：真人裁决渠道必须可追溯")
    notes = hd.get("human_notes", "")
    if not isinstance(notes, str):
        raise OffgraphDecisionError("human_decision.human_notes 必须是字符串")
    return data


# ---------------- Q2 行文案：数值只许从 decision.json 实读（L295-297） ----------------

def _one_mechanism(mech: list, needle: str) -> dict:
    hits = [r for r in mech if needle in str(r.get("label", ""))]
    if len(hits) != 1:
        raise OffgraphDecisionError(
            f"metrics.mechanism 中 {needle!r} 档应恰好 1 行，实得 {len(hits)}："
            "拒写（行内事实只许抄证据，缺源不补）")
    return hits[0]


def _qback_from_residual(residual: str) -> str:
    m = re.search(r"Qback=([0-9]+(?:\.[0-9]+)?)", str(residual))
    if m is None:
        raise OffgraphDecisionError(
            "metrics.residual 未找到 Qback=<number>：残差声明缺失，拒写")
    return m.group(1)


def build_q2_fields(decision: dict[str, Any]) -> dict[str, str]:
    """从 simpraxis.q2_human_gate.v1 裁决件构造 Q2 学习行两段事实文案。

    计划 L295-296 草案逐字落实；所有数值槽位由 decision.json 的 metrics
    实读格式化，任一字段缺失/对不上即 OffgraphDecisionError（STOP，禁止编
    造或自行修正文案）。

    锁内适配（Task0a）：附录 A 第七条事实追溯测试要求正文可与 decision.json
    同源比对 71.73%/−1.116/4.45e-05 等高精度原值，而 L296 草案用一位小数
    摘要值；两者并存——摘要句逐字保留草案口径（71.7%/-1.12），末尾追加
    「机判原值」追溯句（71.73%/-1.116），完整知识 prose 按 L311 归 0b
    solutions README，学习表只放摘要+原值锚点。
    """
    try:
        metrics = decision["metrics"]
        out = metrics["outcome"]
        mech = metrics["mechanism"]
        bit = out["bit_identical"]
        z = out["z_vs_clear"]
        zc = out["z_negative_control"]
        di = out["survivor_max_abs_dI"]
        has_snow = bool(out["engine_has_snow"])
    except (KeyError, TypeError) as exc:
        raise OffgraphDecisionError(
            f"decision.json metrics 结构缺字段：{exc}（拒写）") from exc
    rain = _one_mechanism(mech, "rain 100")
    snow = _one_mechanism(mech, "snow 100")
    fog = _one_mechanism(mech, "fog V=6")
    qback = _qback_from_residual(metrics.get("residual", ""))
    snow_word = str(has_snow).lower()

    failure_symptom = (
        "无失败：D9 条件闸门图外补点验（无 Gate1），一次通过；对象=固态 LiDAR "
        "雨雪雾物理光学材质模型（Carla 0.10.0）")
    # 计划 L296 逐字草案；数值槽全部实读（一位小数摘要口径）。
    fix_summary = (
        f"无需修复即通过；核验结论：几何射线 {int(bit['comparisons'])} 项 bit "
        f"相等（含跨进程 {int(bit['cross_repeat'])} 项），天气不进几何管线；"
        f"点数天气效应 z={float(z['rain_hard']):.2f}/{float(z['fog_dense']):.2f} "
        f"与阴性对照 {float(zc['rain_hard']):.2f}/{float(zc['fog_dense']):.2f} "
        f"同量级，天气因果被证伪；机理：rain100 表面保留 "
        f"{float(rain['surface_kept_pct']):.1f}%/伪点 "
        f"{float(rain['weather_false']):.0f}/透射 "
        f"{float(rain['transmission']):.3f}/近盲丢点 "
        f"{float(rain['near_blind']):.0f}，snow100 "
        f"{float(snow['surface_kept_pct']):.1f}%/"
        f"{float(snow['weather_false']):.0f}/"
        f"{float(snow['transmission']):.3f}，fog6m "
        f"{float(fog['surface_kept_pct']):.1f}%/"
        f"{float(fog['weather_false']):.0f}/"
        f"{float(fog['transmission']):.3f}；Carla 0.10.0 无 snow 字段"
        f"(engine_has_snow={snow_word})；Qback={qback} 等待定参数未标定，"
        "模型只断言量级与单调性；详见 closeout 与 0b 方案 README")
    # 锁内适配（Task0a）：附录 A 事实追溯锚点（高精度原值，与 decision.json
    # 同源），供机判抽样与 0b 全量 prose 的召回衔接。
    fix_trace = (
        f"｜机判原值（decision.json 同源追溯）：rain100 表面保留 "
        f"{float(rain['surface_kept_pct']):.2f}%/透射 "
        f"{float(rain['transmission']):.3f}、z_vs_clear rain_hard "
        f"{float(z['rain_hard']):.3f}、存活点 max|dI| {di}、Qback={qback}、"
        f"engine_has_snow={snow_word}。")
    return {"failure_symptom": failure_symptom,
            "fix_action": fix_summary + fix_trace}


# ---------------- 0600 追加日志 ----------------

def _append_write_log(record: dict[str, Any], root: "str | Path | None") -> Path:
    base = Path(root) if root is not None else phase2_root()
    log_path = base / WRITE_LOG_RELPATH
    ensure_private_dir(log_path.parent)
    line = json.dumps(record, ensure_ascii=False)
    if not log_path.exists():
        # 首行经原子写创建，出生即 0600（无 umask 宽权限窗口）。
        atomic_write_text(log_path, line + "\n", mode=0o600)
    else:
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(log_path, 0o600)
    return log_path


# ---------------- 主入口 ----------------

def write_offgraph_learning(
    *,
    decision_json: "str | Path | dict",
    evidence_relpath: "str | None",
    store: "LearningStore | None" = None,
    rung: int = SUPPLEMENTARY_RUNG,
    track: str = TRACK,
    sim_engine: str = SIM_ENGINE,
    phase2_root_path: "str | Path | None" = None,
    log_writes: bool = True,
) -> dict[str, Any]:
    """把一次图外真人 approve 运行沉淀为 supplementary accepted 学习行。

    校验失败一律抛异常（OffgraphDecisionError / ValueError /
    FileNotFoundError），不做任何部分写入：decision 校验在读表前完成，
    relpath 校验在 LearningStore.add_learning 写表前完成。

    evidence_relpath 传 None 时按 decision 件绝对路径经
    LearningStore.relativize_evidence 双根换算（q2_human_gate 接线用）；
    CLI 必须显式传 ``--relpath``。
    """
    decision = load_approved_decision(decision_json)
    store = store if store is not None else LearningStore()
    if evidence_relpath is None:
        # None 仅表示「按双根自动换算」；换算失败（根外/不存在）照常拒写。
        decision_path = decision_json if not isinstance(decision_json, dict) \
            else None
        if decision_path is None:
            raise ValueError(
                "evidence_relpath 必填：dict 形态裁决件无法自动定位证据文件")
        evidence_relpath = store.relativize_evidence(decision_path)

    fields = build_q2_fields(decision)
    hd = decision["human_decision"]
    # 锁内适配（Task0a，纪律 12「空意见不得代笔」）：L297 草案括注描述的
    # approve/source/ts/card id 属裁决元数据（经结构校验 + 证据指针 + 操作
    # 日志留痕），不代笔成「人工意见」；human_opinion 只放真人逐字 notes，
    # 空串由 compose_text 渲染「（无）」。
    human_opinion = hd.get("human_notes", "") or ""
    run_id = decision["thread_id"].strip()
    rec = store.add_learning(
        run_id=run_id, track=track, rung=rung, sim_engine=sim_engine,
        gate_source=GATE_ACCEPTED_SUPPLEMENTARY,
        evidence_relpath=evidence_relpath,
        failure_symptom=fields["failure_symptom"],
        fix_action=fields["fix_action"],
        human_opinion=human_opinion, effective=True)

    if log_writes:
        _append_write_log({
            "ts_iso": utc_now_iso_z(),
            "kind": "offgraph_learning_write",
            "schema": DECISION_SCHEMA,
            "id": rec["id"],
            "run_id": run_id,
            "track": track,
            "sim_engine": sim_engine,
            "gate_source": rec["gate_source"],
            "rung": rec["rung"],
            "evidence_relpath": rec["evidence_relpath"],
            "decision_ts_iso": decision["ts_iso"],
            "decision_source": hd.get("source"),
            "card_message_id": hd.get("card_message_id"),
        }, phase2_root_path)
    return rec


def _main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(
        description="图外运行学习回写（纪律 12）：真人 approve decision.json "
                    "→ phase1_learnings 补沉淀一行")
    ap.add_argument("--decision", required=True,
                    help="decision.json 绝对/相对路径（schema %s）"
                         % DECISION_SCHEMA)
    ap.add_argument("--relpath", required=True,
                    help="证据 relpath：@runs/... 走 $SIMPRAXIS_RUN_DIR，"
                         "裸相对走 $SIMPRAXIS_PHASE1_ROOT")
    args = ap.parse_args(argv)
    rec = write_offgraph_learning(
        decision_json=args.decision, evidence_relpath=args.relpath)
    print(json.dumps(rec, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
