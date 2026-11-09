#!/usr/bin/env python3
"""RSI-R0.1 负控与反例回归（AC18 / AC19 / AC13 边界，零模型调用）。

三条 lane：

1. **dispatch lane**（真实容器，R-dev/E-dev）：晚登记、漏格、重复同格、错 run、
   伪数据、单位错、符号错、错误结论——每类都走与候选相同的 ``TrialRunner``
   分派与判据，期望终态写在记录里（不把「抛任意异常」当通过）。
2. **contract lane**（无容器，合成合同序列）：摘要遗漏决定沉降末段的成对案例、
   恰等阈值、全删失/部分删失、``t_low=0``。
3. **AC19 反例回归**：旧 R0 的 200/2000 降采样误拒反例——旧件逐字节不变、差异
   可在新全分辨率数据上复现、新合同同数据正确分析 PASS 而改错 effect FAIL。

产物：``<campaign>/analysis/controls/controls.json``（含逐项 expected/observed）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Mapping

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "scripts" / "rsi_r01"))

from orchestrator.common import runtime_paths as RP  # noqa: E402
from orchestrator.rsi_r01 import evaluator as EV  # noqa: E402
from orchestrator.rsi_r01 import observations as O  # noqa: E402
from orchestrator.rsi_r01 import schemas as S  # noqa: E402
from orchestrator.rsi_r01 import task_contract as TC  # noqa: E402
from orchestrator.rsi_r01.trial import TrialRunner  # noqa: E402
import reference_actor as RA  # noqa: E402  （公开动作构造复用，不改其语义）

#: 旧 R0 反例记录（只读；AC19 要求逐字节不变）。
OLD_DEFECT = Path("/mnt/artifacts/simpraxis/rsi_r0/r0_20261108T0825Z/"
                  "protocol/instrument_defect_t6_rev_4.json")
OLD_DIFF = 0.004693908885525677
OLD_EFFECT_200 = 0.48717948717948717
OLD_EFFECT_FULL = 0.49187339606501285
OLD_TOL = 0.001


# ---------------------------------------------------------------------------
# 分析源码（负控变体：每次只动一个可见旋钮）
# ---------------------------------------------------------------------------

_R_TEMPLATE = '''\
import glob, json
BAND_T, BAND_W, H, SIGN, TAIL_DROP = {band_t!r}, {band_w!r}, {h!r}, {sign!r}, {tail_drop!r}
with open("/data/_analysis_order.json", "r", encoding="utf-8") as fh:
    order = json.load(fh)
docs = []
for rid in order:
    with open("/data/%s.json" % rid, "r", encoding="utf-8") as fh:
        docs.append(json.load(fh))

def settle(doc):
    th, om, t = doc["theta"], doc["omega"], doc["t"]
    if TAIL_DROP:
        th, om, t = th[:-TAIL_DROP], om[:-TAIL_DROP], t[:-TAIL_DROP]
    n = len(th)
    last_out = -1
    for i in range(n - 1, -1, -1):
        if not (abs(th[i]) < BAND_T and abs(om[i]) < BAND_W):
            last_out = i
            break
    if last_out == n - 1:
        return None
    return float(t[last_out + 1])

cells = {{}}
for doc in docs:
    p = doc["params"]
    cells[(float(p["damping"]), float(p["initial_angle"]))] = settle(doc)
dampings = sorted({{k[0] for k in cells}})
angles = sorted({{k[1] for k in cells}})
effects = {{}}
for angle in angles:
    t_low = cells.get((dampings[0], angle))
    t_high = cells.get((dampings[-1], angle))
    effects[str(angle)] = (None if t_low in (None, 0.0) or t_high is None
                           else SIGN * (t_low - t_high) / t_low)
vals = list(effects.values())
mean = None if any(v is None for v in vals) or not vals else sum(vals) / len(vals)
z = None if mean is None else H * mean
claim = "INCONCLUSIVE"
if z is not None and z >= {delta!r}:
    claim = "SUPPORTED"
elif z is not None and z <= -{delta!r}:
    claim = "REFUTED"
with open("/out/analysis.json", "w", encoding="utf-8") as fh:
    json.dump({{"kind": "r_analysis", "effects": effects, "mean_effect": mean,
              "z": z, "claim_status": claim}}, fh, sort_keys=True)
print("rows", len(docs))
'''

#: 分析容器只挂 /data 与 /out；run 顺序由这里落一份公开索引（不是私有答案）。
_ORDER_NAME = "_analysis_order.json"


def r_source(*, h: int, band_t: float = S.THETA_BAND, band_w: float = S.OMEGA_BAND,
             sign: int = 1, tail_drop: int = 0) -> str:
    return _R_TEMPLATE.format(band_t=band_t, band_w=band_w, h=h, sign=sign,
                              tail_drop=tail_drop, delta=S.DELTA)


def _write_order(runner: TrialRunner, run_ids: list[str]) -> None:
    path = runner.public_dir / _ORDER_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(run_ids), encoding="utf-8")


# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------


def _sha_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _new_trial(root: Path, trial_id: str, instance_id: str, gpus: bool) -> TrialRunner:
    return TrialRunner(campaign_root=root, instance_id=instance_id,
                       trial_id=trial_id, gpus=gpus)


def _prereg(runner: TrialRunner, plan: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return RA.emit(runner, {"schema": S.SCHEMA_ACTION, "action": "preregister",
                            "plan": plan if plan is not None else RA.prereg_plan(runner.instance)})


def _run_cells(runner: TrialRunner, cells: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    out = []
    inst = runner.instance
    for cell in cells:
        ev = RA.emit(runner, RA._run_action(inst, {"damping": cell["damping"],
                                                   "initial_angle": cell["theta0"]}))
        out.append(ev)
    return out


def _analyze(runner: TrialRunner, source: str, run_ids: list[str]) -> dict[str, Any]:
    _write_order(runner, run_ids)
    return RA.emit(runner, {"schema": S.SCHEMA_ACTION, "action": "analyze",
                            "source": source, "run_ids": run_ids})


def _claim_of_analysis(runner: TrialRunner, ev: Mapping[str, Any]) -> str:
    if not (ev.get("ok") and ev.get("analysis")):
        return S.ClaimStatus.INCONCLUSIVE
    doc = runner.analyses.read_output(ev["analysis"]["analysis_id"]) or {}
    return str(doc.get("claim_status") or S.ClaimStatus.INCONCLUSIVE)


def _submit(runner: TrialRunner, *, claim: str, run_ids: list[str],
            analysis_ids: list[str]) -> dict[str, Any]:
    return RA.emit(runner, {"schema": S.SCHEMA_ACTION, "action": "submit",
                            "claim": claim, "run_ids": run_ids,
                            "analysis_ids": analysis_ids})


def _ids(ev: Mapping[str, Any]) -> tuple[str | None, list[str]]:
    rid = ev.get("run_id")
    aids = ([ev["analysis"]["analysis_id"]]
            if ev.get("ok") and ev.get("analysis") else [])
    return rid, aids


def _rejected(events: list[Mapping[str, Any]], code: str) -> bool:
    return any((not e.get("ok")) and e.get("code") == code for e in events)


# ---------------------------------------------------------------------------
# dispatch lane：真实容器
# ---------------------------------------------------------------------------


def ctl_late_registration(root: Path, gpus: bool) -> dict[str, Any]:
    """晚登记/改登记：首实验前未登记拒绝；登记后不得再登记。"""
    runner = _new_trial(root, "ctl_late_registration", "R-dev", gpus)
    ev_run_early = RA.emit(runner, RA._run_action(
        runner.instance, {"damping": 0.15, "initial_angle": 0.8}))
    ev_prereg = _prereg(runner)
    cells = runner.instance.r_cells[:1]
    run_events = _run_cells(runner, cells)
    ev_prereg2 = _prereg(runner)
    runner.prereg = None
    ev_prereg3 = _prereg(runner)
    observed = {
        "run_before_prereg": ev_run_early.get("code"),
        "prereg_ok": bool(ev_prereg.get("ok")),
        "second_prereg": ev_prereg2.get("code"),
        "prereg_after_run": ev_prereg3.get("code"),
    }
    ok = (ev_run_early.get("code") == "PREREG_REQUIRED"
          and bool(ev_prereg.get("ok"))
          and all(e.get("ok") for e in run_events)
          and ev_prereg2.get("code") == "PREREG_DUPLICATE"
          and ev_prereg3.get("code") == "PREREG_TOO_LATE")
    return {"name": "late_or_changed_preregistration",
            "expected": "PREREG_REQUIRED / ok / PREREG_DUPLICATE / PREREG_TOO_LATE",
            "observed": observed, "rejected": ok}


def ctl_missing_cell(root: Path, gpus: bool) -> dict[str, Any]:
    """漏格：登记三格被拒；只跑三格则判据 analysis_valid=False。"""
    runner = _new_trial(root, "ctl_missing_cell", "R-dev", gpus)
    plan = RA.prereg_plan(runner.instance)
    plan = dict(plan, cells=plan["cells"][:3])
    ev_bad = _prereg(runner, plan)
    good_plan = RA.prereg_plan(runner.instance)
    ev_ok = _prereg(runner, good_plan)
    cells = runner.instance.r_cells[:3]
    run_events = _run_cells(runner, cells)
    run_ids = [e["run_id"] for e in run_events if e.get("run_id")]
    ev_an = _analyze(runner, r_source(h=1), run_ids)
    aid, aids = _ids(ev_an)
    _submit(runner, claim=_claim_of_analysis(runner, ev_an), run_ids=run_ids,
            analysis_ids=aids)
    judge = runner.judge()
    ok = (ev_bad.get("code") == "PREREG_INVALID" and bool(ev_ok.get("ok"))
          and judge["pass"] is False
          and judge["verdict"].get("analysis_valid") is False)
    return {"name": "missing_cell", "expected": "PREREG_INVALID + judge not pass",
            "observed": {"bad_prereg": ev_bad.get("code"),
                         "judge_pass": judge["pass"],
                         "analysis_valid": judge["verdict"].get("analysis_valid")},
            "rejected": bool(ok)}


def ctl_duplicate_cell(root: Path, gpus: bool) -> dict[str, Any]:
    """同格重复（重复跑后择优）被拒。"""
    runner = _new_trial(root, "ctl_duplicate_cell", "R-dev", gpus)
    _prereg(runner)
    _run_cells(runner, runner.instance.r_cells[:1])
    ev_dup = RA.emit(runner, RA._run_action(
        runner.instance, {"damping": runner.instance.r_cells[0]["damping"],
                          "initial_angle": runner.instance.r_cells[0]["theta0"]}))
    ok = ev_dup.get("code") == "CELL_DUPLICATED"
    return {"name": "duplicate_cell", "expected": "CELL_DUPLICATED",
            "observed": {"code": ev_dup.get("code")}, "rejected": bool(ok)}


def ctl_wrong_run(root: Path, gpus: bool) -> dict[str, Any]:
    """跨 trial / 不存在的 run 引用被拒。"""
    runner = _new_trial(root, "ctl_wrong_run", "R-dev", gpus)
    _prereg(runner)
    _run_cells(runner, runner.instance.r_cells[:1])
    ev = _submit(runner, claim="INCONCLUSIVE", run_ids=["run_deadbeef"],
                 analysis_ids=[])
    ok = ev.get("code") == "RUN_ID_UNKNOWN"
    return {"name": "wrong_run_id", "expected": "RUN_ID_UNKNOWN",
            "observed": {"code": ev.get("code")}, "rejected": bool(ok)}


def ctl_fake_data(root: Path, gpus: bool) -> dict[str, Any]:
    """可信原件被篡改：判据因 hash 不符拒绝加载（不采信被改数据）。"""
    runner = _new_trial(root, "ctl_fake_data", "R-dev", gpus)
    _prereg(runner)
    events = _run_cells(runner, runner.instance.r_cells[:1])
    rec = runner.runs[0]
    path = runner.trusted_dir / f"{rec.run_id}.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["theta"][100] = float(doc["theta"][100]) + 0.5
    path.write_text(S.canonical_json(doc) + "\n", encoding="utf-8")
    raised, code = None, None
    try:
        runner.judge()
    except O.ObservationIntegrityError as exc:
        raised = type(exc).__name__
        code = getattr(exc, "code", None)
    ok = raised is not None
    return {"name": "fake_data_tamper", "expected": "ObservationIntegrityError",
            "observed": {"raised": raised, "code": code,
                         "first_run_ok": bool(events[0].get("ok"))},
            "rejected": bool(ok)}


def _r_variant(root: Path, trial_id: str, *, source: str, gpus: bool,
               bad_submit: str | None = None) -> dict[str, Any]:
    runner = _new_trial(root, trial_id, "R-dev", gpus)
    _prereg(runner)
    events = _run_cells(runner, runner.instance.r_cells)
    run_ids = [e["run_id"] for e in events if e.get("run_id")]
    ev_an = _analyze(runner, source, run_ids)
    _, aids = _ids(ev_an)
    claim = _claim_of_analysis(runner, ev_an)
    _submit(runner, claim=claim, run_ids=run_ids, analysis_ids=aids)
    judge_honest = runner.judge()
    out = {"analysis_claim": claim, "judge_claim": (judge_honest["verdict"] or {}).get(
        "claim_status"), "pass": judge_honest["pass"]}
    if bad_submit is not None:
        _submit(runner, claim=bad_submit, run_ids=run_ids, analysis_ids=aids)
        judge_bad = runner.judge()
        out["bad_submit_claim"] = bad_submit
        out["bad_submit_pass"] = judge_bad["pass"]
        out["bad_submit_reason"] = judge_bad.get("submission_reason")
    return out


def ctl_unit_error(root: Path, gpus: bool) -> dict[str, Any]:
    """单位错：带阈值按「度」而不是 rad 解释（0.05°→0.00087 rad 不成立，
    这里按 0.05 rad 误当 0.05° 的常见错写为 2.86 rad），结论偏离真值。"""
    deg_band = S.THETA_BAND * 180.0 / 3.141592653589793
    obs = _r_variant(root, "ctl_unit_error",
                     source=r_source(h=1, band_t=deg_band, band_w=deg_band),
                     gpus=gpus)
    decisive = obs["analysis_claim"] != obs["judge_claim"]
    return {"name": "unit_error_band_degrees", "expected": "claim mismatch → not pass",
            "observed": obs, "rejected": bool(decisive and not obs["pass"]),
            "decisive": bool(decisive)}


def ctl_sign_error(root: Path, gpus: bool) -> dict[str, Any]:
    """符号错：效应方向写反（(t_high−t_low)/t_low），结论方向翻转。"""
    obs = _r_variant(root, "ctl_sign_error",
                     source=r_source(h=1, sign=-1), gpus=gpus)
    decisive = obs["analysis_claim"] != obs["judge_claim"]
    return {"name": "sign_error_effect", "expected": "claim mismatch → not pass",
            "observed": obs, "rejected": bool(decisive and not obs["pass"]),
            "decisive": bool(decisive)}


def ctl_wrong_conclusion(root: Path, gpus: bool) -> dict[str, Any]:
    """错误结论：分析正确但提交与复算相反的类别。"""
    honest_ev = None

    def _flip(claim: str) -> str:
        return (S.ClaimStatus.REFUTED if claim == S.ClaimStatus.SUPPORTED
                else S.ClaimStatus.SUPPORTED)

    runner = _new_trial(root, "ctl_wrong_conclusion", "R-dev", gpus)
    _prereg(runner)
    events = _run_cells(runner, runner.instance.r_cells)
    run_ids = [e["run_id"] for e in events if e.get("run_id")]
    ev_an = _analyze(runner, r_source(h=1), run_ids)
    _, aids = _ids(ev_an)
    true_claim = _claim_of_analysis(runner, ev_an)
    _submit(runner, claim=_flip(true_claim), run_ids=run_ids, analysis_ids=aids)
    judge = runner.judge()
    ok = judge["pass"] is False and str(judge.get("submission_reason", "")).startswith(
        "claim_mismatch")
    return {"name": "wrong_conclusion", "expected": "claim_mismatch → not pass",
            "observed": {"submitted": _flip(true_claim), "judge_claim": true_claim,
                         "pass": judge["pass"],
                         "reason": judge.get("submission_reason")},
            "rejected": bool(ok)}


def ctl_push_wrong_conclusion(root: Path, gpus: bool) -> dict[str, Any]:
    """工程族错误结论：run 达标却声称 INCONCLUSIVE。"""
    inst = TC.get("E-dev")
    runner = _new_trial(root, "ctl_push_wrong_conclusion", "E-dev", gpus)
    _prereg(runner)
    gains = inst.private["reference_gains"]
    ev = RA.emit(runner, RA._run_action(inst, {**gains,
                                               "steps": inst.private["reference_steps"]}))
    run_ids = [ev["run_id"]] if ev.get("run_id") else []
    ev_an = _analyze(runner, RA._PUSH_SOURCE, run_ids)
    _, aids = _ids(ev_an)
    _submit(runner, claim=S.ClaimStatus.SUPPORTED, run_ids=run_ids, analysis_ids=aids)
    judge_ok = runner.judge()
    _submit(runner, claim=S.ClaimStatus.INCONCLUSIVE, run_ids=run_ids, analysis_ids=aids)
    judge_bad = runner.judge()
    ok = judge_ok["pass"] is True and judge_bad["pass"] is False
    return {"name": "push_wrong_conclusion", "expected": "honest PASS → INCONCLUSIVE not pass",
            "observed": {"run_ok": bool(ev.get("ok")),
                         "honest_pass": judge_ok["pass"],
                         "bad_pass": judge_bad["pass"],
                         "bad_reason": judge_bad.get("submission_reason")},
            "rejected": bool(ok)}


def ctl_push_bad_gains(root: Path, gpus: bool) -> dict[str, Any]:
    """工程族低增益：L4 复算 FAIL，声称 SUPPORTED 也不通过。"""
    inst = TC.get("E-dev")
    runner = _new_trial(root, "ctl_push_bad_gains", "E-dev", gpus)
    _prereg(runner)
    ev = RA.emit(runner, RA._run_action(inst, {"kp": 1.0, "kd": 0.5, "max_force": 2.0,
                                               "steps": 500}))
    run_ids = [ev["run_id"]] if ev.get("run_id") else []
    ev_an = _analyze(runner, RA._PUSH_SOURCE, run_ids)
    _, aids = _ids(ev_an)
    _submit(runner, claim=S.ClaimStatus.SUPPORTED, run_ids=run_ids, analysis_ids=aids)
    judge = runner.judge()
    ok = judge["pass"] is False and (judge.get("verdict") or {}).get("status") == "FAIL"
    return {"name": "push_bad_gains", "expected": "L4 FAIL → not pass",
            "observed": {"run_ok": bool(ev.get("ok")),
                         "l4": (judge.get("verdict") or {}).get("status"),
                         "pass": judge["pass"]},
            "rejected": bool(ok)}


# ---------------------------------------------------------------------------
# contract lane：合成合同序列（AC13 边界 + AC18 第八类）
# ---------------------------------------------------------------------------

_FAKE_DAMS = (0.15, 0.45)


def synth_doc(*, damping: float, theta0: float, settle_index: int,
              out_at_end: bool = False) -> dict[str, Any]:
    """合成一份形状合规的摆观测（2001 行、dt=0.002、末点 4 s）。"""
    n = S.EXPECTED_ROWS
    t = [i * S.DT_S for i in range(n)]
    theta, omega = [], []
    for i in range(n):
        if i < settle_index:
            theta.append(theta0 * (1.0 - i / max(settle_index, 1)))
            omega.append(0.5 * (1.0 - i / max(settle_index, 1)))
        else:
            theta.append(0.0)
            omega.append(0.0)
    if out_at_end:
        theta[-1] = 0.2
        omega[-1] = 0.2
    return {"schema": S.SCHEMA_OBSERVATION, "kind": S.OBS_KIND_PENDULUM,
            "run_id": f"synthetic_{damping}_{theta0}",
            "trial_id": "synthetic", "instance_id": "R-dev",
            "params": {"damping": damping, "initial_angle": theta0,
                       "timestep": S.DT_S, "steps": S.N_STEPS},
            "t": t, "theta": theta, "omega": omega}


def _cells_with(settle: Mapping[str, int], *, out_high_plus_end: bool = False
                ) -> dict[str, dict[str, Any]]:
    docs = {}
    for level, damping in (("low", _FAKE_DAMS[0]), ("high", _FAKE_DAMS[1])):
        for angle, key in (("plus", "plus"), ("minus", "minus")):
            theta0 = 0.8 if key == "plus" else -0.8
            cell = f"{level}/{key}"
            docs[cell] = synth_doc(
                damping=damping, theta0=theta0, settle_index=settle[cell],
                out_at_end=bool(out_high_plus_end and cell == "high/plus"))
    return docs


def ctl_tail_omission() -> dict[str, Any]:
    """第八类：摘要遗漏决定沉降的末段——成对序列同摘要、全文件判决不同。"""
    settle = {"low/plus": 1000, "low/minus": 1000,
              "high/plus": 600, "high/minus": 600}
    clean = _cells_with(settle)
    tail_bad = _cells_with(settle, out_high_plus_end=True)
    # 摘要视角：丢掉每份文档最后一行后都"看起来"在带内（两序列同摘要）。
    def summary_view(docs: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
        out = {}
        for cell, doc in docs.items():
            st = EV.settling(doc["theta"][:-1], doc["omega"][:-1], doc["t"][:-1])
            out[cell] = st.as_dict()
        return out
    clean_sum = summary_view(clean)
    bad_sum = summary_view(tail_bad)
    same_summary = all(
        clean_sum[c]["index"] == bad_sum[c]["index"] for c in clean_sum)
    j_clean = EV.judge_research(observations=clean, h=1)
    j_bad = EV.judge_research(observations=tail_bad, h=1)
    # 摘要推算的结论：把摘要里的沉降当成真值，两序列都会得到 SUPPORTED。
    def claim_from_summary(summary: Mapping[str, Mapping[str, Any]]) -> str:
        t_low = summary["low/plus"]["time_s"]
        t_high = summary["high/plus"]["time_s"]
        if t_low in (None, 0.0) or t_high is None:
            return S.ClaimStatus.INCONCLUSIVE
        mean = (t_low - t_high) / t_low
        return EV.claim_of(mean, 1)[0]
    summary_claim = claim_from_summary(clean_sum)
    replay = EV.judge_research(observations=bad_sum, h=1)  # 摘要当原件的重放
    ok = (same_summary
          and j_clean.claim_status == S.ClaimStatus.SUPPORTED
          and j_bad.observation_status == S.ObservationStatus.CENSORED
          and j_bad.claim_status == S.ClaimStatus.INCONCLUSIVE
          and summary_claim == S.ClaimStatus.SUPPORTED
          and replay.observation_status == S.ObservationStatus.COMPLETE)
    return {"name": "summary_omits_decisive_tail",
            "expected": "同摘要 / 全文件 CENSORED+INCONCLUSIVE / 摘要估算 SUPPORTED 被拒",
            "observed": {"same_summary": same_summary,
                         "clean_claim": j_clean.claim_status,
                         "full_file_observation": j_bad.observation_status,
                         "full_file_claim": j_bad.claim_status,
                         "summary_claim": summary_claim,
                         "summary_replay_observation": replay.observation_status},
            "rejected": bool(ok), "decisive": True}


def ctl_numeric_boundaries() -> dict[str, Any]:
    """AC13：恰等阈值不在带内、全删失、部分删失、t_low=0。"""
    # 恰等阈值
    exactly = EV.inside_band(S.THETA_BAND, 0.0) or EV.inside_band(0.0, S.OMEGA_BAND)
    below = EV.inside_band(S.THETA_BAND * 0.999, 0.0)
    # 全删失
    n = S.EXPECTED_ROWS
    t = [i * S.DT_S for i in range(n)]
    all_out = EV.settling([0.5] * n, [0.0] * n, t)
    # 部分删失：low 正常、high 末点越界
    settle = {"low/plus": 1000, "low/minus": 1000,
              "high/plus": 600, "high/minus": 600}
    partial = _cells_with(settle, out_high_plus_end=True)
    j_partial = EV.judge_research(observations=partial, h=1)
    # t_low = 0
    zero = EV.effect(0.0, 1.0)
    ok = (exactly is False and below is True
          and all_out.censored is True
          and j_partial.observation_status == S.ObservationStatus.CENSORED
          and j_partial.mean_effect is None
          and j_partial.claim_status == S.ClaimStatus.INCONCLUSIVE
          and zero["value"] is None and zero["reason"] == "t_low_zero")
    return {"name": "numeric_boundaries",
            "expected": "恰等不入带/全删失/部分删失 null/t_low=0 null",
            "observed": {"exactly_threshold_inside": exactly,
                         "below_threshold_inside": below,
                         "all_censored": all_out.censored,
                         "partial_observation": j_partial.observation_status,
                         "partial_mean": j_partial.mean_effect,
                         "t_low_zero": zero},
            "rejected": bool(ok)}


# ---------------------------------------------------------------------------
# AC19：旧 200/2000 反例的双向回归
# ---------------------------------------------------------------------------


def ctl_old_counterexample(root: Path, gpus: bool) -> dict[str, Any]:
    """旧件不变 + 差异可复现 + 新合同同数据正确 PASS / 改错 effect FAIL。"""
    if not OLD_DEFECT.is_file():
        return {"name": "old_200_2000_counterexample",
                "expected": "旧件存在", "observed": {"missing": str(OLD_DEFECT)},
                "rejected": False, "skipped_reason": "OLD_ARTIFACT_MISSING"}
    before = _sha_file(OLD_DEFECT)
    record = json.loads(OLD_DEFECT.read_text(encoding="utf-8"))

    # 用新全分辨率可信观测复现（R-dev 与旧件同参数）。
    runner = _new_trial(root, "ctl_old_counterexample", "R-dev", gpus)
    _prereg(runner)
    cells = runner.instance.r_cells
    events = _run_cells(runner, cells)
    by_cell = {runner.runs[i].cell: runner.runs[i] for i in range(len(runner.runs))}
    full_docs, dec_docs = {}, {}
    for cell, rec in by_cell.items():
        doc = runner.store.load_trusted(rec.run_id, rec.manifest,
                                        expected_trial=runner.trial_id,
                                        expected_instance=runner.instance.instance_id)
        full_docs[cell] = doc
        dec_docs[cell] = {**doc,
                          "t": doc["t"][::10], "theta": doc["theta"][::10],
                          "omega": doc["omega"][::10]}
    j_full = EV.judge_research(observations=full_docs, h=1)
    j_dec = EV.judge_research(observations=dec_docs, h=1)
    diff = abs(float(j_full.mean_effect) - float(j_dec.mean_effect))

    # 新合同下：正确分析（全分辨率）提交 → PASS；改错 effect（按降采样结论）→ FAIL。
    run_ids = [rec.run_id for rec in runner.runs]
    ev_full = _analyze(runner, r_source(h=1), run_ids)
    _, aids = _ids(ev_full)
    _submit(runner, claim=_claim_of_analysis(runner, ev_full), run_ids=run_ids,
            analysis_ids=aids)
    judge_correct = runner.judge()
    wrong_claim = "REFUTED" if judge_correct["verdict"]["claim_status"] == "SUPPORTED" \
        else "SUPPORTED"
    _submit(runner, claim=wrong_claim, run_ids=run_ids, analysis_ids=aids)
    judge_wrong = runner.judge()

    after = _sha_file(OLD_DEFECT)
    reproduced = (abs(j_dec.mean_effect - record["mean_effect_candidate_view"]) < 1e-9
                  and abs(j_full.mean_effect - record["mean_effect_full_resolution"]) < 1e-9
                  and abs(diff - record["abs_diff"]) < 1e-9)
    ok = (before == after
          and reproduced
          and judge_correct["pass"] is True
          and judge_wrong["pass"] is False)
    return {"name": "old_200_2000_counterexample",
            "expected": "旧件 hash 不变；差异复现 1e-9；正确 PASS；改错 FAIL",
            "observed": {"old_sha_before": before, "old_sha_after": after,
                         "old_diff_recorded": OLD_DIFF, "new_diff": diff,
                         "decimated_mean": j_dec.mean_effect,
                         "full_mean": j_full.mean_effect,
                         "reproduced": reproduced,
                         "correct_pass": judge_correct["pass"],
                         "wrong_pass": judge_wrong["pass"]},
            "rejected": bool(ok)}


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def run_all(*, root: Path, gpus: bool = False) -> dict[str, Any]:
    dispatch = [ctl_late_registration, ctl_missing_cell, ctl_duplicate_cell,
                ctl_wrong_run, ctl_fake_data, ctl_unit_error, ctl_sign_error,
                ctl_wrong_conclusion, ctl_push_wrong_conclusion, ctl_push_bad_gains,
                ctl_old_counterexample]
    results: list[dict[str, Any]] = []
    for fn in dispatch:
        results.append(fn(root, gpus))
    results.append(ctl_tail_omission())
    results.append(ctl_numeric_boundaries())
    summary = {"schema": "simpraxis.rsi_r01.controls.v1",
               "controls": results,
               "total": len(results),
               "rejected_all": all(r.get("rejected") for r in results),
               "not_rejected": [r["name"] for r in results if not r.get("rejected")]}
    out_dir = Path(root) / "analysis" / "controls"
    out_dir.mkdir(parents=True, exist_ok=True)
    S.write_canonical_json(out_dir / "controls.json", summary)
    return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="RSI-R0.1 T3 负控与反例回归")
    ap.add_argument("--gpus", action="store_true")
    ap.add_argument("--only", default=None, help="只跑一个控制（名字或函数名）")
    args = ap.parse_args(argv)
    root = RP.rsi_r01_campaign_root()
    if not root.is_dir():
        raise SystemExit(f"campaign 根不存在：{root}")
    if args.only:
        table = {"late": ctl_late_registration, "missing": ctl_missing_cell,
                 "duplicate": ctl_duplicate_cell, "wrong_run": ctl_wrong_run,
                 "fake": ctl_fake_data, "unit": ctl_unit_error,
                 "sign": ctl_sign_error, "conclusion": ctl_wrong_conclusion,
                 "push_conclusion": ctl_push_wrong_conclusion,
                 "push_gains": ctl_push_bad_gains, "old": ctl_old_counterexample}
        fn = table.get(args.only)
        if fn is None:
            if args.only == "tail":
                print(json.dumps(ctl_tail_omission(), ensure_ascii=False, indent=1))
                return 0
            if args.only == "bounds":
                print(json.dumps(ctl_numeric_boundaries(), ensure_ascii=False, indent=1))
                return 0
            raise SystemExit(f"未知控制 {args.only!r}")
        out = fn(root, args.gpus)
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return 0 if out.get("rejected") else 3
    summary = run_all(root=root, gpus=args.gpus)
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    return 0 if summary["rejected_all"] else 3


if __name__ == "__main__":
    sys.exit(main())
