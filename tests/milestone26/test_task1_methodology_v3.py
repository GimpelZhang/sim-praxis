"""Phase 2.6 Task 1 方法学 v3 机判（AC-T1-1 / 1b / 2 / 3 / 4；0 付费、0 联网）。

**一条 AC 一个测试函数**：计划给的机判命令用 `-k "ac_t1_N"` 且期望 `1 passed`，
pytest 的 `-k` 是子串匹配，所以每个 AC id 只能出现在唯一一个用例名里。

覆盖：
  1. **AC-T1-1**（`-k "ac_t1_1 and not ac_t1_1b"`）：回代件里未触帽行
     `demand == spend` 五个统计量逐位相等；触帽行 `spend.max == cap` 且
     `demand.max` / `demand.mean` 严格更大、`demand.p90 >= spend.p90`；
     双胞胎控制行（同场景同种子、只改 total_cap）`demand` 五统计量**逐位恒等**；
     两条模拟路径（generic / per_wall）都有回代行；落盘证据件与内存重建相等。
  2. **AC-T1-1b**：夹具 provenance —— 每行 `source_relpaths` 真实存在、
     `observed_calls` 与来源重算一致、`BACKTEST_SHA256.txt` 与夹具相符、
     生成器重跑逐行相等（防手工改数）。
  3. **AC-T1-2**：两份区间件 schema/wall_kind/cache 先验；CLI 两次跑 sha256 相同。
  4. **AC-T1-3**：合成 4 行按触发器命中的 id 集合恰为前三，第四行一条不响。
  5. **AC-T1-4**：`scripts/phase25/estimate_calls.py` 未被修改（`sha256sum -c`）。

**AC-T1-1 的锁内适配（偏差 A9）**：计划原文要求「触帽任务 demand_p90 > spend_p90
严格大于」。实测该不等式在本数据上不可达：同计划的 D1-3 要求逐墙 subcap 在 demand
侧保留（单墙行 demand 本就被子帽限制），而池行 `cap=28` 恰等于 demand P90（v2 把
P90 标成帽留下的历史事实，触帽比例仅 7.8%）。替代的四条联合判据为：触帽行
`spend.max==cap` 且 `demand.max` 严格更大；`demand.mean` 严格更大且
`demand.p90>=spend.p90`；双胞胎控制行 demand 五统计量逐位恒等；历史「触帽且未
收敛」行的燃尽下界严格超帽。**v2 的截断实现会同时判红这四条**（v2 的 demand 就是
截断后的样本），判别力不低于原判据。
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from collections import defaultdict
from pathlib import Path

from scripts.phase26 import build_interval_v3 as cli
from scripts.phase26 import estimate_calls_v3 as v3
from scripts.phase26 import gen_backtest_history as hist

REPO_ROOT = Path(__file__).resolve().parents[2]
EST = Path("/mnt/artifacts/simpraxis/phase26/estimates")
FIXTURE = REPO_ROOT / "tests/milestone26/fixtures/estimate_backtest_history.json"
FIXTURE_SHA = REPO_ROOT / "tests/milestone26/fixtures/BACKTEST_SHA256.txt"
V2_PATH = REPO_ROOT / "scripts/phase25/estimate_calls.py"
STAT_KEYS = ("p50", "p90", "mean", "min", "max")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- AC-T1-1

def test_ac_t1_1_backtest_demand_spend_split():
    bt = cli.build_backtest()
    rows = bt["rows"]

    # ① 两条模拟路径都有回代行
    kinds = {r["path_kind"] for r in rows}
    assert kinds == {"generic", "per_wall"}, f"回代缺少路径：{kinds}"
    assert bt["summary"]["generic_rows"] > 0
    assert bt["summary"]["per_wall_rows"] > 0

    # ② 未触帽行：五个统计量逐位相等（不是「均值相近」）
    uncensored = [r for r in rows if r["censored"]["cap_hit_frac"] == 0]
    assert uncensored, "回代件里必须存在未触帽行"
    for r in uncensored:
        for k in STAT_KEYS:
            assert r["demand"][k] == r["spend"][k], (
                f"{r['row_id']} 未触帽但 {k} 不等："
                f"{r['demand'][k]} != {r['spend'][k]}（截断没拆开？）")

    # ③ 触帽行：spend.max 被帽截断，demand.max / demand.mean 严格更大
    censored = [r for r in rows if r["censored"]["cap_hit_frac"] > 0]
    assert len(censored) >= 2, f"触帽行太少：{[r['row_id'] for r in censored]}"
    for r in censored:
        cap, d, s = r["censored"]["cap"], r["demand"], r["spend"]
        assert cap is not None and s["max"] == cap, (
            f"{r['row_id']} 触帽行 spend.max 必须等于帽：{s['max']} vs {cap}")
        assert d["max"] > s["max"], (
            f"{r['row_id']} demand.max 未严格超过 spend.max（{d['max']} vs "
            f"{s['max']}）——demand 仍被截断")
        assert d["mean"] > s["mean"], (
            f"{r['row_id']} demand.mean 未严格超过 spend.mean")
        assert d["p90"] >= s["p90"]
        assert r["p90_strict"] == (d["p90"] > s["p90"])

    # ④ 双胞胎控制行：同场景同种子，只改 total_cap ⇒ demand 必须逐位恒等
    twin = bt["twin_pair"]
    by_id = {r["row_id"]: r for r in rows}
    capped, uncapped = by_id[twin["capped_row"]], by_id[twin["uncapped_row"]]
    assert twin["demand_identical"] is True, "demand 读了帽 ⇒ 两行 demand 不同"
    assert capped["demand"] == uncapped["demand"]
    assert twin["spend_identical"] is False
    assert capped["spend"]["max"] == capped["censored"]["cap"]
    assert uncapped["spend"]["max"] == uncapped["demand"]["max"]
    assert uncapped["censored"]["cap_hit_frac"] == 0

    # ⑤ A9 替代：历史「触帽且未收敛」行的燃尽下界必须严格超帽
    hist_rows = _fixture()["rows"]
    floors: dict[str, list[int]] = defaultdict(list)
    for a in v3.demand_burn_alerts(hist_rows):
        floors[a["task"]].append(a["adjusted_demand_floor"])
    hit_failed = [r for r in hist_rows if r["cap_hit"] and not r["rc0"]]
    assert hit_failed, "历史里必须存在「触帽且未收敛」的行（A 墙）"
    for r in hit_failed:
        assert floors[r["task"]], f"{r['task']} 触帽未收敛却没触发燃尽规则"
        assert max(floors[r["task"]]) > r["cap"], (
            f"{r['task']} 燃尽下界未超过帽 {r['cap']}：{floors[r['task']]}")

    # ⑥ A9 的诚实登记：严格 p90 与 p90 重合两集合互斥且覆盖全部触帽行
    strict = set(bt["summary"]["p90_strict_rows"])
    coincident = set(bt["summary"]["p90_coincident_rows"])
    censored_ids = {r["row_id"] for r in censored}
    assert strict.isdisjoint(coincident)
    assert strict | coincident == censored_ids
    assert bt["summary"]["ac_t1_1_adaptation"], "A9 锁内适配说明不得为空"

    # ⑦ 证据件必须与内存重建逐字段相等（防陈旧）
    on_disk = json.loads((EST / "backtest_v3.json").read_text(encoding="utf-8"))
    assert on_disk == bt


# ---------------------------------------------------------------- AC-T1-1b

def test_ac_t1_1b_fixture_provenance():
    pin_sha, pin_name = FIXTURE_SHA.read_text(encoding="utf-8").split()
    assert pin_name == FIXTURE.name
    assert _sha(FIXTURE) == pin_sha, "夹具与 BACKTEST_SHA256.txt 不符（被手改？）"

    fx = _fixture()
    assert fx["schema"] == hist.SCHEMA
    assert fx["row_count"] == len(fx["rows"])
    names = [r["task"] for r in fx["rows"]]
    assert len(names) == len(set(names)), f"夹具任务名重复：{names}"
    for row in fx["rows"]:
        assert row["source_relpaths"], f"{row['task']} 缺 source_relpaths"
        for rel in row["source_relpaths"]:
            assert Path(rel).exists(), f"{row['task']} 来源不存在：{rel}"
        assert row["observed_calls"] == hist.derive_observed_calls(row), (
            f"{row['task']} observed_calls 与来源重算不一致")
        assert row["cap_hit"] == (row["observed_calls"] == row["cap"])
        assert row["derivation"], f"{row['task']} 缺 derivation 说明"

    # 生成器重跑必须逐行相等（确定性 + 防手工编辑）
    assert fx["rows"] == hist.build_rows()
    assert fx["cross_checks"] and all(fx["cross_checks"].values())


# ---------------------------------------------------------------- AC-T1-2

def test_ac_t1_2_interval_artifacts_and_idempotent_cli(tmp_path):
    on_disk_sha = {}
    for task, name in (("t4_walls", "interval_t4_walls_v3.json"),
                       ("t9_vla", "interval_t9_vla_v3.json")):
        art = cli.build(task)
        assert art["schema"] == v3.SCHEMA_V3
        assert art["task"] == task
        assert art["wall_kind"] in v3.WALL_KINDS
        assert art["cache_hit_prior"] == 0.0
        assert art["cache_hit_prior_reason"], "cache 先验必须带理由字段"
        assert art["point_estimate_forbidden"] is True
        assert set(art["demand"]) >= set(STAT_KEYS)
        assert set(art["spend"]) >= set(STAT_KEYS)
        assert set(art["censored"]) >= {"cap_hit_frac", "n_capped", "n_total"}
        assert art["demand"]["p90"] >= art["spend"]["p90"]
        path = EST / name
        assert path.exists(), f"缺证据件：{path}"
        on_disk = json.loads(path.read_text(encoding="utf-8"))
        assert on_disk == art, f"{name} 与内存重建不等（证据件陈旧）"
        on_disk_sha[task] = _sha(path)

    # CLI 两次跑 sha256 相同（产物不含时间戳 ⇒ 幂等）
    shas = []
    for i in range(2):
        out = tmp_path / f"t4_{i}.json"
        assert cli.main(["--task", "t4_walls", "--out", str(out)]) == 0
        shas.append(_sha(out))
    assert shas[0] == shas[1], "两次 CLI 产出 sha256 不同（混入了时间戳？）"
    assert shas[0] == on_disk_sha["t4_walls"]


# ---------------------------------------------------------------- AC-T1-3

def test_ac_t1_3_burn_rules_hit_exact_id_sets():
    base = {"cap": 10, "observed_calls": 10}
    rows = [
        {**base, "task": "r_born", "born": True, "rc0": False,
         "selfcheck_written": False, "cap_hit": False},
        {**base, "task": "r_selfcheck", "born": False, "rc0": False,
         "selfcheck_written": True, "cap_hit": False},
        {**base, "task": "r_cap", "born": False, "rc0": False,
         "selfcheck_written": False, "cap_hit": True},
        {**base, "task": "r_none", "born": False, "rc0": True,
         "selfcheck_written": False, "cap_hit": False},
    ]
    alerts = v3.demand_burn_alerts(rows)
    got: dict[str, list[str]] = defaultdict(list)
    for a in alerts:
        got[a["task"]].append(a["id"])
    assert set(got["r_born"]) == {"born_but_not_converged"}
    assert set(got["r_selfcheck"]) == {"selfcheck_written_but_not_rc0"}
    assert set(got["r_cap"]) == {"cap_hit_and_failed"}
    assert "r_none" not in got, f"未出生未触帽行不该报警：{got.get('r_none')}"
    assert set(got) == {"r_born", "r_selfcheck", "r_cap"}

    # 顺序（规则表顺序）与下界算术
    assert [a["id"] for a in alerts] == [
        "born_but_not_converged", "selfcheck_written_but_not_rc0",
        "cap_hit_and_failed"]
    assert alerts[0]["adjusted_demand_floor"] == 13   # ceil(10*1.25)
    assert alerts[1]["adjusted_demand_floor"] == 13   # 10+3
    assert alerts[2]["adjusted_demand_floor"] == 11   # cap+1
    assert alerts[0]["mark"] == {"cap_is_binding": True}
    assert alerts[1]["mark"] == {"repair_tail_starved": True}
    assert alerts[2]["mark"] == {"censored": True}

    # None 旗标（phase2 无 trace）不得触发前两条规则
    none_rows = [{"task": "t", "cap": 5, "observed_calls": 5, "cap_hit": True,
                  "rc0": False, "born": None, "selfcheck_written": None}]
    assert [a["id"] for a in v3.demand_burn_alerts(none_rows)] == [
        "cap_hit_and_failed"]

    # 落盘件与内存重建一致，规则表与代码同源
    on_disk = json.loads((EST / "burn_rules_v3.json").read_text(encoding="utf-8"))
    assert on_disk == cli.build_burn_rules()
    assert [r["id"] for r in on_disk["rules"]] == [r["id"] for r in v3.BURN_RULES]


# ---------------------------------------------------------------- AC-T1-4

def test_ac_t1_4_phase25_estimator_unmodified():
    pin = (EST / "phase25_estimator_sha256.txt").read_text(encoding="utf-8").split()
    assert len(pin) == 2 and pin[1] == str(V2_PATH)
    assert _sha(V2_PATH) == pin[0], "phase25 估页器被改动（v2 是历史证据）"
    assert v3._V2_SHA256 == pin[0], "v3 的钉死值与取证值不一致"
    proc = subprocess.run(
        ["sha256sum", "-c", str(EST / "phase25_estimator_sha256.txt")],
        capture_output=True, text=True, cwd=str(REPO_ROOT))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    # 本机 locale 是 zh_CN，校验成功行输出「成功」而非「OK」；判据取 rc=0 + 文件名在场。
    assert str(V2_PATH) in proc.stdout, proc.stdout
