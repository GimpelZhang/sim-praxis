"""RSI-R1-RV 契约层：来源清单、公开包、许可、六维 rubric 与全轮判定。

Plan §2/§4/§5/§6 的代码化：

* **两源不可伪装同一研究**：``RVSourceManifestV1`` 逐 study 绑定
  ``source_campaign_id/source_run_id/task/plan/design/analysis/output/receipt/
  confirm_manifest/scope`` 的绝对路径与 bytes sha；``verify_source_manifest``
  从磁盘重算并核对 study/必需对比/CI 层级/单位，任一不符在**发网前**阻断。
* **公开包只含已发布观测**：``build_public_package`` 逐件写 allowlist 内的
  字段/文件（producer 明确标 ``candidate_analysis`` 或 ``instrument_observation``），
  私有 expected/旧报告/审查理由永不进入候选 Request。
* **六维 rubric 与合并规则事前唯一固定**（Plan §4.1），不沿用旧版 FAIL 合并。
* **全轮 aggregate 真值表**（Plan §4.3）按唯一优先级实现，保留每 study 原始终态。

本模块不做 I/O 以外的事情，也不发网；它被 ``rv_runtime``/``rv_review``/``rv_cli``
共用。
"""
from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from orchestrator.common.fsutil import atomic_write_text, ensure_private_dir

SCHEMA_PREFIX = "simpraxis.rsi_rv"
SCHEMA_VERSION = 1

STUDIES: "tuple[str, ...]" = ("A", "B")
SOURCE_MANIFEST_SCHEMA = f"{SCHEMA_PREFIX}.source_manifest.v1"
SOURCE_MAP_SCHEMA = f"{SCHEMA_PREFIX}.source_map.v1"
SOURCE_CLOSURE_SCHEMA = f"{SCHEMA_PREFIX}.source_closure.v1"
PUBLIC_ALLOWLIST_SCHEMA = f"{SCHEMA_PREFIX}.public_allowlist.v1"
PREREG_SCHEMA = f"{SCHEMA_PREFIX}.prereg.v1"
PERMIT_SCHEMA = f"{SCHEMA_PREFIX}.permit.v1"
BUDGET_SCHEMA = f"{SCHEMA_PREFIX}.budget.v1"
CLOSURE_SCHEMA = f"{SCHEMA_PREFIX}.implementation_closure.v1"
LINEAGE_SCHEMA = f"{SCHEMA_PREFIX}.lineage.v1"
VERDICT_SCHEMA = f"{SCHEMA_PREFIX}.aggregate_verdict.v1"
REVIEW_SCHEMA = f"{SCHEMA_PREFIX}.review.v1"
REVIEW_PACKET_SCHEMA = f"{SCHEMA_PREFIX}.review_packet.v1"
CALIBRATION_SCHEMA = f"{SCHEMA_PREFIX}.calibration_review.v1"
CALIBRATION_SEAL_SCHEMA = f"{SCHEMA_PREFIX}.calibration_expected.v1"
WORKER_SCHEMA = f"{SCHEMA_PREFIX}.worker_report.v1"
CAPACITY_SCHEMA = f"{SCHEMA_PREFIX}.capacity.v1"
CLOSEOUT_SCHEMA = f"{SCHEMA_PREFIX}.closeout.v1"

MODE_SOURCE = "source_only"

#: 模型调用统一走当前批准的 coding_debug stage（Plan §5）。
RV_STAGE = "coding_debug"
RV_OUTPUT_TOKENS = 32768
RV_TEMPERATURE = 0.2
RV_REQUEST_TIMEOUT_S = 120

#: 四条 lane（Plan §5 表格，唯一真值）；无 repair reserve、不可跨 lane 借额。
RV_LANE_ORDER: "tuple[str, ...]" = (
    "review_calibration", "suffix_A", "suffix_B", "review_final")
RV_LANE_POST_CAPS: "Mapping[str, int]" = {
    "review_calibration": 4, "suffix_A": 8, "suffix_B": 8, "review_final": 4}
#: lane → 账本 task id（RV 独立命名空间内自洽）。
RV_LANE_TASK_ID: "Mapping[str, int]" = {
    "review_calibration": 0, "suffix_A": 1, "suffix_B": 2, "review_final": 3}
RV_TASK_CAPS: "Mapping[int, int]" = {
    RV_LANE_TASK_ID[lane]: RV_LANE_POST_CAPS[lane] for lane in RV_LANE_ORDER}
#: 整轮硬帽 = 独立字面量（不取 sum，沿用项目先例）。
RV_HARD_CAP = 24

#: RV 独立账期（T0 全仓扫描：2900–2905 已占用，2906 无冲突后锁定）。
LEDGER_RSI_RV = 2906

#: 每请求容量（Plan §4.4/§5）：review 单请求输入上限沿用 R1.3 批准值。
REVIEW_INPUT_TOKENS_RV = 20000
REVIEW_PACKET_BYTES_RV = 122_880
PAGE_BYTES_RV = 8 * 1024
ARTIFACT_MAX_RV = 8 * 1024 ** 2
MAX_MANDATORY_READ_TURNS = 5
MIN_PATH_TURNS = 6
MAX_PATH_TURNS = 8
MAX_USD_RV = "UNKNOWN"

#: 六维 rubric（Plan §4.1，唯一固定顺序）。
RUBRIC_DIMS: "tuple[str, ...]" = (
    "question_and_preregistration",
    "design_consistency",
    "confirm_control_and_nc",
    "statistics_censoring_uncertainty",
    "claim_scope",
    "result_driven_limitations_and_next_steps",
)

#: review 通道数（每 study 两审；无第三票）。
REVIEW_CHANNELS = 2


class RVError(RuntimeError):
    """RV 阻断（fail-closed）；``code`` 公开可机判。"""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"[{code}] {detail}" if detail else code)


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha_file(path: "str | Path") -> str:
    return sha_bytes(Path(path).read_bytes())


def canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def canonical_sha(obj: Any) -> str:
    return sha_bytes(canonical(obj))


def read_json(path: "str | Path") -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_json_private(path: "str | Path", obj: Any) -> str:
    target = Path(path)
    ensure_private_dir(target.parent)
    text = (json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=1,
                       allow_nan=False) + "\n")
    atomic_write_text(target, text, mode=0o600)
    return sha_bytes(text.encode("utf-8"))


def write_text_private(path: "str | Path", text: str) -> str:
    target = Path(path)
    ensure_private_dir(target.parent)
    atomic_write_text(target, text, mode=0o600)
    return sha_bytes(text.encode("utf-8"))


def _require_hex(value: Any, *, where: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise RVError("SHA_INVALID", where)
    return value


def _rel_within(root: Path, rel: str) -> Path:
    if not isinstance(rel, str) or not rel or rel.startswith("/"):
        raise RVError("PATH_INVALID", repr(rel))
    if ".." in Path(rel).parts:
        raise RVError("PATH_INVALID", f"越界路径 {rel!r}")
    target = (root / rel).resolve(strict=False)
    try:
        target.relative_to(root)
    except ValueError:
        raise RVError("PATH_INVALID", f"解析后越过根：{rel!r}") from None
    return target


# ---------------------------------------------------------------------------
# 来源清单（RVSourceManifestV1）
# ---------------------------------------------------------------------------

_STUDY_ROLES = ("task", "plan", "source_design", "receipt", "output_raw",
                "output_pointer", "confirm_manifest", "observations_batch",
                "scope_contract", "journal")


def _study_entry_from_g21(study: str, g21_root: Path,
                          source_campaign: str) -> dict[str, Any]:
    """从 G2.1 终态根读取一个 study 的真实来源并落 manifest 条目。"""
    replay = read_json(g21_root / "freeze" / "replay_manifest.json")
    entry = dict((replay.get("studies") or {}).get(study) or {})
    missing = [key for key in _STUDY_ROLES if not entry.get(key)]
    if missing:
        raise RVError("SOURCE_MISSING", f"{study} 缺 {missing}")
    paths: "dict[str, Any]" = {}
    for role in _STUDY_ROLES:
        path = Path(str(entry[role]))
        if not path.exists():
            raise RVError("SOURCE_MISSING", f"{study}.{role}={path}")
        if path.is_dir():
            files = sorted(p for p in path.rglob("*") if p.is_file())
            digest = hashlib.sha256()
            for item in files:
                digest.update(item.relative_to(path).as_posix().encode())
                digest.update(b"\0")
                digest.update(item.read_bytes())
            paths[role] = {"path": str(path), "kind": "dir",
                           "sha256": digest.hexdigest(),
                           "bytes": sum(p.stat().st_size for p in files),
                           "n_files": len(files)}
        else:
            paths[role] = {"path": str(path), "kind": "file",
                           "sha256": sha_file(path),
                           "bytes": path.stat().st_size}
    task_doc = read_json(entry["task"])
    plan_doc = read_json(entry["plan"])
    # G2.1 的 replay_manifest 指向 **G2 封存 scope**（interval_level=0.9、B unit=ratio）；
    # G2.1 已按登记统计口径重建 scope（0.95 / 单位取自 rubric），差异逐项留档在
    # ``contract/scope_provenance.json``。RV 用重建件，并把封存件作独立输入登记。
    sealed_scope = Path(str(entry["scope_contract"]))
    g21_scope = g21_root / "contract" / f"scope_{study}.json"
    if not g21_scope.is_file():
        raise RVError("SOURCE_MISSING", f"{study} 重建 scope 缺失：{g21_scope}")
    paths["scope_contract"] = {"path": str(g21_scope), "kind": "file",
                               "sha256": sha_file(g21_scope),
                               "bytes": g21_scope.stat().st_size}
    paths["scope_sealed_g2"] = {"path": str(sealed_scope), "kind": "file",
                                "sha256": sha_file(sealed_scope),
                                "bytes": sealed_scope.stat().st_size}
    scope_doc = read_json(g21_scope)
    output_bytes = Path(entry["output_raw"]).read_bytes()
    receipt = read_json(entry["receipt"])
    # 分析程序：plan.analysis_source_sha 指向 designs/<sha>.py（replay 与 B 同构）。
    analysis_sha = str(plan_doc.get("analysis_source_sha") or "")
    _require_hex(analysis_sha, where=f"{study}.plan.analysis_source_sha")
    source_root = Path(str(entry["journal"])).parent.parent
    program = source_root / "designs" / f"{analysis_sha}.py"
    if not program.is_file():
        program = Path(str(entry["source_design"]))
    if sha_file(program) != analysis_sha:
        raise RVError("SOURCE_DRIFT",
                      f"{study} analysis program 不是 plan 绑定的 sha")
    paths["analysis_program"] = {"path": str(program), "kind": "file",
                                 "sha256": sha_file(program),
                                 "bytes": program.stat().st_size}
    # 必需对比：独立 raw checker 的任务派生（不 import orchestrator）。
    from orchestrator.rsi_r1 import g21_raw_checker as RC
    required = RC.required_contrast_ids(task_doc)
    output_doc = json.loads(output_bytes.decode("utf-8"))
    present = sorted({str(item.get("contrast_id"))
                      for item in (output_doc.get("contrasts") or [])
                      if isinstance(item, Mapping)})
    missing_c = [cid for cid in required if cid not in present]
    if missing_c:
        raise RVError("SOURCE_INCOMPLETE", f"{study} 缺必需对比 {missing_c}")
    # 凭据绑定：study/phase/plan/output/source/confirm manifest 六项必须自洽。
    from orchestrator.rsi_r1 import experiments as E
    manifest = E.manifest_from_doc(read_json(entry["confirm_manifest"]))
    checks = {
        "study_id": str(receipt.get("study_id")) == study,
        "phase": str(receipt.get("phase")) == "confirm",
        "plan_sha256": str(receipt.get("plan_sha256")) == _plan_sha(plan_doc),
        "output_sha256": str(receipt.get("output_sha256")) == sha_bytes(output_bytes),
        "source_sha256": str(receipt.get("source_sha256")) == analysis_sha,
        "confirm_manifest_sha256":
            str(receipt.get("confirm_manifest_sha256")) == manifest.sha256,
        "validity": receipt.get("validity") is True,
        "exit_code": int(receipt.get("exit_code", -1)) == 0,
        "timed_out": bool(receipt.get("timed_out", False)) is False,
    }
    failed = sorted(k for k, ok in checks.items() if not ok)
    if failed:
        raise RVError("RECEIPT_UNBOUND", f"{study}: {failed}")
    # scope 口径：CI 层级/单位/δ/proposition 必须与登记统计口径一致。
    from orchestrator.rsi_r1 import rubric as RUL
    metric = str(scope_doc.get("metric_id"))
    unit = str(RUL.METRICS[metric]["unit"])
    if abs(float(scope_doc.get("interval_level")) - float(RUL.CI_PRIMARY)) > 1e-12:
        raise RVError("SCOPE_CI_MISMATCH",
                      f"{study}: interval_level={scope_doc.get('interval_level')}")
    if str(scope_doc.get("unit")) != unit:
        raise RVError("SCOPE_UNIT_MISMATCH",
                      f"{study}: unit={scope_doc.get('unit')!r} != {unit!r}")
    return {
        "study_id": study,
        "source_campaign_id": str(source_campaign),
        "source_run_id": Path(str(entry["observations_batch"])).name,
        "g21_root": str(g21_root),
        "source_root": str(Path(str(entry["journal"])).parent.parent),
        "task_sha": sha_bytes(canonical(task_doc)),
        "task_bytes_sha": sha_file(entry["task"]),
        "plan_sha": _plan_sha(plan_doc),
        "plan_bytes_sha": sha_file(entry["plan"]),
        "design_source_sha": str(plan_doc.get("design_source_sha") or ""),
        "analysis_source_sha": analysis_sha,
        "analysis_program_sha": sha_file(program),
        "output_sha": sha_bytes(output_bytes),
        "receipt_sha": sha_file(entry["receipt"]),
        "confirm_manifest_sha": manifest.sha256,
        "confirm_manifest_bytes_sha": sha_file(entry["confirm_manifest"]),
        "scope_contract_sha": str(scope_doc.get("contract_sha256") or ""),
        "scope_contract_bytes_sha": sha_file(
            g21_root / "contract" / f"scope_{study}.json"),
        "source_producer": "candidate",
        "required_contrast_ids": required,
        "present_contrast_ids": present,
        "n_blocks": int(manifest.block_count),
        "n_runs": len(list(Path(str(entry["observations_batch"]))
                           .glob("run-*.jsonl"))),
        "interval_level": float(scope_doc.get("interval_level")),
        "unit": str(scope_doc.get("unit")),
        "metric_id": metric,
        "estimand_id": str(scope_doc.get("estimand_id")),
        "delta": float(scope_doc.get("delta")),
        "proposition": str(scope_doc.get("proposition")),
        "paths": paths,
    }


def _plan_sha(plan_doc: Mapping[str, Any]) -> str:
    from orchestrator.rsi_r1 import contracts as C
    return C.ResearchPlan.from_dict(dict(plan_doc)).sha256


def build_source_manifest(source_map: Mapping[str, Any]) -> dict[str, Any]:
    """RV0：从 source-map 的真实文件生成 ``RVSourceManifestV1``。"""
    if not isinstance(source_map, Mapping):
        raise RVError("SOURCE_MAP_INVALID", "顶层必须是对象")
    raw = source_map.get("studies")
    if not isinstance(raw, Mapping) or set(raw) != set(STUDIES):
        raise RVError("SOURCE_MAP_INVALID", f"studies 必须恰为 {STUDIES}")
    entries = {}
    for study in STUDIES:
        spec = raw[study]
        if not isinstance(spec, Mapping) or not spec.get("g21_root"):
            raise RVError("SOURCE_MAP_INVALID", f"{study}.g21_root")
        g21_root = Path(str(spec["g21_root"])).resolve(strict=False)
        if not g21_root.is_dir():
            raise RVError("SOURCE_MISSING", str(g21_root))
        entries[study] = _study_entry_from_g21(
            study, g21_root,
            str(spec.get("source_campaign_id") or ""))
    doc = {
        "schema": SOURCE_MANIFEST_SCHEMA, "schema_version": 1,
        "mode": MODE_SOURCE,
        "created_at_utc": utc_now(),
        "source_map_sha": canonical_sha(dict(source_map)),
        "studies": entries,
    }
    doc["manifest_sha256"] = canonical_sha({k: v for k, v in doc.items()
                                            if k != "manifest_sha256"})
    return doc


def verify_source_manifest(doc: Mapping[str, Any]) -> dict[str, Any]:
    """从磁盘逐件重算并核对；返回 closure 摘要（不发网）。"""
    if not isinstance(doc, Mapping):
        raise RVError("SOURCE_MANIFEST_INVALID", "不是对象")
    if doc.get("schema") != SOURCE_MANIFEST_SCHEMA:
        raise RVError("SOURCE_MANIFEST_INVALID", f"schema={doc.get('schema')!r}")
    studies = doc.get("studies")
    if not isinstance(studies, Mapping) or set(studies) != set(STUDIES):
        raise RVError("SOURCE_MANIFEST_INVALID", f"studies 必须恰为 {STUDIES}")
    problems: "list[str]" = []
    rows: "list[dict[str, Any]]" = []
    for study in STUDIES:
        entry = studies[study]
        if str(entry.get("study_id")) != study:
            problems.append(f"{study}: study_id 不符")
        # 1) 逐件 bytes 重算
        for role, item in sorted((entry.get("paths") or {}).items()):
            path = Path(str(item.get("path")))
            if not path.exists():
                problems.append(f"{study}.{role}: 消失")
                continue
            if item.get("kind") == "dir":
                files = sorted(p for p in path.rglob("*") if p.is_file())
                digest = hashlib.sha256()
                for f in files:
                    digest.update(f.relative_to(path).as_posix().encode())
                    digest.update(b"\0")
                    digest.update(f.read_bytes())
                if digest.hexdigest() != item.get("sha256"):
                    problems.append(f"{study}.{role}: 目录漂移")
            elif sha_file(path) != item.get("sha256"):
                problems.append(f"{study}.{role}: sha 漂移")
            rows.append({"study": study, "role": role,
                         "path": str(path), "sha256": str(item.get("sha256"))})
        # 2) 语义绑定（task/plan/scope/output/receipt）
        try:
            task_doc = read_json(entry["paths"]["task"]["path"])
            plan_doc = read_json(entry["paths"]["plan"]["path"])
            scope_doc = read_json(entry["paths"]["scope_contract"]["path"])
            output_doc = json.loads(Path(entry["paths"]["output_raw"]["path"])
                                    .read_bytes().decode("utf-8"))
            receipt = read_json(entry["paths"]["receipt"]["path"])
        except (KeyError, OSError, ValueError) as exc:
            problems.append(f"{study}: 源件不可读 {type(exc).__name__}")
            continue
        from orchestrator.rsi_r1 import g21_raw_checker as RC
        required = RC.required_contrast_ids(task_doc)
        if list(entry.get("required_contrast_ids") or []) != required:
            problems.append(f"{study}: required_contrast_ids 与任务不符")
        present = sorted({str(item.get("contrast_id"))
                          for item in (output_doc.get("contrasts") or [])
                          if isinstance(item, Mapping)})
        if any(cid not in present for cid in required):
            problems.append(f"{study}: 输出缺必需对比")
        if sha_bytes(canonical(task_doc)) != entry.get("task_sha"):
            problems.append(f"{study}: task canonical sha 不符")
        if _plan_sha(plan_doc) != entry.get("plan_sha"):
            problems.append(f"{study}: plan sha 不符")
        from orchestrator.rsi_r1 import experiments as E
        manifest = E.manifest_from_doc(
            read_json(entry["paths"]["confirm_manifest"]["path"]))
        if manifest.sha256 != entry.get("confirm_manifest_sha"):
            problems.append(f"{study}: confirm manifest sha 不符")
        if str(manifest.study_id) != study:
            problems.append(f"{study}: confirm manifest study 不符")
        if str(scope_doc.get("task_id")) != str(task_doc.get("task_id")):
            problems.append(f"{study}: scope.task_id 不符")
        if str(scope_doc.get("contract_sha256")) != entry.get("scope_contract_sha"):
            problems.append(f"{study}: scope sha 不符")
        if str(receipt.get("study_id")) != study:
            problems.append(f"{study}: receipt.study_id 不符")
        if str(receipt.get("output_sha256")) != entry.get("output_sha"):
            problems.append(f"{study}: receipt.output_sha 不符")
        if str(receipt.get("source_sha256")) != entry.get("analysis_source_sha"):
            problems.append(f"{study}: receipt.source_sha 不符")
        if str(receipt.get("plan_sha256")) != _cite_plan_sha(plan_doc):
            problems.append(f"{study}: receipt.plan_sha 不符")
        if str(receipt.get("confirm_manifest_sha256")) != manifest.sha256:
            problems.append(f"{study}: receipt.confirm_manifest 绑定不符")
        if receipt.get("producer") != "candidate":
            problems.append(f"{study}: 源分析 producer 不是 candidate")
        if receipt.get("validity") is not True or receipt.get("exit_code") != 0:
            problems.append(f"{study}: 源回执无效")
        # 3) scope ↔ 登记统计口径
        from orchestrator.rsi_r1 import rubric as RUL
        metric = str(scope_doc.get("metric_id"))
        if str(scope_doc.get("unit")) != str(RUL.METRICS[metric]["unit"]):
            problems.append(f"{study}: scope.unit 与登记指标不符")
        if abs(float(scope_doc.get("interval_level"))
               - float(RUL.CI_PRIMARY)) > 1e-12:
            problems.append(f"{study}: scope.interval_level 不符")
        # 4) 研究身份
        if str(task_doc.get("task_id")) != str(scope_doc.get("task_id")):
            problems.append(f"{study}: task/scope 身份不符")
        if not str(entry.get("source_campaign_id") or ""):
            problems.append(f"{study}: 缺 source_campaign_id")
    closure = {"schema": SOURCE_CLOSURE_SCHEMA, "schema_version": 1,
               "created_at_utc": utc_now(), "entries": rows,
               "problems": problems,
               "closure_sha256": canonical_sha(rows)}
    if problems:
        raise RVError("SOURCE_NOT_READY", "; ".join(problems[:8]))
    if doc.get("manifest_sha256") != canonical_sha(
            {k: v for k, v in doc.items() if k != "manifest_sha256"}):
        raise RVError("SOURCE_MANIFEST_DRIFT", "manifest 自 sha 不符")
    return closure


def _cite_plan_sha(plan_doc: Mapping[str, Any]) -> str:
    """分析回执里的 ``plan_sha256`` —— 指向 plan 正文的 canonical sha。"""
    from orchestrator.rsi_r1 import contracts as C
    return C.ResearchPlan.from_dict(dict(plan_doc)).sha256


# ---------------------------------------------------------------------------
# 公开包（allowlist、候选可见事实）
# ---------------------------------------------------------------------------

#: 每个 study 公开件白名单（file → producer）。只允许这些文件进入候选视图。
PUBLIC_FILES: "Mapping[str, str]" = {
    "brief.json": "instrument_observation",
    "task.json": "instrument_observation",
    "scope_contract.json": "instrument_observation",
    "plan.json": "instrument_observation",
    "analysis_output.json": "candidate_analysis",
    "analysis_receipt.json": "instrument_observation",
    "analysis_receipt_public.json": "instrument_observation",
    "assignments.json": "instrument_observation",
    "raw_block_values.json": "instrument_observation",
    "block_effects.json": "candidate_analysis",
    "explore_comparison.json": "candidate_analysis",
}


def _metric_block_values(study: str, batch: Path) -> dict[str, Any]:
    """从封存观测独立重算 per-block × condition 指标值（instrument 观测）。"""
    from orchestrator.rsi_r1 import g21_raw_checker as RC
    info = RC.verify_manifest(batch)
    runs = RC.load_runs(batch)
    values = RC.per_run_metric(study, runs)
    blocks = RC.block_values(runs, values)
    return {"schema": f"{SCHEMA_PREFIX}.raw_block_values.v1", "schema_version": 1,
            "study": study, "producer": "instrument_observation",
            "metric_id": None,  # 由调用方按 scope 注入
            "batch_id": info.get("batch_id"), "n_runs": len(runs),
            "values": {block: {cid: float(v) for cid, v in per.items()}
                       for block, per in sorted(blocks.items())}}


def build_public_package(root: "str | Path", manifest: Mapping[str, Any]
                         ) -> dict[str, Any]:
    """RV0/RV2：把 allowlist 内的公开事实写进 ``public/<study>/``。"""
    root_path = Path(root)
    studies = manifest["studies"]
    written: "list[dict[str, Any]]" = []
    for study in STUDIES:
        entry = studies[study]
        out = ensure_private_dir(root_path / "public" / study)
        paths = entry["paths"]
        task_doc = read_json(paths["task"]["path"])
        plan_doc = read_json(paths["plan"]["path"])
        scope_doc = read_json(paths["scope_contract"]["path"])
        output_bytes = Path(paths["output_raw"]["path"]).read_bytes()
        output_doc = json.loads(output_bytes.decode("utf-8"))
        receipt = read_json(paths["receipt"]["path"])
        from orchestrator.rsi_r1 import experiments as E
        confirm = E.manifest_from_doc(read_json(paths["confirm_manifest"]["path"]))
        raw_blocks = _metric_block_values(
            study, Path(paths["observations_batch"]["path"]))
        raw_blocks["metric_id"] = str(scope_doc.get("metric_id"))
        # 候选程序产出的 block 效应（diagnostics 第一段）；没有就如实标缺。
        block_effects = {"schema": f"{SCHEMA_PREFIX}.block_effects.v1",
                         "schema_version": 1, "study": study,
                         "producer": "candidate_analysis", "contrasts": {}}
        diagnostics = (output_doc.get("diagnostics") or {})
        for row in diagnostics.get("contrasts") or []:
            if not isinstance(row, Mapping):
                continue
            cid = str(row.get("contrast_id"))
            effects = row.get("block_effects")
            if isinstance(effects, list):
                block_effects["contrasts"][cid] = {
                    str(item.get("block_id")): float(item.get("effect"))
                    for item in effects if isinstance(item, Mapping)}
        if not block_effects["contrasts"]:
            block_effects["producer"] = "instrument_observation"
            block_effects["note"] = "候选输出未含逐 block 效应"
        assignments = {
            "schema": f"{SCHEMA_PREFIX}.assignments.v1", "schema_version": 1,
            "study": study, "producer": "instrument_observation",
            "n_blocks": confirm.block_count,
            "varying": {k: list(v) for k, v in confirm.varying.items()},
            "blocks": [
                {"block_id": a.block_id, "condition_id": a.condition_id,
                 "params": dict(a.params)} for a in confirm.assignments],
        }
        comparison = {
            "schema": f"{SCHEMA_PREFIX}.explore_comparison.v1",
            "schema_version": 1, "study": study,
            "producer": "candidate_analysis",
            "phase_comparability": dict(
                diagnostics.get("phase_comparability") or {}),
            "coverage": dict(diagnostics.get("coverage") or {}),
        }
        receipt_public = {
            "schema": f"{SCHEMA_PREFIX}.analysis_receipt_public.v1",
            "schema_version": 1, "study": study,
            "producer": "instrument_observation",
            "execution_id": receipt.get("execution_id"),
            "exit_code": receipt.get("exit_code"),
            "timed_out": receipt.get("timed_out"),
            "validity": receipt.get("validity"),
            "n_inputs": len(receipt.get("inputs") or []),
            "input_view_sha256": receipt.get("input_view_sha256"),
            "output_sha256": receipt.get("output_sha256"),
            "output_run_ids_count": len(receipt.get("output_run_ids") or []),
            "source_sha256": receipt.get("source_sha256"),
            "plan_sha256": receipt.get("plan_sha256"),
            "confirm_manifest_sha256": receipt.get("confirm_manifest_sha256"),
            "profile_id": receipt.get("profile_id"),
            "note": ("源分析由给定候选程序产生（producer=candidate）；"
                     "本回执只证明它读了这份封存观测并绑定同一计划/分配。"),
        }
        brief = _research_brief(study, task_doc, plan_doc, scope_doc, entry)
        payloads = {
            "brief.json": brief,
            "task.json": task_doc,
            "scope_contract.json": scope_doc,
            "plan.json": plan_doc,
            "analysis_receipt_public.json": receipt_public,
            "assignments.json": assignments,
            "raw_block_values.json": raw_blocks,
            "block_effects.json": block_effects,
            "explore_comparison.json": comparison,
        }
        payloads["analysis_output.json"] = output_doc
        payloads["analysis_receipt.json"] = None
        raw_payloads = {
            # envelope 的 artifact sha 绑**原始字节**；公开件必须逐字节相同，
            # 否则 worker 读取会被 ARTIFACT_SHA_MISMATCH 正确拒绝。
            "analysis_output.json": output_bytes,
            "analysis_receipt.json": Path(paths["receipt"]["path"]).read_bytes(),
        }
        for name, obj in payloads.items():
            data = raw_payloads.get(name)
            if data is None:
                data = (json.dumps(obj, ensure_ascii=False, sort_keys=True,
                                   indent=1) + "\n").encode("utf-8")
            target = out / name
            if target.exists():
                if target.read_bytes() != data:
                    raise RVError("PUBLIC_DRIFT", f"{study}/{name} 已存在且不同")
            else:
                atomic_write_text(target, data.decode("utf-8"), mode=0o600)
            written.append({"study": study, "file": f"public/{study}/{name}",
                            "sha256": sha_bytes(data), "bytes": len(data),
                            "producer": PUBLIC_FILES[name]})
    allow = {"schema": PUBLIC_ALLOWLIST_SCHEMA, "schema_version": 1,
             "created_at_utc": utc_now(),
             "files": written,
             "allowlist_sha256": canonical_sha(written)}
    write_json_private(root_path / "contract" / "public_allowlist.json", allow)
    return allow


def _research_brief(study: str, task: Mapping[str, Any],
                    plan: Mapping[str, Any], scope: Mapping[str, Any],
                    entry: Mapping[str, Any]) -> dict[str, Any]:
    """研究问题/预注册/方法/读取规则的公开说明（不含结论与审查理由）。"""
    primary = task.get("primary") or {}
    nc = task.get("negative_control_spec") or {}
    return {
        "schema": f"{SCHEMA_PREFIX}.brief.v1", "schema_version": 1,
        "study": study,
        "producer": "instrument_observation",
        "research_question": (
            f"{task.get('mechanism_id')} 上，"
            f"{primary.get('id')}（{primary.get('kind')}，"
            f"metric={primary.get('metric')}，proposition={primary.get('proposition')}，"
            f"delta={primary.get('delta')}）在给定确认设计下是否成立？"),
        "preregistered_hypotheses": [dict(h) for h in plan.get("hypotheses") or []],
        "primary_contrast": dict(plan.get("primary") or {}),
        # 零 POST 票据 B（2026-11-17）：两种 id 的角色写进公开包本身，
        # 避免 plan 的 primary_contrast 与 task.primary 并列而无用途说明。
        "id_roles": contrast_roles(task, plan),
        "negative_control": dict(plan.get("negative_control") or {}) or None,
        "negative_control_expectation": (nc.get("expectation") if nc else None),
        "conditions": [dict(c) for c in task.get("conditions") or []],
        "nuisance": dict(task.get("nuisance_schema") or {}),
        "scope": {
            "target_population": scope.get("target_population"),
            "metric_id": scope.get("metric_id"), "unit": scope.get("unit"),
            "delta": scope.get("delta"), "interval_level": scope.get("interval_level"),
            "aggregation": scope.get("aggregation"),
            "weighting": scope.get("weighting"),
            "censoring_rule": scope.get("censoring_rule"),
            "coverage": scope.get("coverage"),
            "block_to_contrast_formula": scope.get("block_to_contrast_formula"),
        },
        "actual_assignment": {
            "n_blocks": entry.get("n_blocks"),
            "n_runs": entry.get("n_runs"),
            "nuisance_owner": "host",
            "note": "逐 block 参数见 assignments.json；nuisance 为宿主分配。",
        },
        "how_to_read": {
            "result_view": ("首条 system/user 消息里的结果视图（envelope）已含 "
                            "主对比/全部必需对比/回执绑定，可直接使用。"),
            "read_result_artifact": (
                "可用 artifact_id/输出 sha/cursor/page_bytes 分页读取公开件；"
                "page_bytes ≤ 8192。"),
            "mandatory_read": "analysis/output.json 必须至少完整读取一次。",
            "submit": ("submit_claim 需携带 claims 与 research_report；"
                       "report_text 必须与报告对象的确定性 render 逐字相等。"),
        },
        "allowed_actions": ["read_result_artifact", "submit_claim"],
        "prohibited": ("禁止 write_artifact/propose_plan/analyze/freeze_confirm/"
                       "run_confirm/run_batch/KB 写入/网络/shell；"
                       "禁止读取封存前的探索批次或本 study 之外的任何结果。"),
    }


def artifact_handles(manifest: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """每 study 的结果视图 artifacts（相对路径在 RV 根内）。"""
    out: "dict[str, list[dict[str, Any]]]" = {}
    for study in STUDIES:
        entry = manifest["studies"][study]
        base = f"public/{study}"
        rows = []
        for name in ("analysis_output.json", "analysis_receipt_public.json",
                     "raw_block_values.json", "block_effects.json",
                     "assignments.json", "explore_comparison.json",
                     "task.json", "scope_contract.json", "plan.json"):
            path = base + "/" + name
            rows.append(path)
        out[study] = rows
    return out


# ---------------------------------------------------------------------------
# 预注册与许可
# ---------------------------------------------------------------------------

def build_prereg(*, round_id: str, manifest: Mapping[str, Any],
                 public_allowlist: Mapping[str, Any],
                 source_closure: Mapping[str, Any],
                 user_authorization: str) -> dict[str, Any]:
    per_study = {}
    for study in STUDIES:
        entry = manifest["studies"][study]
        per_study[study] = {
            "source_campaign_id": entry["source_campaign_id"],
            "source_run_id": entry["source_run_id"],
            "task_sha": entry["task_sha"], "plan_sha": entry["plan_sha"],
            "analysis_source_sha": entry["analysis_source_sha"],
            "output_sha": entry["output_sha"],
            "receipt_sha": entry["receipt_sha"],
            "scope_contract_sha": entry["scope_contract_sha"],
            "required_contrast_ids": entry["required_contrast_ids"],
            "interval_level": entry["interval_level"],
            "unit": entry["unit"], "metric_id": entry["metric_id"],
        }
    doc = {
        "schema": PREREG_SCHEMA, "schema_version": 1,
        "round_id": round_id, "created_at_utc": utc_now(),
        "purpose": ("在完整、可信、已给定的实验与分析材料下，当前批准模型能否"
                    "自己读取证据、给出有边界的解释与局限，并通过真实工具提交"
                    "可追溯的研究报告（MODEL_SUFFIX 可行性）。"),
        "not_purpose": ["新物理仿真", "新分析/设计源码", "修改 plan/seed/阈值",
                        "完整 R1 研究", "R2 自主修改系统", "KB 回写"],
        "studies": per_study,
        "lanes": {lane: RV_LANE_POST_CAPS[lane] for lane in RV_LANE_ORDER},
        "hard_cap": RV_HARD_CAP,
        "ledger_phase": LEDGER_RSI_RV,
        "model_stage": RV_STAGE,
        "output_tokens": RV_OUTPUT_TOKENS,
        "source_manifest_sha": manifest["manifest_sha256"],
        "source_closure_sha": source_closure["closure_sha256"],
        "public_allowlist_sha": public_allowlist["allowlist_sha256"],
        "rubric_dims": list(RUBRIC_DIMS),
        "review_channels": REVIEW_CHANNELS,
        "stopping_rules": {
            "instrument_failure": "首个项目 POST 后仪器异常 ⇒ 停当前轮，不边改边继续",
            "unknown_send": "发送/工具执行未知 ⇒ 占用不释放且 STOP_UNKNOWN",
            "legal_model_failure": "合法模型失败保留；A 失败仍测 B，预算分母不消失",
            "global_stop": "安全/来源/未知发送 ⇒ 停止全部",
        },
        "acceptance": {
            "ready": "恰 {A,B} 完整且全部工程/交付/科学审查门 PASS",
            "not_established": "任一题合法失败/预算耗尽/审查失败或分歧",
        },
        "user_authorization": user_authorization,
    }
    doc["prereg_sha256"] = canonical_sha(doc)
    return doc


def verify_prereg(doc: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(doc, Mapping) or doc.get("schema") != PREREG_SCHEMA:
        raise RVError("PREREG_INVALID", "schema")
    body = {k: v for k, v in doc.items() if k != "prereg_sha256"}
    if canonical_sha(body) != doc.get("prereg_sha256"):
        raise RVError("PREREG_DRIFT", "prereg 自 sha 不符")
    lanes = doc.get("lanes") or {}
    if any(int(lanes.get(lane, -1)) != RV_LANE_POST_CAPS[lane]
           for lane in RV_LANE_ORDER):
        raise RVError("PREREG_DRIFT", "lane 帽与冻结值不符")
    if int(doc.get("hard_cap", -1)) != RV_HARD_CAP:
        raise RVError("PREREG_DRIFT", "hard_cap")
    return dict(doc)


def build_permit(*, round_id: str, kind: str, prereg: Mapping[str, Any],
                 closure_sha256: str, worker_policy_sha: str,
                 prompt_sha: str, schema_sha: str, tokenizer_sha: str,
                 rubric_sha: str, config_sha: str,
                 calibration_report_sha: "str | None" = None,
                 expires_at_utc: "str | None" = None) -> dict[str, Any]:
    """两段许可：calibration_only（4 槽）/ suffix_and_review（余 20 槽）。"""
    if kind not in ("calibration_only", "suffix_and_review"):
        raise RVError("PERMIT_KIND", kind)
    allowed = (["review_calibration"] if kind == "calibration_only"
               else ["suffix_A", "suffix_B", "review_final"])
    doc = {
        "schema": PERMIT_SCHEMA, "schema_version": 1,
        "round_id": round_id, "kind": kind, "created_at_utc": utc_now(),
        "expires_at_utc": expires_at_utc,
        "prereg_sha256": prereg.get("prereg_sha256"),
        "source_manifest_sha": prereg.get("source_manifest_sha"),
        "source_closure_sha": prereg.get("source_closure_sha"),
        "public_allowlist_sha": prereg.get("public_allowlist_sha"),
        "implementation_closure_sha256": closure_sha256,
        "worker_policy_sha": worker_policy_sha,
        "prompt_sha": prompt_sha, "schema_sha": schema_sha,
        "tokenizer_sha": tokenizer_sha, "rubric_sha": rubric_sha,
        "config_sha": config_sha,
        "calibration_report_sha": calibration_report_sha,
        "allowed_lanes": allowed,
        "lane_caps": {lane: RV_LANE_POST_CAPS[lane] for lane in allowed},
        "hard_cap": RV_HARD_CAP,
        "ledger_phase": LEDGER_RSI_RV,
        "model_stage": RV_STAGE,
        "output_tokens": RV_OUTPUT_TOKENS,
    }
    doc["permit_sha256"] = canonical_sha(doc)
    return doc


def verify_permit(doc: Mapping[str, Any], *, prereg: Mapping[str, Any]
                  ) -> dict[str, Any]:
    if not isinstance(doc, Mapping) or doc.get("schema") != PERMIT_SCHEMA:
        raise RVError("PERMIT_INVALID", "schema")
    body = {k: v for k, v in doc.items() if k != "permit_sha256"}
    if canonical_sha(body) != doc.get("permit_sha256"):
        raise RVError("PERMIT_DRIFT", "permit 自 sha 不符")
    if doc.get("prereg_sha256") != prereg.get("prereg_sha256"):
        raise RVError("PERMIT_DRIFT", "prereg 绑定不符")
    allowed = doc.get("allowed_lanes")
    if not isinstance(allowed, list) or not allowed:
        raise RVError("PERMIT_INVALID", "allowed_lanes")
    if any(lane not in RV_LANE_ORDER for lane in allowed):
        raise RVError("PERMIT_INVALID", "未知 lane")
    for lane in allowed:
        if int((doc.get("lane_caps") or {}).get(lane, -1)) != RV_LANE_POST_CAPS[lane]:
            raise RVError("PERMIT_DRIFT", f"{lane} 帽不符")
    if doc.get("kind") == "suffix_and_review" \
            and doc.get("calibration_report_sha") is None:
        raise RVError("PERMIT_INVALID", "正式许可必须引用校准原件")
    return dict(doc)


# ---------------------------------------------------------------------------
# 六维 rubric 与合并规则（Plan §4.1）
# ---------------------------------------------------------------------------

RUBRIC_TEXT: "Mapping[str, str]" = {
    "question_and_preregistration":
        ("复述研究问题与预注册的对比/方法/范围；不得改写预注册主张或把"
         "探索目标当确认目标。"),
    "design_consistency":
        ("识别给定探索设计与声明的一致性（固定点 vs 分布、nuisance 分配、"
         "block 含义）；指出给定设计缺陷可以，但不得编造覆盖或歪曲分配。"),
    "confirm_control_and_nc":
        ("正确处理确认控制：有 NC 时必须解释其等效证据；无 NC 时不得声称"
         "已排除所有替代解释。NC 缺失被宣称完备 = 关键错误。"),
    "statistics_censoring_uncertainty":
        ("数值与来源一致；正确解释区间、删失、block 独立单位与不确定性；"
         "不得把点估计当确定结论或忽略宽区间。"),
    "claim_scope":
        ("主张范围与 target_population/weighting 一致；不得把平均效应外推"
         "到未观测条件或声明未经证明的机制。"),
    "result_driven_limitations_and_next_steps":
        ("局限由结果驱动（不是空模板），并给出真正有判别力的下一实验；"
         "允许提出域外但有理由的扩展。"),
}

RUBRIC_RULES: "Mapping[str, Any]" = {
    "verdicts": ["PASS", "FAIL", "UNVERIFIABLE"],
    "refs": ("typed locator：{\"file\": \"<packet 内相对名>\", "
             "\"json_pointer\": \"/...\"}，必须解析到 packet 内实际内容"),
    "no_machine_verdict": True,
    "no_other_review": True,
    "given_design_defects": ("准确性优先：模型准确指出给定设计/分析缺陷并相应"
                             "收敛结论可以 PASS；隐藏缺陷或断言未证明机制 FAIL。"),
    "domain_extension": ("提出当前执行域之外但有理由的扩展不自动 FAIL；"
                         "仅当主张超出证据范围才 FAIL。"),
}


def public_rubric() -> dict[str, Any]:
    return {"schema": f"{SCHEMA_PREFIX}.rubric.v1", "schema_version": 1,
            "dims": [{"id": dim, "text": RUBRIC_TEXT[dim]}
                     for dim in RUBRIC_DIMS],
            "rules": dict(RUBRIC_RULES)}


def merge_review_channels(reviews: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Plan §4.1 唯一优先级：UNVERIFIABLE → PASS → FAIL → INCONCLUSIVE。

    ``reviews`` 是两个通道各自的逐项原票（每项必须含六维与 verdict）。
    任何通道缺项/无有效收据 ⇒ ``REVIEW_UNVERIFIABLE``（不加第三票）。
    """
    if len(reviews) != REVIEW_CHANNELS:
        return {"verdict": "REVIEW_UNVERIFIABLE", "problems": ["通道数不足"],
                "items": {}}
    for index, review in enumerate(reviews):
        items = review.get("items") if isinstance(review, Mapping) else None
        if not isinstance(items, Mapping) or set(items) != set(RUBRIC_DIMS):
            return {"verdict": "REVIEW_UNVERIFIABLE",
                    "problems": [f"通道{index} 缺项"], "items": {}}
        for dim in RUBRIC_DIMS:
            row = items[dim]
            if not isinstance(row, Mapping) or row.get("verdict") not in (
                    "PASS", "FAIL", "UNVERIFIABLE"):
                return {"verdict": "REVIEW_UNVERIFIABLE",
                        "problems": [f"通道{index}.{dim} 非法"], "items": {}}
    merged = {}
    any_unver = False
    any_both_fail = False
    all_both_pass = True
    for dim in RUBRIC_DIMS:
        votes = [str(reviews[0]["items"][dim]["verdict"]),
                 str(reviews[1]["items"][dim]["verdict"])]
        merged[dim] = {"votes": votes}
        if "UNVERIFIABLE" in votes:
            any_unver, all_both_pass = True, False
        elif votes == ["PASS", "PASS"]:
            pass
        elif votes == ["FAIL", "FAIL"]:
            any_both_fail, all_both_pass = True, False
        else:
            all_both_pass = False
    if any_unver:
        verdict = "REVIEW_UNVERIFIABLE"
    elif all_both_pass:
        verdict = "REVIEW_PASS"
    elif any_both_fail:
        verdict = "REVIEW_FAIL"
    else:
        verdict = "REVIEW_INCONCLUSIVE"
    return {"verdict": verdict, "problems": [], "items": merged}


# ---------------------------------------------------------------------------
# 全轮 aggregate（Plan §4.3 真值表）
# ---------------------------------------------------------------------------

STUDY_TERMINAL_STATES = (
    "MODEL_REPORT_VALID", "MODEL_FAILED", "BUDGET_EXHAUSTED",
    "REVIEW_FAIL", "REVIEW_INCONCLUSIVE", "REVIEW_UNVERIFIABLE",
    "NOT_RUN", "PROVENANCE_UNKNOWN", "INSTRUMENT_BLOCKED")
OVERALL_STATES = (
    "INSTRUMENT_BLOCKED", "PROVENANCE_UNKNOWN", "NOT_MEASURED",
    "MODEL_SUFFIX_FEASIBLE_ON_GIVEN_EVIDENCE",
    "MODEL_SUFFIX_NOT_ESTABLISHED")


def aggregate_rv(*, studies: Mapping[str, Mapping[str, Any]],
                 expected: Sequence[str] = STUDIES) -> dict[str, Any]:
    """按 Plan §4.3 的唯一优先级给出 overall；保留每 study 原始终态。

    ``studies[study]`` 至少含 ``state``（见 ``STUDY_TERMINAL_STATES``）、
    ``gates``（工程/机器/双审必需门布尔字典）与 ``problems`` 列表。
    空集合/缺 study 一律不判 READY（防空集合 ``all()`` 晋升）。
    """
    if set(studies) != set(expected):
        return {"overall": "NOT_MEASURED",
                "problems": [f"study 集合必须恰为 {tuple(expected)}"],
                "studies": {k: dict(v) for k, v in studies.items()}}
    reasons: "list[str]" = []
    security = [k for k, v in studies.items()
                if v.get("state") == "INSTRUMENT_BLOCKED"
                or v.get("security_violation")]
    source_drift = [k for k, v in studies.items() if v.get("source_drift")]
    if security or source_drift:
        return {"overall": "INSTRUMENT_BLOCKED",
                "reasons": sorted({f"{k}:security/source" for k in security}
                                  | {f"{k}:source_drift" for k in source_drift}),
                "studies": {k: dict(v) for k, v in studies.items()}}
    unknown = [k for k, v in studies.items()
               if v.get("state") == "PROVENANCE_UNKNOWN"
               or v.get("send_unknown") or v.get("tool_unknown")]
    if unknown:
        return {"overall": "PROVENANCE_UNKNOWN",
                "reasons": sorted(f"{k}:provenance" for k in unknown),
                "studies": {k: dict(v) for k, v in studies.items()}}
    valid_reports = [k for k, v in studies.items()
                     if v.get("state") == "MODEL_REPORT_VALID"]
    if not valid_reports:
        not_started = all(v.get("state") in ("NOT_RUN",)
                          for v in studies.values())
        return {"overall": "NOT_MEASURED" if not_started else "MODEL_SUFFIX_NOT_ESTABLISHED",
                "reasons": ["无 suffix 有效报告"], "studies": {k: dict(v) for k, v in studies.items()}}
    all_gates = True
    for study, row in studies.items():
        gates = row.get("gates") or {}
        if row.get("state") != "MODEL_REPORT_VALID" or not gates \
                or not all(bool(gates[k]) for k in sorted(gates)):
            all_gates = False
            missing = [k for k in sorted(gates) if not gates[k]]
            reasons.append(f"{study}: gate {missing or row.get('state')}")
    # 必需门集合必须完整（缺项不算 PASS）
    required_gates = {"source_valid", "actual_worker_isolated",
                      "request_fidelity", "candidate_origin", "binding",
                      "report_evidence", "review_verdict", "numeric_fidelity"}
    for study, row in studies.items():
        gates = row.get("gates") or {}
        if set(gates) != required_gates:
            all_gates = False
            reasons.append(f"{study}: gate 集合不完整")
    if all_gates and len(valid_reports) == len(expected):
        overall = "MODEL_SUFFIX_FEASIBLE_ON_GIVEN_EVIDENCE"
    else:
        overall = "MODEL_SUFFIX_NOT_ESTABLISHED"
    return {"overall": overall, "reasons": reasons,
            "studies": {k: dict(v) for k, v in studies.items()}}


# ---------------------------------------------------------------------------
# 路径/文件工具（公开包读取）
# ---------------------------------------------------------------------------

def public_path(root: "str | Path", study: str, name: str) -> Path:
    if study not in STUDIES or name not in PUBLIC_FILES:
        raise RVError("PUBLIC_FORBIDDEN", f"{study}/{name}")
    return _rel_within(Path(root).resolve(strict=False),
                       f"public/{study}/{name}")


def verify_public_package(root: "str | Path",
                          allowlist: Mapping[str, Any]) -> dict[str, Any]:
    root_path = Path(root)
    problems: "list[str]" = []
    seen: "set[tuple[str, str]]" = set()
    for row in allowlist.get("files") or []:
        study = str(row.get("study"))
        rel = str(row.get("file"))
        if not rel.startswith(f"public/{study}/"):
            problems.append(f"{rel}: study 前缀不符")
            continue
        name = posixpath.basename(rel)
        if name not in PUBLIC_FILES:
            problems.append(f"{rel}: 不在 allowlist")
            continue
        if (study, name) in seen:
            problems.append(f"{rel}: 重复")
        seen.add((study, name))
        path = root_path / rel
        if not path.is_file():
            problems.append(f"{rel}: 缺失")
        elif sha_file(path) != row.get("sha256"):
            problems.append(f"{rel}: sha 漂移")
    for study in STUDIES:
        for name in PUBLIC_FILES:
            if (study, name) not in seen:
                problems.append(f"public/{study}/{name}: 未登记")
    if problems:
        raise RVError("PUBLIC_NOT_READY", "; ".join(problems[:8]))
    return {"ok": True, "n_files": len(seen),
            "allowlist_sha256": allowlist.get("allowlist_sha256")}


CONTRAST_ROLES_SCHEMA = f"{SCHEMA_PREFIX}.contrast_roles.v1"


def _id_of(obj: Any, key: str) -> str:
    if isinstance(obj, Mapping):
        return str(obj.get(key) or "")
    return str(getattr(obj, key, "") or "")


def contrast_roles(task: Any, plan: Any = None) -> dict[str, Any]:
    """两种 id 的唯一权威解释：task 对比 id 用于报告；plan 组件锚点只标假设归属。

    2026-11-17 核验：B 的冻结 brief 里同时出现 ``tc_x_inertia``（task.primary.id）
    与 ``component_single_factor``（plan.primary.id 与 plan.hypotheses[].contrast_id），
    且 brief 把后者叫 ``primary_contrast``，没有角色说明；模型两轮把后者写进
    ``primary_results[0].contrast_id``。提示、预检错误文本与 brief 注记都从这里取
    同一份措辞。``task``/``plan`` 既可是契约对象也可是其公开 dict。
    """
    task_primary = (task.get("primary") if isinstance(task, Mapping)
                    else getattr(task, "primary", None))
    task_cid = _id_of(task_primary, "id")
    hyps = (plan.get("hypotheses") if isinstance(plan, Mapping)
            else getattr(plan, "hypotheses", ())) or ()
    plan_cids = sorted({_id_of(h, "contrast_id") for h in hyps} - {""})
    plan_primary = (plan.get("primary") if isinstance(plan, Mapping)
                    else getattr(plan, "primary", None))
    return {"schema": CONTRAST_ROLES_SCHEMA, "schema_version": 1,
            "task_contrast_id": task_cid,
            "task_contrast_role": "报告 primary_results[].contrast_id 与 claims 的"
                                  "对比 id 必须用它（task.json primary.id）",
            "plan_primary_contrast_id": _id_of(plan_primary, "id"),
            "plan_primary_role": "plan 组件层主对比；brief.primary_contrast 就是它，"
                                 "不是报告里的对比 id",
            "plan_component_ids": plan_cids,
            "plan_component_role": "plan.hypotheses[].contrast_id 是组件设计锚点，"
                                   "只标注假设属于哪个组件；不是报告里的对比 id"}


def render_contrast_roles(roles: Mapping[str, Any]) -> str:
    lines = ["## 标识符角色（唯一权威解释）",
             f"- task 对比 id：{roles['task_contrast_id']} —— "
             f"{roles['task_contrast_role']}"]
    if roles.get("plan_primary_contrast_id"):
        lines.append(f"- plan 主对比：{roles['plan_primary_contrast_id']} —— "
                     f"{roles.get('plan_primary_role', '')}")
    if roles.get("plan_component_ids"):
        lines.append("- plan 组件锚点："
                     + ", ".join(roles["plan_component_ids"]) + " —— "
                     + str(roles["plan_component_role"]))
    return "\n".join(lines)


__all__ = [
    "ARTIFACT_MAX_RV", "LEDGER_RSI_RV", "MAX_MANDATORY_READ_TURNS",
    "MAX_PATH_TURNS", "MIN_PATH_TURNS", "PAGE_BYTES_RV", "PUBLIC_FILES",
    "REVIEW_CHANNELS", "REVIEW_INPUT_TOKENS_RV", "REVIEW_PACKET_BYTES_RV",
    "RUBRIC_DIMS", "RUBRIC_TEXT", "RVError", "RV_HARD_CAP", "RV_LANE_ORDER",
    "RV_LANE_POST_CAPS", "RV_LANE_TASK_ID", "RV_OUTPUT_TOKENS", "RV_STAGE",
    "RV_TASK_CAPS", "SCHEMA_PREFIX", "STUDIES", "aggregate_rv",
    "artifact_handles", "build_permit", "build_prereg",
    "build_public_package", "build_source_manifest", "canonical",
    "canonical_sha", "contrast_roles", "merge_review_channels",
    "public_path", "public_rubric", "read_json", "render_contrast_roles",
    "sha_bytes", "sha_file", "utc_now", "verify_permit",
    "verify_prereg", "verify_public_package", "verify_source_manifest",
    "write_json_private", "write_text_private",
]
