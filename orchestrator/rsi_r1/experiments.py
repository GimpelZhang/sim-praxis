"""RSI-R1 T3：候选代码与可信执行分离（Plan §5.3；AC15/AC17/AC18 半边）。

四条接口（计划原文函数名）：

- :func:`compile_design` —— 把候选 design 源码的 sha 绑定到公开合同与 plan，
  产出可信的 ``DesignArtifact``；**不执行**候选代码（执行在 ``sandbox.py``）。
- :func:`validate_matrix` —— 候选输出的探索矩阵是**不可信 JSON**：逐字段验证后由
  host 重建 ``run_id``、``spec_hash``、``units``、整数 ticks 与步数；候选自报的
  成本类字段一律 ``UNKNOWN_KEY``（AC15）。
- :func:`allocate_confirm` —— 确认批的私有 block 由 host 用 keeper seed 生成，
  候选永远看不到；条件只看已锁 plan 的条件（不得看确认结果再选条件）。
- :func:`execute_batch` —— 逐 spec 预留资源后交给 host backend 真跑；基础设施
  失败即整批 ``blocked``，**不把剩余样本补成 32**；科学失稳的终止 raw 是有效数据。

本模块不 import mujoco / docker：真实物理只在 ``sandbox.py`` 与 backend 实现里。
"""

from __future__ import annotations

import functools
import hashlib
import json
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Iterable, Mapping

from orchestrator.rsi_r1 import contracts as C
from orchestrator.rsi_r1 import lanes as LANES
from orchestrator.rsi_r1 import rubric as R
from orchestrator.rsi_r1 import scenes as S

#: 产物 schema（canonical JSON 内明写版本，§5.1）。
DESIGN_SCHEMA = f"{S.SCHEMA_PREFIX}.design.v1"
SPEC_SCHEMA = f"{S.SCHEMA_PREFIX}.spec.v1"
MATRIX_SCHEMA = f"{S.SCHEMA_PREFIX}.matrix.v1"
CONFIRM_MANIFEST_SCHEMA = f"{S.SCHEMA_PREFIX}.confirm_manifest.v1"

#: 探索矩阵：候选可提供的**全部**字段。任何额外键（含 nsteps/cost/budget 等
#: 自报成本）都是 ``UNKNOWN_KEY``——host 自己重建这些。
MATRIX_KEYS = frozenset({"schema", "schema_version", "study_id", "plan_sha",
                         "cells"})
CELL_KEYS = frozenset({"condition_id", "block_id", "params"})

#: Study A 的积分步长由 host 附加（场景 A 的公开参数里没有 dt，候选不可覆盖）。
A_FIXED_DT_S = Decimal("0.0005")

#: 确认批采样网格：把 [lo, hi] 等分成 10 000 份后取整到 1e-6，避免二进制浮点。
CONFIRM_GRID = 10_000
_QUANT = Decimal("0.000001")

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class ExperimentError(C.ContractError):
    """带机器码的拒绝；subclass ContractError 以便上层统一捕获。"""


def _err(code: str, detail: str) -> ExperimentError:
    return ExperimentError(code, detail)


def _public(fn):
    """公开面只抛 ExperimentError：内部 ContractError 统一转码后重抛。"""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ExperimentError:
            raise
        except C.ContractError as exc:
            raise ExperimentError(exc.code, exc.detail) from None

    return wrapper


# ---------------------------------------------------------------------------
# 小工具：hash / canonical
# ---------------------------------------------------------------------------

def _sha_doc(body: Mapping[str, Any]) -> str:
    return C.canonical_sha256(body)


def _require_hex(value: Any, *, where: str) -> str:
    if not isinstance(value, str) or not _HEX64.fullmatch(value):
        raise _err("SOURCE_SHA_INVALID", f"{where} 必须是 64 位小写 hex，实为 {value!r}")
    return value


def _seed_hex(seed: Any) -> str:
    """把 keeper seed 规范成 64 位 hex **摘要**；manifest 永不携带原始 seed。"""
    if isinstance(seed, (bytes, bytearray)):
        raw = bytes(seed)
        if len(raw) < 16:
            raise _err("SEED_INVALID", "keeper seed 至少 16 字节")
        return hashlib.sha256(raw).hexdigest()
    if isinstance(seed, str):
        if not _HEX64.fullmatch(seed):
            raise _err("SEED_INVALID", "keeper seed 字符串必须是 64 位小写 hex")
        return hashlib.sha256(seed.encode("utf-8")).hexdigest()
    raise _err("SEED_INVALID", f"keeper seed 类型 {type(seed).__name__} 不接受")


def _plain(value: Decimal) -> str:
    """Decimal → 无指数、无多余零的十进制字面量。"""
    if value == 0:
        return "0"
    text = format(value.normalize(), "f")
    return text


def _scene_params(scene_id: str, raw: Mapping[str, Any]) -> "dict[str, Any]":
    try:
        return S.normalise_params(scene_id, raw)
    except S.SceneError as exc:
        raise _err(exc.code, exc.detail or exc.code) from None


def _run_shape(scene_id: str, params: Mapping[str, Any]) -> "dict[str, int]":
    if scene_id == S.SCENE_A:
        dt = A_FIXED_DT_S
        tc = None
    else:
        dt = Decimal(params["dt_s"])
        tc = Decimal(params["tc_s"])
    try:
        return S.run_shape(dt, tc_s=tc)
    except S.SceneError as exc:
        raise _err(exc.code, exc.detail or exc.code) from None


# ---------------------------------------------------------------------------
# DesignArtifact：候选源码身份绑定（不执行）
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DesignArtifact:
    """候选 design 源码在 host 侧的身份绑定件。

    ``source_sha256`` 必须等于候选 ``write_artifact`` 落盘时的 sha；``plan_sha256``
    与 ``public_contract_sha256`` 是编译时看到的公开面。任何一项漂移都会在
    ``run_batch``/``freeze_confirm`` 时被重新比对拒绝。
    """

    source_sha256: str
    plan_sha256: str
    public_contract_sha256: str
    kind: str = "design"

    def body(self) -> "dict[str, Any]":
        return {"schema": DESIGN_SCHEMA, "schema_version": 1,
                "kind": self.kind, "source_sha256": self.source_sha256,
                "plan_sha256": self.plan_sha256,
                "public_contract_sha256": self.public_contract_sha256}

    def to_dict(self) -> "dict[str, Any]":
        return {**self.body(), "design_sha256": self.sha256}

    @property
    def sha256(self) -> str:
        return _sha_doc(self.body())


@_public
def compile_design(source_sha: str, public_contract: "Mapping[str, Any] | str",
                   plan_sha: str) -> DesignArtifact:
    """绑定候选 design 源码身份；不执行、不读取候选文件内容。

    ``public_contract`` 可以是公开合同的 JSON 文档（取其 canonical sha）或已
    计算的 64 位 hex；两者都不采信候选自报。
    """
    src = _require_hex(source_sha, where="design.source_sha256")
    plan = _require_hex(plan_sha, where="design.plan_sha256")
    if isinstance(public_contract, str):
        contract_sha = _require_hex(public_contract, where="design.public_contract_sha256")
    else:
        contract_sha = _sha_doc(C.require_object(public_contract,
                                                 where="public_contract"))
    return DesignArtifact(source_sha256=src, plan_sha256=plan,
                          public_contract_sha256=contract_sha)


# ---------------------------------------------------------------------------
# ExperimentSpec：host 重建的可信规格
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ExperimentSpec:
    schema: str
    study_id: str
    plan_sha256: str
    condition_id: str
    block_id: str
    scene_id: str
    physical_params: "Mapping[str, str]"
    controller_params: "Mapping[str, str]"
    dt_s: str
    tc_s: "str | None"
    duration_s: str
    units: "Mapping[str, str]"
    ticks: "Mapping[str, int]"
    run_id: str
    spec_hash: str

    def body(self) -> "dict[str, Any]":
        return {
            "schema": self.schema, "schema_version": 1,
            "study_id": self.study_id, "plan_sha256": self.plan_sha256,
            "condition_id": self.condition_id, "block_id": self.block_id,
            "scene_id": self.scene_id,
            "physical_params": dict(self.physical_params),
            "controller_params": dict(self.controller_params),
            "dt_s": self.dt_s, "tc_s": self.tc_s,
            "duration_s": self.duration_s,
            "units": dict(self.units), "ticks": dict(self.ticks),
            "run_id": self.run_id,
        }

    def to_dict(self) -> "dict[str, Any]":
        return {**self.body(), "spec_hash": self.spec_hash}

    @property
    def nsteps(self) -> int:
        return int(self.ticks["nsteps"])


def run_id_for(*, plan_sha256: str, study_id: str, condition_id: str,
               block_id: str) -> str:
    """run_id 由 plan + study + condition + block 身份确定（候选不能随意复用）。"""
    material = C.canonical_json({"plan": plan_sha256, "study": study_id,
                                 "condition": condition_id, "block": block_id})
    return "run-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:20]


def _build_spec(*, plan: C.ResearchPlan, condition: C.Condition,
                block_id: str, params: Mapping[str, Any]) -> ExperimentSpec:
    return build_host_spec(plan_sha256=plan.sha256, study_id=plan.study_id,
                           condition_id=condition.id, block_id=block_id,
                           scene_id=condition.scene_id, params=params)


@_public
def build_host_spec(*, plan_sha256: str, study_id: str, condition_id: str,
                    block_id: str, scene_id: str, params: Mapping[str, Any]
                    ) -> ExperimentSpec:
    """由 host 侧已校验的身份 + 参数构造 ``ExperimentSpec``（唯一构造点）。

    候选矩阵路径（:func:`validate_matrix`）与维护侧校准路径
    （``calibration.py``）都走这里，保证 run_id/spec_hash/units/整数 ticks
    只有一份实现。
    """
    _require_hex(plan_sha256, where="plan_sha256")
    if study_id not in ("A", "B"):
        raise _err("STUDY_UNKNOWN", f"study_id={study_id!r}")
    doc = S.scene_doc(scene_id)
    if doc.study != study_id:
        raise _err("STUDY_MISMATCH", f"{scene_id} 属于 study {doc.study!r}")
    normalised = _scene_params(scene_id, params)
    shape = _run_shape(scene_id, normalised)
    physical: "dict[str, str]" = {}
    controller: "dict[str, str]" = {}
    units: "dict[str, str]" = {}
    for spec in doc.params:
        bucket = physical if spec.role != "controller" else controller
        bucket[spec.id] = normalised[spec.id]
        units[spec.id] = spec.unit
    if scene_id == S.SCENE_A:
        dt_s, tc_s = str(A_FIXED_DT_S), None
    else:
        dt_s, tc_s = normalised["dt_s"], normalised["tc_s"]
    body = {
        "schema": SPEC_SCHEMA, "schema_version": 1,
        "study_id": study_id, "plan_sha256": plan_sha256,
        "condition_id": condition_id, "block_id": block_id,
        "scene_id": scene_id,
        "physical_params": physical, "controller_params": controller,
        "dt_s": dt_s, "tc_s": tc_s, "duration_s": str(S.DURATION_S),
        "units": units, "ticks": dict(shape),
        "run_id": run_id_for(plan_sha256=plan_sha256, study_id=study_id,
                             condition_id=condition_id, block_id=block_id),
    }
    return ExperimentSpec(
        schema=SPEC_SCHEMA, study_id=study_id, plan_sha256=plan_sha256,
        condition_id=condition_id, block_id=block_id, scene_id=scene_id,
        physical_params=physical, controller_params=controller,
        dt_s=dt_s, tc_s=tc_s, duration_s=str(S.DURATION_S), units=units,
        ticks=dict(shape), run_id=body["run_id"], spec_hash=_sha_doc(body))


def _parse_json(value: Any, *, where: str) -> Mapping[str, Any]:
    if isinstance(value, (str, bytes, bytearray)):
        try:
            value = json.loads(value)
        except (ValueError, UnicodeDecodeError) as exc:
            raise _err("MALFORMED_JSON", f"{where}: {exc}") from None
    return C.require_object(value, where=where)


# ---------------------------------------------------------------------------
# validate_matrix：探索矩阵逐字段验证 + host 重建
# ---------------------------------------------------------------------------

@_public
def validate_matrix(matrix_json: Any, plan: C.ResearchPlan,
                    budget: C.BudgetSpec) -> "tuple[ExperimentSpec, ...]":
    """把候选探索矩阵规范化成可信 ``ExperimentSpec`` 序列（AC15）。

    拒绝面：unknown 字段（含自报 nsteps/cost/budget）、越域参数、未知条件、
    重复 (condition, block)、超出探索 runs 帽、总步数超整轮帽、plan 漂移。
    """
    obj = _parse_json(matrix_json, where="matrix")
    C.require_exact_keys(obj, MATRIX_KEYS, where="matrix")
    if obj["schema"] != MATRIX_SCHEMA:
        raise _err("SCHEMA_MISMATCH", f"matrix.schema={obj['schema']!r} != {MATRIX_SCHEMA}")
    C.require_int(obj["schema_version"], where="matrix.schema_version")
    study_id = C.require_str(obj["study_id"], where="matrix.study_id")
    if study_id != plan.study_id:
        raise _err("STUDY_MISMATCH", f"matrix.study_id={study_id!r} != plan {plan.study_id!r}")
    plan_sha = _require_hex(obj["plan_sha"], where="matrix.plan_sha")
    if plan_sha != plan.sha256:
        raise _err("PLAN_MISMATCH", "matrix.plan_sha 与当前 plan sha 不符")

    cells = obj["cells"]
    if not isinstance(cells, list) or not cells:
        raise _err("WRONG_TYPE", "matrix.cells 必须是非空数组")
    cap = min(LANES.MAX_EXPLORE_RUNS_PER_STUDY, budget.max_runs_by_lane["explore"])
    if len(cells) > cap:
        raise _err("RESOURCE_EXCEEDED",
                   f"matrix.cells={len(cells)} 超过探索 runs 帽 {cap}")

    conditions = {c.id: c for c in plan.conditions}
    specs: "list[ExperimentSpec]" = []
    seen_cells: "set[tuple[str, str]]" = set()
    seen_runs: "set[str]" = set()
    steps = 0
    for i, raw in enumerate(cells):
        cell = C.require_object(raw, where=f"matrix.cells[{i}]")
        C.require_exact_keys(cell, CELL_KEYS, where=f"matrix.cells[{i}]")
        cid = C.require_safe_id(cell["condition_id"],
                                where=f"matrix.cells[{i}].condition_id")
        condition = conditions.get(cid)
        if condition is None:
            raise _err("CONDITION_UNKNOWN",
                       f"matrix.cells[{i}].condition_id={cid!r} 不在 plan.conditions")
        block_id = C.require_safe_id(cell["block_id"],
                                     where=f"matrix.cells[{i}].block_id")
        key = (cid, block_id)
        if key in seen_cells:
            raise _err("DUPLICATE_ID",
                       f"matrix.cells[{i}] 重复 (condition, block)={key}")
        seen_cells.add(key)
        params = C.require_object(cell["params"], where=f"matrix.cells[{i}].params")
        spec = _build_spec(plan=plan, condition=condition, block_id=block_id,
                           params=params)
        if spec.run_id in seen_runs:
            raise _err("DUPLICATE_ID", f"run_id={spec.run_id} 重复")
        seen_runs.add(spec.run_id)
        steps += spec.nsteps
        specs.append(spec)
    if steps > budget.max_steps:
        raise _err("RESOURCE_EXCEEDED",
                   f"矩阵总步数 {steps} 超过整轮帽 {budget.max_steps}")
    return tuple(specs)


# ---------------------------------------------------------------------------
# allocate_confirm：host 私有 block（候选不可见）
# ---------------------------------------------------------------------------

def _varying_ranges(plan: C.ResearchPlan,
                    scene_id: str) -> "dict[str, tuple[Decimal, Decimal]]":
    doc = S.scene_doc(scene_id)
    by_id = {p.id: p for p in doc.params}
    ranges: "dict[str, tuple[Decimal, Decimal]]" = {}
    public = S.parameter_ranges(scene_id)
    for pid, (lo_raw, hi_raw) in plan.nuisance_distribution.varying.items():
        spec = by_id.get(pid)
        if spec is None:
            raise _err("UNKNOWN_PARAM", f"varying 参数 {pid!r} 不在 {scene_id}")
        if spec.kind == "enum":
            raise _err("ENUM_NOT_SAMPLEABLE",
                       f"varying 参数 {pid!r} 是枚举，不能连续采样")
        if pid not in public:
            raise _err("UNKNOWN_PARAM", f"varying 参数 {pid!r} 没有公开连续域")
        lo, hi = Decimal(str(lo_raw)), Decimal(str(hi_raw))
        if lo > hi:
            raise _err("DISTRIBUTION", f"varying.{pid} 区间倒置 [{lo},{hi}]")
        full_lo, full_hi = public[pid]
        if lo < full_lo or hi > full_hi:
            raise _err("DISTRIBUTION",
                       f"varying.{pid}=[{lo},{hi}] 越出公开域 [{full_lo},{full_hi}]")
        if lo == hi:
            raise _err("DISTRIBUTION_DEGENERATE", f"varying.{pid} 是点质量")
        ranges[pid] = (lo, hi)
    if not ranges:
        raise _err("DISTRIBUTION_DEGENERATE", "nuisance_distribution.varying 为空")
    try:
        S.check_distribution_non_degenerate(scene_id, ranges)
    except S.SceneError as exc:
        raise _err(exc.code, exc.detail or exc.code) from None
    return ranges


def _sample(seed_hex: str, *, study_id: str, param_id: str,
            block_index: int, lo: Decimal, hi: Decimal) -> str:
    material = f"{seed_hex}|{study_id}|{param_id}|{block_index}".encode("utf-8")
    n = int.from_bytes(hashlib.sha256(material).digest()[:8], "big")
    k = n % (CONFIRM_GRID + 1)
    value = lo + (hi - lo) * Decimal(k) / Decimal(CONFIRM_GRID)
    value = value.quantize(_QUANT)
    if value < lo:
        value = lo
    if value > hi:
        value = hi
    return _plain(value)


@dataclass(frozen=True)
class ConfirmAssignment:
    condition_id: str
    block_id: str
    params: "Mapping[str, str]"

    def to_dict(self) -> "dict[str, Any]":
        return {"condition_id": self.condition_id, "block_id": self.block_id,
                "params": dict(self.params)}


@dataclass(frozen=True)
class ConfirmManifest:
    """确认批的私有分配件。

    ``keeper_seed_sha256`` 只是 seed 的 hash——manifest 本身不含原始 seed，
    因此可以在不泄漏 block 生成器的前提下进入证据包；原始 seed 由 host 的
    私有存储持有（``sandbox``/``state`` 不进渲染）。
    """

    study_id: str
    plan_sha256: str
    keeper_seed_sha256: str
    block_count: int
    varying: "Mapping[str, tuple[str, str]]"
    assignments: "tuple[ConfirmAssignment, ...]"

    def body(self) -> "dict[str, Any]":
        return {
            "schema": CONFIRM_MANIFEST_SCHEMA, "schema_version": 1,
            "study_id": self.study_id, "plan_sha256": self.plan_sha256,
            "keeper_seed_sha256": self.keeper_seed_sha256,
            "block_count": self.block_count,
            "varying": {k: [v[0], v[1]] for k, v in self.varying.items()},
            "assignments": [a.to_dict() for a in self.assignments],
        }

    def to_dict(self) -> "dict[str, Any]":
        return {**self.body(), "manifest_sha256": self.sha256}

    @property
    def sha256(self) -> str:
        return _sha_doc(self.body())

    @property
    def n_assignments(self) -> int:
        return len(self.assignments)


@_public
def allocate_confirm(plan: C.ResearchPlan, keeper_seed: Any) -> ConfirmManifest:
    """host 用私有 keeper seed 把已锁 plan 的分布展开成 32×条件 的确认批。

    候选在这个函数里没有任何输入面：它只能看到 plan 里**事先公开并冻结**的
    varying 区间与条件，看不到逐 block 抽样值。
    """
    if plan.phase != "confirm":
        raise _err("PLAN_PHASE_INVALID", f"allocate_confirm 只接受 confirm 计划，实为 {plan.phase!r}")
    if not plan.conditions:
        raise _err("CONDITION_UNKNOWN", "confirm 计划没有条件")
    if len(plan.conditions) > R.MAX_CONDITIONS:
        raise _err("MAX_CONDITIONS",
                   f"条件数 {len(plan.conditions)} 超过 {R.MAX_CONDITIONS}")
    scenes = {c.scene_id for c in plan.conditions}
    if len(scenes) != 1:
        raise _err("SCENE_MISMATCH", f"confirm 条件跨场景 {sorted(scenes)}")
    scene_id = scenes.pop()
    block_count = plan.nuisance_distribution.block_count
    if block_count != R.CONFIRM_BLOCKS:
        raise _err("CONFIRM_BLOCK_COUNT",
                   f"block_count={block_count} != 量表固定 {R.CONFIRM_BLOCKS}")
    seed = _seed_hex(keeper_seed)
    ranges = _varying_ranges(plan, scene_id)

    assignments: "list[ConfirmAssignment]" = []
    for condition in plan.conditions:
        for index in range(block_count):
            params = dict(condition.params)
            for pid, (lo, hi) in ranges.items():
                if pid not in params:
                    raise _err("MISSING_PARAM",
                               f"条件 {condition.id} 缺 varying 参数 {pid!r}")
                params[pid] = _sample(seed, study_id=plan.study_id, param_id=pid,
                                      block_index=index, lo=lo, hi=hi)
            checked = _scene_params(scene_id, params)
            assignments.append(ConfirmAssignment(condition_id=condition.id,
                                                 block_id=f"b{index + 1:02d}",
                                                 params=checked))
    if len(assignments) * 1 > LANES.MAX_CONFIRM_RUNS_PER_STUDY:
        raise _err("RESOURCE_EXCEEDED",
                   f"确认批 {len(assignments)} 超过 {LANES.MAX_CONFIRM_RUNS_PER_STUDY} runs")
    return ConfirmManifest(
        study_id=plan.study_id, plan_sha256=plan.sha256,
        keeper_seed_sha256=seed, block_count=block_count,
        varying={pid: (str(lo), str(hi)) for pid, (lo, hi) in ranges.items()},
        assignments=tuple(assignments))


# ---------------------------------------------------------------------------
# execute_batch：资源预留 + host backend 真跑
# ---------------------------------------------------------------------------

class BackendInfraError(RuntimeError):
    """基础设施失败：整批 BLOCKED，不补样本。

    机器码取自消息的首个 token（**必须 ASCII 全大写**，如 ``"RAW_SHA_MISMATCH ..."``）；
    没有这种前缀时退回 ``INFRA_FAILURE``。``RunOutcome.error_code`` 用它。
    注意 CJK 也满足 ``str.isalpha()/isalnum()/isupper()``，故必须显式限定 ASCII 字母表。
    """

    code = "INFRA_FAILURE"
    _CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")

    def __init__(self, detail: str = "", *, code: "str | None" = None) -> None:
        super().__init__(detail)
        if code is not None:
            self.code = code
            return
        token = str(detail).split(" ", 1)[0].rstrip(":").strip()
        if self._CODE_RE.match(token):
            self.code = token


class BackendScientificStop(RuntimeError):
    """科学失稳产生的有效终止 raw（可发布，不补样本）。"""

    code = "SCIENTIFIC_STOP"

    def __init__(self, detail: str, record: "Mapping[str, Any] | None" = None) -> None:
        super().__init__(detail)
        self.record = dict(record) if record is not None else None


@dataclass
class ResourceBook:
    """每批次资源账：runs 与物理步数，预留成功才计费。"""

    max_runs: int
    max_steps: int
    used_runs: int = 0
    used_steps: int = 0

    def reserve(self, spec: ExperimentSpec) -> None:
        if self.used_runs + 1 > self.max_runs:
            raise _err("RESOURCE_EXCEEDED",
                       f"runs {self.used_runs + 1} 超过帽 {self.max_runs}")
        if self.used_steps + spec.nsteps > self.max_steps:
            raise _err("RESOURCE_EXCEEDED",
                       f"steps {self.used_steps + spec.nsteps} 超过帽 {self.max_steps}")
        self.used_runs += 1
        self.used_steps += spec.nsteps

    def to_dict(self) -> "dict[str, Any]":
        return {"max_runs": self.max_runs, "max_steps": self.max_steps,
                "used_runs": self.used_runs, "used_steps": self.used_steps}


@dataclass(frozen=True)
class RunOutcome:
    run_id: str
    condition_id: str
    block_id: str
    ok: bool
    record: "Mapping[str, Any] | None"
    stop_reason: "str | None" = None
    error_code: "str | None" = None

    def to_dict(self) -> "dict[str, Any]":
        return {"run_id": self.run_id, "condition_id": self.condition_id,
                "block_id": self.block_id, "ok": self.ok,
                "record": None if self.record is None else dict(self.record),
                "stop_reason": self.stop_reason, "error_code": self.error_code}


@dataclass(frozen=True)
class BatchEvidence:
    study_id: str
    plan_sha256: str
    outcomes: "tuple[RunOutcome, ...]"
    blocked: bool
    reason: "str | None"
    resources: "Mapping[str, Any]" = field(default_factory=dict)

    @property
    def n_runs(self) -> int:
        return len(self.outcomes)

    @property
    def n_ok(self) -> int:
        return sum(1 for o in self.outcomes if o.ok)

    def to_dict(self) -> "dict[str, Any]":
        return {"schema": f"{S.SCHEMA_PREFIX}.batch_evidence.v1",
                "schema_version": 1, "study_id": self.study_id,
                "plan_sha256": self.plan_sha256, "blocked": self.blocked,
                "reason": self.reason, "n_runs": self.n_runs,
                "n_ok": self.n_ok, "resources": dict(self.resources),
                "outcomes": [o.to_dict() for o in self.outcomes]}


def _verify_spec(spec: ExperimentSpec) -> None:
    if not isinstance(spec, ExperimentSpec):
        raise _err("WRONG_TYPE", f"spec 类型 {type(spec).__name__} 不接受")
    rebuild = _sha_doc(spec.body())
    if rebuild != spec.spec_hash:
        raise _err("SPEC_TAMPERED",
                   f"{spec.run_id}: spec_hash 重算 {rebuild[:16]}… 与登记 "
                   f"{spec.spec_hash[:16]}… 不符")
    expected = run_id_for(plan_sha256=spec.plan_sha256, study_id=spec.study_id,
                          condition_id=spec.condition_id, block_id=spec.block_id)
    if expected != spec.run_id:
        raise _err("SPEC_TAMPERED", f"run_id {spec.run_id!r} 与派生 {expected!r} 不符")


@_public
def execute_batch(specs: Iterable[ExperimentSpec], backend: Any,
                  resource_book: Any) -> BatchEvidence:
    """逐 spec 预留资源后调用 ``backend.run(spec)``；不采信候选自报成本。

    ``backend`` 只需实现 ``run(spec) -> Mapping``；infra 失败抛
    :class:`BackendInfraError`，科学失稳抛 :class:`BackendScientificStop`。
    基础设施失败后**不再执行剩余 spec**（整批 blocked，不补样本）。
    """
    items = tuple(specs)
    if not items:
        raise _err("WRONG_TYPE", "execute_batch 需要非空 specs")
    studies = {s.study_id for s in items}
    plans = {s.plan_sha256 for s in items}
    if len(studies) != 1 or len(plans) != 1:
        raise _err("STUDY_MISMATCH",
                   f"同一批只能一个 study/plan，实为 {sorted(studies)}/{len(plans)} plan")
    seen: "set[str]" = set()
    for spec in items:
        _verify_spec(spec)
        if spec.run_id in seen:
            raise _err("DUPLICATE_ID", f"run_id={spec.run_id} 在批内重复")
        seen.add(spec.run_id)

    outcomes: "list[RunOutcome]" = []
    blocked = False
    reason: "str | None" = None
    for spec in items:
        resource_book.reserve(spec)
        try:
            record = backend.run(spec)
        except BackendScientificStop as exc:
            outcomes.append(RunOutcome(run_id=spec.run_id,
                                       condition_id=spec.condition_id,
                                       block_id=spec.block_id, ok=True,
                                       record=exc.record,
                                       stop_reason=str(exc) or "scientific_stop"))
            continue
        except BackendInfraError as exc:
            outcomes.append(RunOutcome(run_id=spec.run_id,
                                       condition_id=spec.condition_id,
                                       block_id=spec.block_id, ok=False,
                                       record=None, error_code=exc.code))
            blocked = True
            reason = f"{exc.code}: {exc}"
            break
        except Exception as exc:  # noqa: BLE001 —— 未预期失败同样留证，不静默
            outcomes.append(RunOutcome(run_id=spec.run_id,
                                       condition_id=spec.condition_id,
                                       block_id=spec.block_id, ok=False,
                                       record=None,
                                       error_code="BACKEND_ERROR"))
            blocked = True
            reason = f"BACKEND_ERROR: {type(exc).__name__}: {exc}"
            break
        if not isinstance(record, Mapping):
            outcomes.append(RunOutcome(run_id=spec.run_id,
                                       condition_id=spec.condition_id,
                                       block_id=spec.block_id, ok=False,
                                       record=None, error_code="BACKEND_ERROR"))
            blocked = True
            reason = "BACKEND_ERROR: backend.run 未返回 mapping"
            break
        outcomes.append(RunOutcome(run_id=spec.run_id,
                                   condition_id=spec.condition_id,
                                   block_id=spec.block_id, ok=True,
                                   record=dict(record)))
    resources = (resource_book.to_dict()
                 if hasattr(resource_book, "to_dict") else {})
    return BatchEvidence(study_id=items[0].study_id,
                         plan_sha256=items[0].plan_sha256,
                         outcomes=tuple(outcomes), blocked=blocked,
                         reason=reason, resources=resources)
