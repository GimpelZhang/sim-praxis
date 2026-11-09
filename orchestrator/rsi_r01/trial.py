"""RSI-R0.1 试次分派器：公开动作 → 可信实验/受限分析/裁判的唯一通路。

计划 §5.1/§5.2 的关键要求是**同路径**：确定性 reference actor 与未来的模型候选
必须走完全相同的分派、容器、文件、schema、submit 与判据。因此把这条通路集中
在这里，reference actor 只负责「产生动作」，不碰 judge 输入。

纪律：

* ``preregister`` 必须是**首个实验之前**的唯一登记；晚登记/改登记/重复登记拒绝。
* R 族四格：每格**恰好一次**原始 run；同格重复、跨格顶数一律拒绝（§4.4）。
* 观测以可信原件为唯一判据；候选只拿只读副本。
* ``submit`` 只记录候选断言；判决由宿主从可信观测复算（候选自报数值不参与）。
* 资源帽（每 trial 实验次数/步数/分析次数/墙钟）在这里先扣后用，越界即拒。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from orchestrator.rsi_r01 import actions as AC
from orchestrator.rsi_r01 import attempts as AT
from orchestrator.rsi_r01 import evaluator as EV
from orchestrator.rsi_r01 import observations as O
from orchestrator.rsi_r01 import schemas as S
from orchestrator.rsi_r01 import task_contract as TC
from orchestrator.rsi_r01.analysis_runner import (AnalysisPolicyViolation,
                                                  AnalysisRunner)


class TrialRejected(RuntimeError):
    """动作被拒（结构化事件：trial 继续，但该动作不生效）。"""

    def __init__(self, code: str, msg: str):
        super().__init__(f"{code}: {msg}")
        self.code = code
        self.detail = msg


def action_surface(inst: TC.Instance) -> AC.ActionSurface:
    """公开动作面：与 ``task_contract`` 公开面同源，不另立一套边界。"""
    bounds = dict(inst.param_bounds)
    bounds["steps"] = (1.0, float(inst.max_steps))
    fixed: dict[str, float] = {}
    if inst.template == "pendulum":
        fixed["timestep"] = S.DT_S
    else:
        fixed["timestep"] = float(inst.public["scene_params"]["timestep"])
    return AC.ActionSurface(kind=("pendulum_run" if inst.template == "pendulum"
                                  else "push_control"),
                            param_bounds=bounds, fixed_params=fixed,
                            max_steps=inst.max_steps,
                            allow_analyze=True)


@dataclass
class RunRecord:
    run_id: str
    cell: str | None
    params: dict[str, Any]
    manifest: dict[str, Any]
    summary: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {"run_id": self.run_id, "cell": self.cell, "params": dict(self.params),
                "manifest": dict(self.manifest), "summary": dict(self.summary)}


def _cell_of(inst: TC.Instance, params: Mapping[str, Any]) -> str | None:
    """把一次 R 运行归类到四格之一；参数不在冻结矩阵上即拒绝（不猜）。"""
    if inst.family != "R":
        return None
    damping = float(params["damping"])
    theta0 = float(params["initial_angle"])
    for cell in inst.r_cells:
        if abs(damping - float(cell["damping"])) < 1e-12 and \
                abs(theta0 - float(cell["theta0"])) < 1e-12:
            return cell["cell"]
    raise TrialRejected(
        "CELL_NOT_IN_MATRIX",
        f"(damping={damping}, theta0={theta0}) 不在本实例冻结四格内")


def cell_of(inst: "TC.Instance", params: Mapping[str, Any]) -> "str | None":
    """``_cell_of`` 的公开入口（单真值不放两份）。

    R0.2 的恢复路径要按受信 manifest 的 params 复原 RunRecord.cell，必须与
    真实分派用**同一条**归类逻辑，否则复原出来的 cell 可能与原 run 不同
    （oracle 会拿错的格做独立复算）。
    """
    return _cell_of(inst, params)


class TrialRunner:
    """一个 (instance, trial) 的全部分派状态与产物。"""

    def __init__(self, *, campaign_root: Path, instance_id: str,
                 trial_id: str, gpus: bool = False):
        self.instance = TC.get(instance_id)
        self.trial_id = trial_id
        self.gpus = bool(gpus)
        root = Path(campaign_root)
        self.trusted_dir = root / "observations" / "trusted" / trial_id
        self.public_dir = root / "observations" / "public" / trial_id
        self.trial_dir = root / "trials" / trial_id
        self.store = O.TrialObservationStore(trusted_dir=self.trusted_dir,
                                             public_dir=self.public_dir)
        self.analyses = AnalysisRunner(trial_dir=self.trial_dir,
                                       data_dir=self.public_dir)
        self.resources = AT.TrialResources(trial_id=trial_id)
        self.surface = action_surface(self.instance)
        self.prereg: dict[str, Any] | None = None
        self.runs: list[RunRecord] = []
        self.submissions: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        self._seq = 0

    # -- 只读视图 ---------------------------------------------------------
    def _event(self, *, action: str | None, ok: bool, code: str | None = None,
               message: str | None = None, extra: Mapping[str, Any] | None = None
               ) -> dict[str, Any]:
        self._seq += 1
        doc = AC.action_event(action=action, ok=ok, code=code, message=message,
                              seq=self._seq, extra=extra)
        self.events.append(doc)
        return doc

    def cell_runs(self) -> dict[str, RunRecord]:
        out: dict[str, RunRecord] = {}
        for rec in self.runs:
            if rec.cell is not None:
                out[rec.cell] = rec
        return out

    # -- 分派 -------------------------------------------------------------
    def dispatch(self, action: AC.Action | AC.ActionFailure) -> dict[str, Any]:
        """执行一个动作；拒绝也返回事件（trial 继续，动作不生效）。"""
        if isinstance(action, AC.ActionFailure):
            return self._event(action=action.action, ok=False, code=action.code,
                               message=action.message)
        try:
            if isinstance(action, AC.Preregister):
                return self._preregister(action)
            if isinstance(action, AC.RunExperiment):
                return self._run_experiment(action)
            if isinstance(action, AC.Analyze):
                return self._analyze(action)
            if isinstance(action, AC.Submit):
                return self._submit(action)
        except TrialRejected as exc:
            return self._event(action=type(action).__name__, ok=False,
                               code=exc.code, message=exc.detail)
        except AT.ResourceExceeded as exc:
            return self._event(action=type(action).__name__, ok=False,
                               code="RESOURCE_EXCEEDED", message=str(exc))
        except (AnalysisPolicyViolation, O.ObservationIntegrityError) as exc:
            return self._event(action=type(action).__name__, ok=False,
                               code=getattr(exc, "code", "POLICY_VIOLATION"),
                               message=str(exc)[:400])
        raise AssertionError(f"未知动作类型 {type(action)!r}")

    def _preregister(self, action: AC.Preregister) -> dict[str, Any]:
        if self.prereg is not None:
            raise TrialRejected("PREREG_DUPLICATE", "本 trial 已有登记，禁止改登记")
        if self.runs:
            raise TrialRejected("PREREG_TOO_LATE", "首个实验之后不得补登记")
        problems = EV.check_prereg(
            action.plan, expected_cells=TC.prereg_expected_cells(self.instance.instance_id))
        if problems:
            raise TrialRejected("PREREG_INVALID", "; ".join(problems[:5]))
        self.prereg = dict(action.plan)
        self.prereg["_sha256"] = S.sha256_of(action.plan)
        self.prereg["_registered_at_utc"] = S.utc_now()
        path = self.trial_dir / "prereg.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        S.write_canonical_json(path, self.prereg)
        return self._event(action="preregister", ok=True,
                           extra={"prereg_sha256": self.prereg["_sha256"]})

    def _run_experiment(self, action: AC.RunExperiment) -> dict[str, Any]:
        if self.prereg is None:
            raise TrialRejected("PREREG_REQUIRED", "首个实验之前必须先登记")
        params = dict(action.params)
        steps = int(round(float(params.pop("steps"))))
        if steps < 1 or steps > self.instance.max_steps:
            raise TrialRejected("STEPS_OUT_OF_BOUNDS",
                                f"steps={steps} 越界 [1, {self.instance.max_steps}]")
        cell = _cell_of(self.instance, params)
        if cell is not None and cell in self.cell_runs():
            raise TrialRejected("CELL_DUPLICATED",
                                f"{cell} 已有原始 run（禁止重复跑后择优）")
        self.resources.check_wallclock()
        self.resources.charge_experiment(steps)
        if self.instance.template == "pendulum":
            request = O.ExperimentRequest.pendulum(
                trial_id=self.trial_id, instance_id=self.instance.instance_id,
                scene_xml=self.instance.faulted_scene(),
                damping=float(params["damping"]),
                initial_angle=float(params["initial_angle"]),
                timestep=float(params["timestep"]), steps=steps)
        else:
            target = tuple(self.instance.public["goal"]["target"])
            request = O.ExperimentRequest.push(
                trial_id=self.trial_id, instance_id=self.instance.instance_id,
                scene_xml=self.instance.faulted_scene(),
                kp=float(params["kp"]), kd=float(params["kd"]),
                max_force=float(params["max_force"]), target=(float(target[0]),
                                                              float(target[1])),
                timestep=float(params["timestep"]), steps=steps,
                host_injected=self.instance.host_injections)
        manifest, doc = self.store.run(request, gpus=self.gpus)
        if self.instance.template == "pendulum":
            settle = EV.settling(doc["theta"], doc["omega"], doc["t"])
            summary: dict[str, Any] = {"settle": settle.as_dict()}
        else:
            last = len(doc["t"]) - 1
            summary = {
                "final_distance": float(((doc["x"][last] - request.params["target_x"]) ** 2
                                         + (doc["y"][last] - request.params["target_y"]) ** 2
                                         ) ** 0.5),
                "final_speed": float((doc["vx"][last] ** 2 + doc["vy"][last] ** 2) ** 0.5),
                "distance": float(((doc["x"][last] - request.params["target_x"]) ** 2
                                   + (doc["y"][last] - request.params["target_y"]) ** 2
                                   ) ** 0.5),
            }
        rec = RunRecord(run_id=manifest["run_id"], cell=cell,
                        params=dict(manifest["params"]), manifest=manifest,
                        summary=summary)
        self.runs.append(rec)
        path = self.trial_dir / "runs.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(S.canonical_json(rec.as_dict()) + "\n")
        return self._event(action="run_experiment", ok=True,
                           extra={"run_id": rec.run_id, "cell": cell,
                                  "summary": summary,
                                  "preview": O.preview(doc)})

    def _analyze(self, action: AC.Analyze) -> dict[str, Any]:
        self.resources.check_wallclock()
        self.resources.charge_analysis()
        event = self.analyses.run(action.source, action.run_ids)
        return self._event(action="analyze", ok=not event.violations,
                           code=("ANALYSIS_POLICY_VIOLATION"
                                 if event.violations else None),
                           message="; ".join(event.violations)[:400] or None,
                           extra={"analysis": event.as_dict()})

    def _submit(self, action: AC.Submit) -> dict[str, Any]:
        known = {rec.run_id for rec in self.runs}
        unknown = [r for r in action.run_ids if r not in known]
        if unknown:
            raise TrialRejected("RUN_ID_UNKNOWN", f"引用了本 trial 不存在的 run {unknown}")
        analysis_ids = {e.analysis_id for e in self.analyses.events}
        bad = [a for a in action.analysis_ids if a not in analysis_ids]
        if bad:
            raise TrialRejected("ANALYSIS_ID_UNKNOWN", f"引用了不存在的分析 {bad}")
        doc = action.as_dict()
        doc["submitted_utc"] = S.utc_now()
        self.submissions.append(doc)
        path = self.trial_dir / "submissions.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(S.canonical_json(doc) + "\n")
        return self._event(action="submit", ok=True,
                           extra={"claim": action.claim})

    # -- 裁判（只信可信原件） ---------------------------------------------
    def _submission_check(self, expected_claim: str) -> tuple[bool, str]:
        """结论一致性：候选最后一次 submit 必须引用真实分析且结论与复算一致。

        数值（mean_effect/z）不采信候选自报——只比对**结论类别**，因为类别是
        公开合同的产物；候选若给出与可信数据相反/无关的结论，L4/L5 不得通过
        （AC18「错误结论」类负控据此拒绝）。
        """
        if not self.submissions:
            return False, "no_submission"
        last = self.submissions[-1]
        if not last.get("analysis_ids"):
            return False, "no_analysis_referenced"
        if str(last.get("claim")) != str(expected_claim):
            return False, f"claim_mismatch:{last.get('claim')}!={expected_claim}"
        return True, ""

    def judge(self) -> dict[str, Any]:
        """从**可信原件**复算判决；候选自报的 mean_effect/z 一律不参与。"""
        inst = self.instance
        if inst.family == "R":
            cells = self.cell_runs()
            expected = [f"{d}/{a}" for d in ("low", "high") for a in EV.ANGLE_KEYS]
            observations: dict[str, Mapping[str, Any]] = {}
            for cell in expected:
                rec = cells.get(cell)
                if rec is None:
                    continue
                observations[cell] = self.store.load_trusted(
                    rec.run_id, rec.manifest, expected_trial=self.trial_id,
                    expected_instance=inst.instance_id)
            j = EV.judge_research(observations=observations,
                                  h=int(inst.public["research"]["h"]),
                                  prereg=self.prereg)
            sub_ok, sub_reason = self._submission_check(j.claim_status)
            passed = bool(j.analysis_valid and j.informative
                          and j.claim_status in (S.ClaimStatus.SUPPORTED,
                                                 S.ClaimStatus.REFUTED)
                          and sub_ok)
            return {"schema": S.SCHEMA_VERDICT, "layer": EV.Layer.L5,
                    "family": "R", "verdict": j.as_dict(), "pass": passed,
                    "submission_ok": sub_ok, "submission_reason": sub_reason,
                    "prereg_sha256": (self.prereg or {}).get("_sha256")}

        runs = [{"run_id": rec.run_id, **rec.summary} for rec in self.runs]
        eng = EV.evaluate_engineering(runs=runs, tolerance=inst.tolerance)
        sub_ok, sub_reason = self._submission_check(S.ClaimStatus.SUPPORTED)
        return {"schema": S.SCHEMA_VERDICT, "layer": EV.Layer.L4,
                "family": inst.family, "verdict": eng,
                "pass": bool(eng["status"] == "PASS" and sub_ok),
                "submission_ok": sub_ok, "submission_reason": sub_reason,
                "prereg_sha256": (self.prereg or {}).get("_sha256")}

    def summary(self) -> dict[str, Any]:
        return {"trial_id": self.trial_id, "instance_id": self.instance.instance_id,
                "family": self.instance.family, "runs": len(self.runs),
                "analyses": self.analyses.used,
                "submissions": len(self.submissions),
                "events": len(self.events),
                "resources": self.resources.as_dict(),
                "prereg_sha256": (self.prereg or {}).get("_sha256"),
                "judge": self.judge()}
