#!/usr/bin/env python3
"""Task 5 墙 B 冻结评分器（B-eng 工程闸；首呼前冻结，模型不见本件源码）。

在一次性兄弟评分容器内运行，导入模型自著 ``agent_solution.py``，独立核验
「测量仪器合格」（Phase 2 run_5 合同 v2/C6/C7 的工程部分）：

  M1 程序化著器：单一刚体、水密、开口中空壶（闭合 r/z 剖面旋转类工艺），
     导出 OBJ 后重载复检闭合边；禁止复制冻结历史见证件；
  M2 边流形计数仪：无向边被引用恰两次为内边、一次为边界边、≥3 为非流形；
     三冻结拓扑向量（768 散三角/2304 边界、三面扇形共享边、1536 历史件）；
  K1..K4 运动学见证仪：冻结合成轨迹上独立重算 11 个见证量（rel/abs
     1e-9），合同违例名逐字正确（合同 1/2/3/6）；
  K5/K6 非有限/退化输入必须抛异常，禁止哨兵占位（-1/-999 字面量 AST 扫）；
  S1 源码反作弊：禁止面向测试的硬编码（768/2304/1536 字面量、读取冻结
     见证件路径）。

B-cap（speed≤0.20、clearance≥0.012 等）只在 ``b_cap`` 段描述性记录，
不参与本评分器 rc（独立结论轴，见 VERDICT_CRITERIA.md）。

用法：``python wall_b_checker.py <solution.py> <frozen_obj> <workdir>
<score_out_dir>``；rc 0=B-eng 过 / 1=未过；JSON 写 stdout 与
``<score_out_dir>/wall_b-<ts>.json``。
"""
import ast
import hashlib
import importlib.util
import json
import math
import sys
import time
import traceback
from pathlib import Path

import numpy as np
import trimesh

SOLUTION_PATH = Path(sys.argv[1])
FROZEN_OBJ = Path(sys.argv[2])     # 评分容器内冻结历史见证件（模型容器不挂载）
WORKDIR = Path(sys.argv[3])
SCORE_DIR = Path(sys.argv[4])
SCORE_DIR.mkdir(parents=True, exist_ok=True)

verdict = {"schema": "simpraxis.phase25.wall_b_score.v1",
           "checks": [], "b_cap": None}


def check(name, ok, **kw):
    verdict["checks"].append({"check": name, "ok": bool(ok), **kw})
    print(f"[wall_b] {name}: {'PASS' if ok else 'FAIL'} {kw}", flush=True)
    return bool(ok)


def load_solution():
    spec = importlib.util.spec_from_file_location("agent_solution", SOLUTION_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---- 冻结拓扑向量（确定性；签名钉死历史） ----------------------------------
def open_shell_soup():
    """768 两两不交三角形 → 2304 条边各引用一次（C10 开口壳历史签名）。"""
    rng = np.random.RandomState(20261028)
    verts, faces = [], []
    for _ in range(768):
        base = len(verts)
        tri = rng.normal(size=(3, 3)) * 0.01 + np.array([base * 1e-5, 0, 0])
        verts.extend(tri.tolist())
        faces.append((base, base + 1, base + 2))
    return np.asarray(verts), np.asarray(faces, dtype=np.int64)


def nonmanifold_fan():
    """三面共享边 {0,1}：非流形计数 ≥1。"""
    verts = [(0, 0, 0), (1, 0, 0), (0, 1, 0), (0, 0, 1)]
    faces = [(0, 1, 2), (1, 0, 3), (0, 1, 3)]
    return np.asarray(verts, dtype=np.float64), np.asarray(faces, dtype=np.int64)


# ---- 冻结合成轨迹 -----------------------------------------------------------
FLOW_START, FLOW_END = 3.3, 3.9


def _smoothstep(x):
    x = np.clip(x, 0.0, 1.0)
    return x * x * (3.0 - 2.0 * x)


def _base_motion(tilt_s=2.6):
    t = np.arange(0.0, 4.6 + 1e-9, 0.02)
    th = np.zeros_like(t)
    m = (t >= 0.6) & (t <= 0.6 + tilt_s)
    th[m] = np.deg2rad(130.0) * _smoothstep((t[m] - 0.6) / tilt_s)
    th[t > 0.6 + tilt_s] = np.deg2rad(130.0)
    return t, th


def traj_good():
    t, th = _base_motion()
    p = np.zeros((len(t), 3))
    p[:, 2] = 1.045
    cl = np.full_like(t, 0.05)
    return t, th, p, cl


def traj_static():
    t, _ = _base_motion()
    th = np.zeros_like(t)
    p = np.zeros((len(t), 3)); p[:, 2] = 1.045
    cl = np.full_like(t, 0.05)
    return t, th, p, cl


def traj_too_fast():
    """保流窗内 ±3mm 往复：speed 0.30 m/s 但 drift/rho 不超——隔离合同 6。"""
    t, th, p, cl = traj_good()
    p = p.copy()
    inside = (t >= FLOW_START) & (t <= FLOW_END)
    idx = np.where(inside)[0]
    p[idx, 0] = 0.003 * np.where(np.arange(len(idx)) % 2 == 0, 1.0, -1.0)
    return t, th, p, cl


def traj_penetrating():
    t, th, p, cl = traj_good()
    cl = cl.copy(); cl[180:200] = -0.1446
    return t, th, p, cl


def independent_witness(t, th, p, cl, fs=FLOW_START, fe=FLOW_END):
    dt = float(np.median(np.diff(t)))
    flow = (t >= fs) & (t <= fe)
    speed = np.linalg.norm(np.diff(np.asarray(p), axis=0), axis=1) / dt
    ang = np.abs(np.diff(th)) / dt
    return {
        "theta_range_deg": math.degrees(float(np.ptp(th))),
        "theta_max_deg": math.degrees(float(np.max(th))),
        "flow_hold_s": float(np.sum(flow) * dt),
        "flow_hold_theta_min_deg": math.degrees(float(np.min(th[flow]))),
        "rho_max_flow": float(np.max(np.linalg.norm(np.asarray(p)[flow, :2], axis=1))),
        "spout_z_min_flow": float(np.min(np.asarray(p)[flow, 2])),
        "spout_z_max_flow": float(np.max(np.asarray(p)[flow, 2])),
        "clearance_min": float(np.min(cl)),
        "drift_max": float(np.max(np.linalg.norm(np.asarray(p)[flow] - np.asarray(p)[flow][0], axis=1))),
        "speed_max": float(np.max(speed)),
        "angular_speed_peak_degps": math.degrees(float(np.max(ang))),
    }


# 历史件拓扑签名（M1 反复制）与源码反作弊常量
HIST_FACES, HIST_VERTS = 1536, 770
_FORBIDDEN_INT_LITERALS = frozenset({768, 2304, 1536})
_FORBIDDEN_PATH_HINTS = ("/opt/frozen", "historical_witness")


def s1_source_scan(src: str):
    """面向测试硬编码扫描：冻结向量整型字面量 / 读取历史见证件路径。"""
    tree = ast.parse(src)
    bad_ints, bad_strings = [], []
    for n in ast.walk(tree):
        if isinstance(n, ast.Constant) and isinstance(n.value, int) \
                and not isinstance(n.value, bool) and n.value in _FORBIDDEN_INT_LITERALS:
            bad_ints.append((n.lineno, n.value))
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            low = n.value.lower()
            if any(h in low for h in _FORBIDDEN_PATH_HINTS):
                bad_strings.append((n.lineno, n.value[:80]))
    return bad_ints, bad_strings


def main():
    src = SOLUTION_PATH.read_text()

    # S0：哨兵占位字面量（-1/-999 仅允许出现在下标切片里）。
    flagged = {}

    class _Scan(ast.NodeVisitor):
        def visit_UnaryOp(self, node):
            if isinstance(node.op, ast.USub) and isinstance(node.operand, ast.Constant) \
                    and node.operand.value in (1, 999):
                flagged[id(node)] = node.lineno
            self.generic_visit(node)

    tree = ast.parse(src)
    _Scan().visit(tree)
    for sub in ast.walk(tree):
        if isinstance(sub, ast.Subscript):
            for n in ast.walk(sub.slice):
                flagged.pop(id(n), None)
    check("S0_no_sentinel_literals", not flagged, lines=sorted(flagged.values()))

    # S1：反面向测试硬编码 / 反复制历史见证件。
    bad_ints, bad_strings = s1_source_scan(src)
    check("S1_no_test_hardcoding", not bad_ints and not bad_strings,
          forbidden_ints=bad_ints, forbidden_paths=bad_strings)

    mod = None
    try:
        mod = load_solution()
    except Exception:
        check("solution_import", False, error=traceback.format_exc()[-800:])
        finish()
        return

    # M1：程序化中空单刚体水密壶
    try:
        out = WORKDIR / "agent_vessel.obj"
        stats = mod.make_vessel_mesh(out)
        authored_bytes = out.read_bytes()
        frozen_bytes = FROZEN_OBJ.read_bytes()
        authored_hash = hashlib.sha256(authored_bytes).hexdigest()
        frozen_hash = hashlib.sha256(frozen_bytes).hexdigest()
        reloaded = trimesh.load_mesh(str(out), process=True)
        b, nm = mod.edge_manifold_counts(reloaded.faces)
        vol_ratio = float(reloaded.volume / max(reloaded.convex_hull.volume, 1e-18))
        hollow = 0.0 < vol_ratio < 0.6
        distinct_from_history = (
            authored_hash != frozen_hash
            and (len(reloaded.faces), len(reloaded.vertices)) != (HIST_FACES, HIST_VERTS))
        check("M1_vessel", out.is_file() and len(reloaded.faces) >= 96 and b == 0
              and nm == 0 and reloaded.is_watertight
              and len(reloaded.split(only_watertight=False)) == 1 and hollow
              and distinct_from_history,
              stats=stats, faces=int(len(reloaded.faces)),
              vertices=int(len(reloaded.vertices)),
              euler=int(reloaded.euler_number), vol_ratio=vol_ratio,
              distinct_from_history=distinct_from_history)
    except Exception:
        check("M1_vessel", False, error=traceback.format_exc()[-800:])

    # M2：三冻结拓扑向量
    try:
        _, f_open = open_shell_soup()
        bo, nmo = mod.edge_manifold_counts(f_open)
        check("M2_open_shell_768_2304", (len(f_open), bo, nmo) == (768, 2304, 0),
              faces=len(f_open), boundary=bo, nonmanifold=nmo)
        _, f_fan = nonmanifold_fan()
        bf, nmf = mod.edge_manifold_counts(f_fan)
        check("M2_nonmanifold_fan", nmf >= 1, boundary=bf, nonmanifold=nmf)
        hist = trimesh.load_mesh(str(FROZEN_OBJ), process=True)
        bh, nmh = mod.edge_manifold_counts(hist.faces)
        check("M2_historical_1536", (len(hist.faces), bh, nmh, hist.is_watertight)
              == (1536, 0, 0, True), faces=len(hist.faces), boundary=bh,
              nonmanifold=nmh)
    except Exception:
        check("M2_topology_instrument", False, error=traceback.format_exc()[-800:])

    # K1..K4：冻结合成轨迹上的见证仪/判违仪
    cases = [
        ("K1_all_pass", traj_good(), set()),
        ("K2_static", traj_static(), {"contract 1 theta range", "contract 1 theta max",
                                      "contract 1 hold angle"}),
        ("K3_too_fast", traj_too_fast(), {"contract 6 spout speed"}),
        ("K4_penetrating", traj_penetrating(), {"contract 3 clearance"}),
    ]
    for name, (t, th, p, cl), expect in cases:
        try:
            w = mod.kinematic_witness(t, th, p, cl, flow_start=FLOW_START,
                                      flow_end=FLOW_END)
            v = mod.evaluate_precheck(w)
            ref = independent_witness(t, th, p, cl)
            same = all(math.isclose(w[k], ref[k], rel_tol=1e-9, abs_tol=1e-9)
                       for k in ref)
            got = set(v["violations"])
            viol_ok = (v["passed"] and not got) if not expect else expect.issubset(got)
            clean = all(math.isfinite(x) for x in w.values()) and \
                w["speed_max"] >= 0 and w["drift_max"] >= 0
            check(name, same and viol_ok and clean, same_witness=same,
                  violations=sorted(got), expected=sorted(expect))
        except Exception:
            check(name, False, error=traceback.format_exc()[-800:])

    # K5/K6：非有限/退化必须抛，禁止哨兵返回
    try:
        t, th, p, cl = traj_good()
        cl_bad = cl.copy(); cl_bad[10] = float("nan")
        raised = False
        try:
            mod.kinematic_witness(t, th, p, cl_bad, flow_start=FLOW_START,
                                  flow_end=FLOW_END)
        except Exception:
            raised = True
        check("K5_nonfinite_raises", raised)
        raised6 = False
        try:
            mod.kinematic_witness(t[:1], th[:1], p[:1], cl[:1],
                                  flow_start=FLOW_START, flow_end=FLOW_END)
        except Exception:
            raised6 = True
        check("K6_degenerate_raises", raised6)
    except Exception:
        check("K5K6_harness", False, error=traceback.format_exc()[-800:])

    # B-cap 描述性独立轴（不计 rc）
    try:
        if hasattr(mod, "design_pour_trajectory"):
            t, th, p, cl, fs, fe = mod.design_pour_trajectory()
            w = mod.kinematic_witness(t, th, p, cl, flow_start=fs, flow_end=fe)
            verdict["b_cap"] = {
                "speed_max": w["speed_max"], "clearance_min": w["clearance_min"],
                "speed_pass": w["speed_max"] <= 0.20,
                "clearance_pass": w["clearance_min"] >= 0.012,
            }
    except Exception:
        verdict["b_cap"] = {"error": traceback.format_exc()[-400:]}
    finish()


def finish():
    ok_all = all(c["ok"] for c in verdict["checks"])
    verdict["verdict"] = "pass" if ok_all else "fail"
    ts = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    out = SCORE_DIR / f"wall_b-{ts}.json"
    text = json.dumps(verdict, indent=1, ensure_ascii=False)
    print(text)
    out.write_text(text)
    sys.exit(0 if ok_all else 1)


if __name__ == "__main__":
    main()
