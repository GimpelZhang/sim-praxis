"""RSI-R1 T1b 真实校准：固定非正式组合 + 零 POST 控制 permit（Plan §7）。

纪律：

* 校准**先注册后跑**：``contract/calibration_prereg.json`` 记录矩阵、每份 spec 的
  ``spec_hash``、oracle 的 sha256、预算与失败规则，早于任何 raw（AC06）；
* 校准**不绕过**正式 dispatcher：矩阵 → ``build_host_spec`` → ``execute_batch``
  → ``PhysicsBackend``，同一 raw 采集与资源账；生产入口没有 unchecked 参数；
* 校准期间**零模型 POST**：只发 ``CalibrationPermit``（kind=zero_post_control），
  并机械证明 ``build_production_sender`` 因 paid LOCK 不存在而拒绝（LOCK_MISSING）；
* oracle/参考解只在闭源 ``private/calibration_private/``，sha 进预注册，候选不可挂载；
* 任何检查失败 ⇒ ``DESIGN_NOT_READY``（科学面）或 ``INVALID``（完整性），
  不换种子、不调阈值、不发任何项目模型 POST。

本模块不触网；容器执行由调用方注入（``runner``），测试里用假 runner。
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import math
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Mapping

from orchestrator.agentic.image_pin import BASE_IMAGE
from orchestrator.common.clock import utc_now_iso_z
from orchestrator.rsi_r1 import backend as BK
from orchestrator.rsi_r1 import contracts as C
from orchestrator.rsi_r1 import experiments as E
from orchestrator.rsi_r1 import loop as L
from orchestrator.rsi_r1 import metrics as M
from orchestrator.rsi_r1 import physics as P
from orchestrator.rsi_r1 import prereg as PR
from orchestrator.rsi_r1 import rubric as R
from orchestrator.rsi_r1 import scenes as S

PREREG_SCHEMA = f"{C.SCHEMA_PREFIX}.calibration_prereg.v1"
REPORT_SCHEMA = f"{C.SCHEMA_PREFIX}.calibration_report.v1"
PERMIT_SCHEMA = f"{C.SCHEMA_PREFIX}.zero_post_permit.v1"
ORACLE_SCHEMA = f"{C.SCHEMA_PREFIX}.calibration_oracle.v1"

PREREG_RELPATH = "contract/calibration_prereg.json"
PERMIT_RELPATH = "contract/calibration_permit.json"
REPORT_RELPATH = "contract/calibration_report.json"
REPORT_MD_RELPATH = "contract/calibration_report.md"
ORACLE_RELPATH = "private/calibration_private/oracle.py"

#: paid sender 的 LOCK 注册位（T5 冻结后才允许存在；本阶段必须不存在）。
PAID_LOCK_RELPATH = "contract/paid_lock.json"
#: 任何 ``*lock*.json`` 出现在 contract/ 都视为 paid LOCK 存在（宽口径拒绝）。
_LOCK_GLOB = "*lock*.json"

#: 校准伪 plan id：不是候选研究计划，恒等常量，只用于让 run_id/spec_hash 可复算。
CAL_PLAN_SHA256 = hashlib.sha256(
    b"simpraxis.rsi_r1.calibration_plan.v1").hexdigest()

#: Plan §7：总校准/探针/练习/测试物理执行 ≤2,000,000 step、≤250 runs。
CAL_MAX_RUNS = 250
CAL_MAX_STEPS = 2_000_000
#: 本会话 T1b 前的临时仪器探测（未持久化 raw）保守记账：诚实计入总帽。
PRE_PROBE_ESTIMATE_RUNS = 14
PRE_PROBE_ESTIMATE_STEPS = 112_000

#: 容器镜像（含 MuJoCo 3.15.0 的基础沙盒，digest 固定）。
CAL_IMAGE = BASE_IMAGE

#: Study A 校准组合（固定非正式；8 blocks 覆盖质量域）。
A_MU = "0.4"
A_V0 = "0.4"
A_BASE_TORSIONAL = "0.001"
A_SHAM_TORSIONAL = "0.05"
A_BLOCK_MASSES = ("0.2", "0.3", "0.4", "0.5", "0.6", "0.7", "0.8", "0.9")
A_REPLAY_BLOCK = "b01"

#: Study B 校准组合（Tc/dt 整数整除；8 blocks，其中 b08 必饱和）。
B_BLOCKS: "tuple[tuple[str, dict[str, str]], ...]" = (
    ("b01", {"inertia_ratio": "1.0", "target_amplitude_rad": "0.2",
             "target_freq_hz": "1.0", "tc_s": "0.004", "dt_s": "0.0005",
             "controller": "PD", "kp": "5", "kd": "0.1"}),
    ("b02", {"inertia_ratio": "1.0", "target_amplitude_rad": "0.2",
             "target_freq_hz": "1.0", "tc_s": "0.008", "dt_s": "0.001",
             "controller": "PD", "kp": "5", "kd": "0.1"}),
    ("b03", {"inertia_ratio": "1.0", "target_amplitude_rad": "0.3",
             "target_freq_hz": "0.5", "tc_s": "0.016", "dt_s": "0.002",
             "controller": "PD", "kp": "5", "kd": "0.1"}),
    ("b04", {"inertia_ratio": "1.0", "target_amplitude_rad": "0.3",
             "target_freq_hz": "0.5", "tc_s": "0.032", "dt_s": "0.002",
             "controller": "PD", "kp": "5", "kd": "0.1"}),
    ("b05", {"inertia_ratio": "1.0", "target_amplitude_rad": "0.4",
             "target_freq_hz": "2.0", "tc_s": "0.004", "dt_s": "0.001",
             "controller": "PD", "kp": "10", "kd": "0.5"}),
    ("b06", {"inertia_ratio": "1.0", "target_amplitude_rad": "0.2",
             "target_freq_hz": "0.8", "tc_s": "0.016", "dt_s": "0.001",
             "controller": "P", "kp": "2", "kd": "0"}),
    ("b07", {"inertia_ratio": "1.0", "target_amplitude_rad": "0.3",
             "target_freq_hz": "0.3", "tc_s": "0.032", "dt_s": "0.001",
             "controller": "P", "kp": "2", "kd": "0"}),
    ("b08", {"inertia_ratio": "1.0", "target_amplitude_rad": "0.4",
             "target_freq_hz": "2.0", "tc_s": "0.008", "dt_s": "0.001",
             "controller": "P", "kp": "20", "kd": "0"}),
)
B_REPLAY_BLOCK = "b02"
B_REPLAY_SOURCE = "b02"
B_SATURATION_BLOCK = "b08"

#: 暴露门（仪器健全性，不是科学 δ；只进本校准预注册，**不进 rubric**）。
CONTACT_MIN_FRACTION = Decimal("0.98")
CONTACT_MAX_GAP_STEPS = 25
FORCE_BALANCE_REL = Decimal("0.02")
Z_EXCURSION_MAX_M = Decimal("5e-4")
V0_TOL_MPS = Decimal("0.01")
V0_CHECK_SAMPLES = 5
MIN_MOVED_M = Decimal("0.005")

#: oracle 对照容差（事前注册；A 闭式解 vs 软接触引擎，B 细网格参考 vs 真实 dt）。
A_ORACLE_ABS_TOL_M = Decimal("1e-3")
A_ORACLE_REL_TOL = Decimal("0.03")
A_ORACLE_STOP_TOL_S = Decimal("0.02")
B_ORACLE_ABS_TOL = Decimal("0.01")
B_TARGET_TOL_RAD = Decimal("1e-5")
B_MIN_DISTINCT_METRICS = 4

#: 检查家族（预注册即固定；报告不得出现未注册的检查 id）。
CHECK_FAMILIES: "tuple[tuple[str, str, int], ...]" = (
    ("A-NEG", "每 block 基准 vs sham 位移差 ≤ 0.001 m", 8),
    ("A-PAIR", "每 block 两条件仅 torsional_friction_m 不同", 8),
    ("A-EXPO-CONTACT", "每 A run 有效接触占比 ≥0.98 且最大间隙 ≤25 步", 19),
    ("A-EXPO-FORCE", "每 A run 法向力均值/(m·g) 在 ±2% 内", 19),
    ("A-EXPO-Z", "每 A run |z−z0| ≤ 5e-4 m", 19),
    ("A-EXPO-V0", "每 A run 前 5 步 |vx−v0| ≤ 0.01 m/s", 19),
    ("A-MOVED", "每 A run 净位移 ≥0.005 m（仪器能测到运动）", 19),
    ("A-ORACLE-DISP", "每 A run 位移与闭式解差 ≤ max(1e-3 m, 3%)", 19),
    ("A-ORACLE-STOP", "每 A run 停止时间与闭式解差 ≤0.02 s 且删失一致", 19),
    ("A-SENS", "μ=0.15 vs 0.6 位移差 >0.01 m", 1),
    ("A-REPLAY", "b01 复跑指标在 REPLAY 容差内", 1),
    ("B-EXEC-STRIDE", "每 B run control_update_index 恰为 i//(Tc/dt)", 9),
    ("B-EXEC-TARGET", "每 B run target_q 与 A·sin(2πft) 逐点差 ≤1e-5", 9),
    ("B-SAT", "b08 记录到扭矩饱和（限幅接线真实生效）", 1),
    ("B-ORACLE", "每 B run 归一化 RMSE 与细网格参考差 ≤0.01", 9),
    ("B-IDENT", "8 个 block 的 RMSE 至少 4 个互异值", 1),
    ("B-NO-INSTABILITY", "每 B run 无失稳事件（域内校准不得触损帽）", 9),
    ("B-REPLAY", "b02 复跑 RMSE 在 REPLAY 容差内", 1),
    ("GLOBAL-PREREG-FIRST", "预注册时刻严格早于全部 raw mtime", 1),
    ("GLOBAL-PREREG-SOURCES", "矩阵/spec_hash/量表 sha 与当前代码一致", 1),
    ("GLOBAL-PERMIT", "零 POST permit 有效且 paid LOCK 不存在", 1),
    ("GLOBAL-LEDGER-ZERO", "校准后账本 2903 无任何 call 行", 1),
    ("GLOBAL-ORACLE-PRIVATE", "oracle 仅在 private 下且 sha 与预注册一致", 1),
    ("GLOBAL-BUDGET", "runs/steps 在 250/2,000,000 帽内（含预探测估计）", 1),
)

_INTEGRITY_PREFIXES = ("GLOBAL-",)

# ---------------------------------------------------------------------------
# 设计版本（v2 = 2026-11-11 用户批准的两条仪器定义修正；缺口见
# contract/calibration_gap_analysis.md）
#
# 修正原则：只改「证明 v0 在首次 mj_step 前写入」的暴露门与 B oracle 容差，
# 矩阵/场景/seed/其余门一律不变。v2 的等价约束都不弱于 v1：
#   * A：首步位移门直接证明初速通道；位移 ∝ v0² 的 oracle 门等价 v0 误差 ≤1.5%；
#   * B：abs 0.01 + 1% rel 在 b06 上等价参数级一致 ≤0.35%，并新增周期/包络
#     第二证据（相位不敏感），防相对容差掩盖真实相位/增益错误。
# ---------------------------------------------------------------------------

PREREG_SCHEMA_V2 = f"{C.SCHEMA_PREFIX}.calibration_prereg.v2"
REPORT_SCHEMA_V2 = f"{C.SCHEMA_PREFIX}.calibration_report.v2"
ORACLE_SCHEMA_V2 = f"{C.SCHEMA_PREFIX}.calibration_oracle.v2"
PREREG_RELPATH_V2 = "contract/calibration_prereg_v2.json"
REPORT_RELPATH_V2 = "contract/calibration_report_v2.json"
REPORT_MD_RELPATH_V2 = "contract/calibration_report_v2.md"
ORACLE_RELPATH_V2 = "private/calibration_private/oracle_v2.py"
GAP_RELPATH = "contract/calibration_gap_analysis.md"
BATCH_A_V2 = "calibration_a_v2"
BATCH_B_V2 = "calibration_b_v2"

#: v2 暴露门：首步位移必须等于 v0·dt（qvel 在首次 mj_step 前写入）。
V0_FIRST_STEP_DISP_TOL_M = Decimal("1e-5")
#: v2 B oracle：尺度感知容差与相位不敏感第二证据。
B_ORACLE_REL_TOL = Decimal("0.01")
B_ORACLE_PHASE_BLOCKS = ("b01", "b02", "b06")
B_PERIOD_REL_TOL = Decimal("0.01")
B_ENVELOPE_REL_TOL = Decimal("0.02")

_V2_FAMILIES = []
for _fam, _desc, _count in CHECK_FAMILIES:
    if _fam == "A-EXPO-V0":
        _desc = "每 A run 首步位移 |x[0]−x0−v0·dt| ≤ 1e-5 m（qvel 先于首次 mj_step）"
    _V2_FAMILIES.append((_fam, _desc, _count))
    if _fam == "B-ORACLE":
        _V2_FAMILIES.append((
            "B-ORACLE-PHASE",
            "欠阻尼 block 周期误差 ≤1% 且包络幅值差 ≤2%（相位不敏感第二证据）", 3))
    if _fam == "GLOBAL-BUDGET":
        _V2_FAMILIES.append((
            "GLOBAL-PRIOR-ATTEMPT",
            "v1 预注册/报告/raw 与缺口件逐字节未变（历史不可改写）", 1))
V2_CHECK_FAMILIES: "tuple[tuple[str, str, int], ...]" = tuple(_V2_FAMILIES)
del _V2_FAMILIES

DESIGN_V1: Mapping[str, Any] = {
    "key": "v1",
    "version": 1,
    "prereg_schema": PREREG_SCHEMA,
    "report_schema": REPORT_SCHEMA,
    "prereg_relpath": PREREG_RELPATH,
    "report_relpath": REPORT_RELPATH,
    "report_md_relpath": REPORT_MD_RELPATH,
    "oracle_relpath": ORACLE_RELPATH,
    "oracle_schema": ORACLE_SCHEMA,
    "batch_a": "calibration_a",
    "batch_b": "calibration_b",
    "families": CHECK_FAMILIES,
    "v0_gate": "first5_velocity",
    "underdamped_blocks": (),
    "b_oracle_rel": Decimal("0"),
    "prior_attempt_required": False,
}

DESIGN_V2: Mapping[str, Any] = {
    "key": "v2",
    "version": 2,
    "prereg_schema": PREREG_SCHEMA_V2,
    "report_schema": REPORT_SCHEMA_V2,
    "prereg_relpath": PREREG_RELPATH_V2,
    "report_relpath": REPORT_RELPATH_V2,
    "report_md_relpath": REPORT_MD_RELPATH_V2,
    "oracle_relpath": ORACLE_RELPATH_V2,
    "oracle_schema": ORACLE_SCHEMA_V2,
    "batch_a": BATCH_A_V2,
    "batch_b": BATCH_B_V2,
    "families": V2_CHECK_FAMILIES,
    "v0_gate": "first_step_displacement",
    "underdamped_blocks": B_ORACLE_PHASE_BLOCKS,
    "b_oracle_rel": B_ORACLE_REL_TOL,
    "prior_attempt_required": True,
}

DESIGNS: Mapping[str, Mapping[str, Any]] = {"v1": DESIGN_V1, "v2": DESIGN_V2}


def design_for(version: "int | str") -> Mapping[str, Any]:
    key = f"v{version}" if isinstance(version, int) else str(version)
    try:
        return DESIGNS[key]
    except KeyError:
        raise CalibrationError("DESIGN_UNKNOWN", repr(version)) from None


class CalibrationError(RuntimeError):
    """校准前置件/完整性失败（fail-closed；不降级为「报告里记一笔」）。"""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


# ---------------------------------------------------------------------------
# 矩阵
# ---------------------------------------------------------------------------

def _entry(study: str, block: str, cond: str, role: str,
           params: Mapping[str, str]) -> "dict[str, Any]":
    return {"study": study, "block_id": block, "condition_id": cond,
            "role": role, "scene_id": S.SCENE_A if study == "A" else S.SCENE_B,
            "params": {k: str(v) for k, v in params.items()}}


def calibration_matrix() -> "tuple[dict[str, Any], ...]":
    """固定非正式组合：A 8 blocks×（base, sham）+ 灵敏度 2 + 复跑 1；B 8 + 复跑 1。"""
    base_by_block: "dict[str, dict[str, str]]" = {}
    entries: "list[dict[str, Any]]" = []
    for i, mass in enumerate(A_BLOCK_MASSES, start=1):
        block = f"b{i:02d}"
        base = {"mass_kg": mass, "v0_mps": A_V0, "sliding_friction": A_MU,
                "torsional_friction_m": A_BASE_TORSIONAL}
        sham = dict(base, torsional_friction_m=A_SHAM_TORSIONAL)
        base_by_block[block] = base
        entries.append(_entry("A", block, "base", "negative_base", base))
        entries.append(_entry("A", block, "sham", "negative_sham", sham))
    sens = {"mass_kg": str(R.SENSITIVITY_MASS_KG),
            "v0_mps": str(R.SENSITIVITY_V0_MPS),
            "sliding_friction": str(R.SENSITIVITY_MU_LOW),
            "torsional_friction_m": A_BASE_TORSIONAL}
    entries.append(_entry("A", "s01", "mu_low", "sensitivity", sens))
    entries.append(_entry("A", "s02", "mu_high", "sensitivity",
                          dict(sens, sliding_friction=str(R.SENSITIVITY_MU_HIGH))))
    entries.append(_entry("A", "r01", "base", "replay",
                          base_by_block[A_REPLAY_BLOCK]))
    b_params = dict(B_BLOCKS)
    for block, params in B_BLOCKS:
        entries.append(_entry("B", block, f"cal_{block}", "b_cal", params))
    entries.append(_entry("B", f"{B_REPLAY_BLOCK}r", f"cal_{B_REPLAY_BLOCK}r",
                          "replay", b_params[B_REPLAY_SOURCE]))
    return tuple(entries)


def specs_for_matrix(entries: Iterable[Mapping[str, Any]]
                     ) -> "tuple[E.ExperimentSpec, ...]":
    out = []
    for e in entries:
        out.append(E.build_host_spec(
            plan_sha256=CAL_PLAN_SHA256, study_id=e["study"],
            condition_id=e["condition_id"], block_id=e["block_id"],
            scene_id=e["scene_id"], params=e["params"]))
    return tuple(out)


# ---------------------------------------------------------------------------
# oracle
# ---------------------------------------------------------------------------

def oracle_sha256(path: "Path | str") -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_oracle(path: "Path | str",
                schema: str = ORACLE_SCHEMA) -> Any:
    p = Path(path)
    if not p.is_file():
        raise CalibrationError("ORACLE_MISSING", f"{p} 不存在（闭源参考缺失）")
    spec = importlib.util.spec_from_file_location("rsi_r1_calibration_oracle", p)
    if spec is None or spec.loader is None:
        raise CalibrationError("ORACLE_LOAD", f"{p} 不是可加载的 Python 件")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for attr in ("ORACLE_SCHEMA", "study_a_oracle", "study_b_rmse"):
        if not hasattr(mod, attr):
            raise CalibrationError("ORACLE_INTERFACE", f"oracle 缺 {attr}")
    if mod.ORACLE_SCHEMA != schema:
        raise CalibrationError("ORACLE_INTERFACE",
                               f"oracle schema {mod.ORACLE_SCHEMA!r}（期望 {schema}）")
    return mod


def _oracle_path(ctx: Any, design: Mapping[str, Any] = DESIGN_V1) -> Path:
    return ctx.relative(design["oracle_relpath"])


def _assert_oracle_private(ctx: Any, path: Path, sha: str) -> None:
    root = ctx.root.resolve()
    p = path.resolve()
    if root not in p.parents or "private" not in p.relative_to(root).parts:
        raise CalibrationError("ORACLE_NOT_PRIVATE", f"{p} 不在 campaign private/ 下")
    if oracle_sha256(path) != sha:
        raise CalibrationError("ORACLE_DRIFT", "oracle sha256 与预注册不符")


# ---------------------------------------------------------------------------
# v2 前次尝试取证 + oracle 等价性
# ---------------------------------------------------------------------------

#: v1 尝试必须逐字节留存的件（缺口分析 + 预注册 + 报告 + v1 oracle）。
PRIOR_ATTEMPT_FILES = (
    "contract/calibration_prereg.json",
    "contract/calibration_report.json",
    "contract/calibration_report.md",
    "contract/calibration_gap_analysis.md",
    "private/calibration_private/oracle.py",
)
PRIOR_ATTEMPT_RAW_BATCHES = ("calibration_a", "calibration_b")


def prior_attempt_manifest(ctx: Any) -> "dict[str, Any]":
    """v1 尝试的逐件 sha256 清单（文件 + 全部 raw）；缺件即拒绝签发 v2。"""
    files: "dict[str, str]" = {}
    for rel in PRIOR_ATTEMPT_FILES:
        if not ctx.exists(rel):
            raise CalibrationError("PRIOR_ATTEMPT_MISSING", f"{rel} 不存在")
        files[rel] = hashlib.sha256(ctx.relative(rel).read_bytes()).hexdigest()
    obs = ctx.subdir("observations")
    raws: "dict[str, str]" = {}
    for batch in PRIOR_ATTEMPT_RAW_BATCHES:
        base = obs / batch
        if not base.is_dir():
            raise CalibrationError("PRIOR_ATTEMPT_MISSING", f"observations/{batch}/ 缺失")
        for p in sorted(base.iterdir()):
            if p.is_file():
                raws[f"observations/{batch}/{p.name}"] = hashlib.sha256(
                    p.read_bytes()).hexdigest()
    if not raws:
        raise CalibrationError("PRIOR_ATTEMPT_MISSING", "v1 raw 为空")
    return {
        "files": dict(sorted(files.items())),
        "raws": dict(sorted(raws.items())),
        "files_sha256": C.canonical_sha256(dict(sorted(files.items()))),
        "raws_sha256": C.canonical_sha256(dict(sorted(raws.items()))),
        "n_files": len(files),
        "n_raws": len(raws),
    }


def verify_prior_attempt(ctx: Any,
                         recorded: Mapping[str, Any]) -> "dict[str, Any]":
    """重算 v1 清单并与预注册登记件逐项比对；任何漂移即 PRIOR_ATTEMPT_DRIFT。"""
    now = prior_attempt_manifest(ctx)
    for section in ("files", "raws"):
        old = dict(recorded.get(section) or {})
        new = dict(now[section])
        if old != new:
            missing = sorted(set(old) - set(new))
            added = sorted(set(new) - set(old))
            changed = sorted(k for k in set(old) & set(new) if old[k] != new[k])
            raise CalibrationError(
                "PRIOR_ATTEMPT_DRIFT",
                f"{section}: missing={missing[:3]} added={added[:3]} changed={changed[:3]}")
    for key in ("files_sha256", "raws_sha256"):
        if recorded.get(key) != now[key]:
            raise CalibrationError("PRIOR_ATTEMPT_DRIFT", key)
    return now


def assert_oracle_v2_equivalence(ctx: Any, *,
                                 design_v1: Mapping[str, Any] = DESIGN_V1,
                                 design_v2: Mapping[str, Any] = DESIGN_V2) -> "dict[str, Any]":
    """v2 oracle 的 B 参考必须与 v1 逐位相等（≤1e-12）；不等即拒绝签发 v2。"""
    p1 = _oracle_path(ctx, design_v1)
    p2 = _oracle_path(ctx, design_v2)
    o1 = load_oracle(p1, schema=design_v1["oracle_schema"])
    o2 = load_oracle(p2, schema=design_v2["oracle_schema"])
    out: "dict[str, float]" = {}
    for e in calibration_matrix():
        if e["study"] != "B":
            continue
        params = e["params"]
        kw = dict(inertia_ratio=float(params["inertia_ratio"]),
                  amplitude_rad=float(params["target_amplitude_rad"]),
                  freq_hz=float(params["target_freq_hz"]),
                  tc_s=float(params["tc_s"]), dt_s=float(params["dt_s"]),
                  controller=params["controller"], kp=float(params["kp"]),
                  kd=float(params["kd"]), duration_s=float(S.DURATION_S),
                  window_start_s=float(R.TRACKING_WINDOW_START_S),
                  window_end_s=float(R.TRACKING_WINDOW_END_S))
        a = float(o1.study_b_rmse(**kw)["normalized_rmse"])
        b = float(o2.study_b_rmse(**kw)["normalized_rmse"])
        out[e["block_id"]] = abs(a - b)
    worst = max(out.values()) if out else float("inf")
    if not (worst <= 1e-12):
        raise CalibrationError("ORACLE_EQUIVALENCE", f"max|ΔRMSE|={worst!r} > 1e-12")
    return {"per_block_abs_diff": dict(sorted(out.items())),
            "max_abs_diff": worst,
            "v1_sha256": oracle_sha256(p1), "v2_sha256": oracle_sha256(p2)}


# ---------------------------------------------------------------------------
# 预注册
# ---------------------------------------------------------------------------

def _instrument_prereg_sha(ctx: Any) -> "str | None":
    rel = "contract/instrument_prereg.json"
    if not ctx.exists(rel):
        return None
    return ctx.read_json(rel).get("document_sha256")


def _document_sha(doc: Mapping[str, Any]) -> str:
    return C.canonical_sha256({k: v for k, v in doc.items()
                               if k != "document_sha256"})


def _gravity() -> Decimal:
    doc = S.scene_doc(S.SCENE_A)
    for fixed in doc.fixed:
        if fixed.id == "gravity_mps2":
            return Decimal(str(fixed.value))
    raise CalibrationError("SCENE_GRAVITY", "场景 A 未注册 gravity_mps2")


def _matrix_doc(entries, specs) -> "list[dict[str, Any]]":
    rows = []
    for e, spec in zip(entries, specs):
        settle = P.A_SETTLE_STEPS if spec.study_id == "A" else 0
        rows.append({
            "study": e["study"], "block_id": e["block_id"],
            "condition_id": e["condition_id"], "role": e["role"],
            "scene_id": e["scene_id"], "params": dict(e["params"]),
            "run_id": spec.run_id, "spec_hash": spec.spec_hash,
            "nsteps": spec.nsteps, "settle_steps": settle,
        })
    return rows


def build_calibration_prereg(*, ctx: Any, oracle_path: "Path | str",
                             registered_at_utc: "str | None" = None,
                             reviewer: str = "maintainer-side (non-blind)",
                             design: Mapping[str, Any] = DESIGN_V1,
                             supersedes: "Mapping[str, Any] | None" = None,
                             prior_attempt: "Mapping[str, Any] | None" = None
                             ) -> "dict[str, Any]":
    """构造校准预注册（纯函数；不写盘）。"""
    entries = calibration_matrix()
    specs = specs_for_matrix(entries)
    matrix = _matrix_doc(entries, specs)
    oracle = load_oracle(oracle_path, schema=design["oracle_schema"])
    now = registered_at_utc or utc_now_iso_z()
    a_runs = sum(1 for r in matrix if r["study"] == "A")
    b_runs = sum(1 for r in matrix if r["study"] == "B")
    doc = {
        "schema": design["prereg_schema"],
        "schema_version": design["version"],
        "envelope_id": ctx.envelope_id,
        "registered_at_utc": now,
        "purpose": ("T1b 校准：只验证参数接线/观测函数/暴露/负控/灵敏度/复跑；"
                    "不跑正式研究、不选赢家、不发项目模型 POST"),
        "instrument_prereg_sha256": _instrument_prereg_sha(ctx),
        "rubric_sha256": R.rubric_sha256(),
        "calibration_plan_sha256": CAL_PLAN_SHA256,
        "container_image": CAL_IMAGE,
        "maintainer_review": {
            "reviewer": reviewer,
            "reviewed_at_utc": now,
            "scope": ["物理意义", "公开信息完整性", "oracle 独立性与容差事前性",
                      "无候选泄漏面"],
            "physical_soundness": [
                "A：匀减速 a=μ·g 与质量无关；固定 μ=0.4、v0=0.4 ⇒ 位移约 0.020 m、"
                "停止约 0.102 s，4 s 窗内必停",
                "A 阴性控制：condim=3 时 torsional friction 不参与约束（需 condim≥4），"
                "故只改 torsional_friction_m 的 sham 必须在 ±0.001 m 内等效",
                "A 灵敏度：μ=0.15 vs 0.6 的闭式位移差约 0.041 m，远大于 0.01 m 门",
                "B：Tc/dt 全为整数整除；Kp=20、A=0.4、f=2 的 b08 必然触 ±2 Nm 限幅",
                "B oracle 为同积分族 16× 细网格参考，只检查仪器一致性，不给最优参数",
            ] + ([
                "v2 依据（缺口见 contract/calibration_gap_analysis.md）：v1 的 A 前 5 步"
                "速度门实测到的是 MuJoCo 每步摩擦约束冲量量化（首步≈2.2×μg·dt、随后"
                "数步为 0 的平台、与质量无关），不是初速通道；v1 的 B 0.01 绝对容差在"
                "欠阻尼 b06 上等价于要求 Δkp≈0.14% 的参数级一致，低于两种积分器结构差",
                "v2 A 门：首步位移 |x[0]−x0−v0·dt| ≤1e-5 m，直接证明 qvel 在首次 mj_step 前"
                "写入（v1 全部 19 个 raw 离线复核最大 3.72e-6 m）；A-ORACLE-DISP 保留，"
                "位移 ∝ v0² ⇒ 3% 位移容差等价 v0 误差 ≤1.5%，不弱于 v1 的 0.01 m/s 门",
                "v2 B 门：abs 0.01 + 1%·|ref|（b06 门 0.02596 vs 实测 0.010557），并对"
                "b01/b02/b06 增报周期 ≤1% 与包络 ≤2%（相位不敏感；实测 b06 0.096%/0.62%），"
                "饱和 block 保留饱和计数一致性；矩阵/场景/seed 不变",
            ] if design["version"] >= 2 else []),
            "no_leakage": ("矩阵为非正式固定组合，不复用为未来确认 seed；oracle 只在 "
                           "private/calibration_private/，候选不可挂载"),
            "verdict": "APPROVE_FOR_CALIBRATION",
        },
        "matrix": matrix,
        "checks": [{"family": fam, "description": desc, "count": count}
                   for fam, desc, count in design["families"]],
        "exposure_gates": {
            "contact_min_fraction": str(CONTACT_MIN_FRACTION),
            "contact_max_gap_steps": CONTACT_MAX_GAP_STEPS,
            "force_balance_rel": str(FORCE_BALANCE_REL),
            "z_excursion_max_m": str(Z_EXCURSION_MAX_M),
            **({"v0_tol_mps": str(V0_TOL_MPS),
                "v0_check_samples": V0_CHECK_SAMPLES}
               if design["v0_gate"] == "first5_velocity" else
               {"v0_first_step_disp_tol_m": str(V0_FIRST_STEP_DISP_TOL_M)}),
            "min_moved_m": str(MIN_MOVED_M),
            "note": ("仪器健全性边距，不扩 rubric δ；默认值由 T1b 前的仪器探测实测"
                     "（接触占比 ≥0.9957、最大间隙 ≤8 步、力平衡 ≤0.43%、z ≤64 µm）"
                     "留 2–20× 余量后事前登记"),
        },
        "oracle": {
            "relpath": design["oracle_relpath"],
            "sha256": oracle_sha256(oracle_path),
            "schema": oracle.ORACLE_SCHEMA,
            "reference_a": "闭式匀减速解（解析）",
            "reference_b": "同族 implicitfast 细网格参考（REFINE 倍细化）",
            "tolerances": {
                "a_displacement_abs_m": str(A_ORACLE_ABS_TOL_M),
                "a_displacement_rel": str(A_ORACLE_REL_TOL),
                "a_stop_time_abs_s": str(A_ORACLE_STOP_TOL_S),
                "b_normalized_rmse_abs": str(B_ORACLE_ABS_TOL),
                **({"b_normalized_rmse_rel": str(design["b_oracle_rel"]),
                    "b_period_rel": str(B_PERIOD_REL_TOL),
                    "b_envelope_rel": str(B_ENVELOPE_REL_TOL),
                    "b_phase_blocks": list(design["underdamped_blocks"])}
                   if design["version"] >= 2 else {}),
                "b_target_abs_rad": str(B_TARGET_TOL_RAD),
            },
            "replay_tolerances": {k: {"abs": str(a), "rel": str(r)}
                                  for k, (a, r) in sorted(R.REPLAY_TOLERANCES.items())},
        },
        "budget": {
            "max_runs": CAL_MAX_RUNS,
            "max_steps": CAL_MAX_STEPS,
            "planned_runs": len(matrix),
            "planned_a_runs": a_runs,
            "planned_b_runs": b_runs,
            "planned_spec_steps": sum(r["nsteps"] for r in matrix),
            "planned_settle_steps": sum(r["settle_steps"] for r in matrix),
            "pre_probe_estimate_runs": PRE_PROBE_ESTIMATE_RUNS,
            "pre_probe_estimate_steps": PRE_PROBE_ESTIMATE_STEPS,
            "pre_probe_note": ("T1b 前临时仪器探测（未持久化 raw）保守估计，"
                               "诚实计入总帽"),
        },
        "failure_rules": {
            "scientific_failure": "DESIGN_NOT_READY",
            "integrity_failure": "INVALID",
            "no_rescue": "不换 seed、不调阈值、不加跑量；提交缺口与新设计提案",
            "no_model_post": "校准期零项目模型 POST；paid permit 只在 T5 冻结后成立",
            "instability_in_domain": ("域内 B run 若触 |q|>4 或非有限，仪器设计判"
                                      "DESIGN_NOT_READY（不得把损帽当结论）"),
        },
    }
    if design["version"] >= 2:
        if design["prior_attempt_required"] and (not supersedes or not prior_attempt):
            raise CalibrationError(
                "PRIOR_ATTEMPT_MISSING",
                "v2 预注册必须携带被取代件的 sha 与历史件清单")
        doc["design_version"] = design["version"]
        doc["supersedes"] = dict(supersedes or {})
        doc["prior_attempt"] = dict(prior_attempt or {})
    doc["document_sha256"] = _document_sha(doc)
    return doc


def _raw_paths(ctx: Any, design: Mapping[str, Any] = DESIGN_V1) -> "list[Path]":
    root = ctx.subdir("observations")
    if not root.exists():
        return []
    if design["version"] == 1:
        return sorted(root.glob("**/*.jsonl"))
    out: "list[Path]" = []
    for batch in (design["batch_a"], design["batch_b"]):
        out.extend(sorted((root / batch).glob("*.jsonl")))
    return sorted(out)


def write_calibration_prereg(ctx: Any, doc: Mapping[str, Any],
                             design: Mapping[str, Any] = DESIGN_V1) -> Path:
    """写预注册；已有同内容件则幂等复用；漂移/晚于 raw 一律拒绝。"""
    rel = design["prereg_relpath"]
    if ctx.exists(rel):
        existing = ctx.read_json(rel)
        if existing.get("schema") != design["prereg_schema"]:
            raise CalibrationError("PREREG_SCHEMA", "已有件 schema 不符")
        if existing.get("document_sha256") != doc.get("document_sha256"):
            raise CalibrationError(
                "PREREG_DRIFT", "已有预注册与待写件不一致；不得静默覆盖（重开 campaign）")
        return ctx.relative(rel)
    if _raw_paths(ctx, design):
        raise CalibrationError("RAW_BEFORE_PREREG",
                               f"{design['batch_a']}/{design['batch_b']} 已有 raw，拒绝补注册")
    ctx.write_json(rel, dict(doc))
    return ctx.relative(rel)


def load_calibration_prereg(ctx: Any,
                            design: Mapping[str, Any] = DESIGN_V1) -> "dict[str, Any]":
    rel = design["prereg_relpath"]
    if not ctx.exists(rel):
        raise CalibrationError("PREREG_MISSING", f"先写 {rel}")
    doc = ctx.read_json(rel)
    if doc.get("schema") != design["prereg_schema"]:
        raise CalibrationError("PREREG_SCHEMA", f"{doc.get('schema')!r}")
    if doc.get("document_sha256") != _document_sha(doc):
        raise CalibrationError("PREREG_SELFTEST", "document_sha256 不自洽")
    return doc


def assert_prereg_matches_sources(doc: Mapping[str, Any], *,
                                  oracle_path: "Path | str") -> None:
    """矩阵/spec_hash/量表/oracle 必须与当前代码逐字节一致（AC09 校准面）。"""
    if doc.get("rubric_sha256") != R.rubric_sha256():
        raise CalibrationError("RUBRIC_DRIFT", "量表在校准预注册后被改动")
    entries = calibration_matrix()
    specs = specs_for_matrix(entries)
    if doc.get("matrix") != _matrix_doc(entries, specs):
        raise CalibrationError("MATRIX_DRIFT", "校准矩阵或 spec_hash 与当前代码不符")
    if doc.get("calibration_plan_sha256") != CAL_PLAN_SHA256:
        raise CalibrationError("PLAN_DRIFT", "校准伪 plan id 不符")
    want = (doc.get("oracle") or {}).get("sha256")
    if not want or oracle_sha256(oracle_path) != want:
        raise CalibrationError("ORACLE_DRIFT", "oracle sha256 与预注册不符")


# ---------------------------------------------------------------------------
# 零 POST permit
# ---------------------------------------------------------------------------

def _lock_present(ctx: Any) -> "list[str]":
    contract = ctx.subdir("contract")
    return sorted(p.name for p in contract.glob(_LOCK_GLOB)
                  if p.name != "calibration_prereg.json"
                  and "calibration" not in p.name)


def ledger_call_rows(ledger_path: "Path | str") -> int:
    p = Path(ledger_path)
    if not p.exists():
        return 0
    rows = 0
    with open(p, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if isinstance(obj, Mapping) and obj.get("event") == "call":
                rows += 1
    return rows


def _prove_sender_refusal(lock_path: Path) -> "dict[str, Any]":
    """用生产装配入口证明：paid LOCK 不存在时 transport 根本不会被构造。"""
    called = {"transport": False}

    def factory():
        called["transport"] = True
        raise AssertionError("模型 transport 在校准期不得被构造")

    def reader():
        return json.loads(lock_path.read_text(encoding="utf-8"))

    try:
        L.build_production_sender(lock_reader=reader, transport_factory=factory,
                                  admission=None, budget=None, envelope=None,
                                  spend_contract=None)
    except L.LoopError as exc:
        return {"code": exc.code, "terminal": bool(exc.terminal),
                "transport_factory_called": called["transport"],
                "detail": exc.detail[:200]}
    raise CalibrationError("SENDER_NOT_REFUSED",
                           "paid LOCK 缺失却构造出了 model sender")


def issue_zero_post_permit(ctx: Any, *, ledger_path: "Path | str") -> "dict[str, Any]":
    locks = _lock_present(ctx)
    if locks:
        raise CalibrationError("PAID_LOCK_PRESENT", f"paid LOCK 已存在：{locks}")
    rows = ledger_call_rows(ledger_path)
    if rows:
        raise CalibrationError("NOT_ZERO_POST", f"账本已有 {rows} 行 call")
    refusal = _prove_sender_refusal(ctx.relative(PAID_LOCK_RELPATH))
    if refusal.get("code") != "LOCK_MISSING" or refusal.get("transport_factory_called"):
        raise CalibrationError("SENDER_NOT_REFUSED", json.dumps(refusal)[:300])
    doc = {
        "schema": PERMIT_SCHEMA,
        "schema_version": 1,
        "envelope_id": ctx.envelope_id,
        "issued_at_utc": utc_now_iso_z(),
        "kind": "zero_post_control",
        "model_posts_allowed": 0,
        "paid_lock_present": False,
        "paid_lock_relpath": PAID_LOCK_RELPATH,
        "ledger_path": str(ledger_path),
        "ledger_call_rows_at_issue": 0,
        "sender_refusal": refusal,
        "note": ("本 permit 只覆盖不触网的物理校准；任何模型 POST 需要 T5 冻结后的 "
                 "paid permit（LOCK 存在才可构造 transport）"),
    }
    ctx.write_json(PERMIT_RELPATH, doc)
    return doc


def assert_permit_usable(ctx: Any, permit: Mapping[str, Any], *,
                         ledger_path: "Path | str") -> None:
    if permit.get("schema") != PERMIT_SCHEMA:
        raise CalibrationError("PERMIT_SCHEMA", f"{permit.get('schema')!r}")
    if permit.get("kind") != "zero_post_control" or permit.get("model_posts_allowed") != 0:
        raise CalibrationError("PERMIT_KIND", "permit 不是零 POST 控制件")
    if permit.get("envelope_id") != ctx.envelope_id:
        raise CalibrationError("PERMIT_ENVELOPE", "permit 属于另一个 envelope")
    if _lock_present(ctx):
        raise CalibrationError("PAID_LOCK_PRESENT", "paid LOCK 已出现，零 POST 期结束")
    rows = ledger_call_rows(ledger_path)
    if rows:
        raise CalibrationError("NOT_ZERO_POST", f"账本已有 {rows} 行 call")
    if (permit.get("sender_refusal") or {}).get("code") != "LOCK_MISSING":
        raise CalibrationError("PERMIT_PROOF", "permit 缺 LOCK_MISSING 拒绝取证")


def load_permit(ctx: Any) -> "dict[str, Any]":
    if not ctx.exists(PERMIT_RELPATH):
        raise CalibrationError("PERMIT_MISSING", "先 issue zero-post permit")
    return ctx.read_json(PERMIT_RELPATH)


# ---------------------------------------------------------------------------
# 资源账（含 A 的整定步）
# ---------------------------------------------------------------------------

@dataclass
class CalibrationBook(E.ResourceBook):
    """同 ``ResourceBook`` 语义，但把 A 的整定步诚实计入步数帽。"""

    def reserve(self, spec: "E.ExperimentSpec") -> None:
        extra = P.A_SETTLE_STEPS if spec.study_id == "A" else 0
        if self.used_runs + 1 > self.max_runs:
            raise E.ExperimentError(
                "RESOURCE_EXCEEDED", f"runs {self.used_runs + 1} 超过帽 {self.max_runs}")
        if self.used_steps + spec.nsteps + extra > self.max_steps:
            raise E.ExperimentError(
                "RESOURCE_EXCEEDED",
                f"steps {self.used_steps + spec.nsteps + extra} 超过帽 {self.max_steps}")
        self.used_runs += 1
        self.used_steps += spec.nsteps + extra


# ---------------------------------------------------------------------------
# 检查
# ---------------------------------------------------------------------------

def _check(checks: "list[dict[str, Any]]", cid: str, ok: bool,
           detail: str = "", values: "Mapping[str, Any] | None" = None) -> None:
    checks.append({"id": cid, "ok": bool(ok), "detail": detail,
                   "values": dict(values or {})})


def _contact_stats(record: Mapping[str, Any]) -> "tuple[float, int]":
    dims = record["contact_dim_per_step"]
    n = len(dims)
    good = sum(1 for d in dims if d == 3)
    gap = cur = 0
    for d in dims:
        if d == 3:
            cur = 0
        else:
            cur += 1
            gap = max(gap, cur)
    return good / n, gap


def _study_a_checks(entry, record, record_meta, metric, oracle_doc,
                    checks: "list[dict[str, Any]]",
                    design: Mapping[str, Any] = DESIGN_V1) -> "dict[str, Any]":
    run_id = record["run_id"]
    params = record["params_applied"]
    mass = float(params["mass_kg"])
    v0 = float(params["v0_mps"])
    g = float(_gravity())
    n = record["nsteps"]
    fraction, gap = _contact_stats(record)
    _check(checks, f"A-EXPO-CONTACT-{run_id}",
           fraction >= float(CONTACT_MIN_FRACTION) and gap <= CONTACT_MAX_GAP_STEPS,
           f"fraction={fraction:.5f} gap={gap}",
           {"fraction": fraction, "max_gap_steps": gap})
    mean_force = sum(float(v) for v in record["contact_force_n"]) / n
    ratio = mean_force / (mass * g)
    _check(checks, f"A-EXPO-FORCE-{run_id}",
           abs(ratio - 1.0) <= float(FORCE_BALANCE_REL),
           f"mean/(m·g)={ratio:.6f}", {"ratio": ratio})
    z0 = float(record_meta["initial_z_m"])
    z_exc = max(abs(float(row[2]) - z0) for row in record["qpos"])
    _check(checks, f"A-EXPO-Z-{run_id}", z_exc <= float(Z_EXCURSION_MAX_M),
           f"max|z-z0|={z_exc:.3e}", {"z_excursion_m": z_exc})
    if design["v0_gate"] == "first_step_displacement":
        # v2：首步位移必须恰为 v0·dt（证明 qvel 先于首次 mj_step 写入）。
        dt = float(record["time_s"][1]) - float(record["time_s"][0])
        dx = float(record["qpos"][0][0]) - float(record_meta["initial_x_m"])
        pred = v0 * dt
        v0_err = abs(dx - pred)
        _check(checks, f"A-EXPO-V0-{run_id}", v0_err <= float(V0_FIRST_STEP_DISP_TOL_M),
               f"|dx-v0·dt|={v0_err:.2e} m (dx={dx:.9f}, v0·dt={pred:.9f})",
               {"v0_first_step_disp_error_m": v0_err, "dx_m": dx, "v0_dt_m": pred,
                "dt_s": dt})
    else:
        vx = [float(row[0]) for row in record["qvel"]]
        v0_err = max(abs(vx[i] - v0) for i in range(min(V0_CHECK_SAMPLES, len(vx))))
        _check(checks, f"A-EXPO-V0-{run_id}", v0_err <= float(V0_TOL_MPS),
               f"max|vx-v0|={v0_err:.4f}", {"v0_error_mps": v0_err})
    disp = float(metric.values[M.DISPLACEMENT])
    _check(checks, f"A-MOVED-{run_id}", abs(disp) >= float(MIN_MOVED_M),
           f"|disp|={abs(disp):.5f}", {"displacement_m": disp})

    oracle = oracle_doc.study_a_oracle(
        mu=float(params["sliding_friction"]), mass_kg=mass, v0=v0,
        duration_s=float(S.DURATION_S))
    tol = max(float(A_ORACLE_ABS_TOL_M),
              float(A_ORACLE_REL_TOL) * abs(oracle["displacement_m"]))
    diff = abs(disp - oracle["displacement_m"])
    _check(checks, f"A-ORACLE-DISP-{run_id}", diff <= tol,
           f"sim={disp:.6f} oracle={oracle['displacement_m']:.6f} diff={diff:.2e}",
           {"sim": disp, "oracle": oracle["displacement_m"], "diff": diff,
            "tol": tol})
    censored = bool(metric.censored[M.STOP_TIME])
    stop_ok = censored == bool(oracle["censored"])
    stop_diff = None
    if stop_ok and not censored:
        stop_diff = abs(float(metric.values[M.STOP_TIME]) - oracle["stop_time_s"])
        stop_ok = stop_diff <= float(A_ORACLE_STOP_TOL_S)
    _check(checks, f"A-ORACLE-STOP-{run_id}", stop_ok,
           f"censored={censored} oracle_censored={oracle['censored']} diff={stop_diff}",
           {"censored": censored, "oracle_censored": oracle["censored"],
            "diff_s": stop_diff})
    return {"run_id": run_id, "study": "A", "role": entry["role"],
            "block_id": entry["block_id"], "condition_id": entry["condition_id"],
            "params": dict(params), "metrics": metric.to_dict(),
            "oracle": oracle}


def _study_b_checks(entry, record, metric, oracle_doc,
                    checks: "list[dict[str, Any]]",
                    design: Mapping[str, Any] = DESIGN_V1) -> "dict[str, Any]":
    run_id = record["run_id"]
    params = record["params_applied"]
    dt = float(params["dt_s"])
    tc = float(params["tc_s"])
    stride = int(round(tc / dt))
    index = [int(v) for v in record["control_update_index"]]
    stride_ok = all(index[i] == i // stride for i in range(len(index)))
    _check(checks, f"B-EXEC-STRIDE-{run_id}", stride_ok,
           f"stride={stride} n={len(index)}", {"stride": stride})
    amp = float(params["target_amplitude_rad"])
    freq = float(params["target_freq_hz"])
    worst = 0.0
    for t, tgt in zip(record["time_s"], record["target_q"]):
        want = amp * math.sin(2.0 * math.pi * freq * float(t))
        worst = max(worst, abs(float(tgt) - want))
    _check(checks, f"B-EXEC-TARGET-{run_id}", worst <= float(B_TARGET_TOL_RAD),
           f"max|target-want|={worst:.2e}", {"max_target_error_rad": worst})
    events = record.get("instability_events") or []
    _check(checks, f"B-NO-INSTABILITY-{run_id}", not events,
           f"events={events}", {"events": list(events)})
    oracle = oracle_doc.study_b_rmse(
        inertia_ratio=float(params["inertia_ratio"]), amplitude_rad=amp,
        freq_hz=freq, tc_s=tc, dt_s=dt, controller=params["controller"],
        kp=float(params["kp"]), kd=float(params["kd"]),
        duration_s=float(S.DURATION_S),
        window_start_s=float(R.TRACKING_WINDOW_START_S),
        window_end_s=float(R.TRACKING_WINDOW_END_S))
    sim = float(metric.values[M.TRACKING_RMSE])
    diff = abs(sim - oracle["normalized_rmse"])
    tol = float(B_ORACLE_ABS_TOL) + float(design["b_oracle_rel"]) * abs(
        float(oracle["normalized_rmse"]))
    _check(checks, f"B-ORACLE-{run_id}", diff <= tol,
           f"sim={sim:.6f} oracle={oracle['normalized_rmse']:.6f} diff={diff:.2e} tol={tol:.5f}",
           {"sim": sim, "oracle": oracle["normalized_rmse"], "diff": diff, "tol": tol})
    if entry["block_id"] in design["underdamped_blocks"]:
        _b_phase_check(entry, record, oracle_doc, checks)
    return {"run_id": run_id, "study": "B", "role": entry["role"],
            "block_id": entry["block_id"], "condition_id": entry["condition_id"],
            "params": dict(params), "metrics": metric.to_dict(),
            "oracle": oracle}


def _b_phase_check(entry, record, oracle_doc,
                   checks: "list[dict[str, Any]]") -> None:
    """v2 第二证据：欠阻尼 block 的周期与包络（相位不敏感）。"""
    run_id = record["run_id"]
    params = record["params_applied"]
    trace_fn = getattr(oracle_doc, "study_b_trace", None)
    if trace_fn is None:
        _check(checks, f"B-ORACLE-PHASE-{run_id}", False,
               "oracle 缺 study_b_trace（v2 需参考轨迹）")
        return
    kw = dict(inertia_ratio=float(params["inertia_ratio"]),
              amplitude_rad=float(params["target_amplitude_rad"]),
              freq_hz=float(params["target_freq_hz"]),
              tc_s=float(params["tc_s"]), dt_s=float(params["dt_s"]),
              controller=params["controller"], kp=float(params["kp"]),
              kd=float(params["kd"]), duration_s=float(S.DURATION_S))
    start = float(R.TRACKING_WINDOW_START_S)
    end = float(R.TRACKING_WINDOW_END_S)
    sim_stats = M.tracking_period_and_envelope(
        record["time_s"], [float(row[0]) for row in record["qpos"]],
        window_start_s=start, window_end_s=end)
    ref = trace_fn(**kw)
    ref_stats = M.tracking_period_and_envelope(
        ref["time_s"], ref["q"], window_start_s=start, window_end_s=end)
    if sim_stats is None or ref_stats is None:
        _check(checks, f"B-ORACLE-PHASE-{run_id}", False,
               "周期/包络不可辨识（窗口内向上过零 <2）")
        return
    period_rel = (abs(sim_stats["period_s"] - ref_stats["period_s"])
                  / ref_stats["period_s"])
    env_rel = (abs(sim_stats["envelope"] - ref_stats["envelope"])
               / ref_stats["envelope"])
    ok = (period_rel <= float(B_PERIOD_REL_TOL)
          and env_rel <= float(B_ENVELOPE_REL_TOL))
    _check(checks, f"B-ORACLE-PHASE-{run_id}", ok,
           f"period_rel={period_rel:.4f} envelope_rel={env_rel:.4f}",
           {"period_rel": period_rel, "envelope_rel": env_rel,
            "sim_period_s": sim_stats["period_s"],
            "reference_period_s": ref_stats["period_s"],
            "sim_envelope": sim_stats["envelope"],
            "reference_envelope": ref_stats["envelope"]})


def _a_group_checks(runs_doc, checks) -> None:
    by_block: "dict[str, dict[str, dict[str, Any]]]" = {}
    for row in runs_doc:
        if row["study"] != "A" or row["role"] not in ("negative_base", "negative_sham"):
            continue
        by_block.setdefault(row["block_id"], {})[row["role"]] = row
    delta = float(R.METRICS[R.NEGATIVE_CONTROL_METRIC]["delta"])
    for block, pair in sorted(by_block.items()):
        base, sham = pair.get("negative_base"), pair.get("negative_sham")
        if base is None or sham is None:
            _check(checks, f"A-NEG-{block}", False, "缺 base/sham 半边")
            _check(checks, f"A-PAIR-{block}", False, "缺 base/sham 半边")
            continue
        diff = abs(float(base["metrics"]["values"][M.DISPLACEMENT])
                   - float(sham["metrics"]["values"][M.DISPLACEMENT]))
        _check(checks, f"A-NEG-{block}", diff <= delta,
               f"|Δdisp|={diff:.3e} ≤ {delta}", {"abs_diff_m": diff})
        pa, ps = dict(base["params"]), dict(sham["params"])
        changed = sorted(k for k in set(pa) | set(ps) if pa.get(k) != ps.get(k))
        ok = (changed == list(R.NEGATIVE_CONTROL_ALLOWED_DIFF)
              and ps.get("torsional_friction_m") == A_SHAM_TORSIONAL
              and pa.get("torsional_friction_m") == A_BASE_TORSIONAL)
        _check(checks, f"A-PAIR-{block}", ok, f"changed={changed}",
               {"changed_fields": changed})

    def _find(block, role):
        for row in runs_doc:
            if row["block_id"] == block and row["role"] == role:
                return row
        return None

    low, high = _find("s01", "sensitivity"), _find("s02", "sensitivity")
    if low is None or high is None:
        _check(checks, "A-SENS", False, "缺灵敏度半边")
    else:
        diff = abs(float(low["metrics"]["values"][M.DISPLACEMENT])
                   - float(high["metrics"]["values"][M.DISPLACEMENT]))
        need = float(R.SENSITIVITY_MIN_DISPLACEMENT_DIFF_M)
        _check(checks, "A-SENS", diff > need, f"|Δdisp|={diff:.5f} > {need}",
               {"abs_diff_m": diff, "min_required_m": need})


def _b_group_checks(runs_doc, checks) -> None:
    cal = [r for r in runs_doc if r["study"] == "B" and r["role"] == "b_cal"]
    values = sorted({round(float(r["metrics"]["values"][M.TRACKING_RMSE]), 9)
                     for r in cal})
    _check(checks, "B-IDENT", len(values) >= B_MIN_DISTINCT_METRICS,
           f"distinct={len(values)} values={values}",
           {"distinct_metrics": len(values)})


def _replay_checks(runs_doc, checks) -> None:
    base = next((r for r in runs_doc if r["study"] == "A"
                 and r["block_id"] == A_REPLAY_BLOCK and r["role"] == "negative_base"),
                None)
    rep = next((r for r in runs_doc if r["study"] == "A" and r["role"] == "replay"),
               None)
    if base is None or rep is None:
        _check(checks, "A-REPLAY", False, "缺基准或复跑")
    else:
        disp_abs = R.REPLAY_TOLERANCES["displacement_x_m"][0]
        stop_abs = R.REPLAY_TOLERANCES["stop_time_s"][0]
        b_disp = float(base["metrics"]["values"][M.DISPLACEMENT])
        r_disp = float(rep["metrics"]["values"][M.DISPLACEMENT])
        d1 = abs(b_disp - r_disp)
        cens = (base["metrics"]["censored"][M.STOP_TIME]
                == rep["metrics"]["censored"][M.STOP_TIME])
        stop_ok = cens
        d2 = None
        if cens and not base["metrics"]["censored"][M.STOP_TIME]:
            d2 = abs(float(base["metrics"]["values"][M.STOP_TIME])
                     - float(rep["metrics"]["values"][M.STOP_TIME]))
            stop_ok = d2 <= float(stop_abs)
        _check(checks, "A-REPLAY", d1 <= float(disp_abs) and stop_ok,
               f"Δdisp={d1:.2e} Δstop={d2}",
               {"disp_diff_m": d1, "stop_diff_s": d2})
    bbase = next((r for r in runs_doc if r["study"] == "B"
                  and r["block_id"] == B_REPLAY_SOURCE and r["role"] == "b_cal"), None)
    brep = next((r for r in runs_doc if r["study"] == "B" and r["role"] == "replay"), None)
    if bbase is None or brep is None:
        _check(checks, "B-REPLAY", False, "缺基准或复跑")
    else:
        d = abs(float(bbase["metrics"]["values"][M.TRACKING_RMSE])
                - float(brep["metrics"]["values"][M.TRACKING_RMSE]))
        _check(checks, "B-REPLAY",
               d <= float(R.REPLAY_TOLERANCES["normalized_tracking_rmse"][0]),
               f"Δrmse={d:.2e}", {"rmse_diff": d})


def _validate_check_registry(
        checks,
        families: "tuple[tuple[str, str, int], ...] | None" = None) -> None:
    families = CHECK_FAMILIES if families is None else families
    counts: "dict[str, int]" = {}
    # 家族名互为前缀（B-ORACLE / B-ORACLE-PHASE）⇒ 必须最长匹配优先，
    # 否则 B-ORACLE-PHASE-* 会被记到 B-ORACLE 名下（实得 12 vs 期望 9）。
    names = sorted((fam for fam, _, _ in families), key=len, reverse=True)
    for c in checks:
        fam = next((f for f in names
                    if c["id"] == f or c["id"].startswith(f + "-")), None)
        if fam is None:
            raise CalibrationError("CHECK_UNREGISTERED", c["id"])
        counts[fam] = counts.get(fam, 0) + 1
    for fam, _desc, count in families:
        if counts.get(fam, 0) != count:
            raise CalibrationError(
                "CHECK_COUNT", f"{fam} 期望 {count} 实得 {counts.get(fam, 0)}")


def _verdict(checks) -> str:
    bad = [c for c in checks if not c["ok"]]
    if not bad:
        return "CALIBRATION_PASS"
    if any(c["id"].startswith(_INTEGRITY_PREFIXES) for c in bad):
        return "INVALID"
    return "DESIGN_NOT_READY"


# ---------------------------------------------------------------------------
# 运行
# ---------------------------------------------------------------------------

def collect_run_checks(*, a_entries, a_specs, ev_a, a_metas,
                       b_entries, b_specs, ev_b, oracle,
                       design: Mapping[str, Any] = DESIGN_V1
                       ) -> "tuple[list[dict[str, Any]], list[dict[str, Any]]]":
    """逐 run + 分组 + 复跑 + 饱和计数检查（生产路径与离线测试共用同一实现）。

    ``design`` 必须一路透传到 study 检查：v2 的暴露门 / 尺度感知容差 / 相位第二
    证据都由它切换（T1b-v2 abort 即因漏传而在 v2 预注册下跑了 v1 语义）。
    """
    checks: "list[dict[str, Any]]" = []
    runs_doc: "list[dict[str, Any]]" = []
    for e, spec in zip(a_entries, a_specs):
        record = _record_of(ev_a, spec.run_id)
        meta = a_metas[spec.run_id]
        metric = M.compute_study_a(record, initial_x_m=meta["initial_x_m"])
        runs_doc.append(_study_a_checks(e, record, meta, metric, oracle, checks,
                                        design=design))
    for e, spec in zip(b_entries, b_specs):
        record = _record_of(ev_b, spec.run_id)
        metric = M.compute_study_b(
            record,
            amplitude_rad=float(record["params_applied"]["target_amplitude_rad"]))
        runs_doc.append(_study_b_checks(e, record, metric, oracle, checks,
                                        design=design))
    _a_group_checks(runs_doc, checks)
    _b_group_checks(runs_doc, checks)
    _replay_checks(runs_doc, checks)
    if ev_b is not None:
        _fill_b_saturation(ev_b, b_specs, checks)
    return runs_doc, checks


def run_calibration(ctx: Any, *, runner: Any, image: str = CAL_IMAGE,
                    ledger_path: "Path | str | None" = None,
                    notes: "Iterable[str]" = (),
                    design: Mapping[str, Any] = DESIGN_V1) -> "dict[str, Any]":
    """沿正式 dispatcher 跑固定校准组合；返回并落盘报告。"""
    ledger = Path(ledger_path) if ledger_path is not None else _default_ledger(ctx)
    prereg = load_calibration_prereg(ctx, design)
    oracle_path = _oracle_path(ctx, design)
    assert_prereg_matches_sources(prereg, oracle_path=oracle_path)
    _assert_oracle_private(ctx, oracle_path, prereg["oracle"]["sha256"])
    permit = load_permit(ctx)
    assert_permit_usable(ctx, permit, ledger_path=ledger)
    oracle = load_oracle(oracle_path, schema=design["oracle_schema"])

    entries = calibration_matrix()
    specs = specs_for_matrix(entries)
    spec_by_run = {s.run_id: s for s in specs}
    rows_by_run = {r["run_id"]: r for r in prereg["matrix"]}
    for s in specs:
        row = rows_by_run.get(s.run_id)
        if row is None or row["spec_hash"] != s.spec_hash:
            raise CalibrationError("MATRIX_DRIFT", f"{s.run_id} 不在预注册矩阵")
    if prereg.get("container_image") != image:
        raise CalibrationError("IMAGE_DRIFT",
                               f"镜像 {image} 与预注册 {prereg.get('container_image')} 不符")

    a_entries = [e for e in entries if e["study"] == "A"]
    b_entries = [e for e in entries if e["study"] == "B"]
    a_specs = [spec_by_run_for(e, specs) for e in a_entries]
    b_specs = [spec_by_run_for(e, specs) for e in b_entries]

    book = CalibrationBook(max_runs=CAL_MAX_RUNS, max_steps=CAL_MAX_STEPS)
    suffix = "" if design["version"] == 1 else f"_{design['key']}"
    be_a = BK.PhysicsBackend(
        observations_root=ctx.subdir("observations"),
        evidence_root=ctx.subdir("evidence_pack"),
        work_root=ctx.subdir("private") / f"calibration_work{suffix}",
        runner=runner, image=image, batch_id=design["batch_a"])
    be_b = BK.PhysicsBackend(
        observations_root=ctx.subdir("observations"),
        evidence_root=ctx.subdir("evidence_pack"),
        work_root=ctx.subdir("private") / f"calibration_work_b{suffix}",
        runner=runner, image=image, batch_id=design["batch_b"])

    # v2：先核 v1 尝试（预注册/报告/缺口件/oracle/raw）未被改写；失败即不跑容器、
    # 不产生新 raw，报告判 INVALID（历史不可改写是完整性条件，不是科学结论）。
    prior_err = None
    prior_now = None
    if design["version"] >= 2:
        try:
            prior_now = verify_prior_attempt(ctx, prereg.get("prior_attempt") or {})
        except CalibrationError as exc:
            prior_err = str(exc)

    ev_a = None
    ev_b = None
    if prior_err is None:
        ev_a = E.execute_batch(a_specs, be_a, book)
        ev_b = E.execute_batch(b_specs, be_b, book) if not ev_a.blocked else None

    checks: "list[dict[str, Any]]" = []
    runs_doc: "list[dict[str, Any]]" = []
    blocked = None
    if prior_err is not None:
        blocked = f"PRIOR_ATTEMPT: {prior_err}"
    elif ev_a.blocked:
        blocked = f"A: {ev_a.reason}"
    elif ev_b is not None and ev_b.blocked:
        blocked = f"B: {ev_b.reason}"
    elif ev_a.n_ok != len(a_specs) or (ev_b is not None and ev_b.n_ok != len(b_specs)):
        blocked = (f"partial: A {ev_a.n_ok}/{len(a_specs)} ok, "
                   f"B {None if ev_b is None else ev_b.n_ok}/{len(b_specs)} ok")
    if blocked is None:
        runs_doc, checks = collect_run_checks(
            a_entries=a_entries, a_specs=a_specs, ev_a=ev_a, a_metas=be_a.metas,
            b_entries=b_entries, b_specs=b_specs, ev_b=ev_b, oracle=oracle,
            design=design)

    # ---- 全局完整性检查（失败即 INVALID）----
    raw_paths = _raw_paths(ctx, design)
    try:
        PR.assert_prereg_precedes_raw(prereg, raw_paths)
        _check(checks, "GLOBAL-PREREG-FIRST", True,
               f"{len(raw_paths)} raw 件晚于预注册")
    except PR.PreregError as exc:
        _check(checks, "GLOBAL-PREREG-FIRST", False, str(exc))
    try:
        assert_prereg_matches_sources(prereg, oracle_path=oracle_path)
        _assert_oracle_private(ctx, oracle_path, prereg["oracle"]["sha256"])
        _check(checks, "GLOBAL-PREREG-SOURCES", True, "矩阵/量表/oracle 一致")
        _check(checks, "GLOBAL-ORACLE-PRIVATE", True, ORACLE_RELPATH)
    except CalibrationError as exc:
        _check(checks, "GLOBAL-PREREG-SOURCES", False, str(exc))
        _check(checks, "GLOBAL-ORACLE-PRIVATE", False, str(exc))
    try:
        assert_permit_usable(ctx, permit, ledger_path=ledger)
        _check(checks, "GLOBAL-PERMIT", True, "zero-post permit 有效")
    except CalibrationError as exc:
        _check(checks, "GLOBAL-PERMIT", False, str(exc))
    rows = ledger_call_rows(ledger)
    _check(checks, "GLOBAL-LEDGER-ZERO", rows == 0, f"call_rows={rows}",
           {"call_rows": rows})
    used = book.to_dict()
    settle = sum(P.A_SETTLE_STEPS for _ in a_specs)
    total_runs = used["used_runs"] + PRE_PROBE_ESTIMATE_RUNS
    total_steps = used["used_steps"] + PRE_PROBE_ESTIMATE_STEPS
    _check(checks, "GLOBAL-BUDGET",
           total_runs <= CAL_MAX_RUNS and total_steps <= CAL_MAX_STEPS,
           f"runs={total_runs}/{CAL_MAX_RUNS} steps={total_steps}/{CAL_MAX_STEPS}",
           {"runs": total_runs, "steps": total_steps})
    if design["version"] >= 2:
        _check(checks, "GLOBAL-PRIOR-ATTEMPT", prior_err is None,
               ("v1 预注册/报告/oracle/raw 逐字节未变 "
                f"（{PRIOR_ATTEMPT_FILES.__len__()} 件 + "
                f"{(prior_now or {}).get('n_raws')} raw）")
               if prior_err is None else prior_err,
               {"n_files": (prior_now or {}).get("n_files"),
                "n_raws": (prior_now or {}).get("n_raws"),
                "recorded_files_sha256": (prereg.get("prior_attempt") or {}).get(
                    "files_sha256"),
                "recorded_raws_sha256": (prereg.get("prior_attempt") or {}).get(
                    "raws_sha256")})

    verdict = "INVALID" if blocked else _verdict(checks)
    if blocked is None:
        _validate_check_registry(checks, design["families"])
    report = {
        "schema": design["report_schema"],
        "schema_version": design["version"],
        "envelope_id": ctx.envelope_id,
        "generated_at_utc": utc_now_iso_z(),
        "verdict": verdict,
        "blocked": blocked,
        "prereg_sha256": prereg["document_sha256"],
        "instrument_prereg_sha256": prereg.get("instrument_prereg_sha256"),
        "oracle_sha256": prereg["oracle"]["sha256"],
        "container_image": image,
        "permit": {"kind": permit.get("kind"),
                   "model_posts_allowed": permit.get("model_posts_allowed"),
                   "sender_refusal": permit.get("sender_refusal")},
        "budget": {
            "book": used,
            "settle_steps": settle,
            "pre_probe_estimate_runs": PRE_PROBE_ESTIMATE_RUNS,
            "pre_probe_estimate_steps": PRE_PROBE_ESTIMATE_STEPS,
            "total_runs": total_runs,
            "total_steps": total_steps,
            "max_runs": CAL_MAX_RUNS,
            "max_steps": CAL_MAX_STEPS,
        },
        "runs": runs_doc,
        "checks": checks,
        "failures": [c for c in checks if not c["ok"]],
        "notes": list(notes),
    }
    if design["version"] >= 2:
        report["design_version"] = design["version"]
        report["supersedes"] = dict(prereg.get("supersedes") or {})
        report["prior_attempt"] = {"files_sha256": (prereg.get("prior_attempt") or {}).get(
            "files_sha256"),
            "raws_sha256": (prereg.get("prior_attempt") or {}).get("raws_sha256"),
            "verified": prior_err is None}
    ctx.write_json(design["report_relpath"], report)
    ctx.write_text(design["report_md_relpath"], render_report_markdown(report))
    return report


def spec_by_run_for(entry: Mapping[str, Any], specs) -> "E.ExperimentSpec":
    for s in specs:
        if (s.study_id == entry["study"] and s.block_id == entry["block_id"]
                and s.condition_id == entry["condition_id"]):
            return s
    raise CalibrationError("MATRIX_LOOKUP", f"找不到 {entry}")


def _record_of(ev_a: "E.BatchEvidence", run_id: str) -> "Mapping[str, Any]":
    for outcome in ev_a.outcomes:
        if outcome.run_id == run_id:
            if not outcome.ok or outcome.record is None:
                raise CalibrationError(
                    "RUN_FAILED", f"{run_id}: {outcome.error_code}")
            return outcome.record
    raise CalibrationError("RUN_MISSING", run_id)


def _fill_b_saturation(ev_b: "E.BatchEvidence", b_specs,
                       checks: "list[dict[str, Any]]") -> None:
    sat_counts = {}
    for outcome in ev_b.outcomes:
        if outcome.ok and outcome.record is not None:
            sat_counts[outcome.run_id] = int(outcome.record["saturation_count"])
    target = next((s for s in b_specs if s.block_id == B_SATURATION_BLOCK), None)
    ok = target is not None and sat_counts.get(target.run_id, 0) > 0
    _check(checks, "B-SAT", ok,
           f"block={B_SATURATION_BLOCK} saturation_count="
           f"{sat_counts.get(target.run_id) if target else None}",
           {"saturation_count": sat_counts.get(target.run_id) if target else None})


def _default_ledger(ctx: Any) -> Path:
    from orchestrator.common import runtime_paths as RP
    return RP.rsi_r1_ledger_path()


# ---------------------------------------------------------------------------
# 报告渲染
# ---------------------------------------------------------------------------

def render_report_markdown(report: Mapping[str, Any]) -> str:
    lines = ["# RSI-R1 T1b 校准报告", ""]
    lines.append(f"- verdict: **{report['verdict']}**")
    lines.append(f"- envelope: `{report['envelope_id']}`")
    lines.append(f"- prereg sha256: `{report['prereg_sha256']}`")
    lines.append(f"- oracle sha256: `{report['oracle_sha256']}`")
    lines.append(f"- 容器镜像: `{report['container_image']}`")
    if report.get("blocked"):
        lines.append(f"- blocked: `{report['blocked']}`")
    b = report["budget"]
    lines.append(f"- 预算: runs {b['total_runs']}/{b['max_runs']}，"
                 f"steps {b['total_steps']}/{b['max_steps']}"
                 f"（含预探测估计 {b['pre_probe_estimate_runs']} runs / "
                 f"{b['pre_probe_estimate_steps']} steps）")
    lines.append("- 缺口与处置: 见 `contract/calibration_gap_analysis.md`（如已登记；"
                 "按 Plan §7，容差超出不得调阈/换种子救场）")
    lines.append("")
    lines.append("## 检查汇总")
    lines.append("")
    counts: "dict[str, list[int]]" = {}
    for c in report["checks"]:
        fam = next((f for f, _, _ in CHECK_FAMILIES if c["id"].startswith(f + "-")),
                   c["id"])
        slot = counts.setdefault(fam, [0, 0])
        slot[0] += 1
        slot[1] += 0 if c["ok"] else 1
    lines.append("| 家族 | 通过/总数 | 失败 |")
    lines.append("|---|---|---|")
    for fam in [f for f, _, _ in CHECK_FAMILIES] + sorted(set(counts) - {f for f, _, _ in CHECK_FAMILIES}):
        if fam not in counts:
            continue
        total, bad = counts[fam]
        lines.append(f"| {fam} | {total - bad}/{total} | {bad} |")
    failures = report["failures"]
    lines.append("")
    if failures:
        lines.append("## 失败项")
        lines.append("")
        for c in failures:
            lines.append(f"- `{c['id']}`: {c['detail']}")
    else:
        lines.append("全部检查通过。")
    lines.append("")
    lines.append("## run 指标")
    lines.append("")
    lines.append("| run_id | role | metrics | oracle |")
    lines.append("|---|---|---|---|")
    for r in report["runs"]:
        metrics = "; ".join(f"{k}={v}" for k, v in r["metrics"]["values"].items())
        if "normalized_rmse" in r["oracle"]:
            oracle = f"rmse={r['oracle']['normalized_rmse']:.6f}"
        elif "displacement_m" in r["oracle"]:
            oracle = (f"disp={r['oracle']['displacement_m']:.6f} "
                      f"stop={r['oracle']['stop_time_s']}")
        else:
            oracle = "-"
        lines.append(f"| `{r['run_id']}` | {r['role']} | {metrics} | {oracle} |")
    lines.append("")
    return "\n".join(lines)
