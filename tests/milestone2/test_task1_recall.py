#!/usr/bin/env python3
"""Phase 2 Task 1 — 生产级经验召回中枢（计划 L370-398，全部离线 0 付费）。

五测映射（计划零付费测试清单）：
  1 test_recall_hits_real_learning_row        真 embed 语义检索命中真实学习行
  2 test_adoption_counter_no_fake_hit         未渲染 0 采纳；逐字渲染计 1，行六字段
  3 test_repair_fragments_only_from_learning_tables
                                              补丁片段只来自学习表真实行，0 命中不编造
  4 test_macro_callreplay_kill9_zero_rebill   macro 重放 0 二次实呼/0 证据覆盖
    test_setter_cache_put_after_resp_evidence
                                              QA-2 M2：put 为最后 commit marker
                                              （resp→put→采纳），kill 后 miss
                                              安全重付且证据补齐
  5 test_phase1_table_not_written (+live)     召回全过程学习表行数恒等（tmp=1/活库=5）
  6 test_setter_call_failure_zero_adoption /
    test_runner_call_failure_zero_adoption    QA-1 M1：call 未成功（发网前失败
                                              同构）0 采纳行、jsonl 不出生

纪律：全部临时 LanceDB + 断网 env（mpnet 走 /mnt/caches 本地缓存）；不向活
phase1_learnings/kb_chunks 写任何内容（活库版打 kb_integration，默认 skip）。
"""
from __future__ import annotations

import hashlib
import json
import stat
import sys
from pathlib import Path

import pytest

from memory_flywheel.phase1.learning_store import TABLE_NAME, LearningStore
from memory_flywheel.recall import (
    LOG_FIELDS,
    ProductionRecall,
    adopted,
    count_adoptions,
    prompt_embedded_text,
)
from orchestrator.agent_runner.runner import AgentRunner, RunSpec
from orchestrator.common.callcache import CallReplay
from orchestrator.common.clock import parse_ts_iso_strict
from orchestrator.direction.setter import (
    DirectionSetter,
    build_first_messages,
    load_engine_catalog,
)
from tests.kb_support import require_kb_integration

FAIL_SCRIPT = '''#!/usr/bin/env python3
import pathlib, sys
d = pathlib.Path(__file__).parent
fix = (d / "fix.txt").read_text(encoding="utf-8").strip()
if fix != "PATCHED":
    print("Traceback (most recent call last):", file=sys.stderr)
    print(f'  File "{d / "script.py"}", line 8, in <module>', file=sys.stderr)
    print("RuntimeError: 仿真失败 EGL headless render libEGL 初始化失败",
          file=sys.stderr)
    sys.exit(1)
(d / "telemetry.json").write_text('{"status": "ok"}', encoding="utf-8")
'''

PATCH_JSON = json.dumps({"patches": [{
    "path": "fix.txt", "old": "NEEDS_FIX", "new": "PATCHED"}]},
    ensure_ascii=False)

SYMPTOM = ("无头容器内 MuJoCo EGL 渲染失败：libEGL 无法加载设备、屏幕初始化"
           "报错，仿真进程退出码非零")
FIX = ("安装 libegl1/libgl1 并导出 MUJOCO_GL=egl；确认 NVIDIA 驱动与"
       "LD_LIBRARY_PATH；用 EGL 设备探针验证 /dev/dri 在场")


@pytest.fixture(autouse=True)
def _offline_env(monkeypatch):
    """断网：fastembed 任何下载尝试都应直接失败（mpnet 已在本地缓存）。"""
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    monkeypatch.setenv("HF_DATASETS_OFFLINE", "1")


# ---------- 合成件 ----------

class FakeVector:
    """回放合成 KB 命中；记录 namespace/top_k（不做任何 embed）。"""

    def __init__(self, *, hits=None, namespace="capabilities", top_k=8):
        if hits is not None:
            self.hits = hits
        elif namespace == "capabilities":
            self.hits = [{
                "id": f"cap{i}", "text": f"合成能力证据第 {i} 行：物理引擎接触模型。",
                "kb_relpath": f"knowledge/cap_{i}.md",
                "section_path": f"章节{i}", "doc_type": "guide"}
                for i in range(1, top_k + 1)]
        else:
            self.hits = [{
                "id": f"tb{i}",
                "text": f"夹具排障知识条目 {i}（字段名必须是 text，无 fix 字段）",
                "evidence_refs": [f"E{i}"],
                "kb_relpath": "knowledge/projects/fixture/troubleshooting.md",
                "section_path": f"s{i}"}
                for i in range(1, top_k + 1)]
        self.queries = []

    def query_kb(self, query, *, namespace, top_k, **kw):
        self.queries.append({"q": query, "namespace": namespace, "top_k": top_k})
        return [dict(h) for h in self.hits]


def _new_store(tmp_path: Path) -> LearningStore:
    return LearningStore(uri=str(tmp_path / "lance"),
                         evidence_root=tmp_path / "phase1",
                         runs_evidence_root=tmp_path / "runs")


def _add_real_row(store: LearningStore, tmp_path: Path) -> dict:
    """写 1 条真实形状失败-修复学习行（先 touch 证据；无证据不写）。"""
    ev = tmp_path / "runs" / "phase1" / "w1" / "telemetry.json"
    ev.parent.mkdir(parents=True, exist_ok=True)
    ev.write_text('{"status": "failed-then-fixed"}', encoding="utf-8")
    rec = store.add_learning(
        run_id="t1-egl-20261017", track="track1", rung=0,
        sim_engine="mujoco", gate_source="human_failure_agent_run",
        evidence_relpath="@runs/phase1/w1/telemetry.json",
        failure_symptom=SYMPTOM, fix_action=FIX,
        human_opinion="真人点验：EGL 环境变量缺失所致", effective=False)
    return rec


def _workdir(tmp_path: Path) -> Path:
    wd = tmp_path / "work"
    wd.mkdir(parents=True)
    (wd / "script.py").write_text(FAIL_SCRIPT, encoding="utf-8")
    (wd / "fix.txt").write_text("NEEDS_FIX", encoding="utf-8")
    return wd


class CodingFakeGateway:
    def __init__(self, response=PATCH_JSON, usage=(10, 20)):
        self.calls = []
        self.response = response
        self.usage = usage

    def model_id(self, stage):
        return "fixture-coding-redacted-0001"

    def call(self, stage, messages, **kw):
        for mm in messages:
            assert isinstance(mm.get("content"), str)  # 硬规则1：纯文本
        self.calls.append({"stage": stage, "messages": messages, "kw": kw})
        return self.response, self.usage


class DirectionFakeGateway:
    def __init__(self, raw, usage=(100, 200)):
        self.calls = []
        self.raw = raw
        self.usage = usage

    def model_id(self, stage):
        return "fixture-macro-model"

    def call(self, stage, messages, *, max_tokens, temperature, **kw):
        self.calls.append({"stage": stage, "messages": messages})
        return self.raw, self.usage


def _valid_proposals_raw() -> str:
    slugs = sorted(load_engine_catalog())[:2]
    return json.dumps({
        "question": "合成题目",
        "proposals": [{
            "name": f"方案{chr(65 + i)}", "sim_engine": slug,
            "first_principles": f"第一性原理接触模型推导 [S1]（候选 {i+1}）。",
            "key_params": "时间步长 0.5ms；摩擦系数 0.3",
            "controls_vs_experiment": "Baseline vs Ours 参数鲁棒性对照",
            "evidence_refs": ["S1"],
            "risk": "接触穿透；最小闭环探针验证",
            "expected_artifacts": [f"probe_{i}.mp4", f"metrics_{i}.json"]}
            for i, slug in enumerate(slugs)]}, ensure_ascii=False)


# ---------- 1. 真实学习行语义召回（禁止硬编码 id 命中） ----------

def test_recall_hits_real_learning_row(tmp_path):
    store = _new_store(tmp_path)
    rec = _add_real_row(store, tmp_path)
    # 额外 store：空 uri 且从不建表——缺表该路 []，不报错不建表（Task5 接缝）
    absent = _new_store(tmp_path / "absent_slot")
    fv = FakeVector(namespace="capabilities", top_k=8)
    recall = ProductionRecall(store, fv, extra_learning_stores=(absent,))

    ret = recall.retrieve("MuJoCo 容器里 EGL 无头渲染失败 libEGL 报错怎么办",
                          site="direction")
    # kb 路：同参透传（namespace/top_k 与 Phase 1 setter 常量逐字一致）
    assert fv.queries == [{"q": "MuJoCo 容器里 EGL 无头渲染失败 libEGL 报错怎么办",
                           "namespace": "capabilities", "top_k": 8}]
    assert [h["id"] for h in ret["kb_hits"]] == [f"cap{i}" for i in range(1, 9)]
    # learning 路：命中 id 必须来自 add_learning 返回值（禁止硬编码字面量命中）
    assert [h["id"] for h in ret["learning_hits"]] == [rec["id"]]
    hit = ret["learning_hits"][0]
    # 真实行全部字段原样保留（12 列减去弹出的 vector = 11），不增造业务字段；
    # _distance 是 lancedb 向量检索自身带出的距离元数据（LearningStore.query
    # 既有契约即如此，非本模块增造）。
    assert set(hit) == {
        "id", "text", "namespace", "layer", "run_id", "track", "rung",
        "sim_engine", "gate_source", "evidence_relpath", "created_at",
        "_distance"}
    assert "vector" not in hit and hit["run_id"] == "t1-egl-20261017"
    assert "修复动作" in hit["text"] and FIX in hit["text"]
    # 缺表额外库没有被探测动作建表
    assert absent.db.table_names() == []

    # coding 站：namespace/top_k 切到 troubleshooting/6
    fv2 = FakeVector(namespace="troubleshooting", top_k=6)
    recall2 = ProductionRecall(store, fv2)
    ret2 = recall2.retrieve("仿真失败 EGL render", site="coding")
    assert fv2.queries[0]["namespace"] == "troubleshooting"
    assert fv2.queries[0]["top_k"] == 6
    assert [h["id"] for h in ret2["learning_hits"]] == [rec["id"]]

    # site/hint 严校验
    with pytest.raises(ValueError, match="site"):
        recall.retrieve("x", site="bogus")
    with pytest.raises(ValueError):
        recall.retrieve("  ", site="direction")


# ---------- 2. 采纳计数：不可伪造 + 六字段对账 ----------

def test_adoption_counter_no_fake_hit(tmp_path):
    store = _new_store(tmp_path)
    rec = _add_real_row(store, tmp_path)
    row = store.query(SYMPTOM, top_k=1)[0]
    assert row["id"] == rec["id"]
    log = tmp_path / "phase2" / "recall" / "production_recall.jsonl"
    recall = ProductionRecall(store, FakeVector())

    embedded = prompt_embedded_text(row["text"])
    # 未渲染学习段：纯函数判否、不落行、文件不出生
    fake_prompt = json.dumps([{"role": "user", "content": "与避坑行无关的 prompt"}],
                             ensure_ascii=False)
    assert adopted(fake_prompt, embedded) is False
    assert recall.record_adoption(
        thread_id="t1adopt", site="direction", learning_id=row["id"],
        prompt_text=fake_prompt, learning_text=embedded, path=log) is False
    assert count_adoptions(log) == 0 and not log.exists()

    # 经真实 builder 逐字渲染进 prompt（与 setter 同一 JSON 规范 sort_keys=False）
    messages = build_first_messages(
        track="track2", payload={"question": "EGL 无头渲染实证"},
        hits=FakeVector().hits, catalog=load_engine_catalog(),
        learning_hits=[row])
    prompt_text = json.dumps(messages, ensure_ascii=False, sort_keys=False)
    assert row["text"] in messages[1]["content"]       # content 内逐字原文
    assert adopted(prompt_text, embedded) is True
    assert recall.record_adoption(
        thread_id="t1adopt", site="direction", learning_id=row["id"],
        prompt_text=prompt_text, learning_text=embedded, path=log) is True
    assert count_adoptions(log) == 1

    # 行 schema 恰好六字段，两 sha 可独立复算
    line = json.loads(log.read_text(encoding="utf-8").strip())
    assert tuple(line.keys()) == LOG_FIELDS
    parse_ts_iso_strict(line["ts"])
    assert line == {**line, "thread_id": "t1adopt", "site": "direction",
                    "learning_id": rec["id"]}
    assert line["prompt_sha256"] == hashlib.sha256(
        prompt_text.encode("utf-8")).hexdigest()
    assert line["adopted_text_sha256"] == hashlib.sha256(
        embedded.encode("utf-8")).hexdigest()
    assert stat.S_IMODE(log.stat().st_mode) == 0o600
    assert stat.S_IMODE(log.parent.stat().st_mode) == 0o700

    # 非法 site / 非法 thread_id fail-closed
    with pytest.raises(ValueError, match="site"):
        recall.record_adoption(thread_id="t1adopt", site="x",
                               learning_id=row["id"], prompt_text=prompt_text,
                               learning_text=embedded, path=log)
    with pytest.raises(ValueError, match="thread_id"):
        recall.record_adoption(thread_id="../escape", site="direction",
                               learning_id=row["id"], prompt_text=prompt_text,
                               learning_text=embedded, path=log)
    assert count_adoptions(log) == 1  # 拒写不产生残行


def test_adopted_pure_function_exact_substring_only():
    assert adopted("abc 学习行原文 xyz", "学习行原文") is True
    assert adopted("abc", "") is False
    # 同义改写/模糊包含不算采纳（不可伪造）
    assert adopted("这里只是大致提到了 EGL 的修复思路", "EGL 导出 MUJOCO_GL=egl") is False
    with pytest.raises(ValueError):
        adopted(123, "x")  # type: ignore[arg-type]


def test_count_adoptions_corrupt_line_fail_closed(tmp_path):
    log = tmp_path / "recall.jsonl"
    log.write_text('{"ts": "2026-10-17T00:00:00Z"}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="schema"):
        count_adoptions(log)
    log.write_text("not-json\n", encoding="utf-8")
    with pytest.raises(ValueError, match="损坏"):
        count_adoptions(log)


# ---------- 3. 补丁片段只能来自学习表真实行（runner 端到端，0 付费 fake） ----------

def test_repair_fragments_only_from_learning_tables(tmp_path):
    # 3a. learning 路 0 命中：prompt 不得出现避坑段/修复动作/编造的 learning 引用
    empty_store = _new_store(tmp_path / "empty")
    fv0 = FakeVector(namespace="troubleshooting", top_k=6)
    runner0 = AgentRunner(
        CodingFakeGateway(), fv0, evidence_root=tmp_path / "ev0",
        recall=ProductionRecall(empty_store, fv0))
    wd0 = _workdir(tmp_path / "run0")
    rec0 = runner0.run(RunSpec(
        thread_id="t1nolearn", workdir=wd0,
        cmd=[sys.executable, str(wd0 / "script.py")],
        telemetry_relpath="telemetry.json", batch_id="T1-NL"))
    assert rec0["status"] == "ok" and rec0["coding_round"] == 1
    sent0 = runner0.gateway.calls[0]["messages"][1]["content"]
    assert "实战演化避坑" not in sent0
    assert "learning_id=" not in sent0
    assert "修复动作" not in sent0          # kb 行无 fix 字段，不得编造
    assert "夹具排障知识条目 1" in sent0    # 真实 kb 命中照常渲染
    assert "E1" in sent0
    assert count_adoptions() == 0

    # 3b. learning 路真命中：避坑段逐字引用真实行，采纳计数 1（site=coding）
    store = _new_store(tmp_path / "learned")
    rec = _add_real_row(store, tmp_path / "learned")
    fv1 = FakeVector(namespace="troubleshooting", top_k=6)
    gw1 = CodingFakeGateway()
    runner1 = AgentRunner(
        gw1, fv1, evidence_root=tmp_path / "ev1",
        recall=ProductionRecall(store, fv1))
    wd1 = _workdir(tmp_path / "run1")
    rec1 = runner1.run(RunSpec(
        thread_id="t1learn", workdir=wd1,
        cmd=[sys.executable, str(wd1 / "script.py")],
        telemetry_relpath="telemetry.json", batch_id="T1-L"))
    assert rec1["status"] == "ok" and len(gw1.calls) == 1
    sent1 = gw1.calls[0]["messages"][1]["content"]
    assert "实战演化避坑" in sent1
    assert f"learning_id={rec['id']}" in sent1
    assert FIX in sent1 and SYMPTOM in sent1       # 真实行 text 逐字在 prompt
    assert count_adoptions() == 1
    line = json.loads(ProductionRecall.telemetry_path()
                      .read_text(encoding="utf-8").strip())
    assert line["site"] == "coding" and line["learning_id"] == rec["id"]
    assert line["thread_id"] == "t1learn"
    # kb 路仍是 troubleshooting 同参查询（语义未被 learning 路改变）
    assert fv1.queries[0]["namespace"] == "troubleshooting"
    assert fv1.queries[0]["top_k"] == 6

    # 3c. setter 侧默认（learning_hits=None）prompt 无避坑段（旧行为零变化）
    msgs = build_first_messages(
        track="track2", payload={"question": "q"},
        hits=FakeVector().hits, catalog=load_engine_catalog())
    assert "实战演化避坑" not in msgs[1]["content"]


# ---------- 4. macro CallReplay：kill-9 节点重放 0 二次计费/0 证据覆盖 ----------

def test_macro_callreplay_kill9_zero_rebill(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMPRAXIS_PHASE1_ROOT", str(tmp_path / "phase1"))
    monkeypatch.setenv("SIMPRAXIS_GATEWAY_STATE", str(tmp_path / "gwstate"))
    import orchestrator.direction.setter as setter_mod
    set_evidence_calls = []
    real_set = setter_mod.set_current_evidence
    monkeypatch.setattr(setter_mod, "set_current_evidence",
                        lambda ref: set_evidence_calls.append(ref) or real_set(ref))

    cache_dir = tmp_path / "cache"
    cache = CallReplay(cache_dir)
    gw = DirectionFakeGateway(_valid_proposals_raw())
    fv = FakeVector(namespace="capabilities", top_k=8)
    payload = {"question": "合成题目：接触模型最小闭环探针"}

    def _setter():
        return DirectionSetter(gw, fv, call_cache=cache)

    r1 = _setter().run_direction(
        track="track2", payload=payload, thread_id="t1replay",
        batch_id="T1-REPLAY", gate_round=0)
    assert len(gw.calls) == 1
    assert r1["attempts"][0]["replay"] is False
    cache_file = cache_dir / "macro_t1replay_r0_a0.json"
    assert cache_file.is_file()
    cached = json.loads(cache_file.read_text(encoding="utf-8"))
    assert cached["kind"] == "macro_ideation" and cached["usage"] == [100, 200]

    ev_dir = tmp_path / "phase1" / "task2_direction"
    req_p = ev_dir / "macro_t1replay_r0_a0.req.json"
    resp_p = ev_dir / "macro_t1replay_r0_a0.resp.json"
    frozen = {p: (p.read_bytes(), p.stat().st_mtime_ns)
              for p in (req_p, resp_p)}
    assert len(set_evidence_calls) == 1

    # 新进程语义：重新构造 setter（注入同一缓存），同参整节点重放
    r2 = _setter().run_direction(
        track="track2", payload=payload, thread_id="t1replay",
        batch_id="T1-REPLAY", gate_round=0)
    assert len(gw.calls) == 1                  # 0 二次实呼
    assert len(set_evidence_calls) == 1        # 重放不挂 billing_context 证据
    assert r2["attempts"][0]["replay"] is True
    assert r2["attempts"][0]["req"] is None
    assert r2["attempts"][0]["resp"] is None
    assert [p["name"] for p in r2["proposals"]] == \
           [p["name"] for p in r1["proposals"]]
    # req/resp 证据字节与 mtime 均不被覆盖，目录仍只有首轮两件
    for p, (blob, mt) in frozen.items():
        assert p.read_bytes() == blob
        assert p.stat().st_mtime_ns == mt
    assert len(list(ev_dir.glob("macro_t1replay_r0_a0.*.json"))) == 2
    assert stat.S_IMODE(cache_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(cache_file.stat().st_mode) == 0o600


# ---------- 4b. QA-2 M2：put 是最后 commit marker（resp→put→采纳） ----------

def test_setter_cache_put_after_resp_evidence(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMPRAXIS_PHASE1_ROOT", str(tmp_path / "phase1"))
    monkeypatch.setenv("SIMPRAXIS_GATEWAY_STATE", str(tmp_path / "gwstate"))
    import orchestrator.direction.setter as setter_mod
    real_write = setter_mod._write_evidence

    class _KillOnResp:
        """注入 kill：写 kind="resp" 证据时抛 KeyboardInterrupt（req 正常写）。"""

        def __call__(self, stem, kind, payload):
            if kind == "resp":
                raise KeyboardInterrupt("注入 kill：resp 证据写入中断")
            return real_write(stem, kind, payload)

    # recall 接真实学习行：若采纳点仍排在 resp 之前，kill 后会留下采纳残行
    store = _new_store(tmp_path / "learned")
    rec = _add_real_row(store, tmp_path / "learned")
    fv = FakeVector(namespace="capabilities", top_k=8)
    recall = ProductionRecall(store, fv)

    cache_dir = tmp_path / "cache"
    cache = CallReplay(cache_dir)
    payload = {"question": "无头容器内 MuJoCo EGL 渲染失败 libEGL 报错怎么办"}
    gw1 = DirectionFakeGateway(_valid_proposals_raw(), usage=(100, 200))
    s1 = DirectionSetter(gw1, fv, evidence_writer=_KillOnResp(),
                         recall=recall, call_cache=cache)

    with pytest.raises(KeyboardInterrupt, match="注入 kill"):
        s1.run_direction(track="track2", payload=payload,
                         thread_id="t1m2", batch_id="T1-M2", gate_round=0)
    assert len(gw1.calls) == 1
    cache_file = cache_dir / "macro_t1m2_r0_a0.json"
    ev_dir = tmp_path / "phase1" / "task2_direction"
    # ① put 尚未发生：缓存文件不存在
    assert not cache_file.is_file()
    # ② req 证据存在、resp 不存在
    assert (ev_dir / "macro_t1m2_r0_a0.req.json").is_file()
    assert not (ev_dir / "macro_t1m2_r0_a0.resp.json").is_file()
    # ③ 采纳也排在 resp 之后：kill 在 resp 处 → 0 采纳行、jsonl 不出生
    assert count_adoptions() == 0
    assert not ProductionRecall.telemetry_path().exists()

    # 新进程语义：新 setter（不再注入故障 writer，同一 cache/recall）同参
    # 重放 → miss 安全重付 1 次真实 call、req/resp 两件齐备、proposals 正常
    gw2 = DirectionFakeGateway(_valid_proposals_raw(), usage=(100, 200))
    s2 = DirectionSetter(gw2, fv, recall=recall, call_cache=cache)
    r2 = s2.run_direction(track="track2", payload=payload,
                          thread_id="t1m2", batch_id="T1-M2", gate_round=0)
    assert len(gw2.calls) == 1
    assert r2["attempts"][0]["replay"] is False
    assert (ev_dir / "macro_t1m2_r0_a0.req.json").is_file()
    assert (ev_dir / "macro_t1m2_r0_a0.resp.json").is_file()
    assert len(r2["proposals"]) == 2
    assert cache_file.is_file()  # resp 补齐后缓存才落（最后 commit marker）
    cached = json.loads(cache_file.read_text(encoding="utf-8"))
    assert cached["kind"] == "macro_ideation" and cached["usage"] == [100, 200]
    # 成功重付走完 resp→put→采纳：恰好 1 条真实学习行采纳
    assert count_adoptions() == 1
    line = json.loads(ProductionRecall.telemetry_path()
                      .read_text(encoding="utf-8").strip())
    assert line["site"] == "direction" and line["learning_id"] == rec["id"]


# ---------- 6. QA-1 M1：call 未成功（发网前失败同构）0 采纳行 ----------

class _RaisingDirectionGateway(DirectionFakeGateway):
    """call 即抛：同构发网前失败（StageUnresolved/入口熔断/缺凭据/image 守卫）。"""

    def call(self, stage, messages, *, max_tokens, temperature, **kw):
        self.calls.append({"stage": stage, "messages": messages})
        raise RuntimeError("发网前失败同构：入口熔断/缺凭据，无整数 ledger 行")


class _RaisingCodingGateway(CodingFakeGateway):
    def call(self, stage, messages, **kw):
        self.calls.append({"stage": stage})
        raise RuntimeError("发网前失败同构：入口熔断/缺凭据，无整数 ledger 行")


def test_setter_call_failure_zero_adoption(tmp_path, monkeypatch):
    # 证据/网关状态全部落 tmp（不碰真 phase1 root 与真 phase2 遥测）
    monkeypatch.setenv("SIMPRAXIS_PHASE1_ROOT", str(tmp_path / "phase1"))
    monkeypatch.setenv("SIMPRAXIS_GATEWAY_STATE", str(tmp_path / "gwstate"))
    store = _new_store(tmp_path / "learned")
    rec = _add_real_row(store, tmp_path / "learned")
    fv = FakeVector(namespace="capabilities", top_k=8)
    recall = ProductionRecall(store, fv)
    gw = _RaisingDirectionGateway(_valid_proposals_raw())
    setter = DirectionSetter(
        gw, fv, recall=recall, call_cache=CallReplay(tmp_path / "cache"))

    # 学习行确会逐字渲染进待发 prompt（否则本测无防错意义）
    pre = build_first_messages(
        track="track2", payload={"question": "EGL 无头渲染失败如何修"},
        hits=fv.hits, catalog=load_engine_catalog(),
        learning_hits=store.query(SYMPTOM, top_k=1))
    assert f"learning_id={rec['id']}" in pre[1]["content"]

    with pytest.raises(RuntimeError, match="发网前失败"):
        setter.run_direction(
            track="track2", payload={"question": "EGL 无头渲染失败如何修"},
            thread_id="t1m1dir", batch_id="T1-M1D", gate_round=0)
    assert len(gw.calls) == 1
    # call 未成功：0 采纳行、jsonl 不出生
    assert count_adoptions() == 0
    assert not ProductionRecall.telemetry_path().exists()
    # 异常路径不入缓存（按 miss 安全重付）
    assert list((tmp_path / "cache").glob("*.json")) == []


def test_runner_call_failure_zero_adoption(tmp_path):
    store = _new_store(tmp_path / "learned")
    rec = _add_real_row(store, tmp_path / "learned")
    fv = FakeVector(namespace="troubleshooting", top_k=6)
    recall = ProductionRecall(store, fv)
    gw = _RaisingCodingGateway()
    runner = AgentRunner(
        gw, fv, evidence_root=tmp_path / "ev", recall=recall)
    wd = _workdir(tmp_path / "run")

    # 契约：coding 站 gateway 异常不经 run() 吞掉，原样向上抛
    with pytest.raises(RuntimeError, match="发网前失败"):
        runner.run(RunSpec(
            thread_id="t1m1code", workdir=wd,
            cmd=[sys.executable, str(wd / "script.py")],
            telemetry_relpath="telemetry.json", batch_id="T1-M1C"))
    assert len(gw.calls) == 1
    assert rec["id"]  # 学习行存在且被召回（未成功发送不产生采纳）
    assert count_adoptions() == 0
    assert not ProductionRecall.telemetry_path().exists()


# ---------- 5. 召回全过程不写学习表（tmp=1；活库 kb_integration=5/1542/82） ----------

def test_phase1_table_not_written(tmp_path):
    store = _new_store(tmp_path)
    rec = _add_real_row(store, tmp_path)
    absent = _new_store(tmp_path / "absent2")
    fv = FakeVector(namespace="capabilities", top_k=8)
    recall = ProductionRecall(store, fv, extra_learning_stores=(absent,))

    assert store.count() == 1
    ret = recall.retrieve(SYMPTOM, site="direction")
    assert [h["id"] for h in ret["learning_hits"]] == [rec["id"]]
    messages = build_first_messages(
        track="track2", payload={"question": "q"}, hits=ret["kb_hits"],
        catalog=load_engine_catalog(), learning_hits=ret["learning_hits"])
    prompt_text = json.dumps(messages, ensure_ascii=False, sort_keys=False)
    log = tmp_path / "recall.jsonl"
    for hit in ret["learning_hits"]:
        assert recall.record_adoption(
            thread_id="t1ro", site="direction", learning_id=hit["id"],
            prompt_text=prompt_text,
            learning_text=prompt_embedded_text(hit["text"]), path=log)
    # 全流程后：学习表仍 1 行、表集合不变、缺表额外库未被建表
    assert store.count() == 1
    assert store.db.table_names() == [TABLE_NAME]
    assert absent.db.table_names() == []
    assert count_adoptions(log) == 1


@pytest.mark.kb_integration
def test_live_phase1_table_not_written(tmp_path):
    """活库只读版（SIMPRAXIS_KB_INTEGRATION=1 才跑）：三表行数前后恒等。

    L4（QA-1）：采纳遥测显式 path=tmp（即使 SIMPRAXIS_ALLOW_PAID=1 时
    conftest 不重定向 PHASE2_ROOT，也绝不写穿真 /mnt phase2 账）。
    """
    require_kb_integration(data=True)
    import lancedb

    from memory_flywheel import kb_pins
    store = LearningStore()
    recall = ProductionRecall(store, _LiveVector())
    live_log = tmp_path / "live" / "recall.jsonl"

    uri = kb_pins.default_lancedb_uri()
    db = lancedb.connect(uri)

    def _counts():
        return {name: db.open_table(name).count_rows()
                for name in ("kb_chunks", TABLE_NAME, "solution_chunks")}

    before = _counts()
    assert before["kb_chunks"] == 1542
    assert before[TABLE_NAME] == 5
    assert before["solution_chunks"] == 82

    ret = recall.retrieve(
        "MuJoCo EGL 无头渲染 接触求解 仿真失败 修复", site="direction")
    messages = build_first_messages(
        track="track2", payload={"question": "活库只读召回对账"},
        hits=ret["kb_hits"], catalog=load_engine_catalog(),
        learning_hits=ret["learning_hits"] or None)
    prompt_text = json.dumps(messages, ensure_ascii=False, sort_keys=False)
    n_adopt = 0
    for hit in ret["learning_hits"]:
        if recall.record_adoption(
                thread_id="t1live-ro", site="direction",
                learning_id=hit["id"], prompt_text=prompt_text,
                learning_text=prompt_embedded_text(hit["text"]),
                path=live_log):
            n_adopt += 1
    # 遥测只落本测显式 tmp path（L4：ALLOW_PAID 下 conftest 不重定向也安全）；
    # 命中行全部逐字渲染故全部采纳（n 随 top_k 与真实近邻，不钉死字面量）。
    assert n_adopt == len(ret["learning_hits"]) and n_adopt >= 1
    assert count_adoptions(live_log) == n_adopt
    assert not ProductionRecall.telemetry_path().exists()
    after = _counts()
    assert after == before


class _LiveVector:
    """活库版用真实 VectorEngine（包一层避免模块级构造时连接 /mnt）。"""

    def __init__(self):
        from memory_flywheel.vector_engine import VectorEngine
        self._ve = VectorEngine()

    def query_kb(self, query, *, namespace, top_k, **kw):
        return self._ve.query_kb(query, namespace=namespace, top_k=top_k)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
