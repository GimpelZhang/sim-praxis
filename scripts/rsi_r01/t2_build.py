#!/usr/bin/env python3
"""T2 构建（零模型调用）：九个公开任务包、私有真值副本与任务锁。

产物分三层（KB 三层分离纪律的 R0.1 版）：

* **公开**（进仓库 + campaign ``public/`` 只读副本）：
  ``benchmarks/rsi_r01/tasks/<instance_id>/task.json`` 与
  ``benchmarks/rsi_r01/task_matrix.json``、``measurement_contract.json``；
* **私有**（只落 ``/mnt``，0700/0600）：``<campaign>/private/instances/<id>.json``；
* **锁**：``<campaign>/protocol/t2_tasks_lock.json``（逐件 sha256 + 顺序 +
  实例数 + 预注册 hash），供 T5 LOCK 与结案机判引用。

公开面一律过私有键扫描（:data:`orchestrator.rsi_r01.task_contract._SECRET_KEYS`），
任何私有键出现在公开件即**拒绝构建**（不是写进去再掩）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from orchestrator.common import runtime_paths as RP
from orchestrator.common.fsutil import atomic_write_text, ensure_private_dir
from orchestrator.rsi_r01 import schemas as S
from orchestrator.rsi_r01 import task_contract as TC

SCHEMA_TASK_PACK = "simpraxis.rsi_r01.task_pack.v1"
SCHEMA_TASK_LOCK = "simpraxis.rsi_r01.task_lock.v1"

#: 出现在公开任务包里即泄题的键（与 task_contract 的私有键单一源）。
_PRIVATE = set(TC._SECRET_KEYS) | {"fault", "expected"}


class TaskBuildError(RuntimeError):
    """任务包不合法（维护侧 bug ⇒ 不能拿去跑候选）。"""


def _leaks(obj: Any, path: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(obj, Mapping):
        for key, value in obj.items():
            if str(key) in _PRIVATE:
                found.append(f"{path}.{key}".lstrip("."))
            found.extend(_leaks(value, f"{path}.{key}"))
    elif isinstance(obj, list):
        for i, value in enumerate(obj):
            found.extend(_leaks(value, f"{path}[{i}]"))
    return found


def public_task(inst: TC.Instance) -> dict[str, Any]:
    """公开任务书：任务/接口/边界/目标 + 公开基础场景（故障由宿主私有注入）。"""
    doc: dict[str, Any] = {
        "schema": SCHEMA_TASK_PACK,
        "instance_id": inst.instance_id,
        "family": inst.family,
        "role": inst.role,
        "template": inst.template,
        "public": dict(inst.public),
        "base_scene_xml": inst.scene_xml(),   # **未注入故障**的公开基础场景
        "host_note": ("可信服务由宿主按实例合同构造场景并执行请求；候选只给参数。"
                      "D 族实例的施加层故障不在此文件中公开。"),
    }
    leaks = _leaks(doc)
    if leaks:
        raise TaskBuildError(f"{inst.instance_id} 公开任务包含私有键 {leaks}")
    return doc


def build(*, repo_root: Path, campaign_root: Path) -> dict[str, Any]:
    problems = TC.assert_matrix_complete() + TC.validate_fault_table()
    if problems:
        raise TaskBuildError("任务表不合法：" + "; ".join(problems[:5]))

    bench = ensure_private_dir(repo_root / "benchmarks" / "rsi_r01", 0o755)
    public_root = ensure_private_dir(campaign_root / "public" / "tasks")
    private_root = ensure_private_dir(campaign_root / "private" / "instances")
    protocol_root = ensure_private_dir(campaign_root / "protocol")

    tasks: dict[str, Any] = {}
    for inst in TC.INSTANCES:
        doc = public_task(inst)
        path = bench / "tasks" / inst.instance_id / "task.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        sha = S.write_canonical_json(path, doc)
        # campaign 只读副本（0400）：候选在 trial 内读的就是这一份。
        copy = public_root / inst.instance_id / "task.json"
        copy.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(copy, S.canonical_json(doc) + "\n", mode=0o400)
        # 私有真值（0600）只落数据层。
        S.write_canonical_json(private_root / f"{inst.instance_id}.json",
                               inst.as_private_dict())
        tasks[inst.instance_id] = {"task_sha256": sha,
                                   "family": inst.family, "role": inst.role}

    matrix = TC.task_matrix()
    matrix_sha = S.write_canonical_json(bench / "task_matrix.json", matrix)
    contract = TC.measurement_contract()
    contract_sha = S.write_canonical_json(bench / "measurement_contract.json",
                                          contract)
    private_sha = S.write_canonical_json(campaign_root / "private" / "task_matrix.json",
                                         TC.private_matrix())

    lock = {
        "schema": SCHEMA_TASK_LOCK,
        "campaign_id": Path(campaign_root).name,
        "created_utc": S.utc_now(),
        "instances": len(TC.INSTANCES),
        "tasks": tasks,
        "task_matrix_sha256": matrix_sha,
        "measurement_contract_sha256": contract_sha,
        "private_matrix_sha256": private_sha,
        "dev_order": list(TC.DEV_IDS),
        "validation_order": list(TC.VALIDATION_IDS),
        "d_design": "v2（external_force_bias / friction_scale / actuator_deadzone）",
    }
    lock_sha = S.write_canonical_json(protocol_root / "t2_tasks_lock.json", lock)
    return {"lock_sha256": lock_sha, "instances": len(tasks),
            "task_matrix_sha256": matrix_sha,
            "measurement_contract_sha256": contract_sha,
            "private_matrix_sha256": private_sha}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="RSI-R0.1 T2 任务包构建（零模型调用）")
    ap.add_argument("--repo-root", default=str(_REPO_ROOT))
    ap.add_argument("--campaign-root", default=None)
    args = ap.parse_args(argv)
    root = (Path(args.campaign_root) if args.campaign_root
            else RP.rsi_r01_campaign_root())
    if not root.is_dir():
        raise SystemExit(f"campaign 根不存在：{root}")
    out = build(repo_root=Path(args.repo_root), campaign_root=root)
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
