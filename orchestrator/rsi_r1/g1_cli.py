"""RSI-G1 薄入口：A 层零 POST 取证/审计/编译/决策（Plan §3/§3.1）。

只暴露 A 层子命令；**没有** study / review / sender 子命令，也不构造
CampaignContext、不建 family 成员、不写旧根。B 层（24 POST）走
``g1_probe.py`` 的独立入口与单独的批准件。

用法（统一 `source scripts/00_env.sh` 后用项目 venv）：

    python -m orchestrator.rsi_r1.g1_cli inventory --diagnostic-id <id> \
        --out <g1root> --sources <allowlist.json>
    python -m orchestrator.rsi_r1.g1_cli trace --diagnostic-id <id> \
        --out <g1root> --source <campaign根> --study A|B
    python -m orchestrator.rsi_r1.g1_cli packet-audit ... --source <根> --study ...
    python -m orchestrator.rsi_r1.g1_cli delivery-test --diagnostic-id <id> \
        --out <g1root> --cases <cases.json>
    python -m orchestrator.rsi_r1.g1_cli decision --diagnostic-id <id> --out <g1root>
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping

from orchestrator.common import runtime_paths as RP
from orchestrator.common.fsutil import atomic_write_text, ensure_private_dir
from orchestrator.rsi_r1 import g1_decision as GD
from orchestrator.rsi_r1 import g1_delivery as DV
from orchestrator.rsi_r1 import g1_packet_audit as PA
from orchestrator.rsi_r1 import g1_probe as GP
from orchestrator.rsi_r1 import g1_trace as GT


class G1CliError(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"[{code}] {detail}" if detail else code)


def _root(args: Any) -> Path:
    out = Path(args.out) if args.out else RP.rsi_g1_diagnostic_root(args.diagnostic_id)
    return out


def _write(out: Path, name: str, doc: Any) -> str:
    ensure_private_dir(out)
    text = json.dumps(doc, ensure_ascii=False, sort_keys=True, indent=1) + "\n"
    atomic_write_text(out / name, text, mode=0o600)
    return GT.sha_bytes(text.encode("utf-8"))


# ---------------------------------------------------------------------------
# A 层子命令
# ---------------------------------------------------------------------------

def cmd_inventory(args: Any) -> int:
    out = _root(args)
    spec = json.loads(Path(args.sources).read_text(encoding="utf-8"))
    roots = [GT.RootSpec(root_id=str(r["root_id"]), path=Path(r["path"]),
                         max_files=int(r.get("max_files", GT.DEFAULT_MAX_FILES)),
                         max_total_bytes=int(r.get("max_total_bytes",
                                                   GT.MAX_INPUT_BYTES)))
             for r in spec.get("roots") or []]
    files = [GT.FileSpec(root_id=str(f["root_id"]), relpath=str(f["relpath"]),
                         role=str(f.get("role") or "unspecified"),
                         sensitivity=str(f.get("sensitivity") or "private"))
             for f in spec.get("files") or []]
    manifest = GT.inventory_evidence(roots, files, out_root=out,
                                     diagnostic_id=str(args.diagnostic_id),
                                     source_commit=str(args.source_commit or ""))
    print(json.dumps({"n_files": manifest["n_files"],
                      "total_bytes": manifest["total_bytes"],
                      "missing": sum(1 for f in manifest["files"]
                                     if f["state"] == "missing")},
                     ensure_ascii=False))
    return 0


def cmd_trace(args: Any) -> int:
    out = _root(args)
    result = GT.trace_study(source_root=Path(args.source), study=str(args.study),
                            diagnostic_id=str(args.diagnostic_id), out_root=out,
                            source_commit=str(args.source_commit or ""))
    verify = GT.verify_trace(out / "trace" / str(args.study))
    print(json.dumps({"study": args.study,
                      "first_divergence": result["summary"]["first_divergence"],
                      "suspected": result["summary"]["suspected"],
                      "gaps": result["summary"]["gaps"],
                      "trace_verified": verify["ok"]}, ensure_ascii=False))
    return 0 if verify["ok"] else 2


def cmd_packet_audit(args: Any) -> int:
    out = _root(args)
    source = Path(args.source)
    study = str(args.study)
    inputs = PA.load_study_inputs(source, study)
    historical = PA.validate_packet(
        inputs["task"], inputs["assignments"], inputs["raw_metrics"],
        inputs["packet"], run_specs=inputs["run_specs"], expect_blocks=32)
    doc_hist = PA.audit_to_disk(historical, out / "packet_sanity",
                                name=f"historical_{study}")
    # 修复旁件：只作 sanity，不写回旧根、不触发旧 closeout（G1-09）
    from orchestrator.rsi_r1 import r13_review_run as RR
    from orchestrator.rsi_r1.runtime import CampaignContext
    from orchestrator.common import runtime_paths as _RP
    ctx = CampaignContext(envelope_id=source.name, root=source,
                          protected_roots=())
    fixed, _, _ = RR.build_formal_packet(ctx, study, inputs["task"])
    repaired = PA.validate_packet(
        inputs["task"], inputs["assignments"], inputs["raw_metrics"], fixed,
        run_specs=inputs["run_specs"], expect_blocks=32)
    doc_fix = PA.audit_to_disk(repaired, out / "packet_sanity",
                               name=f"repaired_{study}")
    print(json.dumps({"study": study, "historical": doc_hist["codes"],
                      "repaired": doc_fix["codes"],
                      "historical_sha": doc_hist["report_sha256"],
                      "repaired_sha": doc_fix["report_sha256"]},
                     ensure_ascii=False))
    return 0


def cmd_delivery_test(args: Any) -> int:
    out = _root(args)
    cases = json.loads(Path(args.cases).read_text(encoding="utf-8"))
    results = [DV.run_case(case) for case in cases.get("cases") or []]
    summary = {"n": len(results),
               "passed": sum(1 for r in results if r["ok"]),
               "failed": [r["case_id"] for r in results if not r["ok"]],
               "rule": "只证明编译/校验器保真与拒绝性，不产生模型比较结论"}
    ensure_private_dir(out / "compiler_cases")
    sha = _write(out / "compiler_cases", "results.json",
                 {"summary": summary, "results": results})
    print(json.dumps({"n": summary["n"], "passed": summary["passed"],
                      "failed": summary["failed"], "sha256": sha},
                     ensure_ascii=False))
    return 0 if not summary["failed"] else 2


def _load(out: Path, pattern: str) -> "list[dict[str, Any]]":
    docs: "list[dict[str, Any]]" = []
    for path in sorted(out.glob(pattern)):
        docs.append(json.loads(path.read_text(encoding="utf-8")))
    return docs


def cmd_decision(args: Any) -> int:
    out = _root(args)
    traces = [json.loads(p.read_text(encoding="utf-8"))
              for p in sorted((out / "trace").glob("*_summary.json"))]
    audits = _load(out, "packet_sanity/*.json")
    cases_doc = out / "compiler_cases" / "results.json"
    cases = (json.loads(cases_doc.read_text(encoding="utf-8")).get("results")
             if cases_doc.is_file() else [])
    probe_doc = None
    probe_path = (Path(args.probe_report) if getattr(args, "probe_report", None)
                  else out / "probe" / "probe_report.json")
    if probe_path.is_file():
        probe_doc = json.loads(probe_path.read_text(encoding="utf-8"))
        probe_doc = {"overall": probe_doc.get("overall"),
                     "delivery_axis": (probe_doc.get("axes") or {}).get("delivery"),
                     "semantic_axis": (probe_doc.get("axes") or {}).get("semantic")}
    doc = GD.aggregate_diagnostics(
        diagnostic_id=str(args.diagnostic_id), traces=traces,
        packet_audits=audits, compiler_cases=cases, probe=probe_doc,
        notes=[str(n) for n in (args.note or [])])
    ensure_private_dir(out / "decision")
    sha = _write(out / "decision", "decision.json", doc)
    print(json.dumps({"axes": doc["axes"], "sha256": sha}, ensure_ascii=False))
    return 0


def cmd_probe_preregister(args: Any) -> int:
    """冻结 24 槽清单（零 POST）；B 层运行前必须先有此件。"""
    from orchestrator.rsi_r1 import g1_probe as GP
    import yaml
    out = _root(args)
    stage_models = Path(args.stage_models)
    conf = yaml.safe_load(stage_models.read_text(encoding="utf-8"))
    model_id = str(((conf.get("stage_models") or {}).get(GP.STAGE) or {})
                   .get("model_id") or "")
    if not model_id:
        raise G1CliError("MODEL_UNRESOLVED", GP.STAGE)
    ledger = out / "quota" / "ledger.jsonl"
    spec = GP.build_prereg(diagnostic_id=str(args.diagnostic_id),
                           ledger_path=ledger,
                           stage_models_path=stage_models,
                           model_id=model_id,
                           input_manifest_sha=str(args.input_manifest_sha))
    sha = GP.write_prereg(out, spec)
    print(json.dumps({"slots": len(spec["slots"]), "prereg_sha256": sha,
                      "model_id": model_id}, ensure_ascii=False))
    return 0


def cmd_probe(args: Any) -> int:
    """B 层实跑：批准件 + SIMPRAXIS_ALLOW_PAID=1 双闸；24 POST 硬帽。"""
    from orchestrator.rsi_r1 import g1_probe as GP
    out = _root(args)
    approval = Path(args.approval)
    GP.require_probe_ready(out, approval)
    spec = GP.load_prereg(out)
    if str(spec.get("diagnostic_id")) != str(args.diagnostic_id):
        raise G1CliError("PREREG_ID", str(spec.get("diagnostic_id")))
    if str(spec.get("stage_config_sha") or "") != GP.sha(
            Path(args.stage_models).read_bytes()):
        raise G1CliError("CONFIG_DRIFT", "stage_models.yaml 与冻结件不符")
    sender = GP.build_sender(root=out, diagnostic_id=str(args.diagnostic_id))
    report = GP.run_probe(root=out, prereg=spec, sender=sender,
                          approval_path=approval,
                          ledger_path=out / "quota" / "ledger.jsonl")
    print(json.dumps({"overall": report["overall"], "posts": report["posts"],
                      "axes": report["axes"],
                      "sha256": report["report_sha256"]}, ensure_ascii=False))
    return 0 if report["overall"] == "DIAGNOSTIC_COMPLETE" else 2


def cmd_verify(args: Any) -> int:
    out = _root(args)
    reports = {}
    for study in ("A", "B"):
        d = out / "trace" / study
        if d.is_dir():
            reports[study] = GT.verify_trace(d)
    ok = all(r["ok"] for r in reports.values()) if reports else False
    print(json.dumps({"ok": ok, "reports": reports}, ensure_ascii=False))
    return 0 if ok else 2


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rsi-g1", description="RSI-G1 A 层：零模型 POST 的证据辨因闸")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--campaign", default="rsi-g1")
        p.add_argument("--diagnostic-id", required=True)
        p.add_argument("--out", default=None)
        p.add_argument("--source-commit", default="")

    pi = sub.add_parser("inventory", help="按 allowlist 登记输入（零 POST）")
    common(pi)
    pi.add_argument("--sources", required=True)
    pi.set_defaults(func=cmd_inventory)

    pt = sub.add_parser("trace", help="回放一个 study 的跳点（零 POST）")
    common(pt)
    pt.add_argument("--source", required=True)
    pt.add_argument("--study", required=True, choices=["A", "B"])
    pt.set_defaults(func=cmd_trace)

    pp = sub.add_parser("packet-audit", help="审查包语义守恒审计（零 POST）")
    common(pp)
    pp.add_argument("--source", required=True)
    pp.add_argument("--study", required=True, choices=["A", "B"])
    pp.set_defaults(func=cmd_packet_audit)

    pd = sub.add_parser("delivery-test", help="受控交付案例（零 POST）")
    common(pd)
    pd.add_argument("--cases", required=True)
    pd.set_defaults(func=cmd_delivery_test)

    pdec = sub.add_parser("decision", help="G4 多轴决策（零 POST）")
    common(pdec)
    pdec.add_argument("--note", action="append", default=None)
    pdec.add_argument("--probe-report", default=None,
                      help="可显式指定 B 层报告（默认 <root>/probe/probe_report.json）")
    pdec.set_defaults(func=cmd_decision)

    ppr = sub.add_parser("probe-preregister", help="冻结 B 层 24 槽（零 POST）")
    common(ppr)
    ppr.add_argument("--stage-models", required=True)
    ppr.add_argument("--input-manifest-sha", required=True)
    ppr.set_defaults(func=cmd_probe_preregister)

    pb = sub.add_parser("probe", help="B 层配对诊断（24 POST；需批准件）")
    common(pb)
    pb.add_argument("--approval", required=True)
    pb.add_argument("--stage-models", required=True)
    pb.set_defaults(func=cmd_probe)

    pv = sub.add_parser("verify", help="复核 trace 完整性（零 POST）")
    common(pv)
    pv.set_defaults(func=cmd_verify)
    return parser


def main(argv: "list[str] | None" = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "campaign", "rsi-g1") != "rsi-g1":
        raise G1CliError("CAMPAIGN", "G1 只有 rsi-g1 一个 campaign")
    try:
        return int(args.func(args))
    except (G1CliError, GT.G1TraceError, DV.DeliveryError, GP.ProbeError,
            RP.RsiR13CampaignUnsetError, OSError, ValueError,
            FileNotFoundError) as exc:
        print(f"BLOCKED {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
