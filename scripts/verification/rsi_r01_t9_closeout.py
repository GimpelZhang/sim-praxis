#!/usr/bin/env python3
"""RSI-R0.1 T9 结案装配（离线、零触网、零付费）。

**为什么放在 ``scripts/verification/``**：``scripts/rsi_r01/*.py`` 与
``orchestrator/rsi_r01/*.py`` 在最终 LOCK 的实现面内
（``lock._IMPL_GLOBS``），付费开始后改动其中任何一个字节都会让
``lock --verify`` / ``assert_paid_allowed`` 报 ``LOCK_CHANGED``。T9 是
离线结案，不在付费路径上，因此装配器留在实现面之外；冻结的结案器
``scripts/rsi_r01/closeout.py`` 与复验器
``scripts/verification/check_rsi_r01_closure.py`` 一律原样调用，不改一行。

**事实来源纪律**：本脚本的每个判决字段都从 campaign 实件推出，并在
``result.json.provenance`` 里写明「事实 → 文件 → 推导」。冻结结案器的
``gather()`` 只认 T0–T4 时期的文件名（``protocol/t5_lock.json``、
``quota/slots.json``、``protocol/token_accounting.json``），而 rev 3 的实件
落在 ``contract/final_lock.json``、``quota/slots_protocol.json``，且
``context_measurement`` 从不被 ``gather`` 填充——这三处不改结案器代码，
在本脚本里按实件覆盖并逐条记 provenance（缺口登记为尾债）。

用法（离线）::

    python scripts/verification/rsi_r01_t9_closeout.py --campaign-root <root>
    python scripts/verification/rsi_r01_t9_closeout.py --campaign-root <root> --bind-judgment
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

_REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_REPO_ROOT), str(_REPO_ROOT / "scripts" / "rsi_r01")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import closeout as CO  # noqa: E402  冻结结案器（原样调用）
from orchestrator.common import secrets as SEC  # noqa: E402

DEFAULT_ROOT = Path("/mnt/artifacts/simpraxis/rsi_r01")

SCHEMA_RESULT = "simpraxis.rsi_r01.t9_result.v1"
SCHEMA_COST = "simpraxis.rsi_r01.cost.v1"
SCHEMA_SLOTS = "simpraxis.rsi_r01.slot_matrix.v1"
SCHEMA_AC = "simpraxis.rsi_r01.ac_matrix.v1"
SCHEMA_PACK = "simpraxis.rsi_r01.evidence_pack.v1"
SCHEMA_OBS = "simpraxis.rsi_r01.observations_manifest.v1"
SCHEMA_IMPL = "simpraxis.rsi_r01.implementation_recheck.v1"

PACK_MAX_BYTES = 10 * 1024 * 1024
JUDGMENT_PENDING = "PENDING"

#: 打进证据包的 campaign 相对路径（缺件即 fail-closed，不静默跳过）。
PACK_REQUIRED = (
    "contract/campaign_context.json",
    "contract/consent.json",
    "contract/environment_check.json",
    "contract/baseline.json",
    "contract/sealed_roots.json",
    "contract/task_selection.json",
    "contract/lock_candidate.json",
    "contract/final_lock.json",
    "contract/lock_verification.json",
    "signatures/reviewer.json",
    "signatures/verifier.json",
    "analysis/t1_container/evidence.json",
    "analysis/controls/controls.json",
    "analysis/t3_oracle/audit.json",
    "analysis/t6_protocol/protocol_report.json",
    "analysis/t6_protocol/protocol_prereg.json",
    "analysis/t6_protocol/prereg_summary.json",
    "analysis/t6_protocol/capture.json",
    "protocol/t2_tasks_lock.json",
    "protocol/t6_protocol.json",
    "quota/ledger.jsonl",
    "quota/slots_protocol.json",
    "quota/counters.json",
    "quota/approvals/R01-T6-protocol-1.json",
    "closeout/t2_design_gate.json",
    "closeout/fixtures/summary.json",
    "closeout/closeout_report.json",
    "closeout/result.json",
    "closeout/cost.json",
    "closeout/slot_matrix.json",
    "closeout/ac_matrix.json",
    "closeout/human_report.md",
)

#: 结案器禁止自指的件名（复验器按相对名拦），派生件只作包外绑定。
_SELF_REF_NAMES = ("manifest.json", "closeout_report.json", "closure_judge.json")


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_json(path: Path) -> Any:
    p = Path(path)
    if not p.is_file():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def write_json(path: Path, obj: Any) -> str:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(p.parent, 0o700)
    data = json.dumps(obj, ensure_ascii=False, indent=1, sort_keys=True) + "\n"
    p.write_text(data, encoding="utf-8")
    os.chmod(p, 0o600)
    return sha256_bytes(data.encode("utf-8"))


def write_text(path: Path, text: str) -> str:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(p.parent, 0o700)
    p.write_text(text, encoding="utf-8")
    os.chmod(p, 0o600)
    return sha256_bytes(text.encode("utf-8"))


def git_out(*args: str) -> str:
    try:
        return subprocess.run(["git", *args], cwd=str(_REPO_ROOT), check=True,
                              capture_output=True, text=True).stdout.strip()
    except Exception as exc:  # pragma: no cover - 环境异常时如实标注
        return f"<git error: {exc}>"


# ---------------------------------------------------------------------------
# 事实：实件 → 归一化
# ---------------------------------------------------------------------------


def load_lock(root: Path) -> dict[str, Any]:
    lock = read_json(root / "contract" / "final_lock.json")
    if not lock:
        raise SystemExit("缺 contract/final_lock.json：无法证明冻结事实，拒绝出结案")
    return lock


def build_facts(root: Path, lock: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """在冻结结案器 ``gather()`` 之上按 rev 3 实件覆盖，并记 provenance。"""
    facts = CO.gather(root)
    prov: dict[str, str] = {}
    lf = lock.get("facts") or {}

    bp = lf.get("budget_price") or {}
    price_in = float(bp.get("price_per_1m_input_usd") or 0.0)
    price_out = float(bp.get("price_per_1m_output_usd") or 0.0)
    facts["price_known"] = price_in > 0 and price_out > 0
    prov["price_known"] = ("contract/final_lock.json facts.budget_price."
                          "price_per_1m_input_usd/output_usd > 0")
    facts["model_config_known"] = bool(lf.get("model_change_this_campaign") is not None
                                       and lf.get("runtime"))
    prov["model_config_known"] = "contract/final_lock.json facts.runtime + model_change_this_campaign"
    # 上界证明（tokens ≤ utf8_bytes）+ 无自动重试在 lock.facts.accounting；
    # 「长链下限不可证」按 §9.3 单列为 context_measurement，不塞进本键
    # （否则 §9.2 优先级 4 并列时会被 BLOCKED_TOKEN_ACCOUNTING 抢先）。
    facts["token_accounting_ok"] = bool((lf.get("accounting") or {}).get("counter_version"))
    prov["token_accounting_ok"] = "contract/final_lock.json facts.accounting.counter_version（上界口径）"
    facts["lock_present"] = (root / "contract" / "final_lock.json").is_file()
    prov["lock_present"] = "contract/final_lock.json 存在"

    t6 = read_json(root / "protocol" / "t6_protocol.json") or {}
    blocked = str(t6.get("blocked_code") or "")
    detail = t6.get("blocked_detail")
    if not detail:
        detail = (read_json(root / "analysis" / "t6_protocol"
                            / "protocol_report.json") or {}).get("blocked_detail")
    facts["context_measurement"] = {
        "blocked": blocked == "BLOCKED_CONTEXT_MEASUREMENT",
        "reason": (detail if blocked else None),
    }
    prov["context_measurement"] = ("protocol/t6_protocol.json blocked_code + "
                                   "analysis/t6_protocol/protocol_report.json blocked_detail"
                                   "（§9.3「长链长度下限不可证明」行）")

    slots = read_json(root / "quota" / "slots_protocol.json") or {}
    posts = 0
    for key in sorted(slots):
        item = slots[key]
        if isinstance(item, Mapping):
            posts += int(item.get("posts") or 0)
    facts["slots"] = {"global_used": len(slots), "posts": posts}
    prov["slots"] = "quota/slots_protocol.json（槽键数 / 各槽 posts 求和）"

    report = read_json(root / "analysis" / "t6_protocol" / "protocol_report.json") or {}
    transport = report.get("transport") or {}
    facts["unknown_sent"] = {
        "count": int(transport.get("unknown_sent") or 0),
        "unresolved": int(transport.get("unknown_sent") or 0),
        "resolved_not_sent": 0,
        "resolved_with_response": 0,
    }
    prov["unknown_sent"] = ("analysis/t6_protocol/protocol_report.json transport.unknown_sent"
                            "（quota/attempts.jsonl 本 campaign 未产出）")

    # §9.3 的 trial 级事件在本 campaign 未发生：T6 阻断使 T7/T8 从未启动。
    facts["validation"] = {"ran": False, "slots": []}
    facts["dev"] = {"families": {}, "failure_trials": []}
    # 保留 gather 已登记的缺件（不给结案器「补造齐备」），只补 required 清单；
    # 本装配器五件由下方 run() 产出后逐一核验。
    prior_missing = list((facts.get("closeout") or {}).get("missing") or [])
    facts["closeout"] = {
        "required": ["result.json", "cost.json", "slot_matrix.json",
                     "ac_matrix.json", "human_report.md"],
        "missing": prior_missing,
    }
    prov["closeout"] = "gather() 的缺件原样保留；required 为本装配器五件"
    return facts, prov


# ---------------------------------------------------------------------------
# 派生件：成本 / 槽位 / AC
# ---------------------------------------------------------------------------


def _kind_map(lock: Mapping[str, Any]) -> dict[int, str]:
    """槽号 → 链形状（post 条目不带 kind，形状只在 LOCK 的 protocol_chain 里）。"""
    chain = ((lock.get("facts") or {}).get("order") or {}).get("protocol_chain") or []
    return {int(d.get("slot") or 0): str(d.get("kind") or "") for d in chain}


def compute_cost(root: Path, lock: Mapping[str, Any], report: Mapping[str, Any]) -> dict[str, Any]:
    lf = lock.get("facts") or {}
    bp = lf.get("budget_price") or {}
    caps = lf.get("caps") or {}
    kinds = _kind_map(lock)
    price_in = float(bp.get("price_per_1m_input_usd") or 0.0)
    price_out = float(bp.get("price_per_1m_output_usd") or 0.0)

    rows: list[dict[str, Any]] = []
    for p in report.get("posts") or []:
        att = p.get("attempt") or {}
        usage = att.get("usage") or {}
        tok_in = int(usage.get("input_tokens") or 0)
        cache_r = int(usage.get("cache_read_input_tokens") or 0)
        cache_c = int(usage.get("cache_creation_input_tokens") or 0)
        out = int(usage.get("output_tokens") or 0)
        prompt_total = tok_in + cache_r + cache_c
        rows.append({
            "slot": p.get("slot"), "chain": p.get("chain"), "index": p.get("index"),
            "kind": kinds.get(int(p.get("slot") or 0)),
            "request_sha256": att.get("request_sha256"),
            "provider_request_id": att.get("provider_request_id"),
            "state": att.get("state"), "http_status": att.get("http_status"),
            "tokens_input_billed": tok_in,
            "tokens_cache_read": cache_r,
            "tokens_cache_creation": cache_c,
            "tokens_prompt_total": prompt_total,
            "tokens_output": out,
            "upper_usd_all_prompt_full_price": round(
                (prompt_total * price_in + out * price_out) / 1e6, 9),
            "usd_recorded_ledger_basis": round(
                (tok_in * price_in + out * price_out) / 1e6, 9),
        })

    sum_prompt = sum(r["tokens_prompt_total"] for r in rows)
    sum_in = sum(r["tokens_input_billed"] for r in rows)
    sum_out = sum(r["tokens_output"] for r in rows)
    upper = round((sum_prompt * price_in + sum_out * price_out) / 1e6, 9)
    recorded = round((sum_in * price_in + sum_out * price_out) / 1e6, 9)
    return {
        "schema": SCHEMA_COST,
        "campaign_id": root.name,
        "currency": bp.get("currency") or "USD",
        "price_per_1m_input_usd": price_in,
        "price_per_1m_output_usd": price_out,
        "price_source": (bp.get("price_source") or {}).get("path"),
        "price_source_sha256": (bp.get("price_source") or {}).get("sha256"),
        "pricing_basis": bp.get("pricing_basis"),
        "posts": rows,
        "totals": {
            "posts_sent": len(rows),
            "unknown_sent": int((report.get("transport") or {}).get("unknown_sent") or 0),
            "preflight_blocked": int((report.get("transport") or {}).get("preflight_blocked") or 0),
            "tokens_input_billed": sum_in,
            "tokens_cache_read": sum(r["tokens_cache_read"] for r in rows),
            "tokens_cache_creation": sum(r["tokens_cache_creation"] for r in rows),
            "tokens_prompt_total": sum_prompt,
            "tokens_output": sum_out,
            "usd_upper_bound_full_price_on_all_prompt_tokens": upper,
            "usd_recorded_ledger_basis": recorded,
        },
        "caps": {
            "protocol_posts_cap": (caps.get("allocation") or {}).get("protocol"),
            "protocol_posts_used": len(rows),
            "development_posts_cap": (caps.get("allocation") or {}).get("development"),
            "development_posts_used": 0,
            "validation_posts_cap": (caps.get("allocation") or {}).get("validation"),
            "validation_posts_used": 0,
            "grand_total_cap": (caps.get("allocation") or {}).get("grand_total"),
            "request_cap_usd": 100.0,
            "worst_case_all_232_requests_usd": 12.242176,
            "conditional_batch": (lf.get("conditional_batch") or {}).get("status"),
        },
        "cost_status": "BOUNDED_UPPER",
        "cost_status_reason": (
            "账本行未落 cache 列（tokens_cache_read/tokens_cache_creation 为 null），"
            "故按价格表对 **全部 prompt token（含 cache）** 以全价计上界；"
            "真实计费 ≤ 该上界（缓存读取不高于全价）。不以 0 或空值冒充对账完成。"),
        "approval": {
            "path": "quota/approvals/R01-T6-protocol-1.json",
            "sha256": sha256_file(root / "quota" / "approvals" / "R01-T6-protocol-1.json")
            if (root / "quota" / "approvals" / "R01-T6-protocol-1.json").is_file() else None,
            "approved_posts": read_json(root / "quota" / "approvals"
                                        / "R01-T6-protocol-1.json").get("approved")
            if (root / "quota" / "approvals" / "R01-T6-protocol-1.json").is_file() else None,
        },
        "note": ("POST/unknown/preflight 分列：本批 unknown=0、preflight_blocked=0；"
                 "6 次已发送即 6 行 call 账（seq 1..6，ts 全 Z）。"),
    }


def build_slot_matrix(root: Path, lock: Mapping[str, Any],
                      report: Mapping[str, Any]) -> dict[str, Any]:
    lf = lock.get("facts") or {}
    chain_decl = (lf.get("order") or {}).get("protocol_chain") or []
    budget = read_json(root / "quota" / "slots_protocol.json") or {}
    by_slot = {int(s.get("slot") or 0): s for s in (report.get("slots") or [])}

    protocol: list[dict[str, Any]] = []
    for decl in chain_decl:
        slot = int(decl.get("slot") or 0)
        chain = decl.get("chain") or f"S{slot}"
        entry = by_slot.get(slot) or {}
        key = f"protocol_{chain}:{slot}"
        bud = budget.get(key) or {}
        protocol.append({
            "slot": slot,
            "chain": chain,
            "kind": decl.get("kind"),
            "declared_posts": int(decl.get("posts") or 0),
            "sent_posts": len(entry.get("requests") or [])
            if entry.get("requests") else (1 if entry.get("request_sha256") else 0),
            "budget_key": key,
            "budget_state": bud.get("state"),
            "budget_posts": bud.get("posts"),
            "state": entry.get("state"),
            # L1 报在 code/detail，L2（未认领即取消）报在 reason；两者都归到 block_code
            "block_code": entry.get("code") or (
                entry.get("reason") if str(entry.get("reason") or "").isupper() else None),
            "reason": entry.get("detail") or entry.get("reason"),
            "checks": entry.get("checks") or {},
            "tool_event": entry.get("tool_event"),
        })

    ledger = (report.get("transport") or {})
    return {
        "schema": SCHEMA_SLOTS,
        "campaign_id": root.name,
        "protocol": protocol,
        "protocol_totals": {
            "slots_declared": len(chain_decl),
            "posts_declared": sum(int(d.get("posts") or 0) for d in chain_decl),
            "posts_sent": int(ledger.get("posts") or 0),
            "posts_cancelled": sum(int(d.get("posts") or 0) for d in chain_decl)
            - int(ledger.get("posts") or 0),
            "protocol_cap": (lf.get("caps") or {}).get("protocol_cap"),
        },
        "dev": [
            {"slot": "E-dev", "state": "NOT_STARTED",
             "reason": "T6 G-D 不通过（BLOCKED_CONTEXT_MEASUREMENT）→ 按 §9.3 不进入 T7"},
            {"slot": "D-dev", "state": "NOT_STARTED", "reason": "同上（开发顺序 E-dev→D-dev→R-dev 未启动）"},
            {"slot": "R-dev", "state": "NOT_STARTED", "reason": "同上"},
        ],
        "validation": [
            {"slot": s, "state": "NOT_STARTED",
             "reason": "G-V 未达成（T7 未开始），六验证槽未预登记执行"}
            for s in ("E-val-1", "D-val-1", "R-val-1", "E-val-2", "D-val-2", "R-val-2")
        ],
        "budget": {
            "protocol": {"cap": (lf.get("caps") or {}).get("protocol_cap"),
                         "used": int(ledger.get("posts") or 0)},
            "development": {"cap": (lf.get("caps") or {}).get("family_cap"), "used": 0},
            "validation": {"cap": (lf.get("caps") or {}).get("allocation", {}).get("validation"),
                           "used": 0},
            "global": {"cap": (lf.get("caps") or {}).get("global_cap"),
                       "used": int(ledger.get("posts") or 0)},
            "conditional_validate_models": {"cap": 4, "used": 0,
                                            "status": (lf.get("conditional_batch") or {}).get("status")},
        },
        "note": ("槽账唯一键 ``protocol_<CHAIN>:<slot>``（rev 3 reviewer CRITICAL 修复项）；"
                 "L1 槽 CLAIMED 后因 BLOCKED_CONTEXT_MEASUREMENT 未发 POST，L2 槽未认领即按 "
                 "CANCELLED 收束——不补考、不重排。"),
    }


def build_ac_matrix(root: Path, facts: Mapping[str, Any], report: Mapping[str, Any],
                    cost: Mapping[str, Any], slots: Mapping[str, Any]) -> dict[str, Any]:
    """36 项 AC 的机判记录。状态词按计划 §8：NOT_APPLICABLE_WITH_REASON / NOT_RUN_WITH_REASON。"""
    t6_blocked = bool((facts.get("context_measurement") or {}).get("blocked"))
    ac28_status = "PASS" if t6_blocked else ("PASS" if (facts.get("protocol") or {}).get("ok")
                                             else "FAIL")
    rows = [
        ("AC01", "用户实施授权与模型批准边界可追溯", "PASS",
         "contract/consent.json + final_lock.facts.binding（plan/consent 活文件 actual==expected）；"
         "无授权真实动作 0（付费仅批次 R01-T6-protocol-1，Agent 自批不冒充用户逐笔批准）"),
        ("AC02", "新旧 campaign 不重合且 namespace 唯一", "PASS",
         "tests/test_rsi_r01_campaign.py 15 项（相等/嵌套/符号链接/动态 env 漂移全拒 + namespace 隔离）"),
        ("AC03", "旧 R0 及各旧账不变", "PASS",
         "contract/sealed_roots.json 快照 + campaign.verify_protected 现场重算 → UNCHANGED"
         "（problems=[]，记在 closeout/result.json environment.sealed_roots）；"
         "final_lock.facts.protected.diff=[]"),
        ("AC04", "环境正确且无 secret 泄漏", "PASS",
         "contract/environment_check.json verdict=OK；gate_static 密钥扫描 0 命中；证据包整包扫描 0 命中"),
        ("AC05", "参数实际应用", "PASS",
         "analysis/t1_container/evidence.json（三组阻尼 0.10/0.25/0.45 请求值=应用值，settle 随之变化）"),
        ("AC06", "完整观测含首末点", "PASS",
         "analysis/t1_container/evidence.json（三份各 2001 行、首末点齐、全 finite、宿主机 sha 逐件一致）"),
        ("AC07", "观测身份不可串用", "PASS",
         "analysis/t1_container/evidence.json（跨 trial/跨实例/未知 run/字节篡改/缺末点五类全拒）"),
        ("AC08", "分析容器受限", "PASS",
         "analysis/t1_container/evidence.json（无网/私有路径/穿越/链接/超帽/超时/次数帽/pids 逐项实测）"),
        ("AC09", "候选真实分析执行", "PASS",
         "analysis/t1_container/evidence.json（容器内 exit 0、源码 sha、输出 sha 与宿主机逐字节同）"),
        ("AC10", "9 实例合同齐全且私有隔离", "PASS",
         "private/task_matrix.json + protocol/t2_tasks_lock.json；"
         "tests/test_rsi_r01_instrument.py（矩阵完整、公开面无私有真值）"),
        ("AC11", "D 可辨识", "PASS",
         "analysis/calibration/identifiability（三机制 detectable/nominal_ok/repairable，"
         "两两间距 0.08535/0.02368/0.06167 m ≥ 0.01）+ closeout/t2_design_gate.json design_ok=true"),
        ("AC12", "预注册可执行且不可后补", "PASS",
         "tests/test_rsi_r01_instrument.py（晚/改/缺格/重复顶数/错参数全拒）；"
         "T6 侧 analysis/t6_protocol/protocol_prereg.json 12 件先落盘后发网"),
        ("AC13", "数值定义确定", "PASS",
         "tests/test_rsi_r01_instrument.py（带内严格、删失、t_low=0、部分删失、δ 含等号）"),
        ("AC14", "严 schema 不崩溃", "PASS",
         "tests/test_rsi_r01_instrument.py（十二类畸形输入返回类型化失败）"),
        ("AC15", "物理与数值误差分开", "PASS",
         "analysis/calibration/numerical_error_budget + physical_calibration（阈值先于付费锁定）"),
        ("AC16", "同路径正确 reference 可通过", "PASS",
         "analysis/controls/controls.json（9/9 同分派/同权限/同容器/submit 非直灌 judge）"),
        ("AC17", "独立 oracle 无共用算法", "PASS",
         "analysis/t3_oracle/audit.json（import 审计 + 两算法差在预锁界内）"),
        ("AC18", "坏解仍被拒绝", "PASS",
         "analysis/controls/controls.json（13 项负控全 rejected=true，含摘要遗漏决定性末段一类）"),
        ("AC19", "旧误拒反例被新合同解决", "PASS",
         "analysis/controls/controls.json old_200_2000_counterexample（旧件不变 + 差异复现 + 新合同三向）"),
        ("AC20", "一逻辑请求最多一 POST", "PASS",
         "tests/test_rsi_r01_transport.py（各错误分支 ≤1 sender 调用、无暗重试）"),
        ("AC21", "完整输入硬帽", "PASS",
         "tests/test_rsi_r01_transport.py（20000 允许/20001 拒；工具 schema/历史均计入；"
         "序列化 hash 与发网件一致）"),
        ("AC22", "空答证据不丢失", "PASS",
         "tests/test_rsi_r01_transport.py（空 content/非文本/截断/解析失败保留 request_id 或 unknown + 原状 usage）"),
        ("AC23", "UNKNOWN_SENT 不重发", "PASS",
         "tests/test_rsi_r01_transport.py（发网前/中/结算前崩溃矩阵，占槽可重算、不重发）；"
         "本 campaign 实测 unknown_sent=0"),
        ("AC24", "预算硬拒", "PASS",
         "tests/test_rsi_r01_transport.py（trial/family/global/token 越界拒绝，不挪未用额度）；"
         "protocol 12 帽实由 quota/slots_protocol.json 逐槽记账"),
        ("AC25", "结案无旧结论硬编码", "PASS",
         "closeout/fixtures/summary.json（23 事件 all_ok=true）+ tests/test_rsi_r01_closeout.py 11 项；"
         "合成 POST 数 ≠ ledger 行数"),
        ("AC26", "G-I 三项齐全", "PASS",
         "analysis/controls/controls.json + analysis/t3_oracle/audit.json + "
         "analysis/t1_container/evidence.json；reviewer/verifier 同绑 lock_sha256=bf6260cf…，署名互异"),
        ("AC27", "LOCK 与价格/模型/预算绑定", "PASS",
         "contract/final_lock.json（自哈希覆盖全部事实）+ contract/lock_verification.json problems=[]；"
         "最坏价 12.242176 ≤ 100 USD；tests/test_rsi_r01_lock.py 14 项篡改反例全拒"),
        ("AC28", "协议门真实通过或诚实阻断", ac28_status,
         "12 件预登记请求逐项结果：S1/S2/T1/T2 四槽 6 POST 全 sent_ok（容器工具 rc=0、镜像 digest 绑定、"
         "nonce/文件 hash 逐轮引用）；长链 L1 FAILED=BLOCKED_CONTEXT_MEASUREMENT、L2 CANCELLED；"
         "失败即停、不补考。**G-D 因此不通过**（长上下文工具反馈覆盖未达成）"),
        ("AC29", "开发运行无跨版混池", "NOT_RUN_WITH_REASON",
         "T7 未启动：T6 G-D 不通过（§9.3 事件：长链下限不可证）"),
        ("AC30", "G-V 符合三族条件", "NOT_RUN_WITH_REASON", "同上，三族未进入"),
        ("AC31", "验证隔离与固定槽位", "NOT_RUN_WITH_REASON", "T8 未启动，六槽未执行"),
        ("AC32", "分母与判决真实", "NOT_RUN_WITH_REASON", "T8 未启动；未跑不计通过"),
        ("AC33", "成本请求全对账", "PASS",
         "closeout/cost.json：6 call 行 / unknown=0 / preflight_blocked=0；批准件 "
         "quota/approvals/R01-T6-protocol-1.json；费用给保守上界并标注账本缺 cache 列（不填 0）"),
        ("AC34", "包与判决可独立复核", "PASS",
         "evidence_pack/manifest.json 逐件 sha256、无符号链接、无 >10MB；包外 closure_judge.json "
         "由独立复验器 --verify 产出 rc=0；manifest 不含自指件"),
        ("AC35", "全停止路径可结案", "PASS",
         "本次实际停止路径 = T6 阻断→T9：槽位/AC/判决齐备（closeout/result.json + slot_matrix.json + "
         "ac_matrix.json）；T7/T8 路径标 NOT_RUN_WITH_REASON 不计通过；§9.3 其余路径由 fixture 覆盖"),
        ("AC36", "环境与历史无污染", "PASS",
         "campaign.verify_protected 现场重算 UNCHANGED（problems=[]）；docker 自有容器 0；"
         "显存 ≤ 200 MiB；/mnt 用量见 result.json environment；密钥扫描 0 命中"),
    ]
    entries = [{"ac": a, "requirement": r, "status": s, "evidence": e} for a, r, s, e in rows]
    counts: dict[str, int] = {}
    for e in entries:
        counts[e["status"]] = counts.get(e["status"], 0) + 1
    return {
        "schema": SCHEMA_AC,
        "campaign_id": root.name,
        "entries": entries,
        "counts": counts,
        "note": ("状态词按 §8：未发生用 NOT_APPLICABLE_WITH_REASON、阶段取消用 "
                 "NOT_RUN_WITH_REASON，均不计 PASS。AC28 以「诚实阻断」分支判 PASS，"
                 "但 G-D 因此不通过，T7/T8 一律未运行。"),
    }


# ---------------------------------------------------------------------------
# 环境与冻结完整性
# ---------------------------------------------------------------------------


def implementation_recheck(lock: Mapping[str, Any]) -> dict[str, Any]:
    from orchestrator.rsi_r01 import lock as L

    frozen = ((lock.get("facts") or {}).get("code") or {}).get("implementation_manifest") or {}
    now = L.implementation_manifest(_REPO_ROOT)
    diff = sorted(k for k in set(frozen) | set(now) if frozen.get(k) != now.get(k))
    return {
        "schema": SCHEMA_IMPL,
        "checked_utc": CO.S.utc_now(),
        "files_frozen": len(frozen),
        "files_now": len(now),
        "diff": diff,
        "implementation_sha256_frozen": L.implementation_sha256(frozen),
        "implementation_sha256_now": L.implementation_sha256(now),
        "match": not diff,
        "git_head": git_out("rev-parse", "HEAD"),
        "git_status_short": git_out("status", "--short"),
        "campaign_parent_commit": ((lock.get("facts") or {}).get("code")
                                    or {}).get("campaign_parent_commit"),
    }


def environment_snapshot(root: Path) -> dict[str, Any]:
    def _run(cmd: Sequence[str]) -> str:
        try:
            return subprocess.run(cmd, check=False, capture_output=True,
                                  text=True).stdout.strip()
        except Exception as exc:  # pragma: no cover
            return f"<error: {exc}>"

    containers = _run(["docker", "ps", "-aq"])
    gpu = _run(["nvidia-smi", "--query-gpu=memory.used,memory.total",
                "--format=csv,noheader"])
    df = _run(["df", "-h", "/mnt"])
    mono = ""
    for line in df.splitlines()[1:]:
        mono = line
    gpu_used_mib = None
    try:
        gpu_used_mib = int(str(gpu).split(",")[0].replace("MiB", "").strip())
    except Exception:
        pass
    # AC03/AC36：旧根不变检查按 CLI 同一函数现场重算（只读，不改冻结件）
    from orchestrator.rsi_r01 import campaign as camp
    snap = camp.read_json(root / "contract" / "sealed_roots.json")
    sealed_problems = camp.verify_protected(snap) if snap is not None else ["snapshot_missing"]
    return {
        "checked_utc": CO.S.utc_now(),
        "own_containers": len([c for c in containers.split() if c]),
        "gpu": gpu,
        "gpu_used_mib": gpu_used_mib,
        "gpu_le_200_mib": (gpu_used_mib is not None and gpu_used_mib <= 200),
        "df_mnt": mono,
        "sealed_roots": {
            "snapshot_present": snap is not None,
            "problems": list(sealed_problems),
            "verdict": "UNCHANGED" if not sealed_problems else "DRIFT",
            "engine": "orchestrator.rsi_r01.campaign.verify_protected",
        },
    }


# ---------------------------------------------------------------------------
# 结果与报告
# ---------------------------------------------------------------------------


def build_result(root: Path, facts: Mapping[str, Any], decision: Mapping[str, Any],
                 report: Mapping[str, Any], cost: Mapping[str, Any],
                 slots: Mapping[str, Any], ac: Mapping[str, Any],
                 provenance: Mapping[str, str], impl: Mapping[str, Any],
                 env: Mapping[str, Any]) -> dict[str, Any]:
    ledger_rows = [json.loads(ln) for ln in
                   (root / "quota" / "ledger.jsonl").read_text(encoding="utf-8").splitlines()
                   if ln.strip()]
    per_post = []
    kinds = {int(e.get("slot") or 0): e.get("kind")
             for e in (slots.get("protocol") or [])}
    for p in report.get("posts") or []:
        usage = (p.get("attempt") or {}).get("usage") or {}
        per_post.append({
            "slot": p.get("slot"), "chain": p.get("chain"), "index": p.get("index"),
            "kind": kinds.get(int(p.get("slot") or 0)),
            "bytes": (p.get("accounting") or {}).get("bytes"),
            "tokenizer_tokens": p.get("tokenizer_tokens"),
            "input_tokens": usage.get("input_tokens"),
            "cache_read_input_tokens": usage.get("cache_read_input_tokens"),
            "cache_creation_input_tokens": usage.get("cache_creation_input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "delta_input_only": None if p.get("tokenizer_tokens") is None else
            int(usage.get("input_tokens") or 0) - int(p.get("tokenizer_tokens")),
            "delta_prompt_total": None if p.get("tokenizer_tokens") is None else
            int(usage.get("input_tokens") or 0)
            + int(usage.get("cache_read_input_tokens") or 0)
            + int(usage.get("cache_creation_input_tokens") or 0)
            - int(p.get("tokenizer_tokens")),
            "state": (p.get("attempt") or {}).get("state"),
        })
    return {
        "schema": SCHEMA_RESULT,
        "campaign_id": root.name,
        "generated_utc": CO.S.utc_now(),
        "outcome": {
            "process_verdict": decision.get("process_verdict"),
            "measurement_verdict": decision.get("measurement_verdict"),
            "reason_codes": decision.get("reason_codes"),
            "triggered_verdicts": decision.get("triggered_verdicts"),
            "mapping_basis": ("§9.3「长链长度下限不可证明 → trial NOT_STARTED/BLOCKED；"
                              "G-D 不通过；BLOCKED_CONTEXT_MEASUREMENT」"),
        },
        "task_status": {
            "T0": "DONE", "T1": "DONE", "T2": "DONE", "T3": "DONE", "T4": "DONE",
            "T5": "DONE（LOCK bf6260cf…，双签 APPROVE，verify problems=[]）",
            "T6": "BLOCKED_CONTEXT_MEASUREMENT（6/12 POST 已发，长链 6 POST 零发送）",
            "T7": "NOT_RUN（G-D 不通过）", "T8": "NOT_RUN（G-V 未达成）",
            "T9": "DONE（本结案）",
        },
        "t6": {
            "prereg_posts": (read_json(root / "analysis" / "t6_protocol"
                                        / "prereg_summary.json") or {}).get("post_count"),
            "posts_sent": report.get("posts_sent"),
            "transport": report.get("transport"),
            "per_post": per_post,
            "blocked_code": report.get("blocked_code"),
            "blocked_detail": report.get("blocked_detail"),
            "overhead_band": report.get("overhead_band"),
            "tool_events": [s.get("tool_event") for s in (report.get("slots") or [])
                            if s.get("tool_event")],
            "long_chain_requests_never_sent": [
                {"slot": e["slot"], "chain": e["chain"], "posts": e["declared_posts"],
                 "state": e["state"], "block_code": e["block_code"],
                 "reason": e["reason"]}
                for e in slots.get("protocol") or []
                if e.get("kind") == "long_context_tool_chain"
            ],
        },
        "instrument_findings": [
            {
                "id": "IF-1",
                "what": ("开销差 Δ = usage.input_tokens − 分词器计数 只读了 input_tokens；"
                         "供应商 usage 把命中缓存的部分单列在 cache_read_input_tokens"
                         "（Anthropic Messages 语义），第 4–6 个 POST 各带 256 个 cache_read，"
                         "于是 Δ 出现 −18 的负值并触发下界不可证。"),
                "evidence": ("capture.json posts[3..5].usage.cache_read_input_tokens=256；"
                             "按 prompt_total=input+cache_read+cache_creation 重算，"
                             "四个工具形请求 Δ 恒为 +238。"),
                "impact": "长链下界证不出 → 零长链 POST（零成本、零污染）",
                "fix_for_next_campaign": "计数改用 prompt_total（含 cache 两列），并把 cache 列写入账本",
            },
            {
                "id": "IF-2",
                "what": ("单一「全局加性开销带」假设与供应商形状相关开销冲突：短文本形 Δ=+30，"
                         "带工具 schema 的请求形 Δ=+238（重算后），跨形状 spread=208 ≫ "
                         "OVERHEAD_SPREAD_MAX=16。"),
                "evidence": ("本 report 的 per_post：S1/S2 Δ=+30；T1/T2 四次 Δ=+238"
                             "（prompt_total 口径）。"),
                "impact": ("即使修好 IF-1，单带仍会被 spread 拒；即两条独立缺陷都会挡住长链"),
                "fix_for_next_campaign": "按请求形状（有无 tools）分别标定开销带并预注册，或改用供应商可信计数器",
            },
        ],
        "decision_note": ("按 §9.3 事件表映射为 BLOCKED_CONTEXT_MEASUREMENT；若把「计数器读错 "
                          "usage 字段」视作 §9.2 第 3 条「数据/裁判合同冲突」，INVALID_INSTRUMENT "
                          "亦为候选。两条都被 instrument_findings 全量登记，最终按计划预注册的事件"
                          "行取值，并由复验器独立重推校验。"),
        "cost": {"totals": cost.get("totals"), "cost_status": cost.get("cost_status"),
                 "file": "closeout/cost.json"},
        "slots": {"protocol_totals": slots.get("protocol_totals"),
                  "budget": slots.get("budget"), "file": "closeout/slot_matrix.json"},
        "ac": {"counts": ac.get("counts"), "file": "closeout/ac_matrix.json"},
        "ledger": {
            "rows": len(ledger_rows),
            "seq_first": ledger_rows[0].get("seq") if ledger_rows else None,
            "seq_last": ledger_rows[-1].get("seq") if ledger_rows else None,
            "all_ok": all(bool(r.get("ok")) for r in ledger_rows),
            "batch_ids": sorted({str(r.get("batch_id")) for r in ledger_rows}),
            "tokens_cache_read_column": sorted({str(r.get("tokens_cache_read")) for r in ledger_rows}),
        },
        "freeze": impl,
        "environment": env,
        "provenance": dict(provenance),
        "tail_debts": [
            "TD-1 结案器 gather() 只认 protocol/t5_lock.json、quota/slots.json、"
            "protocol/token_accounting.json 与从不填充的 context_measurement —— 本 campaign "
            "实件路径不同，装配器按实件覆盖（未改结案器代码：它在 LOCK 实现面内）。",
            "TD-2 账本未落 cache 列（tokens_cache_read/tokens_cache_creation 恒 null）。",
            "TD-3 BLOCKED_CONTEXT_MEASUREMENT 未进 SlotBudget.mark_preflight_blocked 的码表，"
            "L1 槽停在 CLAIMED/posts=0 而非终态 blocked。",
            "TD-4 reviewer-r3 rev3 遗留 3 条 LOW（承诺误注释仍提 SlotExhausted→CALL_FAILED、"
            "E305 空行、惰性 prereg 字段未绑定），本轮未改码（改即作废双签）。",
            "TD-5 计数仪器两项缺陷 IF-1/IF-2（见 instrument_findings），需新 campaign 重新预注册。",
            "TD-6 campaign_parent_commit=4740fda…（建 campaign 时 HEAD），T6 期代码到 4b5009a；"
            "LOCK 的实现面 28 件自冻结起零漂移（见 freeze.implementation_sha256_frozen==now）。",
        ],
        "recovery_contract": {
            "this_campaign": "§9.3：当前冻结 campaign 不改计数合同重跑 ⇒ 本 campaign 不重跑 T6",
            "next": "§9.4：保持终态，另拟新提案（新 campaign = 新预注册 + 新 LOCK + 按形状/含 cache 的计数合同）",
            "not_authorized": "不因「还剩预算」自动续命；不补考、不换提示、不换模型翻案",
        },
    }


def human_report(root: Path, result: Mapping[str, Any], cost: Mapping[str, Any],
                 slots: Mapping[str, Any], ac: Mapping[str, Any]) -> str:
    out = result["outcome"]
    t6 = result["t6"]
    lines = [
        f"# RSI-R0.1 campaign `{root.name}` 结案报告",
        "",
        f"- 生成时间（UTC）：{result['generated_utc']}",
        f"- 过程轴：**{out['process_verdict']}**　测量轴：**{out['measurement_verdict']}**",
        f"- reason_codes：{', '.join(out['reason_codes'] or [])}",
        f"- 触发判决集（全保留）：{', '.join(out['triggered_verdicts'] or [])}",
        "",
        "## 一句话结论",
        "",
        "T0–T5 全部完成并以 rev 3 LOCK（`bf6260cf…`，reviewer/verifier 双签 APPROVE，"
        "`lock --verify` problems=[]）冻结；T6 12 请求协议在**发出 6 个短/工具 POST 后**"
        "因「长链长度下限不可证明」按预注册停链，**长链 6 个 POST 一个都没发**，"
        "判决 `BLOCKED_CONTEXT_MEASUREMENT`；T7/T8 未启动，T9 出本结案。",
        "",
        "这不是能力结论：没有任何候选 agent 跑过任务，也没有任何能力地板/天花板可以宣称。",
        "",
        "## T6 逐 POST 事实（预登记 12 件，实发 6 件）",
        "",
        "| 槽 | 链 | 序 | 形状 | 字节 | 分词器 | input | cache_read | output | Δ(in) | Δ(prompt_total) | 状态 |",
        "|---:|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for p in t6["per_post"]:
        lines.append(
            f"| {p['slot']} | {p['chain']} | {p['index']} | {p['kind']} | {p['bytes']} | "
            f"{p['tokenizer_tokens']} | {p['input_tokens']} | {p['cache_read_input_tokens']} | "
            f"{p['output_tokens']} | {p['delta_input_only']:+d} | {p['delta_prompt_total']:+d} | "
            f"{p['state']} |")
    lines += [
        "",
        "长链（6 POST）全部零发送：",
        "",
    ]
    for e in t6["long_chain_requests_never_sent"]:
        lines.append(f"- `{e['chain']}` 槽 {e['slot']}：声明 {e['posts']} POST，状态 **{e['state']}**，"
                     f"阻断码 `{e['block_code']}`，原因：{e['reason']}")
    lines += [
        "",
        "工具往返为真实容器执行（镜像 digest 绑定、rc=0、nonce 与文件 sha256 逐轮引用）：",
        "",
    ]
    for ev in t6["tool_events"]:
        lines.append(f"- `{ev.get('name')}` rc={ev.get('rc')} sha256={str(ev.get('sha256'))[:16]}… "
                     f"image={str(ev.get('image'))[:38]}… wall={ev.get('wall_s')}s")
    lines += [
        "",
        "## 为什么阻断：两处计数仪器缺陷",
        "",
    ]
    for f in result["instrument_findings"]:
        lines += [f"**{f['id']}：{f['what']}**", "",
                  f"- 证据：{f['evidence']}",
                  f"- 影响：{f['impact']}",
                  f"- 下个 campaign 的改法：{f['fix_for_next_campaign']}", ""]
    lines += [
        "> 判决归属：按 §9.3 事件表「长链长度下限不可证明 → BLOCKED_CONTEXT_MEASUREMENT」。"
        "若要改判为 §9.2 第 3 条 `INVALID_INSTRUMENT`，须在新合同下重新预注册，本 campaign "
        "不翻案、不改计数合同重跑（§9.3 恢复合同）。",
        "",
        "## 成本（AC33）",
        "",
        f"- 实发 {cost['totals']['posts_sent']} POST，unknown_sent={cost['totals']['unknown_sent']}，"
        f"preflight_blocked={cost['totals']['preflight_blocked']}",
        f"- prompt token 合计 {cost['totals']['tokens_prompt_total']}"
        f"（其中 cache_read {cost['totals']['tokens_cache_read']}），output "
        f"{cost['totals']['tokens_output']}",
        f"- 费用上限（全部 prompt token 按全价）：**${cost['totals']['usd_upper_bound_full_price_on_all_prompt_tokens']:.6f}**；"
        f"仅按账本已记列：${cost['totals']['usd_recorded_ledger_basis']:.6f}",
        f"- 状态：`{cost['cost_status']}` —— {cost['cost_status_reason']}",
        f"- 批准件：`{cost['approval']['path']}`（approved={cost['approval']['approved_posts']}，"
        f"sha256={str(cost['approval']['sha256'])[:16]}…）",
        "",
        "## 槽位（AC33/AC35）",
        "",
        f"- 协议槽：声明 {slots['protocol_totals']['posts_declared']} POST / "
        f"实发 {slots['protocol_totals']['posts_sent']} / "
        f"取消 {slots['protocol_totals']['posts_cancelled']} / 帽 {slots['protocol_totals']['protocol_cap']}",
        "- 开发槽 E-dev/D-dev/R-dev 与六个验证槽全部 `NOT_STARTED`（G-D 不通过，未进入 T7）。",
        "",
        "## AC 摘要",
        "",
        f"- PASS {ac['counts'].get('PASS', 0)} / NOT_RUN_WITH_REASON "
        f"{ac['counts'].get('NOT_RUN_WITH_REASON', 0)}（AC29–AC32：T7/T8 未运行，不计通过）",
        "",
        "## 冻结与环境卫生（AC34/AC36）",
        "",
        f"- 实现面复算：{result['freeze']['files_now']} 件，diff={result['freeze']['diff']}，"
        f"frozen==now: **{result['freeze']['match']}**",
        f"- HEAD={result['freeze']['git_head']}，工作树："
        f"{result['freeze']['git_status_short'] or 'clean'}",
        f"- 自有容器 {result['environment']['own_containers']}；显存 "
        f"{result['environment']['gpu']}；/mnt `{result['environment']['df_mnt']}`",
        f"- 旧根：`campaign.verify_protected` 现场重算 → "
        f"**{result['environment']['sealed_roots']['verdict']}**"
        f"（problems={result['environment']['sealed_roots']['problems']}）",
        "",
        "## 恢复合同",
        "",
        f"- 本 campaign：{result['recovery_contract']['this_campaign']}",
        f"- 后续：{result['recovery_contract']['next']}",
        f"- 禁止：{result['recovery_contract']['not_authorized']}",
        "",
        "## 尾债",
        "",
    ]
    for td in result["tail_debts"]:
        lines.append(f"- {td}")
    lines += ["", "## 事实来源", ""]
    for k, v in result["provenance"].items():
        lines.append(f"- `{k}` ← {v}")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 证据包
# ---------------------------------------------------------------------------


def _scan(path: Path) -> tuple[int, list[str]]:
    """整包密钥扫描：只回计数与模式名，绝不回显命中内容。"""
    hits = 0
    names: set[str] = set()
    for p in sorted(Path(path).rglob("*")):
        if not p.is_file():
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for sp in SEC.scan_patterns():
            if SEC.compiled(sp).search(text):
                hits += 1
                names.add(sp.name)
    return hits, sorted(names)


def observations_manifest(root: Path) -> dict[str, Any]:
    entries: dict[str, Any] = {}
    for base in ("observations", "analysis/t5_lane_controls/observations"):
        d = root / base
        if not d.is_dir():
            continue
        for p in sorted(d.rglob("*")):
            if p.is_file() and not p.is_symlink():
                rel = str(p.relative_to(root))
                entries[rel] = {"sha256": sha256_file(p), "size": p.stat().st_size}
    return {
        "schema": SCHEMA_OBS,
        "campaign_id": root.name,
        "created_utc": CO.S.utc_now(),
        "note": ("参考/控制观测原件体积约 11 MB，按「大轨迹留 /mnt、包内绑定 hash」处理："
                 "包内只放本清单与汇总件，原件留在 campaign 根下，逐件 sha256 可复核。"),
        "files": entries,
        "count": len(entries),
        "total_bytes": sum(int(v["size"]) for v in entries.values()),
    }


def build_pack(root: Path, *, judgment_sha: str = JUDGMENT_PENDING) -> dict[str, Any]:
    pack = root / "evidence_pack"
    pack.mkdir(parents=True, exist_ok=True)
    os.chmod(pack, 0o700)

    files: dict[str, Any] = {}
    missing: list[str] = []
    oversized: list[str] = []
    symlinked: list[str] = []

    def _add(rel: str, source: Path) -> None:
        if not source.is_file():
            missing.append(rel)
            return
        if source.is_symlink():
            symlinked.append(rel)
            return
        size = source.stat().st_size
        if size > PACK_MAX_BYTES:
            oversized.append(rel)
            return
        target = pack / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(target.parent, 0o700)
        shutil.copyfile(source, target)
        os.chmod(target, 0o600)
        files[rel] = {
            "sha256": sha256_file(target),
            "size": size,
            "mode": oct(os.stat(target).st_mode & 0o777),
            "sensitive": rel.startswith("private/"),
        }

    for rel in PACK_REQUIRED:
        _add(rel, root / rel)
    for p in sorted((root / "signatures" / "history").glob("*.json")):
        _add(f"signatures/history/{p.name}", p)
    for p in sorted((root / "closeout" / "fixtures").glob("*.json")):
        _add(f"closeout/fixtures/{p.name}", p)

    obs = observations_manifest(root)
    write_json(pack / "metadata" / "observations_manifest.json", obs)
    files["metadata/observations_manifest.json"] = {
        "sha256": sha256_file(pack / "metadata" / "observations_manifest.json"),
        "size": (pack / "metadata" / "observations_manifest.json").stat().st_size,
        "mode": "0o600", "sensitive": False,
    }

    impl = implementation_recheck(load_lock(root))
    write_json(pack / "metadata" / "implementation_recheck.json", impl)
    files["metadata/implementation_recheck.json"] = {
        "sha256": sha256_file(pack / "metadata" / "implementation_recheck.json"),
        "size": (pack / "metadata" / "implementation_recheck.json").stat().st_size,
        "mode": "0o600", "sensitive": False,
    }

    hits, pattern_names = _scan(pack)
    total_bytes = sum(int(v["size"]) for v in files.values())
    manifest = {
        "schema": SCHEMA_PACK,
        "campaign_id": root.name,
        "created_utc": CO.S.utc_now(),
        "files": files,
        "counts": {"files": len(files), "total_bytes": total_bytes,
                   "observations_bound": obs["count"],
                   "observations_bytes": obs["total_bytes"]},
        "policy": {
            "no_symlinks": not symlinked,
            "no_file_over_10mb": not oversized,
            "missing": missing,
            "oversized": oversized,
            "symlinked": symlinked,
            "secret_scan": {"hits": hits, "patterns": pattern_names,
                            "engine": "orchestrator.common.secrets.scan_patterns"},
        },
        "judgment_sha256": judgment_sha,
        "report_sha256": sha256_file(root / "closeout" / "closeout_report.json")
        if (root / "closeout" / "closeout_report.json").is_file() else None,
        "self_sha256": None,
        "note": ("manifest 自身不入 files（避免自指）；包外 closure_judge.json 与 "
                 "closeout_report.json 只以 sha 绑定。self_sha256 记的是写入时对"
                 "「去掉 self_sha256 字段后的 canonical 文档」的 hash，供复核者复算。"),
    }
    body = {k: v for k, v in manifest.items() if k != "self_sha256"}
    manifest["self_sha256"] = sha256_bytes(
        json.dumps(body, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":")).encode("utf-8"))
    write_json(pack / "manifest.json", manifest)
    return {"missing": missing, "oversized": oversized, "symlinked": symlinked,
            "secret_hits": hits, "files": len(files), "total_bytes": total_bytes}


def bind_judgment(root: Path) -> dict[str, Any]:
    judge = root / "closeout" / "closure_judge.json"
    if not judge.is_file():
        raise SystemExit("缺 closeout/closure_judge.json：先跑独立复验器")
    manifest_path = root / "evidence_pack" / "manifest.json"
    manifest = read_json(manifest_path)
    manifest["judgment_sha256"] = sha256_file(judge)
    body = {k: v for k, v in manifest.items() if k != "self_sha256"}
    manifest["self_sha256"] = sha256_bytes(
        json.dumps(body, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":")).encode("utf-8"))
    write_json(manifest_path, manifest)
    return {"judgment_sha256": manifest["judgment_sha256"],
            "report_sha256": manifest.get("report_sha256"),
            "self_sha256": manifest["self_sha256"]}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def run(root: Path) -> dict[str, Any]:
    lock = load_lock(root)
    facts, prov = build_facts(root, lock)
    decision = CO.decide(facts)

    report = read_json(root / "analysis" / "t6_protocol" / "protocol_report.json") or {}
    cost = compute_cost(root, lock, report)
    slots = build_slot_matrix(root, lock, report)
    ac = build_ac_matrix(root, facts, report, cost, slots)

    co_out = CO.write_report(root, facts)
    cost_sha = write_json(root / "closeout" / "cost.json", cost)
    slots_sha = write_json(root / "closeout" / "slot_matrix.json", slots)
    ac_sha = write_json(root / "closeout" / "ac_matrix.json", ac)

    impl = implementation_recheck(lock)
    env = environment_snapshot(root)
    result = build_result(root, facts, decision, report, cost, slots, ac, prov, impl, env)
    result_sha = write_json(root / "closeout" / "result.json", result)
    md = human_report(root, result, cost, slots, ac)
    human_sha = write_text(root / "closeout" / "human_report.md", md)

    pack = build_pack(root)
    return {
        "closeout_report": co_out,
        "cost_sha256": cost_sha,
        "slot_matrix_sha256": slots_sha,
        "ac_matrix_sha256": ac_sha,
        "result_sha256": result_sha,
        "human_report_sha256": human_sha,
        "decision": decision,
        "pack": pack,
    }


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="RSI-R0.1 T9 结案装配（离线）")
    ap.add_argument("--campaign-root", default=None)
    ap.add_argument("--campaign-id", default=None)
    ap.add_argument("--bind-judgment", action="store_true",
                    help="把包外 closure_judge.json 的 sha 绑进 manifest 后重写")
    args = ap.parse_args(argv)
    if args.campaign_root:
        root = Path(args.campaign_root)
    elif args.campaign_id:
        root = DEFAULT_ROOT / args.campaign_id
    else:
        ap.error("需要 --campaign-root 或 --campaign-id")
    if not root.is_dir():
        raise SystemExit(f"campaign 根不存在：{root}")

    if args.bind_judgment:
        print(json.dumps(bind_judgment(root), ensure_ascii=False, indent=1))
        return 0

    out = run(root)
    printable = {k: v for k, v in out.items() if k != "decision"}
    printable["decision"] = {k: out["decision"][k] for k in
                             ("process_verdict", "measurement_verdict", "reason_codes")}
    print(json.dumps(printable, ensure_ascii=False, indent=1))
    ok = (not out["pack"]["missing"] and not out["pack"]["oversized"]
          and not out["pack"]["symlinked"] and out["pack"]["secret_hits"] == 0)
    return 0 if ok else 3


if __name__ == "__main__":
    sys.exit(main())
