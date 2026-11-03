"""声明式场景规格（D1-4）：只有数据，没有引擎调用。

宿主进程 import genesis 会**段错误**（2026-11-03 实测，核心已转储），
所以场景装配不能在本体里写 ``import genesis``。本模块产出的
``SceneSpec`` 是一份 JSON，由 ``sandbox/probes/probe_scene_build.py``
在容器内读取并在 Genesis 里实例化。模型的动作因此收敛为三件：
选资产、定位姿、配参数。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

SCENE_SPEC_SCHEMA = "simpraxis.scene_spec.v1"
ENGINES = ("genesis", "mujoco")


class SceneSpecError(ValueError):
    """spec 结构非法（模型可修错：写回一条具体字段的拒绝观察即可）。"""


def _triple(value, field_name: str) -> "tuple[float, float, float]":
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise SceneSpecError(f"{field_name} 必须是 3 元数组（x,y,z）：{value!r}")
    out = []
    for item in value:
        if not isinstance(item, (int, float)) or isinstance(item, bool):
            raise SceneSpecError(f"{field_name} 分量必须是数字：{value!r}")
        out.append(float(item))
    return (out[0], out[1], out[2])


def _quat(value, field_name: str) -> "tuple[float, float, float, float]":
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise SceneSpecError(f"{field_name} 必须是 4 元数组（w,x,y,z）：{value!r}")
    out = []
    for item in value:
        if not isinstance(item, (int, float)) or isinstance(item, bool):
            raise SceneSpecError(f"{field_name} 分量必须是数字：{value!r}")
        out.append(float(item))
    norm = sum(v * v for v in out) ** 0.5
    if abs(norm - 1.0) > 1e-6:
        raise SceneSpecError(f"{field_name} 必须是单位四元数（当前模长 {norm:.9f}）")
    return (out[0], out[1], out[2], out[3])


@dataclass(frozen=True)
class Body:
    """一件资产的位姿（资产名必须来自 Asset Zoo manifest）。"""

    asset: str
    pos: "tuple[float, float, float]"
    # 四元数按 **(w, x, y, z)**（Genesis `morphs.*(quat=…)` 的口径），
    # 单位元是 (1,0,0,0)。写成 xyzw 的 (0,0,0,1) 会被 Genesis 读成
    # 「绕 z 轴 180°」——位姿静默错转，且固定件不会因物理纠正暴露。
    quat: "tuple[float, float, float, float]" = (1.0, 0.0, 0.0, 0.0)
    fixed: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.asset, str) or not self.asset:
            raise SceneSpecError(f"asset 必须是非空字符串：{self.asset!r}")
        object.__setattr__(self, "pos", _triple(self.pos, "pos"))
        object.__setattr__(self, "quat", _quat(self.quat, "quat"))
        if not isinstance(self.fixed, bool):
            raise SceneSpecError(f"fixed 必须是 bool：{self.fixed!r}")

    def to_json(self) -> dict:
        return {"asset": self.asset, "pos": list(self.pos),
                "quat": list(self.quat), "fixed": self.fixed}

    @classmethod
    def from_json(cls, doc: dict) -> "Body":
        if not isinstance(doc, dict):
            raise SceneSpecError(f"body 必须是对象：{doc!r}")
        return cls(asset=doc.get("asset"), pos=doc.get("pos"),
                   quat=doc.get("quat", (1.0, 0.0, 0.0, 0.0)),
                   fixed=doc.get("fixed", True))


@dataclass(frozen=True)
class SceneSpec:
    """整场规格：引擎 + 若干 body + 可选流体（D2 的 SPH 参数留位）。"""

    engine: str
    bodies: "tuple[Body, ...]"
    fluid: "dict | None" = None

    def __post_init__(self) -> None:
        if self.engine not in ENGINES:
            raise SceneSpecError(f"engine 必须是 {ENGINES} 之一：{self.engine!r}")
        bodies = tuple(self.bodies)
        object.__setattr__(self, "bodies", bodies)
        if not bodies:
            raise SceneSpecError("bodies 不能为空（空场景请用探针的 --empty 形态）")
        for body in bodies:
            if not isinstance(body, Body):
                raise SceneSpecError(f"bodies 元素必须是 Body：{body!r}")
        if self.fluid is not None:
            if not isinstance(self.fluid, dict):
                raise SceneSpecError(f"fluid 必须是对象或 null：{self.fluid!r}")
            target = self.fluid.get("target")
            if target is not None and target not in {b.asset for b in bodies}:
                raise SceneSpecError(
                    f"fluid.target {target!r} 不在 bodies 的资产集合内")

    def to_json(self) -> str:
        return json.dumps({
            "schema": SCENE_SPEC_SCHEMA,
            "engine": self.engine,
            "bodies": [b.to_json() for b in self.bodies],
            "fluid": self.fluid,
        }, ensure_ascii=False, sort_keys=True, indent=1) + "\n"

    @classmethod
    def from_json(cls, text: str) -> "SceneSpec":
        try:
            doc = json.loads(text)
        except json.JSONDecodeError as exc:
            raise SceneSpecError(f"scene_spec 不是合法 JSON：{exc}") from None
        if not isinstance(doc, dict) or doc.get("schema") != SCENE_SPEC_SCHEMA:
            raise SceneSpecError(
                f"scene_spec.schema 必须是 {SCENE_SPEC_SCHEMA}："
                f"{doc.get('schema') if isinstance(doc, dict) else type(doc)!r}")
        bodies = doc.get("bodies")
        if not isinstance(bodies, list):
            raise SceneSpecError("bodies 必须是数组")
        return cls(engine=doc.get("engine"),
                   bodies=tuple(Body.from_json(b) for b in bodies),
                   fluid=doc.get("fluid"))
