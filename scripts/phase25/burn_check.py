#!/usr/bin/env python3
"""Phase 2.5 Task 1 — 燃尽告警与强制重基线（AC-T1-5；0 付费、0 触网）。

纯函数库 + 薄 CLI，不读活账、不触网；呼量状态由调用方（批次执行器/战役
CLI）以 state 字典喂入。规则：

1. 批次用量两线告警：50% 提醒、80% 强提醒（跨线才发，不重复刷）。
2. 首次出生完成后强制重估：实际出生呼数超先验 P90 → 全线重算落重基线件。
3. 每跨一堵错误墙、每次 Gate 驳回 → 触发重估。
4. 供应侧（Beta 后验）失败率 P90 仍低于实测失败率 → 触发重估。
5. 重基线只改【后续区间】；预注册科学帽（n/阈值/判据）永不改，重基线件里
   逐字钉死。

# 锁内适配（Phase 2.5）：告警日志落 estimates/burn_alerts.jsonl，与 T2
# watchdog 的 watchdog/alerts.jsonl 命名空间隔离，避免两任务写同一文件。
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

# scripts/ 非包（无 __init__.py），按文件路径加载同目录 sibling 模块。
import importlib.util as _ilu
_ec_spec = _ilu.spec_from_file_location(
    "simpraxis_phase25_estimate_calls",
    REPO_ROOT / "scripts" / "phase25" / "estimate_calls.py")
ec = _ilu.module_from_spec(_ec_spec)
_ec_spec.loader.exec_module(ec)

SEED = ec.SEED
RULES_SCHEMA = "simpraxis.phase25.burn_rules.v1"
ALERT_LOG_REL = "estimates/burn_alerts.jsonl"  # 锁内适配（Phase 2.5）：独立于 T2

BAND_UNDER = "under_50"
BAND_50 = "warn_50"
BAND_80 = "warn_80"


def default_rules() -> dict:
    """burn_rules.json 的不可变内容（无时间戳，重写逐字节幂等）。"""
    return {
        "schema": RULES_SCHEMA,
        "batch_utilization_lines": [
            {"id": "batch_50", "fraction": 0.50,
             "action": "warn", "message": "批次用量达 50%：核对出生/墙进度"},
            {"id": "batch_80", "fraction": 0.80,
             "action": "strong_warn",
             "message": "批次用量达 80%：收口前必须完成重估或申请提预算"},
        ],
        "rebaseline_triggers": [
            {"id": "first_birth_beyond_prior_p90",
             "rule": "首次出生完成后，实际出生呼数 > C_birth 先验 P90 即全线重算",
             "applies_once": True},
            {"id": "error_wall_crossed",
             "rule": "每跨过一堵错误墙（墙计数 +1）即重估后续墙区间"},
            {"id": "gate_reject",
             "rule": "每次 Gate 驳回即重估（human_failure 补救线程占新呼量）"},
            {"id": "supply_failure_rate_above_prior_p90",
             "rule": "实测供应失败率 > 代际分箱 Beta(fail+1, ok+1) 后验 P90 即重估"},
        ],
        "rebaseline_scope": {
            "changes": ["后续批次/后续墙的 P50/P90 区间"],
            "never_changes": [
                "预注册科学帽：样本量 n、通过阈值、判据（如 85%/speed/clearance）",
                "Gate 判据与轮帽",
            ],
            "budget_caps": "预算帽可经规则 2 自治上调；上调本身不是重基线",
            "full_recalc": "重算＝estimate_calls 全线重渲染到 rebaselines/<as_of>/",
        },
        "alert_log": ALERT_LOG_REL,
        "dedup": "同一 (task, trigger_id, dedup_key) 只允许一条告警",
        "t10_closeout": "实际>P90＝方法学失败结论入 Complete，禁翻案（AC-T1-7）",
    }


def write_rules(out_root: Path) -> Path:
    est = ensure_private_dir(out_root / "estimates")
    p = est / "burn_rules.json"
    atomic_write_text(p, json.dumps(default_rules(), ensure_ascii=False,
                                    indent=2) + "\n")
    return p


# ---------------------------------------------------------------- 纯函数

def utilization_band(spent: int, cap: int) -> dict:
    if cap <= 0:
        raise ValueError("cap 必须为正")
    if spent < 0:
        raise ValueError("spent 不得为负")
    frac = spent / cap
    if frac >= 0.80:
        band = BAND_80
    elif frac >= 0.50:
        band = BAND_50
    else:
        band = BAND_UNDER
    return {"spent": spent, "cap": cap, "fraction": round(frac, 4),
            "band": band}


def _posterior_quantile(ok: int, fail: int, q: float,
                        n: int = 20000, seed: int = SEED) -> float:
    """失败概率 p ~ Beta(fail+1, ok+1) 的最近秩分位（确定性抽样）。"""
    import math
    rng = random.Random(seed)
    s = sorted(rng.betavariate(fail + 1, ok + 1) for _ in range(n))
    rank = min(n, max(1, math.ceil(q * n)))
    return s[rank - 1]


def supply_rate_breach(supply_ok: int, supply_fail: int,
                       observed_ok: int, observed_fail: int) -> dict:
    """实测失败率是否超代际先验后验 P90。"""
    observed_total = observed_ok + observed_fail
    observed_rate = (observed_fail / observed_total) if observed_total else 0.0
    p90 = _posterior_quantile(supply_ok, supply_fail, 0.90)
    return {
        "observed_ok": observed_ok, "observed_fail": observed_fail,
        "observed_rate": round(observed_rate, 4),
        "prior_bin": {"ok": supply_ok, "fail": supply_fail},
        "prior_p90_rate": round(p90, 4),
        "breach": observed_total > 0 and observed_rate > p90,
    }


def evaluate_burn(state: dict) -> dict:
    """喂入累计 state，返回当前告警与是否强制重基线。

    state 字段：
      task, cap, spent, last_band（可选）,
      birth_actual / birth_prior_p90（首次出生后填）,
      walls_crossed（累计跨墙数）, gate_rejects（累计驳回数）,
      supply_bin（SUPPLY_BINS 键名）, supply_ok/supply_fail（本批次实测）。
    触发器做沿沿触发：walls_crossed/gate_rejects 为相对上次评估的累计增量，
    调用方须在 state 里给 last_walls_crossed/last_gate_rejects（缺省 0）。
    """
    alerts: list[dict] = []
    reasons: list[dict] = []

    util = utilization_band(state["spent"], state["cap"])
    last_band = state.get("last_band", BAND_UNDER)
    order = {BAND_UNDER: 0, BAND_50: 1, BAND_80: 2}
    crossed = [b for b in (BAND_50, BAND_80)
               if order[util["band"]] >= order[b] > order[last_band]]
    for band in crossed:
        line = next(x for x in default_rules()["batch_utilization_lines"]
                    if x["id"] == f"batch_{band[-2:]}")
        alerts.append({"alert_id": line["id"], "action": line["action"],
                       "message": line["message"], "utilization": util})

    birth_actual = state.get("birth_actual")
    if birth_actual is not None and not state.get("first_birth_seen_before"):
        p90 = state["birth_prior_p90"]
        beyond = birth_actual > p90
        reasons.append({"trigger_id": "first_birth_beyond_prior_p90",
                        "birth_actual": birth_actual,
                        "birth_prior_p90": p90, "fires": beyond})
        if beyond:
            alerts.append({"alert_id": "first_birth_beyond_prior_p90",
                           "action": "force_rebaseline",
                           "message": f"首次出生 {birth_actual} 呼 > 先验 P90 "
                                      f"{p90}：全线重算", })

    new_walls = state.get("walls_crossed", 0) \
        - state.get("last_walls_crossed", 0)
    if new_walls > 0:
        reasons.append({"trigger_id": "error_wall_crossed",
                        "new_walls": new_walls, "fires": True})
        alerts.append({"alert_id": "error_wall_crossed",
                       "action": "force_rebaseline",
                       "message": f"新跨 {new_walls} 堵错误墙：重估后续区间"})

    new_rejects = state.get("gate_rejects", 0) \
        - state.get("last_gate_rejects", 0)
    if new_rejects > 0:
        reasons.append({"trigger_id": "gate_reject",
                        "new_rejects": new_rejects, "fires": True})
        alerts.append({"alert_id": "gate_reject",
                       "action": "force_rebaseline",
                       "message": f"新增 {new_rejects} 次 Gate 驳回：重估补救呼量"})

    supply = None
    binname = state.get("supply_bin")
    if binname and state.get("supply_ok") is not None:
        prior = ec.SUPPLY_BINS[binname]
        supply = supply_rate_breach(
            prior["ok"], prior["fail"],
            state["supply_ok"], state.get("supply_fail", 0))
        if supply["breach"]:
            reasons.append({"trigger_id":
                            "supply_failure_rate_above_prior_p90",
                            "observed_rate": supply["observed_rate"],
                            "prior_p90_rate": supply["prior_p90_rate"],
                            "fires": True})
            alerts.append({"alert_id":
                           "supply_failure_rate_above_prior_p90",
                           "action": "force_rebaseline",
                           "message": "供应失败率超先验 P90：重估失败税"})

    force = any(a["action"] == "force_rebaseline" for a in alerts)
    return {"task": state["task"], "utilization": util,
            "crossed_lines": crossed, "alerts": alerts,
            "trigger_evaluation": reasons,
            "supply_check": supply,
            "rebaseline_required": force}


# ---------------------------------------------------------------- 落件

def _alert_log(out_root: Path) -> Path:
    return ensure_private_dir(out_root / "estimates") / "burn_alerts.jsonl"


def append_alerts(out_root: Path, task: str, alerts: list[dict],
                  dedup_keys: dict[str, str] | None = None) -> list[Path]:
    """按 (task, alert_id, dedup_key) 去重追加；返回写入的日志路径列表。"""
    log = _alert_log(out_root)
    seen = set()
    if log.exists():
        lines = [ln for ln in log.read_text(encoding="utf-8").splitlines()
                 if ln.strip()]
        clean_lines: list[str] = []
        trailing_corrupt = False
        for idx, line in enumerate(lines):
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                # 锁内适配（Phase 2.5，MINOR-9）：仅容忍末尾一行被 kill -9
                # 截断的半行（无尾换行的损坏 JSON）；中间坏行＝日志损坏，
                # 必须显式失败，不得静默丢去重历史。
                if idx != len(lines) - 1:
                    raise ValueError(
                        f"burn_alerts.jsonl 第 {idx + 1} 行 JSON 损坏"
                        f"（非末尾行，共 {len(lines)} 行）：{exc}") from exc
                trailing_corrupt = True
                continue
            clean_lines.append(line)
            seen.add((row.get("task"), row.get("alert_id"),
                      row.get("dedup_key")))
        if trailing_corrupt:
            # 半行愈合：原子重写丢掉未写完的末行（它从未构成完整告警记录）。
            # 不愈合则补换行后该半行在下一轮变成「中间坏行」永久堵死通道。
            atomic_write_text(
                log, "".join(ln + "\n" for ln in clean_lines))
    # 末尾半行（kill -9 截断，无尾换行）后直接追加会把新告警拼到半行同一
    # 物理行；首条写入前补一个换行，保证告警通道在半行后仍可用（MINOR-9）。
    prefix = ""
    if log.exists():
        tail_byte = log.read_bytes()[-1:] if log.stat().st_size else b""
        if tail_byte and tail_byte != b"\n":
            prefix = "\n"
    written = []
    for a in alerts:
        key = (task, a["alert_id"], (dedup_keys or {}).get(a["alert_id"], ""))
        if key in seen:
            continue
        seen.add(key)
        row = {"task": task, "dedup_key": key[2], **a}
        with open(log, "a", encoding="utf-8") as fh:  # 操作件追加，非台账
            fh.write(prefix + json.dumps(row, ensure_ascii=False) + "\n")
        prefix = ""
        try:
            log.chmod(0o600)
        except OSError:
            pass
        written.append(log)
    return written


def write_rebaseline(out_root: Path, as_of: str, state: dict,
                     reasons: list[dict]) -> dict:
    """全线重算：estimate_calls 重渲染到 rebaselines/<as_of>/，并落重基线件。

    重基线件逐字声明科学帽冻结；目录 0700/文件 0600。
    """
    # render_outputs(root) 写 root/estimates/*；root 取重基线目录即得
    # rebaselines/<as_of>/estimates/*（全线重算的区间件）。
    base = ensure_private_dir(
        out_root / "estimates" / "rebaselines" / as_of)
    rendered = ec.render_outputs(base)
    rules = default_rules()
    manifest = {
        "schema": "simpraxis.phase25.rebaseline.v1",
        "as_of": as_of,
        "task": state["task"],
        "triggered_by": [r for r in reasons if r.get("fires")],
        "state_snapshot": state,
        "scope": rules["rebaseline_scope"],
        "scientific_caps_frozen": rules["rebaseline_scope"]["never_changes"],
        "recalculated_artifacts": rendered,
    }
    p = base / "rebaseline.json"
    atomic_write_text(p, json.dumps(manifest, ensure_ascii=False,
                                    indent=2) + "\n")
    return {"manifest": str(p), "recalculated": rendered}


# ---------------------------------------------------------------- T10 对账（AC-T1-7 预设）

def t10_closeout(actual: int, interval: dict) -> dict:
    """实际 vs P50/P90 机判；实际>P90 即方法学失败，无翻案字段。"""
    est = interval["estimate"]
    p50, p90 = est["p50"], est["p90"]
    if actual < 0:
        raise ValueError("actual 不得为负")
    if actual > p90:
        verdict = "methodology_failure"
        note = ("实际>P90：方法学失败结论入 Complete（注明失准先验类别："
                "出生数/修复轮/每轮呼数/cache），禁「情况特殊」翻案")
    elif actual < p50:
        verdict = "below_p50"
        note = "实际<P50：区间保守侧，如实记录不调区间"
    else:
        verdict = "within_p50_p90"
        note = "实际落 P50–P90：方法学校准成立"
    return {"schema": "simpraxis.phase25.t10_accounting.v1",
            "actual": actual, "p50": p50, "p90": p90,
            "verdict": verdict, "note": note,
            "reversal_forbidden": True}


# ---------------------------------------------------------------- CLI

def _dedup_keys(state: dict) -> dict[str, str]:
    """累计计数器即每事件去重键：同水平不重发，跨事件各发一次。"""
    return {
        "batch_50": "50",
        "batch_80": "80",
        "first_birth_beyond_prior_p90": str(state.get("birth_actual")),
        "error_wall_crossed": f"walls={state.get('walls_crossed', 0)}",
        "gate_reject": f"rejects={state.get('gate_rejects', 0)}",
        "supply_failure_rate_above_prior_p90":
            f"{state.get('supply_ok')}-{state.get('supply_fail')}",
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out-root", default=None,
                    help="phase25 根（缺省走 runtime_paths）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="写 estimates/burn_rules.json")
    chk = sub.add_parser("check", help="读 state JSON，输出告警/重基线判定")
    chk.add_argument("--state", required=True)
    chk.add_argument("--as-of", default=None,
                     help="触发重基线时的重基线目录名（调用方给时间戳）")
    t10p = sub.add_parser("t10", help="T10 实际 vs P50/P90 对账")
    t10p.add_argument("--actual", required=True, type=int)
    t10p.add_argument("--interval", required=True)
    args = ap.parse_args(argv)

    from orchestrator.common import runtime_paths as rp
    out = Path(args.out_root) if args.out_root else rp.phase25_root()

    if args.cmd == "init":
        print(json.dumps({"written": str(write_rules(out))},
                         ensure_ascii=False))
        return 0
    if args.cmd == "check":
        state = json.loads(Path(args.state).read_text(encoding="utf-8"))
        result = evaluate_burn(state)
        append_alerts(out, state["task"], result["alerts"],
                      dedup_keys=_dedup_keys(state))
        if result["rebaseline_required"]:
            if not args.as_of:
                raise SystemExit("rebaseline 触发：必须传 --as-of 目录名")
            result["rebaseline"] = write_rebaseline(
                out, args.as_of, state, result["trigger_evaluation"])
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    interval = json.loads(Path(args.interval).read_text(encoding="utf-8"))
    print(json.dumps(t10_closeout(args.actual, interval),
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
