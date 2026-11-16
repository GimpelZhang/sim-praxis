"""RSI-G2.1 合成「封存 study」构建器（**fixture_only**；不是真实证据）。

用途：在没有付费/物理的前提下，构造一个与封存件**同构**的最小研究批
（32 block × 条件，真实 ``TaskSpecV13``/``ResearchPlan``/``ConfirmManifest``/
``ExperimentSpec``/观测 manifest/候选分析输出/凭据/状态事件），供
``test_rsi_g21_submit_e2e`` 驱动真实的 ``ResearchLoop``/``CampaignDispatcher``
/``judge_v13`` 走通 envelope→read→submit。

诚实边界（Plan §3.1）：

* 本模块**不产生**任何可引用的科学/工程证据；产物只写测试临时目录；
* 它不替代封存 O-A/O-B 的 source readiness 判定；
* 候选分析输出由宿主用**生产估计器**在同一 raw 上生成（fixture 生成器，不是
  被测对象的输出），因此 judge 的数值比较是自洽构建，不代表候选能力。

生成结构（与 ``bridge-*`` 输入清单一致）::

    <root>/private/tasks/task_a.json
    <root>/private/receipts/A-confirm.json
    <root>/private/analysis/A-confirm.raw.json + A-confirm.json
    <root>/confirm/plan_A.json + manifest_A.json
    <root>/designs/<analysis_source>.py
    <root>/observations/confirm-A-<token>/{manifest.json,run-*.jsonl,run-*.meta.json}
    <root>/state/journal.jsonl + state/replies/<id>.txt
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from orchestrator.rsi_r1 import analysis_receipt as AR
from orchestrator.rsi_r1 import contracts as C
from orchestrator.rsi_r1 import diagnostics as DG
from orchestrator.rsi_r1 import experiments as E
from orchestrator.rsi_r1 import judge as J
from orchestrator.rsi_r1 import observations as O
from orchestrator.rsi_r1 import profiles as P
from orchestrator.rsi_r1 import state as S
from orchestrator.rsi_r1.state import PRODUCER_CANDIDATE
from orchestrator.rsi_r1 import task_specs as TS

BLOCK_COUNT = 32
#: 每 study 的宿主 nuisance 变动参数（与任务 nuisance_schema 的支持集一致）。
VARYING = {"A": ("mass_kg", "0.2", "1.0"),
           "B": ("target_freq_hz", "0.2", "2.0")}
TOKEN = "5eed00000000c0de00000000000000aa"
STUDY = "A"


def AR_receipt_producer() -> str:
    from orchestrator.rsi_r1.assembly import receipt_producer
    from orchestrator.rsi_r1.state import PRODUCER_CANDIDATE
    return receipt_producer({"producer": PRODUCER_CANDIDATE})


def sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write(path: Path, text: str, mode: int = 0o600) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(text, encoding="utf-8")
    path.chmod(mode)
    return path


def _json_doc(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=1,
                      allow_nan=False) + "\n"


def _design_source() -> bytes:
    return ("# fixture_only analysis source (never executed in G2.1)\n"
            "# 本文件只用于满足凭据的 source_file_sha 绑定。\n"
            "print('{}')\n").encode("utf-8")


A_LEVELS = {"hi_base": 0.050, "hi_low_mu": 0.090, "lo_base": 0.005,
            "lo_low_mu": 0.010, "sham": 0.050}
B_LEVELS = {"s_low": 0.10, "l_low": 0.05, "l_high": 0.19, "s_high": 0.20}


def _displacement(condition_id: str, block_index: int) -> float:
    """设计好的 DiD 与阴性对照（噪声极小但有 block 间异质性）。"""
    return A_LEVELS[condition_id] + 1e-6 * (block_index % 7)


def _trajectory(study: str, condition_id: str, block_index: int,
                *, n: int = 40, dt: float = 0.1,
                amplitude: float = 0.2,
                frequency: float = 1.0) -> dict[str, Any]:
    import math
    times = [round(i * dt, 6) for i in range(n)]
    if study == "A":
        target = _displacement(condition_id, block_index)
        x = [target * (i / (n - 1)) for i in range(n)]
        return {"time_s": times, "qpos": [[v, 0.0, 0.05, 0.0, 0.0, 0.0, 0.0]
                                          for v in x],
                "qvel": [[0.2, 0.0, 0.0, 0.0, 0.0, 0.0] for _ in range(n)]}
    if study == "B":
        # 让窗口内 RMSE = level（常数偏差 ⇒ 归一化后等于偏差/幅值）
        level = B_LEVELS[condition_id] + 1e-6 * (block_index % 7)
        target = [amplitude * math.sin(2 * math.pi * frequency * t)
                  for t in times]
        q = [value + level * amplitude for value in target]
        return {"time_s": times,
                "qpos": [[value, 0.0, 0.0, 0.0, 0.0, 0.0] for value in q],
                "qvel": [[0.0, 0.0, 0.0, 0.0, 0.0, 0.0] for _ in range(n)],
                "target_q": [[value, 0.0, 0.0, 0.0, 0.0, 0.0]
                             for value in target],
                "instability_events": []}
    raise ValueError(study)


def _condition_params(task: TS.TaskSpecV13) -> dict[str, dict[str, str]]:
    return {c.id: dict(c.params) for c in task.conditions}


def _plan_doc(task: TS.TaskSpecV13, *, design_sha: str,
              analysis_sha: str) -> dict[str, Any]:
    study = task.mechanism_id.split("_")[0]
    varying_name, varying_lo, varying_hi = VARYING[study]
    primary_terms = task.primary.terms
    plus = next(cid for cid, w in primary_terms if str(w) == "1")
    minus = next(cid for cid, w in primary_terms if str(w) == "-1")
    nc = (task.negative_control_spec or {}).get("contrast") or {}
    return {
        "schema": C.PLAN_SCHEMA, "schema_version": 1,
        "study_id": study, "revision": 2,
        "parent_sha": "e" * 64, "phase": "confirm",
        "conditions": [c.to_dict() for c in task.conditions],
        "primary": {"id": task.primary.id, "kind": "primary",
                    "metric": task.primary.metric,
                    "proposition": task.primary.proposition,
                    "delta": str(task.primary.delta),
                    "condition_ids": [minus, plus]},
        "negative_control": (None if not nc else {
            "id": str(nc.get("id")), "kind": "negative",
            "metric": str(nc.get("metric")),
            "proposition": str(nc.get("proposition")),
            "delta": str(nc.get("delta")),
            "condition_ids": [str(t["condition_id"]) for t in nc["terms"]]}),
        "hypotheses": [{"id": "h_interaction",
                        "contrast_id": task.primary.id,
                        "predicted": task.primary.proposition,
                        "mechanism": "fixture_only 合成机制",
                        "falsification": "fixture_only 合成否证条件"}],
        "nuisance_distribution": {"generator": "host_uniform_v1",
                                  "block_count": BLOCK_COUNT,
                                  "varying": {varying_name: [varying_lo,
                                                             varying_hi]}},
        "analysis_source_sha": analysis_sha,
        "design_source_sha": design_sha,
    }


def _assignments(params: Mapping[str, Mapping[str, str]], study: str
                 ) -> "list[E.ConfirmAssignment]":
    name, lo, hi = VARYING[study]
    lo_f, hi_f = float(lo), float(hi)
    out: "list[E.ConfirmAssignment]" = []
    for index in range(1, BLOCK_COUNT + 1):
        block_id = f"b{index:02d}"
        value = lo_f + (hi_f - lo_f) * ((index - 1) % BLOCK_COUNT) / (
            BLOCK_COUNT - 1)
        for condition_id in sorted(params):
            values = dict(params[condition_id])
            values[name] = f"{value:.6f}"
            out.append(E.ConfirmAssignment(condition_id=condition_id,
                                           block_id=block_id, params=values))
    return out


def _journal_events(study: str, *, plan: Mapping[str, Any],
                    reply_id: str, action_sha: str, primary_metric: str,
                    primary_id: str,
                    design_sha: str, analysis_sha: str,
                    manifest_sha: str, plan_sha: str,
                    receipt_sha: str, block_count: int) -> "list[tuple[str, dict]]":
    events: "list[tuple[str, dict]]" = [
        ("root_transition", {"from": "INIT", "to": "AUTHORIZED",
                             "reason": "fixture_only"}),
        ("root_transition", {"from": "AUTHORIZED", "to": "CALIBRATED",
                             "reason": "fixture_only"}),
        ("root_transition", {"from": "CALIBRATED", "to": "FROZEN",
                             "reason": "fixture_only"}),
        ("root_transition", {"from": "FROZEN", "to": f"STUDY_{study}",
                             "reason": "fixture_only"}),
        ("study_transition", {"study": study, "from": "INIT",
                              "to": "EXPLORING", "reason": "fixture_only"}),
        ("plan_revision", {"study": study, "doc": dict(plan),
                           "parent_sha256": None, "plan_sha256": plan_sha,
                           "revision": 1}),
        ("artifact", {"study": study, "kind": "design",
                      "source_sha256": design_sha, "response_id": reply_id,
                      "action_sha256": action_sha, "producer": PRODUCER_CANDIDATE,
                      "original_producer": PRODUCER_CANDIDATE,
                      "assisted": False}),
        ("artifact", {"study": study, "kind": "analysis",
                      "source_sha256": analysis_sha, "response_id": reply_id,
                      "action_sha256": action_sha, "producer": PRODUCER_CANDIDATE,
                      "original_producer": PRODUCER_CANDIDATE,
                      "assisted": False}),
        ("study_transition", {"study": study, "from": "EXPLORING",
                              "to": "CONFIRM_LOCKED", "reason": "fixture_only"}),
        ("confirm_lock", {"study": study, "token": TOKEN,
                          "analysis_source_sha256": analysis_sha,
                          "analysis_reply_id": reply_id,
                          "analysis_action_id": action_sha,
                          "analysis_receipt_sha256": receipt_sha,
                          "design_source_sha256": design_sha,
                          "design_reply_id": reply_id,
                          "design_action_id": action_sha,
                          "distribution_sha256": "f" * 64,
                          "metric_id": str(primary_metric),
                          "primary_id": str(primary_id),
                          "plan_sha256": plan_sha, "block_count": block_count,
                          "checks": []}),
        ("confirm_intent", {"intent_id": f"confirm-{study}-{TOKEN[:12]}",
                            "specs_sha256": "a" * 64, "study": study,
                            "token": TOKEN}),
        ("confirm_result", {"intent_id": f"confirm-{study}-{TOKEN[:12]}",
                            "outcome": "OK", "result_sha256": "b" * 64,
                            "study": study}),
        ("confirm_publish", {"intent_id": f"confirm-{study}-{TOKEN[:12]}",
                             "manifest_sha256": manifest_sha, "study": study}),
        ("study_transition", {"study": study, "from": "CONFIRM_LOCKED",
                              "to": "RESULTS_RELEASED",
                              "reason": "fixture_only"}),
    ]
    return events


def build_sealed_study(root: "str | Path", study: str = "A") -> dict[str, Any]:
    """在新目录里造一个完整（A 含 NC）的合成封存 study；返回路径表。"""
    if study not in ("A", "B"):
        raise ValueError(study)
    root = Path(root)
    task = (TS.build_new_a(commitment="c" * 64) if study == "A"
            else TS.build_new_b(commitment="c" * 64))
    design = _design_source()
    design_sha = sha_bytes(design)
    analysis_sha = sha_bytes("# fixture_only analysis source\n".encode() + design)
    task_doc = task.to_dict()
    _write(root / "private" / "tasks" / f"task_{study.lower()}.json",
           _json_doc(task_doc))
    plan_doc = _plan_doc(task, design_sha=design_sha,
                         analysis_sha=analysis_sha)
    plan = C.ResearchPlan.from_dict(plan_doc)
    _write(root / "confirm" / f"plan_{study}.json", _json_doc(plan_doc))
    _write(root / "designs" / f"{analysis_sha}.py",
           ("# fixture_only analysis source\n" + design.decode()))
    params = _condition_params(task)
    manifest = E.ConfirmManifest(
        study_id=study, plan_sha256=plan.sha256,
        keeper_seed_sha256="d" * 64, block_count=BLOCK_COUNT,
        varying={VARYING[study][0]: (VARYING[study][1], VARYING[study][2])},
        assignments=tuple(_assignments(params, study)))
    _write(root / "confirm" / f"manifest_{study}.json",
           _json_doc(manifest.to_dict()))
    specs = J.build_specs(plan, manifest)
    batch_id = f"confirm-{study}-{TOKEN[:12]}"
    batch_root = root / "observations" / batch_id
    entries: "list[O.ManifestEntry]" = []
    for run_id in sorted(specs):
        spec = specs[run_id]
        block_index = int(spec.block_id[1:])
        amplitude = float(spec.physical_params.get("target_amplitude_rad") or 0.2)
        record = _trajectory(
            study, spec.condition_id, block_index, amplitude=amplitude,
            frequency=float(spec.physical_params.get("target_freq_hz") or 1.0))
        record.update({"run_id": run_id, "block_id": spec.block_id,
                       "spec_hash": spec.spec_hash,
                       "instability_events": [], "nsteps": len(
                           record["time_s"])})
        meta = {"schema": "simpraxis.rsi_r1.run_meta.v1", "schema_version": 1,
                "run_id": run_id, "study_id": study,
                "block_id": spec.block_id,
                "condition_id": spec.condition_id,
                "spec_hash": spec.spec_hash, "status": "ok",
                "batch_id": batch_id, "initial_x_m": 0.0,
                "nsteps_recorded": len(record["time_s"]),
                "spec_envelope": {"params_applied": dict(spec.physical_params)},
                "reply_extra": {}}
        jsonl = zip_write(batch_root / f"{run_id}.jsonl",
                          json.dumps(record, ensure_ascii=False,
                                     sort_keys=True, allow_nan=False) + "\n")
        zip_write(batch_root / f"{run_id}.meta.json", _json_doc(meta))
        entries.append(O.manifest_entry(run_id, f"{run_id}.jsonl", jsonl,
                                        len(record["time_s"])))
    _write(batch_root / "manifest.json",
           _json_doc(O.build_manifest(entries, study=study, batch_id=batch_id)))
    return _finish(root, study=study, task=task, task_doc=task_doc, plan=plan,
                   plan_doc=plan_doc, manifest=manifest, specs=specs,
                   batch_id=batch_id, batch_root=batch_root,
                   design_sha=design_sha, analysis_sha=analysis_sha)


def zip_write(path: Path, text: str) -> Path:
    return _write(path, text)


def _finish(root: Path, *, study: str, task: TS.TaskSpecV13,
            task_doc: Mapping[str, Any],
            plan: C.ResearchPlan, plan_doc: Mapping[str, Any],
            manifest: E.ConfirmManifest, specs: Mapping[str, Any],
            batch_id: str, batch_root: Path, design_sha: str,
            analysis_sha: str) -> dict[str, Any]:
    # 观测产物（production 读回路径）
    artifacts = {}
    for run_id in sorted(specs):
        artifacts[run_id] = {
            "meta": json.loads((batch_root / f"{run_id}.meta.json").read_text()),
            "record": json.loads((batch_root / f"{run_id}.jsonl").read_text())}
    per_run = J.recompute_metrics(plan=plan, artifacts=artifacts, specs=specs)
    blocks: "dict[str, dict[str, float]]" = {}
    for run_id, row in per_run.items():
        spec = specs[run_id]
        blocks.setdefault(spec.block_id, {})[spec.condition_id] = float(
            row["values"][task.primary.metric])
    primary = task.primary.estimate(blocks)
    nc_doc = (task.negative_control_spec or {}).get("contrast") or {}
    nc_est = None
    if nc_doc:
        from orchestrator.rsi_r1 import contrasts as X
        nc = X.ContrastV13.from_dict(dict(nc_doc, schema="simpraxis.rsi_r13.contrast.v1",
                                          schema_version=1)).estimate(blocks)
        nc_est = nc
    contrasts = [{"contrast_id": task.primary.id,
                  "metric_id": task.primary.metric,
                  "proposition": task.primary.proposition,
                  "delta": float(task.primary.delta),
                  "estimate": primary["effect"], "lower": primary["lower"],
                  "upper": primary["upper"], "n_blocks": primary["n_blocks"],
                  "status": primary["claim"]}]
    if nc_est is not None:
        contrasts.append({"contrast_id": str(nc_doc.get("id")),
                          "metric_id": str(nc_doc.get("metric")),
                          "proposition": str(nc_doc.get("proposition")),
                          "delta": float(nc_doc.get("delta")),
                          "estimate": nc_est["effect"],
                          "lower": nc_est["lower"], "upper": nc_est["upper"],
                          "n_blocks": nc_est["n_blocks"],
                          "status": nc_est["claim"]})
    effect_rows = task.primary.block_effects(blocks)
    diagnostics = {
        "schema": DG.DIAG_SCHEMA, "schema_version": 1, "phase": "confirm",
        "study_id": study, "task_id": task.task_id,
        "input_sha256": plan.sha256, "source_sha256": analysis_sha,
        "contrasts": [{"contrast_id": task.primary.id,
                       "effect": primary["effect"],
                       "lower": primary["lower"], "upper": primary["upper"],
                       "n_blocks": primary["n_blocks"],
                       "block_effects": [{"block_id": bid,
                                          "effect": effect_rows[bid]}
                                         for bid in sorted(effect_rows)]}],
        "coverage": {"blocks": sorted(blocks), "run_count": len(specs),
                     "unique_design_count": len(specs),
                     "independent_unit_count": len(blocks)},
        "summaries": [{"group_id": "all", "metric": task.primary.metric,
                       "n": len(per_run), "mean": primary["effect"],
                       "median": primary["effect"], "min": primary["effect"],
                       "max": primary["effect"], "preregistered": False}],
        "threshold_counts": [{
            "contrast_id": task.primary.id,
            "above_delta": sum(1 for v in effect_rows.values()
                               if v > float(task.primary.delta)),
            "below_minus_delta": sum(1 for v in effect_rows.values()
                                     if v < -float(task.primary.delta)),
            "within": sum(1 for v in effect_rows.values()
                          if -float(task.primary.delta) <= v
                          <= float(task.primary.delta))}],
        "phase_comparability": {"explore_target": "fixture_only",
                                "confirm_target": "fixture_only",
                                "relation": "same_target",
                                "rationale": "fixture_only 合成 compare"},
    }
    output_doc = {"schema": "simpraxis.rsi_r1.analysis_output.v1",
                  "schema_version": 1, "run_ids": sorted(specs),
                  "contrasts": contrasts, "diagnostics": diagnostics}
    output_bytes = (json.dumps(output_doc, ensure_ascii=False, sort_keys=True,
                               allow_nan=False) + "\n").encode("utf-8")
    _write(root / "private" / "analysis" / f"{study}-confirm.raw.json",
           output_bytes.decode("utf-8"))
    pointer = {"schema": "simpraxis.rsi_r1.analysis_pointer.v1",
               "schema_version": 1, "study_id": study, "phase": "confirm",
               "relpath": f"private/analysis/{study}-confirm.raw.json",
               "raw_relpath": f"private/analysis/{study}-confirm.raw.json",
               "sha256": sha_bytes(output_bytes),
               "raw_sha256": sha_bytes(output_bytes)}
    _write(root / "private" / "analysis" / f"{study}-confirm.json",
           _json_doc(pointer))
    reply_id = "fixture-reply-001"
    action_sha = "1" * 64
    inputs = []
    for run_id in sorted(specs):
        for suffix in (".jsonl", ".meta.json"):
            name = f"{batch_id}/{run_id}{suffix}"
            inputs.append({"run_id": run_id, "relative_path": name,
                           "sha256": sha_bytes((root / "observations" / name)
                                               .read_bytes())})
    receipt = AR.build_receipt(
        profile_id=P.PROFILE_R13, study_id=study, phase="confirm",
        plan_sha256=plan.sha256,
        confirm_manifest_sha256=manifest.sha256,
        source_sha256=analysis_sha, registered_reply_id=reply_id,
        registered_action_id=action_sha,
        producer=AR_receipt_producer(),
        inputs=inputs, execution_id="fixture-exec-0001",
        sandbox_status="ok", exit_code=0, timed_out=False,
        output_bytes=output_bytes, output_doc=output_doc,
        validity=True)
    _write(root / "private" / "receipts" / f"{study}-confirm.json",
           _json_doc(receipt))
    # 状态 journal（typed loader 的输入面）
    events = _journal_events(study, plan=plan_doc, reply_id=reply_id,
                             primary_metric=task.primary.metric,
                             primary_id=task.primary.id,
                             action_sha=action_sha, design_sha=design_sha,
                             analysis_sha=analysis_sha,
                             manifest_sha=manifest.sha256,
                             plan_sha=plan.sha256,
                             receipt_sha=receipt["output_sha256"],
                             block_count=len(blocks))
    journal = S.Journal(FixtureCtx(root))
    reply_text = '{"action":"analyze","source_sha256":"%s","run_ids":[]}' % analysis_sha
    _write(root / "state" / "replies" / f"{reply_id}.txt", reply_text)
    for name, payload in events:
        journal.append(name, dict(payload))
    task_canon_sha = sha_bytes(json.dumps(
        task_doc, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False).encode("utf-8"))
    sealed_scope = {
        "schema": "simpraxis.rsi_g2.scope_contract.v1", "schema_version": 1,
        "contract_id": f"g2-fixture-{task.task_id}",
        "task_id": task.task_id, "task_spec_sha256": task_canon_sha,
        "hypothesis_id": f"{task.task_id}:main",
        "estimand_id": task.primary.id, "metric_id": task.primary.metric,
        "proposition": task.primary.proposition,
        "delta": float(task.primary.delta), "unit": "m",
        "target_population": "registered_distribution", "aggregation": "mean",
        "weighting": "equal", "block_to_contrast_formula": "mean",
        "formula_notes": "fixture_only", "censoring_rule": "none",
        "coverage": "observed_blocks", "interval_level": 0.9,
        "allowed_semantic_labels": ["SUPPORTED", "REFUTED", "INCONCLUSIVE",
                                    "INCONSISTENT_EVIDENCE"],
        "notes": "fixture_only sealed scope（故意 0.9，用于演练 provenance）",
    }
    _write(root / "contract" / "g2_sealed_scope.json",
           _json_doc(sealed_scope))
    journal.append("reply", {"study": study, "response_id": reply_id,
                             "text_sha256": sha_bytes(reply_text.encode()),
                             "n_bytes": len(reply_text.encode()),
                             "phase": "confirm", "nonce": "fixture",
                             "relpath": f"state/replies/{reply_id}.txt"})
    return {"root": str(root),
            "task": str(root / "private/tasks" / f"task_{study.lower()}.json"),
            "plan": str(root / "confirm" / f"plan_{study}.json"),
            "design": str(root / "designs" / f"{analysis_sha}.py"),
            "receipt": str(root / "private/receipts" / f"{study}-confirm.json"),
            "output_raw": str(root / "private/analysis"
                              / f"{study}-confirm.raw.json"),
            "output_pointer": str(root / "private/analysis"
                                  / f"{study}-confirm.json"),
            "manifest": str(root / "confirm" / f"manifest_{study}.json"),
            "study": study,
            "observations": str(batch_root),
            "journal": str(root / "state/journal.jsonl"),
            "sealed_scope": str(root / "contract/g2_sealed_scope.json"),
            "analysis_source_sha": analysis_sha,
            "design_source_sha": design_sha,
            "plan_sha256": plan.sha256,
            "manifest_sha256": manifest.sha256,
            "n_runs": len(specs)}


def replay_manifest_doc(root: "str | Path") -> dict[str, Any]:
    """造 A/B 两个合成封存 study 并返回 G2.1 输入清单（fixture_only）。"""
    root = Path(root)
    studies: "dict[str, Any]" = {}
    for study in ("A", "B"):
        info = build_sealed_study(root / f"source_{study}", study)
        studies[study] = {
            "task": info["task"], "plan": info["plan"],
            "source_design": info["design"], "receipt": info["receipt"],
            "output_raw": info["output_raw"],
            "output_pointer": info["output_pointer"],
            "confirm_manifest": info["manifest"],
            "observations_batch": info["observations"],
            "scope_contract": info["sealed_scope"],
            "journal": info["journal"]}
    return {"schema": "simpraxis.rsi_g21.replay_manifest.v1",
            "schema_version": 1, "source_campaign": "fixture_only",
            "source_commit": "0" * 40,
            "g2": {"root": str(root / "source_A"),
                   "contract_snapshot": studies["A"]["scope_contract"]},
            "studies": studies}


class FixtureCtx:
    """最小 ctx（Journal 只用到 root/相对路径写）。"""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def relative(self, rel: str) -> Path:
        return self.root / rel

    def subdir(self, name: str) -> Path:
        path = self.root / name
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        return path

    def assert_write_allowed(self, target: "str | Path") -> Path:
        return Path(target)
