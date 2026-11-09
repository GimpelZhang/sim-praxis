"""RSI-R0.2 测试共用夹具（不是测试文件本身，不含 test_ 函数）。

只做三件事：临时环境变量、最小 campaign、槽账/假 trial/假模型调用。
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Mapping, Sequence

from orchestrator.rsi_r01 import actions as AC
from orchestrator.rsi_r02 import campaign as C
from orchestrator.rsi_r02 import candidate as CD
from orchestrator.rsi_r02 import state as ST

CAMPAIGN_ID = "r02_test"


def set_env(tmp_path: Path, monkeypatch) -> None:
    for name in ("PHASE1", "PHASE2", "PHASE25", "PHASE26", "PHASE27", "PHASE281"):
        monkeypatch.setenv(f"SIMPRAXIS_{name}_ROOT", str(tmp_path / name.lower()))
    monkeypatch.setenv("SIMPRAXIS_RSI_R0_ROOT", str(tmp_path / "rsi_r0"))
    monkeypatch.setenv("SIMPRAXIS_RSI_R01_ROOT", str(tmp_path / "rsi_r01"))
    monkeypatch.setenv("SIMPRAXIS_RSI_R02_ROOT", str(tmp_path / "rsi_r02"))
    monkeypatch.setenv("SIMPRAXIS_RSI_R02_CAMPAIGN", CAMPAIGN_ID)


def make_campaign(tmp_path: Path) -> C.CampaignContext:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    env = dict(os.environ,
               GIT_AUTHOR_NAME="Junchuan Zhang",
               GIT_AUTHOR_EMAIL="zjunchuan@gmail.com",
               GIT_COMMITTER_NAME="Junchuan Zhang",
               GIT_COMMITTER_EMAIL="zjunchuan@gmail.com")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, env=env)
    (repo / "x.txt").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "x.txt"], cwd=repo, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True, env=env)
    plan = tmp_path / "plan.md"
    plan.write_text("# plan\n", encoding="utf-8")
    consent = tmp_path / "consent.txt"
    consent.write_text("authorized\n", encoding="utf-8")
    return C.create_campaign(campaign_id=CAMPAIGN_ID, plan_path=plan,
                             consent_path=consent, repo_root=repo,
                             root=tmp_path / "rsi_r02")


def write_slots(ctx: C.CampaignContext, slot_ids: Sequence[str], *,
                phase: str = "dev", family: str = "E") -> None:
    slots = [{"slot_id": sid, "phase": phase, "family": family,
              "post_cap": 1, "status": ST.SLOT_UNUSED} for sid in slot_ids]
    ctx.write_json("quota/slots.json", {
        "schema": ST.SCHEMA_SLOTBOOK,
        "recorded_at_utc": "2026-11-09T00:00:00Z",
        "counts": {"total": len(slots)},
        "slots": slots,
    })


def write_trial_slots(ctx: C.CampaignContext, specs: Sequence[Any], *,
                      per_trial: int = 24) -> None:
    """按调度规格写整本 224 槽账（每 trial 恰好 ``per_trial`` 个）。"""
    slots = []
    for spec in specs:
        for i in range(1, per_trial + 1):
            slots.append({"slot_id": f"{spec.slot_prefix}{i:02d}",
                          "phase": spec.phase, "family": spec.family,
                          "post_cap": 1, "status": ST.SLOT_UNUSED})
    ctx.write_json("quota/slots.json", {
        "schema": ST.SCHEMA_SLOTBOOK,
        "recorded_at_utc": "2026-11-09T00:00:00Z",
        "counts": {"total": len(slots)},
        "slots": slots,
    })


def write_public_task(ctx: C.CampaignContext, instance_id: str = "E1", *,
                      family: str = "E") -> dict[str, Any]:
    doc = {"schema": "simpraxis.rsi_r02.public_task.v1",
           "instance_id": instance_id, "family": family,
           "public": {"goal": "把推杆推到目标位置",
                      "interface": {"kind": "pendulum_run",
                                    "keys": ["force"], "steps_max": 600}}}
    ctx.write_json(f"public/tasks/{instance_id}/task.json", doc)
    return doc


class FakeTrial:
    """替身 TrialRunner：暴露 surface / dispatch / judge，记录调用次数。"""

    def __init__(self, *, results: "list[dict[str, Any]] | None" = None,
                 raise_on: "set[str] | None" = None,
                 judge_pass: bool = True):
        self.surface = AC.ActionSurface(
            kind="pendulum_run", param_bounds={"force": (-2.0, 2.0)},
            fixed_params={"theta0": 0.1}, max_steps=600)
        self.results = list(results or [])
        self.raise_on = set(raise_on or ())
        self.judge_pass = judge_pass
        self.calls: "list[AC.Action]" = []

    def dispatch(self, action: AC.Action) -> "dict[str, Any]":
        name = CD.action_name(action)
        self.calls.append(action)
        if name in self.raise_on:
            raise RuntimeError(f"容器在 {name} 中崩溃（结果不确定）")
        if self.results:
            return dict(self.results.pop(0))
        if isinstance(action, AC.RunExperiment):
            return {"ok": True, "action": "run_experiment",
                    "run_id": f"run-{len(self.calls)}"}
        if isinstance(action, AC.Analyze):
            return {"ok": True, "action": "analyze",
                    "analysis_id": f"a-{len(self.calls)}",
                    "path": "analysis/a.json", "sha256": "0" * 64}
        if isinstance(action, AC.Submit):
            return {"ok": True, "action": "submit", "claim": action.claim}
        return {"ok": True, "action": name}

    def judge(self) -> "dict[str, Any]":
        return {"pass": bool(self.judge_pass), "layer": "L4", "family": "E"}


class FakeModel:
    """按序回放给定回复；记录每次请求（供「动态反馈被下一轮消费」断言）。"""

    def __init__(self, replies: Sequence[str | Exception], *,
                 usage: Mapping[str, Any] | None = None,
                 attempts_http: int = 1):
        self.replies = list(replies)
        self.requests: "list[Any]" = []
        self.usage = dict(usage if usage is not None else {
            "in": 1000, "out": 200, "cache_read": 0, "cache_create": 0})
        self.attempts_http = attempts_http

    def __call__(self, request: Any) -> CD.Reply:
        self.requests.append(request)
        if not self.replies:
            raise AssertionError("FakeModel 回复已用尽（多余的一次 POST）")
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return CD.Reply(text=r, capture={
            "usage": dict(self.usage), "attempts_http": self.attempts_http,
            "stop_reason": "stop", "provider_model": "fake",
            "provider_request_id": f"req-{len(self.requests)}",
            "body_sha256": "1" * 64, "content_bytes": len(r)})


def action_json(**kw: Any) -> str:
    doc = {"schema": "simpraxis.rsi_r01.action.v1"}
    doc.update(kw)
    return json.dumps(doc, ensure_ascii=False)


def build_loop(ctx: C.CampaignContext, *, slot_ids: Sequence[str],
               replies: Sequence[str | Exception], post_cap: int = 24,
               billing: Any = None, counter: Any = None,
               trial: "FakeTrial | None" = None,
               usage: Mapping[str, Any] | None = None,
               attempts_http: int = 1, with_campaign_state: bool = False,
               family: str = "E", instance_id: str = "E1"):
    """装配一个完整候选闭环（含 trial 状态、journal、槽账、计费账）。"""
    from orchestrator.rsi_r02 import billing as B
    from orchestrator.rsi_r02 import messages as M

    public_doc = write_public_task(ctx, instance_id, family=family)
    store = ST.StateStore(ctx)
    journal = ST.Journal(ctx)
    slots = ST.SlotBook(ctx)
    book = billing if billing is not None else B.BillingBook(ctx)
    state = store.start(trial_id=f"{instance_id}-dev", instance_id=instance_id,
                        family=family, phase="dev", slot_ids=slot_ids,
                        post_cap=len(slot_ids))
    model = FakeModel(replies, usage=usage, attempts_http=attempts_http)
    loop = CD.CandidateLoop(
        ctx=ctx, trial=trial or FakeTrial(), public_doc=public_doc,
        store=store, state=state, journal=journal, slots=slots, billing=book,
        counter=counter or (lambda text: 100), call_model=model,
        slot_ids=slot_ids,
        campaign=ST.CampaignState(ctx) if with_campaign_state else None,
        config=CD.LoopConfig(post_cap=post_cap))
    return loop, model, store, journal, slots, book
