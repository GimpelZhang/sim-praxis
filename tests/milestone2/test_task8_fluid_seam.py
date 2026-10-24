"""Phase 2 Task8 Stage0 流体能力探针 + 图接缝零付费五测（附录 A L958-962）。

五测严格对应计划：
  1. 反射探针不硬编码求解器 API 名（合成 materials 模块注入，含重命名/异类 API）；
  2. 粒子上限常量单点 20000，CLI 20001 拒绝；
  3. VRAM 看门狗配置点（35GB 优雅 FAILURE，下调最多 2 次，不抛 OOM）；
  4. 85% 入杯率纯几何语义（末 0.5s 窗、中位帧、严格边界、飞溅分列、无像素）；
  5. 能力缺口白皮书入证据包清单且回链探针报告编号（仅 fail 路径，无假内容）。

纪律：全程不装/不导入 genesis（探针模块 genesis/torch 惰性，宿主可收集），
无网络、无付费、无 GPU 实跑；合成件全部函数内构造，SIMPRAXIS_PHASE2_ROOT
由 tests/conftest.py autouse 夹具重定向 tmp。
"""
from __future__ import annotations

import importlib.util
import inspect
import json
import re
import subprocess
import sys
import types
from pathlib import Path

import pytest

from orchestrator.graph import task8_fluid as seam
from orchestrator.graph.nodes import GraphStateError
from orchestrator.solutions import validate_solution_id

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_probe():
    path = _REPO_ROOT / "sandbox" / "probes" / "probe_genesis_fluid.py"
    spec = importlib.util.spec_from_file_location(
        "simpraxis_test_probe_genesis_fluid", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclass + PEP563 解析要求 exec 前在册
    spec.loader.exec_module(mod)
    return mod


probe = _load_probe()

THREAD = "t8q5-fluid-20261024a"


# ---------------------------------------------------------------------------
# 合成 materials 模块构造
# ---------------------------------------------------------------------------

def _ns(namespace_name: str, class_specs: dict[str, type]) -> types.ModuleType:
    """造一个 materials 子命名空间（模块形），内含给定类属性。"""
    mod = types.ModuleType(f"fake_materials.{namespace_name}")
    for leaf_name, cls in class_specs.items():
        setattr(mod, leaf_name, cls)
    return mod


def _fake_materials() -> types.ModuleType:
    """合成反射结构（命名空间全部改名，证明发现逻辑不认字面求解器名）。

    - Weird：只有一个液体叶子（最专用族 → 首选）；
    - Zzz：叶子改名 Water、带粘度参数（重命名 API 仍须被发现）；
    - Zorp：液体叶子 + Cloth/Smoke 非流体叶子（具体叶子多 → 次选族）；
    - Rigidish：顶层直接材料类，无嵌套类、无液体特征（不得成为命名空间/候选）；
    - PLANCK/helper_fn：非类噪声，必须跳过。
    """
    class Liquid:  # 角色名命中
        def __init__(self, rho: float = 1000.0):
            self.rho = rho

    class Water:  # 叶子改名；构造参数含粘度片段
        def __init__(self, viscosity: float = 0.01):
            self.viscosity = viscosity

    class ViscousRelaxed:
        def __init__(self, viscosity_relaxation: float = 0.01):
            pass

    class Cloth:
        pass

    class Smoke:
        pass

    class Base:
        """族基类：__name__=='Base' 不计具体叶子（与真实 1.3.3 同构）。"""

    class Rigidish:
        """A rigid material with no nested classes."""

    fake = types.ModuleType("fake_gs_materials")
    setattr(fake, "Weird", _ns("Weird", {"Liquid": Liquid, "Base": Base}))
    setattr(fake, "Zzz", _ns("Zzz", {"Water": Water}))
    setattr(fake, "Zorp", _ns(
        "Zorp", {"Liquid": ViscousRelaxed, "Cloth": Cloth, "Smoke": Smoke}))
    setattr(fake, "Rigidish", Rigidish)
    fake.PLANCK = 6.626e-34

    def helper_fn():
        return None

    fake.helper_fn = helper_fn
    return fake


# ---------------------------------------------------------------------------
# 测 1：反射探针不硬编码 API 名
# ---------------------------------------------------------------------------

def test_stage0_probe_reflection_no_hardcoded_api():
    fake = _fake_materials()
    candidates, listing = probe.discover_fluid_candidates(fake)

    # 反射名单完整落证：顶层名/嵌套名全部来自 dir()
    assert set(["Weird", "Zzz", "Zorp", "Rigidish", "PLANCK", "helper_fn"
                ]).issubset(set(listing["top"]))
    assert set(listing["nested"]) == {"Weird", "Zzz", "Zorp"}
    assert "Liquid" in listing["nested"]["Weird"]
    assert "Cloth" in listing["nested"]["Zorp"]

    # 结构偏好（叶子越少越专用，同票名字典序）：Weird→Zzz→Zorp；
    # 重命名的 Zzz.Water 靠粘度参数被发现，Zorp 的 Cloth/Smoke 不干扰。
    assert [c.api_path for c in candidates] == [
        "Weird.Liquid", "Zzz.Water", "Zorp.Liquid"]
    assert [c.family_index for c in candidates] == [0, 1, 1]
    assert probe.outcome_for_family(0) == "sph_ok"
    assert probe.outcome_for_family(1) == "pbd_ok"

    # 实例化确实拿到注入的合成构造器（反射可用，不是名字字符串游戏）
    assert isinstance(candidates[0].constructor(), object)
    assert candidates[1].constructor().viscosity == 0.01

    # 重命名叶子走文档串液体词也应被发现（无角色名、无粘度参数的兜底路径）
    class DocLiquid:
        """a liquid material for a renamed solver family."""

    class Base:
        pass

    renamed = types.ModuleType("fake_materials.Qux")
    renamed.Base = Base
    renamed.DocLiquid = DocLiquid
    fake2 = types.ModuleType("fake_gs_materials2")
    fake2.Qux = renamed
    cands2, _ = probe.discover_fluid_candidates(fake2)
    assert [c.api_path for c in cands2] == ["Qux.DocLiquid"]
    assert cands2[0].family_index == 0

    # 异类 API：无液体特征的命名空间/叶子不入选
    class Plain:
        def __init__(self, stiffness: float = 1.0):
            pass

    plain_ns = types.ModuleType("fake_materials.Pln")
    plain_ns.Thing = Plain
    fake3 = types.ModuleType("fake_gs_materials3")
    fake3.Pln = plain_ns
    assert probe.discover_fluid_candidates(fake3)[0] == []

    # 源码断言：发现链任何函数都不得字面分支求解器名；检测必须派生自 dir()
    forbidden = re.compile(r"\b(?:SPH|PBD|MPM)\b")
    detection_src = "\n".join(
        inspect.getsource(fn) for fn in (
            probe._public_names,
            probe.reflect_material_names,
            probe._is_namespace,
            probe._is_fluid_constructor,
            probe.discover_fluid_candidates,
            probe.outcome_for_family,
        ))
    assert not forbidden.search(detection_src), (
        "反射发现链不允许出现 SPH/PBD/MPM 字面分支")
    assert "dir(" in detection_src, "候选必须从 dir() 反射派生"


# ---------------------------------------------------------------------------
# 测 2：粒子上限常量单点 20000
# ---------------------------------------------------------------------------

def test_particle_cap_constant_20000():
    assert probe.PARTICLE_CAP == 20000
    source = inspect.getsource(probe)
    # 硬天花板赋值只允许常量单点（其余出现只能是注释/文案引用常量名）
    cap_assigns = re.findall(r"^PARTICLE_CAP\s*=\s*\d+", source, re.M)
    assert cap_assigns == ["PARTICLE_CAP = 20000"]
    # Stage0 最小场景上限与硬天花板是两层不同的点
    assert probe.STAGE0_N_MAX == 2000 < probe.PARTICLE_CAP

    parser = probe.build_arg_parser()
    parsed = parser.parse_args(["--out", "/tmp/x.json", "--n", "20000"])
    assert parsed.n == 20000
    with pytest.raises(SystemExit):
        parser.parse_args(["--out", "/tmp/x.json", "--n", "20001"])
    with pytest.raises(SystemExit):
        parser.parse_args(["--out", "/tmp/x.json", "--n", "0"])

    # Stage0 策略闸：2000 允许，2001 拒绝（20000 只属于 Stage1）
    assert probe.validate_stage0_budget(2000) == 2000
    with pytest.raises(ValueError):
        probe.validate_stage0_budget(2001)
    with pytest.raises(ValueError):
        probe.validate_stage0_budget(True)


# ---------------------------------------------------------------------------
# 测 3：VRAM 看门狗配置点
# ---------------------------------------------------------------------------

def test_vram_watchdog_config_points():
    assert probe.VRAM_ABORT_GB == 35.0
    source = inspect.getsource(probe)
    vram_assigns = re.findall(r"^VRAM_ABORT_GB\s*=\s*[\d.]+", source, re.M)
    assert vram_assigns == ["VRAM_ABORT_GB = 35.0"]

    # 越过 35GB 才中止（严格大于；恰 35 不属「超过」）；判定永不抛异常
    assert probe.vram_watchdog(36.0).abort is True
    assert probe.vram_watchdog(35.0).abort is False
    assert probe.vram_watchdog(34.9).abort is False
    for bad in (float("nan"), float("inf"), "n/a", -1.0):
        verdict = probe.vram_watchdog(bad)
        assert verdict.abort is True and isinstance(verdict.reason, str)
    # 总开关关掉时不中止
    assert probe.vram_watchdog(100.0, guard=False).abort is False

    # 优雅 FAILURE 件：结构化、JSON 可序列化、带独立 rc 语义，绝不产 OOM
    failure = probe.vram_abort_attempt(
        "Weird.Liquid", n_particles=1000, steps=probe.STAGE0_STEPS,
        reductions=probe.MAX_REDUCTIONS, peak_gb=36.2,
        reason="显存 36.2GB 超过中止线")
    assert failure["passed"] is False
    assert failure["finite_coords"] is False
    assert failure["vram_abort"] is True
    assert failure["reductions"] == 2
    assert failure["completed_steps"] == 0
    assert failure["position_accessor"] == probe.POSITION_ACCESSOR_UNAVAILABLE
    json.dumps(failure)  # 不得含不可序列化件
    assert probe.RC_OK != probe.RC_VRAM_ABORT != probe.RC_BOTH_FAIL
    assert probe.RC_VRAM_ABORT == 3 and probe.RC_BOTH_FAIL == 4

    # 参数下调：最多 2 次；先砍粒子预算，再砍子步；地板 125
    n1, s1 = probe.reduced_attempt_params(0, 1000, probe.SUBSTEP_MAX)
    assert (n1, s1) == (500, probe.SUBSTEP_MAX)
    n2, s2 = probe.reduced_attempt_params(1, n1, s1)
    assert (n2, s2) == (250, probe.SUBSTEP_MAX // 2)
    with pytest.raises(ValueError):
        probe.reduced_attempt_params(probe.MAX_REDUCTIONS, n2, s2)
    n_floor, _ = probe.reduced_attempt_params(0, 200, 28)
    assert n_floor == probe.MIN_PARTICLE_BUDGET == 125


# ---------------------------------------------------------------------------
# 测 4：85% 入杯率纯几何语义
# ---------------------------------------------------------------------------

def _frame(n_in=85, n_splash=10, n_outside=3, n_below=2):
    """造一帧合成粒子：杯内/飞溅(>=rim)/侧壁外/杯底以下 四类圆柱坐标。"""
    import numpy as np
    pts = []
    pts += [[0.05, 0.0, 0.10]] * n_in          # r<R，bottom<z<rim
    pts += [[0.02, 0.0, 0.30]] * n_splash      # z>=rim
    pts += [[0.50, 0.0, 0.10]] * n_outside     # r>=R，中段高度
    pts += [[0.02, 0.0, -0.01]] * n_below      # z<=bottom
    return np.asarray(pts, dtype=np.float64)


def test_cup_entry_semantics_geometry():
    import numpy as np
    cup = probe.CupGeometry(inner_radius=0.1, bottom_z=0.0, rim_z=0.2)

    # 严格边界：贴壁/贴底/贴口的粒子一律不入杯，归入越界/飞溅
    boundary = np.asarray([
        [0.10, 0.0, 0.10],   # 恰在壁面 r==R
        [0.05, 0.0, 0.00],   # 恰在杯底 z==bottom
        [0.05, 0.0, 0.20],   # 恰在杯口 z==rim
    ], dtype=np.float64)
    bc = probe.classify_particles(boundary, cup)
    assert bc["in_cup"] == 0
    assert bc["outside_wall"] == 1
    assert bc["below_bottom"] == 1
    assert bc["splash"] == 1
    assert bc["out_of_bounds"] == 2
    assert bc["n"] == 3

    one_frame = _frame()
    c = probe.classify_particles(one_frame, cup)
    assert (c["in_cup"], c["splash"], c["outside_wall"], c["below_bottom"]) \
        == (85, 10, 3, 2)
    assert c["in_cup_ratio"] == 0.85

    # 末 0.5s 沉降窗 @50Hz = 25 帧；中位计数帧 = 下标 12
    assert probe.settling_window_frame_count(0.02) == 25
    window = np.stack([_frame() for _ in range(25)])
    m = probe.cup_entry_metrics(window, cup, frame_dt=0.02)
    assert m["window_frames"] == 25
    assert m["count_moment_frame"] == 12
    assert m["ratio_at_count_moment"] == pytest.approx(0.85)
    assert m["entry_ratio_median"] == pytest.approx(0.85)
    assert m["splash_ratio_median"] == pytest.approx(0.10)
    assert m["out_of_bounds_ratio_median"] == pytest.approx(0.05)

    # 整条 60 帧轨迹：只取末 25 帧（前 35 帧全飞溅不得影响结果）
    splash_only = np.stack([_frame(0, 100, 0, 0) for _ in range(35)])
    trajectory = np.concatenate([splash_only, window], axis=0)
    m2 = probe.cup_entry_metrics(trajectory, cup, frame_dt=0.02)
    assert m2["entry_ratio_median"] == pytest.approx(0.85)

    # 帧数放不下整窗 → fail-closed（计划：语义字段无法从几何算出即 STOP）
    short = np.stack([_frame() for _ in range(10)])
    with pytest.raises(ValueError):
        probe.cup_entry_metrics(short, cup, frame_dt=0.02)
    with pytest.raises(ValueError):
        probe.CupGeometry(inner_radius=0.0, bottom_z=0.0, rim_z=1.0)
    with pytest.raises(ValueError):
        probe.CupGeometry(0.1, bottom_z=0.5, rim_z=0.2)

    # 零像素：几何函数链路源码不得出现任何渲染/像素/相机词
    geom_src = "\n".join(inspect.getsource(fn) for fn in (
        probe.classify_particles, probe.cup_entry_metrics,
        probe.settling_window_frame_count))
    assert not re.search(r"pixel|image|render|camera|opencv",
                         geom_src, re.IGNORECASE)


# ---------------------------------------------------------------------------
# 测 5：白皮书证据包（fail 路径）+ 图接缝其余 Stage0 契约
# ---------------------------------------------------------------------------

def _good_report(api_path="Weird.Liquid", *, ok=True, outcome="sph_ok",
                 passed=True, reflected=None):
    ns, leaf = api_path.split(".", 1)
    if reflected is None:
        reflected = {"top": ["Weird"], "nested": {ns: [leaf, "Base"]}}
    return {
        "schema": seam.STAGE0_REPORT_SCHEMA,
        "ok": ok,
        "outcome": outcome,
        "reflected_materials": reflected,
        "attempts": [{
            "api_used": api_path,
            "n_particles": 1000,
            "steps": seam.STAGE0_STEPS,
            "completed_steps": seam.STAGE0_STEPS if passed else 0,
            "finite_coords": passed,
            "passed": passed,
            "vram_abort": False,
            "position_accessor": "get_particles_pos",
            "vram_peak_gb": 1.2,
            "reductions": 0,
            "failure_verbatim": None if passed else "boom",
        }],
        "gpu": {"name": "Fake A800", "baseline_mib": 300.0,
                "peak_mib": 1228.8},
        "versions": {"python": "3.10.99", "genesis": "1.3.3",
                     "torch": "2.4.1"},
        "ts_utc": "2026-10-24T00:00:00Z",
        "wall_s": 12.3,
    }


def test_whitepaper_artifact(tmp_path):
    report_id = "t8f-stage0-probe-20261024a"
    pack = seam.fluid_evidence_pack(stage0_report_id=report_id)
    names = [item["name"] for item in pack]
    assert seam.WHITEPAPER_NAME == "whitepaper_fluid_rigid_coupling.md"
    assert seam.WHITEPAPER_NAME in names
    wp = next(item for item in pack if item["name"] == seam.WHITEPAPER_NAME)
    assert wp["stage0_fail_only"] is True
    assert wp["source_report_id"] == report_id
    assert wp["content_present"] is False
    probe_item = next(
        item for item in pack if item["name"] == seam.STAGE0_REPORT_NAME)
    assert probe_item["report_id"] == report_id
    assert probe_item["schema"] == "simpraxis.task8.stage0_fluid.v1"
    with pytest.raises(GraphStateError):
        seam.fluid_evidence_pack(stage0_report_id="  ")

    # 引擎仓不得携带白皮书内容（fail 路径只登记清单，无 fake content）
    tracked = subprocess.run(
        ["git", "-C", str(_REPO_ROOT), "ls-files"],
        text=True, capture_output=True, check=True).stdout.splitlines()
    assert not any(p.endswith(seam.WHITEPAPER_NAME) for p in tracked), (
        "白皮书内容不允许预置于引擎仓")

    # ---- Stage0 报告 fail-closed 校验 ----
    assert seam.validate_stage0_report(_good_report())["outcome"] == "sph_ok"
    bad_cases = []
    r = _good_report(); r["schema"] = "wrong"; bad_cases.append(r)
    r = _good_report(); r["outcome"] = "exploded"; bad_cases.append(r)
    r = _good_report(); r["reflected_materials"]["top"] = []; bad_cases.append(r)
    r = _good_report(
        "Weird.Missing",
        reflected={"top": ["Weird"],
                   "nested": {"Weird": ["Liquid", "Base"]}})
    bad_cases.append(r)
    r = _good_report(passed=False); r["ok"] = False
    r["outcome"] = "both_fail"; bad_cases.append(r)  # 一致情形应通过
    assert seam.validate_stage0_report(bad_cases.pop())["ok"] is False
    r = _good_report(); r["ok"] = False
    r["outcome"] = "both_fail"  # attempts 仍 passed=True → 矛盾
    r["attempts"][0]["passed"] = True
    r["attempts"][0]["finite_coords"] = True
    bad_cases.append(r)
    r = _good_report(); r["attempts"][0]["n_particles"] = 2001
    bad_cases.append(r)
    # 冷读 E/A：attempt 行收紧三件 + steps 口径不漂移 + 失败行必须有原文
    r = _good_report(); del r["attempts"][0]["position_accessor"]
    bad_cases.append(r)
    r = _good_report(); r["attempts"][0]["completed_steps"] = (
        seam.STAGE0_STEPS - 1)
    bad_cases.append(r)
    r = _good_report(); r["attempts"][0]["vram_abort"] = True
    bad_cases.append(r)
    r = _good_report(); r["attempts"][0]["steps"] = seam.STAGE0_STEPS - 1
    r["attempts"][0]["completed_steps"] = seam.STAGE0_STEPS - 1
    bad_cases.append(r)
    r = _good_report(passed=False)
    r["ok"] = False
    r["outcome"] = "both_fail"
    r["attempts"][0]["failure_verbatim"] = "   "
    bad_cases.append(r)
    r = _good_report(); del r["ts_utc"]; bad_cases.append(r)
    for bad in bad_cases:
        with pytest.raises(GraphStateError):
            seam.validate_stage0_report(bad)
    load_p = tmp_path / "stage0_fluid_report.json"
    load_p.write_text(json.dumps(_good_report(outcome="pbd_ok")),
                      encoding="utf-8")
    assert seam.load_stage0_report(load_p)["outcome"] == "pbd_ok"
    with pytest.raises(GraphStateError):
        seam.load_stage0_report(tmp_path / "missing.json")

    # ---- 常量单一源：接缝常量/类全部来自按路径加载的探针模块对象 ----
    # （测试自身以另一模块名加载探针，故这里对 seam 内部那一份取 is）
    assert seam.PARTICLE_CAP == probe.PARTICLE_CAP == 20000
    assert seam.VRAM_ABORT_GB == probe.VRAM_ABORT_GB == 35.0
    assert seam.SUBSTEP_MAX == probe.SUBSTEP_MAX == 28
    assert seam.CupGeometry is seam._probe.CupGeometry
    assert seam.vram_watchdog is seam._probe.vram_watchdog
    assert seam.discover_fluid_candidates is seam._probe.discover_fluid_candidates

    # ---- resolver staging：Gate1 批准后从内容层 stage，引擎无题目脚本 ----
    validate_solution_id(seam.FLUID_SOLUTION_ID)  # id 必须 C4 合规

    class _FakeStaged:
        def __init__(self, workdir):
            self.workdir = Path(workdir)

    captured = {}

    def fake_resolver(solution_id):
        captured["id"] = solution_id

        class _Resolver:
            def stage(self, workdir):
                captured["workdir"] = Path(workdir)
                return _FakeStaged(workdir)

        return _Resolver()

    wd = seam.fluid_run_dir(THREAD, 1)
    staged = seam.stage_gate1_approved_solution(wd, resolver=fake_resolver)
    assert captured["id"] == seam.FLUID_SOLUTION_ID
    assert captured["workdir"] == wd
    assert staged.workdir == wd
    # 引擎仓任何位置不得携带解题入口（三层分离）
    assert not (wd / seam.FLUID_ENTRYPOINT).exists()

    # ---- Stage1 契约 executed=False（对齐 frames.py 命令契约纪律）----
    contract = seam.build_stage1_contract(
        stage0_outcome="sph_ok", workdir=wd, thread_id=THREAD)
    assert contract.executed is False
    assert contract.particle_cap == 20000
    assert contract.vram_abort_gb == 35.0
    assert contract.solution_id == seam.FLUID_SOLUTION_ID
    assert contract.cmd[0] == "/mnt/venvs/simpraxis/bin/python"
    assert str(wd / seam.FLUID_ENTRYPOINT) == contract.cmd[1]
    with pytest.raises(GraphStateError):
        seam.build_stage1_contract(
            stage0_outcome="both_fail", workdir=wd, thread_id=THREAD)
    with pytest.raises(RuntimeError):
        seam.execute_stage1(contract)


# ---------------------------------------------------------------------------
# 冷读 A 回归：位置访问器 get_particles_pos 优先（PBD/MPM 不得假 both_fail）
# ---------------------------------------------------------------------------

def test_entity_position_accessor_fallback_order():
    import numpy as np
    arr4 = np.arange(12, dtype=np.float64).reshape(4, 3)
    arr3 = np.arange(9, dtype=np.float64).reshape(3, 3)

    # 1) ParticleEntity 形态（PBD/MPM）：只有 get_particles_pos，直返 numpy
    class ParticlesOnly:
        def get_particles_pos(self):
            return arr4

    out, name = probe._entity_positions(ParticlesOnly())
    assert name == "get_particles_pos"
    assert out.shape == (4, 3)
    assert np.array_equal(out, arr4)
    assert probe._resolve_position_accessor(ParticlesOnly()) == (
        "get_particles_pos")

    # 2) SPH 形态：只有 get_position，返回 torch-like（detach/cpu/numpy 链）
    class _TensorLike:
        def __init__(self, a):
            self._a = a

        def detach(self):
            return self

        def cpu(self):
            return self

        def numpy(self):
            return self._a

    class PositionOnly:
        def get_position(self):
            return _TensorLike(arr3)

    out2, name2 = probe._entity_positions(PositionOnly())
    assert (name2, out2.shape) == ("get_position", (3, 3))
    assert np.array_equal(out2, arr3)

    # 2b) env 维 (1,N,3) 剥 [0]
    class PositionEnv:
        def get_position(self):
            return _TensorLike(arr3[None, ...])

    out3, name3 = probe._entity_positions(PositionEnv())
    assert out3.shape == (3, 3) and name3 == "get_position"

    # 3) 最末防御回退：get_pos（全仓不存在，但保留兜底）
    class PosFallback:
        def get_pos(self):
            return arr3

    out4, name4 = probe._entity_positions(PosFallback())
    assert (name4, out4.shape) == ("get_pos", (3, 3))

    # 4) 三者皆无：必须 RuntimeError（worker 守卫转结构化失败，非假通过）
    class Naked:
        n_particles = 100

    with pytest.raises(RuntimeError):
        probe._entity_positions(Naked())
    with pytest.raises(RuntimeError):
        probe._resolve_position_accessor(Naked())
    # 探测序单点：wheel 1.3.3 实证 get_particles_pos 三族皆有，必须居首
    assert probe.POSITION_ACCESSORS == (
        "get_particles_pos", "get_position", "get_pos")


# ---------------------------------------------------------------------------
# 冷读 B 回归：reductions 按候选（每族）重置，回退族不被首族耗尽
# ---------------------------------------------------------------------------

def _vram_result(ns: str) -> dict:
    return {"passed": False, "vram_abort": True,
            "api_used": f"{ns}.Liquid", "n_particles": 1000, "steps": 10,
            "completed_steps": 0, "finite_coords": True,
            "position_accessor": "get_particles_pos",
            "vram_peak_gb": 36.0, "baseline_mib": 300.0,
            "failure_verbatim": "显存 36.0GB 超过中止线 35GB"}


def _ok_result(ns: str) -> dict:
    return {"passed": True, "vram_abort": False,
            "api_used": f"{ns}.Liquid", "n_particles": 1000, "steps": 10,
            "completed_steps": 10, "finite_coords": True,
            "position_accessor": "get_particles_pos",
            "vram_peak_gb": 1.2, "baseline_mib": 300.0,
            "failure_verbatim": None}


def _crash_result() -> dict:
    return {"_worker_error": True, "error_type": "RuntimeError",
            "traceback": "RuntimeError: synthetic non-vram failure",
            "n_particles": 1000}


def _scripted_main(monkeypatch, tmp_path, script: dict) -> tuple:
    """以合成 _spawn_call 驱动 main：script = {命名空间: [逐次 worker 结果]}。"""
    namespaces = list(script)
    listing = {"top": sorted(namespaces),
               "nested": {ns: ["Base", "Liquid"] for ns in namespaces}}
    reflect_payload = {
        "backend": "cuda", "gpu_name": "Fake A800",
        "genesis_version": "1.3.3", "torch_version": "2.4.1",
        "reflected": listing,
        "candidates": [
            {"path": [ns, "Liquid"], "family": 0 if i == 0 else 1,
             "api_path": f"{ns}.Liquid"}
            for i, ns in enumerate(namespaces)]}
    payloads: list[dict] = []
    counters = {ns: 0 for ns in namespaces}

    def fake_spawn(target, payload, timeout_s=600):
        if target == probe._reflect_worker:
            return reflect_payload
        ns = payload["path"][0]
        idx = counters[ns]
        counters[ns] += 1
        payloads.append(dict(payload))
        return script[ns][idx]

    monkeypatch.setattr(probe, "_spawn_call", fake_spawn)
    out = tmp_path / "stage0_scripted.json"
    rc = probe.main(["--out", str(out)])
    return rc, json.loads(out.read_text(encoding="utf-8")), payloads


def test_reductions_reset_per_candidate(monkeypatch, tmp_path):
    # 场景 1：首族两次 vram_abort 后第三次非显存失败；次族首次即过。
    rc, report, payloads = _scripted_main(monkeypatch, tmp_path, {
        "Weird": [_vram_result("Weird"), _vram_result("Weird"),
                  _crash_result()],
        "Zzz": [_ok_result("Zzz")],
    })
    assert rc == probe.RC_OK and report["ok"] is True
    assert report["outcome"] == "pbd_ok"
    weird = [p for p in payloads if p["path"][0] == "Weird"]
    zzz = [p for p in payloads if p["path"][0] == "Zzz"]
    # 首族吃掉完整两次下调：1000/28 → 500/28 → 250/14
    assert [(p["substeps"], p["box_size"]) for p in weird] == [
        (28, probe.budget_box_size(1000, 0.01)),
        (28, probe.budget_box_size(500, 0.01)),
        (14, probe.budget_box_size(250, 0.01))]
    # 次族预算/子步完全重置，且只跑一次
    assert len(zzz) == 1
    assert (zzz[0]["substeps"], zzz[0]["box_size"]) == (
        28, probe.budget_box_size(1000, 0.01))
    zzz_rows = [a for a in report["attempts"] if a["api_used"] == "Zzz.Liquid"]
    assert len(zzz_rows) == 1
    assert zzz_rows[0]["reductions"] == 0
    assert zzz_rows[0]["position_accessor"] == "get_particles_pos"
    assert [a["reductions"] for a in report["attempts"]
            if a["api_used"] == "Weird.Liquid"] == [0, 1, 2]
    seam.validate_stage0_report(report)  # 落盘形状必须被图接缝接受

    # 场景 2：首族两次 vram_abort（第三次仍 vram，耗尽终态），次族即败，
    # 第三族首次即过——证明第二回退族同样拿到完整预算。
    rc2, report2, payloads2 = _scripted_main(monkeypatch, tmp_path, {
        "Weird": [_vram_result("Weird"), _vram_result("Weird"),
                  _vram_result("Weird")],
        "Zzz": [_crash_result()],
        "Zorp": [_ok_result("Zorp")],
    })
    assert rc2 == probe.RC_OK and report2["outcome"] == "pbd_ok"
    zorp = [p for p in payloads2 if p["path"][0] == "Zorp"]
    assert len(zorp) == 1
    assert (zorp[0]["substeps"], zorp[0]["box_size"]) == (
        28, probe.budget_box_size(1000, 0.01))
    zorp_rows = [a for a in report2["attempts"]
                 if a["api_used"] == "Zorp.Liquid"]
    assert zorp_rows[0]["reductions"] == 0
    assert len(report2["attempts"]) == 5  # 3 + 1 + 1，spawn 上界内
    seam.validate_stage0_report(report2)


# ---------------------------------------------------------------------------
# 冷读 C/D 回归：反射/init 灾难路径空 attempts + 空 top 合法；报告 0600
# ---------------------------------------------------------------------------

def test_stage0_report_early_failure_empty_attempts(tmp_path):
    gpu = {"name": "unknown", "baseline_mib": 0.0, "peak_mib": 0.0}
    versions = {"python": "3.10", "genesis": None, "torch": None}
    disaster = probe.assemble_report(
        ok=False, outcome="both_fail",
        reflected={"top": [], "nested": {}}, attempts=[],
        gpu=gpu, versions=versions, wall_s=0.1,
        note="反射/init 阶段即失败：RuntimeError: synthetic init failure")
    assert seam.validate_stage0_report(disaster)["outcome"] == "both_fail"

    # 缺 note 的手造空 attempts 件：拒
    no_note = {k: v for k, v in disaster.items() if k != "note"}
    with pytest.raises(GraphStateError):
        seam.validate_stage0_report(no_note)
    # 空白 note 同样拒
    blank_note = dict(disaster)
    blank_note["note"] = "   "
    with pytest.raises(GraphStateError):
        seam.validate_stage0_report(blank_note)
    # ok=true（或非 both_fail 结局）+ 空 attempts：拒
    fake_ok = dict(disaster)
    fake_ok["ok"] = True
    fake_ok["outcome"] = "sph_ok"
    with pytest.raises(GraphStateError):
        seam.validate_stage0_report(fake_ok)
    # 空 top 放宽只属于该灾难路径：有 attempts 时 top=[] 仍拒
    with pytest.raises(GraphStateError):
        seam.validate_stage0_report(_good_report(
            reflected={"top": [], "nested": {"Weird": ["Liquid", "Base"]}}))
    # nested 任何路径必须是 dict
    bad_nested = dict(disaster)
    bad_nested["reflected_materials"] = {"top": [], "nested": []}
    with pytest.raises(GraphStateError):
        seam.validate_stage0_report(bad_nested)

    # D 项：探针落盘报告权限 0600
    out = tmp_path / "nested" / "stage0_fluid_report.json"
    probe._write_report(str(out), disaster)
    assert (out.stat().st_mode & 0o777) == 0o600
