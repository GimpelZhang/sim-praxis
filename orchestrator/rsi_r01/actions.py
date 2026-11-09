"""RSI-R0.1 候选动作面：每模型响应只允许**一个** schema 动作。

计划 §5.1 末段：畸形输出形成明确失败事件，下一轮修正只在原 24 POST 帽内，禁止
隐藏的自动「格式修复模型」。因此这里的解析**永不抛异常**给调用方——任何畸形都
返回 :class:`ActionFailure`（带稳定 code 与可回喂的简短说明），由轨迹层记账。

四个动作（公开合同）：

* ``preregister``  —— 首个实验前唯一登记（问题、h、四格矩阵、指标、公式版本、
  δ、删失规则、分析流程、停止规则）；
* ``run_experiment`` —— 提交有界数值参数，宿主注入场景与故障；
* ``analyze`` —— 提交 Python 源码与本 trial 的 run_id 清单；
* ``submit`` —— 提交结论（断言 + 引用 run_id + 分析 id）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Mapping

from orchestrator.rsi_r01 import schemas as S


class ActionCode:
    NOT_JSON = "ACTION_NOT_JSON"
    NOT_OBJECT = "ACTION_NOT_OBJECT"
    BAD_SCHEMA = "ACTION_BAD_SCHEMA"
    UNKNOWN_ACTION = "ACTION_UNKNOWN"
    MISSING_FIELD = "ACTION_MISSING_FIELD"
    BAD_TYPE = "ACTION_BAD_TYPE"
    BAD_VALUE = "ACTION_BAD_VALUE"
    UNAUTHORIZED_KEY = "ACTION_UNAUTHORIZED_KEY"
    OUT_OF_BOUNDS = "ACTION_OUT_OF_BOUNDS"


@dataclass(frozen=True)
class ActionFailure:
    code: str
    message: str
    action: str | None = None
    path: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"ok": False, "code": self.code, "message": self.message,
                "action": self.action, "path": self.path}


@dataclass(frozen=True)
class RunExperiment:
    params: dict[str, float]

    def as_dict(self) -> dict[str, Any]:
        return {"ok": True, "action": "run_experiment", "params": dict(self.params)}


@dataclass(frozen=True)
class Preregister:
    plan: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {"ok": True, "action": "preregister", "plan": dict(self.plan)}


@dataclass(frozen=True)
class Analyze:
    source: str
    run_ids: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {"ok": True, "action": "analyze", "source": self.source,
                "run_ids": list(self.run_ids)}


@dataclass(frozen=True)
class Submit:
    claim: str
    run_ids: tuple[str, ...]
    analysis_ids: tuple[str, ...] = ()
    mean_effect: float | None = None
    z: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"ok": True, "action": "submit", "claim": self.claim,
                "run_ids": list(self.run_ids),
                "analysis_ids": list(self.analysis_ids),
                "mean_effect": self.mean_effect, "z": self.z}


Action = RunExperiment | Preregister | Analyze | Submit


@dataclass(frozen=True)
class ActionSurface:
    """一个实例的公开动作面（由任务合同给出，模型可见）。"""

    kind: str                                  # "pendulum_run" | "push_control"
    param_bounds: Mapping[str, tuple[float, float]]
    fixed_params: Mapping[str, float] = field(default_factory=dict)
    max_steps: int = S.N_STEPS
    allow_analyze: bool = True


def _fail(code: str, message: str, *, action: str | None = None,
          path: str | None = None) -> ActionFailure:
    return ActionFailure(code=code, message=message, action=action, path=path)


def parse_action(text: str, surface: ActionSurface) -> Action | ActionFailure:
    """把一段模型输出解析成动作；任何问题都结构化返回，绝不抛异常。"""
    if not isinstance(text, str):
        return _fail(ActionCode.BAD_TYPE, "输入必须是字符串")
    stripped = text.strip()
    if stripped.startswith("```"):
        # 容忍一层 markdown 围栏（不修改语义，只剥壳）；壳内仍须是合法 JSON。
        lines = [ln for ln in stripped.splitlines() if not ln.strip().startswith("```")]
        stripped = "\n".join(lines).strip()
    try:
        obj = json.loads(stripped)
    except (json.JSONDecodeError, ValueError) as exc:
        return _fail(ActionCode.NOT_JSON, f"不是合法 JSON：{exc}"[:300])
    return parse_action_obj(obj, surface)


def parse_action_obj(obj: Any, surface: ActionSurface) -> Action | ActionFailure:
    if not isinstance(obj, dict):
        return _fail(ActionCode.NOT_OBJECT, "动作必须是 JSON 对象")
    problems = S.check_action_doc(obj)
    if problems:
        first = problems[0]
        code = (ActionCode.BAD_SCHEMA if first["path"] == "schema"
                else ActionCode.UNKNOWN_ACTION)
        return _fail(code, f"{first['path']}: {first['problem']} "
                           f"(expected {first['expected']}, got {first['actual']})",
                     path=first["path"])
    action = obj["action"]
    if action == "run_experiment":
        return _parse_run(obj, surface)
    if action == "preregister":
        return _parse_prereg(obj)
    if action == "analyze":
        return _parse_analyze(obj, surface)
    return _parse_submit(obj)


def _parse_run(obj: Mapping[str, Any], surface: ActionSurface
               ) -> RunExperiment | ActionFailure:
    raw = obj.get("params")
    if raw is None:
        return _fail(ActionCode.MISSING_FIELD, "缺 params", action="run_experiment")
    if not isinstance(raw, dict):
        return _fail(ActionCode.BAD_TYPE, "params 必须是对象", action="run_experiment",
                     path="params")
    bounds = dict(surface.param_bounds)
    illegal = sorted(set(raw) - set(bounds))
    if illegal:
        return _fail(ActionCode.UNAUTHORIZED_KEY,
                     f"未授权字段 {illegal}（本实例可选 {sorted(bounds)}）",
                     action="run_experiment", path="params")
    clean: dict[str, float] = {}
    for key, value in raw.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return _fail(ActionCode.BAD_TYPE, f"{key} 必须是数值",
                         action="run_experiment", path=f"params.{key}")
        v = float(value)
        if v != v or v in (float("inf"), float("-inf")):
            return _fail(ActionCode.BAD_VALUE, f"{key} 必须是有限数",
                         action="run_experiment", path=f"params.{key}")
        lo, hi = bounds[key]
        if key == "steps":
            hi = min(float(hi), float(surface.max_steps))
        if not (lo <= v <= hi):
            return _fail(ActionCode.OUT_OF_BOUNDS,
                         f"{key}={v} 越界 [{lo}, {hi}]",
                         action="run_experiment", path=f"params.{key}")
        clean[key] = v
    clean.update({str(k): float(v) for k, v in surface.fixed_params.items()})
    return RunExperiment(params=clean)


def _parse_prereg(obj: Mapping[str, Any]) -> Preregister | ActionFailure:
    plan = obj.get("plan")
    if not isinstance(plan, dict):
        return _fail(ActionCode.MISSING_FIELD, "缺 plan 对象", action="preregister",
                     path="plan")
    missing = [k for k in ("question", "h", "cells", "metric", "formula_version",
                           "delta", "censoring_rule", "analysis_procedure", "stop_rule")
               if k not in plan]
    if missing:
        return _fail(ActionCode.MISSING_FIELD, f"plan 缺字段 {missing}",
                     action="preregister", path="plan")
    return Preregister(plan=dict(plan))


def _parse_analyze(obj: Mapping[str, Any], surface: ActionSurface
                   ) -> Analyze | ActionFailure:
    if not surface.allow_analyze:
        return _fail(ActionCode.BAD_VALUE, "本实例不开放 analyze", action="analyze")
    source = obj.get("source")
    if not isinstance(source, str) or not source.strip():
        return _fail(ActionCode.MISSING_FIELD, "缺 source 源码字符串",
                     action="analyze", path="source")
    run_ids = obj.get("run_ids")
    if not isinstance(run_ids, list) or not run_ids:
        return _fail(ActionCode.MISSING_FIELD, "缺 run_ids 数组",
                     action="analyze", path="run_ids")
    if not all(isinstance(r, str) for r in run_ids):
        return _fail(ActionCode.BAD_TYPE, "run_ids 必须全是字符串",
                     action="analyze", path="run_ids")
    return Analyze(source=source, run_ids=tuple(run_ids))


def _parse_submit(obj: Mapping[str, Any]) -> Submit | ActionFailure:
    claim = obj.get("claim")
    if claim not in S.ClaimStatus.ALL:
        return _fail(ActionCode.BAD_VALUE,
                     f"claim 必须是 {list(S.ClaimStatus.ALL)}",
                     action="submit", path="claim")
    run_ids = obj.get("run_ids")
    if not isinstance(run_ids, list) or not run_ids:
        return _fail(ActionCode.MISSING_FIELD, "缺 run_ids 数组", action="submit",
                     path="run_ids")
    analysis_ids = obj.get("analysis_ids", [])
    if not isinstance(analysis_ids, list):
        return _fail(ActionCode.BAD_TYPE, "analysis_ids 必须是数组",
                     action="submit", path="analysis_ids")

    def _opt_num(key: str):
        v = obj.get(key)
        if v is None:
            return None
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError(f"{key} 必须是数值或 null")
        if v != v or v in (float("inf"), float("-inf")):
            raise ValueError(f"{key} 必须是有限数或 null")
        return float(v)

    try:
        mean_effect = _opt_num("mean_effect")
        z = _opt_num("z")
    except ValueError as exc:
        return _fail(ActionCode.BAD_TYPE, str(exc), action="submit")
    if not all(isinstance(r, str) for r in run_ids + analysis_ids):
        return _fail(ActionCode.BAD_TYPE, "run_ids/analysis_ids 必须全是字符串",
                     action="submit")
    return Submit(claim=claim, run_ids=tuple(run_ids),
                  analysis_ids=tuple(analysis_ids),
                  mean_effect=mean_effect, z=z)


def action_event(*, action: str | None, ok: bool, code: str | None = None,
                 message: str | None = None, seq: int | None = None,
                 extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """统一动作事件（轨迹层落盘用；失败也是合法事件，不是异常）。"""
    doc = {"schema": S.SCHEMA_ACTION_EVENT, "action": action, "ok": bool(ok),
           "code": code, "message": (message or "")[:500], "seq": seq}
    if extra:
        doc.update(dict(extra))
    return doc
