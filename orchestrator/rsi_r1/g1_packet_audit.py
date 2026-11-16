"""RSI-G1 审查包语义守恒审计（零 POST；Plan §3 G2 / §5 case 8）。

独立于生产 ``build_formal_packet``：本模块**只读** TaskSpec、确认 manifest、
trusted raw 重算值与历史包，逐值对账，不调用生产构造器、不修改旧包、不改判。
四类历史缺陷各给稳定错误码（Plan G1-08）：

* ``PACKET_INTENT_STUDY``  —— 包内 design_intent 不是本 study 的（D10）；
* ``PACKET_METRIC``        —— 主对比 metric 与任务登记不符（D11）；
* ``PACKET_NUISANCE``      —— 逐 block nuisance 缺失/与分配不符（D12）；
* ``PACKET_EVIDENCE_EMPTY``—— evidence_index 为空。

另有块集/逐块数值/主效应重算/任务绑定四项守恒检查。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from orchestrator.rsi_r1 import g1_delivery as GD
from orchestrator.rsi_r1 import g1_trace as GT

AUDIT_SCHEMA = "simpraxis.rsi_g1.packet_audit.v1"

CODES = ("PACKET_INTENT_STUDY", "PACKET_METRIC", "PACKET_NUISANCE",
         "PACKET_EVIDENCE_EMPTY", "PACKET_BLOCKS", "PACKET_VALUES",
         "PACKET_RECOMPUTE", "PACKET_TASK")


@dataclass(frozen=True)
class PacketAudit:
    items: "tuple[dict[str, Any], ...]"
    codes: "tuple[str, ...]"

    @property
    def ok(self) -> bool:
        return not self.codes

    def as_dict(self) -> "dict[str, Any]":
        return {"schema": AUDIT_SCHEMA, "schema_version": 1,
                "ok": self.ok, "verdict": "PASS" if self.ok else "FAIL",
                "items": [dict(i) for i in self.items],
                "codes": list(self.codes),
                "rule": "只审包内一致性，不重判旧 study、不修改历史包"}


def _params_of(assignment: Any) -> "dict[str, str]":
    if isinstance(assignment, Mapping):
        return {str(k): str(v) for k, v in (assignment.get("params") or {}).items()}
    return {str(k): str(v) for k, v in (getattr(assignment, "params", {}) or {}).items()}


def _block_of(assignment: Any) -> str:
    if isinstance(assignment, Mapping):
        return str(assignment.get("block_id"))
    return str(getattr(assignment, "block_id"))


def block_values_from_raw(task: Any, raw_metrics: Mapping[str, Mapping[str, Any]],
                          run_specs: Sequence[Any]) -> "tuple[dict[str, dict[str, float]], list[str]]":
    """按任务主指标把 trusted raw 重算值聚成逐 block 表（与生产同源语义）。

    ``run_specs`` 是 ``{run_id, block_id, condition_id}`` 行（来自宿主重建的
    spec，不是候选自报）；确认 manifest 的 ``assignments`` 没有 run_id。
    """
    metric = str(task.primary.metric)
    block_of: "dict[str, str]" = {}
    cond_of: "dict[str, str]" = {}
    for row in run_specs:
        rid = (row.get("run_id") if isinstance(row, Mapping)
               else getattr(row, "run_id", None))
        if rid is None:
            continue
        block_of[str(rid)] = _block_of(row)
        cond_of[str(rid)] = str(row.get("condition_id") if isinstance(row, Mapping)
                                else getattr(row, "condition_id", ""))
    values: "dict[str, dict[str, float]]" = {}
    missing: "list[str]" = []
    for run_id, row in raw_metrics.items():
        block = block_of.get(str(run_id))
        cond = cond_of.get(str(run_id))
        if block is None or cond is None:
            missing.append(str(run_id))
            continue
        mvals = (row.get("values") if isinstance(row, Mapping) else None) or {}
        if metric not in mvals:
            missing.append(str(run_id))
            continue
        values.setdefault(block, {})[cond] = float(mvals[metric])
    return values, missing


def validate_packet(task: Any,
                    assignments: Sequence[Any],
                    raw_metrics: Mapping[str, Mapping[str, Any]],
                    packet: Mapping[str, Any], *,
                    run_specs: "Sequence[Any] | None" = None,
                    expect_blocks: "int | None" = None) -> PacketAudit:
    """逐项对账；返回 PacketAudit（不改输入、不写盘）。"""
    items: "list[dict[str, Any]]" = []
    codes: "list[str]" = []

    def add(item_id: str, ok: bool, detail: Any = None, code: "str | None" = None) -> None:
        items.append({"id": item_id, "ok": bool(ok), "detail": detail})
        if not ok and code and code not in codes:
            codes.append(code)

    # 1) 任务绑定
    pkt_task = packet.get("task") or {}
    task_ok = (str(pkt_task.get("task_id")) == str(task.task_id)
               and str(packet.get("task_sha256") or "") == str(task.sha256))
    add("PACKET_TASK", task_ok,
        {"task_id": pkt_task.get("task_id"), "sha_match": task_ok}, "PACKET_TASK")

    # 2) 设计意图属于本 study（D10）
    intent = packet.get("design_intent") or {}
    estimand = (intent.get("estimand") or {}) if isinstance(intent, Mapping) else {}
    contrast_ids = [str(c) for c in (estimand.get("contrast_ids") or [])]
    metric_id = str(estimand.get("metric_id") or "")
    intent_ok = (contrast_ids == [str(task.primary.id)]
                 and metric_id == str(task.primary.metric))
    add("PACKET_INTENT_STUDY", intent_ok,
        {"contrast_ids": contrast_ids, "metric_id": metric_id,
         "want_contrast": str(task.primary.id),
         "want_metric": str(task.primary.metric)}, "PACKET_INTENT_STUDY")

    # 3) 主对比 metric（D11）
    primary_contrast = packet.get("primary_contrast") or {}
    metric_ok = str(primary_contrast.get("metric")) == str(task.primary.metric)
    add("PACKET_METRIC", metric_ok,
        {"metric": primary_contrast.get("metric"),
         "want": str(task.primary.metric)}, "PACKET_METRIC")

    # 4) 逐 block nuisance（D12）
    nuisance_names = sorted(str(k) for k in (task.nuisance_schema or {}))
    want_nuisance: "dict[str, dict[str, str]]" = {}
    for row in assignments:
        params = _params_of(row)
        block = _block_of(row)
        for name in nuisance_names:
            if name in params:
                want_nuisance.setdefault(block, {})[name] = params[name]
    table = packet.get("block_table") or []
    nuisance_problems: "list[str]" = []
    for row in table:
        block = str(row.get("block_id"))
        got = {str(k): str(v) for k, v in (row.get("nuisance") or {}).items()}
        want = want_nuisance.get(block)
        if want is None:
            nuisance_problems.append(f"{block}:manifest 无分配")
            continue
        for name in nuisance_names:
            if got.get(name) != want.get(name):
                nuisance_problems.append(f"{block}.{name}")
    add("PACKET_NUISANCE", not nuisance_problems,
        {"problems": nuisance_problems[:8],
         "keys": nuisance_names, "n_rows": len(table)}, "PACKET_NUISANCE")

    # 5) evidence_index 非空
    evidence = packet.get("evidence_index") or {}
    add("PACKET_EVIDENCE", bool(evidence),
        {"keys": sorted(evidence)[:6], "n": len(evidence)},
        "PACKET_EVIDENCE_EMPTY")

    # 6) 块集完整
    specs = list(run_specs) if run_specs is not None else list(assignments)
    raw_values, missing_runs = block_values_from_raw(task, raw_metrics, specs)
    blocks_packet = sorted(str(row.get("block_id")) for row in table)
    blocks_raw = sorted(raw_values)
    blocks_ok = (blocks_packet == blocks_raw and not missing_runs
                 and (expect_blocks is None or len(blocks_packet) == int(expect_blocks)))
    add("PACKET_BLOCKS", blocks_ok,
        {"packet": len(blocks_packet), "raw": len(blocks_raw),
         "missing_runs": missing_runs[:5],
         "expect": expect_blocks}, "PACKET_BLOCKS")

    # 7) 逐块数值 = trusted raw 重算
    value_problems: "list[str]" = []
    by_block = {str(row.get("block_id")): row for row in table}
    for block, conds in raw_values.items():
        row = by_block.get(block)
        if row is None:
            continue
        got = row.get("values") or {}
        # 只比主对比实际使用的条件；sham 等额外条件不在块表值里是设计如此。
        for cond in [str(c) for c in task.primary.condition_ids]:
            if cond not in conds:
                value_problems.append(f"{block}.{cond}:raw 缺")
                continue
            want = conds[cond]
            got_value = got.get(cond)
            if got_value is None or not GD.close(got_value, want):
                value_problems.append(f"{block}.{cond}")
    add("PACKET_VALUES", not value_problems,
        {"problems": value_problems[:8]}, "PACKET_VALUES")

    # 8) 主效应重算
    recompute_ok = False
    recomputed: "dict[str, Any]" = {}
    try:
        primary = task.primary.estimate(raw_values)
        recomputed = {"effect": float(primary["effect"]),
                      "lower": float(primary["lower"]),
                      "upper": float(primary["upper"]),
                      "n_blocks": int(primary["n_blocks"])}
        claimed = packet.get("primary_recomputation") or {}
        recompute_ok = (GD.close(claimed.get("effect"), recomputed["effect"])
                        and int(claimed.get("n_blocks") or -1) == recomputed["n_blocks"])
    except Exception as exc:  # noqa: BLE001 —— 重算失败即审计失败（不是科学结论）
        recomputed = {"error": f"{type(exc).__name__}: {exc}"}
    add("PACKET_RECOMPUTE", recompute_ok, recomputed, "PACKET_RECOMPUTE")

    return PacketAudit(items=tuple(items), codes=tuple(codes))


def audit_to_disk(audit: PacketAudit, out_dir: Any, *, name: str) -> "dict[str, Any]":
    """落盘（0600）；返回写入体的 sha（供 AC 记录报告 hash）。"""
    from pathlib import Path
    from orchestrator.common.fsutil import atomic_write_text, ensure_private_dir
    doc = audit.as_dict()
    doc["name"] = name
    text = json.dumps(doc, ensure_ascii=False, sort_keys=True, indent=1) + "\n"
    out = Path(out_dir)
    ensure_private_dir(out)
    atomic_write_text(out / f"{name}.json", text, mode=0o600)
    doc["report_sha256"] = GT.sha_bytes(text.encode("utf-8"))
    return doc


# ---------------------------------------------------------------------------
# 真实历史输入装载（只读；维护侧自有路径）
# ---------------------------------------------------------------------------

class MaintenanceInputs(dict):
    """维护侧输入包：明确标 store_kind=maintenance（编译器必须拒收）。"""

    maintenance = True
    store_kind = "maintenance"


def load_study_inputs(root: Any, study: str) -> "dict[str, Any]":
    """读一个 study 的 TaskSpec/manifest/trusted raw/historical packet。

    只接受明确的 campaign 根（调用方给绝对路径）；不自动发现目录。
    """
    from pathlib import Path
    import json as _json
    from orchestrator.rsi_r1 import contracts as C
    from orchestrator.rsi_r1 import experiments as E
    from orchestrator.rsi_r1 import judge as J
    from orchestrator.rsi_r1 import task_specs as TS

    root = Path(root)
    task = TS.TaskSpecV13.from_dict(_json.loads(
        (root / f"private/tasks/task_{study.lower()}.json").read_text(encoding="utf-8")))
    plan = C.ResearchPlan.from_dict(_json.loads(
        (root / f"confirm/plan_{study}.json").read_text(encoding="utf-8")))
    manifest = E.manifest_from_doc(_json.loads(
        (root / f"confirm/manifest_{study}.json").read_text(encoding="utf-8")))
    specs = J.build_specs(plan, manifest)
    artifacts: "dict[str, dict[str, Any]]" = {}
    base = root / "observations"
    for run_id in specs:
        hits = list(base.glob(f"*/{run_id}.jsonl"))
        if not hits:
            raise FileNotFoundError(f"缺 run 观测：{run_id}")
        path = hits[0]
        artifacts[run_id] = {
            "record": _json.loads(path.read_text(encoding="utf-8")),
            "meta": _json.loads(path.with_name(f"{run_id}.meta.json")
                                .read_text(encoding="utf-8")),
        }
    raw_metrics = J.recompute_metrics(plan=plan, artifacts=artifacts, specs=specs)
    packets = sorted((root / "reviews").glob(f"review-{study.lower()}-rev-*"))
    if not packets:
        raise FileNotFoundError(f"缺历史审查包：review-{study.lower()}-rev-*")
    packet = _json.loads((packets[0] / "packet.json").read_text(encoding="utf-8"))
    return MaintenanceInputs({"task": task, "plan": plan, "manifest": manifest,
            "raw_metrics": raw_metrics, "packet": packet,
            "packet_dir": packets[0], "assignments": list(manifest.assignments),
            "run_specs": [{"run_id": spec.run_id, "block_id": spec.block_id,
                           "condition_id": spec.condition_id}
                          for spec in specs.values()]})
