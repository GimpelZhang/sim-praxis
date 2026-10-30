#!/usr/bin/env python3
"""Task 5 墙 A 冻结评分器（首呼前冻结，模型只读其行为契约、不见本件源码）。

在一次性兄弟评分容器内运行：导入模型自著 ``agent_solution.py`` 的
``build_env()``，随后**评分器独立**驱动并核验同一条历史 CPU 链路
（Phase 2 Complete G-9 两坑）：

  import 链 → ControlEnv 构造（渲染全关）→ reset → 3 个零动作 step；
  pit1：binding_utils ``jnt_type in (HINGE,SLIDE)`` 断言分支——评分器对
        合并场景全部 hinge/slide 关节实跑 ``get_joint_qpos_addr``，逐字
        比对冻结的 13 关节（名/型/qpos 地址）；
  pit2：base_controller ``mujoco.mj_fullM(..., sim.data.qM)``——reset+step
        后原始 qM 是紧凑稀疏（len=nM=153，nv=43），OSC 控制器的
        mass_matrix 必须是 7×7 finite 稠密矩阵；
  无头证据：构造后与 reset 后两次独立检查 ``_render_context_offscreen is
        None``（模型开渲染即 EGL 崩溃或被本检查判红，双向反证）。

用法：``python wall_a_runner.py <solution.py> <score_out_dir>``
退出码 0=闸过 / 1=未过；JSON 判决同时写 stdout 与
``<score_out_dir>/wall_a-<ts>.json``。纯 CPU、禁网、只读挂载评分。
"""
import importlib.util
import json
import math
import os
import sys
import time
import traceback
from pathlib import Path

# --- 容器机制由评分器预置（模型只解历史墙，不做容器考古） --------------------
_INST_DIR = Path(__file__).resolve().parent
os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")
os.environ.setdefault("NUMBA_CACHE_DIR", "/tmp/numba_cache")
os.environ.setdefault("MPLCONFIGDIR", "/tmp/mpl")
os.makedirs("/tmp/numba_cache", exist_ok=True)
os.makedirs("/tmp/mpl", exist_ok=True)
# /opt/gate 由冻结脚本物化（pyshim + libero_cfg）；同路径在评分容器与
# 模型容器一致，TASK.md 不暴露这些机制。
if Path("/opt/gate").is_dir():
    sys.path.insert(0, "/opt/gate/pyshim")
    os.environ["LIBERO_CONFIG_PATH"] = "/opt/gate/libero_cfg"

SOLUTION_PATH = Path(sys.argv[1])
SCORE_DIR = Path(sys.argv[2])
SCORE_DIR.mkdir(parents=True, exist_ok=True)

verdict = {"schema": "simpraxis.phase25.wall_a_score.v1", "checks": []}


def check(name, ok, **kw):
    verdict["checks"].append({"check": name, "ok": bool(ok), **kw})
    print(f"[wall_a] {name}: {'PASS' if ok else 'FAIL'} {kw}", flush=True)
    return bool(ok)


def load_solution():
    spec = importlib.util.spec_from_file_location("agent_solution", SOLUTION_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if not callable(getattr(mod, "build_env", None)):
        raise AttributeError("agent_solution 必须定义可调用的 build_env()")
    return mod


def finish(rc):
    verdict["verdict"] = "pass" if rc == 0 else "fail"
    ts = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    out = SCORE_DIR / f"wall_a-{ts}.json"
    text = json.dumps(verdict, indent=1, ensure_ascii=False)
    print(text)
    out.write_text(text)
    sys.exit(rc)


def main():
    t0 = time.monotonic()
    expected = json.loads((_INST_DIR / "expected_vectors.json").read_text())
    env = None
    try:
        import numpy as np
        check("prov_numpy", np.__version__ == expected["numpy"],
              version=np.__version__)
        import mujoco
        check("prov_mujoco", mujoco.__version__ == expected["mujoco"],
              version=mujoco.__version__,
              file=mujoco.__file__)
        import robosuite
        check("prov_robosuite", robosuite.__version__ == expected["robosuite"],
              version=robosuite.__version__, file=robosuite.__file__)

        from libero.libero.envs.env_wrapper import ControlEnv
        mod = load_solution()
        env = mod.build_env()

        # LIBERO ControlEnv 是纯包装类：env.env 才是动态生成的 robosuite
        # MujocoEnv（类名随 bddl problem 变化），故 isinstance 必须打在外层。
        inner_raw = getattr(env, "env", None)
        check("env_is_controlenv", isinstance(env, ControlEnv),
              outer_type=type(env).__name__,
              inner_type=type(inner_raw).__name__
              if inner_raw is not None else None)
        sim0 = env.sim
        check("pre_reset_no_render_context",
              sim0._render_context_offscreen is None)

        # pit1：评分器独立遍历活模型，不接受模型自报。
        raw = sim0.model._model
        hinge_slide = (mujoco.mjtJoint.mjJNT_HINGE, mujoco.mjtJoint.mjJNT_SLIDE)
        got = []
        for i in range(raw.njnt):
            jname = mujoco.mj_id2name(raw, mujoco.mjtObj.mjOBJ_JOINT, i)
            jtype = int(raw.jnt_type[i])
            if jtype in tuple(int(x) for x in hinge_slide):
                got.append({"name": jname, "type": jtype,
                            "addr": int(sim0.model.get_joint_qpos_addr(jname))})
        check("pit1_joints", got == expected["pit1_joints"],
              n=len(got), joints=got)

        obs = env.reset()
        check("reset_obs_keys",
              sorted(obs.keys()) == sorted(expected["obs_keys"])
              and len(obs) == len(expected["obs_keys"]),
              n=len(obs))
        sim = env.sim          # hard_reset 释放了构造期 MjSim，必须重取
        check("post_reset_no_render_context",
              sim._render_context_offscreen is None)

        a = np.zeros(7, dtype=np.float64)
        last = None
        for _ in range(int(expected["steps"])):
            last = env.step(a)
        obs2, reward, done, _info = last
        check("zero_action_steps",
              len(obs2) == len(expected["obs_keys"])
              and math.isclose(float(reward), float(expected["reward"]))
              and bool(done) == bool(expected["done"]),
              reward=float(reward), done=bool(done))

        raw = sim.model._model
        nv, nM = int(sim.model.nv), int(raw.nM)
        qM = np.asarray(sim.data.qM, dtype=np.float64)
        exp2 = expected["pit2"]
        check("pit2_compact_qM",
              nv == exp2["nv"] and nM == exp2["qM_len"]
              and qM.shape == (nM,) and np.isfinite(qM).all(),
              nv=nv, nM=nM, qM_len=int(qM.shape[0]))
        ctrl = env.robots[0].controller
        mm = np.asarray(ctrl.mass_matrix, dtype=np.float64)
        n_arm = int(np.asarray(ctrl.qvel_index).shape[0])
        check("pit2_controller_mass_matrix",
              [int(x) for x in mm.shape] == exp2["mass_matrix_shape"]
              and np.isfinite(mm).all() and n_arm == expected["qvel_index_len"],
              shape=list(mm.shape), qvel_index_len=n_arm)

        # LIBERO 通用目标求值器可能返回 numpy.bool_，身份比较 is False 会误红，
        # 归一化到内置 bool；语义仍是「三零动作后任务未成功」。
        cs = env.check_success()
        check("check_success_false", bool(cs) is False,
              raw_type=type(cs).__name__, value=bool(cs))
        env.close()
        env = None
        verdict["elapsed_s"] = round(time.monotonic() - t0, 3)
        ok_all = all(c["ok"] for c in verdict["checks"])
        finish(0 if ok_all else 1)
    except SystemExit:
        raise
    except Exception:
        verdict["traceback"] = traceback.format_exc()[-2000:]
        try:
            if env is not None:
                env.close()
        except Exception:
            pass
        verdict["elapsed_s"] = round(time.monotonic() - t0, 3)
        finish(1)


if __name__ == "__main__":
    main()
