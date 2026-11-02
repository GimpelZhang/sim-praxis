import json
import numpy as np
import trimesh
import agent_solution as a

assert a.edge_manifold_counts(np.array([[0,1,2],[0,2,1]])) == (2, 0)
assert a.edge_manifold_counts(np.array([[0,1,2]])) == (3, 0)
assert a.edge_manifold_counts(np.array([[0,1,2],[1,0,2],[0,1,2]])) == (2, 1)
assert a.edge_manifold_counts(np.empty((0,3), dtype=int)) == (0, 0)

stats = a.make_vessel_mesh("check_vessel.obj")
print("stats", json.dumps(stats))
m = trimesh.load("check_vessel.obj", force="mesh", process=True)
print("loaded", len(m.vertices), len(m.faces), m.is_watertight, m.is_winding_consistent, m.volume, m.convex_hull.volume)
assert len(m.split(only_watertight=False)) == 1
assert len(m.faces) >= 96
assert m.is_watertight
assert a.edge_manifold_counts(m.faces) == (0, 0)
ratio = m.volume / m.convex_hull.volume
print("ratio", ratio)
assert 0 < ratio < 0.6

# Hand-computable values, including a clearance minimum outside the flow window.
t = np.array([0., 1., 2., 3.])
th = np.radians([0., 120., 110., 130.])
p = np.array([[0.,0.,1.04], [.01,0.,1.05], [.03,.04,1.06], [0.,0.,1.07]])
c = np.array([.005, .02, .02, .02])
w = a.kinematic_witness(t, th, p, c, flow_start=1., flow_end=3.)
print("witness", w)
assert list(w) == ["dt_s", "theta_range_deg", "theta_max_deg", "flow_hold_s", "flow_hold_theta_min_deg", "rho_max_flow", "spout_z_min_flow", "spout_z_max_flow", "clearance_min", "drift_max", "speed_max", "angular_speed_peak_degps"]
assert w["dt_s"] == 1 and w["flow_hold_s"] == 3
assert abs(w["theta_range_deg"] - 130) < 1e-10
assert abs(w["theta_max_deg"] - 130) < 1e-10
assert abs(w["flow_hold_theta_min_deg"] - 110) < 1e-10
assert abs(w["rho_max_flow"] - .05) < 1e-12
assert w["spout_z_min_flow"] == 1.05 and w["spout_z_max_flow"] == 1.07
assert w["clearance_min"] == .005
assert abs(w["drift_max"] - np.linalg.norm(p[3]-p[1])) < 1e-12
assert abs(w["speed_max"] - np.linalg.norm(p[2]-p[1])) < 1e-12
assert abs(w["angular_speed_peak_degps"] - 20) < 1e-10
v = a.evaluate_precheck(w)
print("violations", v)
assert v["violations"] == ["contract 1 hold angle", "contract 2 spout alignment", "contract 3 clearance", "contract 6 spout drift", "contract 6 spout speed"]

base = dict(dt_s=.1, theta_range_deg=120., theta_max_deg=120., flow_hold_s=1., flow_hold_theta_min_deg=105., rho_max_flow=.035, spout_z_min_flow=1.025, spout_z_max_flow=1.07, clearance_min=.012, drift_max=.010, speed_max=.20, angular_speed_peak_degps=80.)
assert a.evaluate_precheck(base) == {"passed": True, "violations": []}
checks = [("theta_range_deg",119.,"contract 1 theta range"), ("theta_max_deg",119.,"contract 1 theta max"), ("flow_hold_theta_min_deg",104.,"contract 1 hold angle"), ("rho_max_flow",.036,"contract 2 spout alignment"), ("spout_z_min_flow",1.024,"contract 2 spout height"), ("spout_z_max_flow",1.071,"contract 2 spout height"), ("clearance_min",.011,"contract 3 clearance"), ("drift_max",.011,"contract 6 spout drift"), ("speed_max",.201,"contract 6 spout speed"), ("angular_speed_peak_degps",81.,"contract 6 angular speed")]
for key, value, name in checks:
    item = dict(base); item[key] = value
    assert a.evaluate_precheck(item) == {"passed": False, "violations": [name]}

traj = a.design_pour_trajectory()
dw = a.kinematic_witness(*traj[:4], flow_start=traj[4], flow_end=traj[5])
print("design", dw, a.evaluate_precheck(dw))
assert a.evaluate_precheck(dw)["passed"]

bad = [
    (np.array([0.]), np.array([0.]), np.zeros((1,3)), np.array([1.])),
    (np.array([0.,1.]), np.array([0.]), np.zeros((2,3)), np.ones(2)),
    (np.array([0.,0.]), np.zeros(2), np.zeros((2,3)), np.ones(2)),
    (np.array([0.,1.,2.1]), np.zeros(3), np.zeros((3,3)), np.ones(3)),
    (np.array([0.,np.nan]), np.zeros(2), np.zeros((2,3)), np.ones(2)),
    (np.array([0.,1.]), np.array([0.,np.inf]), np.zeros((2,3)), np.ones(2)),
    (np.array([0.,1.]), np.zeros(2), np.zeros((2,2)), np.ones(2)),
]
for args in bad:
    try:
        a.kinematic_witness(*args, flow_start=0., flow_end=1.)
    except ValueError:
        pass
    else:
        raise AssertionError("invalid input accepted: " + repr(args))
print("ALL CHECKS PASSED")
