#!/usr/bin/env python3
"""Task 3 ENV_SPEC 探针（D3-2 探针段 + 真值段，AC-T3-9）：容器内只读扫描。

回答三个问题，全部**实测**、**不触网**：

1. 这台容器里到底装了什么（解释器、关键库版本）；
2. 工作目录长什么样（深度 ≤ 3、条数 ≤ 200 的树）；
3. 如果给了 ``--allow-reset`` 与 ``--bddl``：真跑一次 ``reset()``，把
   ``obs_keys`` / 动作维度 / **真值状态可及性**（能否拿到物体位姿与夹爪
   状态、走哪个入口、返回哪些字段）如实登记下来。

第 3 条是 A-13 的直接落地：FAEA 的 84.9% 建立在「特权真值状态 + 绝对末端
动作」上。我们**默认拿不到**真值——那就写 ``available=false``，不得兜底填
true、不得用「理论上可以」冒充实测。取不到时一并登记 ``reason``。

回执**恰好一行** JSON（stdout 只留回执，任何库的横幅一律重定向到 stderr）：

    {"schema":"simpraxis.env_scan.v1","ok":true,
     "python":{"version":"3.10.12","executable":"/usr/bin/python"},
     "packages":{"numpy":"1.26.4",...},
     "workdir_tree":["..."],
     "obs_keys":["..."],"action_dim":7,
     "privileged_state":{"available":true,"accessor":"env.sim.data.body_xpos",
                         "returns":["..."]},
     "demo_entry":null,"errors":[]}

纪律：只读（不写 workdir、不装包、不改环境）；输出只写 ``--out``；
0 联网；0 付费。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

RECEIPT_SCHEMA = "simpraxis.env_scan.v1"
DEFAULT_OUT = "/tmp/simpraxis_env_scan.json"
DEFAULT_WORKDIR = "/work"

# 关键库清单（缺哪个就**不列**，不编版本）。
KEY_PACKAGES = (
    "numpy", "torch", "mujoco", "robosuite", "libero", "easydict", "bddl",
    "h5py", "imageio", "gym", "gymnasium", "dm-control", "trimesh",
    "genesis-world", "mani-skill", "sapien", "scipy", "opencv-python",
)

WORKDIR_MAX_DEPTH = 3
WORKDIR_MAX_ENTRIES = 200

_STDOUT_FD = os.dup(1)


def _emit(payload: dict) -> None:
    os.write(_STDOUT_FD, (json.dumps(payload, ensure_ascii=False) + "\n").encode())


def _mute_library_output() -> None:
    """库的横幅/告警一律进 stderr，保证 stdout 只有一行回执。"""
    os.dup2(2, 1)


def _python_info() -> dict:
    return {"version": sys.version.split()[0],
            "executable": sys.executable,
            "implementation": sys.implementation.name}


def _packages() -> "tuple[dict, list]":
    """按 KEY_PACKAGES 取已装版本；缺件不编造（只进 errors，不进 packages）。"""
    from importlib import metadata

    found: dict = {}
    errors: list = []
    for name in KEY_PACKAGES:
        try:
            found[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            continue
        except Exception as exc:  # noqa: BLE001 - 坏元数据不该让整段扫描失败
            errors.append(f"metadata({name}): {type(exc).__name__}: {exc}")
    return found, errors


def _walk_tree(root: Path) -> "tuple[list, list]":
    """深度 ≤ 3、条数 ≤ 200 的确定性目录树（排序；符号链接不跟随）。"""
    entries: list = []
    errors: list = []
    if not root.is_dir():
        return entries, [f"workdir 不存在或不是目录：{root}"]

    def _walk(path: Path, depth: int) -> None:
        if depth > WORKDIR_MAX_DEPTH or len(entries) >= WORKDIR_MAX_ENTRIES:
            return
        try:
            children = sorted(path.iterdir(), key=lambda p: p.name)
        except OSError as exc:
            errors.append(f"listdir({path}): {type(exc).__name__}: {exc}")
            return
        for child in children:
            if len(entries) >= WORKDIR_MAX_ENTRIES:
                return
            rel = child.relative_to(root).as_posix()
            try:
                is_dir = child.is_dir() and not child.is_symlink()
                is_link = child.is_symlink()
            except OSError:
                is_dir, is_link = False, False
            entries.append(rel + ("/" if is_dir else ("@" if is_link else "")))
            if is_dir:
                _walk(child, depth + 1)

    _walk(root, 1)
    return entries, errors


def _action_dim(env) -> "int | None":
    """动作维度：优先 ``action_dim``/``action_spec``，其次 ``action_space``。"""
    for attr in ("action_dim",):
        value = getattr(env, attr, None)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
    action_space = getattr(env, "action_space", None)
    shape = getattr(getattr(action_space, "shape", None), "__len__", None)
    if shape:
        dims = tuple(int(x) for x in action_space.shape)
        if len(dims) == 1 and dims[0] > 0:
            return dims[0]
    spec = getattr(env, "action_spec", None)
    flat = getattr(spec, "shape", None)
    if isinstance(flat, (tuple, list)) and len(flat) == 1:
        return int(flat[0])
    return None


def _privileged_probe(env) -> dict:
    """实测真值状态可及性（A-13）：**只登记真的取到的东西**。

    判据按优先级：``env.sim.data`` 能取到物体位姿数组 ⇒ 可用；否则
    ``env.robots[0]._joint_positions`` 这类夹爪真值 ⇒ 部分可用；都取不到
    ⇒ ``available=False`` 并写明 reason。任何异常都如实记，不降级成 true。
    """
    result = {"available": False, "accessor": "unavailable",
              "returns": [], "reason": ""}
    try:
        sim = getattr(env, "sim", None)
        sim_data = getattr(sim, "data", None)
        if sim_data is not None and hasattr(sim_data, "body_xpos"):
            n = len(sim_data.body_xpos)
            result.update(available=True, accessor="env.sim.data.body_xpos",
                          returns=["body_xpos", "body_xquat", "qpos", "qvel"])
            result["reason"] = f"env.sim.data 可取；body_xpos 长度 {n}"
            return result
        robots = getattr(env, "robots", None)
        if robots:
            robot = robots[0]
            for attr in ("_joint_positions", "gripper_qpos", "eef_pos"):
                if hasattr(robot, attr):
                    result.update(
                        available=True,
                        accessor=f"env.robots[0].{attr}",
                        returns=[attr],
                        reason="只取到夹爪/末端真值，物体位姿未暴露")
                    return result
        result["reason"] = "env 没有 sim.data / robots 真值入口"
    except Exception as exc:  # noqa: BLE001 - 探测失败要如实记录
        result["reason"] = f"{type(exc).__name__}: {exc}"
    return result


def _obs_probe(env) -> "tuple[list, list]":
    """``reset()`` 一次，取观测键（**会推进一次仿真**，故须 --allow-reset）。"""
    errors: list = []
    try:
        obs = env.reset()
    except Exception as exc:  # noqa: BLE001
        return [], [f"reset(): {type(exc).__name__}: {exc}"]
    if isinstance(obs, dict):
        return [str(k) for k in sorted(obs)], errors
    # robosuite/libero 的 reset 可能返回 (obs, info) 或直接返回 env。
    if isinstance(obs, (tuple, list)) and obs and isinstance(obs[0], dict):
        return [str(k) for k in sorted(obs[0])], errors
    errors.append(f"reset() 返回类型 {type(obs).__name__}，无法取键")
    return [], errors


def _build_libero_env(bddl: Path):
    """构造 LIBERO 的 OffScreenRenderEnv（唯一支持的真跑入口）。

    刻意**不**做「猜模块名」的兜底：入口不存在就报错，由调用方决定退到
    静态段——猜出来的 key 是假环境卡，比没有更糟。
    """
    from libero.libero.envs import OffScreenRenderEnv   # noqa: PLC0415

    return OffScreenRenderEnv(bddl_file_name=str(bddl), camera_heights=128,
                              camera_widths=128)


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default=DEFAULT_OUT)
    parser.add_argument("--workdir", default=DEFAULT_WORKDIR)
    parser.add_argument("--allow-reset", action="store_true",
                        help="允许 reset() 一次以取观测键（会推进仿真）")
    parser.add_argument("--bddl", default=None,
                        help="LIBERO 任务 bddl 路径；缺省则不做真跑探测")
    parser.add_argument("--demo-entry", default=None)
    args = parser.parse_args(argv)

    receipt: dict = {"schema": RECEIPT_SCHEMA, "ok": False,
                     "python": {}, "packages": {}, "workdir_tree": [],
                     "obs_keys": [], "action_dim": None,
                     "privileged_state": {"available": False,
                                          "accessor": "unavailable",
                                          "returns": [], "reason": "未探测"},
                     "demo_entry": None, "errors": []}
    _mute_library_output()
    try:
        receipt["python"] = _python_info()
        receipt["packages"], pkg_errors = _packages()
        receipt["errors"].extend(pkg_errors)
        tree, tree_errors = _walk_tree(Path(args.workdir))
        receipt["workdir_tree"] = tree
        receipt["errors"].extend(tree_errors)
        if args.demo_entry:
            receipt["demo_entry"] = args.demo_entry

        if args.allow_reset:
            if not args.bddl:
                receipt["errors"].append(
                    "--allow-reset 需要 --bddl：没有任务文件就无法 reset 取键"
                    "（不得编造 obs_keys）")
            elif not Path(args.bddl).is_file():
                receipt["errors"].append(f"bddl 不存在：{args.bddl}")
            else:
                env = None
                try:
                    env = _build_libero_env(Path(args.bddl))
                    receipt["obs_keys"], obs_errors = _obs_probe(env)
                    receipt["errors"].extend(obs_errors)
                    receipt["action_dim"] = _action_dim(env)
                    receipt["privileged_state"] = _privileged_probe(env)
                except ImportError as exc:
                    receipt["errors"].append(
                        f"libero 不可导入，真跑探测跳过：{type(exc).__name__}: {exc}")
                except Exception as exc:  # noqa: BLE001
                    receipt["errors"].append(
                        f"环境构造/reset 失败：{type(exc).__name__}: {exc}")
                finally:
                    closers = ("close", "__del__")
                    for name in closers:
                        fn = getattr(env, name, None)
                        if callable(fn):
                            try:
                                fn()
                            except Exception:  # noqa: BLE001
                                pass
                            break
        receipt["ok"] = True
    except Exception as exc:  # noqa: BLE001 - 回执要的是 ok/err，不是栈
        receipt["errors"].append(f"{type(exc).__name__}: {exc}")
    finally:
        try:
            out = Path(args.out)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(receipt, ensure_ascii=False, indent=2),
                           encoding="utf-8")
        except OSError as exc:
            receipt["errors"].append(f"写回执失败：{exc}")
        _emit(receipt)
    return 0 if receipt["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
