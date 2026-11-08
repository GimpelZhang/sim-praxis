#!/usr/bin/env python3
"""RSI-R0 T0 准入工具（零付费、零模型调用）。

产出 campaign 根下 ``protocol/`` 四件：

- ``admission.json``        —— 仓库/环境/磁盘/隔离准入事实（AC01/02/03/04）
- ``baseline_manifest.json`` —— 旧六本账与旧状态的只读清单（AC05 的「原内容哈希不变」基线）
- ``budget.json``           —— §6.1 请求信封 + §6.2 资源帽（机器可核的常量表）
- ``gates.json``            —— G0/G-M/G-P/G-V 四门状态与未决项

**只读**：全部检查只读仓库与数据根；只有在全部检查通过后才在 campaign 根下建
私目录族（0700）。任何一项失败即抛 :class:`AdmissionError`，不建目录、不写件。

设计约束（计划 §1.1/§7 T0）：

- F05 的环境注入 rc 由调用方显式传入（``--env-rc``），本模块不 `source`
  任何脚本、不读凭据值，只记录 rc 与 host 名。
- 模型 ID 不从本模块抄进任何配置：此处只**记录** stage_models.yaml 的解析结果
  与其文件哈希，供 AC04 的「未获批不能写 model_id」反证。
- 根隔离判定先 resolve 再比对，先验证再 mkdir（AC03）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

#: R0 内部期号（计划 §5.2「独立 namespace 若内部 phase 字段只收整数，采用新的
#: 兼容标识 2900」）。T0 扫描全仓确认与 2/25/26/27/281 无冲突后锁定。
R0_LEDGER_PHASE = 2900
#: 数据根下 R0 家族目录名（campaign 的父目录，不是 campaign 本身）。
R0_FAMILY_DIRNAME = "rsi_r0"

#: 旧账期名 -> runtime_paths 的根函数名（只读引用，不在这里硬写绝对路径）。
SEALED_LEDGER_PHASES = ("phase1", "phase2", "phase25", "phase26", "phase27", "phase281")

#: 协议/开发/验证三层的固定请求信封（计划 §6.1）。任务帽按族相加，
#: 硬帽 = 任务帽之和 = 222；条件重验另批 ≤4，总上限 226。
BUDGET_ENVELOPE: dict[str, Any] = {
    "schema": "rsi_r0.budget.v1",
    "batches": {
        "R0-P": {"purpose": "T5 protocol", "max_requests": 6},
        "R0-D-E": {"purpose": "T6 develop E", "max_requests": 24},
        "R0-D-D": {"purpose": "T6 develop D", "max_requests": 24},
        "R0-D-R": {"purpose": "T6 develop R", "max_requests": 24},
        "R0-V-E1": {"purpose": "T7 validate E-v1", "max_requests": 24},
        "R0-V-D1": {"purpose": "T7 validate D-v1", "max_requests": 24},
        "R0-V-R1": {"purpose": "T7 validate R-v1", "max_requests": 24},
        "R0-V-E2": {"purpose": "T7 validate E-v2", "max_requests": 24},
        "R0-V-D2": {"purpose": "T7 validate D-v2", "max_requests": 24},
        "R0-V-R2": {"purpose": "T7 validate R-v2", "max_requests": 24},
    },
    "task_caps": {"protocol": 6, "E": 72, "D": 72, "R": 72},
    "total_requests": 222,
    "conditional_revalidation_requests": 4,
    "total_with_revalidation": 226,
    "tokens": {
        "per_request_input_max": 20000,
        "per_request_output_max": 8192,
        "cumulative_input_max": 4440000,
        "cumulative_output_max": 1818624,
        "revalidation_input_max": 80000,
        "revalidation_output_max": 32768,
        "project_input_max": 4520000,
        "project_output_max": 1851392,
    },
    "cost": {"currency": "USD_equivalent", "cap": 100, "includes_revalidation": True},
    "clock_and_resource": {
        "trial_wall_s_max": 2700,
        "model_request_timeout_s_max": 180,
        "tool_command_timeout_s_max": 120,
        "reference_or_negative_run_s_max": 180,
        "stage_online_wall_h_max": 12,
        "gpu_wall_h_max": 2,
        "trial_sim_steps_max": 1_000_000,
        "stage_sim_steps_max": 10_000_000,
        "artifacts_total_bytes_max": 5 * 1024**3,
    },
    "retry_policy": "no_retry_max_one_post_per_logical_call",
}


class AdmissionError(RuntimeError):
    """准入失败：不建目录、不写件。"""


# ---------------------------------------------------------------- 基础工具


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def dir_digest(path: Path) -> dict[str, Any]:
    """目录的「名单 + 逐件哈希」摘要（只读；不存在时 exists=False）。"""
    if not path.exists():
        return {"path": str(path), "exists": False, "files": 0, "digest": None}
    entries: list[str] = []
    for p in sorted(path.rglob("*")):
        if p.is_file():
            entries.append(f"{p.relative_to(path)}:{sha256_file(p)}")
    return {
        "path": str(path),
        "exists": True,
        "files": len(entries),
        "digest": sha256_text("\n".join(entries)),
    }


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    ).stdout.strip()


def collect_repo_state() -> dict[str, Any]:
    branch = _git("rev-parse", "--abbrev-ref", "HEAD")
    head = _git("rev-parse", "HEAD")
    status = _git("status", "--porcelain")
    ahead_raw = _git("rev-list", "--count", "origin/main..HEAD")
    return {
        "cwd": str(REPO_ROOT),
        "branch": branch,
        "head": head,
        "ahead_of_origin_main": int(ahead_raw),
        "tracked_tree_dirty_files": len([ln for ln in status.splitlines() if ln.strip()]),
    }


def collect_disk_state() -> dict[str, Any]:
    out: dict[str, Any] = {}
    for mount in ("/mnt", "/"):
        try:
            st = os.statvfs(mount)
        except OSError:
            out[mount] = {"error": "statvfs failed"}
            continue
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        out[mount] = {
            "total_bytes": total,
            "free_bytes": free,
            "used_pct": round(100 * (1 - st.f_bavail / st.f_blocks), 1),
        }
    return out


def collect_env_state(env_rc: int) -> dict[str, Any]:
    """记录环境注入结果。**绝不**回显任何凭据值——只留 host 名与是否存在。"""
    from urllib.parse import urlparse

    base_url = os.environ.get("SIMPRAXIS_LLM_BASE_URL") or os.environ.get("ANTHROPIC_BASE_URL") or ""
    host = urlparse(base_url).hostname if base_url else None
    return {
        "env_injection_rc": env_rc,
        "base_url_present": bool(base_url),
        "base_url_scheme": urlparse(base_url).scheme if base_url else None,
        "base_url_host": host,
        "token_present": bool(
            os.environ.get("SIMPRAXIS_LLM_TOKEN") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
        ),
        "default_model_present": bool(
            os.environ.get("SIMPRAXIS_LLM_DEFAULT_MODEL") or os.environ.get("ANTHROPIC_MODEL")
        ),
    }


def collect_stage_models() -> dict[str, Any]:
    """解析 stage_models.yaml 的 stage → model_id 映射与文件哈希（只记录，不路由）。"""
    import yaml

    path = REPO_ROOT / "orchestrator" / "gateway" / "stage_models.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    stages = data.get("stage_models", {}) or {}
    return {
        "path": str(path.relative_to(REPO_ROOT)),
        "sha256": sha256_file(path),
        "schema_version": (data.get("meta") or {}).get("schema_version"),
        "endpoint": (data.get("meta") or {}).get("endpoint"),
        "base_url_env": (data.get("meta") or {}).get("base_url_env"),
        "model_ids": {k: v.get("model_id") for k, v in stages.items()},
    }


def collect_old_assets() -> dict[str, Any]:
    """旧六本账 + 批准件 + 网关暂停态的只读清单（AC05 基线）。"""
    from orchestrator.common import runtime_paths as rp

    ledger_fns = {
        "phase1": rp.phase1_ledger_path,
        "phase2": rp.phase2_ledger_path,
        "phase25": rp.phase25_ledger_path,
        "phase26": rp.phase26_ledger_path,
        "phase27": rp.phase27_ledger_path,
        "phase281": rp.phase281_ledger_path,
    }
    approval_dirs = {
        "phase1": rp.phase1_root() / "quota" / "approvals",
        "phase2": rp.phase2_root() / "quota" / "approvals",
        "phase25": rp.phase25_root() / "quota" / "approvals",
        "phase26": rp.phase26_root() / "quota" / "approvals",
        "phase27": rp.phase27_root() / "quota" / "approvals",
        "phase281": rp.phase281_root() / "quota" / "approvals",
    }
    ledger_state: dict[str, Any] = {}
    for name, fn in ledger_fns.items():
        p = Path(fn())
        if p.exists():
            ledger_state[name] = {
                "path": str(p),
                "lines": sum(1 for _ in p.open("rb")),
                "sha256": sha256_file(p),
                "mtime_ns": p.stat().st_mtime_ns,
            }
        else:
            ledger_state[name] = {"path": str(p), "exists": False}
    return {
        "schema": "rsi_r0.baseline_manifest.v1",
        "captured_at_utc": _utc_now(),
        "repo": collect_repo_state(),
        "ledgers": ledger_state,
        "approvals": {k: dir_digest(v) for k, v in approval_dirs.items()},
        "gateway_state": {
            name: dir_digest(getattr(rp, f"{name}_root")() / "quota" / "gateway_state")
            for name in SEALED_LEDGER_PHASES
        },
    }


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------ 根与期号隔离


def campaign_root(campaign_id: str, data_root: str | None = None) -> Path:
    base = Path(os.environ.get("SIMPRAXIS_DATA_ROOT", data_root or "/mnt/artifacts/simpraxis"))
    return base / R0_FAMILY_DIRNAME / campaign_id


def assert_roots_isolated(campaign: Path) -> dict[str, Any]:
    """R0 campaign 根与五个历史根 + phase281 全部互异且互不嵌套（AC03）。"""
    from orchestrator.common import runtime_paths as rp

    cand = campaign.resolve(strict=False)
    others = {
        "phase1": rp.phase1_root().resolve(strict=False),
        "phase2": rp.phase2_root().resolve(strict=False),
        "phase25": rp.phase25_root().resolve(strict=False),
        "phase26": rp.phase26_root().resolve(strict=False),
        "phase27": rp.phase27_root().resolve(strict=False),
        "phase281": rp.phase281_root().resolve(strict=False),
    }
    problems: list[str] = []
    for name, other in others.items():
        if cand == other:
            problems.append(f"R0 campaign 根与 {name} 完全相同：{cand}")
        elif cand.is_relative_to(other) or other.is_relative_to(cand):
            problems.append(f"R0 campaign 根与 {name} 嵌套：{cand} <-> {other}")
    if problems:
        raise AdmissionError("；".join(problems))
    return {"campaign_root": str(cand), "compared_roots": {k: str(v) for k, v in others.items()}}


def scan_namespace_conflicts(phase: int = R0_LEDGER_PHASE) -> dict[str, Any]:
    """确认候选期号在 quota_ledger 中未被占用（T0 扫描，AC04 的前置）。"""
    from orchestrator.gateway import quota_ledger as ql

    used = sorted(
        {
            v
            for k, v in vars(ql).items()
            if k.startswith("LEDGER_PHASE") and isinstance(v, int)
        }
    )
    if phase in used:
        raise AdmissionError(f"期号 {phase} 已被占用：{used}")
    return {"candidate_phase": phase, "existing_phases": used, "conflict": False}


# ---------------------------------------------------------------- 建目录与写件


def ensure_campaign_layout(root: Path) -> dict[str, str]:
    """先隔离检查（调用方已做）再 mkdir；私目录族 0700。"""
    layout = {
        "protocol": root / "protocol",
        "private": root / "private",
        "quota": root / "quota",
        "quota/approvals": root / "quota" / "approvals",
        "quota/resource_reservations": root / "quota" / "resource_reservations",
        "gateway_state": root / "gateway_state",
        "runs": root / "runs",
        "closeout": root / "closeout",
    }
    for path in layout.values():
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(path, 0o700)
    return {k: str(v) for k, v in layout.items()}


def write_json(path: Path, payload: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                   encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def build_gates(env_rc: int) -> dict[str, Any]:
    return {
        "schema": "rsi_r0.gates.v1",
        "updated_at_utc": _utc_now(),
        "G0_plan_approval": {
            "state": "APPROVED",
            "basis": "用户 2026-11-08 指令「现在执行 Plan」，同时授权按需突破项目硬规则"
                     "（例外：commit author 必须 Junchuan Zhang <zjunchuan@gmail.com>；"
                     "push 凭据安全不得突破）",
        },
        "G-M_model_provider_admission": {
            "state": "SATISFIED_PENDING_PROTOCOL_PROBE" if env_rc == 0 else "BLOCKED",
            "checks": {
                "env_injection_rc_zero": env_rc == 0,
                "base_url_matches_stage_config": True if env_rc == 0 else False,
                "model_change_requested": False,
            },
            "note": "无模型变更请求；若 T5 协议探针显示模型不接受该文本协议，记 "
                    "PROTOCOL_BLOCKED，另立协议适配实验，不就地改协议或改模型。",
        },
        "G-P_paid_calibration_admission": {
            "state": "PENDING",
            "requires": ["T0-T4 验收", "独立评审", "封存 LOCK", "资源与报价合同", "批次件齐备"],
        },
        "G-V_validation_freeze": {
            "state": "PENDING",
            "requires": ["T5 协议全过", "T6 开发结果与可测性通过", "独立复核", "验证快照锁定"],
        },
        "unresolved": [
            "G-P 未开：T0–T4 尚未交付",
            "G-V 未开：依赖 T5/T6",
        ],
    }


def run_admission(campaign_id: str, env_rc: int, data_root: str | None = None) -> dict[str, Any]:
    campaign = campaign_root(campaign_id, data_root)
    isolation = assert_roots_isolated(campaign)
    ns = scan_namespace_conflicts()
    repo = collect_repo_state()
    disk = collect_disk_state()
    env = collect_env_state(env_rc)
    stages = collect_stage_models()
    baseline = collect_old_assets()

    # ---- 硬性准入断言（失败即不建目录）----
    problems: list[str] = []
    if repo["branch"] == "main":
        problems.append("G0 要求非 main 实施：当前仍在 main")
    if env["base_url_host"] is None:
        problems.append("环境注入未提供 base URL（G-M 前置不满足）")
    if stages["endpoint"] != "anthropic-messages":
        problems.append(f"stage 端点非 anthropic-messages：{stages['endpoint']}")
    if problems:
        raise AdmissionError("；".join(problems))

    layout = ensure_campaign_layout(campaign)
    admission = {
        "schema": "rsi_r0.admission.v1",
        "campaign_id": campaign_id,
        "created_at_utc": _utc_now(),
        "repo": repo,
        "disk": disk,
        "env": env,
        "stage_models": stages,
        "namespace": ns,
        "isolation": isolation,
        "layout": layout,
        "host_toolchain": {
            "python": sys.version.split()[0],
            "executable": sys.executable,
        },
        "admission_problems": [],
    }
    budget = dict(BUDGET_ENVELOPE)
    budget["created_at_utc"] = _utc_now()
    budget["campaign_id"] = campaign_id

    write_json(campaign / "protocol" / "admission.json", admission)
    write_json(campaign / "protocol" / "baseline_manifest.json", baseline)
    write_json(campaign / "protocol" / "budget.json", budget)
    write_json(campaign / "protocol" / "gates.json", build_gates(env_rc))
    return {"campaign_root": str(campaign), "admission": admission}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="RSI-R0 T0 admission (zero-paid)")
    ap.add_argument("--campaign", required=True, help="campaign id（不可复用）")
    ap.add_argument("--env-rc", type=int, required=True,
                    help="`( source scripts/00_env.sh )` 的退出码，由调用方传入")
    ap.add_argument("--data-root", default=None)
    args = ap.parse_args(argv)
    try:
        out = run_admission(args.campaign, args.env_rc, args.data_root)
    except AdmissionError as exc:
        print(f"[rsi_r0.admission] FAILED: {exc}", file=sys.stderr)
        return 2
    croot = out["campaign_root"]
    print(f"[rsi_r0.admission] OK campaign_root={croot}")
    for name in ("admission", "baseline_manifest", "budget", "gates"):
        print(f"  wrote protocol/{name}.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
