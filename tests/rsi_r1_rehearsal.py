"""RSI-R1 T5 排演台（harness，不是测试文件；不含 ``test_`` 函数）。

目的（AC26/AC27）：在**零付费、零容器、零模型**的前提下，把生产装配面
（``build_campaign_parts`` + ``build_production_sender`` + ``ResearchLoop``）
整条走通，覆盖 T6–T9 的行为面：

* probe lane 的八动作协议链（AC28 的工程基座）；
* practice lane 的 16 呼玩具任务；
* 正式 study_A / study_B lane 的八动作链 + 独立复跑 + 结案产物 + 独立复算。

三条红线在排演里**不放松**：

1. LOCK 必须真冻结（真算闭包 sha、真依赖索引）并真双签；
2. sender 必须是 ``build_production_sender`` 造出来的 ``LockedSender``——
   mock 只替换最底层的 transport，准入/预算/槽/LOCK 复核全走生产代码；
3. 合成记录的判据面不造假：记录过 ``observations.validate_record``，
   判据 ``judge.judge_study`` 自己重算，候选主张由独立统计量构造。

本文件只提供夹具与驱动器；断言在 ``tests/test_rsi_r1_integration.py``。
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from orchestrator.common import research_transport as T
from orchestrator.rsi_r1 import assembly as A
from orchestrator.rsi_r1 import closure as CL
from orchestrator.rsi_r1 import contracts as C
from orchestrator.rsi_r1 import experiments as E
from orchestrator.rsi_r1 import judge as J
from orchestrator.rsi_r1 import lock as LK
from orchestrator.rsi_r1 import loop as L
from orchestrator.rsi_r1 import observations as O
from orchestrator.rsi_r1 import replay as RP
from orchestrator.rsi_r1 import sandbox as SB
from orchestrator.rsi_r1 import scenes as SC
from orchestrator.rsi_r1 import state as ST
from tests import rsi_r1_helpers as H

REHEARSAL_IMAGE = "simpraxis/rsi-r1-rehearsal@sha256:" + "e" * 64
REPLAY_IMAGE_NOTE = "mock replay 不跑容器；镜像钉死仅作 AC27 闭包证据"

#: probe/practice 不许自修的协议链：首错即停（§8.1）。
PROBE_CHAIN = 8
PRACTICE_CHAIN = 16


def sha256_file(path: "Path | str") -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def repo_sha(relpath: str) -> str:
    """给 ``build_production_sender(sha_of=...)`` 的依赖复核。"""
    return sha256_file(LK.REPO_ROOT / relpath)


# ---------------------------------------------------------------------------
# 本地 runner：替身 docker CLI，只认 ``-v host:/data:ro`` / ``-v host:/out:rw``
# ---------------------------------------------------------------------------

@dataclass
class LocalRunner:
    """把沙盒 argv 里的挂载点解出来，在宿主上跑同一个脚本。

    这不是"模拟沙盒"：``sandbox._run_in_sandbox`` 照样建精确视图、写脚本、
    校验输出、回收工作目录；只有最后一步 ``docker run`` 换成本机解释器。
    因此视图/输出/权限/回收逻辑是**生产代码**，排演不重复实现它们。
    """

    python: str = sys.executable
    calls: "list[dict[str, Any]]" = field(default_factory=list)

    @staticmethod
    def mounts(argv: Sequence[str]) -> "tuple[Path, Path]":
        view = out = None
        for i, token in enumerate(argv):
            if token != "-v":
                continue
            spec = argv[i + 1]
            host, target, _mode = spec.rsplit(":", 2)
            if target == "/data":
                view = Path(host)
            elif target == "/out":
                out = Path(host)
        if view is None or out is None:
            raise AssertionError(f"argv 里找不到 /data 与 /out 挂载：{list(argv)}")
        return view, out

    def run(self, argv: "list[str]", *, timeout_s: int,
            container: str) -> SB.RunResult:
        view, out = self.mounts(argv)
        # 容器契约（与 sandbox._container_argv 同源）：python3 <script> <view> <out>。
        # 不要假设 script 是 argv[-1]——契约尾参是视图根/输出根。
        idx = list(argv).index("python3")
        script = Path(argv[idx + 1]).name
        self.calls.append({"argv": list(argv), "container": container})
        try:
            proc = subprocess.run(
                [self.python, "-I", str(view / script), str(view), str(out)],
                cwd=str(view), capture_output=True, text=True,
                timeout=timeout_s)
        except subprocess.TimeoutExpired:
            return SB.RunResult(exit_code=-1, timed_out=True)
        return SB.RunResult(exit_code=proc.returncode,
                            stdout_tail=proc.stdout[-2000:],
                            stderr_tail=proc.stderr[-2000:])

    def residue(self, container: str) -> bool:
        return False

    def force_remove(self, container: str) -> None:
        return None


# ---------------------------------------------------------------------------
# 候选侧夹具源码（真子进程、真视图、真输出帽）
# ---------------------------------------------------------------------------

#: ``__PLAN_SHA__`` 由驱动器替换：候选 design 代码知道自己的计划 sha（公开）。
DESIGN_SOURCE = '''\
import json
import sys
from pathlib import Path

PLAN_SHA = "__PLAN_SHA__"


def main() -> int:
    view = Path(sys.argv[1])
    out = Path(sys.argv[2])
    plans = sorted(view.glob("plan-*.json"))
    if len(plans) != 1:
        print("expected exactly one plan, found %d" % len(plans), file=sys.stderr)
        return 2
    if not (view / "public_contract.json").is_file():
        print("public contract missing", file=sys.stderr)
        return 2
    plan = json.loads(plans[0].read_text(encoding="utf-8"))
    cells = []
    for cond in plan["conditions"]:
        for block_id in ("b01", "b02"):
            cells.append({"condition_id": cond["id"], "block_id": block_id,
                          "params": dict(cond["params"])})
    doc = {"schema": "simpraxis.rsi_r1.matrix.v1", "schema_version": 1,
           "study_id": plan["study_id"], "plan_sha": PLAN_SHA, "cells": cells}
    (out / "matrix.json").write_text(
        json.dumps(doc, ensure_ascii=False, sort_keys=True) + "\\n",
        encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''

ANALYSIS_SOURCE = '''\
import json
import math
import sys
from pathlib import Path


def _metric(record):
    qpos = record["qpos"]
    if "target_q" in record:
        target = record["target_q"]
        sq = 0.0
        for a, b in zip(qpos, target):
            d = float(a[0]) - float(b[0])
            sq += d * d
        amp = max(abs(float(b[0])) for b in target) or 1.0
        return {"metric_id": "normalized_tracking_rmse",
                "value": math.sqrt(sq / len(qpos)) / amp,
                "n_samples": len(qpos)}
    return {"metric_id": "displacement_x_m",
            "value": float(qpos[-1][0]) - float(qpos[0][0]),
            "n_samples": len(qpos)}


def main() -> int:
    view = Path(sys.argv[1])
    out = Path(sys.argv[2])
    runs = {}
    for path in sorted(view.rglob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            runs[record["run_id"]] = {"block_id": record["block_id"],
                                      **_metric(record)}
    doc = {"schema": "simpraxis.rsi_r1.rehearsal_analysis.v1",
           "schema_version": 1, "n_runs": len(runs), "runs": runs}
    (out / "analysis.json").write_text(
        json.dumps(doc, ensure_ascii=False, sort_keys=True) + "\\n",
        encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


def design_source_for(plan_sha: str) -> str:
    return DESIGN_SOURCE.replace("__PLAN_SHA__", plan_sha)


# ---------------------------------------------------------------------------
# 合成物理 backend：记录过生产校验，判据自己重算
# ---------------------------------------------------------------------------

def _a_record(spec: E.ExperimentSpec, *, displacement: float) -> dict:
    times = [i * 0.001 for i in range(6)]
    n = len(times)
    zero = [0.0] * 6
    return {
        "time_s": times,
        "qpos": [[displacement if i == n - 1 else 0.0, 0.0, 0.0,
                  0.0, 0.0, 0.0, 0.0] for i in range(n)],
        "qvel": [list(zero) for _ in times],
        "params_applied": {**dict(spec.physical_params),
                           **dict(spec.controller_params)},
        "engine": {"name": "mujoco", "version": "3.15.0"},
        "run_id": spec.run_id, "spec_hash": spec.spec_hash,
        "block_id": spec.block_id, "nsteps": n, "nonfinite_count": 0,
        "solver_warnings": [],
        "contact_pair": "box_floor",
        "contact_dim_per_step": [3] * n,
        "friction_sliding_per_step": [float(spec.physical_params["sliding_friction"])] * n,
        "friction_torsional_per_step": [float(spec.physical_params["torsional_friction_m"])] * n,
        "contact_force_n": [1.0] * n,
    }


def _b_record(spec: E.ExperimentSpec, *, rmse: float) -> dict:
    amp = float(spec.physical_params["target_amplitude_rad"])
    times = [i * 0.5 for i in range(9)]
    n = len(times)
    return {
        "time_s": times,
        "qpos": [[rmse * amp, 0.0] for _ in times],
        "qvel": [[0.0, 0.0] for _ in times],
        "params_applied": {**dict(spec.physical_params),
                           **dict(spec.controller_params)},
        "engine": {"name": "mujoco", "version": "3.15.0"},
        "run_id": spec.run_id, "spec_hash": spec.spec_hash,
        "block_id": spec.block_id, "nsteps": n, "nonfinite_count": 0,
        "solver_warnings": [],
        "target_q": [[0.0, 0.0] for _ in times],
        "command_torque": [0.0] * n,
        "applied_torque": [0.0] * n,
        "control_update_index": list(range(n)),
        "saturation_count": 0,
        "instability_events": [],
    }


def synthetic_record(spec: E.ExperimentSpec) -> dict:
    """A：低摩擦组位移 −0.01，其余 0（主对比可检测、阴性控制等效）。

    B：base 臂 rmse=1.02，其余 1.00（tc 对比差 0.02 > δ 0.01）。
    数值是**排演夹具**，不声明任何科学结论。
    """
    if spec.study_id == "A":
        disp = -0.01 if spec.condition_id == "low_mu" else 0.0
        record = _a_record(spec, displacement=disp)
    else:
        record = _b_record(spec, rmse=1.02 if spec.condition_id == "base" else 1.0)
    return O.validate_record(record, scene_id=spec.scene_id)


@dataclass
class SyntheticBackend:
    """``execute_batch`` 的 backend 实现；落盘形态与 ``PhysicsBackend`` 同构。"""

    observations_root: Path
    evidence_root: Path
    batch_id: str
    tag: str
    clock_hour: int = 1
    entries: "list[O.ManifestEntry]" = field(default_factory=list)
    metas: "dict[str, dict[str, Any]]" = field(default_factory=dict)
    records: "dict[str, dict[str, Any]]" = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.observations_root = Path(self.observations_root)
        self.evidence_root = Path(self.evidence_root)

    # -- 主路径 ---------------------------------------------------------
    def run(self, spec: E.ExperimentSpec) -> "dict[str, Any]":
        if not isinstance(spec, E.ExperimentSpec):
            raise E.BackendInfraError(f"spec 类型 {type(spec).__name__} 不接受")
        record = synthetic_record(spec)
        base = self.observations_root / self.batch_id
        base.mkdir(parents=True, exist_ok=True, mode=0o700)
        jsonl = base / f"{spec.run_id}.jsonl"
        if jsonl.exists():
            raise E.BackendInfraError(f"RAW_EXISTS: {jsonl}")
        O.write_record_jsonl(jsonl, record)
        meta = self._meta(spec, nsteps=len(record["time_s"]))
        target = base / f"{spec.run_id}.meta.json"
        target.write_text(
            json.dumps(meta, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":"), allow_nan=False) + "\n",
            encoding="utf-8")
        os.chmod(target, 0o600)
        self.metas[spec.run_id] = meta
        self.records[spec.run_id] = record
        self.entries.append(O.manifest_entry(
            spec.run_id, jsonl.name, jsonl, nsteps=len(record["time_s"])))
        return record

    def _meta(self, spec: E.ExperimentSpec, *, nsteps: int) -> "dict[str, Any]":
        identity = hashlib.sha256(
            f"{self.tag}:{self.batch_id}:{spec.run_id}".encode("utf-8")).hexdigest()
        meta = {
            "schema": "simpraxis.rsi_r1.run_meta.v1", "schema_version": 1,
            "batch_id": self.batch_id, "run_id": spec.run_id,
            "spec_hash": spec.spec_hash, "study_id": spec.study_id,
            "scene_id": spec.scene_id, "condition_id": spec.condition_id,
            "block_id": spec.block_id, "status": "ok",
            "nsteps_requested": spec.nsteps, "nsteps_recorded": nsteps,
            "execution_id": f"exec-{self.tag}-{spec.run_id}",
            "container_id": f"ctr-{self.tag}-{spec.run_id}",
            "started_at_utc": f"2026-11-11T{self.clock_hour:02d}:00:00Z",
            "raw_sha256": identity,
            "root": str(self.evidence_root),
            "cache_hit": False,
        }
        if spec.study_id == "A":
            meta["initial_x_m"] = 0.0
        return meta

    def write_manifest(self) -> "dict[str, Any] | None":
        if not self.entries:
            return None
        studies = {m["study_id"] for m in self.metas.values()}
        if len(studies) != 1:
            raise E.BackendInfraError(f"MANIFEST_MULTI_STUDY {sorted(studies)}")
        manifest = O.build_manifest(self.entries, study=studies.pop(),
                                    batch_id=self.batch_id)
        target = self.observations_root / self.batch_id / "manifest.json"
        target.write_text(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":"), allow_nan=False) + "\n",
            encoding="utf-8")
        os.chmod(target, 0o600)
        O.verify_manifest(manifest, target.parent)
        return manifest

    def artifacts(self) -> "dict[str, dict[str, Any]]":
        return {run_id: {"record": self.records[run_id],
                         "meta": self.metas[run_id]}
                for run_id in sorted(self.records)}


# ---------------------------------------------------------------------------
# 脚本化模型：一次一行动作，且必须消费上一轮真实结果
# ---------------------------------------------------------------------------

@dataclass
class ScriptedModel:
    """mock transport：按预编排动作回包，并断言请求里带上了真实结果。

    ``build(index) -> (action_doc, must_contain)``；``must_contain`` 里的每个
    字符串都必须出现在请求正文里——这条断言就是"后继请求必须消费真实结果"
    （§8.1）的机器证据。

    ``label`` 必须**全 campaign 唯一**：``ResearchState.record_action`` 按
    ``(response_id, action_sha)`` 幂等去重，两条 study 若各自从 ``resp-001``
    重新编号，第 4 呼的 analysis 源码逐字节相同 ⇒ 动作 sha 相同 ⇒ 去重命中、
    事件不落盘，``_binding`` 就会比对到上一 study 的动作。真实供应商的 response
    id 天然唯一，排演必须复现这一前提。
    """

    build: Callable[[int], "tuple[dict[str, Any], Sequence[str]]"]
    label: str
    index: int = 0
    replies: "list[dict[str, Any]]" = field(default_factory=list)

    def __call__(self, request: T.Request) -> L.Reply:
        action, must = self.build(self.index)
        blob = "\n".join(str(m.get("content", "")) for m in request.messages)
        for token in must:
            if token not in blob:
                raise AssertionError(
                    f"第 {self.index + 1} 呼没有消费真实结果 {token!r}")
        self.index += 1
        text = json.dumps(action, ensure_ascii=False, sort_keys=True)
        self.replies.append({"text": text, "request_sha256": request.sha256,
                             "n_bytes": request.n_bytes})
        return L.Reply(text=text,
                       response_id=f"resp-{self.label}-{self.index:03d}",
                       usage={"input_tokens": 1, "output_tokens": 1})


# ---------------------------------------------------------------------------
# 八动作链编排
# ---------------------------------------------------------------------------

class StudyScript:
    """一条 study 的八动作链：design→plan→run→analysis→analyze→freeze→run→claim。"""

    def __init__(self, *, study: str, dispatcher: A.CampaignDispatcher) -> None:
        self.study = study
        self.dispatcher = dispatcher
        self.explore_doc = H.plan_doc(study, phase="explore", revision=1,
                                      parent_sha=None)
        self.explore_plan = C.ResearchPlan.from_dict(self.explore_doc)
        self.design_source = design_source_for(self.explore_plan.sha256)
        self.results: "list[dict[str, Any]]" = []
        self.confirm_doc: "dict[str, Any] | None" = None

    # -- 驱动器把每轮真实结果交给脚本 -----------------------------------
    def observe(self, outcome: L.StepOutcome) -> None:
        self.results.append(dict(outcome.result or {}))

    def _design_sha(self) -> str:
        return str(self.results[0]["source_sha256"])

    def _analysis_sha(self) -> str:
        return str(self.results[3]["source_sha256"])

    def _confirm_plan(self) -> "dict[str, Any]":
        if self.confirm_doc is None:
            self.confirm_doc = H.plan_doc(
                self.study, phase="confirm", revision=2,
                parent_sha=self.explore_plan.sha256,
                design_source_sha=self._design_sha(),
                analysis_source_sha=self._analysis_sha())
        return self.confirm_doc

    def _claims(self) -> "list[dict[str, Any]]":
        plan = C.ResearchPlan.from_dict(self._confirm_plan())
        manifest = self.dispatcher.confirm_manifest
        artifacts = self.dispatcher.confirm_artifacts
        if manifest is None or not artifacts:
            raise AssertionError("submit_claim 之前没有确认批产物")
        first = J.judge_study(plan=plan, manifest=manifest, artifacts=artifacts)
        by_contrast = {c["id"]: c for c in first["layers"]["scientific"]}
        claims: "list[dict[str, Any]]" = []
        for hid, hyp in sorted({h.id: h for h in plan.hypotheses}.items()):
            row = by_contrast[hyp.contrast_id]
            # 判据层把 δ 记成字符串（对外报告口径）；动作契约要求数值且须与
            # 量表逐字相符（``loop._validate_claims``），故这里显式转 float。
            claims.append({"hypothesis_id": hid,
                           "proposition": row["proposition"],
                           "metric_id": row["metric_id"],
                           "delta": float(row["delta"]),
                           "estimate": row["effect"],
                           "lower": row["lower"], "upper": row["upper"]})
        return claims

    # -- 编排 -----------------------------------------------------------
    def build(self, index: int) -> "tuple[dict[str, Any], Sequence[str]]":
        s = self.study
        if index == 0:
            return ({"action": "write_artifact", "kind": "design",
                     "source": self.design_source}, ())
        if index == 1:
            return ({"action": "propose_plan", "plan": self.explore_doc},
                    (self._design_sha(),))
        if index == 2:
            return ({"action": "run_batch",
                     "plan_sha256": self.explore_plan.sha256,
                     "design_sha256": self._design_sha(), "phase": "explore"},
                    (self.explore_plan.sha256,))
        if index == 3:
            return ({"action": "write_artifact", "kind": "analysis",
                     "source": ANALYSIS_SOURCE}, ())
        if index == 4:
            run_ids = [str(r) for r in self.results[2]["run_ids"]]
            return ({"action": "analyze", "source_sha256": self._analysis_sha(),
                     "run_ids": run_ids}, tuple(run_ids[:2]))
        if index == 5:
            doc = self._confirm_plan()
            plan = C.ResearchPlan.from_dict(doc)
            return ({"action": "freeze_confirm", "plan_sha256": plan.sha256,
                     "design_sha256": self._design_sha(),
                     "analysis_sha256": self._analysis_sha(), "plan": doc},
                    (self._analysis_sha(),))
        if index == 6:
            token = str(self.results[5]["confirm_token"])
            return ({"action": "run_confirm", "confirm_token": token}, (token,))
        if index == 7:
            n_runs = int(self.results[6]["n_runs"])
            body = (f"排演报告（study {s}）：零付费 mock 链，仅证明协议与产物"
                    f"闭合，不声明研究结论。确认批 {n_runs} run。")
            return ({"action": "submit_claim", "claims": self._claims(),
                     "report_text": body},
                    (str(self.results[6]["result_sha256"]),))
        raise AssertionError(f"第 {index + 1} 个动作超出八动作链")


# ---------------------------------------------------------------------------
# 驱动器：一个 campaign 根 = 一条 lane 场景
# ---------------------------------------------------------------------------

class RehearsalDriver:
    """装配生产面并逐 lane 排演；所有状态转换都走真实状态机。"""

    def __init__(self, root: "Path | str", *, envelope: str = H.ENVELOPE) -> None:
        self.ctx = H.make_ctx(Path(root), envelope)
        H.make_dirs(self.ctx.root)
        for extra in ("public", "analysis", "private", "replay", "claim",
                      "judge", "deliverable", "human_events", "protocol"):
            (self.ctx.root / extra).mkdir(parents=True, exist_ok=True, mode=0o700)
        self.runner = LocalRunner()
        self.budget = C.default_budget(envelope)
        self.ctx.write_json("contract/budget.json", self.budget.to_dict())
        self.ctx.write_json("contract/envelope.json", {
            "schema": A.ENVELOPE_SCHEMA, "schema_version": 1,
            "envelope_id": envelope, "budget_sha256": self.budget.sha256})
        CL.ensure_ledger_file(self.ctx)
        self.spend = A.SpendContract(mode=A.MODE_ZERO_PAID)
        self.state = ST.ResearchState(self.ctx)
        self.chains: "dict[str, dict[str, Any]]" = {}
        self._write_public_contract()

    # -- 合同面 ---------------------------------------------------------
    def _write_public_contract(self) -> None:
        scenes = (SC.SCENE_A, SC.SCENE_B)
        doc = C.public_contract_document(scene_ids=scenes)
        C.assert_public_contract_complete(doc)
        self.ctx.write_json(A.PUBLIC_CONTRACT_RELPATH, doc)
        self.ctx.write_text("public/public_contract.md",
                            C.render_public_contract(scene_ids=scenes))

    def setup(self) -> None:
        """授权 + 校准件（fixture）→ root AUTHORIZED→CALIBRATED。"""
        self.ctx.write_json("contract/calibration_report_v2.json", {
            "schema": "simpraxis.rsi_r1.calibration_report.v2",
            "schema_version": 2, "verdict": "CALIBRATION_PASS",
            "design_version": 2, "container_image": REHEARSAL_IMAGE,
            "rehearsal": True})
        self.ctx.write_json("contract/instrument_prereg.json", {
            "schema": "simpraxis.rsi_r1.rehearsal_prereg.v1",
            "schema_version": 1, "rehearsal": True})
        self.state.advance_root(ST.ROOT_AUTHORIZED,
                                reason="排演：零付费 envelope 落件")
        self.state.advance_root(ST.ROOT_CALIBRATED,
                                reason="排演：fixture 校准 PASS")

    def freeze(self) -> "dict[str, Any]":
        """真冻结 LOCK + 双签 + 全量复核 → root FROZEN。"""
        frozen = LK.freeze_lock(self.ctx, repo_root=LK.REPO_ROOT,
                                image_pin=REHEARSAL_IMAGE)
        LK.sign_lock(self.ctx, role="reviewer", actor="rehearsal-reviewer",
                     decision="APPROVE", rationale="mock 排演，零付费")
        LK.sign_lock(self.ctx, role="verifier", actor="rehearsal-verifier",
                     decision="APPROVE", rationale="mock 排演，零付费")
        verified = LK.verify_lock(self.ctx, repo_root=LK.REPO_ROOT)
        self.state.advance_root(ST.ROOT_FROZEN, reason="LOCK 双签通过")
        self.frozen = frozen
        return verified

    def advance(self, to: str, *, reason: str) -> None:
        self.state.advance_root(to, reason=reason)

    # -- 八动作链 -------------------------------------------------------
    def _factory(self, lane: str) -> Callable[[str], SyntheticBackend]:
        tag = lane.lower()
        def build(batch_id: str) -> SyntheticBackend:
            return SyntheticBackend(
                observations_root=self.ctx.subdir("observations"),
                evidence_root=self.ctx.subdir("observations"),
                batch_id=batch_id, tag=f"{tag}-{batch_id}", clock_hour=1)
        return build

    def chain(self, study: str, lane: str) -> "dict[str, Any]":
        lock = LK.load_frozen(self.ctx)
        admission, slots, dispatcher, state = A.build_campaign_parts(
            self.ctx, spend=self.spend, lane=lane, study=study,
            runner=self.runner, image=REHEARSAL_IMAGE,
            backend_factory=self._factory(lane), lock_sha256=lock.sha256)
        store = L.EventStore(state)
        script = StudyScript(study=study, dispatcher=dispatcher)
        model = ScriptedModel(build=script.build, label=f"{study}-{lane}")
        envelope = A.Envelope.from_ctx(self.ctx)

        def counter(text: str) -> int:
            return (len(text.encode("utf-8")) + 3) // 4

        sender = L.build_production_sender(
            lock_reader=LK.lock_reader(self.ctx),
            transport_factory=lambda: model,
            admission=admission, budget=slots, envelope=envelope,
            spend_contract=self.spend, sha_of=repo_sha)
        loop = L.ResearchLoop(
            sender=sender, parser=L.parse_action, dispatcher=dispatcher,
            store=store,
            messages=lambda: T.build_request(
                system=L.render_system(),
                task_brief=L.render_task_brief(dispatcher.visible_state()),
                turns=store.history(), counter=counter),
            counter=counter, admission=admission, budget=slots,
            envelope=envelope, root_lock=lock, spend_contract=self.spend)

        outcomes: "list[dict[str, Any]]" = []
        for _ in range(PROBE_CHAIN):
            outcome = loop.turn()
            script.observe(outcome)
            outcomes.append(outcome.as_dict())
            if not outcome.ok:
                raise AssertionError(
                    f"{study}/{lane} 第 {len(outcomes)} 呼失败："
                    f"{outcome.code} {dict(outcome.result or {})}")
        if model.index != PROBE_CHAIN:
            raise AssertionError(f"模型被问 {model.index} 次 != {PROBE_CHAIN}")
        record = {"study": study, "lane": lane, "outcomes": outcomes,
                  "dispatcher": dispatcher, "model": model,
                  "confirm_plan": script._confirm_plan()}
        self.chains[f"{study}/{lane}"] = record
        return record

    def expect_refused(self, study: str, lane: str,
                       *, code: str = "BUDGET_REFUSED") -> "dict[str, Any]":
        """槽满后的下一呼必须被拒（probe 8 / practice 16 的边界证据）。

        ``SlotBook.reserve_atomic`` 在槽尽时抛 **terminal** ``LoopError``（不返回
        可自修反馈），故这里按生产语义捕获并核对 code；transport 绝不可触达。
        """
        lock = LK.load_frozen(self.ctx)
        admission, slots, dispatcher, state = A.build_campaign_parts(
            self.ctx, spend=self.spend, lane=lane, study=study,
            runner=self.runner, image=REHEARSAL_IMAGE,
            backend_factory=self._factory(lane), lock_sha256=lock.sha256)
        store = L.EventStore(state)
        envelope = A.Envelope.from_ctx(self.ctx)

        def counter(text: str) -> int:
            return (len(text.encode("utf-8")) + 3) // 4

        sender = L.build_production_sender(
            lock_reader=LK.lock_reader(self.ctx),
            transport_factory=lambda: _never_called,
            admission=admission, budget=slots, envelope=envelope,
            spend_contract=self.spend, sha_of=repo_sha)
        loop = L.ResearchLoop(
            sender=sender, parser=L.parse_action, dispatcher=dispatcher,
            store=store,
            messages=lambda: T.build_request(
                system=L.render_system(),
                task_brief=L.render_task_brief(dispatcher.visible_state()),
                turns=store.history(), counter=counter),
            counter=counter, admission=admission, budget=slots,
            envelope=envelope, root_lock=lock, spend_contract=self.spend)
        try:
            outcome = loop.turn()
        except L.LoopError as exc:
            if exc.code != code:
                raise AssertionError(
                    f"预期 {code}，实得 LoopError {exc.code}: {exc}") from None
            return {"ok": False, "code": exc.code, "terminal": True,
                    "raised": True}
        raise AssertionError(
            f"预期 {code}，实得 ok={outcome.ok} code={outcome.code!r}")

    # -- 复跑与结案 -----------------------------------------------------
    def replay(self, study: str) -> "dict[str, Any]":
        record = self.chains[f"{study}/" + ("study_A" if study == "A"
                                            else "study_B")]
        dispatcher = record["dispatcher"]
        plan = C.ResearchPlan.from_dict(record["confirm_plan"])
        manifest = dispatcher.confirm_manifest
        if manifest is None:
            raise AssertionError("复跑前缺确认 manifest")
        specs = J.build_specs(plan, manifest)
        backend = SyntheticBackend(
            observations_root=self.ctx.subdir("observations"),
            evidence_root=self.ctx.subdir("replay") / f"independent_{study}",
            batch_id=f"replay-{study}", tag=f"repl-{study}", clock_hour=2)
        for run_id in sorted(specs):
            backend.run(specs[run_id])
        backend.write_manifest()
        comparison = RP.compare_study(plan=plan, specs=specs,
                                      originals=dispatcher.confirm_artifacts,
                                      replays=backend.artifacts())
        CL.write_replay_comparison(self.ctx, study, comparison)
        return comparison

    def closeout(self) -> "dict[str, Any]":
        lock = LK.load_frozen(self.ctx)
        entries: "dict[str, dict[str, Any]]" = {}
        for study in ("A", "B"):
            report = self.ctx.read_json(f"judge/report_{study}.json")
            comparison = self.ctx.read_json(f"replay/comparison_{study}.json")
            entries[study] = CL.study_entry(judge_report=report,
                                            comparison=comparison)
        deliverable = CL.write_deliverable(self.ctx, studies=entries,
                                           lock_sha256=lock.sha256)
        CL.build_evidence_pack(self.ctx)
        judged = CL.run_closure_judge(self.ctx)
        return {"studies": entries, "deliverable": deliverable,
                "judged": judged}

    # -- 只读面 ---------------------------------------------------------
    def ledger_calls(self) -> int:
        return CL.ledger_summary(self.ctx)["calls"]

    def public_texts(self) -> "list[str]":
        """候选可见文本（system + 每轮任务块），供公私泄漏对照（AC26）。"""
        out: "list[str]" = [L.render_system()]
        for record in self.chains.values():
            dispatcher = record["dispatcher"]
            out.append(L.render_task_brief(dispatcher.visible_state()))
        return out


def _never_called(request: T.Request) -> L.Reply:
    raise AssertionError("槽满/门禁之后的请求不得触达 transport")
