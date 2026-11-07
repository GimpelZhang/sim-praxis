"""Phase 2.8.1 T6：**干跑**（0 付费）——罐头候选走完整条 Trial 流水线。

干跑要回答的问题只有一个：**流水线本身通不通**。它不回答任何能力问题，因此
本模块做三件事，且只做这三件事：

1. :class:`CannedGateway` —— 自证身份的罐头网关（``guard_of`` +
   ``canned=True``），按 ``(stage, 第几次)`` 返回预置文本。它在真网关的位置上，
   由 ``trial_harness.run_trial`` 在 ``dry_run=True`` 时**只**替换 ``gateway``
   这一步采信（计划 §10.3）：别的什么都不降级——目录、TASK.md、出生判定、交付
   冻结、清单、三源遥测全部走生产路径。
2. 每个任务一份**结构合法**的罐头候选：M1 暴露 ``build_scene()``、L1 暴露
   ``build_env()``、R1 是四件文档。它们的唯一目标是让 B0–B4 与产物面真的
   跑起来，而不是拿分。
3. :func:`stamp_tree` —— 把干跑产物树里**每一个** ``*.json`` 就地打上
   ``"simulated": true`` 与 ``"simulated_reason"``，并落 ``simulated_index.json``
   （逐件记打标前后的 sha256）。下游分析只认这一枚标记：任何一条干跑数字都不许
   混进 P281-9 的报告。:func:`load_real` 就是那道闸——读到模拟件即抛
   :class:`AnalysisRefused`，**不做降级、不做静默过滤**。

产物落在 ``<root>/dryrun/<UTC 时间戳>/``（``paths.dryrun_dir()``），Trial 目录
落其下的 ``runs/`` 子目录：真实 ``<root>/runs/`` **一个字节都不动**（AC-09）。
账本视角是一条**不存在的空账文件路径**：干跑 0 实呼，不读也不写任何真实账。
"""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from orchestrator.benchmark.phase281 import arm_adapter, constants
from orchestrator.common.fsutil import atomic_write_text, ensure_private_dir

__all__ = (
    "AnalysisRefused", "CannedExhausted", "CannedGateway", "DRYRUN_SUBDIR",
    "SIMULATED_REASON", "StampError", "agentic_reply", "assert_real",
    "files_for", "legacy_reply", "load_real", "run_all", "stamp_tree",
)

#: 干跑产物子目录名（``paths.dryrun_dir()`` 同一字符串）
DRYRUN_SUBDIR = "dryrun"
#: 打标理由逐字取自计划 §10.3（英文原文；不要翻译，机判按字面比）
SIMULATED_REASON = "canned gateway; not a model output"
#: 干跑账本视图的文件名。**父目录不建、文件不写**：``read_lines`` 对不存在的
#: 路径返回空账，于是 TrialBudget 与三源遥测看到的都是「0 实呼」这一真事实。
LEDGER_VIEW = "dryrun_ledger.jsonl"


class CannedExhausted(RuntimeError):
    """罐头回复用完了。**抛**而不是循环复用：缺条数就是缺，不许静默糊过去。"""


class AnalysisRefused(RuntimeError):
    """读到干跑（模拟）产物：它不是模型输出，任何分析都必须拒绝。"""


# ---------------------------------------------------------------------------
# 罐头网关
# ---------------------------------------------------------------------------

class CannedGateway:
    """零网络网关：按 ``(stage, 序号)`` 吐预置文本，用量恒 ``(0, 0)``。

    形状与 ``ModelGateway`` 的 ``call`` 对齐（``(text, (tokens_in, tokens_out))``），
    并自证身份：``guard_of`` 与本模块路径一致、``canned is True``。``run_trial``
    在干跑时只认这两条都成立的对象，其余一律换成 ``_DryRunGateway``（空补丁）。
    """

    guard_of = "phase281._CannedGateway"
    canned = True

    def __init__(self, replies: Mapping[str, Sequence[str]]) -> None:
        self._replies = {str(k): list(v) for k, v in replies.items()}
        self._counts: "dict[str, int]" = {}
        self.calls = 0
        self.history: "list[tuple[str, str]]" = []

    def model_id(self, stage: str) -> str:
        return "dry-run"

    def call(self, stage: str, messages: Any = None, **kwargs: Any
             ) -> "tuple[str, tuple[int, int]]":
        stage = str(stage)
        index = self._counts.get(stage, 0)
        self._counts[stage] = index + 1
        self.calls += 1
        sequence = self._replies.get(stage) or []
        if index >= len(sequence):
            raise CannedExhausted(
                f"罐头网关没有第 {index + 1} 条 {stage} 回复"
                f"（预置 {len(sequence)} 条）")
        text = str(sequence[index])
        self.history.append((stage, text))
        return text, (0, 0)

    def call_visual(self, *args: Any, **kwargs: Any) -> Any:
        raise CannedExhausted("干跑不提供视觉通道：三个任务都不发 image block")

    def __getattr__(self, name: str) -> Any:      # pragma: no cover - 防御
        raise AttributeError(f"罐头网关没有 {name!r}")


# ---------------------------------------------------------------------------
# 三个任务的罐头候选（结构合法，不求高分）
# ---------------------------------------------------------------------------

M1_SOLUTION = '''\
"""干跑罐头 M1：结构合法的合成轨迹（simulated；不是模型产出）。"""
import argparse
import json
import math
from pathlib import Path


class _Scene:
    action_dim = 2
    dt = 0.002

    def step(self, action):
        gain = 0.01 + (float(action[0]) if action else 0.0)
        return {"cube_pos": [round(0.05 + gain, 6), 0.02, 0.02],
                "target_pos": [0.30, 0.20, 0.02],
                "max_penetration": 0.0,
                "contact_force_norm": 0.0}


def build_scene():
    return _Scene()


def _rows(steps=8):
    rows = []
    for index in range(steps):
        rows.append({
            "step": index,
            "t": round(index * 0.002, 6),
            "cube_pos": [round(0.05 + 0.01 * index, 6), 0.02, 0.02],
            "target_pos": [0.30, 0.20, 0.02],
            "max_penetration": 0.0,
            "contact_force_norm": 0.0,
        })
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", default=".")
    parser.add_argument("--seed", default="0")
    args = parser.parse_args()
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    rows = _rows()
    (out / "telemetry.jsonl").write_text(
        "".join(json.dumps(row, sort_keys=True) + "\\n" for row in rows),
        encoding="utf-8")
    last = rows[-1]
    metrics = {
        "endpoint_distance_m": round(math.hypot(
            last["cube_pos"][0] - last["target_pos"][0],
            last["cube_pos"][1] - last["target_pos"][1]), 6),
        "max_penetration_m": max(row["max_penetration"] for row in rows),
        "contact_force_peak_n": max(row["contact_force_norm"] for row in rows),
        "steps": len(rows),
    }
    (out / "metrics.json").write_text(
        json.dumps(metrics, sort_keys=True) + "\\n", encoding="utf-8")
    (out / "scene_spec.json").write_text(
        json.dumps({"action_dim": 2, "dt": 0.002, "objects": ["cube", "target"]},
                   sort_keys=True) + "\\n", encoding="utf-8")


if __name__ == "__main__":
    main()
'''

L1_ADAPTER = '''\
"""干跑罐头 L1：结构合法的假适配器（simulated；不是模型产出）。"""
import argparse
import json
import platform
import sys
from pathlib import Path

_OBS_KEYS = ("robot0_eef_pos", "robot0_eef_quat", "robot0_gripper_qpos",
             "robot0_joint_pos", "agentview_image", "object-state")


class _Env:
    action_dim = 7
    observation_keys = list(_OBS_KEYS)

    def reset(self, seed=0):
        self._t = 0
        return {"state": [0.10, 0.0, 0.0]}

    def step(self, action):
        self._t += 1
        return {"state": [round(0.10 + 0.01 * self._t, 6), 0.0, 0.0],
                "reward": 0.0}


def build_env():
    return _Env()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", default=".")
    parser.add_argument("--seed", default="0")
    parser.add_argument("--chown-uid", default=None)
    parser.add_argument("--chown-gid", default=None)
    args, _unknown = parser.parse_known_args()
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    env = build_env()
    probe = {
        "schema": "simpraxis.phase281.canned_env_probe.v1",
        "action_dim": env.action_dim,
        "observation_keys": list(env.observation_keys),
        "python_executable": sys.executable,
        "python_version": platform.python_version(),
        "captured_in": "container",
    }
    (out / "env_probe.json").write_text(
        json.dumps(probe, sort_keys=True) + "\\n", encoding="utf-8")
    env.reset(seed=0)
    state = env.step([0.0] * env.action_dim)
    one_step = {
        "schema": "simpraxis.phase281.canned_one_step.v1",
        "action": [0.0] * env.action_dim,
        "state": state,
        "advanced": True,
    }
    (out / "one_step_result.json").write_text(
        json.dumps(one_step, sort_keys=True) + "\\n", encoding="utf-8")


if __name__ == "__main__":
    main()
'''

R1_PLAN = """\
schema: simpraxis.phase281.canned_research_plan.v1
simulated: true
simulated_reason: canned gateway; not a model output

parameter_definitions:
  friction:
    unit: dimensionless
    range: [0.2, 1.2]
  contact_stiffness:
    unit: N/m
    range: [200, 2000]
  joint_damping:
    unit: N*m*s/rad
    range: [0.1, 2.0]

probability_distributions:
  friction: uniform
  contact_stiffness: loguniform
  joint_damping: uniform

parameter_correlations:
  - friction 与 contact_stiffness 弱正相关，仅作假设，未在数据上估计

randomization_method: 逐 episdoe 采样，固定场景几何
sampling_strategy: 分层采样，每层等量
curriculum: 由低摩擦到高摩擦，四个阶段
dataset_splits:
  train: 60%
  validation: 20%
  holdout: 20%
random_seeds: [0, 1, 2]
metrics:
  success_rate: 成功率
  slip_distance_m: 滑移距离
ablation_study: 逐参数组开关，共四组
failure_taxonomy: 打滑 / 失控 / 超时
sim_to_real_risks: 传感器噪声与执行器延迟未在仿真中建模
conclusion_criteria: 仅以 holdout 上的原始计数判断方向
"""

R1_REPORT = """\
# 冰面域随机化研究设计（干跑罐头）

simulated: true
simulated_reason: canned gateway; not a model output

本文件是干跑罐头，用于验证流水线与证据面，不构成任何研究结论。
参数定义、分布、相关性假设、随机化方法、采样、课程、切分、种子、指标、消融、
失败分类、Sim-to-Real 风险与结论判定标准见 `experiment_plan.yaml`。
"""

R1_PARAMETERS = """\
{
  "simulated": true,
  "simulated_reason": "canned gateway; not a model output",
  "parameter_space": {
    "friction": {"unit": "dimensionless", "low": 0.2, "high": 1.2},
    "contact_stiffness": {"unit": "N/m", "low": 200, "high": 2000},
    "joint_damping": {"unit": "N*m*s/rad", "low": 0.1, "high": 2.0}
  }
}
"""

R1_METRICS = """\
{
  "simulated": true,
  "simulated_reason": "canned gateway; not a model output",
  "metrics": {
    "success_rate": 0.0,
    "slip_distance_m": 0.0
  }
}
"""


def files_for(task_id: str) -> "dict[str, str]":
    """罐头候选要落进工作根的文件（相对路径 → 文本）。"""
    if task_id == "mujoco_contact_push_v1":
        return {"solution.py": M1_SOLUTION}
    if task_id == "libero_env_adapter_v1":
        return {"adapter.py": L1_ADAPTER}
    if task_id == "icy_ground_dr_v1":
        return {"experiment_plan.yaml": R1_PLAN,
                "research_report.md": R1_REPORT,
                "parameter_space.json": R1_PARAMETERS,
                "evaluation_metrics.json": R1_METRICS}
    raise ValueError(f"没有罐头候选的任务：{task_id!r}")


def legacy_reply(task_id: str) -> str:
    """旧引擎的一轮回复：一份建立文件的补丁集（``old=""`` 即新建）。"""
    patches = [{"path": rel, "old": "", "new": text}
               for rel, text in files_for(task_id).items()]
    return json.dumps({"patches": patches}, ensure_ascii=False)


def _container_python() -> str:
    return "/opt/venv/bin/python"


def agentic_reply(task_id: str, *, seed: str) -> str:
    """agentic 臂的一步回复：写文件 → （M1/L1）自跑一次 → ``finish``。

    写成**一条 markdown python 块**：smolagents 的每个 step 执行一个代码块，
    三件事放在同一块里就只需一步。``write_file`` 的 content 用 ``json.dumps``
    生成字面量（JSON 转义是合法的 Python 字面量）。
    """
    lines = []
    for rel, text in files_for(task_id).items():
        lines.append(f"write_file(path={rel!r}, content={json.dumps(text)})")
    entry = {"mujoco_contact_push_v1": "solution.py",
             "libero_env_adapter_v1": "adapter.py"}.get(task_id)
    if entry:
        argv = [_container_python(), entry, "--out-dir", "."]
        if task_id == "libero_env_adapter_v1":
            argv += ["--seed", str(seed), "--chown-uid", "0",
                     "--chown-gid", "0"]
        else:
            argv += ["--seed", str(seed)]
        lines.append(f"run_cmd(argv={json.dumps(argv)}, timeout_s=600)")
    lines.append('finish(answer="干跑罐头完成", reason="canned gateway")')
    body = "\n".join(lines)
    return f"```python\n{body}\n```\n"


# ---------------------------------------------------------------------------
# 跑一遍
# ---------------------------------------------------------------------------

def _stamp_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def run_all(*, root: "Path | None" = None,
            tasks: "Iterable[str] | None" = None,
            arms: "Iterable[str] | None" = None,
            rep: int = 0,
            stamp: "str | None" = None,
            runner: Callable[..., Any] = subprocess.run,
            sandbox_factory: "Callable[..., Any] | None" = None,
            engine_runner: "Callable[..., Any] | None" = None,
            birth_runner: "Callable[..., Any] | None" = None,
            stamp_tree_after: bool = True,
            **trial_kwargs: Any) -> dict:
    """跑 3 任务 × 2 臂（缺省 rep0），产物落在 ``<root>/dryrun/<stamp>/``。

    ``root`` 缺省取 ``paths.phase281_root()``（数据根）；给定 ``root`` 时**不**碰
    真数据根的任何文件——干跑试验全部落在 ``<root>/dryrun/...`` 之下。

    ``trial_kwargs`` 原样透传给 :func:`trial_harness.run_trial`（测试用它注入
    合成技能包 / 出生桩 / 运行时证明），**不**允许覆盖试次身份与网关参数——
    那些由本函数决定，覆盖它们等于让调用方偷偷另起一条流水线。
    """
    from . import paths as paths_mod
    from . import trial_harness
    from . import candidate_birth

    allowed = {"capture", "pre_seed", "skills_rows", "assets_dir"}
    illegal = sorted(set(trial_kwargs) - allowed)
    if illegal:
        raise TypeError("run_all 不接受这些透传参数：" + repr(illegal))

    root_dir = (Path(root) if root is not None
                else paths_mod.phase281_root())
    stamp = stamp or _stamp_now()
    out = ensure_private_dir(Path(root_dir) / DRYRUN_SUBDIR / stamp)
    ledger_view = Path(out) / LEDGER_VIEW           # 故意不创建：空账视图

    wanted_tasks = tuple(tasks) if tasks is not None else constants.TASK_IDS
    wanted_arms = tuple(arms) if arms is not None else constants.ARMS
    unknown = [t for t in wanted_tasks if t not in constants.TASK_IDS]
    if unknown:
        raise ValueError(f"未知任务：{unknown}")
    unknown = [a for a in wanted_arms if a not in constants.ARMS]
    if unknown:
        raise ValueError(f"未知臂：{unknown}")

    trials: "list[dict]" = []
    for task_id in wanted_tasks:
        spec = candidate_birth.load_task(task_id)
        seed = str(trial_harness.seed_int(trial_harness.pair_seed(task_id, rep)))
        for arm_id in wanted_arms:
            replies = {"coding_debug": _replies_for(task_id, arm_id,
                                                    seed=seed)}
            gateway = CannedGateway(replies)
            outcome = trial_harness.run_trial(
                spec=spec, arm_id=arm_id, rep=rep, root=out,
                gateway=gateway, ledger=ledger_view, dry_run=True,
                runner=runner, sandbox_factory=sandbox_factory,
                engine_runner=engine_runner, birth_runner=birth_runner,
                **trial_kwargs)
            trials.append({
                "trial_id": outcome.trial_id,
                "task_id": outcome.task_id,
                "arm_id": outcome.arm_id,
                "status": outcome.status,
                "used_calls": outcome.used_calls,
                "canned_calls": int(gateway.calls),
                "trial_dir": str(outcome.trial_dir),
            })

    summary = {
        "schema": "simpraxis.phase281.dryrun_summary.v1",
        "simulated": True,
        "simulated_reason": SIMULATED_REASON,
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "stamp": stamp,
        "root": str(out),
        "rep": int(rep),
        "ledger_view": str(ledger_view),
        "ledger_rows": 0,
        "trials": trials,
    }
    atomic_write_text(Path(out) / "dryrun_summary.json",
                      json.dumps(summary, ensure_ascii=False, indent=2,
                                 sort_keys=True) + "\n", 0o600)
    if stamp_tree_after:
        summary["stamp"] = dict(stamp_tree(out))
    return summary


def _replies_for(task_id: str, arm_id: str, *, seed: str) -> "list[str]":
    """同一份预置回复重复三次：引擎若需要更多轮，说明罐头本身不够用。"""
    arm = arm_adapter.ARMS[arm_id]
    if bool(getattr(arm, "runspec_agentic", False)):
        return [agentic_reply(task_id, seed=seed)] * 3
    return [legacy_reply(task_id)] * 3


# ---------------------------------------------------------------------------
# 打标与读闸
# ---------------------------------------------------------------------------

def _iter_json(tree: "Path | str") -> "list[Path]":
    base = Path(tree)
    return sorted(path for path in base.rglob("*.json")
                  if path.is_file() and not path.is_symlink())


def stamp_tree(tree: "Path | str") -> dict:
    """把树里每个 ``*.json`` 就地打上 ``simulated`` 标记，并写索引件。

    **这枚标记是毒丸，不是装饰**（计划 §10.3 的「每件顶层」逐字要求）：它同样
    打进 ``delivered/`` 里候选冻下来的产物件，于是 ``run_manifest`` 里登记的
    sha256 绑定、``runtime_fingerprint`` 的自封印、``delivered/`` 与清单之间的
    逐件对账**统统故意失效**。这正是要的效果——干跑产物**不该**能被任何下游
    当作真实证据读：真要是某条路径漏了闸，它会先撞上「清单对不上」而炸掉，
    而不是安静地把罐头数字算成能力指标。允许读它的唯一入口是
    :func:`load_real`（读到即 :class:`AnalysisRefused`）。

    顶层不是对象的 JSON（列表/标量）一律 :class:`StampError`——那种件不可能
    携带标记，放过它等于让模拟数字带着「没标记」混进分析。
    """
    base = Path(tree)
    entries: "list[dict]" = []
    for path in _iter_json(base):
        raw = path.read_bytes()
        try:
            doc = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise StampError(f"{path} 不是合法 JSON：{exc}") from exc
        if not isinstance(doc, dict):
            raise StampError(
                f"{path} 顶层不是对象（{type(doc).__name__}），无法打标")
        before = _sha256(raw)
        doc["simulated"] = True
        doc["simulated_reason"] = SIMULATED_REASON
        body = json.dumps(doc, ensure_ascii=False, indent=2,
                          sort_keys=True) + "\n"
        atomic_write_text(path, body, 0o600)
        entries.append({
            "path": path.relative_to(base).as_posix(),
            "pre_stamp_sha256": before,
            "post_stamp_sha256": _sha256(body.encode("utf-8")),
        })
    index = {
        "schema": "simpraxis.phase281.dryrun_simulated_index.v1",
        "simulated": True,
        "simulated_reason": SIMULATED_REASON,
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "root": str(base),
        "files": entries,
    }
    atomic_write_text(base / "simulated_index.json",
                      json.dumps(index, ensure_ascii=False, indent=2,
                                 sort_keys=True) + "\n", 0o600)
    return {"tree": str(base), "files": len(entries)}


def _sha256(raw: bytes) -> str:
    import hashlib

    return hashlib.sha256(raw).hexdigest()


class StampError(RuntimeError):
    """树里有无法打标的 JSON 件（顶层非对象/非法 JSON）。"""


def load_real(path: "Path | str") -> Any:
    """读一份**真实**产物；干跑件一律拒读。

    判定不看路径、只看内容：``simulated is True`` 或存在
    ``simulated_reason`` 即拒。路径闸由 :func:`assert_real` 另行提供——两道
    闸各自独立，任一条命中都拒绝。
    """
    target = Path(path)
    if target.is_symlink():
        raise AnalysisRefused(f"{target} 是符号链接：分析面不跟随链接")
    if not target.is_file():
        raise AnalysisRefused(f"{target} 不是文件")
    doc = json.loads(target.read_text(encoding="utf-8"))
    if isinstance(doc, dict) and (doc.get("simulated") is True
                                  or "simulated_reason" in doc):
        raise AnalysisRefused(
            f"{target} 是干跑（模拟）产物：它不是模型输出，禁止进分析")
    return doc


def assert_real(root: "Path | str") -> Path:
    """路径闸：任何疑为干跑树的位置都拒绝（``dryrun/`` 或带 ``SIMULATED.json``）。"""
    base = Path(root)
    for parent in [base, *base.parents]:
        if parent.name == DRYRUN_SUBDIR:
            raise AnalysisRefused(f"{base} 位于 {DRYRUN_SUBDIR}/ 之下：禁止分析")
    if base.is_dir() and any(base.rglob("SIMULATED.json")):
        raise AnalysisRefused(f"{base} 含 SIMULATED.json：禁止分析")
    return base
