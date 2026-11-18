"""九类研究域 Contract —— 研究数据流的**单一真值**（设计族 §2 类型表逐行落码）。

规矩：
- 只在这里声明 Contract；其余模块一律 `from orchestrator.hk.contracts import ...`，
  禁止第二份字面量（S0-2 完成判据 grep 机判）。
- 单位/坐标系/本体靠 Contract 的 unit/frame/embodiment 字段结构性校验；跨单位
  必须显式 `adapt(...)`，隐式转换即 `CompositionTypeError`。
- `experiment_spec` / `confirm_plan` 里的 δ、α、判据阈值是**内容字段**，
  对 `params.ParameterSpace`（工程容差面）结构性不可达（见 params.py 负控）。
"""
from __future__ import annotations

from ._physicalrsi import Contract, mismatches

EMBODIMENT = "mujoco_box_slide"

#: K0→K1：研究假设/对照/停止条件（json 文档）
EXPERIMENT_SPEC = Contract(name="experiment_spec", embodiment=EMBODIMENT, dtype="json")
#: K1→K2：条件矩阵与配对区块（json 文档）
STUDY_PLAN = Contract(name="study_plan", embodiment=EMBODIMENT, dtype="json")
#: K1/K4→K5：冻结分析源码（py-src，sha256 随收据）
ANALYSIS_PROGRAM = Contract(name="analysis_program", dtype="py-src")
#: K2→K3：运行分配表 (n_cond, n_block)
BATCH_PLAN = Contract(name="batch_plan", embodiment=EMBODIMENT,
                      shape=(None, None), dtype="int32")
#: K3→K4：逐 run 原始观测句柄（blob 引用），单位 m / world 系
RUN_MANIFEST = Contract(name="run_manifest", unit="m", frame="world",
                        embodiment=EMBODIMENT, shape=(None,), dtype="jsonl")
#: K5→K6：逐对比 estimate/CIlo/CIhi
ANALYSIS_RESULT = Contract(name="analysis_result", unit="m", frame="world",
                           embodiment=EMBODIMENT, shape=(None, 3), dtype="float64")
#: K6→K7：冻结方法 + 私有 block 分配（json 文档）
CONFIRM_PLAN = Contract(name="confirm_plan", embodiment=EMBODIMENT, dtype="json")
#: K7→K8：host 独立重算值
CONFIRM_RESULT = Contract(name="confirm_result", unit="m", frame="world",
                          embodiment=EMBODIMENT, shape=(None, 3), dtype="float64")
#: K8 出口：SUPPORTED / REFUTED / INCONCLUSIVE
CLAIM = Contract(name="claim", dtype="json")

ALL_CONTRACTS = {
    "experiment_spec": EXPERIMENT_SPEC,
    "study_plan": STUDY_PLAN,
    "analysis_program": ANALYSIS_PROGRAM,
    "batch_plan": BATCH_PLAN,
    "run_manifest": RUN_MANIFEST,
    "analysis_result": ANALYSIS_RESULT,
    "confirm_plan": CONFIRM_PLAN,
    "confirm_result": CONFIRM_RESULT,
    "claim": CLAIM,
}

#: K0–K8 链的边（producer→consumer），守卫与类型闸口按此表挂接
CHAIN_EDGES = (
    ("experiment_spec", "experiment_spec"),   # K0: spec → registered spec
    ("experiment_spec", "study_plan"),        # K1
    ("study_plan", "batch_plan"),             # K2
    ("batch_plan", "run_manifest"),           # K3
    ("run_manifest", "analysis_program"),     # K4
    ("analysis_program", "analysis_result"),  # K5
    ("analysis_result", "confirm_plan"),      # K6
    ("confirm_plan", "confirm_result"),       # K7
    ("confirm_result", "claim"),              # K8
)


def get(name: str) -> Contract:
    """按名取 Contract；未知名 fail-closed（防拼写漂移出第二真值）。"""
    try:
        return ALL_CONTRACTS[name]
    except KeyError:
        raise KeyError(
            f"未登记的 Contract {name!r}；九类见 ALL_CONTRACTS"
            f"（{sorted(ALL_CONTRACTS)}）") from None
