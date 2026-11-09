"""RSI-R0.1 合同单一真值：schema 常量、状态枚举、数值合同与规范化哈希。

计划 §12 要求「T1/T2 的 schema 与 T4 的结案状态先统一，再并行实现，不能各自
产生冲突格式」。本模块即该统一面：所有 schema 字符串、状态字面量、数值常量与
canonical JSON / sha256 实现都只在这里出现一次，其他 R0.1 模块与测试一律引用，
禁止抄字面量。

数值常量出处（计划 §4.2/§4.3/§5.1/§6.2/§6.3）：

* 观测：``dt=0.002``、2000 步、``t=0`` 初态 ⇒ 2001 行，末点恰 ``T=4 s``；
* 带内：``|θ|<0.05 rad AND |ω|<0.05 rad/s``（严格小于，等于阈值不在带内）；
* 最小有意义效应 ``δ=0.10``（不是数值容差，不随付费结果移动）；
* 每 POST 输入 ≤20000 token、``max_output=8192``；
* 分析：源码 ≤32 KiB、stdout+stderr ≤16 KiB、输出单文件 ≤64 KiB、
  每 trial ≤8 次、单次 ≤30 s、1 CPU / 512 MiB / pids 64。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

# ---------------------------------------------------------------------------
# schema 身份
# ---------------------------------------------------------------------------

PREFIX = "simpraxis.rsi_r01"

SCHEMA_CAMPAIGN_CONTEXT = f"{PREFIX}.campaign_context.v1"
SCHEMA_CONSENT = f"{PREFIX}.consent.v1"
SCHEMA_BASELINE = f"{PREFIX}.baseline.v1"
SCHEMA_ENVIRONMENT_CHECK = f"{PREFIX}.environment_check.v1"
SCHEMA_SEALED_ROOTS = f"{PREFIX}.sealed_roots.v1"
SCHEMA_TASK_SELECTION = f"{PREFIX}.task_selection.v1"
SCHEMA_SERVICE_REQUEST = f"{PREFIX}.service.v1"
SCHEMA_OBSERVATION = f"{PREFIX}.observation.v1"
SCHEMA_OBSERVATION_MANIFEST = f"{PREFIX}.observation_manifest.v1"
SCHEMA_ACTION = f"{PREFIX}.action.v1"
SCHEMA_ACTION_EVENT = f"{PREFIX}.action_event.v1"
SCHEMA_ANALYSIS_SOURCE = f"{PREFIX}.analysis_source.v1"
SCHEMA_ANALYSIS_RESULT = f"{PREFIX}.analysis_result.v1"
SCHEMA_TASK_MATRIX = f"{PREFIX}.task_matrix.v1"
SCHEMA_MEASUREMENT_CONTRACT = f"{PREFIX}.measurement_contract.v1"
SCHEMA_PREREG = f"{PREFIX}.prereg.v1"
SCHEMA_SUBMISSION = f"{PREFIX}.submission.v1"
SCHEMA_VERDICT = f"{PREFIX}.verdict.v1"
SCHEMA_INPUT_ACCOUNTING = f"{PREFIX}.input_accounting.v1"
SCHEMA_ATTEMPT = f"{PREFIX}.attempt.v1"
SCHEMA_LOCK = f"{PREFIX}.lock.v1"
SCHEMA_CLOSEOUT = f"{PREFIX}.closeout.v1"
SCHEMA_AC_MATRIX = f"{PREFIX}.ac_matrix.v1"
SCHEMA_SLOT_MATRIX = f"{PREFIX}.slot_matrix.v1"

# ---------------------------------------------------------------------------
# 观测与数值合同
# ---------------------------------------------------------------------------

DT_S = 0.002
N_STEPS = 2000
EXPECTED_ROWS = N_STEPS + 1           # t=0 初态 + 2000 步
T_END_S = round(DT_S * N_STEPS, 12)   # 4.0
THETA_BAND = 0.05
OMEGA_BAND = 0.05
DELTA = 0.10

#: 观测文件大小上限（计划 §4.2：≤8 MiB/run，每 trial 公开观测 ≤64 MiB）。
MAX_OBSERVATION_BYTES = 8 * 1024 * 1024
MAX_TRIAL_OBSERVATION_BYTES = 64 * 1024 * 1024
#: 工具回包摘要上限（计划 §4.2：≤4 KiB 摘要）。
MAX_PREVIEW_BYTES = 4096

# ---------------------------------------------------------------------------
# 分析容器限制（计划 §5.1）
# ---------------------------------------------------------------------------

ANALYSIS_MAX_SOURCE_BYTES = 32 * 1024
ANALYSIS_MAX_STREAM_BYTES = 16 * 1024      # stdout+stderr 合计
ANALYSIS_MAX_OUTPUT_FILE_BYTES = 64 * 1024
ANALYSIS_MAX_PER_TRIAL = 8
ANALYSIS_TIMEOUT_S = 30
ANALYSIS_CPUS = 1
ANALYSIS_MEMORY = "512m"
ANALYSIS_PIDS = 64
ANALYSIS_OUTPUT_NAMES = ("analysis.json",)
ANALYSIS_ENTRY_NAME = "prog.py"

# ---------------------------------------------------------------------------
# 调用与预算（计划 §6.2/§6.3）
# ---------------------------------------------------------------------------

MAX_INPUT_TOKENS = 20000
MAX_OUTPUT_TOKENS = 8192
PROTOCOL_POSTS = 12
DEV_POSTS_PER_FAMILY = 24
VAL_POSTS_PER_FAMILY = 24
TOTAL_NORMAL_POSTS = 228
CONDITIONAL_VALIDATE_POSTS = 4
TOTAL_WITH_CONDITIONAL_POSTS = 232

#: 资源上限（计划 §6.3）。
TRIAL_WALLCLOCK_MAX_S = 45 * 60
CANDIDATE_EXPERIMENTS_PER_TRIAL = 12
MAX_SIM_STEPS_PER_CALL = 100_000
MAX_SIM_STEPS_PER_TRIAL = 1_000_000
MAX_SIM_STEPS_TOTAL = 10_000_000

# ---------------------------------------------------------------------------
# 状态枚举（字面量即合同，禁止别处另造同义串）
# ---------------------------------------------------------------------------


class ObservationStatus:
    COMPLETE = "COMPLETE"
    CENSORED = "CENSORED"
    INVALID = "INVALID"

    ALL = (COMPLETE, CENSORED, INVALID)


class ClaimStatus:
    SUPPORTED = "SUPPORTED"
    REFUTED = "REFUTED"
    INCONCLUSIVE = "INCONCLUSIVE"

    ALL = (SUPPORTED, REFUTED, INCONCLUSIVE)


class TrialState:
    """试次终态（计划 §9.3 的 trial 列）。"""

    NOT_STARTED = "NOT_STARTED"
    RUNNING = "RUNNING"
    VALID_PASS = "VALID_PASS"
    VALID_FAIL_CAP = "VALID_FAIL_CAP"
    VALID_FAIL_POLICY = "VALID_FAIL_POLICY"
    BLOCKED_CALL = "BLOCKED_CALL"
    BLOCKED_INPUT_LIMIT = "BLOCKED_INPUT_LIMIT"
    PAUSED_UNKNOWN = "PAUSED_UNKNOWN"
    CANCELLED = "CANCELLED"
    ABORTED = "ABORTED"
    INVALID = "INVALID"

    TERMINAL = (VALID_PASS, VALID_FAIL_CAP, VALID_FAIL_POLICY, BLOCKED_CALL,
                BLOCKED_INPUT_LIMIT, CANCELLED, ABORTED, INVALID)
    #: 有效能力结果：只有这两类进入能力分母（计划 §9.2 第 8 条）。
    VALID_CAPABILITY = (VALID_PASS, VALID_FAIL_CAP)


class ProcessVerdict:
    COMPLETE = "COMPLETE"
    ABORTED_INCOMPLETE = "ABORTED_INCOMPLETE"
    CLOSEOUT_INCOMPLETE = "CLOSEOUT_INCOMPLETE"

    ALL = (COMPLETE, ABORTED_INCOMPLETE, CLOSEOUT_INCOMPLETE)


class MeasurementVerdict:
    """测量/能力轴主判决（计划 §9.2，优先级自上而下）。"""

    # 1
    SAFETY_INVALID = "SAFETY_INVALID"
    # 2
    FREEZE_INVALID = "FREEZE_INVALID"
    # 3
    INVALID_INSTRUMENT = "INVALID_INSTRUMENT"
    # 4 付费前阻断（不叫能力地板）
    BLOCKED_ENV = "BLOCKED_ENV"
    BLOCKED_MODEL_CONFIG = "BLOCKED_MODEL_CONFIG"
    BLOCKED_TOKEN_ACCOUNTING = "BLOCKED_TOKEN_ACCOUNTING"
    BLOCKED_PRICE = "BLOCKED_PRICE"
    BLOCKED_APPROVAL = "BLOCKED_APPROVAL"
    BLOCKED_CONTEXT_MEASUREMENT = "BLOCKED_CONTEXT_MEASUREMENT"
    # 5
    BLOCKED_PROTOCOL = "BLOCKED_PROTOCOL"
    BLOCKED_TRANSPORT = "BLOCKED_TRANSPORT"
    BLOCKED_BUDGET = "BLOCKED_BUDGET"
    BLOCKED_INPUT_LIMIT = "BLOCKED_INPUT_LIMIT"
    # 6
    ABORTED_INCOMPLETE = "ABORTED_INCOMPLETE"
    CLOSEOUT_INCOMPLETE = "CLOSEOUT_INCOMPLETE"
    # 7
    DESIGN_NOT_READY = "DESIGN_NOT_READY"
    DEVELOPMENT_NOT_READY = "DEVELOPMENT_NOT_READY"
    # 8 能力轴
    RESEARCH_FLOOR = "RESEARCH_FLOOR"
    ENGINEERING_FLOOR = "ENGINEERING_FLOOR"
    CEILING = "CEILING"
    PILOT_CALIBRATED = "PILOT_CALIBRATED"

    #: 判定优先级（数字小者优先覆盖）。计划 §9.2 的顺序。
    PRIORITY = {
        SAFETY_INVALID: 1,
        FREEZE_INVALID: 2,
        INVALID_INSTRUMENT: 3,
        BLOCKED_ENV: 4,
        BLOCKED_MODEL_CONFIG: 4,
        BLOCKED_TOKEN_ACCOUNTING: 4,
        BLOCKED_PRICE: 4,
        BLOCKED_APPROVAL: 4,
        BLOCKED_CONTEXT_MEASUREMENT: 4,
        BLOCKED_PROTOCOL: 5,
        BLOCKED_TRANSPORT: 5,
        BLOCKED_BUDGET: 5,
        BLOCKED_INPUT_LIMIT: 5,
        ABORTED_INCOMPLETE: 6,
        CLOSEOUT_INCOMPLETE: 6,
        DESIGN_NOT_READY: 7,
        DEVELOPMENT_NOT_READY: 7,
        RESEARCH_FLOOR: 8,
        ENGINEERING_FLOOR: 8,
        CEILING: 8,
        PILOT_CALIBRATED: 8,
    }

    ALL = tuple(PRIORITY)


class ReasonCode:
    """细粒度 reason_codes（与主判决并存，全部保留）。"""

    UNRESOLVED_SEND = "UNRESOLVED_SEND"
    CAP_EXHAUSTED = "CAP_EXHAUSTED"
    CENSORED = "CENSORED"
    SCHEMA_INVALID = "SCHEMA_INVALID"
    POLICY_VIOLATION = "POLICY_VIOLATION"
    ANALYSIS_TIMEOUT = "ANALYSIS_TIMEOUT"
    ANALYSIS_OUTPUT_OVER_CAP = "ANALYSIS_OUTPUT_OVER_CAP"
    ANALYSIS_POLICY_VIOLATION = "ANALYSIS_POLICY_VIOLATION"
    PREREG_MISSING = "PREREG_MISSING"
    PREREG_MISMATCH = "PREREG_MISMATCH"
    OBSERVATION_TAMPERED = "OBSERVATION_TAMPERED"
    OBSERVATION_INCOMPLETE = "OBSERVATION_INCOMPLETE"
    EMPTY_CONTENT = "EMPTY_CONTENT"
    NON_TEXT_BLOCK = "NON_TEXT_BLOCK"
    TRUNCATED = "TRUNCATED"
    PARSE_FAILURE = "PARSE_FAILURE"
    TIMEOUT = "TIMEOUT"
    UNKNOWN_SENT = "UNKNOWN_SENT"
    PREFLIGHT_BLOCKED = "PREFLIGHT_BLOCKED"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    TOKEN_OVER_CAP = "TOKEN_OVER_CAP"
    LOCK_MISMATCH = "LOCK_MISMATCH"
    INSTRUMENT_UNVERIFIED = "INSTRUMENT_UNVERIFIED"

    ALL = tuple(v for k, v in list(locals().items()) if k.isupper() and isinstance(v, str))


class Family:
    E = "E"
    D = "D"
    R = "R"

    ALL = (E, D, R)


class Tariff:
    """实例角色（开发/验证），与族正交。"""

    DEV = "dev"
    VALIDATION = "validation"

    ALL = (DEV, VALIDATION)


# ---------------------------------------------------------------------------
# 规范化 JSON 与哈希
# ---------------------------------------------------------------------------


class ContractError(ValueError):
    """合同不满足（fail-closed，调用方须落结构化事件，不得静默修补）。"""


def canonical_json(obj: Any) -> str:
    """确定性序列化：键排序、无多余空白、拒绝 NaN/Inf、保留非 ASCII。"""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_of(obj: Any) -> str:
    return sha256_text(canonical_json(obj))


def utc_now() -> str:
    """统一时间戳格式（秒精度、严格 Z 后缀；与旧账的 +0000/+0800 混写区分）。"""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_canonical_json(path, obj: Any) -> str:
    """原子写规范化 JSON（0600），返回内容 sha256。"""
    import os
    from pathlib import Path

    p = Path(path)
    text = canonical_json(obj) + "\n"
    tmp = p.with_name(p.name + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, p)
    return sha256_text(text)


def read_json(path) -> Any:
    from pathlib import Path

    return json.loads(Path(path).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 结构化校验（AC14：任何候选 JSON 都返回类型化结果，不 crash）
# ---------------------------------------------------------------------------


def _is_num(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _finite(x: Any) -> bool:
    return _is_num(x) and (x == x) and (x not in (float("inf"), float("-inf")))


def _type_problem(path: str, want: str, got: Any) -> dict[str, str]:
    return {"path": path, "problem": "type", "expected": want,
            "actual": type(got).__name__}


#: 观测文档的两种合法形状：R 的摆逐步合同（§4.2）与 E/D 的推箱逐步记录。
OBS_KIND_PENDULUM = "pendulum_run"
OBS_KIND_PUSH = "push_control"
OBS_SERIES: dict[str, tuple[str, ...]] = {
    OBS_KIND_PENDULUM: ("theta", "omega"),
    OBS_KIND_PUSH: ("x", "y", "vx", "vy"),
}


def check_observation_doc(doc: Any, *, kind: str | None = None) -> list[dict[str, str]]:
    """校验观测文档（模型/候选回传件一律走这里，绝不抛异常）。

    返回问题列表；空列表即通过。两族形状的规则分别冻结：

    * ``pendulum_run``：**恰好 2001 行**、t 从 0 严格递增、末点恰 T=4 s（§4.2）；
    * ``push_control``：行数 = ``params.steps + 1``、末点 = steps×dt（推箱没有
      固定窗口，但同样要求首末点齐全与严格单调时间）。
    """
    p: list[dict[str, str]] = []
    if not isinstance(doc, dict):
        return [_type_problem("$", "object", doc)]
    if doc.get("schema") != SCHEMA_OBSERVATION:
        p.append({"path": "schema", "problem": "value",
                  "expected": SCHEMA_OBSERVATION, "actual": str(doc.get("schema"))[:80]})
    declared = doc.get("kind")
    k = kind or declared
    if k not in OBS_SERIES:
        return [{"path": "kind", "problem": "value",
                 "expected": "|".join(sorted(OBS_SERIES)),
                 "actual": str(declared)[:40]}]
    if isinstance(declared, str) and declared != k:
        p.append({"path": "kind", "problem": "value", "expected": k,
                  "actual": declared[:40]})
    names = ("t",) + OBS_SERIES[k]
    series_map = {name: doc.get(name) for name in names}
    for name, series in series_map.items():
        if not isinstance(series, list):
            p.append(_type_problem(name, "array", series))
    if p:
        return p
    t = series_map["t"]
    n = len(t)

    # 行数：按 kind 分别冻结（R 固定 2001；push 由请求 steps 决定）
    if k == OBS_KIND_PENDULUM:
        expected_rows = EXPECTED_ROWS
    else:
        params = doc.get("params") if isinstance(doc.get("params"), dict) else {}
        steps = params.get("steps")
        expected_rows = int(steps) + 1 if _is_num(steps) else None
        if expected_rows is None:
            p.append({"path": "params.steps", "problem": "missing",
                      "expected": "present", "actual": "absent"})
    for name, series in series_map.items():
        if len(series) != n:
            p.append({"path": name, "problem": "length_mismatch",
                      "expected": str(n), "actual": str(len(series))})
    if expected_rows is not None and n != expected_rows:
        p.append({"path": "rows", "problem": "row_count",
                  "expected": str(expected_rows), "actual": str(n)})

    limit = min(n, max(EXPECTED_ROWS, 4096) + 8)
    for i in range(limit):
        for name, series in series_map.items():
            v = series[i]
            if not _finite(v):
                p.append({"path": f"{name}[{i}]", "problem": "not_finite",
                          "expected": "finite", "actual": repr(v)[:40]})
                if len(p) > 40:
                    return p
        if i and _finite(t[i]) and _finite(t[i - 1]) and not (t[i] > t[i - 1]):
            p.append({"path": f"t[{i}]", "problem": "not_monotonic",
                      "expected": "strictly increasing", "actual": f"{t[i-1]}->{t[i]}"})
    if n and _finite(t[0]) and abs(t[0]) > 1e-12:
        p.append({"path": "t[0]", "problem": "value", "expected": "0", "actual": str(t[0])})
    if k == OBS_KIND_PENDULUM:
        if n and _finite(t[-1]) and abs(t[-1] - T_END_S) > 1e-9:
            p.append({"path": "t[-1]", "problem": "value", "expected": str(T_END_S),
                      "actual": str(t[-1])})
    elif n and _finite(t[-1]) and expected_rows is not None:
        params = doc.get("params") or {}
        dt = params.get("timestep")
        steps = params.get("steps")
        if _is_num(dt) and _is_num(steps):
            t_end = float(steps) * float(dt)
            if abs(t[-1] - t_end) > 1e-9:
                p.append({"path": "t[-1]", "problem": "value",
                          "expected": repr(t_end), "actual": str(t[-1])})
    return p


def check_manifest_doc(doc: Any) -> list[dict[str, str]]:
    p: list[dict[str, str]] = []
    if not isinstance(doc, dict):
        return [_type_problem("$", "object", doc)]
    if doc.get("schema") != SCHEMA_OBSERVATION_MANIFEST:
        p.append({"path": "schema", "problem": "value",
                  "expected": SCHEMA_OBSERVATION_MANIFEST,
                  "actual": str(doc.get("schema"))[:80]})
    for key in ("trial_id", "run_id", "instance_id", "scene_sha256",
                "observation_sha256", "rows", "t_start", "t_end"):
        if key not in doc:
            p.append({"path": key, "problem": "missing", "expected": "present",
                      "actual": "absent"})
    if isinstance(doc.get("rows"), bool) or not isinstance(doc.get("rows"), int):
        if "rows" in doc:
            p.append(_type_problem("rows", "int", doc.get("rows")))
    else:
        kind = doc.get("kind") or OBS_KIND_PENDULUM
        params = doc.get("params") if isinstance(doc.get("params"), dict) else {}
        if kind == OBS_KIND_PENDULUM:
            expected_rows: int | None = EXPECTED_ROWS
        else:
            steps = params.get("steps")
            expected_rows = int(steps) + 1 if _is_num(steps) else None
        if expected_rows is not None and doc["rows"] != expected_rows:
            p.append({"path": "rows", "problem": "row_count",
                      "expected": str(expected_rows), "actual": str(doc["rows"])})
    for key in ("t_start", "t_end"):
        if key in doc and not _finite(doc[key]):
            p.append({"path": key, "problem": "not_finite", "expected": "finite",
                      "actual": repr(doc[key])[:40]})
    params = doc.get("params")
    if params is not None and not isinstance(params, dict):
        p.append(_type_problem("params", "object", params))
    return p


def check_action_doc(doc: Any) -> list[dict[str, str]]:
    """候选动作（模型响应解析后）的结构校验（AC14 / §5.1 每响应一个动作）。"""
    p: list[dict[str, str]] = []
    if not isinstance(doc, dict):
        return [_type_problem("$", "object", doc)]
    if doc.get("schema") != SCHEMA_ACTION:
        p.append({"path": "schema", "problem": "value",
                  "expected": SCHEMA_ACTION, "actual": str(doc.get("schema"))[:80]})
    action = doc.get("action")
    allowed = ("preregister", "run_experiment", "analyze", "submit")
    if action not in allowed:
        p.append({"path": "action", "problem": "value",
                  "expected": "|".join(allowed), "actual": str(action)[:40]})
    return p


def observation_status_of(doc: Mapping[str, Any]) -> str:
    """由数据本身判定观测状态（不看候选自报）。"""
    problems = check_observation_doc(doc)
    return ObservationStatus.INVALID if problems else ObservationStatus.COMPLETE


def pick_primary_verdict(codes) -> tuple[str, tuple[str, ...]]:
    """按 §9.2 优先级挑主判决，保留全部 reason_codes（去重、稳定排序）。"""
    uniq = []
    for code in codes:
        if code not in uniq:
            uniq.append(code)
    if not uniq:
        raise ContractError("没有可判定的判决码")
    unknown = [c for c in uniq if c not in MeasurementVerdict.PRIORITY]
    if unknown:
        raise ContractError(f"未知判决码：{unknown}")
    primary = min(uniq, key=lambda c: MeasurementVerdict.PRIORITY[c])
    return primary, tuple(uniq)
