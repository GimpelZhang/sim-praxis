"""Phase 2.6 呼量估算方法论 v3：demand / spend 双输出 + 截尾挂牌 + 墙型分层。

设计依据：`~/文档/Phase_2.6_Plan_detailed.md` T1.3–T1.5；v2 病根见
`Phase_2.5_Plan_Complete.md` §13.2。

三条修正（相对 v2）：
  1. **demand / spend 解耦**：demand 是「任务真实需要的呼数」（不截断），
     spend 是「到帽熔断后实际会计费的呼数」= min(demand, total_cap)。
     v2 只输出后者，于是「估算低估」与「配额截断」两件事被混成一个数，
     判负时无法区分方法学失败与预算失败。
  2. **截尾挂牌**：censored 记录 cap_hit_frac / n_capped / n_total；
     触帽任务在判决里记 censored、不计入「落区间」得分。
  3. **墙型分层**：`wall_kind ∈ {"info","contract","capability","composite"}`，
     按墙的**性质**而不是按 Task 号分层——同一 Task 里可以有不同类型的墙。

只读复用 `scripts/phase25/estimate_calls.py`（sha256 钉死，**不改它**）：
分布的抽样原语、供应税分箱与四个场景构造函数全部复用，避免「同一场景两处
定义」的漂移。v2 一改，本模块 import 期即 fail-closed。

判负只看 demand（`closeout_rule` 逐字写进产物）。spend 只用于记账预测。
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import random
from pathlib import Path

# ---------------------------------------------------------------- 只读复用 v2

_V2_PATH = (Path(__file__).resolve().parents[1] / "phase25"
            / "estimate_calls.py")
# 实施步骤 1 的取证值：$ROOT/estimates/phase25_estimator_sha256.txt
_V2_SHA256 = "c0c51f4ed845ae427b38f319773f32f03fda0689af6086c26cad97b47d1ad899"


def _load_v2():
    raw = _V2_PATH.read_bytes()
    got = hashlib.sha256(raw).hexdigest()
    if got != _V2_SHA256:
        raise RuntimeError(
            f"phase25 估页器 sha 漂移：{got} != {_V2_SHA256}（v2 是历史证据，"
            "不得修改；确需变更须走新预注册）")
    spec = importlib.util.spec_from_file_location("phase25_estimate_calls",
                                                  _V2_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_V2 = _load_v2()
draw = _V2.draw
quantile = _V2.quantile
SUPPLY_BINS = _V2.SUPPLY_BINS
scenario_t3 = _V2.scenario_t3
scenario_t5 = _V2.scenario_t5
scenario_t9 = _V2.scenario_t9
scenario_t10 = _V2.scenario_t10

WALL_KINDS = ("info", "contract", "capability", "composite")
SCHEMA_V3 = "simpraxis.phase26.call_interval.v3"

SIM_N_V3 = 20000
SEED_V3 = 20261102

CLOSEOUT_RULE = ("判负只看 demand：未触帽任务 demand 落区间外＝方法学失败；"
                 "触帽任务记 censored，不计入落区间得分")
CACHE_PRIOR_REASON = ("agentic-v3 指纹纪元重置（Task 3 F2/F4），"
                     "跨纪元命中率无先验价值")


# ---------------------------------------------------------------- 统计

def _stats(samples: list[int]) -> dict:
    """p50/p90/mean/min/max（mean 保留 2 位，与 v2 同口径）。"""
    if not samples:
        raise ValueError("empty samples")
    s = sorted(samples)
    return {"p50": quantile(s, 0.50), "p90": quantile(s, 0.90),
            "mean": round(sum(samples) / len(samples), 2),
            "min": s[0], "max": s[-1], "n": len(samples)}


def _censored(demands: list[int], spends: list[int], cap) -> dict:
    n_capped = sum(1 for d, sp in zip(demands, spends) if d != sp)
    return {"cap": cap, "n_capped": n_capped, "n_total": len(demands),
            "cap_hit_frac": round(n_capped / len(demands), 4)}


# ---------------------------------------------------------------- 通用路径

def _supply_for_iteration(scenario: dict, rng: random.Random):
    """单次迭代用哪个供应税箱：`supply_bin`（单箱）或 `supply_bin_mixture`
    （每迭代抽一箱，与 v2 `simulate_t9` 同法）。都没有则 None。"""
    if scenario.get("supply_bin_mixture"):
        return SUPPLY_BINS[rng.choice(scenario["supply_bin_mixture"])]
    name = scenario.get("supply_bin")
    return SUPPLY_BINS.get(name) if name else None


def _tax(x: int, p: float, rng: random.Random) -> int:
    """Beta-二项递归失败税（v2 逐字同法）：每轮失败的呼重试、全部计费。"""
    remaining, billed = x, 0
    while remaining:
        billed += remaining
        remaining = sum(1 for _ in range(remaining) if rng.random() < p)
    return billed


def simulate_v3(scenario: dict, *, n: int = SIM_N_V3,
                seed: int = SEED_V3) -> dict:
    """通用路径：**demand 不截断**，spend = min(demand, total_cap)。

    与 v2 `simulate()` 的唯一差别是删掉 `total = min(total, total_cap)` 那一行，
    改为同时记录 demand 与 spend（这正是 v3 的病根修正）。
    """
    rng = random.Random(seed)
    comps = scenario.get("components", {})
    hit_rate = float(scenario.get("cache_hit_prior", 0.0))
    if not 0.0 <= hit_rate <= 1.0:
        raise ValueError("cache_hit_prior 越界")
    cap = scenario.get("total_cap")
    demands, spends = [], []
    for _ in range(n):
        supply = _supply_for_iteration(scenario, rng)
        p = None
        if supply is not None:
            p = rng.betavariate(supply["fail"] + 1, supply["ok"] + 1)
        total = 0
        for name, dist in comps.items():
            planned = draw(dist, rng)
            if hit_rate > 0 and scenario.get("cache_applies_to") \
                    and name not in scenario["cache_applies_to"]:
                net = planned
            else:
                hits = sum(1 for _ in range(planned) if rng.random() < hit_rate)
                net = planned - hits
            if p is not None and net > 0 and \
                    name in scenario.get("supply_taxed_terms", list(comps)):
                term_total = _tax(net, p, rng)
            else:
                term_total = net
            total += term_total
        demands.append(total)
        spends.append(total if cap is None else min(total, cap))
    return {"demand": _stats(demands), "spend": _stats(spends),
            "censored": _censored(demands, spends, cap),
            "path_kind": "generic",
            "sim_n": n, "seed": seed}


# ---------------------------------------------------------------- 逐墙路径

def _wall_groups(scenario: dict) -> dict[str, list[str]]:
    """`per_term_caps` 给分组；没有就整组当一堵墙。"""
    groups = scenario.get("per_term_caps")
    if groups:
        return {w: list(terms) for w, terms in groups.items()}
    return {"all": list(scenario.get("components", {}))}


def simulate_walls_v3(scenario: dict, *, n: int = SIM_N_V3,
                      seed: int = SEED_V3) -> dict:
    """逐墙路径：每墙先各自受 subcap 截断（**真实配额规则，保留**），
    求和后再把 `total_cap` 拆成 demand/spend（**方法学失真，拆分**）。

    这是本 Task 最易做错处（计划 T1.5 步骤 3）：逐墙 subcap 是「真实规则」，
    总帽截断是「方法学失真」。前者在 demand/spend 两条路上**都保留**。
    """
    rng = random.Random(seed)
    comps = scenario.get("components", {})
    groups = _wall_groups(scenario)
    subcaps = scenario.get("subcaps", {})
    cap = scenario.get("total_cap")
    taxed_terms = scenario.get("supply_taxed_terms", list(comps))
    demands, spends = [], []
    for _ in range(n):
        supply = _supply_for_iteration(scenario, rng)
        p = None
        if supply is not None:
            p = rng.betavariate(supply["fail"] + 1, supply["ok"] + 1)
        total = 0
        for wall, terms in groups.items():
            raw = 0
            for term in terms:
                x = draw(comps[term], rng)
                raw += _tax(x, p, rng) if (p is not None and x > 0
                                           and term in taxed_terms) else x
            if wall in subcaps:
                raw = min(raw, subcaps[wall])
            total += raw
        for term in comps:
            if any(term in terms for terms in groups.values()):
                continue
            x = draw(comps[term], rng)
            total += _tax(x, p, rng) if (p is not None and x > 0
                                         and term in taxed_terms) else x
        demands.append(total)
        spends.append(total if cap is None else min(total, cap))
    return {"demand": _stats(demands), "spend": _stats(spends),
            "censored": _censored(demands, spends, cap),
            "path_kind": "per_wall",
            "sim_n": n, "seed": seed}


# ---------------------------------------------------------------- 产物

def interval_artifact_v3(task: str, wall_kind: str, scenario: dict,
                         result: dict, *, envelope: list[int],
                         scientific_cap: int | None,
                         provenance: list[str],
                         cache_hit_prior: float = 0.0) -> dict:
    """区间件（**不含时间戳**，故同输入逐字节幂等；AC-T1-2）。"""
    if wall_kind not in WALL_KINDS:
        raise ValueError(f"wall_kind 非法：{wall_kind!r}，合法 {WALL_KINDS}")
    if cache_hit_prior != 0.0:
        raise ValueError("v3 纪元 cache 先验必须为 0.0（跨纪元命中率无先验价值）")
    if result.get("path_kind") not in ("generic", "per_wall"):
        raise ValueError("result 必须带 path_kind ∈ {generic, per_wall}")
    return {
        "schema": SCHEMA_V3,
        "task": task,
        "wall_kind": wall_kind,
        "path_kind": result["path_kind"],
        "demand": result["demand"],
        "spend": result["spend"],
        "censored": result["censored"],
        "plan_envelope": list(envelope),
        "scientific_cap": scientific_cap,
        "supply_bin": scenario.get("supply_bin"),
        "supply_bin_mixture": scenario.get("supply_bin_mixture"),
        "components": [{"term": name, "dist": dist, "kind": dist["kind"]}
                       for name, dist in scenario.get("components", {}).items()],
        "subcaps": scenario.get("subcaps"),
        "cache_hit_prior": cache_hit_prior,
        "cache_hit_prior_reason": CACHE_PRIOR_REASON,
        "closeout_rule": CLOSEOUT_RULE,
        "point_estimate_forbidden": True,
        "provenance": provenance,
    }


def dump_artifact(obj: dict, path: Path) -> str:
    """确定性落盘（sort_keys + 固定缩进），返回 sha256。"""
    text = json.dumps(obj, ensure_ascii=False, indent=1, sort_keys=True) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- 需求侧燃尽触发器

def _born_but_not_converged(row: dict) -> bool:
    """已出生但没收敛：v2 的「出生呼数 > 出生先验 P90」对 A 墙一条不响
    （A 墙根本没出生），本规则盯的是「出生了却过不了」。"""
    return bool(row.get("born")) and not bool(row.get("rc0"))


def _selfcheck_written_but_not_rc0(row: dict) -> bool:
    """已写出自检件但 rc != 0：修复尾被饿死的典型现场。"""
    return bool(row.get("selfcheck_written")) and not bool(row.get("rc0"))


def _cap_hit_and_failed(row: dict) -> bool:
    """到帽且仍失败：右截尾，只能给下界。"""
    return bool(row.get("cap_hit")) and not bool(row.get("rc0"))


# 顺序固定：同一条记录可命中多条，输出按本序排列（可机判的前缀约束）。
BURN_RULES = (
    {"id": "born_but_not_converged",
     "cond": _born_but_not_converged,
     "action": "上修该墙 demand 至当前观测 ×1.25，并标记 cap_is_binding=true",
     "fixed_mark": {"cap_is_binding": True}},
    {"id": "selfcheck_written_but_not_rc0",
     "cond": _selfcheck_written_but_not_rc0,
     "action": "上修该墙 demand 至当前观测 + 3，并标记 repair_tail_starved=true",
     "fixed_mark": {"repair_tail_starved": True}},
    {"id": "cap_hit_and_failed",
     "cond": _cap_hit_and_failed,
     "action": "记 censored=true，demand 下界 = cap + 1（右截尾下界估计）",
     "fixed_mark": {"censored": True}},
)


def demand_burn_alerts(rows: list[dict]) -> list[dict]:
    """按 BURN_RULES 扫历史行，返回命中告警（含调整后的 demand 下界）。

    只读：不修改入参行，不改任何科学帽。
    """
    import math

    alerts = []
    for row in rows:
        observed = int(row.get("observed_calls", 0))
        cap = row.get("cap")
        for rule in BURN_RULES:
            if not rule["cond"](row):
                continue
            if rule["id"] == "born_but_not_converged":
                floor = math.ceil(observed * 1.25)
            elif rule["id"] == "selfcheck_written_but_not_rc0":
                floor = observed + 3
            else:
                floor = (int(cap) + 1) if cap is not None else observed + 1
            alerts.append({"id": rule["id"], "task": row.get("task"),
                           "observed_calls": observed,
                           "adjusted_demand_floor": floor,
                           "action": rule["action"],
                           "mark": dict(rule["fixed_mark"])})
    return alerts


# ---------------------------------------------------------------- 场景选择

def build_task_artifact(task_key: str) -> tuple[dict, list[str]]:
    """`--task` 到 (区间件, provenance) 的映射（供 build_interval_v3 复用）。

    t4_walls：历史墙 A/B 重测（A 信息墙 + B 合同墙 ⇒ composite），逐墙路径。
    t9_vla  ：LIBERO VLA 多 Stage 战役（能力墙），通用路径。
    """
    if task_key == "t4_walls":
        scenario, prov, _ = scenario_t5()
        result = simulate_walls_v3(scenario)
        return interval_artifact_v3(
            "t4_walls", "composite", scenario, result,
            envelope=[0, 28], scientific_cap=28,
            provenance=prov + [
                "Phase 2.6 Task 4 科学帽 28（提纲已定，v3 只解释不重定）",
                "逐墙 subcap 16/9 保留为真实配额规则，仅总帽 28 拆 demand/spend"]), prov
    if task_key == "t9_vla":
        scenario, prov, _ = scenario_t9()
        result = simulate_v3(scenario)
        return interval_artifact_v3(
            "t9_vla", "capability", scenario, result,
            envelope=[40, 160], scientific_cap=160,
            provenance=prov + [
                "Phase 2.6 Task 8 受账 Cap 70 为账本帽，非科学帽；本件给结构性"
                "宽区间，不解释账本帽截断"]), prov
    raise SystemExit(f"未知 task：{task_key}（可选 t4_walls / t9_vla）")
