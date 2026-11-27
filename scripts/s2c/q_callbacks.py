"""S2c S2c-3：G-DESIGN 三物理回调（宿主侧，本地 mujoco，非付费）。

design_runner.check_design 注入：
- nc_check(design)       → (ok, detail)：base vs host-sham 亚阈 + 非退化（跨 nuisance 变异）
- pilot_effect(design)   → |DiD|：真 regime 下 actor 主对比效应绝对值
- discriminability(design) → {"min_pair_diff","variants"}：regime 低/高两档 DiD 差
全部基于物理仿真（受信 host mujoco），世界变体数值只回 private。
"""
from __future__ import annotations

import sys
from pathlib import Path
from decimal import Decimal
from typing import Mapping

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "s2b"))

from orchestrator.rsi_r1 import rubric as R
from orchestrator.rsi_r1 import scenes as S
from scripts.s2b.calibrate_s2b import _run_one, _make_spec
from scripts.s2c.open_tasks import scene_config, _DEFAULTS, _map_condition

_SCENE_NAME = {"C_collision_rebound": "C", "D_forced_vibration": "D"}


def _cell_params(design, brief, scene, regime, cell):
    return _map_condition(cell, brief, scene, regime)


def _run_cells(svc, design, brief, scene, regime, n_block, tag):
    """跑 design 的所有条件 × n_block，返回 condition_id → [metric]。"""
    study = _SCENE_NAME[scene]
    vals = {}
    for cell in design["conditions"]:
        params = _cell_params(design, brief, scene, regime, cell)
        cid = str(cell["id"])
        for blk in range(n_block):
            spec = _make_spec(study_id=study, condition_id=cid, block_id=f"b{blk}",
                              scene_id=scene, params=params)
            r = _run_one(svc, spec, f"{tag}-{cid}-{blk}")
            vals.setdefault(cid, []).append(r["metric_value"])
    return vals


def _nc_id(x):
    """negative_control 的 base/sham：兼容 str 或嵌套 {condition_id,...}。"""
    if isinstance(x, dict):
        return str(x.get("condition_id"))
    return str(x)


def _did(design, vals):
    pc = design["primary_contrast"]
    src = pc["terms"] if isinstance(pc.get("terms"), Mapping) else pc
    m = lambda c: sum(vals[c]) / len(vals[c])
    return ((m(src["treatment_high"]) - m(src["treatment_low"]))
            - (m(src["control_high"]) - m(src["control_low"])))


def make_callbacks(svc, brief, *, true_regime, regime_param, regime_lo, regime_hi,
                   n_block=2):
    """返回 (nc_check, pilot_effect, discriminability) 三回调（闭包捕获 brief/物理）。"""
    scene = brief.scene_id
    cfg = scene_config(scene)
    metric = brief.metrics[0]
    delta = float(R.metric_delta(metric))
    delta_ne = float(R.metric_delta(cfg["metric_ne"]))
    _ctr = {"n": 0}

    def _tick(tag):
        _ctr["n"] += 1
        return f"{tag}{_ctr['n']}"  # 每次回调/每次 gate 重算唯一，避免 RAW_EXISTS

    def pilot_effect(design):
        vals = _run_cells(svc, design, brief, scene, true_regime, n_block, _tick("pilot"))
        return abs(_did(design, vals))

    def discriminability(design):
        v_lo = _run_cells(svc, design, brief, scene,
                          {**true_regime, regime_param: regime_lo}, n_block, _tick("dlo"))
        v_hi = _run_cells(svc, design, brief, scene,
                          {**true_regime, regime_param: regime_hi}, n_block, _tick("dhi"))
        d_lo, d_hi = _did(design, v_lo), _did(design, v_hi)
        min_pair = abs(d_lo - d_hi)
        return {"min_pair_diff": min_pair,
                "variants": [{"regime": regime_lo, "did": d_lo},
                             {"regime": regime_hi, "did": d_hi}]}

    def nc_check(design):
        base = _nc_id(design["negative_control"]["base"])
        base_cell = next(c for c in design["conditions"] if str(c["id"]) == base)
        sp = cfg["sham_param"]
        lo_n, hi_n = cfg["nuisance_support"]
        effs = []
        for nuis_val in (lo_n, hi_n):
            reg = {**true_regime, cfg["nuisance_param"]: nuis_val}
            p_base = _cell_params(design, brief, scene, reg, base_cell)
            p_sham = dict(p_base); p_sham[sp] = str(Decimal(p_sham[sp]) + cfg["sham_perturb"])
            study = _SCENE_NAME[scene]
            tag = _tick("nc")
            rb = _run_one(svc, _make_spec(study_id=study, condition_id=base, block_id="nc_b",
                                          scene_id=scene, params=p_base), f"{tag}-{base}-{nuis_val}")["metric_value"]
            rs = _run_one(svc, _make_spec(study_id=study, condition_id="sham", block_id="nc_b",
                                          scene_id=scene, params=p_sham), f"{tag}-sham-{nuis_val}")["metric_value"]
            effs.append(rb - rs)
        sub = all(abs(e) < delta_ne for e in effs)
        nondeg = abs(effs[0] - effs[1]) > 1e-9
        ok = sub and nondeg
        detail = (f"sham_eff(lo)={effs[0]:.3g} sham_eff(hi)={effs[1]:.3g} "
                  f"Δ={abs(effs[0]-effs[1]):.3g} 亚阈={sub} 非退化={nondeg}")
        return ok, detail

    return nc_check, pilot_effect, discriminability
