"""RSI-R1-RV 薄 CLI：冻结 → 预检 → 零 POST 预演 → 校准 → A/B → 双审 → 结案。

所有 Python 使用指定 venv；付费命令必须显式 ``SIMPRAXIS_ALLOW_PAID=1``，
``preflight_blocked`` 事件不占实呼；每个 POST 前复核 ledger/permit/闭包。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

from orchestrator.common import research_transport as T
from typing import Any, Mapping, Sequence

from orchestrator.common.fsutil import ensure_private_dir
from orchestrator.rsi_r1 import g21_bridge as G21
from orchestrator.rsi_r1 import g2_contract as G2
from orchestrator.rsi_r1 import review_receipt as RRC
from orchestrator.rsi_r1 import rv_contract as RV
from orchestrator.rsi_r1 import rv_review as RVW
from orchestrator.rsi_r1 import rv_runtime as RT
from orchestrator.rsi_r1 import rv_worker as W
from orchestrator.rsi_r1 import state as S
from orchestrator.rsi_r1 import task_specs as TS

USER_AUTHORIZATION = (
    "执行Plan：/home/vipuser/文档/RSI_R1-RV_Plan_detailed.md。授权：(1) 除"
    "「commit author 必须是 Junchuan Zhang <zjunchuan@gmail.com>」与「push 到 "
    "github remote 时必须保证所有帐号密码安全」外，其余硬性规定可按需突破；"
    "(2) 新轮次/重新预注册无需停下等批准；(3) sudo 口令在 "
    "~/access/access_methods.txt。")


class CLIError(RV.RVError):
    pass


# ---------------------------------------------------------------------------
# 基础
# ---------------------------------------------------------------------------

def _bind_env(root: Path) -> None:
    """把 RV 账根/campaign 绑进本进程（只影响本命令）。"""
    os.environ["SIMPRAXIS_RSI_RV_ROOT"] = str(root.resolve(strict=False).parent)
    os.environ["SIMPRAXIS_RSI_RV_CAMPAIGN"] = root.name


def _round(root: "str | Path") -> Path:
    path = Path(root).resolve(strict=False)
    _bind_env(path)
    return path


def _round_id(root: Path) -> str:
    return root.name


def _manifest(root: Path) -> dict[str, Any]:
    path = root / "manifest" / "rv_sources.json"
    if not path.is_file():
        raise CLIError("MANIFEST_MISSING", str(path))
    return RV.read_json(path)


def _prereg(root: Path) -> dict[str, Any]:
    path = root / "contract" / "prereg.json"
    if not path.is_file():
        raise CLIError("PREREG_MISSING", str(path))
    return RV.read_json(path)


def _allowlist(root: Path) -> dict[str, Any]:
    return RV.read_json(root / "contract" / "public_allowlist.json")


def _closure(root: Path) -> dict[str, Any]:
    files = [RT.repo_root() / rel for rel in RT._IMPLEMENTATION_FILES]
    missing = [str(p) for p in files if not p.is_file()]
    if missing:
        raise CLIError("CLOSURE_FILE_MISSING", str(missing[:3]))
    return G2.implementation_closure(files)


def _closure_reader(root: Path):
    def reader() -> Mapping[str, Any]:
        return _closure(root)
    return reader


def _public_verifier(root: Path):
    def verifier() -> Mapping[str, Any]:
        return RV.verify_public_package(root, _allowlist(root))
    return verifier


def _tokenizer_snapshot(root: Path) -> dict[str, Any]:
    from orchestrator.rsi_r1 import review_capacity as RC
    path = Path(RC.DEFAULT_TOKENIZER_PATH)
    doc = {"schema": f"{RV.SCHEMA_PREFIX}.tokenizer.v1", "schema_version": 1,
           "path": str(path), "expected_sha256": RC.DEFAULT_TOKENIZER_SHA256,
           "input_token_cap": RV.REVIEW_INPUT_TOKENS_RV,
           "output_token_cap": RV.RV_OUTPUT_TOKENS,
           "framing": ("system 1 条 + messages 逐条；请求序列化 = "
                       "json.dumps({system,messages,max_tokens}, sort_keys=True)"),
           "supplier_token_count": "UNKNOWN_NOT_CLAIMED",
           "created_at_utc": RV.utc_now()}
    if not path.is_file():
        raise CLIError("TOKENIZER_UNAVAILABLE", str(path))
    doc["sha256"] = RV.sha_file(path)
    if doc["sha256"] != RC.DEFAULT_TOKENIZER_SHA256:
        raise CLIError("TOKENIZER_APPROVAL_UNRESOLVED", doc["sha256"])
    RC.load_tokenizer()
    return doc


def _config_snapshot() -> dict[str, Any]:
    stage_models = RT.repo_root() / "orchestrator" / "gateway" / "stage_models.yaml"
    text = stage_models.read_text(encoding="utf-8")
    model = None
    in_coding = False
    for line in text.splitlines():
        if line.strip().startswith("coding_debug:"):
            in_coding = True
            continue
        if in_coding:
            if line.strip().startswith("model_id:"):
                model = line.split(":", 1)[1].strip()
                break
            if line and not line.startswith(" "):
                break
    if not model:
        raise CLIError("STAGE_UNRESOLVED", "coding_debug.model_id 未解析")
    return {"schema": f"{RV.SCHEMA_PREFIX}.config.v1", "schema_version": 1,
            "stage_models_path": str(stage_models),
            "stage_models_sha256": RV.sha_file(stage_models),
            "stage": RV.RV_STAGE, "model_id": model,
            "output_tokens": RV.RV_OUTPUT_TOKENS,
            "temperature": RV.RV_TEMPERATURE,
            "request_timeout_s": RV.RV_REQUEST_TIMEOUT_S,
            "endpoint": "ApprovedProvider (credentials from env; never recorded)",
            "created_at_utc": RV.utc_now()}


def _prompt_sha(root: Path) -> str:
    parts = []
    for study in RV.STUDIES:
        brief = RV.read_json(root / "public" / study / "brief.json")
        parts.append(RT._rv_system(study, brief, scripted=False))
    parts.append(RVW.build_review_request(
        packet={"study": "A", "packet_body_sha256": "0" * 64},
        calibration=False)[0])
    parts.append(RVW.SLOT_RUNNER)
    return RV.sha_bytes("\n\n".join(parts).encode("utf-8"))


def _schema_sha() -> str:
    return RV.canonical_sha({"rubric": RV.public_rubric(),
                             "report": "simpraxis.rsi_r13.research_report.v1",
                             "review": RV.REVIEW_SCHEMA,
                             "calibration": RV.CALIBRATION_SCHEMA})


def _ledger(root: Path) -> Any:
    return RT.rv_ledger(root, _round_id(root))


def _require_paid() -> None:
    if os.environ.get("SIMPRAXIS_ALLOW_PAID") != "1":
        raise CLIError("PAID_NOT_AUTHORIZED", "需 SIMPRAXIS_ALLOW_PAID=1")


def _write_approval(root: Path, batch_id: str, *, task: int, approved: int,
                    note: str) -> Path:
    from orchestrator.common.runtime_paths import rsi_rv_approvals_dir
    path = ensure_private_dir(rsi_rv_approvals_dir()) / f"{batch_id}.json"
    RV.write_json_private(path, {
        "schema": "simpraxis.quota.batch_approval.v1", "schema_version": 1,
        "batch_id": batch_id, "task": int(task), "stage": RV.RV_STAGE,
        "approved": int(approved), "beyond_normal": True,
        "ledger_phase": RV.LEDGER_RSI_RV,
        "approved_at_utc": RV.utc_now(),
        "approval_ref": "~/文档/RSI_R1-RV_Plan_detailed.md §5（用户实施授权）",
        "note": note})
    return path


def _permits(root: Path) -> "dict[str, dict[str, Any]]":
    out = {}
    for name, key in (("permit_calibration.json", "calibration"),
                      ("permit_suffix.json", "suffix")):
        path = root / "contract" / name
        if path.is_file():
            out[key] = RV.read_json(path)
    return out


def _gateway(root: Path):
    from orchestrator.common.runtime_paths import rsi_rv_gateway_state_path
    ledger = _ledger(root)
    gateway = RT.build_rv_gateway(
        ledger=ledger, pause_state_path=rsi_rv_gateway_state_path())
    return gateway, ledger


# ---------------------------------------------------------------------------
# init（RV0）
# ---------------------------------------------------------------------------

def cmd_init(args: argparse.Namespace) -> int:
    root = _round(args.root)
    source_map = RV.read_json(args.source_map)
    ensure_private_dir(root)
    RV.write_json_private(root / "manifest" / "source_map.json", source_map)
    manifest = RV.build_source_manifest(source_map)
    RV.write_json_private(root / "manifest" / "rv_sources.json", manifest)
    closure = RV.verify_source_manifest(manifest)
    RV.write_json_private(root / "manifest" / "source_closure.json", closure)
    # 统计口径：两源必须同合同（否则不是同一套复算口径）。
    stats = None
    for study in RV.STUDIES:
        path = (Path(manifest["studies"][study]["g21_root"])
                / "freeze" / "stat_contract.json")
        doc = RV.read_json(path)
        if stats is None:
            stats = doc
        elif RV.canonical(stats) != RV.canonical(doc):
            raise CLIError("STAT_CONTRACT_MISMATCH", f"{study}")
    RV.write_json_private(root / "manifest" / "stat_contract.json", stats)
    allow = RV.build_public_package(root, manifest)
    RV.verify_public_package(root, allow)
    budget = RT.RVBudget(envelope_id=_round_id(root),
                         posts_by_lane=dict(RV.RV_LANE_POST_CAPS))
    RV.write_json_private(root / "contract" / "budget.json", budget.to_dict())
    RV.write_json_private(root / "contract" / "tokenizer.json",
                          _tokenizer_snapshot(root))
    RV.write_json_private(root / "contract" / "config_snapshot.json",
                          _config_snapshot())
    RV.write_json_private(root / "contract" / "implementation_closure.json",
                          _closure(root))
    RV.write_json_private(root / "contract" / "user_authorization.json", {
        "schema": f"{RV.SCHEMA_PREFIX}.authorization.v1", "schema_version": 1,
        "verbatim": USER_AUTHORIZATION, "created_at_utc": RV.utc_now(),
        "plan_path": "/home/vipuser/文档/RSI_R1-RV_Plan_detailed.md"})
    prereg = RV.build_prereg(
        round_id=_round_id(root), manifest=manifest, public_allowlist=allow,
        source_closure=closure, user_authorization=USER_AUTHORIZATION)
    RV.write_json_private(root / "contract" / "prereg.json", prereg)
    RV.write_json_private(root / "contract" / "envelope.json", {
        "schema": f"{RV.SCHEMA_PREFIX}.round.v1", "schema_version": 1,
        "round_id": _round_id(root), "mode": RV.MODE_SOURCE,
        "offline_validation": False})
    print(json.dumps({"ok": True, "root": str(root),
                      "manifest_sha256": manifest["manifest_sha256"],
                      "source_closure_sha256": closure["closure_sha256"],
                      "public_allowlist_sha256": allow["allowlist_sha256"],
                      "prereg_sha256": prereg["prereg_sha256"]},
                     ensure_ascii=False, sort_keys=True))
    return 0


# ---------------------------------------------------------------------------
# preflight（零 POST）
# ---------------------------------------------------------------------------

def _probe_envelope(root: Path, study: str, manifest: Mapping[str, Any]) -> dict[str, Any]:
    entry = manifest["studies"][study]
    task_doc = RV.read_json(root / "public" / study / "task.json")
    scope = G2.load_scope_contract(root / "public" / study / "scope_contract.json")
    return RT.build_rv_envelope(
        study=study, plan_sha256=entry["plan_sha"],
        output_bytes=Path(entry["paths"]["output_raw"]["path"]).read_bytes(),
        receipt_bytes=Path(entry["paths"]["receipt"]["path"]).read_bytes(),
        output_rel=f"analysis/{study}/output.json",
        receipt_rel=f"analysis/{study}/receipt.json",
        scope=scope, task=TS.TaskSpecV13.from_dict(task_doc),
        public_root=Path(root) / "public")


def cmd_preflight(args: argparse.Namespace) -> int:
    root = _round(args.root)
    problems: "list[str]" = []
    checks: "dict[str, Any]" = {}
    try:
        manifest = _manifest(root)
        closure = RV.verify_source_manifest(manifest)
        checks["source"] = {"closure_sha256": closure["closure_sha256"],
                            "entries": len(closure["entries"])}
    except RV.RVError as exc:
        problems.append(f"source:{exc.code}:{exc.detail}")
        manifest = None
    try:
        allow = _allowlist(root)
        checks["public"] = RV.verify_public_package(root, allow)
    except (RV.RVError, OSError) as exc:
        problems.append(f"public:{type(exc).__name__}:{exc}")
    try:
        disk = _closure(root)
        frozen = RV.read_json(root / "contract" / "implementation_closure.json")
        checks["closure"] = disk["closure_sha256"]
        if disk["closure_sha256"] != frozen.get("closure_sha256"):
            problems.append("closure: STALE（实现已漂移）")
    except (RV.RVError, OSError, ValueError) as exc:
        problems.append(f"closure:{type(exc).__name__}:{exc}")
    try:
        tok = _tokenizer_snapshot(root)
        checks["tokenizer"] = tok["sha256"]
    except (RV.RVError, OSError) as exc:
        problems.append(f"tokenizer:{type(exc).__name__}:{exc}")
    try:
        cfg = _config_snapshot()
        frozen_cfg = RV.read_json(root / "contract" / "config_snapshot.json")
        checks["config"] = cfg["model_id"]
        if cfg["model_id"] != frozen_cfg.get("model_id") \
                or cfg["stage_models_sha256"] != frozen_cfg.get(
                    "stage_models_sha256"):
            problems.append("config: DRIFT（模型/配置已漂移）")
        if int(cfg["output_tokens"]) != RV.RV_OUTPUT_TOKENS:
            problems.append("config: output_tokens != 32768")
    except (RV.RVError, OSError) as exc:
        problems.append(f"config:{type(exc).__name__}:{exc}")
    if manifest is not None:
        policy = W.WorkerPolicy()
        checks["worker"] = {"policy_sha256": policy.sha256,
                            "image": policy.image,
                            "reader_sha256": policy.reader_sha256}
        try:
            image_id = subprocess.run(
                ["docker", "image", "inspect", "-f", "{{.Id}}", policy.image],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
            if image_id.returncode != 0:
                problems.append("worker: 批准镜像不存在")
            else:
                checks["worker"]["image_id"] = image_id.stdout.decode().strip()
        except (OSError, subprocess.SubprocessError) as exc:
            problems.append(f"worker:docker:{type(exc).__name__}")
        try:
            cap = RV.read_json(root / "contract" / "capacity.json") \
                if (root / "contract" / "capacity.json").is_file() else None
            if cap is None:
                env_a = _probe_envelope(root, "A", manifest)
                system = RT._rv_system("A", RV.read_json(
                    root / "public" / "A" / "brief.json"), scripted=False)
                cap = RT.capacity_report(
                    manifest=manifest, allowlist=_allowlist(root),
                    system_probe=system, envelope_probe=env_a)
                RV.write_json_private(root / "contract" / "capacity.json", cap)
            checks["capacity"] = cap["ok"]
            if not cap["ok"]:
                problems.extend(f"capacity:{p}" for p in cap["problems"])
        except (RV.RVError, OSError, ValueError) as exc:
            problems.append(f"capacity:{type(exc).__name__}:{exc}")
    report = {"schema": f"{RV.SCHEMA_PREFIX}.preflight.v1", "schema_version": 1,
              "created_at_utc": RV.utc_now(), "ok": not problems,
              "problems": problems, "checks": checks}
    RV.write_json_private(root / "preflight" / "report.json", report)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if not problems else 1


# ---------------------------------------------------------------------------
# 零 POST 预演（RV3）
# ---------------------------------------------------------------------------

class _NullLedger:
    def _read_rows(self) -> "list[dict[str, Any]]":
        return []


def cmd_validate_offline(args: argparse.Namespace) -> int:
    root = _round(args.root)
    manifest = _manifest(root)
    prereg = _prereg(root)
    allow = _allowlist(root)
    policy = W.WorkerPolicy()
    out: "dict[str, Any]" = {"schema": f"{RV.SCHEMA_PREFIX}.offline.v1",
                             "schema_version": 1, "created_at_utc": RV.utc_now()}
    problems: "list[str]" = []
    # 1) 实际 worker 隔离（同一 executable/image/mount 策略）
    iso = W.run_isolation(root, policy=policy)
    out["isolation"] = {"ok": iso["ok"], "summary": iso["summary"],
                        "policy_sha256": policy.sha256}
    if not iso["ok"]:
        problems.append("ISOLATION_FAILED")
    # 2) 两 study 脚本正控：真实 dispatcher/loop/worker
    budget = RT.RVBudget(envelope_id=_round_id(root),
                         posts_by_lane=dict(RV.RV_LANE_POST_CAPS))
    lanes = RT.RVSlotBook(RT.RVContext(root), budget, prefix="offline")
    studies: "dict[str, Any]" = {}
    for study in RV.STUDIES:
        material = RT.materialize_rv_study(
            root, manifest, study, studies_dir="offline_studies")
        entry = manifest["studies"][study]
        primary = material["task_doc"]["primary"]
        hypotheses = [str(h.get("id")) for h in
                      (material["plan_doc"].get("hypotheses") or [])]
        consumer = RT.ScriptedRVConsumer(
            study=study, receipt_sha=entry["receipt_sha"],
            task_id=str(material["task_doc"]["task_id"]),
            primary_id=str(primary["id"]),
            required=entry["required_contrast_ids"],
            hypotheses=hypotheses, metric_id=str(primary["metric"]),
            delta=float(primary["delta"]),
            proposition=str(primary["proposition"]))
        RT.assert_offline_sender(consumer)
        parts = RT.build_rv_parts(
            root=root, study=study, manifest=manifest, material=material,
            prereg=prereg, budget=budget, lanes=lanes, ledger=_NullLedger(),
            permits={}, closure_reader=_closure_reader(root),
            public_verifier=_public_verifier(root), sender=consumer,
            lane=f"suffix_{study}", policy=policy, scripted=True,
            worker_root=root / "worker_offline")
        outcome = RT.run_turns(parts, max_turns=RV.MAX_PATH_TURNS)
        faces = _five_faces(parts.study_ctx, study)
        judge = _judge_report(parts.study_ctx, study, material["plan"])
        studies[study] = {"turns": outcome,
                          "five_faces_ok": bool(faces.get("ok")),
                          "faces": faces,
                          "judge_verdict": (judge or {}).get("verdict")}
        if outcome["study_state"] not in (S.ST_CLAIM_SUBMITTED, S.ST_VERIFIED,
                                          S.ST_VALID_FAIL):
            problems.append(f"{study}: 脚本正控未到提交终态（{outcome['study_state']}）")
        if not faces.get("ok"):
            problems.append(f"{study}: 五面校验失败")
    # 1b) 正式审查包容量（Plan §4.4）：用"最坏情况"合成报告按实际 tokenizer 测。
    try:
        probe = _review_capacity_probe(root, manifest, prereg)
        out["review_capacity"] = probe
        if not probe["ok"]:
            problems.append(f"REVIEW_PACKET_TOO_LARGE:{probe['detail']}")
    except (RV.RVError, OSError, ValueError) as exc:
        problems.append(f"review_capacity:{type(exc).__name__}:{exc}")
    # 3) 负控：错 sha 必须被 worker/reader 拒绝；错 study/缺 NC 的包必须被拒
    neg: "dict[str, Any]" = {}
    try:
        parts_a = None
        # 直接通过 reader 触发：用错 sha 读取
        reader = RT.make_artifact_reader(
            worker_root=root, study="A", policy=policy,
            publication=root / "public" / "A")
        registry = G2.ArtifactRegistry(
            [{"artifact_id": "analysis/output.json",
              "relative_path": "analysis/A/output.json",
              "sha256": manifest["studies"]["A"]["output_sha"],
              "bytes": len((root / "public" / "A" / "analysis_output.json").read_bytes()),
              "access_mode": "read_result_artifact"}])
        try:
            reader(registry=registry, artifact_id="analysis/output.json",
                   output_sha256="0" * 64, cursor=0, page_bytes=4096)
            neg["wrong_sha"] = "NOT_DETECTED"
        except G2.G2Error as exc:
            neg["wrong_sha"] = exc.code
        # D10/D13：错 study / 缺 NC 的审查包
        for name, mutate in (
                ("wrong_study", lambda p: {**p, "study": "B"}),
                ("missing_nc", lambda p: {**p,
                    "task": {**p["task"], "negative_control_spec": None}})):
            packet = RVW.build_rv_packet(
                root=root, study="A", report=None,
                study_root=root / "offline_studies" / "A")
            try:
                RVW.validate_packet(mutate(packet), expected_study="A")
                neg[name] = "NOT_DETECTED"
            except RVW.ReviewError as exc:
                neg[name] = exc.code
        out["negative_controls"] = neg
        for key, value in neg.items():
            if value == "NOT_DETECTED":
                problems.append(f"negative_control:{key}")
    except (RV.RVError, OSError, ValueError) as exc:
        problems.append(f"negative_control:{type(exc).__name__}:{exc}")
    out["studies"] = studies
    out["ok"] = not problems
    out["problems"] = problems
    RV.write_json_private(root / "offline" / "report.json", out)
    print(json.dumps({"ok": out["ok"], "problems": problems,
                      "isolation": out["isolation"]["ok"],
                      "studies": {k: v["turns"]["study_state"]
                                  for k, v in studies.items()},
                      "negative_controls": out.get("negative_controls")},
                     ensure_ascii=False, sort_keys=True))
    return 0 if out["ok"] else 1


def _review_capacity_probe(root: Path, manifest: Mapping[str, Any],
                           prereg: Mapping[str, Any]) -> dict[str, Any]:
    """构造最坏情况正式审查包（约 12 KB 报告正文）并测 token 帽。"""
    from orchestrator.rsi_r1 import rv_review as RVW
    study = "A"
    entry = manifest["studies"][study]
    task = RV.read_json(root / "public" / study / "task.json")
    output = RV.read_json(root / "public" / study / "analysis_output.json")
    filler = ("这项解释引用给定来源并在登记的目标总体内收敛；" * 20)
    locator = {"kind": "json_pointer", "file": "analysis/output.json",
               "sha256": entry["output_sha"], "pointer": "/contrasts/0/estimate"}
    report = {
        "schema": "simpraxis.rsi_r13.research_report.v1", "schema_version": 1,
        "task_id": task["task_id"],
        "question_and_hypotheses": filler,
        "methods_and_assignment_responsibility": filler,
        "primary_results": [{"contrast_id": task["primary"]["id"],
                             "analysis_receipt_sha": entry["receipt_sha"],
                             "result_locator": locator}],
        "heterogeneity_and_scope": [{"claim": filler, "evidence_refs": [locator],
                                     "applicability": filler}],
        "explore_confirm_comparison": {"estimands": filler, "comparable": False,
                                       "explanation": filler,
                                       "evidence_refs": [locator]},
        "alternative_explanations": [filler, filler],
        "limitations": [{"statement": filler, "evidence_refs": [locator],
                         "consequence": filler}],
        "next_steps": [{"unresolved_question": filler,
                        "discriminating_experiment": filler,
                        "possible_outcomes": [filler, filler]}],
    }
    packet = RVW.build_rv_packet(
        root=root, study=study, report=report, report_text=filler,
        model_lineage={"analysis_receipt_sha": entry["receipt_sha"]},
        study_root=root / "offline_studies" / study)
    sys_, user = RVW.build_review_request(
        packet=packet, calibration=False, review_id="probe-final-A",
        reviewer_id="probe")
    try:
        m = RVW.measure_request(system=sys_, user=user, label="probe-final-A")
        ok, detail = True, ""
    except Exception as exc:  # noqa: BLE001 —— 超帽即阻断
        ok, detail = False, f"{getattr(exc, 'code', type(exc).__name__)}: {exc}"
        m = {"input_tokens": None, "bytes": len(sys_) + len(user)}
    return {"ok": ok, "detail": detail, "tokens": m.get("input_tokens"),
            "bytes": m.get("bytes"), "cap_tokens": RV.REVIEW_INPUT_TOKENS_RV}


def _five_faces(study_ctx: RT.RVContext, study: str) -> dict[str, Any]:
    try:
        return RT.five_faces(study_ctx, study)
    except (RV.RVError, S.StateError) as exc:
        return {"ok": False, "code": getattr(exc, "code", type(exc).__name__),
                "detail": str(exc)}


def _judge_report(study_ctx: RT.RVContext, study: str, plan: Any) -> dict[str, Any] | None:
    # dispatcher 写 judge/report_<study>.json（closure.write_judge_report 的相对路径）
    for rel in (f"judge/report_{study}.json", f"judge/{study}.json",
                "judge/report.json"):
        if study_ctx.exists(rel):
            return study_ctx.read_json(rel)
    return None


# ---------------------------------------------------------------------------
# 许可
# ---------------------------------------------------------------------------

def _require_recovery_approval(root: Path) -> None:
    """若本轮带 recovery 预注册，执行前必须有人工批准件；否则拒绝付费授权。"""
    rp = root / "contract" / "recovery_prereg.json"
    if not rp.is_file():
        return
    doc = RV.read_json(rp)
    if doc.get("status") != "APPROVED":
        raise CLIError("RECOVERY_NOT_APPROVED",
                       "recovery 预注册仍为 PENDING_USER_APPROVAL")
    consent_path = root / "contract" / "recovery_consent.json"
    if not consent_path.is_file():
        raise CLIError("RECOVERY_CONSENT_MISSING", str(consent_path))
    consent = RV.read_json(consent_path)
    if consent.get("approved") is not True:
        raise CLIError("RECOVERY_NOT_APPROVED", "consent.approved != true")
    if str(consent.get("prereg_sha256")) != RV.sha_file(rp):
        raise CLIError("RECOVERY_CONSENT_STALE", "pre-registration sha mismatch")


def cmd_authorize_calibration(args: argparse.Namespace) -> int:
    root = _round(args.root)
    _require_recovery_approval(root)
    prereg = RV.verify_prereg(_prereg(root))
    closure = _closure(root)
    policy = W.WorkerPolicy()
    tok = RV.read_json(root / "contract" / "tokenizer.json")
    cfg = _config_snapshot()
    permit = RV.build_permit(
        round_id=_round_id(root), kind="calibration_only", prereg=prereg,
        closure_sha256=closure["closure_sha256"],
        worker_policy_sha=policy.sha256, prompt_sha=_prompt_sha(root),
        schema_sha=_schema_sha(), tokenizer_sha=str(tok["sha256"]),
        rubric_sha=RV.canonical_sha(RV.public_rubric()),
        config_sha=RV.canonical_sha(cfg))
    RV.write_json_private(root / "contract" / "permit_calibration.json", permit)
    _write_approval(root, f"{_round_id(root)}-calibration", task=0, approved=4,
                    note="review_calibration 4 槽（Plan §5）")
    print(json.dumps({"ok": True, "permit_sha256": permit["permit_sha256"],
                      "kind": permit["kind"]}, sort_keys=True))
    return 0


def cmd_qualify(args: argparse.Namespace) -> int:
    root = _round(args.root)
    prereg = RV.verify_prereg(_prereg(root))
    cal = RV.read_json(root / "review" / "calibration_report.json")
    if not cal.get("ok"):
        raise CLIError("CALIBRATION_NOT_READY", str(cal.get("problems")))
    closure = _closure(root)
    live = cal.get("implementation_closure_sha256")
    if live != closure["closure_sha256"]:
        raise CLIError("CALIBRATION_STALE", "校准闭包与当前实现不符")
    allow = _allowlist(root)
    if cal.get("public_allowlist_sha") != allow.get("allowlist_sha256"):
        raise CLIError("CALIBRATION_STALE", "校准公开包与当前公开包不符")
    permit = RV.build_permit(
        round_id=_round_id(root), kind="suffix_and_review", prereg=prereg,
        closure_sha256=closure["closure_sha256"],
        worker_policy_sha=W.WorkerPolicy().sha256,
        prompt_sha=_prompt_sha(root), schema_sha=_schema_sha(),
        tokenizer_sha=str(RV.read_json(root / "contract" / "tokenizer.json")["sha256"]),
        rubric_sha=RV.canonical_sha(RV.public_rubric()),
        config_sha=RV.canonical_sha(_config_snapshot()),
        calibration_report_sha=RV.sha_file(root / "review" / "calibration_report.json"))
    RV.write_json_private(root / "contract" / "permit_suffix.json", permit)
    for lane, approved in (("suffix_A", 8), ("suffix_B", 8), ("review_final", 4)):
        _write_approval(root, f"{_round_id(root)}-{lane}",
                        task=RV.RV_LANE_TASK_ID[lane], approved=approved,
                        note=f"{lane} {approved} 槽（Plan §5）")
    print(json.dumps({"ok": True, "permit_sha256": permit["permit_sha256"],
                      "kind": permit["kind"]}, sort_keys=True))
    return 0


# ---------------------------------------------------------------------------
# 审查（校准 4 / 正式 4）
# ---------------------------------------------------------------------------

class OneShotReviewSender:
    """每槽一次 POST：ledger 行绑定 + 请求/回复字节 sha；禁止重试。"""

    def __init__(self, *, root: Path, lane: str, slot_dir: Path,
                 transport: RT.RVTransport, ledger: Any) -> None:
        self.root = root
        self.lane = lane
        self.slot_dir = slot_dir
        self.transport = transport
        self.ledger = ledger
        self.calls: "list[dict[str, Any]]" = []
        self.unknown = False

    def _append_intent(self, row: Mapping[str, Any]) -> None:
        path = self.root / "review" / "intents.jsonl"
        ensure_private_dir(path.parent)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(dict(row), ensure_ascii=False,
                                sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(path, 0o600)

    def __call__(self, system: str, user: str) -> str:
        before = {int(r["seq"]) for r in RT.ledger_call_rows(self.ledger)
                  if isinstance(r.get("seq"), int)}
        messages = ({"role": "user", "content": user},)
        serialized = json.dumps({"system": system, "messages": list(messages),
                                 "max_tokens": RV.RV_OUTPUT_TOKENS},
                                sort_keys=True, ensure_ascii=False)
        counter = RT._token_counter()
        request = T.Request(
            system=system, messages=messages,
            max_tokens=RV.RV_OUTPUT_TOKENS, serialized=serialized,
            sha256=hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
            n_bytes=len(serialized.encode("utf-8")),
            local_tokens=counter(system) + counter(user) + 8)
        intent = {"schema": "simpraxis.rsi_rv.review_intent.v1",
                  "schema_version": 1, "lane": self.lane,
                  "request_sha256": request.sha256,
                  "max_tokens": int(request.max_tokens),
                  "ts_iso": RV.utc_now()}
        RV.write_json_private(self.slot_dir / "intent.json", intent)
        self._append_intent(intent)
        try:
            reply = self.transport(request)
        except Exception as exc:
            self.unknown = True
            RV.write_json_private(self.slot_dir / "result.json", {
                "schema": "simpraxis.rsi_rv.review_result.v1",
                "schema_version": 1, "ok": False, "resolved": False,
                "unknown": True,
                "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                "ts_iso": RV.utc_now()})
            raise
        rows = [r for r in RT.ledger_call_rows(self.ledger)
                if isinstance(r.get("seq"), int) and int(r["seq"]) not in before]
        if not rows:
            raise CLIError("COST_UNKNOWN", "审查回复无账本行")
        row = rows[-1]
        if not row.get("ok"):
            raise CLIError("PROVIDER_FAILED", str(row.get("err")))
        send = {"schema": "simpraxis.rsi_rv.review_send.v1", "schema_version": 1,
                "lane": self.lane, "request_sha256": request.sha256,
                "response_id": reply.response_id,
                "response_sha256": RV.sha_bytes(reply.text.encode("utf-8")),
                "ledger_seq": int(row.get("seq")), "model": row.get("model"),
                "tokens_in": row.get("tokens_in"),
                "tokens_out": row.get("tokens_out"),
                "attempts_http": int(reply.attempts_http),
                "system_sha256": RV.sha_bytes(system.encode("utf-8")),
                "user_sha256": RV.sha_bytes(user.encode("utf-8")),
                "max_tokens": RV.RV_OUTPUT_TOKENS, "ts_iso": RV.utc_now()}
        send["resolved"] = True
        RV.write_json_private(self.slot_dir / "send.json", send)
        RV.write_json_private(self.slot_dir / "result.json", {
            "schema": "simpraxis.rsi_rv.review_result.v1", "schema_version": 1,
            "ok": True, "resolved": True, "unknown": False,
            "ledger_seq": send["ledger_seq"], "ts_iso": RV.utc_now()})
        self.calls.append(send)
        return reply.text


def _assert_no_dangling_reviews(root: Path) -> None:
    intents = root / "review" / "intents.jsonl"
    if not intents.is_file():
        return
    rows = [json.loads(line) for line in intents.read_text().splitlines()
            if line.strip()]
    # 每个 intent 的 request sha 必须能在某个槽的 result 里找到 resolved=True。
    closed = set()
    for slot_dir in (root / "review").iterdir():
        if not slot_dir.is_dir():
            continue
        result = slot_dir / "result.json"
        intent = slot_dir / "intent.json"
        if result.is_file() and intent.is_file():
            r = json.loads(result.read_text())
            i = json.loads(intent.read_text())
            if r.get("resolved") is True:
                closed.add(i.get("request_sha256"))
    dangling = sorted({r.get("request_sha256") for r in rows} - closed)
    if dangling:
        raise CLIError("STOP_UNKNOWN", f"存在未闭合审查发送：{dangling[:3]}")


def cmd_calibrate_review(args: argparse.Namespace) -> int:
    _require_paid()
    root = _round(args.root)
    _assert_no_dangling_reviews(root)
    permit = RV.verify_permit(RV.read_json(
        root / "contract" / "permit_calibration.json"), prereg=_prereg(root))
    if "review_calibration" not in permit["allowed_lanes"]:
        raise CLIError("PERMIT_LANE", "calibration permit 不含 review_calibration")
    # 校准包需要真实 study 根的观测副本；这里只做 source import（不跑 turn），
    # 与离线预演（offline_studies/）彻底分开。
    manifest = _manifest(root)
    for study in RV.STUDIES:
        RT.materialize_rv_study(root, manifest, study)
    gateway, ledger = _gateway(root)
    from orchestrator.common.runtime_paths import rsi_rv_gateway_state_path
    slot_rows: "list[dict[str, Any]]" = []
    for index, study in enumerate(RV.STUDIES):
        cal_packet = RVW.build_calibration_packet(root=root, study=study)
        packet = cal_packet["packet"]
        cases = cal_packet["cases"]
        for channel in (1, 2):
            review_id = f"cal-{study}-ch{channel}"
            slot = RVW.ReviewSlot(review_id=review_id,
                                  reviewer_id=f"rv-cal-{study}-{channel}",
                                  study=study, channel=channel,
                                  kind="calibration")
            slot_dir = ensure_private_dir(root / "review" / review_id)
            transport = RT.RVTransport(
                gateway=gateway, lane="review_calibration",
                batch_id=f"{_round_id(root)}-calibration",
                thread_id=review_id)
            sender = OneShotReviewSender(root=root, lane="review_calibration",
                                         slot_dir=slot_dir,
                                         transport=transport, ledger=ledger)
            try:
                row = RVW.execute_slot(
                    root=root, slot=slot, packet_doc=packet,
                    calibration=True, sender=sender, extra_cases=cases)
            except RVW.ReviewError as exc:
                slot_rows.append({"review_id": review_id, "study": study,
                                  "channel": channel, "kind": "calibration",
                                  "error": exc.code, "detail": exc.detail,
                                  "cases": {}})
                continue
            slot_rows.append(row)
    seen = {(str(r.get("study")), int(r.get("channel") or 0)) for r in slot_rows}
    for study in RV.STUDIES:
        for channel in (1, 2):
            if (study, channel) in seen:
                continue
            slot_rows.append({"review_id": f"cal-{study}-ch{channel}",
                              "reviewer_id": f"rv-cal-{study}-{channel}",
                              "study": study, "channel": channel,
                              "kind": "calibration", "error": "NOT_CALLED",
                              "cases": {}})
    result = RVW.check_calibration(slots=slot_rows)
    report = {"schema": f"{RV.SCHEMA_PREFIX}.calibration_report.v1",
              "schema_version": 1, "created_at_utc": RV.utc_now(),
              "ok": result["ok"], "problems": result["problems"],
              "code": result["code"], "slots": slot_rows,
              "implementation_closure_sha256": _closure(root)["closure_sha256"],
              "public_allowlist_sha": _allowlist(root)["allowlist_sha256"],
              "permit_sha256": permit["permit_sha256"]}
    RV.write_json_private(root / "review" / "calibration_report.json", report)
    print(json.dumps({"ok": report["ok"], "code": report["code"],
                      "problems": report["problems"][:8]},
                     ensure_ascii=False, sort_keys=True))
    return 0 if report["ok"] else 1


def cmd_review(args: argparse.Namespace) -> int:
    _require_paid()
    root = _round(args.root)
    study = str(args.study)
    if study not in RV.STUDIES:
        raise CLIError("STUDY_UNKNOWN", study)
    _assert_no_dangling_reviews(root)
    permit = RV.verify_permit(RV.read_json(
        root / "contract" / "permit_suffix.json"), prereg=_prereg(root))
    if "review_final" not in permit["allowed_lanes"]:
        raise CLIError("PERMIT_LANE", "review_final")
    result_path = root / "studies" / study / "result.json"
    state = (RV.read_json(result_path).get("state")
             if result_path.is_file() else "NOT_RUN")
    if state != "MODEL_REPORT_VALID":
        doc = {"schema": f"{RV.SCHEMA_PREFIX}.final_review.v1",
               "schema_version": 1, "created_at_utc": RV.utc_now(),
               "study": study, "verdict": "REVIEW_NOT_RUN",
               "items": {}, "problems": [f"study_state={state}"], "slots": [],
               "permit_sha256": None}
        RV.write_json_private(root / "review" / f"final_{study}.json", doc)
        print(json.dumps({"study": study, "verdict": "REVIEW_NOT_RUN",
                          "reason": state}, ensure_ascii=False, sort_keys=True))
        return 0
    report_path = root / "submit" / study / "report.json"
    if not report_path.is_file():
        raise CLIError("REPORT_MISSING", str(report_path))
    payload = RV.read_json(report_path)
    report = payload.get("report")
    lineage = payload.get("lineage") or {}
    try:
        packet = RVW.build_rv_packet(
            root=root, study=study, report=report,
            report_text=str(payload.get("report_text") or ""),
            model_lineage=lineage)
    except (RVW.ReviewError, RV.RVError) as exc:
        # 审查包事实/引用/容量不守恒 ⇒ 仪器阻断，不把包错误记成候选科学 FAIL。
        doc = {"schema": f"{RV.SCHEMA_PREFIX}.final_review.v1",
               "schema_version": 1, "created_at_utc": RV.utc_now(),
               "study": study, "verdict": "INSTRUMENT_BLOCKED",
               "items": {}, "slots": [],
               "problems": [f"{getattr(exc, 'code', type(exc).__name__)}: "
                            f"{getattr(exc, 'detail', '')}"],
               "permit_sha256": permit.get("permit_sha256")}
        RV.write_json_private(root / "review" / f"final_{study}.json", doc)
        print(json.dumps({"study": study, "verdict": "INSTRUMENT_BLOCKED",
                          "problems": doc["problems"]},
                         ensure_ascii=False, sort_keys=True))
        return 1
    gateway, ledger = _gateway(root)
    slots: "list[dict[str, Any]]" = []
    for channel in (1, 2):
        review_id = f"final-{study}-ch{channel}"
        slot = RVW.ReviewSlot(review_id=review_id,
                              reviewer_id=f"rv-final-{study}-{channel}",
                              study=study, channel=channel, kind="final")
        slot_dir = ensure_private_dir(root / "review" / review_id)
        transport = RT.RVTransport(
            gateway=gateway, lane="review_final",
            batch_id=f"{_round_id(root)}-review_final",
            thread_id=review_id)
        sender = OneShotReviewSender(root=root, lane="review_final",
                                     slot_dir=slot_dir,
                                     transport=transport, ledger=ledger)
        try:
            row = RVW.execute_slot(root=root, slot=slot, packet_doc=packet,
                                   calibration=False, sender=sender)
        except RVW.ReviewError as exc:
            slots.append({"review_id": review_id, "reviewer_id": slot.reviewer_id,
                          "study": study, "kind": "final", "channel": channel,
                          "error": exc.code, "detail": exc.detail, "items": {}})
            continue
        slots.append(row)
    seen = {int(r.get("channel") or 0) for r in slots}
    for channel in (1, 2):
        if channel not in seen:
            slots.append({"review_id": f"final-{study}-ch{channel}",
                          "reviewer_id": f"rv-final-{study}-{channel}",
                          "study": study, "channel": channel,
                          "kind": "final", "error": "NOT_CALLED",
                          "items": {}})
    merged = RVW.merge_study_reviews(slots=slots)
    doc = {"schema": f"{RV.SCHEMA_PREFIX}.final_review.v1", "schema_version": 1,
           "created_at_utc": RV.utc_now(), "study": study,
           "verdict": merged["verdict"], "items": merged["items"],
           "problems": merged.get("problems") or [], "slots": slots,
           "permit_sha256": permit["permit_sha256"]}
    RV.write_json_private(root / "review" / f"final_{study}.json", doc)
    print(json.dumps({"study": study, "verdict": doc["verdict"],
                      "problems": doc["problems"][:6]},
                     ensure_ascii=False, sort_keys=True))
    return 0 if doc["verdict"] in ("REVIEW_PASS", "REVIEW_FAIL",
                                   "REVIEW_INCONCLUSIVE") else 1


# ---------------------------------------------------------------------------
# suffix（A/B 真实模型末段）
# ---------------------------------------------------------------------------

def cmd_suffix(args: argparse.Namespace) -> int:
    _require_paid()
    root = _round(args.root)
    study = str(args.study)
    if study not in RV.STUDIES:
        raise CLIError("STUDY_UNKNOWN", study)
    manifest = _manifest(root)
    prereg = _prereg(root)
    allow = _allowlist(root)
    RV.verify_source_manifest(manifest)  # 发网前逐件重算；失败即阻断
    permit = RV.verify_permit(RV.read_json(
        root / "contract" / "permit_suffix.json"), prereg=prereg)
    lane = f"suffix_{study}"
    if lane not in permit["allowed_lanes"]:
        raise CLIError("PERMIT_LANE", lane)
    closure = _closure(root)
    if closure["closure_sha256"] != permit["implementation_closure_sha256"]:
        raise CLIError("CLOSURE_DRIFT", "suffix 许可闭包与实现不符")
    if allow["allowlist_sha256"] != permit["public_allowlist_sha"]:
        raise CLIError("PUBLIC_DRIFT", "suffix 许可公开包不符")
    material = RT.materialize_rv_study(root, manifest, study)
    budget = RT.RVBudget(envelope_id=_round_id(root),
                         posts_by_lane=dict(RV.RV_LANE_POST_CAPS))
    lanes = RT.RVSlotBook(RT.RVContext(root), budget)
    gateway, ledger = _gateway(root)
    transport = RT.RVTransport(gateway=gateway, lane=lane,
                               batch_id=f"{_round_id(root)}-{lane}",
                               thread_id=f"rv-{study}")
    admission = RT.RVAdmission(
        ctx=RT.RVContext(root), round_id=_round_id(root), study=study,
        study_ctx=RT.RVContext(material["study_root"]), prereg=prereg,
        budget=budget, ledger=ledger, lanes=lanes,
        permits=_permits(root),
        closure_reader=_closure_reader(root),
        public_verifier=_public_verifier(root))
    admission.enter_lane(lane)
    sender = RT.RVSender(ctx=RT.RVContext(root), transport=transport,
                         admission=admission, ledger=ledger, lane=lane,
                         study=study, round_id=_round_id(root))
    sender.assert_no_dangling()
    parts = RT.build_rv_parts(
        root=root, study=study, manifest=manifest, material=material,
        prereg=prereg, budget=budget, lanes=lanes, ledger=ledger,
        permits=_permits(root), closure_reader=_closure_reader(root),
        public_verifier=_public_verifier(root), sender=sender, lane=lane,
        policy=W.WorkerPolicy(), scripted=False)
    outcome = RT.run_turns(parts, max_turns=RV.MAX_PATH_TURNS)
    faces = _five_faces(parts.study_ctx, study)
    judge = _judge_report(parts.study_ctx, study, material["plan"])
    lineage = sender.lineage()
    claim = _claim_event(parts.state, study)
    gates = _gates(study=study, outcome=outcome, faces=faces, judge=judge,
                   lineage=lineage, claim=claim, state=parts.state,
                   parts=parts, source_valid=True)
    security = outcome["stopped"] in (
        "ACTION_FORBIDDEN", "HIDDEN_RETRY", "LOCK_MISMATCH", "PERMIT_DRIFT",
        "PERMIT_MISSING", "CLOSURE_DRIFT", "PUBLIC_DRIFT", "PREREG_DRIFT",
        "SOURCE_NOT_READY")
    unknown = outcome["stopped"] in ("STOP_UNKNOWN", "COST_UNKNOWN")
    judge_ok = str((judge or {}).get("verdict") or "") == "JUDGE_OK"
    # 只认机器判据 JUDGE_OK + 全部工程门；任何一项不成立都不是“有效报告”。
    valid_report = judge_ok and bool(claim) and all(
        bool(gates.get(k)) for k in (
            "source_valid", "actual_worker_isolated", "request_fidelity",
            "candidate_origin", "binding", "report_evidence",
            "numeric_fidelity"))
    if security:
        state = "INSTRUMENT_BLOCKED"
    elif unknown:
        state = "PROVENANCE_UNKNOWN"
    elif valid_report:
        state = "MODEL_REPORT_VALID"
    elif outcome["stopped"] in ("MAX_TURNS", "BUDGET_REFUSED"):
        state = "BUDGET_EXHAUSTED"
    else:
        state = "MODEL_FAILED"
    result = {"schema": f"{RV.SCHEMA_PREFIX}.study_result.v1",
              "schema_version": 1, "study": study, "created_at_utc": RV.utc_now(),
              "state": state, "gates": gates, "turns": outcome,
              "security_violation": security,
              "send_unknown": unknown,
              "five_faces": faces, "judge": judge,
              "lineage": {"n_calls": len(lineage["results"]),
                          "results": lineage["results"]},
              "problems": sorted(k for k, v in gates.items() if not v)}
    RV.write_json_private(root / "studies" / study / "result.json", result)
    # 封存报告（含报告对象/正文/渲染/lineage；供正式双审）
    if claim is not None:
        action_ev = _action_event(parts.state, str(claim.payload.get("response_id")))
        action_doc = dict((action_ev.payload.get("payload") if action_ev else {}) or {})
        report = action_doc.get("research_report")
        submit_dir = ensure_private_dir(root / "submit" / study)
        RV.write_json_private(submit_dir / "report.json", {
            "schema": f"{RV.SCHEMA_PREFIX}.sealed_report.v1",
            "schema_version": 1, "study": study,
            "report": report,
            "report_text": str(action_doc.get("report_text") or ""),
            "claimed": True,
            "lineage": {"producer": RT.RV_PRODUCER,
                        "lane": lane,
                        "analysis_receipt_sha": manifest["studies"][study]["receipt_sha"],
                        "calls": lineage["results"]},
            "five_faces": faces,
        })
    print(json.dumps({"study": study, "state": state,
                      "stopped": outcome["stopped"],
                      "gates": gates, "n_posts": len(lineage["results"])},
                     ensure_ascii=False, sort_keys=True))
    return 0 if state == "MODEL_REPORT_VALID" else 1


def _claim_event(state: "S.ResearchState", study: str) -> "Any | None":
    for event in reversed(state.journal.events()):
        if event.event == "claim" and event.payload.get("study") == study:
            return SimpleClaim(event)
    return None


class SimpleClaim:
    def __init__(self, event: Any) -> None:
        self.event = event
        self.payload = dict(event.payload)


def _action_event(state: "S.ResearchState", response_id: str) -> "Any | None":
    for event in state.journal.events():
        if event.event == "action" and event.payload.get("response_id") == response_id:
            return event
    return None


def _gates(*, study: str, outcome: Mapping[str, Any],
           faces: Mapping[str, Any], judge: "Mapping[str, Any] | None",
           lineage: Mapping[str, Any], claim: Any,
           state: "S.ResearchState", parts: Any,
           source_valid: bool) -> dict[str, bool]:
    layers = (judge or {}).get("layers") or {}
    results = [r for r in (lineage.get("results") or [])
               if r.get("resolved") is True]
    intents = list(lineage.get("intents") or [])
    response_ids = {r.get("response_id") for r in results}
    # 实际 worker：每次 read 都有真实容器实例证据（container_id + host_pid）。
    reader = getattr(getattr(parts, "reader", None), "reader", None)
    records = list(getattr(reader, "records", []) or [])
    reader_ok = bool(records) and all(
        r.get("container_id") and int(r.get("host_pid") or 0) > 0
        for r in records)
    request_ok = (bool(results) and len(intents) == len(results) and all(
        int(r.get("attempts_http") or 0) == 1
        and int(r.get("max_tokens") or 0) == RV.RV_OUTPUT_TOKENS
        for r in results))
    candidate = False
    if claim is not None:
        payload = claim.payload
        rid = str(payload.get("response_id"))
        has_action = any(
            ev.event == "action" and ev.payload.get("response_id") == rid
            and ev.payload.get("name") == "submit_claim"
            for ev in state.journal.events())
        candidate = (str(payload.get("producer")) == RT.RV_PRODUCER
                     and has_action and rid in response_ids)
    binding = bool(layers.get("receipt_valid")
                   and layers.get("numerics_valid")
                   and layers.get("evidence_valid")
                   and layers.get("diagnostics_valid")
                   and layers.get("claim_status") == "MATCH")
    return {
        "source_valid": bool(source_valid),
        "actual_worker_isolated": reader_ok,
        "request_fidelity": request_ok,
        "candidate_origin": bool(candidate),
        "binding": binding,
        "report_evidence": bool(layers.get("report_valid")),
        "review_verdict": False,  # 由 review 阶段回填
        "numeric_fidelity": bool(layers.get("numerics_valid")),
    }


# ---------------------------------------------------------------------------
# closeout
# ---------------------------------------------------------------------------

def cmd_closeout(args: argparse.Namespace) -> int:
    root = _round(args.root)
    manifest = _manifest(root)
    studies: "dict[str, Any]" = {}
    problems: "list[str]" = []
    for study in RV.STUDIES:
        result_path = root / "studies" / study / "result.json"
        row = RV.read_json(result_path) if result_path.is_file() else {
            "study": study, "state": "NOT_RUN",
            "gates": {k: False for k in (
                "source_valid", "actual_worker_isolated", "request_fidelity",
                "candidate_origin", "binding", "report_evidence",
                "review_verdict", "numeric_fidelity")},
            "problems": ["NOT_RUN"]}
        review_path = root / "review" / f"final_{study}.json"
        if review_path.is_file():
            review = RV.read_json(review_path)
            row = dict(row)
            row["gates"] = dict(row.get("gates") or {})
            row["gates"]["review_verdict"] = (
                review.get("verdict") == "REVIEW_PASS")
            row["review_verdict"] = review.get("verdict")
            if review.get("verdict") == "INSTRUMENT_BLOCKED":
                row["state"] = "INSTRUMENT_BLOCKED"
                row["instrument_blocked"] = True
        else:
            row = dict(row)
            row["review_verdict"] = "REVIEW_NOT_RUN"
        studies[study] = row
    iso_path = root / "offline" / "report.json"
    iso_ok = bool((RV.read_json(iso_path).get("isolation") or {}).get("ok")) \
        if iso_path.is_file() else False
    for study, row in studies.items():
        row["gates"]["actual_worker_isolated"] = iso_ok
    verdict = RV.aggregate_rv(studies=studies)
    calibration: "dict[str, Any] | None" = None
    cal_path = root / "review" / "calibration_report.json"
    if cal_path.is_file():
        cal = RV.read_json(cal_path)
        calibration = {"ok": cal.get("ok"), "code": cal.get("code"),
                       "problems": list(cal.get("problems") or []),
                       "slots": [(s.get("review_id"), s.get("error"))
                                 for s in cal.get("slots") or []]}
    ledger = _ledger(root)
    calls = RT.ledger_call_rows(ledger)
    closeout = {"schema": RV.CLOSEOUT_SCHEMA, "schema_version": 1,
                "round_id": _round_id(root), "created_at_utc": RV.utc_now(),
                "overall": verdict["overall"], "reasons": verdict["reasons"],
                "studies": studies,
                "slots": {lane: {"cap": RV.RV_LANE_POST_CAPS[lane],
                                 "used": RT.posts_used(ledger, lane)}
                          for lane in RV.RV_LANE_ORDER},
                "ledger": {"n_calls": len(calls),
                           "rows": [{k: r.get(k) for k in
                                     ("seq", "task", "batch_id", "ok", "model",
                                      "tokens_in", "tokens_out")}
                                    for r in calls]},
                "source": {study: {"campaign": manifest["studies"][study][
                    "source_campaign_id"], "run": manifest["studies"][study][
                    "source_run_id"],
                    "required": manifest["studies"][study]["required_contrast_ids"]}
                    for study in RV.STUDIES},
                "calibration": calibration,
                "notes": [
                    "仅两个开发案例，不估计总体研究成功率。",
                    "不修改任何旧 r1 轴；A/B 条件与结果已被开发侧看到，非新留出。",
                    "suffix 通过只表示 MODEL_SUFFIX_FEASIBLE_ON_GIVEN_EVIDENCE。",
                ]}
    RV.write_json_private(root / "closeout" / "rv_closeout.json", closeout)
    print(json.dumps({"overall": closeout["overall"],
                      "reasons": closeout["reasons"],
                      "slots": closeout["slots"]},
                     ensure_ascii=False, sort_keys=True))
    # 合法终态（含 NOT_MEASURED）返回 0；仪器/来源/未知是阻断，返回 1。
    return 0 if closeout["overall"] in (
        "MODEL_SUFFIX_FEASIBLE_ON_GIVEN_EVIDENCE",
        "MODEL_SUFFIX_NOT_ESTABLISHED", "NOT_MEASURED") else 1


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rv_cli",
                                     description="RSI-R1-RV（真实模型末段验证）")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("init")
    p.add_argument("--root", required=True)
    p.add_argument("--source-map", required=True)
    p.set_defaults(func=cmd_init)
    p = sub.add_parser("preflight")
    p.add_argument("--root", required=True)
    p.set_defaults(func=cmd_preflight)
    p = sub.add_parser("validate-offline")
    p.add_argument("--root", required=True)
    p.set_defaults(func=cmd_validate_offline)
    p = sub.add_parser("authorize-calibration")
    p.add_argument("--root", required=True)
    p.add_argument("--consent", default="")
    p.set_defaults(func=cmd_authorize_calibration)
    p = sub.add_parser("calibrate-review")
    p.add_argument("--root", required=True)
    p.set_defaults(func=cmd_calibrate_review)
    p = sub.add_parser("qualify")
    p.add_argument("--root", required=True)
    p.set_defaults(func=cmd_qualify)
    p = sub.add_parser("suffix")
    p.add_argument("--root", required=True)
    p.add_argument("--study", required=True, choices=list(RV.STUDIES))
    p.set_defaults(func=cmd_suffix)
    p = sub.add_parser("review")
    p.add_argument("--root", required=True)
    p.add_argument("--study", required=True, choices=list(RV.STUDIES))
    p.set_defaults(func=cmd_review)
    p = sub.add_parser("closeout")
    p.add_argument("--root", required=True)
    p.set_defaults(func=cmd_closeout)
    return parser


def main(argv: "Sequence[str] | None" = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except RV.RVError as exc:
        print(json.dumps({"ok": False, "code": exc.code, "detail": exc.detail},
                         ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
