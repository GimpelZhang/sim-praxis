"""P281-1 parity 脚手架：把一份产物目录包成**合法的**候选 Trial 再评测。

为什么要有这一层：``CandidateEvaluator`` 的输入契约是 ``candidate_trial_manifest.v1``
（``delivered/`` 冻结副本 + 运行器 telemetry + 出生件 + 账本），而可用的「已知正确
答案」输入是 Phase 2.8.0 的参考运行与夹具（平铺产物 + ``run_manifest.json``）。

**打包实现只有一份**：生产侧 :mod:`orchestrator.benchmark.phase281.references`
（``$ROOT/references/<task>`` 的构建者）。本模块只是测试侧的薄封装——
额外只做两件生产侧不做的事：建一个可评的数据根（仓外锚 + 0700 目录族），以及
为夹具合成 ``birth.json``（参考运行自带真件，夹具没有）。

包装件里有两处是**构造的**，不是 Phase 2.8.0 产物自带的，报告里要写明：

- ``birth.json``：夹具按用例指定 ``birth_status``（参考运行则逐字节复制
  ``birth/functional_birth.json``）；
- ``trial_dir/telemetry.jsonl``（Trial 根的运行器遥测，**不是** ``delivered/``
  里那份产物）：只写一行 ``event=summary``，字段取自 ``claimed``——它是运行器
  契约的占位（真实 summary 行由 P281-4 的运行器产出），用于让「自述 vs 观测」
  这一层可被独立测试，而不是让测试绕过它。

``quota/ledger.jsonl`` 缺省不写（= 0 次付费调用），需要时由测试显式铺行。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterable, Mapping

from orchestrator.benchmark.phase281 import anchor as anchor_mod
from orchestrator.benchmark.phase281 import candidate_evaluator as ce
from orchestrator.benchmark.phase281 import constants
from orchestrator.benchmark.phase281 import references as refs

__all__ = (
    "DEFAULT_CLAIMED", "FIXTURE_ROOT", "GOLDEN_DIR", "NEGATIVE_DIR",
    "PROJECTION", "REPO_ROOT", "build_root", "build_trial", "copy_payload",
    "criterion_map", "ledger_row", "payload_relpaths", "project_status",
    "read_json", "sha256_file", "write_json", "write_ledger",
)

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "benchmark"
GOLDEN_DIR = FIXTURE_ROOT / "golden"
NEGATIVE_DIR = FIXTURE_ROOT / "negative"

#: 七态 → P281 四态的投影（计划 §5.4.2 的裁定表）
PROJECTION = {"INVALID_RUNTIME": "BLOCKED_INFRA",
              "INCOMPLETE_EVIDENCE": "INVALID_ARTIFACT"}

DEFAULT_CLAIMED = refs.DEFAULT_CLAIMED

read_json = refs.read_json
sha256_file = refs.sha256_file
write_json = refs.write_json
payload_relpaths = refs.payload_relpaths
copy_payload = refs.copy_payload


def project_status(status: Any) -> str:
    """把 Phase 0 的七态投影到 P281 实际产出的四态（不适用者原样返回）。"""
    return PROJECTION.get(str(status), str(status))


def ledger_row(*, trial_id: str, seq: int, tokens_in: int = 0,
               tokens_out: int = 0, task: "str | None" = None,
               model: "str | None" = None) -> dict:
    """一条 ``event="call"`` 的账本行（评测器只认 thread_id/task 与 token 三列）。"""
    return {"event": "call", "seq": seq, "thread_id": trial_id,
            "task": task, "model": model, "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "ts_iso": "2026-11-06T00:00:00Z", "ok": True}


def write_ledger(root: "str | Path", rows: Iterable[Mapping]) -> Path:
    path = Path(root) / "quota" / "ledger.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps(dict(row), ensure_ascii=False, sort_keys=True) + "\n"
                   for row in rows)
    path.write_text(text, encoding="utf-8")
    return path


def build_trial(dst: "str | Path", *, payload_src: "str | Path | None" = None,
                task_id: str, arm: str = "legacy_v1", rep: int = 0,
                claimed: "Mapping[str, Any] | None" = None,
                birth_status: "str | None" = "BORN",
                source_note: "str | None" = None) -> Path:
    """把夹具 ``payload_src`` 包成合法 Trial；``birth_status=None`` ⇒ 不写出生件。"""
    if payload_src is None:
        raise ValueError("build_trial 需要 payload_src")
    trial_id = constants.trial_id(task_id, arm, rep)
    birth_doc = None
    if birth_status is not None:
        birth_doc = {"birth_status": birth_status,
                     "source": "phase2.8.0_reference_run_or_fixture",
                     "trial_id": trial_id}
    return refs.build_trial(dst, payload_src=payload_src, task_id=task_id,
                            arm=arm, rep=rep, claimed=claimed,
                            birth_doc=birth_doc, source_note=source_note)


def build_root(root: "str | Path") -> dict:
    """建一个**可评**的数据根（0700 全目录 + 仓外锚），返回上下文。

    锚由生产写侧 :func:`anchor.write_anchor` 生成：判据/运行时快照从仓内
    ``benchmarks/research_engineering_v1/`` 拷进 ``<root>/private/`` 并逐件钉
    sha256，这样测试与生产走同一条锚路径，不另造一份假锚。
    """
    base = Path(root)
    base.mkdir(parents=True, exist_ok=True)
    os.chmod(base, 0o700)
    for name in ("quota", "private", "references", "evaluator_results"):
        (base / name).mkdir(parents=True, exist_ok=True)
        os.chmod(base / name, 0o700)
    manifest = anchor_mod.write_anchor(base, written_by="p281_tests")
    anchor = anchor_mod.load_anchor(anchor_mod.anchor_path(base))
    evaluator = ce.CandidateEvaluator(anchor=anchor, root=base)
    return {"root": base, "anchor": anchor, "manifest": manifest,
            "evaluator": evaluator}


def criterion_map(doc: Mapping) -> "dict[str, bool]":
    return {str(item["id"]): bool(item["passed"]) for item in doc["criteria"]}
