#!/usr/bin/env python3
"""LIBERO episode 执行器（Phase 2.7 Task 6，容器内跑；0 付费）。

职责（只做三件事，别的不做）：
1. 用**官方** `libero.libero.benchmark` 取任务 / 初态，用**官方**
   `OffScreenRenderEnv` 起环境（D6-5：基线不重写）；
2. 按 `--policy` 产出动作（`zero` / `octo-small` / `openvla-7b`），逐控制步
   推进并记录 telemetry（EE 位姿、接触数、穿模深度、success 谓词）；
3. 写 1920×1080 MP4（`imageio`，20 fps）+ `telemetry_*.jsonl` + `episode_*.json`。

纪律：
- 成功判定只用官方 `env.check_success()`（预注册 §3），不自己造谓词；
- 穿模深度取 MuJoCo `data.contact[:ncon].dist` 的负值最大者（图省事写 0 会
  在 Rung1 上造成 vacuous pass，禁止）；
- 每个数值都来自真实步进；没有动作时**不推进**（No Fake Physics）；
- 策略模型（Octo/OpenVLA）**整进程只加载一次**，逐 episode 复用——否则
  10 episode × 模型加载会把 85 呼预算之外的时间全烧在冷启动上。

用法（容器内）：
    # 冒烟（单 episode）
    python3 /workspace/scripts/phase27/libero_episode_runner.py \
        --policy zero --task-index 0 --init-index 0 \
        --video /runs/s1/videos/T1-0.mp4 --out-dir /runs/s1
    # 整批（10 episode，模型只加载一次）
    python3 ... --policy octo-small --spec /runs/s2/episodes_spec.json \
        --out-dir /runs/s2
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any

WIDTH = 1920
HEIGHT = 1080
FPS = 20
MAX_STEPS = 300
# 官方评估惯例的稳定步：reset + set_init_state 后先跑几步让物体落定，
# 不计入控制步、不进 telemetry（口径写死在预注册 §2）。
WARMUP_STEPS = 5

TASK_NAMES = [
    "pick_up_the_alphabet_soup_and_place_it_in_the_basket",
    "pick_up_the_cream_cheese_and_place_it_in_the_basket",
    "pick_up_the_salad_dressing_and_place_it_in_the_basket",
    "pick_up_the_bbq_sauce_and_place_it_in_the_basket",
    "pick_up_the_ketchup_and_place_it_in_the_basket",
]


def _penetration(env) -> float:
    """当前步最大穿模深度（米）：MuJoCo contact.dist 的负值最大者。

    robosuite 的 `sim` 是 `MjSim` 包装；`sim.data.contact` 是 contact 列表，
    `dist` 负值 = 相互穿透深度。无接触时返回 0.0。
    """
    try:
        data = env.sim.data
    except AttributeError:
        return 0.0
    ncon = int(getattr(data, "ncon", 0) or 0)
    if ncon <= 0:
        return 0.0
    worst = 0.0
    try:
        for i in range(ncon):
            d = float(data.contact[i].dist)
            if d < worst:
                worst = d
    except (IndexError, AttributeError, TypeError):
        return 0.0
    return abs(worst)


def _contact_force_norm(env) -> float:
    """当前步接触力合力模长（N）：MuJoCo `cfrc_ext` 全场景范数。

    Rung2 的「接触稳定」子项要一条力时序；LIBERO 没有现成的 wrench 通道，
    用 MuJoCo 自带的 `data.cfrc_ext`（每个 body 的外力旋量）范数之和作代理，
    口径写进预注册 §8（不冒充特定接触点的力）。
    """
    try:
        data = env.sim.data
    except AttributeError:
        return 0.0
    try:
        import numpy as np
        cf = np.asarray(data.cfrc_ext, dtype=float)
        if cf.size == 0:
            return 0.0
        return float(np.linalg.norm(cf.reshape(-1, 6), axis=1).sum())
    except Exception:  # noqa: BLE001 - 取不到就如实返回 0（Rung2 会判不通过）
        return 0.0


def _ee_pos(env) -> list[float]:
    try:
        return [float(x) for x in env.robots[0].controller.ee_pos]
    except (AttributeError, IndexError, TypeError):
        try:
            return [float(x) for x in env.sim.data.site_xpos[
                env.sim.model.site_name2id("gripper0_grip_site")]]
        except Exception:  # noqa: BLE001 - 观测缺字段时如实返回空
            return []


def _obj_pos(env) -> list[float]:
    """物体位姿（取场景里第一个 non-robot body 的位置；取不到返回空）。"""
    try:
        import numpy as np
        data = env.sim.data
        body_names = [env.sim.model.body_id2name(i)
                      for i in range(env.sim.model.nbody)]
        for name in body_names:
            if not name or "robot" in name or name in ("world", "table"):
                continue
            if name.endswith("_main") or name.startswith("object"):
                return [float(x) for x in data.body_xpos[
                    env.sim.model.body_name2id(name)]]
        return []
    except Exception:  # noqa: BLE001
        return []


def _obs_frame(obs: dict) -> Any:
    frame = obs.get("agentview_image")
    if frame is None:
        raise RuntimeError("观测里没有 agentview_image：无法出帧（No Fake Physics）")
    return frame[::-1, :, ::-1]  # MuJoCo 图像上下左右翻转


class ZeroPolicy:
    """S1 基线：官方零动作（不写新策略，D6-5）。"""

    name = "zero"

    def __init__(self) -> None:
        pass

    def bind(self, env, task: dict[str, Any]) -> None:
        self.action_dim = int(getattr(env, "action_dim", 7) or 7)

    def act(self, obs: dict, step: int) -> Any:
        import numpy as np
        a = np.zeros(self.action_dim, dtype=float)
        # 夹爪保持张开（-1）以便观察接近过程；其余维度为 0 = 不动
        if self.action_dim >= 7:
            a[-1] = -1.0
        return a


class _NeedMoreHistory(RuntimeError):
    pass


class OctoPolicy:  # pragma: no cover - 需要容器内 JAX/octo 权重
    """Octo-Small（JAX）。整进程只 `load_pretrained` 一次。"""

    name = "octo-small"
    _model = None
    _tf = None

    def __init__(self) -> None:
        if OctoPolicy._model is None:
            try:
                import tensorflow as tf  # type: ignore
                from octo.model.octo_model import OctoModel  # type: ignore
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(
                    f"octo 依赖不可用：{type(exc).__name__}: {exc}") from exc
            local = os.environ.get("OCTO_CKPT") or ""
            if not local:
                raise RuntimeError("OCTO_CKPT 未设置（Octo 权重目录）")
            OctoPolicy._tf = tf
            OctoPolicy._model = OctoModel.load_pretrained(local)
        self.tf = OctoPolicy._tf
        self.model = OctoPolicy._model
        self.history: list[dict[str, Any]] = []
        self.task = None

    def bind(self, env, task: dict[str, Any]) -> None:
        self.action_dim = int(getattr(env, "action_dim", 7) or 7)
        self.history = []
        self.task = self.model.create_tasks(texts=[task["language"]])

    def act(self, obs: dict, step: int):
        import numpy as np
        img = obs["agentview_image"][::-1, :, ::-1]
        # Octo 期望 image_primary: (batch, window, 256, 256, 3), 0..1
        img = self.tf.image.resize(img, (256, 256)).numpy().astype("float32") / 255.0
        self.history.append({"image_primary": img,
                             "timestep": np.array([step]),
                             "pad_mask": np.array([True])})
        window = 2
        if len(self.history) < window:
            raise _NeedMoreHistory()
        obs_in = {k: np.stack([h[k] for h in self.history[-window:]])[None]
                  for k in ("image_primary", "timestep", "pad_mask")}
        actions = self.model.sample_actions(obs_in, self.task,
                                            rng=np.random.default_rng(0))
        return actions[0, 0]


class OpenVLAPolicy:  # pragma: no cover - 需要容器内权重
    """OpenVLA-7B（HF transformers）。整进程只 `from_pretrained` 一次。"""

    name = "openvla-7b"
    _model = None
    _proc = None
    _Image = None

    def __init__(self) -> None:
        if OpenVLAPolicy._model is None:
            try:
                import torch
                from PIL import Image
                from transformers import AutoModelForVision2Seq, AutoProcessor
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(f"transformers 依赖不可用：{exc}") from exc
            model_id = os.environ.get("OPENVLA_MODEL", "openvla/openvla-7b")
            four_bit = os.environ.get("OPENVLA_4BIT") == "1"
            OpenVLAPolicy._proc = AutoProcessor.from_pretrained(
                model_id, trust_remote_code=True)
            kwargs: dict[str, Any] = dict(low_cpu_mem_usage=True,
                                          trust_remote_code=True)
            # 适配（2026-11-04）：`torch_dtype` 必须是 `torch.dtype` 或 `"auto"`。
            # 早前这里写成字符串 `"bfloat16"`，transformers 4.x 直接
            # `ValueError: torch_dtype can be either torch.dtype or "auto", but
            # received bfloat16`，导致 fp16 主策略在探活阶段被判
            # `policy_unreachable`（round 5 S2/S3 全降级，chosen={}）。这是本仓
            # 代码 bug，不是策略不可达，修的是传参类型。
            if four_bit:
                kwargs["load_in_4bit"] = True
            kwargs["torch_dtype"] = torch.bfloat16
            OpenVLAPolicy._model = AutoModelForVision2Seq.from_pretrained(
                model_id, **kwargs).to("cuda:0")
            OpenVLAPolicy._Image = Image
        self.model = OpenVLAPolicy._model
        self.processor = OpenVLAPolicy._proc
        self.Image = OpenVLAPolicy._Image
        self.instruction = ""
        self.unnorm_key: "str | None" = None
        self.unnorm_note: "str | None" = None

    def bind(self, env, task: dict[str, Any]) -> None:
        self.action_dim = int(getattr(env, "action_dim", 7) or 7)
        self.instruction = task["language"]
        self.unnorm_key, self.unnorm_note = self._resolve_unnorm_key()

    def _resolve_unnorm_key(self) -> "tuple[str | None, str | None]":
        """动作反归一化键：预注册请求的键优先，缺键时**如实降级并留痕**。

        适配（2026-11-04）：S0 钉死的检查点是 `openvla/openvla-7b`（base，OpenX 混训），
        它的 `norm_stats` 里没有 LIBERO 的 `libero_object` 键（那个键只存在于
        `openvla-7b-finetuned-libero-*` 检查点）。预注册要求跑的是 S0 登记的那一个，
        所以这里不偷换检查点，而是：缺键 ⇒ 用检查点自带的键做反归一化，并把
        「请求键 vs 实际键」写进 episode 摘要与 degradations（动作分布不匹配，
        成功率预期偏低，不得据此声称策略不可行）。
        """
        want = os.environ.get("OPENVLA_UNNORM_KEY", "") or ""
        stats = getattr(self.model, "norm_stats", None) or {}
        keys = sorted(stats) if isinstance(stats, dict) else []
        if want and want in keys:
            return want, None
        if not keys:
            return None, f"检查点无 norm_stats（请求 {want!r}），动作按原始输出使用"
        pref = [k for k in ("bridge_orig", "bridge") if k in stats] or keys
        used = pref[0]
        note = (f"请求 unnorm_key={want!r} 不在检查点 norm_stats（{len(keys)} 键："
                f"{','.join(keys[:6])}{'…' if len(keys) > 6 else ''}）里；改用 {used!r} "
                "做反归一化——动作分布不匹配，成功率预期偏低（base 检查点非 LIBERO 微调）")
        return used, note

    def act(self, obs: dict, step: int):
        import numpy as np
        img = self.Image.fromarray(obs["agentview_image"][::-1, :, ::-1])
        prompt = f"In: What action should the robot take to {self.instruction}?\nOut:"
        inputs = self.processor(prompt, img).to("cuda:0", dtype=self.model.dtype)
        kwargs: dict[str, Any] = {}
        if self.unnorm_key:
            kwargs["unnorm_key"] = self.unnorm_key
        action = self.model.predict_action(**inputs, **kwargs)
        return np.asarray(action, dtype=float)


POLICIES = {"zero": ZeroPolicy, "octo-small": OctoPolicy,
            "openvla-7b": OpenVLAPolicy}


def _vram_mib() -> float | None:
    try:
        import torch
        if not torch.cuda.is_available():
            return None
        return round(torch.cuda.max_memory_allocated() / (1024 ** 2), 1)
    except Exception:  # noqa: BLE001
        return None


def run_episode(env, bench, policy, task_index: int, init_index: int, *,
                seed: int, out: Path, video: Path,
                max_steps: int = MAX_STEPS) -> dict[str, Any]:
    """跑一个 episode，落 telemetry/episode/video 三件，返回摘要。"""
    import numpy as np
    import imageio.v2 as imageio

    task = bench.get_task(task_index)
    init_states = bench.get_task_init_states(task_index)
    init = init_states[init_index]
    np.random.seed(seed)

    tag = f"T{task_index + 1}-{init_index}"
    tele_path = out / f"telemetry_{policy.name}_{tag}.jsonl"
    video.parent.mkdir(parents=True, exist_ok=True)
    tele_fh = tele_path.open("w", encoding="utf-8")
    writer = imageio.get_writer(str(video), fps=FPS, codec="libx264",
                                quality=8, macro_block_size=8)
    t0 = time.time()
    success = False
    steps = 0
    err = None
    ee_track: list[list[float]] = []
    obj_track: list[list[float]] = []
    pen_track: list[float] = []
    force_track: list[float] = []
    active_track: list[bool] = []
    try:
        env.reset()
        obs = env.set_init_state(init)
        # 官方评估惯例的稳定步（不计入控制步、不进 telemetry）
        for _ in range(WARMUP_STEPS):
            obs, _, _, _ = env.step(np.array([0, 0, 0, 0, 0, 0, -1.0]))
        policy.bind(env, {"language": task.language})
        for step in range(max_steps):
            frame = _obs_frame(obs)
            writer.append_data(frame)
            try:
                action = policy.act(obs, step)
            except _NeedMoreHistory:
                action = np.zeros(7)
            obs, reward, done, info = env.step(np.asarray(action, dtype=float))
            steps = step + 1
            success = bool(env.check_success())
            ee = _ee_pos(env)
            obj = _obj_pos(env)
            pen = _penetration(env)
            frc = _contact_force_norm(env)
            ncon = int(getattr(env.sim.data, "ncon", 0) or 0)
            ee_track.append(ee)
            obj_track.append(obj)
            pen_track.append(pen)
            force_track.append(frc)
            active_track.append(ncon > 0)
            tele_fh.write(json.dumps({
                "step": steps,
                "reward": float(reward) if reward is not None else None,
                "done": bool(done),
                "success": success,
                "ee_pos": ee,
                "obj_pos": obj,
                "penetration_m": pen,
                "contact_force_n": frc,
                "ncon": ncon,
                "action": [float(x) for x in np.asarray(action).ravel()[:7]],
            }, ensure_ascii=False) + "\n")
            if done:
                break
    except Exception as exc:  # noqa: BLE001 - 失败也如实落件，不静默
        err = f"{type(exc).__name__}: {exc}"
        traceback.print_exc()
    finally:
        try:
            writer.close()
        finally:
            tele_fh.close()

    summary = {
        "policy": policy.name,
        "task_index": task_index,
        "task_name": task.name,
        "task_language": task.language,
        "init_index": init_index,
        "seed": seed,
        "steps": steps,
        "success": success,
        "max_steps": max_steps,
        "wallclock_s": round(time.time() - t0, 3),
        "video": str(video),
        "telemetry": str(tele_path),
        "resolution": [WIDTH, HEIGHT],
        "fps": FPS,
        "vram_mib": _vram_mib(),
        "warmup_steps": WARMUP_STEPS,
        # 反归一化键取证（见 OpenVLAPolicy._resolve_unnorm_key）：None = 未降级。
        # 该字段会进 degradations（kind="other"），不得静默。
        "unnorm_key_used": getattr(policy, "unnorm_key", None),
        "unnorm_key_note": getattr(policy, "unnorm_note", None),
        "error": err,
        # 供 Rung 判定用的原始时序（同进程内直接判，不依赖二次读盘）
        "_tracks": {"ee": ee_track, "obj": obj_track,
                    "pen": pen_track, "force": force_track,
                    "active": active_track},
    }
    (out / f"episode_{policy.name}_{tag}.json").write_text(
        json.dumps({k: v for k, v in summary.items() if k != "_tracks"},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--policy", choices=sorted(POLICIES), required=True)
    ap.add_argument("--spec", default=None,
                    help="批量清单 JSON：[{task_index, init_index, seed}, ...]")
    ap.add_argument("--task-index", type=int, default=None)
    ap.add_argument("--init-index", type=int, default=0)
    ap.add_argument("--seed", type=int, default=1000)
    ap.add_argument("--max-steps", type=int, default=MAX_STEPS,
                    help="单 episode 控制步上限（默认 300；预检用小值）")
    ap.add_argument("--video", default=None)
    ap.add_argument("--out-dir", required=True)
    args = ap.parse_args(argv)

    from libero.libero import benchmark, get_libero_path  # noqa: F401
    from libero.libero.envs import OffScreenRenderEnv

    bench = benchmark.get_benchmark_dict()["libero_object"]()
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)

    if args.spec:
        episodes = json.loads(Path(args.spec).read_text(encoding="utf-8"))
    elif args.task_index is not None:
        episodes = [{"task_index": args.task_index,
                     "init_index": args.init_index, "seed": args.seed}]
    else:
        raise SystemExit("必须给 --spec 或 --task-index")

    # 预注册守卫：任务必须在预注册清单里，顺序/种子不得临时改
    for ep in episodes:
        name = bench.get_task(int(ep["task_index"])).name
        if name not in TASK_NAMES:
            raise SystemExit(f"任务不在预注册清单：{name}")

    policy = POLICIES[args.policy]()
    results: list[dict[str, Any]] = []
    for ep in episodes:
        ti = int(ep["task_index"])
        ii = int(ep["init_index"])
        task = bench.get_task(ti)
        bddl = Path(get_libero_path("bddl_files")) / task.problem_folder / task.bddl_file
        env = OffScreenRenderEnv(bddl_file_name=str(bddl),
                                 camera_heights=HEIGHT, camera_widths=WIDTH,
                                 render_gpu_device_id=0)
        try:
            video = (Path(args.video) if args.video and len(episodes) == 1
                     else out / "videos" / f"{args.policy}_T{ti + 1}-{ii}.mp4")
            res = run_episode(env, bench, policy, ti, ii,
                              seed=int(ep.get("seed", 1000)),
                              out=out, video=video,
                              max_steps=int(args.max_steps))
        finally:
            try:
                env.close()
            except Exception:  # noqa: BLE001
                pass
        tracks = res.pop("_tracks", {})
        results.append({"summary": res, "tracks": tracks})
        print(json.dumps(res, ensure_ascii=False))

    # 整批结果（含时序原始值）交给宿主侧判 Rung；容器内不做判定。
    (out / f"episodes_{args.policy}.json").write_text(
        json.dumps(results, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
