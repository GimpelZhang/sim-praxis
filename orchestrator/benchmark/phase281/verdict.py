"""Phase 2.8.1 判决纯函数：三态判决、四类失败与禁用措辞（计划 §13.3 / §18.2）。

本模块是 P281-9 的**唯一真值**，只放纯函数——不读盘、不写盘、不联网、
不 import ``router`` / ``requests`` / ``httpx``（§13.9 的 0 付费静态断言）。
``analysis.py`` 与 ``check_phase281_closure.py`` 都从这里取判据，两处各写一份
即视为判据漂移。

为什么判决要写成「数学上互斥且穷尽」的三分支：计划 §13.3.2 的纪律是
「``INCONCLUSIVE`` 只用于**数据本身不足以区分**」，「方向与预期相反」永远是
``NOT_ESTABLISHED``。若把两个分支写成两条 if，迟早有人加个 else 兜底，
让「相反」悄悄落进 ``INCONCLUSIVE``。这里每个 ``decide_hN`` 都按
``sign(delta)`` 分成 ``+1 / 0 / -1`` 三支，``else: raise`` 兜底，
``test_phase281_analysis.py::test_ac_p281_9_05_partition`` 逐分支覆盖。
"""
from __future__ import annotations

import hashlib
from typing import Any, Iterable, Mapping, Sequence

# ---------------------------------------------------------------------------
# 枚举（计划 §13.3.1 / §13.3.2）
# ---------------------------------------------------------------------------

#: 每 Trial 的判决值域（§13.3.1 五元组的第一个分量）
TRIAL_VERDICTS = ("PASS", "FAIL", "BLOCKED_INFRA", "INVALID_ARTIFACT")
#: 失败四分类（§13.3.1）＋ ``NONE``（PASS 专用的「无失败」）
FAILURE_CLASSES = ("MODEL_FAILURE", "TASK_FAILURE", "INVALID_ARTIFACT",
                   "BLOCKED_INFRA", "NONE")
#: 每个假设的三态判决值域（§1.1 / §13.3.2）
HYPOTHESIS_STATES = ("CAPABILITY_DEMONSTRATED", "NOT_ESTABLISHED", "INCONCLUSIVE")
#: 出生五层（``orchestrator/benchmark/birth.py::LEVELS`` 的同形镜像；不 import
#: 是为了不让本纯模块被出生实现的重依赖链拖进来，漂移由机判测试钉死）
BIRTH_LEVELS = ("B0", "B1", "B2", "B3", "B4")
#: §13.3.1「跑起来了但不达标」的判据边界
FINAL_BIRTH_LEVEL = "B4"

HYPOTHESIS_IDS = ("H1", "H2", "H3", "H4", "H5")

#: §1.1：任务代号 → 假设里用到的短名（M1 宿主快反馈 / L1 真容器 / R1 文档）
TASK_SHORT_REQUIRED = ("M1", "L1", "R1")


# ---------------------------------------------------------------------------
# §13.3.1 失败四分类
# ---------------------------------------------------------------------------

def birth_rank(level: "str | None") -> "int | None":
    """``B0``→0 … ``B4``→4；``None``/未知 → ``None``（= 最底层，从未出生）。"""
    if level is None:
        return None
    try:
        return BIRTH_LEVELS.index(str(level))
    except ValueError:
        return None


def classify_failure(verdict: str, birth_level: "str | None") -> str:
    """§13.3.1 的判定表，逐行机械实现。

    | 情形 | class |
    |---|---|
    | ``verdict ∈ {BLOCKED_INFRA, INVALID_ARTIFACT}`` | 同名 |
    | ``verdict == FAIL`` 且 ``birth_level < B4`` | ``TASK_FAILURE`` |
    | ``verdict == FAIL`` 且 ``birth_level == B4`` | ``MODEL_FAILURE`` |
    | ``verdict == PASS`` | ``NONE`` |
    """
    if verdict in ("BLOCKED_INFRA", "INVALID_ARTIFACT"):
        return verdict
    if verdict == "PASS":
        return "NONE"
    if verdict == "FAIL":
        rank = birth_rank(birth_level)
        # 未出生（None）也是「任务没跑起来」，与 B0–B3 同归 TASK_FAILURE
        return "MODEL_FAILURE" if rank is not None and rank >= 4 else "TASK_FAILURE"
    raise ValueError(f"未知 trial verdict：{verdict!r}（合法值 {TRIAL_VERDICTS}）")


def failure_histogram(classes: Iterable[str]) -> "dict[str, int]":
    """四类失败的直方图（缺项补 0，键序固定——保证 JSON 逐字节可复算）。"""
    counts = {name: 0 for name in FAILURE_CLASSES}
    for name in classes:
        counts[name] = counts.get(name, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# §1.1 的指标：首次出生轮次与中位数
# ---------------------------------------------------------------------------

def first_birth_round(birth_status: "str | None", raw: Any) -> "int | None":
    """首次出生轮次；**未出生记 ``None``**（在比较里等价于 ``+∞``）。

    运行器的口径是「0 = 从未可解析」（``trial_harness._first_birth_round``），
    所以不能直接拿 0 参与中位数——那会把「没出生」算成「第 0 轮就出生」，
    正好把 H2 的方向反过来。故以 ``birth_status == "BORN"`` 为准入条件。
    """
    if str(birth_status or "") != "BORN":
        return None
    if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
        # 声称 BORN 却拿不出正轮次——按「不可判定」处理，不猜 0
        return None
    return raw


def median_rounds(values: "Sequence[int | None]") -> "int | None":
    """含 ``+∞`` 的中位数：``None`` 按无穷大排序；两侧都无穷大时返回 ``None``。

    偶数个样本取中间两值的**下中位**（整数，避免 3.5 这种非整轮次）。
    """
    if not values:
        return None
    ordered = sorted(values, key=lambda v: (1, 0) if v is None else (0, v))
    mid = (len(ordered) - 1) // 2
    return ordered[mid]


# ---------------------------------------------------------------------------
# §13.3.2 每假设的机械判决（三分支互斥且穷尽）
# ---------------------------------------------------------------------------

def _sign(delta: int) -> int:
    return (delta > 0) - (delta < 0)


def decide_h1(agentic_pass: int, legacy_pass: int) -> dict:
    """H1：``agentic`` 在 L1 的 ``CAPABILITY_DEMONSTRATED`` 数 ≥ ``legacy``。"""
    delta = int(agentic_pass) - int(legacy_pass)
    sign = _sign(delta)
    if sign > 0:
        state, code = "CAPABILITY_DEMONSTRATED", "AGENTIC_GREATER"
    elif sign == 0:
        state, code = "INCONCLUSIVE", "EQUAL_COUNT"
    elif sign < 0:
        state, code = "NOT_ESTABLISHED", "LEGACY_SUPERIOR_ON_L1"
    else:  # pragma: no cover - _sign 只返回 {-1,0,1}
        raise AssertionError(f"_sign 违约：{sign}")
    return {"id": "H1", "state": state, "reason_code": code,
            "detail": {"agentic_pass": int(agentic_pass),
                       "legacy_pass": int(legacy_pass), "delta": delta}}


def decide_h2(agentic_median: "int | None", legacy_median: "int | None") -> dict:
    """H2：``agentic`` 的首次出生轮次中位数 ≤ ``legacy``（``None`` = ``+∞``）。"""
    if agentic_median is None and legacy_median is None:
        sign = 0
    elif agentic_median is None:
        sign = 1                      # agentic 无穷大，legacy 有限 ⇒ agentic 更差
    elif legacy_median is None:
        sign = -1                     # legacy 无穷大 ⇒ agentic 更好
    else:
        sign = _sign(int(agentic_median) - int(legacy_median))
    if sign < 0:
        state, code = "CAPABILITY_DEMONSTRATED", "AGENTIC_EARLIER"
    elif sign == 0:
        state, code = "INCONCLUSIVE", "EQUAL_MEDIAN"
    elif sign > 0:
        state, code = "NOT_ESTABLISHED", "AGENTIC_LATER"
    else:  # pragma: no cover
        raise AssertionError(f"_sign 违约：{sign}")
    return {"id": "H2", "state": state, "reason_code": code,
            "detail": {"agentic_median_round": agentic_median,
                       "legacy_median_round": legacy_median}}


def decide_h3(d_m1: int, d_l1: int) -> dict:
    """H3：M1 上的臂间差**绝对值**小于 L1 上的臂间差绝对值。"""
    a, b = abs(int(d_m1)), abs(int(d_l1))
    sign = _sign(a - b)
    if sign < 0:
        state, code = "CAPABILITY_DEMONSTRATED", "M1_GAP_SMALLER"
    elif sign == 0:
        state, code = "INCONCLUSIVE", "GAPS_EQUAL"
    elif sign > 0:
        state, code = "NOT_ESTABLISHED", "M1_GAP_NOT_SMALLER"
    else:  # pragma: no cover
        raise AssertionError(f"_sign 违约：{sign}")
    return {"id": "H3", "state": state, "reason_code": code,
            "detail": {"d_m1": int(d_m1), "d_l1": int(d_l1),
                       "abs_d_m1": a, "abs_d_l1": b}}


def decide_h4(*, structure_worse_reps: "Sequence[int]",
              structure_agentic_median: float,
              structure_legacy_median: float,
              semantic_lane: str,
              structure_present: bool = True,
              semantic_worse_reps: "Sequence[int]" = (),
              semantic_present: bool = False,
              semantic_agentic_median: "float | None" = None,
              semantic_legacy_median: "float | None" = None) -> dict:
    """H4：R1 上 ``agentic`` 结构分不劣于 ``legacy``，语义车道开启时语义分也 ≥。

    分支（互斥且穷尽）：

    1. 结构分或语义分任一 rep 严格劣 → ``NOT_ESTABLISHED``；
    2. 否则**结构分缺席**（``structure_present=False``）→ ``INCONCLUSIVE``；
       *锁内适配*：评测器目前只给 ``verdict``、不给连续结构分。旧实现把缺分
       直接写成 ``0.0 vs 0.0``，于是「没有分」在分支 4 变成「不劣」⇒
       ``CAPABILITY_DEMONSTRATED``——那是拿缺席当达标。缺分一律 ``INCONCLUSIVE``；
    3. 否则语义车道 ``disabled`` → ``INCONCLUSIVE``（计划：此时不得给强结论）；
    4. 否则语义车道 ``enabled`` 但无盲评分 → ``INCONCLUSIVE``（数据不足）；
    5. 否则两侧中位数都 ≥ → ``CAPABILITY_DEMONSTRATED``；
    6. 否则（结构分中位数反而更低却没被抓到「逐 rep 严格劣」）→ ``NOT_ESTABLISHED``。
    """
    if semantic_lane not in ("enabled", "disabled"):
        raise ValueError(f"semantic_lane 只能是 enabled/disabled：{semantic_lane!r}")
    worse = sorted({int(r) for r in structure_worse_reps}
                   | {int(r) for r in semantic_worse_reps})
    detail = {"structure_worse_reps": worse,
              "structure_present": bool(structure_present),
              "structure_agentic_median": structure_agentic_median,
              "structure_legacy_median": structure_legacy_median,
              "semantic_lane": semantic_lane,
              "semantic_present": bool(semantic_present),
              "semantic_agentic_median": semantic_agentic_median,
              "semantic_legacy_median": semantic_legacy_median}
    if worse:
        return {"id": "H4", "state": "NOT_ESTABLISHED",
                "reason_code": "STRICTLY_WORSE", "detail": detail}
    if not structure_present:
        return {"id": "H4", "state": "INCONCLUSIVE",
                "reason_code": "STRUCTURE_SCORES_ABSENT", "detail": detail}
    if semantic_lane == "disabled":
        return {"id": "H4", "state": "INCONCLUSIVE",
                "reason_code": "SEMANTIC_LANE_DISABLED", "detail": detail}
    if not semantic_present:
        return {"id": "H4", "state": "INCONCLUSIVE",
                "reason_code": "SEMANTIC_SCORES_ABSENT", "detail": detail}
    if semantic_agentic_median is None or semantic_legacy_median is None:
        # 声称有盲评分却拿不出中位数——与「没有分」同判，不拿 0.0 顶上
        return {"id": "H4", "state": "INCONCLUSIVE",
                "reason_code": "SEMANTIC_SCORES_ABSENT", "detail": detail}
    if float(semantic_agentic_median) < float(semantic_legacy_median):
        return {"id": "H4", "state": "NOT_ESTABLISHED",
                "reason_code": "SEMANTIC_STRICTLY_WORSE", "detail": detail}
    if float(structure_agentic_median) >= float(structure_legacy_median):
        return {"id": "H4", "state": "CAPABILITY_DEMONSTRATED",
                "reason_code": "NOT_WORSE", "detail": detail}
    return {"id": "H4", "state": "NOT_ESTABLISHED",
            "reason_code": "STRUCTURE_MEDIAN_LOWER", "detail": detail}


def decide_h5(agentic_tokens_median: float, legacy_tokens_median: float) -> dict:
    """H5：``agentic`` 每 Trial token 消耗**高于** ``legacy``（成本—能力不同向）。"""
    delta = float(agentic_tokens_median) - float(legacy_tokens_median)
    if delta > 0:
        state, code = "CAPABILITY_DEMONSTRATED", "AGENTIC_COSTLIER"
    else:
        # 计划原文：≤ 即推翻（「不高于」包含相等）；不是三分支中的「打平」
        state, code = "NOT_ESTABLISHED", "AGENTIC_NOT_COSTLIER"
    return {"id": "H5", "state": state, "reason_code": code,
            "detail": {"agentic_tokens_median": agentic_tokens_median,
                       "legacy_tokens_median": legacy_tokens_median}}


def confirmatory_absent(hypothesis_id: str, *, reason: str) -> dict:
    """确认性阶段未发生时的统一判决：``INCONCLUSIVE`` + 显式的「没跑」事由。

    这不是给 ``INCONCLUSIVE`` 找台阶：§13.3.2 的三态里，``NOT_ESTABLISHED``
    的语义是「方向与预注册相反」，而 0 个确认性 Trial **没有任何方向**。
    用 ``CONFIRMATORY_NOT_RUN`` 把「没数据」与「n=12 打平」区分开，
    免得读者把本判决误读成「跑了但说不清」。
    """
    if hypothesis_id not in HYPOTHESIS_IDS:
        raise ValueError(f"未知假设：{hypothesis_id!r}")
    return {"id": hypothesis_id, "state": "INCONCLUSIVE",
            "reason_code": "CONFIRMATORY_NOT_RUN", "detail": {"reason": reason}}


def assert_partition(hypothesis_id: str, state: str) -> None:
    """机判辅助：三态必须落在值域内（分支遗漏在单测里先炸）。"""
    if state not in HYPOTHESIS_STATES:
        raise ValueError(f"{hypothesis_id} 的判决 {state!r} 不在 {HYPOTHESIS_STATES}")


# ---------------------------------------------------------------------------
# §18.2 禁用措辞（无上下文豁免；豁免只有 §18.3 的 sha256 整块剔除一种）
# ---------------------------------------------------------------------------

#: ``(字面, 为什么禁用)``。**顺序与计划 §18.2 表格逐行同序**，便于对账。
#: 不做任何「否定前瞻」式的上下文豁免——那种写法会让「不含 X」与「X = 0.03」
#: 一起被放过（§18.3 明令）。
FORBIDDEN_PATTERNS: "tuple[tuple[str, str], ...]" = (
    ("p 值", "n=3 做不了；写了就是夸大"),
    ("p-value", "n=3 做不了；写了就是夸大"),
    ("显著性", "n=3 做不了；写了就是夸大"),
    ("置信区间", "n=3 做不了；写了就是夸大"),
    ("统计显著", "n=3 做不了；写了就是夸大"),
    ("证明了", "与三态判决冲突"),
    ("铁证", "与三态判决冲突"),
    ("运行时十类全覆盖", "生产只接四类"),
    ("出生隔离等于沙箱", "不是沙箱，残余已登记"),
    ("完全隔离", "不是沙箱，残余已登记"),
    ("Phase 2.8.1 全部完成", "结果为负时不得表述为完成"),
    ("两臂等价", "只在「除引擎外的参数」上等价，不是结果等价；本报告一律改用"
                 "「部署形态 + 引擎」两处差异的写法"),
    ("重跑后结果不变", "未跑过确认性就不写"),
    ("覆盖率 100%", "无定义的指标不写"),
    ("纯引擎差异", "两臂运行形态必然不同"),
    ("纯代码差异", "两臂运行形态必然不同"),
    ("只有引擎不同", "两臂运行形态必然不同"),
)

#: §18.3 的固定首页，**逐字**（三行，含换行；末尾无空行）
SAMPLE_SIZE_DISCLAIMER = (
    "本实验在每格 n=3（Pilot 1 + 确认性 2）的样本量下进行，产出的是效应量估计\n"
    "与可行性证据，不足以做统计显著性检验。本报告不含任何 p 值、置信区间或\n"
    "显著性结论。所有判决由预注册规则机械产出，负结果原样保留。"
)


def sha256_text(text: str) -> str:
    """UTF-8 字节的 sha256（报告件的登记口径）。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def disclaimer_sha256() -> str:
    """§18.3 固定首页的 sha256（AC-P281-9-07 登记到报告件内的那个值）。"""
    return sha256_text(SAMPLE_SIZE_DISCLAIMER)


def strip_disclaimer(text: str, registered_sha256: "str | None" = None) -> str:
    """按 sha256 **整块剔除**固定首页，返回其余全文。

    豁免的**唯一**实现方式（§18.3）：只认 ``registered_sha256`` 与
    ``SAMPLE_SIZE_DISCLAIMER`` 的哈希相等时的**那一块字面**。块被改动一个字符
    ⇒ 哈希对不上 ⇒ 不剔除 ⇒ 块内的禁用词当场判红（既防篡改，也不给
    「改个说法再豁免」留口子）。``registered_sha256`` 为 ``None`` 时用当前常量
    的哈希（测试与分析器自己算的那个值）。
    """
    known = disclaimer_sha256() if registered_sha256 is None else str(registered_sha256)
    if known != sha256_text(SAMPLE_SIZE_DISCLAIMER):
        return text
    return text.replace(SAMPLE_SIZE_DISCLAIMER, "")


def scan_forbidden(text: str, *, registered_disclaimer_sha256: "str | None" = None
                   ) -> "list[dict[str, Any]]":
    """禁用措辞扫描：先剔固定首页，再对**剩余全文**逐词扫。

    返回命中的 ``[{"pattern","why","line_no","column"}]``（空列表 = 通过）。
    行号是**剔除后**文本的行号——扫描器不替读者做「原文第几行」的换算。
    """
    body = strip_disclaimer(text, registered_disclaimer_sha256)
    hits: "list[dict[str, Any]]" = []
    for pattern, why in FORBIDDEN_PATTERNS:
        start = 0
        while True:
            idx = body.find(pattern, start)
            if idx < 0:
                break
            line_no = body.count("\n", 0, idx) + 1
            line_start = body.rfind("\n", 0, idx) + 1
            hits.append({"pattern": pattern, "why": why, "line_no": line_no,
                         "column": idx - line_start + 1})
            start = idx + len(pattern)
    hits.sort(key=lambda h: (h["line_no"], h["column"], h["pattern"]))
    return hits


def forbidden_report_text_problems(text: str, *,
                                   registered_disclaimer_sha256: "str | None" = None
                                   ) -> "list[str]":
    """``scan_forbidden`` 的人读版（机判问题串）。"""
    return [f"报告含禁用措辞 {h['pattern']!r}（第 {h['line_no']} 行第 "
            f"{h['column']} 列）：{h['why']}"
            for h in scan_forbidden(text,
                                    registered_disclaimer_sha256=registered_disclaimer_sha256)]
