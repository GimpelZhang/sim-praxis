"""RSI-R0 合同层：实例/试次/分层结果/判决/资源帽的单一真值定义。

本模块是 **纯数据与纯函数**，不做 IO、不读环境、不触网、不建目录。其余 R0 模块
（runner/evaluate/controls/cli）一律从这里取枚举与常量，禁止各自抄字面量——
计划 §4.2「用同一判据口径」的前提就是状态集与优先级只有一个定义点。

关键约定（计划 §4.1/§9.1）：

- **分层结果**：L0 有效响应/协议，L1 真实产物，L2 真实加载，L3 真实仿真步进，
  L4 功能目标，L5 研究结论有效。每层只能取 :class:`LayerStatus` 五个值之一。
  L5 对 E/D 族 :data:`LayerStatus.NOT_APPLICABLE`，不进研究分母。
- **两轴分离**：科学标签（SUPPORTED/REFUTED/INCONCLUSIVE）与执行标签
  （COMPLETE/FAILED/BLOCKED）互不替代；工程崩溃不等于否定假设。
- **唯一主判决**：:data:`CALIBRATION_VERDICT_PRIORITY` 给出全序；未覆盖状态
  fail-closed 为 ``ABORTED_INCOMPLETE/reason=UNCLASSIFIED``，由人工查明。
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

#: 合同 schema 版本。任何字段语义变化都必须升这个号（判决件与包都记录它）。
CONTRACT_SCHEMA = "rsi_r0.contracts.v1"

# ---------------------------------------------------------------- 任务与族


class Family(str, Enum):
    """三个任务族（计划 §3.2）。"""

    E = "E"   # 真实执行
    D = "D"   # 单故障诊断
    R = "R"   # 受控微研究


#: 计划 §3.2 的九个固定实例：三族 × {dev, v1, v2}。
INSTANCE_IDS: tuple[str, ...] = (
    "E-dev", "E-v1", "E-v2",
    "D-dev", "D-v1", "D-v2",
    "R-dev", "R-v1", "R-v2",
)

#: 计划 §3.3：验证实例的**固定顺序**——不得按即时成功情况选题。
VALIDATION_ORDER: tuple[str, ...] = ("E-v1", "D-v1", "R-v1", "E-v2", "D-v2", "R-v2")

#: 开发轨迹的固定顺序（三族各一条，共 3 个 slot）。
DEVELOPMENT_ORDER: tuple[str, ...] = ("E-dev", "D-dev", "R-dev")

#: 计划 §7 T0：账本任务编号（协议 0、E 1、D 2、R 3）。
TASK_ID_PROTOCOL = 0
TASK_ID_FAMILY: Mapping[Family, int] = {Family.E: 1, Family.D: 2, Family.R: 3}

#: 每个验证实例的固定请求帽与开发轨迹同值（计划 §3.4）。
SLOT_REQUEST_CAP = 24

#: 开发轨迹的只读检查点位置（第 8/16/24 次；计划 §3.4）。
CHECKPOINTS: tuple[int, ...] = (8, 16, 24)


def instance_family(instance_id: str) -> Family:
    if instance_id not in INSTANCE_IDS:
        raise ValueError(f"未知 R0 实例：{instance_id!r}")
    return Family(instance_id.split("-", 1)[0])


def is_development(instance_id: str) -> bool:
    return instance_id.endswith("-dev")


def instance_task_id(instance_id: str) -> int:
    if instance_id == "protocol":
        return TASK_ID_PROTOCOL
    return TASK_ID_FAMILY[instance_family(instance_id)]


# ---------------------------------------------------------------- 分层状态


class Layer(str, Enum):
    """可信侧分层（计划 §4.1）。"""

    L0 = "L0"  # 有效响应 / 协议
    L1 = "L1"  # 真实产物
    L2 = "L2"  # 真实加载（可信服务）
    L3 = "L3"  # 真实仿真步进（可信服务）
    L4 = "L4"  # 功能目标
    L5 = "L5"  # 研究结论有效


#: 层序（诊断按此顺序找**首次失败层**）。
LAYER_ORDER: tuple[Layer, ...] = (
    Layer.L0, Layer.L1, Layer.L2, Layer.L3, Layer.L4, Layer.L5,
)


class LayerStatus(str, Enum):
    """单层状态。**没有**「跳过」：未测到就写 NOT_REACHED，不写零分通过。"""

    PASS = "PASS"
    FAIL = "FAIL"
    NOT_REACHED = "NOT_REACHED"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    BLOCKED_INFRA = "BLOCKED_INFRA"


#: 每族适用的层：E/D 无 L5（研究结论层不适用，不进研究分母）。
APPLICABLE_LAYERS: Mapping[Family, tuple[Layer, ...]] = {
    Family.E: (Layer.L0, Layer.L1, Layer.L2, Layer.L3, Layer.L4),
    Family.D: (Layer.L0, Layer.L1, Layer.L2, Layer.L3, Layer.L4),
    Family.R: (Layer.L0, Layer.L1, Layer.L2, Layer.L3, Layer.L4, Layer.L5),
}


def applicable_layers(family: Family) -> tuple[Layer, ...]:
    return APPLICABLE_LAYERS[family]


# ---------------------------------------------------------------- 两轴判决


class ExecutionVerdict(str, Enum):
    """执行轴（结构/工程是否走完并如实记录）。"""

    COMPLETE = "COMPLETE"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"


class ScientificVerdict(str, Enum):
    """科学轴（假设结论）。与执行轴正交。"""

    SUPPORTED = "SUPPORTED"
    REFUTED = "REFUTED"
    INCONCLUSIVE = "INCONCLUSIVE"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class CalibrationVerdict(str, Enum):
    """R0 主判决（计划 §9.1 九类 + fail-closed 兜底）。"""

    SAFETY_INVALID = "SAFETY_INVALID"
    FREEZE_INVALID = "FREEZE_INVALID"
    INVALID_INSTRUMENT = "INVALID_INSTRUMENT"
    BLOCKED_INFRA = "BLOCKED_INFRA"
    BLOCKED_MODEL = "BLOCKED_MODEL"
    BLOCKED_COST = "BLOCKED_COST"
    ABORTED_INCOMPLETE = "ABORTED_INCOMPLETE"
    PROTOCOL_BLOCKED = "PROTOCOL_BLOCKED"
    ENGINEERING_FLOOR = "ENGINEERING_FLOOR"
    RESEARCH_FLOOR = "RESEARCH_FLOOR"
    CEILING_WARNING = "CEILING_WARNING"
    CALIBRATED_PILOT = "CALIBRATED_PILOT"
    ASSISTED_ONLY = "ASSISTED_ONLY"
    UNCLASSIFIED = "UNCLASSIFIED"


#: 计划 §9.1 的**全序**（数字小 = 优先）。同时出现多个候选时取最前者为主状态，
#: 其余写进 reason_codes，绝不丢弃。
#:
#: 说明两处容易读错的次序：
#: * ``ABORTED_INCOMPLETE`` 在 ``PROTOCOL_BLOCKED`` 之前——「协议已实测失败」是
#:   计划内 gate 取消（第 6 类），而外部中断造成的未完成是第 5 类，二者不可互换；
#: * ``ASSISTED_ONLY`` 在 ``CEILING_WARNING``/``CALIBRATED_PILOT`` 之前（计划原文
#:   「此状态优先于第8/9类」），它有实质人工救援，不能准入下一阶段。
CALIBRATION_VERDICT_PRIORITY: tuple[CalibrationVerdict, ...] = (
    CalibrationVerdict.SAFETY_INVALID,
    CalibrationVerdict.FREEZE_INVALID,
    CalibrationVerdict.INVALID_INSTRUMENT,
    CalibrationVerdict.BLOCKED_INFRA,
    CalibrationVerdict.BLOCKED_MODEL,
    CalibrationVerdict.BLOCKED_COST,
    CalibrationVerdict.ABORTED_INCOMPLETE,
    CalibrationVerdict.PROTOCOL_BLOCKED,
    CalibrationVerdict.ENGINEERING_FLOOR,
    CalibrationVerdict.RESEARCH_FLOOR,
    CalibrationVerdict.ASSISTED_ONLY,
    CalibrationVerdict.CEILING_WARNING,
    CalibrationVerdict.CALIBRATED_PILOT,
)


def pick_primary_verdict(candidates: Iterable[CalibrationVerdict]) -> tuple[CalibrationVerdict, tuple[CalibrationVerdict, ...]]:
    """按全序取唯一主状态；返回 (主状态, 其余按同一全序)。空输入 fail-closed。"""
    uniq = set(candidates)
    if not uniq:
        return CalibrationVerdict.UNCLASSIFIED, ()
    ordered = [v for v in CALIBRATION_VERDICT_PRIORITY if v in uniq]
    unknown = sorted(v.value for v in uniq - set(CALIBRATION_VERDICT_PRIORITY))
    if unknown:
        raise ValueError(f"判决不在全序内：{unknown}")
    return ordered[0], tuple(ordered[1:])


# ---------------------------------------------------------------- 分层结果件


@dataclass(frozen=True)
class LayerResult:
    """单层判定：状态 + 机器可读原因码 + 原始证据定位（相对 campaign 根）。"""

    layer: Layer
    status: LayerStatus
    reason_code: str
    evidence: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "layer": self.layer.value,
            "status": self.status.value,
            "reason_code": self.reason_code,
            "evidence": list(self.evidence),
        }


def first_failure(family: Family, layers: Mapping[Layer, LayerResult]) -> Layer | None:
    """按层序找**首个**非 PASS 的层（诊断对象）。

    只看该族**适用层**：L5 对 E/D 不适用，缺席不得被当成失败（同理适用层缺席
    一律算未过——不能靠「没写」冒充通过）。
    """
    for layer in applicable_layers(family):
        res = layers.get(layer)
        if res is None:
            return layer
        if res.status is LayerStatus.NOT_APPLICABLE:
            # 适用层被标 NOT_APPLICABLE 是自相矛盾：仍按未过处理。
            return layer
        if res.status is not LayerStatus.PASS:
            return layer
    return None


def all_applicable_pass(family: Family, layers: Mapping[Layer, LayerResult]) -> bool:
    """该族全部适用层是否 PASS（不适用层必须显式 NOT_APPLICABLE，不能缺席）。"""
    for layer in applicable_layers(family):
        res = layers.get(layer)
        if res is None or res.status is not LayerStatus.PASS:
            return False
    return True


# ---------------------------------------------------------------- 实例合同


@dataclass(frozen=True)
class InstanceSpec:
    """一个冻结实例的全部可变面（T2 生成，付费前锁死；计划 §3.3）。

    真实数值（力/质量/步长/初态）**不进**本 dataclass 的公开文本字段：它们只在
    ``private_ref`` 指向的数据层件里。候选侧只拿 :attr:`public_spec_hash` 与
    公开任务书。
    """

    instance_id: str
    family: Family
    role: str                     # "development" | "validation"
    task_id: int
    public_spec_hash: str         # 公开任务书/接口文档的规范 JSON 哈希
    private_ref: str              # 数据层私有件相对路径（候选无权读取）
    seeds: tuple[int, ...]        # 每组条件的重复初态（数值稳健性，不是总体采样）
    order_index: int              # 固定执行顺序

    def as_dict(self) -> dict[str, Any]:
        return {
            "instance_id": self.instance_id,
            "family": self.family.value,
            "role": self.role,
            "task_id": self.task_id,
            "public_spec_hash": self.public_spec_hash,
            "private_ref": self.private_ref,
            "seeds": list(self.seeds),
            "order_index": self.order_index,
        }


# ---------------------------------------------------------------- 试次与尝试


class SlotState(str, Enum):
    """一个 slot（一次实例轨迹）的终态。所有 nine/three slot 都必须在分母里。"""

    COMPLETED = "COMPLETED"                 # 轨迹走完（含「失败但走完」）
    CANCELLED_BY_GATE = "CANCELLED_BY_GATE"  # 计划内 gate 取消（协议失败 / 开发未过门）
    CANCELLED = "CANCELLED"                 # 安全/资源硬门触发后的停批
    BLOCKED_INFRA = "BLOCKED_INFRA"
    NOT_STARTED = "NOT_STARTED"


class AttemptState(str, Enum):
    """单次逻辑调用的状态机（计划 §6.3）。

    ``PREPARED`` → ``SENT`` → ``SETTLED_OK`` / ``SETTLED_FAIL``；
    崩溃在发网与结算之间时恢复为 ``UNKNOWN_SENT``（耗位、预留不释放、**不重发**）。
    ``BLOCKED_PREFLIGHT`` 只可用于**可证实发网前中止**，且该 slot 仍算已消耗。
    """

    PREPARED = "PREPARED"
    SENT = "SENT"
    SETTLED_OK = "SETTLED_OK"
    SETTLED_FAIL = "SETTLED_FAIL"
    UNKNOWN_SENT = "UNKNOWN_SENT"
    BLOCKED_PREFLIGHT = "BLOCKED_PREFLIGHT"


@dataclass
class AttemptRecord:
    """一次逻辑调用的发网事实（与 ledger 分权，靠 request_id 关联）。"""

    request_id: str
    slot_id: str
    batch_id: str
    task_id: int
    stage: str
    state: AttemptState
    seq: int | None = None
    attempts_http: int = 0
    reserved_usd: float = 0.0
    reserved_tokens_in: int = 0
    reserved_tokens_out: int = 0
    usage_in: int | None = None
    usage_out: int | None = None
    http_status: int | None = None
    stop_reason: str | None = None
    content_block_types: tuple[str, ...] = ()
    provider_request_id: str | None = None
    body_sha256: str | None = None
    error_class: str | None = None
    created_at_utc: str = ""
    updated_at_utc: str = ""

    def as_row(self) -> dict[str, Any]:
        row = {
            "request_id": self.request_id,
            "slot_id": self.slot_id,
            "batch_id": self.batch_id,
            "task_id": self.task_id,
            "stage": self.stage,
            "state": self.state.value,
            "seq": self.seq,
            "attempts_http": self.attempts_http,
            "reserved_usd": self.reserved_usd,
            "reserved_tokens_in": self.reserved_tokens_in,
            "reserved_tokens_out": self.reserved_tokens_out,
            "usage_in": self.usage_in,
            "usage_out": self.usage_out,
            "http_status": self.http_status,
            "stop_reason": self.stop_reason,
            "content_block_types": list(self.content_block_types),
            "provider_request_id": self.provider_request_id,
            "body_sha256": self.body_sha256,
            "error_class": self.error_class,
            "created_at_utc": self.created_at_utc,
            "updated_at_utc": self.updated_at_utc,
        }
        return row


# ---------------------------------------------------------------- 资源帽


@dataclass(frozen=True)
class ResourceCaps:
    """计划 §6.2 的停止帽（**不是**预测消耗；任一先到即停）。"""

    per_request_input_tokens: int = 20000
    per_request_output_tokens: int = 8192
    cumulative_input_tokens: int = 4_440_000
    cumulative_output_tokens: int = 1_818_624
    revalidation_input_tokens: int = 80_000
    revalidation_output_tokens: int = 32_768
    total_usd_equivalent: float = 100.0
    trial_wall_s: int = 2700
    request_timeout_s: int = 180
    tool_command_timeout_s: int = 120
    reference_run_s: int = 180
    stage_online_wall_s: int = 12 * 3600
    stage_gpu_wall_s: int = 2 * 3600
    trial_sim_steps: int = 1_000_000
    stage_sim_steps: int = 10_000_000
    artifacts_total_bytes: int = 5 * 1024**3

    def as_dict(self) -> dict[str, Any]:
        return {
            "per_request_input_tokens": self.per_request_input_tokens,
            "per_request_output_tokens": self.per_request_output_tokens,
            "cumulative_input_tokens": self.cumulative_input_tokens,
            "cumulative_output_tokens": self.cumulative_output_tokens,
            "revalidation_input_tokens": self.revalidation_input_tokens,
            "revalidation_output_tokens": self.revalidation_output_tokens,
            "total_usd_equivalent": self.total_usd_equivalent,
            "trial_wall_s": self.trial_wall_s,
            "request_timeout_s": self.request_timeout_s,
            "tool_command_timeout_s": self.tool_command_timeout_s,
            "reference_run_s": self.reference_run_s,
            "stage_online_wall_s": self.stage_online_wall_s,
            "stage_gpu_wall_s": self.stage_gpu_wall_s,
            "trial_sim_steps": self.trial_sim_steps,
            "stage_sim_steps": self.stage_sim_steps,
            "artifacts_total_bytes": self.artifacts_total_bytes,
        }


class ResourceCapExceeded(RuntimeError):
    """任一资源帽先到：发网前拒绝（AC10）。"""


# ---------------------------------------------------------------- 规范化哈希


def canonical_json(obj: Any) -> str:
    """确定性 JSON 文本：键排序、无多余空格、UTF-8。所有哈希都以此为准。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_of(obj: Any) -> str:
    """对任意可 JSON 化对象取规范哈希。"""
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def private_write(path: Path, text: str) -> None:
    """原子写 campaign 产物并落 0600（N-4 权限；第三份评审 LOW-4 原子性）。

    旧版 ``write_text`` 再 ``chmod``：进程死在两步之间会留下半截文件，且写入瞬间
    可能是 0644。现在先在同目录建 0600 的临时件，fsync 后 ``os.replace`` 换名——
    读者要么看到旧版、要么看到完整新版，权限从第一字节起就是 0600。
    """
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------- 研究判据枚举

#: 计划 §4.2 的八项研究有效性判据（key 用于报告与负控命名，不复用序号）。
RESEARCH_CRITERIA: tuple[str, ...] = (
    "C1_preregistered_plan",        # 首次实验前保存 hypothesis/对照/预期方向
    "C2_two_levels_two_configs",    # ≥2 干预水平 × 两组预定义初态，50–2000 真实 step
    "C3_trusted_series",            # 原始时间序列由可信重放采集，分析方法事前锁定
    "C4_units_and_controls",        # 单位/控制变量正确，未偷改质量/阻尼/求解器
    "C5_independent_analysis",      # 独立分析器复算，容差冻结内一致
    "C6_conclusion_boundary",       # v1 报支持/反驳+数值；v2 用事前等效界判阴性
    "C7_scope_statement",           # 结论明示无接触/有限步长/适用边界
    "C8_inconclusive_discipline",   # INCONCLUSIVE 必须满足事前诊断覆盖
)


class CriteriaError(ValueError):
    """判据集合不完整（漏一项 = 有一个判据没人实现）。"""


def assert_all_criteria_present(keys: Iterable[str]) -> None:
    missing = set(RESEARCH_CRITERIA) - set(keys)
    if missing:
        raise CriteriaError(f"研究判据缺失：{sorted(missing)}")


# ---------------------------------------------------------------- 汇总件


@dataclass
class TrialResult:
    """一次 slot 的完整结果（写进 runs/<slot_id>/verdict.json）。"""

    slot_id: str
    instance_id: str
    family: Family
    role: str
    state: SlotState
    layers: dict[Layer, LayerResult] = field(default_factory=dict)
    execution: ExecutionVerdict = ExecutionVerdict.BLOCKED
    scientific: ScientificVerdict = ScientificVerdict.NOT_APPLICABLE
    requests_used: int = 0
    human_assistance: bool = False
    notes: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "slot_id": self.slot_id,
            "instance_id": self.instance_id,
            "family": self.family.value,
            "role": self.role,
            "state": self.state.value,
            "layers": {k.value: v.as_dict() for k, v in self.layers.items()},
            "execution": self.execution.value,
            "scientific": self.scientific.value,
            "requests_used": self.requests_used,
            "human_assistance": self.human_assistance,
            "notes": list(self.notes),
        }


def summarize_metric(results: Sequence[TrialResult]) -> dict[str, Any]:
    """原始 k/n 汇总：不生成 p 值/CIs（n=2 每族只支持可行性判断，计划 §9.1）。"""
    out: dict[str, Any] = {"families": {}, "all_assigned": len(results)}
    for fam in Family:
        fam_results = [r for r in results if r.family is fam]
        completed = [r for r in fam_results if r.state is SlotState.COMPLETED]
        out["families"][fam.value] = {
            "assigned": len(fam_results),
            "completed": len(completed),
            "execution_complete": sum(
                1 for r in completed if r.execution is ExecutionVerdict.COMPLETE),
            "scientific_supported": sum(
                1 for r in completed if r.scientific is ScientificVerdict.SUPPORTED),
            "scientific_refuted": sum(
                1 for r in completed if r.scientific is ScientificVerdict.REFUTED),
        }
    out["note"] = ("n=2/族（验证）与 n=1/族（开发）只支持可行性判断；"
                   "固定设计样本，不得推断总体成功率；不报告 p 值或置信区间。")
    return out


# ---------------------------------------------------------------- 候选交卷格式
#
# 交卷**只允许引用宿主记录的 run_id**：候选不能自己报一串数字，评测器一律拿
# run_id 回宿主事件日志取原始序列再复算（AC19「只有重放而无原实验事件必拒」）。

ANSWER_SCHEMAS: Mapping[Family, dict[str, Any]] = {
    Family.E: {
        "required": ["family", "run_id", "claim"],
        "shape": {
            "family": "E",
            "run_id": "<宿主返回的 run_id>",
            "claim": {"distance": "<m>", "final_speed": "<m/s>"},
        },
        "note": "claim 必须与宿主对该 run_id 的复算一致（1e-6 内），不采信候选自算。",
    },
    Family.D: {
        "required": ["family", "run_ids", "hypothesis"],
        "shape": {
            "family": "D",
            "run_ids": ["<至少 4 个探测 run_id：两档力 × 两个方向>"],
            "hypothesis": {"kind": "scene_tilt|friction_scale|force_scale",
                           "scale": "<幅度>"},
        },
        "note": "种类与幅度由维护侧独立分类器从这些 run_id 复算，候选只declare 假设。",
    },
    Family.R: {
        "required": ["family", "run_ids", "verdict", "effect", "criteria", "scope"],
        "shape": {
            "family": "R",
            "run_ids": ["<两个阻尼水平 × 两个初态，各 ≥1 个 run_id>"],
            "verdict": "SUPPORTED|REFUTED|INCONCLUSIVE",
            "effect": "<相对效应量>",
            "criteria": {c: "<证据说明>" for c in RESEARCH_CRITERIA},
            "scope": "<结论适用边界：无接触/有限步长/…>",
        },
        "note": ("八项判据必须逐项给出证据（C1–C8）；INCONCLUSIVE 另需 "
                 "diagnostics 列出事前诊断覆盖。"),
    },
}


def answer_schema(family: Family) -> dict[str, Any]:
    """公开任务书里的交卷格式（唯一真值；任务包与评测器都从这里取）。"""
    schema = ANSWER_SCHEMAS[family]
    return {"required": list(schema["required"]),
            "shape": dict(schema["shape"]),
            "note": schema["note"]}
