"""T5 最终 LOCK：把 §3.2 的全部绑定事实冻结成一份不可变合同。

设计要点（计划 §3.2 / §6.3 / T5 / AC26–27）：

* 事实 **重新计算** 而非抄写：``collect_facts`` 每次从代码/配置/campaign 实件
  现场取数；LOCK 只保存当次快照与自哈希。
* 任何一处漂移即拒绝付费：``verify_lock`` 重算事实并逐字段比对，同时校验
  文档自哈希与 G-I 双签；``assert_paid_allowed`` 是付费入口的唯一闸。
* 价格必须可上界：最坏费用按 **232 次**（228 正常 + 4 条件）与每请求
  输入/输出帽计算；无法上界或超过货币帽 ⇒ ``BLOCKED_PRICE``，不触网。
* 签名绑定同一 ``lock_sha256``：reviewer/verifier 的签名件各自独立生成，
  角色不同且签名人不同；缺一即 ``LOCK_PENDING_SIGNATURES``。

本模块零网络、零模型调用；只读旧根（不写）。
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import sys
from pathlib import Path
from typing import Any, Mapping

import yaml

from orchestrator.rsi_r01 import attempts as A
from orchestrator.rsi_r01 import campaign as C
from orchestrator.rsi_r01 import schemas as S
from orchestrator.rsi_r01.observations import (IMAGE_DIGEST, IMAGE_REF,
                                               IMAGE_REPO, SERVICE_SCRIPT)

REPO_ROOT = Path(__file__).resolve().parents[2]
LOCK_SCHEMA = S.SCHEMA_LOCK
SIGN_SCHEMA = f"{S.PREFIX}.signature.v1"
LOCK_FILENAME = "final_lock.json"
CANDIDATE_FILENAME = "lock_candidate.json"
SIG_DIR = "signatures"
SIG_ROLES = ("reviewer", "verifier")
G_I_GATE = "G-I"

#: 实现面：这些文件的逐件 sha256 进 LOCK；任一变化 LOCK 即失效。
_IMPL_GLOBS = ("orchestrator/rsi_r01/*.py", "scripts/rsi_r01/*.py")
_IMPL_FILES = ("orchestrator/gateway/stage_models.yaml",)

#: G-I 证据（T1 容器 + T3 三件 + T2 设计门 + 九份参考 actor）。
_GI_FILES = {
    "t1_container": "analysis/t1_container/evidence.json",
    "controls": "analysis/controls/controls.json",
    "oracle_audit": "analysis/t3_oracle/audit.json",
    "t2_design_gate": "closeout/t2_design_gate.json",
}
_REF_TRIALS = ("E-dev", "E-val-1", "E-val-2", "D-dev", "D-val-1", "D-val-2",
               "R-dev", "R-val-1", "R-val-2")

#: 价格来源：R0 已签的保守上限合同（同供应商、同三阶段 model_id）。
#: 它**不是**供应商报价（pricing_basis=conservative_ceiling_not_vendor_quote）；
#: 单价上调或新模型价目超过该上限即本 LOCK 失效 ⇒ BLOCKED_PRICE。
PRICE_SOURCE = Path(
    "/mnt/artifacts/simpraxis/rsi_r0/r0_20261108T0825Z/protocol/"
    "pricing_contract.json")
PRICE_PER_1M_INPUT_USD = 1.0
PRICE_PER_1M_OUTPUT_USD = 4.0
USD_CEILING = 100.0
CONDITIONAL_POSTS = 4

#: §6.1 固定协议链（12 POST）：2 短文本 + 2 条×2 真实工具往返 + 2 条×3 长链。
PROTOCOL_CHAIN = (
    {"slot": 1, "kind": "short_text", "posts": 1},
    {"slot": 2, "kind": "short_text", "posts": 1},
    {"slot": 3, "kind": "tool_roundtrip", "chain": "T1", "posts": 2},
    {"slot": 4, "kind": "tool_roundtrip", "chain": "T2", "posts": 2},
    {"slot": 5, "kind": "long_context_tool_chain", "chain": "L1", "posts": 3,
     "input_target_tokens": [[15000, 18000], [18000, 20000]]},
    {"slot": 6, "kind": "long_context_tool_chain", "chain": "L2", "posts": 3,
     "input_target_tokens": [[15000, 18000], [18000, 20000]]},
)

_BLOCK = {
    "env": "BLOCKED_ENV", "model": "BLOCKED_MODEL_CONFIG",
    "accounting": "BLOCKED_TOKEN_ACCOUNTING", "price": "BLOCKED_PRICE",
    "approval": "BLOCKED_APPROVAL", "context": "BLOCKED_CONTEXT_MEASUREMENT",
    "protocol": "BLOCKED_PROTOCOL", "transport": "BLOCKED_TRANSPORT",
    "budget": "BLOCKED_BUDGET", "input": "BLOCKED_INPUT_LIMIT",
}


class LockError(RuntimeError):
    """LOCK 不成立（缺件/漂移/签名无效）；``code`` 为 §9.2 阻断类。"""

    def __init__(self, code: str, problems: "list[str]"):
        self.code = code
        self.problems = list(problems)
        super().__init__(f"{code}: " + "; ".join(problems[:8]))


def refusal_code(problems: "list[str]") -> str:
    """按问题文本映射 §9.2 阻断类（价格/模型/审批优先于通用环境）。"""
    joined = " ".join(problems)
    if "价格" in joined or "货币帽" in joined:
        return _BLOCK["price"]
    if "model_id" in joined or "模型" in joined:
        return _BLOCK["model"]
    if "签名" in joined or "LOCK 不存在" in joined or "候选" in joined:
        return _BLOCK["approval"]
    return _BLOCK["env"]


def canonical_bytes(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


#: 不进自哈希的字段：签名（后加）、自哈希本身、以及纯时间戳元数据。
#: 排除 ``created_utc`` 是为了让候选**可复算**：独立复核重建候选应得同一 hash。
_UNHASHED = ("signatures", "lock_sha256", "created_utc")


def core_sha256(doc: Mapping[str, Any]) -> str:
    """LOCK 自哈希：对除签名/自哈希/时间戳外的全部字段。"""
    core = {k: v for k, v in doc.items() if k not in _UNHASHED}
    return hashlib.sha256(canonical_bytes(core)).hexdigest()


def _sha(path: Path) -> str:
    return C.sha256_file(path)


def _require(path: Path, what: str, missing: "list[str]") -> None:
    if not path.is_file():
        missing.append(f"{what}: {path}")


def implementation_manifest(repo_root: Path = REPO_ROOT) -> "dict[str, str]":
    repo_root = Path(repo_root)
    files: list[Path] = []
    for pattern in _IMPL_GLOBS:
        files.extend(sorted(repo_root.glob(pattern)))
    files.extend(repo_root / f for f in _IMPL_FILES)
    manifest = {str(p.relative_to(repo_root)): _sha(p) for p in files if p.is_file()}
    if not manifest:
        raise LockError("BLOCKED_ENV", ["实现清单为空（repo_root 不对？）"])
    return manifest


def implementation_sha256(manifest: Mapping[str, str]) -> str:
    lines = "".join(f"{k} {manifest[k]}\n" for k in sorted(manifest))
    return hashlib.sha256(lines.encode("utf-8")).hexdigest()


def stage_models(path: Path) -> dict[str, Any]:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    stages = (doc.get("stages") or doc.get("stage_models") or {})
    models = {name: (cfg or {}).get("model_id") for name, cfg in stages.items()
              if isinstance(cfg, Mapping)}
    # 供应端身份取自 yaml 的 ``meta`` 段（t0_baseline 读的同一键）；旧实现误读
    # ``gateway`` 导致 endpoint 恒 null，reviewer MEDIUM-1 已修。
    meta = doc.get("meta") or doc.get("gateway") or {}
    return {"path": str(path), "sha256": _sha(path), "models": models,
            "endpoint": meta.get("endpoint"),
            "base_url_env": meta.get("base_url_env")}


def _verdicts(campaign: Path) -> "tuple[dict[str, Any], list[str]]":
    """G-I 证据门：缺件/负结论一律列出（不静默略过）。"""
    problems: "list[str]" = []
    hashes: "dict[str, Any]" = {}
    for key, rel in _GI_FILES.items():
        path = campaign / rel
        if not path.is_file():
            problems.append(f"G-I 缺件 {key}: {rel}")
            continue
        hashes[key] = _sha(path)
        doc = json.loads(path.read_text(encoding="utf-8"))
        if key == "t1_container":
            if doc.get("all_pass") is not True:
                problems.append("T1 容器证据 all_pass != true")
            verdicts = doc.get("verdicts")
            if not isinstance(verdicts, Mapping):
                problems.append("T1 容器证据缺逐 AC verdicts（只信摘要不算过）")
            else:
                bad = [f"{k}={verdicts.get(k)}" for k in
                       ("AC05", "AC06", "AC07", "AC08", "AC09")
                       if verdicts.get(k) != "PASS"]
                if bad:
                    problems.append(f"T1 逐 AC 未全 PASS：{', '.join(bad)}")
        if key == "controls":
            if doc.get("rejected_all") is not True or int(doc.get("total") or 0) < 13:
                problems.append("T3 负控未全拒或数量 < 13")
            items = doc.get("controls")
            if not isinstance(items, list) or len(items) < 13:
                problems.append("T3 负控缺逐项列表（只信摘要不算过）")
            else:
                not_rejected = [str(it.get("name")) for it in items
                                if isinstance(it, Mapping)
                                and it.get("rejected") is not True]
                if not_rejected:
                    problems.append("T3 逐项存在未拒：" + ", ".join(not_rejected))
        if key == "oracle_audit":
            if doc.get("verdict") != "PASS":
                problems.append("T3 oracle 审计 verdict != PASS")
            checks = doc.get("checks")
            if not isinstance(checks, Mapping) or not checks:
                problems.append("T3 oracle 审计缺逐项 checks")
            else:
                bad = [k for k, v in checks.items() if v is not True]
                if bad:
                    problems.append("T3 oracle 子项未全 true：" + ", ".join(map(str, bad)))
        if key == "t2_design_gate":
            if (doc.get("campaign_verdict") != "DESIGN_READY"
                    or doc.get("decision") != "PROCEED_T3"):
                problems.append("T2 设计门未放行")
    refs: "dict[str, str]" = {}
    for trial in _REF_TRIALS:
        path = campaign / "analysis" / "reference_actor" / f"{trial}.json"
        if not path.is_file():
            problems.append(f"缺参考 actor 记录 {trial}")
            continue
        refs[trial] = _sha(path)
        if json.loads(path.read_text(encoding="utf-8")).get("ok") is not True:
            problems.append(f"参考 actor {trial} ok != true")
    hashes["reference_actor"] = refs
    return hashes, problems


_CONSENT_ENV = "SIMPRAXIS_RSI_R01_CONSENT"
_CONSENT_DEFAULT = Path.home() / "文档" / "RSI_R0.1_Consent.txt"


def _consent_path(ctx: C.CampaignContext) -> "Path | None":
    """授权活文件路径：优先上下文记录；早期上下文无此字段时用常量/环境回退。"""
    raw = getattr(ctx, "consent_path", "") or ""
    if raw:
        return Path(raw)
    env = os.environ.get(_CONSENT_ENV)
    return Path(env) if env else _CONSENT_DEFAULT


def collect_facts(ctx: C.CampaignContext,
                  repo_root: Path = REPO_ROOT) -> dict[str, Any]:
    """现场重算 §3.2 全部绑定事实；缺件与不一致以 problems 返回。"""
    repo_root = Path(repo_root)
    campaign = Path(ctx.root)
    problems: "list[str]" = []
    impl = implementation_manifest(repo_root)
    models = stage_models(repo_root / "orchestrator/gateway/stage_models.yaml")
    if None in models["models"].values():
        problems.append("stage_models 含 model_id: null（需用户裁决）")
    if not models.get("endpoint"):
        problems.append("stage_models 缺 meta.endpoint（供应端身份未知）")

    # 计划/授权活文件重算（reviewer MEDIUM-2）：建 campaign 后改计划或授权必须暴露，
    # 不能只信 campaign_context.json 里的旧 hash。
    binding: "dict[str, Any]" = {}
    for key, raw_path, expected in (
            ("plan_sha256", ctx.plan_path, ctx.plan_sha256),
            ("consent_sha256", _consent_path(ctx), ctx.consent_sha256)):
        live = Path(raw_path) if raw_path else None
        actual = _sha(live) if live is not None and live.is_file() else None
        binding[key] = {"path": str(live) if live else None,
                        "expected": expected, "actual": actual}
        if actual != expected:
            problems.append(f"{key} 与活文件不符（计划/授权在冻结后被改）")

    baseline_path = campaign / "contract" / "baseline.json"
    baseline: dict[str, Any] = {}
    if baseline_path.is_file():
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    else:
        problems.append("缺 contract/baseline.json")

    gi, gi_problems = _verdicts(campaign)
    problems.extend(gi_problems)

    task_lock_path = campaign / "protocol" / "t2_tasks_lock.json"
    task_lock: dict[str, Any] = {}
    if task_lock_path.is_file():
        task_lock = json.loads(task_lock_path.read_text(encoding="utf-8"))
    else:
        problems.append("缺 protocol/t2_tasks_lock.json")
    task_files: "dict[str, str]" = {}
    for trial in _REF_TRIALS:
        path = repo_root / "benchmarks" / "rsi_r01" / "tasks" / trial / "task.json"
        if path.is_file():
            task_files[trial] = _sha(path)
        else:
            problems.append(f"缺任务书 {trial}/task.json")

    caps = A.RSI_R01_CAPS
    normal_posts = A.GLOBAL_CAP
    grand_posts = normal_posts + CONDITIONAL_POSTS
    worst_per_request = round(
        (S.MAX_INPUT_TOKENS * PRICE_PER_1M_INPUT_USD
         + S.MAX_OUTPUT_TOKENS * PRICE_PER_1M_OUTPUT_USD) / 1_000_000, 12)
    worst_total = round(worst_per_request * grand_posts, 12)
    price_sha = _sha(PRICE_SOURCE) if PRICE_SOURCE.is_file() else None
    price_doc: "dict[str, Any]" = {}
    if price_sha is None:
        problems.append(f"价格来源缺失（BLOCKED_PRICE）：{PRICE_SOURCE}")
    else:
        # 价目必须从来源件实读并与绑定常量相等（reviewer LOW-7）：硬编码不再自证。
        try:
            price_doc = json.loads(PRICE_SOURCE.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 —— 解析失败即价格未知
            problems.append(f"价格来源不可解析（BLOCKED_PRICE）：{exc}")
        for key, const in (("price_per_1m_input_usd", PRICE_PER_1M_INPUT_USD),
                           ("price_per_1m_output_usd", PRICE_PER_1M_OUTPUT_USD)):
            actual = price_doc.get(key)
            if actual != const:
                problems.append(
                    f"价格来源 {key}={actual} 与绑定常量 {const} 不符（BLOCKED_PRICE）")
    if worst_total > USD_CEILING:
        problems.append(
            f"最坏费用 {worst_total} > 货币帽 {USD_CEILING}（BLOCKED_PRICE）")

    sealed_path = campaign / "contract" / "sealed_roots.json"
    protected: dict[str, Any] = {"snapshot_sha256": None, "diff": []}
    if not sealed_path.is_file():
        problems.append("缺 contract/sealed_roots.json（无法证明旧根不变）")
    else:
        snap = json.loads(sealed_path.read_text(encoding="utf-8"))
        protected = {"snapshot_sha256": _sha(sealed_path),
                     "diff": C.verify_protected(snap)}
        if protected["diff"]:
            problems.append("旧根/封存期台账 hash 已变："
                            + ", ".join(protected["diff"][:5]))

    facts: dict[str, Any] = {
        "code": {
            "campaign_parent_commit": ctx.code_commit,
            "implementation_manifest": impl,
            "implementation_sha256": implementation_sha256(impl),
            "stage_models": models,
        },
        "runtime": {
            "python_version": platform.python_version(),
            "executable": sys.executable,
            "baseline_sha256": _sha(baseline_path) if baseline_path.is_file() else None,
            "pinned_expected": baseline.get("pinned_expected"),
            "image": {"repo": IMAGE_REPO, "digest": IMAGE_DIGEST, "ref": IMAGE_REF,
                      "service_py_sha256": _sha(SERVICE_SCRIPT)},
        },
        "tasks": {
            "task_lock_sha256": _sha(task_lock_path) if task_lock_path.is_file() else None,
            "task_lock_fields": {k: task_lock.get(k) for k in
                                 ("task_matrix_sha256", "measurement_contract_sha256",
                                  "private_matrix_sha256", "dev_order",
                                  "validation_order", "instances")},
            "task_files": task_files,
            "public_prompt_sha256": task_files,
            "instance_count": len(task_files),
        },
        "schemas": {
            "action_schema_sha256": _sha(repo_root / "orchestrator/rsi_r01/actions.py"),
            "data_schema_sha256": _sha(repo_root / "orchestrator/rsi_r01/schemas.py"),
            "trial_dispatch_sha256": _sha(repo_root / "orchestrator/rsi_r01/trial.py"),
        },
        "security": {
            "observations_sha256": _sha(repo_root / "orchestrator/rsi_r01/observations.py"),
            "analysis_runner_sha256": _sha(repo_root / "orchestrator/rsi_r01/analysis_runner.py"),
            "analysis_pids": S.ANALYSIS_PIDS,
            "analysis_max_source_bytes": S.ANALYSIS_MAX_SOURCE_BYTES,
            "analysis_max_stream_bytes": S.ANALYSIS_MAX_STREAM_BYTES,
            "analysis_max_output_file_bytes": S.ANALYSIS_MAX_OUTPUT_FILE_BYTES,
            "analysis_timeout_s": S.ANALYSIS_TIMEOUT_S,
            "analysis_max_per_trial": S.ANALYSIS_MAX_PER_TRIAL,
        },
        "numerics": {
            "evaluator_sha256": _sha(repo_root / "orchestrator/rsi_r01/evaluator.py"),
            "dt_s": S.DT_S, "n_steps": S.N_STEPS, "expected_rows": S.EXPECTED_ROWS,
            "t_end_s": S.T_END_S, "theta_band": S.THETA_BAND,
            "omega_band": S.OMEGA_BAND, "delta": S.DELTA,
            "band_rule": "inside iff |theta| < theta_band AND |omega| < omega_band（严格）",
            "effect_formula": "(t_low - t_high) / t_low",
            "effect_undefined_rule": "t_low == 0 ⇒ effect=null（不做伪值）",
            "censoring_rule": "全程未入带 ⇒ CENSORED；缺末点/行数不符/非有限 ⇒ INVALID",
        },
        "caps": {
            "per_request": {"input_tokens": S.MAX_INPUT_TOKENS,
                            "output_tokens": S.MAX_OUTPUT_TOKENS},
            "allocation": {"protocol": S.PROTOCOL_POSTS,
                           "development": 3 * S.DEV_POSTS_PER_FAMILY,
                           "validation": 6 * S.VAL_POSTS_PER_FAMILY,
                           "normal_total": normal_posts,
                           "conditional_validate_models": CONDITIONAL_POSTS,
                           "grand_total": grand_posts},
            "family_cap": A.FAMILY_CAP, "trial_cap": A.TRIAL_CAP,
            "protocol_cap": A.PROTOCOL_CAP, "global_cap": A.GLOBAL_CAP,
            "cumulative_normal": {"input_tokens": caps.cumulative_input_tokens,
                                  "output_tokens": caps.cumulative_output_tokens},
            "cumulative_grand": {
                "input_tokens": caps.cumulative_input_tokens
                + CONDITIONAL_POSTS * S.MAX_INPUT_TOKENS,
                "output_tokens": caps.cumulative_output_tokens
                + CONDITIONAL_POSTS * S.MAX_OUTPUT_TOKENS},
            "resource": {"trial_wall_s": caps.trial_wall_s,
                         "trial_sim_steps": caps.trial_sim_steps,
                         "stage_sim_steps": caps.stage_sim_steps,
                         "stage_online_wall_s": caps.stage_online_wall_s,
                         "stage_gpu_wall_s": caps.stage_gpu_wall_s},
        },
        "accounting": {
            "transport_sha256": _sha(repo_root / "orchestrator/rsi_r01/transport.py"),
            "attempts_sha256": _sha(repo_root / "orchestrator/rsi_r01/attempts.py"),
            "serialized_object": "SendPlan.serialized（sort_keys/compact/utf-8）",
            "counter_version": "v1: upper=tokens<=utf8_bytes；lower 需供应商 "
                               "max_token_bytes 事实，未锁则不宣称下限",
            "retry_policy": "全链一次逻辑请求最多 1 POST；无自动重试",
            "capture_rules": "发网前记录 request sha256；响应记录 HTTP/状态/"
                             "stop_reason/usage/body hash；UNKNOWN_SENT 占槽不重发",
        },
        "budget_price": {
            "self_approval": {
                "path": "quota/approvals/r01_budget_approval.json",
                "sha256": _sha(campaign / "quota" / "approvals"
                               / "r01_budget_approval.json")
                if (campaign / "quota" / "approvals"
                    / "r01_budget_approval.json").is_file() else None,
            },
            "currency": "USD",
            "fx_basis": "计价与结算同为美元，无换汇（改币种须重签）",
            "pricing_basis": "conservative_ceiling_not_vendor_quote",
            "price_source": {"path": str(PRICE_SOURCE), "sha256": price_sha},
            "price_source_values": {
                "price_per_1m_input_usd": price_doc.get("price_per_1m_input_usd"),
                "price_per_1m_output_usd": price_doc.get("price_per_1m_output_usd")},
            "price_per_1m_input_usd": PRICE_PER_1M_INPUT_USD,
            "price_per_1m_output_usd": PRICE_PER_1M_OUTPUT_USD,
            "worst_case_per_request_usd": worst_per_request,
            "requests_total_cap": grand_posts,
            "worst_case_total_usd": worst_total,
            "usd_ceiling": USD_CEILING,
            "validity": "任一绑定模型价目超过本上限或来源件 hash 变化即失效",
        },
        "order": {
            "protocol_chain": list(PROTOCOL_CHAIN),
            "dev_order": task_lock.get("dev_order"),
            "validation_order": task_lock.get("validation_order"),
            "slots": [f"{trial}×{S.DEV_POSTS_PER_FAMILY}" for trial in _REF_TRIALS],
            "slot_policy": "每 trial 未用额度不转移；不补考",
        },
        "failure_classification": {
            "closeout_sha256": _sha(repo_root / "scripts/rsi_r01/closeout.py"),
            "checker_sha256": _sha(repo_root / "scripts/verification/check_rsi_r01_closure.py"),
            "measurement_verdicts": sorted(
                v for k, v in vars(S.MeasurementVerdict).items()
                if k.isupper() and isinstance(v, str)),
            "process_verdicts": list(S.ProcessVerdict.ALL),
            "trial_states": list(S.TrialState.TERMINAL),
            "priority": "§9.2 严格优先级；低优先级主判决被覆盖时保留全部 reason_codes",
        },
        "g_i": {
            "evidence": gi,
            "required": "reference 同路径 9/9 + 负控 13/13 全拒 + 独立 oracle PASS",
        },
        "protected": protected,
        "binding": binding,
        "model_change_this_campaign": baseline.get("model_change_this_campaign"),
        "conditional_batch": {
            "name": "R01-M0",
            "status": "N/A" if not baseline.get("model_change_this_campaign")
                      else "REQUIRED",
            "invoice_rule": "两账分别对账、总成本合并不重复；本 LOCK 不含条件批次授权",
        },
    }
    facts["problems"] = problems
    return facts


APPROVAL_REL = "quota/approvals/r01_budget_approval.json"


def ensure_budget_approval(ctx: C.CampaignContext) -> Path:
    """Agent 自批预算批准件（硬规则 2：自批但逐次记账、件落 approvals/）。

    已存在即复用（内容不得改写）；不存在时按当前帽/价格生成一次。
    """
    path = Path(ctx.root) / APPROVAL_REL
    if path.is_file():
        return path
    caps = A.RSI_R01_CAPS
    payload = {
        "schema": f"{S.PREFIX}.budget_approval.v1",
        "campaign_id": ctx.campaign_id,
        "approved_by": "agent_self_approval",
        "basis": "硬规则 2（实呼批次与预算由 Agent 自批）；本件不冒充用户逐笔批准",
        "allocation": {"protocol": S.PROTOCOL_POSTS,
                       "development": 3 * S.DEV_POSTS_PER_FAMILY,
                       "validation": 6 * S.VAL_POSTS_PER_FAMILY,
                       "normal_total": A.GLOBAL_CAP,
                       "conditional_validate_models": CONDITIONAL_POSTS,
                       "grand_total": A.GLOBAL_CAP + CONDITIONAL_POSTS},
        "cumulative_normal": {"input_tokens": caps.cumulative_input_tokens,
                              "output_tokens": caps.cumulative_output_tokens},
        "usd_ceiling": USD_CEILING,
        "price_basis": "conservative_ceiling_not_vendor_quote（R0 已签合同）",
        "created_utc": S.utc_now(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    C.write_json(ctx, APPROVAL_REL, payload)
    return path


def build_candidate(ctx: C.CampaignContext,
                    repo_root: Path = REPO_ROOT) -> dict[str, Any]:
    ensure_budget_approval(ctx)
    facts = collect_facts(ctx, repo_root=repo_root)
    problems = facts.pop("problems")
    if problems:
        raise LockError(refusal_code(problems), problems)
    doc = {
        "schema": LOCK_SCHEMA,
        "campaign_id": ctx.campaign_id,
        "created_utc": S.utc_now(),
        "plan_sha256": ctx.plan_sha256,
        "consent_sha256": ctx.consent_sha256,
        "facts": facts,
    }
    doc["lock_sha256"] = core_sha256(doc)
    path = C.write_json(ctx, f"contract/{CANDIDATE_FILENAME}", doc)
    return {"path": str(path), "lock_sha256": doc["lock_sha256"],
            "implementation_sha256": facts["code"]["implementation_sha256"],
            "worst_case_total_usd": facts["budget_price"]["worst_case_total_usd"]}


def signature_document(*, role: str, verdict: str, binding_sha256: str,
                       signed_by: str, evidence_sha256: "Mapping[str, Any]",
                       notes: "list[str] | None" = None) -> dict[str, Any]:
    return {"schema": SIGN_SCHEMA, "gate": G_I_GATE, "role": role,
            "verdict": verdict, "binding_sha256": binding_sha256,
            "signed_by": signed_by, "signed_at_utc": S.utc_now(),
            "evidence_sha256": dict(evidence_sha256),
            "notes": list(notes or [])}


def validate_signature(sig: Mapping[str, Any], role: str,
                       expected_sha: str) -> "list[str]":
    problems: "list[str]" = []
    if sig.get("schema") != SIGN_SCHEMA:
        problems.append(f"{role}: schema 不符")
    if sig.get("gate") != G_I_GATE:
        problems.append(f"{role}: gate != {G_I_GATE}")
    if sig.get("role") != role:
        problems.append(f"{role}: role 字段不符")
    if sig.get("verdict") != "APPROVE":
        problems.append(f"{role}: verdict != APPROVE（{sig.get('verdict')}）")
    if sig.get("binding_sha256") != expected_sha:
        problems.append(f"{role}: binding_sha256 与 LOCK 不一致")
    if not str(sig.get("signed_by") or "").strip():
        problems.append(f"{role}: 缺 signed_by")
    if not str(sig.get("signed_at_utc") or "").strip():
        problems.append(f"{role}: 缺 signed_at_utc")
    if not isinstance(sig.get("evidence_sha256"), Mapping):
        problems.append(f"{role}: 缺 evidence_sha256")
    return problems


def signature_evidence_problems(sig: Mapping[str, Any], role: str,
                                facts: Mapping[str, Any]) -> "list[str]":
    """签名声明绑定的证据 hash 必须与候选 ``g_i.evidence`` 逐字相同。

    reviewer MEDIUM-3：只校验「是 Mapping」等于没校验——签名可以声明一组与
    候选无关的证据 hash 而仍通过。此处做等值比对，任一差异即列问题。
    """
    declared = sig.get("evidence_sha256")
    actual = (facts.get("g_i") or {}).get("evidence")
    if canonical_bytes(declared) != canonical_bytes(actual):
        return [f"{role}: 签名声明证据 hash 与候选 g_i.evidence 不同"]
    return []


def load_signatures(campaign: Path) -> "dict[str, Any]":
    out: "dict[str, Any]" = {}
    for role in SIG_ROLES:
        path = campaign / SIG_DIR / f"{role}.json"
        if path.is_file():
            doc = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(doc, Mapping):
                raise LockError("BLOCKED_APPROVAL",
                                [f"{role} 签名不是 JSON 对象"])
            out[role] = doc
    return out


def finalize(ctx: C.CampaignContext,
             repo_root: Path = REPO_ROOT) -> dict[str, Any]:
    """汇总候选 + 双签 → ``contract/final_lock.json``（签名前候选必须存在）。"""
    campaign = Path(ctx.root)
    cand_path = campaign / "contract" / CANDIDATE_FILENAME
    if not cand_path.is_file():
        raise LockError("BLOCKED_APPROVAL", ["缺 lock_candidate（先 lock --candidate）"])
    candidate = json.loads(cand_path.read_text(encoding="utf-8"))
    expected = core_sha256(candidate)
    if expected != candidate.get("lock_sha256"):
        raise LockError("LOCK_HASH_MISMATCH", ["候选自哈希不符（候选被改写）"])
    # 签名前再算一次事实：候选时间点到签字之间的漂移必须暴露。
    fresh = collect_facts(ctx, repo_root=repo_root)
    fresh_problems = fresh.pop("problems")
    if fresh_problems:
        raise LockError(refusal_code(fresh_problems), fresh_problems)
    if canonical_bytes(fresh) != canonical_bytes(candidate["facts"]):
        raise LockError("LOCK_FACTS_CHANGED",
                        ["候选落盘后现场事实已变（重新 --candidate 并重签）"])
    sigs = load_signatures(campaign)
    problems: "list[str]" = []
    for role in SIG_ROLES:
        if role not in sigs:
            problems.append(f"缺 {role} 签名（LOCK_PENDING_SIGNATURES）")
        else:
            problems.extend(validate_signature(sigs[role], role, expected))
            problems.extend(signature_evidence_problems(sigs[role], role, fresh))
    signers = [s.get("signed_by") for s in sigs.values() if isinstance(s, Mapping)]
    if len(signers) == 2 and signers[0] == signers[1]:
        problems.append("reviewer/verifier 签名人相同（须独立两遍）")
    if problems:
        raise LockError("BLOCKED_APPROVAL", problems)
    doc = {k: v for k, v in candidate.items() if k != "signatures"}
    doc["signatures"] = {role: sigs[role] for role in SIG_ROLES}
    doc["lock_sha256"] = core_sha256(doc)
    if doc["lock_sha256"] != expected:
        raise LockError("LOCK_HASH_MISMATCH", ["finalize 改动了 core（禁止）"])
    path = C.write_json(ctx, f"contract/{LOCK_FILENAME}", doc)
    return {"path": str(path), "lock_sha256": doc["lock_sha256"],
            "signers": {r: sigs[r]["signed_by"] for r in SIG_ROLES}}


def load_lock(campaign: Path) -> dict[str, Any]:
    path = campaign / "contract" / LOCK_FILENAME
    if not path.is_file():
        raise LockError("BLOCKED_APPROVAL", [f"LOCK 不存在：{path}"])
    return json.loads(path.read_text(encoding="utf-8"))


def verify_lock(ctx: C.CampaignContext,
                repo_root: Path = REPO_ROOT) -> dict[str, Any]:
    """重算事实并核对自哈希/双签；返回判件（不抛，调用方决定闸门）。"""
    campaign = Path(ctx.root)
    problems: "list[str]" = []
    doc = load_lock(campaign)
    expected = core_sha256(doc)
    if doc.get("lock_sha256") != expected:
        problems.append("LOCK 自哈希不符（文档被改写）")
    if doc.get("plan_sha256") != ctx.plan_sha256:
        problems.append("计划文件 hash 与 LOCK 不一致")
    if doc.get("consent_sha256") != ctx.consent_sha256:
        problems.append("授权（consent）hash 与 LOCK 不一致")
    fresh = collect_facts(ctx, repo_root=repo_root)
    problems.extend(fresh.pop("problems", []))
    if canonical_bytes(fresh) != canonical_bytes(doc.get("facts")):
        problems.append("绑定事实已变（代码/配置/任务/证据/价格/旧根）")
    for role in SIG_ROLES:
        sig = (doc.get("signatures") or {}).get(role)
        if not isinstance(sig, Mapping):
            problems.append(f"缺 {role} 签名")
        else:
            problems.extend(validate_signature(sig, role, expected))
            problems.extend(signature_evidence_problems(sig, role, fresh))
    return {"schema": f"{S.PREFIX}.lock_verification.v1",
            "campaign_id": ctx.campaign_id,
            "checked_utc": S.utc_now(),
            "lock_sha256": doc.get("lock_sha256"),
            "recomputed_core_sha256": expected,
            "problems": problems,
            "verdict": "PASS" if not problems else "FAIL"}


def assert_paid_allowed(ctx: C.CampaignContext,
                        repo_root: Path = REPO_ROOT) -> dict[str, Any]:
    """付费入口唯一闸：任何问题 ⇒ LockError（带 §9.2 阻断类），零触网。"""
    try:
        report = verify_lock(ctx, repo_root=repo_root)
    except LockError:
        raise
    if report["problems"]:
        joined = " ".join(report["problems"])
        code = ("LOCK_PRICE_UNKNOWN" if "价格" in joined else "BLOCKED_APPROVAL")
        if "自哈希" in joined or "事实已变" in joined:
            code = "LOCK_CHANGED"
        raise LockError(code, report["problems"])
    doc = load_lock(Path(ctx.root))
    price = doc["facts"]["budget_price"]
    if price["worst_case_total_usd"] > price["usd_ceiling"]:
        raise LockError("BLOCKED_PRICE", ["最坏费用超过货币帽"])
    return doc
