#!/usr/bin/env python3
"""Phase 2.8.1 单 Trial CLI（计划 §8.4 / §8.6 步骤 2 / AC-P281-4-18）。

一个 Trial = 冻结任务 × 一条实验臂 × 一个重复轮次。本脚本是**唯一**的对外入口：
它只做四件事——解析参数、取冻结任务、调 :func:`trial_harness.run_trial`、打印终态。

    python scripts/benchmark/phase281_run_trial.py \\
        --task mujoco_contact_push_v1 --arm legacy_v1 --rep 0 --dry-run

三种模式：

* ``--dry-run``（**0 付费、0 触网**）：假网关返回空补丁，候选必然产不出入口
  （``NOT_BORN``）。它验证的是流水线骨架（目录 / 取证 / 说明 / 分派 / 交付 /
  清单 / 终态），**不是**候选能力；结果落 ``trial.json.simulated=true`` 且同目录
  留 ``SIMULATED.json``，真实运行会**拒绝**该目录（防止干跑残留混进证据包）。
* ``--probe-run-form``（只读探针，**不发模型请求**）：实测该任务的运行形态能不能
  起来，写 ``<root>/analysis/run_form_probe.json``（AC-18 要求逐项在场
  ``python_v`` / ``import_ok`` / ``rc``）。**真实付费 Trial 之前必须先把这件
  探针件跑出来**：探针红的形态下开跑，等于拿付费额度去证明一个已知的环境错误。
* 缺省（真实运行，**计费**）：网关是 ``ModelGateway(ledger=QuotaLedger.phase281())``，
  全程过一次 ``CapGuardGateway``；第 11 次调用在联网之前被拦。

**付费守卫（锁内适配，硬规则 2）**：计划 §8.4 只写了 ``--task --arm --rep`` 与
``--dry-run``，没有写「真实运行要不要额外确认」。本脚本在真实运行前要求环境变量
``SIMPRAXIS_ALLOW_PAID=1``——与单测 ``paid`` marker 的守卫同一条纪律：**误敲一次
命令不该花掉预算**。干跑与探针不需要它。

**数据根**：缺省 ``paths.phase281_root()``（``$SIMPRAXIS_PHASE281_ROOT`` >
``/mnt/artifacts/simpraxis/phase281``）。``--root`` 只用于测试/干跑演练，
**不**写进任何产物（产物里的路径都能从根自证）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:  # 允许 `python scripts/...` 裸跑（与 phase281_freeze 同）
    sys.path.insert(0, str(REPO_ROOT))

from orchestrator.benchmark.phase281 import (  # noqa: E402  依赖仓根已在 sys.path
    arm_adapter,
    candidate_birth,
    constants,
    paths,
    trial_harness,
)
from orchestrator.common.fsutil import atomic_write_text, ensure_private_dir  # noqa: E402

PAID_ENV = "SIMPRAXIS_ALLOW_PAID"
#: 探针件的相对落点（AC-P281-4-18：``analysis/run_form_probe.json``）
PROBE_RELPATH = Path("analysis") / "run_form_probe.json"


class CliError(RuntimeError):
    """参数/前置条件不成立（**不是** Trial 的终态之一）。"""


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="phase281_run_trial",
        description="Phase 2.8.1 单 Trial（--dry-run 为假网关、0 付费）")
    ap.add_argument("--task", choices=list(constants.TASK_IDS), required=True,
                    help="冻结任务 id（登记名，不是目录名）")
    # 探针模式与臂无关（AC-18 的形态闸是臂盲的），故 --arm 只在真跑/干跑时必需
    ap.add_argument("--arm", choices=list(constants.ARMS), default=None,
                    help="实验臂（冻结表 ARMS 的成员；--probe-run-form 时可省）")
    ap.add_argument("--rep", type=int, default=0,
                    help=f"重复轮次，[0, {constants.REPS_TOTAL})（缺省 0）")
    ap.add_argument("--root", default=None,
                    help="数据根（缺省取 paths.phase281_root()）")
    ap.add_argument("--dry-run", action="store_true",
                    help="假网关干跑：0 付费、0 触网；产物带 SIMULATED 标记")
    ap.add_argument("--probe-run-form", action="store_true",
                    help="只跑运行形态探针并写 analysis/run_form_probe.json")
    ap.add_argument("--json", action="store_true", help="以 JSON 打印终态")
    ap.add_argument("--batch", default=None,
                    help="计费批量身份（计划 §11.3 的任务票，如 P281-B1b-1）；"
                         "缺省按 trial_id 记（首轮口径）")
    return ap


def _root(value: "str | None") -> Path:
    return Path(value) if value else paths.phase281_root()


def _guard_paid(dry_run: bool) -> None:
    """真实运行前的付费守卫（干跑/探针不受限）。"""
    if dry_run:
        return
    if os.environ.get(PAID_ENV) != "1":
        raise CliError(
            f"真实 Trial 会经网关实呼计费：请显式设置 {PAID_ENV}=1 后再跑"
            "（或改用 --dry-run / --probe-run-form，两者 0 付费）")


def _write_probe(root: Path, doc: dict) -> Path:
    """探针件落盘（0700 目录 / 0600 文件，原子写）。

    一件文件装**三个任务**的探针（``probes[task_id]``），而不是一方覆盖一方：
    AC-P281-4-18 要的是「跑过的那条任务形态有件可查」，三个任务逐个跑完之后
    后跑的不能把先跑的抹掉——证据包要能同时回答三条任务的运行形态。
    """
    target = Path(root) / PROBE_RELPATH
    ensure_private_dir(target.parent)
    probes: "dict[str, dict]" = {}
    if target.is_file():
        try:
            existing = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            existing = None
        if isinstance(existing, dict) and isinstance(existing.get("probes"), dict):
            probes = {str(k): dict(v) for k, v in existing["probes"].items()
                      if isinstance(v, dict)}
    task_id = str(doc.get("task_id") or "")
    if not task_id:
        raise CliError("探针文档缺 task_id")
    probes[task_id] = dict(doc)
    payload = {"schema": "simpraxis.phase281.run_form_probe.v1",
               "relpath": PROBE_RELPATH.as_posix(),
               "probes": probes,
               "updated_at": doc.get("probed_at")}
    atomic_write_text(target, json.dumps(payload, ensure_ascii=False, indent=2,
                                         sort_keys=True) + "\n", mode=0o600)
    return target


def run_probe(*, task_id: str, root: Path, runner=None) -> dict:
    """跑一次运行形态探针并落盘；返回 ``(文件文档, 路径)`` 二元组中的文档。"""
    spec = candidate_birth.load_task(task_id)
    kwargs = {} if runner is None else {"runner": runner}
    doc = trial_harness.probe_run_form(spec, **kwargs)
    path = _write_probe(root, doc)
    doc = dict(doc)
    doc["path"] = str(path)
    return doc


def _gateway(ledger: Any) -> Any:
    """真实运行的网关：**计划 §11.3 钉 ``max_attempts=1``**（一次逻辑调用一次供应侧请求）。

    锁内适配 A7-7：本 Task 之前这里用 ``ModelGateway`` 的缺省 ``max_attempts=5``，
    与 §11.3 那一行不符（Pilot 已按 5 跑过，见 P281-7 Complete 的 D4 登记：账本 34 行
    是**逻辑调用**数，不bound供应侧请求数——4 条 ``EmptyResponseError`` 各在内部发了
    5 次）。此处按计划改回 1：逻辑调用与供应侧请求一一对应，账本才兜得住预算。
    """
    from orchestrator.gateway.router import ModelGateway

    return ModelGateway(ledger=ledger, max_attempts=1)


def run(*, task_id: str, arm_id: str, rep: int, root: Path,
        dry_run: bool, gateway=None, ledger=None, batch_id=None,
        **kwargs) -> trial_harness.TrialOutcome:
    """调流水线跑一个 Trial（本函数可被测试直接调用，不起 CLI 解析）。

    ``batch_id``：计费批量身份。缺省 ``None`` ⇒ harness 用 ``trial_id``（P281-7
    首轮口径）；传任务票（``P281-B1b-<n>``）⇒ 按计划 §11.3 记（锁内适配 A7-8）。
    """
    _guard_paid(dry_run)
    spec = candidate_birth.load_task(task_id)
    if not dry_run and (gateway is None or ledger is None):
        from orchestrator.gateway.quota_ledger import QuotaLedger

        ledger = ledger if ledger is not None else QuotaLedger.phase281()
        gateway = gateway if gateway is not None else _gateway(ledger)
    return trial_harness.run_trial(spec=spec, arm_id=arm_id, rep=int(rep),
                                   root=Path(root), gateway=gateway,
                                   ledger=ledger if ledger is not None
                                   else paths.ledger_path(),
                                   dry_run=bool(dry_run),
                                   batch_id=batch_id, **kwargs)


def main(argv: "list[str] | None" = None) -> int:
    args = _parser().parse_args(argv)
    # 与 run_trial 同一条入口断言：本阶段任何进程里 2.8.0 的根 env 都必须未设置
    paths.assert_no_benchmark_root_env()
    root = _root(args.root)

    if args.probe_run_form:
        doc = run_probe(task_id=args.task, root=root)
        printable = {k: doc.get(k) for k in
                     ("task_id", "runtime", "python", "image", "python_v",
                      "import_ok", "rc", "path", "stderr_tail")}
        print(json.dumps(printable, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if int(doc.get("rc") or 0) == 0 and doc.get("import_ok") else 1

    try:
        if not args.arm:
            raise CliError("缺 --arm：真实运行与干跑都必须指定实验臂（--probe-run-form 除外）")
        outcome = run(task_id=args.task, arm_id=args.arm, rep=args.rep,
                      root=root, dry_run=bool(args.dry_run),
                      batch_id=args.batch)
    except CliError as exc:
        print(f"[phase281-run-trial] {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(outcome.to_json(), ensure_ascii=False,
                         indent=2, sort_keys=True))
    else:
        print(f"[phase281-run-trial] {outcome.trial_id} status={outcome.status} "
              f"used_calls={outcome.used_calls} dir={outcome.trial_dir}")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main())
