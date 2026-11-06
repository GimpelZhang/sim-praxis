"""Phase 2.8.1 T1：候选试用清单与候选评测结果的**手写严格校验器**。

为什么手写而不引 ``jsonschema``：本阶段要求「零新增依赖 + 校验器自身可被仓外锚
复算」。``jsonschema`` 虽已在 venv 里（Phase 2.8.0 的 ``schema_problems`` 用它），
但它的报错文本随版本漂移，而 AC-P281-1-02 要求**逐类点名**拒收原因、AC-P281-1-15
要求把产物回灌同一校验器。手写校验器的输出是本仓的契约，版本稳定。

与 ``run_manifest.v1`` 的关系：**零关系**。``run_manifest.v1.schema.json`` 的
``paid_model_calls: const 0`` 是 Phase 2.8.0 的冻结契约，这里**一个字节都不改**；
本模块的两份 schema 是并存的新契约（AC-P281-1-01 逐字节核对旧 schema 未动）。

``claimed`` 的语义（也写进 schema 的 description）：候选的自述，仅供交叉核对。
任何 ``claimed.*`` 与账本/观测不一致，**以账本/观测为准**，并把该 Trial 记为
``INVALID_ARTIFACT``。因此本校验器**允许** ``claimed.paid_model_calls > 0``
（AC-P281-1-03）——付费候选必须能被评测，拒绝它就等于让付费候选永远无法评测。

路径纪律：``inputs`` 必须落在 ``input/`` 下、``deliverables`` 必须落在
``delivered/`` 下。这不是审美，而是评测器「只读冻结副本」的机判面：引擎结束时
``workdir/`` 仍是候选可写的，评测器若能从 manifest 指到 ``workdir/`` 就等于读
一份可以随时被改的文件。``delivered/`` 是运行器在结束时刻克隆的冻结副本。
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from orchestrator.benchmark.phase281 import constants

__all__ = (
    "CLAIMED_KEYS",
    "INPUT_PREFIX",
    "MANIFEST_NAME",
    "MANIFEST_SCHEMA",
    "ManifestError",
    "OUTPUT_PREFIX",
    "RESULT_SCHEMA",
    "build_manifest",
    "file_facts",
    "manifest_problems",
    "relpath_ok",
    "result_problems",
    "validate_manifest",
    "validate_result",
)

MANIFEST_SCHEMA = "simpraxis.candidate_trial_manifest.v1"
RESULT_SCHEMA = "simpraxis.candidate_evaluation_result.v1"
MANIFEST_NAME = "candidate_trial_manifest.json"

#: 输入件与交付件的固定前缀（见模块 docstring 的路径纪律）
INPUT_PREFIX = "input/"
OUTPUT_PREFIX = "delivered/"

#: ``claimed`` 的六键（顺序即 schema 里的书写顺序）
CLAIMED_KEYS = ("paid_model_calls", "coding_rounds", "first_birth_round",
                "tool_steps", "tokens_in", "tokens_out")

#: 结果里的 ``criteria[].source`` / ``gates[].source`` 取值
SOURCES = ("recomputed", "ledger", "runner_telemetry", "file_sha256")

#: 七态与 Phase 2.8.0 逐字相同（``result_schema.STATUSES``）
VERDICTS = ("BLOCKED_INFRA", "INVALID_RUNTIME", "INCOMPLETE_EVIDENCE",
            "INVALID_ARTIFACT", "FAIL", "PASS", "NOT_APPLICABLE")

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_STRICT_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")
_RELPATH = re.compile(r"^(?!/)(?!.*(^|/)\.\.(/|$))[A-Za-z0-9._/-]+$")
_TRIAL_ID = re.compile(r"^[a-z0-9_]+__[a-z0-9_]+__rep[0-9]+$")
_CRIT_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{2,63}$")
_GATE_ID = re.compile(r"^GATE_[A-Z0-9_]{2,59}$")
_FIELD = re.compile(r"^(claimed|engine|runtime|inputs|deliverables)\.[A-Za-z0-9_.[\]-]+$")


class ManifestError(ValueError):
    """清单或结果不合契约；``str(exc)`` 逐条点名拒收原因（换行分隔）。"""


class _Problems:
    """收集式校验：一次报全部问题，而不是撞到第一条就抛。"""

    def __init__(self) -> None:
        self.items: "list[str]" = []

    def add(self, message: str) -> None:
        self.items.append(message)

    def require(self, condition: bool, message: str) -> bool:
        if not condition:
            self.add(message)
        return bool(condition)

    def __bool__(self) -> bool:  # pragma: no cover - 可读性糖
        return bool(self.items)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _exact_keys(out: _Problems, obj: Mapping, *, where: str,
                required: Sequence[str]) -> None:
    """``additionalProperties: false`` 的手写等价：键集必须**逐字等于**期望集。"""
    actual = set(obj)
    want = set(required)
    missing = sorted(want - actual)
    extra = sorted(actual - want)
    if missing:
        out.add(f"{where} 缺字段：{missing}")
    if extra:
        out.add(f"{where} 多字段：{extra}")


def _hex64(out: _Problems, value: Any, *, where: str) -> None:
    if not (isinstance(value, str) and _HEX64.match(value)):
        out.add(f"{where} 必须是 64 位小写十六进制：{value!r}")


def _strict_z(out: _Problems, value: Any, *, where: str) -> None:
    if not (isinstance(value, str) and _STRICT_Z.match(value)):
        out.add(f"{where} 必须是严格 Z 结尾的 ISO-8601：{value!r}")
        return
    try:
        datetime.strptime(value.replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S%z")
    except ValueError:
        # 允许小数秒：strptime 的 %S 不吃小数，故单独再试一次
        try:
            datetime.strptime(value[:-1].split(".")[0], "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            out.add(f"{where} 不是可解析的时间：{value!r}")


def relpath_ok(rel: Any) -> bool:
    """相对 POSIX 路径：非空、非绝对、无 ``..`` 段、字符集受限。"""
    return isinstance(rel, str) and bool(_RELPATH.match(rel))


def _entry_list(out: _Problems, value: Any, *, where: str,
                prefix: str) -> None:
    """``inputs`` / ``deliverables`` 的逐件校验（含固定前缀纪律）。"""
    if not isinstance(value, list):
        out.add(f"{where} 必须是列表：{type(value).__name__}")
        return
    if not value:
        out.add(f"{where} 不得为空（至少要有一件）")
        return
    for index, item in enumerate(value):
        spot = f"{where}[{index}]"
        if not isinstance(item, Mapping):
            out.add(f"{spot} 必须是对象")
            continue
        _exact_keys(out, item, where=spot, required=("relpath", "sha256", "bytes"))
        rel = item.get("relpath")
        if not relpath_ok(rel):
            out.add(f"{spot}.relpath 非法（绝对路径/.. 逃逸/字符集）：{rel!r}")
        elif not str(rel).startswith(prefix):
            out.add(f"{spot}.relpath 必须以 {prefix!r} 开头（冻结副本纪律）：{rel!r}")
        _hex64(out, item.get("sha256"), where=f"{spot}.sha256")
        size = item.get("bytes")
        if not _is_int(size) or size < 0:
            out.add(f"{spot}.bytes 必须是非负整数：{size!r}")


# --------------------------------------------------------------------------
# 清单
# --------------------------------------------------------------------------

_MANIFEST_KEYS = ("schema", "experiment_id", "trial_id", "arm", "task_id",
                  "runtime", "inputs", "deliverables", "claimed", "engine",
                  "created_at")


def manifest_problems(obj: Any) -> "list[str]":
    """返回全部拒收原因（空列表 = 合法）。不抛异常。"""
    out = _Problems()
    if not isinstance(obj, Mapping):
        return [f"清单顶层必须是对象：{type(obj).__name__}"]
    _exact_keys(out, obj, where="清单", required=_MANIFEST_KEYS)
    if obj.get("schema") != MANIFEST_SCHEMA:
        out.add(f"schema 必须逐字等于 {MANIFEST_SCHEMA!r}：{obj.get('schema')!r}")
    if obj.get("experiment_id") != constants.EXPERIMENT_ID:
        out.add("experiment_id 必须逐字等于 "
                f"{constants.EXPERIMENT_ID!r}：{obj.get('experiment_id')!r}")
    trial = obj.get("trial_id")
    if not (isinstance(trial, str) and _TRIAL_ID.match(trial)):
        out.add(f"trial_id 形态非法（应为 <task_id>__<arm_id>__rep<k>）：{trial!r}")
    else:
        # 反向校验：用 constants 重建一次，防止三段各自合法但组合不合法
        parts = trial.split("__")
        if len(parts) == 3 and parts[2].startswith("rep"):
            try:
                rebuilt = constants.trial_id(parts[0], parts[1],
                                             int(parts[2][3:]))
            except (ValueError, TypeError) as exc:
                out.add(f"trial_id 含未登记的任务/臂/rep：{exc}")
            else:
                if rebuilt != trial:
                    out.add(f"trial_id 重建不一致：{trial!r} vs {rebuilt!r}")
    arm = obj.get("arm")
    if arm not in constants.ARMS:
        out.add(f"arm 必须是 {list(constants.ARMS)} 之一：{arm!r}")
    if obj.get("task_id") not in constants.TASK_IDS:
        out.add(f"task_id 必须是 {list(constants.TASK_IDS)} 之一："
                f"{obj.get('task_id')!r}")

    runtime = obj.get("runtime")
    if not isinstance(runtime, Mapping):
        out.add(f"runtime 必须是对象：{type(runtime).__name__}")
    else:
        _exact_keys(out, runtime, where="runtime",
                    required=("mode", "fingerprint_sha256"))
        if runtime.get("mode") not in ("host", "docker"):
            out.add(f"runtime.mode 必须是 'host' 或 'docker'："
                    f"{runtime.get('mode')!r}")
        _hex64(out, runtime.get("fingerprint_sha256"),
               where="runtime.fingerprint_sha256")

    _entry_list(out, obj.get("inputs"), where="inputs", prefix=INPUT_PREFIX)
    _entry_list(out, obj.get("deliverables"), where="deliverables",
                prefix=OUTPUT_PREFIX)

    claimed = obj.get("claimed")
    if not isinstance(claimed, Mapping):
        out.add(f"claimed 必须是对象：{type(claimed).__name__}")
    else:
        _exact_keys(out, claimed, where="claimed", required=CLAIMED_KEYS)
        for key in CLAIMED_KEYS:
            if key not in claimed:
                continue
            value = claimed[key]
            if key == "first_birth_round" and value is None:
                continue
            if not _is_int(value) or value < 0:
                out.add(f"claimed.{key} 必须是非负整数（first_birth_round 允许 "
                        f"null）：{value!r}")

    engine = obj.get("engine")
    if not isinstance(engine, Mapping):
        out.add(f"engine 必须是对象：{type(engine).__name__}")
    else:
        _exact_keys(out, engine, where="engine",
                    required=("entry", "config_digest"))
        if engine.get("entry") not in ("agent_runner", "agentic_loop"):
            out.add(f"engine.entry 必须是 'agent_runner' 或 'agentic_loop'："
                    f"{engine.get('entry')!r}")
        _hex64(out, engine.get("config_digest"), where="engine.config_digest")

    _strict_z(out, obj.get("created_at"), where="created_at")
    return out.items


def validate_manifest(obj: Any) -> dict:
    """合法则返回**深拷贝的规范化对象**，否则抛 :class:`ManifestError`。"""
    problems = manifest_problems(obj)
    if problems:
        raise ManifestError("清单不合契约：\n- " + "\n- ".join(problems))
    return json.loads(json.dumps(obj, ensure_ascii=False))


# --------------------------------------------------------------------------
# 结果
# --------------------------------------------------------------------------

_RESULT_KEYS = ("schema", "trial_id", "task_id", "verdict", "criteria",
                "gates", "facts", "ledger", "manifest_checks", "anchor",
                "evaluated_at", "evaluator")


def _check_criteria(out: _Problems, value: Any, *, verdict: str) -> None:
    if not isinstance(value, list):
        out.add(f"criteria 必须是列表：{type(value).__name__}")
        return
    if verdict in ("BLOCKED_INFRA", "INVALID_ARTIFACT") and value:
        out.add(f"verdict={verdict} 时 criteria 必须为空数组（输入不可信不得给分）")
    for index, item in enumerate(value):
        spot = f"criteria[{index}]"
        if not isinstance(item, Mapping):
            out.add(f"{spot} 必须是对象")
            continue
        _exact_keys(out, item, where=spot,
                    required=("id", "weight", "passed", "fact", "source"))
        cid = item.get("id")
        if not (isinstance(cid, str) and _CRIT_ID.match(cid)):
            out.add(f"{spot}.id 非法：{cid!r}")
        weight = item.get("weight")
        if not isinstance(weight, (int, float)) or isinstance(weight, bool) \
                or not (0 <= float(weight) <= 1):
            out.add(f"{spot}.weight 必须在 [0,1]：{weight!r}")
        if not isinstance(item.get("passed"), bool):
            out.add(f"{spot}.passed 必须是布尔：{item.get('passed')!r}")
        if not isinstance(item.get("fact"), Mapping):
            out.add(f"{spot}.fact 必须是对象")
        if item.get("source") not in SOURCES:
            out.add(f"{spot}.source 必须是 {list(SOURCES)} 之一："
                    f"{item.get('source')!r}")


def _check_gates(out: _Problems, value: Any, *, verdict: str) -> None:
    if not isinstance(value, list):
        out.add(f"gates 必须是列表：{type(value).__name__}")
        return
    if verdict == "BLOCKED_INFRA" and value:
        out.add("verdict=BLOCKED_INFRA 时 gates 必须为空数组")
    for index, item in enumerate(value):
        spot = f"gates[{index}]"
        if not isinstance(item, Mapping):
            out.add(f"{spot} 必须是对象")
            continue
        _exact_keys(out, item, where=spot,
                    required=("id", "passed", "detail", "source"))
        gid = item.get("id")
        if not (isinstance(gid, str) and _GATE_ID.match(gid)):
            out.add(f"{spot}.id 非法（应形如 GATE_XXX）：{gid!r}")
        if not isinstance(item.get("passed"), bool):
            out.add(f"{spot}.passed 必须是布尔：{item.get('passed')!r}")
        if not isinstance(item.get("detail"), str):
            out.add(f"{spot}.detail 必须是字符串")
        if item.get("source") not in SOURCES:
            out.add(f"{spot}.source 必须是 {list(SOURCES)} 之一："
                    f"{item.get('source')!r}")


def _check_ledger(out: _Problems, value: Any) -> None:
    if not isinstance(value, Mapping):
        out.add(f"ledger 必须是对象：{type(value).__name__}")
        return
    _exact_keys(out, value, where="ledger",
                required=("calls", "seqs", "tokens_in", "tokens_out"))
    calls = value.get("calls")
    if not _is_int(calls) or calls < 0:
        out.add(f"ledger.calls 必须是非负整数：{calls!r}")
    seqs = value.get("seqs")
    if not isinstance(seqs, list) or any(
            not _is_int(s) or s < 1 for s in seqs):
        out.add(f"ledger.seqs 必须是正整数列表：{seqs!r}")
    elif _is_int(calls) and len(seqs) != calls:
        out.add(f"ledger.seqs 长度 {len(seqs)} 与 calls={calls} 不符")
    for key in ("tokens_in", "tokens_out"):
        value_i = value.get(key)
        if not _is_int(value_i) or value_i < 0:
            out.add(f"ledger.{key} 必须是非负整数：{value_i!r}")


def _check_manifest_checks(out: _Problems, value: Any) -> None:
    if not isinstance(value, list):
        out.add(f"manifest_checks 必须是列表：{type(value).__name__}")
        return
    for index, item in enumerate(value):
        spot = f"manifest_checks[{index}]"
        if not isinstance(item, Mapping):
            out.add(f"{spot} 必须是对象")
            continue
        _exact_keys(out, item, where=spot,
                    required=("field", "claimed", "observed", "equal"))
        field = item.get("field")
        if not (isinstance(field, str) and _FIELD.match(field)):
            out.add(f"{spot}.field 非法：{field!r}")
        if not isinstance(item.get("equal"), bool):
            out.add(f"{spot}.equal 必须是布尔：{item.get('equal')!r}")


def _check_anchor(out: _Problems, value: Any) -> None:
    if not isinstance(value, Mapping):
        out.add(f"anchor 必须是对象：{type(value).__name__}")
        return
    _exact_keys(out, value, where="anchor",
                required=("manifest_sha256", "criteria_sha256",
                          "scoring_sha256", "checked"))
    for key in ("manifest_sha256", "scoring_sha256"):
        item = value.get(key)
        if item is not None and not (isinstance(item, str) and _HEX64.match(item)):
            out.add(f"anchor.{key} 必须是 64hex 或 null：{item!r}")
    crits = value.get("criteria_sha256")
    if crits is not None:
        if not isinstance(crits, Mapping) or any(
                not (isinstance(v, str) and _HEX64.match(v))
                for v in crits.values()):
            out.add(f"anchor.criteria_sha256 必须是 {{task: 64hex}} 或 null：{crits!r}")
    if not isinstance(value.get("checked"), bool):
        out.add(f"anchor.checked 必须是布尔：{value.get('checked')!r}")


def _check_evaluator(out: _Problems, value: Any) -> None:
    if not isinstance(value, Mapping):
        out.add(f"evaluator 必须是对象：{type(value).__name__}")
        return
    _exact_keys(out, value, where="evaluator",
                required=("python", "isolated", "argv", "pid"))
    if not (isinstance(value.get("python"), str) and value["python"]):
        out.add(f"evaluator.python 必须是非空字符串：{value.get('python')!r}")
    if not isinstance(value.get("isolated"), bool):
        out.add(f"evaluator.isolated 必须是布尔：{value.get('isolated')!r}")
    argv = value.get("argv")
    if not isinstance(argv, list) or not argv or any(
            not isinstance(a, str) for a in argv):
        out.add(f"evaluator.argv 必须是非空字符串列表：{argv!r}")
    elif isinstance(value.get("isolated"), bool) \
            and (("-I" in argv) != bool(value["isolated"])):
        out.add("evaluator.isolated 与 argv 是否含 -I 不一致")
    if not _is_int(value.get("pid")) or value["pid"] < 1:
        out.add(f"evaluator.pid 必须是正整数：{value.get('pid')!r}")


def result_problems(obj: Any) -> "list[str]":
    """返回全部拒收原因（空列表 = 合法）。不抛异常。"""
    out = _Problems()
    if not isinstance(obj, Mapping):
        return [f"结果顶层必须是对象：{type(obj).__name__}"]
    _exact_keys(out, obj, where="结果", required=_RESULT_KEYS)
    if obj.get("schema") != RESULT_SCHEMA:
        out.add(f"schema 必须逐字等于 {RESULT_SCHEMA!r}：{obj.get('schema')!r}")
    trial = obj.get("trial_id")
    if not (isinstance(trial, str) and _TRIAL_ID.match(trial)):
        out.add(f"trial_id 形态非法：{trial!r}")
    task = obj.get("task_id")
    if task is not None and task not in constants.TASK_IDS:
        out.add(f"task_id 必须是 null 或 {list(constants.TASK_IDS)} 之一：{task!r}")
    verdict = obj.get("verdict")
    if verdict not in VERDICTS:
        out.add(f"verdict 必须是 {list(VERDICTS)} 之一：{verdict!r}")
        verdict = ""
    if verdict == "PASS":
        passed_all = True
        for item in (obj.get("criteria") or []):
            if isinstance(item, Mapping) and item.get("passed") is not True:
                passed_all = False
        for item in (obj.get("gates") or []):
            if isinstance(item, Mapping) and item.get("passed") is not True:
                passed_all = False
        if not passed_all:
            out.add("verdict=PASS 但存在未通过的判据或闸门（禁止假通过）")
        if task is None:
            out.add("verdict=PASS 时 task_id 不得为 null")
    _check_criteria(out, obj.get("criteria"), verdict=verdict)
    _check_gates(out, obj.get("gates"), verdict=verdict)
    if not isinstance(obj.get("facts"), Mapping):
        out.add("facts 必须是对象")
    _check_ledger(out, obj.get("ledger"))
    _check_manifest_checks(out, obj.get("manifest_checks"))
    _check_anchor(out, obj.get("anchor"))
    _strict_z(out, obj.get("evaluated_at"), where="evaluated_at")
    _check_evaluator(out, obj.get("evaluator"))
    if verdict == "INVALID_ARTIFACT" and not any(
            isinstance(item, Mapping) and item.get("equal") is False
            for item in (obj.get("manifest_checks") or [])):
        # 不强制：缺件型的 INVALID_ARTIFACT 没有对账行；只要求「有 false 行时
        # verdict 不得是 PASS/FAIL」——反向约束在 evaluator 里执行。
        pass
    return out.items


def validate_result(obj: Any) -> dict:
    """合法则返回**深拷贝的规范化对象**，否则抛 :class:`ManifestError`。"""
    problems = result_problems(obj)
    if problems:
        raise ManifestError("结果不合契约：\n- " + "\n- ".join(problems))
    return json.loads(json.dumps(obj, ensure_ascii=False))


# --------------------------------------------------------------------------
# 构造（评测器与运行器的单一构造点，避免两处各写一份清单）
# --------------------------------------------------------------------------

def file_facts(path: Path, *, relpath: str) -> dict:
    """一件文件的事实三元组（``relpath`` / ``sha256`` / ``bytes``）。"""
    data = Path(path).read_bytes()
    return {"relpath": relpath, "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data)}


def _now_z() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_manifest(trial_dir: "str | Path", *,
                   trial: str,
                   arm: str,
                   task_id: str,
                   runtime_mode: str,
                   fingerprint_sha256: str,
                   input_relpaths: Iterable[str],
                   deliverable_relpaths: Iterable[str],
                   claimed: Mapping[str, Any],
                   engine_entry: str,
                   config_digest: str,
                   created_at: "str | None" = None) -> dict:
    """按磁盘上的真实字节构造一份清单，并**就地校验**后返回。

    ``input_relpaths`` / ``deliverable_relpaths`` 是相对 ``trial_dir`` 的 POSIX
    路径；本函数逐件读字节算 sha256 与长度——自报值从不进清单。
    """
    base = Path(trial_dir)
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "experiment_id": constants.EXPERIMENT_ID,
        "trial_id": trial,
        "arm": arm,
        "task_id": task_id,
        "runtime": {"mode": runtime_mode,
                    "fingerprint_sha256": fingerprint_sha256},
        "inputs": [file_facts(base / rel, relpath=rel)
                   for rel in input_relpaths],
        "deliverables": [file_facts(base / rel, relpath=rel)
                         for rel in deliverable_relpaths],
        "claimed": dict(claimed),
        "engine": {"entry": engine_entry, "config_digest": config_digest},
        "created_at": created_at or _now_z(),
    }
    return validate_manifest(manifest)
