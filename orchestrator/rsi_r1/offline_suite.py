"""RSI-R1.2 T5：离线生产演练六套夹具（Plan §4）。

纪律（与 ``offline_validation`` 同一张合同）：

* **只用正式生产件**：``protocol.run_protocol``（probe/practice 单链）、
  ``study.run_study``（A/B 完整链）、``replay_driver.run_replay``（独立物理复跑）、
  ``closure.build_evidence_pack``/``run_closure_judge``（closeout）；真实
  ``DockerRunner`` + 真实 MuJoCo 场景 + analysis 沙盒。
* **只替换模型 transport**：suite1 用子进程公开消费者
  （``OfflineConsumerTransport``），suite2 用预注册脚本回合
  （``ScriptedTransport``，非 POST 但占同一决策槽）。
* **零 POST**：每套跑前跑后账本都必须零 call 行；任何一行即中止。
* **先注册再跑**：六套的预期写在 ``contract/offline_suite_spec.json``
  （``offline_validation.suite_spec_document``），跑后不改预期凑绿。

顺序约束（root 状态机单向）：suite2 的探针链带一次超帽拒绝→自修完成，它**就是**
probe 单链（cap 12，决策 10 ≤ 12）；suite1 随后跑 practice 单链 + A/B 完整链 +
两 study 物理 replay + closeout。报告把两套观察事实分开写，不把一条带恢复的链
冒充成两条链。
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from orchestrator.common.clock import utc_now_iso_z
from orchestrator.common import research_transport as T
from orchestrator.rsi_r1 import assembly as A
from orchestrator.rsi_r1 import closure as CLOSURE
from orchestrator.rsi_r1 import closeout as CLOSEOUT
from orchestrator.rsi_r1 import contracts as C
from orchestrator.rsi_r1 import experiments as E
from orchestrator.rsi_r1 import human_events as HE
from orchestrator.rsi_r1 import lock as LK
from orchestrator.rsi_r1 import loop as L
from orchestrator.rsi_r1 import offline_consumer as OC
from orchestrator.rsi_r1 import offline_validation as OV
from orchestrator.rsi_r1 import profiles as P
from orchestrator.rsi_r1 import protocol as PT
from orchestrator.rsi_r1 import replay_driver as RPD
from orchestrator.rsi_r1 import resources as RS
from orchestrator.rsi_r1 import state as S
from orchestrator.rsi_r1 import study as STD
from orchestrator.rsi_r1 import watchdog as W

PROGRESS_RELPATH = "contract/offline_validation/progress.jsonl"
SUITE_ROOT_RELPATH = "contract/offline_validation"

#: suite6 现包现核用的前缀：正式 ``PACK_PREFIXES`` 去掉 ``protocol``（离线演练
#: 的 probe turn 日志原稿数百 MB，复制只增 I/O 不增信号，见 suite6 内注释）。
_SUITE_PACK_PREFIXES = tuple(
    p for p in CLOSURE.PACK_PREFIXES if p != "protocol")

SUITE_ORDER = ("suite2_over_cap_self_repair", "suite1_single_chains",
               "suite3_boundaries", "suite4_transport_faults",
               "suite5_state_isolation", "suite6_human_and_pack")

_RUNNERS = {
    "suite1_single_chains": "suite1_single_chains",
    "suite2_over_cap_self_repair": "suite2_over_cap_self_repair",
    "suite3_boundaries": "suite3_boundaries",
    "suite4_transport_faults": "suite4_transport_faults",
    "suite5_state_isolation": "suite5_state_isolation",
    "suite6_human_and_pack": "suite6_human_and_pack",
}


class OfflineSuiteError(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = str(code)
        self.detail = str(detail)
        super().__init__(f"[{self.code}] {self.detail}")


def _err_code(exc: BaseException) -> str:
    return str(getattr(exc, "code", None) or type(exc).__name__)


# ---------------------------------------------------------------------------
# 进度（长跑可观测；append-only JSONL，不参与判决）
# ---------------------------------------------------------------------------

class Progress:
    def __init__(self, ctx: Any, *, clock: Callable[[], str] = utc_now_iso_z
                 ) -> None:
        self.ctx = ctx
        self.clock = clock
        self.n = 0

    def note(self, step: str, **fields: Any) -> "dict[str, Any]":
        self.n += 1
        doc = {"seq": self.n, "at_utc": self.clock(), "step": str(step), **fields}
        target = self.ctx.relative(PROGRESS_RELPATH)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(doc, ensure_ascii=False, sort_keys=True,
                                allow_nan=False) + "\n")
            fh.flush()
        return doc


# ---------------------------------------------------------------------------
# 超帽设计源码（suite2 第一次提交：65 cells > 64 帽）
# ---------------------------------------------------------------------------

_OVER_CAP_DESIGN = '''\
import hashlib
import json
import sys
from pathlib import Path


def plan_sha(doc):
    blob = json.dumps(doc, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def main() -> int:
    view = Path(sys.argv[1])
    out = Path(sys.argv[2])
    plans = sorted(view.glob("plan-*.json"))
    if len(plans) != 1:
        print("expected exactly one plan", file=sys.stderr)
        return 2
    plan = json.loads(plans[0].read_text(encoding="utf-8"))
    cells = []
    block = 1
    while len(cells) < 65:
        for cond in plan["conditions"]:
            if len(cells) >= 65:
                break
            cells.append({"condition_id": cond["id"],
                          "block_id": "b%03d" % block,
                          "params": dict(cond["params"])})
        block += 1
    doc = {"schema": "simpraxis.rsi_r1.matrix.v1", "schema_version": 1,
           "study_id": plan["study_id"], "plan_sha": plan_sha(plan),
           "cells": cells}
    (out / "matrix.json").write_text(
        json.dumps(doc, sort_keys=True) + "\\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


def _last_result(messages: "list[Mapping[str, Any]]") -> "dict[str, Any] | None":
    for msg in reversed(messages):
        content = str(msg.get("content", ""))
        if not content.startswith("{"):
            continue
        try:
            doc = json.loads(content)
        except ValueError:
            continue
        if isinstance(doc, dict) and "ok" in doc:
            return doc
    return None


def _recovery_decider(*, over_cap_first: bool = True):
    """预注册脚本回合：首次设计超帽（65 cells），被拒后改用合法设计。

    其余动作与公开消费者**同一确定性状态机**（``offline_consumer.decide``）；
    这里只覆盖「设计源码」这一个公开字段，不读任何宿主私有答案。

    宿主失效规则（``recovery.INVALIDATE_FROM``）：design 字节一变，plan 里程碑
    即作废，游标退回 propose_plan 位。候选若只换设计而不重报计划，后续
    run_batch 只是「额外动作」不推进游标，最后撞 ``BUDGET_UNREACHABLE``
    （2026-11-12 实测：6 呼后停）。因此自修序列必须是
    「换设计 → 重报计划（revision+1，否则 REVISION_GAP）→ 新 plan sha 重跑」。
    """
    state = {"injected": False, "repair": 0}

    def decide(messages: "list[Mapping[str, Any]]") -> "Mapping[str, Any]":
        vis = OC._task_brief(messages) or {}
        study = str(vis.get("study") or "A")
        plan = vis.get("plan") or {}
        last = _last_result(messages)
        if isinstance(last, dict) and last.get("ok") is False \
                and str(last.get("code")) == "RESOURCE_EXCEEDED":
            # 同一 attempt 自修：交一份合法设计源码；额外回合照计决策槽。
            state["repair"] = 1
            return {"action": "write_artifact", "kind": "design",
                    "source": OC.DESIGN_SOURCE}
        if state["repair"] == 1:
            # design 一改，宿主把 plan 里程碑作废：重报计划（revision 必须 +1）。
            state["repair"] = 2
            return {"action": "propose_plan",
                    "plan": OC.toy_plan(
                        study, revision=int(plan.get("revision") or 1) + 1)}
        try:
            doc = OC.decide(messages)
        except SystemExit as exc:  # 消费者状态机停滞 → 宿主侧显式失败
            raise OfflineSuiteError("SCRIPTED_STALL", str(exc)) from None
        if over_cap_first and doc.get("action") == "write_artifact" \
                and doc.get("kind") == "design" and not state["injected"]:
            state["injected"] = True
            return {"action": "write_artifact", "kind": "design",
                    "source": _OVER_CAP_DESIGN}
        return doc

    return decide


# ---------------------------------------------------------------------------
# 套件执行上下文
# ---------------------------------------------------------------------------

@dataclass
class SuiteEnv:
    ctx: Any
    image: str
    runner_factory: Callable[[], Any]
    counter: Callable[[str], int]
    ledger_path: Path
    repo_root: "Path | str | None"
    profile: P.CampaignProfile
    progress: Progress
    canaries: OV.CanarySet = OV.CanarySet()
    turn_log: OV.TurnLog = field(init=False)
    spend: A.SpendContract = field(init=False)
    results: "dict[str, dict[str, Any]]" = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.spend = A.SpendContract(mode=A.MODE_OFFLINE_VALIDATION, consent=None)
        self.turn_log = OV.TurnLog(self.ctx, canaries=self.canaries)

    # -- 小工具 ----------------------------------------------------------
    def turn_log_with(self, extra: "tuple[bytes, ...]") -> OV.TurnLog:
        """分相位 canary 集：同一文件、不同禁用串（跨 study/跨 attempt 隔离）。"""
        return OV.TurnLog(self.ctx, canaries=OV.CanarySet(
            self.canaries.values + tuple(extra)))

    # -- 小工具 ----------------------------------------------------------
    def suite_dir(self, name: str) -> Path:
        path = self.ctx.relative(SUITE_ROOT_RELPATH) / name
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        return path

    def assert_zero_posts(self, where: str) -> int:
        # require_exists=True：账本被删 ≠ 零 POST（独立评审 MAJOR：真空证据）。
        rows = OV._paid_rows(self.ctx, self.ledger_path, require_exists=True)
        if rows:
            raise OfflineSuiteError("NOT_ZERO_POST", f"{where}: 账本 {rows} 行实呼")
        return 0

    def resource_ledger(self) -> RS.ResourceLedger:
        return RS.ResourceLedger(self.ctx.root, profile=self.profile)

    def scratch_ledger(self, name: str) -> RS.ResourceLedger:
        return RS.ResourceLedger(self.suite_dir(name), profile=self.profile)

    def record(self, name: str, *, expected: str, observed: Mapping[str, Any],
               status: str) -> "dict[str, Any]":
        spec = OV.SUITE_SPEC.get(name) or {}
        doc = {"suite": name, "expected": expected,
               "registered_expected": spec.get("expected"),
               "observed": dict(observed), "status": status,
               "posts": 0}
        # 独立评审 R5-C1（verifier 实测）：observed 最终要经
        # ``report_document`` → ``write_json`` 序列化；旧实现把 bytes canary 原样
        # 放进来，六套跑满 40 分钟后才在写报告处 TypeError（整轮作废）。落到
        # ``record`` 当场 fail-closed，错误在套内暴露而不是在报告面。
        try:
            json.dumps(doc["observed"], sort_keys=True, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise OfflineSuiteError(
                "SUITE_OBSERVED_NOT_SERIALIZABLE",
                f"{name}: {type(exc).__name__}: {exc}") from None
        if spec and spec.get("expected") != expected:
            doc["expected_note"] = ("运行期期望与注册件不同：注册件为准，"
                                    "不得改注册件凑绿")
            doc["status"] = "FAIL"
        self.results[name] = doc
        self.progress.note(f"suite:{name}", status=doc["status"],
                           expected=expected)
        return doc


# ---------------------------------------------------------------------------
# suite2：超帽拒绝 → 同一 attempt 自修完成（probe 单链，cap 12）
# ---------------------------------------------------------------------------

def _rejected_batch_left_no_runs(ctx: Any, lane: str,
                                 actions: "list[Mapping[str, Any]]") -> bool:
    """超帽被拒的 run_batch 必须**零物理 run**，证据只取盘面。

    锁内适配（2026-11-12 T5 独立评审 MAJOR-1）：旧实现查
    ``attempts/<lane>/attempt-1/observations``（真实路径在 ``protocol/`` 下），
    且按文件名找 ``explore``（真实文件名为 ``<run_id>.jsonl``）；目录不存在即
    ``return True`` ⇒ 这条验收恒真空通过。改为两条真实证据：
    ① 被拒动作的 ``side_effect_status`` 必须 NONE（宿主前执行已落盘的事实）；
    ② ``protocol/<lane>/attempt-1/observations/explore-*/run-*.jsonl`` 的
       run id 集（stem 即完整 run id，形如 ``run-<20hex>``）必须**逐字等于**
       唯一成功探索批声明的 ``run_ids``——被拒的设计若在拒绝之前跑过物理 run，
       会另建 ``explore-<study>-<n>`` 批次目录，盘上就多出它那份 run 文件。
       超帽检查先于 ``backend_factory``，被拒设计连批次目录都不会建。
       （确认批 ``confirm-*`` 不在比较面：它由 ``run_confirm`` 产生且只回
       ``run_ids_head``，不属于「探索批零泄漏」这条验收。）
    """
    rejected = [a for a in actions
                if a.get("action") == "run_batch"
                and _err_code_from_result(a) == "RESOURCE_EXCEEDED"]
    accepted = [a for a in actions
                if a.get("action") == "run_batch" and a.get("ok") is True]
    if not rejected or len(accepted) != 1:
        return False
    for act in rejected:
        side = ((act.get("result") or {}).get("side_effect_status")
                or (act.get("recovery") or {}).get("side_effect_status"))
        if side != "NONE":
            return False
    base = ctx.relative(f"{PT.attempt_prefix(lane, 1)}/observations")
    on_disk = ({p.stem for p in base.glob("explore-*/*.jsonl")}
               if base.is_dir() else set())
    declared = {str(r) for r in
                ((accepted[0].get("result") or {}).get("run_ids") or [])}
    return bool(declared) and on_disk == declared


def suite2_over_cap_self_repair(env: SuiteEnv) -> "dict[str, Any]":
    name = "suite2_over_cap_self_repair"
    env.progress.note("suite2:start")
    ledger = env.resource_ledger()
    ledger.reserve_aux("A", envelope="offline_probe_practice_recovery",
                       steps=int(P.AUX_SUBENVELOPES[
                           "offline_probe_practice_recovery"]))
    scripted = OV.ScriptedTransport(decide=_recovery_decider(), log=env.turn_log)
    report = PT.run_protocol(
        env.ctx, lane="probe", spend=env.spend,
        transport_factory=lambda: scripted,
        runner=env.runner_factory(), image=env.image,
        batch_id="offline-suite2", counter=env.counter,
        profile=env.profile, resource_ledger=ledger)
    studies = report.get("studies") or [{}]
    actions = list(studies[0].get("actions") or [])
    codes = [_err_code_from_result(a) for a in actions if not a.get("ok")]
    decisions = len(actions)
    observed = {
        "lane": "probe", "verdict": report.get("verdict"),
        "decisions": decisions,
        "ok_count": sum(int(s.get("ok_count") or 0) for s in studies),
        "rejected_codes": [c for c in codes if c],
        "posts": report.get("posts"),
        "completed": studies[0].get("completed"),
        "stage": studies[0].get("stage"),
        "recovery_decisions": [a.get("recovery") for a in actions
                               if a.get("recovery")],
        "root_after": report.get("root_after"),
        "report_relpath": "protocol/report_probe.json",
    }
    reasons = []
    status = "PASS"
    if report.get("verdict") != "PASS":
        status = "FAIL"
        reasons.append("probe 链未 PASS")
    if "RESOURCE_EXCEEDED" not in codes:
        status = "FAIL"
        reasons.append("没有出现 RESOURCE_EXCEEDED 拒绝")
    zero_run_ok = _rejected_batch_left_no_runs(env.ctx, "probe", actions)
    observed["rejected_batch_no_runs"] = zero_run_ok
    observed["explore_run_ids"] = sorted(
        str(r) for a in actions if a.get("ok") is True
        and a.get("action") == "run_batch"
        for r in ((a.get("result") or {}).get("run_ids") or []))
    if not zero_run_ok:
        status = "FAIL"
        reasons.append("超帽被拒的批次在盘面留下了物理 run 记录（或被拒动作有副作用）")
    if not (8 < decisions <= 12):
        status = "FAIL"
        reasons.append(f"决策数 {decisions} 不在 (8,12]（cap 12）")
    observed["reasons"] = reasons
    return env.record(name, expected="PASS_WITH_RECOVERY", observed=observed,
                      status=status)


def _err_code_from_result(action: Mapping[str, Any]) -> str:
    """动作码在 ``action.code``（宿主面）与 ``action.result.code`` 两处都可能出现。"""
    return str(action.get("code")
               or (action.get("result") or {}).get("code") or "")


# ---------------------------------------------------------------------------
# suite1：practice 单链 + A/B 完整链 + 物理 replay + closeout
# ---------------------------------------------------------------------------

def _other_study_condition_canaries(study: str) -> "tuple[bytes, ...]":
    """另一 study 独有的条件 id（B 的 tc_only/dt_only 不得出现在 A 的请求里）。"""
    mine = {str(c["id"]) for c in OC.TOY_CONDITIONS[study]}
    other = {str(c["id"]) for k, v in OC.TOY_CONDITIONS.items() if k != study
             for c in v}
    return tuple(sorted(x.encode("utf-8") for x in (other - mine)))


def _canary_disclosure(info: Mapping[str, Any]) -> "dict[str, Any]":
    """canary 的**可序列化**披露面：可用性/数量/来源件/逐条 sha（不含原文）。

    独立评审 R5-C1（verifier 实测 drill21 会在写报告处 TypeError）：canary 字节
    只能留在运输层做匹配，进 ``observed`` 的必须先是纯 JSON 值——并且按
    ``CanarySet`` 的设计也不回显原文，只披露每条的 sha 前缀。
    """
    canaries = tuple(info.get("canaries") or ())
    return {"available": bool(info.get("available")),
            "n": int(info.get("n") or 0),
            "manifest": str(info.get("manifest") or ""),
            "canary_sha256_prefix": [hashlib.sha256(c).hexdigest()[:16]
                                     for c in canaries]}


def _run_id_canaries(env: SuiteEnv, study: str) -> "dict[str, Any]":
    """另一 study 已封存确认批的 run_id（消费者不得在任何请求里看到）。

    锁内适配（2026-11-12 T5 独立评审 MAJOR：「canary 恒空集」）。旧实现读
    ``manifest.get("runs"/"cells")``——确认分配件里根本没有这两个键（只有
    ``assignments``），于是 canary 永远为空、跨 study run_id 泄漏检查从未生效。
    这里按生产同一定义重算：``experiments.run_id_for(plan, study, condition,
    block)``。确认件存在但派生不出任何 run_id ⇒ 夹具态错误，直接 FAIL。

    独立评审 R4-M2（2026-11-13）：**时向不对称必须如实登记**。suite1 先跑 study A
    再跑 study B，因此只有 B 的运输能拿到 A 的 run_id 做**活体** canary
    （A 的确认件此时已在盘上）；A 的运输构造时 B 还没跑、B 的 run_id 尚不存在，
    该方向只能是空集。旧实现把空集静默当「已检查」，等于一句没有副作用的断言。
    现在返回 ``{"canaries": ..., "available": bool, "n": int, "manifest": rel}``，
    由 suite1 显式断言「A 完成后 B 的活体 canary 必须非空」，否则整条链 FAIL。
    """
    rel = f"confirm/manifest_{study}.json"
    if not env.ctx.exists(rel):
        return {"canaries": (), "available": False, "n": 0, "manifest": rel}
    doc = env.ctx.read_json(rel)
    plan_sha = str(doc.get("plan_sha256") or "")
    ids = set()
    for a in doc.get("assignments") or []:
        if not isinstance(a, Mapping):
            continue
        if not (a.get("condition_id") and a.get("block_id")):
            continue
        ids.add(E.run_id_for(plan_sha256=plan_sha,
                             study_id=str(doc.get("study_id") or study),
                             condition_id=str(a["condition_id"]),
                             block_id=str(a["block_id"])))
    if not ids:
        raise OfflineSuiteError(
            "CANARY_EMPTY", f"{rel} 存在但派生不出任何 run_id（夹具态错误）")
    return {"canaries": tuple(sorted(x.encode("utf-8") for x in ids)),
            "available": True, "n": len(ids), "manifest": rel}


def suite1_single_chains(env: SuiteEnv) -> "dict[str, Any]":
    name = "suite1_single_chains"
    env.progress.note("suite1:start")
    ledger = env.resource_ledger()
    ledger.reserve_aux("A", envelope="ab_instrument_confirm_replay",
                       steps=int(P.AUX_SUBENVELOPES["ab_instrument_confirm_replay"]))
    practice = PT.run_protocol(
        env.ctx, lane="practice", spend=env.spend,
        transport_factory=lambda: OV.OfflineConsumerTransport(
            # practice 一条链里先后做 A 与 B，两个 study 的条件都会出现，
            # 因此这里只留基础 canary（私有盘/账本路径），跨 study 语义在
            # study A/B 各自的两条链上分开检。
            consumer=OV.consumer_source(env.repo_root),
            log=env.turn_log_with(()),
            python=sys.executable),
        runner=env.runner_factory(), image=env.image,
        batch_id="offline-suite1", counter=env.counter,
        profile=env.profile, resource_ledger=ledger)
    env.progress.note("suite1:practice", verdict=practice.get("verdict"),
                      posts=practice.get("posts"))
    # R4-M2：A 跑之前 B 的确认件还不存在（时向使然，如实登记）。
    runid_for_a = _run_id_canaries(env, "B")
    study_a = STD.run_study(
        env.ctx, study="A", spend=env.spend,
        transport_factory=lambda: OV.OfflineConsumerTransport(
            consumer=OV.consumer_source(env.repo_root),
            log=env.turn_log_with(
                _other_study_condition_canaries("A")
                + runid_for_a["canaries"]),
            python=sys.executable),
        runner=env.runner_factory(), image=env.image,
        batch_id="offline-suite1", counter=env.counter,
        profile=env.profile, resource_ledger=env.resource_ledger())
    env.progress.note("suite1:studyA", status=study_a.get("status"),
                      state=study_a.get("study_state"))
    # A 完成后其确认件必须在盘上：B 的运输只有拿它才谈得上活体跨 study canary。
    runid_for_b = _run_id_canaries(env, "A")
    study_b = STD.run_study(
        env.ctx, study="B", spend=env.spend,
        transport_factory=lambda: OV.OfflineConsumerTransport(
            consumer=OV.consumer_source(env.repo_root),
            log=env.turn_log_with(
                _other_study_condition_canaries("B")
                + runid_for_b["canaries"]),
            python=sys.executable),
        runner=env.runner_factory(), image=env.image,
        batch_id="offline-suite1", counter=env.counter,
        profile=env.profile, resource_ledger=env.resource_ledger())
    env.progress.note("suite1:studyB", status=study_b.get("status"),
                      state=study_b.get("study_state"))
    replays: "dict[str, dict[str, Any]]" = {}
    for study in sorted(S.STUDIES):
        # 锁内适配（2026-11-12 T5 全量演练）：复跑批目录按 study 分隔。
        # 旧实现两个 study 共用 ``offline-suite1``，第一个复跑建好目录后第二个
        # 必撞 ``BATCH_EXISTS``（复跑必须新批次）；drill14 因 A 复跑先失败才没
        # 暴露，drill15 两个 study 都跑通到复跑才炸出来。
        replays[study] = RPD.run_replay(
            env.ctx, study=study, runner=env.runner_factory(),
            image=env.image, batch_id=f"offline-suite1-{study.lower()}",
            profile=env.profile)
        env.progress.note(f"suite1:replay{study}",
                          status=replays[study].get("status"),
                          verdict=replays[study].get("verdict"))
    # ---- 正式结案（closeout）---------------------------------------------
    # 锁内适配（2026-11-12 T5 全量演练）：先前的 suite1 直接调
    # ``CLOSURE.run_closure_judge``——那是 **R1/T9** 的独立复算器，内嵌 R1 字面量
    # 表（probe 帽 8、总帽 144）且要求 paid 账本，用在零 POST 的 R1.2 离线根上
    # 必报 ``B-DECLARED-CAPS``（12 vs 8）。R1.2 的**新版独立 checker 归 T9**
    # （Plan §6 T9「新版独立checker」），T5 只演练正式 closeout 路径本身：
    # 走 ``closeout.build``（``cli closeout`` 的实现），按 R1.2 事实核五轴、
    # 零 POST 与帽绑定，再加证据包逐件核对。
    image_pin = None
    if env.ctx.exists(LK.LOCK_RELPATH):
        image_pin = ((env.ctx.read_json(LK.LOCK_RELPATH) or {})
                     .get("image") or {}).get("pin")
    readout = CLOSEOUT.build(
        env.ctx, image_pin=image_pin,
        notes=["RSI-R1.2 T5 离线生产演练：零 POST，仪器 fixture，不作科学结论"])
    axes = dict(readout.get("axes") or {})
    closeout_ledger = dict(readout.get("ledger") or {})
    closeout_budget = dict(readout.get("budget") or {})
    expected_lane_caps = {lane: int(env.profile.lane_post_caps[lane])
                          for lane in env.profile.lane_order}
    closeout_checks = {
        "process_complete": axes.get("process") == "COMPLETE",
        "measurement_valid": axes.get("measurement") == "VALID",
        "studies_completed": axes.get("study_execution") == "COMPLETED",
        # 零 POST 是 T5 的定义性质：账本 0 行且 0 次 call。
        # 锁内适配（2026-11-12 T5 独立评审 MINOR-1）：两个键必须**存在**且
        # 为 0——旧实现 ``or 0`` 让「账本块缺键」也判真，零 POST 证据可无账本成立。
        "zero_posts": ("rows" in closeout_ledger and "calls" in closeout_ledger
                       and closeout_ledger["rows"] == 0
                       and closeout_ledger["calls"] == 0),
        # 结案件登记的帽必须与 R1.2 profile 逐值相等（12/16/48/48、总 124）
        "caps_bound": (int(closeout_budget.get("hard_cap") or -1)
                       == int(env.profile.hard_cap)
                       and dict(closeout_budget.get("lane_caps") or {})
                       == expected_lane_caps),
        # 缺键 ≠ 空列表（MINOR-1）：``process_reasons`` 必须是**存在且为空**。
        "no_process_reasons": ("process_reasons" in readout
                               and list(readout["process_reasons"]) == []),
    }
    env.progress.note("suite1:closeout", axes=axes,
                      checks=closeout_checks,
                      reasons=list(readout.get("process_reasons") or []))
    pack = CLOSURE.build_evidence_pack(env.ctx, repo_root=env.repo_root)
    verified = CLOSURE.verify_pack(env.ctx.relative(CLOSURE.PACK_DIRNAME))
    observed = {
        "probe": "由 suite2 的同一条 probe 单链覆盖（cap 12，含一次超帽拒绝自修）",
        "practice": {"verdict": practice.get("verdict"),
                     "posts": practice.get("posts")},
        "study_a": {"status": study_a.get("status"),
                    "state": study_a.get("study_state"),
                    "posts": study_a.get("posts"),
                    "judge": study_a.get("judge")},
        "study_b": {"status": study_b.get("status"),
                    "state": study_b.get("study_state"),
                    "posts": study_b.get("posts"),
                    "judge": study_b.get("judge")},
        "replays": {k: {"status": v.get("status"),
                        "comparison_ok": v.get("comparison_ok"),
                        "analysis_ok": v.get("analysis_ok"),
                        "problems": list(v.get("problems") or [])[:4]}
                    for k, v in replays.items()},
        "pack": {"n_files": pack.get("n_files"),
                 "total_bytes": pack.get("total_bytes"),
                 "verified": {"ok": verified.get("ok"),
                              "n_files": verified.get("n_files")}},
        "closeout": {"axes": axes,
                     "checks": closeout_checks,
                     "process_reasons": list(readout.get("process_reasons") or []),
                     "document_sha256": readout.get("document_sha256"),
                     "ledger": {"rows": closeout_ledger.get("rows"),
                                "calls": closeout_ledger.get("calls")},
                     "budget": {"hard_cap": closeout_budget.get("hard_cap"),
                                "lane_caps": closeout_budget.get("lane_caps")}},
        # R4-M2：跨 study run_id canary 的**时向**如实登记——B 的运输用的是 A 的
        # 活体 run_id（A 已跑完），A 的运输构造时 B 尚未运行（该方向只能是空集）。
        # R5-C1：只登记**可序列化**的披露面（可用性/数量/来源件/逐条 sha），
        # canary 原文既不可进报告也不可进 observed。
        "runid_canary": {
            "for_study_A": {"other": "B", **_canary_disclosure(runid_for_a)},
            "for_study_B": {"other": "A", **_canary_disclosure(runid_for_b)}},
        "root_after": S.ResearchState(env.ctx).root_state(),
    }
    reasons = []
    status = "PASS"
    if practice.get("verdict") != "PASS":
        status = "FAIL"
        reasons.append("practice 链未 PASS")
    if study_a.get("status") != "COMPLETED" or study_b.get("status") != "COMPLETED":
        status = "FAIL"
        reasons.append("A/B 研究链未 COMPLETED")
    # R4-M2：B 的运输必须真正带上 A 的封存 run_id（否则该方向是零副作用的
    # 「检查」）。A 已 COMPLETED 却取不到任何 run_id ⇒ 夹具/生产面有洞，判 FAIL。
    if study_a.get("status") == "COMPLETED" and not runid_for_b.get("available"):
        status = "FAIL"
        reasons.append(
            "study A 完成后确认分配件缺席/不可派生 ⇒ 跨 study run_id canary 未生效")
    for study, rep in replays.items():
        # 独立评审 MINOR：缺键曾被当成通过（``is False`` 对 None 不成立）。
        # 三个轴都要求**严格** True。
        if rep.get("status") != "COMPLETE" \
                or rep.get("comparison_ok") is not True \
                or rep.get("analysis_ok") is not True:
            status = "FAIL"
            reasons.append(
                f"{study} 物理复跑未 COMPLETE（status={rep.get('status')}）")
    if not verified.get("ok"):
        status = "FAIL"
        reasons.append(f"证据包自核对未过：{(verified.get('problems') or [])[:3]}")
    failed_checks = sorted(k for k, ok in closeout_checks.items() if not ok)
    if failed_checks:
        status = "FAIL"
        reasons.append(
            f"closeout 五轴/零POST/帽绑定未闭合：{failed_checks}"
            f"（process_reasons={(readout.get('process_reasons') or [])[:3]}）")
    observed["reasons"] = reasons
    return env.record(name, expected="PASS", observed=observed, status=status)


# ---------------------------------------------------------------------------
# suite3：边界矩阵（生产校验面；不动研究状态、不跑容器）
# ---------------------------------------------------------------------------

def _plan_and_budget(env: SuiteEnv) -> "tuple[C.ResearchPlan, C.BudgetSpec]":
    plan = C.ResearchPlan.from_dict(PT.toy_plan_doc("A"))
    budget = C.budget_from_dict(env.ctx.read_json("contract/budget.json"))
    return plan, budget


def _matrix(plan: C.ResearchPlan, cells: "list[dict[str, Any]]") -> "dict[str, Any]":
    return {"schema": "simpraxis.rsi_r1.matrix.v1", "schema_version": 1,
            "study_id": plan.study_id, "plan_sha": plan.sha256, "cells": cells}


def _cells(plan: C.ResearchPlan, n: int) -> "list[dict[str, Any]]":
    conds = list(plan.conditions)
    return [{"condition_id": conds[i % len(conds)].id,
             "block_id": "b%03d" % (i + 1),
             "params": dict(conds[i % len(conds)].params)}
            for i in range(n)]


def suite3_boundaries(env: SuiteEnv) -> "dict[str, Any]":
    from orchestrator.rsi_r1 import experiments as E

    name = "suite3_boundaries"
    env.progress.note("suite3:start")
    plan, budget = _plan_and_budget(env)
    cap = min(E.LANES.MAX_EXPLORE_RUNS_PER_STUDY,
              int(budget.max_runs_by_lane["explore"]))
    checks: "dict[str, str]" = {}

    def code_of(fn: Callable[[], Any]) -> str:
        try:
            fn()
            return "OK"
        except Exception as exc:  # noqa: BLE001 —— 逐条记录生产拒绝码
            return _err_code(exc)

    def v2(n_cells: int) -> Callable[[], Any]:
        ledger = env.scratch_ledger("suite3")
        return lambda: RS.validate_matrix_v2(
            _matrix(plan, _cells(plan, n_cells)), plan, ledger=ledger,
            study="A", batch_id="offline-suite3")

    checks["cells_63"] = code_of(lambda: E.validate_matrix(
        _matrix(plan, _cells(plan, 63)), plan, budget))
    checks["cells_64"] = code_of(lambda: E.validate_matrix(
        _matrix(plan, _cells(plan, 64)), plan, budget))
    checks["cells_65"] = code_of(lambda: E.validate_matrix(
        _matrix(plan, _cells(plan, 65)), plan, budget))
    checks["cells_96"] = code_of(lambda: E.validate_matrix(
        _matrix(plan, _cells(plan, 96)), plan, budget))
    checks["unknown_condition"] = code_of(lambda: E.validate_matrix(
        _matrix(plan, [{"condition_id": "nope", "block_id": "b001",
                        "params": dict(plan.conditions[0].params)}]),
        plan, budget))
    dup = _cells(plan, 2)
    dup.append(dict(dup[0]))
    checks["duplicate_cell"] = code_of(
        lambda: E.validate_matrix(_matrix(plan, dup), plan, budget))
    checks["plan_drift"] = code_of(lambda: E.validate_matrix(
        {**_matrix(plan, _cells(plan, 2)), "plan_sha": "0" * 64}, plan, budget))
    checks["ledger_cells_65"] = code_of(v2(65))
    checks["ledger_cells_64"] = code_of(v2(64))
    checks.update(_ledger_shape_checks(env))
    checks["batch_limit_8"] = _batch_limit_check(env)
    # Plan L145 固定套 #3 剩余两行（独立评审 MAJOR-7 判缺）：
    # ① 累计 40+25 > 64 必须拒绝（每批各自合法、合计越帽）；
    # ② 确认 POST 余量门：扣当次 POST 后余 4 < 8 必须 ``BUDGET_UNREACHABLE``，
    #    余 8 恰好放行（同一 ledger、同一参数，只改余量，证明门真的在算）。
    checks["cumulative_65"] = _cumulative_check(env)
    checks["confirm_post_margin_refused"], checks["confirm_post_margin_ok"] = (
        _confirm_margin_checks(env))
    expected = {
        "cells_63": "OK", "cells_64": "OK", "cells_65": "RESOURCE_EXCEEDED",
        "cells_96": "RESOURCE_EXCEEDED",
        "unknown_condition": "CONDITION_UNKNOWN",
        "duplicate_cell": "DUPLICATE_ID", "plan_drift": "PLAN_MISMATCH",
        "ledger_cells_64": "OK",
        "ledger_cells_65": "RESOURCE_EXCEEDED",
        # 独立评审 MAJOR：拒绝必须绑**具体生产码**，不得用「任何异常」冒充。
        "sandbox_32_ok": "OK", "sandbox_33rd": "RESOURCE_EXCEEDED",
        "confirm_32_ok": "OK", "confirm_33_blocks": "CONFIRM_BLOCK_COUNT",
        "batch_limit_8": "RESOURCE_EXCEEDED",
        "cumulative_65": "RESOURCE_EXCEEDED",
        "confirm_post_margin_refused": "BUDGET_UNREACHABLE",
        "confirm_post_margin_ok": "OK",
    }
    mismatched = {k: {"want": v, "got": checks.get(k)}
                  for k, v in expected.items() if checks.get(k) != v}
    status = "PASS" if not mismatched else "FAIL"
    return env.record(name, expected="BOUNDARY_MATRIX_PASS",
                      observed={"cap_explore": cap, "checks": checks,
                                "mismatched": mismatched}, status=status)


def _ledger_shape_checks(env: SuiteEnv) -> "dict[str, str]":
    """沙盒/确认形状门：**前置与测量分开**，并记录生产拒绝码本身。

    独立评审 MAJOR：旧实现把「32 次合法预留」和「第 33 次必须失败」放进同一个
    ``try``——只要任何一次合法预留先炸，整段就被记成 ``REFUSED`` 而「通过」，
    测的根本不是第 33 次。现在前置失败直接抛出（演练硬失败），被测量单独判码。
    """
    out: "dict[str, str]" = {}
    ledger = env.scratch_ledger("suite3-sandbox")
    for _ in range(32):
        ledger.reserve_sandbox("A", purpose="explore-design")
    out["sandbox_32_ok"] = "OK"
    out["sandbox_33rd"] = _refusal_code(
        lambda: ledger.reserve_sandbox("A", purpose="explore-design"))
    shape = env.scratch_ledger("suite3-confirm")
    shape.check_confirm_shape(blocks=32, n_conditions=3)
    out["confirm_32_ok"] = "OK"
    out["confirm_33_blocks"] = _refusal_code(
        lambda: shape.check_confirm_shape(blocks=33, n_conditions=3))
    return out


def _batch_limit_check(env: SuiteEnv) -> str:
    from orchestrator.rsi_r1 import experiments as E

    plan, budget = _plan_and_budget(env)
    ledger = env.scratch_ledger("suite3-batch")
    specs = E.validate_matrix(_matrix(plan, _cells(plan, 1)), plan, budget)
    for i in range(8):                       # 前置：8 个合法批（失败即硬炸）
        ledger.reserve_explore("A", specs, batch_id=f"b{i + 1}")
    return _refusal_code(
        lambda: ledger.reserve_explore("A", specs, batch_id="b9"))


def _cumulative_check(env: SuiteEnv) -> str:
    """累计 40+25=65 > 每 study 64 run：两批各自合法，第二批必须拒绝。

    Plan L145「累计40+25超64」的离线同构：不依赖任何科学结果，只验证配额
    账在**跨批累计**上 fail-closed（旧实现只测单批 65，累计面未证明）。
    """
    from orchestrator.rsi_r1 import experiments as E

    plan, budget = _plan_and_budget(env)
    ledger = env.scratch_ledger("suite3-cumulative")
    first = E.validate_matrix(_matrix(plan, _cells(plan, 40)), plan, budget)
    second = E.validate_matrix(_matrix(plan, _cells(plan, 25)), plan, budget)
    ledger.reserve_explore("A", first, batch_id="c1")   # 前置失败即硬炸
    return _refusal_code(
        lambda: ledger.reserve_explore("A", second, batch_id="c2"))


def _confirm_margin_checks(env: SuiteEnv) -> "tuple[str, str]":
    """确认 POST 余量门：余 4 拒绝（BUDGET_UNREACHABLE）/ 余 8 放行。"""
    def attempt(remaining: int) -> str:
        ledger = env.scratch_ledger(f"suite3-margin-{remaining}")

        def call() -> Any:
            return ledger.reserve_freeze(
                "A", lane="study_A", blocks=32, n_conditions=3,
                replay_runs=192,
                confirm_steps=96 * 8000, replay_steps=192 * 8000,
                posts_remaining_after_charge=remaining,
                purpose="offline-suite3")

        return _refusal_code(call)

    return attempt(4), attempt(8)


# ---------------------------------------------------------------------------
# suite4：transport 故障（每条都要有报告、不得双重执行）
# ---------------------------------------------------------------------------

def _run_consumer(env: SuiteEnv, path: Path) -> str:
    transport = OV.OfflineConsumerTransport(consumer=path, log=env.turn_log)
    request = T.build_request(system="offline suite4", task_brief="ping",
                              turns=[], counter=env.counter)
    try:
        transport(request)
        return "NO_ERROR"
    except Exception as exc:  # noqa: BLE001
        return _err_code(exc)


def _temp_consumer(env: SuiteEnv, filename: str, body: str) -> Path:
    path = env.suite_dir("suite4") / filename
    path.write_text(body, encoding="utf-8")
    return path


def suite4_transport_faults(env: SuiteEnv) -> "dict[str, Any]":
    name = "suite4_transport_faults"
    env.progress.note("suite4:start")
    checks: "dict[str, str]" = {}
    checks["consumer_crash"] = _run_consumer(env, _temp_consumer(
        env, "crash.py",
        "import sys\nsys.stderr.write('boom')\nraise SystemExit(3)\n"))
    checks["consumer_kill"] = _run_consumer(env, _temp_consumer(
        env, "kill.py",
        "import os, signal\nos.kill(os.getpid(), signal.SIGKILL)\n"))
    checks["unknown_action"] = _run_consumer(env, _temp_consumer(
        env, "unknown.py",
        "import json, sys\nsys.stdin.read()\n"
        "sys.stdout.write(json.dumps({'action': 'fly_to_moon'}) + '\\n')\n"))
    checks["bad_json"] = _run_consumer(env, _temp_consumer(
        env, "badjson.py",
        "import sys\nsys.stdin.read()\nsys.stdout.write('not json\\n')\n"))
    # Plan T5 固定套 #4 的「空回复」行：rc=0 但 stdout 为空（供应商 200 空答的
    # 离线同构），必须与坏 JSON 同判 CONSUMER_REPLY 且留证，不得当成正常回合。
    checks["empty_reply"] = _run_consumer(env, _temp_consumer(
        env, "empty.py",
        "import sys\nsys.stdin.read()\nsys.stdout.write('')\n"))
    checks["timeout"] = _timeout_check(env)
    checks["action_unknown_in_parser"] = _parser_check()
    expected = {"consumer_crash": "CONSUMER_EXIT", "consumer_kill": "CONSUMER_EXIT",
                "unknown_action": "CONSUMER_ACTION", "bad_json": "CONSUMER_REPLY",
                "empty_reply": "CONSUMER_REPLY",
                "timeout": "CONSUMER_TIMEOUT",
                "action_unknown_in_parser": "UNKNOWN_ACTION"}
    mismatched = {k: {"want": v, "got": checks.get(k)}
                  for k, v in expected.items() if checks.get(k) != v}
    turns = [e for e in env.turn_log.entries if e.get("note") != "ok"]
    status = "PASS" if not mismatched else "FAIL"
    return env.record(name, expected="EACH_HAS_REPORT_NO_DOUBLE_EXEC",
                      observed={"checks": checks, "mismatched": mismatched,
                                "failure_turns_logged": len(turns),
                                # Plan T5 固定套 #4 的其余行在 T3/T2 回归面已覆盖，
                                # 这里如实登记归属，不当成六套遗漏。归属**从
                                # FAILURE_MATRIX 派生**（独立评审 LOW-1：旧实现手写
                                # 一份，把 SIGTERM 行指到 attempts.py，与矩阵已修正
                                # 的 lifecycle 归属互相矛盾；叙述字段也不许两套真值）。
                                "covered_by_regressions": regression_rows()},
                      status=status)


def _timeout_check(env: SuiteEnv) -> str:
    path = _temp_consumer(env, "sleepy.py",
                          "import sys, time\nsys.stdin.read()\ntime.sleep(5)\n")
    transport = OV.OfflineConsumerTransport(consumer=path, log=env.turn_log,
                                            timeout_s=0.5)
    request = T.build_request(system="offline suite4", task_brief="ping",
                              turns=[], counter=env.counter)
    try:
        transport(request)
        return "NO_ERROR"
    except Exception as exc:  # noqa: BLE001
        return _err_code(exc)


def _parser_check() -> str:
    try:
        L.parse_action(json.dumps({"action": "fly_to_moon"}))
        return "OK"
    except Exception as exc:  # noqa: BLE001
        return _err_code(exc)


# ---------------------------------------------------------------------------
# suite5：状态隔离（跨 study、未分析提交、完成后重复 submit、旧心跳）
# ---------------------------------------------------------------------------

def suite5_state_isolation(env: SuiteEnv) -> "dict[str, Any]":
    name = "suite5_state_isolation"
    env.progress.note("suite5:start")
    scope = PT.ScopedContext(env.ctx, f"{SUITE_ROOT_RELPATH}/suite5")
    journal = PT.mirror_root(scope)
    state = S.ResearchState(scope, journal=journal)
    checks: "dict[str, Any]" = {}
    # 候选取材走真实绑定链：reply → action → artifact（不得跳过 UNBOUND_SOURCE）。
    state.record_reply(response_id="offline-suite5", text='{"action":"run_batch"}',
                       phase="explore", study="A")
    acted = state.record_action(response_id="offline-suite5",
                                action={"action": "run_batch"}, study="A")
    state.register_artifact(study="A", kind="design", source="print('A')",
                            response_id="offline-suite5",
                            action_sha256=acted["action_sha256"])
    # 独立评审 MAJOR：旧检查只看「B 的产物列表为空」——那是真空证据（B 从没
    # 登记任何东西，空是必然的）。改成**正反两向**：两个 study 各登记一件，
    # 再证明各自可见列表恰是本 study 的 sha（A 的 sha 不在 B，反之亦然）。
    state.record_reply(response_id="offline-suite5-b",
                       text='{"action":"run_batch"}', phase="explore", study="B")
    acted_b = state.record_action(response_id="offline-suite5-b",
                                  action={"action": "run_batch"}, study="B")
    b_art = state.register_artifact(study="B", kind="design",
                                    source="print('B')",
                                    response_id="offline-suite5-b",
                                    action_sha256=acted_b["action_sha256"])
    a_rec = state.register_artifact(study="A", kind="analysis",
                                    source="print('A2')",
                                    response_id="offline-suite5",
                                    action_sha256=acted["action_sha256"])
    a_design = list(state.study_status("A")["artifacts"]["design"])
    b_design = list(state.study_status("B")["artifacts"]["design"])
    checks["cross_study_hidden"] = (
        len(a_design) == 1 and len(b_design) == 1
        and a_design[0] != b_design[0]
        and b_design[0] == b_art.source_sha256
        and a_rec.source_sha256 not in b_design)
    # 反向负控：把 A 的回复/动作绑到 B 名下登记产物必须拒（跨 study 盗用）。
    checks["cross_study_binding_refused"] = _refusal_code(
        lambda: state.register_artifact(study="B", kind="design",
                                        source="print('B-steal')",
                                        response_id="offline-suite5",
                                        action_sha256=acted["action_sha256"]))
    # 独立评审 R3-m1：拒绝面必须绑**具体生产码**且回复必须是已登记的（否则任何
    # 状态下都先撞 UNKNOWN_REPLY，检查对目标闸不敏感）。
    checks["claim_before_release_refused"] = _refusal_code(
        lambda: state.submit_claim(study="A", claims=[], report_text="x",
                                   response_id="offline-suite5"))
    # R3-m1 / R4-m1：拒绝面绑具体生产码（``CONCLUSION_STATE``），不是「抛了就算」。
    checks["conclude_before_claim_refused"] = (
        _refusal_code(lambda: state.conclude("A", "VERIFIED",
                                             reason="offline-suite5"))
        == "CONCLUSION_STATE")
    # 独立评审 R4-m1：旧检查只要求「拒绝」——任何异常都算，且依赖主根上 suite1
    # 已把 A 结案（standalone 跑 suite5 时不成立）。改为在本 scope 内**自己构造**
    # 结案终态（见下方 claim 夹具之后的 conclude），再证明提交被 CLAIM_STATE 拒。
    # 独立评审 MAJOR：`pid_alive(2**31-1)` 只证明「一个从未存在的 pid 不活」。
    # 改成真进程：拉起子进程 → 确认活着 → kill -9 → wait 回收 → 确认已死。
    child = subprocess.Popen([sys.executable, "-I", "-c",
                              "import time; time.sleep(60)"])
    try:
        alive_before = W.pid_alive(child.pid) is True
        child.kill()
        child.wait(timeout=10)
        dead_after = W.pid_alive(child.pid) is False
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)
    checks["dead_pid_not_alive"] = bool(alive_before and dead_after)
    # 独立评审 MAJOR：旧检查只读暂停件「非 None」，没有证明它**拦住 dispatch**。
    # 这里直接对生产准入 `CampaignAdmission.check_all` 做行为断言：暂停件在 ⇒
    # 首个判据即 PAUSED；无暂停件的对照根 ⇒ 同一调用落到别的判据（LOCK_MISSING）
    # 而不是 PAUSED——两向合起来才证明「PAUSED 来自暂停件本身」。
    admitted_scope = PT.ScopedContext(env.ctx, f"{SUITE_ROOT_RELPATH}/suite5-admit")
    pause_state = admitted_scope.subdir("state")
    pause_state.mkdir(parents=True, exist_ok=True, mode=0o700)
    W.pause_dispatch(pause_state, reason="offline suite5 pause",
                     source="offline-suite5")
    budget_spec = C.budget_from_dict(env.ctx.read_json("contract/budget.json"))
    admission = A.CampaignAdmission(ctx=admitted_scope,
                                    state=S.ResearchState(admitted_scope),
                                    budget=budget_spec, lock_sha256="0" * 64,
                                    spend=None, lane="probe")
    checks["pause_blocks_dispatch"] = _refusal_code(
        lambda: admission.check_all(envelope={"envelope_id": scope.envelope_id},
                                    root_lock=None, phase="probe",
                                    spend_contract=None))
    control_scope = PT.ScopedContext(env.ctx,
                                     f"{SUITE_ROOT_RELPATH}/suite5-nopause")
    control_scope.subdir("state").mkdir(parents=True, exist_ok=True, mode=0o700)
    control = A.CampaignAdmission(ctx=control_scope,
                                  state=S.ResearchState(control_scope),
                                  budget=budget_spec, lock_sha256="0" * 64,
                                  spend=None, lane="probe")
    checks["pause_gate_control"] = _refusal_code(
        lambda: control.check_all(envelope={"envelope_id": scope.envelope_id},
                                  root_lock=None, phase="probe",
                                  spend_contract=None))
    W.resume_dispatch(pause_state, actor="offline-suite5",
                      reason="suite5 暂停件已取证，恢复沙盒")
    checks["pause_cleared"] = W.read_pause(pause_state) is None
    # Plan L146 固定套 #5 的剩余行（独立评审 MAJOR-7 判缺）：
    # ③ 旧 heartbeat 不得给新完成件作证；④ 正常退出（无握手）不得判 COMPLETED；
    # ⑤ 重复 submit 必须拒（状态机已在 CLAIM_SUBMITTED）；⑥ 未绑定/未分析的
    #    reply 提交必须拒（UNKNOWN_REPLY，提交面不得凭空造源）。
    from orchestrator.rsi_r1 import attempts as ATT

    empty_dir = env.suite_dir("suite5-nohandshake")
    empty_dir.mkdir(parents=True, exist_ok=True)
    checks["normal_exit_not_completed"] = (
        W.validate_completion(empty_dir, None)[0] is False)
    stale_dir = env.suite_dir("suite5-stale")
    stale_dir.mkdir(parents=True, exist_ok=True)
    (stale_dir / W.STATE_COMPLETED).write_text(json.dumps({
        "schema": ATT.COMPLETION_SCHEMA, "envelope_id": "offline-suite5",
        "lane": "probe", "attempt_id": "att-new", "pid": 1,
        "completed_at_utc": "2026-11-12T00:00:00Z",
        "report_relpath": "attempts/probe/att-new/report.json",
        "report_sha256": "0" * 64}), encoding="utf-8")
    checks["stale_heartbeat_not_completion"] = (
        W.validate_completion(stale_dir, {
            "attempt_id": "att-old", "lane": "probe",
            "envelope_id": "offline-suite5", "pid": 1})[0] is False)
    # 提交面夹具：把 A 沿合法相邻跳推到 RESULTS_RELEASED（``_advance_study`` 只
    # 接受邻接跳；这里构造前置状态，被测的是 ``submit_claim`` 的公开门）。
    order = list(S.STUDY_ORDER)
    target = order.index(S.ST_RESULTS_RELEASED)
    for nxt in order[order.index(state.study_state("A")) + 1: target + 1]:
        state._advance_study("A", nxt, reason="suite5 夹具：构造可提交状态")
    checks["claim_unbound_source_refused"] = _refusal_code(
        lambda: state.submit_claim(study="A", claims=[], report_text="x",
                                   response_id="never-seen"))
    claimed = state.submit_claim(study="A", claims=[],
                                 report_text="offline suite5 claim",
                                 response_id="offline-suite5")
    checks["claim_recorded"] = bool(claimed.get("report_artifact_id"))
    checks["duplicate_submit_refused"] = _refusal_code(
        lambda: state.submit_claim(study="A", claims=[],
                                   report_text="offline suite5 claim",
                                   response_id="offline-suite5"))
    # R4-m1：claim 已提 ⇒ 可给终局；结案后再提交必须是 CLAIM_STATE 拒（自足，
    # 不依赖 suite1 的结案顺序）。
    state.conclude("A", "VERIFIED", reason="offline-suite5 结案后再提交")
    checks["claim_after_close_refused"] = (
        state.study_state("A") in S.STUDY_TERMINAL
        and _refusal_code(
            lambda: state.submit_claim(study="A", claims=[], report_text="x",
                                       response_id="offline-suite5"))
        == "CLAIM_STATE")
    expected = {"cross_study_hidden": True,
                "cross_study_binding_refused": "STUDY_MISMATCH",
                "claim_before_release_refused": "CLAIM_STATE",
                "conclude_before_claim_refused": True,
                "claim_after_close_refused": True,
                "dead_pid_not_alive": True,
                "pause_blocks_dispatch": "PAUSED",
                "pause_gate_control": "LOCK_MISSING",
                "pause_cleared": True,
                "normal_exit_not_completed": True,
                "stale_heartbeat_not_completion": True,
                "claim_unbound_source_refused": "UNKNOWN_REPLY",
                "claim_recorded": True,
                "duplicate_submit_refused": "CLAIM_STATE"}
    mismatched = {k: {"want": v, "got": checks.get(k)}
                  for k, v in expected.items() if checks.get(k) != v}
    status = "PASS" if not mismatched else "FAIL"
    return env.record(name, expected="REJECT_OR_ISOLATE",
                      observed={"checks": checks, "mismatched": mismatched,
                                "root_after": state.root_state()},
                      status=status)


def _refused(fn: Callable[[], Any]) -> bool:
    try:
        fn()
        return False
    except Exception:  # noqa: BLE001
        return True


def _refusal_code(fn: Callable[[], Any]) -> str:
    """执行一次并返回生产拒绝码（成功返回 ``OK``）；只用于夹具判定。"""
    try:
        fn()
        return "OK"
    except Exception as exc:  # noqa: BLE001
        return _err_code(exc)


# ---------------------------------------------------------------------------
# suite6：人工事件与包对账
# ---------------------------------------------------------------------------

def suite6_human_and_pack(env: SuiteEnv) -> "dict[str, Any]":
    name = "suite6_human_and_pack"
    env.progress.note("suite6:start")
    # 同 root 内重跑（``--only`` 调试/复验）幂等：事件表是 suite 私有草稿，上一轮
    # 留下的未闭事件会让「结束后放行」永远失败，测的就不是本轮语义了。
    scratch = env.ctx.relative(f"{SUITE_ROOT_RELPATH}/suite6")
    if scratch.exists():
        shutil.rmtree(scratch)
    scope = PT.ScopedContext(env.ctx, f"{SUITE_ROOT_RELPATH}/suite6")

    class _Tick:
        """确定性时钟：每调一次 +60s（让「有时长」断言不被秒级同刻抹平）。"""

        def __init__(self) -> None:
            self.t = 0

        def __call__(self) -> str:
            self.t += 60
            return (f"2026-11-12T{(self.t // 3600) % 24:02d}:"
                    f"{(self.t // 60) % 60:02d}:{self.t % 60:02d}Z")

    log = HE.load(scope, clock=_Tick())
    closed = log.begin(kind="approval", actor="offline-suite6",
                       reason="suite6 结束事件", study="A")
    closed = log.end(event_id=closed.event_id)
    pending = log.begin(kind="maintenance", actor="offline-suite6",
                        reason="suite6 未结束事件", study="B")
    review = log.begin(kind="scientific_review", actor="offline-suite6",
                       reason="suite6 关键解释接触", study="A",
                       touch="interpretation")
    state = S.ResearchState(scope)
    assisted = log.apply_assisted_rule(state, review.event_id)
    summary = log.summary()
    checks: "dict[str, Any]" = {
        "closed_has_duration": closed.duration_s() is not None,
        "open_is_unknown": pending.duration_s() is None,
        "unknown_preserved": pending.event_id in summary["unknown_event_ids"],
        "unknown_not_zero": summary["complete"] is False,
        # MINOR-2：``>= 0.0`` 恒真（任何缺省都被读成「有时长」）。闭事件在
        # 确定性时钟下恰跨 60s，故这里要求**严格 > 0**，时长算术才被证明。
        "known_seconds_sum": summary["known_seconds_total"] > 0.0,
        "assisted_applied": assisted is True,
        "assisted_state": state.study_state("A") == S.ST_ASSISTED,
        # 运维事件（maintenance）无 touch，一律不把 study 推进到 ASSISTED。
        "maintenance_not_assisted": (
            log.apply_assisted_rule(state, pending.event_id) is False
            and state.study_state("B") != S.ST_ASSISTED),
    }
    # Plan L146「HumanEvent 开始/结束/**中断**」（独立评审 MAJOR-7 判缺）：未闭
    # 事件必须**中断**结案面（closeout 前断言 fail-closed），结束后同一断言放行。
    # 旧实现只测「未闭 ⇒ summary.complete=false」，结案闸本身没被证明。
    checks["interrupt_blocks_closeout"] = _refused(log.assert_complete_for_closeout)
    log.end(event_id=pending.event_id)
    # 本轮还留着未结束的 review 事件（它只用于 assisted 规则）——把**所有**未闭
    # 事件都结束掉，才轮到「结束后放行」这一半断言（否则测的是 review 未闭）。
    log.end(event_id=review.event_id)
    checks["closeout_after_end_ok"] = not _refused(log.assert_complete_for_closeout)
    # 包对账：**用正式写包器现包现核**，不要求 root 里预先躺着 evidence_pack/。
    #
    # 锁内适配（Plan §4 suite6「人工事件与包对账」）：正式包默认前缀含
    # ``protocol/``，而离线演练的 probe 线程 turn 日志原稿可达数百 MB；把它们
    # 复制进 suite 暂存区只增加 I/O，不增加任何对账信号。所以这里只用**去掉
    # protocol 的其余正式前缀**建包（其余前缀一字不改），并把实际前缀写进
    # observed 供复核。包落在 ``private/`` 下（非进包前缀），避免与将来正式
    # ``evidence_pack/`` 的同名非空拒绝相撞。
    pack_root = env.ctx.relative("private/offline_validation/suite6-pack")
    if pack_root.exists():
        shutil.rmtree(pack_root)     # 同 root 内 --only 重跑幂等
    pack_dir = pack_root / CLOSURE.PACK_DIRNAME
    manifest = CLOSURE.build_evidence_pack(
        env.ctx, dest=pack_dir, prefixes=_SUITE_PACK_PREFIXES,
        repo_root=env.repo_root)
    verified = CLOSURE.verify_pack(pack_dir)
    n_files = int(verified.get("n_files") or 0)
    checks["pack_verified"] = bool(verified.get("ok"))
    checks["pack_files"] = n_files
    checks["pack_matches_manifest"] = (
        n_files == int(manifest.get("n_files") or -1) == len(manifest["files"]))
    # 独立评审 MAJOR：上面三项都是拿 manifest 跟 manifest 自己比。这里**自己**
    # 从源盘枚举前缀内文件并逐件重算 sha，与包内实际字节对账（不经过
    # ``verify_pack``），checker 附加件按前缀名单列，不冒充源件。
    import hashlib as _hashlib

    root_dir = env.ctx.root
    source_sha: "dict[str, str]" = {}
    for prefix in _SUITE_PACK_PREFIXES:
        base = root_dir / prefix
        if not base.is_dir() or base.is_symlink():
            continue
        for p in sorted(base.rglob("*")):
            if p.is_file() and not p.is_symlink():
                source_sha[p.relative_to(root_dir).as_posix()] = \
                    _hashlib.sha256(p.read_bytes()).hexdigest()
    extras = {rel: sha for rel, sha in manifest["files"].items()
              if rel.startswith("checker/")}
    pack_sha = {}
    for rel in manifest["files"]:
        path = pack_dir / rel
        pack_sha[rel] = (_hashlib.sha256(path.read_bytes()).hexdigest()
                         if path.is_file() and not path.is_symlink() else None)
    checks["pack_source_reconciled"] = bool(
        set(pack_sha) == set(manifest["files"])
        and set(source_sha) == set(manifest["files"]) - set(extras)
        and all(pack_sha[rel] == sha == manifest["files"][rel]
                for rel, sha in source_sha.items())
        and all(isinstance(sha, str) and len(sha) == 64
                for sha in extras.values()))
    # 反空虚负控：篡改包内一字节，核对器必须转红；还原后必须重新转绿。
    # 没有这一步，``pack_verified`` 只能证明「文件都在」，不能证明 sha 面真的
    # 在算。
    victim = next((rel for rel in sorted(manifest["files"])
                   if (pack_dir / rel).stat().st_size > 0), "")
    if victim:
        blob = (pack_dir / victim).read_bytes()
        (pack_dir / victim).write_bytes(blob[:-1] + bytes([blob[-1] ^ 0x01]))
        tampered = CLOSURE.verify_pack(pack_dir)
        checks["pack_detects_tamper"] = (not tampered.get("ok")) and any(
            str(p).startswith(victim + ":sha") for p in tampered.get("problems") or [])
        (pack_dir / victim).write_bytes(blob)
        checks["pack_restored_ok"] = bool(CLOSURE.verify_pack(pack_dir).get("ok"))
    else:
        checks["pack_detects_tamper"] = False
        checks["pack_restored_ok"] = False
    expected = {"closed_has_duration": True, "open_is_unknown": True,
                "unknown_preserved": True, "unknown_not_zero": True,
                "known_seconds_sum": True, "assisted_applied": True,
                "assisted_state": True, "maintenance_not_assisted": True,
                "interrupt_blocks_closeout": True,
                "closeout_after_end_ok": True,
                "pack_verified": True, "pack_matches_manifest": True,
                "pack_source_reconciled": True,
                "pack_detects_tamper": True, "pack_restored_ok": True}
    mismatched = {k: {"want": v, "got": checks.get(k)}
                  for k, v in expected.items() if checks.get(k) != v}
    status = "PASS" if not mismatched else "FAIL"
    return env.record(name, expected="UNKNOWN_PRESERVED_AND_RECONCILED",
                      observed={"checks": checks, "summary": summary,
                                "pack_prefixes": list(_SUITE_PACK_PREFIXES),
                                "mismatched": mismatched}, status=status)


# ---------------------------------------------------------------------------
# 总驱动
# ---------------------------------------------------------------------------

def validation_verdict(results: Mapping[str, Any]) -> "dict[str, Any]":
    """由已跑套件结果判定完整性与 verdict（纯函数：CLI、机判与回归共用）。

    六套是验收线：缺一套只能是 INCOMPLETE（**不可签署**），任一 FAIL 即 FAIL。
    锁内适配（2026-11-12 T5 独立评审 MAJOR-2）：旧实现把 ``--only`` 子集算出的
    PASS 当可签署结论，等于没跑全链也能结案。
    """
    required = list(SUITE_ORDER)
    # CR-29（2026-11-14 评审）：完整性按**集合**判断。旧实现比较
    # ``list(results) == required``，用 ``--only`` 以另一种顺序跑完六套会被判
    # INCOMPLETE（一次完整运行变得不可签署）。显式计数防重复。
    complete = (set(results) == set(required) and len(results) == len(required))
    if any(v.get("status") != "PASS" for v in results.values()):
        verdict = "FAIL"
    elif complete:
        verdict = "PASS"
    else:
        verdict = "INCOMPLETE"
    return {"verdict": verdict, "complete": complete,
            "suites_required": required, "suites_run": list(results),
            "missing_suites": [s for s in required if s not in results]}


#: Plan L142–L146 固定失败矩阵的**逐行**覆盖登记（独立评审 MAJOR-7：旧实现缺
#: 13 行且报告里没有任何「哪一行由谁证明」的登记面）。每行 = 触发场景 →
#: ``("suite", observed 点路径, 期望值)``（本演练当场证明），或
#: ``("regression", 测试文件, 场景)``（由两排除全套冷跑证明，报告如实标
#: ``covered_by=regression:*``，不当成六套已覆盖）。
FAILURE_MATRIX: "tuple[tuple[str, tuple[str, ...]], ...]" = (
    # #1 单链 / 完整链 / 复跑 / 结案（probe 见 #2）
    ("practice_single_chain", ("suite1_single_chains", "practice.verdict", "PASS")),
    ("study_a_chain", ("suite1_single_chains", "study_a.status", "COMPLETED")),
    ("study_b_chain", ("suite1_single_chains", "study_b.status", "COMPLETED")),
    ("replay_a", ("suite1_single_chains", "replays.A.status", "COMPLETE")),
    ("replay_b", ("suite1_single_chains", "replays.B.status", "COMPLETE")),
    ("closeout_process", ("suite1_single_chains",
                          "closeout.checks.process_complete", True)),
    ("closeout_zero_posts", ("suite1_single_chains",
                             "closeout.checks.zero_posts", True)),
    ("closeout_caps_bound", ("suite1_single_chains",
                             "closeout.checks.caps_bound", True)),
    # #2 超帽拒绝 + 零物理 run + 同一 attempt 自修完成
    ("probe_over_cap_refused", ("suite2_over_cap_self_repair",
                                "rejected_batch_no_runs", True)),
    ("probe_verdict", ("suite2_over_cap_self_repair", "verdict", "PASS")),
    # #3 边界与无自动截断
    ("cells_65", ("suite3_boundaries", "checks.cells_65", "RESOURCE_EXCEEDED")),
    ("cumulative_40_25", ("suite3_boundaries", "checks.cumulative_65",
                          "RESOURCE_EXCEEDED")),
    ("sandbox_32", ("suite3_boundaries", "checks.sandbox_33rd",
                    "RESOURCE_EXCEEDED")),
    ("confirm_shape", ("suite3_boundaries", "checks.confirm_33_blocks",
                       "CONFIRM_BLOCK_COUNT")),
    ("confirm_post_margin", ("suite3_boundaries",
                             "checks.confirm_post_margin_refused",
                             "BUDGET_UNREACHABLE")),
    ("confirm_post_margin_passes_at_8", ("suite3_boundaries",
                                         "checks.confirm_post_margin_ok", "OK")),
    ("unknown_condition", ("suite3_boundaries", "checks.unknown_condition",
                           "CONDITION_UNKNOWN")),
    ("duplicate_cell", ("suite3_boundaries", "checks.duplicate_cell",
                        "DUPLICATE_ID")),
    ("ninth_batch", ("suite3_boundaries", "checks.batch_limit_8",
                     "RESOURCE_EXCEEDED")),
    # #4 transport 故障（离线同构；宿主侧 SIGTERM/重发布归 T2/T3 回归面）
    ("empty_reply", ("suite4_transport_faults", "checks.empty_reply",
                     "CONSUMER_REPLY")),
    ("timeout_unknown_send", ("suite4_transport_faults", "checks.timeout",
                              "CONSUMER_TIMEOUT")),
    ("tool_intent_unknown", ("suite4_transport_faults",
                             "checks.unknown_action", "CONSUMER_ACTION")),
    ("consumer_sigkill", ("suite4_transport_faults", "checks.consumer_kill",
                          "CONSUMER_EXIT")),
    # 独立评审 R4-M1（2026-11-13）：回归行由「文件里有一批用例通过」升级为
    # 「**这些具名用例**真跑通过」——旧登记把 SIGTERM/SIGKILL 场景压在
    # `tests/test_rsi_r12_attempts.py` 上，而该文件没有信号用例，10 条无关用例
    # 全绿就把这一行判成通过。场景 → 文件 + 用例名 的映射由
    # `tests/test_rsi_r12_offline.py::test_regression_rows_point_at_real_cases`
    # 独立硬编码核对（不从本表派生）。
    ("non_gateway_error", ("regression", "tests/test_rsi_r12_attempts.py",
                           "非 Gateway 异常保原码",
                           ("test_every_attempt_leaves_exactly_one_report",))),
    ("worker_sigterm_sigkill", ("regression", "tests/test_rsi_r12_lifecycle.py",
                                "AttemptSession SIGTERM/SIGKILL 终态",
                                ("test_sigterm_writes_terminal_report_and_teardown",
                                 "test_death_without_handshake_gets_watcher_crash_note"))),
    ("republish_trusted_result", ("regression", "tests/test_rsi_r12_recovery.py",
                                  "KNOWN_COMPLETE 只许 republish_only",
                                  ("test_known_complete_with_open_intent_is_republish_only",))),
    # #5 状态隔离
    ("old_pause", ("suite5_state_isolation", "checks.pause_blocks_dispatch",
                   "PAUSED")),
    ("pause_gate_control", ("suite5_state_isolation",
                            "checks.pause_gate_control", "LOCK_MISSING")),
    ("stale_heartbeat", ("suite5_state_isolation",
                         "checks.stale_heartbeat_not_completion", True)),
    ("normal_exit_no_handshake", ("suite5_state_isolation",
                                  "checks.normal_exit_not_completed", True)),
    ("real_death", ("suite5_state_isolation", "checks.dead_pid_not_alive", True)),
    ("cross_study_contamination", ("suite5_state_isolation",
                                   "checks.cross_study_hidden", True)),
    ("cross_study_binding", ("suite5_state_isolation",
                             "checks.cross_study_binding_refused",
                             "STUDY_MISMATCH")),
    ("duplicate_submit", ("suite5_state_isolation",
                          "checks.duplicate_submit_refused", "CLAIM_STATE")),
    ("claim_unbound_artifact", ("suite5_state_isolation",
                                "checks.claim_unbound_source_refused",
                                "UNKNOWN_REPLY")),
    ("claim_before_release", ("suite5_state_isolation",
                              "checks.claim_before_release_refused",
                              "CLAIM_STATE")),
    # #6 人工事件与包对账
    ("human_event_begin_end", ("suite6_human_and_pack",
                               "checks.closed_has_duration", True)),
    ("human_event_interrupt", ("suite6_human_and_pack",
                               "checks.interrupt_blocks_closeout", True)),
    ("assisted_rescue", ("suite6_human_and_pack", "checks.assisted_applied",
                         True)),
    ("open_event_unknown", ("suite6_human_and_pack", "checks.unknown_not_zero",
                            True)),
    ("pack_reconciled", ("suite6_human_and_pack",
                         "checks.pack_matches_manifest", True)),
    ("pack_source_independent", ("suite6_human_and_pack",
                                 "checks.pack_source_reconciled", True)),
)


def _dot(observed: Mapping[str, Any], path: str) -> Any:
    cur: Any = observed
    for part in path.split("."):
        if not isinstance(cur, Mapping) or part not in cur:
            return "<missing>"
        cur = cur[part]
    return cur


def regression_rows() -> "dict[str, str]":
    """矩阵回归行 → 归属测试文件（**唯一真值派生**，供叙述字段使用）。

    独立评审 LOW-1：suite4 的 ``covered_by_regressions`` 曾是手写副本，与
    ``FAILURE_MATRIX`` 修正后的归属互相矛盾（SIGTERM 行指错文件）。叙述字段
    也只许一处真值：谁要展示归属，就调这里。
    """
    return {str(name): str(spec[1]) for name, spec in FAILURE_MATRIX
            if spec and spec[0] == "regression"}


def _case_passed(cases: Mapping[str, Any], name: str) -> bool:
    """``name``（或其参数化实例 ``name[...]``）在取证件里必须是 ``passed``。"""
    for case_name, status in cases.items():
        text = str(case_name)
        if (text == name or text.startswith(name + "[")) and status == "passed":
            return True
    return False


def _regression_row_ok(evidence: "Mapping[str, Any] | None",
                       file_rel: str, closure_sha256: str,
                       frozen_tests: "Mapping[str, str] | None" = None,
                       required: "Sequence[str] | None" = None
                       ) -> "tuple[bool, Any]":
    """回归行只有拿到**绑定当前闭包、冻结测试字节、且具名用例真跑通过**的取证件才算通过。"""
    if not isinstance(evidence, Mapping):
        return False, "<no-evidence>"
    if evidence.get("schema") != OV.REGRESSION_SCHEMA:
        return False, f"schema={evidence.get('schema')!r}"
    if not closure_sha256 or evidence.get("closure_sha256") != closure_sha256:
        return False, (f"closure={evidence.get('closure_sha256')!r} "
                       f"≠ {closure_sha256[:12]}…")
    # 独立评审 R3-M1：取证件必须绑**冻结的那两份测试文件**（否则 freeze 后把测试
    # 改成 `assert True` 再取证，三行照样绿）。调用方漏传冻结值一律判失败，
    # 不允许「没传就跳过」的 fail-open 形态。
    if not isinstance(frozen_tests, Mapping) or not frozen_tests:
        return False, "<no-frozen-tests>"
    if dict(evidence.get("test_files") or {}) != dict(frozen_tests):
        return False, "test-files≠frozen"
    if file_rel not in frozen_tests:
        return False, "<file-not-frozen>"
    # 独立评审 R4-M1：行必须钉**具名用例**——「同文件里有一批用例绿了」不算证据。
    if not isinstance(required, (tuple, list)) or not required:
        return False, "<no-required-cases>"
    if evidence.get("returncode") != 0:
        return False, f"returncode={evidence.get('returncode')!r}"
    rec = (evidence.get("files") or {}).get(file_rel)
    if not isinstance(rec, Mapping):
        return False, "<file-not-run>"
    passed = rec.get("passed")
    failed = rec.get("failed")
    ok = (isinstance(passed, int) and not isinstance(passed, bool) and passed > 0
          and isinstance(failed, int) and not isinstance(failed, bool)
          and failed == 0)
    # 独立评审 LOW-2：具名用例被 skip、同文件其余实例 passed 而 failed==0 时旧实现
    # 仍判绿。skip 本身就是「这项没跑」，回归行不许靠它凑绿——整文件 skipped 必须为 0。
    skipped = rec.get("skipped")
    if ok and not (isinstance(skipped, int) and not isinstance(skipped, bool)
                   and skipped == 0):
        return False, f"skipped={skipped!r}"
    cases = rec.get("cases")
    if ok and not isinstance(cases, Mapping):
        return False, "<no-case-names>"
    if ok:
        missing = [str(n) for n in required if not _case_passed(cases, str(n))]
        if missing:
            return False, {"missing_cases": missing,
                           "passed": passed, "failed": failed}
    return ok, {"passed": passed, "failed": failed,
                "skipped": rec.get("skipped"),
                "required_cases": [str(n) for n in required]}


def failure_matrix(results: Mapping[str, Any], *,
                   regression_evidence: "Mapping[str, Any] | None" = None,
                   closure_sha256: str = "",
                   regression_tests: "Mapping[str, str] | None" = None,
                   ) -> "dict[str, Any]":
    """逐行判定固定失败矩阵的覆盖证据（纯函数；报告与机判共用）。

    ``complete`` 为真表示每一行都指向**本演练当场通过**的检查，或明确登记为
    T2/T3 回归面且**有绑定当前实现闭包的真跑取证件**（``regression_evidence``，
    见 ``offline_validation.run_regression_attestation``）。任何行指向缺失键
    （重命名/删检查）或缺取证件即 ``complete=False``，verdict 随判 FAIL。
    """
    rows: "list[dict[str, Any]]" = []
    missing: "list[str]" = []
    for name, spec in FAILURE_MATRIX:
        if spec[0] == "regression":
            required = spec[3] if len(spec) > 3 else None
            ok, detail = _regression_row_ok(regression_evidence, spec[1],
                                            closure_sha256, regression_tests,
                                            required)
            rows.append({"row": name, "covered_by": f"regression:{spec[1]}",
                         "scenario": spec[2], "evidence": detail, "ok": ok,
                         "required_cases": list(required or ())})
            if not ok:
                missing.append(name)
            continue
        suite, path, want = spec
        got = _dot((results.get(suite) or {}).get("observed") or {}, path)
        ok = got == want
        rows.append({"row": name, "covered_by": f"{suite}/{path}",
                     "want": want, "got": got, "ok": ok})
        if not ok:
            missing.append(name)
    return {"complete": not missing, "missing": missing, "rows": rows,
            "regression_evidence": {
                "present": isinstance(regression_evidence, Mapping),
                "closure_sha256": (regression_evidence or {}).get("closure_sha256")
                if isinstance(regression_evidence, Mapping) else None}}


def run_validation(ctx: Any, *, image: str,
                   runner_factory: Callable[[], Any],
                   counter: Callable[[str], int], ledger_path: "Path | str",
                   repo_root: "Path | str | None" = None,
                   clock: Callable[[], str] = utc_now_iso_z,
                   only: "list[str] | None" = None) -> "dict[str, Any]":
    """跑六套并返回 ``{"suites": ..., "verdict": ...}``（报告件由 CLI 写）。"""
    OV.assert_authorized(ctx, repo_root=repo_root, ledger_path=ledger_path)
    lock = LK.load_frozen(ctx)
    if LK.lock_purpose(ctx.read_json(LK.LOCK_RELPATH)) != "validation":
        raise OfflineSuiteError("LOCK_PURPOSE",
                                "validate-production 需要验证 LOCK（purpose=validation）")
    # 锁内适配（2026-11-12 T5 独立评审 MAJOR-3/4）：运行期必须**自证**用的是冻结的
    # 画像与镜像，而不是只在结案件里抄一遍 LOCK 的值：
    # ① 现网 profile 字节哈希须与 LOCK 冻结值逐字相等（改 profiles.py 后重跑会被
    #    这里拦下，而不是拿旧 sha 签一份新实现）；
    # ② 实际镜像 pin 须与 LOCK 相等（``--image`` 覆盖只在未冻结/调试根有意义，
    #    验证根一律拒）。
    lock_doc = ctx.read_json(LK.LOCK_RELPATH)
    frozen_profile_sha = str((lock_doc.get("validation") or {})
                             .get("profile_sha256") or "")
    live_profile = P.profile_by_id(P.PROFILE_R12)
    live_profile_sha = C.canonical_sha256(live_profile.to_dict())
    if not frozen_profile_sha:
        raise OfflineSuiteError("LOCK_PROFILE_MISSING",
                                "验证 LOCK 未登记 profile_sha256（拒绝无绑定的演练）")
    if live_profile_sha != frozen_profile_sha:
        raise OfflineSuiteError(
            "PROFILE_DRIFT",
            f"现网 profile {live_profile_sha[:16]}… ≠ 冻结 {frozen_profile_sha[:16]}…")
    frozen_image = str((lock_doc.get("image") or {}).get("pin") or "")
    if not frozen_image:
        raise OfflineSuiteError("LOCK_IMAGE_MISSING",
                                "验证 LOCK 未登记镜像 pin（拒绝无绑定的演练）")
    if str(image) != frozen_image:
        raise OfflineSuiteError(
            "IMAGE_MISMATCH", f"运行镜像 {image} ≠ 冻结 {frozen_image}")
    spec = ctx.read_json(OV.SPEC_RELPATH)
    if spec.get("schema") != OV.SPEC_SCHEMA:
        raise OfflineSuiteError("SUITE_SPEC_MISSING", OV.SPEC_RELPATH)
    # MINOR-7：注册件必须**绑定本根授权件**（先注册夹具与预期，再授权；换授权
    # 不重注册即拒），否则「先注册」只是 schema 摆设。
    _auth = OV.load_authorization(ctx)
    if str(spec.get("authorization_sha256") or "") != C.canonical_sha256(_auth):
        raise OfflineSuiteError("SUITE_SPEC_STALE",
                                "注册件 authorization_sha256 ≠ 本根授权件")
    # 独立评审 MAJOR：注册件必须与**当前代码常量**逐值比对（旧实现只比 schema，
    # 注册件里写什么都行）。套件集合、aux 子信封、aux 计划三面全等才放行。
    if set(spec.get("suites") or {}) != set(SUITE_ORDER):
        raise OfflineSuiteError(
            "SUITE_SPEC_MISMATCH",
            f"注册套件 {sorted((spec.get('suites') or {}))} ≠ 代码 "
            f"{sorted(SUITE_ORDER)}")
    # 独立评审 R3-m4：只比键集等于没约束——注册件里 per-suite 的 expected 与代码
    # 不一致也能跑绿并签名。这里要求**逐值全等**（夹具与预期就是被冻结的那一份）。
    want_suites = {k: dict(v) for k, v in OV.SUITE_SPEC.items()}
    if dict(spec.get("suites") or {}) != want_suites:
        raise OfflineSuiteError(
            "SUITE_SPEC_MISMATCH",
            "注册件 per-suite 夹具/预期 ≠ 代码 OV.SUITE_SPEC（先注册后跑的"
            "唯一合法形态是逐值相同）")
    if dict(spec.get("aux_subenvelopes") or {}) != dict(P.AUX_SUBENVELOPES):
        raise OfflineSuiteError("SUITE_SPEC_MISMATCH",
                                "注册 aux 子信封 ≠ profiles.AUX_SUBENVELOPES")
    want_aux_plan = {k: {"envelope": env_name, "steps": int(steps)}
                     for k, (env_name, steps) in OV.AUX_PLAN.items()}
    if dict(spec.get("aux_plan") or {}) != want_aux_plan:
        raise OfflineSuiteError("SUITE_SPEC_MISMATCH",
                                "注册 aux_plan ≠ 代码 AUX_PLAN")
    env = SuiteEnv(ctx=ctx, image=str(image),
                   runner_factory=runner_factory, counter=counter,
                   ledger_path=Path(ledger_path), repo_root=repo_root,
                   profile=live_profile,
                   progress=Progress(ctx, clock=clock),
                   canaries=OV.CanarySet((
                       str(ctx.root).encode("utf-8"),
                       str(Path(ledger_path)).encode("utf-8"),
                       str(Path.home()).encode("utf-8"),
                       b"ANTHROPIC_",
                       b"SIMPRAXIS_",
                   )))
    env.progress.note("validation:start", envelope=ctx.envelope_id,
                      lock=lock.sha256)
    plan = list(dict.fromkeys(only or SUITE_ORDER))
    unknown = [s for s in plan if s not in SUITE_ORDER]
    if unknown:
        raise OfflineSuiteError("SUITE_UNKNOWN", f"{unknown}")
    for suite in plan:
        fn = {"suite1_single_chains": suite1_single_chains,
              "suite2_over_cap_self_repair": suite2_over_cap_self_repair,
              "suite3_boundaries": suite3_boundaries,
              "suite4_transport_faults": suite4_transport_faults,
              "suite5_state_isolation": suite5_state_isolation,
              "suite6_human_and_pack": suite6_human_and_pack}[suite]
        fn(env)
        env.assert_zero_posts(f"after {suite}")
        if env.results[suite]["status"] != "PASS":
            break                       # fail-closed：失败即停，不让后续套掩盖
    env.progress.note("validation:end", suites=len(env.results),
                      failed=[k for k, v in env.results.items()
                              if v["status"] != "PASS"])
    # 跑后复核：每一套的**运行期期望**必须逐字等于注册件（env.record 已把
    # 不符标 FAIL，这里再做一次结构化断言，不让「未登记」悄悄过关）。
    for name, row in env.results.items():
        if row.get("registered_expected") is None:
            raise OfflineSuiteError("SUITE_SPEC_MISMATCH",
                                    f"{name} 未在注册件登记预期")
        if row.get("expected") != row.get("registered_expected"):
            raise OfflineSuiteError(
                "SUITE_SPEC_MISMATCH",
                f"{name} 运行期期望 {row.get('expected')!r} ≠ 注册 "
                f"{row.get('registered_expected')!r}")
    # 锁内适配（2026-11-12 T5 独立评审 MAJOR-2）：``--only`` 子集**不得**产出可
    # 签署的 PASS——六套是验收线，缺一套就只能是 INCOMPLETE（报告如实登记未跑）。
    verdict_doc = validation_verdict(env.results)
    # 独立评审 MAJOR：回归行不再硬编码 ok=True——读取绑定**本 LOCK 闭包**的真跑
    # 取证件（``cli_r12 regression-attest`` 生成）。缺席 ⇒ 矩阵不完整 ⇒ FAIL。
    regression_evidence = OV.load_regression_evidence(ctx)
    frozen_closure_sha = str((lock_doc.get("closure") or {})
                             .get("closure_sha256") or "")
    # R3-M1：取证件还须绑 LOCK 冻结的**测试文件字节**（freeze 后改测试不再有效）。
    frozen_tests = lock_doc.get("regression_tests")
    if not isinstance(frozen_tests, Mapping) or not frozen_tests:
        raise OfflineSuiteError(
            "REGRESSION_TESTS_UNFROZEN",
            "LOCK 未登记 regression_tests（冻结根须重冻才能演练）")
    matrix = failure_matrix(env.results,
                            regression_evidence=regression_evidence,
                            closure_sha256=frozen_closure_sha,
                            regression_tests=frozen_tests)
    # MINOR-3：零 POST 必须**在报告生成时实测**，不是调用方抄一个 0；
    # require_exists=True：账本缺席（被删/从未建）不构成零 POST 证据。
    ledger_rows = OV.ledger_call_rows(ctx, ledger_path, require_exists=True)
    reasons: "list[str]" = []
    if not matrix["complete"]:
        reasons.append(f"固定失败矩阵缺口：{matrix['missing']}")
    if ledger_rows != 0:
        reasons.append(f"账本出现 {ledger_rows} 行付费调用（T5 要求 0）")
    verdict = verdict_doc["verdict"]
    if reasons and verdict == "PASS":
        verdict = "FAIL"
    # 逐回合证据按相位用不同 canary 集，实例各自只持有自己写的行；报告面
    # 必须从同文件回读全部相位（append-only JSONL，seq 由各实例独立编号）。
    entries = _read_turn_entries(ctx)
    return {"suites": env.results, "verdict": verdict,
            "lock_sha256": lock.sha256,
            "suites_required": verdict_doc["suites_required"],
            "suites_run": verdict_doc["suites_run"],
            "missing_suites": verdict_doc["missing_suites"],
            "complete": verdict_doc["complete"],
            "profile_sha256": live_profile_sha,
            "image": str(image),
            "failure_matrix": matrix,
            "reasons": reasons,
            "suite_spec_sha256": C.canonical_sha256(spec),
            "ledger_call_rows": ledger_rows,
            "turns": len(entries),
            "turn_entries": entries}


def _read_turn_entries(ctx: Any) -> "list[dict[str, Any]]":
    rel = ctx.relative(OV.TURN_LOG_RELPATH)
    if not rel.is_file():
        return []
    out: "list[dict[str, Any]]" = []
    for line in rel.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


__all__ = ["FAILURE_MATRIX", "OfflineSuiteError", "Progress", "SUITE_ORDER",
           "SuiteEnv", "failure_matrix", "regression_rows", "run_validation",
           "validation_verdict"]
