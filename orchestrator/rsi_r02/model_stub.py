"""RSI-R0.2 同路径控制：确定性 ModelStub（计划 T3/AC17）。

**只替换决策源**。与真模型完全同一条路：本对象只收到
:class:`orchestrator.rsi_r02.messages.Request`（已渲染的文本消息，含
system/任务/更早轮次摘要/最近两轮全文），只通过 :class:`candidate.Reply`
的**文本 JSON** 回复；它不调用 ``trial.dispatch`` / ``trial.judge`` /
``analyses.read_output``，也不写任何 trial 产物。动作分派、容器执行、分析、
submit、状态机、journal、槽账与计费全部走候选 loop 本体。

**它是控制仪器，不是能力测量对象**（计划 T3 原文：「ModelStub 只证明编排，
不当候选能力」）。因此它的策略表来自宿主侧参考实现（``scripts/rsi_r01/
reference_actor.py`` 的增益阶梯），由调用方显式传入 ``StubPolicy``；该私有
知识不能出现在 T5/T6 的真实候选链上。

事实来源与真模型一致：每轮只看**工具结果文本**——
E/D 的 ``summary.distance/final_speed``、R 的 ``summary.settle`` 与 ``cell``。
更早轮次会被上下文纪律压缩成固定字段摘要（丢 ``summary``），所以本对象像
真模型一样**在当轮把数值记进自己的对话记忆**，不依赖回看历史摘要。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from orchestrator.rsi_r01 import actions as AC
from orchestrator.rsi_r01 import schemas as S
from orchestrator.rsi_r02 import candidate as CD
from orchestrator.rsi_r02 import messages as M

PRODUCER_CONTROL = "model_stub_control"

#: 控制用固定 usage（零真实 POST；只驱动本地计费/账务路径，不代表供应端）。
DEFAULT_USAGE: "dict[str, Any]" = {"in": 1000, "out": 200,
                                   "cache_read": 0, "cache_create": 0}

#: D/E 的分析源码（在受限容器里跑；与 reference_actor 的实现独立书写，
#: 只共享公开常量，产物仅供 oracle/提交引用，不参与判据）。
PUSH_SOURCE = '''\
import glob, json
docs = []
for path in sorted(glob.glob("/data/run_*.json")):
    with open(path, "r", encoding="utf-8") as fh:
        docs.append(json.load(fh))
rows = []
for doc in docs:
    p = doc["params"]
    x, y = doc["x"][-1], doc["y"][-1]
    vx, vy = doc["vx"][-1], doc["vy"][-1]
    dx, dy = x - p["target_x"], y - p["target_y"]
    rows.append({"run_id": doc["run_id"], "distance": (dx * dx + dy * dy) ** 0.5,
                 "final_speed": (vx * vx + vy * vy) ** 0.5})
best = min(rows, key=lambda r: r["distance"]) if rows else None
out = {"kind": "push_analysis_stub", "runs": rows, "best": best}
with open("/out/analysis.json", "w", encoding="utf-8") as fh:
    json.dump(out, fh, sort_keys=True)
print("analysis rows:", len(rows))
'''

#: R 的分析源码（同上；沉降/效应的独立实现，供 analysis_id 引用与交叉核对）。
R_SOURCE_TEMPLATE = '''\
import glob, json
BAND_T, BAND_W, H, DELTA = {band_t!r}, {band_w!r}, {h!r}, {delta!r}
docs = []
for path in sorted(glob.glob("/data/run_*.json")):
    with open(path, "r", encoding="utf-8") as fh:
        docs.append(json.load(fh))

def settle(doc):
    th, om, t = doc["theta"], doc["omega"], doc["t"]
    n = len(th)
    if n == 0 or len(om) != n or len(t) != n:
        return None
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
    key = (float(p["damping"]), float(p["initial_angle"]))
    cells[key] = settle(doc)
dampings = sorted({{k[0] for k in cells}})
angles = sorted({{k[1] for k in cells}})
effects = {{}}
for angle in angles:
    t_low = cells.get((dampings[0], angle)) if dampings else None
    t_high = cells.get((dampings[-1], angle)) if dampings else None
    if t_low is None or t_high is None or t_low == 0.0:
        effects[str(angle)] = None
    else:
        effects[str(angle)] = (t_low - t_high) / t_low
vals = list(effects.values())
mean = None if (not vals or any(v is None for v in vals)) else sum(vals) / len(vals)
z = None if mean is None else H * mean
claim = "INCONCLUSIVE"
if z is not None and z >= DELTA:
    claim = "SUPPORTED"
elif z is not None and z <= -DELTA:
    claim = "REFUTED"
out = {{"kind": "r_analysis_stub", "effects": effects,
       "mean_effect": mean, "z": z, "claim_status": claim}}
with open("/out/analysis.json", "w", encoding="utf-8") as fh:
    json.dump(out, fh, sort_keys=True)
print("analysis rows:", len(docs))
'''


class StubError(RuntimeError):
    """控制仪器自身的配置/状态错误（不是候选结论）。"""


def _turn_count(messages: Sequence[Mapping[str, str]]) -> int:
    """已完成轮次 = 全文 assistant 条数 + 更早轮次摘要的行数。

    上下文纪律只保留最近两轮全文（``messages.FULL_HISTORY_ROUNDS``），更早的
    每轮压缩成 ``summarize_event`` 的一行 ``"- ..."``；两者相加才是真实轮次，
    只用 assistant 条数会在第 3 轮起饱和（会静默重复同一动作）。
    """
    n = 0
    for m in messages:
        if m.get("role") == "assistant":
            n += 1
        elif (m.get("role") == "user"
              and str(m.get("content") or "").startswith("### 更早轮次")):
            n += sum(1 for line in str(m["content"]).splitlines()
                     if line.startswith("- "))
    return n


@dataclass(frozen=True)
class StubPolicy:
    """确定性决策源（控制用）。

    ``ladder`` 是 E/D 的参数阶梯（首格在最前）；``steps`` 每次实验步数。
    ``self_tol`` 是 actor 自设的停止判据（与冻结容差无关，只决定何时停止搜索）。
    """

    ladder: tuple[Mapping[str, float], ...]
    steps: int = 2000
    self_tol_distance: float = 0.01
    self_tol_speed: float = 0.005
    max_run_attempts_per_rung: int = 2

    def __post_init__(self) -> None:
        if not self.ladder:
            raise StubError("StubPolicy.ladder 不能为空")


@dataclass
class ModelStub:
    """按工具结果文本做确定性决策的替身模型。"""

    public_doc: Mapping[str, Any]
    policy: StubPolicy
    usage: Mapping[str, Any] = field(default_factory=lambda: dict(DEFAULT_USAGE))
    producer: str = PRODUCER_CONTROL

    def __post_init__(self) -> None:
        doc = dict(self.public_doc)
        self.instance_id = str(doc.get("instance_id") or "")
        self.family = str(doc.get("family") or "")
        self.public = dict(doc.get("public") or {})
        if self.family not in ("R", "E", "D"):
            raise StubError(f"未知族：{self.family!r}")
        if self.family == "R":
            research = dict(self.public.get("research") or {})
            try:
                self.h = int(research["h"])
                self.delta = float(research["delta"])
                levels = [float(x) for x in research["damping_levels"]]
                angles = [float(x) for x in research["initial_angles"]]
            except (KeyError, TypeError, ValueError) as exc:
                raise StubError(f"公开研究合同缺字段：{exc}") from exc
            # 四格顺序与冻结合同一致：low/plus, low/minus, high/plus, high/minus
            self.cells: "list[dict[str, Any]]" = [
                {"cell": "low/plus", "damping": levels[0], "theta0": angles[0]},
                {"cell": "low/minus", "damping": levels[0], "theta0": angles[1]},
                {"cell": "high/plus", "damping": levels[1], "theta0": angles[0]},
                {"cell": "high/minus", "damping": levels[1], "theta0": angles[1]},
            ]
            self.cell_keys = {c["cell"]: c for c in self.cells}
        else:
            self.h, self.delta = 1, float(S.DELTA)
            self.cells = []
            self.cell_keys = {}
        self.requests: "list[M.Request]" = []
        self.results: "list[dict[str, Any]]" = []
        self.decisions: "list[dict[str, Any]]" = []

    # -- 模型接口 ---------------------------------------------------------

    def __call__(self, request: M.Request) -> CD.Reply:
        self.requests.append(request)
        n = _turn_count(request.messages)
        if n > len(self.results):
            last = request.messages[-1] if request.messages else {}
            if last.get("role") != "user":
                raise StubError("轮次计数与消息结构不符（末条不是工具结果）")
            self.results.append(json.loads(str(last.get("content") or "")))
        if n != len(self.results):
            raise StubError(f"轮次计数漂移：turns={n} results={len(self.results)}")
        action = self._decide()
        text = json.dumps(action, ensure_ascii=False, sort_keys=True)
        self.decisions.append({"turn": n, "action": action.get("action"),
                               "detail": {k: v for k, v in action.items()
                                          if k not in ("source", "schema")}})
        return CD.Reply(text=text, capture={
            "usage": dict(self.usage), "attempts_http": 1, "stop_reason": "stop",
            "provider_model": "model_stub", "provider_request_id": f"stub-{n}",
            "body_sha256": "0" * 64, "content_bytes": len(text),
            "producer": self.producer})

    # -- 决策 -------------------------------------------------------------

    def _decide(self) -> "dict[str, Any]":
        res = self.results
        if not res:
            return self._preregister()
        action = str(res[0].get("action") or "")
        if action != "preregister" or not res[0].get("ok"):
            # 登记失败：无法在剩余预算内合法继续，直接给一次「明确的结束动作」
            raise StubError(f"登记未成功，控制链终止：{res[0]}")
        if self.family == "R":
            return self._decide_r()
        return self._decide_push()

    def _preregister(self) -> "dict[str, Any]":
        if self.family == "R":
            research = dict(self.public.get("research") or {})
            plan = {
                "question": research.get("question"),
                "h": self.h,
                "cells": [{"damping": c["damping"], "theta0": c["theta0"]}
                          for c in self.cells],
                "metric": "settling_time_s",
                "formula_version": "rsi_r01.effect.v1",
                "delta": self.delta,
                "censoring_rule": "末样本不在带内即删失；任一必要时间删失 ⇒ mean_effect=null",
                "analysis_procedure": "读四份公开观测 → 逐份求沉降 → 两角度 e_a → 算术平均 → z=h·mean",
                "stop_rule": "四格齐全后提交一次结论；不重复同格、不改登记",
            }
        else:
            first = dict(self.policy.ladder[0])
            plan = {
                "question": self.public.get("task") or self.public.get("goal"),
                "h": 1,
                "cells": [dict(first)],
                "metric": "final_distance_m",
                "formula_version": "rsi_r01.push.v1",
                "delta": float(S.DELTA),
                "censoring_rule": "不适用（工程族以末次可信 run 判定）",
                "analysis_procedure": "读公开观测 → 末点距离/速度 → 与自设容差比较",
                "stop_rule": "命中自设容差即停；不重复跑后择优提交",
            }
        return {"schema": S.SCHEMA_ACTION, "action": "preregister", "plan": plan}

    # R：四格 → 分析 → 提交（结论由本轮观测复算，不读分析产物）
    def _decide_r(self) -> "dict[str, Any]":
        runs = [r for r in self.results
                if r.get("action") == "run_experiment" and r.get("ok")]
        done = {str(r.get("cell")) for r in runs if r.get("cell")}
        missing = [c for c in self.cells if c["cell"] not in done]
        if missing:
            cell = missing[0]
            return {"schema": S.SCHEMA_ACTION, "action": "run_experiment",
                    "params": {"damping": cell["damping"],
                               "initial_angle": cell["theta0"],
                               "steps": int(self.policy.steps)}}
        analyses = [r for r in self.results
                    if r.get("action") == "analyze" and r.get("ok")]
        run_ids = [str(r["run_id"]) for r in runs]
        if not analyses:
            source = R_SOURCE_TEMPLATE.format(
                band_t=S.THETA_BAND, band_w=S.OMEGA_BAND,
                h=self.h, delta=self.delta)
            return {"schema": S.SCHEMA_ACTION, "action": "analyze",
                    "source": source, "run_ids": run_ids}
        claim = self._r_claim(runs)
        analysis_ids = [str(a["analysis"]["analysis_id"]) for a in analyses
                        if isinstance(a.get("analysis"), Mapping)]
        return {"schema": S.SCHEMA_ACTION, "action": "submit", "claim": claim,
                "run_ids": run_ids, "analysis_ids": analysis_ids,
                "mean_effect": self._r_mean(runs), "z": self._r_z(claim, runs)}

    def _settles(self, runs: Sequence[Mapping[str, Any]]) -> "dict[str, float | None]":
        """按 cell 取沉降时间；删失记 None（与判据同源字段）。"""
        out: "dict[str, float | None]" = {}
        for r in runs:
            cell = str(r.get("cell") or "")
            if cell not in self.cell_keys:
                continue
            summary = r.get("summary") or {}
            settle = summary.get("settle") or {}
            if settle.get("censored"):
                out[cell] = None
            elif settle.get("time_s") is not None:
                out[cell] = float(settle["time_s"])
            else:
                out[cell] = None
        return out

    def _r_effects(self, runs: Sequence[Mapping[str, Any]]) -> "dict[str, float | None]":
        t = self._settles(runs)
        levels = sorted({c["damping"] for c in self.cells})
        angles = sorted({c["theta0"] for c in self.cells})
        effects: "dict[str, float | None]" = {}
        for angle in angles:
            low = t.get(self._cell_of(levels[0], angle))
            high = t.get(self._cell_of(levels[-1], angle))
            if low is None or high is None or low == 0.0:
                effects[str(angle)] = None
            else:
                effects[str(angle)] = (low - high) / low
        return effects

    def _cell_of(self, damping: float, theta0: float) -> str:
        for c in self.cells:
            if float(c["damping"]) == float(damping) and \
                    float(c["theta0"]) == float(theta0):
                return str(c["cell"])
        raise StubError(f"不在四格内的参数：{damping}/{theta0}")

    def _r_mean(self, runs: Sequence[Mapping[str, Any]]) -> "float | None":
        vals = list(self._r_effects(runs).values())
        if not vals or any(v is None for v in vals):
            return None
        return sum(float(v) for v in vals) / len(vals)

    def _r_z(self, claim: str, runs: Sequence[Mapping[str, Any]]) -> "float | None":
        mean = self._r_mean(runs)
        return None if mean is None else float(self.h) * mean

    def _r_claim(self, runs: Sequence[Mapping[str, Any]]) -> str:
        z = self._r_z("", runs)
        if z is None:
            return S.ClaimStatus.INCONCLUSIVE
        if z >= self.delta:
            return S.ClaimStatus.SUPPORTED
        if z <= -self.delta:
            return S.ClaimStatus.REFUTED
        return S.ClaimStatus.INCONCLUSIVE

    # E/D：阶梯 → 命中自设判据即分析 → 提交
    def _decide_push(self) -> "dict[str, Any]":
        runs = [r for r in self.results
                if r.get("action") == "run_experiment" and r.get("ok")]
        passed = any(self._self_passed(r.get("summary") or {}) for r in runs)
        attempts = len([r for r in self.results
                        if r.get("action") == "run_experiment"])
        analyses = [r for r in self.results
                    if r.get("action") == "analyze" and r.get("ok")]
        if not passed and not analyses:
            if attempts < len(self.policy.ladder):
                params = dict(self.policy.ladder[attempts])
                params["steps"] = int(self.policy.steps)
                return {"schema": S.SCHEMA_ACTION, "action": "run_experiment",
                        "params": params}
            if not runs:
                # 阶梯全数被拒（没有一条可信 run）：如实提交「无法计算」，不空转
                return {"schema": S.SCHEMA_ACTION, "action": "submit",
                        "claim": S.ClaimStatus.INCONCLUSIVE, "run_ids": [],
                        "analysis_ids": [], "mean_effect": None, "z": None}
        run_ids = [str(r["run_id"]) for r in runs]
        if not analyses:
            return {"schema": S.SCHEMA_ACTION, "action": "analyze",
                    "source": PUSH_SOURCE, "run_ids": run_ids}
        analysis_ids = [str(a["analysis"]["analysis_id"]) for a in analyses
                        if isinstance(a.get("analysis"), Mapping)]
        claim = (S.ClaimStatus.SUPPORTED if passed
                 else S.ClaimStatus.REFUTED)
        best = self._best_summary(runs)
        return {"schema": S.SCHEMA_ACTION, "action": "submit", "claim": claim,
                "run_ids": run_ids, "analysis_ids": analysis_ids,
                "mean_effect": None if best is None else float(best["distance"]),
                "z": None}

    def _self_passed(self, summary: Mapping[str, Any]) -> bool:
        try:
            return (float(summary["distance"]) <= self.policy.self_tol_distance
                    and float(summary["final_speed"]) <= self.policy.self_tol_speed)
        except (KeyError, TypeError, ValueError):
            return False

    def _best_summary(self, runs: Sequence[Mapping[str, Any]]
                      ) -> "Mapping[str, Any] | None":
        rows = [r.get("summary") or {} for r in runs]
        rows = [r for r in rows if "distance" in r]
        if not rows:
            return None
        return min(rows, key=lambda r: float(r["distance"]))
