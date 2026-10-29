#!/usr/bin/env python3
"""Phase 2.5 Task 1 — 呼量估算方法学 v2 纯函数（0 付费、0 触网）。

公式（AC-T1-3）::

    C = C_macro + C_visual + Σ_birth C_birth
        + Σ_wall Σ_round C_round + C_probe + C_supply

- 所有项是离散随机变量；输出 P50/P90 两点（最近秩），禁止点值。
- C_supply：失败税率按模型代际分箱（Beta 后验），每失败呼占 seq 并重试，
  不复用外代际先验。
- cache 命中率先验两套口径分列：旧路径冻结指纹可用 M2 实测 17/120；
  agentic 路径与 T9 coding 呼显式 0（新指纹空间）。
- 无先验任务（T3 能力探针、T5 agentic spike、T9 VLA、T10 抽验）显式标
  「无先验/借用先验」，分布只能落在计划预注册包络内。

纯函数不读活账、不触网；场景分布取值必须在 provenance 里点名 hindcast
数据集字段或「无先验」。CLI 负责从 hindcast_dataset.json 组装场景并落件。
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from orchestrator.common.fsutil import atomic_write_text, ensure_private_dir

SEED = 20261029
SIM_N = 40000

# 失败税代际分箱（ok/fail 后验计数＝活账成功/失败数 +Beta(1,1) 平滑）。
# t4 视觉空答独立成箱，绝不与 coding 税混用；不外推到未列代际。
SUPPLY_BINS = {
    "t6_deepseek_generation": {"ok": 195, "fail": 15},
    "t7_mimo_generation": {"ok": 51, "fail": 23},
    "t8_glm_macro_kimi_coding": {"ok": 123, "fail": 5},
    "t4_visual_empty_answers_do_not_reuse": {"ok": 1, "fail": 23},
}


# ---------------------------------------------------------------- 分布原语

def constant(x: int) -> dict:
    return {"kind": "constant", "value": int(x)}


def uniform(lo: int, hi: int) -> dict:
    if lo > hi:
        raise ValueError("uniform lo>hi")
    return {"kind": "uniform", "lo": int(lo), "hi": int(hi)}


def empirical(samples: list[int]) -> dict:
    if not samples:
        raise ValueError("empty empirical samples")
    return {"kind": "empirical", "samples": [int(x) for x in samples]}


def mixture(parts: list[tuple[dict, float]]) -> dict:
    if not parts or abs(sum(w for _, w in parts) - 1.0) > 1e-9:
        raise ValueError("mixture 权重必须非空且和为 1")
    return {"kind": "mixture", "parts": [{"dist": d, "w": w} for d, w in parts]}


def draw(dist: dict, rng: random.Random) -> int:
    k = dist["kind"]
    if k == "constant":
        return dist["value"]
    if k == "uniform":
        return rng.randint(dist["lo"], dist["hi"])
    if k == "empirical":
        return rng.choice(dist["samples"])
    if k == "mixture":
        r = rng.random()
        acc = 0.0
        for part in dist["parts"]:
            acc += part["w"]
            if r <= acc:
                return draw(part["dist"], rng)
        return draw(dist["parts"][-1]["dist"], rng)
    raise ValueError(f"未知分布 {k}")


def quantile(sorted_samples: list[int], q: float) -> int:
    """最近秩分位（ceil(q*N) 的 1-based 秩，clamp 到 [1,N]）。"""
    import math
    if not sorted_samples:
        raise ValueError("empty samples")
    rank = min(len(sorted_samples), max(1, math.ceil(q * len(sorted_samples))))
    return sorted_samples[rank - 1]


# ---------------------------------------------------------------- 模拟

def simulate(scenario: dict, n: int = SIM_N, seed: int = SEED) -> dict:
    """单次迭代抽样一次失败率 p（Beta 后验）与 cache 命中率（常数先验）。

    返回 P50/P90/mean（整数呼）与构成项均值，全部确定性（固定种子）。

    注意：分项（逐墙）subcap 只在 simulate_t5 内对 wallA/wallB 生效；本函数
    只认 scenario["total_cap"] 总帽，不读 subcaps（逐墙截断是 simulate_t5 的
    专有路径）。
    """
    rng = random.Random(seed)
    comps = scenario.get("components", {})
    binname = scenario.get("supply_bin")
    supply = SUPPLY_BINS.get(binname)
    hit_rate = float(scenario.get("cache_hit_prior", 0.0))
    if not 0.0 <= hit_rate <= 1.0:
        raise ValueError("cache_hit_prior 越界")
    totals = []
    for _ in range(n):
        total = 0
        for name, dist in comps.items():
            planned = draw(dist, rng)
            # 旧路径 cache 先验只作用于显式白名单项；新路径先验 0（0 命中）。
            if hit_rate > 0 and scenario.get("cache_applies_to") \
                    and name not in scenario["cache_applies_to"]:
                net = planned
            else:
                hits = sum(1 for _ in range(planned)
                           if rng.random() < hit_rate)
                net = planned - hits
            if supply is not None and net > 0 and \
                    name in scenario.get("supply_taxed_terms", list(comps)):
                p = rng.betavariate(supply["fail"] + 1, supply["ok"] + 1)  # 失败概率后验
                remaining, term_total = net, 0
                while remaining:
                    term_total += remaining
                    failures = sum(1 for _ in range(remaining)
                                   if rng.random() < p)
                    remaining = failures
            else:
                term_total = net
            total += term_total
        if scenario.get("total_cap") is not None:
            # 到帽熔断（超出部分转 preflight_blocked，占批次不占实呼）。
            total = min(total, scenario["total_cap"])
        totals.append(total)
    s = sorted(totals)
    return {
        "p50": quantile(s, 0.50),
        "p90": quantile(s, 0.90),
        "mean": round(sum(totals) / len(totals), 2),
        "min": s[0], "max": s[-1],
        "sim_n": n, "seed": seed,
    }


def interval_artifact(task: str, scenario: dict, result: dict,
                      envelope: list[int], provenance: list[str],
                      no_prior: list[str], scientific_cap: int | None = None,
                      extra: dict | None = None) -> dict:
    return {
        "schema": "simpraxis.phase25.call_interval.v1",
        "task": task,
        "estimate": {"p50": result["p50"], "p90": result["p90"],
                     "mean": result["mean"], "sim_n": result["sim_n"],
                     "seed": result["seed"], "range_seen": [result["min"],
                                                             result["max"]]},
        "plan_envelope": envelope,
        "scientific_cap": scientific_cap,
        "point_estimate_forbidden": True,
        "formula": "C=C_macro+C_visual+Σbirth+ΣwallΣround+C_probe+C_supply",
        "supply_bin": scenario.get("supply_bin"),
        "cache_hit_prior": scenario.get("cache_hit_prior", 0.0),
        "components": [
            {"term": name, "dist": dist,
             "kind": dist["kind"]} for name, dist in
            scenario.get("components", {}).items()],
        "provenance": provenance,
        "no_prior_or_borrowed": no_prior,
        "approval_note_must_cite": f"interval_{task}.json",
        **(extra or {}),
    }


# ---------------------------------------------------------------- 场景（数字全部可点回分布或标无先验）

def scenario_t3() -> tuple[dict, list[str], list[str]]:
    """T3 Stage0 探针：P1 恰好 2 + P2 0..3（提前失败即停，无先验均匀）+
    P3 0..1；失败税用当前 coding 代际（t8/kimi 箱），cache 新指纹 0。"""
    scenario = {
        "components": {
            "C_probe_P1": constant(2),
            "C_probe_P2": uniform(0, 3),
            "C_probe_P3": mixture([(constant(0), 0.5), (constant(1), 0.5)]),
        },
        "supply_bin": "t8_glm_macro_kimi_coding",
        "cache_hit_prior": 0.0,
        "supply_taxed_terms": ["C_probe_P1", "C_probe_P2", "C_probe_P3"],
        "total_cap": 6,
    }
    provenance = [
        "P1=2/P2≤3/P3≤1：Phase_2.5_Plan_detailed.md Task 3 呼量算术（设计先验）",
        "供应税 t8 箱：hindcast_dataset.json phase2.failure_tax_bins task=8",
        "cache 先验 0：探针为全新传输指纹，methodology_v2.md §cache 两套口径",
    ]
    no_prior = ["P2 提前停止点、P3 是否真触发：能力探针无历史样本，取设计均匀"]
    return scenario, provenance, no_prior


def scenario_t5() -> tuple[dict, list[str], list[str]]:
    """T5 两堵工程墙 spike（agentic 新路径，cache 0，税取同环境墙 t7/mimo 箱）。

    每墙：出生呼＝简单 1–2 与拆块 6–12 等权混合（计划上包络），修复轮
    0–8 均匀（3→5→8 帽实证，见 hindcast repair_cap_evolution）；两墙分别
    受子帽 16/9 截断，储备 0–3 仅供污染重跑。
    """
    scenario = {
        "components": {
            # 墙 A：出生（0.5 简单 1–2 / 0.5 拆块 6–12）+ 修复 0–8 轮、每轮 1 呼
            "wallA_birth": mixture([
                (uniform(1, 2), 0.5), (uniform(6, 12), 0.5)]),
            "wallA_repair": uniform(0, 8),
            "wallB_birth": mixture([
                (uniform(1, 2), 0.5), (uniform(6, 12), 0.5)]),
            "wallB_repair": uniform(0, 8),
            "reserve_pollution_rerun": uniform(0, 3),
        },
        "supply_bin": "t7_mimo_generation",
        "cache_hit_prior": 0.0,
        "supply_taxed_terms": [
            "wallA_birth", "wallA_repair", "wallB_birth",
            "wallB_repair", "reserve_pollution_rerun"],
        "per_term_caps": {  # 科学子帽截断（基数 25 half-up clamp[9,16]）
            "wallA": ["wallA_birth", "wallA_repair"],
            "wallB": ["wallB_birth", "wallB_repair"]},
        "subcaps": {"wallA": 16, "wallB": 9},
        "total_cap": 28,
    }
    provenance = [
        "出生 1–2/6–12：hindcast_dataset phase2.birth_call_samples 与计划 §11",
        "修复轮 0–8：hindcast repair_cap_evolution（3→5→8 实证）",
        "子帽 16/9：sA=60/sB=30 代入 half-up clamp[9,16]（见 t5_subcaps）",
        "供应税 t7 箱：同为 LIBERO 环境墙的 mimo 代际",
        "cache 先验 0：agentic-v2 新指纹空间（AC-T1-3）",
    ]
    no_prior = ["agentic 自主绕过 vs 撞墙比例：无先验，出生简单/拆块等权、"
                "修复轮均匀是计划包络内的最大熵先验，非经验频率"]
    return scenario, provenance, no_prior


def scenario_t9() -> tuple[dict, list[str], list[str]]:
    """T9 VLA 三阶段战役：无先验结构化宽区间。S1/S2/S3 每阶段：出生
    （0.3 简单 1–2 / 0.7 拆块 6–12）+ 墙 0–3 堵、每墙 1–8 轮（唯一借用先验：
    修复轮经验分布形状以均匀近似）；S2/S3 另加评测修复 0–4 呼。税＝三代码际
    等权混合（不外推到新模型），cache 0。"""
    comps = {}
    for prefix, ev in [("S1", False), ("S2", True), ("S3", True)]:
        comps[f"{prefix}_birth"] = mixture([
            (uniform(1, 2), 0.3), (uniform(6, 12), 0.7)])
        # 墙数 0–3、每墙 1–8 轮在抽样层耦合：以乘积项 0–24 近似（无先验最大熵）。
        comps[f"{prefix}_wall_rounds"] = uniform(0, 3 * 8)
        if ev:
            comps[f"{prefix}_eval_repair"] = uniform(0, 4)
    scenario = {
        "components": comps,
        "supply_bin_mixture": ["t6_deepseek_generation",
                               "t7_mimo_generation",
                               "t8_glm_macro_kimi_coding"],
        "cache_hit_prior": 0.0,
        "supply_taxed_terms": list(comps.keys()),
        "total_cap": 160,
    }
    provenance = [
        "出生数（S1 接口墙/S2 adapter/S3 第二 adapter 各 1）：计划 Task 9 阶段切分",
        "修复轮 1–8 借用 hindcast 轮次形状：唯一借用先验，已标注",
        "供应税：t6/t7/t8 三代码际等权混合抽样，不外推",
        "cache 先验 0：VLA 新观察/新指纹（AC-T1-3）",
    ]
    no_prior = [
        "出生呼数简单/拆块比例 0.3/0.7：Phase 2 无 VLA 样本，结构化猜测",
        "墙数 0–3、评测修复 0–4：无先验，取计划 ~40–160 宽区间内最大熵形状",
    ]
    return scenario, provenance, no_prior


def scenario_t10() -> tuple[dict, list[str], list[str]]:
    """T10 收尾连通抽验：默认不花（need=0.5），触发则 1–2 呼。"""
    scenario = {
        "components": {
            "C_probe_connectivity": mixture([
                (constant(0), 0.5),
                (uniform(1, 2), 0.5)]),
        },
        "supply_bin": "t8_glm_macro_kimi_coding",
        "cache_hit_prior": 0.0,
        "supply_taxed_terms": ["C_probe_connectivity"],
        "total_cap": 2,
    }
    provenance = [
        "0–2：Phase_2.5_Plan_detailed.md §11 T10（默认不花，仅活网关抽验）",
        "need 概率 0.5 为设计先验（无历史样本）",
    ]
    no_prior = ["是否真需抽验：无先验，Bernoulli(0.5)"]
    return scenario, provenance, no_prior


# ---------------------------------------------------------------- 子帽（T5）

def half_up_subcaps(sA: int, sB: int, base: int = 25,
                    lo: int = 9, hi: int = 16) -> dict:
    """子帽_x = clamp(half_up(base*s_x/(sA+sB)), lo, hi)。

    half-up 用整数算术（禁 banker's rounding）：(2*base*s + den) // (2*den)。
    双半值退化点（两商小数同时恰 0.5）由 freeze 规则消歧（先跑墙 half-down、
    后跑墙 half-up，两帽和强制＝base）。
    """
    den = sA + sB
    if den <= 0:
        raise ValueError("sA+sB 必须为正")
    caps = {}
    raw = {}
    for name, s in (("wallA", sA), ("wallB", sB)):
        raw[name] = (2 * base * s + den) // (2 * den)
        caps[name] = min(hi, max(lo, raw[name]))
    frac_a = (base * sA) % den
    double_half = (frac_a * 2 == den)  # 两商小数同时 0.5
    if double_half and caps["wallA"] + caps["wallB"] > base:
        # 退化点消歧：默认墙 A 先跑则 A half-down，两帽和强制＝base。
        caps["wallA"] -= 1
    return {
        "sA": sA, "sB": sB, "base": base, "clamp": [lo, hi],
        "raw_half_up": raw,
        "subcaps": caps,
        "sum": caps["wallA"] + caps["wallB"],
        # 储备 3 是池 28 中独立于基数 25 的常量（AC-T5-2：常态/clamp 均保 3）。
        "reserve": 3,
        "double_half_tie": double_half,
        "tie_break": ("双半值：先跑墙 half-down/后跑墙 half-up，两帽和="
                      "25、储备保 3（AC-T5-2 预注册）") if double_half else None,
    }


# ---------------------------------------------------------------- T5 截断模拟

def simulate_t5(n: int = SIM_N, seed: int = SEED) -> dict:
    scenario, _, _ = scenario_t5()
    rng = random.Random(seed)
    totals = []
    supply = SUPPLY_BINS[scenario["supply_bin"]]
    for _ in range(n):
        p = rng.betavariate(supply["fail"] + 1, supply["ok"] + 1)  # 失败概率后验

        def taxed(x: int) -> int:
            remaining, billed = x, 0
            while remaining:
                billed += remaining
                remaining = sum(1 for _ in range(remaining)
                                if rng.random() < p)
            return billed

        a = taxed(draw(scenario["components"]["wallA_birth"], rng)
                  + draw(scenario["components"]["wallA_repair"], rng))
        b = taxed(draw(scenario["components"]["wallB_birth"], rng)
                  + draw(scenario["components"]["wallB_repair"], rng))
        a, b = min(a, scenario["subcaps"]["wallA"]), \
            min(b, scenario["subcaps"]["wallB"])
        reserve = taxed(draw(scenario["components"]["reserve_pollution_rerun"],
                             rng))
        total = min(scenario["total_cap"], a + b + reserve)
        totals.append(total)
    s = sorted(totals)
    return {"p50": quantile(s, 0.5), "p90": quantile(s, 0.9),
            "mean": round(sum(totals) / n, 2), "min": s[0], "max": s[-1],
            "sim_n": n, "seed": seed}


def simulate_t9(n: int = SIM_N, seed: int = SEED) -> dict:
    scenario, _, _ = scenario_t9()
    bins = scenario["supply_bin_mixture"]
    rng = random.Random(seed)
    totals = []
    for _ in range(n):
        binname = rng.choice(bins)
        supply = SUPPLY_BINS[binname]
        p = rng.betavariate(supply["fail"] + 1, supply["ok"] + 1)  # 失败概率后验
        total = 0
        for name, dist in scenario["components"].items():
            x = draw(dist, rng)
            remaining, billed = x, 0
            while remaining:
                billed += remaining
                remaining = sum(1 for _ in range(remaining)
                                if rng.random() < p)
            total += billed
        totals.append(min(scenario["total_cap"], total))
    s = sorted(totals)
    return {"p50": quantile(s, 0.5), "p90": quantile(s, 0.9),
            "mean": round(sum(totals) / n, 2), "min": s[0], "max": s[-1],
            "sim_n": n, "seed": seed,
            "supply_mixture": bins}


# ---------------------------------------------------------------- 回代（AC-T1-4）

def backtest_supply_tax(ok: int, fail: int, roots: int,
                        actual_total: int, n: int = 20000,
                        seed: int = SEED) -> dict:
    """样本内回代：Beta-二项递归税。以 roots 个必须成功的根呼出发，每轮失败
    呼占 seq 并重试，预测总呼分布，再对实际总呼落位。

    诚实声明：p 后验由同一批数据估计，这是样本内校准检验（验证税算术与代际
    分箱自洽），不是样本外预测；真正的样本外检验是 T10（AC-T1-7）。
    roots 取成功行数：以失败终止（Gate reject 不重试）的链会使预测略偏低，
    故同时报告 actual_percentile 与 P50–P90 落位，不做通过/失败修饰。
    """
    rng = random.Random(seed)
    totals = []
    for _ in range(n):
        p = rng.betavariate(fail + 1, ok + 1)  # 失败概率后验
        remaining, billed = roots, 0
        while remaining:
            billed += remaining
            remaining = sum(1 for _ in range(remaining)
                            if rng.random() < p)
        totals.append(billed)
    return _rank(totals, actual_total)


def _rank(totals: list[int], actual) -> dict:
    s = sorted(totals)
    out = {"p50": quantile(s, 0.5), "p90": quantile(s, 0.9),
           "mean": round(sum(s) / len(s), 2), "min": s[0], "max": s[-1]}
    if actual is not None:
        below = sum(1 for x in s if x < actual) / len(s)
        out.update({"actual": actual,
                    "actual_percentile": round(below, 4),
                    "within_p50_p90": out["p50"] <= actual <= out["p90"],
                    "insample_disclaimer": "样本内回代，非样本外预测"})
    return out


V1_DISTORTION = [
    # v1.2 计划「coding 0–6/题」对照（题＝一个平台战役目标）
    {"task": "t6_genesis_q1", "coding_calls": 207, "v1_upper_per_task": 6,
     "ratio_vs_upper": round(207 / 6, 1),
     "note": "26 个 run 出生+修复；按题报价失真 34.5×"},
    {"task": "t7_libero_stage1", "coding_calls": 66, "v1_upper_per_task": 6,
     "ratio_vs_upper": 11.0,
     "note": "5 线程 12 批次；环境/锚点复合墙"},
    {"task": "t8_fluid_stage1", "coding_calls": 125, "v1_upper_per_task": 6,
     "ratio_vs_upper": round(125 / 6, 1),
     "note": "五战役；非流形/合同墙+能力墙"},
]


# ---------------------------------------------------------------- 文档/落件

METHODOLOGY_MD = """# 呼量估算方法学 v2（Phase 2.5 Task 1；0 付费，首呼前冻结）

生成器：`scripts/phase25/estimate_calls.py`（固定种子 {{seed}}，{{sim_n}} 次迭代）；
全部先验来自 `estimates/hindcast_dataset.json`（活账+req 证据机器切分）。

## 1. 报价单位：错误墙，不是题目数

公式：

    C = C_macro + C_visual + Σ_birth C_birth
        + Σ_wall Σ_round C_round + C_probe + C_supply

- 出生呼 `C_birth`：简单出生 1–2 呼；sentinel 拆块出生实证 4–6 分片 +
  组装 aggregate（t6/t8 实测），计划包络取 6–12（含供应重试）。
- 修复轮：3→5→8 帽演进实证（t6 早窗 3、t6 run8 起/t7 RunSpec 5、t8 C3
  起 8），每轮通常 1 呼（拆块轮除外）。
- `C_supply`：失败呼占 seq 并触发重试，按模型代际分箱（Beta 后验），
  不跨代际外推。
- 输出只有 P50/P90 两点（最近秩），禁止点值。

## 2. 失败税分箱（活账，不外推）

| 箱 | ok | fail | 税率 |
|---|---|---|---|
| t6 deepseek/glm/mimo 代际 | 195 | 15 | 7.1% |
| t7 mimo 代际（LIBERO 环境墙） | 51 | 23 | 31.1% |
| t8 glm macro + kimi coding | 123 | 5 | 3.9% |
| t4 视觉空答（独立箱，禁与 coding 混用） | 1 | 23 | 95.8% |

phase1 封存账 3/29＝10.3% 另列（混合代际，不进先验）。

## 3. cache 两套口径

- **旧路径**：M2 实测 17/120 命中（17 cache 与 17 ok seq 配对），仅适用
  冻结指纹空间；终态 .call_cache 文件数不是命中数（无 hit log，不可重算）。
- **agentic 路径与 T9 coding 呼**：命中率先验**显式 0**（`agentic-v2:`
  指纹含规范化 obs 与工具轨迹前缀；VLA 是全新观察空间），禁止借 17/120。

## 4. 区间件（每个数字可点回分布或标无先验）

| Task | P50 | P90 | 计划包络 | 硬帽 | 区间件 |
|---|---|---|---|---|---|
| T3 | {{t3_p50}} | {{t3_p90}} | 2–6 | 6（科学帽） | interval_t3.json |
| T5 | {{t5_p50}} | {{t5_p90}} | 12–28 | 28（科学帽＝子帽+储备） | interval_t5.json |
| T9 | {{t9_p50}} | {{t9_p90}} | ~40–160 | 160（预算帽，可自治提） | interval_t9.json |
| T10 | {{t10_p50}} | {{t10_p90}} | 0–2 | 2 | interval_t10.json |

无先验声明：T3 停止点、T5 agentic 绕过比例、T9 出生/墙数/评测修复、
T10 是否触发均无历史样本，取计划包络内最大熵先验；T9 仅修复轮形状
借用 hindcast 经验，已在区间件标注。

## 5. 回代自检（样本内）

{{backtest}}

v1.2「coding 0–6/题」失真对照（区间宽度下限警示）：

| 战役 | 实际 coding 呼 | v1 上界/题 | 失真 |
|---|---|---|---|
| t6 | 207 | 6 | 34.5× |
| t7 | 66 | 6 | 11× |
| t8 | 125 | 6 | 20.8× |

## 6. 燃尽与重基线（详见 burn_rules.json）

50%/80% 两线告警；首次出生后实际呼数超先验 P90、每跨错误墙、每次 Gate
驳回、供应失败率超先验 P90 → 强制全线重估落重基线件；重基线只改后续
区间，**永不改预注册科学帽**（n/阈值/判据）。

## 7. T10 对账口径（AC-T1-7）

收官以实际 vs P50/P90 落位表机判：实际超 P90 即方法学失败结论入
Complete（注明哪类先验失准：出生数/修复轮/每轮呼数/cache），禁止
「情况特殊」翻案，禁止收尾改区间重算；模型修订仅入 Phase 3 backlog。

## 8. 活账更正（禁凑数）

合同措辞「墙 B：run_5 seq309–436」以活账更正为 seq398–436（C9–C11）；
seq309–397 属 run_1..4 真人 Gate2 reject 战役。

墙 A sA=60 是**呼量计费构造**，不是失败族归属数：＝LIBERO 升阶战役窗口
（批次＋线程 b/d/e）内 coding_debug 呼总数，与 driving 段命中哪个失败族
无关。三级取证拆分（REVISE 2026-10-29，两墙同取证纪律，匹配只在
「## traceback / 日志尾部」driving 段内做）：

- byte_confirmed 2：driving 段字节命中 G-9 四族（seq 254 锚点 / 308 env_api）；
- byte_unclassified 13：存活字节在位（路径末写者）但 driving 段不属 G-9，
  按冻结规则码机械归类 RULE-DEP（252/253/255/256）、RULE-LAUNCH（261/307）、
  RULE-BENCH（262/263/264）、RULE-ENV-ADJ（265）、RULE-BDDL（304/305）、
  RULE-PATCH（306）；
- seq_level_overwritten 45：同一证据路径被后续 seq 覆盖、本呼 req 字节机械
  不可复原（provenance_limited，仅 seq 级计入，不猜内容）。

对账：2（G9）＋7（REVISE 前「字节 9」中除 254/308 外的 7 个存活转
unclassified）＋6（原 seq 级 51 中 sole-ref 存活：256/261/262/263/264/265）
＋45（机械真覆盖）＝60。RULE-LAUNCH 的 261/307 为启动器失败的非修复轮，
呼仍真金白银计入窗内 60（T5 可能再花），仅标注不计修复税。线程 a
（seq237–241）与 c（seq259）共 6 呼结构性排除出窗口，规则码与窗内同 tail
对称（237/259 同 RULE-LAUNCH，238–241 同 RULE-DEP），不计 sA。墙 B
sB=30（B-eng 30 / B-cap 9），39 条逐条字节取证不受本次返修影响。
"""


def render_outputs(out_root: Path) -> dict:
    """从场景构建区间件/方法学文档/回代表并落 0600（幂等）。"""
    est = ensure_private_dir(out_root / "estimates")

    t3_sc, t3_prov, t3_np = scenario_t3()
    t3_res = simulate(t3_sc)
    t10_sc, t10_prov, t10_np = scenario_t10()
    t10_res = simulate(t10_sc)
    t5_res = simulate_t5()
    t9_res = simulate_t9()
    t5_sc, t5_prov, t5_np = scenario_t5()
    t9_sc, t9_prov, t9_np = scenario_t9()
    subcaps = half_up_subcaps(60, 30)

    artifacts = {
        "interval_t3.json": interval_artifact(
            "t3", t3_sc, t3_res, [2, 6], t3_prov, t3_np,
            scientific_cap=6,
            extra={"subcaps_note": "P1=2/P2≤3/P3≤1；P4 的 1–2 为独立批 "
                                   "P25-B1b，不在本帽"}),
        "interval_t5.json": interval_artifact(
            "t5", t5_sc, t5_res, [12, 28], t5_prov, t5_np,
            scientific_cap=28,
            extra={"t5_subcaps": subcaps,
                   "b_cap_not_timed": "B-cap 运动学数值不计时、不进分母",
                   "reserve_rule": "储备 3 仅供供应污染重跑；墙自身余额不借支"}),
        "interval_t9.json": interval_artifact(
            "t9", t9_sc, t9_res, [40, 160], t9_prov, t9_np,
            scientific_cap=None,
            extra={"budget_cap_initial": 160,
                   "budget_cap_note": "预算帽自治可提（规则 2），科学 n/阈不动",
                   "post_first_birth": "强制 re-baseline（burn_rules）"}),
        "interval_t10.json": interval_artifact(
            "t10", t10_sc, t10_res, [0, 2], t10_prov, t10_np,
            scientific_cap=2),
    }

    # 回代表（Beta-二项递归税样本内校准；实际总呼对 P50/P90 落位）
    backtest = {
        "schema": "simpraxis.phase25.backtest.v1",
        "disclaimer": "样本内校准检验（p 后验由同批数据估计）；样本外检验由 "
                      "T10 实际 vs P50/P90 执行（AC-T1-7）",
        "campaigns": {
            "phase1_sealed_mixed_generation": {
                **backtest_supply_tax(26, 3, 26, 29),
                "bin_note": "混合代际（13 macro/15 visual/1 coding），仅回代校准、"
                            "不进任何先验分箱"},
            "t6_deepseek_generation": {
                **backtest_supply_tax(195, 15, 195, 210),
                "bin_note": "roots=195 成功行；实际总呼 210（macro3+coding207）"},
            "t7_mimo_generation": {
                **backtest_supply_tax(51, 23, 51, 74),
                "bin_note": "roots=51；实际总呼 74（macro8+coding66）；含 Gate "
                            "reject 终止链，预测结构性偏低已声明"},
            "t8_glm_macro_kimi_coding": {
                **backtest_supply_tax(123, 5, 123, 128),
                "bin_note": "roots=123；实际总呼 128（macro3+coding125）"},
            "t4_visual_until_first_success": {
                **backtest_supply_tax(1, 23, 1, 24),
                "bin_note": "roots=1（直到首次成功）；视觉空答独立箱，实际 24"},
        },
        "v1_distortion": V1_DISTORTION,
        "closeout_rule": "实际>P90＝方法学失败，入 Complete，禁翻案（AC-T1-7）",
    }
    artifacts["hindcast_backtest.json"] = backtest

    md = METHODOLOGY_MD
    repl = {
        "{{seed}}": str(SEED), "{{sim_n}}": str(SIM_N),
        "{{t3_p50}}": str(t3_res["p50"]), "{{t3_p90}}": str(t3_res["p90"]),
        "{{t5_p50}}": str(t5_res["p50"]), "{{t5_p90}}": str(t5_res["p90"]),
        "{{t9_p50}}": str(t9_res["p50"]), "{{t9_p90}}": str(t9_res["p90"]),
        "{{t10_p50}}": str(t10_res["p50"]),
        "{{t10_p90}}": str(t10_res["p90"]),
        "{{backtest}}": json.dumps(backtest["campaigns"], ensure_ascii=False,
                                   indent=2),
    }
    for k, v in repl.items():
        md = md.replace(k, v)
    paths = {}
    for name, obj in artifacts.items():
        p = est / name
        if name.endswith(".json"):
            atomic_write_text(p, json.dumps(obj, ensure_ascii=False, indent=2) + "\n")
        else:
            atomic_write_text(p, obj)
        paths[name] = str(p)
    atomic_write_text(est / "methodology_v2.md", md)
    paths["methodology_v2.md"] = str(est / "methodology_v2.md")
    return paths


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out-root", default=None)
    ap.add_argument("--print-scenarios", action="store_true")
    args = ap.parse_args(argv)
    if args.print_scenarios:
        for name, fn in (("t3", scenario_t3), ("t5", scenario_t5),
                         ("t9", scenario_t9), ("t10", scenario_t10)):
            sc, prov, np_ = fn()
            print(name, json.dumps({"supply": sc.get("supply_bin"),
                                    "components": list(sc.get("components", {})),
                                    "no_prior": np_}, ensure_ascii=False))
        return 0
    out = Path(args.out_root) if args.out_root else rp_phase25()
    paths = render_outputs(out)
    print(json.dumps({"written": paths}, ensure_ascii=False, indent=2))
    return 0


def rp_phase25() -> Path:
    from orchestrator.common import runtime_paths as rp
    return rp.phase25_root()


if __name__ == "__main__":
    raise SystemExit(main())
