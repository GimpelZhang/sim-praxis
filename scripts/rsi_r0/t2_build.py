#!/usr/bin/env python3
"""T2 构建：生成九个公开任务包与私有真值，并落任务锁（零模型调用）。

用法：
    python scripts/rsi_r0/t2_build.py --campaign <id> [--repo-root .] [--out <dir>]

产物：
* ``benchmarks/rsi_r0/tasks/<instance_id>/task.yaml``（公开，进仓库）
* ``<campaign>/private/instances/<instance_id>.json``（私有，数据层，0700）
* ``<campaign>/protocol/t2_tasks_lock.json``（九实例/公开哈希/顺序/冻结时间）
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import yaml  # noqa: E402

from orchestrator.common.fsutil import ensure_private_dir  # noqa: E402
from orchestrator.common.runtime_paths import (  # noqa: E402
    rsi_r0_campaign_root,
    rsi_r0_private_dir,
    rsi_r0_protocol_dir,
)
from orchestrator.rsi_r0.contracts import (  # noqa: E402
    CONTRACT_SCHEMA,
    answer_schema,
    canonical_json,
    sha256_of,
)
from orchestrator.rsi_r0.instances import PLANS, assert_frozen_table  # noqa: E402
from orchestrator.rsi_r0.scenes import build_scene  # noqa: E402

TASK_SCHEMA = "rsi_r0.task.v1"

#: 出现在公开任务书里就等于泄题（真值、判据规则、参考增益、容差）。
PRIVATE_KEYS = ("fault", "expected", "expected_direction", "equivalence_bound",
                "levels", "tolerance", "reference_gains", "probe_forces",
                "probe_steps", "nominal", "classify")


def _write_yaml(path: Path, payload: dict) -> None:
    ensure_private_dir(path.parent)
    path.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=True, width=4096),
        encoding="utf-8")


def public_task(plan) -> dict:
    """公开任务书：只含 candidate 可见字段（场景即输入，真值一律不进）。"""
    scene_xml = build_scene(plan.template, plan.public["scene_params"])
    return {
        "schema": TASK_SCHEMA,
        "contract_schema": CONTRACT_SCHEMA,
        "instance_id": plan.instance_id,
        "family": plan.family.value,
        "role": plan.role,
        "task_id": plan.task_id,
        "order_index": plan.order_index,
        **plan.public,
        "scene_xml_sha256": sha256_of(scene_xml),
        # 交卷格式是合同的一部分：候选必须按 run_id 引用宿主事件，不能自报数字
        "answer": answer_schema(plan.family),
        "rules": [
            "只能通过可信服务的有界数值接口发起实验；不得提交代码/路径/命令。",
            "禁止访问私有数据、评测器、其他实例工作区与历史对话。",
            "每实例最多 24 次模型请求；不因未用完预算而转移额度。",
        ],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--campaign", default=None, help="campaign id（缺省取环境变量）")
    ap.add_argument("--repo-root", default=str(Path(__file__).resolve().parents[2]))
    args = ap.parse_args()

    assert_frozen_table()
    repo = Path(args.repo_root).resolve()
    tasks_root = repo / "benchmarks" / "rsi_r0" / "tasks"

    campaign = rsi_r0_campaign_root()
    private = ensure_private_dir(rsi_r0_private_dir() / "instances")
    protocol = ensure_private_dir(rsi_r0_protocol_dir())

    entries = []
    for plan in PLANS:
        task = public_task(plan)
        path = tasks_root / plan.instance_id / "task.yaml"
        _write_yaml(path, task)
        (private / f"{plan.instance_id}.json").write_text(
            json.dumps(plan.private, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8")
        entries.append({
            "instance_id": plan.instance_id,
            "family": plan.family.value,
            "role": plan.role,
            "task_id": plan.task_id,
            "order_index": plan.order_index,
            "task_path": str(path.relative_to(repo)),
            "public_sha256": sha256_of(task),
            "scene_xml_sha256": task["scene_xml_sha256"],
            "private_ref": f"private/instances/{plan.instance_id}.json",
        })

    lock = {
        "schema": "rsi_r0.tasks_lock.v1",
        "contract_schema": CONTRACT_SCHEMA,
        "campaign": campaign.name,
        "instances": entries,
        "tasks_set_sha256": sha256_of([e["public_sha256"] for e in entries]),
        "note": ("九实例公开包/顺序/私有引用冻结于任何模型试次之前（计划 §3.3）；"
                 "私有真值只落数据层 private/instances/，不进仓库。"),
    }
    out = protocol / "t2_tasks_lock.json"
    out.write_text(json.dumps(lock, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                   encoding="utf-8")

    # 内容层 manifest：公开包逐文件 sha256，并在同一遍里做泄漏自检。
    # AC13 要求"公开包在模型调用前锁定"，锁的对象必须逐文件，不能只锁一份汇总哈希。
    files = sorted(p for p in tasks_root.rglob("*") if p.is_file())
    manifest_files, leaks = [], []
    for path in files:
        text = path.read_text(encoding="utf-8")
        rel = str(path.relative_to(repo))
        manifest_files.append({"path": rel, "size": len(text.encode("utf-8")),
                               "sha256": sha256_of(text)})
        for key in PRIVATE_KEYS:
            if f"{key}:" in text:
                leaks.append({"path": rel, "key": key})
    manifest = {
        "schema": "rsi_r0.public_manifest.v1",
        "campaign": campaign.name,
        "root": str(tasks_root.relative_to(repo)),
        "files": manifest_files,
        "leaks": leaks,
        "tasks_set_sha256": lock["tasks_set_sha256"],
    }
    manifest["manifest_sha256"] = sha256_of([manifest_files, lock["tasks_set_sha256"]])
    (protocol / "t2_public_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    if leaks:
        print(json.dumps({"ok": False, "leaks": leaks}, ensure_ascii=False))
        return 1

    print(json.dumps({"ok": True, "lock": str(out),
                      "manifest_sha256": manifest["manifest_sha256"],
                      "files": len(manifest_files),
                      "tasks_set_sha256": lock["tasks_set_sha256"],
                      "instances": [e["instance_id"] for e in entries]},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
