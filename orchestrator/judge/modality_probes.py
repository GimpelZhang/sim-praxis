#!/usr/bin/env python3
"""Task4 三类新度量探针（详细计划 L465-468）：流体守恒 / 软体网格完整性 /
触觉摩擦锥。

输入纪律（计划 L465）：**输入是 numpy 度量数组，不是假视频帧**——这些是
度量级仪器探针，消费仿真器/遥测产出的标量场时间序列；严禁用 numpy 造像素
帧冒充视频后再由本探针判定。

通用拒收口径（三类探针一致，违例直接抛 ValueError，不返回 pass=False）：
- 非 numpy.ndarray（含 list/tuple）拒收；
- bool（np.bool_ dtype，含 True/False 标量）不是数值度量，拒收；
- NaN/Inf 拒收（math.isfinite 口径，同 video.py S-3）；
- 形状/维度不符、长度不等、索引越界拒收。
物理不可能但形状合法的有限值（负法向力、账目不平、四面体反转等）计入
violations 并 pass=False——这是探针要检出的缺陷，不是坏输入。
返回对齐 gait_rungs：{"pass": bool, "checks": {名: bool}, "violations": [str]}。
阈值集中于本文件命名常量，禁止在判定逻辑里散落魔数。
"""
from __future__ import annotations

from typing import Any

import numpy as np

# ---------------- 流体（plan L466：粒子守恒 / 边界溢出率 / 入杯率） ---------
PARTICLE_STEP_LOSS_TOL = 0.02      # 相邻帧总粒子数凭空消失的相对容差
MAX_BOUNDARY_SPILL_FRAC = 0.02     # 末帧越界粒子占比上限
MIN_FINAL_CUP_CAPTURE_FRAC = 0.80  # 末帧入杯率下限
PARTICLE_ACCOUNT_TOL = 1e-6        # boundary+cup ≤ counts 的账目容差

# ---------------- 软体四面体网格（plan L467） ------------------------------
MAX_TET_INVERSION_RATE = 0.0       # signed volume 变号率：任何反转即拒
MAX_TET_DEGENERATE_RATE = 0.01     # 退化（近零体积）率上限
DEGEN_VOLUME_REL = 1e-9            # |vol_t| ≤ rel·|vol_0| 记退化
TET_REF_VOL_EPS = 1e-12            # 参考帧（t=0）体积必须显著非零
ENERGY_SPIKE_REL = 5.0             # e_t 超 e_{t-1} 该倍数记能量突跳
ENERGY_ABS_FLOOR = 1e-12

# ---------------- 触觉摩擦锥（plan L468） -----------------------------------
MAX_CONE_VIOLATION_RATE = 0.0      # 逐单元越界率：任何越界即拒
CONE_SLACK = 1e-9                  # |shear| ≤ μ·N 的数值容差
NORMAL_NEG_TOL = 1e-9              # 法向力非负容差
FORCE_JUMP_REL = 10.0              # 单元合力逐帧突跳倍数
FORCE_JUMP_ABS_FLOOR = 1e-9


def _metric_array(name: str, a: Any, *, ndim: int) -> np.ndarray:
    """通用度量数组校验：ndarray、非 bool、有限数值、指定维度。"""
    if isinstance(a, bool) or not isinstance(a, np.ndarray):
        raise ValueError(f"{name} 必须是 numpy.ndarray（收到 {type(a).__name__}）")
    if a.dtype == np.bool_ or a.dtype.kind == "b":
        raise ValueError(f"{name} 不接受 bool 数组（bool 不是数值度量）")
    if a.dtype.kind not in ("f", "i", "u"):
        raise ValueError(f"{name} 必须是数值数组（dtype={a.dtype}）")
    if a.ndim != ndim:
        raise ValueError(f"{name} 必须是 {ndim} 维数组，收到 shape={a.shape}")
    if a.size == 0:
        raise ValueError(f"{name} 不能为空数组")
    out = np.asarray(a, dtype=np.float64)
    if not np.all(np.isfinite(out)):
        raise ValueError(f"{name} 含 NaN/Inf，拒收")
    return out


def fluid_conservation(particle_counts_t: np.ndarray,
                       boundary_counts_t: np.ndarray,
                       cup_indicator_t: np.ndarray) -> dict:
    """流体粒子守恒探针（一维时间序列，长度 T≥2）。

    语义：
    - counts：全场粒子总数；boundary：越过刚体边界（溢出）粒子数；
      cup：已入杯粒子数。
    - 守恒：发射器出生（counts 上升）允许；相邻帧 counts 净下降超过
      PARTICLE_STEP_LOSS_TOL 即凭空消失（入杯/越界粒子仍计入 counts）。
    - 账目：boundary_t ≤ counts_t、cup_t ≤ counts_t、boundary+cup ≤ counts。
    - 末帧边界溢出率 ≤ MAX_BOUNDARY_SPILL_FRAC；末帧入杯率
      ≥ MIN_FINAL_CUP_CAPTURE_FRAC。
    """
    counts = _metric_array("particle_counts_t", particle_counts_t, ndim=1)
    boundary = _metric_array("boundary_counts_t", boundary_counts_t, ndim=1)
    cup = _metric_array("cup_indicator_t", cup_indicator_t, ndim=1)
    if not (counts.shape == boundary.shape == cup.shape):
        raise ValueError(
            f"三条序列必须等长：{counts.shape}/{boundary.shape}/{cup.shape}")
    if counts.size < 2:
        raise ValueError("流体序列至少需要 2 个时间步")
    violations: list[str] = []

    neg = [(t, float(counts[t])) for t in range(counts.size) if counts[t] < 0]
    if neg:
        violations.append(f"粒子总数出现负值：{neg[:3]}")

    # 账目守恒（逐时刻）
    bad_acct = [
        t for t in range(counts.size)
        if boundary[t] < 0 or cup[t] < 0
        or boundary[t] > counts[t] + PARTICLE_ACCOUNT_TOL
        or cup[t] > counts[t] + PARTICLE_ACCOUNT_TOL
        or boundary[t] + cup[t] > counts[t] + PARTICLE_ACCOUNT_TOL
    ]
    if bad_acct:
        violations.append(
            f"粒子账目不平（boundary/cup 超出总数）的时间步：{bad_acct[:5]}")

    # 凭空消失（出生允许；入杯/越界不减少 counts）
    losses = []
    for t in range(1, counts.size):
        if counts[t - 1] > 0:
            drop = (counts[t - 1] - counts[t]) / counts[t - 1]
            if drop > PARTICLE_STEP_LOSS_TOL:
                losses.append((t, float(drop)))
    if losses:
        violations.append(
            f"粒子相邻帧凭空消失超容差（>{PARTICLE_STEP_LOSS_TOL:g}）：{losses[:3]}")

    final_total = counts[-1]
    spill = float(boundary[-1] / final_total) if final_total > 0 else 0.0
    capture = float(cup[-1] / final_total) if final_total > 0 else 0.0
    spill_ok = spill <= MAX_BOUNDARY_SPILL_FRAC
    capture_ok = capture >= MIN_FINAL_CUP_CAPTURE_FRAC
    if not spill_ok:
        violations.append(
            f"末帧边界溢出率 {spill:.3f} > {MAX_BOUNDARY_SPILL_FRAC:g}")
    if not capture_ok:
        violations.append(
            f"末帧入杯率 {capture:.3f} < {MIN_FINAL_CUP_CAPTURE_FRAC:g}")
    checks = {
        "accounts_balanced": not bad_acct and not neg,
        "no_unexplained_loss": not losses,
        "boundary_spill_within_limit": spill_ok,
        "cup_capture_above_floor": capture_ok,
    }
    return {"pass": not violations, "checks": checks,
            "violations": violations,
            "metrics": {"final_spill_frac": spill,
                        "final_capture_frac": capture}}


def _signed_volumes(vertices_t: np.ndarray, tet: np.ndarray) -> np.ndarray:
    # (T,K)：每个四面体每时刻的有向体积 = det(e1,e2,e3)/6
    pts = vertices_t[:, tet, :]                    # (T,K,4,3)
    e1 = pts[:, :, 1] - pts[:, :, 0]
    e2 = pts[:, :, 2] - pts[:, :, 0]
    e3 = pts[:, :, 3] - pts[:, :, 0]
    return np.einsum("tkj,tkj->tk",
                     np.cross(e1, e2, axis=2), e3) / 6.0


def soft_mesh_integrity(tet_indices: np.ndarray,
                        vertices_t: np.ndarray,
                        energy_t: np.ndarray) -> dict:
    """软体四面体网格完整性探针。

    - tet_indices：(K,4) 整数顶点索引；vertices_t：(T,V,3) 顶点坐标时间序列
      （T≥2）；energy_t：(T,) 弹性能量（应非负、无突跳）。
    - 反转：相对 t=0 参考有向体积变号（任何变号计违规，率上限 0）。
    - 退化：|vol_t| ≤ DEGEN_VOLUME_REL·|vol_0| 的比例超上限。
    - 能量：非负有限；相邻帧增量 > ENERGY_SPIKE_REL 倍记突跳。
    """
    tet = tet_indices
    if isinstance(tet, bool) or not isinstance(tet, np.ndarray):
        raise ValueError(
            f"tet_indices 必须是 numpy.ndarray（收到 {type(tet).__name__}）")
    if tet.dtype == np.bool_ or tet.dtype.kind == "b":
        raise ValueError("tet_indices 不接受 bool 数组")
    if tet.dtype.kind not in ("i", "u"):
        raise ValueError(f"tet_indices 必须是整数索引数组（dtype={tet.dtype}）")
    if tet.ndim != 2 or tet.shape[1] != 4:
        raise ValueError(f"tet_indices 必须形状 (K,4)，收到 {tet.shape}")
    verts = _metric_array("vertices_t", vertices_t, ndim=3)
    if verts.shape[2] != 3:
        raise ValueError(f"vertices_t 末维必须为 3，收到 {verts.shape}")
    energy = _metric_array("energy_t", energy_t, ndim=1)
    if energy.shape[0] != verts.shape[0]:
        raise ValueError(
            f"energy_t 长度必须等于 T：{energy.shape[0]} != {verts.shape[0]}")
    if verts.shape[0] < 2:
        raise ValueError("vertices_t 至少需要 2 个时间步")
    if tet.size == 0:
        raise ValueError("tet_indices 至少需要 1 个四面体")
    if int(tet.min()) < 0 or int(tet.max()) >= verts.shape[1]:
        raise ValueError(
            f"tet_indices 越界：顶点数 V={verts.shape[1]}，"
            f"索引范围 [{int(tet.min())}, {int(tet.max())}]")
    if any(len(set(int(v) for v in row)) < 4 for row in tet):
        raise ValueError("tet_indices 存在重复顶点索引（非四面体）")

    violations: list[str] = []
    neg_e = [(t, float(energy[t])) for t in range(energy.size)
             if energy[t] < -ENERGY_ABS_FLOOR]
    if neg_e:
        violations.append(f"弹性能量为负：{neg_e[:3]}")

    vol = _signed_volumes(verts, tet)                  # (T,K)
    ref = vol[0]                                       # (K,)
    if np.any(np.abs(ref) <= TET_REF_VOL_EPS):
        raise ValueError(
            "参考帧 t=0 存在近零体积四面体，无法判定反转/退化（夹具非法）")
    later = vol[1:]
    sign_flip = (np.sign(later) != np.sign(ref)[None, :])
    n_later = later.size
    inv_rate = float(sign_flip.sum() / n_later)
    degen = (np.abs(later)
             <= DEGEN_VOLUME_REL * np.abs(ref)[None, :])
    degen_rate = float(degen.sum() / n_later)
    if inv_rate > MAX_TET_INVERSION_RATE:
        idx = np.argwhere(sign_flip)[:3]
        violations.append(
            f"四面体有向体积变号率 {inv_rate:.4f} > "
            f"{MAX_TET_INVERSION_RATE:g}（首组 t,k={idx.tolist()}）")
    if degen_rate > MAX_TET_DEGENERATE_RATE:
        violations.append(
            f"四面体退化率 {degen_rate:.4f} > {MAX_TET_DEGENERATE_RATE:g}")

    spikes = []
    for t in range(1, energy.size):
        base = max(abs(float(energy[t - 1])), ENERGY_ABS_FLOOR)
        if energy[t] - energy[t - 1] > ENERGY_SPIKE_REL * base:
            spikes.append((t, float(energy[t - 1]), float(energy[t])))
    if spikes:
        violations.append(
            f"弹性能量突跳（>{ENERGY_SPIKE_REL:g} 倍）：{spikes[:3]}")

    checks = {
        "no_tet_inversion": inv_rate <= MAX_TET_INVERSION_RATE,
        "degenerate_rate_within_limit": degen_rate <= MAX_TET_DEGENERATE_RATE,
        "energy_nonnegative": not neg_e,
        "no_energy_spike": not spikes,
    }
    return {"pass": not violations, "checks": checks,
            "violations": violations,
            "metrics": {"inversion_rate": inv_rate,
                        "degenerate_rate": degen_rate}}


def tactile_friction_cone(normal_t: np.ndarray,
                          shear_xy_t: np.ndarray,
                          mu_array: np.ndarray) -> dict:
    """逐触觉单元摩擦锥探针。

    - normal_t：(T,N) 法向力（应 ≥0）；shear_xy_t：(T,N,2) 切向力二维；
      mu_array：(N,) 摩擦系数（必须 >0）；T≥2 才能查力值突跳。
    - 锥约束：逐 (t,n) 要求 |shear_xy| ≤ μ_n·N + CONE_SLACK，越界率必须为 0。
    - 法向力非负；单元合力 F=sqrt(N²+|shear|²) 相邻帧增量超 FORCE_JUMP_REL
      倍记突跳。
    """
    normal = _metric_array("normal_t", normal_t, ndim=2)
    shear = _metric_array("shear_xy_t", shear_xy_t, ndim=3)
    mu = _metric_array("mu_array", mu_array, ndim=1)
    if shear.shape[2] != 2:
        raise ValueError(f"shear_xy_t 末维必须为 2，收到 {shear.shape}")
    t_n, n_n = normal.shape
    if shear.shape[:2] != (t_n, n_n):
        raise ValueError(
            f"normal_t {normal.shape} 与 shear_xy_t {shear.shape} 的 T,N 不一致")
    if mu.shape[0] != n_n:
        raise ValueError(
            f"mu_array 长度必须等于 N={n_n}，收到 {mu.shape[0]}")
    if t_n < 2:
        raise ValueError("触觉序列至少需要 2 个时间步")
    if np.any(mu <= 0):
        raise ValueError("mu_array 必须全部为正（μ≤0 的单元无摩擦锥定义）")

    violations: list[str] = []
    neg = np.argwhere(normal < -NORMAL_NEG_TOL)
    if neg.size:
        violations.append(
            f"法向力为负的单元（首组 t,n）：{neg[:3].tolist()}")

    shear_mag = np.hypot(shear[:, :, 0], shear[:, :, 1])   # (T,N)
    cone_limit = mu[None, :] * np.maximum(normal, 0.0) + CONE_SLACK
    outside = shear_mag > cone_limit
    rate = float(outside.sum() / outside.size)
    if rate > MAX_CONE_VIOLATION_RATE:
        idx = np.argwhere(outside)[:3]
        t_n0, n_n0 = idx[0].tolist()
        violations.append(
            f"摩擦锥越界率 {rate:.4f} > {MAX_CONE_VIOLATION_RATE:g}"
            f"（首组 t,n={idx.tolist()}，|shear|={float(shear_mag[t_n0, n_n0]):.3f}"
            f" > μN={float(cone_limit[t_n0, n_n0]):.3f}）")

    force = np.hypot(normal, shear_mag)
    jumps = []
    for t in range(1, t_n):
        for n in range(n_n):
            base = max(float(force[t - 1, n]), FORCE_JUMP_ABS_FLOOR)
            if force[t, n] - force[t - 1, n] > FORCE_JUMP_REL * base:
                jumps.append((t, n, float(force[t - 1, n]),
                              float(force[t, n])))
                if len(jumps) >= 3:
                    break
        if len(jumps) >= 3:
            break
    if jumps:
        violations.append(
            f"触觉合力突跳（>{FORCE_JUMP_REL:g} 倍，首组 t,n,F0,F1）：{jumps}")

    checks = {
        "normal_nonnegative": neg.size == 0,
        "friction_cone_respected": rate <= MAX_CONE_VIOLATION_RATE,
        "no_force_jump": not jumps,
    }
    return {"pass": not violations, "checks": checks,
            "violations": violations,
            "metrics": {"cone_violation_rate": rate}}
