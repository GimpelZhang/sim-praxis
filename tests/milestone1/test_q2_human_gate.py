"""q2_human_gate.py 纯函数/流程单测（H-19），0 付费 0 联网 0 真实飞书。

覆盖（评审 H-5/H-9/H-10/H-19）：
  - 退出矩阵：真人 approve -> 0；reject_with_comments -> 3；
    证据缺失/非 card_form/ledger 增量≠0/事实核验失败 -> 1；
  - ledger 增量非 0 时即使 approve 也 exit 1，decision.json 如实保留；
  - 幂等：decision.json 存在即拒；主表 UNION history 终态即拒；
    并发 PENDING 占位即拒（第二调用方退出，不重推视频）；
  - H-10：meta geo/sem 不一致、Qback 跨源不一致、ffprobe 失败、snow 探测
    失败全部 FATAL 且不构造网关；
  - H-2：trim 严格四键透传（三分屏 contact=None，原片时长 ffprobe 实读），
    explanation 不写字面量（16 线/120°/50m/300kpps/约 10 秒全部来自事实）；
  - main 环境守卫：无 SIMPRAXIS_Q2_HUMAN_GATE / 缺 FEISHU_* -> exit 1。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_REPO = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "q2_human_gate_under_test",
    str(_REPO / "scripts" / "milestone1" / "q2_human_gate.py"))
q2 = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = q2  # dataclass 字符串注解要求模块先注册
_SPEC.loader.exec_module(q2)

from orchestrator.feishu_gateway.store import PendingStore  # noqa: E402

THREAD = q2.THREAD_ID

_INV = {
    "semantic_geometry_bit_identical": {
        "comparisons": 80, "failures": []},
    "geo_cross_repeat_bit_identical": {"comparisons": 20},
    "geo_count_z_vs_clear": {"rain_hard": -1.1159, "fog_dense": -1.0091},
    "survivor_intensity_identity": [{"max_abs_dI": 4.45e-05}],
}
_CTL_INV = {"geo_count_z_vs_clear": {"rain_hard": -1.0767,
                                      "fog_dense": -0.9736}}
_CSV = (
    "condition,frac_surface_kept_mean,n_weather_false_mean,"
    "n_near_blind_mean,transmission_mean_mean\n"
    "rain_100,0.717,6346,3747,0.871\n"
    "snow_100,0.017,8078,3740,0.024\n"
    "fog_v6,0.107,0,0,0.013\n"
    "clear,1.0,0,0,1.0\n"
)
_SUMMARY = (
    "# Q2 总结\n\n"
    "  同位双雷达，16 线 / 120° 水平 FOV / 50 m / 300 kpps / 10 Hz。\n\n"
    "  Qext→2、Qback=0.4，距离门 0.3 m。\n"
)
_OPTICS = "DISTANCE_GATE = 0.3\nQBACK_DEFAULT = 0.4\n"


def _meta(*, geo_fov="120", sem_fov="120"):
    def lidar(fov):
        return {"channels": "16", "range": "50", "points_per_second": "300000",
                "rotation_frequency": "10", "horizontal_fov": fov}
    return {"server_version": "0.10.0",
            "lidar_attrs": {"geo": lidar(geo_fov), "sem": lidar(sem_fov)}}


@pytest.fixture()
def site(tmp_path):
    """构造合成 Q2 现场树；返回 (paths, 路径集合辅助)。"""
    q2d = tmp_path / "q2_carla"
    ana = q2d / "analysis"
    ctl = q2d / "control" / "analysis"
    rep = q2d / "collect_rep1"
    for d in (ana, ctl, rep, tmp_path / "state"):
        d.mkdir(parents=True, exist_ok=True)
    (ana / "invariance_checks.json").write_text(
        json.dumps(_INV), encoding="utf-8")
    (ctl / "invariance_checks.json").write_text(
        json.dumps(_CTL_INV), encoding="utf-8")
    (ana / "optics_sweep.csv").write_text(_CSV, encoding="utf-8")
    (ana / "SUMMARY.md").write_text(_SUMMARY, encoding="utf-8")
    (ana / "q2_overlay.mp4").write_bytes(b"\x00mp4fixture")
    (rep / "meta_rep1.json").write_text(json.dumps(_meta()), encoding="utf-8")
    optics = tmp_path / "optics_model.py"
    optics.write_text(_OPTICS, encoding="utf-8")
    db = tmp_path / "state" / "pending_gates.db"
    paths = q2.Q2Paths(
        q2_dir=q2d, analysis=ana, control_invariance=ctl / "invariance_checks.json",
        gates_db=db, meta_rep1=rep / "meta_rep1.json",
        summary_md=ana / "SUMMARY.md", optics_model=optics,
        video=ana / "q2_overlay.mp4")
    return paths


def _facts_kwargs():
    """全绿事实读取注入（不跑 ffprobe、不 import carla）。"""
    return dict(ffprobe_runner=lambda argv: SimpleNamespace(stdout="10.000000\n"),
                snow_probe=lambda: False)


class FakeGateway:
    """记录调用并按脚本指示落决策的假网关；store 用真实 PendingStore。"""
    def __init__(self, store, *, outcome=("approve", "card_form", None),
                 reject_notes="真人打回意见"):
        self.store = store
        self.requests: list[dict] = []
        self._outcome = outcome
        self._reject_notes = reject_notes

    def request_gate2_approval(self, thread_id, title, metrics, *, video_path,
                               explanation, trim, **kwargs):
        self.requests.append({"thread": thread_id, "trim": trim,
                              "explanation": explanation, "metrics": metrics})
        decision, source, notes = self._outcome
        if decision == "approve":
            assert self.store.mark_decided(
                thread_id, 2, decision="approve", source=source, choice=None)
        else:
            assert self.store.mark_decided(
                thread_id, 2, decision="reject_with_comments",
                source=source, human_notes=notes or self._reject_notes)
        row = self.store.get_pending(thread_id, 2)
        return {"decision": row["decision"], "choice": row["choice"],
                "human_notes": row["human_notes"], "source": row["source"]}

    def close(self):
        self.store.close()


def _factory(store, **kw):
    def _make():
        # run() 最后会 feishu.close()；每个 factory 开一个新 store 句柄指向同库
        return FakeGateway(PendingStore(store.db_path), **kw)
    return _make


class FakeOffgraphWriter:
    """Task0a 纪律 12 接线：记录回写调用，绝不触真实学习表。"""

    def __init__(self):
        self.calls: list[Path] = []

    def __call__(self, decision_path):
        self.calls.append(Path(decision_path))
        return {"id": "fixture-offgraph-id",
                "gate_source": "gate2_accepted_supplementary", "rung": 1}


# ---------------- 退出矩阵 ----------------

def test_exit0_on_human_approve_card_form(site):
    store = PendingStore(site.gates_db)
    writer = FakeOffgraphWriter()
    code = q2.run(site, gateway_factory=_factory(store),
                  count_paid=lambda: 0, offgraph_writer=writer,
                  **_facts_kwargs())
    store.close()
    assert code == 0
    # Task0a 纪律 12：approve 后恰好回写一次，指针就是本次 decision.json
    assert writer.calls == [site.decision_path]
    result = json.loads(site.decision_path.read_text(encoding="utf-8"))
    assert result["human_decision"]["decision"] == "approve"
    assert result["human_decision"]["source"] == "card_form"
    assert result["paid_llm_calls"] == {"before": 0, "after": 0, "delta": 0}
    # H-9：0600 原子落盘
    assert oct(site.decision_path.stat().st_mode & 0o777) == "0o600"
    assert oct(site.log_path.stat().st_mode & 0o777) == "0o600"
    facts = result["verified_facts"]
    assert facts["channels"] == 16 and facts["horizontal_fov_deg"] == 120
    assert facts["range_m"] == 50 and facts["points_per_second"] == 300000
    assert facts["video_duration_s"] == 10.0 and facts["qback"] == 0.4
    assert facts["engine_has_snow"] is False


def test_exit3_on_human_reject_card_form(site):
    store = PendingStore(site.gates_db)
    gw_holder: dict = {}
    def factory():
        gw = FakeGateway(PendingStore(site.gates_db),
                         outcome=("reject", "card_form", None))
        gw_holder["gw"] = gw
        return gw
    writer = FakeOffgraphWriter()
    code = q2.run(site, gateway_factory=factory, count_paid=lambda: 0,
                  offgraph_writer=writer, **_facts_kwargs())
    assert code == 3
    result = json.loads(site.decision_path.read_text(encoding="utf-8"))
    assert result["human_decision"]["decision"] == "reject_with_comments"
    assert "真人打回意见" in result["human_decision"]["human_notes"]
    assert gw_holder["gw"].requests[0]["thread"] == THREAD
    # Task0a：reject 不得沉淀 accepted 学习行
    assert writer.calls == []


def test_exit1_missing_evidence_no_gateway(site):
    site.video.unlink()
    def boom():
        raise AssertionError("证据缺失时不得构造网关")
    code = q2.run(site, gateway_factory=boom, count_paid=lambda: 0,
                  **_facts_kwargs())
    assert code == 1
    assert not site.decision_path.exists()


def test_exit1_non_card_form_source(site):
    store = PendingStore(site.gates_db)
    # 旁路 CLI 落决策（非卡片）：不得算真人 Gate2 通过
    code = q2.run(site, gateway_factory=_factory(
        store, outcome=("approve", "cli", None)),
        count_paid=lambda: 0, **_facts_kwargs())
    assert code == 1
    result = json.loads(site.decision_path.read_text(encoding="utf-8"))
    assert result["human_decision"]["source"] == "cli"


def test_exit1_ledger_delta_nonzero_but_result_retained(site):
    store = PendingStore(site.gates_db)
    seq = iter([0, 1])  # 发卡后多出一条方舟实呼
    code = q2.run(site, gateway_factory=_factory(store),
                  count_paid=lambda: next(seq), **_facts_kwargs())
    assert code == 1  # 0 付费声明被证伪：优先 FATAL，即便真人 approve
    result = json.loads(site.decision_path.read_text(encoding="utf-8"))
    assert result["paid_llm_calls"]["delta"] == 1
    assert result["human_decision"]["decision"] == "approve"  # 结果如实保留


# ---------------- H-5：幂等 / 并发占位 / UNION history ----------------

def test_idempotent_decision_json_blocks_rerun(site):
    site.decision_path.parent.mkdir(parents=True, exist_ok=True)
    site.decision_path.write_text('{"already": true}', encoding="utf-8")
    def boom():
        raise AssertionError("decision.json 已在时不得构造网关/重发")
    code = q2.run(site, gateway_factory=boom, count_paid=lambda: 0,
                  **_facts_kwargs())
    assert code == 1


def test_idempotent_decided_row_in_history_blocks(site):
    store = PendingStore(site.gates_db)
    # 模拟上一轮 approve 已归档进 history（主表无行）
    assert store.claim_pending(THREAD, 2, task_title="old")
    assert store.mark_decided(THREAD, 2, decision="approve",
                              source="card_form")
    assert store.archive_decided(THREAD, 2)
    holder: dict = {}
    def factory():
        gw = FakeGateway(PendingStore(site.gates_db))
        holder["gw"] = gw
        return gw
    code = q2.run(site, gateway_factory=factory, count_paid=lambda: 0,
                  **_facts_kwargs())
    assert code == 1
    assert holder["gw"].requests == []  # 没发卡、没推视频
    assert not site.decision_path.exists()


def test_concurrent_claim_second_caller_exits(site):
    store = PendingStore(site.gates_db)
    # 另一并发调用方已占位（无卡 id）：第二调用方退出，不得接管推视频
    assert store.claim_pending(THREAD, 2, task_title="concurrent")
    holder: dict = {}
    def factory():
        gw = FakeGateway(PendingStore(site.gates_db))
        holder["gw"] = gw
        return gw
    code = q2.run(site, gateway_factory=factory, count_paid=lambda: 0,
                  **_facts_kwargs())
    assert code == 1
    assert holder["gw"].requests == []


# ---------------- H-10：事实不一致 FATAL ----------------

def test_h10_meta_geo_sem_mismatch_fatal(site):
    meta = _meta(geo_fov="90", sem_fov="120")
    site.meta_rep1.write_text(json.dumps(meta), encoding="utf-8")
    def boom():
        raise AssertionError("事实核验失败不得构造网关")
    code = q2.run(site, gateway_factory=boom, count_paid=lambda: 0,
                  **_facts_kwargs())
    assert code == 1
    assert not site.decision_path.exists()


def test_h10_qback_cross_source_mismatch_fatal(site):
    site.optics_model.write_text("QBACK_DEFAULT = 0.5\n", encoding="utf-8")
    code = q2.run(site, gateway_factory=lambda: pytest.fail("不得发卡"),
                  count_paid=lambda: 0, **_facts_kwargs())
    assert code == 1


def test_h10_ffprobe_failure_fatal(site):
    def bad_runner(argv):
        raise OSError("fixture: ffprobe missing")
    code = q2.run(site, gateway_factory=lambda: pytest.fail("不得发卡"),
                  count_paid=lambda: 0,
                  ffprobe_runner=bad_runner, snow_probe=lambda: False)
    assert code == 1


def test_h10_snow_probe_failure_fatal(site):
    def bad_snow():
        raise q2.Q2FactError("fixture: carla gone")
    code = q2.run(site, gateway_factory=lambda: pytest.fail("不得发卡"),
                  count_paid=lambda: 0,
                  ffprobe_runner=lambda argv: SimpleNamespace(stdout="10\n"),
                  snow_probe=bad_snow)
    assert code == 1


def test_h10_pure_readers_reject_bad_values(tmp_path):
    with pytest.raises(q2.Q2FactError):
        q2.read_qback_default(Path("/nonexistent/optics_model_x.py"))
    bad = tmp_path / "optics_model.py"
    bad.write_text("# no constant here\n", encoding="utf-8")
    with pytest.raises(q2.Q2FactError, match="QBACK_DEFAULT"):
        q2.read_qback_default(bad)


def test_h10_qback_reader_parses_constant(tmp_path):
    p = tmp_path / "optics_model.py"
    p.write_text("# comment\nQBACK_DEFAULT = 0.25\n", encoding="utf-8")
    assert q2.read_qback_default(p) == 0.25
    with pytest.raises(q2.Q2FactError):
        q2.ffprobe_duration(tmp_path / "no.mp4",
                            lambda argv: SimpleNamespace(stdout="not-a-float\n"))


# ---------------- H-2：trim 透传 + explanation 无字面量 ----------------

def test_h2_trim_passthrough_and_explanation_from_facts(site):
    store = PendingStore(site.gates_db)
    holder: dict = {}
    def factory():
        gw = FakeGateway(PendingStore(site.gates_db))
        holder["gw"] = gw
        return gw
    writer = FakeOffgraphWriter()
    code = q2.run(site, gateway_factory=factory, count_paid=lambda: 0,
                  offgraph_writer=writer, **_facts_kwargs())
    assert code == 0
    req = holder["gw"].requests[0]
    # Task0a 纪律 12：approve 路径接线恰好一次
    assert writer.calls == [site.decision_path]
    trim = req["trim"]
    assert set(trim) == {"trim_start", "trim_seconds",
                         "contact_moment_s", "source_duration_s"}
    assert trim["contact_moment_s"] is None      # 三分屏无单一接触
    assert trim["source_duration_s"] == 10.0
    expl = req["explanation"]
    assert "约 10 秒" in expl["video"]
    assert "16 线" in expl["task"] and "120°/50m/300kpps" in expl["task"]
    # explanation 构造体里不得残留写死的旧事实（docstring 描述修复历史除外）
    import inspect
    src = inspect.getsource(q2.build_explanation)
    assert "约 10 秒" not in src and "120°/50m/300kpps" not in src
    assert "Qback=0.4" not in inspect.getsource(q2.build_metrics)


# ---------------- main 环境守卫 ----------------

def test_main_env_guard(monkeypatch, site):
    monkeypatch.delenv("SIMPRAXIS_Q2_HUMAN_GATE", raising=False)
    monkeypatch.delenv("FEISHU_APP_ID", raising=False)
    assert q2.main() == 1
    monkeypatch.setenv("SIMPRAXIS_Q2_HUMAN_GATE", "1")
    assert q2.main() == 1  # FEISHU_* 仍缺失
