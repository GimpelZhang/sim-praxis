"""环境卡 ENV_SPEC（Phase 2.7 Task 3 / D3-2，v1.3 三段式）。

背景：Phase 2.6 Task 4/4b 的十一轮盲跑里，模型在**不知道环境长什么样**的
前提下反复 `find /`、猜模块名、猜库版本，把轮次花在「摸环境」而不是
「解题」上（D27-5）。本模块把环境事实固化成一张确定性卡片注入 system
prompt，三段来源分明：

1. **静态段**（仓内可知，宿主侧）：解释器路径、pin 版本、资产清单、技能
   清单、允许透传的环境变量名；
2. **探针段**（容器内实测，`sandbox/probes/probe_env_scan.py` 产出）：
   实际装上的库版本、workdir 树、观测键、动作空间维度、官方 demo 入口；
3. **真值段**（v1.3 新增，`PrivilegedState`）：能否取到真值物体位姿与夹爪
   状态、取的入口、返回的字段名。

**真值段默认不渲染**：FAEA 的成功前提之一是特权真值状态（A-13），把它
默认写进 prompt 等于替模型完成了感知——那是另一种作弊。是否渲染由
``PrivilegedState.inject`` 门控，且**只在模式 N 且预注册写明**时才开
（BR-3 第 5 项 / AC-T3-9）；模式 R 一律不给。

纪律：本模块只做**事实登记与确定性渲染**，不探测、不联网、不读时间戳；
渲染结果对同一份输入逐字节相同（AC-T3-1 的「prompt 不随历史增长」由此
可证）。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

ENV_SPEC_SCHEMA = "simpraxis.env_spec.v1"
ENV_SPEC_NAME = "env_spec.json"
ENV_SPEC_PATH_ENV = "SIMPRAXIS_ENV_SPEC_PATH"

# 渲染预算（Plan §T3.4-B）：环境卡 ≤ 2,000 B。超了先删长列表，不得调大
# PROMPT_SOFT_BYTES / PROMPT_BUDGET_BYTES（Plan §T3.9 失败处理）。
ENV_SPEC_BLOCK_MAX_BYTES = 2_000

# workdir 树与清单的硬边界（防「环境卡自己把 prompt 撑爆」）。
WORKDIR_MAX_DEPTH = 3
WORKDIR_MAX_ENTRIES = 200

# 真值段标记：机判（AC-T3-9）按它做正则否定/肯定断言。
PRIVILEGED_MARKER = "[PRIVILEGED_STATE]"
ENV_SPEC_SECTION_MARKER = "环境卡 ENV_SPEC"

# 各列表在卡片里的展示上限（超出给「前 N 个 + 计数」，全文仍在 JSON 里）。
_SHOW_ASSETS = 8
_SHOW_SKILLS = 8
_SHOW_PACKAGES = 14
_SHOW_TREE = 24
_SHOW_OBS = 12


class EnvSpecError(ValueError):
    """环境卡结构非法（fail-closed：宁可拒绝，也不渲染一张说谎的卡片）。"""


def _str_tuple(value, what: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise EnvSpecError(f"{what} 必须是字符串数组：{value!r}")
    out = []
    for item in value:
        if not isinstance(item, str) or not item:
            raise EnvSpecError(f"{what} 的每项必须是非空字符串：{item!r}")
        out.append(item)
    return tuple(out)


@dataclass(frozen=True)
class PrivilegedState:
    """真值状态**可及性事实**（A-13 / BR-3 第 5 项）。

    只登记事实；「是否渲染进 prompt」由 ``inject`` 门控，渲染逻辑在
    :meth:`EnvSpec.render_block`——两者不混（Plan §T3.4-B 的 v1.4 修正）。

    ``available=False`` 时必须如实登记——取不到真值就是取不到，**不得**
    兜底填 True（AC-T3-9）。
    """

    available: bool
    accessor: str                    # "env.get_obs()" / "env.sim.get_state()" / "unavailable"
    returns: tuple[str, ...] = ()    # 实际字段名
    inject: bool = False             # 仅模式 N 且预注册写明 True 时才渲染

    def __post_init__(self) -> None:
        if type(self.available) is not bool:
            raise EnvSpecError("privileged_state.available 必须是 bool")
        if not isinstance(self.accessor, str) or not self.accessor:
            raise EnvSpecError("privileged_state.accessor 必须是非空字符串")
        object.__setattr__(self, "returns", _str_tuple(self.returns, "returns"))
        if type(self.inject) is not bool:
            raise EnvSpecError("privileged_state.inject 必须是 bool")
        if self.inject and not self.available:
            # 「注入一段并不存在的真值」是自相矛盾的配置，装配期就拒。
            raise EnvSpecError(
                "privileged_state.inject=True 但 available=False："
                "不能渲染不存在的真值状态")

    def to_json(self) -> dict:
        return {"available": self.available, "accessor": self.accessor,
                "returns": list(self.returns), "inject": self.inject}

    @classmethod
    def from_json(cls, doc: dict) -> "PrivilegedState":
        if not isinstance(doc, dict):
            raise EnvSpecError(f"privileged_state 必须是对象：{doc!r}")
        for key in ("available", "accessor"):
            if key not in doc:
                raise EnvSpecError(f"privileged_state 缺字段 {key}")
        return cls(available=doc["available"], accessor=doc["accessor"],
                   returns=doc.get("returns") or [],
                   inject=bool(doc.get("inject", False)))


@dataclass(frozen=True)
class EnvSpec:
    """环境卡（三段式的容器）。字段默认值全部在后，保证可构造（v1.4 修正）。"""

    python: str                          # 容器内解释器绝对路径
    packages: dict = field(default_factory=dict)   # {"numpy": "1.26.4", ...}
    workdir_tree: tuple = ()             # 深度 <= 3、条数 <= 200
    assets: tuple = ()                   # Task 1 manifest 的 name
    skills: tuple = ()                   # Task 2 manifest 的 name
    obs_keys: tuple = ()                 # 探针段：reset() 返回的键
    action_dim: "int | None" = None      # 探针段
    privileged_state: PrivilegedState = field(
        default_factory=lambda: PrivilegedState(
            available=False, accessor="unavailable"))
    demo_entry: "str | None" = None      # 官方 demo 路径
    env_allowlist: tuple = ()            # run_cmd 允许透传的变量名
    host_python: "str | None" = None     # 宿主主 venv 解释器（静态段）

    def __post_init__(self) -> None:
        if not isinstance(self.python, str) or not self.python:
            raise EnvSpecError("python 必须是非空字符串（容器内解释器绝对路径）")
        if not isinstance(self.packages, dict):
            raise EnvSpecError("packages 必须是映射")
        for name, ver in self.packages.items():
            if not isinstance(name, str) or not name:
                raise EnvSpecError(f"packages 的键必须是非空字符串：{name!r}")
            if not isinstance(ver, str) or not ver:
                raise EnvSpecError(f"packages[{name}] 必须是非空字符串版本")
        tree = _str_tuple(self.workdir_tree, "workdir_tree")
        if len(tree) > WORKDIR_MAX_ENTRIES:
            raise EnvSpecError(
                f"workdir_tree 最多 {WORKDIR_MAX_ENTRIES} 条，收到 {len(tree)}")
        for item in tree:
            depth = item.strip("/").count("/") + 1
            if depth > WORKDIR_MAX_DEPTH:
                raise EnvSpecError(
                    f"workdir_tree 条目深度 {depth} 超过 {WORKDIR_MAX_DEPTH}：{item!r}")
        object.__setattr__(self, "workdir_tree", tree)
        object.__setattr__(self, "assets", _str_tuple(self.assets, "assets"))
        object.__setattr__(self, "skills", _str_tuple(self.skills, "skills"))
        object.__setattr__(self, "obs_keys", _str_tuple(self.obs_keys, "obs_keys"))
        object.__setattr__(self, "env_allowlist",
                           _str_tuple(self.env_allowlist, "env_allowlist"))
        dim = self.action_dim
        if dim is not None and (type(dim) is not int or isinstance(dim, bool)
                                or dim <= 0):
            raise EnvSpecError(f"action_dim 必须是正整数或 None：{dim!r}")
        if self.demo_entry is not None and (
                not isinstance(self.demo_entry, str) or not self.demo_entry):
            raise EnvSpecError("demo_entry 必须是非空字符串或 None")
        if not isinstance(self.privileged_state, PrivilegedState):
            raise EnvSpecError("privileged_state 必须是 PrivilegedState")

    # ---------------------------------------------------------------- render

    def render_block(self, *, mode: "str | None" = None) -> str:
        """渲染喂 prompt 的纯文本卡片（<= 2,000 B；确定性顺序）。

        真值段**只有** ``privileged_state.inject`` 为真**且** ``mode == "N"``
        时出现（BR-3 第 5 项）。模式 R / 未声明模式一律不给。
        """
        lines = [f"{ENV_SPEC_SECTION_MARKER}（容器实测 + 仓内静态；"
                 f"解释器 {self.python}）"]
        if self.host_python:
            lines.append(f"- 宿主主 venv：{self.host_python}")
        if self.packages:
            names = sorted(self.packages)
            shown = names[:_SHOW_PACKAGES]
            text = ", ".join(f"{n}=={self.packages[n]}" for n in shown)
            if len(names) > len(shown):
                text += f"（另 {len(names) - len(shown)} 个未展开）"
            lines.append(f"- 已装库：{text}")
        if self.workdir_tree:
            shown = list(self.workdir_tree[:_SHOW_TREE])
            tail = (f"（共 {len(self.workdir_tree)} 条，深度 <= {WORKDIR_MAX_DEPTH}）"
                    if len(self.workdir_tree) > len(shown) else
                    f"（共 {len(self.workdir_tree)} 条，深度 <= {WORKDIR_MAX_DEPTH}）")
            lines.append(f"- workdir 树{tail}：")
            lines.extend(f"    {item}" for item in shown)
        if self.assets:
            shown = self.assets[:_SHOW_ASSETS]
            suffix = (f"（共 {len(self.assets)} 件）" if len(self.assets) > len(shown)
                      else f"（{len(self.assets)} 件）")
            lines.append(f"- 资产{suffix}：{', '.join(shown)}")
        if self.skills:
            shown = self.skills[:_SHOW_SKILLS]
            suffix = (f"（共 {len(self.skills)} 个）" if len(self.skills) > len(shown)
                      else f"（{len(self.skills)} 个）")
            lines.append(f"- 技能{suffix}：{', '.join(shown)}")
        if self.obs_keys:
            shown = self.obs_keys[:_SHOW_OBS]
            lines.append(f"- 观测键：{', '.join(shown)}")
        if self.action_dim is not None:
            lines.append(f"- 动作维度：{self.action_dim}")
        if self.demo_entry:
            lines.append(f"- 官方 demo：{self.demo_entry}")
        if self.env_allowlist:
            lines.append(f"- 可透传环境变量：{', '.join(self.env_allowlist)}")

        if self.privileged_state.inject and mode == "N":
            ps = self.privileged_state
            rets = ", ".join(ps.returns) if ps.returns else "(未登记字段)"
            lines.append(
                f"{PRIVILEGED_MARKER} 真值状态可用：accessor={ps.accessor}；"
                f"returns={rets}（仅模式 N 注入，模式 R 一律不给）")

        block = "\n".join(lines)
        size = len(block.encode("utf-8"))
        if size > ENV_SPEC_BLOCK_MAX_BYTES:
            raise EnvSpecError(
                f"环境卡 {size} B 超过上限 {ENV_SPEC_BLOCK_MAX_BYTES} B："
                "先删长列表（只留前 N 个 + 计数），不得调大 prompt 预算")
        return block

    # ------------------------------------------------------------ serialize

    def to_json(self) -> dict:
        return {
            "schema": ENV_SPEC_SCHEMA,
            "python": self.python,
            "host_python": self.host_python,
            "packages": dict(self.packages),
            "workdir_tree": list(self.workdir_tree),
            "assets": list(self.assets),
            "skills": list(self.skills),
            "obs_keys": list(self.obs_keys),
            "action_dim": self.action_dim,
            "privileged_state": self.privileged_state.to_json(),
            "demo_entry": self.demo_entry,
            "env_allowlist": list(self.env_allowlist),
        }

    @classmethod
    def from_json(cls, doc: dict) -> "EnvSpec":
        if not isinstance(doc, dict):
            raise EnvSpecError(f"环境卡必须是对象：{doc!r}")
        if doc.get("schema") != ENV_SPEC_SCHEMA:
            raise EnvSpecError(
                f"环境卡 schema 必须是 {ENV_SPEC_SCHEMA}，"
                f"收到 {doc.get('schema')!r}")
        if "python" not in doc:
            raise EnvSpecError("环境卡缺 python 字段")
        return cls(
            python=doc["python"], host_python=doc.get("host_python"),
            packages=dict(doc.get("packages") or {}),
            workdir_tree=tuple(doc.get("workdir_tree") or ()),
            assets=tuple(doc.get("assets") or ()),
            skills=tuple(doc.get("skills") or ()),
            obs_keys=tuple(doc.get("obs_keys") or ()),
            action_dim=doc.get("action_dim"),
            privileged_state=PrivilegedState.from_json(
                doc.get("privileged_state")
                or {"available": False, "accessor": "unavailable"}),
            demo_entry=doc.get("demo_entry"),
            env_allowlist=tuple(doc.get("env_allowlist") or ()))


def env_spec_path() -> Path:
    """环境卡路径：``$SIMPRAXIS_ENV_SPEC_PATH`` 优先，缺省落在 phase27 根下。

    与 Plan §T3.7 的证据树一致：``<phase27>/task3_elastic_loop/env_spec.json``。
    """
    override = os.environ.get(ENV_SPEC_PATH_ENV)
    if override:
        return Path(override)
    from orchestrator.common.runtime_paths import phase27_root

    return phase27_root() / "task3_elastic_loop" / ENV_SPEC_NAME


def load_env_spec(path: "str | Path | None" = None) -> EnvSpec:
    """读环境卡并逐字段校验；缺件/坏件抛 ``EnvSpecError``（调用方决定降级）。"""
    target = Path(path) if path is not None else env_spec_path()
    if not target.is_file():
        raise EnvSpecError(f"环境卡不存在：{target}")
    try:
        doc = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise EnvSpecError(f"环境卡无法解析：{target}：{exc}") from None
    return EnvSpec.from_json(doc)


def static_env_spec(*, skills: "tuple[str, ...]" = (),
                    assets: "tuple[str, ...]" = ()) -> EnvSpec:
    """静态段兜底（探针缺件时的降级形态）：只写仓内可知事实。

    Plan §T3.4-G：``env_spec.json`` 缺件时**降级为静态段**并在 trace 打
    ``env_spec_missing``，不 fail 整个 run。降级卡片不含任何探针/真值内容。
    """
    return EnvSpec(python="/usr/bin/python",
                   host_python="/mnt/venvs/simpraxis/bin/python",
                   assets=tuple(assets), skills=tuple(skills),
                   privileged_state=PrivilegedState(
                       available=False, accessor="unavailable"))
