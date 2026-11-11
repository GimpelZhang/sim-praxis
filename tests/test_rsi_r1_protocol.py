"""RSI-R1 T6（AC28）：probe/practice 两阶段真实轨迹的零付费演练。

本文件证明的是 Plan §8.1 的两条**互不混用**策略：

* ``probe``（8 POST，只用 study A）——**严格首错终止**：第一个非 ok 回合即停，
  不再发下一呼，``root`` 不动；
* ``practice``（固定 16 POST，A→B 两条八动作链）——允许**合同内自修**，
  但额度用尽即 ``NOT_MEASURED``，绝不把「修过」当「通过」。

链路全部走生产面：真 ``ResearchLoop``/``CampaignDispatcher``/``SlotBook``/
``LOCK`` 校验/真实状态机；只有两处是测试替身——**transport**（脚本化模型，
零 POST）与 **backend**（``SyntheticBackend``，零容器）。设计/分析源码直接用
``protocol`` 模块里的公开模板，所以模板一旦漂移，这里先炸。
"""
from __future__ import annotations

import json
import os
import re
import stat
import threading
from pathlib import Path
from typing import Any, Sequence

import pytest

from orchestrator.common import research_transport as T
from orchestrator.rsi_r1 import assembly as A
from orchestrator.rsi_r1 import cli as CLI
from orchestrator.rsi_r1 import contracts as C
from orchestrator.rsi_r1 import experiments as E
from orchestrator.rsi_r1 import judge as J
from orchestrator.rsi_r1 import lock as LK
from orchestrator.rsi_r1 import loop as L
from orchestrator.rsi_r1 import observations as O
from orchestrator.rsi_r1 import protocol as PT
from orchestrator.rsi_r1 import state as S
from tests import rsi_r1_helpers as H
from tests.rsi_r1_rehearsal import REHEARSAL_IMAGE, RehearsalDriver, SyntheticBackend

#: 八动作顺序（Plan §8.1 的字面顺序；probe 与 practice 共用）。
CHAIN = ("write_artifact", "propose_plan", "run_batch", "write_artifact",
         "analyze", "freeze_confirm", "run_confirm", "submit_claim")

_STUDY_RE = re.compile(r'"study":\s*"([AB])"')


def _counter(text: str) -> int:
    """本地 token 计数替身（真实 CLI 用 tokenizer；此处只做确定性计费）。"""
    return (len(text.encode("utf-8")) + 3) // 4


def _prev_result(request: T.Request) -> "dict[str, Any] | None":
    """上一回合的宿主结果（``build_request`` 末条即 result JSON）。

    首呼时末条是任务块而不是 JSON，返回 ``None``。
    """
    try:
        doc = json.loads(request.messages[-1]["content"])
    except (ValueError, TypeError, KeyError):
        return None
    return doc if isinstance(doc, dict) else None


# ---------------------------------------------------------------------------
# 候选侧：公开玩具域八动作链（逐字复用 protocol 的公开模板）
# ---------------------------------------------------------------------------

class ToyScript:
    """一条 study 的编排；``results`` 只装**宿主回传的真实结果**。"""

    def __init__(self, study: str, scope: Path, *,
                 sentinel_parent: bool = False) -> None:
        self.study = study
        self.scope = Path(scope)
        self.sentinel_parent = sentinel_parent
        self.explore_doc = PT.toy_plan_doc(study, phase="explore", revision=1)
        self.explore_plan = C.ResearchPlan.from_dict(self.explore_doc)
        self.results: "list[dict[str, Any]]" = []
        self.confirm_doc: "dict[str, Any] | None" = None

    # -- 真实结果的指针 -------------------------------------------------
    def _design_sha(self) -> str:
        return str(self.results[0]["source_sha256"])

    def _analysis_sha(self) -> str:
        return str(self.results[3]["source_sha256"])

    def _confirm_plan(self) -> "dict[str, Any]":
        if self.confirm_doc is None:
            parent = (L.HOST_PLAN_SHA if self.sentinel_parent
                      else self.explore_plan.sha256)
            self.confirm_doc = PT.toy_plan_doc(
                self.study, phase="confirm", revision=2,
                parent_sha=parent,
                design_sha256=self._design_sha(),
                analysis_sha256=self._analysis_sha())
        return self.confirm_doc

    # -- 从落盘证据独立重算 claims（不经 dispatcher 内存态）-------------
    def _manifest(self) -> E.ConfirmManifest:
        rel = self.scope / "confirm" / f"manifest_{self.study}.json"
        doc = json.loads(rel.read_text(encoding="utf-8"))
        return E.ConfirmManifest(
            study_id=str(doc["study_id"]), plan_sha256=str(doc["plan_sha256"]),
            keeper_seed_sha256=str(doc["keeper_seed_sha256"]),
            block_count=int(doc["block_count"]),
            varying={k: (v[0], v[1]) for k, v in doc["varying"].items()},
            assignments=tuple(
                E.ConfirmAssignment(condition_id=str(a["condition_id"]),
                                    block_id=str(a["block_id"]),
                                    params=dict(a["params"]))
                for a in doc["assignments"]))

    def _artifacts(self, run_ids: Sequence[str]) -> "dict[str, dict[str, Any]]":
        root = self.scope / "observations"
        out: "dict[str, dict[str, Any]]" = {}
        for run_id in run_ids:
            matches = list(root.glob(f"*/{run_id}.jsonl"))
            assert len(matches) == 1, f"{run_id} 原始观测条数 {len(matches)}"
            raw = matches[0]
            out[run_id] = {
                "record": O.read_record_jsonl(raw),
                "meta": json.loads((raw.parent / f"{run_id}.meta.json")
                                   .read_text(encoding="utf-8"))}
        return out

    def _claims(self) -> "list[dict[str, Any]]":
        doc = dict(self._confirm_plan())
        # 候选侧自用的合同对象：sentinel 换成真实探索计划 sha（与宿主替换一致）
        if doc.get("parent_sha") == L.HOST_PLAN_SHA:
            doc["parent_sha"] = self.explore_plan.sha256
        plan = C.ResearchPlan.from_dict(doc)
        manifest = self._manifest()
        specs = J.build_specs(plan, manifest)
        report = J.judge_study(plan=plan, manifest=manifest,
                               artifacts=self._artifacts(list(specs)))
        by_contrast = {c["id"]: c for c in report["layers"]["scientific"]}
        claims: "list[dict[str, Any]]" = []
        for hid, hyp in sorted({h.id: h for h in plan.hypotheses}.items()):
            row = by_contrast[hyp.contrast_id]
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
                     "source": PT.DESIGN_TEMPLATE}, ())
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
                     "source": PT.ANALYSIS_TEMPLATE}, ())
        if index == 4:
            run_ids = [str(r) for r in self.results[2]["run_ids"]]
            return ({"action": "analyze", "source_sha256": self._analysis_sha(),
                     "run_ids": run_ids}, tuple(run_ids[:2]))
        if index == 5:
            doc = self._confirm_plan()
            # ``"host"`` sentinel：计划文档自指，候选无法自算 canonical sha；
            # 宿主按收到的文档重算并回传（见 ``loop.HOST_PLAN_SHA``）。
            return ({"action": "freeze_confirm",
                     "plan_sha256": L.HOST_PLAN_SHA,
                     "design_sha256": self._design_sha(),
                     "analysis_sha256": self._analysis_sha(), "plan": doc},
                    (self._analysis_sha(),))
        if index == 6:
            token = str(self.results[5]["confirm_token"])
            return ({"action": "run_confirm", "confirm_token": token}, (token,))
        if index == 7:
            run_ids = [str(r) for r in self.results[6]["run_ids"]]
            body = (f"协议演练报告（study {s}）：公开玩具域、零付费 mock 链；"
                    f"确认批 {len(run_ids)} run，claims 由候选侧按落盘证据独立重算。")
            return ({"action": "submit_claim", "claims": self._claims(),
                     "report_text": body},
                    (str(self.results[6]["result_sha256"]),))
        raise AssertionError(f"第 {index + 1} 个动作超出八动作链")


class ToyModel:
    """脚本化 transport：按 study 分发，且必须消费上一轮真实结果。

    ``repair_at`` 是**合同内**故障注入：在指定下标第一次发出 ``bad_doc``
    （非终止类拒绝），下一次重发正确动作——这就是 practice 的「允许自修」。
    """

    def __init__(self, *, scope: Path, label: str,
                 repair_at: "int | None" = None,
                 bad_doc: "dict[str, Any] | None" = None,
                 sentinel_parent: bool = False) -> None:
        self.scope = Path(scope)
        self.label = label
        self.repair_at = repair_at
        self.bad_doc = bad_doc
        self.sentinel_parent = sentinel_parent
        self.scripts: "dict[str, ToyScript]" = {}
        self.index: "dict[str, int]" = {}
        self.last_sent: "dict[str, int]" = {}
        self.injected = False
        self.calls = 0

    def __call__(self, request: T.Request) -> L.Reply:
        study = _STUDY_RE.search(request.messages[0]["content"]).group(1)
        script = self.scripts.setdefault(
            study, ToyScript(study, self.scope,
                             sentinel_parent=self.sentinel_parent))
        idx = self.index.get(study, 0)
        prev = _prev_result(request)
        if prev is not None and self.last_sent.get(study) == idx:
            if prev.get("ok"):
                # 末条消息就是宿主结果文档本身（build_request 直接 dump t.result）
                script.results.append(dict(prev))
                idx += 1
                self.index[study] = idx
            # ok=False：合同内拒绝 ⇒ 同一动作重发（不前进、不跳过）
        if (self.repair_at is not None and not self.injected
                and idx == self.repair_at):
            self.injected = True
            action, must = dict(self.bad_doc or {}), ()
        else:
            action, must = script.build(idx)
        blob = "\n".join(str(m.get("content", "")) for m in request.messages)
        for token in must:
            assert token in blob, f"第 {self.calls + 1} 呼没有消费真实结果 {token!r}"
        self.last_sent[study] = idx
        self.calls += 1
        text = json.dumps(action, ensure_ascii=False, sort_keys=True)
        return L.Reply(text=text,
                       response_id=f"resp-{self.label}-{study}-{self.calls:03d}",
                       usage={"input_tokens": 1, "output_tokens": 1})


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------

def _attempt_scope(ctx: Any, lane: str, attempt: int = 1) -> Path:
    return ctx.root / PT.PROTOCOL_SUBDIR / lane / f"attempt-{attempt}"


def _run(driver: RehearsalDriver, lane: str, *, label: str,
         repair_at: "int | None" = None,
         bad_doc: "dict[str, Any] | None" = None,
         batch_id: "str | None" = None,
         sentinel_parent: bool = False) -> "tuple[dict[str, Any], ToyModel]":
    model = ToyModel(scope=_attempt_scope(driver.ctx, lane), label=label,
                     repair_at=repair_at, bad_doc=bad_doc,
                     sentinel_parent=sentinel_parent)
    report = PT.run_protocol(
        driver.ctx, lane=lane, spend=driver.spend,
        transport_factory=lambda: model, runner=driver.runner,
        image=REHEARSAL_IMAGE, batch_id=batch_id or f"t6{label}",
        counter=_counter,
        backend_factory=lambda batch: SyntheticBackend(
            observations_root=_attempt_scope(driver.ctx, lane) / "observations",
            evidence_root=driver.ctx.subdir("evidence_pack"),
            batch_id=batch, tag=f"{label}-{batch}", clock_hour=2))
    return report, model


BAD_RUN_BATCH = {"action": "run_batch", "plan_sha256": "0" * 64,
                 "design_sha256": "1" * 64, "phase": "explore"}


@pytest.fixture()
def frozen(tmp_path: Path) -> RehearsalDriver:
    driver = RehearsalDriver(tmp_path / "campaign")
    driver.setup()
    driver.freeze()
    return driver


# ---------------------------------------------------------------------------
# fixture 自身：公开模板必须能过生产合同
# ---------------------------------------------------------------------------

def test_toy_plan_doc_is_contract_valid_for_both_studies() -> None:
    for study in S.STUDIES:
        doc = PT.toy_plan_doc(study, phase="explore", revision=1)
        plan = C.ResearchPlan.from_dict(doc)
        C.validate_plan(plan)  # 不抛即通过
        assert plan.study_id == study
        assert plan.phase == "explore"
        assert len(plan.conditions) == 3
        assert plan.nuisance_distribution.block_count == 32
    # confirm 阶段缺 design/analysis 源 sha ⇒ 构造期就 fail-closed
    with pytest.raises(C.ContractError):
        PT.toy_plan_doc("A", phase="confirm", revision=2,
                        parent_sha="a" * 64)


def test_protocol_brief_is_deterministic_and_documents_host_sha() -> None:
    for lane in PT.PROTOCOL_LANES:
        first = PT.render_protocol_brief(lane)
        second = PT.render_protocol_brief(lane)
        assert first == second and first.strip()
        listed = re.findall(r"^\s*\d+\.\s*([a-z_]+)\(", first, flags=re.MULTILINE)
        assert tuple(listed) == CHAIN, "八动作顺序必须逐字按 Plan §8.1"
        assert L.HOST_PLAN_SHA in first
        assert str(J.JUDGE_STATISTICS_SEED) in first
    probe_brief = PT.render_protocol_brief(PT.PROBE)
    practice_brief = PT.render_protocol_brief(PT.PRACTICE)
    assert "study a" in probe_brief.lower()
    assert "study b" not in probe_brief.lower(), "probe 只跑 study A，不得给 B 的模板"
    assert "study b" in practice_brief.lower()
    assert "16" in practice_brief


def test_study_brief_is_deterministic_and_names_the_lane_cap() -> None:
    """T7 正式研究 brief：同样公开八动作与 sentinel 约定，且明确 48 呼预算。"""
    for study in S.STUDIES:
        first = PT.render_study_brief(study)
        assert first == PT.render_study_brief(study) and first.strip()
        listed = re.findall(r"^\s*\d+\.\s*([a-z_]+)\(", first, flags=re.MULTILINE)
        assert tuple(listed) == CHAIN, "八动作顺序必须逐字按 Plan §8.1"
        assert L.HOST_PLAN_SHA in first and "PARENT_SHA_MISMATCH" in first
        assert f"study {study}" in first
        assert "48" in first
        assert str(J.JUDGE_STATISTICS_SEED) in first
    with pytest.raises(PT.ProtocolError):
        PT.render_study_brief("C")


# ---------------------------------------------------------------------------
# probe：严格首错终止
# ---------------------------------------------------------------------------

def test_probe_pass_advances_root_to_probed(frozen: RehearsalDriver) -> None:
    report, model = _run(frozen, PT.PROBE, label="probe")
    assert report["verdict"] == PT.VERDICT_PASS, report["reason"]
    assert report["posts"]["this_attempt"] == PT.PROBE_POSTS == 8
    assert model.calls == PT.PROBE_POSTS
    assert report["root_before"] == S.ROOT_FROZEN
    assert report["root_after"] == S.ROOT_PROBED
    assert S.ResearchState(frozen.ctx).root_state() == S.ROOT_PROBED
    (study,) = report["studies"]
    assert study["study"] == "A"
    assert study["ok_count"] == 8 and not study["stopped"]
    # 真实分析结果 → 独立重算的 claims 必须被判 MATCH：整链自洽
    assert study["judge_verdict"] == "JUDGE_OK"
    assert study["study_verdict"] == S.ST_VERIFIED
    assert all(act["ok"] for act in study["actions"])
    assert [act["action"] for act in study["actions"]] == list(CHAIN)
    # 报告与独立日志都落盘，且 locked 面被记录
    rel = frozen.ctx.relative("protocol/report_probe.json")
    assert json.loads(rel.read_text(encoding="utf-8"))["verdict"] == PT.VERDICT_PASS
    assert report["lock_verified"] is True
    assert set(report["lock_signatures"]) == {"reviewer", "verifier"}
    # Plan §8.4：跑付费链的 worker 必须真写心跳（外部监护者据此判活），
    # 且链结束后心跳线程必须回收（不留 daemon 线）。
    beat = json.loads(frozen.ctx.relative("state/worker_heartbeat.json")
                      .read_text(encoding="utf-8"))
    assert beat["owner"] == "rsi-r1-protocol"
    assert beat["pid"] == os.getpid()
    assert beat["phase"] == "probe:study-A"
    assert not any(t.name == "rsi-r1-heartbeat" and t.is_alive()
                   for t in threading.enumerate())


def test_probe_parent_sha_sentinel_is_filled_by_host(
        frozen: RehearsalDriver) -> None:
    """L-R1-07 主线：confirm 计划把 ``parent_sha`` 写 ``"host"`` 也能走完整链。"""
    report, model = _run(frozen, PT.PROBE, label="hostparent",
                         sentinel_parent=True)
    assert report["verdict"] == PT.VERDICT_PASS, report["reason"]
    assert model.calls == PT.PROBE_POSTS
    (study,) = report["studies"]
    assert study["ok_count"] == 8 and not study["stopped"]
    assert study["judge_verdict"] == "JUDGE_OK"
    assert study["study_verdict"] == S.ST_VERIFIED


def test_probe_parent_sha_wrong_value_stops_with_mismatch(
        frozen: RehearsalDriver) -> None:
    """64 hex 但值不对：宿主给可解释的 ``PARENT_SHA_MISMATCH``（非格式码）。"""
    doc = PT.toy_plan_doc("A", phase="confirm", revision=2,
                          parent_sha="0" * 64,
                          design_sha256="d" * 64, analysis_sha256="e" * 64)
    bad = {"action": "freeze_confirm", "plan_sha256": L.HOST_PLAN_SHA,
           "design_sha256": "d" * 64, "analysis_sha256": "e" * 64,
           "plan": doc}
    report, model = _run(frozen, PT.PROBE, label="badparent",
                         repair_at=5, bad_doc=bad)
    assert report["verdict"] == PT.VERDICT_NOT_MEASURED
    assert report["posts"]["this_attempt"] == 6
    assert model.calls == 6
    assert report["root_after"] == S.ROOT_FROZEN
    (study,) = report["studies"]
    assert study["ok_count"] == 5
    assert study["stopped"] == "PARENT_SHA_MISMATCH"
    assert study["terminal"] is False, "可自修：改写 \"host\" 即可继续"


def test_probe_stops_at_first_error_without_repair(
        frozen: RehearsalDriver) -> None:
    report, model = _run(frozen, PT.PROBE, label="probeerr",
                         repair_at=2, bad_doc=BAD_RUN_BATCH)
    assert report["verdict"] == PT.VERDICT_NOT_MEASURED
    assert report["posts"]["this_attempt"] == 3
    assert model.calls == 3, "严格首错策略下失败后不得再发下一呼"
    assert report["root_after"] == S.ROOT_FROZEN
    assert S.ResearchState(frozen.ctx).root_state() == S.ROOT_FROZEN
    (study,) = report["studies"]
    assert study["ok_count"] == 2
    assert study["stopped"] == "PLAN_MISMATCH"
    assert study["terminal"] is False
    assert report["policy"]["strict_first_error"] is True


# ---------------------------------------------------------------------------
# practice：合同内自修 + 固定 16 POST
# ---------------------------------------------------------------------------

def test_practice_pass_runs_two_clean_chains(frozen: RehearsalDriver) -> None:
    state = S.ResearchState(frozen.ctx)
    assert state.root_state() == S.ROOT_FROZEN
    # 先过 probe 门（practice 的 ROOT_GATE 是 PROBED）
    probe, _ = _run(frozen, PT.PROBE, label="probe_ok")
    assert probe["verdict"] == PT.VERDICT_PASS
    report, model = _run(frozen, PT.PRACTICE, label="practice")
    assert report["verdict"] == PT.VERDICT_PASS, report["reason"]
    assert report["posts"]["this_attempt"] == PT.PRACTICE_POSTS == 16
    assert model.calls == 16
    assert report["root_before"] == S.ROOT_PROBED
    assert report["root_after"] == S.ROOT_PRACTICED
    assert S.ResearchState(frozen.ctx).root_state() == S.ROOT_PRACTICED
    assert [s["study"] for s in report["studies"]] == ["A", "B"]
    for study in report["studies"]:
        assert study["ok_count"] == 8 and not study["stopped"]
        assert study["judge_verdict"] == "JUDGE_OK"
        assert study["study_verdict"] == S.ST_VERIFIED
    assert report["policy"]["strict_first_error"] is False


def test_practice_repairs_once_but_cap_ends_not_measured(
        frozen: RehearsalDriver) -> None:
    probe, _ = _run(frozen, PT.PROBE, label="probe_ok2")
    assert probe["verdict"] == PT.VERDICT_PASS
    report, model = _run(frozen, PT.PRACTICE, label="practicefix",
                         repair_at=2, bad_doc=BAD_RUN_BATCH)
    assert report["verdict"] == PT.VERDICT_NOT_MEASURED
    # 自修被允许 ⇒ A 链继续跑满 8 ok，但**多吃掉一呼**；practice 的固定 16 呼
    # 恰好等于 16 个动作，于是 study B 在 freeze_confirm 时撞上「冻结后至少余 2」
    # 的 lane 空间门（RESOURCE_EXHAUSTED，终止类）——修过的链不可能被判通过。
    assert model.calls == 15
    assert report["posts"]["this_attempt"] == 15
    a, b = report["studies"]
    assert a["study"] == "A" and a["ok_count"] == 8
    assert b["study"] == "B" and b["ok_count"] == 5
    assert b["stopped"] == "RESOURCE_EXHAUSTED" and b["terminal"] is True
    assert report["root_after"] == S.ROOT_PROBED
    assert S.ResearchState(frozen.ctx).root_state() == S.ROOT_PROBED
    assert any(act.get("code") == "PLAN_MISMATCH" for act in a["actions"])
    assert "实呼 15" in report["reason"]


# ---------------------------------------------------------------------------
# 门与参数：任何拒绝都不得触达 transport
# ---------------------------------------------------------------------------

def test_lock_missing_never_reaches_transport(tmp_path: Path) -> None:
    driver = RehearsalDriver(tmp_path / "campaign")
    driver.setup()  # 只有 CALIBRATED，没有 LOCK
    touched: "list[bool]" = []

    def factory() -> Any:
        touched.append(True)
        raise AssertionError("LOCK 缺失时 transport 不得被构造")

    with pytest.raises(LK.LockError) as excinfo:
        PT.run_protocol(driver.ctx, lane=PT.PROBE, spend=driver.spend,
                        transport_factory=factory, runner=driver.runner,
                        image=REHEARSAL_IMAGE, batch_id="t6nolk",
                        counter=_counter)
    assert excinfo.value.code == "LOCK_MISSING"
    assert touched == []
    assert not driver.ctx.exists("protocol/report_probe.json")


def test_practice_refused_at_frozen_root_gate(frozen: RehearsalDriver) -> None:
    touched: "list[bool]" = []

    def factory() -> Any:
        touched.append(True)
        raise AssertionError("门未开时 transport 不得被构造")

    with pytest.raises(PT.ProtocolError) as excinfo:
        PT.run_protocol(frozen.ctx, lane=PT.PRACTICE, spend=frozen.spend,
                        transport_factory=factory, runner=frozen.runner,
                        image=REHEARSAL_IMAGE, batch_id="t6gate",
                        counter=_counter)
    assert excinfo.value.code == "ROOT_GATE"
    assert touched == []
    assert S.ResearchState(frozen.ctx).root_state() == S.ROOT_FROZEN
    assert not frozen.ctx.exists("protocol/report_practice.json")


def test_lane_and_batch_guards(frozen: RehearsalDriver) -> None:
    with pytest.raises(PT.ProtocolError) as bad_lane:
        PT.run_protocol(frozen.ctx, lane="study", spend=frozen.spend,
                        transport_factory=lambda: None, runner=frozen.runner,
                        image=REHEARSAL_IMAGE, batch_id="t6x", counter=_counter)
    assert bad_lane.value.code == "LANE_UNKNOWN"
    with pytest.raises(PT.ProtocolError) as bad_batch:
        PT.run_protocol(frozen.ctx, lane=PT.PROBE, spend=frozen.spend,
                        transport_factory=lambda: None, runner=frozen.runner,
                        image=REHEARSAL_IMAGE, batch_id="  ", counter=_counter)
    assert bad_batch.value.code == "BATCH_MISSING"


# ---------------------------------------------------------------------------
# 批次批准件（规则 2）：幂等、0600、落在本 campaign 根下、漂移拒绝
# ---------------------------------------------------------------------------

def _env_campaign(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                  campaign_id: str = "t6cap") -> RehearsalDriver:
    family = tmp_path / "rsi_r1_family"
    driver = RehearsalDriver(family / campaign_id)
    monkeypatch.setenv("SIMPRAXIS_RSI_R1_ROOT", str(family))
    monkeypatch.setenv("SIMPRAXIS_RSI_R1_CAMPAIGN", campaign_id)
    return driver


def test_ensure_approval_is_idempotent_and_private(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    driver = _env_campaign(tmp_path, monkeypatch)
    first = PT.ensure_approval(driver.ctx, batch_id="t6probe", lane=PT.PROBE,
                               approved=PT.PROBE_POSTS)
    second = PT.ensure_approval(driver.ctx, batch_id="t6probe", lane=PT.PROBE,
                                approved=PT.PROBE_POSTS)
    assert first == second == driver.ctx.root / "quota" / "approvals" / "t6probe.json"
    assert stat.S_IMODE(first.stat().st_mode) == 0o600
    doc = json.loads(first.read_text(encoding="utf-8"))
    want = PT.approval_doc(batch_id="t6probe", lane=PT.PROBE,
                           approved=PT.PROBE_POSTS)
    for key in ("batch_id", "task", "stage", "approved"):
        assert doc[key] == want[key]
    # 同票不同额度 ⇒ 明确拒绝（不静默复用）
    with pytest.raises(PT.ProtocolError) as drift:
        PT.ensure_approval(driver.ctx, batch_id="t6probe", lane=PT.PROBE,
                           approved=PT.PRACTICE_POSTS)
    assert drift.value.code == "APPROVAL_DRIFT"


def test_ensure_approval_refuses_foreign_campaign_root(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    driver = _env_campaign(tmp_path, monkeypatch)
    monkeypatch.setenv("SIMPRAXIS_RSI_R1_CAMPAIGN", "othercap")
    with pytest.raises(PT.ProtocolError) as excinfo:
        PT.ensure_approval(driver.ctx, batch_id="t6probe", lane=PT.PROBE,
                           approved=PT.PROBE_POSTS)
    assert excinfo.value.code == "APPROVAL_ROOT_MISMATCH"


def test_cli_probe_refuses_without_paid_env_gate(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    driver = _env_campaign(tmp_path, monkeypatch)
    driver.setup()
    driver.freeze()
    (driver.ctx.root / "contract" / "scope.json").write_text(
        json.dumps({"schema": "simpraxis.rsi_r1.scope.v1", "schema_version": 1,
                    "envelope_id": H.ENVELOPE}) + "\n", encoding="utf-8")
    monkeypatch.delenv("SIMPRAXIS_ALLOW_PAID", raising=False)
    rc = CLI.main(["probe", "--envelope", H.ENVELOPE,
                   "--batch-id", "t6cligate",
                   "--tokenizer", str(tmp_path / "no-such-tokenizer.json"),
                   "--tokenizer-sha256", "0" * 64])
    assert rc == 2
    err = capsys.readouterr().err
    assert "PAID_NOT_AUTHORIZED" in err
    assert "SIMPRAXIS_ALLOW_PAID=1" in err
    # 环境闸先于批准件写入：拒绝路径不得留下任何票
    assert not (driver.ctx.root / "quota" / "approvals" / "t6cligate.json").exists()
    assert not driver.ctx.exists("protocol/report_probe.json")
