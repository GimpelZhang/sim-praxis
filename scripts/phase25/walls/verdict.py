"""Task 5 判据纯函数（AC-T5-2/4；零 IO、零付费、合成 trace 可反证）。

口径全部以 Phase 2.5 计划 v1.2 §3 Task 5 为唯一准：

- 子帽：``子帽_x = clamp(half_up(25·s_x/(sA+sB)), 9, 16)``，half-up 显式
  定义为 0.5 进位（禁止 Python 内建 round 的 banker's rounding）；
- 双半值 tie-break：两商小数部分同时恰为 0.5 时，先跑墙 half-down、
  后跑墙 half-up，两帽和强制＝基数 25、储备保 3；
- 储备 3 仅供供应侧污染重跑；墙自身未花余额只能本墙重跑用，禁止跨墙
  借支；单墙重跑合计 ≤4 呼；只重跑被污染的那堵墙、只有一次机会；
- 三态判决只对工程闸（墙 A 整闸 + 墙 B 的 B-eng）；
- B-cap 是独立结论轴：B-eng 过而 B-cap 不过只出「G.8 能力墙复现」，
  不触发 D-4；只有 B-eng 未过才 STOP 报用户。

本模块不 import orchestrator.agentic（依赖方向：操作件 → 产品件，反向
不允许），所有输入显式传参。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

# --- AC-T5-2 冻结常量（代入值另见 gates/VERDICT_CRITERIA.md） ---------------
BASE = 25            # 两子帽基数
CLAMP_LO = 9
CLAMP_HI = 16
RESERVE = 3          # 共享池 28 = 基数 25 + 储备 3
POOL_TOTAL = BASE + RESERVE
MAX_RERUN_CALLS_PER_WALL = 4
WALL_A = "wallA"
WALL_B = "wallB"
WALLS = (WALL_A, WALL_B)
FIRST_RUN_DEFAULT = WALL_A

# 三态常量
V_ESTABLISHED = "成立"
V_NOT_ESTABLISHED = "不成立"
V_INCONCLUSIVE = "不确定"

# B-cap 独立轴标签
BC_BOTH = "b_eng_and_b_cap_pass"
BC_CAP_WALL_REPRODUCED = "g8_capability_wall_reproduced"
BC_ENG_FAIL_D4 = "b_eng_fail_d4_stop"


def half_up_quotient(num: int, den: int) -> int:
    """``half_up(num/den)``：0.5 进位的整数商（正数域，计划 NF-1）。

    两种等价实现交叉自检：decimal ROUND_HALF_UP 与整数算术
    ``(2*num + den) // (2*den)``。禁止 Python 内建 ``round``（half-to-even）。
    """
    if type(num) is not int or type(den) is not int:
        raise TypeError("half_up_quotient 只接受 int")
    if num < 0 or den <= 0:
        raise ValueError("half_up_quotient 只定义正数域")
    via_int = (2 * num + den) // (2 * den)
    via_dec = int(
        (Decimal(num) / Decimal(den)).quantize(Decimal("1"),
                                               rounding=ROUND_HALF_UP))
    if via_int != via_dec:
        raise AssertionError("half-up 双实现不一致（不变量被破坏）")
    return via_int


def _clamp(v: int, lo: int = CLAMP_LO, hi: int = CLAMP_HI) -> int:
    return max(lo, min(hi, v))


def _is_exact_half(num: int, den: int) -> bool:
    """num/den 的小数部分是否恰为 0.5（双半值 tie 检测，精确整数算术）。"""
    return (2 * num) % (2 * den) == den and (num % den) != 0


def compute_subcaps(s_a: int, s_b: int, *,
                    base: int = BASE, lo: int = CLAMP_LO,
                    hi: int = CLAMP_HI,
                    first_run: str = FIRST_RUN_DEFAULT) -> dict:
    """按冻结公式分配两墙子帽（首呼前唯一调用点）。

    返回 raw half-up 值、clamp 后子帽、双半值 tie 与消歧留痕、帽和/储备。
    """
    if type(s_a) is not int or type(s_b) is not int or s_a <= 0 or s_b <= 0:
        raise ValueError("sA/sB 必须是正整数（B-eng 工程墙基线呼量）")
    if first_run not in WALLS:
        raise ValueError(f"first_run 必须是 {WALLS} 之一")
    if lo > hi or base <= 0:
        raise ValueError("clamp/基数非法")
    total = s_a + s_b
    raw = {
        WALL_A: half_up_quotient(base * s_a, total),
        WALL_B: half_up_quotient(base * s_b, total),
    }
    frac_a = _is_exact_half(base * s_a, total)
    frac_b = _is_exact_half(base * s_b, total)
    double_half = frac_a and frac_b

    tie_break = None
    if double_half:
        # 唯一退化点：两商同时 x.5。half-up 会使和＝base+1、储备只剩 2；
        # 冻结消歧——先跑墙该位 half-down（floor），后跑墙 half-up（ceil），
        # 两帽和强制＝base，角色随实际先跑墙，基数/界/clamp 均不改。
        floor_a = (base * s_a) // total
        floor_b = (base * s_b) // total
        second = WALL_B if first_run == WALL_A else WALL_A
        sub = {first_run: _clamp(floor_a if first_run == WALL_A else floor_b,
                                 lo, hi),
               second: _clamp(
                   raw[second], lo, hi)}
        tie_break = {
            "rule": "first_run_half_down_second_run_half_up",
            "first_run": first_run,
            "second_run": second,
            "raw_half_up_pair": [raw[WALL_A], raw[WALL_B]],
            "floor_pair": [floor_a, floor_b],
        }
    else:
        sub = {WALL_A: _clamp(raw[WALL_A], lo, hi),
               WALL_B: _clamp(raw[WALL_B], lo, hi)}

    cap_sum = sub[WALL_A] + sub[WALL_B]
    reserve = POOL_TOTAL - cap_sum
    result = {
        "schema": "simpraxis.phase25.t5_subcaps.v1",
        "sA": s_a, "sB": s_b, "base": base, "clamp": [lo, hi],
        "raw_half_up": raw,
        "double_half_tie": double_half,
        "tie_break": tie_break,
        "subcaps": sub,
        "sum": cap_sum,
        "reserve": reserve,
        "pool_total": POOL_TOTAL,
        "first_run": first_run,
    }
    # 冻结不变量（任何退化点都不许静默破）：
    if cap_sum != base:
        # clamp 极端情形 9+16 仍＝25；tie-break 保 25。若未来参数组合破坏，
        # 必须首呼前 STOP 改规则，不允许带着破不变量冻结。
        raise AssertionError(
            f"子帽和 {cap_sum} != 基数 {base}（clamp/tie 不变量被破坏）")
    if reserve < RESERVE:
        raise AssertionError(f"储备 {reserve} < 冻结值 {RESERVE}")
    return result


def calls_to_rc0_threshold(s: int) -> int:
    """成立线：calls-to-rc0 ≤ s 的 50%（向下取整：sA=60→30，sB=30→15）。"""
    if type(s) is not int or s <= 0:
        raise ValueError("基线呼量必须是正整数")
    return s // 2


# --- AC-T5-2 重跑/储备算账（纯函数，合成开支序列可逐边反证） -----------------

@dataclass(frozen=True)
class RerunClaim:
    """一堵墙的重跑支取申请（污染裁决后、实际重跑前冻结）。"""

    wall: str
    contaminated: bool          # 是否被供应侧故障污染（重跑唯一合法理由）
    first_run_spent: int        # 首跑实呼（失败/发网前失败都占 seq）
    rerun_calls: int = 0        # 本次重跑拟用呼量（含自身余额+储备）
    reserve_taken: int = 0      # 其中从共享储备支取数

    def validate(self, subcaps: dict) -> tuple[bool, str]:
        if self.wall not in WALLS:
            return False, f"未知墙：{self.wall}"
        sub = subcaps[self.wall]
        for fld in ("first_run_spent", "rerun_calls", "reserve_taken"):
            v = getattr(self, fld)
            if type(v) is not int or v < 0:
                return False, f"{fld} 必须是非负整数"
        if self.rerun_calls == 0 and self.reserve_taken == 0:
            return True, "no_rerun"
        if not self.contaminated:
            return False, ("非供应侧污染不得重跑：到子帽即熔断记「未过墙」，"
                           "不抬帽/不换种子/不放宽闸")
        if self.rerun_calls > MAX_RERUN_CALLS_PER_WALL:
            return False, (f"单墙重跑合计 {self.rerun_calls} > "
                           f"{MAX_RERUN_CALLS_PER_WALL} 呼封顶")
        own_unspent = max(0, sub - self.first_run_spent)
        if self.reserve_taken > self.rerun_calls:
            return False, "储备支取不得大于重跑总呼量"
        if self.rerun_calls - self.reserve_taken > own_unspent:
            return False, (f"先花本墙未花余额 {own_unspent}，不足部分才动储备；"
                           f"申请自有部分 {self.rerun_calls - self.reserve_taken}"
                           " 超余额且不得跨墙借支")
        if self.reserve_taken > RESERVE:
            return False, f"储备支取 {self.reserve_taken} > 共享储备 {RESERVE}"
        return True, "ok"


def settle_pool(claims: "dict[str, RerunClaim]", subcaps: dict,
                *, first_run: str = FIRST_RUN_DEFAULT) -> dict:
    """两墙重跑申请的共享池总账（顺序 A 先 B 后为默认；B 先同公式同子帽）。

    - 储备池先到先得（先跑墙先支取），后跑墙只能拿剩余储备；
    - 任墙非法申请 → 整账 fail-closed；
    - 返回每墙首跑+重跑实呼、储备池余额、总实呼（含 ≤28 硬顶断言）。
    """
    if set(claims) != set(WALLS):
        raise ValueError("必须对两墙各给一份 RerunClaim（未申请也显式给零值）")
    reserve_left = RESERVE
    rows = {}
    for wall in (first_run,) + tuple(w for w in WALLS if w != first_run):
        claim = claims[wall]
        if claim.wall != wall:
            return {"ok": False, "reason": f"{wall} 申请墙名错位：{claim.wall}"}
        ok, why = claim.validate(subcaps)
        if not ok:
            return {"ok": False, "reason": f"{wall}: {why}"}
        if claim.reserve_taken > reserve_left:
            return {"ok": False,
                    "reason": (f"{wall}: 储备池仅剩 {reserve_left}，"
                               f"申请 {claim.reserve_taken}（不补不借）")}
        reserve_left -= claim.reserve_taken
        sub = subcaps[wall]
        total_calls = claim.first_run_spent + claim.rerun_calls
        # 首跑超子帽本身即熔断事实（到帽未过）；此处只记账不裁决三态。
        rows[wall] = {
            "subcap": sub,
            "first_run_spent": claim.first_run_spent,
            "rerun_calls": claim.rerun_calls,
            "reserve_taken": claim.reserve_taken,
            "total_for_wall": total_calls,
            "cap_exhausted_without_pass": claim.first_run_spent >= sub,
        }
    grand_total = sum(r["total_for_wall"] for r in rows.values())
    if grand_total > POOL_TOTAL:
        return {"ok": False,
                "reason": f"两墙总实呼 {grand_total} > 共享池 {POOL_TOTAL}"}
    return {"ok": True, "rows": rows, "reserve_left": reserve_left,
            "grand_total": grand_total, "first_run": first_run}


# --- AC-T5-4 三态判决（只对工程闸；合成 trace 必红/必绿） --------------------

@dataclass(frozen=True)
class WallResult:
    """一堵墙工程闸的最终事实（重跑后、裁决前）。

    - ``passed``：工程闸最终是否过（墙 A 整闸 / 墙 B 仅 B-eng）；
    - ``calls_to_rc0``：到首次闸 rc0 的模型轮数（未过为 None；B-cap
      数值尝试永不计入）；
    - ``escalated``/``human``：引擎侧脚手架升级/真人介入（发生即记 1，
      即便闸最终过也判负）；
    - ``cap_exhausted``：是否到子帽仍未过（熔断）；
    - ``contaminated_undecidable``：供应侧污染且重跑后仍不可判
      （储备尽/重跑仍污）——按疑罪从有落「不成立」；
    - ``pending_rerun``：污染已确认、重跑尚未执行（中间态，落「不确定」）。
    """

    wall: str
    passed: bool
    calls_to_rc0: "int | None" = None
    escalated: bool = False
    human: bool = False
    cap_exhausted: bool = False
    contaminated_undecidable: bool = False
    pending_rerun: bool = False
    b_cap_passed: "bool | None" = None     # 仅墙 B 用；独立轴，不进三态


def engineering_verdict(result_a: WallResult, result_b: WallResult,
                        baselines: "dict[str, int] | None" = None) -> dict:
    """三态判决纯函数（AC-T5-4）。返回 {verdict, reasons[], per_wall[]}。"""
    baselines = baselines or {WALL_A: 60, WALL_B: 30}
    reasons: list[str] = []
    per_wall = {}

    for r in (result_a, result_b):
        if r.wall not in WALLS:
            raise ValueError(f"未知墙结果：{r.wall}")
        row = {"passed": r.passed, "within_50pct": None,
               "escalated": r.escalated, "human": r.human,
               "cap_exhausted": r.cap_exhausted,
               "contaminated_undecidable": r.contaminated_undecidable}
        if r.passed:
            if r.calls_to_rc0 is None or r.calls_to_rc0 <= 0:
                raise ValueError(f"{r.wall} 过闸必须给正整数 calls-to-rc0")
            thr = calls_to_rc0_threshold(baselines[r.wall])
            row["within_50pct"] = r.calls_to_rc0 <= thr
            row["threshold"] = thr
            row["calls_to_rc0"] = r.calls_to_rc0
        per_wall[r.wall] = row

    # 1) 中间态：污染确认、重跑未决（唯一合法的「悬而未判」窗口）。
    pending = [r.wall for r in (result_a, result_b) if r.pending_rerun]
    if pending:
        return {"verdict": V_INCONCLUSIVE,
                "reasons": [f"供应侧污染已确认，待唯一一次重跑：{pending}"],
                "per_wall": per_wall}

    # 2) 疑罪从有：重跑后仍不可判 → 该墙按未过，整体「不成立」（不得再开口）。
    guilty = [r.wall for r in (result_a, result_b)
              if r.contaminated_undecidable]
    if guilty:
        reasons.append("供应侧污染重跑后仍不可判，预注册疑罪从有："
                       f"{guilty} 按未过")

    # 3) 脚手架升级/真人介入：发生即判负（不靠这个过闸；暗中升级交互面同样）。
    for r in (result_a, result_b):
        if r.escalated:
            reasons.append(f"{r.wall} 存在引擎侧脚手架升级（spike 判负并停）")
        if r.human:
            reasons.append(f"{r.wall} 存在真人介入（spike 判负并停）")

    # 4) 到子帽未过：不成立。
    capped = [r.wall for r in (result_a, result_b)
              if (not r.passed) and r.cap_exhausted]
    if capped:
        reasons.append(f"工程闸到子帽未过：{capped}（不抬帽/不换种/不放宽）")

    # 5) 过闸但超 50% 基线：改善未达预注册线 → 不成立。
    over50 = []
    for r in (result_a, result_b):
        row = per_wall[r.wall]
        if r.passed and row["within_50pct"] is False:
            over50.append(r.wall)
            reasons.append(
                f"{r.wall} calls-to-rc0={r.calls_to_rc0} > 50% 基线 "
                f"{row['threshold']}（量化改善未达预注册线）")

    # 6) 干净双过：成立。
    both_pass = all(r.passed for r in (result_a, result_b))
    clean = not (guilty or capped or over50) and not any(
        r.escalated or r.human for r in (result_a, result_b))
    if both_pass and clean:
        return {"verdict": V_ESTABLISHED,
                "reasons": ["两工程闸均过、calls-to-rc0 均 ≤50% 基线、"
                            "零升级零介入"],
                "per_wall": per_wall}

    # 7) 未到帽、未升级介入的一过一负（模型自终局/引擎错等非熔断负）：不确定。
    if not reasons:
        passed_walls = [r.wall for r in (result_a, result_b) if r.passed]
        failed_walls = [r.wall for r in (result_a, result_b) if not r.passed]
        if len(passed_walls) == 1 and len(failed_walls) == 1:
            reasons.append(
                f"一过一负（{passed_walls[0]} 过 / {failed_walls[0]} 负，"
                "负方未到子帽且无升级介入）")
            return {"verdict": V_INCONCLUSIVE, "reasons": reasons,
                    "per_wall": per_wall}

    return {"verdict": V_NOT_ESTABLISHED, "reasons": reasons,
            "per_wall": per_wall}


def b_cap_axis(result_b: WallResult) -> dict:
    """B-cap 独立结论轴（不计 calls、不进三态；AC-T5-4 M7）。"""
    if result_b.wall != WALL_B:
        raise ValueError("B-cap 轴只接受墙 B 结果")
    if result_b.passed and result_b.b_cap_passed:
        label = BC_BOTH
        d4 = False
        desc = "B-eng 与 B-cap 运动学数值均过"
    elif result_b.passed and not result_b.b_cap_passed:
        label = BC_CAP_WALL_REPRODUCED
        d4 = False
        desc = ("B-eng 过而 speed≤0.20/clearance≥0.012 等能力数值不过："
                "Complete G.8「合同消灭工程不合格、不消灭能力不足」在工具层"
                "复现；单列结论，不触发 D-4，Task 9 由 G3 正常裁决")
    else:
        label = BC_ENG_FAIL_D4
        d4 = True
        desc = "B-eng 未过 → agentic 不成立，STOP 报用户（D-4，Task 9 G2 门）"
    return {"label": label, "d4_stop": d4, "desc": desc,
            "b_eng_passed": result_b.passed,
            "b_cap_passed": result_b.b_cap_passed}


def deny_records_from_trace(trace_events: list) -> dict:
    """AC-T5-6：trace 中 policy deny 计数（白名单外调用尝试必须留证）。"""
    denies = [e for e in trace_events
              if isinstance(e, dict)
              and isinstance(e.get("policy"), dict)
              and e["policy"].get("decision") == "deny"]
    return {"deny_count": len(denies),
            "deny_tools": sorted({str(e.get("tool")) for e in denies})}
