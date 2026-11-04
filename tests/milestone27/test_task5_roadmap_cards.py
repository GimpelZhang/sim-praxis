"""Phase 2.7 Task 5（D5-1..D5-5）：多 Stage 路线图 + 飞书富卡机判 —— AC-T5-1..5。

对应计划 §T5.6 的五条验收准则，用例名与机判命令逐字一致：

``test_ac_t5_1_stage_isolation`` / ``test_ac_t5_2_grant_narrowing`` /
``test_ac_t5_3_human_drill_report`` / ``test_ac_t5_4_graph_regression`` /
``test_ac_t5_5_card_snapshot``。

纪律（硬规则 2/7）：

- **0 付费 / 0 联网**：网关与飞书全为本地接缝；AC-T5-3 只读演练落盘的证据件，
  不连飞书（真人点击发生在 ``scripts/phase27/drill_roadmap_cards.py``，不在此）；
- **断言不放水**：Stage 隔离按**事件序列 + agent_run 计数 + 产物哈希**三重断言；
  授权收窄按 **GrantTable 结构子集 + 策略 deny** 双证；图回归按**节点集合与
  条件边目标集合逐字相等**断言（不是包含）；卡片按**表单区字段逐个**断言；
- AC-T5-3 是过程型证据（计划 §L2561 明列）：本测试只断言「判决件齐备且自洽」，
  **不**替人断言结论为正面；缺件即红，不 skip。

证据落 ``$SIMPRAXIS_PHASE27_ROOT/task5_roadmap/``（§T5.7）：本测试负责
``graph_topology.json`` 与 ``card_snapshots/``，``roadmap_fixture.json`` 与
``drill_report_phase27.md`` 由演练脚本落。
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver

REPO_ROOT = Path(__file__).resolve().parents[2]

from orchestrator.agent_runner.runner import RunSpec  # noqa: E402
from orchestrator.agentic.policy import PreToolUsePolicy  # noqa: E402
from orchestrator.agentic.wiring import parse_grant_strings  # noqa: E402
from orchestrator.feishu_gateway.cards import (  # noqa: E402
    build_roadmap_card,
    build_stage_gate_card,
    md_escape,
)
from orchestrator.graph import GraphDeps, build_graph  # noqa: E402
from orchestrator.graph.nodes import initial_state  # noqa: E402
from orchestrator.graph.roadmap import (  # noqa: E402
    ROADMAP_SCHEMA,
    RoadmapError,
    grants_for_stage,
    narrow_spec_grants,
    stage_statuses,
    stage_to_mapping,
    stage_view,
    validate_roadmap,
)

PHASE_ROOT = Path(
    os.environ.get("SIMPRAXIS_PHASE27_ROOT") or "/mnt/artifacts/simpraxis/phase27")
EVIDENCE = PHASE_ROOT / "task5_roadmap"

# 全局授予上限 = Gate 1 选中提案携带的 tool_grants（D5-4：全局只是上限）。
GLOBAL_GRANTS = ("read_file", "list_dir", "write:", "run:python3:-c")

APPROVE = {"decision": "approve", "choice": 0, "human_notes": ""}


def _reject(note: str) -> dict:
    return {"decision": "reject_with_comments", "choice": 0, "human_notes": note}


def _roadmap_doc() -> dict:
    """三阶段合成路线图（AC 要求三阶段；Stage 2 刻意含一个未授的 ``grep``）。"""
    return {
        "schema": ROADMAP_SCHEMA,
        "title": "合成：环境出生 → 接触推箱 → 轨迹回放",
        "stages": [
            {"stage_id": "s1_env_boot", "title": "环境出生",
             "objective": "沙盒起来、依赖可导入",
             "exit_ac": ["AC-S1-1 环境构建成功"],
             "tool_grants": ["read_file", "list_dir"], "max_rounds": 8},
            {"stage_id": "s2_contact_push", "title": "接触推箱",
             "objective": "产出遥测文件且指标非 NaN",
             "exit_ac": ["AC-S2-1 telemetry 存在", "AC-S2-2 指标可读"],
             "tool_grants": ["write:", "grep"], "max_rounds": 8},
            {"stage_id": "s3_trace_replay", "title": "轨迹回放",
             "objective": "回放窗口可复现",
             "exit_ac": ["AC-S3-1 回放跑通"],
             "tool_grants": ["run:python3:-c"], "max_rounds": 8},
        ],
    }


class ScriptedGate:
    def __init__(self, script: list[dict]):
        self.script = list(script)
        self.calls: list[dict] = []

    def __call__(self, state: dict) -> dict:
        self.calls.append({"stage_id": (state.get("stage_frame") or {}).get("stage_id")})
        return dict(self.script.pop(0))


class FakeDirection:
    def __call__(self, state: dict) -> dict:
        # 上限随 Gate 1 提案下达；Gate 1 一次性审整条路线图的授权矩阵（D5-4）。
        return {"proposals": [{"name": "P0", "sim_engine": "fixture",
                               "tool_grants": list(GLOBAL_GRANTS)}],
                "question": state.get("question", "")}


class FakeAgent:
    """写出公式生成、自标 fixture 的 telemetry，并按 Stage 记名。"""

    def __init__(self, root: Path):
        self.root = root
        self.calls: list[dict] = []

    def __call__(self, state: dict) -> dict:
        stage_id = (state.get("stage_frame") or {}).get("stage_id") or "?"
        n = len(self.calls) + 1
        t = 8
        tele = {
            "_fixture": True,
            "_formula": "penetration_m=0.001*m; force_k=1.0+0.1*sin(k)",
            "engine": "fixture-engine", "run_id": f"fixture-run-{n}",
            "dt": 0.016,
            "penetrations": [0.001 * m for m in range(t)],
            "contact_forces": [1.0 + 0.1 * (((k - 3) % 3) - 1) for k in range(t)],
        }
        path = self.root / f"telemetry_{n}_{stage_id}.json"
        path.write_text(json.dumps(tele), encoding="utf-8")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        self.calls.append({"n": n, "stage_id": stage_id, "uri": str(path),
                           "sha256": digest})
        return {"telemetry_uri": str(path), "coding_round": 0}


class FakePersist:
    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, state: dict) -> dict:
        self.calls.append({"stage_status": dict(state.get("stage_status") or {})})
        return {"written": 1}


def _deps(tmp_path: Path, gate2_script: list[dict]):
    gate1 = ScriptedGate([dict(APPROVE, choice=0)])
    gate2 = ScriptedGate(gate2_script)
    agent = FakeAgent(tmp_path / "runs")
    agent.root.mkdir(parents=True, exist_ok=True)
    persist = FakePersist()
    deps = GraphDeps(direction=FakeDirection(), gate1=gate1, agent_run=agent,
                     gate2=gate2, persist=persist,
                     run_dir=tmp_path / "runs")
    return deps, agent, persist


def _run(deps, stages=None, *, thread="t5"):
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    compiled = build_graph(deps).compile(checkpointer=SqliteSaver(conn))
    state = initial_state(thread_id=thread, track="track1", question="q")
    if stages:
        state.update({"stages": [stage_to_mapping(s) for s in stages],
                      "stage_index": 0,
                      "stage_status": stage_statuses(stages),
                      "stage_frame": {}})
    cfg = {"configurable": {"thread_id": thread}, "recursion_limit": 80}
    snapshots = list(compiled.stream(state, config=cfg, stream_mode="values"))
    return snapshots


def _evidence_dir() -> Path:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    os.chmod(EVIDENCE, 0o700)
    return EVIDENCE


def _write(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    os.chmod(path, 0o600)


# ---------------------------------------------------------------------------
# 纯函数层（AC-T5-1/2 的底座；先单独钉死，失败面最小）
# ---------------------------------------------------------------------------

def test_roadmap_validate_fail_closed():
    stages = validate_roadmap(_roadmap_doc())
    assert [s.stage_id for s in stages] == ["s1_env_boot", "s2_contact_push",
                                            "s3_trace_replay"]
    for bad in (
        {"schema": "simpraxis.roadmap.v0", "stages": _roadmap_doc()["stages"]},
        {"schema": ROADMAP_SCHEMA, "stages": []},
        {"schema": ROADMAP_SCHEMA,
         "stages": [_roadmap_doc()["stages"][0], _roadmap_doc()["stages"][0]]},
        {"schema": ROADMAP_SCHEMA,
         "stages": [dict(_roadmap_doc()["stages"][0], tool_grants=[])]},
        {"schema": ROADMAP_SCHEMA,
         "stages": [dict(_roadmap_doc()["stages"][0], max_rounds=0)]},
        {"schema": ROADMAP_SCHEMA, "stages": _roadmap_doc()["stages"],
         "unknown_key": 1},
    ):
        with pytest.raises(RoadmapError):
            validate_roadmap(bad)


# ---------------------------------------------------------------------------
# AC-T5-1：Stage 隔离（Stage 2 驳回只重试 Stage 2；Stage 1 零变化）
# ---------------------------------------------------------------------------

def test_ac_t5_1_stage_isolation(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMPRAXIS_PHASE1_ROOT", str(tmp_path))
    stages = validate_roadmap(_roadmap_doc())
    deps, agent, persist = _deps(tmp_path, [
        dict(APPROVE),                              # s1 放行
        _reject("AC-S2-2 不达标：penetration 峰值越界，重跑本 Stage"),
        dict(APPROVE),                              # s2 补跑放行
        dict(APPROVE),                              # s3 放行
    ])
    snaps = _run(deps, stages)
    final = snaps[-1]

    # ① agent_run 只按 Stage 重跑：s1 恰 1 次，s2 两次，s3 一次
    assert [c["stage_id"] for c in agent.calls] == [
        "s1_env_boot", "s2_contact_push", "s2_contact_push", "s3_trace_replay"]

    # ② Stage 1 的产物从未被重写：同一文件、同一 sha256、共 1 件
    s1 = [c for c in agent.calls if c["stage_id"] == "s1_env_boot"]
    assert len(s1) == 1
    assert Path(s1[0]["uri"]).is_file()
    assert hashlib.sha256(Path(s1[0]["uri"]).read_bytes()).hexdigest() \
        == s1[0]["sha256"]
    assert len(list((tmp_path / "runs").glob("telemetry_*_s1_env_boot.json"))) == 1

    # ③ 驳回当步：stage_index 未前进，Stage 2 未变 approved
    rej_snap = next(s for s in snaps
                    if s.get("events")
                    and s["events"][-1]["type"] == "gate2_rejected")
    assert rej_snap["stage_index"] == 1
    assert rej_snap["stage_status"]["s1_env_boot"] == "approved"
    assert rej_snap["stage_status"]["s2_contact_push"] != "approved"
    assert rej_snap["events"][-1]["stage_id"] == "s2_contact_push"

    # ④ 事件序列逐字：accept→frame 前进；reject→frame 原地重试
    kinds = [(ev["type"], ev.get("stage_id")) for ev in final["events"]
             if ev["type"] in ("stage_frame", "agent_run", "gate1_approved",
                               "gate2_accepted", "gate2_rejected")]
    assert kinds == [
        ("gate1_approved", None),
        ("stage_frame", "s1_env_boot"), ("agent_run", "s1_env_boot"),
        ("gate2_accepted", "s1_env_boot"),
        ("stage_frame", "s2_contact_push"), ("agent_run", "s2_contact_push"),
        ("gate2_rejected", "s2_contact_push"),
        ("stage_frame", "s2_contact_push"), ("agent_run", "s2_contact_push"),
        ("gate2_accepted", "s2_contact_push"),
        ("stage_frame", "s3_trace_replay"), ("agent_run", "s3_trace_replay"),
        ("gate2_accepted", "s3_trace_replay"),
    ]

    # ⑤ 终态：三 Stage 全 approved、索引停在最后一格、persist 恰 1 次
    assert final["stage_status"] == {"s1_env_boot": "approved",
                                     "s2_contact_push": "approved",
                                     "s3_trace_replay": "approved"}
    assert final["stage_index"] == 2
    assert len(persist.calls) == 1

    # ⑥ 路线图运行时事件里带 stage_id；旧路径不带（形状不变，对照见 AC-T5-4）
    assert all(ev.get("stage_id") == "s1_env_boot"
               for ev in final["events"] if ev["type"] == "stage_frame"
               and ev["stage_id"] == "s1_env_boot")


def test_ac_t5_1b_legacy_path_has_no_stage_events(tmp_path, monkeypatch):
    """无 stages ⇒ stage_advance 节点零事件、旧七事件序列逐字不变。"""
    monkeypatch.setenv("SIMPRAXIS_PHASE1_ROOT", str(tmp_path))
    deps, agent, _ = _deps(tmp_path, [dict(APPROVE)])
    final = _run(deps)[-1]
    assert [ev["type"] for ev in final["events"]] == [
        "direction", "gate1_approved", "agent_run", "evaluate",
        "gate2_accepted", "persist_learning", "done"]
    assert all("stage_id" not in ev for ev in final["events"])
    assert final.get("stage_index") in (None, 0)


# ---------------------------------------------------------------------------
# AC-T5-2：授权收窄（⊆ 全局；未授工具不在 GrantTable；策略 100% deny）
# ---------------------------------------------------------------------------

def test_ac_t5_2_grant_narrowing(tmp_path):
    stages = validate_roadmap(_roadmap_doc())
    global_table = parse_grant_strings(GLOBAL_GRANTS)

    for index, stage in enumerate(stages):
        narrowed = grants_for_stage(stages, index, GLOBAL_GRANTS)
        # ① 收窄结果必须是全局上限的**结构子集**（工具集/argv/write 根/探针/模块）
        table = parse_grant_strings(narrowed)
        assert table.tools <= global_table.tools, (stage.stage_id, narrowed)
        assert set(table.run_argv) <= set(global_table.run_argv)
        assert set(table.write_roots) <= set(global_table.write_roots)
        assert table.probes <= global_table.probes
        assert table.expr_modules <= global_table.expr_modules

    # ② Stage 2 声明的 grep 未被 Gate 1 勾选 ⇒ 收窄后消失
    s2 = grants_for_stage(stages, 1, GLOBAL_GRANTS)
    assert s2 == ("write:",)
    table2 = parse_grant_strings(s2)
    assert "grep" not in table2.tools
    assert "write_file" in table2.tools and "apply_patch" in table2.tools

    # ③ 策略钩子对该工具 100% deny（多次同参同决，理由稳定）
    policy = PreToolUsePolicy(table2, workdir=tmp_path)
    decisions = [policy.check("grep", {"pattern": "x"}) for _ in range(3)]
    assert all(d.decision == "deny" for d in decisions)
    assert len({d.reason for d in decisions}) == 1
    assert "grep" in decisions[0].reason
    # Stage 1 的 read_file 在其自身表内允许（收窄不等于全拒）
    policy_s1 = PreToolUsePolicy(
        parse_grant_strings(grants_for_stage(stages, 0, GLOBAL_GRANTS)),
        workdir=tmp_path)
    assert policy_s1.check("read_file", {}).decision == "allow"

    # ④ RunSpec 覆盖点：只换 tool_grants，agentic 开关一字不改（BR-2 冻结）
    spec = RunSpec(thread_id="t5", workdir=tmp_path, cmd=["true"],
                   telemetry_relpath="telemetry.json", batch_id="b",
                   tool_grants=GLOBAL_GRANTS, agentic=False)
    state = {"stage_frame": stage_view(stages[1], s2)}
    narrowed_spec = narrow_spec_grants(spec, state)
    assert narrowed_spec.tool_grants == s2
    assert narrowed_spec.agentic is False
    # 无 stage_frame（旧路径）⇒ 原样返回，不做任何改写
    assert narrow_spec_grants(spec, {}) is spec

    # ⑤ 节点产出的 stage_frame 事件与 grants_for_stage 逐字一致（图侧同源）
    agents = _run_seam_check(tmp_path, stages)
    assert agents == [("s1_env_boot", ["read_file", "list_dir"]),
                      ("s2_contact_push", ["write:"]),
                      ("s3_trace_replay", ["run:python3:-c"])]


def _run_seam_check(tmp_path: Path, stages) -> list[tuple[str, list[str]]]:
    deps, _, _ = _deps(tmp_path / "seam", [dict(APPROVE), dict(APPROVE),
                                           dict(APPROVE)])
    final = _run(deps, stages, thread="t5-seam")[-1]
    return [(ev["stage_id"], ev["tool_grants"]) for ev in final["events"]
            if ev["type"] == "stage_frame"]


# ---------------------------------------------------------------------------
# AC-T5-3：真人演练报告（过程型证据：齐备且自洽，不替人判结论）
# ---------------------------------------------------------------------------

def test_ac_t5_3_human_drill_report():
    report = EVIDENCE / "drill_report_phase27.md"
    records_p = EVIDENCE / "drill_records.json"
    assert report.is_file(), (
        f"缺真人演练报告 {report}：先跑 scripts/phase27/drill_roadmap_cards.py"
        " 并由真人在飞书点一次核准、一次放行、一次驳回（No Fake Approval）")
    assert records_p.is_file(), f"缺演练机读记录 {records_p}"
    records = json.loads(records_p.read_text(encoding="utf-8"))
    text = report.read_text(encoding="utf-8")

    cards = records.get("cards") or []
    assert len(cards) == 3, f"必须恰三条记录（核准/放行/驳回）：{len(cards)}"
    assert [c["gate"] for c in cards] == [1, 2, 2]
    assert [c["decision"] for c in cards] == [
        "approve", "approve", "reject_with_comments"]
    assert len({c["message_id"] for c in cards}) == 3

    # ① 每条都有飞书 message_id（om_ 前缀，可在飞书侧按时间窗查到），且全文可见
    for c in cards:
        assert isinstance(c["message_id"], str) and c["message_id"].startswith("om_")
        assert c["message_id"] in text
        assert c["card_schema"] == "2.0"
        # pending 行终态字面值与 store.py 逐字一致：CHECK 约束只允许
        # PENDING/DECIDED/EXPIRED，来源枚举为 card_form/p2p_text/cli。
        assert c["pending_status"] == "DECIDED"
        assert c["pending_source"] == "card_form"

    # ② 回调时刻：三条各一条，UTC、严格 Z 后缀、与 message_id 一一对应
    callbacks = [c for c in (records.get("callbacks") or []) if c.get("decided")]
    assert len(callbacks) == 3
    by_mid = {c["message_id"]: c for c in callbacks}
    for c in cards:
        assert c["message_id"] in by_mid
        ts = by_mid[c["message_id"]]["received_at_utc"]
        assert isinstance(ts, str) and ts.endswith("Z") and "T" in ts
        assert by_mid[c["message_id"]]["decision"] == c["decision"]
        assert ts in text

    # ③ 检查点侧互证：gate1_approved / gate2_accepted / gate2_rejected 各在场；
    #    驳回后 Stage 未前进（stage_index 停在 Stage 2 = 1）
    events = records.get("events") or []
    assert "gate1_approved" in events
    assert "gate2_accepted" in events
    assert "gate2_rejected" in events
    assert records.get("stage_index") == 1, (
        f"驳回后 Stage 前进了：stage_index={records.get('stage_index')}")
    status = records.get("stage_status") or {}
    assert status.get("s1_env_boot") == "approved"
    assert status.get("s2_contact_push") != "approved"
    # 三 Stage 路线图（计划 §T5.4-F / §T5.7）：驳回 Stage 2 后第三 Stage
    # 必须原样停在 pending——必须是「在场且等于 pending」，不接受缺席。
    assert status.get("s3_trace_replay") == "pending"


# ---------------------------------------------------------------------------
# AC-T5-4：图回归（节点数 10；条件边目标集合逐字相等）
# ---------------------------------------------------------------------------

_EXPECTED_NODES = {"classify", "direction", "gate1", "stage_advance", "agent_run",
                   "evaluate", "gate2", "persist_learning", "human_failure", "done"}

_EXPECTED_CONDITIONAL = {
    "__start__": {"classify"},
    "gate1": {"agent_run", "stage_advance", "direction", "human_failure"},
    "agent_run": {"evaluate", "human_failure"},
    "gate2": {"persist_learning", "stage_advance", "agent_run", "human_failure"},
}

_EXPECTED_STATIC = {
    ("classify", "direction"), ("direction", "gate1"),
    ("stage_advance", "agent_run"), ("evaluate", "gate2"),
    ("persist_learning", "done"), ("done", "__end__"),
    ("human_failure", "__end__"),
}


def test_ac_t5_4_graph_regression():
    draw = build_graph(None).compile().get_graph()
    nodes = {n.id for n in draw.nodes.values()} - {"__start__", "__end__"}
    assert nodes == _EXPECTED_NODES, sorted(nodes ^ _EXPECTED_NODES)
    assert len(nodes) == 10

    cond: dict[str, set[str]] = {}
    static: set[tuple[str, str]] = set()
    for e in draw.edges:
        if e.conditional:
            cond.setdefault(e.source, set()).add(e.target)
        else:
            static.add((e.source, e.target))
    assert cond == _EXPECTED_CONDITIONAL, cond
    assert static == _EXPECTED_STATIC, static ^ _EXPECTED_STATIC

    # 旧边（Phase 1 语义）仍在：gate1→agent_run、gate2→agent_run
    assert ("gate1", "agent_run") in {("gate1", t) for t in cond["gate1"]}
    assert ("gate2", "agent_run") in {("gate2", t) for t in cond["gate2"]}

    _write(_evidence_dir() / "graph_topology.json", {
        "nodes": sorted(nodes),
        "conditional": {k: sorted(v) for k, v in cond.items()},
        "static": sorted([list(e) for e in static]),
        "note": "Phase 2.7 Task5 AC-T5-4 机判落盘；节点 stage_advance 即计划所称 "
                "stage_frame 框节点（langgraph 0.2.28 禁止节点名与状态键同名）",
    })


# ---------------------------------------------------------------------------
# AC-T5-5：卡片渲染快照（离线，schema 2.0 表单区 + 字段校验）
# ---------------------------------------------------------------------------

def _form_of(card: dict) -> dict:
    forms = [el for el in card["body"]["elements"] if el.get("tag") == "form"]
    assert len(forms) == 1, f"必须恰一个表单区：{[el.get('tag') for el in card['body']['elements']]}"
    return forms[0]


def test_ac_t5_5_card_snapshot():
    doc = _roadmap_doc()
    stages = validate_roadmap(doc)
    card = build_roadmap_card(doc)

    assert card["schema"] == "2.0"
    assert card["config"]["wide_screen_mode"] is True
    assert card["header"]["title"]["content"].strip()
    body = card["body"]["elements"]
    assert all(el.get("tag") for el in body)
    divs = [el for el in body if el.get("tag") == "div"]
    assert len(divs) >= 1 + len(stages)          # 导语 + 每 Stage 一段
    for div in divs:
        assert div["text"]["content"].strip()
        assert div["text"]["tag"] == "lark_md"
    joined = "\n".join(el["text"]["content"] for el in divs)
    for stage in stages:
        # 卡片正文经 md_escape（下划线转义），逐字段按同一转义后比对
        assert md_escape(stage.stage_id) in joined
        assert md_escape(stage.title) in joined
        for ac in stage.exit_ac:
            assert md_escape(ac) in joined

    form = _form_of(card)
    assert form["name"] == "gate1_form_0"
    assert [el["tag"] for el in form["elements"]] == ["input", "button", "button"]
    assert form["elements"][0]["name"] == "feedback_0"
    assert form["elements"][1]["value"] == {"gate": 1, "decision": "approve", "choice": 0}
    assert form["elements"][2]["value"] == {
        "gate": 1, "decision": "reject_with_comments", "choice": 0}

    stage_card = build_stage_gate_card(
        stage_view(stages[1], grants_for_stage(stages, 1, GLOBAL_GRANTS)),
        [{"name": "telemetry_2_s2_contact_push.json", "detail": "stage=s2"}],
        stage_index=1, total_stages=3)
    assert stage_card["schema"] == "2.0"
    assert stage_card["header"]["title"]["content"].strip()
    sdivs = [el for el in stage_card["body"]["elements"] if el.get("tag") == "div"]
    smd = "\n".join(el["text"]["content"] for el in sdivs)
    assert md_escape("s2_contact_push") in smd and "2/3" in smd
    assert md_escape("telemetry_2_s2_contact_push.json") in smd
    sform = _form_of(stage_card)
    assert sform["name"] == "gate2_form"
    assert [el["tag"] for el in sform["elements"]] == ["input", "button", "button"]
    assert sform["elements"][1]["value"]["gate"] == 2
    assert sform["elements"][2]["value"]["decision"] == "reject_with_comments"

    # 失败面：空路线图 / 无 stage_id 的 Stage 卡必须直接拒绝（不渲染半成品）
    with pytest.raises(ValueError):
        build_roadmap_card({"schema": ROADMAP_SCHEMA, "stages": []})
    with pytest.raises(ValueError):
        build_stage_gate_card({"title": "无 id"})

    snaps = _evidence_dir() / "card_snapshots"
    _write(snaps / "roadmap_card.json", card)
    _write(snaps / "stage_gate_card.json", stage_card)
    for path in (snaps / "roadmap_card.json", snaps / "stage_gate_card.json"):
        assert json.loads(path.read_text(encoding="utf-8"))["schema"] == "2.0"
