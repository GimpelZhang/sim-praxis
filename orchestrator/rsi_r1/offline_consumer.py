"""RSI-R1.2 T5：离线「公开消费者」（被测模型的确定性替身）。

本文件是**独立进程**脚本：只从 stdin 读一个 JSON 对象（正式最终 request 的公开
投影：``system`` / ``messages`` / ``max_tokens``），向 stdout 写**单条**动作 JSON。
它不 import 本仓库任何模块，也不读任何宿主路径（运行器以 ``python -I`` 在隔离
临时目录里拉起它，环境只给 ``PATH``），因此：

* 看不到私有 root / seed / raw / 维护侧判件，也看不到宿主 judge 的数值；
* 只能消费 request 里本来就公开的东西：可见状态 JSON、工具回包、公开估计量；
* 固定公开玩具算法（两条公开模板 + 公开 bootstrap 估计量）只是仪器 fixture，
  不当作自主研究贡献（Plan §4）。

协议动作序（与 ``protocol._ACTIONS_BLOCK`` / ``loop.ACTION_NAMES`` 一致）：

1. ``write_artifact(design)`` → 2. ``propose_plan`` → 3. ``run_batch`` →
4. ``write_artifact(analysis)`` → 5. ``analyze`` → 6. ``freeze_confirm`` →
7. ``run_confirm`` → 8. ``submit_claim``。

失败自修：宿主回包 ``ok=false`` 时重发**同一动作**（最多 2 次），这是合同允许的
字段手滑自修；真正越界（terminal）由宿主在发下一呼之前拦下，消费者没有机会。
"""
from __future__ import annotations

import json
import sys

# ---------------------------------------------------------------------------
# 公开计划（与 protocol.toy_plan_doc 同一公开 fixture；逐字复制可校验）
# ---------------------------------------------------------------------------

#: 场景 id 是**公开**事实：与 ``scenes.SCENE_A/SCENE_B`` 逐字一致（由
#: ``tests/test_rsi_r12_offline.py`` 的合同回归用例机判，不靠人工同步）。
SCENE_A = "A_contact_friction"
SCENE_B = "B_sampling_integration"

TOY_CONDITIONS = {
    "A": (
        {"id": "base", "scene_id": SCENE_A,
         "params": {"mass_kg": "0.5", "v0_mps": "0.4",
                    "sliding_friction": "0.4", "torsional_friction_m": "0.01"}},
        {"id": "low_mu", "scene_id": SCENE_A,
         "params": {"mass_kg": "0.5", "v0_mps": "0.4",
                    "sliding_friction": "0.2", "torsional_friction_m": "0.01"}},
        {"id": "sham", "scene_id": SCENE_A,
         "params": {"mass_kg": "0.5", "v0_mps": "0.4",
                    "sliding_friction": "0.4", "torsional_friction_m": "0.02"}},
    ),
    "B": (
        # 与 Plan L148 登记的 B 夹具逐值一致：共享基准 dt=.002/Tc=.008，只改
        # Tc(.002→.016)、只改 dt(.002→.001)；每 block 2000+2000+4000 = 8000
        # step。dt 取 .0005 会让每 block 翻倍（16000），aux 子信封按 8000 登记。
        {"id": "base", "scene_id": SCENE_B,
         "params": {"inertia_ratio": "1.0", "target_amplitude_rad": "0.2",
                    "target_freq_hz": "1.0", "tc_s": "0.008", "dt_s": "0.002",
                    "controller": "PD", "kp": "5", "kd": "0.1"}},
        {"id": "tc_only", "scene_id": SCENE_B,
         "params": {"inertia_ratio": "1.0", "target_amplitude_rad": "0.2",
                    "target_freq_hz": "1.0", "tc_s": "0.016", "dt_s": "0.002",
                    "controller": "PD", "kp": "5", "kd": "0.1"}},
        {"id": "dt_only", "scene_id": SCENE_B,
         "params": {"inertia_ratio": "1.0", "target_amplitude_rad": "0.2",
                    "target_freq_hz": "1.0", "tc_s": "0.008", "dt_s": "0.001",
                    "controller": "PD", "kp": "5", "kd": "0.1"}},
    ),
}

TOY_VARYING = {"A": {"mass_kg": ["0.2", "1.0"]},
               "B": {"inertia_ratio": ["0.5", "2.0"]}}

PLAN_SCHEMA = "simpraxis.rsi_r1.plan.v1"

DELTA = {"displacement_x_m": "0.001",
         "displacement_x_m_negative_equivalence": "0.001",
         "normalized_tracking_rmse": "0.01"}


def canonical_sha(doc) -> str:
    import hashlib

    blob = json.dumps(doc, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def toy_plan(study: str, *, phase: str = "explore", revision: int = 1,
             parent_sha=None, design_sha: str | None = None,
             analysis_sha: str | None = None) -> dict:
    conditions = [dict(c) for c in TOY_CONDITIONS[study]]
    if study == "A":
        primary = {"id": "primary", "kind": "primary",
                   "metric": "displacement_x_m", "proposition": "decrease_gt",
                   "delta": "0.001", "condition_ids": ["base", "low_mu"]}
        negative = {"id": "negative", "kind": "negative",
                    "metric": "displacement_x_m_negative_equivalence",
                    "proposition": "equivalent", "delta": "0.001",
                    "condition_ids": ["base", "sham"]}
        hypotheses = [
            {"id": "h1",
             "mechanism": "滑动摩擦做功耗散平动动能，摩擦系数越低位移越远",
             "predicted": "decrease_gt", "contrast_id": "primary",
             "falsification": "配对差区间完全落在 δ 之内即反证主解释"},
            {"id": "h2", "mechanism": "扭转摩擦只耦合转动，不改平动位移",
             "predicted": "equivalent", "contrast_id": "negative",
             "falsification": "阴性对比的位移区间越出 ±δ 即反证"},
        ]
    else:
        primary = {"id": "primary", "kind": "primary",
                   "metric": "normalized_tracking_rmse",
                   "proposition": "increase_gt", "delta": "0.01",
                   "condition_ids": ["base", "tc_only"]}
        negative = None
        hypotheses = [
            {"id": "h1",
             "mechanism": "控制周期变长削弱高频校正，跟踪误差变大",
             "predicted": "increase_gt", "contrast_id": "primary",
             "falsification": "Tc-only 与基准的 RMSE 差区间不越过 δ 即反证"},
            {"id": "h2", "mechanism": "积分步长变细只改离散误差，不改跟踪带宽",
             "predicted": "equivalent", "contrast_id": "primary",
             "falsification": "dt-only 对比的区间越出等效界即反证"},
        ]
    return {
        "schema": PLAN_SCHEMA, "schema_version": 1,
        "study_id": study, "revision": int(revision), "parent_sha": parent_sha,
        "hypotheses": hypotheses, "conditions": conditions,
        "nuisance_distribution": {"generator": "pcg64-host", "block_count": 32,
                                  "varying": dict(TOY_VARYING[study])},
        "primary": primary, "negative_control": negative,
        "analysis_source_sha": (analysis_sha if phase == "confirm" else None),
        "design_source_sha": (design_sha if phase == "confirm" else None),
        "phase": phase,
    }


# ---------------------------------------------------------------------------
# 公开设计模板（argv[1]=只读视图，argv[2]=输出目录；写 matrix.json）
# ---------------------------------------------------------------------------

DESIGN_SOURCE = '''\
import hashlib
import json
import sys
from pathlib import Path


def plan_sha(doc):
    blob = json.dumps(doc, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def main() -> int:
    view = Path(sys.argv[1])
    out = Path(sys.argv[2])
    plans = sorted(view.glob("plan-*.json"))
    if len(plans) != 1:
        print("expected exactly one plan, found %d" % len(plans), file=sys.stderr)
        return 2
    plan = json.loads(plans[0].read_text(encoding="utf-8"))
    blocks = ["b01", "b02"]           # 探索批只取前两个 block，省算力
    cells = []
    for cond in plan["conditions"]:
        for block_id in blocks:
            cells.append({"condition_id": cond["id"], "block_id": block_id,
                          "params": dict(cond["params"])})
    doc = {"schema": "simpraxis.rsi_r1.matrix.v1", "schema_version": 1,
           "study_id": plan["study_id"], "plan_sha": plan_sha(plan),
           "cells": cells}
    (out / "matrix.json").write_text(
        json.dumps(doc, sort_keys=True) + "\\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''

# ---------------------------------------------------------------------------
# 公开分析模板：逐 run 指标 + 公开 percentile bootstrap（预注册固定）
#
# 与宿主 ``metrics.py``/``statistics.py`` 的定义一致（公开方法）：
# * A：displacement = x[-1] − meta.initial_x_m；
# * B：0.5–4 s 窗口 q−target 的 RMS / 目标幅值；instability_events ⇒ LOSS_CAP；
# * 每 block 配对差 → mean effect → 10000 次 PCG64(20261111) 重抽样、
#   ``linear`` 分位、95%/90% 区间。
# stream 视图里有 ``*.meta.json`` 时优先用它取 initial_x_m / 条件名。
# ---------------------------------------------------------------------------

ANALYSIS_SOURCE = r'''import json
import math
import sys
from pathlib import Path

import numpy as np

SEED = 20261111
RESAMPLES = 10000
WINDOW = (0.5, 4.0)
AMPLITUDE_FALLBACK = 0.2
LOSS_VALUE = 10.0         # 量表 LOSS_CAP：硬失稳 run 记 10，不剔出配对（与宿主同义）


def amplitude_of(meta):
    """目标幅值：meta 顶层没有，真值在 ``spec_envelope.run`` 里（宿主 spec）。

    兜底只给旧夹具；与判决路径（``judge._metric_b`` 用 spec 的
    ``target_amplitude_rad``）不同读法会让 RMSE 差一个比例因子 ⇒ 逐字段比对
    必挂，所以这里按宿主真值路径读。
    """
    run = (meta.get("spec_envelope") or {}).get("run") or {}
    if "target_amplitude_rad" in run:
        return float(run["target_amplitude_rad"])
    if "target_amplitude_rad" in meta:
        return float(meta["target_amplitude_rad"])
    return AMPLITUDE_FALLBACK


def col(rows, index=0):
    """取序列的第 index 列；行可能是 ``[x, …]`` 也可能是标量 ``x``。

    判决路径 ``judge._col`` 对两种形状同样兼容（宽行取第 index 列、标量行
    只允许 index=0）；分析必须逐位复现它，所以这里保持同一读法。2026-11-12
    T5 演练实测：B 场景（单自由度）的 ``target_q``/``qpos`` 是标量序列，
    旧模板 ``float(row[0])`` 直接 ``TypeError`` ⇒ analyze 连续 31 呼失败。
    """
    out = []
    for row in rows:
        if isinstance(row, (list, tuple)):
            out.append(float(row[index]))
        else:
            if index != 0:
                raise SystemExit("scalar series cannot be indexed")
            out.append(float(row))
    return out


def metric_of(rec, meta):
    times = [float(t) for t in rec["time_s"]]
    if "target_q" in rec:
        events = rec.get("instability_events") or []
        if events:
            # 硬失稳按量表记 LOSS_CAP，**不剔出配对**（judge._metric_b 同义：
            # 剔一组就会与宿主 n_blocks/区间不一致）。
            return {"metric_id": "normalized_tracking_rmse", "value": LOSS_VALUE,
                    "loss": True, "n_samples": len(times)}
        target = col(rec["target_q"])
        q = col(rec["qpos"])
        lo, hi = WINDOW
        idx = [i for i, t in enumerate(times) if lo <= t <= hi]
        if len(idx) < 2:
            raise SystemExit("empty tracking window")
        sq = 0.0
        for i in idx:
            d = q[i] - target[i]
            sq += d * d
        amp = amplitude_of(meta)
        return {"metric_id": "normalized_tracking_rmse",
                "value": math.sqrt(sq / len(idx)) / amp,
                "loss": False, "n_samples": len(idx)}
    x = col(rec["qpos"])
    x0 = float(meta.get("initial_x_m", x[0]))
    return {"metric_id": "displacement_x_m", "value": x[-1] - x0,
            "loss": False, "n_samples": len(times)}


def interval(diffs, level):
    arr = np.asarray(diffs, dtype=np.float64)
    n = int(arr.shape[0])
    rng = np.random.default_rng(np.random.PCG64(SEED))
    idx = rng.integers(0, n, size=(RESAMPLES, n))
    means = arr[idx].mean(axis=1)
    alpha = (1.0 - level) / 2.0
    lo, hi = np.quantile(means, [alpha, 1.0 - alpha], method="linear")
    return float(arr.mean()), float(lo), float(hi)


def classify(prop, delta, lower, upper):
    if prop == "decrease_gt":
        if upper < -delta:
            return "SUPPORTED"
        return "REFUTED" if lower >= -delta else "INCONCLUSIVE"
    if prop == "increase_gt":
        if lower > delta:
            return "SUPPORTED"
        return "REFUTED" if upper <= delta else "INCONCLUSIVE"
    if lower > -delta and upper < delta:
        return "SUPPORTED"
    if upper <= -delta or lower >= delta:
        return "REFUTED"
    return "INCONCLUSIVE"


def main() -> int:
    view = Path(sys.argv[1])
    out = Path(sys.argv[2])
    meta = {}
    for path in sorted(view.rglob("*.meta.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(doc, dict) and doc.get("run_id"):
            meta[str(doc["run_id"])] = doc
    runs = {}
    for path in sorted(view.rglob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            rid = str(rec["run_id"])
            runs[rid] = {"block_id": str(rec["block_id"]),
                         "condition_id": str(meta.get(rid, {}).get(
                             "condition_id", rec.get("condition_id", ""))),
                         **metric_of(rec, meta.get(rid, {}))}
    plans = sorted(view.glob("plan-*.json"))
    if len(plans) != 1:
        raise SystemExit("expected exactly one plan")
    plan = json.loads(plans[0].read_text(encoding="utf-8"))
    contrasts = [dict(plan["primary"])]
    if plan.get("negative_control"):
        contrasts.append(dict(plan["negative_control"]))
    payload = []
    for spec in contrasts:
        treat, ctrl = spec["condition_ids"]
        pairs = {}
        for rid, row in runs.items():
            if row["condition_id"] not in (treat, ctrl):
                continue
            # 失稳 run 以 LOSS_CAP 值入场（judge 同义：宿主不剔除，n_blocks 才一致）
            pairs.setdefault(row["block_id"], {})[row["condition_id"]] = row["value"]
        diffs = [pairs[b][treat] - pairs[b][ctrl] for b in sorted(pairs)
                 if treat in pairs[b] and ctrl in pairs[b]]
        level = 0.90 if spec.get("kind") == "negative" else 0.95
        # 估计量与区间**逐字对齐**宿主判决路径（judge._judge_contrast）：
        # 配对差恒为 treat-ctrl，不按主张方向翻符号，也不为 equivalent 取负。
        # 宿主用同一条 bootstrap（PCG64、seed 20261111、10000 次、linear 分位）
        # 独立重算，再按 1e-8+1e-6|ref| 逐字段比对；任何符号约定差异都会被判成
        # INVALID_NUMERICS。这是仪器对齐（候选必须实现公开量表写明的估计量），
        # 不是放水：分类边界仍由 δ 与区间独立判定。
        effect, lower, upper = interval(diffs, level)
        delta = float(spec["delta"])
        status = classify(spec["proposition"], delta, lower, upper)
        payload.append({"contrast_id": spec["id"],
                        "metric_id": spec["metric"],
                        "proposition": spec["proposition"],
                        "estimate": effect, "lower": lower, "upper": upper,
                        "status": status, "n_blocks": len(diffs)})
    doc = {"schema": "simpraxis.rsi_r1.analysis_output.v1",
           "run_ids": sorted(runs), "contrasts": payload}
    (out / "analysis.json").write_text(
        json.dumps(doc, allow_nan=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


# ---------------------------------------------------------------------------
# 决策：只读 request 里的公开事实
# ---------------------------------------------------------------------------

def _task_brief(messages):
    decoder = json.JSONDecoder()
    for msg in messages:
        content = str(msg.get("content", ""))
        if not content.startswith("### 当前可见状态"):
            continue
        start = content.find("{")
        if start < 0:
            continue
        try:
            # 正式 brief 在 JSON 之后还有固定提示句（"请给出你的下一个动作…"），
            # 用 raw_decode 只消费首个完整对象，不假设消息以 JSON 结尾。
            doc, _ = decoder.raw_decode(content[start:])
        except ValueError:
            continue
        if isinstance(doc, dict):
            return doc
    return None


def _last_result(messages):
    for msg in reversed(messages):
        content = str(msg.get("content", ""))
        if content.startswith("{"):
            try:
                doc = json.loads(content)
            except ValueError:
                continue
            if isinstance(doc, dict) and "ok" in doc:
                return doc
    return None


def _prev_reply(messages):
    for msg in reversed(messages):
        if msg.get("role") != "assistant":
            continue
        try:
            doc = json.loads(str(msg.get("content", "")))
        except ValueError:
            return None
        return doc if isinstance(doc, dict) else None
    return None


def _prev_action(messages, last):
    """上一条动作名：优先取**宿主回包**的结构字段，不靠 assistant 原文。

    宿主 ``Turn.clipped()`` 把历史里的模型原稿截到 ``TOOL_REPLY_MAX_BYTES``
    （4096 字节）并追加 ``[TRUNCATED_BY_HOST]``：第 7 呼的 analysis 原稿有
    5.5 KB，第 8 呼请求里它已不是合法 JSON，``json.loads`` 必然失败。回包 JSON
    走的是**保结构**截断（``ok``/``action``/``kind`` 等键恒在），所以动作名
    只能从回包里读（2026-11-12 T5 实测：靠原稿解析 ⇒ prev 为空 ⇒ 消费者把
    已完成的 write_artifact(analysis) 之后误判成重跑 run_batch）。
    """
    name = str((last or {}).get("action") or "")
    if name:
        return name
    prev = _prev_reply(messages) or {}
    return str(prev.get("action") or "")


def decide(messages):
    vis = _task_brief(messages)
    if not isinstance(vis, dict):
        raise SystemExit("no visible state in request")
    study = str(vis.get("study") or "A")
    art = vis.get("artifacts") or {}
    designs = [str(s) for s in (art.get("design") or [])]
    analyses = [str(s) for s in (art.get("analysis") or [])]
    plan = vis.get("plan")
    last = _last_result(messages) or {}
    prev = _prev_reply(messages) or {}
    prev_name = _prev_action(messages, last)
    # 失败自修：同一动作原样重发（宿主回包明确 ok=false 时）
    if prev and last.get("ok") is False and prev_name:
        return prev
    if not designs:
        return {"action": "write_artifact", "kind": "design",
                "source": DESIGN_SOURCE}
    if plan is None:
        return {"action": "propose_plan", "plan": toy_plan(study)}
    if vis.get("last_batch") is None:
        return {"action": "run_batch", "plan_sha256": canonical_sha(plan),
                "design_sha256": designs[-1], "phase": "explore"}
    if not analyses:
        return {"action": "write_artifact", "kind": "analysis",
                "source": ANALYSIS_SOURCE}
    if prev_name == "write_artifact" or (
            prev_name == "analyze" and not last.get("ok")):
        ids = [str(r) for r in ((vis.get("last_batch") or {}).get("run_ids")
                                or [])]
        return {"action": "analyze", "source_sha256": analyses[-1],
                "run_ids": ids}
    if prev_name == "analyze" and last.get("ok"):
        confirm = toy_plan(study, phase="confirm",
                           revision=int((plan or {}).get("revision") or 1) + 1,
                           parent_sha="host",
                           design_sha=designs[-1],
                           analysis_sha=analyses[-1])
        return {"action": "freeze_confirm", "plan_sha256": "host",
                "design_sha256": designs[-1], "analysis_sha256": analyses[-1],
                "plan": confirm}
    if prev_name == "freeze_confirm" and last.get("ok"):
        token = str(last.get("confirm_token")
                    or (vis.get("confirm_token") or ""))
        return {"action": "run_confirm", "confirm_token": token}
    if prev_name == "run_confirm" and last.get("ok"):
        output = last.get("candidate_output") or {}
        contrasts = {str(c.get("contrast_id")): c
                     for c in (output.get("contrasts") or [])
                     if isinstance(c, dict)}
        doc = plan if isinstance(plan, dict) else {}
        claims = []
        for hyp in sorted(doc.get("hypotheses") or [],
                          key=lambda h: str(h.get("id"))):
            row = contrasts.get(str(hyp.get("contrast_id")))
            if row is None:
                raise SystemExit("missing candidate contrast")
            metric = str(row["metric_id"])
            claims.append({"hypothesis_id": str(hyp["id"]),
                           "proposition": str(row["proposition"]),
                           "metric_id": metric,
                           "delta": float(DELTA[metric]),
                           "estimate": float(row["estimate"]),
                           "lower": float(row["lower"]),
                           "upper": float(row["upper"])})
        body = ("离线公开消费者报告（study %s）：这是仪器 fixture 的确定性输出，"
                "不构成研究结论。" % study)
        return {"action": "submit_claim", "claims": claims,
                "report_text": body}
    # 其余情形：从可见状态重推一次 run_batch（例如 freeze 失败后的重规划）
    if plan is not None:
        return {"action": "run_batch", "plan_sha256": canonical_sha(plan),
                "design_sha256": designs[-1], "phase": "explore"}
    raise SystemExit("consumer state machine stalled")


def main() -> int:
    raw = sys.stdin.read()
    payload = json.loads(raw)
    messages = list(payload.get("messages") or [])
    reply = decide(messages)
    sys.stdout.write(json.dumps(reply, ensure_ascii=False, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
