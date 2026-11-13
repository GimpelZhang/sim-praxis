"""RSI-R1 T6：probe / practice 协议链（Plan §8.1，AC28）。

两条链的**策略不同，且不能混用**（§8.1 原文）：

* ``probe``：固定 **8 次**调用，公开小型合成设计 + 真实简短仿真；
  ``write design → propose plan → run exploration → write analysis →
  analyze exploration → freeze confirm → run confirm → submit claim``；
  **首错终止、无修复**（任何一呼不 ok 即停，不再发下一呼）；
* ``practice``：固定 **16 次**调用（两条 8 动作链），公开玩具域、不含 A/B 答案；
  合同内失败允许**自行修正**（重发同一动作在额度内合法），但一旦出现终止类
  拒绝（越权/状态不明/LOCK 漂移/预算耗尽）立即停。

判据（AC28）：两链各自走通 ⇒ ``PASS``；否则 ``NOT_MEASURED``——协议没走完
**不是**科学阴性结论，报告只如实记录真实动作序列与真实判据输出，不补写、
不改判、不把仪器失败包装成阴性结果。

布局（每次尝试独立目录，失败可复现、不覆盖）：

    <root>/protocol/<lane>/attempt-<k>/        ← 该次尝试的完整事实面
        state/journal.jsonl                    ← 独立事件日志（root 镜像见下）
        public/ designs/ observations/ ...     ← 与正式 study 同构的子目录
    <root>/protocol/report_<lane>.json         ← 汇总（含 attempt 号与判决）

**root 镜像**：协议链用独立日志，但 root 阶段门（``FROZEN``/``PROBED``）来自
campaign 主日志。进入链前把主日志里的 ``root_transition`` 事件按序镜像到独立
日志，于是 ``root_state()`` 与主日志逐字一致、``_require_root`` 照常生效；
已存在镜像时只校验末态一致（不一致即 ``PROTOCOL_ROOT_DRIFT``，不静默续跑）。
预算/准入/台账仍绑**主 ctx**（``quota/``、``state/pause.json`` 必须全 campaign 共享），
只有候选侧产物路径被 ``ScopedContext`` 限定在 ``protocol/<lane>/attempt-<k>/``。
"""
from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from orchestrator.common import research_transport as T
from orchestrator.common.clock import utc_now_iso_z
from orchestrator.common.runtime_paths import RSI_R1_CAMPAIGN_SUBDIRS
from orchestrator.rsi_r1 import analysis_receipt as AR
from orchestrator.rsi_r1 import assembly as A
from orchestrator.rsi_r1 import backend as BK
from orchestrator.rsi_r1 import contracts as C
from orchestrator.rsi_r1 import lanes as LANES
from orchestrator.rsi_r1 import lock as LK
from orchestrator.rsi_r1 import loop as L
from orchestrator.rsi_r1 import profiles as P
from orchestrator.rsi_r1 import recovery as RCV
from orchestrator.rsi_r1 import repair as RPR
from orchestrator.rsi_r1 import scenes as SC
from orchestrator.rsi_r1 import state as S
from orchestrator.rsi_r1 import watchdog as W

PROBE = "probe"
PRACTICE = "practice"
PROTOCOL_LANES: "tuple[str, ...]" = (PROBE, PRACTICE)

#: §8.1 字面值：probe 8 呼、practice 16 呼（= 两条 8 动作链）。
PROBE_POSTS = 8
PRACTICE_POSTS = 16
PRACTICE_STUDIES: "tuple[str, ...]" = ("A", "B")
CHAIN_ACTIONS = 8

#: 八动作链的**规范顺序**（Plan §8.1 / T3 合同）。完成判据按这个游标走，
#: 不按 ok 计数：合同内自修会产生额外同名动作（重写 design 等），计数会把
#: 链提前凑满、漏掉 submit_claim。
CHAIN_SEQUENCE: "tuple[str, ...]" = (
    "write_artifact",   # 1 design
    "propose_plan",     # 2
    "run_batch",        # 3 explore
    "write_artifact",   # 4 analysis
    "analyze",          # 5
    "freeze_confirm",   # 6
    "run_confirm",      # 7
    "submit_claim",     # 8
)
assert len(CHAIN_SEQUENCE) == CHAIN_ACTIONS

PROTOCOL_SUBDIR = "protocol"
REPORT_SCHEMA = "simpraxis.rsi_r1.protocol_report.v1"
SCHEMA_VERSION = 1

VERDICT_PASS = "PASS"
VERDICT_NOT_MEASURED = "NOT_MEASURED"

#: 过门后主 root 的下一状态（严格 ±1，由状态机自己再核一次）。
ROOT_NEXT = {PROBE: S.ROOT_PROBED, PRACTICE: S.ROOT_PRACTICED}
ROOT_GATE = {PROBE: S.ROOT_FROZEN, PRACTICE: S.ROOT_PROBED}


class ProtocolError(RuntimeError):
    """协议层的配置/状态错误（不是模型结论）。``code`` 公开可记。"""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


# ---------------------------------------------------------------------------
# 作用域上下文：候选侧路径限定在 protocol/<lane>/attempt-<k>/
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ScopedContext:
    """把 ``relative()/subdir()`` 前缀化的 campaign 上下文代理。

    预算、槽、暂停件与台账**不**走这里（它们必须全 campaign 共享，见
    ``run_protocol`` 的装配注释）；写守卫仍委托给主 ctx 的
    ``assert_write_allowed``，因此「越出 campaign 根 / 落进旧根」的拒绝面不变。
    """

    base: Any
    prefix: str

    def __post_init__(self) -> None:
        rel = Path(self.prefix)
        if rel.is_absolute() or ".." in rel.parts or not rel.parts:
            raise ProtocolError("SCOPE_INVALID", f"prefix={self.prefix!r}")

    # -- 只读转发 --------------------------------------------------------
    @property
    def envelope_id(self) -> str:
        return self.base.envelope_id

    @property
    def root(self) -> Path:
        return self.base.root

    @property
    def protected_roots(self) -> Any:
        return self.base.protected_roots

    # -- 路径 ------------------------------------------------------------
    def _scoped(self, relpath: "str | Path") -> str:
        rel = Path(relpath)
        if rel.is_absolute() or ".." in rel.parts:
            raise ProtocolError("SCOPE_INVALID", f"只接受相对路径：{rel}")
        return f"{self.prefix}/{rel.as_posix()}"

    def relative(self, relpath: "str | Path") -> Path:
        return self.base.relative(self._scoped(relpath))

    def subdir(self, name: str) -> Path:
        if name not in RSI_R1_CAMPAIGN_SUBDIRS:
            raise ProtocolError("SUBDIR_UNKNOWN", repr(name))
        return self.relative(name)

    # -- 写守卫（绝对路径原样交给主 ctx 复核）----------------------------
    def assert_write_allowed(self, target: "str | Path") -> Path:
        p = Path(target)
        if not p.is_absolute():
            p = self.relative(p)
        return self.base.assert_write_allowed(p)

    def write_text(self, relpath: "str | Path", text: str) -> Path:
        return self.base.write_text(self._scoped(relpath), text)

    def write_json(self, relpath: "str | Path", obj: Any) -> Path:
        return self.base.write_json(self._scoped(relpath), obj)

    def read_json(self, relpath: "str | Path") -> Any:
        return self.base.read_json(self._scoped(relpath))

    def exists(self, relpath: "str | Path") -> bool:
        return self.base.exists(self._scoped(relpath))


# ---------------------------------------------------------------------------
# 公开玩具域（固定公开 fixture，不是 A/B study 的设计，也不含任何答案）
# ---------------------------------------------------------------------------

def _cond(cid: str, scene: str, **params: str) -> "dict[str, Any]":
    return {"id": cid, "scene_id": scene, "params": params}


#: 条件值全部取自公开合同里的公开参数域；三条件即满足
#: 「主对比 + 单字段阴性控制（A）/ Tc-dt 可分离（B）」的最小设计。
TOY_CONDITIONS: "Mapping[str, tuple[dict[str, Any], ...]]" = {
    "A": (
        _cond("base", SC.SCENE_A, mass_kg="0.5", v0_mps="0.4",
              sliding_friction="0.4", torsional_friction_m="0.01"),
        _cond("low_mu", SC.SCENE_A, mass_kg="0.5", v0_mps="0.4",
              sliding_friction="0.2", torsional_friction_m="0.01"),
        _cond("sham", SC.SCENE_A, mass_kg="0.5", v0_mps="0.4",
              sliding_friction="0.4", torsional_friction_m="0.02"),
    ),
    "B": (
        _cond("base", SC.SCENE_B, inertia_ratio="1.0", target_amplitude_rad="0.2",
              target_freq_hz="1.0", tc_s="0.008", dt_s="0.001",
              controller="PD", kp="5", kd="0.1"),
        _cond("tc_only", SC.SCENE_B, inertia_ratio="1.0",
              target_amplitude_rad="0.2", target_freq_hz="1.0", tc_s="0.016",
              dt_s="0.001", controller="PD", kp="5", kd="0.1"),
        _cond("dt_only", SC.SCENE_B, inertia_ratio="1.0",
              target_amplitude_rad="0.2", target_freq_hz="1.0", tc_s="0.008",
              dt_s="0.0005", controller="PD", kp="5", kd="0.1"),
    ),
}

TOY_VARYING = {"A": {"mass_kg": ["0.2", "1.0"]},
               "B": {"inertia_ratio": ["0.5", "2.0"]}}


def toy_plan_doc(study: str, *, phase: str = "explore", revision: int = 1,
                 parent_sha: "str | None" = None,
                 design_sha256: "str | None" = None,
                 analysis_sha256: "str | None" = None) -> "dict[str, Any]":
    """协议 fixture 的 ResearchPlan 文档（可被候选逐字复制，也可自行改写）。

    构造后立即用生产合同校验（``ResearchPlan.from_dict`` + ``validate_plan``），
    fixture 一旦与合同漂移就在**构造期**炸掉，不会拖到真实链里。
    """
    if study not in S.STUDIES:
        raise ProtocolError("STUDY_UNKNOWN", repr(study))
    conditions = [dict(c) for c in TOY_CONDITIONS[study]]
    if study == "A":
        primary = {"id": "primary", "kind": "primary",
                   "metric": "displacement_x_m", "proposition": "decrease_gt",
                   "delta": "0.001", "condition_ids": ["base", "low_mu"]}
        negative: "dict[str, Any] | None" = {
            "id": "negative", "kind": "negative",
            "metric": "displacement_x_m_negative_equivalence",
            "proposition": "equivalent", "delta": "0.001",
            "condition_ids": ["base", "sham"]}
        hypotheses = [
            {"id": "h1", "mechanism": "滑动摩擦做功耗散平动动能，摩擦系数越低位移越远",
             "predicted": "decrease_gt", "contrast_id": "primary",
             "falsification": "配对差区间完全落在 δ 之内即反证主解释"},
            {"id": "h2", "mechanism": "扭转摩擦只耦合转动，不改平动位移",
             "predicted": "equivalent", "contrast_id": "negative",
             "falsification": "阴性对比的位移区间越出 ±δ 即反证"},
        ]
    else:
        primary = {"id": "primary", "kind": "primary",
                   "metric": "normalized_tracking_rmse",
                   "proposition": "increase_gt", "delta": "0.01",
                   "condition_ids": ["base", "tc_only"]}
        negative = None
        hypotheses = [
            {"id": "h1", "mechanism": "控制周期变长削弱高频校正，跟踪误差变大",
             "predicted": "increase_gt", "contrast_id": "primary",
             "falsification": "Tc-only 与基准的 RMSE 差区间不越过 δ 即反证"},
            {"id": "h2", "mechanism": "积分步长变细只改离散误差，不改跟踪带宽",
             "predicted": "equivalent", "contrast_id": "primary",
             "falsification": "dt-only 对比的区间越出等效界即反证"},
        ]
    doc = {
        "schema": C.PLAN_SCHEMA, "schema_version": 1,
        "study_id": study, "revision": int(revision), "parent_sha": parent_sha,
        "hypotheses": hypotheses, "conditions": conditions,
        "nuisance_distribution": {"generator": "pcg64-host", "block_count": 32,
                                  "varying": dict(TOY_VARYING[study])},
        "primary": primary, "negative_control": negative,
        "analysis_source_sha": (analysis_sha256 if phase == "confirm" else None),
        "design_source_sha": (design_sha256 if phase == "confirm" else None),
        "phase": phase,
    }
    # 锁内适配（RSI-R1.1 / L-R1-07）：``parent_sha`` 允许字面 sentinel ``"host"``
    # （宿主代填当前探索计划的 sha）。结构校验用占位 hex，返回时原样还原 sentinel。
    sentinel = doc["parent_sha"] == L.HOST_PLAN_SHA
    if sentinel:
        doc["parent_sha"] = "0" * 64
    plan = C.ResearchPlan.from_dict(doc)
    C.validate_plan(plan)
    out = plan.to_dict()
    if sentinel:
        out["parent_sha"] = L.HOST_PLAN_SHA
    return out


# ---------------------------------------------------------------------------
# 候选侧模板（公开示例源码；写清"这两段是格式示例，不是最优设计"）
# ---------------------------------------------------------------------------

DESIGN_TEMPLATE = '''\
import hashlib
import json
import sys
from pathlib import Path


def plan_sha(doc):
    blob = json.dumps(doc, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def main() -> int:
    view = Path(sys.argv[1])
    out = Path(sys.argv[2])
    plans = sorted(view.glob("plan-*.json"))
    if len(plans) != 1:
        print("expected exactly one plan, found %d" % len(plans), file=sys.stderr)
        return 2
    plan = json.loads(plans[0].read_text(encoding="utf-8"))
    blocks = ["b01", "b02"]           # 探索批只取前两个 block，省算力
    cells = []
    for cond in plan["conditions"]:
        for block_id in blocks:
            cells.append({"condition_id": cond["id"], "block_id": block_id,
                          "params": dict(cond["params"])})
    doc = {"schema": "simpraxis.rsi_r1.matrix.v1", "schema_version": 1,
           "study_id": plan["study_id"], "plan_sha": plan_sha(plan),
           "cells": cells}
    (out / "matrix.json").write_text(
        json.dumps(doc, sort_keys=True) + "\\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''

_ANALYSIS_TEMPLATE_SOURCE = '''\
"""候选分析程序示例（**形状完整**：产出必须过宿主 analysis_receipt 门）。

argv[1] = 只读观测视图（run 文件与 ``<run_id>.meta.json`` 平铺在根目录，
          另有宿主放入的 ``plan-<sha12>.json``，对比定义与 δ 从中读取）；
argv[2] = 输出目录，必须写 ``analysis.json``。
"""
import json
import math
import sys
from pathlib import Path

import numpy as np

SEED = 20261111          # 公开统计 seed（固定、与数据无关，见公开合同）
RESAMPLES = 10000


def metric_value(record, metric_id):
    qpos = record["qpos"]
    if metric_id == "normalized_tracking_rmse":
        target = record["target_q"]
        sq = 0.0
        for a, b in zip(qpos, target):
            d = float(a[0]) - float(b[0])
            sq += d * d
        amp = max(abs(float(b[0])) for b in target) or 1.0
        return math.sqrt(sq / len(qpos)) / amp
    if metric_id == "displacement_x_m":
        return float(qpos[-1][0]) - float(qpos[0][0])
    raise SystemExit("unknown metric: %s" % metric_id)


def interval(values, level):
    arr = np.asarray(values, dtype=np.float64)
    rng = np.random.default_rng(np.random.PCG64(SEED))
    idx = rng.integers(0, arr.shape[0], size=(RESAMPLES, arr.shape[0]))
    means = arr[idx].mean(axis=1)
    alpha = (1.0 - level) / 2.0
    lo, hi = np.quantile(means, [alpha, 1.0 - alpha], method="linear")
    return float(lo), float(hi)


def verdict(proposition, delta, lower, upper):
    if proposition == "decrease_gt":
        if upper < -delta:
            return "SUPPORTED"
        if lower >= -delta:
            return "REFUTED"
    elif proposition == "increase_gt":
        if lower > delta:
            return "SUPPORTED"
        if upper <= delta:
            return "REFUTED"
    else:  # equivalent
        if lower > -delta and upper < delta:
            return "SUPPORTED"
        if upper <= -delta or lower >= delta:
            return "REFUTED"
    return "INCONCLUSIVE"


def main() -> int:
    view = Path(sys.argv[1])
    out = Path(sys.argv[2])
    plans = sorted(view.glob("plan-*.json"))
    records = {}
    for path in sorted(view.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            records[rec["run_id"]] = rec
    if not plans:
        # 旧 R1 路径的视图不含 plan（R1.2 才由宿主放入）：保持旧行为，只报
        # run 身份与计数，旧门只查 n_runs。新门不会被走到这条分支。
        doc = {"schema": "__OUTPUT_SCHEMA__", "schema_version": 1,
               "n_runs": len(records), "run_ids": sorted(records),
               "contrasts": []}
        (out / "analysis.json").write_text(
            json.dumps(doc, sort_keys=True, allow_nan=False) + "\\n",
            encoding="utf-8")
        return 0
    plan = json.loads(plans[0].read_text(encoding="utf-8"))
    condition = {}
    for meta in sorted(view.glob("*.meta.json")):
        doc = json.loads(meta.read_text(encoding="utf-8"))
        condition[meta.name[: -len(".meta.json")]] = doc["condition_id"]
    specs = [plan["primary"]]
    if plan.get("negative_control"):
        specs.append(plan["negative_control"])
    contrasts = []
    for spec in specs:
        metric_id = spec["metric"]
        by_block = {}
        for run_id, rec in records.items():
            by_block.setdefault(rec["block_id"], {})[condition[run_id]] = \
                metric_value(rec, metric_id)
        first, second = spec["condition_ids"]
        diffs = [pair[first] - pair[second] for _, pair in sorted(by_block.items())]
        level = 0.90 if spec["kind"] == "negative" else 0.95
        effect = float(np.mean(diffs))
        lower, upper = interval(diffs, level)
        contrasts.append({
            "contrast_id": spec["id"],
            "metric_id": metric_id,
            "proposition": spec["proposition"],
            "estimate": effect,
            "lower": lower,
            "upper": upper,
            "status": verdict(spec["proposition"], float(spec["delta"]),
                              lower, upper),
            "n_blocks": len(diffs),
        })
    doc = {"schema": "__OUTPUT_SCHEMA__", "schema_version": 1,
           "run_ids": sorted(records), "contrasts": contrasts}
    (out / "analysis.json").write_text(
        json.dumps(doc, sort_keys=True, allow_nan=False) + "\\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''

#: 单一源：模板写出的 schema 与宿主门 ``analysis_receipt.OUTPUT_SCHEMA`` 恒等
#: （2026-11-13 修 D-1：R1.2 T4 换门后模板仍写旧 schema，导致候选照抄必被拒）。
ANALYSIS_TEMPLATE = _ANALYSIS_TEMPLATE_SOURCE.replace(
    "__OUTPUT_SCHEMA__", AR.OUTPUT_SCHEMA)

#: 公开估计量（``statistics.py`` 自述「公开方法，候选可用」；Plan §4.1 固定）。
ESTIMATOR_SPEC = """\
估计量（公开、预注册前固定；你要在 claims 里逐字段复现宿主的重算值）：
- 抽样单位 = block：32 个确认 block 一一配对，d_i = metric(处理_i) − metric(对照_i)；
- effect = mean(d_i)；
- 区间 = percentile bootstrap，10000 次重抽样，`linear` 分位数，float64；
- 95% 主区间用于主对比；阴性等效对比用 90% 区间；
- 统计 seed 固定为 20261111（与数据无关，`numpy.random.Generator(PCG64(seed))`
  逐次 `integers(0, n, n)` 抽 block 下标；同日同实现的候选可逐位复现）。"""


PLAN_SENTINEL_NOTE = (
    "### 两个「不可计算」字段的公开约定\n"
    "模型无法自己算 sha256，这一点是公开约定。以下两处接受字面 sentinel `\"host\"`：\n"
    "1) `freeze_confirm.plan_sha256`：宿主对**你发来的 confirm 文档**计算 sha；\n"
    "2) confirm 计划里的 `parent_sha`：宿主填成**当前探索计划**的 sha（第 2 步返回的\n"
    "   `plan_sha256` 逐字复制也可以）。\n"
    "传 64 hex 时宿主逐字比对：计划文档不符 ⇒ `PLAN_MISMATCH`；`parent_sha` 不是当前\n"
    "探索计划 ⇒ `PARENT_SHA_MISMATCH`。有探索计划时 `parent_sha` 不得为空。\n")

_ACTIONS_BLOCK = (
    "### 八动作顺序（每个回复只给一个动作）\n"
    "1. write_artifact(kind=design, source=<python>)\n"
    "2. propose_plan(plan=<ResearchPlan>)   ← 返回 plan_sha256；计划全文也会在每轮的\n"
    "   「当前可见状态.plan」里逐字回灌，freeze 时照它改字段即可\n"
    "3. run_batch(plan_sha256, design_sha256, phase=\"explore\")  ← 返回 run_ids\n"
    "4. write_artifact(kind=analysis, source=<python>)\n"
    "5. analyze(source_sha256, run_ids=[...])  ← 返回该程序的 analysis 结果\n"
    "   （单次最多 64 个 run；确认批 run 更多时按对比拆成多次 analyze）\n"
    "6. freeze_confirm(plan_sha256=\"host\", design_sha256, analysis_sha256,\n"
    "                   plan=<confirm 计划>)  ← 返回 confirm_token\n"
    "7. run_confirm(confirm_token)  ← 返回确认批 run_ids_head 与 references：\n"
    "   references 是宿主用公开估计量从**你这批确认观测**算出的每条假设的\n"
    "   estimate/lower/upper（你自己看不到确认批数值，直接逐字复制即可）\n"
    "8. submit_claim(claims=[...], report_text=<正文>)\n"
    "\n"
    "### 失效规则（2026-11-13 公开；违反不会报错，但后续动作**只烧预算不推进**）\n"
    "改写某个位置的动作会让它的下游位置失效：改写 design 后必须重新 "
    "propose_plan 才能推进 run_batch；改写 plan 后必须重新 run_batch；改写 "
    "analysis 后必须重新 analyze。失效位置已消耗的 POST 不退还；确认冻结后"
    "禁止改写 design/plan/analysis（terminal）。\n")

_SOURCE_TEMPLATES_BLOCK = (
    "### 格式示例（不是最优设计，只演示输入输出形状）\n"
    "design 程序：argv[1]=只读视图（含 `plan-<sha12>.json` 与 `public_contract.json`），\n"
    "argv[2]=输出目录，必须写 `matrix.json`。注意 matrix 的 `plan_sha` 是**计划\n"
    "文档的 canonical sha256**：`sha256(json.dumps(plan, sort_keys=True,\n"
    "separators=(\",\", \":\"), ensure_ascii=False))`，示例里已写好这个函数。\n"
    "```python\n" + DESIGN_TEMPLATE + "```\n"
    "analysis 程序：argv[1]=只读观测视图（run 文件**平铺**在根目录，"
    "用 `glob(\"*.jsonl\")` 或 `rglob(\"*.jsonl\")`；不是 `*/*.jsonl`），"
    "argv[2]=输出目录，\n必须写 `analysis.json`，字段恒为："
    "`schema`（必须逐字等于公开合同 `analysis_output.output_schema` 的值，"
    "即 `simpraxis.rsi_r1.analysis_output.v1`；**不要**读 "
    "`analysis_output.schema`——那是合同文档自身的 schema，不是输出 schema）、"
    "`run_ids`（本次请求的全部 run，不许多不许少）、"
    "`contrasts`（按 `plan-<sha12>.json` 里 primary/negative_control 的 "
    "`id/metric/proposition/condition_ids` 逐条给出 "
    "`contrast_id, metric_id, proposition, estimate, lower, upper, status, "
    "n_blocks`；区间用公开估计量：95% 主区间 / 90% 阴性区间，block 重抽样，"
    "seed 20261111）。\n"
    "```python\n" + ANALYSIS_TEMPLATE + "```\n")

_CLAIMS_BLOCK = (
    "### claims 字段（每个假设一条，逐条提交，不能汇总替代）\n"
    "`{\"hypothesis_id\", \"proposition\", \"metric_id\", \"delta\", \"estimate\",\n"
    "\"lower\", \"upper\"}`；`delta` 必须与公开合同里该指标的 δ 逐字相等（数值），\n"
    "`metric_id`/`proposition` 必须与计划里的对比一致。**estimate/lower/upper 直接\n"
    "逐字复制第 7 步 `run_confirm` 返回的 `references[i]` 同名同值**（那是宿主用\n"
    "公开估计量从你自己的确认批观测算出的测量结果）。宿主仍会用公开估计量从 raw\n"
    "独立重算，并按 1e-8 + 1e-6×|参考值| 逐字段比对；不一致即 CLAIM_INVALID。\n")


def render_protocol_brief(lane: str) -> str:
    """system 追加段：把公开玩具域、八动作顺序与格式示例写清（不写任何答案）。"""
    if lane not in PROTOCOL_LANES:
        raise ProtocolError("LANE_UNKNOWN", repr(lane))
    studies = (S.STUDIES[0],) if lane == PROBE else PRACTICE_STUDIES
    parts: "list[str]" = []
    if lane == PROBE:
        parts.append(
            "### 本轮任务：probe 协议演练（非研究）\n"
            "这是**协议演练**：唯一目的是让八个动作在真实宿主上依次走通。它不产生\n"
            "任何研究结论，也不能被引用为结果；判决由独立判据照常给出并原样记录。\n"
            "本链**首错即止**：任何一呼失败都不会重发，所以每个动作的 JSON 必须\n"
            "严格遵守下面的字段表。")
    else:
        parts.append(
            "### 本轮任务：practice 演练（16 呼，两条 8 动作链）\n"
            "先完成 study A 的八动作链，再完成 study B 的八动作链。合同内的失败可以\n"
            "自己修正（例如字段写错、设计代码报错），修正也消耗调用额度；越权、状态\n"
            "不明、LOCK 漂移、额度耗尽是终止类，不会再给你下一呼。")
    parts.append(_ACTIONS_BLOCK)
    parts.append(PLAN_SENTINEL_NOTE)
    for study in studies:
        explore = toy_plan_doc(study, phase="explore", revision=1)
        parts.append(
            f"### study {study} 的计划模板（可逐字复制；也可自行改写，但必须过合同）\n"
            "```json\n" + json.dumps(explore, ensure_ascii=False, sort_keys=True,
                                      indent=1) + "\n```\n"
            f"confirm 计划 = 同一份文档把 phase 改成 \"confirm\"、revision 改成 2、\n"
            f"parent_sha 逐字复制第 2 步返回的 plan_sha256（或写 \"host\"）、\n"
            "design_source_sha/analysis_source_sha 填第 1/4 步返回的 sha；其余字段\n"
            "逐字不变。")
    parts.append(ESTIMATOR_SPEC)
    parts.append(_SOURCE_TEMPLATES_BLOCK)
    parts.append(_CLAIMS_BLOCK)
    return "\n".join(parts)


def render_study_brief(study: str) -> str:
    """正式研究 study 的 system 追加段（T7：真实研究，48 呼预算，不是演练）。"""
    if study not in S.STUDIES:
        raise ProtocolError("STUDY_UNKNOWN", repr(study))
    cap = LANES.LANE_POST_CAPS[f"study_{study}"]
    parts: "list[str]" = [
        f"### 本轮任务：正式研究 study {study}（{cap} 呼预算）\n"
        "这是**真实研究**：针对公开合同里 study "
        f"{study} 的题目，自主提出竞争解释、\n"
        "设计鉴别实验、写实验/分析程序、跑真实物理、冻结唯一确认集、按证据下结论。\n"
        "研究过程允许合同内自修（字段错、代码报错），每次调用都消耗预算；越权、\n"
        "状态不明、LOCK 漂移、预算耗尽是终止类。判定由独立判据给出，与你提交的\n"
        "自我评价无关。探索阶段可以提交多份修订（revision 递增）；确认集只能冻结\n"
        "一次，冻结时宿主检查本 lane 冻结后至少还剩 8 呼；确认批跑完后按每个假设\n"
        "逐条提交主张。\n",
        _ACTIONS_BLOCK,
        PLAN_SENTINEL_NOTE,
    ]
    explore = toy_plan_doc(study, phase="explore", revision=1)
    parts.append(
        f"### study {study} 的公开计划示例（可逐字复制；也可自行改写，但必须过合同）\n"
        "```json\n" + json.dumps(explore, ensure_ascii=False, sort_keys=True,
                                  indent=1) + "\n```\n"
        "confirm 计划 = 同一份文档把 phase 改成 \"confirm\"、revision 改成 2、\n"
        "parent_sha 逐字复制最后一次 propose_plan 返回的 plan_sha256（或写 \"host\"）、\n"
        "design_source_sha/analysis_source_sha 填对应 write_artifact 返回的 sha；\n"
        "其余字段逐字不变。")
    parts.append(ESTIMATOR_SPEC)
    parts.append(_SOURCE_TEMPLATES_BLOCK)
    parts.append(_CLAIMS_BLOCK)
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# 装配
# ---------------------------------------------------------------------------

@dataclass
class LaneParts:
    """一条协议链的全部生产件（预算面绑主 ctx，候选面绑 scoped ctx）。"""

    lane: str
    scope: ScopedContext
    admission: A.CampaignAdmission
    slots: A.SlotBook
    state: S.ResearchState
    dispatcher: A.CampaignDispatcher
    store: L.EventStore
    sender: L.LockedSender
    loop: L.ResearchLoop
    study: str
    #: 记账 lane：正常 runs 与 ``lane`` 相同；仪器修复重做固定为
    #: ``repair_reserve``（§8.1：储备 24 呼只跑 probe8+practice16，不回填原槽）。
    billing_lane: str = PROBE


def next_attempt(ctx: Any, lane: str) -> int:
    """下一次尝试号：已有目录数 + 1（失败尝试原样留档，不覆盖）。"""
    base = ctx.relative(PROTOCOL_SUBDIR) / lane
    if not base.exists():
        return 1
    used = [p.name for p in base.iterdir()
            if p.is_dir() and p.name.startswith("attempt-")]
    return len(used) + 1


def attempt_prefix(lane: str, attempt: int) -> str:
    if lane not in PROTOCOL_LANES:
        raise ProtocolError("LANE_UNKNOWN", repr(lane))
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        raise ProtocolError("ATTEMPT_INVALID", repr(attempt))
    return f"{PROTOCOL_SUBDIR}/{lane}/attempt-{attempt}"


def mirror_root(scope: ScopedContext) -> S.Journal:
    """把主日志的 ``root_transition`` 镜像进独立日志（只镜像，不推断）。"""
    main = S.Journal(scope.base)
    journal = S.Journal(scope)
    have = journal.events()
    want = [ev for ev in main.events() if ev.event == "root_transition"]
    if not have:
        for ev in want:
            journal.append("root_transition", dict(ev.payload))
        return journal
    last = None
    for ev in have:
        if ev.event == "root_transition":
            last = str(ev.payload.get("to"))
    current = None
    for ev in want:
        current = str(ev.payload.get("to"))
    if last != current:
        raise ProtocolError(
            "PROTOCOL_ROOT_DRIFT",
            f"独立日志 root={last!r} 与主日志 {current!r} 不一致（拒绝静默续跑）")
    return journal


def _seed_public(scope: ScopedContext) -> None:
    """把公开合同按字节复制进本尝试的公开视图（design 程序要读它）。"""
    for name in ("public_contract.json", "public_contract.md"):
        rel = f"public/{name}"
        target = scope.relative(rel)
        if target.is_file():
            continue
        source = scope.base.relative(rel)
        if not source.is_file():
            raise ProtocolError("PUBLIC_CONTRACT_MISSING", rel)
        scope.write_text(rel, source.read_text(encoding="utf-8"))


def backend_factory_for(scope: ScopedContext, *, runner: Any, image: str,
                        lane: str) -> Callable[[str], BK.PhysicsBackend]:
    """真容器 backend：观测落本尝试的 ``observations/``，证据落 campaign 证据根。"""
    def build(batch_id: str) -> BK.PhysicsBackend:
        return BK.PhysicsBackend(
            observations_root=scope.subdir("observations"),
            evidence_root=scope.base.subdir("evidence_pack"),
            work_root=scope.subdir("trials") / f"work-{lane}",
            runner=runner, image=image, batch_id=batch_id)
    return build



def _resource_ledger(ctx: Any, profile: "P.CampaignProfile | None",
                     given: "Any") -> "Any":
    """R1.2 profile 必须有 campaign 级资源账；legacy 不建（行为不变）。"""
    if profile is None or not getattr(profile, "is_r12", False):
        return None
    if given is not None:
        return given
    from orchestrator.rsi_r1 import resources as RS

    return RS.ResourceLedger(ctx.root, profile=profile)


def build_lane_parts(ctx: Any, *, lane: str, study: str, attempt: int,
                     spend: A.SpendContract, lock: L.LockDocument,
                     transport_factory: Callable[[], Callable[[T.Request], L.Reply]],
                     runner: Any, image: str, counter: Callable[[str], int],
                     backend_factory: "Callable[[str], Any] | None" = None,
                     billing_lane: "str | None" = None,
                     profile: "P.CampaignProfile | None" = None,
                     resource_ledger: "Any" = None,
                     ) -> LaneParts:
    """按生产面装配一条链：准入/槽绑主 ctx，状态/分派绑 scoped ctx。"""
    if lane not in PROTOCOL_LANES:
        raise ProtocolError("LANE_UNKNOWN", repr(lane))
    if study not in S.STUDIES:
        raise ProtocolError("STUDY_UNKNOWN", repr(study))
    if study != "A" and lane == PROBE:
        raise ProtocolError("STUDY_MISMATCH", f"probe 只用 study A，收到 {study!r}")
    billing = lane if billing_lane is None else str(billing_lane)
    if billing not in LANES.LANE_ORDER:
        raise ProtocolError("LANE_UNKNOWN", repr(billing))
    scope = ScopedContext(ctx, attempt_prefix(lane, attempt))
    journal = mirror_root(scope)
    _seed_public(scope)
    state = S.ResearchState(scope, journal=journal)
    budget = C.budget_from_dict(ctx.read_json("contract/budget.json"))
    # 暂停件/台账/预留必须全 campaign 共享 ⇒ admission 用主 ctx；root 门读独立日志。
    admission = A.CampaignAdmission(ctx=ctx, state=state, budget=budget,
                                    lock_sha256=lock.sha256, spend=spend, lane=billing)
    slots = A.SlotBook(ctx, budget)
    admission.attach_slots(slots)
    factory = backend_factory or backend_factory_for(
        scope, runner=runner, image=image, lane=lane)
    dispatcher = A.CampaignDispatcher(
        ctx=scope, state=state, admission=admission, budget=budget, study=study,
        runner=runner, image=image, backend_factory=factory,
        profile=profile, resource_ledger=resource_ledger)
    store = L.EventStore(state, study=study)
    envelope = A.Envelope.from_ctx(ctx)
    sender = L.build_production_sender(
        lock_reader=LK.lock_reader(ctx), transport_factory=transport_factory,
        admission=admission, budget=slots, envelope=envelope,
        spend_contract=spend, sha_of=repo_sha)
    system = L.render_system(extra_rules=render_protocol_brief(lane))

    def messages() -> T.Request:
        return T.build_request(
            system=system,
            task_brief=L.render_task_brief(dispatcher.visible_state()),
            turns=store.history(), counter=counter)

    loop = L.ResearchLoop(
        sender=sender, parser=L.parse_action, dispatcher=dispatcher, store=store,
        messages=messages, counter=counter, admission=admission, budget=slots,
        envelope=envelope, root_lock=lock, spend_contract=spend)
    return LaneParts(lane=lane, scope=scope, admission=admission, slots=slots,
                     state=state, dispatcher=dispatcher, store=store,
                     sender=sender, loop=loop, study=study, billing_lane=billing)


def repo_sha(relpath: str) -> str:
    path = LK.REPO_ROOT / relpath
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# 链驱动
# ---------------------------------------------------------------------------

CHAINS_TOO_LONG = "CHAIN_TOO_LONG"  # 连续同名动作超出合同（诊断用，非判据）


@dataclass
class ChainResult:
    study: str
    actions: "list[dict[str, Any]]" = field(default_factory=list)
    ok_count: int = 0
    stopped: str = ""
    terminal: bool = False
    study_state: str = ""
    judge_verdict: "str | None" = None
    study_verdict: "str | None" = None
    #: 八个**规范动作**按序全部走完（由 ``drive_chain`` 的游标决定，不由 ok 计数决定）。
    completed: bool = False
    #: 游标所在位置（0..8）：报告里用它说明「走到第几个规范动作」。
    stage: int = 0
    #: R1.2 资格检查块（首次率/可恢复失败/里程碑）；legacy 路径恒为 None。
    qualification: "dict[str, Any] | None" = None

    def as_dict(self) -> "dict[str, Any]":
        doc = {"study": self.study, "actions": list(self.actions),
               "ok_count": self.ok_count, "stopped": self.stopped,
               "terminal": bool(self.terminal),
               "completed": bool(self.completed),
               "stage": int(self.stage),
               "study_state": self.study_state,
               "judge_verdict": self.judge_verdict,
               "study_verdict": self.study_verdict}
        if self.qualification is not None:
            doc["qualification"] = self.qualification
        return doc


def drive_chain(parts: LaneParts, *, strict: bool, cap: int,
                policy: "P.ProtocolPolicy | None" = None) -> ChainResult:
    """跑一条 8 动作链。

    ``strict=True``（probe）：出现第一个非 ok 回合即停，**不再发下一呼**；
    ``strict=False``（practice/正式 study）：允许合同内自修，直到八个规范动作
    按序全部成功或额度用尽为止。终止类拒绝（``terminal``）两种策略都立即停。

    ``policy`` 给定时是 R1.2 的有界自修语义（``bounded_recovery``）：完成判据
    仍是规范动作游标，但每次失败都要过 ``recovery.classify_failure`` 的证据
    判别（缺证据默认 terminal），并且每次发送前按依赖图算
    ``min_remaining_actions``，剩余不足即 ``BUDGET_UNREACHABLE`` 不发明知无法
    闭合的调用。旧调用不传 ``policy``，行为逐字节不变。

    完成判据是**规范动作游标**而不是「ok 计数」：合同内自修一定会产生额外的
    同名动作（如 run_batch 资源超额后重写 design 再来一次），按 ok 计数会在
    run_confirm 之后提前凑满 8 个 ok，把 submit_claim 整个跳过去——2026-11-11
    practice A 链就是这样空跑到 B 链（B 拿到 A 的上下文后提交 A 的 claim）。
    """
    if policy is not None and policy.mode == "bounded_recovery":
        return _drive_chain_bounded(parts, policy=policy, cap=cap)
    result = ChainResult(study=parts.study)
    lane = parts.billing_lane
    stage = 0
    while stage < len(CHAIN_SEQUENCE):
        if parts.admission.posts_used(lane) >= cap:
            result.stopped = "POSTS_EXHAUSTED"
            break
        try:
            outcome = parts.loop.turn()
        except L.LoopError as exc:  # 准入/槽的终止类拒绝（含 PAUSED）
            result.actions.append({"n": len(result.actions) + 1,
                                   "action": None, "ok": False,
                                   "code": exc.code, "terminal": True,
                                   "raised": True})
            result.stopped = exc.code
            result.terminal = True
            break
        doc = outcome.as_dict()
        doc["n"] = len(result.actions) + 1
        result.actions.append(doc)
        if outcome.ok:
            result.ok_count += 1
            # 只有**当前规范位**的动作成功才推进游标；自修产生的额外动作
            # （重写 design、换 run_batch 参数等）只记账不推进。
            if outcome.action == CHAIN_SEQUENCE[stage]:
                stage += 1
            continue
        if outcome.terminal:
            result.stopped = outcome.code or "TERMINAL"
            result.terminal = True
            break
        if strict:
            result.stopped = outcome.code or "FIRST_ERROR"
            break
    result.stage = stage
    result.completed = stage >= len(CHAIN_SEQUENCE)
    result.study_state = parts.state.study_state(parts.study)
    claim = None
    for act in result.actions:
        if act.get("action") == "submit_claim":
            claim = dict(act.get("result") or {})
    if claim is not None:
        result.judge_verdict = claim.get("judge_verdict")
        result.study_verdict = claim.get("verdict")
    return result


def _failure_evidence(parts: LaneParts, outcome: L.StepOutcome,
                      policy: "P.ProtocolPolicy") -> RCV.FailureEvidence:
    """把一次失败回合转成**宿主证据**（状态/意图/副作用均取自宿主面）。"""
    del policy
    status: "dict[str, Any]" = {}
    try:
        status = parts.state.study_status(parts.study)
    except Exception:  # noqa: BLE001 —— 状态不可读本身是缺证据（→ terminal）
        status = {}
    result = dict(outcome.result or {})
    code = str(outcome.code or result.get("code") or "").strip()
    phase = str(result.get("phase") or "").strip()
    if not phase:
        phase = "confirm" if outcome.action in ("run_confirm", "submit_claim") \
            else "explore"
    effect = str(result.get("side_effect_status") or RCV.EFFECT_UNKNOWN)
    return RCV.FailureEvidence(
        phase=phase,
        frozen=bool(status.get("confirm_lock")),
        open_intent=bool(status.get("open_intent")),
        effect_status=effect,
        code=code,
        error_class=str(result.get("exception_type") or ""),
        transport=bool(result.get("transport_failure")),
        budget_exhausted=code == "BUDGET_REFUSED",
        method_change=bool(result.get("method_change")),
    )


def _drive_chain_bounded(parts: LaneParts, *, policy: "P.ProtocolPolicy",
                         cap: int) -> ChainResult:
    """R1.2 有界自修驱动：证据判别 + 里程碑游标 + 依赖余量门。

    与 legacy ``drive_chain`` 的差别：
    * 失败不按 ``strict`` 一刀切，而是逐次过 ``classify_failure``；
    * 可恢复失败**不重发原请求**、也不跳步：候选必须在同一 attempt 自己修正，
      每次额外调用照计 POST 与沙盒；
    * 每次发送前算 ``min_remaining_actions``；剩余不足即
      ``BUDGET_UNREACHABLE``（不发明知无法闭合的调用）；
    * 报告带 ``qualification``：八位置首次尝试 + 最终完成两面分开输出。
    """
    result = ChainResult(study=parts.study)
    milestones = RCV.MilestoneState(policy=policy)
    lane = parts.billing_lane
    while not milestones.completed:
        used = parts.admission.posts_used(lane)
        remaining = int(cap) - used
        need = milestones.min_remaining_actions()
        if remaining <= 0:
            result.stopped = "POSTS_EXHAUSTED"
            break
        if remaining < need:
            milestones.note_budget_stop(need=need, remaining=remaining)
            result.stopped = RCV.CODE_BUDGET_UNREACHABLE
            break
        try:
            outcome = parts.loop.turn()
        except L.LoopError as exc:  # 准入/槽的终止类拒绝（含 PAUSED）
            result.actions.append({"n": len(result.actions) + 1,
                                   "action": None, "ok": False,
                                   "code": exc.code, "terminal": True,
                                   "raised": True})
            result.stopped = exc.code
            result.terminal = True
            break
        doc = outcome.as_dict()
        doc["n"] = len(result.actions) + 1
        result.actions.append(doc)
        if outcome.ok:
            result.ok_count += 1
            event = milestones.record_success(
                action=outcome.action, result=dict(outcome.result or {}),
                action_index=len(result.actions))
            if event.get("terminal"):
                doc["recovery"] = {"kind": RCV.RECOVERY_TERMINAL,
                                   "code": event["terminal"],
                                   "reason": event.get("detail", ""),
                                   "recoverable": False}
                result.stopped = str(event["terminal"])
                result.terminal = True
                break
            continue
        decision = RCV.classify_failure(
            _failure_evidence(parts, outcome, policy), policy=policy)
        doc["recovery"] = decision.as_dict()
        milestones.note_failure(action=outcome.action,
                                result=dict(outcome.result or {}),
                                decision=decision,
                                action_index=len(result.actions))
        if decision.kind != RCV.RECOVERY_RECOVERABLE:
            result.stopped = decision.code or outcome.code or "TERMINAL"
            result.terminal = True
            break
    result.stage = milestones.cursor
    result.completed = milestones.completed
    result.study_state = parts.state.study_state(parts.study) \
        if hasattr(parts.state, "study_state") else ""
    result.qualification = milestones.qualification(
        total_actions=len(result.actions), posts_cap=int(cap))
    claim = None
    for act in result.actions:
        if act.get("action") == "submit_claim":
            claim = dict(act.get("result") or {})
    if claim is not None:
        result.judge_verdict = claim.get("judge_verdict")
        result.study_verdict = claim.get("verdict")
    return result


def _check_gate(ctx: Any, lane: str, *, billing_lane: "str | None" = None
                ) -> "tuple[L.LockDocument, dict[str, Any], dict[str, Any] | None]":
    """过门前复核：主 root 必须在门位，LOCK 必须已冻结且双签有效。

    修复重做（``billing_lane=repair_reserve``）额外要求：一次性授权件在场、
    协议与门位配對（probe→FROZEN、practice→PROBED）、practice 须有同 lane 的
    probe PASS 在先（§8.1；每协议只重做一次）。
    """
    lock, report = LK.require_frozen(ctx)
    root = S.ResearchState(ctx).root_state()
    want = ROOT_GATE[lane]
    if root != want:
        raise ProtocolError(
            "ROOT_GATE",
            f"{lane} 需要 root={want}，当前 {root}（先完成前一阶段）")
    if lock.envelope_id != ctx.envelope_id:
        raise ProtocolError("LOCK_MISMATCH",
                            f"LOCK.envelope={lock.envelope_id!r} != {ctx.envelope_id!r}")
    auth = None
    if billing_lane is not None and billing_lane != lane:
        if billing_lane != RPR.LANE:
            raise ProtocolError("LANE_UNKNOWN",
                                f"重做记账 lane 只能是 {RPR.LANE!r}，收到 {billing_lane!r}")
        try:
            auth = RPR.require_repair_gate(ctx, root=root, protocol=lane)
            RPR.require_protocol_sequence(ctx, protocol=lane)
        except RPR.RepairError as exc:
            raise ProtocolError(exc.code, exc.detail) from None
    return lock, report, auth


def posts_used(ctx: Any, *, lane: str, lock: L.LockDocument,
                spend: A.SpendContract) -> int:
    """lane 已用 POST 数（槽预留 ∪ 账本实呼取大者，与准入同口径）。"""
    admission = A.CampaignAdmission(
        ctx=ctx, state=S.ResearchState(ctx),
        # 按 schema 分派：R1.2 根写 budget.v2（12/16/48/48），旧根写 v1。
        budget=C.budget_from_dict(ctx.read_json("contract/budget.json")),
        lock_sha256=lock.sha256, spend=spend, lane=lane)
    # 槽表必须挂上：``posts_used`` 取「已预留槽 ∪ 台账实呼」的较大者，
    # 缺了槽表就只剩台账行——零付费演练会恒读 0，把 8/16 呼判成没发生。
    admission.attach_slots(A.SlotBook(ctx, admission.budget))
    return admission.posts_used(lane)


class HeartbeatPump:
    """worker 侧心律：独立线程每 60 s 写一次心跳（Plan §8.4）。

    为什么必须独立线程：确认批是 96 个 run 的阻塞调用，单线程「每回合写一次」
    会在长批次里静默超过 5 分钟，外部监护者会（正确地）判 SLA 违约并暂停
    dispatch。心跳写失败**不吞异常**——线程死掉正是监护者该看见的故障。
    """

    def __init__(self, state_dir: Path, *, owner: str = "rsi-r1-protocol",
                 interval_s: float = W.HEARTBEAT_INTERVAL_S) -> None:
        self.heartbeat = W.Heartbeat(state_dir, owner=owner)
        self.interval_s = float(interval_s)
        self.phase = "starting"
        self._stop = threading.Event()
        self._thread: "threading.Thread | None" = None

    def set_phase(self, phase: str) -> None:
        # 立刻写一拍：让「当前在哪个 lane/study」在磁盘上可观测，而不是等
        # 下一个 60 s 周期（短期链可能在下一个心跳前就结束）。
        self.phase = str(phase)
        self.heartbeat.beat(phase=self.phase)

    def _loop(self) -> None:
        while not self._stop.wait(self.interval_s):
            self.heartbeat.beat(phase=self.phase)

    def start(self) -> "HeartbeatPump":
        self.heartbeat.beat(phase=self.phase)
        self._thread = threading.Thread(target=self._loop,
                                        name="rsi-r1-heartbeat", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(5.0, self.interval_s * 2))
        self._thread = None


def r12_studies(lane: str, policy: "P.ProtocolPolicy") -> "tuple[str, ...]":
    """一条 lane 要跑几条玩具链（R1.2 两 lane 各**一条**，不是 A+B）。"""
    if policy.single_practice_chain:
        return (S.STUDIES[0],)
    return (S.STUDIES[0],) if lane == PROBE else PRACTICE_STUDIES


def run_protocol(ctx: Any, *, lane: str, spend: A.SpendContract,
                 transport_factory: Callable[[], Callable[[T.Request], L.Reply]],
                 runner: Any, image: str, batch_id: str,
                 counter: Callable[[str], int],
                 clock: Callable[[], str] = utc_now_iso_z,
                 backend_factory: "Callable[[str], Any] | None" = None,
                 billing_lane: "str | None" = None,
                 profile: "P.CampaignProfile | None" = None,
                 resource_ledger: "Any" = None,
                 ) -> "dict[str, Any]":
    """跑一条协议链并落 ``protocol/report_<lane>.json``，PASS 时推进主 root。

    ``billing_lane=repair_reserve`` 是 §8.1 的仪器修复重做：协议动作/报告仍按
    ``lane``（probe/practice）组织，但**全部 POST 记 repair_reserve 槽**，且
    每协议只允许 attempt 1 一次。

    ``profile`` 给 R1.2 profile 时走有界自修语义（Plan §3.1）：两 lane 各
    **一条**玩具链、帽取 profile 的 12/16、PASS 判据是「游标走完 + 无终止停 +
    实呼 ≤ 帽」，绝不要求恰好 12 或 16；``--repair`` 在该 profile 下没有入口。
    """
    policy = profile.protocol_policy if profile is not None else P.LEGACY_POLICY
    bounded = policy.mode == "bounded_recovery"
    if lane not in PROTOCOL_LANES:
        raise ProtocolError("LANE_UNKNOWN", repr(lane))
    billing = lane if billing_lane is None else str(billing_lane)
    if billing not in LANES.LANE_ORDER:
        raise ProtocolError("LANE_UNKNOWN", repr(billing))
    if bounded and billing != lane:
        raise ProtocolError(
            "REPAIR_NOT_AVAILABLE",
            "RSI-R1.2 无 repair_reserve；付费后宿主缺陷只能整轮停，不得边改边测")
    if billing != lane and billing != RPR.LANE:
        raise ProtocolError(
            "LANE_UNKNOWN", f"只有 {RPR.LANE!r} 可作为重做记账 lane，收到 {billing!r}")
    if not str(batch_id).strip():
        raise ProtocolError("BATCH_MISSING", "batch_id 不得为空")
    lock, freeze_report, auth = _check_gate(ctx, lane, billing_lane=billing)
    root_before = S.ResearchState(ctx).root_state()
    attempt = next_attempt(ctx, lane)
    if auth is not None and attempt != 1:
        raise ProtocolError(
            "REPAIR_ATTEMPT_EXHAUSTED",
            f"修复重做 {lane} 已有 attempt-{attempt - 1}：§8.1 每协议只允许一次")
    started = clock()
    wanted_posts = PROBE_POSTS if lane == PROBE else PRACTICE_POSTS
    # 正常链路用 lane 的绝对帽（probe 8 / practice 16）；修复重做按「本 lane
    # 已用 + 本协议额度」封闭，绝不把 repair 的余量借给未发生的动作。
    # R1.2：帽取 profile 的 12/16（Plan §1.1），不是旧 8/16 等值判据。
    if profile is not None and bounded:
        cap = int(profile.lane_post_caps[billing])
    else:
        cap = wanted_posts
    studies = r12_studies(lane, policy) if bounded else (
        (S.STUDIES[0],) if lane == PROBE else PRACTICE_STUDIES)
    strict = lane == PROBE and not bounded

    used_before = posts_used(ctx, lane=billing, lock=lock, spend=spend)
    if billing != lane:
        cap = used_before + wanted_posts

    chains: "list[ChainResult]" = []
    pump = HeartbeatPump(ctx.subdir("state")).start()
    try:
        transport = transport_factory()
        for study in studies:
            pump.set_phase(f"{lane}:study-{study}")
            parts = build_lane_parts(
                ctx, lane=lane, study=study, attempt=attempt, spend=spend,
                lock=lock, transport_factory=lambda: transport, runner=runner,
                image=image, counter=counter, backend_factory=backend_factory,
                billing_lane=billing, profile=profile,
                resource_ledger=_resource_ledger(ctx, profile, resource_ledger))
            chains.append(drive_chain(parts, strict=strict, cap=cap,
                                      policy=policy if bounded else None))
            if chains[-1].stopped:
                break
    finally:
        pump.stop()

    used_after = posts_used(ctx, lane=billing, lock=lock, spend=spend)

    ok_chains = all(c.completed and not c.stopped for c in chains)
    posts = used_after - used_before
    if bounded:
        # R1.2：完成判据 = 游标走完 + 无终止停 + POST ≤ 帽（不要求恰好 12/16）。
        passed = bool(ok_chains and len(chains) == len(studies) and posts <= cap)
        if passed:
            verdict, reason = VERDICT_PASS, (
                f"{len(chains)}×{CHAIN_ACTIONS} 规范动作全走完，实呼 {posts} ≤ 帽 {cap}")
        else:
            verdict = VERDICT_NOT_MEASURED
            reason = _not_measured_reason_bounded(chains, posts, cap)
    else:
        passed = bool(ok_chains and len(chains) == len(studies)
                      and posts == wanted_posts)
        if passed:
            verdict, reason = VERDICT_PASS, (
                f"{len(chains)}×{CHAIN_ACTIONS} 动作全 ok，恰好 {posts} 呼")
        else:
            verdict = VERDICT_NOT_MEASURED
            reason = _not_measured_reason(chains, posts, wanted_posts)

    report = {
        "schema": REPORT_SCHEMA, "schema_version": SCHEMA_VERSION,
        "lane": lane, "attempt": attempt, "billing_lane": billing,
        "repair": _repair_block(auth, billing),
        "envelope_id": ctx.envelope_id, "lock_sha256": lock.sha256,
        "lock_verified": bool(freeze_report.get("ok")),
        "lock_signatures": freeze_report.get("signatures") or {},
        "image": image, "batch_id": batch_id,
        "started_at_utc": started, "ended_at_utc": clock(),
        "verdict": verdict, "reason": reason,
        "policy": {"strict_first_error": bool(strict),
                   "chain_actions": CHAIN_ACTIONS,
                   "posts_allowed": wanted_posts, "post_cap": cap,
                   "mode": policy.mode,
                   "completion": ("cursor_and_cap" if bounded
                                  else "all_ok_and_exact_posts"),
                   "single_practice_chain": bool(policy.single_practice_chain)},
        "posts": {"used_before": used_before, "used_after": used_after,
                  "this_attempt": posts,
                  "allowed": cap if bounded else wanted_posts},
        "root_before": root_before, "root_after": root_before,
        "studies": [c.as_dict() for c in chains],
        "attempt_relpath": attempt_prefix(lane, attempt),
        "qualification": _campaign_qualification(chains) if bounded else None,
        "notes": ("协议演练：只判动作路径是否走通，不产生科学结论；"
                  "submit_claim 的判据输出原样记录，不因演练而改写。"),
    }
    if report["qualification"] is None:
        del report["qualification"]
    if passed:
        state = S.ResearchState(ctx)
        state.advance_root(ROOT_NEXT[lane],
                           reason=f"{lane} 协议链 PASS（attempt {attempt}）")
        report["root_after"] = state.root_state()
    rel = f"{PROTOCOL_SUBDIR}/report_{lane}.json"
    ctx.write_json(rel, report)
    return report


def _repair_block(auth: "dict[str, Any] | None",
                  billing: str) -> "dict[str, Any] | None":
    """报告里的修复归因块：只有 repair_reserve 运行才有内容。"""
    if billing != RPR.LANE:
        return None
    if auth is None:  # 防御性：lane 已核过授权，正常不会到这。
        return {"authorization": "MISSING"}
    return {"authorization_relpath": RPR.AUTH_RELPATH,
            "authorization_sha256": auth.get("document_sha256"),
            "prior_root": auth.get("prior_root"),
            "carryover_relpath": auth.get("carryover_relpath"),
            "single_use": True}


def _campaign_qualification(chains: "list[ChainResult]") -> "dict[str, Any]":
    """整条 lane 的资格检查汇总（逐 studiy 明细 + 合计两面分开报）。"""
    studies = [{"study": c.study, "qualification": c.qualification}
               for c in chains]
    positions = [row for c in chains
                 for row in ((c.qualification or {}).get("positions") or [])]
    attempted = [p for p in positions if p.get("first_attempt_ok") is not None]
    return {
        "schema": RCV.QUALIFICATION_SCHEMA,
        "studies": studies,
        "first_attempt_ok_count": sum(
            1 for p in attempted if p.get("first_attempt_ok")),
        "attempted_positions": len(attempted),
        "first_error": next((c.qualification.get("first_error") for c in chains
                             if (c.qualification or {}).get("first_error")), None),
        "recoverable_failures": [f for c in chains
                                 for f in ((c.qualification or {})
                                           .get("recoverable_failures") or [])],
        "note": ("小样本资格检查（两 lane 各一条链），只报首次尝试与最终完成两面；"
                 "不构成稳定成功率估计。"),
    }


def _not_measured_reason_bounded(chains: "list[ChainResult]", posts: int,
                                 cap: int) -> str:
    """R1.2：按里程碑游标与帽报原因（不是「实呼 != 允许」的等值判据）。"""
    parts: "list[str]" = []
    for chain in chains:
        if chain.stopped:
            parts.append(f"study {chain.study} 停于 {chain.stopped}"
                         f"（里程碑 {chain.stage}/{CHAIN_ACTIONS}）")
        elif not chain.completed:
            parts.append(f"study {chain.study} 只走完里程碑 "
                         f"{chain.stage}/{CHAIN_ACTIONS}")
        note = (chain.qualification or {}).get("stop_note")
        if note:
            parts.append(str(note))
    if posts > cap:
        parts.append(f"实呼 {posts} > 帽 {cap}")
    return "；".join(parts) or "链未走通"


def _not_measured_reason(chains: "list[ChainResult]", posts: int,
                         wanted: int) -> str:
    parts: "list[str]" = []
    for chain in chains:
        if chain.stopped:
            parts.append(f"study {chain.study} 停于 {chain.stopped}"
                         f"（规范动作 {chain.stage}/{CHAIN_ACTIONS}）")
        elif not chain.completed:
            parts.append(f"study {chain.study} 只完成规范动作 "
                         f"{chain.stage}/{CHAIN_ACTIONS}")
    if posts != wanted:
        parts.append(f"实呼 {posts} != 允许 {wanted}")
    return "；".join(parts) or "链未走通"


# ---------------------------------------------------------------------------
# 批次批准件（规则 2：每批实呼前必须有书面批准，批准件落 quota/approvals/）
# ---------------------------------------------------------------------------

def approval_doc(*, batch_id: str, lane: str, approved: int,
                 note: str = "") -> "dict[str, Any]":
    """本批批准件的**期望字段**（与 ``QuotaLedger.write_approval`` 逐键对照）。"""
    from orchestrator.rsi_r1 import lanes as LANES

    # 批准件按**记账 lane** 出票：仪器修复重做的票是 repair_reserve（task 4），
    # 不是被重做的协议名——预算审计必须能一眼看出这 8/16 呼动了储备。
    if lane not in LANES.LANE_ORDER:
        raise ProtocolError("LANE_UNKNOWN", repr(lane))
    if isinstance(approved, bool) or not isinstance(approved, int) or approved <= 0:
        raise ProtocolError("APPROVAL_INVALID", f"approved={approved!r}")
    return {
        "batch_id": str(batch_id), "lane": lane,
        "task": int(LANES.LANE_TASK_ID[lane]), "stage": "coding_debug",
        "approved": int(approved),
        "note": note or f"RSI-R1 {lane} 协议链（自批；Plan §8.1 预算内）",
    }


def ensure_approval(ctx: Any, *, batch_id: str, lane: str, approved: int,
                    note: str = "", namespace: Any = None) -> Path:
    """幂等落批准件（缺省 RSI-R1 账套 2903；R1.2 传 2904 namespace）。

    唯一写入口是 ``QuotaLedger.write_approval``（拒绝覆盖）；已存在时逐键复核
    ``batch_id/task/stage/approved``，不一致即 ``APPROVAL_DRIFT``（不静默复用
    别人的票）。``ctx`` 只用来复核 campaign 根一致，不另写第二份。
    """
    from orchestrator.gateway import quota_ledger as QL

    ns = QL.RSI_R1_NAMESPACE if namespace is None else namespace
    want = approval_doc(batch_id=batch_id, lane=lane, approved=approved,
                        note=note)
    path = ns.approvals_dir_fn() / f"{want['batch_id']}.json"
    # approvals 目录是 ``<campaign>/quota/approvals``：逐段比对而不是数
    # ``.parent`` 层数（数层数容易错一层，把误投的票当成合法票）。
    expected_dir = ctx.root.resolve() / "quota" / "approvals"
    if path.resolve().parent != expected_dir:
        raise ProtocolError(
            "APPROVAL_ROOT_MISMATCH",
            f"批准件目录 {path.parent} 不在 campaign 根 {ctx.root} 下")
    if path.is_file():
        old = json.loads(path.read_text(encoding="utf-8"))
        for key in ("batch_id", "task", "stage", "approved"):
            if old.get(key) != want[key]:
                raise ProtocolError(
                    "APPROVAL_DRIFT",
                    f"{path.name} 的 {key}={old.get(key)!r} != 本次 {want[key]!r}")
        return path
    return QL.write_approval(
        batch_id=want["batch_id"], task=int(want["task"]),
        stage=str(want["stage"]), approved=int(approved), beyond_normal=False,
        note=str(want["note"]), namespace=ns)


__all__ = [
    "ANALYSIS_TEMPLATE", "CHAIN_ACTIONS", "ChainResult", "DESIGN_TEMPLATE",
    "ESTIMATOR_SPEC", "LaneParts", "PRACTICE", "PRACTICE_POSTS",
    "PRACTICE_STUDIES", "PROBE", "PROBE_POSTS", "PROTOCOL_LANES",
    "ProtocolError", "REPORT_SCHEMA", "ROOT_GATE", "ROOT_NEXT", "ScopedContext",
    "TOY_CONDITIONS", "VERDICT_NOT_MEASURED", "VERDICT_PASS", "approval_doc",
    "attempt_prefix", "backend_factory_for", "build_lane_parts", "drive_chain",
    "ensure_approval", "mirror_root", "next_attempt", "posts_used",
    "render_protocol_brief", "repo_sha", "HeartbeatPump",
    "render_study_brief", "run_protocol", "toy_plan_doc", "r12_studies",
]
