"""Phase 2.8.1：把 Phase 2.8.0 参考运行包装成**可评测的 Trial 目录**。

为什么需要这一层：P281-1 的 parity（AC-P281-1-10）与后续出生自检都要一份
「已知正确答案」的输入，而 Phase 2.8.0 落盘的是**运行产物**（平铺文件 +
``birth/`` + ``candidate/``），不是 ``candidate_trial_manifest.v1`` 的 Trial
形态（``delivered/`` 冻结副本 + 运行器 telemetry + 出生件 + 清单）。

本模块只做**搬运与包装**：判据一个字都不碰，清单一律经
:func:`orchestrator.benchmark.phase281.candidate_manifest.build_manifest` 这
一生产构造点按磁盘真实字节生成（自报值从不进清单）。

包装件里有一处是**本模块构造的**，报告里要写明：

- ``trial_dir/telemetry.jsonl``（Trial 根的**运行器遥测**，不是
  ``delivered/telemetry.jsonl`` 那份产物）：只写一行 ``event=summary``，字段
  取自 ``claimed``。真实 summary 行由 P281-4 的运行器写入；参考运行没有它，
  因此在参考快照里这一行是占位。

``birth.json`` 不构造：参考运行自带 ``birth/functional_birth.json``（含
``birth_status`` 与四层证据），**逐字节复制**即可。

``delivered/`` 的搬运是**排除式**而非白名单：三个任务的产物集不同
（M1 有 ``metrics.json``/``repro/``；L1 有 ``adapter.py``/``env_probe.json``；
R1 有 ``evaluation_metrics.json``/``research_report.md``……），白名单会悄悄
漏件，而漏件在评测器眼里是 INVALID_ARTIFACT——把「搬漏了」伪装成「候选缺件」
正是本阶段要避免的那类错误。排除的只有两类**不属于产物**的东西：``candidate/``（候选工作区原文，含
``__pycache__``，与 ``delivered/`` 的冻结副本语义冲突）与 ``capture_meta.json``
（宿主侧采集元数据）。

**``birth/`` 必须搬**：L1 的结构判据（``scoring.libero_facts``）就是把
``birth/functional_birth.json`` 当**产物**读的（判据取的是件里的层级状态，
不是评测器自留的出生件）。早先若把它当「评测器的东西」排除掉，L1 会满屏
``B1=None``——那是搬运错误被伪装成候选缺件。
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Mapping

from orchestrator.benchmark.phase281 import candidate_manifest as cm
from orchestrator.benchmark.phase281 import constants
from orchestrator.common.fsutil import atomic_write_text, ensure_private_dir

__all__ = (
    "BIRTH_FILE", "DEFAULT_CLAIMED", "PAYLOAD_EXCLUDE", "RUNNER_TELEMETRY",
    "build_reference", "build_trial", "copy_payload", "payload_relpaths",
    "read_json", "sha256_file", "write_json",
)

#: 出生件在 Trial 根的文件名（与 ``CandidateEvaluator._birth`` 的读取面同名）
BIRTH_FILE = "birth.json"
#: 运行器遥测在 Trial 根的文件名
RUNNER_TELEMETRY = "telemetry.jsonl"

#: 不搬进 ``delivered/`` 的条目（理由见模块 docstring 末段）。``_provenance.json``
#: 是 ``tests/fixtures/benchmark`` 的注记件（真实运行没有它），一并不搬。
PAYLOAD_EXCLUDE = ("candidate", "capture_meta.json",
                   "__pycache__", ".omc", "_provenance.json")

#: 包装件缺省自述：零付费、零轮次、首轮出生（与「无账本行」的观测一致）
DEFAULT_CLAIMED = {"paid_model_calls": 0, "coding_rounds": 0,
                   "first_birth_round": 1, "tool_steps": 0,
                   "tokens_in": 0, "tokens_out": 0}


def sha256_file(path: "str | Path") -> str:
    import hashlib

    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_json(path: "str | Path") -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path: "str | Path", doc: Any) -> Path:
    """原子写 0600（数据根纪律：所有写文件走 ``atomic_write_*``）。"""
    target = Path(path)
    ensure_private_dir(target.parent)
    atomic_write_text(target, json.dumps(doc, ensure_ascii=False, indent=2,
                                        sort_keys=True) + "\n", mode=0o600)
    return target


def payload_relpaths(src: "str | Path") -> "list[str]":
    """一份参考运行目录里**属于产物**的相对路径清单（POSIX 序）。"""
    base = Path(src)
    out: "list[str]" = []
    for path in sorted(base.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(base)
        if rel.parts[0] in PAYLOAD_EXCLUDE:
            continue
        if any(part in PAYLOAD_EXCLUDE for part in rel.parts):
            continue
        out.append(rel.as_posix())
    return out


def copy_payload(src: "str | Path", delivered: "str | Path") -> "list[str]":
    """把产物复制进 ``delivered/``；返回搬运的相对路径（= 清单顺序）。"""
    source = Path(src)
    target = Path(delivered)
    ensure_private_dir(target)
    rels = payload_relpaths(source)
    for rel in rels:
        dst = target / rel
        ensure_private_dir(dst.parent)
        shutil.copyfile(source / rel, dst)
        dst.chmod(0o600)
    return rels


def build_trial(dst: "str | Path", *, payload_src: "str | Path",
                task_id: str, arm: str = "legacy_v1", rep: int = 0,
                claimed: "Mapping[str, Any] | None" = None,
                birth_doc: "Mapping[str, Any] | None" = None,
                source_note: "str | None" = None,
                config_digest: str = "0" * 64) -> Path:
    """把 ``payload_src`` 包成一个合法 Trial 目录并返回该目录。

    ``birth_doc`` 为 ``None`` ⇒ **不写** ``birth.json``（用于「缺出生件」的负例）；
    传入即逐字落盘。``claimed`` 缺省取 :data:`DEFAULT_CLAIMED`。
    """
    trial_dir = Path(dst)
    if trial_dir.exists():
        shutil.rmtree(trial_dir)
    ensure_private_dir(trial_dir)
    trial_id = constants.trial_id(task_id, arm, rep)
    rels = copy_payload(payload_src, trial_dir / cm.OUTPUT_PREFIX)

    #: 输入件：记录「这份产物是从哪来的」。评测器不读输入，但清单契约要求非空，
    #: 且这一件让包装件保持可追溯（真实运行由 P281-4 的运行器写输入快照）。
    write_json(trial_dir / "input" / "task_snapshot.json", {
        "task_id": task_id,
        "source": source_note or Path(payload_src).as_posix(),
        "note": "参考运行包装件：Phase 2.8.0 产物不得改写，只做搬运",
    })

    claims = dict(DEFAULT_CLAIMED if claimed is None else claimed)
    manifest = cm.build_manifest(
        trial_dir, trial=trial_id, arm=arm, task_id=task_id,
        runtime_mode="host",
        fingerprint_sha256=sha256_file(
            trial_dir / cm.OUTPUT_PREFIX / "runtime_fingerprint.json"),
        input_relpaths=["input/task_snapshot.json"],
        deliverable_relpaths=[f"{cm.OUTPUT_PREFIX}{rel}" for rel in rels],
        claimed=claims,
        engine_entry="agent_runner",
        config_digest=config_digest)
    write_json(trial_dir / cm.MANIFEST_NAME, manifest)

    if birth_doc is not None:
        write_json(trial_dir / BIRTH_FILE, dict(birth_doc))

    summary = {"event": "summary", "trial_id": trial_id,
               "coding_rounds": claims["coding_rounds"],
               "tool_steps": claims["tool_steps"],
               "first_birth_round": claims["first_birth_round"]}
    atomic_write_text(
        trial_dir / RUNNER_TELEMETRY,
        json.dumps(summary, ensure_ascii=False, sort_keys=True) + "\n",
        mode=0o600)
    return trial_dir


def build_reference(root: "str | Path", task_id: str,
                    run_dir: "str | Path") -> dict:
    """把 ``run_dir``（Phase 2.8.0 的钉死参考运行）包成 ``<root>/references/<task>``。

    返回摘要：``{task_id, run_id, trial_id, verdict 前的清单件数, birth_status,
    payload 件数, run_manifest_sha256}``。**不改写**来源目录（只读复制）。
    """
    run = Path(run_dir)
    run_manifest = read_json(run / "run_manifest.json")
    if run_manifest.get("task_id") != task_id:
        raise ValueError(f"run_manifest.task_id={run_manifest.get('task_id')!r} "
                         f"≠ {task_id!r}（{run}）")
    birth_path = run / "birth" / "functional_birth.json"
    if not birth_path.is_file():
        raise ValueError(f"参考运行缺出生证据：{birth_path}")
    birth_doc = read_json(birth_path)

    claimed = dict(DEFAULT_CLAIMED)
    claimed["paid_model_calls"] = int(run_manifest.get("paid_model_calls") or 0)

    target = ensure_private_dir(Path(root) / "references" / task_id)
    build_trial(target, payload_src=run, task_id=task_id,
                claimed=claimed, birth_doc=birth_doc,
                source_note=f"phase2.8.0:{run.name}")
    return {
        "task_id": task_id,
        "run_id": run.name,
        "trial_id": constants.trial_id(task_id, "legacy_v1", 0),
        "birth_status": birth_doc.get("birth_status"),
        "payload_files": len(payload_relpaths(run)),
        "run_manifest_sha256": sha256_file(run / "run_manifest.json"),
        "run_status": run_manifest.get("status"),
        "repository_sha": run_manifest.get("repository_sha"),
        "target": str(target),
    }
