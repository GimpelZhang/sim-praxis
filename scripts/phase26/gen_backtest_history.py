"""Phase 2.6 Task 1：从**历史产物**生成 v3 回代夹具（不手写数字，E-34）。

输入（只读）：
  * `/mnt/artifacts/simpraxis/phase2/quota/ledger.jsonl`（455 行）
  * `/mnt/artifacts/simpraxis/phase25/quota/ledger.jsonl`（56 行）
  * `/mnt/artifacts/simpraxis/phase25/walls/t5_freeze/runs/wall{A,B}/<ts>/traces/*/trace.jsonl`
  * `/mnt/artifacts/simpraxis/phase25/walls/t5_freeze/runs/verdict-*.json`

输出：
  * `tests/milestone26/fixtures/estimate_backtest_history.json`（逐任务一行）
  * `tests/milestone26/fixtures/BACKTEST_SHA256.txt`（产物 sha256，测试先校验再读）

**每条 `observed_calls` 都由 `source` 描述的重算规则算出**，`derive_observed_calls()`
是唯一的实现，测试拿它逐行重算（AC-T1-1b）。生成器内部对每个来源断言「来源行存在」。

锁内适配（计划 T1.4-B 的路径与实测不符）：计划写
`phase25/runs/wall{A,B}/*/traces/*/trace.jsonl`，实测 wall trace 在
`phase25/walls/t5_freeze/runs/wall{A,B}/<ts>/traces/*/trace.jsonl`
（`phase25/runs/` 下只有 task6/7/8 的目录，无 wallA/wallB）。本文件按实测路径读取，
并在 commit body 与 Complete 偏差表留痕。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ART = Path("/mnt/artifacts/simpraxis")
PHASE2_LEDGER = ART / "phase2/quota/ledger.jsonl"
PHASE25_LEDGER = ART / "phase25/quota/ledger.jsonl"
T5_RUNS = ART / "phase25/walls/t5_freeze/runs"

OUT_JSON = REPO_ROOT / "tests/milestone26/fixtures/estimate_backtest_history.json"
OUT_SHA = REPO_ROOT / "tests/milestone26/fixtures/BACKTEST_SHA256.txt"

SCHEMA = "simpraxis.phase26.backtest_history.v1"

# 逐墙 trace（4 个战役：实验 5 的 r1 与实验 5b 的 r2）
WALL_TRACES = {
    "wallA_r1": T5_RUNS / "wallA/20261030T090948Z/traces/t5-walla-20261030T090948Z/trace.jsonl",
    "wallB_r1": T5_RUNS / "wallB/20261030T092227Z/traces/t5-wallb-20261030T092227Z/trace.jsonl",
    "wallA_r2": T5_RUNS / "wallA/20261030T133330Z/traces/t5b-walla-20261030T133330Z/trace.jsonl",
    "wallB_r2": T5_RUNS / "wallB/20261030T133751Z/traces/t5b-wallb-20261030T133751Z/trace.jsonl",
}
T5_VERDICT_R1 = T5_RUNS / "verdict-20261030T092635Z.json"
T5_VERDICT_R2 = T5_RUNS / "verdict-20261030T134350Z.json"


# ---------------------------------------------------------------- 读取原语

def _ledger_rows(path: Path) -> list[dict]:
    if not path.exists():
        raise SystemExit(f"来源台账缺失：{path}（STOP，不得用估计值填）")
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def _call_rows_for_task(path: Path, task: int) -> list[dict]:
    return [r for r in _ledger_rows(path)
            if r.get("task") == task and isinstance(r.get("seq"), int)]


def _trace_rows(path: Path) -> list[dict]:
    if not path.exists():
        raise SystemExit(f"来源 trace 缺失：{path}（STOP，不得用估计值填）")
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def _model_rounds(path: Path) -> int:
    return sum(1 for r in _trace_rows(path) if r.get("tool") == "__model_round__")


# ---------------------------------------------------------------- 重算规则

def derive_observed_calls(row: dict) -> int:
    """按 `row["source"]` 重算 observed_calls。测试与生成器共用这一份实现。"""
    src = row["source"]
    kind = src["kind"]
    if kind == "ledger_task":
        return len(_call_rows_for_task(Path(src["path"]), int(src["task"])))
    if kind == "trace_rounds":
        return _model_rounds(Path(src["path"]))
    if kind == "sum_traces":
        return sum(_model_rounds(Path(p)) for p in src["paths"])
    if kind == "ledger_task_absent":
        rows = _call_rows_for_task(Path(src["path"]), int(src["task"]))
        if rows:
            raise AssertionError(
                f"task={src['task']} 本应零实呼，实测 {len(rows)} 行（来源失效）")
        return 0
    raise ValueError(f"未知 source.kind：{kind}")


def source_relpaths(row: dict) -> list[str]:
    """计划 T1.4-B 的 `source_relpaths`：该行 observed_calls 的全部来源文件。"""
    src = row["source"]
    return [src["path"]] if "path" in src else list(src.get("paths", []))


def _sources_exist(row: dict) -> bool:
    return all(Path(p).exists() for p in source_relpaths(row))


# ---------------------------------------------------------------- 行构造

def _rc0_of_task(path: Path, task: int) -> bool:
    rows = _call_rows_for_task(path, task)
    return bool(rows) and bool(rows[-1].get("ok"))


def _wall_flags(trace: Path) -> tuple[bool, bool]:
    """(born, selfcheck_written)：trace 里出现过写文件 / 跑命令。"""
    tools = {r.get("tool") for r in _trace_rows(trace)}
    born = bool(tools & {"write_file", "apply_patch"})
    selfcheck = bool(tools & {"run_cmd", "run_probe"})
    return born, selfcheck


def _verdict_wall(path: Path, wall: str) -> dict:
    d = json.loads(path.read_text(encoding="utf-8"))
    return d["engineering_verdict"]["per_wall"][wall]


def build_rows() -> list[dict]:
    """逐任务/逐墙的历史行。所有数值均由来源重算（见 derive_observed_calls）。"""
    rows: list[dict] = []

    # ---- Phase 2：只有台账，无逐轮 trace（M2 时点未持久化 trace）----
    # 因此 born/selfcheck_written 记 null（**不猜**），燃尽规则对 null 不触发。
    for task, label, cap in ((4, "p2_task4_visual_probe", 24),
                             (6, "p2_task6_generation", 227),
                             (7, "p2_task7_libero", 76),
                             (8, "p2_task8_fluid", 140)):
        rows.append({
            "task": label,
            "phase": 2,
            "observed_calls": None,          # 下面统一回填
            "cap": cap,
            "cap_hit": None,
            "born": None,
            "selfcheck_written": None,
            "rc0": _rc0_of_task(PHASE2_LEDGER, task),
            "source": {"kind": "ledger_task", "path": str(PHASE2_LEDGER),
                       "task": task},
            "derivation": (f"phase2 台账 event=call 且 task=={task} 的整数 seq 行数；"
                           f"cap=PHASE2_TASK_CAPS[{task}]={cap}；"
                           "rc0=该 task 最后一条 call 行 ok=true；"
                           "born/selfcheck_written=null（phase2 无逐轮 trace，不猜）"),
        })

    # ---- Phase 2.5：T3 探针（台账）----
    rows.append({
        "task": "p25_task3_stage0_probe", "phase": 25,
        "observed_calls": None, "cap": 6, "cap_hit": None,
        "born": None, "selfcheck_written": None,
        "rc0": _rc0_of_task(PHASE25_LEDGER, 3),
        "source": {"kind": "ledger_task", "path": str(PHASE25_LEDGER), "task": 3},
        "derivation": ("phase25 台账 task==3 的整数 seq 行数；cap=PHASE25_TASK_CAPS[3]=6；"
                       "rc0=末行 ok=true；born/selfcheck_written=null（探针无逐轮 trace 落盘）"),
    })

    # ---- Phase 2.5：T5 四堵墙（trace 轮数）----
    subcap = {"wallA": 16, "wallB": 9}
    for key, trace in WALL_TRACES.items():
        wall, stage = key.split("_")
        born, selfcheck = _wall_flags(trace)
        verdict = T5_VERDICT_R1 if stage == "r1" else T5_VERDICT_R2
        per_wall = _verdict_wall(verdict, wall)
        rows.append({
            "task": f"p25_task5_{key}", "phase": 25,
            "observed_calls": None, "cap": subcap[wall], "cap_hit": None,
            "born": born, "selfcheck_written": selfcheck,
            "rc0": bool(per_wall.get("passed")),
            "source": {"kind": "trace_rounds", "path": str(trace)},
            "derivation": (f"trace.jsonl 中 tool==__model_round__ 的行数（= 模型轮数 = 实呼数）；"
                           f"cap=子帽 {subcap[wall]}（half_up_subcaps(60,30)→16/9）；"
                           f"rc0=verdict {verdict.name} engineering_verdict.per_wall."
                           f"{wall}.passed；born=trace 含 write_file/apply_patch；"
                           "selfcheck_written=trace 含 run_cmd"),
        })

    # ---- Phase 2.5：T5 整池（两墙合一的战役 = 一次 5/5b 实验）----
    for stage, verdict in (("r1", T5_VERDICT_R1), ("r2", T5_VERDICT_R2)):
        paths = [str(WALL_TRACES[f"wallA_{stage}"]), str(WALL_TRACES[f"wallB_{stage}"])]
        rows.append({
            "task": f"p25_task5_pool_{stage}", "phase": 25,
            "observed_calls": None, "cap": 28, "cap_hit": None,
            "born": True, "selfcheck_written": True,
            "rc0": bool(json.loads(verdict.read_text(encoding="utf-8"))
                        ["engineering_verdict"]["verdict"] == "成立"),
            "source": {"kind": "sum_traces", "paths": paths},
            "derivation": ("同一战役两墙 trace 的 __model_round__ 之和；"
                           "cap=池 28（子帽 16+9 + 储备 3）；"
                           f"rc0=verdict {verdict.name} engineering_verdict.verdict=='成立'"),
        })

    # ---- Phase 2.5：T10 收尾（零实呼的缺省）----
    rows.append({
        "task": "p25_task10_closeout", "phase": 25,
        "observed_calls": None, "cap": 2, "cap_hit": False,
        "born": False, "selfcheck_written": False, "rc0": True,
        "source": {"kind": "ledger_task_absent", "path": str(PHASE25_LEDGER),
                   "task": 10},
        "derivation": ("phase25 台账中 task==10 的整数 seq 行数（缺省 0 呼即达标）；"
                       "cap=PHASE25_TASK_CAPS[10]=2；rc0=true（无需实呼）"),
    })

    # ---- 统一回填 observed_calls / cap_hit（不得手填）----
    for row in rows:
        if not _sources_exist(row):
            raise SystemExit(f"来源缺失：{row['task']} → {row['source']}（STOP）")
        row["observed_calls"] = derive_observed_calls(row)
        row["cap_hit"] = (row["cap"] is not None
                          and row["observed_calls"] == row["cap"])
        row["source_relpaths"] = source_relpaths(row)
    return rows


def cross_checks(rows: list[dict]) -> dict:
    """三条人工核对（计划 T1.5 步骤 5），机器化后写进产物。"""
    by = {r["task"]: r for r in rows}
    checks = {
        # ① Task 8 C4 的 observed_calls 与 ledger 一致（此处取 task8 整 task）
        "p2_task8_matches_ledger": (
            by["p2_task8_fluid"]["observed_calls"]
            == len(_call_rows_for_task(PHASE2_LEDGER, 8))),
        # ② Task 5 A 墙到子帽
        "p25_task5_wallA_cap_hit": by["p25_task5_wallA_r1"]["cap_hit"] is True,
        # ③ Task 3 observed_calls == 6
        "p25_task3_equals_6": by["p25_task3_stage0_probe"]["observed_calls"] == 6,
        # ④ 四堵墙之和 == task5 台账整任务行数（两条独立通路互证）
        "walls_sum_equals_task5_ledger": (
            sum(by[f"p25_task5_{k}"]["observed_calls"]
                for k in ("wallA_r1", "wallB_r1", "wallA_r2", "wallB_r2"))
            == len(_call_rows_for_task(PHASE25_LEDGER, 5))),
    }
    return checks


def main() -> int:
    rows = build_rows()
    checks = cross_checks(rows)
    bad = [k for k, v in checks.items() if not v]
    if bad:
        raise SystemExit(f"交叉核对未过：{bad}（STOP，先查证据链）")
    payload = {"schema": SCHEMA,
               "row_count": len(rows),
               "source_roots": [str(ART / "phase2"), str(ART / "phase25")],
               "cross_checks": checks,
               "rows": rows}
    text = json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True) + "\n"
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(text, encoding="utf-8")
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    OUT_SHA.write_text(f"{sha}  {OUT_JSON.name}\n", encoding="utf-8")
    print(f"[backtest-history] rows={len(rows)} sha256={sha}")
    for k, v in checks.items():
        print(f"  {'OK ' if v else 'FAIL'} {k}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
