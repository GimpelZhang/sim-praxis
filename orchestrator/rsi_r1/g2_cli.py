"""RSI-G2 离线薄入口（**零模型 POST / 零仿真**；Plan §3.3）。

子命令：``init`` / ``contract-check`` / ``validate-visibility`` /
``validate-scope`` / ``validate-isolation`` / ``closeout``。

纪律：

* 没有 study / review / sender 子命令；不构造 CampaignContext、不登记 campaign
  或账本、不写旧根（旧 R1/R1.2/R1.3/G1 根一律只读）；
* 所有诊断在 ``rsi_g2/<id>/`` 内落盘（0700/0600），输入 allowlist ≤2 GiB，
  输出 ≤100 MiB，墙钟 ≤30 min，峰值 RSS（VmHWM）≤2 GiB；
* ``validate-*`` 期间 socket 连接被替换成「调用即失败」，任何真实网关/HTTP
  立即 ``PAID_GATE``；scripted sender 必须显式 ``OFFLINE=True``。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping, Sequence

from orchestrator.common import research_transport as T
from orchestrator.common import runtime_paths as RP
from orchestrator.common.clock import utc_now_iso_z
from orchestrator.common.fsutil import atomic_write_text, ensure_private_dir
from orchestrator.rsi_r1 import g1_trace as GT
from orchestrator.rsi_r1 import g2_contract as G2
from orchestrator.rsi_r1 import g2_validation as GV
from orchestrator.rsi_r1 import loop as L

SCHEMA_SHAPE_REPORT = "simpraxis.rsi_g2.visibility_report.v1"
SCHEMA_ISOLATION = "simpraxis.rsi_g2.isolation_report.v1"
SCHEMA_CLOSEOUT = "simpraxis.rsi_g2.closeout.v1"

MAX_WALLCLOCK_S = 30 * 60
MAX_RSS_BYTES = 2 * 1024 ** 3
MAX_OUT_BYTES = 100 * 1024 ** 2
MAX_INPUT_BYTES = 2 * 1024 ** 3

TOKENIZER_ID = "local_len_div4_v1"


class G2CliError(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"[{code}] {detail}" if detail else code)


def _counter(text: str) -> int:
    """无分词器依赖的本地上下文计数（len//4）；身份写入合同快照。"""
    return max(1, len(text) // 4)


def _tokenizer_sha() -> str:
    return G2.sha_bytes(b"len(text)//4; local context control only")


def _root(args: argparse.Namespace) -> Path:
    if getattr(args, "root", None):
        return Path(args.root)
    if getattr(args, "diagnostic_id", None):
        return RP.rsi_g2_diagnostic_root(args.diagnostic_id)
    raise G2CliError("ROOT_MISSING", "必须给 --root 或 --diagnostic-id")


def _write_json(path: Path, doc: Mapping[str, Any]) -> str:
    ensure_private_dir(Path(path).parent)
    text = json.dumps(doc, ensure_ascii=False, sort_keys=True, indent=1) + "\n"
    atomic_write_text(Path(path), text, mode=0o600)
    return G2.sha_bytes(text.encode("utf-8"))


def _read_json(path: Path) -> Any:
    return G2.parse_json_strict(Path(path).read_bytes())


# ---------------------------------------------------------------------------
# 资源与离线守卫（可机判的纯函数 + CLI 上下文）
# ---------------------------------------------------------------------------

def limit_problems(*, wall_s: float, peak_rss: int, out_bytes: int,
                   in_bytes: int = 0) -> "list[str]":
    problems: "list[str]" = []
    if wall_s > MAX_WALLCLOCK_S:
        problems.append(f"wall={wall_s:.0f}s")
    if peak_rss > MAX_RSS_BYTES:
        problems.append(f"peak_rss={peak_rss}")
    if out_bytes > MAX_OUT_BYTES:
        problems.append(f"out={out_bytes}")
    if in_bytes > MAX_INPUT_BYTES:
        problems.append(f"in={in_bytes}")
    return problems


class _ResourceGuard:
    """独立进程**绝对峰值** RSS（VmHWM）守卫（Plan §3.3；不复用 G1 增量）。"""

    def __init__(self, out: Path, *, inputs: "Sequence[Path]" = ()) -> None:
        self.out = Path(out)
        self.inputs = list(inputs)

    def __enter__(self) -> "_ResourceGuard":
        self._t0 = time.monotonic()
        # Plan §3.3：pytest 宿主本身不计成诊断进程。嵌入测试时峰值 RSS 是**整
        # 个测试进程**的历史峰值（G1-D5 实测可达 4 GB），与本命令无关；独立
        # CLI 运行时才按绝对峰值判。墙钟与输出字节始终按本命令判。
        self.embedded = "PYTEST_CURRENT_TEST" in os.environ
        self.last_problems: "list[str]" = []
        return self

    @staticmethod
    def peak_rss() -> int:
        try:
            for line in Path("/proc/self/status").read_text().splitlines():
                if line.startswith("VmHWM:"):
                    return int(line.split()[1]) * 1024
        except OSError:
            pass
        import resource
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024

    def _out_bytes(self) -> int:
        if not self.out.exists():
            return 0
        return sum(p.stat().st_size for p in self.out.rglob("*") if p.is_file())

    def _in_bytes(self) -> int:
        total = 0
        for path in self.inputs:
            if path.is_file():
                total += path.stat().st_size
            elif path.is_dir():
                total += sum(p.stat().st_size for p in path.rglob("*")
                             if p.is_file())
        return total

    def __exit__(self, exc_type, exc, tb) -> bool:
        problems = limit_problems(wall_s=time.monotonic() - self._t0,
                                  peak_rss=self.peak_rss(),
                                  out_bytes=self._out_bytes(),
                                  in_bytes=self._in_bytes())
        self.last_problems = problems
        if self.embedded:
            # 宿主峰值不归本命令：只保留墙钟/输出类判定。
            problems = [p for p in problems if not p.startswith("peak_rss=")]
        if problems and exc_type is None:
            raise G2CliError("RESOURCE_LIMITED", ",".join(problems))
        return False


class _OfflineGuard:
    """把 socket 连接替换成「调用即失败」；G2 不允许任何真实网络。"""

    def __enter__(self) -> "_OfflineGuard":
        import socket
        self._socket = socket
        self._orig_connect = socket.socket.connect
        self._orig_connect_ex = socket.socket.connect_ex

        def _boom(*args: Any, **kwargs: Any) -> Any:
            raise G2CliError("PAID_GATE", "G2 零 POST：禁止建立 socket 连接")

        socket.socket.connect = _boom  # type: ignore[assignment]
        socket.socket.connect_ex = _boom  # type: ignore[assignment]
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self._socket.socket.connect = self._orig_connect  # type: ignore[assignment]
        self._socket.socket.connect_ex = self._orig_connect_ex  # type: ignore[assignment]
        return False


def _state(root: Path, diagnostic_id: str, step: "str | None" = None) -> Any:
    """G2 自己的步骤记录（与 G1 STEPS 不同；不写/不改 G1 state）。"""
    return G2State(Path(root), diagnostic_id=diagnostic_id).advance(step) \
        if step else G2State(Path(root), diagnostic_id=diagnostic_id).load()


G2_STEPS: "tuple[str, ...]" = ("REGISTERED", "INIT", "CONTRACT", "VISIBILITY",
                               "SCOPE", "ISOLATION", "CLOSED")


class G2State:
    STEPS = G2_STEPS
    REL = "state.json"

    def __init__(self, root: Path, *, diagnostic_id: str) -> None:
        if not re_safe_id(diagnostic_id):
            raise G2CliError("DIAGNOSTIC_ID", diagnostic_id)
        self.root = Path(root)
        self.diagnostic_id = diagnostic_id
        self.path = self.root / self.REL

    def load(self) -> "dict[str, Any]":
        if not self.path.is_file():
            return {"schema": "simpraxis.rsi_g2.state.v1", "schema_version": 1,
                    "diagnostic_id": self.diagnostic_id,
                    "step": "REGISTERED",
                    "history": [{"step": "REGISTERED", "at_utc": None}]}
        return _read_json(self.path)

    def advance(self, step: str) -> "dict[str, Any]":
        if step not in self.STEPS:
            raise G2CliError("STEP_UNKNOWN", step)
        doc = self.load()
        current = str(doc.get("step"))
        if step == current:
            return doc
        history = list(doc.get("history") or [])
        warnings = list(doc.get("warnings") or [])
        if self.STEPS.index(step) != self.STEPS.index(current) + 1:
            warnings.append({"step": step, "out_of_order_from": current,
                             "at_utc": utc_now_iso_z()})
        history.append({"step": step, "at_utc": utc_now_iso_z()})
        out = dict(doc, step=step, history=history, warnings=warnings)
        ensure_private_dir(self.root)
        atomic_write_text(self.path,
                          json.dumps(out, ensure_ascii=False, sort_keys=True,
                                     indent=1) + "\n", mode=0o600)
        return out


def re_safe_id(value: Any) -> bool:
    return isinstance(value, str) and bool(
        re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$", value))


def _diagnostic_id(root: Path, explicit: "str | None") -> str:
    if explicit:
        return explicit
    state = _read_json(Path(root) / GT.STATE_REL) if (Path(root) / GT.STATE_REL).is_file() else {}
    value = str(state.get("diagnostic_id") or "")
    if not value:
        raise G2CliError("STATE_MISSING", "缺 state.json/diagnostic_id")
    return value


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------

def cmd_init(args: argparse.Namespace) -> int:
    root = _root(args)
    RP.assert_rsi_g2_roots_isolated()
    for sub in RP.RSI_G2_SUBDIRS:
        ensure_private_dir(root / sub)
    diagnostic_id = args.diagnostic_id or root.name
    source_commit = args.source_commit or ""
    inputs: "dict[str, Any]" = {"roots": [], "files": []}
    if args.input_manifest:
        data = _read_json(Path(args.input_manifest))
        raw_roots = data.get("roots") or []
        root_specs: "list[GT.RootSpec]" = []
        for i, item in enumerate(raw_roots):
            if isinstance(item, Mapping):
                root_specs.append(GT.RootSpec(
                    root_id=str(item.get("root_id") or f"r{i}"),
                    path=Path(str(item["path"]))))
            else:
                root_specs.append(GT.RootSpec(root_id=f"r{i}",
                                              path=Path(str(item))))
        raw_files = data.get("files") or []
        file_specs: "list[GT.FileSpec]" = []
        for i, item in enumerate(raw_files):
            if isinstance(item, Mapping):
                file_specs.append(GT.FileSpec(
                    root_id=str(item.get("root_id") or
                                (root_specs[0].root_id if root_specs else "")),
                    relpath=str(item["relpath"]),
                    role=str(item.get("role") or "input"),
                    sensitivity=str(item.get("sensitivity") or "private")))
            else:
                file_specs.append(GT.FileSpec(
                    root_id=root_specs[0].root_id if root_specs else "",
                    relpath=str(item), role="input"))
        inventory = GT.inventory_evidence(
            root_specs, file_specs, out_root=root, diagnostic_id=diagnostic_id,
            source_commit=source_commit)
        inputs = {"roots": [str(r.path) for r in root_specs],
                  "files": [f"{f.root_id}:{f.relpath}" for f in file_specs],
                  "inventory_sha256": G2.content_sha(inventory),
                  "n_files": inventory["n_files"],
                  "total_bytes": inventory["total_bytes"]}
        problems = [row for row in inventory["files"]
                    if row.get("state") != "observed"]
        if problems:
            raise G2CliError("INPUT_MISSING", f"{len(problems)} 个输入不可观测")
    manifest = {"schema": "simpraxis.rsi_g2.freeze.v1", "schema_version": 1,
                "diagnostic_id": diagnostic_id,
                "source_commit": source_commit,
                "inputs": inputs,
                "created_at_utc": utc_now_iso_z(),
                "policy_ids": list(T.DELIVERY_POLICIES),
                "note": "G2 冻结：旧根只读；历史事件不重构；零 POST/零仿真"}
    manifest["manifest_sha256"] = G2.content_sha(manifest)
    _write_json(root / "manifest" / "g2_freeze.json", manifest)
    state = _state(root, diagnostic_id, "INIT")
    print(json.dumps({"ok": True, "root": str(root),
                      "manifest_sha256": manifest["manifest_sha256"],
                      "state_step": state.get("step")},
                     ensure_ascii=False, sort_keys=True))
    return 0


# ---------------------------------------------------------------------------
# contract-check
# ---------------------------------------------------------------------------

def cmd_contract_check(args: argparse.Namespace) -> int:
    root = _root(args)
    contract = G2.load_scope_contract(args.scope_contract)
    task_sha = ""
    if args.task_spec:
        raw = Path(args.task_spec).read_bytes()
        task_sha = G2.sha_bytes(raw)
        doc = G2.parse_json_strict(raw)
        if isinstance(doc, Mapping) and doc.get("task_id") != contract.task_id:
            raise G2CliError("TASK_MISMATCH", str(doc.get("task_id")))
        canon = G2.sha_bytes(G2.canonical(doc))
        if contract.task_spec_sha256 not in (task_sha, canon):
            raise G2CliError("SCOPE_CONTRACT_UNDEFINED",
                             "task_spec_sha256 既不等于文件字节 sha 也不等于规范 sha")
    if args.expected:
        expected = _read_json(Path(args.expected))
        bound = str((expected or {}).get("contract_sha256") or "")
        if bound and bound != contract.sha256:
            raise G2CliError("SCOPE_CONTRACT_MISMATCH",
                             "expected 的 contract sha 与公开合同不一致")
    scope_path = root / "contract" / "scope_contract.json"
    G2.write_scope_contract(scope_path, contract)
    public_text = root / "public" / "scope_contract.txt"
    ensure_private_dir(public_text.parent)
    atomic_write_text(public_text, contract.render_public_text(), mode=0o600)
    if contract.render_public_text() not in public_text.read_text(encoding="utf-8"):
        raise G2CliError("SCOPE_CONTRACT_UNDEFINED", "公开文本缺合同原文")
    impl_files = [Path(__file__).resolve(),
                  Path(G2.__file__).resolve(),
                  Path(GV.__file__).resolve()]
    schema_files = {"result_envelope": Path(G2.__file__).resolve(),
                    "scope_contract": Path(GV.__file__).resolve()}
    freeze = (_read_json(root / "manifest" / "g2_freeze.json")
              if (root / "manifest" / "g2_freeze.json").is_file() else {})
    snapshot = G2.build_contract_snapshot(
        policy_id=T.POLICY_G2_ATOMIC, scope_contract=contract,
        schema_files=schema_files,
        required_contrast_ids=args.required_contrast or [contract.contract_id],
        source_commit=str(freeze.get("source_commit") or args.source_commit or ""),
        implementation_files=impl_files, tokenizer_id=TOKENIZER_ID,
        tokenizer_sha256=_tokenizer_sha())
    G2.verify_contract_snapshot(snapshot)
    _write_json(root / "contract" / "g2_contract.json", snapshot)
    _state(root, _diagnostic_id(root, args.diagnostic_id), "CONTRACT")
    print(json.dumps({"ok": True, "scope_contract_sha256": contract.sha256,
                      "contract_sha256": snapshot["contract_sha256"],
                      "limits": snapshot["limits"]},
                     ensure_ascii=False, sort_keys=True))
    return 0


# ---------------------------------------------------------------------------
# validate-visibility（脚本化真实 turn；只读 envelope/请求字节）
# ---------------------------------------------------------------------------

def _base_envelope(*, case_id: str, n_blocks: int = 32,
                   estimate: float = 0.040867675677873194,
                   extra_pad: int = 0, n_contrasts: int = 1) -> "dict[str, Any]":
    contract = G2.ScopeContractV1.from_dict({
        "schema": G2.SCOPE_CONTRACT_SCHEMA, "schema_version": 1,
        "contract_id": f"g2-vis-{case_id}", "task_id": "rsi-g2-shape",
        "task_spec_sha256": "a" * 64, "hypothesis_id": "h_interaction",
        "estimand_id": "est_main", "metric_id": "displacement_x_m",
        "proposition": "increase_gt", "delta": 0.001, "unit": "m",
        "target_population": "registered_distribution", "aggregation": "mean",
        "weighting": "equal", "block_to_contrast_formula": "mean",
        "formula_notes": "等权 mean", "censoring_rule": "dropped 不补值",
        "coverage": "observed_blocks", "interval_level": 0.9,
        "allowed_semantic_labels": list(G2.SEMANTIC_LABELS),
    })
    ids = [f"contrast_{i:02d}" for i in range(n_contrasts)]
    contrasts = [{"contrast_id": cid, "metric_id": "displacement_x_m",
                  "proposition": "increase_gt", "delta": 0.001,
                  "estimate": estimate + i * 1e-9,
                  "lower": estimate - 1e-6, "upper": estimate + 1e-6,
                  "level": 0.9, "science_status": "SUPPORTED",
                  "n_blocks": n_blocks,
                  "dropped": {"count": 0, "ids": [], "ids_sha256": ""}}
                 for i, cid in enumerate(ids)]
    artifacts = [{"artifact_id": "analysis/output.json",
                  "relative_path": "analysis/output.json",
                  "sha256": "b" * 64, "bytes": 2048,
                  "access_mode": "read_result_artifact"},
                 {"artifact_id": "analysis/receipt.json",
                  "relative_path": "analysis/receipt.json",
                  "sha256": "c" * 64, "bytes": 512,
                  "access_mode": "read_result_artifact"}]
    evidence = [{"artifact_id": "analysis/output.json",
                 "json_pointer": f"/contrasts/{i}/estimate",
                 "field_sha256": G2.field_sha(estimate)}
                for i in range(n_contrasts)]
    envelope = G2.build_envelope(
        study_id="A", phase="confirm", plan_sha256="d" * 64,
        analysis_receipt_sha256="c" * 64, analysis_output_sha256="b" * 64,
        scope_contract=contract, required_contrast_ids=ids, contrasts=contrasts,
        primary_contrast_id=ids[0], denominator=n_blocks * n_contrasts,
        dropped={"count": 0, "ids": [], "ids_sha256": ""},
        diagnostics={"status": "OBSERVED", "contrast_count": n_contrasts},
        artifacts=artifacts, evidence=evidence, summary={"n_required": n_contrasts})
    if extra_pad:
        envelope = dict(envelope)
        envelope["pad"] = "x" * extra_pad
    return envelope


def _legacy_confirm_result(n_runs: int = 160) -> "dict[str, Any]":
    """旧 R1.3 confirm 回包的真实形状（含 32 block 效应 ⇒ 8–9 KB）。"""
    estimate = 0.040867675677873194
    contrasts = [{"contrast_id": "friction_x_speed",
                  "metric_id": "displacement_x_m",
                  "proposition": "increase_gt", "delta": "0.001",
                  "estimate": estimate,
                  "lower": 0.040860710191191175,
                  "upper": 0.04087292938493192,
                  "status": "SUPPORTED", "n_blocks": 32}]
    diagnostics = {"contrasts": [{
        "contrast_id": "friction_x_speed", "effect": estimate,
        "block_effects": [{"block_id": f"b{i:02d}",
                           "effect": estimate + i * 1e-6}
                          for i in range(32)]}],
        "coverage": {"blocks": [f"b{i:02d}" for i in range(32)],
                     "run_count": n_runs}}
    return {"ok": True, "action": "run_confirm",
            "candidate_output": {
                "schema": "simpraxis.rsi_r1.analysis_output.v1",
                "schema_version": 1,
                "run_ids": [f"run-{i:04d}" for i in range(n_runs)],
                "contrasts": contrasts, "diagnostics": diagnostics},
            "analysis_receipt": {"sha256": "f" * 64},
            "evidence_index": {"analysis/output.json": "a" * 64,
                               "analysis/receipt.json": "b" * 64},
            "primary_contrast_id": "friction_x_speed", "n_runs": n_runs,
            "result_sha256": "d" * 64, "run_ids_head": ["run-0000"]}


def _visibility_cases() -> "list[dict[str, Any]]":
    """Plan §3.2 的 10 条路径（合成/生产形状；真实封存件可经 --cases 注入）。"""
    legacy = _legacy_confirm_result(160)
    cases: "list[dict[str, Any]]" = []
    cases.append({"case_id": "small_lt_4096", "policy": T.POLICY_G2_ATOMIC,
                  "envelope": _base_envelope(case_id="small"),
                  "expected_outcome": "SUCCESS"})
    cases.append({"case_id": "legacy_control_8k", "policy": T.POLICY_LEGACY,
                  "legacy_result": legacy,
                  "primary_marker": "0.040867675677873194",
                  "expected_outcome": "EXPECTED_LEGACY_FAILURE"})
    cases.append({"case_id": "g2_8k_shape", "policy": T.POLICY_G2_ATOMIC,
                  "envelope": _base_envelope(case_id="g2_8k", n_blocks=160),
                  "expected_outcome": "SUCCESS"})
    cases.append({"case_id": "g2_boundary_16384", "policy": T.POLICY_G2_ATOMIC,
                  "envelope": _base_envelope(case_id="bound16k", n_blocks=32),
                  "expected_outcome": "SUCCESS"})
    cases.append({"case_id": "g2_oversize_block", "policy": T.POLICY_G2_ATOMIC,
                  "envelope": _base_envelope(case_id="over", extra_pad=17000),
                  "expected_outcome": "BLOCK_BEFORE_SEND"})
    cases.append({"case_id": "g2_192run_shape", "policy": T.POLICY_G2_ATOMIC,
                  "envelope": _base_envelope(case_id="r192", n_blocks=32,
                                             n_contrasts=6),
                  "expected_outcome": "SUCCESS"})
    cases.append({"case_id": "history_shift_keeps_context",
                  "policy": T.POLICY_G2_ATOMIC,
                  "envelope": _base_envelope(case_id="hist"),
                  "history_rounds": 6, "envelope_position": "first",
                  "use_result_context": True,
                  "expected_outcome": "SUCCESS"})
    cases.append({"case_id": "history_shift_without_context",
                  "policy": T.POLICY_G2_ATOMIC,
                  "envelope": _base_envelope(case_id="hist2"),
                  "history_rounds": 6, "envelope_position": "first",
                  "use_result_context": False,
                  "expected_outcome": "BLOCK_BEFORE_SEND"})
    cases.append({"case_id": "cross_study_no_leak",
                  "policy": T.POLICY_G2_ATOMIC,
                  "envelope": _base_envelope(case_id="xa"),
                  "other_envelope": _base_envelope(case_id="xb"),
                  "expected_outcome": "SUCCESS"})
    cases.append({"case_id": "g2_boundary_4096",
                  "policy": T.POLICY_G2_ATOMIC,
                  "envelope": _base_envelope(case_id="b4096", n_contrasts=2),
                  "expected_outcome": "SUCCESS"})
    cases.append({"case_id": "request_token_cap",
                  "policy": T.POLICY_G2_ATOMIC,
                  "envelope": _base_envelope(case_id="tokcap"),
                  "huge_system": True,
                  "expected_outcome": "INPUT_LIMIT_BEFORE_SEND"})
    return cases


def _turns_for(envelope: Mapping[str, Any], rounds: int,
               *, position: str = "last") -> "list[T.Turn]":
    turns: "list[T.Turn]" = []
    for i in range(max(1, rounds)):
        turns.append(T.Turn(model_text=json.dumps({"action": "analyze",
                                                   "source_sha256": "e" * 64,
                                                   "run_ids": [f"r{i}"]}),
                            result={"ok": True, "action": "analyze",
                                    "analysis_id": f"a{i}"}))
    target = 0 if position == "first" else len(turns) - 1
    turns[target] = T.Turn(
        model_text=json.dumps({"action": "run_confirm",
                               "confirm_token": "tok"}),
        result={"ok": True, "action": "run_confirm",
                T.G2_ENVELOPE_KEY: dict(envelope), "n_runs": 160})
    return turns


def _run_visibility_case(case: Mapping[str, Any]) -> "dict[str, Any]":
    policy = str(case.get("policy"))
    envelope = case.get("envelope")
    rounds = int(case.get("history_rounds") or 1)
    position = str(case.get("envelope_position") or "last")
    use_context = bool(case["use_result_context"]) \
        if "use_result_context" in case else rounds > 2
    marker = str(case.get("primary_marker") or "")
    if policy == T.POLICY_LEGACY:
        turns = [T.Turn(model_text=json.dumps({"action": "run_confirm",
                                               "confirm_token": "tok"}),
                        result=dict(case.get("legacy_result") or {}))]
        status, req = _try_build(turns=turns, policy=policy, context=None)
        joined = "" if req is None else "\n".join(m["content"]
                                                  for m in req.messages)
        carries = bool(marker) and marker in joined
        out = {"case_id": case.get("case_id"), "policy": policy,
               "expected_outcome": case.get("expected_outcome"),
               "outcome": ("EXPECTED_LEGACY_FAILURE" if not carries
                           else "LEGACY_UNEXPECTEDLY_COMPLETE"),
               "primary_marker_present": carries,
               "n_bytes": None if req is None else req.n_bytes,
               "legacy_truncation_marker": "[TRUNCATED_BY_HOST]" in joined,
               "first_divergence": "CANDIDATE_OUTPUT" if not carries else "NONE"}
        out["ok"] = (not carries and
                     case.get("expected_outcome") == "EXPECTED_LEGACY_FAILURE")
        return out
    turns = _turns_for(envelope, rounds, position=position)
    context = dict(envelope) if use_context else None
    expected = str(case.get("expected_outcome") or "SUCCESS")
    system = ("s" * 90000) if case.get("huge_system") \
        else "g2 visibility（离线）"
    status, request = _try_build(turns=turns, policy=policy, context=context,
                                 system=system)
    out: "dict[str, Any]" = {"case_id": case.get("case_id"),
                             "policy": policy,
                             "expected_outcome": expected,
                             "ok": False}
    if request is None:
        out.update({"outcome": "BLOCK_BEFORE_SEND",
                    "code": (status or {}).get("code"),
                    "detail": (status or {}).get("detail")})
        out["ok"] = expected == "BLOCK_BEFORE_SEND"
        return out
    joined = "\n".join(m["content"] for m in request.messages)
    accounting = T.check_request(request)
    out["accounting"] = accounting.as_dict()
    if not accounting.ok:
        out.update({"outcome": "INPUT_LIMIT_BEFORE_SEND",
                    "code": accounting.verdict,
                    "envelope_in_request": G2.canonical(dict(envelope)).decode()
                    in joined if envelope is not None else False})
        out["ok"] = expected == "INPUT_LIMIT_BEFORE_SEND"
        return out
    env_text = G2.canonical(dict(envelope)).decode("utf-8") \
        if envelope is not None else ""
    carries = bool(env_text) and env_text in joined
    out.update({"outcome": "SUCCESS" if carries else "NO_ENVELOPE",
                "n_bytes": request.n_bytes,
                "local_tokens": request.local_tokens,
                "envelope_in_request": carries,
                "legacy_truncation_marker": any(
                    "[TRUNCATED_BY_HOST]" in m["content"]
                    for m in request.messages),
                "request_sha256": request.sha256,
                "first_divergence": G2.first_divergence(
                    envelope if carries else None, joined)})
    out["ok"] = carries
    if case.get("other_envelope") is not None:
        other_text = G2.canonical(dict(case["other_envelope"])).decode("utf-8")
        out["cross_study_leak"] = other_text in joined
        out["ok"] = out["ok"] and not out["cross_study_leak"]
    return out


def _try_build(*, turns: Sequence[T.Turn], policy: str,
               context: "Mapping[str, Any] | None",
               system: str = "g2 visibility（离线）"
               ) -> "tuple[dict[str, Any] | None, T.Request | None]":
    try:
        request = T.build_request(
            system=system, task_brief="只读形状演示；无模型",
            turns=turns, counter=_counter, max_tokens=8192,
            delivery_policy=policy, result_context=context)
        return None, request
    except T.ResultViewIncomplete as exc:
        return {"code": "RESULT_VIEW_INCOMPLETE", "detail": exc.detail}, None
    except ValueError as exc:
        return {"code": "VALUE_ERROR", "detail": str(exc)}, None


def _run_read_paths() -> "dict[str, Any]":
    """⑨ 多页/错 cursor/跨 artifact/SHA 替换；⑩ 读后提交不重跑 confirm。"""
    data = json.dumps({"schema": "simpraxis.rsi_g2.case_analysis.v1",
                       "blocks": [{"i": i, "v": i / 1000.0} for i in range(600)]},
                      ensure_ascii=False).encode("utf-8")
    digest = G2.sha_bytes(data)
    registry = G2.ArtifactRegistry([
        {"artifact_id": "analysis/output.json",
         "relative_path": "analysis/output.json", "sha256": digest,
         "bytes": len(data), "access_mode": "read_result_artifact"},
        {"artifact_id": "analysis/other.json",
         "relative_path": "analysis/other.json", "sha256": "f" * 64,
         "bytes": 3, "access_mode": "read_result_artifact"}])
    out: "dict[str, Any]" = {"ok": False, "pages": [], "errors": {}}
    chunks: "list[bytes]" = []
    cursor: "int | None" = 0
    while cursor is not None:
        page = G2.read_page(registry=registry,
                            read_bytes=lambda rel: data,
                            artifact_id="analysis/output.json",
                            output_sha256=digest, cursor=cursor,
                            page_bytes=8192)
        chunks.append(page["data"].encode("utf-8"))
        out["pages"].append({"offset": page["offset"],
                             "n_bytes": page["n_bytes"],
                             "next_cursor": page["next_cursor"],
                             "page_sha256": page["page_sha256"]})
        cursor = page["next_cursor"]
    out["reassembled_ok"] = b"".join(chunks) == data
    def _err(name: str, **kwargs: Any) -> None:
        try:
            G2.read_page(registry=kwargs.pop("registry", registry),
                         read_bytes=kwargs.pop("read_bytes", lambda rel: data),
                         artifact_id=kwargs.pop("artifact_id",
                                                "analysis/output.json"),
                         output_sha256=kwargs.pop("output_sha256", digest),
                         cursor=kwargs.pop("cursor", 0),
                         page_bytes=kwargs.pop("page_bytes", 8192))
            out["errors"][name] = "ALLOWED"
        except G2.G2Error as exc:
            out["errors"][name] = exc.code
    _err("cursor_past_end", cursor=len(data) + 1)
    _err("cursor_negative", cursor=-1)
    _err("page_too_large", page_bytes=9000)
    _err("sha_substitution", output_sha256="0" * 64)
    _err("cross_artifact", artifact_id="analysis/other.json")
    _err("unregistered", artifact_id="analysis/missing.json")
    # ⑩ 读后提交：context 固定携带 envelope 一次，不重复、不重跑 confirm。
    envelope = _base_envelope(case_id="readsubmit")
    submit_turn = T.Turn(
        model_text=json.dumps({"action": "submit_claim", "claims": [],
                               "report_text": "read-then-submit"}),
        result={"ok": True, "action": "submit_claim"})
    request = T.build_request(
        system="g2 read→submit", task_brief="读取完成后提交；无模型",
        turns=[T.Turn(model_text=json.dumps({"action": "run_confirm",
                                             "confirm_token": "tok"}),
                      result={"ok": True, "action": "run_confirm",
                              T.G2_ENVELOPE_KEY: dict(envelope)}),
               submit_turn],
        counter=_counter, max_tokens=8192, delivery_policy=T.POLICY_G2_ATOMIC,
        result_context=dict(envelope))
    env_text = G2.canonical(dict(envelope)).decode("utf-8")
    out["context_occurrences"] = "\n".join(
        m["content"] for m in request.messages).count(env_text)
    out["confirm_rerun"] = out["context_occurrences"] != 1
    out["submit_text_present"] = "read-then-submit" in request.serialized
    expected = {"cursor_past_end": "CURSOR_INVALID",
                "cursor_negative": "CURSOR_INVALID",
                "page_too_large": "PAGE_TOO_LARGE",
                "sha_substitution": "ARTIFACT_SHA_MISMATCH",
                "cross_artifact": "ARTIFACT_SHA_MISMATCH",
                "unregistered": "ARTIFACT_MISSING"}
    out["ok"] = (out["reassembled_ok"] and len(out["pages"]) >= 2
                 and out["errors"] == expected
                 and out["context_occurrences"] == 1
                 and out["submit_text_present"])
    return out


def cmd_validate_visibility(args: argparse.Namespace) -> int:
    root = _root(args)
    cases = (_read_json(Path(args.cases)).get("cases")
             if args.cases else _visibility_cases())
    if not isinstance(cases, list) or not cases:
        raise G2CliError("CASES_MISSING", "空 cases")
    with _OfflineGuard(), _ResourceGuard(root, inputs=[Path(args.cases)] if args.cases else []):
        results = [_run_visibility_case(case) for case in cases]
        reads = _run_read_paths()
        ok = all(bool(r.get("ok")) for r in results) and reads["ok"]
        report = {"schema": SCHEMA_SHAPE_REPORT, "schema_version": 1,
                  "policy_id": T.POLICY_G2_ATOMIC, "ok": ok,
                  "n_cases": len(results), "results": results,
                  "read_paths": reads,
                  "created_at_utc": utc_now_iso_z(),
                  "note": "合成/生产形状；真实封存件经 --cases 注入且只读"}
        _write_json(root / "visibility" / "report.json", report)
    _state(root, _diagnostic_id(root, args.diagnostic_id), "VISIBILITY")
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# validate-scope
# ---------------------------------------------------------------------------

def cmd_validate_scope(args: argparse.Namespace) -> int:
    root = _root(args)
    doc = _read_json(Path(args.cases))
    batch = ({"cases": doc.get("cases") or [], "claims": doc.get("claims") or {}}
             if "schema" not in doc or doc.get("schema") == GV.CASE_SCHEMA + ".batch"
             else doc)
    with _OfflineGuard(), _ResourceGuard(root, inputs=[Path(args.cases)]):
        report = GV.score_batch(batch)
        _write_json(root / "scope" / "report.json", report)
    _state(root, _diagnostic_id(root, args.diagnostic_id), "SCOPE")
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["ok"] else 1


# ---------------------------------------------------------------------------
# validate-isolation
# ---------------------------------------------------------------------------

def cmd_validate_isolation(args: argparse.Namespace) -> int:
    """分主体隔离检查；同 uid 环境无法建立 OS 级边界时诚实标 UNVERIFIED。"""
    root = _root(args)
    for sub in ("public", "private_expected", "judge", "raw"):
        ensure_private_dir(root / sub)
    (root / "public" / "allowed.json").write_text('{"ok": true}\n',
                                                  encoding="utf-8")
    for sub in ("private_expected", "judge", "raw"):
        path = root / sub / "secret.json"
        path.write_text('{"secret": true}\n', encoding="utf-8")
        os.chmod(path, 0o600)
    store_checks: "dict[str, Any]" = {}
    allowed_data = b'{"contrasts": []}\n'
    allowed_art = {"artifact_id": "analysis/output.json",
                   "relative_path": "analysis/output.json",
                   "sha256": G2.sha_bytes(allowed_data),
                   "bytes": len(allowed_data),
                   "access_mode": "read_result_artifact"}
    try:
        page = G2.read_page(
            registry=G2.ArtifactRegistry([allowed_art]),
            read_bytes=lambda rel: allowed_data,
            artifact_id="analysis/output.json",
            output_sha256=G2.sha_bytes(allowed_data))
        store_checks["registered_read_ok"] = bool(page.get("data"))
    except G2.G2Error as exc:
        store_checks["registered_read_ok"] = exc.code
    for name, art in (("private_path", "private_expected/secret.json"),
                      ("judge_path", "judge/secret.json"),
                      ("raw_path", "raw/secret.json"),
                      ("traversal", "analysis/../../etc/passwd")):
        try:
            G2.read_page(registry=G2.ArtifactRegistry([
                {"artifact_id": "x", "relative_path": art,
                 "sha256": "a" * 64, "bytes": 4,
                 "access_mode": "read_result_artifact"}]),
                read_bytes=lambda rel: b"nope", artifact_id="x",
                output_sha256="a" * 64)
            store_checks[name] = "ALLOWED"
        except G2.G2Error as exc:
            store_checks[name] = exc.code
    # 子进程：`-I`、最小 env、显式允许清单；同 uid 无法做 OS 级拒绝。
    helper = ("import json,sys\n"
              "allowed=sys.argv[1]\n"
              "open(allowed,'rb').read()\n"
              "print('allowed-read-ok')\n")
    proc = subprocess.run([sys.executable, "-I", "-c", helper,
                           str(root / "public" / "allowed.json")],
                          capture_output=True, text=True, timeout=60,
                          env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")})
    same_uid = not _can_switch_principal()
    report = {
        "schema": SCHEMA_ISOLATION, "schema_version": 1,
        "mode": "same_uid_store_allowlist",
        "os_level_denial": False if same_uid else True,
        "status": "ISOLATION_UNVERIFIED" if same_uid else "ISOLATION_OK",
        "store_checks": store_checks,
        "subprocess": {"rc": proc.returncode,
                       "stdout": proc.stdout.strip(),
                       "stderr": proc.stderr.strip()[:400],
                       "isolated": True},
        "reason": ("同 uid 无法为 renderer/candidate/compiler 建立 OS 权限主体；"
                   "按 Plan §3.3 明确标 ISOLATION_UNVERIFIED，不宣称无泄漏"
                   if same_uid else ""),
        "created_at_utc": utc_now_iso_z(),
    }
    ok = (store_checks.get("registered_read_ok") is True
          and store_checks.get("private_path") == "ARTIFACT_FORBIDDEN"
          and store_checks.get("judge_path") == "ARTIFACT_FORBIDDEN"
          and store_checks.get("raw_path") == "ARTIFACT_FORBIDDEN"
          and store_checks.get("traversal") in ("ARTIFACT_PATH", "ARTIFACT_FORBIDDEN"))
    report["store_allowlist_ok"] = ok
    report["ok"] = ok and report["status"] == "ISOLATION_OK"
    _write_json(root / "isolation" / "report.json", report)
    _state(root, _diagnostic_id(root, args.diagnostic_id), "ISOLATION")
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["ok"] else 1


def _can_switch_principal() -> bool:
    """能否真正以**不同 uid** 运行子进程（OS 级主体隔离的前提）。"""
    if os.geteuid() == 0:
        return True
    try:
        proc = subprocess.run(["sudo", "-n", "-u", "nobody", "true"],
                              capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


# ---------------------------------------------------------------------------
# closeout（只出工程轴；不产生科学 claim/R1 晋升）
# ---------------------------------------------------------------------------

def cmd_closeout(args: argparse.Namespace) -> int:
    root = _root(args)
    visibility = (_read_json(root / "visibility" / "report.json")
                  if (root / "visibility" / "report.json").is_file() else None)
    scope = (_read_json(root / "scope" / "report.json")
             if (root / "scope" / "report.json").is_file() else None)
    isolation = (_read_json(root / "isolation" / "report.json")
                 if (root / "isolation" / "report.json").is_file() else None)
    contract = (_read_json(root / "contract" / "g2_contract.json")
                if (root / "contract" / "g2_contract.json").is_file() else None)
    compat = (_read_json(Path(args.compat_report)) if args.compat_report
              else None)
    compat_ok: "bool | None" = None
    if compat is not None:
        record = (compat.get("records") or {}).get("G2-13")
        if record is not None:
            compat_ok = record.get("rc") == 0
    gaps: "list[str]" = []
    content = "PASS" if visibility and visibility.get("ok") else "FAIL"
    if visibility is None:
        content = "UNKNOWN"
    history = ("PASS" if visibility and any(
        r.get("envelope_in_request") for r in visibility.get("results") or [])
        else "UNKNOWN")
    scope_axis = "PASS" if scope and scope.get("ok") else (
        "UNKNOWN" if scope is None else "FAIL")
    binding = "PASS" if scope and scope.get("ok") and not scope.get(
        "semantic_mismatch") else ("UNKNOWN" if scope is None else "FAIL")
    legacy = ("PASS" if compat_ok is True
              else "UNKNOWN" if compat_ok is None else "FAIL")
    iso_axis = (isolation or {}).get("status", "UNKNOWN")
    if iso_axis == "ISOLATION_UNVERIFIED":
        gaps.append("ISOLATION_UNVERIFIED")
    contract_ok = False
    if contract is not None:
        try:
            G2.verify_contract_snapshot(contract)
            contract_ok = True
        except G2.G2Error as exc:
            gaps.append(exc.code)
    if not contract_ok and contract is not None:
        scope_axis = "FAIL"
    axes = {"content_integrity": content,
            "visibility_observability": history,
            "scope_contract": scope_axis,
            "binding_numeric": binding,
            "legacy_compat": legacy,
            "isolation": iso_axis}
    unknown = [name for name, value in axes.items()
               if value not in ("PASS", "ISOLATION_UNVERIFIED", "ISOLATION_OK")]
    if content == "FAIL":
        overall = "G2_BLOCKED_VISIBILITY"
    elif scope_axis == "FAIL" or binding == "FAIL":
        overall = "G2_BLOCKED_SCOPE"
    elif legacy == "FAIL":
        overall = "G2_LEGACY_DRIFT"
    elif unknown or gaps:
        overall = "G2_ENGINEERING_READY_WITH_GAPS"
    else:
        overall = "G2_ENGINEERING_READY"
    if unknown:
        gaps.extend(f"UNKNOWN:{name}" for name in unknown)
    report = {"schema": SCHEMA_CLOSEOUT, "schema_version": 1,
              "overall": overall, "axes": axes, "gaps": gaps,
              "policy_id": T.POLICY_G2_ATOMIC,
              "scientific_claim": "NONE（只出工程轴；不晋升 R1/R2）",
              "reports": {"visibility": bool(visibility) and
                          visibility.get("ok"),
                          "scope": bool(scope) and scope.get("ok"),
                          "isolation": iso_axis,
                          "contract": contract_ok},
              "created_at_utc": utc_now_iso_z()}
    _write_json(root / "closeout" / "g2_closeout.json", report)
    _state(root, _diagnostic_id(root, args.diagnostic_id), "CLOSED")
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if overall in ("G2_ENGINEERING_READY",
                            "G2_ENGINEERING_READY_WITH_GAPS") else 1


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------

def _g21():
    from orchestrator.rsi_r1 import g21_bridge as G21
    return G21


def _bridge_root(args: argparse.Namespace) -> Path:
    if not getattr(args, "root", None):
        raise G2CliError("ROOT_MISSING", "bridge-* 必须给 --root")
    return Path(args.root)


def _bridge_manifest(root: Path, args: argparse.Namespace):
    G21 = _g21()
    frozen = root / "freeze" / "replay_manifest.json"
    if frozen.is_file():
        return G21.load_manifest(frozen)
    explicit = getattr(args, "inputs", "") or ""
    if explicit:
        return G21.load_manifest(Path(explicit))
    raise G2CliError("MANIFEST_MISSING", f"{frozen} 不存在且未给 --inputs")


def cmd_bridge_init(args: argparse.Namespace) -> int:
    """G21-0：建新根、冻结输入清单与 sha、写合同快照。"""
    G21 = _g21()
    root = _bridge_root(args)
    RP.assert_rsi_g21_roots_isolated()
    for sub in RP.RSI_G21_SUBDIRS:
        ensure_private_dir(root / sub)
    manifest = G21.load_manifest(Path(args.inputs))
    frozen = G21.freeze_inputs(root, manifest)
    scopes = G21.build_scopes(root, manifest)
    snapshot = G21.snapshot_files(root)
    G21.write_json_private(root / "freeze" / "replay_manifest.json", manifest)
    G21.write_json_private(root / "freeze" / "freeze_report.json",
                           {"frozen": frozen, "scopes": scopes})
    print(json.dumps({"ok": True, "root": str(root),
                      "closure_sha256": frozen["closure_sha256"],
                      "snapshot_sha256": snapshot["snapshot_sha256"],
                      "n_inputs": frozen["entries"]},
                     ensure_ascii=False, sort_keys=True))
    return 0


def cmd_bridge_preflight(args: argparse.Namespace) -> int:
    """核实冻结件/实现闭包/批准 tokenizer/两 study 源就绪。"""
    G21 = _g21()
    root = _bridge_root(args)
    manifest = _bridge_manifest(root, args)
    closure = G21.verify_source_closure(root, manifest)
    if not (root / "contract" / "scope_A.json").is_file():
        G21.build_scopes(root, manifest)
    freshness = G21.verify_signature_freshness(root, manifest)
    tok = G21.tokenizer_contract(root)
    readiness = {}
    for study in G21.STUDIES:
        task = G21.TS.TaskSpecV13.from_dict(
            G21.read_json(manifest["studies"][study]["task"]))
        readiness[study] = G21.source_readiness(manifest["studies"][study], task)
    doc = {"ok": tok["ok"] and bool(closure["ok"]),
           "closure": closure, "signature": freshness,
           "tokenizer": {"status": tok["status"],
                         "sha256": tok["contract"].get("sha256"),
                         "approval_ref": tok["contract"].get("approval_ref")},
           "source": readiness}
    G21.write_json_private(root / "reports" / "preflight.json", doc)
    print(json.dumps(doc, ensure_ascii=False, sort_keys=True))
    blocked = [s for s, r in readiness.items()
               if r["status"] != G21.SOURCE_READY]
    return 0 if not blocked else 1


def cmd_bridge_run(args: argparse.Namespace) -> int:
    """真实 loop/state/dispatcher 的离线证据桥（读+submit，零 POST）。"""
    G21 = _g21()
    root = _bridge_root(args)
    manifest = _bridge_manifest(root, args)
    tok = G21.tokenizer_contract(root)
    consumer = Path(args.consumer) if args.consumer else (
        G21.repo_root() / "tests" / "fixtures" / "g21_consumer" / "consumer.py")
    result = G21.run_study(root, manifest, str(args.study),
                           consumer_path=consumer, python=sys.executable,
                           max_turns=int(args.max_turns or 12),
                           tokenizer=tok["tokenizer"])
    print(json.dumps({k: result.get(k) for k in
                      ("study", "ok", "status", "study_state",
                       "envelope_sha256", "envelope_bytes")},
                     ensure_ascii=False, sort_keys=True))
    return 0 if result.get("ok") else 1


def cmd_bridge_raw_check(args: argparse.Namespace) -> int:
    """独立 trusted raw 复算（子进程；不 import 生产投影）。"""
    G21 = _g21()
    root = _bridge_root(args)
    manifest = _bridge_manifest(root, args)
    report = G21.run_raw_check(root, manifest, python=sys.executable)
    print(json.dumps({"ok": report.get("ok"),
                      "independent": report.get("independent"),
                      "studies": sorted((report.get("studies") or {}))},
                     ensure_ascii=False, sort_keys=True))
    return 0 if report.get("ok") else 1


def cmd_bridge_capacity(args: argparse.Namespace) -> int:
    G21 = _g21()
    root = _bridge_root(args)
    manifest = _bridge_manifest(root, args)
    tok = G21.tokenizer_contract(root)
    if args.write_matrix:
        matrix = G21.capacity_matrix(
            G2.load_scope_contract(manifest["studies"]["A"]["scope_contract"]))
        G21.write_json_private(root / "freeze" / "capacity_matrix.json", matrix)
    report = G21.run_capacity(root, manifest, tokenizer=tok["tokenizer"])
    print(json.dumps({k: report.get(k) for k in ("ok", "n_ok", "n_total")},
                     ensure_ascii=False, sort_keys=True))
    return 0 if report.get("ok") else 1


def cmd_bridge_isolation(args: argparse.Namespace) -> int:
    G21 = _g21()
    root = _bridge_root(args)
    report = G21.run_isolation(root, image_digest=str(args.image_digest))
    print(json.dumps({"status": report.get("status"),
                      "negative_control":
                      (report.get("negative_control") or {}).get(
                          "readable_private")},
                     ensure_ascii=False, sort_keys=True))
    return 0 if report.get("status") == "ISOLATION_OK" else 1


def cmd_bridge_closeout(args: argparse.Namespace) -> int:
    G21 = _g21()
    root = _bridge_root(args)
    report = G21.closeout(root)
    print(json.dumps({"overall": report.get("overall"),
                      "axes": report.get("axes"),
                      "gaps": report.get("gaps")},
                     ensure_ascii=False, sort_keys=True))
    return 0 if report.get("overall") in (
        "G2.1_ENGINEERING_READY_FOR_NEW_DESIGN",) else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="g2_cli", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init")
    p.add_argument("--root")
    p.add_argument("--diagnostic-id")
    p.add_argument("--input-manifest")
    p.add_argument("--source-commit", default="")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("contract-check")
    p.add_argument("--root", required=True)
    p.add_argument("--scope-contract", required=True)
    p.add_argument("--task-spec")
    p.add_argument("--expected")
    p.add_argument("--required-contrast", action="append")
    p.add_argument("--source-commit", default="")
    p.add_argument("--diagnostic-id")
    p.set_defaults(func=cmd_contract_check)

    p = sub.add_parser("validate-visibility")
    p.add_argument("--root", required=True)
    p.add_argument("--cases")
    p.add_argument("--diagnostic-id")
    p.set_defaults(func=cmd_validate_visibility)

    p = sub.add_parser("validate-scope")
    p.add_argument("--root", required=True)
    p.add_argument("--cases", required=True)
    p.add_argument("--diagnostic-id")
    p.set_defaults(func=cmd_validate_scope)

    p = sub.add_parser("validate-isolation")
    p.add_argument("--root", required=True)
    p.add_argument("--diagnostic-id")
    p.set_defaults(func=cmd_validate_isolation)

    p = sub.add_parser("closeout")
    p.add_argument("--root", required=True)
    p.add_argument("--compat-report")
    p.add_argument("--diagnostic-id")
    p.set_defaults(func=cmd_closeout)

    # ---- RSI-G2.1（Plan §4.1：沿 g2_cli 加 bridge-* 离线子命令）----
    p = sub.add_parser("bridge-init")
    p.add_argument("--root", required=True)
    p.add_argument("--inputs", required=True)
    p.add_argument("--source-commit", default="")
    p.set_defaults(func=cmd_bridge_init)

    p = sub.add_parser("bridge-preflight")
    p.add_argument("--root", required=True)
    p.add_argument("--inputs", default="")
    p.add_argument("--tokenizer-contract", default="")
    p.set_defaults(func=cmd_bridge_preflight)

    p = sub.add_parser("bridge-run")
    p.add_argument("--root", required=True)
    p.add_argument("--study", required=True, choices=["A", "B"])
    p.add_argument("--inputs", default="")
    p.add_argument("--consumer", default="")
    p.add_argument("--max-turns", type=int, default=12)
    p.set_defaults(func=cmd_bridge_run)

    p = sub.add_parser("bridge-raw-check")
    p.add_argument("--root", required=True)
    p.add_argument("--inputs", default="")
    p.set_defaults(func=cmd_bridge_raw_check)

    p = sub.add_parser("bridge-capacity")
    p.add_argument("--root", required=True)
    p.add_argument("--inputs", default="")
    p.add_argument("--fixtures", default="")
    p.add_argument("--write-matrix", action="store_true")
    p.set_defaults(func=cmd_bridge_capacity)

    p = sub.add_parser("bridge-isolation")
    p.add_argument("--root", required=True)
    p.add_argument("--image-digest", required=True)
    p.set_defaults(func=cmd_bridge_isolation)

    p = sub.add_parser("bridge-closeout")
    p.add_argument("--root", required=True)
    p.set_defaults(func=cmd_bridge_closeout)
    return parser


def main(argv: "Sequence[str] | None" = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except G2CliError as exc:
        print(json.dumps({"ok": False, "code": exc.code, "detail": exc.detail},
                         ensure_ascii=False), file=sys.stderr)
        return 2
    except G2.G2Error as exc:
        print(json.dumps({"ok": False, "code": exc.code, "detail": exc.detail},
                         ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
