"""Phase 2.8.1 P281-9 收尾车道的**合成数据根**（两条测试共用的唯一造根处）。

纪律（与 ``phase281_trial_fixtures.py`` 同一条）：

* 只造形状，不造真钱——合成根的账本行不与任何生产 seq 重叠，``batch_id``
  一律 ``SYN-`` 前缀，``approval_ref`` 不指向真实批准件；
* 每个造根函数都可**按 trial_id 逐项覆盖**，负例不需要手改 JSON 字面；
* ``table_counts.json`` / ``ledger_state.json`` 允许指向 tmp 路径——机判读的是
  「记录与实际是否相符」，不是「这些路径必须存在生产件」。
"""
from __future__ import annotations

import json
from pathlib import Path

from orchestrator.benchmark.phase281 import analysis, constants

TS_START = "2026-11-07T00:00:00Z"
TS_END = "2026-11-07T00:01:40Z"


def write_json(path: Path, doc) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=1,
                               sort_keys=True) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def trial_doc(*, status: str = "FINISHED", birth: str = "BORN",
              first_round: "int | None" = 2, tokens: "tuple[int, int]" = (10, 5),
              engine: "dict | None" = None,
              created_at: str = TS_START, finished_at: str = TS_END) -> dict:
    observed: "dict[str, object]" = {"calls": 2, "tokens_in": tokens[0],
                                     "tokens_out": tokens[1]}
    if first_round is not None:
        observed["first_birth_round"] = first_round
    return {
        "status": status,
        "birth": {"status": birth, "reason": "synthetic", "schema": "x"},
        "observed": observed,
        "engine": engine or {"status": "ok", "reason": ""},
        "problems": [],
        "created_at": created_at,
        "finished_at": finished_at,
    }


def ledger_rows(trial_ids: "list[str]", *, tokens: int = 15) -> "list[dict]":
    """一 Trial 一行实呼（seq 从 1 连续），任务号按 ``TASK_NO_BY_ID`` 现查。"""
    rows: "list[dict]" = []
    for idx, tid in enumerate(trial_ids, 1):
        task_id = tid.split("__")[0]
        rows.append({
            "ts_iso": "2026-11-07T00:00:00Z",
            "event": "call",
            "stage": "coding_debug",
            "model": "synthetic-model",
            "thread_id": tid,
            "task": constants.TASK_NO_BY_ID[task_id],
            "batch_id": f"SYN-{tid}",
            "seq": idx,
            "tokens_in": tokens,
            "tokens_out": 0,
            "ok": True,
        })
    return rows


def write_ledger(root: Path, rows: "list[dict]") -> Path:
    path = Path(root) / "quota" / "ledger.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n"
                            for r in rows), encoding="utf-8")
    path.chmod(0o600)
    return path


def make_root(root: Path, *, reps: "tuple[int, ...]" = (0,),
              trials: "dict[str, dict] | None" = None,
              verdicts: "dict[str, str] | None" = None,
              omitted: "tuple[str, ...]" = (),
              ledger_tokens: int = 15) -> Path:
    """造一个能喂 ``build_final_report`` 的根。

    ``trials[trial_id]`` 是 ``trial_doc`` 的关键字覆盖；``verdicts[trial_id]``
    写进 ``evaluator_results``；``omitted`` 里的 Trial **故意不落盘**（用来测
    「缺件也占一行」与 go/no-go 的缺件前置）。
    """
    root = Path(root)
    trials = dict(trials or {})
    verdicts = dict(verdicts or {})
    present: "list[str]" = []
    for task_id in constants.TASK_IDS:
        for arm_id in constants.ARMS:
            for rep in reps:
                tid = constants.trial_id(task_id, arm_id, rep)
                if tid in omitted:
                    continue
                td = analysis.trial_dir(root, task_id, arm_id, rep)
                write_json(td / analysis.TRIAL_JSON,
                           trial_doc(**trials.get(tid, {})))
                write_json(analysis.evaluator_result_path(root, task_id, arm_id, rep),
                           {"verdict": verdicts.get(tid, "PASS")})
                present.append(tid)
    write_ledger(root, ledger_rows(present, tokens=ledger_tokens))
    write_json(root / "intake" / "table_counts.json", {
        "generated_by": "synthetic",
        "git_head": "0" * 40,
        "lancedb_uri": str(root / "lancedb"),
        "tables": {"kb_chunks": 1542, "phase1_learnings": 44,
                   "phase2_learnings": 1, "solution_chunks": 106},
        "ts_iso": "2026-11-07T00:00:00Z",
    })
    return root


def make_confirmatory_root(root: Path, *,
                           trials: "dict[str, dict] | None" = None,
                           verdicts: "dict[str, str] | None" = None) -> Path:
    """18 个 Trial 全在场的根（H1–H5 走真判决分支的那条路）。"""
    return make_root(root, reps=tuple(range(constants.REPS_TOTAL)),
                     trials=trials, verdicts=verdicts)
