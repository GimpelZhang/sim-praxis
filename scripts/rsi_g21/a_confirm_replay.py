#!/usr/bin/env python3
"""RSI-G2.1 A-confirm replay 轮（新口径：候选分析输出必须含阴性对照）。

背景：封存 O-A 的候选 bytes 缺任务冻结的 `negative_torsional`，而 Plan §3.1
禁止宿主回填。数据模型又把"分析源身份"锁在 confirm lock、把"观测身份"绑在
plan sha 上，所以**同批换程序**不存在——最小诚实替代是**同块/同参数的确认批
重执行（confirm replay）**：

1. 复用封存 `keeper_A.seed` ⇒ `allocate_confirm` 产生**同一组 block/参数**；
2. 候选模型新写分析程序（**唯一付费动作**，≤4 次 coding_debug POST）；
3. 该程序先在探索批上跑一次（freeze 的探索凭据要求），再随 `run_confirm`
   在确认批上跑；
4. 于是新封存源的分析输出含主对比 + 阴性对照，供 G2.1 门使用。

本脚本只做"轮"，不做判据：判据仍是 `judge_v13`，G2.1 门仍是 `g2_cli bridge-*`。

步骤（全部幂等，可重入）::

    python scripts/rsi_g21/a_confirm_replay.py prereg  --round <dir>
    python scripts/rsi_g21/a_confirm_replay.py call    --round <dir>
    python scripts/rsi_g21/a_confirm_replay.py drive   --round <dir> [--max-turns 8]
    python scripts/rsi_g21/a_confirm_replay.py manifest --round <dir> --out <path>

付费纪律：`SIMPRAXIS_ALLOW_PAID=1`；账本走本轮的 `rsi_r13/<campaign>/quota/`；
批次批准件由 `prereg` 落盘（依据 = 用户 2026-11-16 的逐字指示，见 prereg.json）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping, Sequence

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from orchestrator.common import research_transport as T  # noqa: E402
from orchestrator.common.fsutil import atomic_write_text, ensure_private_dir  # noqa: E402
from orchestrator.rsi_r1 import assembly as A  # noqa: E402
from orchestrator.rsi_r1 import contracts as C  # noqa: E402
from orchestrator.rsi_r1 import judge as J  # noqa: E402
from orchestrator.rsi_r1 import loop as L  # noqa: E402
from orchestrator.rsi_r1 import profiles as P  # noqa: E402
from orchestrator.rsi_r1 import protocol_r13 as P13  # noqa: E402
from orchestrator.rsi_r1 import state as S  # noqa: E402
from orchestrator.rsi_r1 import task_specs as TS  # noqa: E402
from orchestrator.rsi_r1 import g21_bridge as G21  # noqa: E402
from orchestrator.rsi_r1 import design_intent as DI  # noqa: E402

SEALED = Path("/mnt/artifacts/simpraxis/rsi_r13/r13-paid-20261115o")
IMAGE = ("sim-praxis/sandbox@sha256:"
         "50b6e10df854db40ea41e33f3ed9731a9d5d35b2d9d2ccf9b9f178d8565e4bfe")
ROUND_SCHEMA = "simpraxis.rsi_g21.confirm_replay.v1"
PREREG_SCHEMA = "simpraxis.rsi_g21.confirm_replay_prereg.v1"
STUDY = "A"
BATCH_ID = "g21ar1-analysis"
TASK_ID = 2  # LANES.LANE_TASK_ID["study_A"]
STAGE = "coding_debug"
MAX_POSTS = 4


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha_file(path: "str | Path") -> str:
    return _sha(Path(path).read_bytes())


def _json(path: Path, obj: Any) -> Path:
    ensure_private_dir(path.parent)
    atomic_write_text(path, json.dumps(obj, ensure_ascii=False, sort_keys=True,
                                       indent=1, allow_nan=False) + "\n",
                      mode=0o600)
    return path


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


class RoundError(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"[{code}] {detail}" if detail else code)


def campaign_root(round_dir: Path) -> Path:
    return round_dir / "campaign"


# ---------------------------------------------------------------------------
# prereg：零 POST，先把轮定义/预算/停止条件与输入 sha 冻结
# ---------------------------------------------------------------------------

def prereg(round_dir: Path, *, sealed: Path = SEALED) -> dict[str, Any]:
    campaign = campaign_root(round_dir)
    for sub in ("contract", "private/tasks", "private/receipts",
                "private/analysis", "designs", "confirm", "observations",
                "state", "quota/approvals", "logs", "trials", "evidence_pack",
                "reports", "public"):
        ensure_private_dir(campaign / sub)
    # 固定输入（逐字节复制 + 记录 sha）
    copies = {}
    src_task = sealed / "private/tasks/task_a.json"
    copies["task"] = (src_task, campaign / "private/tasks/task_a.json")
    seed = sealed / "private/keeper_A.seed"
    copies["keeper_seed"] = (seed, campaign / "private/keeper_A.seed")
    plan_sealed = sealed / "confirm/plan_A.json"
    copies["sealed_confirm_plan"] = (plan_sealed,
                                     campaign / "confirm/sealed_plan_A.json")
    for name, (src, dst) in copies.items():
        if not src.is_file():
            raise RoundError("INPUT_MISSING", f"{name}: {src}")
        if not dst.is_file():
            shutil.copyfile(src, dst)
            dst.chmod(0o600)
        if _sha_file(src) != _sha_file(dst):
            raise RoundError("COPY_DRIFT", name)
    # 设计源码（冻结时绑定）与探索批（探索期分析凭据需要）
    journal = [json.loads(line) for line in
               (sealed / "state/journal.jsonl").read_text().splitlines()
               if line.strip()]
    artefacts = [e for e in journal if e["event"] == "artifact"]
    design_sha = str(artefacts[0]["payload"]["source_sha256"])
    design_src = sealed / "designs" / f"{design_sha}.py"
    design_dst = campaign / "designs" / f"{design_sha}.py"
    if not design_dst.is_file():
        shutil.copyfile(design_src, design_dst)
        design_dst.chmod(0o600)
    explore_plan = None
    for event in journal:
        if (event["event"] == "plan_revision"
                and event["payload"].get("study") == STUDY
                and (event["payload"].get("doc") or {}).get("phase") == "explore"):
            explore_plan = event["payload"]
    if explore_plan is None:
        raise RoundError("EXPLORE_PLAN_MISSING", "封存 journal 无探索计划")
    explore_batch = sorted((sealed / "observations").glob("explore-A-*"))[0]
    dst_batch = campaign / "observations" / explore_batch.name
    if not dst_batch.is_dir():
        shutil.copytree(explore_batch, dst_batch)
    for item in dst_batch.rglob("*"):
        if item.is_file():
            item.chmod(0o600)
    # 前缀回复原文：导入的事件引用 ``state/replies/<id>.txt``，必须随源复制，
    # 否则桥的 materialize 会判"回复原文缺失"。
    replies_dir = campaign / "state/replies"
    ensure_private_dir(replies_dir)
    for event in journal:
        if event["event"] == "reply":
            rel = str(event["payload"].get("relpath") or "")
            src = sealed / rel
            dst = campaign / rel
            if src.is_file() and not dst.is_file():
                shutil.copyfile(src, dst)
                dst.chmod(0o600)
        elif event["event"] == "study_transition" and \
                event["payload"].get("study") == STUDY:
            break
    # 公开合同（分析视图要用）
    for rel in ("contract/public_contract.json", "public/public_contract.json"):
        src = sealed / rel
        if src.is_file():
            dst = campaign / rel
            ensure_private_dir(dst.parent)
            if not dst.is_file():
                shutil.copyfile(src, dst)
                dst.chmod(0o600)
    prereg_doc = {
        "schema": PREREG_SCHEMA, "schema_version": 1,
        "round_id": round_dir.name, "study": STUDY,
        "purpose": ("新口径 confirm replay：同 keeper seed/同参数/同块，候选新写分析"
                    "程序（含 negative_torsional），重执行确认批，产出可过 G2.1 门的"
                    "封存源；不产生新科学条件、不做新设计"),
        "authorization": {
            "user_instruction": ("2026-11-16 用户逐字指示：按新口径预注册一轮 A 重跑，"
                                 "看 G2.1 会不会 READY；AskUserQuestion 选择"
                                 "『执行 confirm replay（推荐）』"),
            "budget_rule": "CLAUDE.md 硬规则 2（实呼预算自批，逐次记账）",
            "max_posts": MAX_POSTS,
        },
        "fixed_inputs": {
            "sealed_root": str(sealed),
            "task_sha256": _sha_file(campaign / "private/tasks/task_a.json"),
            "keeper_seed_sha256": _sha_file(campaign / "private/keeper_A.seed"),
            "design_sha256": design_sha,
            "explore_plan_sha256": C.ResearchPlan.from_dict(
                explore_plan["doc"]).sha256,
            "explore_batch": explore_batch.name,
            "sealed_confirm_plan_sha256": _sha_file(plan_sealed),
        },
        "changes_vs_sealed": [
            "分析源换成本轮模型新写的程序（plan.analysis_source_sha 重绑）",
            "确认批重执行（同 keeper seed ⇒ 同 block/同参数）",
        ],
        "required_contrast_ids": [str(TS.TaskSpecV13.from_dict(
            _read(campaign / "private/tasks/task_a.json")).primary.id),
            str(((TS.TaskSpecV13.from_dict(
                _read(campaign / "private/tasks/task_a.json"))
                .negative_control_spec or {}).get("contrast") or {}).get("id"))],
        "stop_conditions": [
            "预算耗尽（>4 POST）即停，不手写候选输出",
            "沙箱失败/输出 schema 无效即停（INSTRUMENT_FAILURE）",
            "物理批 BLOCKED 即停（不开第二批）",
        ],
        "acceptance": ("新确认批 160 run + 分析输出同时含主对比与阴性对照 + 回执 validity + "
                       "judge 层可在 G2.1 桥里复算一致"),
        "created_at_utc": G21._utc_now(),
    }
    _json(campaign / "reports/prereg.json", prereg_doc)
    approval = {
        "batch_id": BATCH_ID, "task": TASK_ID, "stage": STAGE,
        "approved": MAX_POSTS, "beyond_normal": 0,
        "note": ("RSI-G2.1 A-confirm replay 分析程序批次；依据 prereg.json 的"
                 "用户逐字指示；预算 4 次 coding_debug POST"),
        "authorized_by": "user instruction 2026-11-16 (confirm replay)",
        "authorized_at_utc": G21._utc_now(),
    }
    _json(campaign / "quota/approvals" / f"{BATCH_ID}.json", approval)
    return {"ok": True, "campaign": str(campaign), "design_sha256": design_sha,
            "explore_plan_sha256": prereg_doc["fixed_inputs"]["explore_plan_sha256"]}


# ---------------------------------------------------------------------------
# call：唯一付费动作（写分析程序）
# ---------------------------------------------------------------------------

ANALYSIS_PROMPT = """你是 RSI-R1.3 研究 study {study} 的候选。任务规格（冻结）：

{task_block}

### 本轮要求（新口径）
写一个**独立分析程序**（python3，只用标准库 + numpy），从视图目录读取确认批观测，
计算并输出**两个**对比：
- 主对比 `{primary_id}`（{primary_prop}，δ={primary_delta}，metric={metric}）
- 阴性对照 `{nc_id}`（equivalent，δ={nc_delta}，terms={nc_terms}）

阴性对照的 metric_label 用 `{nc_metric}`，但底层量仍是同一 metric 的测量值。

### 视图目录（sys.argv[1]）
- `task.json`：公开任务块（含 conditions/primary/negative_control_spec）
- `plan-<sha12>.json`：确认计划（含 conditions、primary、negative_control、
  analysis_source_sha）
- `public_contract.json`：公开合同
- `run-*.jsonl`（每次运行一条 JSON：time_s/qpos/qvel/target_q/block_id/run_id/spec_hash）
- `run-*.meta.json`（同一 run 的 meta：condition_id/initial_x_m/spec_params）

### 输出（写到 sys.argv[2]/analysis.json）
顶层 `simpraxis.rsi_r1.analysis_output.v1`：
`{{"schema","schema_version","run_ids","contrasts":[...],"diagnostics":{{...}}}}`
`contrasts` 每项：`contrast_id, metric_id, proposition, estimate, lower, upper,
status, n_blocks`（estimate 是 block 效应的等权均值；lower/upper 是
percentile bootstrap，seed 20261111，10000 次，linear 分位，0.95 水平；
status ∈ SUPPORTED/REFUTED/INCONCLUSIVE，按 δ 与区间判）。

`diagnostics` 必须含：schema `simpraxis.rsi_r13.analysis_output.v1`、
phase="confirm"、study_id、task_id、input_sha256=<plan 规范 sha>、
source_sha256=<plan.analysis_source_sha>、contrasts（只放主对比，含 blocks 逐项
effect）、coverage{{blocks,run_count,unique_design_count,independent_unit_count}}、
summaries=[]、threshold_counts（主对比）、phase_comparability。

### 参考模板（生产模板；**它只算主对比**，请在其基础上补阴性对照并保持其它
口径逐字一致）
```python
{template}
```

只输出**完整可运行**的程序本身（可带 ```python 围栏），不要任何解释或注释性
前言；程序请保持紧凑（≤140 行），但必须自洽：读视图 → 算两个对比 → 写
analysis.json。负 block 缺失/非有限一律按
dropped 处理，不补值。"""


def build_prompt(round_dir: Path) -> "tuple[str, str]":
    campaign = campaign_root(round_dir)
    task = TS.TaskSpecV13.from_dict(_read(campaign / "private/tasks/task_a.json"))
    nc = (task.negative_control_spec or {}).get("contrast") or {}
    terms = ", ".join(f"{t['condition_id']}:{t['weight']}"
                      for t in nc.get("terms") or [])
    system = "你是严谨的科研程序作者；只输出可运行代码。"
    brief = ANALYSIS_PROMPT.format(
        study=STUDY, task_block=P13.task_block(task),
        primary_id=task.primary.id, primary_prop=task.primary.proposition,
        primary_delta=task.primary.delta, metric=task.primary.metric,
        nc_id=str(nc.get("id")), nc_delta=str(nc.get("delta")),
        nc_terms=terms, nc_metric=str(nc.get("metric")),
        template=P13.ANALYSIS_TEMPLATE_R13)
    return system, brief


def _strip_fences(text: str) -> str:
    body = text.strip()
    if "```" in body:
        blocks = []
        for chunk in body.split("```")[1:]:
            lines = chunk.splitlines()
            if lines and lines[0].strip().lower().startswith("python"):
                lines = lines[1:]
            blocks.append("\n".join(lines).strip())
        if blocks:
            best = max(blocks, key=len)
            if "sys.argv" in best:
                return best
    if "sys.argv" in body and "import" in body:
        start = body.find("import ")
        return body[start:].strip()
    return body


def call_model(round_dir: Path) -> dict[str, Any]:
    """一次逻辑调用（max_attempts=1）；账本走本轮 campaign 命名空间。"""
    campaign = campaign_root(round_dir)
    prereg_doc = _read(campaign / "reports/prereg.json")
    if str(os.environ.get("SIMPRAXIS_ALLOW_PAID", "")) != "1":
        raise RoundError("PAID_NOT_AUTHORIZED", "需要 SIMPRAXIS_ALLOW_PAID=1")
    round_id = str(prereg_doc["round_id"])
    # 账本/批准件命名空间与轮目录同级：``<round>/campaign/quota/...``。
    os.environ["SIMPRAXIS_RSI_R13_ROOT"] = str(round_dir)
    os.environ["SIMPRAXIS_RSI_R13_CAMPAIGN"] = "campaign"
    from orchestrator.gateway.quota_ledger import QuotaLedger
    from orchestrator.rsi_r1 import gateway_transport as GT
    ledger = QuotaLedger.rsi_r13()
    rows = ledger._call_rows() if hasattr(ledger, "_call_rows") else []
    if len(rows) >= MAX_POSTS:
        raise RoundError("BUDGET_EXHAUSTED", f"已用 {len(rows)} ≥ {MAX_POSTS}")
    gateway = GT.build_gateway(ledger=ledger, stage=STAGE)
    transport = GT.build_transport(gateway=gateway, lane=f"study_{STUDY}",
                                   batch_id=BATCH_ID, thread_id=round_id)
    system, brief = build_prompt(round_dir)
    # 该端点把思考 token 计入输出上限（R1.3 合同：32768 含 thinking）；
    # 8192 时程序会在函数中间被截断（2026-11-16 实测 len=1855）。
    request = T.build_request(system=system, task_brief=brief, turns=[],
                              counter=lambda s: max(1, len(s) // 4),
                              max_tokens=32768)
    reply = transport(request)
    text = str(reply.text)
    call_dir = campaign / "calls"
    ensure_private_dir(call_dir)
    index = len(list(call_dir.glob("reply-*.txt"))) + 1
    (call_dir / f"reply-{index:02d}.txt").write_text(text, encoding="utf-8")
    program = _strip_fences(text).rstrip() + "\n"
    if "sys.argv" not in program or "import" not in program:
        raise RoundError(
            "REPLY_NOT_PROGRAM",
            f"len={len(text)} head={text[:120]!r} tail={text[-160:]!r}")
    program_sha = _sha(program.encode("utf-8"))  # 含末尾单个换行
    call_dir = campaign / "calls"
    ensure_private_dir(call_dir)
    index = len(list(call_dir.glob("call-*.json"))) + 1
    _json(call_dir / f"call-{index:02d}.json", {
        "n": index, "request_sha256": request.sha256,
        "reply_sha256": _sha(text.encode("utf-8")),
        "reply_bytes": len(text.encode("utf-8")),
        "program_sha256": program_sha, "stage": STAGE, "batch_id": BATCH_ID,
        "ledger_namespace": "rsi_r13", "created_at_utc": G21._utc_now()})
    (call_dir / f"program-{index:02d}.py").write_text(program,
                                                      encoding="utf-8")
    (call_dir / f"reply-{index:02d}.txt").write_text(text, encoding="utf-8")
    (campaign / "designs" / f"{program_sha}.py").write_text(program,
                                                           encoding="utf-8")
    os.chmod(campaign / "designs" / f"{program_sha}.py", 0o600)
    if _sha_file(campaign / "designs" / f"{program_sha}.py") != program_sha:
        raise RoundError("PROGRAM_DRIFT", program_sha)
    _json(campaign / "reports/program.json",
          {"program_sha256": program_sha, "call": index,
           "file": f"designs/{program_sha}.py"})
    return {"ok": True, "call": index, "program_sha256": program_sha,
            "program_lines": len(program.splitlines())}


# ---------------------------------------------------------------------------
# drive：真实 loop/dispatcher 走 analyze → freeze_confirm → run_confirm
# ---------------------------------------------------------------------------

def _ctx(round_dir: Path) -> G21.G21Context:
    return G21.G21Context(campaign_root(round_dir))


def _prefix_journal(ctx: G21.G21Context, round_dir: Path) -> None:
    """把封存 A 的探索期事实按类型导入新 root（只到 EXPLORING）。"""
    campaign = campaign_root(round_dir)
    journal = S.Journal(ctx)
    if journal.events():
        return
    prereg_doc = _read(campaign / "reports/prereg.json")
    sealed = Path(prereg_doc["fixed_inputs"]["sealed_root"])
    source = [json.loads(line) for line in
              (sealed / "state/journal.jsonl").read_text().splitlines()
              if line.strip()]
    keep = []
    for event in source:
        name = event["event"]
        payload = event["payload"]
        if name in ("claim", "study_conclusion"):
            continue
        # 本 study 的第一条 transition 就是 EXPLORING→CONFIRM_LOCKED；它之前的
        # 全部事实（探索计划/设计/分析产物）保留，之后的一律截断。
        if name == "study_transition" and payload.get("study") == STUDY:
            break
        keep.append(event)
    reply_ids = {str(e["payload"].get("response_id")) for e in keep
                 if e["event"] == "reply"}
    for event in keep:
        payload = dict(event["payload"])
        if event["event"] in ("artifact", "action"):
            rid = payload.get("response_id")
            if rid is not None and str(rid) not in reply_ids:
                continue
        journal.append(event["event"], payload)
    # 探索批回复原文（analyze 动作的 reply 由后续 loop 记录，不需要历史回复文件）


def _scripted_sender(replies: "list[str]") -> Any:
    class _Sender:
        OFFLINE = True

        def __init__(self) -> None:
            self.replies = list(replies)
            self.n = 0

        def send_once(self, request: Any, reservation: Any) -> Any:
            self.n += 1
            text = self.replies.pop(0)
            return L.Reply(text=text, response_id=f"g21ar1-{self.n:03d}",
                           is_actionable=True)

    return _Sender()


def _explore_run_ids(ctx: G21.G21Context) -> "list[str]":
    ids: "set[str]" = set()
    for path in ctx.root.glob("observations/explore-A-*/*.jsonl"):
        ids.add(path.name[: -len(".jsonl")])
    return sorted(ids)


def drive(round_dir: Path, *, max_turns: int = 8) -> dict[str, Any]:
    campaign = campaign_root(round_dir)
    ctx = _ctx(round_dir)
    program = _read(campaign / "reports/program.json")
    program_sha = str(program["program_sha256"])
    source_text = (campaign / "designs" / f"{program_sha}.py").read_text()
    disk_sha = _sha_file(campaign / "designs" / f"{program_sha}.py")
    if disk_sha != program_sha:
        raise RoundError("PROGRAM_DRIFT", f"{disk_sha} != {program_sha}")
    task_doc = _read(campaign / "private/tasks/task_a.json")
    task = TS.TaskSpecV13.from_dict(task_doc)
    prereg_doc = _read(campaign / "reports/prereg.json")
    design_sha = str(prereg_doc["fixed_inputs"]["design_sha256"])
    plan_doc = _read(campaign / "confirm/sealed_plan_A.json")
    rebound = dict(plan_doc)
    rebound["analysis_source_sha"] = program_sha
    rebound["design_source_sha"] = design_sha
    rebound["parent_sha"] = "host"
    intent = P13._intent_template(task, role="confirmation")
    _prefix_journal(ctx, round_dir)
    state = S.ResearchState(ctx)
    store = L.EventStore(state, study=STUDY)
    admission = G21.OfflineAdmission(study=STUDY, envelope_id="g21ar1")
    budget = G21.OfflineBudget()
    from orchestrator.rsi_r1 import backend as BK
    from orchestrator.rsi_r1 import sandbox as SB

    def backend_factory(batch_id: str) -> Any:
        return BK.PhysicsBackend(
            observations_root=ctx.subdir("observations"),
            evidence_root=ctx.subdir("evidence_pack"),
            work_root=ctx.subdir("trials") / "work-study_A",
            runner=SB.DockerRunner(), image=IMAGE, batch_id=batch_id)

    dispatcher = A.CampaignDispatcher(
        ctx=ctx, state=state, admission=admission, budget=budget,
        study=STUDY, runner=SB.DockerRunner(), image=IMAGE,
        backend_factory=backend_factory, profile=P.PROFILE_R13_DEF,
        task=task, require_task_contrasts=True)
    explore_runs = _explore_run_ids(ctx)
    if not explore_runs:
        raise RoundError("EXPLORE_BATCH_MISSING", "explore-A-* 观测缺失")
    # 断点续跑：按 journal/凭据判断哪些动作已完成（不重发没有必要的动作）。
    events = state.journal.events()
    have_artifact = any(
        ev.event == "artifact" and ev.payload.get("source_sha256") == program_sha
        for ev in events)
    explore_receipt = campaign / "private/receipts/A-explore.json"
    have_explore = False
    if explore_receipt.is_file():
        doc = _read(explore_receipt)
        have_explore = (doc.get("source_sha256") == program_sha
                        and doc.get("phase") == "explore"
                        and bool(doc.get("validity")))
    actions = []
    if not have_artifact:
        actions.append({"action": "write_artifact", "kind": "analysis",
                        "source": source_text})
    if not have_explore:
        actions.append({"action": "analyze", "source_sha256": program_sha,
                        "run_ids": explore_runs})
    actions.append({"action": "freeze_confirm", "plan_sha256": "host",
                    "design_sha256": design_sha,
                    "analysis_sha256": program_sha, "plan": rebound,
                    "design_intent": intent})
    sender = _scripted_sender([json.dumps(a, ensure_ascii=False, sort_keys=True)
                               for a in actions])

    def messages() -> T.Request:
        return T.build_request(
            system="g21ar1 replay（宿主脚本动作；无模型）",
            task_brief=L.render_task_brief(dispatcher.visible_state()),
            turns=store.history(), counter=lambda s: max(1, len(s) // 4))

    loop = L.ResearchLoop(sender=sender, parser=L.parse_action,
                          dispatcher=dispatcher, store=store,
                          messages=messages, counter=lambda s: 1,
                          admission=admission, budget=budget,
                          envelope=SimpleNamespace(envelope_id="g21ar1"),
                          root_lock=None, spend_contract=None)
    rows: "list[dict[str, Any]]" = []
    stopped = ""
    for index in range(1, max_turns + 1):
        state_now = state.study_state(STUDY)
        if state_now == S.ST_RESULTS_RELEASED:
            break
        if not sender.replies and state_now != S.ST_CONFIRM_LOCKED:
            stopped = "NO_MORE_ACTIONS"
            break
        if state_now == S.ST_CONFIRM_LOCKED and not sender.replies:
            token = str((state.confirm_lock(STUDY) or {}).token)
            sender.replies.append(json.dumps(
                {"action": "run_confirm", "confirm_token": token},
                sort_keys=True))
        outcome = loop.turn()
        rows.append(outcome.as_dict() | {"n": index})
        if outcome.terminal:
            stopped = outcome.code or "TERMINAL"
            break
        if not outcome.ok:
            stopped = outcome.code or "NOT_OK"
            break
    final = state.study_state(STUDY)
    result = {"ok": final == S.ST_RESULTS_RELEASED, "study_state": final,
              "stopped": stopped, "turns": rows,
              "n_turns": len(rows)}
    _json(campaign / "reports/drive.json", result)
    return result


# ---------------------------------------------------------------------------
# manifest：给 G2.1 桥准备输入清单（指向本轮 campaign 根）
# ---------------------------------------------------------------------------

def manifest(round_dir: Path, out: Path) -> dict[str, Any]:
    campaign = campaign_root(round_dir)
    prereg_doc = _read(campaign / "reports/prereg.json")
    sealed = Path(prereg_doc["fixed_inputs"]["sealed_root"])
    batch = sorted((campaign / "observations").glob("confirm-A-*"))
    if not batch:
        raise RoundError("CONFIRM_BATCH_MISSING", "先 drive")
    entry = {
        "task": str(campaign / "private/tasks/task_a.json"),
        "plan": str(campaign / "confirm/plan_A.json"),
        "source_design": str(campaign / "designs" /
                             f"{_read(campaign / 'reports/program.json')['program_sha256']}.py"),
        "receipt": str(campaign / "private/receipts/A-confirm.json"),
        "output_raw": str(campaign / "private/analysis/A-confirm.raw.json"),
        "output_pointer": str(campaign / "private/analysis/A-confirm.json"),
        "confirm_manifest": str(campaign / "confirm/manifest_A.json"),
        "observations_batch": str(batch[0]),
        "scope_contract": ("/mnt/artifacts/simpraxis/rsi_g2/"
                           "g2-eng-20261116T074713Z/contract/sealed/A.json"),
        "journal": str(campaign / "state/journal.jsonl"),
    }
    # B 不重跑：沿用封存源（B 本来就 SOURCE_READY，无阴性控制要求）。
    sealed_b = {
        "task": str(sealed / "private/tasks/task_b.json"),
        "plan": str(sealed / "confirm/plan_B.json"),
        "source_design": str(sealed / "designs" / (
            "d311bbf199ec0c463ad03efee953836e82f5c2df0a2677cb4dd013fca3ab8c0f.py")),
        "receipt": str(sealed / "private/receipts/B-confirm.json"),
        "output_raw": str(sealed / "private/analysis/B-confirm.raw.json"),
        "output_pointer": str(sealed / "private/analysis/B-confirm.json"),
        "confirm_manifest": str(sealed / "confirm/manifest_B.json"),
        "observations_batch": str(sorted((sealed / "observations").glob(
            "confirm-B-*"))[0]),
        "scope_contract": ("/mnt/artifacts/simpraxis/rsi_g2/"
                           "g2-eng-20261116T074713Z/contract/sealed/B.json"),
        "journal": str(sealed / "state/journal.jsonl"),
    }
    doc = {"schema": G21.MANIFEST_SCHEMA, "schema_version": 1,
           "source_campaign": str(round_dir.name),
           "source_commit": "replay-round",
           "g2": {"root": str(campaign),
                  "contract_snapshot": entry["scope_contract"]},
           "studies": {"A": dict(entry), "B": sealed_b}}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, ensure_ascii=False, sort_keys=True),
                   encoding="utf-8")
    return {"ok": True, "manifest": str(out), "batch": batch[0].name}


def main(argv: "Sequence[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(prog="a_confirm_replay")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("prereg", "call"):
        p = sub.add_parser(name)
        p.add_argument("--round", required=True)
    p = sub.add_parser("manifest")
    p.add_argument("--round", required=True)
    p.add_argument("--out", required=True)
    p = sub.add_parser("drive")
    p.add_argument("--round", required=True)
    p.add_argument("--max-turns", type=int, default=8)
    args = parser.parse_args(argv)
    round_dir = Path(args.round)
    if args.cmd == "prereg":
        print(json.dumps(prereg(round_dir), ensure_ascii=False, sort_keys=True))
    elif args.cmd == "call":
        print(json.dumps(call_model(round_dir), ensure_ascii=False,
                         sort_keys=True))
    elif args.cmd == "drive":
        print(json.dumps(drive(round_dir, max_turns=int(args.max_turns)),
                         ensure_ascii=False, sort_keys=True))
    else:
        print(json.dumps(manifest(round_dir, Path(args.out)),
                         ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
