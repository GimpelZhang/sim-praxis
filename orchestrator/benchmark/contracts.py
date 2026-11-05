"""Task 3 契约层：B2 构造 / B3 契约 / B4 最小步进的判据纯函数（Plan T3.5 步骤 1）。

**只在出生验证子进程内被调用**：``birth.py`` 把候选文件按路径加载成 module
对象后传进来，本模块自己**绝不** ``import`` 候选代码，也绝不读写候选产物以外
的文件。判据全部是**结构性**的（存在性 / 可调用性 / 类型与有限性），不做物理
数值评分——那是 Task 4 独立评测器的事（「Birth ≠ Task Success」）。

三个契约对应三个任务（D3-3：由 ``task.yaml.birth_contract`` 的四个布尔决定
哪些层必需，契约本身只回答「这一层怎么算过」）：

- :class:`MujocoPushContract` —— M1，``python_script`` 入口，暴露 ``build_scene()``；
- :class:`LiberoAdapterContract` —— L1，``python_script`` 入口，暴露 ``build_env()``；
- :class:`ResearchDocContract` —— R1，``document_bundle``，四件作者产物。

契约**不发明**数值常量：``action_dim`` 等只做「正整型」结构判定；若调用方给了
期望值（``ctx["expect_action_dim"]`` / ``ctx["expect_observation_keys"]``，例如
来自 Task 2 的容器指纹或 task.yaml 声明），才做**精确相等**比对并把实测值写进
``observed``。没有期望值就如实记 ``None``，不拿结构判定冒充契约一致。
"""
from __future__ import annotations

import json
import numbers
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

__all__ = [
    "Contract",
    "ContractViolation",
    "LiberoAdapterContract",
    "MujocoPushContract",
    "ResearchDocContract",
    "CONTRACTS",
    "contract_by_name",
    "module_get",
]


@dataclass(frozen=True)
class ContractViolation:
    """一条契约不符（``code`` 机判、``detail`` 给人读）。"""

    code: str
    detail: str

    def to_json(self) -> dict:
        return {"code": self.code, "detail": self.detail}


def module_get(module: Any, name: str) -> Any:
    """从 runpy 的 dict 命名空间或 importlib 的 module 对象里取名字。"""
    if isinstance(module, Mapping):
        return module.get(name)
    return getattr(module, name, None)


def _is_int(value: Any) -> bool:
    """整数判定（评审 W2-13）。

    只认 Python ``int`` 会把 ``numpy.int64``（例如 ``np.prod(shape)`` 的返回值）
    判成契约不符，而候选只是写了完全合法的整数。接受 ``numbers.Integral``
    （不 import numpy），布尔仍在外部排除。
    """
    return isinstance(value, numbers.Integral) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def flatten_numbers(obj: Any, prefix: str = "") -> "dict[str, float]":
    """递归收集 dict/list 里的数值（B4 的 finite 与推进判定用）。

    兼容 numpy 数组/标量：非容器但带 ``tolist()`` 的对象先展平（不 import numpy，
    避免把数值库变成出生验证的硬依赖）。
    """
    out: "dict[str, float]" = {}
    if not isinstance(obj, (Mapping, list, tuple, str, bytes)) and hasattr(obj, "tolist"):
        try:
            obj = obj.tolist()
        except Exception:                                   # noqa: BLE001
            pass
    if isinstance(obj, Mapping):
        for key in obj:
            out.update(flatten_numbers(obj[key], f"{prefix}.{key}" if prefix else str(key)))
    elif isinstance(obj, (list, tuple)):
        for index, item in enumerate(obj):
            out.update(flatten_numbers(item, f"{prefix}[{index}]"))
    elif _is_number(obj):
        out[prefix or "value"] = float(obj)
    return out


class Contract:
    """契约基类：一个任务一组判据。子类只覆写数据与方法，不碰 I/O 策略。"""

    name: str = ""
    builder: str = ""
    needs_object: bool = True
    reset_required: bool = False
    author_outputs: "tuple[str, ...]" = ()
    description: str = ""

    # -- B2 -----------------------------------------------------------------
    def build(self, module: Any) -> Any:
        """调候选的构造入口；返回构造出的对象（异常由调用方归 construction_error）。"""
        factory: "Callable[..., Any] | None" = module_get(module, self.builder)
        if factory is None or not callable(factory):
            raise LookupError(f"候选模块未暴露可调用的构造入口 {self.builder}()")
        obj = factory()
        if obj is None:
            raise ValueError(f"{self.builder}() 返回 None")
        return obj

    # -- B3 -----------------------------------------------------------------
    def check(self, obj: Any, ctx: "Mapping[str, Any]") -> "list[ContractViolation]":
        """契约检查；返回空列表才算过。子类实现。"""
        raise NotImplementedError

    # -- B4 -----------------------------------------------------------------
    def reset(self, obj: Any, ctx: "Mapping[str, Any]") -> Any:
        return obj.reset(seed=0)

    def action(self, obj: Any, ctx: "Mapping[str, Any]") -> Any:
        dim = getattr(obj, "action_dim", None)
        if not _is_int(dim) or dim < 1:
            raise ValueError("action_dim 不可用，无法构造最小动作")
        return [0.0] * int(dim)

    def step(self, obj: Any, action: Any, ctx: "Mapping[str, Any]") -> Any:
        return obj.step(action)

    # -- 描述 ---------------------------------------------------------------
    def describe(self) -> dict:
        return {
            "name": self.name,
            "builder": self.builder,
            "needs_object": self.needs_object,
            "reset_required": self.reset_required,
            "author_outputs": list(self.author_outputs),
            "description": self.description,
        }


class MujocoPushContract(Contract):
    """M1：构造出可步进的场景对象；一次最小步进必须推进且全 finite。"""

    name = "mujoco_push"
    builder = "build_scene"
    needs_object = True
    reset_required = False
    description = "MuJoCo 场景对象：action_dim 正整数、step 可调用、dt 为正"

    def check(self, obj: Any, ctx: "Mapping[str, Any]") -> "list[ContractViolation]":
        out: "list[ContractViolation]" = []
        dim = getattr(obj, "action_dim", None)
        if not _is_int(dim) or dim < 1:
            out.append(ContractViolation(
                "action_dim", f"action_dim 必须是正整数，实测 {dim!r}"))
        if not callable(getattr(obj, "step", None)):
            out.append(ContractViolation("step", "对象缺少可调用的 step()"))
        dt = getattr(obj, "dt", None)
        if dt is not None and (not _is_number(dt) or float(dt) <= 0.0):
            out.append(ContractViolation("dt", f"dt 必须是正数，实测 {dt!r}"))
        expect = ctx.get("expect_action_dim")
        if expect is not None and dim != expect:
            out.append(ContractViolation(
                "expect_action_dim",
                f"action_dim 期望 {expect}，实测 {dim}"))
        return out


class LiberoAdapterContract(Contract):
    """L1：构造真实 Environment；reset → step 一次；观测键与动作维度结构合规。"""

    name = "libero_env_adapter"
    builder = "build_env"
    needs_object = True
    reset_required = True
    description = "LIBERO 环境对象：action_dim 正整数、observation_keys 非空字符串序列、reset/step 可调用"

    def check(self, obj: Any, ctx: "Mapping[str, Any]") -> "list[ContractViolation]":
        out: "list[ContractViolation]" = []
        dim = getattr(obj, "action_dim", None)
        if not _is_int(dim) or dim < 1:
            out.append(ContractViolation(
                "action_dim", f"action_dim 必须是正整数，实测 {dim!r}"))
        keys = getattr(obj, "observation_keys", None)
        if (not isinstance(keys, (tuple, list)) or not keys
                or not all(isinstance(key, str) and key for key in keys)):
            out.append(ContractViolation(
                "observation_keys",
                f"observation_keys 必须是非空字符串序列，实测 {keys!r}"))
        for method in ("reset", "step"):
            if not callable(getattr(obj, method, None)):
                out.append(ContractViolation(method, f"对象缺少可调用的 {method}()"))
        expect_dim = ctx.get("expect_action_dim")
        if expect_dim is not None and dim != expect_dim:
            out.append(ContractViolation(
                "expect_action_dim", f"action_dim 期望 {expect_dim}，实测 {dim}"))
        expect_keys = ctx.get("expect_observation_keys")
        if expect_keys is not None:
            observed = list(keys) if isinstance(keys, (tuple, list)) else None
            if observed != list(expect_keys):
                out.append(ContractViolation(
                    "expect_observation_keys",
                    f"observation_keys 期望 {list(expect_keys)}，实测 {observed}"))
        return out


class ResearchDocContract(Contract):
    """R1：document_bundle——没有可执行对象，B2/B4 天然不适用，B3 核验四件产物。"""

    name = "research_doc"
    builder = ""
    needs_object = False
    reset_required = False
    author_outputs = ("experiment_plan.yaml", "research_report.md",
                      "parameter_space.json", "evaluation_metrics.json")
    description = "研究设计方案四件产物：存在、非空、结构化文件可解析"

    def check(self, obj: Any, ctx: "Mapping[str, Any]") -> "list[ContractViolation]":
        workdir = Path(ctx["workdir"])
        out: "list[ContractViolation]" = []
        for name in self.author_outputs:
            path = workdir / name
            if not path.is_file():
                out.append(ContractViolation("missing", f"{name} 不在场"))
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if not text.strip():
                out.append(ContractViolation("too_small", f"{name} 为空文件"))
                continue
            if name.endswith((".yaml", ".yml")):
                try:
                    import yaml
                    data = yaml.safe_load(text)
                except Exception as exc:            # noqa: BLE001 - 归 contract_error
                    out.append(ContractViolation(
                        "parse", f"{name} YAML 解析失败：{exc}"))
                    continue
                if not isinstance(data, (dict, list)):
                    out.append(ContractViolation(
                        "parse", f"{name} 顶层必须是 mapping/sequence"))
            elif name.endswith(".json"):
                try:
                    json.loads(text)
                except Exception as exc:            # noqa: BLE001
                    out.append(ContractViolation(
                        "parse", f"{name} JSON 解析失败：{exc}"))
        return out


CONTRACTS: "dict[str, Contract]" = {
    MujocoPushContract.name: MujocoPushContract(),
    LiberoAdapterContract.name: LiberoAdapterContract(),
    ResearchDocContract.name: ResearchDocContract(),
}


def contract_by_name(name: str) -> Contract:
    try:
        return CONTRACTS[name]
    except KeyError:
        raise KeyError(
            f"未知契约 {name!r}；已登记：{sorted(CONTRACTS)}") from None
