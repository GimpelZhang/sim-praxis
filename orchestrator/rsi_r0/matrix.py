"""RSI-R0 控制矩阵的**离线**部分：判据冻结、21 负控、6 mutation、权限面事实。

从 ``scripts/rsi_r0/t3_controls.py`` 抽出来的原因：CLI 的 ``controls`` 子命令
（T4，AC21 的"所有路径显式 R0"）不能反过来依赖 ``scripts/`` 下的脚本，否则
安装面与执行面会互相缠绕。容器正控仍在脚本里（它需要 docker）。

纯函数 + 只读文件系统事实，零模型调用、零容器。
"""
from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

from orchestrator.common.runtime_paths import rsi_r0_campaign_root
from orchestrator.rsi_r0 import service as _service
from orchestrator.rsi_r0.controls import (
    MUTANTS,
    NEGATIVE_CATEGORIES,
    criteria_definition,
    criteria_hash,
    negative_cases,
    run_mutant,
    status_is_rejection,
)
from orchestrator.rsi_r0.image_pin import IMAGE_REF
from orchestrator.rsi_r0.trusted import (
    CANDIDATE_PARAM_BOUNDS,
    HOST_ONLY_FIELDS,
    ExperimentSession,
    container_argv_template,
)

_HERE = Path(__file__).resolve()


CRITERIA_SCHEMA = "rsi_r0.criteria.v1"


def permission_facts(protocol: Path) -> dict[str, Any]:
    """四项机器可核的权限事实；任一项不成立就阻塞（AC18）。

    这里不是"声明"而是**读回来核对**：容器 argv 真去数隔离旗标、公开 manifest
    真去读 leaks、私有目录真去 stat 权限位、服务端源码真去查有没有
    import 评测器（候选一旦能读评测器就等于拿到真值口径）。
    """
    argv = container_argv_template(IMAGE_REF, gpus=False)
    flags = {
        "network_none": "--network=none" in argv,
        "cap_drop_all": "--cap-drop=ALL" in argv,
        "read_only_rootfs": "--read-only" in argv,
        "non_root_user": any(str(a).startswith("--user=1000:") for a in argv),
        "no_new_privileges": "--security-opt=no-new-privileges" in argv,
    }
    mounts = [argv[i + 1] for i, a in enumerate(argv) if a == "-v"]
    flags["only_service_script_is_mounted"] = (
        len(mounts) == 1 and mounts[0].endswith("/svc/service.py:ro"))

    manifest_path = protocol / "t2_public_manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:          # 缺件/坏件一律记失败，不许崩
        manifest = {}
        public = {"leaks": ["<manifest unreadable>"], "files": 0,
                  "all_hashed": False, "error": type(exc).__name__}
    else:
        public = {"leaks": list(manifest.get("leaks", [])),
                  "files": len(manifest.get("files", [])),
                  "all_hashed": all(f.get("sha256") and f.get("size") is not None
                                    for f in manifest.get("files", []))}

    bounds = {kind: sorted(keys) for kind, keys in CANDIDATE_PARAM_BOUNDS.items()}
    surface = {"host_only_fields": sorted(HOST_ONLY_FIELDS),
               "candidate_keys": bounds,
               "scene_never_from_candidate":
                   "scene_xml" in HOST_ONLY_FIELDS
                   and all("scene_xml" not in keys for keys in bounds.values())}

    private = rsi_r0_campaign_root() / "private"
    outside = not str(private.resolve()).startswith(str(_HERE.parents[2].resolve()))
    try:
        mode: int | None = private.stat().st_mode & 0o777
    except OSError as exc:
        mode = None
        priv = {"path_outside_repo": outside, "mode_private": False, "mode": None,
                "error": type(exc).__name__}
    else:
        priv = {"path_outside_repo": outside, "mode_private": mode == 0o700,
                "mode": oct(mode)}

    # 用 AST 取真实 import，别用子串匹配（"evaluator" 出现在注释里也算命中的话，
    # 这条事实就既会误报，也可能被一句注释绕过）
    try:
        tree = ast.parse(Path(_service.__file__).read_text(encoding="utf-8"))
    except (OSError, SyntaxError) as exc:
        imported: set[str] = set()
        parse_error: str | None = type(exc).__name__
    else:
        imported, parse_error = set(), None
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                imported.add((node.module or "").split(".")[0])
    isolated = {"service_imports_evaluator": "evaluator" in imported,
                "service_imports_instances": "instances" in imported,
                "imports": sorted(imported), "parse_error": parse_error,
                "session_exposes_truth": False}
    # 会话对象只有 run/calls/validate——没有任何属性能把私有真值带出去
    exposed = [a for a in dir(ExperimentSession) if a in
               ("truth", "private", "expected", "fault", "answer")]
    isolated["session_exposes_truth"] = bool(exposed)

    facts = {
        "container_isolation": {"flags": flags, "ok": all(flags.values())},
        "public_manifest": {**public, "ok": not public["leaks"] and public["all_hashed"]},
        "candidate_surface": {**surface, "ok":
                              surface["scene_never_from_candidate"]
                              and set(surface["host_only_fields"]) ==
                              {"fault", "force_scale", "scene_xml"}},
        "private_data": {**priv, "ok": priv["path_outside_repo"] and priv["mode_private"]},
        "evaluator_hidden": {**isolated, "exposed_attrs": exposed,
                             "ok": parse_error is None
                             and not isolated["service_imports_evaluator"]
                             and not isolated["service_imports_instances"]
                             and not isolated["session_exposes_truth"]},
    }
    facts["ok"] = all(v["ok"] for k, v in facts.items() if isinstance(v, dict))
    return facts


def freeze_criteria(protocol: Path, plans,
                    *, allow_refreeze: bool = False) -> tuple[dict[str, Any], str]:
    definition = criteria_definition(plans)
    digest = criteria_hash(plans)
    path = protocol / "t3_criteria.json"
    payload = {"schema": CRITERIA_SCHEMA, "criteria_hash": digest,
               "definition": definition}
    if path.exists():
        old = json.loads(path.read_text(encoding="utf-8"))
        if str(old.get("criteria_hash")) != digest:
            if not allow_refreeze:
                raise SystemExit(
                    f"判据已被改动：冻结 {old.get('criteria_hash')} ≠ 当前 {digest}"
                    "（AC20：判据冻结后不得修改；确需修改须新开 campaign，"
                    "或在 T3 收盘前显式 --refreeze 并留痕）")
            # 留痕，不删旧值：被取代的哈希进 superseded 列表
            payload["superseded"] = list(old.get("superseded", [])) + [
                old.get("criteria_hash")]
        else:
            return old, digest
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
                    + "\n", encoding="utf-8")
    return payload, digest


def run_negatives() -> dict[str, Any]:
    items = []
    for case in negative_cases():
        rejected, code = status_is_rejection(case.build(), case)
        items.append({"case_id": case.case_id, "category": case.category,
                      "description": case.description,
                      "expected_layer": case.expected_layer.value,
                      "expected_code": case.expected_code, "got": code,
                      "rejected_at_expected_layer": rejected, "passed": rejected})
    return {"total": len(items), "passed": sum(i["passed"] for i in items),
            "by_category": {c: sum(1 for i in items if i["category"] == c)
                            for c in NEGATIVE_CATEGORIES},
            "missed": [i["case_id"] for i in items if not i["passed"]],
            "items": items}


def run_mutations() -> dict[str, Any]:
    by_id = {c.case_id: c for c in negative_cases()}
    items = []
    for name, meta in MUTANTS.items():
        rows = []
        for case_id in meta["killed_by"]:
            case = by_id[case_id]
            run = run_mutant(name, case)
            # 变异前该负控必须被拒（否则不是"摘掉判据导致漏检"）
            baseline_rejected, _code = status_is_rejection(case.build(), case)
            # 杀灭 = 摘掉该判据后**整条试次被放行**；只是换层/换码仍被拒不算
            killed_this = baseline_rejected and bool(run["released_under_mutation"])
            rows.append({"case": case_id,
                         "baseline_rejected": baseline_rejected,
                         "rejected_at_expected_layer_under_mutation":
                             run["rejected_at_expected_layer"],
                         "released_under_mutation": run["released_under_mutation"],
                         "execution_under_mutation": run["execution"],
                         "code_under_mutation": run["code"],
                         "killed_this_case": killed_this})
        killed = any(r["killed_this_case"] for r in rows)
        items.append({"mutant": name, "patch": meta["patch"],
                      "killed_by": rows, "killed": killed})
    return {"total": len(items), "killed": sum(i["killed"] for i in items),
            "survivors": [i["mutant"] for i in items if not i["killed"]],
            "items": items}
