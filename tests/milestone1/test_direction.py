"""Phase 1 Task 2 direction_setter 单测（0 付费 0 联网）。

网关与向量库全部注入合成实现：FakeGateway 只回放脚本化 JSON 字符串并记录调用
（Task3 起 call 契约返回 (text, (tokens_in, tokens_out))，与 ModelGateway 同形），
FakeVector 只回放合成 KB 命中；证据根目录与网关暂停态目录全部隔离到 tmp_path。

Task3（§6.3）记账收口后，setter 不再持账本：preflight/seq 记账统一由
ModelGateway(ledger=...) 内的 BillingGate 完成，其集成断言在
tests/milestone1/test_gateway_billing.py（含原「无批次凭据不发网拦截」用例）。

用例：
  1  首轮合法 2 方案 → 通过，证据落盘；
  2  首轮坏 JSON → 恰好重问 1 次后通过（共 2 次调用，无第 3 次）；
  3  sim_engine 不在矩阵可用集 → 校验失败重问，第 2 次合法通过；
  4  两次均不过校验 → DirectionSchemaError，调用恰好 2 次；
  5  Gate1 首轮批准；
  6  Gate1 打回 1 次：human_notes 进第 2 轮 prompt（最高优先级标记原样出现）；
  7  Gate1 第 3 次仍打回 → human_failure（首轮 + 2 次再问，共 3 轮 macro）；
  8  源码红线：direction 包 .py 零裸引擎名字面量（白名单仅矩阵数据文件）；
  10 macro 阶段永不发 image：每次实呼消息 content 全是 str；
  11 capabilities 检索 0 行 → DirectionError，0 次实呼；
  12 parse_json_object 围栏/噪声提取；
  13 evidence_refs 严格性（越界/重复/空/非编号均拒）。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

from orchestrator.direction import setter as setter_mod
from orchestrator.direction.inputs import get_input
from orchestrator.direction.setter import (
    DirectionError, DirectionSchemaError, DirectionSetter,
    build_first_messages, load_engine_catalog, parse_json_object,
    validate_proposals,
)
from orchestrator.gateway import quota_ledger as ql_mod
from orchestrator.gateway.quota_ledger import QuotaLedger
from orchestrator.gateway.router import GatewayPausedError

BATCH = "T2-FIXTURE"
STAGE = "macro_ideation"


# ---------- 合成件 ----------

class FakeGateway:
    def __init__(self, raws):
        self.raws = list(raws)
        self.calls = []

    def model_id(self, stage):
        return "fixture-macro-model"

    def call(self, stage, messages, *, max_tokens, temperature, **kw):
        self.calls.append({"stage": stage, "messages": messages,
                           "max_tokens": max_tokens, "temperature": temperature})
        if not self.raws:
            raise AssertionError("FakeGateway 脚本耗尽：出现计划外的额外 macro 调用")
        # Task3 call() 契约：(text, (tokens_in, tokens_out))；合成网关不造用量
        return self.raws.pop(0), (None, None)


class FakeVector:
    def __init__(self, hits=None):
        self.hits = hits if hits is not None else [
            {"id": f"rowid{i}", "text": f"合成能力证据第 {i} 行：物理引擎接触模型与传感器特性。",
             "kb_relpath": f"capabilities/engine_{i}.md",
             "section_path": f"章节{i}", "doc_type": "capability_fact"}
            for i in range(1, 9)]
        self.queries = []

    def query_kb(self, query, *, namespace=None, top_k=5, **kw):
        self.queries.append((query, namespace, top_k))
        assert namespace == "capabilities" and top_k == 8
        return list(self.hits)


class FakeFeishu:
    def __init__(self, decisions):
        self.decisions = list(decisions)
        self.requests = []

    def request_gate1_approval(self, thread_id, title, proposals, **kw):
        self.requests.append({"thread_id": thread_id, "title": title,
                              "proposals": proposals})
        return self.decisions.pop(0)


def good_proposals_raw(slugs, *, question="合成题目"):
    return json.dumps({
        "question": question,
        "proposals": [
            {"name": f"方案{chr(65+i)}", "sim_engine": slug,
             "first_principles": f"第一性原理推导 [S{i+1}]，接触力学量级论证 [S1]。",
             "key_params": "摩擦系数 logU(0.05,0.8)；求解步长 0.5ms-2ms",
             "controls_vs_experiment": "Baseline 单一标称参数 vs Ours 域随机化",
             "evidence_refs": [f"S{i+1}"],
             "risk": "风险：接触穿透；验证：最小闭环探针",
             "expected_artifacts": [f"probe_{i}.mp4", f"metrics_{i}.json"]}
            for i, slug in enumerate(slugs)],
    }, ensure_ascii=False)


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMPRAXIS_PHASE1_ROOT", str(tmp_path / "phase1"))
    monkeypatch.setenv("SIMPRAXIS_GATEWAY_STATE", str(tmp_path / "gwstate"))
    catalog = load_engine_catalog()
    payload = get_input("track2", 1)
    return {"tmp": tmp_path, "catalog": catalog,
            "slugs": sorted(catalog), "payload": payload}


def make_setter(isolated, raws, *, hits=None):
    # Task3：setter 不接账本；FakeGateway 无 BillingGate（纯回放，0 付费语义）
    return DirectionSetter(FakeGateway(raws), FakeVector(hits))


# ---------- 用例 ----------

def test_01_valid_first_round(isolated):
    slugs = isolated["slugs"]
    s = make_setter(isolated, [good_proposals_raw(slugs[:2])])
    out = s.run_direction(track="track2", payload=isolated["payload"],
                          thread_id="fx01", batch_id=BATCH)
    assert len(out["proposals"]) == 2
    assert {p["sim_engine"] for p in out["proposals"]} == set(slugs[:2])
    assert out["kb_sources"][0]["ref"] == "S1"
    gw = s.gateway
    assert len(gw.calls) == 1 and gw.calls[0]["stage"] == STAGE
    # Task3：seq 记账已收口进 gateway.call（BillingGate），不在 setter 侧写账本；
    # 本用例（无账本 FakeGateway）不得生成任何 ledger 文件。
    assert not (isolated["tmp"] / "phase1/quota/ledger.jsonl").exists()
    # 证据文件（脱敏 req/resp + proposals）
    ev = isolated["tmp"] / "phase1/task2_direction"
    assert len(list(ev.glob("macro_fx01_r0_a0.req.json"))) == 1
    assert len(list(ev.glob("macro_fx01_r0_a0.resp.json"))) == 1
    assert (ev / "proposals_fx01_r0.json").exists()


def test_02_bad_json_then_one_repair(isolated):
    slugs = isolated["slugs"]
    s = make_setter(isolated, ["@@@不是JSON@@@", good_proposals_raw(slugs[:2])])
    out = s.run_direction(track="track2", payload=isolated["payload"],
                          thread_id="fx02", batch_id=BATCH)
    gw = s.gateway
    assert len(gw.calls) == 2                      # 首轮 + 恰好 1 次重问
    assert len(out["proposals"]) == 2
    # 重问消息含校验反馈与上一轮 assistant 原文
    repair_user = gw.calls[1]["messages"][-1]["content"]
    assert "未通过严格校验" in repair_user
    assert gw.calls[1]["messages"][-2]["role"] == "assistant"
    # prompt 中证据以 [来源: relpath#section] 列注
    assert "[来源: capabilities/engine_1.md#章节1]" in gw.calls[0]["messages"][1]["content"]


def test_03_invalid_engine_requeried(isolated):
    slugs = isolated["slugs"]
    bad = json.loads(good_proposals_raw(slugs[:2]))
    bad["proposals"][0]["sim_engine"] = "carla"   # 矩阵中 absent/不存在
    s = make_setter(isolated, [json.dumps(bad), good_proposals_raw(slugs[1:3])])
    out = s.run_direction(track="track2", payload=isolated["payload"],
                          thread_id="fx03", batch_id=BATCH)
    assert len(s.gateway.calls) == 2
    assert all(p["sim_engine"] in isolated["catalog"] for p in out["proposals"])
    resp = json.loads((isolated["tmp"] /
                       "phase1/task2_direction/macro_fx03_r0_a0.resp.json").read_text())
    assert resp["validation_errors"] and any("引擎目录" in e for e in resp["validation_errors"])


def test_04_two_failures_raise_schema_error(isolated):
    s = make_setter(isolated, ["bad1", "bad2"])
    with pytest.raises(DirectionSchemaError):
        s.run_direction(track="track2", payload=isolated["payload"],
                        thread_id="fx04", batch_id=BATCH)
    assert len(s.gateway.calls) == 2              # 严禁无限重问


def test_05_gate1_approve_first_round(isolated):
    slugs = isolated["slugs"]
    s = make_setter(isolated, [good_proposals_raw(slugs[:2])])
    feishu = FakeFeishu([{"decision": "approve", "choice": 0,
                          "human_notes": "", "source": "card_form"}])
    out = s.run_gate1_loop(feishu, track="track2", payload=isolated["payload"],
                           thread_id="fx05", batch_id=BATCH)
    assert out["status"] == "approved" and out["gate1_rejects"] == 0
    assert len(feishu.requests) == 1


def test_06_reject_notes_injected(isolated):
    slugs = isolated["slugs"]
    s = make_setter(isolated, [good_proposals_raw(slugs[:2]),
                               good_proposals_raw(slugs[1:3])])
    notes = "必须补充油污与冰块的黏度量级依据，并加入名义参数 Baseline 对照"
    feishu = FakeFeishu([
        {"decision": "reject_with_comments", "choice": None,
         "human_notes": notes, "source": "card_form"},
        {"decision": "approve", "choice": 0, "human_notes": "", "source": "card_form"},
    ])
    out = s.run_gate1_loop(feishu, track="track2", payload=isolated["payload"],
                           thread_id="fx06", batch_id=BATCH)
    assert out["status"] == "approved" and out["gate1_rejects"] == 1
    second_prompt = s.gateway.calls[1]["messages"][-1]["content"]
    assert "人工评审意见（最高优先级，必须逐条响应）" in second_prompt
    assert notes in second_prompt
    # 上一轮被打回方案随附
    assert "上一轮候选方案（已被打回）" in second_prompt


def test_07_three_rejects_human_failure(isolated):
    slugs = isolated["slugs"]
    s = make_setter(isolated, [good_proposals_raw(slugs[:2]) for _ in range(3)])
    rej = {"decision": "reject_with_comments", "choice": None,
           "human_notes": "仍不满意", "source": "card_form"}
    feishu = FakeFeishu([dict(rej), dict(rej), dict(rej)])
    out = s.run_gate1_loop(feishu, track="track2", payload=isolated["payload"],
                           thread_id="fx07", batch_id=BATCH)
    assert out["status"] == "human_failure" and out["gate1_rejects"] == 3
    assert len(s.gateway.calls) == 3             # 首轮 + 2 次再问，无第 4 次
    assert len(feishu.requests) == 3


def test_08_no_engine_literal_in_direction_source():
    pkg = Path(setter_mod.__file__).parent
    matrix = yaml.safe_load(
        (setter_mod.REPO_ROOT / "memory_flywheel"
         / "capability_matrix.yaml").read_text(encoding="utf-8"))
    # 红线：矩阵全部 slug（含 absent 的 sapien/bullet）+ 基础计划 L452 旧硬编码 carla；
    # 白名单：capability_matrix.yaml 数据文件本身。
    names = {e["slug"] for e in matrix["engines"]} | {"carla"}
    pattern = re.compile("|".join(rf"\b{re.escape(n)}\b" for n in names if n))
    hits = []
    for py in pkg.glob("*.py"):
        for i, line in enumerate(py.read_text().splitlines(), 1):
            if pattern.search(line):
                hits.append(f"{py.name}:{i}:{line.strip()}")
    assert not hits, f"direction 包出现裸引擎名字面量：{hits}"


# 原 test_09（无批次凭据 → preflight 不发网拦截 + seq=null 审计行 + 不写持久
# 熔断文件）Task3 已随记账收口移入 tests/milestone1/test_gateway_billing.py
# （test_preflight_missing_credential_blocks_without_net），在真 ModelGateway +
# BillingGate + billing_context 上断言，比旧 FakeGateway 路径更贴近生产。


def test_10_never_send_image_blocks(isolated):
    slugs = isolated["slugs"]
    s = make_setter(isolated, [good_proposals_raw(slugs[:2])])
    s.run_direction(track="track1", payload=get_input("track1", 1),
                    thread_id="fx10", batch_id=BATCH)
    for call in s.gateway.calls:
        for m in call["messages"]:
            assert isinstance(m["content"], str)        # 纯文本块
            assert '"image"' not in m["content"]
    # builder 直测：track1 注入 canonical spine 且不含任何引擎选择分支
    msgs = build_first_messages(track="track1", payload=get_input("track1", 1),
                                hits=FakeVector().hits, catalog=isolated["catalog"])
    assert "canonical spine" in msgs[1]["content"]
    assert "Stage 0" in msgs[1]["content"] and "Stage 3" in msgs[1]["content"]


def test_11_zero_hits_raises(isolated):
    s = make_setter(isolated, [], hits=[])
    with pytest.raises(DirectionError, match="0 行"):
        s.run_direction(track="track2", payload=isolated["payload"],
                        thread_id="fx11", batch_id=BATCH)
    assert s.gateway.calls == []


def test_14_litellm_pydantic_shim_bare_import():
    """锁内适配回归（2026-10-14 实呼取证）：router 必须在任何前置包缺失时
    自行修复 litellm 1.99.4 的 py3.10 两处缺陷；子进程裸导入序锁定。"""
    import os
    import subprocess
    import sys
    code = (
        "import sys;"
        "assert 'transformers' not in sys.modules;"
        "import orchestrator.gateway.router;"
        "from litellm import ModelResponse;"
        "ModelResponse();"
        "from litellm.types.utils import Delta;"
        "Delta();"
        "print('SHIM_OK')")
    env = {k: v for k, v in os.environ.items()
           if k.startswith(("PATH", "PYTHON", "LD_LIBRARY"))}
    env["PYTHONPATH"] = str(setter_mod.REPO_ROOT)
    r = subprocess.run([sys.executable, "-c", code], cwd=str(setter_mod.REPO_ROOT),
                       env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, f"裸导入序 shim 失效：\n{r.stderr[-800:]}"
    assert "SHIM_OK" in r.stdout


def test_12_parse_json_object_fences():
    obj = parse_json_object('前缀噪声\n```json\n{"question": "q", "proposals": []}\n```\n尾巴')
    assert obj["question"] == "q"
    obj2 = parse_json_object('{"question": "q2"}')
    assert obj2["question"] == "q2"


def test_13_evidence_refs_strict(isolated):
    hits = FakeVector().hits
    base = json.loads(good_proposals_raw(isolated["slugs"][:2]))
    for bad_refs in ([], ["S9"], ["S1", "S1"], ["随便写"], ["S0"]):
        bad = json.loads(json.dumps(base))
        bad["proposals"][0]["evidence_refs"] = bad_refs
        _, errors = validate_proposals(bad, hits=hits, catalog=isolated["catalog"])
        assert any("evidence_refs" in e for e in errors), (bad_refs, errors)
    # 1..8 合法、去重合法
    ok = json.loads(json.dumps(base))
    ok["proposals"][0]["evidence_refs"] = ["S1", "S8"]
    props, errors = validate_proposals(ok, hits=hits, catalog=isolated["catalog"])
    assert not errors and props[0]["evidence_refs"] == ["S1", "S8"]


# ---------- 评审 M4：preflight 各拦截分支 + pause 超集回归（纯文件，0 付费） ----------

PAUSE_KEYS_V1 = {"status", "stage", "reason", "paused_at_epoch", "paused_at_iso",
                 "resume_at_epoch", "resume_at_iso"}


def _seed_calls(path, n, *, task=2, batch_id=BATCH, stage=STAGE, offset=0):
    """预置 n 行真实调用行；seq/global_used_after 从 offset+1 起全局连续。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        for j in range(1, n + 1):
            i = offset + j
            fh.write(json.dumps({
                "ts_iso": "2026-10-14T00:00:00+0800", "event": "call",
                "stage": stage, "model": "fixture", "thread_id": "seed",
                "task": task, "batch_id": batch_id, "seq": i,
                "global_used_after": i, "batch_approved": 99,
                "batch_used_after": j, "ok": True, "err": None,
                "approval_ref": f"quota/approvals/{batch_id}.json",
                "evidence": None,
            }, ensure_ascii=False) + "\n")


def _seed_blocked(path, n):
    """预置 n 行 preflight_blocked（seq=null，不计数）。"""
    with open(path, "a", encoding="utf-8") as fh:
        for _ in range(n):
            fh.write(json.dumps({"ts_iso": "2026-10-14T00:00:00+0800",
                                 "event": "preflight_blocked", "stage": STAGE,
                                 "task": 2, "batch_id": BATCH, "seq": None,
                                 "ok": False,
                                 "err": "preflight_blocked: fixture"},
                                ensure_ascii=False) + "\n")


def test_15_credential_mismatch_blocks_without_persist(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMPRAXIS_PHASE1_ROOT", str(tmp_path / "phase1"))
    monkeypatch.setenv("SIMPRAXIS_GATEWAY_STATE", str(tmp_path / "gwstate"))
    ql_mod.write_approval(batch_id="T2-OTHER", task=4, stage="coding_debug",
                          approved=8, beyond_normal=False, note="fixture 他任务凭据")
    led = QuotaLedger()
    with pytest.raises(GatewayPausedError) as ei:
        led.preflight(STAGE, task=2, batch_id="T2-OTHER")
    assert ei.value.state["reason"] == "batch_approval_mismatch"
    assert ei.value.state.get("persisted") is False
    assert not (tmp_path / "gwstate/quota_pause.json").exists()


def test_16_batch_exhausted_writes_v2_superset_pause(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMPRAXIS_PHASE1_ROOT", str(tmp_path / "phase1"))
    monkeypatch.setenv("SIMPRAXIS_GATEWAY_STATE", str(tmp_path / "gwstate"))
    ql_mod.write_approval(batch_id=BATCH, task=2, stage=STAGE, approved=2,
                          beyond_normal=False, note="fixture 小批")
    led = QuotaLedger()
    lp = tmp_path / "phase1/quota/ledger.jsonl"
    _seed_calls(lp, 2)
    with pytest.raises(GatewayPausedError) as ei:
        led.preflight(STAGE, task=2, batch_id=BATCH)
    st = ei.value.state
    assert st["reason"] == "batch_budget_reached"
    assert PAUSE_KEYS_V1 <= set(st) and st["schema_version"] == 2
    assert st["resume_at_epoch"] is None and st["affected_stages"] == ["*"]
    # 落盘文件同构（router.is_paused 零改动可读）
    import orchestrator.gateway.router as router
    assert router.is_paused(STAGE) is not None


def test_17_normal36_without_beyond_blocks(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMPRAXIS_PHASE1_ROOT", str(tmp_path / "phase1"))
    monkeypatch.setenv("SIMPRAXIS_GATEWAY_STATE", str(tmp_path / "gwstate"))
    ql_mod.write_approval(batch_id="T2-N", task=2, stage=STAGE, approved=12,
                          beyond_normal=False, note="fixture 常态批 36 起点")
    led = QuotaLedger()
    _seed_calls(tmp_path / "phase1/quota/ledger.jsonl", 36, task=1,
                batch_id="T2-OLD")  # 无 cap 的任务行，专测 36 常态门
    with pytest.raises(GatewayPausedError) as ei:
        led.preflight(STAGE, task=2, batch_id="T2-N")
    assert ei.value.state["reason"] == "batch_budget_reached"


def test_18_beyond_normal_unlocks_36_to_48(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMPRAXIS_PHASE1_ROOT", str(tmp_path / "phase1"))
    monkeypatch.setenv("SIMPRAXIS_GATEWAY_STATE", str(tmp_path / "gwstate"))
    _seed_calls(tmp_path / "phase1/quota/ledger.jsonl", 36, task=1,
                batch_id="T2-OLD")
    # 36 起点可开 beyond 批；开到 48 硬门
    ql_mod.write_approval(batch_id="T2-BN", task=2, stage=STAGE, approved=12,
                          beyond_normal=True, note="fixture 36-48 书面批")
    led = QuotaLedger()
    info = led.preflight(STAGE, task=2, batch_id="T2-BN")
    assert info["global_used"] == 36
    _seed_calls(tmp_path / "phase1/quota/ledger.jsonl", 12, task=1,
                batch_id="T2-BN", offset=36)
    with pytest.raises(GatewayPausedError) as ei:
        led.preflight(STAGE, task=2, batch_id="T2-BN")
    assert ei.value.state["reason"] == "global_hard_cap"


def test_19_task_hard_cap_blocks_at_14(tmp_path, monkeypatch):
    # 锁内适配（2026-10-14 用户书面签收）：Task2 cap 10→14
    monkeypatch.setenv("SIMPRAXIS_PHASE1_ROOT", str(tmp_path / "phase1"))
    monkeypatch.setenv("SIMPRAXIS_GATEWAY_STATE", str(tmp_path / "gwstate"))
    ql_mod.write_approval(batch_id=BATCH, task=2, stage=STAGE, approved=20,
                          beyond_normal=False, note="fixture 大批")
    led = QuotaLedger()
    lp = tmp_path / "phase1/quota/ledger.jsonl"
    _seed_calls(lp, 13)
    assert led.preflight(STAGE, task=2, batch_id=BATCH)["task_used"] == 13
    _seed_calls(lp, 1)
    with pytest.raises(GatewayPausedError) as ei:
        led.preflight(STAGE, task=2, batch_id=BATCH)
    # G-11：任务局部硬顶独立 reason（不再混入 global_hard_cap），暂停件只挂本 stage
    assert ei.value.state["reason"] == "task_hard_cap"
    assert ei.value.state["stage"] == STAGE
    assert ei.value.state["scope"] == "task"
    assert ei.value.state["affected_stages"] == [STAGE]
    assert ei.value.state["trigger"]["task_used"] == 14


def test_20_blocked_rows_do_not_consume_seq(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMPRAXIS_PHASE1_ROOT", str(tmp_path / "phase1"))
    monkeypatch.setenv("SIMPRAXIS_GATEWAY_STATE", str(tmp_path / "gwstate"))
    ql_mod.write_approval(batch_id=BATCH, task=2, stage=STAGE, approved=8,
                          beyond_normal=False, note="fixture")
    led = QuotaLedger()
    lp = tmp_path / "phase1/quota/ledger.jsonl"
    _seed_calls(lp, 1)
    _seed_blocked(lp, 3)
    led.record(STAGE, task=2, batch_id=BATCH, ok=True, model="fixture",
               thread_id="fx20")
    rows = [json.loads(l) for l in lp.read_text().splitlines()]
    ints = [r["seq"] for r in rows if isinstance(r.get("seq"), int)]
    assert ints == [1, 2]


def test_21_corrupt_credential_is_quota_error(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMPRAXIS_PHASE1_ROOT", str(tmp_path / "phase1"))
    monkeypatch.setenv("SIMPRAXIS_GATEWAY_STATE", str(tmp_path / "gwstate"))
    d = tmp_path / "phase1/quota/approvals"
    d.mkdir(parents=True)
    (d / "T2-BAD.json").write_text("{不是合法JSON", encoding="utf-8")
    led = QuotaLedger()
    with pytest.raises(GatewayPausedError):
        led.preflight(STAGE, task=2, batch_id="T2-BAD")
    rows = [json.loads(l) for l in
            (tmp_path / "phase1/quota/ledger.jsonl").read_text().splitlines()]
    assert rows and rows[0]["event"] == "preflight_blocked"


# ---------- 评审 M1/M2 ----------

def test_22_no_gate_bypass_stamps_canonical_file(isolated, tmp_path):
    slugs = isolated["slugs"]
    s = make_setter(isolated, [good_proposals_raw(slugs[:2])])
    out = s.run_direction(track="track2", payload=isolated["payload"],
                          thread_id="fx22", batch_id=BATCH, bypass_stamp=True)
    assert out["gate_bypassed_locally"] is True
    on_disk = json.loads((isolated["tmp"] /
        "phase1/task2_direction/proposals_fx22_r0.json").read_text())
    assert on_disk["gate_bypassed_locally"] is True
    req = json.loads((isolated["tmp"] /
        "phase1/task2_direction/macro_fx22_r0_a0.req.json").read_text())
    assert req["gate_bypassed_locally"] is True


def test_23_reject_with_empty_notes_human_failure_no_extra_call(isolated):
    slugs = isolated["slugs"]
    s = make_setter(isolated, [good_proposals_raw(slugs[:2])])
    feishu = FakeFeishu([{"decision": "reject_with_comments", "choice": None,
                          "human_notes": "   ", "source": "p2p"}])
    out = s.run_gate1_loop(feishu, track="track2", payload=isolated["payload"],
                           thread_id="fx23", batch_id=BATCH)
    assert out["status"] == "human_failure"
    assert len(s.gateway.calls) == 1          # 空意见不得触发第 2 次付费 macro


def test_24_unknown_decision_value_raises(isolated):
    slugs = isolated["slugs"]
    s = make_setter(isolated, [good_proposals_raw(slugs[:2])])
    feishu = FakeFeishu([{"decision": "maybe", "human_notes": "", "source": "x"}])
    with pytest.raises(DirectionError, match="决策值非法"):
        s.run_gate1_loop(feishu, track="track2", payload=isolated["payload"],
                         thread_id="fx24", batch_id=BATCH)
    assert len(s.gateway.calls) == 1


def test_25_artifacts_list_and_string_normalization(isolated):
    """锁内适配（2026-10-14 真实 macro 两轮实测）：expected_artifacts 规范为
    非空文件名数组；字符串单值容忍归一化；空/含空项/非字符串数组拒绝。"""
    hits = FakeVector().hits
    base = json.loads(good_proposals_raw(isolated["slugs"][:2]))
    from orchestrator.direction.setter import _normalize_artifacts
    assert _normalize_artifacts(["a.json", "b.mp4"]) == ["a.json", "b.mp4"]
    assert _normalize_artifacts("x.json") == ["x.json"]
    assert _normalize_artifacts([]) is None
    assert _normalize_artifacts(["ok", " "]) is None
    assert _normalize_artifacts(123) is None
    # 端到端：字符串形态仍可通过校验并归一化为数组
    base["proposals"][1]["expected_artifacts"] = "single.md"
    props, errors = validate_proposals(base, hits=hits, catalog=isolated["catalog"])
    assert not errors, errors
    assert props[1]["expected_artifacts"] == ["single.md"]


# ---------- G-5：thread_id 字符集白名单（路径遍历/覆盖防护） ----------

@pytest.mark.parametrize("bad_thread", [
    "../x",          # 路径遍历
    "a/b",           # 斜杠路径段
    "a b",           # 空格
    "x" * 65,        # 超 64 长度
    "fx$",           # 白名单外字符
])
def test_26_run_direction_rejects_bad_thread_id(isolated, bad_thread):
    # raws=[]：若校验没拦在最前，FakeGateway 会抛 AssertionError（脚本耗尽）
    s = make_setter(isolated, [])
    with pytest.raises(ValueError, match="thread_id"):
        s.run_direction(track="track2", payload=isolated["payload"],
                        thread_id=bad_thread, batch_id=BATCH)
    assert s.gateway.calls == []  # 非法 thread 必须死在 KB 查询/实呼之前


def test_27_cli_rejects_bad_thread_id_exit_2(tmp_path, monkeypatch, capsys):
    from orchestrator.direction import cli as dir_cli
    monkeypatch.setenv("SIMPRAXIS_PHASE1_ROOT", str(tmp_path / "phase1"))
    monkeypatch.setenv("SIMPRAXIS_GATEWAY_STATE", str(tmp_path / "gwstate"))
    rc = dir_cli.main(["--thread", "../escape", "--track", "track2",
                       "--index", "1", "--batch-id", "T2-G5"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "非法 --thread" in err
