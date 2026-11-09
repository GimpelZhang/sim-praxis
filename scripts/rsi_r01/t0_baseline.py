#!/usr/bin/env python
"""RSI-R0.1 T0 基座取证（零付费、不触网、不写旧根）。

产出（全部落在 campaign ``contract/``，0600）：
  consent.json            用户实施授权的逐字文本 + hash（AC01）
  baseline.json           git/代码 hash/解释器/依赖/阶段模型配置（AC04/AC27 基线）
  environment_check.json  env 引导 rc 与计划期差异、凭据存在性（不读值）、
                          工具/GPU/磁盘、密钥扫描 0 命中（AC04）
  sealed_roots.json       R0 家族根整树 + 六份封存期台账族的只读 hash 快照（AC03）
  task_selection.json     9 实例选择与 R 固定参数矩阵、POST 帽（T0 锁选择，T2 出全表）

用法：
  /mnt/venvs/simpraxis/bin/python scripts/rsi_r01/t0_baseline.py \
      --campaign-id r01_XXX --plan <plan> --consent <consent.txt> \
      --repo-root /home/vipuser/sim-praxis [--verify-only]
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata as md
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parents[2]))  # 仓库根：import orchestrator.*

REPO_DEFAULT = "/home/vipuser/sim-praxis"

#: 需记录版本的关键依赖（钉死版本；缺失记 null，不隐藏）。
DEPS = ("langgraph", "lark-oapi", "numpy", "torch", "mujoco", "mani_skill",
        "sapien", "genesis-world", "lancedb", "pytest", "PyYAML")

#: T0 改动/新增的代码文件（AC04 密钥扫描与 baseline hash 白名单）。
CODE_FILES = (
    "orchestrator/common/runtime_paths.py",
    "orchestrator/gateway/quota_ledger.py",
    "orchestrator/rsi_r01/__init__.py",
    "orchestrator/rsi_r01/campaign.py",
    "orchestrator/rsi_r01/cli.py",
    "scripts/rsi_r01/t0_baseline.py",
    "tests/test_rsi_r01_campaign.py",
)

PINNED = {"langgraph": "0.2.28", "lark-oapi": "1.7.4", "numpy": "1.26.4",
          "torch": "2.4.1+cu124", "mujoco": "3.15.0", "mani_skill": "3.0.1",
          "sapien": "3.0.3", "genesis-world": "1.3.3"}


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def run(cmd: "list[str]", *, cwd: "str | None" = None, timeout: int = 30
        ) -> "tuple[int, str]":
    try:
        out = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                             timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, f"<error: {exc}>"
    return out.returncode, (out.stdout or "") + (out.stderr or "")


def scan_for_secrets(text: str) -> "list[str]":
    from orchestrator.common.secrets import scan_text

    return [f"{name}" for name, _ in scan_text(text)]


def env_bootstrap(repo_root: Path, log_path: Path) -> "dict[str, object]":
    """在子进程里 source 00_env.sh，只记录 rc 与已脱敏日志（值由脚本自己保证不回显）。"""
    rc, out = run(["bash", "-c",
                   "set +x; source scripts/00_env.sh; echo \"__ENV_RC__=$?\""],
                  cwd=str(repo_root), timeout=120)
    hits = scan_for_secrets(out)
    if hits:  # 绝不把疑似密钥写盘
        return {"rc": None, "secret_scan": hits,
                "refused": "env 引导输出疑似含密钥，已拒绝落盘"}
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(out, encoding="utf-8")
    os.chmod(log_path, 0o600)
    env_rc = None
    for line in out.splitlines():
        if line.startswith("__ENV_RC__="):
            env_rc = int(line.split("=", 1)[1])
    host = None
    for line in out.splitlines():
        if "endpoint host=" in line:
            tail = line.split("endpoint host=", 1)[1]
            host = tail.split("；")[0].split(";")[0].split()[0]
    return {"rc": env_rc, "log_path": str(log_path),
            "log_sha256": sha256_file(log_path), "endpoint": host,
            "secret_scan": []}


def git_state(repo_root: Path) -> "dict[str, object]":
    rc, branch = run(["git", "-C", str(repo_root), "rev-parse", "--abbrev-ref", "HEAD"])
    rc2, commit = run(["git", "-C", str(repo_root), "rev-parse", "HEAD"])
    rc3, status = run(["git", "-C", str(repo_root), "status", "--porcelain"])
    return {"branch": branch.strip() if rc == 0 else None,
            "commit": commit.strip() if rc2 == 0 else None,
            "dirty": bool(status.strip()) if rc3 == 0 else None,
            "dirty_entries": len([l for l in status.splitlines() if l.strip()]),
            "head_author": _head_author(repo_root)}


def _head_author(repo_root: Path) -> "str | None":
    rc, out = run(["git", "-C", str(repo_root), "log", "-1", "--format=%an <%ae>"])
    return out.strip() if rc == 0 else None


def deps_inventory() -> "dict[str, object]":
    out: "dict[str, object]" = {}
    for name in DEPS:
        try:
            out[name] = md.version(name)
        except md.PackageNotFoundError:
            out[name] = None
    return out


def stage_models(repo_root: Path) -> "dict[str, object]":
    path = repo_root / "orchestrator/gateway/stage_models.yaml"
    import yaml

    obj = yaml.safe_load(path.read_text(encoding="utf-8"))
    models = {k: v.get("model_id") for k, v in (obj.get("stage_models") or {}).items()}
    return {"path": str(path), "sha256": sha256_file(path),
            "models": models,
            "endpoint": (obj.get("meta") or {}).get("endpoint"),
            "base_url_env": (obj.get("meta") or {}).get("base_url_env")}


def hardware() -> "dict[str, object]":
    rc, out = run(["nvidia-smi", "--query-gpu=memory.used,memory.total",
                   "--format=csv,noheader,nounits"], timeout=20)
    gpu = [{"used_mib": int(a), "total_mib": int(b)}
           for a, b in (l.split(",") for l in out.splitlines() if "," in l)] if rc == 0 else None
    rc_d, docker = run(["docker", "version", "--format", "{{.Server.Version}}"], timeout=20)
    du = shutil.disk_usage("/mnt")
    return {"gpu": gpu, "gpu_query_rc": rc,
            "docker_server": docker.strip() if rc_d == 0 else None,
            "docker_rc": rc_d,
            "disk_mnt": {"total_gb": round(du.total / 1e9, 1),
                         "used_gb": round(du.used / 1e9, 1),
                         "free_gb": round(du.free / 1e9, 1)}}


def credentials_presence() -> "dict[str, object]":
    access = Path.home() / "access"
    files = {"user_access_methods.txt": ["huggingface token", "github access token",
                                         "NVIDIA NGC API Key"]}
    out: "dict[str, object]" = {"values_recorded": False,
                                "dir": str(access),
                                "dir_mode": _mode(access)}
    present = {}
    for fn in files:
        p = access / fn
        present[fn] = p.is_file()
    out["files_present"] = present
    home_bashrc = Path.home() / ".bashrc"
    text = home_bashrc.read_text(encoding="utf-8", errors="replace") if home_bashrc.is_file() else ""
    out["bashrc_anthropic_exports"] = {
        name: (f"export {name}=" in text or f"\n{name}=" in text)
        for name in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_MODEL")}
    return out


def _mode(p: Path) -> "str | None":
    try:
        return oct(p.stat().st_mode & 0o777)
    except OSError:
        return None


def task_selection(ctx_campaign_id: str) -> "dict[str, object]":
    r_matrix = [
        {"instance_id": "R-dev", "damping_low": 0.15, "damping_high": 0.45,
         "theta0_low": -0.8, "theta0_high": 0.8, "window_s": 4.0, "h_public": 1,
         "role": "dev", "evidence_grade": "regression_anchor_from_R0_diagnostics"},
        {"instance_id": "R-val-1", "damping_low": 0.12, "damping_high": 0.35,
         "theta0_low": -0.6, "theta0_high": 0.6, "window_s": 4.0, "h_public": 1,
         "role": "validation", "evidence_grade": "new_combination_unmeasured"},
        {"instance_id": "R-val-2", "damping_low": 0.18, "damping_high": 0.50,
         "theta0_low": -1.0, "theta0_high": 1.0, "window_s": 4.0, "h_public": -1,
         "role": "validation", "evidence_grade": "new_combination_unmeasured"},
    ]
    instances = [
        {"instance_id": "E-dev", "family": "E", "role": "dev"},
        {"instance_id": "D-dev", "family": "D", "role": "dev"},
        {"instance_id": "R-dev", "family": "R", "role": "dev"},
        {"instance_id": "E-val-1", "family": "E", "role": "validation"},
        {"instance_id": "E-val-2", "family": "E", "role": "validation"},
        {"instance_id": "D-val-1", "family": "D", "role": "validation"},
        {"instance_id": "D-val-2", "family": "D", "role": "validation"},
        {"instance_id": "R-val-1", "family": "R", "role": "validation"},
        {"instance_id": "R-val-2", "family": "R", "role": "validation"},
    ]
    return {
        "schema": "simpraxis.rsi_r01.task_selection.v1",
        "campaign_id": ctx_campaign_id,
        "selected_at_utc": _utc(),
        "status": "LOCKED_AT_T0_SELECTION_ONLY",
        "instances": instances,
        "r_parameter_matrix": r_matrix,
        "post_caps": {"protocol": 12, "E_dev": 24, "D_dev": 24, "R_dev": 24,
                      "E_val_1": 24, "E_val_2": 24, "D_val_1": 24, "D_val_2": 24,
                      "R_val_1": 24, "R_val_2": 24, "total": 228},
        "notes": [
            "T0 只锁『选择』：9 实例 id/角色与 R 固定参数矩阵；完整任务/难度/故障/"
            "阈值/参考来源表由 T2 产出 task_matrix.json（计划 §4.1）。",
            "R-dev 借用 R0 已公开诊断参数，明确为回归锚、非独立新证据；"
            "R-val-1/2 是拟定组合，T2 必须先用固定物理配置验证四格完整观测、"
            "收敛与可判别性，未满足即 STOP_TASK_DESIGN（计划 §4.1）。",
            "验证实例不得复用旧 R0 未执行 T7 的隐藏集；只换 seed 不称任务迁移。",
        ],
    }


def _utc() -> str:
    from orchestrator.common.clock import utc_now_iso_z

    return utc_now_iso_z()


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--campaign-id", required=True)
    ap.add_argument("--plan", required=True)
    ap.add_argument("--consent", required=True)
    ap.add_argument("--repo-root", default=REPO_DEFAULT)
    ap.add_argument("--root", default=None)
    ap.add_argument("--verify-only", action="store_true",
                    help="只重算并比对 sealed_roots.json（旧根不变检查）")
    args = ap.parse_args(argv)

    from orchestrator.rsi_r01 import campaign as camp

    repo = Path(args.repo_root)
    ctx = camp.load_campaign(campaign_id=args.campaign_id, root=args.root)
    if Path(args.plan).resolve() != Path(ctx.plan_path) or \
            sha256_file(Path(args.plan)) != ctx.plan_sha256:
        raise SystemExit("计划文件与 campaign 上下文不一致（拒绝在漂移的计划上取证）")

    if args.verify_only:
        snap = camp.read_json(ctx.root / "contract" / "sealed_roots.json")
        diff = camp.verify_protected(snap)
        print(json.dumps({"verified_at_utc": _utc(), "problems": diff,
                          "verdict": "UNCHANGED" if not diff else "DRIFT"},
                         ensure_ascii=False, indent=2))
        return 0 if not diff else 3

    consent_text = Path(args.consent).read_text(encoding="utf-8")
    consent = {
        "schema": "simpraxis.rsi_r01.consent.v1",
        "recorded_at_utc": _utc(),
        "granted_by": "user",
        "verbatim_text": consent_text,
        "verbatim_sha256": sha256_text(consent_text),
        "file_sha256": sha256_file(Path(args.consent)),
        "plan_path": ctx.plan_path,
        "plan_sha256": ctx.plan_sha256,
        "code_commit_at_record": ctx.code_commit,
        "non_breakable_constraints": [
            "commit message 的 author 必须是 Junchuan Zhang <zjunchuan@gmail.com>",
            "push 到 github remote 仓库时必须保证所有帐号密码的安全",
        ],
        "scope": "按计划实施 RSI-R0.1 T0–T9（含零付费仪器与冻结后付费试次）",
        "real_actions_before_consent": 0,
    }
    camp.write_json(ctx, "contract/consent.json", consent)

    env = env_bootstrap(repo, ctx.root / "contract" / "env_bootstrap.log")
    gits = git_state(repo)
    deps = deps_inventory()
    models = stage_models(repo)
    hw = hardware()
    creds = credentials_presence()

    code_hashes = {}
    for rel in CODE_FILES:
        p = repo / rel
        code_hashes[rel] = sha256_file(p) if p.is_file() else None

    baseline = {
        "schema": "simpraxis.rsi_r01.baseline.v1",
        "campaign_id": ctx.campaign_id,
        "recorded_at_utc": _utc(),
        "git": gits,
        "code_hashes": code_hashes,
        "python": {"version": sys.version.split()[0],
                   "executable": sys.executable,
                   "platform": platform.platform()},
        "dependencies": deps,
        "pinned_expected": PINNED,
        "stage_models": models,
        "model_change_this_campaign": False,
        "notes": [
            "code_commit 绑定的是 T0 开工时的 HEAD；最终 LOCK 由 T5 绑定实现哈希"
            "（计划 §3.2/§7 T5）。",
            "stage_models.yaml 是模型 ID 唯一源；本计划不写死模型 ID，不换供。",
        ],
    }

    env_check = {
        "schema": "simpraxis.rsi_r01.environment_check.v1",
        "campaign_id": ctx.campaign_id,
        "checked_at_utc": _utc(),
        "env_bootstrap": env,
        "plan_recorded_state": {"env_rc_at_planning": 1,
                                "note": "计划编制时现场记录 env rc=1（host 白名单未含"
                                        " tokenhub）；本脚本实测见 env_bootstrap.rc。"},
        "credentials": creds,
        "hardware": hw,
        "secret_scan": {"files": list(code_hashes) + [
            "contract/consent.json", "contract/baseline.json",
            "contract/environment_check.json", "contract/sealed_roots.json",
            "contract/task_selection.json"], "hits": []},
        "verdict": "PENDING_SECRET_SCAN",
    }

    sealed = camp.snapshot_protected()
    selection = task_selection(ctx.campaign_id)

    camp.write_json(ctx, "contract/baseline.json", baseline)
    camp.write_json(ctx, "contract/sealed_roots.json", sealed)
    camp.write_json(ctx, "contract/task_selection.json", selection)

    # 密钥扫描：扫描 T0 代码文件 + 已写合同件的文本。
    hits: "list[dict[str, object]]" = []
    for rel in list(code_hashes) + ["contract/consent.json", "contract/baseline.json",
                                    "contract/sealed_roots.json",
                                    "contract/task_selection.json"]:
        p = repo / rel if (repo / rel).is_file() else ctx.root / rel
        if not p.is_file():
            continue
        found = scan_for_secrets(p.read_text(encoding="utf-8", errors="replace"))
        if found:
            hits.append({"file": str(p), "patterns": found})
    env_check["secret_scan"]["hits"] = hits
    env_check["verdict"] = ("OK" if env.get("rc") == 0 and not hits
                            else ("BLOCKED_ENV" if env.get("rc") != 0
                                  else "SECRET_LEAK"))
    camp.write_json(ctx, "contract/environment_check.json", env_check)

    summary = {
        "campaign_id": ctx.campaign_id,
        "root": str(ctx.root),
        "env_rc": env.get("rc"),
        "env_endpoint": env.get("endpoint"),
        "verdict": env_check["verdict"],
        "secret_hits": len(hits),
        "sealed_manifest_sha256": sealed["rsi_r0"]["tree"]["manifest_sha256"],
        "sealed_r0_files": sealed["rsi_r0"]["tree"]["n_files"],
        "instances": len(selection["instances"]),
        "post_cap_total": selection["post_caps"]["total"],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if env_check["verdict"] == "OK" else 4


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
