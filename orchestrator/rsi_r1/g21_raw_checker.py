"""RSI-G2.1 独立 trusted-raw 复算器（Plan §3.2；不 import 生产投影）。

独立子进程只读：``trusted raw / manifest / TaskSpec / ScopeContract / expected``。
本文件**不得** import ``orchestrator`` 任何模块（尤其 ``g2_*`` 生产投影、
``metrics``/``statistics``/``contrasts`` 的生产实现），只允许标准库与 numpy：

* 自己实现 metric（A：4 s 净位移；B：0.5–4 s 归一化跟踪 RMSE + LOSS_CAP）；
* 自己实现 block 线性对比与等权聚合；
* 自己实现 percentile bootstrap（PCG64；seed/重抽样数/分位法来自
  ``freeze/stat_contract.json``——参数是登记事实，实现是本文件）；
* 自己实现 proposition 判定表与容差（``1e-8 + 1e-6*|ref|``）。

结果轴分开：``binding_valid / numeric_valid / tuple_match / scope_valid /
coverage_valid / science_status``；``MATCH`` 不在任何路径上把
``science_status`` 提升成 SUPPORTED（状态只由区间 + δ + proposition 决定）。

用法::

    python -I g21_raw_checker.py --study A --task task.json --scope scope.json \
        --raw <observations batch> --candidate <output.json> \
        --stat-contract stat_contract.json --out report.json [--mutation NAME]

退出码：0 = ``ok``；1 = 复算/比对不成立；2 = 输入/绑定错误。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

SCHEMA = "simpraxis.rsi_g21.raw_check.v1"
SCHEMA_VERSION = 1

#: 复算实现身份（写进报告；与生产实现的差异由双实现 golden 检验兜底）。
IMPLEMENTATION = "g21_independent_v1"

PROPOSITIONS = ("decrease_gt", "increase_gt", "equivalent")
STATUS_SUPPORTED = "SUPPORTED"
STATUS_REFUTED = "REFUTED"
STATUS_INCONCLUSIVE = "INCONCLUSIVE"

LOSS_CAP = 10.0
TRACKING_WINDOW = (0.5, 4.0)

#: 预登记变异矩阵：observed 必须等于 expected（Plan §3.2 双实现 golden）。
#: 每个变异必须出现的**问题集合**（空集 = 合法变化不得误判）。
MUTATIONS: "Mapping[str, tuple[str, ...]]" = {
    "run_value": ("NUMERIC_MISMATCH",),
    "condition_swap": ("NUMERIC_MISMATCH",),
    "wrong_unit": ("NUMERIC_MISMATCH",),
    "missing_block": ("COVERAGE_MISMATCH",),
    "duplicate_block": ("COVERAGE_MISMATCH",),
    "all_equal": ("SCIENCE_STATUS_MISMATCH",),
    "irrelevant_field": (),
    "unregistered_metric": ("METRIC_UNREGISTERED",),
    "delta_drift": ("DELTA_DRIFT",),
    "contrast_unknown": ("CONTRAST_UNREGISTERED",),
    "scope_drift": ("SCOPE_DRIFT",),
    "schema_drift": ("SCHEMA_DRIFT",),
    "meta_spec_hash": ("BINDING_MISMATCH",),
    "required_contrast_removed": ("REQUIRED_CONTRAST_MISSING",),
}


class CheckError(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"[{code}] {detail}" if detail else code)


# ---------------------------------------------------------------------------
# 基础
# ---------------------------------------------------------------------------

def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha_file(path: "str | Path") -> str:
    return sha_bytes(Path(path).read_bytes())


def content_sha(doc: Mapping[str, Any],
                exclude: "Sequence[str]" = ()) -> str:
    body = {k: v for k, v in doc.items() if k not in set(exclude)}
    return sha_bytes(canonical_bytes(body))


def read_json(path: "str | Path") -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def tolerance(reference: float, stats: Mapping[str, Any]) -> float:
    return float(stats["claim_abs_tol"]) + float(stats["claim_rel_tol"]) * abs(
        float(reference))


# ---------------------------------------------------------------------------
# 输入
# ---------------------------------------------------------------------------

def load_stat_contract(path: "str | Path") -> dict[str, Any]:
    doc = read_json(path)
    for key in ("seed", "bootstrap_resamples", "bootstrap_quantile",
                "ci_level", "claim_abs_tol", "claim_rel_tol"):
        if key not in doc:
            raise CheckError("STAT_SPEC_UNRESOLVED", f"stat_contract 缺 {key}")
    return dict(doc)


def load_task(path: "str | Path") -> dict[str, Any]:
    doc = read_json(path)
    if not isinstance(doc, dict) or doc.get("schema") != "simpraxis.rsi_r13.task_spec.v1":
        raise CheckError("SCHEMA_DRIFT", "TaskSpec schema")
    return doc


def load_scope(path: "str | Path") -> dict[str, Any]:
    doc = read_json(path)
    want = content_sha(doc, exclude=("contract_sha256",))
    if doc.get("contract_sha256") != want:
        raise CheckError("SCOPE_DRIFT", "scope_contract sha 不符")
    return dict(doc)


def required_contrast_ids(task: Mapping[str, Any]) -> "list[str]":
    primary = task.get("primary") or {}
    ids = [str(primary.get("id") or "")]
    nc = (task.get("negative_control_spec") or {}).get("contrast") or {}
    nc_id = str(nc.get("id") or nc.get("contrast_id") or "")
    if nc_id and nc_id not in ids:
        ids.append(nc_id)
    return [cid for cid in ids if cid]


def verify_manifest(raw_dir: Path) -> dict[str, Any]:
    path = raw_dir / "manifest.json"
    if not path.is_file():
        raise CheckError("RAW_MANIFEST_MISSING", str(path))
    doc = read_json(path)
    problems: "list[str]" = []
    entries = doc.get("entries") or []
    for entry in entries:
        target = raw_dir / str(entry.get("relpath"))
        if not target.is_file():
            problems.append(f"缺失 {entry.get('relpath')}")
            continue
        data = target.read_bytes()
        if sha_bytes(data) != str(entry.get("sha256")):
            problems.append(f"sha 不符 {entry.get('relpath')}")
        if len(data) != int(entry.get("bytes") or -1):
            problems.append(f"字节数不符 {entry.get('relpath')}")
    if problems:
        raise CheckError("RAW_TAMPERED", "; ".join(problems[:6]))
    return {"count": int(doc.get("count") or len(entries)),
            "batch_id": str(doc.get("batch_id") or ""),
            "study": str(doc.get("study") or ""),
            "manifest_sha256": sha_file(path)}


def load_runs(raw_dir: Path) -> dict[str, dict[str, Any]]:
    runs: "dict[str, dict[str, Any]]" = {}
    for meta_path in sorted(raw_dir.glob("run-*.meta.json")):
        meta = read_json(meta_path)
        run_id = str(meta.get("run_id") or "")
        if not run_id or meta_path.name != f"{run_id}.meta.json":
            raise CheckError("BINDING_MISMATCH", f"{meta_path.name} run_id")
        data_path = meta_path.with_name(f"{run_id}.jsonl")
        if not data_path.is_file():
            raise CheckError("RAW_MANIFEST_MISSING", data_path.name)
        record = json.loads(data_path.read_text(encoding="utf-8"))
        if str(record.get("run_id")) != run_id:
            raise CheckError("BINDING_MISMATCH", f"{run_id} jsonl.run_id")
        if str(record.get("spec_hash")) != str(meta.get("spec_hash")):
            raise CheckError("BINDING_MISMATCH", f"{run_id} spec_hash")
        if str(record.get("block_id")) != str(meta.get("block_id")):
            raise CheckError("BINDING_MISMATCH", f"{run_id} block_id")
        runs[run_id] = {"meta": meta, "record": record}
    if not runs:
        raise CheckError("RAW_MANIFEST_MISSING", "没有 run-*.meta.json")
    return runs


# ---------------------------------------------------------------------------
# metric（独立实现）
# ---------------------------------------------------------------------------

def _series(record: Mapping[str, Any], name: str) -> "np.ndarray":
    values = record.get(name)
    if not isinstance(values, list) or not values:
        raise CheckError("RAW_SHAPE", f"{name} 不是非空数组")
    return np.asarray(values, dtype=np.float64)


def metric_value(study: str, meta: Mapping[str, Any],
                 record: Mapping[str, Any]) -> float:
    if study == "A":
        x = _series(record, "qpos")
        if x.ndim != 2 or x.shape[1] < 1:
            raise CheckError("RAW_SHAPE", "qpos 形状")
        times = _series(record, "time_s")
        if len(times) != x.shape[0]:
            raise CheckError("RAW_SHAPE", "time_s 与 qpos 不等长")
        x0 = float(meta.get("initial_x_m"))
        return float(x[-1, 0] - x0)
    if study == "B":
        events = record.get("instability_events")
        if events is None:
            events = (meta.get("reply_extra") or {}).get("instability_events") or []
        if events:
            return float(LOSS_CAP)
        q = _series(record, "qpos")
        target = _series(record, "target_q")
        times = _series(record, "time_s")
        if q.ndim == 2:
            q = q[:, 0]
        if target.ndim == 2:
            target = target[:, 0]
        if q.shape != target.shape or q.ndim != 1 or len(times) != q.shape[0]:
            raise CheckError("RAW_SHAPE", "B 轨迹形状")
        amplitude = float(((meta.get("spec_envelope") or {})
                           .get("params_applied") or {})
                          .get("target_amplitude_rad"))
        if not (amplitude > 0):
            raise CheckError("PARAM_INVALID", "target_amplitude_rad")
        lo, hi = TRACKING_WINDOW
        mask = (times >= lo) & (times <= hi)
        if int(mask.sum()) < 2:
            raise CheckError("WINDOW_EMPTY", "跟踪窗口")
        diff = q[mask] - target[mask]
        return float(math.sqrt(float((diff * diff).mean())) / amplitude)
    raise CheckError("STUDY_UNKNOWN", study)


def verify_run_binding(runs: Mapping[str, Mapping[str, Any]]) -> None:
    """变异后重新核对 meta ↔ record 绑定（spec_hash/run_id/block 一致）。"""
    for run_id, item in runs.items():
        meta, record = item["meta"], item["record"]
        if str(meta.get("run_id")) != str(run_id) \
                or str(record.get("run_id")) != str(run_id):
            raise CheckError("BINDING_MISMATCH", f"{run_id} run_id")
        if str(meta.get("spec_hash")) != str(record.get("spec_hash")):
            raise CheckError("BINDING_MISMATCH", f"{run_id} spec_hash")
        if str(meta.get("block_id")) != str(record.get("block_id")):
            raise CheckError("BINDING_MISMATCH", f"{run_id} block_id")


def per_run_metric(study: str, runs: Mapping[str, Mapping[str, Any]]) -> dict[str, float]:
    return {run_id: metric_value(study, item["meta"], item["record"])
            for run_id, item in runs.items()}


# ---------------------------------------------------------------------------
# 对比/聚合/区间（独立实现）
# ---------------------------------------------------------------------------

def block_values(runs: Mapping[str, Mapping[str, Any]],
                 values: Mapping[str, float]) -> dict[str, dict[str, float]]:
    blocks: "dict[str, dict[str, float]]" = {}
    for run_id, item in runs.items():
        block = str(item["meta"].get("block_id"))
        condition = str(item["meta"].get("condition_id"))
        if condition in blocks.setdefault(block, {}):
            raise CheckError("COVERAGE_MISMATCH",
                             f"block {block} 条件 {condition} 重复")
        blocks[block][condition] = float(values[run_id])
    return blocks


def contrast_effects(terms: Sequence[Mapping[str, Any]],
                     blocks: Mapping[str, Mapping[str, float]]
                     ) -> "dict[str, float]":
    weights = {str(t["condition_id"]): float(t["weight"]) for t in terms}
    effects: "dict[str, float]" = {}
    for block, per_cond in blocks.items():
        missing = [cid for cid in weights if cid not in per_cond]
        if missing:
            raise CheckError("COVERAGE_MISMATCH", f"{block} 缺条件 {missing}")
        effects[block] = float(sum(weights[cid] * per_cond[cid]
                                   for cid in weights))
    return effects


def bootstrap_ci(effects: Mapping[str, float], stats: Mapping[str, Any]
                 ) -> "tuple[float, float]":
    ids = sorted(effects)
    arr = np.asarray([effects[i] for i in ids], dtype=np.float64)
    n = arr.shape[0]
    rng = np.random.default_rng(np.random.PCG64(int(stats["seed"])))
    idx = rng.integers(0, n, size=(int(stats["bootstrap_resamples"]), n))
    means = arr[idx].mean(axis=1)
    level = float(stats["ci_level"])
    alpha = (1.0 - level) / 2.0
    lower, upper = np.quantile(means, [alpha, 1.0 - alpha],
                               method=str(stats["bootstrap_quantile"]))
    return float(lower), float(upper)


def classify(proposition: str, delta: float, lower: float, upper: float) -> str:
    if proposition not in PROPOSITIONS:
        raise CheckError("PROPOSITION_INVALID", proposition)
    if lower > upper:
        raise CheckError("CI_INVALID", f"{lower} > {upper}")
    d = float(delta)
    if proposition == "decrease_gt":
        if upper < -d:
            return STATUS_SUPPORTED
        if lower >= -d:
            return STATUS_REFUTED
        return STATUS_INCONCLUSIVE
    if proposition == "increase_gt":
        if lower > d:
            return STATUS_SUPPORTED
        if upper <= d:
            return STATUS_REFUTED
        return STATUS_INCONCLUSIVE
    if lower > -d and upper < d:
        return STATUS_SUPPORTED
    if upper <= -d or lower >= d:
        return STATUS_REFUTED
    return STATUS_INCONCLUSIVE


def recompute_contrast(*, terms: Sequence[Mapping[str, Any]],
                       blocks: Mapping[str, Mapping[str, float]],
                       delta: float, proposition: str,
                       level: float, stats: Mapping[str, Any]
                       ) -> dict[str, Any]:
    effects = contrast_effects(terms, blocks)
    n = len(effects)
    estimate = float(np.mean(np.asarray([effects[i] for i in sorted(effects)],
                                        dtype=np.float64)))
    lower, upper = bootstrap_ci(effects, stats)
    status = classify(proposition, delta, lower, upper)
    return {"estimate": estimate, "lower": lower, "upper": upper,
            "n_blocks": n, "status": status, "level": level,
            "block_effects": {k: effects[k] for k in sorted(effects)}}


# ---------------------------------------------------------------------------
# 候选比对
# ---------------------------------------------------------------------------

def _candidate_contrasts(doc: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    raw = doc.get("contrasts")
    if isinstance(raw, list):
        return {str(item.get("contrast_id")): item for item in raw
                if isinstance(item, dict)}
    if isinstance(raw, dict):
        return {str(key): value for key, value in raw.items()
                if isinstance(value, dict)}
    raise CheckError("SCHEMA_DRIFT", "candidate.contrasts 形状")


def compare(*, study: str, task: Mapping[str, Any], scope: Mapping[str, Any],
            stats: Mapping[str, Any], blocks: Mapping[str, Mapping[str, float]],
            candidate: Mapping[str, Any]) -> dict[str, Any]:
    problems: "list[str]" = []
    axes = {"binding_valid": True, "numeric_valid": True, "tuple_match": True,
            "scope_valid": True, "coverage_valid": True}
    # --- scope 一致性（task ↔ scope）---
    primary = task.get("primary") or {}
    if str(scope.get("estimand_id")) != str(primary.get("id")):
        problems.append("SCOPE_DRIFT")
        axes["scope_valid"] = False
    if abs(float(scope.get("delta")) - float(primary.get("delta"))) > 1e-12:
        problems.append("SCOPE_DRIFT")
        axes["scope_valid"] = False
    if str(scope.get("proposition")) != str(primary.get("proposition")):
        problems.append("SCOPE_DRIFT")
        axes["scope_valid"] = False
    if str(scope.get("task_id")) != str(task.get("task_id")):
        problems.append("SCOPE_DRIFT")
        axes["scope_valid"] = False
    if abs(float(scope.get("interval_level")) - float(stats["ci_level"])) > 1e-12:
        problems.append("SCOPE_DRIFT")
        axes["scope_valid"] = False
    # --- 必需对比 / 候选 schema ---
    if str(candidate.get("schema") or "") != "simpraxis.rsi_r1.analysis_output.v1":
        problems.append("SCHEMA_DRIFT")
    required = required_contrast_ids(task)
    cand = _candidate_contrasts(candidate)
    missing = [cid for cid in required if cid not in cand]
    if missing:
        problems.append("REQUIRED_CONTRAST_MISSING")
    extra = [cid for cid in cand if cid not in required]
    if extra:
        problems.append("CONTRAST_UNREGISTERED")
    recomputed: "dict[str, Any]" = {}
    science: "dict[str, str]" = {}
    for cid in required:
        spec = primary if cid == primary.get("id") else (
            (task.get("negative_control_spec") or {}).get("contrast") or {})
        if not spec:
            continue
        metric = str((cand.get(cid) or {}).get("metric_id")
                     or (primary.get("metric") if cid == primary.get("id")
                         else (task.get("negative_control_spec") or {}).get(
                             "contrast", {}).get("metric")))
        registered = {str(primary.get("metric"))}
        nc_metric = ((task.get("negative_control_spec") or {})
                     .get("contrast") or {}).get("metric")
        if nc_metric:
            registered.add(str(nc_metric))
        if metric not in registered:
            problems.append("METRIC_UNREGISTERED")
            axes["tuple_match"] = False
            continue
        unit = ((stats.get("metrics") or {}).get(metric) or {}).get("unit")
        if unit and str(scope.get("unit")) != str(unit):
            problems.append("SCOPE_DRIFT")
            axes["scope_valid"] = False
        ref = recompute_contrast(
            terms=spec.get("terms") or [],
            blocks=blocks, delta=float(spec.get("delta")),
            proposition=str(spec.get("proposition")),
            level=float(scope.get("interval_level")), stats=stats)
        recomputed[cid] = ref
        science[cid] = ref["status"]
        got = cand.get(cid)
        if got is None:
            continue
        # tuple 面
        if str(got.get("metric_id")) != metric:
            problems.append("TUPLE_MISMATCH")
            axes["tuple_match"] = False
        if str(got.get("proposition")) != str(spec.get("proposition")):
            problems.append("TUPLE_MISMATCH")
            axes["tuple_match"] = False
        if int(got.get("n_blocks") or -1) != int(ref["n_blocks"]):
            problems.append("COVERAGE_MISMATCH")
            axes["coverage_valid"] = False
        if "delta" in got and abs(float(got["delta"])
                                   - float(spec.get("delta"))) > 1e-12:
            problems.append("DELTA_DRIFT")
            axes["tuple_match"] = False
        # 数值面
        for key in ("estimate", "lower", "upper"):
            reference = float(ref[key])
            if abs(float(got.get(key, float("nan"))) - reference) > tolerance(
                    reference, stats):
                problems.append("NUMERIC_MISMATCH")
                axes["numeric_valid"] = False
        # 科学状态：只由区间/δ/proposition 决定，MATCH 不晋升
        if str(got.get("status")) != str(ref["status"]):
            problems.append("SCIENCE_STATUS_MISMATCH")
    return {"axes": axes, "problems": sorted(set(problems)),
            "recomputed": recomputed, "science_status": science,
            "ok": not problems and all(axes.values())}


# ---------------------------------------------------------------------------
# 变异（预登记矩阵；只改输入，不改实现/预期）
# ---------------------------------------------------------------------------

def apply_mutation(name: str, runs: dict[str, dict[str, Any]],
                   task: Mapping[str, Any], scope: dict[str, Any],
                   candidate: dict[str, Any]) -> tuple[
                       dict[str, dict[str, Any]], dict[str, Any],
                       dict[str, Any], dict[str, Any]]:
    if name == "run_value":
        run_id = sorted(runs)[0]
        run = runs[run_id]
        run["record"]["qpos"] = [[float(v) + 0.05 if index == 0 else float(v)
                                  for index, v in enumerate(row)]
                                 for row in run["record"]["qpos"]]
    elif name == "condition_swap":
        # 同一 block 内两个条件的**轨迹**互换（条件标签不动）⇒ 数值必须变，
        # 且不制造重复条件（那会退化成覆盖错误）。
        # 确定性选点：第一个 block 里按 condition_id 排序的前两个条件。
        first_block = sorted({str(item["meta"]["block_id"])
                              for item in runs.values()})[0]
        by_condition = {}
        for rid, item in runs.items():
            if str(item["meta"]["block_id"]) != first_block:
                continue
            by_condition[str(item["meta"]["condition_id"])] = rid
        conditions = sorted(by_condition)
        if len(conditions) < 2:
            raise CheckError("COVERAGE_MISMATCH", "变异需要同 block 两个条件")
        a, b = by_condition[conditions[0]], by_condition[conditions[1]]
        for key in ("qpos", "target_q"):
            if key in runs[a]["record"] and key in runs[b]["record"]:
                runs[a]["record"][key], runs[b]["record"][key] = (
                    runs[b]["record"][key], runs[a]["record"][key])
    elif name == "wrong_unit":
        for run in runs.values():
            run["record"]["qpos"] = [[float(v) * 1000.0 for v in row]
                                     for row in run["record"]["qpos"]]
    elif name == "missing_block":
        block = sorted({str(r["meta"]["block_id"]) for r in runs.values()})[0]
        for run_id in [rid for rid, r in runs.items()
                       if str(r["meta"]["block_id"]) == block]:
            runs.pop(run_id)
    elif name == "duplicate_block":
        run_id = sorted(runs)[0]
        clone_id = "run-" + "f" * 20
        clone = {"meta": dict(runs[run_id]["meta"]),
                 "record": dict(runs[run_id]["record"])}
        clone["meta"]["run_id"] = clone_id
        clone["record"]["run_id"] = clone_id
        runs[clone_id] = clone
    elif name == "all_equal":
        for run in runs.values():
            run["record"]["qpos"] = [[0.0 for _ in row]
                                     for row in run["record"]["qpos"]]
            run["meta"]["initial_x_m"] = 0.0
    elif name == "irrelevant_field":
        for run in runs.values():
            run["record"]["solver_warnings"] = 7
    elif name == "unregistered_metric":
        for item in _raw_contrast_list(candidate):
            item["metric_id"] = "not_a_registered_metric"
    elif name == "delta_drift":
        for item in _raw_contrast_list(candidate):
            item["delta"] = float(item.get("delta", 0.0)) + 0.5
    elif name == "contrast_unknown":
        _raw_contrast_list(candidate).append(
            {"contrast_id": "not_in_task", "metric_id": "x", "proposition":
             "equivalent", "delta": 0.001, "estimate": 0.0, "lower": 0.0,
             "upper": 0.0, "n_blocks": 1, "status": "SUPPORTED"})
    elif name == "scope_drift":
        scope["estimand_id"] = "not_the_registered_estimand"
    elif name == "schema_drift":
        candidate["schema"] = "simpraxis.rsi_r1.not_the_schema.v1"
    elif name == "meta_spec_hash":
        run_id = sorted(runs)[0]
        runs[run_id]["record"]["spec_hash"] = "0" * 64

    elif name == "required_contrast_removed":
        candidate["contrasts"] = []
    else:
        raise CheckError("MUTATION_UNKNOWN", name)
    return runs, task, scope, candidate


def _raw_contrast_list(candidate: dict[str, Any]) -> list:
    raw = candidate.get("contrasts")
    if isinstance(raw, list):
        return raw
    raise CheckError("SCHEMA_DRIFT", "变异要求 contrasts 为数组")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def run_check(*, study: str, task: Mapping[str, Any], scope: Mapping[str, Any],
              stats: Mapping[str, Any], raw_dir: Path,
              candidate: Mapping[str, Any],
              mutation: str = "") -> dict[str, Any]:
    if str(task.get("mechanism_id", "")).split("_")[0] != study:
        raise CheckError("STUDY_MISMATCH", "task.mechanism_id 与 study 不符")
    manifest = verify_manifest(raw_dir)
    runs = load_runs(raw_dir)
    task_dict = json.loads(json.dumps(task, default=str))
    scope_dict = json.loads(json.dumps(scope, default=str))
    cand = json.loads(json.dumps(candidate, default=str))
    if mutation:
        runs, task_dict, scope_dict, cand = apply_mutation(
            mutation, runs, task_dict, scope_dict, cand)
    primary = task_dict.get("primary") or {}
    values: "dict[str, float]" = {}
    blocks: "dict[str, dict[str, float]]" = {}
    try:
        verify_run_binding(runs)
        values = per_run_metric(study, runs)
        blocks = block_values(runs, values)
        base = recompute_contrast(
            terms=primary.get("terms") or [], blocks=blocks,
            delta=float(primary.get("delta")),
            proposition=str(primary.get("proposition")),
            level=float(scope_dict.get("interval_level")), stats=stats)
        comparison = compare(study=study, task=task_dict, scope=scope_dict,
                             stats=stats, blocks=blocks, candidate=cand)
    except CheckError as exc:
        base = {"error": exc.code}
        comparison = {"axes": {name: False for name in (
            "binding_valid", "numeric_valid", "tuple_match", "scope_valid",
            "coverage_valid")},
            "problems": [exc.code], "recomputed": {},
            "science_status": {}, "ok": False}
    axis = {}
    axis["binding_valid"] = comparison["axes"]["binding_valid"]
    axis["numeric_valid"] = comparison["axes"]["numeric_valid"]
    axis["tuple_match"] = comparison["axes"]["tuple_match"]
    axis["scope_valid"] = comparison["axes"]["scope_valid"]
    axis["coverage_valid"] = comparison["axes"]["coverage_valid"]
    axis["science_status"] = comparison["science_status"].get(
        str(primary.get("id")), "NOT_MEASURED")
    # MATCH ≠ SUPPORTED：状态来自独立分类，不从候选 label 抄
    problems = list(comparison["problems"])
    if mutation:
        required = list(MUTATIONS[mutation])
        observed = problems[0] if problems else "OK"
        ok = (set(required) <= set(problems)) if required else not problems
        expected: Any = required
    else:
        ok = comparison["ok"]
        observed = problems[0] if problems else "OK"
        expected = None
    return {"schema": SCHEMA, "schema_version": SCHEMA_VERSION,
            "study": study, "implementation": IMPLEMENTATION,
            "independent": True,
            "mutation": mutation or None,
            "expected": expected,
            "observed": observed,
            "ok": bool(ok),
            "axes": axis, "problems": problems,
            "recomputed_primary": base,
            "recomputed_contrasts": comparison["recomputed"],
            "raw": {"batch_id": manifest["batch_id"],
                    "manifest_sha256": manifest["manifest_sha256"],
                    "n_runs": len(values), "n_blocks": len(blocks)},
            "candidate_sha256": sha_bytes(canonical_bytes(cand)),
            "task_sha256": sha_bytes(canonical_bytes(task)),
            "scope_sha256": str(scope.get("contract_sha256") or ""),
            "stat_contract": {k: stats[k] for k in
                              ("seed", "bootstrap_resamples",
                               "bootstrap_quantile", "ci_level")}}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="g21_raw_checker")
    parser.add_argument("--study", required=True, choices=["A", "B"])
    parser.add_argument("--task", required=True)
    parser.add_argument("--scope", required=True)
    parser.add_argument("--raw", required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--stat-contract", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--mutation", default="")
    parser.add_argument("--print-imports", action="store_true")
    return parser


def main(argv: "Sequence[str] | None" = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.print_imports:
            names = sorted({name.split(".")[0] for name in sys.modules})
            print(json.dumps({"top_level_modules": names}))
            return 0
        task = load_task(args.task)
        scope = load_scope(args.scope)
        stats = load_stat_contract(args.stat_contract)
        candidate_bytes = Path(args.candidate).read_bytes()
        candidate = json.loads(candidate_bytes.decode("utf-8"))
        report = run_check(study=args.study, task=task, scope=scope, stats=stats,
                           raw_dir=Path(args.raw), candidate=candidate,
                           mutation=args.mutation)
        report["candidate_bytes_sha256"] = sha_bytes(candidate_bytes)
        Path(args.out).write_text(
            json.dumps(report, ensure_ascii=False, sort_keys=True, indent=1)
            + "\n", encoding="utf-8")
        return 0 if report["ok"] else 1
    except CheckError as exc:
        print(json.dumps({"ok": False, "code": exc.code, "detail": exc.detail},
                         ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
