"""Phase 2 Task2 — RSI 螺旋升阶引擎（ascent 模式）零付费测试。

纪律：0 付费/0 联网；不硬编码 sha256 与 /mnt 字面路径（测试全部自己复算，
真实档案只在 SIMPRAXIS_V1_INDEX_REAL=1 时读取）；不做 numpy 假帧；除真实
档案环境守卫外不允许任何 skip。
"""
from __future__ import annotations

import ast
import copy
import hashlib
import json
import math
import os
import types
from pathlib import Path

import pytest

from orchestrator.direction.inputs import get_input
from orchestrator.direction.setter import build_first_messages, validate_proposals
from orchestrator.graph import cli as graph_cli
from orchestrator.graph.ascent import (
    V1_INDEX_SCHEMA,
    V1IndexCorrupt,
    V1IndexMissing,
    build_ascent_messages,
    classify_run,
    load_v1_index,
    render_v1_metrics,
    sha256_text,
)
from orchestrator.graph.nodes import (
    GraphDeps,
    GraphStateError,
    build_graph,
    node_classify,
)
from orchestrator.graph.state import SimPraxisState
from orchestrator.judge import gait_thresholds as gt
from orchestrator.judge import pareto as par
from orchestrator.judge import ascent_thresholds as at

FIX = Path(__file__).parent / "fixtures"
SUMMARY_FIXTURE = FIX / "v1_summary_q1.json"
GOLDEN_FIXTURE = FIX / "standard_messages_golden.json"

THREAD_ID = "t8-genesis-20261016a"
REGISTERED_RELS = (
    ["summary.json",
     "split_normal_vs_ice_nominal_s0.mp4",
     "split_ice_nominal_vs_ice_dr_s0.mp4"]
    + [f"telemetry/{arm}_s{s}.json"
       for arm in ("normal", "ice_nominal", "ice_dr") for s in range(3)]
)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _build_fixture_index(tmp_path: Path) -> tuple[dict, Path]:
    """在 tmp 下造一份与操作件同 schema 的 V1 档案（路径/sha 全部现场复算）。"""
    workdir = tmp_path / f"run_{THREAD_ID}_1"
    (workdir / "telemetry").mkdir(parents=True)
    summary = json.loads(SUMMARY_FIXTURE.read_text(encoding="utf-8"))
    # 落盘 12 个登记件：summary 用夹具原件，mp4 用小字节，telemetry 用组数值。
    (workdir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False), encoding="utf-8")
    (workdir / "split_normal_vs_ice_nominal_s0.mp4").write_bytes(b"fake-mp4-a")
    (workdir / "split_ice_nominal_vs_ice_dr_s0.mp4").write_bytes(b"fake-mp4-b")
    for arm in ("normal", "ice_nominal", "ice_dr"):
        for s in range(3):
            (workdir / "telemetry" / f"{arm}_s{s}.json").write_text(
                json.dumps({"arm": arm, "seed": s}), encoding="utf-8")
    files = [{"relpath": rel, "sha256": _sha256_file(workdir / rel)}
             for rel in REGISTERED_RELS]
    question = get_input("track2", 1)["question"]
    index = {
        "schema": V1_INDEX_SCHEMA,
        "generated_iso": "2026-10-17T00:00:00Z",
        "entries": [{
            "track": "track2",
            "question_index": 1,
            "thread_id": THREAD_ID,
            "question": question,
            "question_sha256": sha256_text(question),
            "host_workdir": str(workdir),
            "files": files,
            "dr_envelope": summary["dr_envelope"],
            "v1_metrics": {
                "schema": summary["schema"], "seeds": summary["seeds"],
                "disturbance": summary["disturbance"],
                "groups": summary["groups"],
            },
        }],
    }
    index_path = tmp_path / "v1_index.json"
    index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2),
                          encoding="utf-8")
    return index, workdir


# ---------------------------------------------------------------------------
# 分类器
# ---------------------------------------------------------------------------

def test_classifier_q1_is_ascent(tmp_path):
    index, workdir = _build_fixture_index(tmp_path)
    loaded = load_v1_index(tmp_path / "v1_index.json")  # 严格校验必须通过
    question = index["entries"][0]["question"]
    result = classify_run({"track": "track2", "question": question}, loaded)
    assert result["run_mode"] == "ascent"
    arc = result["v1_archive"]
    assert arc["thread_id"] == THREAD_ID
    assert arc["question_sha256"] == sha256_text(question)
    assert arc["host_workdir"] == str(workdir)
    assert [f["relpath"] for f in arc["files"]] == list(REGISTERED_RELS)
    targets = result["ascent_targets"]
    assert set(targets) == {"factors", "recompute_tolerances", "physical_bounds",
                            "anchors_unchanged", "dr_envelope_v1",
                            "dr_envelope_tight"}
    # 批判段组装（system 一字不动；user 头部插入 V1 摘要与超越假设约束）
    base = [{"role": "system", "content": "SYS"},
            {"role": "user", "content": "USER-BODY"}]
    msgs = build_ascent_messages(base, v1_archive=arc,
                                 ascent_targets=targets)
    assert msgs[0] == {"role": "system", "content": "SYS"}
    assert msgs[1]["content"].endswith("USER-BODY")
    head = msgs[1]["content"][:-len("USER-BODY")]
    assert "RSI 螺旋升阶" in head and "ascent_claims" in head
    assert "逐条指出 V1" in head and "RL 算法端不修改" in head
    assert f"thread_id={THREAD_ID}" in head
    # V1 真实数值必须出现在摘要里（独立重算值口径）
    rendered = render_v1_metrics(arc)
    assert "0.3333" in rendered and "19.198" in rendered and "139.36" in rendered


def test_unknown_question_standard(tmp_path):
    index, _ = _build_fixture_index(tmp_path)
    cases = [
        {"track": "track2", "question": "一道全新的、从未登记过的题面"},
        {"track": "track1", "name": "ManiSkill"},
        {"track": "track2", "question": ""},
        {"track": "track2", "question": None},
        {},
    ]
    for payload in cases:
        r = classify_run(payload, index)
        assert r == {"run_mode": "standard", "v1_archive": None,
                     "ascent_targets": {}}, payload
    # 空索引 / None 索引 / 损坏形态不影响标准题
    assert classify_run({"track": "track2", "question": "x"},
                        None)["run_mode"] == "standard"
    assert classify_run({"track": "track2", "question": "x"},
                        {"entries": []})["run_mode"] == "standard"


def test_v1_index_paths_locked_with_sha(tmp_path):
    """夹具版路径锁：12 个登记件全部存在且 sha 现场复算一致（测试不自报 sha）。"""
    index, workdir = _build_fixture_index(tmp_path)
    entry = index["entries"][0]
    assert sha256_text(entry["question"]) == entry["question_sha256"]
    assert len(entry["files"]) == 12
    for f in entry["files"]:
        p = workdir / f["relpath"]
        assert p.is_file(), f"登记件缺失：{p}"
        assert _sha256_file(p) == f["sha256"]
    # 任一登记件被改动 → 复算 sha 立即对不上（完整性闸生效）
    tampered = workdir / "summary.json"
    tampered.write_text(tampered.read_text(encoding="utf-8") + " ",
                        encoding="utf-8")
    assert _sha256_file(tampered) != entry["files"][0]["sha256"]
    # 题面一字漂移：逐字匹配立即落 standard（不做模糊匹配）
    drifted = {"track": "track2",
               "question": entry["question"] + "？"}
    assert classify_run(drifted, index)["run_mode"] == "standard"


@pytest.mark.skipif(
    os.environ.get("SIMPRAXIS_V1_INDEX_REAL") != "1",
    reason="真实 V1 档案闸：仅在 SIMPRAXIS_V1_INDEX_REAL=1 时读 "
           "<DEFAULT_DATA_ROOT>/phase2/v1_index.json（conftest 会把 "
           "SIMPRAXIS_PHASE2_ROOT 重定向到 tmp，故此处显式指向操作件）；"
           "缺省 skip，零 /mnt 硬编码")
def test_v1_index_real_archive_locked():
    """唯一允许的 skip 守卫：对操作件做存在性 + sha 现场复算（路径来自档案自身）。"""
    from orchestrator.common.runtime_paths import DEFAULT_DATA_ROOT
    operational = Path(DEFAULT_DATA_ROOT) / "phase2" / "v1_index.json"
    index = load_v1_index(operational)
    assert len(index["entries"]) == 1
    entry = index["entries"][0]
    assert entry["thread_id"] == THREAD_ID
    # 题面与仓库冻结样本逐字一致（sha 现场复算，不抄字面量）
    assert entry["question"] == get_input("track2", 1)["question"]
    assert sha256_text(entry["question"]) == entry["question_sha256"]
    workdir = Path(entry["host_workdir"])
    assert workdir.is_dir()
    rels = [f["relpath"] for f in entry["files"]]
    assert len(rels) == 12 and rels == list(REGISTERED_RELS)
    for f in entry["files"]:
        p = workdir / f["relpath"]
        assert p.is_file(), f"真实登记件缺失：{p}"
        assert _sha256_file(p) == f["sha256"], f"sha 不符：{p}"
    result = classify_run(
        {"track": "track2", "question": entry["question"]}, index)
    assert result["run_mode"] == "ascent"


def test_missing_index_standard_corrupt_fail_closed(tmp_path):
    # 缺失 = V1IndexMissing（调用方按空索引处理），损坏 = fail-closed
    with pytest.raises(V1IndexMissing):
        load_v1_index(tmp_path / "absent.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(V1IndexCorrupt):
        load_v1_index(bad)
    bad.write_text(json.dumps({"schema": "other", "entries": []}),
                   encoding="utf-8")
    with pytest.raises(V1IndexCorrupt):
        load_v1_index(bad)
    # 节点语义：loader 抛缺失 → standard；抛损坏 → GraphStateError
    state = {"track": "track2", "question": "q"}

    def missing_loader():
        return load_v1_index(tmp_path / "absent.json")

    out = node_classify(state, GraphDeps(v1_index=missing_loader))
    assert out["run_mode"] == "standard" and "events" not in out

    def corrupt_loader():
        return load_v1_index(bad)

    with pytest.raises(GraphStateError):
        node_classify(state, GraphDeps(v1_index=corrupt_loader))
    # 索引内部不一致（题面逐字命中但 sha 不符）同样 fail-closed
    _, _ = _build_fixture_index(tmp_path / "sub")
    good = load_v1_index(tmp_path / "sub" / "v1_index.json")
    good["entries"][0]["question_sha256"] = "a" * 64
    with pytest.raises(V1IndexCorrupt):
        classify_run(
            {"track": "track2",
             "question": good["entries"][0]["question"]}, good)


# ---------------------------------------------------------------------------
# Pareto 支配判定
# ---------------------------------------------------------------------------

def test_pareto_dominance_three_verdicts():
    v1 = {"success_rate": 0.0, "slip_dist_mean_m": 0.3,
          "postpush_vmax_mean_mps": 10.0, "tilt_max_deg_max": 150.0,
          "solve_wallclock_s": 100.0, "vram_peak_gb": 20.0}
    # 严格优于一项、其余持平 → v2_dominates
    better = dict(v1, success_rate=0.1)
    assert par.pareto_verdict(v1, better) == "v2_dominates"
    # 严格劣于一项 → dominated_by_v1（同一 V1 锚点下的有序判定）
    worse = dict(v1, vram_peak_gb=30.0)
    assert par.pareto_verdict(v1, worse) == "dominated_by_v1"
    # 角色互换的互补性只能在 ε=0 同形表下断言（相对 ε 带随第一参数重锚，
    # 不承诺互换镜像；见 test_pareto_ordered_relation_anchor_asymmetry）
    zero_eps = {m: ("absolute", 0.0) for m in par.METRIC_EPSILON}
    assert par.pareto_verdict(better, v1, epsilons=zero_eps) \
        == "dominated_by_v1"
    assert par.pareto_verdict(worse, v1, epsilons=zero_eps) == "v2_dominates"
    # 互有胜负 → non_dominated；全等/全 ε 内持平 → non_dominated
    mixed = dict(v1, success_rate=0.1, vram_peak_gb=30.0)
    assert par.pareto_verdict(v1, mixed) == "non_dominated"
    assert par.pareto_verdict(v1, dict(v1)) == "non_dominated"
    # 精确相对 ε 边界：0.05 非二进制有限小数、浮点上恰在界沿会落 1 ulp 侧，
    # 故注入同形的二进制约 12.5% 容差表（eps=1/8），v1=1/8：band=1/64、
    # v2=7/64 全部精确可表示，严格验证「ε 含端点=持平，越界才算胜负」。
    dyadic_eps = {m: (("relative", 0.125) if m == "slip_dist_mean_m" else e)
                  for m, e in par.METRIC_EPSILON.items()}
    base = dict(v1, slip_dist_mean_m=0.125)
    band = 0.125 * abs(base["slip_dist_mean_m"])  # =0.015625 精确
    v_at = base["slip_dist_mean_m"] - band        # =0.109375 精确
    assert par.pareto_verdict(
        base, dict(base, slip_dist_mean_m=v_at),
        epsilons=dyadic_eps) == "non_dominated"
    assert par.pareto_verdict(
        base, dict(base, slip_dist_mean_m=v_at - 1e-15),
        epsilons=dyadic_eps) == "v2_dominates"
    assert par.pareto_verdict(
        base, dict(base, slip_dist_mean_m=base["slip_dist_mean_m"] + band),
        epsilons=dyadic_eps) == "non_dominated"
    assert par.pareto_verdict(
        base, dict(base, slip_dist_mean_m=base["slip_dist_mean_m"] + band
                   + 1e-15),
        epsilons=dyadic_eps) == "dominated_by_v1"
    # 规范 5% 表：明确落在 ε 内/外的两点（不取 ulp 边沿）
    band5 = 0.05 * abs(v1["slip_dist_mean_m"])
    assert par.pareto_verdict(
        v1, dict(v1, slip_dist_mean_m=v1["slip_dist_mean_m"] - band5 * 0.5)
        ) == "non_dominated"
    assert par.pareto_verdict(
        v1, dict(v1, slip_dist_mean_m=v1["slip_dist_mean_m"] - band5 * 2)
        ) == "v2_dominates"
    # success_rate 容差绝对 0：任何正提升都严格优于
    assert par.pareto_verdict(v1, dict(v1, success_rate=1e-300)) == "v2_dominates"


def test_pareto_ordered_relation_anchor_asymmetry():
    """QA-1 LOW-1：verdict 是 V2 相对 V1 的有序关系，ε 带单向锚定第一参数。

    禁止反序比较：同一对数值在两个调用方向上可以得到不同判定
    （容差带随锚点重算）。两方向结果都固化为预期，防 Task4/6 接线者
    误以为 pareto_verdict 可交换。
    """
    base = {m: 10.0 for m in par.METRIC_DIRECTIONS}
    base["success_rate"] = 0.5  # 两表同值持平（避开与本断言无关的 0 锚点）
    # -1 方向指标：候选 9.51 = 10×(1−0.049)，改善 4.9%
    cand = dict(base, slip_dist_mean_m=10.0 * (1.0 - 0.049))
    # 正向（档案=10、候选=9.51）：0.49 落在锚 10 的 5% 带（0.5）内=持平
    assert par.pareto_verdict(base, cand) == "non_dominated"
    # 反序（错误用法）：带重锚为 0.05×9.51=0.4755，0.49 越带 → 第二参数
    # 10 相对第一参数 9.51 严格更劣 → dominated_by_v1。
    assert par.pareto_verdict(cand, base) == "dominated_by_v1"


def test_pareto_fail_closed():
    v1 = {m: 1.0 for m in par.METRIC_DIRECTIONS}
    with pytest.raises(par.ParetoInputError):  # 缺指标
        par.pareto_verdict(v1, {k: v for k, v in v1.items() if k != "vram_peak_gb"})
    with pytest.raises(par.ParetoInputError):  # 多余指标
        par.pareto_verdict(v1, dict(v1, extra=1.0))
    with pytest.raises(par.ParetoInputError):  # NaN
        par.pareto_verdict(v1, dict(v1, tilt_max_deg_max=float("nan")))
    with pytest.raises(par.ParetoInputError):  # inf
        par.pareto_verdict(v1, dict(v1, solve_wallclock_s=float("inf")))
    with pytest.raises(par.ParetoInputError):  # bool 冒充数值
        par.pareto_verdict(v1, dict(v1, success_rate=True))
    with pytest.raises(par.ParetoInputError):  # 方向冲突
        par.pareto_verdict(
            v1, dict(v1),
            directions=dict(par.METRIC_DIRECTIONS, vram_peak_gb=1))


def test_pareto_no_weighted_total():
    """禁止加权总分/标量化：源码 AST 不得出现权重/总分类标识符。"""
    src = Path(par.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    banned_fragments = ("weight", "score", "scalar")
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
                             ast.Name)):
            ident = node.name if hasattr(node, "name") else node.id
            assert not any(frag in ident.lower() for frag in banned_fragments), \
                f"pareto.py 出现加权/总分标识符：{ident}"
    # 常量只声明方向与容差，不声明任何权重表
    assert set(par.METRIC_DIRECTIONS.values()) <= {1, -1}
    assert par.METRIC_EPSILON["success_rate"] == ("absolute", 0.0)
    assert par.METRIC_EPSILON["slip_dist_mean_m"] == ("relative", 0.05)


# ---------------------------------------------------------------------------
# 加严阈值表
# ---------------------------------------------------------------------------

def test_tightened_thresholds_factors():
    # 物理/预注册锚点：同一对象 re-export（is 身份），值一个不改
    for name in ("FALL_Z_M", "FALL_TILT_DEG", "PUSH_FORCE_N", "PUSH_DUR_S",
                 "CONTRAST_RATIO"):
        assert getattr(at, name) is getattr(gt, name), name
    assert at.FALL_Z_M == 0.85 and at.FALL_TILT_DEG == 50.0
    assert at.PUSH_FORCE_N == 35.0 and at.PUSH_DUR_S == 0.2
    # 因子集中配置
    assert at.TOLERANCE_FACTOR == 0.8
    assert at.ENVELOPE_BOUND_FACTOR == 0.8
    assert at.DR_LOG_EXTEND == 0.30
    # 公差 ×0.8
    assert math.isclose(at.SLIP_DIST_REL_TOL_TIGHT,
                        gt.SLIP_DIST_REL_TOL * 0.8)
    assert math.isclose(at.VMAX_ABS_TOL_TIGHT, gt.VMAX_ABS_TOL * 0.8)
    assert math.isclose(at.TILT_MAX_ABS_TOL_DEG_TIGHT,
                        gt.TILT_MAX_ABS_TOL_DEG * 0.8)
    # 接触/滑移数值界 ×0.8
    assert math.isclose(at.CONTACT_FORCE_N_TIGHT, gt.CONTACT_FORCE_N * 0.8)
    assert math.isclose(at.SLIP_V_MAX_MPS_TIGHT, gt.SLIP_V_MAX_MPS * 0.8)
    assert math.isclose(at.CONTACT_FORCE_MAX_N_TIGHT,
                        gt.CONTACT_FORCE_MAX_N * 0.8)
    assert math.isclose(at.CONTACT_FORCE_MAX_N_DR_TIGHT,
                        gt.CONTACT_FORCE_MAX_N_DR * 0.8)
    # DR 应力端外扩：μ 下界 0.05→0.035、Kp 上界 1e6→1.3e6；另一端按
    # 几何中心不动（lo·hi 乘积不变）对称
    mu0, mu1 = at.DR_MU_LOGUNIFORM_TIGHT
    kp0, kp1 = at.DR_KP_LOGUNIFORM_TIGHT
    assert math.isclose(mu0, 0.035)
    assert kp1 == 1.3e6
    assert math.isclose(mu0 * mu1, 0.05 * 0.4)
    assert math.isclose(kp0 * kp1, 1e4 * 1e6)
    # build_targets：夹具 V1 包络 → 加严目标；漂移即 fail-closed
    summary = json.loads(SUMMARY_FIXTURE.read_text(encoding="utf-8"))
    targets = at.build_targets(summary["dr_envelope"])
    assert math.isclose(
        targets["dr_envelope_tight"]["mu_loguniform"][0], 0.035)
    assert targets["dr_envelope_tight"]["Kp_loguniform"][1] == 1.3e6
    # QA-1 LOW-3：Kd 登记基线常量；tight/v1 两表逐值对质后原值透传
    assert gt.DR_KD_LOGUNIFORM == (15.0, 200.0)
    assert targets["dr_envelope_v1"]["Kd_loguniform"] == [15.0, 200.0]
    assert targets["dr_envelope_tight"]["Kd_loguniform"] == [15.0, 200.0]
    with pytest.raises(at.AscentThresholdError):
        at.build_targets(dict(summary["dr_envelope"],
                              mu_loguniform=[0.06, 0.4]))
    with pytest.raises(at.AscentThresholdError):
        at.build_targets(None)
    # QA-1 LOW-3：Kd 漂移与缺失都 fail-closed（不再 None 透传）
    with pytest.raises(at.AscentThresholdError):
        at.build_targets(dict(summary["dr_envelope"],
                              Kd_loguniform=[15.0, 201.0]))
    env_without_kd = {k: v for k, v in summary["dr_envelope"].items()
                      if k != "Kd_loguniform"}
    with pytest.raises(at.AscentThresholdError):
        at.build_targets(env_without_kd)


# ---------------------------------------------------------------------------
# QA-1 回归（C1 入图题面丢失 / M1 命中路径异常归一）
# ---------------------------------------------------------------------------

def test_qa1_c1_index_payload_question_enters_graph(tmp_path):
    """C1：--index 入口入图题面必须取自冻结 payload（纯函数 _initial_state）。

    旧行为两处 initial_state 用 args.question or ""，--index 1 下入图题面
    为空 → 唯一登记 ascent 题静默 standard（Task2 主体在生产路径失效）。
    全程 hermetic：夹具索引 dict 经 GraphDeps 注入，零方舟/飞书/真实件。
    """
    index, _ = _build_fixture_index(tmp_path)
    frozen = get_input("track2", 1)
    args = types.SimpleNamespace(track="track2", thread="t-qa1-c1")
    state = graph_cli._initial_state(args, frozen)
    assert state["question"] == frozen["question"] and state["question"]
    assert state["track"] == "track2" and state["thread_id"] == "t-qa1-c1"
    out = node_classify(state, GraphDeps(v1_index=index))
    assert out["run_mode"] == "ascent"
    assert "events" not in out and out["ascent_targets"]
    # 对照 (a)：旧行为复现——空题面入图 → standard（钉死症状与修复差异）
    stale = graph_cli._initial_state(
        types.SimpleNamespace(track="track2", thread="t-qa1-c1"),
        {"question": ""})
    assert stale["question"] == ""
    assert node_classify(stale, GraphDeps(v1_index=index))["run_mode"] \
        == "standard"
    # 对照 (b)：题面误拼（追加全角问号）逐字不等 → standard
    misspelled = dict(state, question=frozen["question"] + "？")
    assert node_classify(misspelled, GraphDeps(v1_index=index))["run_mode"] \
        == "standard"
    # 对照 (c)：track1 口径——payload["name"] 进入 question
    t1 = graph_cli._initial_state(
        types.SimpleNamespace(track="track1", thread="t1"),
        {"name": "ManiSkill 巡礼"})
    assert t1["question"] == "ManiSkill 巡礼" and t1["track"] == "track1"


def test_qa1_m1_node_classify_hit_path_errors_wrapped(tmp_path):
    """M1：命中路径的 V1IndexCorrupt/AscentThresholdError 归一 GraphStateError。

    cause 链保留可区分来源；索引缺失仍 standard（现有行为不回归）。
    KeyError/TypeError 等真 bug 不在 node_classify 内吞。
    """
    index, _ = _build_fixture_index(tmp_path)
    question = index["entries"][0]["question"]
    state = {"track": "track2", "question": question}
    # 题面逐字命中但登记 sha 被篡改（dict 注入绕过 loader 粗校验）
    tampered = copy.deepcopy(index)
    tampered["entries"][0]["question_sha256"] = "a" * 64
    with pytest.raises(GraphStateError) as ei:
        node_classify(state, GraphDeps(v1_index=tampered))
    assert isinstance(ei.value.__cause__, V1IndexCorrupt)
    # dr_envelope 基线漂移（loader 只查是 dict；build_targets 才逐值对质）
    drift = copy.deepcopy(index)
    drift["entries"][0]["dr_envelope"]["mu_loguniform"] = [0.06, 0.4]
    with pytest.raises(GraphStateError) as ei:
        node_classify(state, GraphDeps(v1_index=drift))
    assert isinstance(ei.value.__cause__, at.AscentThresholdError)
    # Kd 漂移同源归一（LOW-3 收紧在节点路径同样 fail-closed）
    drift_kd = copy.deepcopy(index)
    drift_kd["entries"][0]["dr_envelope"]["Kd_loguniform"] = [15.0, 201.0]
    with pytest.raises(GraphStateError) as ei:
        node_classify(state, GraphDeps(v1_index=drift_kd))
    assert isinstance(ei.value.__cause__, at.AscentThresholdError)
    # 索引文件缺失 → standard 不回归
    def missing_loader():
        return load_v1_index(tmp_path / "absent.json")

    out = node_classify(state, GraphDeps(v1_index=missing_loader))
    assert out["run_mode"] == "standard" and "events" not in out


# ---------------------------------------------------------------------------
# 状态加法兼容 + 图接线
# ---------------------------------------------------------------------------

def test_state_additive_backwards_compatible():
    old_keys = {"thread_id", "track", "question", "proposals", "chosen",
                "gate1_rejects", "agent_runs", "gate2_rejects", "coding_round",
                "telemetry_uri", "metrics", "rung4", "events"}
    annotations = set(SimPraxisState.__annotations__)
    assert old_keys <= annotations
    for new_key in ("run_mode", "v1_archive", "ascent_targets",
                    "pareto_verdict"):
        assert new_key in annotations
    # 老 12 字段快照（无 4 个新键）仍可序列化/被分类节点消费
    old_snapshot = {
        "thread_id": "t", "track": "track2", "question": "q",
        "proposals": [], "chosen": {}, "gate1_rejects": 0,
        "agent_runs": [], "gate2_rejects": 0, "coding_round": 0,
        "telemetry_uri": "", "metrics": {}, "rung4": {}, "events": []}
    json.loads(json.dumps(old_snapshot))
    out = node_classify(old_snapshot, GraphDeps(v1_index={"entries": []}))
    assert out == {"run_mode": "standard", "v1_archive": None,
                   "ascent_targets": {}}


def test_graph_wiring_classify_before_direction():
    compiled = build_graph(None).compile()
    draw = compiled.get_graph()
    pairs = {(e.source, e.target) for e in draw.edges}
    assert ("classify", "direction") in pairs
    # 条件入口两轨都进 classify，不再直连 direction
    start_targets = {e.target for e in draw.edges if e.source == "__start__"}
    assert start_targets == {"classify"}
    assert not any(s == "__start__" and t == "direction" for s, t in pairs)
    node_ids = set(draw.nodes)
    assert {"classify", "direction", "gate1", "agent_run", "evaluate",
            "gate2", "persist_learning", "human_failure", "done"} <= node_ids
    # classify 节点零事件（保 Phase1 七事件序列不被污染）
    out = node_classify({"track": "track1", "question": "x"},
                        GraphDeps(v1_index={"entries": []}))
    assert "events" not in out


# ---------------------------------------------------------------------------
# ascent_claims schema
# ---------------------------------------------------------------------------

def _proposal(name: str, **extra) -> dict:
    p = {
        "name": name, "sim_engine": "mujoco",
        "first_principles": "推导 [S1]", "key_params": "mu=0.1",
        "controls_vs_experiment": "baseline vs ours",
        "risk": "风险 [S1]", "evidence_refs": ["S1"],
        "expected_artifacts": ["metrics.json"]}
    p.update(extra)
    return p


def _catalog_hits():
    catalog = {"mujoco": {"slug": "mujoco", "display_name": "MuJoCo",
                          "kind": "rigid", "coverage": "full", "facts": 1}}
    hits = [{"kb_relpath": "k.md", "section_path": "s", "doc_type": "g",
             "text": "证据", "id": "q1"}]
    return catalog, hits


def test_ascent_proposals_claims_schema():
    catalog, hits = _catalog_hits()
    # ascent 模式：合法 claims（inc/dec 与 ±1 两种写法）规范化通过
    obj = {"question": "题", "proposals": [
        _proposal("A", ascent_claims=[
            {"metric": "success_rate", "direction": "inc", "task": "ice_dr 臂",
             "target": 0.5, "epsilon": 0.0}]),
        _proposal("B", ascent_claims=[
            {"metric": "slip_dist_mean_m", "direction": -1,
             "task": "三臂平均", "target": 0.1, "epsilon": 0.005}])]}
    props, errors = validate_proposals(obj, hits=hits, catalog=catalog,
                                       ascent=True)
    assert errors == []
    assert props[0]["ascent_claims"][0]["direction"] == 1
    assert props[0]["ascent_claims"][0]["metric"] == "success_rate"
    assert props[1]["ascent_claims"][0]["direction"] == -1
    # ascent 模式不带 claims 仍合法（可选字段）
    props, errors = validate_proposals(
        {"question": "题", "proposals": [_proposal("A"), _proposal("B")]},
        hits=hits, catalog=catalog, ascent=True)
    assert errors == []
    assert all("ascent_claims" not in p for p in props)

    def expect_error(prop, *, ascent=True, field_hint=None):
        o = {"question": "题", "proposals": [prop, _proposal("B")]}
        _, errs = validate_proposals(o, hits=hits, catalog=catalog,
                                     ascent=ascent)
        assert errs, f"应拒绝：{prop}"
        if field_hint:
            assert any(field_hint in e for e in errs), errs

    # 方向词越界 / 与改善方向冲突 / 指标未知 / 数值非法 / task 缺失
    expect_error(_proposal(
        "A", ascent_claims=[{"metric": "success_rate", "direction": "up",
                              "task": "t", "target": 1, "epsilon": 0}]),
        field_hint="direction")
    expect_error(_proposal(
        "A", ascent_claims=[{"metric": "success_rate", "direction": -1,
                              "task": "t", "target": 1, "epsilon": 0}]),
        field_hint="冲突")
    expect_error(_proposal(
        "A", ascent_claims=[{"metric": "slip_dist_mean_m", "direction": "inc",
                              "task": "t", "target": 0.1, "epsilon": 0}]),
        field_hint="冲突")
    expect_error(_proposal(
        "A", ascent_claims=[{"metric": "nonsense", "direction": 1,
                              "task": "t", "target": 1, "epsilon": 0}]),
        field_hint="metric")
    expect_error(_proposal(
        "A", ascent_claims=[{"metric": "vram_peak_gb", "direction": -1,
                              "task": "t", "target": "x", "epsilon": 0}]),
        field_hint="target")
    expect_error(_proposal(
        "A", ascent_claims=[{"metric": "vram_peak_gb", "direction": -1,
                              "task": "t", "target": 1, "epsilon": -0.1}]),
        field_hint="epsilon")
    expect_error(_proposal(
        "A", ascent_claims=[{"metric": "vram_peak_gb", "direction": -1,
                              "task": "  ", "target": 1, "epsilon": 0}]),
        field_hint="task")
    expect_error(_proposal("A", ascent_claims=[]), field_hint="非空数组")
    # standard 模式出现 claims 即协议外数据
    expect_error(_proposal(
        "A", ascent_claims=[{"metric": "success_rate", "direction": 1,
                              "task": "t", "target": 1, "epsilon": 0}]),
        ascent=False, field_hint="standard")


# ---------------------------------------------------------------------------
# Task6 入场 LOW-4：跨条目去重（同题 / 同 thread_id 登记两条即损坏）
# ---------------------------------------------------------------------------

def _write_index(tmp_path: Path, index: dict) -> Path:
    p = tmp_path / "v1_index.json"
    p.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
    return p


def test_v1_index_duplicate_question_sha_rejected(tmp_path):
    index, _ = _build_fixture_index(tmp_path / "a")
    second = copy.deepcopy(index["entries"][0])
    second["thread_id"] = "t8-other-20261017a"  # 同题不同 thread 仍不许两条
    index["entries"].append(second)
    p = _write_index(tmp_path, index)
    with pytest.raises(V1IndexCorrupt) as ei:
        load_v1_index(p)
    msg = str(ei.value)
    assert "entries[0]" in msg and "entries[1]" in msg
    assert "重复登记同一题" in msg


def test_v1_index_duplicate_thread_id_rejected(tmp_path):
    index, _ = _build_fixture_index(tmp_path / "a")
    other_q = "另一道完全不同的、合法登记的 track2 题面"
    second = copy.deepcopy(index["entries"][0])
    second["question"] = other_q
    second["question_sha256"] = sha256_text(other_q)
    index["entries"].append(second)  # thread_id 与首条相同
    p = _write_index(tmp_path, index)
    with pytest.raises(V1IndexCorrupt) as ei:
        load_v1_index(p)
    msg = str(ei.value)
    assert "entries[0]" in msg and "entries[1]" in msg
    assert "重复登记同一" in msg and THREAD_ID in msg


# ---------------------------------------------------------------------------
# Task6 入场 LOW-6：v1_metrics.groups 的 summary.json 形态严格校验
# ---------------------------------------------------------------------------

def _groups_index(tmp_path: Path) -> tuple[Path, dict]:
    index, _ = _build_fixture_index(tmp_path)
    return _write_index(tmp_path, index), index


def _groups_of(index: dict) -> dict:
    return index["entries"][0]["v1_metrics"]["groups"]


def test_groups_valid_summary_shape_loads(tmp_path):
    # 夹具实样（三臂、定长向量）原样通过
    p, _ = _groups_index(tmp_path)
    loaded = load_v1_index(p)
    assert set(loaded["entries"][0]["v1_metrics"]["groups"]) == \
        {"normal", "ice_nominal", "ice_dr"}
    # 生产实样每臂多一个 fall_frames 臂内杂项键：闸只钉对质字段，不拒臂内余键
    index2, _ = _build_fixture_index(tmp_path / "prodshape")
    for arm in ("normal", "ice_nominal", "ice_dr"):
        _groups_of(index2)[arm]["fall_frames"] = [62, 93, 254]
    p2 = _write_index(tmp_path / "prodshape", index2)
    assert load_v1_index(p2)["entries"][0]["thread_id"] == THREAD_ID


@pytest.mark.parametrize("case_id,mutate,fragments", [
    ("groups_missing", lambda g, vm: vm.pop("groups"), ("groups", "对象")),
    ("groups_not_object", lambda g, vm: vm.update(groups=[]), ("groups", "对象")),
    ("arm_missing", lambda g, vm: g.pop("ice_dr"),
     ("ice_dr", "缺")),
    ("arm_extra", lambda g, vm: g.update(ice_extra=copy.deepcopy(g["normal"])),
     ("ice_extra", "多余")),
    ("arm_not_object", lambda g, vm: g.update(normal=[]),
     ("groups.normal", "对象")),
    ("n_bool", lambda g, vm: g["normal"].update(n=True),
     ("normal.n", "整数")),
    ("n_zero", lambda g, vm: g["normal"].update(n=0),
     ("normal.n", "≥1")),
    ("n_float", lambda g, vm: g["normal"].update(n=3.0),
     ("normal.n", "整数")),
    ("n_string", lambda g, vm: g["normal"].update(n="3"),
     ("normal.n", "整数")),
    ("n_fell_bool", lambda g, vm: g["ice_nominal"].update(n_fell=True),
     ("ice_nominal.n_fell",)),
    ("n_fell_gt_n", lambda g, vm: g["normal"].update(n_fell=4),
     ("normal.n_fell", "0≤x≤n")),
    ("n_fell_negative", lambda g, vm: g["normal"].update(n_fell=-1),
     ("normal.n_fell",)),
    ("success_rate_bool", lambda g, vm: g["normal"].update(success_rate=True),
     ("normal.success_rate",)),
    ("success_rate_over_one", lambda g, vm: g["normal"].update(success_rate=1.01),
     ("normal.success_rate", "[0,1]")),
    ("success_rate_nan", lambda g, vm: g["normal"].update(
        success_rate=float("nan")), ("normal.success_rate",)),
    ("success_rate_inf", lambda g, vm: g["normal"].update(
        success_rate=float("inf")), ("normal.success_rate",)),
    ("tilt_bool", lambda g, vm: g["ice_dr"].update(tilt_max_deg_max=True),
     ("ice_dr.tilt_max_deg_max",)),
    ("tilt_negative", lambda g, vm: g["ice_dr"].update(tilt_max_deg_max=-0.5),
     ("ice_dr.tilt_max_deg_max", "≥0")),
    ("tilt_nan", lambda g, vm: g["ice_dr"].update(
        tilt_max_deg_max=float("nan")), ("ice_dr.tilt_max_deg_max",)),
    ("slip_dist_too_short",
     lambda g, vm: g["normal"].update(slip_dist_m_mean=[0.1]),
     ("normal.slip_dist_m_mean", "长度必须恰为 2")),
    ("slip_dist_too_long",
     lambda g, vm: g["normal"].update(slip_dist_m_mean=[0.1, 0.2, 0.3]),
     ("normal.slip_dist_m_mean", "长度必须恰为 2")),
    ("slip_dist_nan_element",
     lambda g, vm: g["normal"].update(
         slip_dist_m_mean=[0.1, float("nan")]),
     ("normal.slip_dist_m_mean[1]",)),
    ("slip_dist_bool_element",
     lambda g, vm: g["normal"].update(slip_dist_m_mean=[0.1, True]),
     ("normal.slip_dist_m_mean[1]",)),
    ("vmax_wrong_length",
     lambda g, vm: g["ice_nominal"].update(
         slip_vmax_postpush_mps_max=[5.6, 1.2]),
     ("ice_nominal.slip_vmax_postpush_mps_max", "长度必须恰为 1")),
    ("vmax_string_element",
     lambda g, vm: g["ice_nominal"].update(
         slip_vmax_postpush_mps_max=["5.6"]),
     ("ice_nominal.slip_vmax_postpush_mps_max[0]",)),
    ("mu_length_mismatch",
     lambda g, vm: g["ice_dr"].update(mu_per_seed=[0.1, 0.2]),
     ("ice_dr.mu_per_seed", "n（=3）")),
    ("mu_bool_element",
     lambda g, vm: g["ice_dr"].update(mu_per_seed=[0.1, 0.2, False]),
     ("ice_dr.mu_per_seed[2]",)),
    ("mu_inf_element",
     lambda g, vm: g["ice_dr"].update(
         mu_per_seed=[0.1, 0.2, float("inf")]),
     ("ice_dr.mu_per_seed[2]",)),
])
def test_groups_corrupt_shapes_fail_closed(tmp_path, case_id, mutate, fragments):
    base, _ = _build_fixture_index(tmp_path / "base")
    vm = base["entries"][0]["v1_metrics"]
    mutate(_groups_of(base), vm)
    p = _write_index(tmp_path / "base", base)
    with pytest.raises(V1IndexCorrupt) as ei:
        load_v1_index(p)
    msg = str(ei.value)
    for frag in fragments:
        assert frag in msg, (case_id, frag, msg)


def test_standard_messages_byte_freeze():
    """standard 路径消息逐字节冻结（golden 夹具），且不含任何 ascent 片段。

    golden 是 Phase1/HEAD 2d83528 行为的人工独立重建件：禁止用
    build_first_messages 回灌生成；任何 prompt 变更须先经评审，并同步说明
    与 Phase1 的字节差异（QA-1 LOW-5）。夹具 JSON 本身不动。
    """
    catalog = {"mujoco": {"slug": "mujoco", "display_name": "MuJoCo",
                          "kind": "rigid", "coverage": "full", "facts": 3}}
    hits = [{"kb_relpath": "k.md", "section_path": "s1", "doc_type": "guide",
             "text": "证据甲", "id": "q1"},
            {"kb_relpath": "k2.md", "section_path": "s2", "doc_type": "guide",
             "text": "证据乙", "id": "q2"}]
    msgs = build_first_messages(track="track2", payload={"question": "题面ABC"},
                                hits=hits, catalog=catalog)
    golden = json.loads(GOLDEN_FIXTURE.read_text(encoding="utf-8"))
    assert msgs == golden
    joined = json.dumps(msgs, ensure_ascii=False)
    assert "ascent" not in joined and "升阶" not in joined
    assert "ascent_claims" not in joined
