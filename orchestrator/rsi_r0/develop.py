"""T6 三条开发校准轨迹（E/D/R 各一实例，每族至多 24 次请求）。

计划 §3.5 T6：

* 每族 24 请求帽，8/16/24 只读检查点；
* 记录首失败层、首次功能/研究成功、token/时间/人工干预；
* 开发结束独立评审；G-V 条件 = E、D 均有 L4 PASS，R 有 L5 PASS，安全/仪器未坏；
* **禁止把开发 0/1 无限补测到 1/1**：每族只有一条轨迹，失败了就是失败，
  不做第二次"再试一次"。

候选可见面与 T5 协议一致：单行 JSON、无围栏、无重试、宿主代跑仿真。
候选只能发有界数值参数；场景、故障、真值全在宿主侧（``ExperimentSession``）。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from orchestrator.common import runtime_paths as rp
from orchestrator.gateway import quota_ledger as ql
from orchestrator.gateway.quota_ledger import billing_context
from orchestrator.rsi_r0 import budget_contract
from orchestrator.rsi_r0.attempts import AttemptJournal, BudgetReservations, campaign_lock
from orchestrator.rsi_r0.contracts import (ResourceCaps, instance_task_id,
                                            private_write, sha256_of)
from orchestrator.rsi_r0.evaluator import EvaluationInput, evaluate
from orchestrator.rsi_r0.faults import apply_fault
from orchestrator.rsi_r0.image_pin import IMAGE_REF
from orchestrator.rsi_r0.instances import PLANS
from orchestrator.rsi_r0.scenes import build_scene
from orchestrator.rsi_r0.trusted import (
    CandidateRequestError,
    ExperimentSession,
    TrustedServiceError,
)

REPORT_SCHEMA = "rsi_r0.development_report.v1"
STAGE = "coding_debug"
DEV_CAP = 24
CHECKPOINTS = (8, 16, 24)
REQUEST_TIMEOUT_S = 180
#: 输出预算（不是实验判据，是仪器参数；报价合同 output_tokens_cap=8192 已按此价）。
#: 2026-11-08 T6 重跑取证：E/D/R 三族都在**续轮**（第 2/3 次请求）撞 HTTP 200 空回答
#: （EmptyResponseError，13–14s；成功轮 9–10s），与 gateway/router.py 攢批92 记录
#: 的「思考把输出预算吃光、零正文」同签名；1200 是我自己拍的数，计划从未规定。
#: 抬到合同上限 8192，使「撞帽」不再是候选；若仍在 8192 下空答，则不是帽的问题。
MAX_TOKENS = 8192
DEV_BATCHES: Mapping[str, str] = {"E": "R0-D-E", "D": "R0-D-D", "R": "R0-D-R"}
DEV_ACTOR_NAME = "rsi_r0.develop"
#: 仪器类失败标记：必须出现在 blocked_reason 里，T7 停批判定靠它（别只写进 journal）。
HIDDEN_RETRY_MARKER = "HiddenRetryDetected"
TASKS_ROOT = Path(__file__).resolve().parents[2] / "benchmarks" / "rsi_r0" / "tasks"

PROTOCOL_SPEC: Mapping[str, Any] = {
    "schema": "rsi_r0.dev_protocol.v1",
    "response_format": "single_line_json_object",
    "actions": ["run_experiment", "submit"],
    "request_cap": DEV_CAP,
    "checkpoints": list(CHECKPOINTS),
    "retry": "none",
    "context_compression": "none",
    "error_feedback": "class_only_once",
    "temperature": 0.2,
}


def protocol_spec_hash() -> str:
    return sha256_of(dict(PROTOCOL_SPEC))


class DevelopmentBlocked(RuntimeError):
    """本族开发轨迹终止（不是能力结论，是流程结论）。"""


class DevelopmentError(RuntimeError):
    """前置条件不成立（没资格跑），与 DevelopmentBlocked 分开。"""


# ---------------------------------------------------------------- 任务与场景
def load_task(instance_id: str) -> dict[str, Any]:
    import yaml

    path = TASKS_ROOT / instance_id / "task.yaml"
    if not path.is_file():
        raise DevelopmentError(f"任务包缺失：{path}")
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def load_private(instance_id: str) -> dict[str, Any]:
    path = rp.rsi_r0_private_dir() / "instances" / f"{instance_id}.json"
    if not path.is_file():
        raise DevelopmentError(f"私有真值缺失：{path}")
    return json.loads(path.read_text(encoding="utf-8"))


def plan_for(instance_id: str):
    for plan in PLANS:
        if plan.instance_id == instance_id:
            return plan
    raise DevelopmentError(f"未冻结的实例：{instance_id}")


def build_host_session(instance_id: str) -> tuple[ExperimentSession, dict[str, Any], str]:
    """宿主侧会话：场景由任务书构造；D 族在这里注入故障（候选永远看不到）。"""
    plan = plan_for(instance_id)
    task = load_task(instance_id)
    private = load_private(instance_id)
    scene_xml = build_scene(plan.template, plan.public["scene_params"])
    host_fields: dict[str, Any] = {}
    fault = private.get("fault")
    if fault:
        scene_xml = apply_fault(scene_xml, fault)
        if fault["kind"] == "force_scale":
            host_fields = {"force_scale": float(fault["scale"])}
    kind = str(task["interface"]["kind"])
    # 任务书公开 keys 只有增益类字段；target_x/target_y 是**题目**（goal.target），
    # 由宿主注入为固定参数。T6 首跑实测（2026-11-08）：不注入时容器 push_control
    # 以 rc=2 拒绝"缺 target_x"，整条轨迹被误判成仪器故障。
    fixed: dict[str, float] = {}
    if kind == "push_control":
        target = list(task["goal"]["target"])
        fixed = {"target_x": float(target[0]), "target_y": float(target[1])}
    session = ExperimentSession(
        kind=kind, image_digest=IMAGE_REF,
        scene_builder=lambda _params, _xml=scene_xml: _xml,
        max_steps=int(task["steps_max"]), host_fields=host_fields or None,
        fixed_params=fixed or None)
    return session, task, scene_xml


# ---------------------------------------------------------------- 提示词
def brief_prompt(task: Mapping[str, Any], *, remaining: int) -> str:
    return (
        "你在参加一次机器人仿真平台的开发校准轨迹。你只能通过可信服务做实验。\n"
        "任务书（公开面，JSON）：\n"
        f"{json.dumps(task, ensure_ascii=False, sort_keys=True)}\n"
        f"本轨迹最多还能发 {remaining} 次实验请求。\n"
        "每条回复只输出一行 JSON，不要解释、不要代码块围栏：\n"
        '发实验：{"action":"run_experiment","params":{...}}\n'
        '交卷：{"action":"submit", ...按任务书 answer.required 的字段...}'
    )


def result_prompt(result: Mapping[str, Any]) -> str:
    return ("实验已执行，宿主回报如下（JSON）：\n"
            f"{json.dumps(result, ensure_ascii=False, sort_keys=True)}\n"
            "继续：只输出一行 JSON（run_experiment 或 submit）。")


def error_prompt(kind: str) -> str:
    return (f'上一条动作被拒绝，错误类别：{kind}。'
            "继续：只输出一行 JSON（run_experiment 或 submit）。")


def summarize_result(result: Mapping[str, Any]) -> dict[str, Any]:
    """给候选看的**可观测**结果：去掉宿主内部字段，序列下采样到 200 点。"""
    keep = ("run_id", "steps_run", "distance", "final_pos", "final_speed", "target",
            "final_angle", "final_angular_speed", "energy_initial", "energy_final",
            "nq", "nv", "mujoco_version")
    out = {k: result[k] for k in keep if k in result}
    series = result.get("series") or result.get("angle_series")
    if isinstance(series, list) and series:
        stride = max(1, len(series) // 200)
        out["series_sample_stride"] = stride
        out["series_len"] = len(series)
        out["series_head"] = series[:3]
        out["series_tail"] = series[-3:]
        out["series"] = series[::stride][:200]
    return out


# ---------------------------------------------------------------- 轨迹主体
class DevelopmentTrial:
    """一条轨迹（T6 开发或 T7 验证共用同一台机器，差别只在参数）。

    T7 的六条验证轨迹与 T6 结构一致（单行 JSON、宿主代跑、无重试），差别是：
    批次号不同、请求帽按批次票、检查点只在预算耗尽时记终态、``role`` 不同。
    """

    def __init__(self, runner: Any, gateway: Any, family_key: str, *,
                 instance_id: str | None = None,
                 batch_id: str | None = None,
                 role: str = "development",
                 task_id: int | None = None,
                 request_cap: int = DEV_CAP,
                 checkpoints: Sequence[int] = CHECKPOINTS,
                 journal: AttemptJournal | None = None,
                 session_factory: Callable[[str], tuple[Any, dict, str]] | None = None,
                 clock: Callable[[], str] | None = None) -> None:
        if family_key not in DEV_BATCHES and batch_id is None:
            raise DevelopmentError(f"未知族：{family_key!r}")
        self.runner = runner
        self.gateway = gateway
        self.family_key = family_key
        self.instance_id = instance_id or f"{family_key}-dev"
        self.role = role
        # task 号必须按实例族取（D=2/R=3）：T6 首跑实测（2026-11-08）默认 1
        # 与 D/R 批次票的 task 不符，被账本按 batch_approval_mismatch 挡下。
        self.task_id = (int(task_id) if task_id is not None
                        else instance_task_id(self.instance_id))
        self.request_cap = int(request_cap)
        self.checkpoints = tuple(int(c) for c in checkpoints)
        self.batch_id = batch_id or DEV_BATCHES[family_key]
        self.journal = journal or AttemptJournal(
            rp.rsi_r0_attempts_path(),
            BudgetReservations(ResourceCaps(), rp.rsi_r0_reservations_dir()))
        self.session_factory = session_factory or build_host_session
        self.clock = clock or (lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        self.contract = budget_contract.read_contract(
            rp.rsi_r0_protocol_dir() / "pricing_contract.json")
        self._used = 0
        self._tokens_in = 0
        self._tokens_out = 0
        self._seqs: list[int] = []
        self._human_interventions = 0
        # 逐请求日志必须先挂在 self 上：``_request`` 抛 DevelopmentBlocked 时
        # ``run()`` 还没走到构造行那一步，若日志只是局部变量，整条轨迹的现场
        # （每个请求的动作类型/stop_reason/用量）就随异常一起丢了——BLOCKED 行
        # 只剩一句 blocked_reason，事后根本分不清是"模型第一次就作答非法"还是
        # "第二轮才空回答"（2026-11-08 T6 重跑实测踩到：三族全 BLOCKED，log 全空）。
        self._log: list[dict[str, Any]] = []
        self._checkpoints_seen: list[dict[str, Any]] = []

    # ---- 前置 ----
    def preconditions(self) -> dict[str, Any]:
        ok, why = budget_contract.contract_ok(self.contract)
        if not ok:
            raise DevelopmentError(f"BLOCKED_COST：{why}")
        # 评审 N-1（阻断项）：AC27 要的是「开发没过门就一个 POST 都不许发到验证」。
        # 旧版这里无条件查 G-P，于是 role="validation" 的试验只凭 G-P 成立就能发满
        # 6 slot × 24 = 144 次——G-V 只在 CLI 一层，直接调用 API 就绕过去了。
        # 按角色选门：验证试验查 G-V（G-V 的第一条就是 gate_p_passed，含批准件与
        # 本批票据的校验），开发试验查 G-P。
        gate_name = "G-V" if self.role == "validation" else "G-P"
        try:
            gate = self.runner.require_gate(gate_name, batch_id=self.batch_id)
        except Exception as exc:  # noqa: BLE001 —— 统一成本模块自己的错误类型
            raise DevelopmentError(f"{gate_name} 不成立：{exc}") from exc
        try:
            ticket = ql.load_approval(self.batch_id, rp.rsi_r0_approvals_dir())
        except Exception as exc:  # noqa: BLE001
            raise DevelopmentError(f"批次票据不可用：{exc}") from exc
        if int(ticket.get("approved") or 0) > self.request_cap:
            raise DevelopmentError(
                f"票据批准量 {ticket.get('approved')} 超过本族帽 {self.request_cap}")
        # T6 重跑实测（2026-11-08）：本批票据是**跨进程累计**的，而 request_cap 是
        # 单进程计划量。E 首跑已用 1 行、D/R 各有 1 行 preflight_blocked（占批次），
        # 若重跑仍按 24 规划，第 24 次就会撞上 quota_ledger 的
        # `batch_budget_reached` 熔断（写 pause 件、要 resume 票）。计划量必须取
        # 「票据批准量 − 本批已占行」与族帽的较小者。
        used = sum(1 for r in ql.read_lines(rp.rsi_r0_ledger_path())
                   if str(r.get("batch_id")) == self.batch_id)
        remaining = int(ticket.get("approved") or 0) - used
        if remaining <= 0:
            raise DevelopmentError(
                f"批次 {self.batch_id} 已用 {used} 行，票据批准 "
                f"{ticket.get('approved')} 行已耗尽（不许超票发网）")
        self.request_cap = min(self.request_cap, remaining)
        return {"gate": gate, "ticket": ticket,
                "batch_used": used, "request_cap": self.request_cap,
                "pricing_contract_sha256": self.contract.get("contract_sha256")}

    # ---- 单请求 ----
    def _request(self, *, index: int, messages: Sequence[Mapping[str, Any]]
                 ) -> tuple[str, dict[str, Any]]:
        if index > self.request_cap:
            raise DevelopmentError(f"已达本族请求帽 {self.request_cap}，拒绝再发")
        cap_in = int(self.contract["input_tokens_cap"])
        cap_out = int(self.contract["output_tokens_cap"])
        per_usd = float(self.contract["worst_case_per_request_usd"])
        slot_id = f"{self.family_key}-{index:02d}"
        rec = self.journal.reserve(slot_id=slot_id, batch_id=self.batch_id,
                                   task_id=self.task_id, stage=STAGE,
                                   tokens_in=cap_in, tokens_out=cap_out, usd=per_usd)
        self.journal.mark_sent(rec, seq=None)          # 崩溃契约：SENT 先于发网
        before = len(ql.read_lines(rp.rsi_r0_ledger_path()))
        meta: dict[str, Any] = {}
        try:
            with billing_context(task=self.task_id, batch_id=self.batch_id,
                                 thread_id=f"dev:{self.family_key}"):
                text, usage = self.gateway.call(
                    STAGE, list(messages), max_tokens=MAX_TOKENS, temperature=0.2,
                    timeout=REQUEST_TIMEOUT_S, capture=meta)
        except Exception as exc:  # noqa: BLE001
            row = self._new_row(before)
            seq = row.get("seq") if row else None
            if isinstance(seq, int):
                self.journal.mark_sent(rec, seq=seq)
                self.journal.settle_fail(rec, error_class=type(exc).__name__)
            elif row is not None:
                self.journal.block_preflight(rec, reason=f"gateway_refused:{type(exc).__name__}")
            else:
                self.journal.settle_fail(rec, error_class=f"unaccounted:{type(exc).__name__}")
            raise DevelopmentBlocked(
                f"{self.family_key} 第 {index} 个请求失败：{type(exc).__name__}: {exc}") from exc
        row = self._new_row(before)
        if not row or row.get("ok") is not True or not isinstance(row.get("seq"), int):
            if row and isinstance(row.get("seq"), int):
                self.journal.mark_sent(rec, seq=row["seq"])
                self.journal.settle_fail(rec, error_class="ledger_row_not_ok")
            elif row is not None:
                self.journal.block_preflight(rec, reason="gateway_refused_no_seq")
            else:
                self.journal.settle_fail(rec, error_class="unaccounted:no_ledger_row")
            raise DevelopmentBlocked(f"{self.family_key} 第 {index} 个请求：记账缺口")
        self._used += 1
        self._seqs.append(int(row["seq"]))
        self._tokens_in += int(usage[0] or 0)
        self._tokens_out += int(usage[1] or 0)
        attempts_http = meta.get("attempts_http")
        if isinstance(attempts_http, int) and attempts_http > 1:
            self.journal.mark_sent(rec, seq=row["seq"])
            self.journal.settle_fail(rec, error_class=HIDDEN_RETRY_MARKER)
            raise DevelopmentBlocked(
                f"{self.family_key} 第 {index} 个请求实际发了 {attempts_http} 次"
                f"（AC07 {HIDDEN_RETRY_MARKER}）")
        self.journal.mark_sent(rec, seq=row["seq"])
        self.journal.settle_ok(
            rec, usage_in=usage[0], usage_out=usage[1],
            stop_reason=meta.get("stop_reason"), block_types=("text",),
            provider_request_id=meta.get("provider_request_id"),
            body_sha256=meta.get("body_sha256"), usd=None)
        return text, {"seq": row["seq"], "attempts_http": attempts_http,
                      "stop_reason": meta.get("stop_reason"),
                      "usage": {"in": usage[0], "out": usage[1]},
                      "body_sha256": meta.get("body_sha256")}

    @staticmethod
    def _new_row(before: int) -> dict[str, Any] | None:
        rows = ql.read_lines(rp.rsi_r0_ledger_path())
        return rows[-1] if len(rows) > before else None

    # ---- 全轨迹 ----
    def run(self) -> dict[str, Any]:
        pre = self.preconditions()
        instance_id = self.instance_id
        session, task, scene_xml = self.session_factory(instance_id)
        scene_sha = sha256_of(scene_xml)
        answer: dict[str, Any] | None = None
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": brief_prompt(task, remaining=self.request_cap)}]
        # 与 self._log/self._checkpoints_seen 同一对象：中途抛异常时现场仍然可读。
        checkpoints: list[dict[str, Any]] = self._checkpoints_seen
        blocked_reason: str | None = None
        log: list[dict[str, Any]] = self._log
        started = time.time()

        for index in range(1, self.request_cap + 1):
            text, meta = self._request(index=index, messages=messages)
            messages.append({"role": "assistant", "content": text})
            action = _parse_action(text)
            entry = {"index": index, "seq": meta["seq"], "usage": meta["usage"],
                     "stop_reason": meta["stop_reason"], "action": None,
                     # 现场取证：模型这一轮到底回的是什么形状（后一轮为空回答时，
                     # 唯一能判断"是不是上一轮把上下文带歪了"的材料就是它）。
                     "text_len": len(text), "text_head": text[:200]}
            if action is None:
                entry["action"] = {"kind": "invalid_json"}
                messages.append({"role": "user", "content": error_prompt("invalid_json")})
            elif action.get("action") == "run_experiment":
                try:
                    result = session.run(action.get("params") or {})
                except CandidateRequestError as exc:
                    entry["action"] = {"kind": "rejected", "error": str(exc)}
                    messages.append({"role": "user", "content": error_prompt("CandidateRequestError")})
                except TrustedServiceError as exc:
                    entry["action"] = {"kind": "service_error", "error": str(exc)}
                    blocked_reason = f"可信服务失败：{exc}"
                    log.append(entry)
                    break
                else:
                    summary = summarize_result(result)
                    entry["action"] = {"kind": "run_experiment", "run_id": result.get("run_id"),
                                       "params_sha256": sha256_of(dict(action.get("params") or {}))}
                    messages.append({"role": "user", "content": result_prompt(summary)})
            elif action.get("action") == "submit":
                answer = {k: v for k, v in action.items() if k != "action"}
                entry["action"] = {"kind": "submit", "answer_sha256": sha256_of(answer)}
            else:
                entry["action"] = {"kind": "unknown_action",
                                   "value": str(action.get("action"))[:40]}
                messages.append({"role": "user", "content": error_prompt("unknown_action")})

            log.append(entry)
            if answer is not None:
                break
            if index in self.checkpoints:
                checkpoints.append(self._checkpoint(index, log, session, task, None))

        wall = round(time.time() - started, 3)
        evaluation = self._evaluate(session, task, instance_id, answer, scene_sha,
                                    requests_used=self._used)
        if answer is not None:
            checkpoints.append({"at_request": self._used, "requests_used": self._used,
                                "runs": len(session.calls),
                                "tokens": {"in": self._tokens_in, "out": self._tokens_out},
                                "recorded_at_utc": self.clock(), "answer_present": True,
                                "layer_status": {k: v["status"] for k, v in
                                                 (evaluation.get("layers") or {}).items()}})
        if blocked_reason and answer is None:
            state = "BLOCKED"
        elif answer is not None:
            state = "SUBMITTED"
        else:
            state = "NO_SUBMISSION"
        row = {
            "family": self.family_key,
            "role": self.role,
            "instance_id": instance_id,
            "batch_id": self.batch_id,
            "state": state,
            "blocked_reason": blocked_reason,
            "requests_used": self._used,
            "request_cap": self.request_cap,
            "ledger_seqs": list(self._seqs),
            "tokens": {"in": self._tokens_in, "out": self._tokens_out},
            "wall_s": wall,
            "human_interventions": self._human_interventions,
            "checkpoints": checkpoints,
            "calls": len(session.calls),
            "scene_sha256": scene_sha,
            "answer": answer,
            "evaluation": evaluation,
            "first_failure_layer": _first_failure(evaluation),
            "log": log,
        }
        return row

    def _checkpoint(self, index: int, log: Sequence[Mapping[str, Any]], session: Any,
                    task: Mapping[str, Any], answer: Mapping[str, Any] | None
                    ) -> dict[str, Any]:
        """只读检查点：只读现有事实，不发起任何新请求、不改轨迹。"""
        out = {"at_request": index, "requests_used": self._used,
               "runs": len(session.calls),
               "tokens": {"in": self._tokens_in, "out": self._tokens_out},
               "recorded_at_utc": self.clock(),
               "answer_present": answer is not None}
        return out

    def _evaluate(self, session: Any, task: Mapping[str, Any], instance_id: str,
                  answer: Mapping[str, Any] | None, scene_sha: str, *,
                  requests_used: int, scenes: bool = True) -> dict[str, Any]:
        if answer is None:
            return {"execution": "FAILED", "scientific": "NOT_APPLICABLE",
                    "layers": {}, "reason": "no_answer_submitted",
                    "requests_used": requests_used}
        private = load_private(instance_id) if instance_id else {}
        truth = private
        priv_rules = _priv_rules(private)

        def replay(call: Mapping[str, Any]) -> dict[str, Any]:
            fresh, _task, _scene = self.session_factory(instance_id)
            return fresh.run(call["params"])

        scenes_kwargs: dict[str, Any] = {}
        if scenes and scene_sha:
            scenes_kwargs["scene_sha256"] = scene_sha
        result = evaluate(EvaluationInput(
            task=task, answer=answer, calls=list(session.calls),
            replay=replay, truth=truth, priv_rules=priv_rules,
            slot_id=instance_id or f"{self.family_key}-checkpoint",
            requests_used=requests_used, **scenes_kwargs))
        # TrialResult → 可 JSON 化的字典（报告与门事实都只读这份）
        return result.as_dict()


def _priv_rules(private: Mapping[str, Any]) -> dict[str, Any]:
    rules: dict[str, Any] = {}
    if "tolerance" in private:
        tol = private["tolerance"]
        rules["tolerance"] = tol
    if "equivalence_bound" in private:
        rules["equivalence_bound"] = private["equivalence_bound"]
    if "support_threshold" in private:
        rules["support_threshold"] = private["support_threshold"]
    return rules


def _parse_action(text: str) -> dict[str, Any] | None:
    if not text or not text.strip():
        return None
    stripped = text.strip()
    if "\n" in stripped or stripped.startswith("```"):
        return None
    try:
        doc = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    return doc if isinstance(doc, dict) else None


def _first_failure(evaluation: Mapping[str, Any]) -> dict[str, Any] | None:
    layers = evaluation.get("layers") or {}
    for name in ("L0", "L1", "L2", "L3", "L4", "L5"):
        layer = layers.get(name) or {}
        status = layer.get("status")
        if status in ("FAIL", "NOT_REACHED"):
            return {"layer": name, "status": status,
                    "reason_code": layer.get("reason_code")}
    return None


# ---------------------------------------------------------------- 批次入口
def run_development(runner: Any, gateway: Any, families: Sequence[str] = ("E", "D", "R"),
                    *, clock: Callable[[], str] | None = None) -> dict[str, Any]:
    """按 E→D→R 顺序跑三条开发轨迹（串行；全程持 campaign 锁，中途失败不补测）。"""
    with campaign_lock(rp.rsi_r0_quota_dir()):
        return _run_development_locked(runner, gateway, families, clock=clock)


def _run_development_locked(runner: Any, gateway: Any, families: Sequence[str],
                            *, clock: Callable[[], str] | None = None) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for key in families:
        trial = DevelopmentTrial(runner, gateway, key, clock=clock)
        try:
            rows.append(trial.run())
        except DevelopmentBlocked as exc:
            # 现场照抄，不丢：BLOCKED 的轨迹同样要能回答"模型到底做了什么"。
            rows.append({"family": key, "instance_id": getattr(trial, "instance_id", None)
                         or f"{key}-dev",
                         "batch_id": DEV_BATCHES[key], "state": "BLOCKED",
                         "blocked_reason": str(exc), "requests_used": trial._used,
                         "ledger_seqs": list(trial._seqs), "evaluation": {},
                         "first_failure_layer": None,
                         "tokens": {"in": trial._tokens_in, "out": trial._tokens_out},
                         "checkpoints": list(trial._checkpoints_seen),
                         "log": list(trial._log)})
    report = {
        "schema": REPORT_SCHEMA,
        "campaign": str(runner.root),
        "created_at_utc": (clock or (lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                           time.gmtime())))(),
        "protocol_spec": dict(PROTOCOL_SPEC),
        "protocol_spec_sha256": protocol_spec_hash(),
        "model": {"stage": STAGE, "image": IMAGE_REF},
        # 生成参数逐次入报告：抬帽/改超时这类仪器动作必须能被评审看见。
        "generation": {"max_tokens": MAX_TOKENS, "request_timeout_s": REQUEST_TIMEOUT_S},
        "families": rows,
        "totals": {
            "requests_used": sum(int(r.get("requests_used") or 0) for r in rows),
            "tokens": {"in": sum(int((r.get("tokens") or {}).get("in") or 0) for r in rows),
                       "out": sum(int((r.get("tokens") or {}).get("out") or 0) for r in rows)},
            "ledger_seqs": [s for r in rows for s in (r.get("ledger_seqs") or [])],
        },
    }
    report["report_sha256"] = sha256_of(report)
    return report


def gate_v_facts(report: Mapping[str, Any]) -> dict[str, bool]:
    """G-V 里与开发报告有关的两条条件（gate_p_passed / snapshot_frozen 由 runner 判）。"""
    by_family = {r["family"]: r for r in report.get("families") or []}

    def _layer_ok(key: str, layer: str) -> bool:
        layers = ((by_family.get(key) or {}).get("evaluation") or {}).get("layers") or {}
        return bool((layers.get(layer) or {}).get("status") == "PASS")

    instrument_ok = all(
        r.get("state") != "BLOCKED" or HIDDEN_RETRY_MARKER not in str(r.get("blocked_reason"))
        for r in report.get("families") or [])
    return {
        "development_report_present": len(by_family) == 3
        and all(by_family.get(k) is not None for k in ("E", "D", "R")),
        "development_gate_met": (_layer_ok("E", "L4") and _layer_ok("D", "L4")
                                 and _layer_ok("R", "L5") and instrument_ok),
    }


def write_report(report: Mapping[str, Any], path: Path | None = None) -> Path:
    target = path or (rp.rsi_r0_protocol_dir() / "development_report.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    private_write(target, json.dumps(report, ensure_ascii=False, indent=2,
                                     sort_keys=True) + "\n")
    return target
