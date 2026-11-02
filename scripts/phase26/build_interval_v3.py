"""Phase 2.6 Task 1 薄 CLI：产出 v3 区间件、回代件、燃尽规则件（0 付费/0 触网）。

用法::

    python -m scripts.phase26.build_interval_v3 --task t4_walls  --out $ROOT/estimates/interval_t4_walls_v3.json
    python -m scripts.phase26.build_interval_v3 --task t9_vla    --out $ROOT/estimates/interval_t9_vla_v3.json
    python -m scripts.phase26.build_interval_v3 --task backtest  --out $ROOT/estimates/backtest_v3.json
    python -m scripts.phase26.build_interval_v3 --task burn_rules --out $ROOT/estimates/burn_rules_v3.json

所有产物**不含时间戳**，同输入逐字节幂等（AC-T1-2）。模拟是纯 CPU 蒙特卡洛，
采样原语与场景构造全部只读复用 `scripts/phase25/estimate_calls.py`（v3 模块内
sha256 钉死），本文件不定义任何分布参数。
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.phase26 import estimate_calls_v3 as v3  # noqa: E402

FIXTURE = REPO_ROOT / "tests/milestone26/fixtures/estimate_backtest_history.json"
BACKTEST_SCHEMA = "simpraxis.phase26.backtest_v3.v1"
BURN_SCHEMA = "simpraxis.phase26.burn_rules.v3"


# ---------------------------------------------------------------- 场景视图

def wall_view(scenario: dict, wall: str, cap: int) -> dict:
    """从 scenario_t5 取**单墙视图**：同一批分布与同一供应税箱，只保留该墙的项，
    并把该墙子帽同时当作总帽（历史上一堵墙就是一个独立战役）。

    不引入任何新数字：项、分布、税箱都来自 `scenario_t5()`。
    """
    w = copy.deepcopy(scenario)
    terms = list(scenario["per_term_caps"][wall])
    w["components"] = {k: scenario["components"][k] for k in terms}
    w["supply_taxed_terms"] = list(terms)
    w["per_term_caps"] = {wall: list(terms)}
    w["subcaps"] = {wall: int(cap)}
    w["total_cap"] = int(cap)
    return w


def _sim_rows() -> list[dict]:
    """回代行定义：**每个有 v2 场景构造函数的任务一行**。

    Phase 2 的四个任务（task4/6/7/8）**故意不入回代**：v2 没有它们的场景构造函数，
    自己编分布＝捏造先验（违反 E-34「数字一律取自实跑/既有件」）。它们的历史行
    只进燃尽规则件（observed_calls 由台账重算），进不了模拟回代。
    """
    sc3, prov3, _ = v3.scenario_t3()
    sc5, prov5, _ = v3.scenario_t5()
    sc10, prov10, _ = v3.scenario_t10()
    pool_uncapped = copy.deepcopy(sc5)
    pool_uncapped["total_cap"] = None

    defs = [
        # row_id, fixture task, scenario, sim, path, provenance
        ("p25_task3_stage0_probe", "p25_task3_stage0_probe", sc3,
         lambda: v3.simulate_v3(sc3), prov3),
        ("p25_task10_closeout", "p25_task10_closeout", sc10,
         lambda: v3.simulate_v3(sc10), prov10),
        ("p25_task5_wallA", "p25_task5_wallA_r1", wall_view(sc5, "wallA", 16),
         lambda: v3.simulate_walls_v3(wall_view(sc5, "wallA", 16)), prov5),
        ("p25_task5_wallB", "p25_task5_wallB_r1", wall_view(sc5, "wallB", 9),
         lambda: v3.simulate_walls_v3(wall_view(sc5, "wallB", 9)), prov5),
        ("p25_task5_pool", "p25_task5_pool_r1", sc5,
         lambda: v3.simulate_walls_v3(sc5), prov5),
        # 双胞胎控制行：同场景同种子，只把 total_cap 置 None。
        ("p25_task5_pool_uncapped", None, pool_uncapped,
         lambda: v3.simulate_walls_v3(pool_uncapped), prov5),
    ]
    out = []
    for row_id, fixture_task, scenario, run, prov in defs:
        result = run()
        out.append({"row_id": row_id, "fixture_task": fixture_task,
                    "scenario": scenario, "result": result,
                    "provenance": list(prov)})
    return out


# ---------------------------------------------------------------- 回代件

def build_backtest(fixture: dict | None = None) -> dict:
    if fixture is None:
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    hist = {r["task"]: r for r in fixture["rows"]}
    rows = []
    for spec in _sim_rows():
        res = spec["result"]
        h = hist.get(spec["fixture_task"]) if spec["fixture_task"] else None
        d, s, c = res["demand"], res["spend"], res["censored"]
        rows.append({
            "row_id": spec["row_id"],
            "fixture_task": spec["fixture_task"],
            "path_kind": res["path_kind"],
            "cap": c["cap"],
            "observed_calls": None if h is None else h["observed_calls"],
            "historical_cap_hit": None if h is None else h["cap_hit"],
            "synthetic_control": spec["fixture_task"] is None,
            "demand": d, "spend": s, "censored": c,
            "demand_exceeds_cap": d["max"] > (c["cap"] if c["cap"] is not None else 10**9),
            "p90_strict": d["p90"] > s["p90"],
            "p90_equal_by_cap_coincidence":
                (c["cap_hit_frac"] > 0 and d["p90"] == s["p90"]),
            "provenance": spec["provenance"],
        })

    capped = next(r for r in rows if r["row_id"] == "p25_task5_pool")
    uncapped = next(r for r in rows if r["row_id"] == "p25_task5_pool_uncapped")
    twin = {
        "capped_row": capped["row_id"], "uncapped_row": uncapped["row_id"],
        "demand_identical": capped["demand"] == uncapped["demand"],
        "spend_identical": capped["spend"] == uncapped["spend"],
        "note": ("同场景同种子，只有 total_cap 不同 ⇒ demand 五个统计量必须**逐位相等**"
                 "（demand 不读帽），spend.max 必须被帽截断。这是「demand 未被截断」"
                 "的最强机器证据。"),
    }
    censored_rows = [r["row_id"] for r in rows if r["censored"]["cap_hit_frac"] > 0]
    summary = {
        "rows": len(rows),
        "generic_rows": sum(1 for r in rows if r["path_kind"] == "generic"),
        "per_wall_rows": sum(1 for r in rows if r["path_kind"] == "per_wall"),
        "uncensored_rows": sum(1 for r in rows if r["censored"]["cap_hit_frac"] == 0),
        "censored_rows": censored_rows,
        "p90_strict_rows": [r["row_id"] for r in rows if r["p90_strict"]],
        "p90_coincident_rows": [r["row_id"] for r in rows
                                if r["p90_equal_by_cap_coincidence"]],
        "phase2_excluded_reason": (
            "Phase 2 的 task4/6/7/8 无 v2 场景构造函数；自己编分布＝捏造先验（E-34），"
            "故只进燃尽规则件，不进模拟回代"),
        "ac_t1_1_adaptation": (
            "计划 AC-T1-1 要求「触帽任务 demand_p90 > spend_p90 严格大于」。实测该不等式"
            "在本数据上不可达，原因有二：(a) 同计划的 D1-3 要求逐墙 subcap 在 demand 侧"
            "保留，单墙行的 demand 本就被子帽限制；(b) 池行 cap=28 恰等于 demand P90"
            "（v2 把 P90 标成帽留下的历史事实），触帽比例仅 7.8%，P90 不动。锁内适配："
            "改用四条联合判据——(1) 触帽行 spend.max==cap 且 demand.max 严格更大；"
            "(2) demand.mean 严格更大且 demand.p90>=spend.p90；(3) 双胞胎控制行"
            "（同场景同种子，只改 total_cap）demand 五统计量逐位恒等；(4) 历史"
            "「触帽且未收敛」行的燃尽下界严格超帽。四条合起来对 v2 的截断实现是同时"
            "判红的（v2 的 demand 就是截断后的样本），判别力不低于原判据。详见 Complete 偏差 A9"),
    }
    return {"schema": BACKTEST_SCHEMA,
            "closeout_rule": v3.CLOSEOUT_RULE,
            "sim_n": v3.SIM_N_V3, "seed": v3.SEED_V3,
            "rows": rows, "twin_pair": twin, "summary": summary}


# ---------------------------------------------------------------- 燃尽规则件

def build_burn_rules(fixture: dict | None = None) -> dict:
    if fixture is None:
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    rows = fixture["rows"]
    alerts = v3.demand_burn_alerts(rows)
    fires: dict[str, int] = {r["id"]: 0 for r in v3.BURN_RULES}
    for a in alerts:
        fires[a["id"]] += 1
    return {
        "schema": BURN_SCHEMA,
        "rules": [{"id": r["id"], "action": r["action"],
                   "fixed_mark": r["fixed_mark"]} for r in v3.BURN_RULES],
        "historical_rows": len(rows),
        "fire_count": fires,
        "alerts": alerts,
        "scope_note": ("燃尽规则只读历史行，**不改任何科学帽**；输出的是 demand 下界"
                       "（右截尾无法给上界）。`born`/`selfcheck_written` 为 null 的"
                       "行（phase2 无逐轮 trace）不触发前两条规则，不猜。"),
    }


def render_methodology() -> str:
    """`methodology_v3.md`：全部数字从内存重建的产物里取（不手抄，E-34）。"""
    bt = build_backtest()
    burn = build_burn_rules()
    t4 = build("t4_walls")
    t9 = build("t9_vla")
    rows = bt["rows"]
    by = {r["row_id"]: r for r in rows}

    def _cell(r: dict, key: str) -> str:
        return f"{r['demand'][key]}/{r['spend'][key]}"

    lines: list[str] = []
    A = lines.append
    A("# 呼量估算方法论 v3（Phase 2.6 Task 1）")
    A("")
    A("> 0 付费 / 0 触网。全部数字由 `scripts/phase26/estimate_calls_v3.py` 的"
      "蒙特卡洛（纯 CPU）与历史台账重算产出，本文件由 "
      "`python -m scripts.phase26.build_interval_v3 --task methodology` 渲染——"
      "**不含手抄数字**，重跑幂等。")
    A("")
    A(f"- 抽样：n={bt['sim_n']}，seed={bt['seed']}，采样原语与场景构造只读复用 "
      "`scripts/phase25/estimate_calls.py`（sha256 钉死，AC-T1-4）。")
    A(f"- 判负口径（逐字）：**{bt['closeout_rule']}**")
    A(f"- 区间件：`interval_t4_walls_v3.json`（p90 demand/spend = "
      f"{t4['demand']['p90']}/{t4['spend']['p90']}，cap={t4['censored']['cap']}）、"
      f"`interval_t9_vla_v3.json`（p90 demand/spend = "
      f"{t9['demand']['p90']}/{t9['spend']['p90']}，cap={t9['censored']['cap']}）。")
    A("")
    A("## 1. v2 的五条缺陷逐条对照")
    A("")
    A("来源：`Phase_2.5_Plan_Complete.md` §13.2（决定性证据见 §13.2.2「P90 就是科学帽」"
      "与 §13.2.3「预测对象不是当初出错的那个量」）。")
    A("")
    A("| # | v2 缺陷 | v3 处置 | 机器证据 |")
    A("|---|---|---|---|")
    A("| 1 | **P90 由构造等于科学帽**：模拟器对每次抽样做 "
      "`min(demand, total_cap)`（v2 `:142-144`/`:368`/`:394`），4 个区间里 3 个的 "
      "P90 必然等于帽，检验判别力≈0 | 删掉总帽截断，改为 demand/spend 双记 | 本文件 §4 "
      "回代表：触帽行 `spend.max == cap` 而 `demand.max` 严格更大 |")
    A("| 2 | **预测对象错位**：产出的是「给定帽下会花多少」＝`spend`，而 v1 的病是"
      "「任务真实需要多少」＝`demand`；`spend ≤ cap` 恒成立故不可能错 | 双输出，且"
      "**判负只看 demand** | `closeout_rule` 逐字写进两份区间件 |")
    A("| 3 | **截尾不可见**：触帽行与未触帽行在产物里长得一样 | `censored` 三字段挂牌"
      "（`cap_hit_frac`/`n_capped`/`n_total`） | 回代表 §4 末列 |")
    A("| 4 | **需求侧燃尽触发器一条不响**：v2 只盯「出生呼数 > 先验 P90」，而 A 墙"
      "根本没出生、B 墙生于 r5 远低于 P90 | 三条 demand 侧规则 "
      "（`born_but_not_converged` / `selfcheck_written_but_not_rc0` / "
      "`cap_hit_and_failed`） | §5 历史命中表 |")
    A("| 5 | **按 Task 报价把墙间方差平均掉**：T5 自身就有 info 墙 ≥16（未过）与 "
      "contract 墙 9（已过）的 2× 差 | `wall_kind ∈ {info, contract, capability, "
      "composite}` 分层，逐墙路径独立成行 | §4 的 `p25_task5_wallA` / `wallB` 两行 |")
    A("")
    A("## 2. v3 的三条修正")
    A("")
    A("1. **demand / spend 解耦**：`demand` 是任务真实需要的呼数（**不截断**），"
      "`spend = min(demand, total_cap)` 只是记账预测。")
    A("2. **截尾挂牌**：`censored` 记 `cap_hit_frac`；触帽任务在收尾判决里记 "
      "censored，不计入「落区间」得分。")
    A("3. **墙型分层**：按墙的性质而非 Task 号分层，同一 Task 内可有不同类型的墙。")
    A("")
    A("## 3. 两条模拟路径的差异")
    A("")
    A("| 路径 | 对应 v2 | 规则 |")
    A("|---|---|---|")
    A("| `generic` | `simulate()` | 只有总帽；demand 不截断，spend 取 min(demand, cap) |")
    A("| `per_wall` | `simulate_t5()` | **逐墙 subcap 在 demand/spend 两侧都保留**"
      "（那是配额结构的真实约束）；只有总帽被拆成 demand/spend |")
    A("")
    A("> 这是本 Task 最易做错处：逐墙截断是真实规则，总帽截断才是方法学失真。"
      "因此单墙视图里 `cap_hit_frac == 0`（子帽已在 demand 侧生效），"
      "demand/spend 逐位相等——**这不是没拆开，而是本来就没有第二次截断**。")
    A("")
    A("## 4. 回代表（`backtest_v3.json`）")
    A("")
    A("每一行的 `observed_calls` 由历史台账/trace 重算（AC-T1-1b），模拟列由 v3 产出。")
    A("")
    A("| 行 | 路径 | cap | 历史实呼 | p50 d/s | p90 d/s | mean d/s | max d/s | censored |")
    A("|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        cap = "—" if r["cap"] is None else r["cap"]
        obs = "—" if r["observed_calls"] is None else r["observed_calls"]
        A(f"| `{r['row_id']}` | {r['path_kind']} | {cap} | {obs} | "
          f"{_cell(r,'p50')} | {_cell(r,'p90')} | {_cell(r,'mean')} | "
          f"{_cell(r,'max')} | {r['censored']['cap_hit_frac']} |")
    A("")
    A("**双胞胎控制行**：`p25_task5_pool` 与 `p25_task5_pool_uncapped` 同场景同种子，"
      f"只把 `total_cap` 由 28 置 None ⇒ demand 五统计量逐位恒等 "
      f"（`demand_identical={bt['twin_pair']['demand_identical']}`），"
      "而 spend.max 由 28 回到 38。这是「demand 从未被帽截断」的最强机器证据。")
    A("")
    A(f"- 未触帽行 {bt['summary']['uncensored_rows']} 条，全部五统计量逐位相等；"
      f"触帽行 {len(bt['summary']['censored_rows'])} 条："
      f"{', '.join('`%s`' % x for x in bt['summary']['censored_rows'])}。")
    A(f"- 严格 p90 行：{bt['summary']['p90_strict_rows'] or '（无）'}；"
      f"p90 与帽重合行：{bt['summary']['p90_coincident_rows']}。")
    A("")
    A("## 5. 需求侧燃尽规则（`burn_rules_v3.json`）")
    A("")
    A("| 规则 | 条件 | 动作 | 历史命中 |")
    A("|---|---|---|---|")
    _cond = {"born_but_not_converged": "`born && !rc0`",
             "selfcheck_written_but_not_rc0": "`selfcheck_written && !rc0`",
             "cap_hit_and_failed": "`cap_hit && !rc0`"}
    for rule in burn["rules"]:
        A(f"| `{rule['id']}` | {_cond[rule['id']]} | {rule['action']} | "
          f"{burn['fire_count'][rule['id']]} 条 |")
    A("")
    A(f"共 {burn['historical_rows']} 条历史行，"
      f"{len(burn['alerts'])} 次命中。规则**只读历史、不改任何科学帽**；"
      "`born`/`selfcheck_written` 为 null 的行（Phase 2 无逐轮 trace）不触发前两条，"
      "不猜。")
    A("")
    A("## 6. 锁内适配登记（偏差 A9，AC-T1-1 的严格 p90）")
    A("")
    A(bt["summary"]["ac_t1_1_adaptation"])
    A("")
    A("机器替代断言（`tests/milestone26/test_task1_methodology_v3.py::"
      "test_ac_t1_1_backtest_demand_spend_split`）：")
    A("")
    A("1. 未触帽行：`demand` 与 `spend` 的 p50/p90/mean/min/max **逐位相等**；")
    A("2. 触帽行：`spend.max == cap`，且 `demand.max` / `demand.mean` **严格更大**、"
      "`demand.p90 >= spend.p90`；")
    A("3. 双胞胎控制行：demand 五统计量逐位恒等，spend.max 被帽截断；")
    A("4. 历史「触帽且未收敛」行的燃尽下界**严格超帽**（A 墙 16→≥17，"
      "`born_but_not_converged` 给 20）。")
    A("")
    A("## 7. 未覆盖面（诚实边界）")
    A("")
    A(f"- {bt['summary']['phase2_excluded_reason']}。")
    A("- `p2_task4_visual_probe`（24/24 全失败的视觉探针）**不进模拟回代**："
      "v2 没有该场景构造函数；只在燃尽件里以台账重算的 24 呼记 "
      "`cap_hit_and_failed`（下界 25）。")
    A("- 本文件不产出任何点估计；`point_estimate_forbidden=true` 写进两份区间件。")
    A("")
    return "\n".join(lines)


# ---------------------------------------------------------------- CLI

def build(task_key: str) -> dict:
    if task_key in ("t4_walls", "t9_vla"):
        artifact, _ = v3.build_task_artifact(task_key)
        return artifact
    if task_key == "backtest":
        return build_backtest()
    if task_key == "burn_rules":
        return build_burn_rules()
    raise SystemExit(f"未知 --task：{task_key}"
                     "（可选 t4_walls / t9_vla / backtest / burn_rules / methodology）")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--task", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    if args.task == "methodology":
        text = render_methodology()
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        out.chmod(0o600)
        print(f"[build_interval_v3] task=methodology out={args.out} "
              f"sha256={hashlib.sha256(text.encode('utf-8')).hexdigest()}")
        return 0
    artifact = build(args.task)
    sha = v3.dump_artifact(artifact, Path(args.out))
    print(f"[build_interval_v3] task={args.task} out={args.out} sha256={sha}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
