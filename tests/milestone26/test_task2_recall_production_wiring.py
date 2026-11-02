"""Phase 2.6 Task 2 生产级四路全量召回正式接线（0 付费、0 联网）。

**一条 AC 一个测试函数**：计划给的机判命令用 `-k "ac_t2_Nx"` 且期望 `1 passed`，
pytest 的 `-k` 是子串匹配，所以每个 AC id 只能出现在唯一一个用例名里。

覆盖：
  1. **AC-T2-1**：生产构造点（`build_direction_setter`）注入 `PlannerRecall` 后，
     LIBERO 形状请求的 macro prompt 同时含 `PLANNER_SECTION_KB/SOLUTIONS/
     LEARNINGS` 三段头（导入常量断言，不写字面量），且三段命中文本逐字在场。
  2. **AC-T2-2a**：遥测 jsonl 三条路由（kb/solutions/learnings）各至少一行，
     每行键序恰为 `LOG_FIELDS_V2`（七字段）。
  3. **AC-T2-2b**（kb_integration）：活库 `PlannerRecall.retrieve()` 在 LIBERO
     形状查询下返回行带三路 `source_route`，且 `03_libero` 方案块在 top-8 内；
     同处取活库四表前后快照，断言 1542/44/94/1 全程不变（AC-T2-3 的活库半）。
  4. **AC-T2-3**：接线全过程只经只读 API（fake 引擎/学习库把写路径断言掉），
     前后行数快照相等。
  5. **AC-T2-4a**：`--recall-mode dual` 时 prompt 与旧 `build_first_messages`
     **逐字节相等**，且不含方案实录段头（仓内自证，不引外部冻结件）。
  6. **AC-T2-4b**：同一组命中下 fourway 的 prompt 含三段头且 sha256 **≠** dual
     分支（接线被误删时 4a 仍绿，只有 4b 会红——双向闸）。
  7. **AC-T2-4c**：`--recall-mode` 的 argparse 声明与缺省值，以及「旗标 → 构造
     实参」这一跳（dual ⇒ `planner_recall is None`，fourway ⇒ `PlannerRecall`）。

AC-T2-4d 不是新用例：机判命令是原样跑 `tests/milestone25/test_task6_r4_live.py`
（3 passed），故本文件不得出现含 `ac_t2_4d` 的用例名。

纪律：合成件 + 断网 env；除证据件（计划明确要求落 phase26 生产根）外一律
tmp 落盘，绝不写活库/活账。
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

import pytest

from memory_flywheel.recall import (
    LOG_FIELDS_V2,
    SOURCE_ROUTE_KB,
    SOURCE_ROUTE_LEARNINGS,
    SOURCE_ROUTE_SOLUTIONS,
    ProductionRecall,
    iter_adoption_rows,
)
from orchestrator.common import runtime_paths as rp
from orchestrator.direction.planner_recall import (
    PRODUCTION_RULES_VERSION,
    TIER_PROBE,
    PlannerRecall,
)
from orchestrator.direction.setter import (
    PLANNER_SECTION_KB,
    PLANNER_SECTION_LEARNINGS,
    PLANNER_SECTION_SOLUTIONS,
    DirectionSetter,
    build_first_messages,
    load_engine_catalog,
)
from orchestrator.graph import cli as graph_cli
from tests.kb_support import require_kb_integration

REPO_ROOT = Path(__file__).resolve().parents[2]
# 生产 phase26 根在**模块导入时点**取一次：此时任何 fixture 的 monkeypatch 都还
# 没生效。AC 表要求证据件落 $ROOT/task9_milestone/，而用例自身把账根指 tmp。
PROD_P26_ROOT = rp.phase26_root()
EVIDENCE_DIR = PROD_P26_ROOT / "task9_milestone"

LIBERO_SOLUTION_ID = "track1_tools/03_libero"
LIBERO_PAYLOAD = {"name": "LIBERO 机械臂 VLA 闭环", "urls": []}


@pytest.fixture(autouse=True)
def _offline_env(monkeypatch):
    """断网：任何 embedding 下载尝试都应直接失败（mpnet 已在本地缓存）。"""
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    monkeypatch.setenv("HF_DATASETS_OFFLINE", "1")


# ---------------------------------------------------------------- 只读 fake

class _Table:
    def __init__(self, name):
        self.name = name


class _FakeStore:
    """最小 LearningStore 形状：只读查询 + 只读扫描（**无任何写方法**）。"""

    READ_METHODS = ("table_exists", "table", "count", "query", "scan_rows")

    def __init__(self, name, rows=(), *, exists=True):
        self.name = name
        self.rows = [dict(r) for r in rows]
        self.exists = exists
        self.calls: list[str] = []

    def table_exists(self):
        self.calls.append("table_exists")
        return self.exists

    def table(self, create=False):
        assert create is False, "接线路径不得要求建表"
        self.calls.append("table")
        return _Table(self.name)

    def count(self):
        self.calls.append("count")
        return len(self.rows) if self.exists else 0

    def query(self, hint, top_k=5):
        self.calls.append("query")
        if not self.exists:
            return []
        return [dict(r) for r in self.rows[:top_k]]

    def scan_rows(self, columns, *, limit=10 ** 6):
        self.calls.append("scan_rows")
        if not self.exists:
            return []
        return [{c: r.get(c) for c in columns} for r in self.rows]


class _FakeEngine:
    """最小 VectorEngine 形状：稠密查询 + 只读扫描（**无任何写方法**）。"""

    READ_METHODS = ("count_rows", "scan_rows", "query_kb", "query_solutions")

    def __init__(self, *, kb_corpus=(), kb_hits=(), sol_corpus=(), sol_hits=()):
        self.kb_corpus = [dict(r) for r in kb_corpus]
        self.sol_corpus = [dict(r) for r in sol_corpus]
        self.kb_hits = [dict(r) for r in kb_hits]
        self.sol_hits = [dict(r) for r in sol_hits]
        self.calls: list[str] = []

    def count_rows(self, table_name):
        self.calls.append("count_rows")
        if table_name == "kb_chunks":
            return len(self.kb_corpus)
        if table_name == "solution_chunks":
            return len(self.sol_corpus)
        raise AssertionError(f"未预期表名 {table_name}")

    def scan_rows(self, table_name, columns, *, limit=10 ** 6):
        self.calls.append("scan_rows")
        if table_name == "kb_chunks":
            rows = self.kb_corpus
        elif table_name == "solution_chunks":
            rows = self.sol_corpus
        else:
            raise AssertionError(f"未预期表名 {table_name}")
        return [{c: r.get(c) for c in columns} for r in rows]

    def query_kb(self, query, *, namespace=None, project=None,
                 doc_types=None, top_k=5, include_index=False):
        self.calls.append("query_kb")
        if project is not None:
            # r2 项目作用域 lane：本 fake 无作用域语料，回空（不建表、不报错）。
            return []
        assert namespace in ("capabilities", "troubleshooting"), namespace
        # 四路召回对 kb 双 namespace 各发一条；本 fake 只在 capabilities 有料。
        if namespace != "capabilities":
            return []
        return [dict(r) for r in self.kb_hits]

    def query_solutions(self, query, *, top_k=8, solution_id=None):
        self.calls.append("query_solutions")
        if solution_id is not None:
            return []
        return [dict(r) for r in self.sol_hits]


class _ScriptedGateway:
    """记录 messages、回放合法 proposals；不做任何网络动作。"""

    def __init__(self, raw: str):
        self.raw = raw
        self.calls: list[dict] = []

    def model_id(self, stage):
        return "fixture-macro-redacted-0001"

    def call(self, stage, messages, *, max_tokens, temperature, timeout=None):
        self.calls.append({"stage": stage, "messages": messages,
                           "max_tokens": max_tokens,
                           "temperature": temperature})
        return self.raw, (100, 200)


def _valid_proposals_raw() -> str:
    catalog = load_engine_catalog()
    slugs = sorted(catalog)[:2]
    return json.dumps({
        "question": "LIBERO 机械臂操作闭环如何搭建",
        "proposals": [{
            "name": f"方案{chr(65 + i)}", "sim_engine": slug,
            "first_principles": f"第一性原理接触模型推导 [S1]（候选 {i + 1}）。",
            "key_params": "时间步长 0.5ms；摩擦系数 0.3",
            "controls_vs_experiment": "Baseline vs Ours 参数鲁棒性对照",
            "evidence_refs": ["S1"],
            "risk": "接触穿透；最小闭环探针验证",
            "expected_artifacts": [f"probe_{i}.mp4", f"metrics_{i}.json"]}
            for i, slug in enumerate(slugs)]}, ensure_ascii=False)


def _kb_doc(rid, *, text, relpath=None, section="§1"):
    return {"id": rid, "text": text,
            "kb_relpath": relpath or f"knowledge/{rid}.md",
            "section_path": section, "doc_type": "background"}


def _sol_doc(rid, *, text, kind="readme", chunk_index=0):
    return {"id": rid, "text": text,
            "relpath": f"solutions/{LIBERO_SOLUTION_ID}/README.md",
            "kind": kind, "chunk_index": chunk_index,
            "solution_id": LIBERO_SOLUTION_ID}


def _lr_doc(rid, *, text):
    return {"id": rid, "text": text,
            "evidence_relpath": "@runs/phase2/task7/summary.json",
            "run_id": f"run-{rid}", "gate_source": "human_failure_agent_run",
            "rung": 1}


def _corpus():
    kb = _kb_doc("kb_libero_1",
                 text="LIBERO 基准套件提供长程操作任务族与闭环评测协议。")
    sol = _sol_doc("sol_libero_1",
                   text="LIBERO 安装：pip install libero；评测脚本分四档任务套件。")
    lr = _lr_doc("lr_libero_1",
                 text="避坑叙述：无头渲染需 EGL 后端与 setrlimit 提栈。")
    return kb, sol, lr


def _noop_evidence(stem, kind, payload):
    return f"phase26/task2_direction/{stem}.{kind}.json"


def _cache(tmp_path):
    from orchestrator.common.callcache import CallReplay
    return CallReplay(tmp_path / ".call_cache")


def _setter(tmp_path, *, recall_mode, gw=None, engine=None, store=None):
    tmp_path = Path(tmp_path)
    tmp_path.mkdir(parents=True, exist_ok=True)
    kb, sol, lr = _corpus()
    ve = engine or _FakeEngine(kb_corpus=[kb], kb_hits=[kb],
                               sol_corpus=[sol], sol_hits=[sol])
    st = store or _FakeStore("phase1_learnings", [lr])
    prod = ProductionRecall(st, ve)
    setter = graph_cli.build_direction_setter(
        gw or _ScriptedGateway(_valid_proposals_raw()), ve, prod,
        recall_mode=recall_mode, evidence_writer=_noop_evidence,
        call_cache=_cache(tmp_path))
    return setter, ve, st


def _run(setter, *, tier=TIER_PROBE):
    return setter.run_direction(
        track="track1", payload=LIBERO_PAYLOAD,
        thread_id="t2wiring", batch_id="P26-T-FIXTURE", planner_tier=tier)


def _write_evidence(name: str, text: str) -> Path:
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(EVIDENCE_DIR, 0o700)
    path = EVIDENCE_DIR / name
    path.write_text(text, encoding="utf-8")
    os.chmod(path, 0o600)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    return path


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- AC-T2-1

def test_ac_t2_1_prompt_three_sections_via_production_builder(tmp_path):
    setter, ve, st = _setter(tmp_path, recall_mode="fourway")
    # 生产构造点确实注入了 PlannerRecall，且规则版本由常量单点给出。
    assert isinstance(setter.planner_recall, PlannerRecall)
    assert setter.planner_recall.rules_version == PRODUCTION_RULES_VERSION
    assert setter.recall is not None  # 双路中枢仍在（Phase 2 语义不丢）

    gw = setter.gateway
    result = _run(setter)
    assert len(gw.calls) == 1, "每候选轮恰好一次 macro 调用"
    user = gw.calls[0]["messages"][1]["content"]

    # 三段头用 setter 常量断言（不写字面量）。
    assert PLANNER_SECTION_KB in user
    assert PLANNER_SECTION_SOLUTIONS in user
    assert PLANNER_SECTION_LEARNINGS in user
    # 三段命中文本逐字在场（kb / solutions / learnings）。
    kb, sol, lr = _corpus()
    for hit in (kb, sol, lr):
        assert hit["text"] in user, f"{hit['id']} 文本未逐字渲染"
    # 方案块带 solution_id 与 relpath 出处（渲染器契约）。
    assert LIBERO_SOLUTION_ID in user
    assert f"solutions/{LIBERO_SOLUTION_ID}" in user
    # 结果件的 kb_sources 带来源路（kb + solutions 占 [Si] 名额）。
    routes = {s["source_route"] for s in result["kb_sources"]}
    assert routes == {SOURCE_ROUTE_KB, SOURCE_ROUTE_SOLUTIONS}

    _write_evidence("ac_t2_1_prompt.txt", user)


# ---------------------------------------------------------------- AC-T2-2a

def test_ac_t2_2a_telemetry_three_routes_seven_fields(tmp_path, monkeypatch):
    log = tmp_path / "production_recall.jsonl"
    # 生产写入方的路径由 telemetry_path() 单点给出；setter 不传 path，
    # 故在此把该单点指到 tmp（不新增注入参数、不改写入方）。
    monkeypatch.setattr(ProductionRecall, "telemetry_path",
                        staticmethod(lambda root=None: log))

    setter, _, _ = _setter(tmp_path, recall_mode="fourway")
    _run(setter)

    rows = list(iter_adoption_rows(log))
    assert rows, "接线后必须产生采纳行"
    by_route: dict[str, list[dict]] = {}
    for r in rows:
        assert tuple(r.keys()) == LOG_FIELDS_V2, f"行字段不是七字段：{list(r)}"
        by_route.setdefault(r["source_route"], []).append(r)
    assert set(by_route) == {SOURCE_ROUTE_KB, SOURCE_ROUTE_SOLUTIONS,
                             SOURCE_ROUTE_LEARNINGS}, by_route.keys()
    for route, rs in by_route.items():
        assert rs, f"{route} 路由无行"

    _write_evidence("ac_t2_2_telemetry.jsonl",
                    log.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- AC-T2-3

def test_ac_t2_3_four_tables_unchanged_read_only_path(tmp_path):
    setter, ve, st = _setter(tmp_path, recall_mode="fourway")
    before = (len(ve.kb_corpus), len(ve.sol_corpus), len(st.rows),
              len(setter.planner_recall._lexical_cache))
    _run(setter)
    after = (len(ve.kb_corpus), len(ve.sol_corpus), len(st.rows),
             len(setter.planner_recall._lexical_cache))

    # (1) 全程只经只读 API：fake 只暴露读方法，且实际调用名全在读方法集内。
    assert set(ve.calls) <= set(ve.READ_METHODS), ve.calls
    assert set(st.calls) <= set(st.READ_METHODS), st.calls
    # (2) 三个数据源行数前后不变（写路径根本没有被触及）。
    assert before[:3] == after[:3]
    # (3) 词法索引只建一次（同一实例复用，不重复扫表）。
    assert len(setter.planner_recall._lexical_cache) == 1

    snapshot = {
        "fake_before": {"kb_chunks": before[0], "solution_chunks": before[1],
                        "phase1_learnings": before[2]},
        "fake_after": {"kb_chunks": after[0], "solution_chunks": after[1],
                       "phase1_learnings": after[2]},
        "engine_calls": sorted(set(ve.calls)),
        "store_calls": sorted(set(st.calls)),
        "note": ("本用例用只读 fake 引擎/学习库证明接线路径不触任何写 API；"
                 "活库四表 1542/44/94/1 的前后快照见 AC-T2-2b 的 "
                 "ac_t2_3_live_table_snapshot.json（需 SIMPRAXIS_KB_INTEGRATION=1）"),
    }
    _write_evidence("ac_t2_3_fake_table_snapshot.json",
                    json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n")


def _both_prompts(tmp_path):
    """同语料跑 dual 与 fourway 两条支路，返回 (dual_messages, fourway_messages)。"""
    dual, _, _ = _setter(tmp_path / "dual", recall_mode="dual")
    _run(dual)
    four, _, _ = _setter(tmp_path / "four", recall_mode="fourway")
    _run(four)
    return dual.gateway.calls[0]["messages"], four.gateway.calls[0]["messages"]


def _msg_sha(messages) -> str:
    return _sha(json.dumps(messages, ensure_ascii=False, sort_keys=True))


def _write_prompt_sha(dual_msg, four_msg) -> dict:
    """两条支路的 prompt sha256 落同一证据件（两用例各写一次，内容确定）。"""
    data = {"dual_messages_sha256": _msg_sha(dual_msg),
            "fourway_messages_sha256": _msg_sha(four_msg),
            "note": ("dual 侧逐字节等于 build_first_messages（AC-T2-4a）；"
                     "fourway 侧含三段头且 sha 不同（AC-T2-4b 双向闸）")}
    _write_evidence("ac_t2_4_prompt_sha.json",
                    json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    return data


# ---------------------------------------------------------------- AC-T2-4a

def test_ac_t2_4a_dual_mode_byte_identical_to_legacy_builder(tmp_path):
    setter, ve, st = _setter(tmp_path, recall_mode="dual")
    assert setter.planner_recall is None, "dual 分支必须 planner_recall=None"

    gw = setter.gateway
    _run(setter)
    assert len(gw.calls) == 1
    content = gw.calls[0]["messages"][1]["content"]
    # 逐字节等于旧 builder（Phase 2.5 AC-T6-6 的仓内自证形态）。
    # recall 注入时 learning 段由旧 builder 自己渲染（Phase 2 语义），
    # 故 learning_hits 必须同参传入才是同一次发送串。
    expected = build_first_messages(
        track="track1", payload=LIBERO_PAYLOAD, hits=list(ve.kb_hits),
        catalog=setter.catalog, learning_hits=list(st.rows))
    assert gw.calls[0]["messages"] == expected
    assert PLANNER_SECTION_SOLUTIONS not in content
    assert PLANNER_SECTION_LEARNINGS not in content

    dual_msg, four_msg = _both_prompts(tmp_path)
    assert _msg_sha(dual_msg) == _msg_sha(gw.calls[0]["messages"]), (
        "同一 dual 配置两次渲染的 prompt 必须逐字节相同（无隐藏状态）")
    _write_prompt_sha(dual_msg, four_msg)


# ---------------------------------------------------------------- AC-T2-4b

def test_ac_t2_4b_fourway_prompt_differs_from_dual(tmp_path):
    dual_msg, four_msg = _both_prompts(tmp_path)

    four_user = four_msg[1]["content"]
    assert PLANNER_SECTION_KB in four_user
    assert PLANNER_SECTION_SOLUTIONS in four_user
    assert PLANNER_SECTION_LEARNINGS in four_user

    data = _write_prompt_sha(dual_msg, four_msg)
    assert data["fourway_messages_sha256"] != data["dual_messages_sha256"], (
        "fourway 与 dual 的 prompt 相同 ⇒ 接线没生效（双向闸）")
    # dual 侧仍必须是旧 builder 的形状（不含三段头），否则「不等」可能来自
    # 无关漂移而不是四路注入。
    assert PLANNER_SECTION_SOLUTIONS not in dual_msg[1]["content"]


# ---------------------------------------------------------------- AC-T2-4c

def test_ac_t2_4c_cli_flag_maps_to_constructor_args(tmp_path, monkeypatch):
    # (1) 常量与 argparse 声明。
    assert graph_cli.RECALL_MODES == ("fourway", "dual")
    assert graph_cli.RECALL_FOURWAY == "fourway"
    assert graph_cli.RECALL_DUAL == "dual"

    captured = {}

    def _fake_invoke(args, *, resume):
        captured["args"] = args
        return 0

    monkeypatch.setattr(graph_cli, "_invoke", _fake_invoke)
    base = ["run", "--thread", "t2cli", "--track", "track1",
            "--index", "1", "--batch-id", "P26-T-FIXTURE"]

    assert graph_cli.main(base) == 0
    assert captured["args"].recall_mode == "fourway", "缺省必须是 fourway"
    assert captured["args"].planner_tier == TIER_PROBE, "缺省档位必须仍是 probe"

    assert graph_cli.main(base + ["--recall-mode", "dual"]) == 0
    assert captured["args"].recall_mode == "dual"

    assert graph_cli.main(base + ["--planner-tier", "policy_closed_loop"]) == 0
    assert captured["args"].planner_tier == "policy_closed_loop"

    # 非法值由 argparse choices 拒（rc=2），不会把未知模式带进构造点。
    with pytest.raises(SystemExit) as exc:
        graph_cli.main(base + ["--recall-mode", "bogus"])
    assert exc.value.code == 2
    with pytest.raises(SystemExit) as exc:
        graph_cli.main(base + ["--planner-tier", "policy"])
    assert exc.value.code == 2

    # (2) 旗标 → 构造实参这一跳。
    kb, sol, lr = _corpus()
    ve = _FakeEngine(kb_corpus=[kb], kb_hits=[kb],
                     sol_corpus=[sol], sol_hits=[sol])
    st = _FakeStore("phase1_learnings", [lr])
    prod = ProductionRecall(st, ve)
    gw = _ScriptedGateway(_valid_proposals_raw())

    dual = graph_cli.build_direction_setter(gw, ve, prod, recall_mode="dual")
    assert isinstance(dual, DirectionSetter)
    assert dual.planner_recall is None

    four = graph_cli.build_direction_setter(gw, ve, prod, recall_mode="fourway")
    assert isinstance(four.planner_recall, PlannerRecall)
    assert four.planner_recall.rules_version == PRODUCTION_RULES_VERSION
    assert four.planner_recall.prod is prod, "四路必须组合同一个 ProductionRecall"

    with pytest.raises(ValueError):
        graph_cli.build_direction_setter(gw, ve, prod, recall_mode="bogus")

    _write_evidence("ac_t2_4c_flag_mapping.json", json.dumps({
        "recall_modes": list(graph_cli.RECALL_MODES),
        "default_recall_mode": graph_cli.RECALL_FOURWAY,
        "default_planner_tier": TIER_PROBE,
        "production_rules_version": PRODUCTION_RULES_VERSION,
        "dual_planner_recall_is_none": dual.planner_recall is None,
        "fourway_planner_recall_type": type(four.planner_recall).__name__,
        "shares_production_recall": four.planner_recall.prod is prod,
    }, ensure_ascii=False, indent=2) + "\n")


# ---------------------------------------------------------------- AC-T2-2b

@pytest.mark.kb_integration
def test_ac_t2_2b_t2_reachable_live_three_routes(tmp_path):
    """活库：四路召回在 LIBERO 形状查询下三路可达，且 03_libero 在 top-8。"""
    kb_root = require_kb_integration(data=True)
    from memory_flywheel.phase1.learning_store import LearningStore
    from memory_flywheel.vector_engine import VectorEngine

    def _counts() -> dict[str, int]:
        db = ve.db
        out = {}
        for name in ("kb_chunks", "solution_chunks", "phase1_learnings",
                     "phase2_learnings"):
            try:
                out[name] = db.open_table(name).count_rows()
            except Exception:
                out[name] = None
        return out

    ve = VectorEngine()
    store = LearningStore()
    prod = ProductionRecall(store, ve)
    planner = PlannerRecall(prod, rules_version=PRODUCTION_RULES_VERSION)

    before = _counts()
    ret = planner.retrieve("track1", LIBERO_PAYLOAD, tier=TIER_PROBE,
                           site="direction")
    after = _counts()

    def _rows(key):
        return ret[key]

    kb_hits = _rows("kb_hits")
    sol_hits = _rows("solution_hits")
    lr_hits = _rows("learning_hits")
    assert kb_hits, "活库 kb 路零命中（先查 relpath/前缀是否变了，不 gold-fishing）"
    assert sol_hits, "活库 solutions 路零命中"
    assert lr_hits, "活库 learnings 路零命中"

    routes = {r.get("source_route") for r in kb_hits}
    assert routes == {SOURCE_ROUTE_KB}
    assert all(r.get("source_route") == SOURCE_ROUTE_SOLUTIONS for r in sol_hits)
    assert all(r.get("source_route") == SOURCE_ROUTE_LEARNINGS for r in lr_hits)

    top8 = [r for r in sol_hits if r.get("solution_id") == LIBERO_SOLUTION_ID]
    assert top8, (f"{LIBERO_SOLUTION_ID} 不在 solutions top-8："
                  f"{[r.get('solution_id') for r in sol_hits]}")

    # AC-T2-3 的活库半：四表全程不变（kb/phase1 由 Task0b 起的钉死基线）。
    assert before["kb_chunks"] == 1542
    assert before["solution_chunks"] == 94
    assert before["phase1_learnings"] == 44
    assert before == after, f"检索触发了写：{before} → {after}"

    _write_evidence("ac_t2_2b_live_hits.json", json.dumps({
        "kb_root": str(kb_root),
        "query": LIBERO_PAYLOAD["name"],
        "kb_hits": [{"id": r.get("id"), "relpath": r.get("kb_relpath"),
                     "source_route": r.get("source_route")} for r in kb_hits],
        "solution_hits": [{"id": r.get("id"), "solution_id": r.get("solution_id"),
                           "relpath": r.get("relpath"),
                           "source_route": r.get("source_route")}
                          for r in sol_hits],
        "learning_hits": [{"id": r.get("id"),
                           "learning_table": r.get("learning_table"),
                           "source_route": r.get("source_route")}
                          for r in lr_hits],
    }, ensure_ascii=False, indent=2) + "\n")
    _write_evidence("ac_t2_3_live_table_snapshot.json", json.dumps(
        {"before": before, "after": after,
         "note": "活库四表前后快照；检索全程只读"},
        ensure_ascii=False, indent=2) + "\n")
