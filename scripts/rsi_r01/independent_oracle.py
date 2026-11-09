#!/usr/bin/env python3
"""RSI-R0.1 独立 oracle：用**不同算法**复算沉降/效应/判决（AC17）。

与 ``orchestrator.rsi_r01.evaluator`` 的关系：

* **允许**共享 ``schemas`` 的常量（带宽、δ、dt、行数）——同一任务定义；
* **禁止** import 或调用 judge 的沉降/效应/判决函数。这里用后缀归约（先算
  ``inside`` 布尔数组，再从尾部折叠出「最早的全真后缀」），judge 用的是
  「从尾向前找最后一个越界索引」；两条路径互为交叉核对。

用途：T3 每条参考 run 逐条比对（索引/时刻/效应/判决），差异必须在预锁的
1e-12 相对界内；T8 之后也用它对真实观测复算。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from orchestrator.rsi_r01 import schemas as S  # 只取常量，不取计算函数

TOL = 1e-12


def inside_mask(theta: Sequence[float], omega: Sequence[float]) -> list[bool]:
    return [(abs(th) < S.THETA_BAND) and (abs(om) < S.OMEGA_BAND)
            for th, om in zip(theta, omega)]


def settle_suffix(theta: Sequence[float], omega: Sequence[float], t: Sequence[float]
                  ) -> dict[str, Any]:
    """后缀归约：从尾向前维护「从 i 到最后是否全在带内」的布尔折。

    与 judge 的反向扫描不同点在于：这里显式构造布尔序列并逐位折叠，
    对同一输入应得到相同的 index/time，但计算路径互不调用。
    """
    n = len(theta)
    if n == 0 or len(omega) != n or len(t) != n:
        return {"ok": False, "reason": "length_mismatch"}
    mask = inside_mask(theta, omega)
    all_in = True
    start = n              # 最早使 ∀j≥i 全真的 i
    for i in range(n - 1, -1, -1):
        if not mask[i]:
            all_in = False
            break
        start = i
    if not all_in and start == n and not mask[-1]:
        return {"ok": True, "censored": True, "index": None, "time_s": None,
                "rows": n}
    if start == n:
        # 全序列都在带内 ⇒ 最早 i=0
        start = 0
    censored = start > n - 1
    return {"ok": True, "censored": bool(censored),
            "index": None if censored else start,
            "time_s": None if censored else float(t[start]),
            "rows": n}


def effect_of(t_low: float | None, t_high: float | None) -> float | None:
    if t_low is None or t_high is None or t_low == 0.0:
        return None
    value = (t_low - t_high) / t_low
    if value != value or value in (float("inf"), float("-inf")):
        return None
    return value


def mean_effect_of(values: Sequence[float | None]) -> float | None:
    if not values or any(v is None for v in values):
        return None
    return sum(float(v) for v in values) / len(values)


def claim_of(mean: float | None, h: int) -> tuple[str, float | None]:
    if mean is None:
        return S.ClaimStatus.INCONCLUSIVE, None
    z = float(h) * float(mean)
    if z >= S.DELTA:
        return S.ClaimStatus.SUPPORTED, z
    if z <= -S.DELTA:
        return S.ClaimStatus.REFUTED, z
    return S.ClaimStatus.INCONCLUSIVE, z


def judge_from_observations(observations: Mapping[str, Mapping[str, Any]], h: int
                            ) -> dict[str, Any]:
    """独立复算一份四格观测的判决（不看 prereg、不看候选自报）。"""
    cells: dict[str, Any] = {}
    settle: dict[str, dict[str, Any]] = {}
    for key, doc in observations.items():
        st = settle_suffix(doc["theta"], doc["omega"], doc["t"])
        settle[key] = st
        cells[key] = st
    effects: list[float | None] = []
    for angle in ("plus", "minus"):
        low = settle.get(f"low/{angle}", {}).get("time_s")
        high = settle.get(f"high/{angle}", {}).get("time_s")
        effects.append(effect_of(low, high))
    mean = mean_effect_of(effects)
    claim, z = claim_of(mean, h)
    return {"settle": settle, "effects": effects, "mean_effect": mean,
            "z": z, "claim_status": claim}


def compare(judge: Mapping[str, Any], oracle: Mapping[str, Any]) -> list[str]:
    """逐项比对（差异超 1e-12 相对即报）。"""
    problems: list[str] = []
    j_claim = judge.get("claim_status")
    if j_claim != oracle.get("claim_status"):
        problems.append(f"claim_status {j_claim} ≠ {oracle.get('claim_status')}")
    jm, om = judge.get("mean_effect"), oracle.get("mean_effect")
    if (jm is None) != (om is None):
        problems.append(f"mean_effect 空性不一致 {jm} vs {om}")
    elif jm is not None and abs(float(jm) - float(om)) > TOL * max(1.0, abs(float(jm))):
        problems.append(f"mean_effect {jm} ≠ {om}")
    jz, oz = judge.get("z"), oracle.get("z")
    if (jz is None) != (oz is None):
        problems.append(f"z 空性不一致 {jz} vs {oz}")
    elif jz is not None and abs(float(jz) - float(oz)) > TOL * max(1.0, abs(float(jz))):
        problems.append(f"z {jz} ≠ {oz}")
    j_cells = judge.get("cells") or {}
    for key, o in (oracle.get("settle") or {}).items():
        j = j_cells.get(key) or {}
        if j.get("status") == "INVALID":
            problems.append(f"{key} judge 判 INVALID，oracle 仍可计算")
            continue
        settle = j.get("settle") or {}
        if bool(settle.get("censored")) != bool(o.get("censored")):
            problems.append(f"{key} censored 不一致")
            continue
        ji, oi = settle.get("index"), o.get("index")
        if ji != oi:
            problems.append(f"{key} index {ji} ≠ {oi}")
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="RSI-R0.1 独立 oracle")
    ap.add_argument("--observations", nargs="+", required=True,
                    metavar="CELL=PATH", help='例如 low/plus=a.json high/plus=b.json')
    ap.add_argument("--h", type=int, required=True, choices=(1, -1))
    args = ap.parse_args(argv)
    observations: dict[str, Any] = {}
    for item in args.observations:
        cell, _, path = item.partition("=")
        observations[cell] = json.loads(Path(path).read_text(encoding="utf-8"))
    out = judge_from_observations(observations, args.h)
    print(json.dumps(out, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
