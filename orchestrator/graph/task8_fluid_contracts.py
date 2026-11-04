"""Task 8 流体 Stage1 构造性合同 C1–C7 的**评测期**判据（Phase 2.7 Task3 / BR-7）。

BR-7（2026-11-03 用户裁决）把已签收的构造性合同从**生成期 prompt** 迁到
**评测期 Gate 2 / WJ 校验器**判定：合同语义一个字不改，只改「在哪个阶段
执行」。本合同号沿用 Phase 2 战役口径（`Phase_2_Plan_Complete.md` 攢批102
「合同 v2 C1–C5」+ 攢批103 C6 + 攢批104 C7）：

===================  ==================================================
C1                   单体刚体壶：程序化 trimesh 生成开口空心壶网格
                     （wall+bottom 一体、watertight），经**一次**
                     ``scene.add_entity(morph=gs.morphs.Mesh(...))`` 创建；
                     严禁多 Box/Cylinder 散件拼壶、严禁只驱动其中几块
C2                   fixed 运动学驱动、绕壶嘴尖点铰链：逐帧 set_pos/
                     set_quat 下发同一实体；备选仅限**同一实体**改
                     fixed=False + 同变换
C3                   慢动力学：静置 ≥0.3s、θ=0→≥120° 历时 ≥1.5s、
                     smoothstep 类平滑、角速度**按逐帧数值导数峰值**
                     ≤80°/s、壶嘴漂移 ≤0.010m、合速度 ≤0.20m/s
C4                   v1 运动学不等式全保留：θ_max ≥120°、流出窗 ≥0.5s、
                     壶嘴水平投影 ρ ≤ R−0.010、壶嘴高度 ∈[rim+0.005,
                     rim+0.05]、壶体-杯解析间隙 ≥0.012m、50Hz 采样
C5                   双自检：``kinematic_precheck()`` 在任何 gs.init/
                     scene.build **之前**；``sim_blowout()`` 在仿真**之后**
                     （NaN/Inf 或 ‖pos‖>2.5m 比例 >2% 即结构化 FAILURE）
C6                   边流形：每条无向边恰被 2 个三角形引用；边界边数、
                     非流形边数计数为 0 且见证登记；is_watertight=True
C7                   见证值禁哨兵：几何见证一律真实有限量（禁止 -1/
                     None/NaN/inf 占位）、角速度按峰值而非均值上报
===================  ==================================================

## 两种证据等级（fail-closed，**不可判 ≠ 通过**）

- **运行期见证**（``witnesses``，契约里出生脚本自检在 FAILURE 分支落下的
  同一组数值）：数值可直接逐条对照阈值，判为 pass / violation。
- **静态判据**（``script_text`` + ``mesh_path``）：C1/C2/C5/C6 的结构性
  条款可以完全静态判定（AST + 网格拓扑）；C3 的时长/角速阈值只能读脚本
  常量（弱判据）；C4 的数值不等式与 C7 的见证真实性**没有运行期见证就无法
  判定**，一律记 ``undecidable`` 并写明原因——**绝不**把「无法判定」写成
  「通过」，也绝不为了好看把它记成「违例」。

所有检查函数签名统一为 ``(ContractEvidence) -> list[str]``：空列表 = 通过，
非空 = 违例文本（中文，直接进 Gate 2 卡与证据包）。``evaluate_contracts``
把三态（pass/violation/undecidable）聚合成一份可落盘的报告。

本模块**不携带任何解题实现**（硬规则 3）：它只读出生脚本的 AST 与网格
文件的拓扑，不 import Genesis、不建场景、不跑物理。合同原文的唯一归档仍在
``task8_fluid_prompts._kinematic_contract_block()`` /
``_vessel_structure_contract_block()``。
"""
from __future__ import annotations

import ast
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import task8_fluid_prompts as prompts

CONTRACTS_SCHEMA = "simpraxis.contracts_report.v1"

PASS = "pass"
VIOLATION = "violation"
UNDECIDABLE = "undecidable"

#: 合同号 → 一句话语义（与 task8_fluid_prompts 归档块逐条对应，编号沿用
#: Phase 2 战役口径；文字只做索引，语义以归档块为准）。
CONTRACT_TITLES: dict[str, str] = {
    "C1": "单体刚体壶（程序化 trimesh 开口空心、watertight；一次 Mesh "
          "add_entity，禁散件拼壶）",
    "C2": "fixed 运动学驱动、绕壶嘴尖点铰链（set_pos/set_quat 同一实体）",
    "C3": "慢动力学（静置≥0.3s、θ 0→≥120° 历时≥1.5s、smoothstep、"
          "角速峰值≤80°/s、漂移≤0.010m、合速度≤0.20m/s）",
    "C4": "v1 运动学不等式全保留（θ_max≥120°、流出窗≥0.5s、ρ 上界、"
          "壶嘴 z 窗、间隙≥0.012m、50Hz 采样）",
    "C5": "双自检（kinematic_precheck 在 scene.build 前；sim_blowout 在"
          "仿真后，阈值与 kind 固定）",
    "C6": "边流形（每条无向边恰 2 面；边界边/非流形边计数 0 且见证登记）",
    "C7": "见证值禁哨兵（真实有限量；角速度按逐帧数值导数峰值判）",
}

#: AC-T3-13 的机判锚点：Gate 2 侧必须**逐条**可定位，缺一条即红。
EXPECTED_CONTRACTS: frozenset[str] = frozenset(CONTRACT_TITLES)

# v1 合同（运动学）阈值：与归档块同源常量，禁止他处再写字面量。
LIP_RADIUS_MAX_M = round(prompts.CUP_INNER_RADIUS - 0.010, 3)   # 0.035
SPOUT_Z_LO_M = round(prompts.CUP_RIM_Z + 0.005, 3)              # 1.025
SPOUT_Z_HI_M = round(prompts.CUP_RIM_Z + 0.05, 3)               # 1.070
VESSEL_CUP_CLEARANCE_MIN_M = 0.012                               # v1 第 3 条
THETA_MAX_MIN_DEG = 120.0                                        # v1 第 1 条
OUTFLOW_WINDOW_MIN_S = 0.5                                       # v1 第 1 条
PRECHECK_SAMPLE_HZ_MIN = 50.0                                    # v1 第 4 条

# 网格「开口空心」判据：壳体积 / 包围盒体积必须低于此值（实心块 ≈1.0，
# 实心圆柱 ≈0.785，薄壁开口壶远低于 0.5）。
HOLLOW_VOLUME_RATIO_MAX = 0.5

_SMOOTH_EASE_RE = re.compile(r"smoothstep|smootherstep|ease_?in|ease_?out|"
                             r"cosine_?ramp|easing", re.IGNORECASE)
_ANG_SPEED_NAME_RE = re.compile(r"ang.*speed|omega|angular", re.IGNORECASE)
_SETTLE_NAME_RE = re.compile(r"settle", re.IGNORECASE)
_TILT_DUR_NAME_RE = re.compile(r"tilt.*(dur|time|s)|rise.*(dur|time)", re.IGNORECASE)


@dataclass(frozen=True)
class ContractEvidence:
    """一次 Gate 2 评测可拿到的最小证据面（缺项按「不可判」处理）。

    - ``script_text``：出生脚本全文（``genesis_fluid_pour.py``）。
    - ``mesh_path``：壶网格文件（STL/OBJ；缺省时由 :func:`collect_evidence`
      在 workdir 内按脚本引用解析）。
    - ``witnesses``：运行期见证 dict（契约里 ``kinematic_precheck`` /
      ``sim_blowout`` FAILURE 分支落下的同一组数值；成功跑通常没有）。
    - ``workdir``：产物目录（只用于定位脚本/网格，不写任何文件）。
    """

    script_text: "str | None" = None
    mesh_path: "Path | None" = None
    witnesses: "dict[str, Any] | None" = None
    workdir: "Path | None" = None

    def __post_init__(self) -> None:
        if self.mesh_path is not None:
            object.__setattr__(self, "mesh_path", Path(self.mesh_path))
        if self.workdir is not None:
            object.__setattr__(self, "workdir", Path(self.workdir))
        if self.witnesses is not None and not isinstance(self.witnesses, dict):
            raise ValueError("witnesses 必须是 dict 或 None")


# ---------------------------------------------------------------------------
# 小工具：AST 解析 / 数值见证
# ---------------------------------------------------------------------------

def _parse(ev: ContractEvidence) -> "tuple[ast.Module | None, list[str]]":
    if ev.script_text is None:
        return None, ["缺出生脚本全文（script_text=None）"]
    try:
        return ast.parse(ev.script_text), []
    except SyntaxError as exc:
        return None, [f"出生脚本无法解析（{type(exc).__name__}: {exc.msg}）"]


def _dotted(node: ast.AST) -> str:
    """``gs.morphs.Mesh`` / ``scene.add_entity`` / ``pot.set_pos`` 的点分名。"""
    parts: list[str] = []
    cur = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
        return ".".join(reversed(parts))
    return ""


def _calls(tree: ast.AST) -> "list[tuple[int, str, ast.Call]]":
    """按源码顺序返回 (行号, 点分函数名, Call 节点)。"""
    out: list[tuple[int, str, ast.Call]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _dotted(node.func)
            if name:
                out.append((getattr(node, "lineno", 0), name, node))
    out.sort(key=lambda item: item[0])
    return out


def _kw(call: ast.Call, name: str) -> "ast.AST | None":
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


def _const(node: "ast.AST | None") -> Any:
    return node.value if isinstance(node, ast.Constant) else None


def _receiver(name: str) -> str:
    return name.rsplit(".", 1)[0] if "." in name else ""


def _is_finite_number(value: Any) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(float(value)))


# ---------------------------------------------------------------------------
# C1：单体刚体壶（脚本结构 + 网格拓扑）
# ---------------------------------------------------------------------------

def check_c1_single_hollow_body(ev: ContractEvidence) -> list[str]:
    """C1：壶必须是**一个** Mesh 实体、来自程序化 trimesh 的开口空心水密件。"""
    tree, errs = _parse(ev)
    if tree is None:
        return [f"C1 不可判：{errs[0]}"]

    mesh_adds: list[ast.Call] = []
    for _lineno, name, call in _calls(tree):
        if not name.endswith("add_entity"):
            continue
        morph = call.args[0] if call.args else _kw(call, "morph")
        if not isinstance(morph, ast.Call):
            continue
        if _dotted(morph.func).endswith("morphs.Mesh") and _kw(morph, "file"):
            mesh_adds.append(call)
    violations: list[str] = []
    if len(mesh_adds) != 1:
        violations.append(
            f"C1 违例：壶必须经**恰好一次** Mesh add_entity 创建，"
            f"实测 {len(mesh_adds)} 次（多实体拼壶/散件都落此条）")
    for call in mesh_adds:
        morph = call.args[0] if call.args else _kw(call, "morph")
        if not isinstance(morph, ast.Call):
            continue
        nonconvex = _const(_kw(morph, "decompose_nonconvex"))
        if nonconvex is not True:
            violations.append(
                "C1 违例：Mesh morph 必须 decompose_nonconvex=True"
                "（凸包化会封死壶口）")
        fixed = _const(_kw(morph, "fixed"))
        if fixed is None:
            violations.append("C1 违例：Mesh morph 必须显式声明 fixed=...")

    violations.extend(_mesh_topology_violations(ev, "C1"))
    return violations


def _load_mesh(ev: ContractEvidence) -> "tuple[Any | None, str | None]":
    path = ev.mesh_path
    if path is None and ev.workdir is not None:
        path = _discover_mesh(ev.workdir, ev.script_text or "")
    if path is None:
        return None, "未找到壶网格文件（STL/OBJ）"
    if not Path(path).is_file():
        return None, f"壶网格文件不存在：{path}"
    try:
        import trimesh  # noqa: PLC0415 - 只在评测期按需加载
    except Exception as exc:  # noqa: BLE001
        return None, f"trimesh 不可用：{type(exc).__name__}: {exc}"
    try:
        return trimesh.load(str(path), force="mesh", process=False), None
    except Exception as exc:  # noqa: BLE001
        return None, f"网格无法加载：{type(exc).__name__}: {exc}"


def _mesh_topology_violations(ev: ContractEvidence, tag: str) -> list[str]:
    mesh, reason = _load_mesh(ev)
    if mesh is None:
        return [f"{tag} 不可判：{reason}"]
    violations: list[str] = []
    faces = getattr(mesh, "faces", None)
    if faces is None or len(faces) == 0:
        return [f"{tag} 违例：网格三角面数为 0（无法作为壶体）"]
    if not bool(getattr(mesh, "is_watertight", False)):
        violations.append(f"{tag} 违例：网格 is_watertight=False（壶体漏水）")
    extents = getattr(mesh, "extents", None)
    volume = getattr(mesh, "volume", None)
    if extents is not None and volume is not None:
        bbox_volume = float(extents[0]) * float(extents[1]) * float(extents[2])
        if bbox_volume > 0.0:
            ratio = float(volume) / bbox_volume
            if ratio > HOLLOW_VOLUME_RATIO_MAX:
                violations.append(
                    f"{tag} 违例：网格体积/包围盒体积={ratio:.3f} > "
                    f"{HOLLOW_VOLUME_RATIO_MAX}（实心块/实心柱，非开口空心壶）")
    return violations


def _discover_mesh(workdir: Path, script_text: str) -> "Path | None":
    """按脚本里出现的文件名优先匹配 workdir 内的 .stl/.obj；其次唯一件。"""
    try:
        candidates = sorted(
            p for p in Path(workdir).iterdir()
            if p.is_file() and p.suffix.lower() in (".stl", ".obj"))
    except OSError:
        return None
    if not candidates:
        return None
    mentioned = [p for p in candidates if p.name in script_text]
    if len(mentioned) == 1:
        return mentioned[0]
    if len(candidates) == 1:
        return candidates[0]
    return None


# ---------------------------------------------------------------------------
# C2：运动学驱动（绕嘴铰链 / 只驱动那一个实体）
# ---------------------------------------------------------------------------

def check_c2_kinematic_hinge(ev: ContractEvidence) -> list[str]:
    """C2：逐帧 set_pos/set_quat 驱动**同一个** Mesh 实体；散件驱动即违例。"""
    tree, errs = _parse(ev)
    if tree is None:
        return [f"C2 不可判：{errs[0]}"]
    calls = _calls(tree)

    mesh_handles: set[str] = set()
    for _lineno, name, call in calls:
        if not name.endswith("add_entity"):
            continue
        morph = call.args[0] if call.args else _kw(call, "morph")
        if isinstance(morph, ast.Call) and \
                _dotted(morph.func).endswith("morphs.Mesh") and _kw(morph, "file"):
            handle = _receiver(name)
            if handle:
                mesh_handles.add(handle)

    driven = {n for _l, n, _c in calls
              if n.endswith(".set_pos") or n.endswith(".set_quat")}
    violations: list[str] = []
    if not driven:
        violations.append("C2 违例：脚本没有任何 set_pos/set_quat 运动学驱动"
                          "（壶不会被倾倒）")
    receivers = {_receiver(n) for n in driven if _receiver(n)}
    if len(receivers) > 1:
        violations.append(
            f"C2 违例：逐帧驱动了 {len(receivers)} 个不同实体 "
            f"{sorted(receivers)}（合同只允许驱动那一个壶实体）")
    if mesh_handles and receivers and not (receivers & mesh_handles):
        violations.append(
            f"C2 违例：被驱动的实体 {sorted(receivers)} 不是 Mesh 壶句柄 "
            f"{sorted(mesh_handles)}（散件驱动/驱动器指错对象）")
    if not mesh_handles:
        violations.append("C2 不可判：脚本里找不到 Mesh 壶句柄（先过 C1）")

    witnesses = ev.witnesses or {}
    drift = witnesses.get("lip_drift_m")
    speed = witnesses.get("lip_speed_max_mps")
    if drift is None and speed is None:
        violations.append("C2 不可判：缺运行期见证 lip_drift_m / "
                          "lip_speed_max_mps（铰链漂移无法判定）")
    if drift is not None:
        if not _is_finite_number(drift):
            violations.append(f"C2 违例：lip_drift_m 非有限数：{drift!r}")
        elif float(drift) > prompts.VESSEL_LIP_DRIFT_MAX_M:
            violations.append(
                f"C2 违例：壶嘴漂移 {float(drift):.4f}m > "
                f"{prompts.VESSEL_LIP_DRIFT_MAX_M}m（不是绕嘴铰链）")
    if speed is not None:
        if not _is_finite_number(speed):
            violations.append(f"C2 违例：lip_speed_max_mps 非有限数：{speed!r}")
        elif float(speed) > prompts.VESSEL_LIP_SPEED_MAX_MPS:
            violations.append(
                f"C2 违例：壶嘴合速度 {float(speed):.4f}m/s > "
                f"{prompts.VESSEL_LIP_SPEED_MAX_MPS}m/s")
    return violations


# ---------------------------------------------------------------------------
# C3：慢动力学
# ---------------------------------------------------------------------------

def check_c3_slow_dynamics(ev: ContractEvidence) -> list[str]:
    """C3：静置/历时/平滑/角速峰值四件；有序数值即判，否则读脚本常量（弱）。"""
    witnesses = ev.witnesses or {}
    violations: list[str] = []
    checked = 0

    settle = witnesses.get("settle_s")
    if settle is not None:
        checked += 1
        if not _is_finite_number(settle):
            violations.append(f"C3 违例：settle_s 非有限数：{settle!r}")
        elif float(settle) < prompts.VESSEL_SETTLE_MIN_S:
            violations.append(
                f"C3 违例：静置 {float(settle):.3f}s < "
                f"{prompts.VESSEL_SETTLE_MIN_S}s")
    tilt = witnesses.get("tilt_duration_s")
    if tilt is not None:
        checked += 1
        if not _is_finite_number(tilt):
            violations.append(f"C3 违例：tilt_duration_s 非有限数：{tilt!r}")
        elif float(tilt) < prompts.VESSEL_TILT_MIN_S:
            violations.append(
                f"C3 违例：θ=0→≥120° 历时 {float(tilt):.3f}s < "
                f"{prompts.VESSEL_TILT_MIN_S}s")
    omega = witnesses.get("ang_speed_peak_dps")
    if omega is not None:
        checked += 1
        if not _is_finite_number(omega):
            violations.append(f"C3 违例：ang_speed_peak_dps 非有限数：{omega!r}")
        elif float(omega) > prompts.VESSEL_TILT_ANG_SPEED_MAX_DPS:
            violations.append(
                f"C3 违例：角速度峰值 {float(omega):.2f}°/s > "
                f"{prompts.VESSEL_TILT_ANG_SPEED_MAX_DPS}°/s")
    if checked == 3:
        if _SMOOTH_EASE_RE.search(ev.script_text or "") is None:
            violations.append("C3 违例：脚本未见 smoothstep 类平滑缓动"
                              "（逐帧大跳/瞬移）")
        return violations

    # 降级到静态常量（弱判据：只能证明设计值，不能证明实跑）
    tree, errs = _parse(ev)
    if tree is None:
        return [f"C3 不可判：缺运行期见证，且{errs[0]}"]
    consts = _numeric_constants(tree)
    static_hits = 0
    for name, value in consts.items():
        if _SETTLE_NAME_RE.search(name) and value >= prompts.VESSEL_SETTLE_MIN_S:
            static_hits += 1
        if _TILT_DUR_NAME_RE.search(name) and value >= prompts.VESSEL_TILT_MIN_S:
            static_hits += 1
        if _ANG_SPEED_NAME_RE.search(name) and 0 < value \
                <= prompts.VESSEL_TILT_ANG_SPEED_MAX_DPS:
            static_hits += 1
    if static_hits == 0:
        violations.append(
            "C3 不可判：缺运行期见证（settle_s/tilt_duration_s/"
            "ang_speed_peak_dps），脚本里也找不到可识别的时长/角速常量")
    else:
        violations.append(
            f"C3 不可判：仅有脚本常量设计值（命中 {static_hits} 项），"
            "实跑时长/角速峰值无见证")
    return violations


def _numeric_constants(tree: ast.AST) -> "dict[str, float]":
    """脚本里 ``NAME = <数值>`` 形式的常量表（含模块级与函数内）。"""
    out: dict[str, float] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) \
                and _is_finite_number(node.value.value):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    out.setdefault(target.id, float(node.value.value))
        if isinstance(node, ast.AnnAssign) and isinstance(node.value, ast.Constant) \
                and _is_finite_number(node.value.value) \
                and isinstance(node.target, ast.Name):
            out.setdefault(node.target.id, float(node.value.value))
    return out


# ---------------------------------------------------------------------------
# C4：v1 运动学不等式（需要运行期见证）
# ---------------------------------------------------------------------------

def check_c4_v1_inequalities(ev: ContractEvidence) -> list[str]:
    """C4：真倾倒/对杯口/不穿模三条不等式 + 50Hz 采样（数值见证为准）。"""
    witnesses = ev.witnesses or {}
    keys = ("theta_max_deg", "outflow_window_s", "rho_max_m",
            "spout_z_min_m", "spout_z_max_m", "clearance_min_m", "sample_hz")
    missing = [k for k in keys if witnesses.get(k) is None]
    if missing:
        return [f"C4 不可判：缺运行期见证 {missing}（v1 不等式无法逐条核验）"]
    v: list[str] = []
    nonfinite = [k for k in keys if not _is_finite_number(witnesses.get(k))]
    if nonfinite:
        return [f"C4 违例：见证非有限数 {nonfinite}（测量代码缺陷）"]

    if float(witnesses["theta_max_deg"]) < THETA_MAX_MIN_DEG:
        v.append(f"C4 违例：θ_max {float(witnesses['theta_max_deg']):.1f}° < "
                 f"{THETA_MAX_MIN_DEG}°（未真倾倒）")
    if float(witnesses["outflow_window_s"]) < OUTFLOW_WINDOW_MIN_S:
        v.append(f"C4 违例：流出停留窗 "
                 f"{float(witnesses['outflow_window_s']):.2f}s < "
                 f"{OUTFLOW_WINDOW_MIN_S}s")
    if float(witnesses["rho_max_m"]) > LIP_RADIUS_MAX_M:
        v.append(f"C4 违例：壶嘴水平投影 ρ_max "
                 f"{float(witnesses['rho_max_m']):.4f}m > {LIP_RADIUS_MAX_M}m"
                 "（水流落不进杯口）")
    if float(witnesses["spout_z_min_m"]) < SPOUT_Z_LO_M:
        v.append(f"C4 违例：壶嘴最低 z {float(witnesses['spout_z_min_m']):.4f}m "
                 f"< {SPOUT_Z_LO_M}m（探进杯口平面以下）")
    if float(witnesses["spout_z_max_m"]) > SPOUT_Z_HI_M:
        v.append(f"C4 违例：壶嘴最高 z {float(witnesses['spout_z_max_m']):.4f}m "
                 f"> {SPOUT_Z_HI_M}m（水柱过长易飞溅）")
    clear = float(witnesses["clearance_min_m"])
    if clear < VESSEL_CUP_CLEARANCE_MIN_M:
        v.append(f"C4 违例：壶-杯最小解析间隙 {clear:.4f}m < "
                 f"{VESSEL_CUP_CLEARANCE_MIN_M}m（穿模风险）")
    if float(witnesses["sample_hz"]) < PRECHECK_SAMPLE_HZ_MIN:
        v.append(f"C4 违例：自检采样 {float(witnesses['sample_hz']):.1f}Hz < "
                 f"{PRECHECK_SAMPLE_HZ_MIN:g}Hz")
    return v


# ---------------------------------------------------------------------------
# C5：双自检（静态顺序判据）
# ---------------------------------------------------------------------------

def check_c5_dual_precheck(ev: ContractEvidence) -> list[str]:
    """C5：``kinematic_precheck`` 在 gs.init/scene.build 之前，
    ``sim_blowout`` 在仿真之后；两者都得是脚本里的**调用**而非仅定义。"""
    tree, errs = _parse(ev)
    if tree is None:
        return [f"C5 不可判：{errs[0]}"]
    calls = _calls(tree)
    by_name: dict[str, list[int]] = {}
    for lineno, name, _call in calls:
        by_name.setdefault(name.rsplit(".", 1)[-1], []).append(lineno)

    violations: list[str] = []
    pre = by_name.get("kinematic_precheck", [])
    blow = by_name.get("sim_blowout", [])
    init = by_name.get("init", [])          # gs.init(...)
    build = by_name.get("build", [])        # scene.build(...)
    step = by_name.get("step", [])          # scene.step(...)
    if not pre:
        violations.append("C5 违例：脚本没有调用 kinematic_precheck()")
    if not blow:
        violations.append("C5 违例：脚本没有调用 sim_blowout()")
    if pre and (init or build):
        first_pre = min(pre)
        first_field = min((init + build))
        # ``def kinematic_precheck`` 的 lineno 也进 pre；调用点之后才有意义。
        if first_pre > first_field:
            violations.append(
                "C5 违例：kinematic_precheck 调用晚于 gs.init/scene.build"
                "（自检必须在建场之前）")
    if blow and step:
        if max(blow) < max(step):
            violations.append(
                "C5 违例：sim_blowout 调用早于仿真步进（护栏必须读跑后轨迹）")
    if blow and not step:
        violations.append("C5 不可判：脚本找不到 scene.step（仿真步进）")
    return violations


# ---------------------------------------------------------------------------
# C6：边流形
# ---------------------------------------------------------------------------

def check_c6_edge_manifold(ev: ContractEvidence) -> list[str]:
    """C6：每条无向边恰被 2 个三角形引用；边界边/非流形边计数必须为 0。"""
    mesh, reason = _load_mesh(ev)
    if mesh is None:
        return [f"C6 不可判：{reason}"]
    violations: list[str] = []
    faces = getattr(mesh, "faces", None)
    if faces is None or len(faces) == 0:
        return ["C6 违例：网格三角面数为 0"]
    counts: dict[tuple[int, int], int] = {}
    for tri in faces:
        a, b, c = (int(tri[0]), int(tri[1]), int(tri[2]))
        for u, w in ((a, b), (b, c), (c, a)):
            key = (u, w) if u < w else (w, u)
            counts[key] = counts.get(key, 0) + 1
    boundary = sum(1 for n in counts.values() if n == 1)
    nonmanifold = sum(1 for n in counts.values() if n >= 3)
    if boundary:
        violations.append(f"C6 违例：边界边 {boundary} 条（每条无向边须恰 2 面）")
    if nonmanifold:
        violations.append(
            f"C6 违例：非流形边 {nonmanifold} 条（被 ≥3 个三角形引用）")
    if not bool(getattr(mesh, "is_watertight", False)):
        violations.append("C6 违例：mesh.is_watertight=False")

    witnesses = ev.witnesses or {}
    claimed = {
        "boundary_edges": boundary,
        "nonmanifold_edges": nonmanifold,
        "mesh_faces": int(len(faces)),
    }
    for key, measured in claimed.items():
        if key not in witnesses:
            violations.append(f"C6 不可判：见证缺 {key}（计数必须登记）")
            continue
        value = witnesses.get(key)
        if not _is_finite_number(value) or int(value) != measured:
            violations.append(
                f"C6 违例：见证 {key}={value!r} 与实测 {measured} 不符"
                "（见证不得自述）")
    if "is_watertight" in witnesses and \
            bool(witnesses["is_watertight"]) != bool(
                getattr(mesh, "is_watertight", False)):
        violations.append("C6 违例：见证 is_watertight 与实测不符")
    return violations


# ---------------------------------------------------------------------------
# C7：见证值禁哨兵 + 角速度峰值判
# ---------------------------------------------------------------------------

#: 几何/时间见证键 → 语义下界（None = 允许任意有限值）。负值是物理上
#: 无意义的量（距离/角度/时长/计数），出现 -1 即哨兵占位。
_WITNESS_KEYS: dict[str, str] = {
    "theta_max_deg": "非负角度",
    "outflow_window_s": "非负时长",
    "rho_max_m": "非负距离",
    "spout_z_min_m": "高度",
    "spout_z_max_m": "高度",
    "clearance_min_m": "有符号间隙",
    "lip_drift_m": "非负距离",
    "lip_speed_max_mps": "非负速度",
    "settle_s": "非负时长",
    "tilt_duration_s": "非负时长",
    "ang_speed_peak_dps": "非负角速度",
    "sample_hz": "非负频率",
    "mesh_faces": "非负计数",
    "boundary_edges": "非负计数",
    "nonmanifold_edges": "非负计数",
    "blowout_frac": "比例",
}
_SENTINEL_VALUES = (-1.0,)


def check_c7_no_sentinel_witness(ev: ContractEvidence) -> list[str]:
    """C7：见证一律真实有限量；角速度必须按逐帧数值导数**峰值**上报。"""
    witnesses = ev.witnesses
    if witnesses is None:
        return ["C7 不可判：缺运行期见证（无见证即无哨兵可查）"]
    violations: list[str] = []
    for key, semantics in _WITNESS_KEYS.items():
        if key not in witnesses:
            continue
        value = witnesses[key]
        if value is None:
            violations.append(
                f"C7 违例：见证 {key}=None（{semantics}不得用 None 占位）")
            continue
        if not _is_finite_number(value):
            violations.append(
                f"C7 违例：见证 {key}={value!r} 非有限数（NaN/inf 即测量缺陷）")
            continue
        if key != "clearance_min_m" and float(value) in _SENTINEL_VALUES:
            violations.append(
                f"C7 违例：见证 {key}={float(value):g} 是哨兵占位"
                f"（{semantics}必须返回真实量）")
    missing = [k for k in _WITNESS_KEYS if k not in witnesses]
    if missing:
        violations.append(f"C7 不可判：见证缺键 {missing}")

    mean_key = "ang_speed_mean_dps"
    if mean_key in witnesses and "ang_speed_peak_dps" not in witnesses:
        violations.append(
            "C7 违例：角速度只上报均值（合同要求逐帧数值导数峰值）")
    series = witnesses.get("ang_speed_series_dps")
    peak = witnesses.get("ang_speed_peak_dps")
    if isinstance(series, (list, tuple)) and series:
        if not all(_is_finite_number(x) for x in series):
            violations.append("C7 违例：角速度序列含非有限数")
        elif _is_finite_number(peak):
            measured = max(abs(float(x)) for x in series)
            if abs(float(peak) - measured) > 1e-6:
                violations.append(
                    f"C7 违例：上报峰值 {float(peak):.6f}°/s 与逐帧序列最大"
                    f" {measured:.6f}°/s 不符（峰值必须由序列数值导数给出）")
        elif peak is not None:
            violations.append(f"C7 违例：ang_speed_peak_dps 非有限数：{peak!r}")
    return violations


# ---------------------------------------------------------------------------
# 注册表（AC-T3-13 的机判锚点：缺一条即红）
# ---------------------------------------------------------------------------

CONTRACT_CHECKS: dict[str, Callable[[ContractEvidence], list[str]]] = {
    "C1": check_c1_single_hollow_body,
    "C2": check_c2_kinematic_hinge,
    "C3": check_c3_slow_dynamics,
    "C4": check_c4_v1_inequalities,
    "C5": check_c5_dual_precheck,
    "C6": check_c6_edge_manifold,
    "C7": check_c7_no_sentinel_witness,
}


def registry_errors() -> list[str]:
    """注册表自检：合同号必须与 ``EXPECTED_CONTRACTS`` 逐条相等且可调用。"""
    errs: list[str] = []
    missing = sorted(EXPECTED_CONTRACTS - set(CONTRACT_CHECKS))
    extra = sorted(set(CONTRACT_CHECKS) - EXPECTED_CONTRACTS)
    if missing:
        errs.append(f"Gate 2 判据缺合同：{missing}（缺一条即红）")
    if extra:
        errs.append(f"Gate 2 判据有未登记合同号：{extra}")
    for cid, fn in CONTRACT_CHECKS.items():
        if not callable(fn):
            errs.append(f"{cid} 的判据不是可调用对象：{fn!r}")
        elif not (getattr(fn, "__doc__", "") or "").strip():
            errs.append(f"{cid} 的判据缺说明（不可定位）")
        elif cid not in CONTRACT_TITLES:
            errs.append(f"{cid} 缺语义索引（CONTRACT_TITLES）")
    return errs


def evaluate_contracts(ev: ContractEvidence) -> dict:
    """逐条跑 C1–C7，聚合三态报告（可直接落盘 / 进 Gate 2 卡）。

    判定口径（fail-closed，**不可判 ≠ 通过**）：空违例 + 至少一条证据被真正
    读取 ⇒ pass；有 ``不可判`` 字样且无实判违例 ⇒ undecidable；其余 ⇒
    violation。一个检查同时给出实判违例与不可判项时按 violation 计（实判
    优先）。
    """
    contracts: dict[str, dict[str, Any]] = {}
    counts = {PASS: 0, VIOLATION: 0, UNDECIDABLE: 0}
    for cid in sorted(EXPECTED_CONTRACTS):
        fn = CONTRACT_CHECKS.get(cid)
        if fn is None:
            contracts[cid] = {
                "title": CONTRACT_TITLES.get(cid, ""),
                "status": VIOLATION,
                "findings": ["判据函数缺失（注册表不完整，缺一条即红）"],
            }
            counts[VIOLATION] += 1
            continue
        findings = list(fn(ev))
        hard = [f for f in findings if "不可判" not in f]
        undecidable = [f for f in findings if "不可判" in f]
        if hard:
            status = VIOLATION
        elif undecidable:
            status = UNDECIDABLE
        else:
            status = PASS
        counts[status] += 1
        contracts[cid] = {
            "title": CONTRACT_TITLES[cid],
            "status": status,
            "findings": hard + undecidable,
        }
    return {
        "schema": CONTRACTS_SCHEMA,
        "expected": sorted(EXPECTED_CONTRACTS),
        "contracts_checked": len(CONTRACT_CHECKS),
        "registry_errors": registry_errors(),
        "contracts": contracts,
        "counts": counts,
        "all_pass": counts[VIOLATION] == 0 and counts[UNDECIDABLE] == 0,
    }


def summarize(report: dict) -> str:
    """一行摘要（Gate 2 卡 / 日志用）。"""
    counts = report.get("counts") or {}
    return (f"C1–C7 构造性合同：通过 {counts.get(PASS, 0)}/"
            f"{len(report.get('expected') or [])}，违例 "
            f"{counts.get(VIOLATION, 0)}，不可判 "
            f"{counts.get(UNDECIDABLE, 0)}")


def collect_evidence(workdir: "str | Path", *, script_name: str,
                     witnesses: "dict[str, Any] | None" = None
                     ) -> ContractEvidence:
    """从产物目录装配证据（只读；缺件如实留 None，由判据转「不可判」）。"""
    wd = Path(workdir)
    script_path = wd / script_name
    script_text = None
    if script_path.is_file():
        try:
            script_text = script_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            script_text = None
    mesh_path = _discover_mesh(wd, script_text or "")
    return ContractEvidence(script_text=script_text, mesh_path=mesh_path,
                            witnesses=witnesses, workdir=wd)


# ---------------------------------------------------------------------------
# 归档块的可定位性（BR-7：归档件保留，内环 prompt 不含原文）
# ---------------------------------------------------------------------------

def archived_contract_blocks() -> "dict[str, str]":
    """构造性合同的两个归档块（仍在 ``task8_fluid_prompts``，可追溯）。"""
    return {
        "kinematic": prompts._kinematic_contract_block(),
        "vessel_structure": prompts._vessel_structure_contract_block(),
    }


def prompt_leaks(text: str) -> "list[str]":
    """返回 ``text`` 里**逐字**出现的归档合同块名（空 = 内环 prompt 干净）。"""
    leaks: list[str] = []
    for name, block in archived_contract_blocks().items():
        if block and block in text:
            leaks.append(name)
    return leaks
