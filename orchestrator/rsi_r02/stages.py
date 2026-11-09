"""RSI-R0.2 阶段调度规格（计划 §6.2/§6.3，AC23/AC26）。

调度只做一件事：给出**固定顺序**的 trial 规格（实例、族、槽前缀、POST 帽），
让 cli 逐 trial 起会话。它不决定是否该跑（那是 G-V 与 campaign 状态的事），
也不隐藏重跑：任何已有终态的 trial 都不会被再次选中。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

from orchestrator.rsi_r01 import task_contract as TC
from orchestrator.rsi_r02 import state as ST

POST_CAP = 24

#: 开发顺序固定（§6.2）。
DEV_ORDER = ("E", "D", "R")
#: 验证顺序固定（§6.3）：E1,D1,R1,E2,D2,R2。
VALIDATION_ORDER = ("E-val-1", "D-val-1", "R-val-1", "E-val-2", "D-val-2", "R-val-2")


class StageError(RuntimeError):
    """调度规格与槽账不一致。"""


@dataclass(frozen=True)
class TrialSpec:
    trial_id: str
    instance_id: str
    family: str
    phase: str
    slot_prefix: str
    post_cap: int = POST_CAP

    def as_dict(self) -> dict[str, object]:
        return {"trial_id": self.trial_id, "instance_id": self.instance_id,
                "family": self.family, "phase": self.phase,
                "slot_prefix": self.slot_prefix, "post_cap": self.post_cap}


def _family_of(instance_id: str) -> str:
    return instance_id.split("-", 1)[0]


def dev_specs() -> "tuple[TrialSpec, ...]":
    specs = []
    for family in DEV_ORDER:
        instance_id = f"{family}-dev"
        if instance_id not in TC.DEV_IDS:
            raise StageError(f"未知开发实例：{instance_id}")
        specs.append(TrialSpec(trial_id=instance_id, instance_id=instance_id,
                               family=family, phase="dev",
                               slot_prefix=f"{instance_id}-"))
    return tuple(specs)


def validation_specs() -> "tuple[TrialSpec, ...]":
    specs = []
    for order, instance_id in enumerate(VALIDATION_ORDER, start=1):
        if instance_id not in TC.VALIDATION_IDS:
            raise StageError(f"未知验证实例：{instance_id}")
        # 槽 id 是 **分配表序号**（`{family}-val-{order}-{i:02d}`，T0 build_slots 冻结），
        # 不是实例名后缀：D-val-1 是第 2 个验证实例 ⇒ 槽前缀 `D-val-2-`。
        specs.append(TrialSpec(trial_id=instance_id, instance_id=instance_id,
                               family=_family_of(instance_id), phase="validation",
                               slot_prefix=f"{_family_of(instance_id)}-val-{order}-"))
    return tuple(specs)


def specs_for(phase: str) -> "tuple[TrialSpec, ...]":
    if phase == "dev":
        return dev_specs()
    if phase == "validation":
        return validation_specs()
    raise StageError(f"未知阶段：{phase!r}（只允许 dev/validation）")


def slot_ids_for(slots: ST.SlotBook, spec: TrialSpec) -> "list[str]":
    """该 trial 的槽（按 id 排序，最多 24）；缺槽即拒绝，不静默缩帽。"""
    ids = sorted(sid for sid in slots.ids(prefix=spec.slot_prefix))
    if len(ids) != spec.post_cap:
        raise StageError(
            f"{spec.trial_id} 期望 {spec.post_cap} 槽，实际 {len(ids)}："
            f"{ids[:3]}{'...' if len(ids) > 3 else ''}（槽账与调度规格不一致）")
    return ids


def trial_status(store: ST.StateStore, spec: TrialSpec) -> "str | None":
    try:
        return store.load(spec.trial_id).status
    except ST.StateError:
        return None


def next_trial(specs: Sequence[TrialSpec], store: ST.StateStore) -> "TrialSpec | None":
    """固定顺序里第一个**尚未开跑**的 trial；终态/已开跑一律跳过（不重跑）。"""
    for spec in specs:
        if trial_status(store, spec) is None:
            return spec
    return None


def pending_trials(specs: Iterable[TrialSpec],
                   store: ST.StateStore) -> "list[TrialSpec]":
    return [s for s in specs if trial_status(store, s) is None]
