#!/usr/bin/env python
"""RSI-R1 独立闭合复算器（Plan §5.6，预先编写并版本化）。

**独立性纪律**：本文件**只依赖标准库**。它不 import ``orchestrator.rsi_r1``
的任何模块（尤其 ``closure``/``judge``/``statistics``/``replay``），预算合计、
样本集合、终判条件、CI/分类一致性全部在本地重写一遍。与生产实现共享的只有
canonical JSON 与 sha256 这类非判定工具——这里也本地重写，避免任何版本漂移。

它读的是**产物**而非内存对象：

    <root>/contract/budget.json            预算单真值（BudgetSpec.to_dict）
    <root>/confirm/manifest_<study>.json   确认分配（ConfirmManifest.to_dict）
    <root>/judge/report_<study>.json       judge 报告
    <root>/replay/comparison_<study>.json  replay 比较
    <root>/quota/ledger.jsonl              账本行（每行一个对象）
    <root>/deliverable/closure.json        各 study 的终态声明（可选）

``--verify <pack_dir>`` 额外核对证据包：``evidence_manifest.json`` 里的
``{"files": {relpath: sha256}}`` 必须与盘上逐件相等、无缺件/无增件、无符号
链接。

输出判决件带 ``validator_source_sha256``（本文件字节 sha256）与
``run_at_utc``；rc=0 PASS、rc=1 FAIL、rc=2 输入不可读/内部错误。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

SCHEMA = "simpraxis.rsi_r1.closure_judge.v1"

#: 独立字面量（Plan §8.2）。与生产表不同源是**故意的**：两边同时改才算漂移。
LANE_CAPS = {"probe": 8, "practice": 16, "study_A": 48, "study_B": 48,
             "repair_reserve": 24}
HARD_CAP = 144
NORMAL_BUDGET = 120
STUDIES = ("A", "B")
CONFIRM_BLOCKS = 32
PROPOSITIONS = ("decrease_gt", "increase_gt", "equivalent")
LANE_TASK_ID = {"probe": 0, "practice": 1, "study_A": 2, "study_B": 3,
                "repair_reserve": 4}


def canonical_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def run_id_for(plan_sha256: str, study_id: str, condition_id: str,
               block_id: str) -> str:
    material = canonical_json({"plan": plan_sha256, "study": study_id,
                               "condition": condition_id, "block": block_id})
    return "run-" + sha256_text(material)[:20]


def classify(proposition: str, delta, lower, upper) -> str:
    """独立实现的主张分类（与量表语义逐条相同，代码不共享）。"""
    d, lo, hi = Decimal(str(delta)), Decimal(str(lower)), Decimal(str(upper))
    if lo > hi:
        return "CI_INVALID"
    if proposition == "decrease_gt":
        if hi < -d:
            return "SUPPORTED"
        return "REFUTED" if lo >= -d else "INCONCLUSIVE"
    if proposition == "increase_gt":
        if lo > d:
            return "SUPPORTED"
        return "REFUTED" if hi <= d else "INCONCLUSIVE"
    if proposition == "equivalent":
        if lo > -d and hi < d:
            return "SUPPORTED"
        return "REFUTED" if (hi <= -d or lo >= d) else "INCONCLUSIVE"
    return "PROPOSITION_INVALID"


class Checker:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.checks = []
        self.problems = []

    def add(self, cid: str, ok: bool, detail: str = "") -> None:
        self.checks.append({"id": cid, "ok": bool(ok), "detail": str(detail)})
        if not ok:
            self.problems.append(f"{cid}: {detail}")

    # -- 读取 ------------------------------------------------------------
    def load(self, rel: str, required: bool = True):
        path = self.root / rel
        if not path.exists():
            if required:
                self.add(f"READ-{rel}", False, "缺件")
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            self.add(f"READ-{rel}", False, f"不可读: {exc}")
            return None

    def ledger_rows(self):
        path = self.root / "quota" / "ledger.jsonl"
        rows = []
        if not path.exists():
            self.add("READ-ledger", False, "缺 quota/ledger.jsonl")
            return rows
        with path.open("r", encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    self.add("READ-ledger", False, f"第 {lineno} 行不是 JSON")
        return rows

    # -- 检查 ------------------------------------------------------------
    def check_budget(self, budget, rows) -> None:
        self.add("B-LANE-TABLE", sum(LANE_CAPS.values()) == HARD_CAP,
                 f"sum(lane)={sum(LANE_CAPS.values())} vs {HARD_CAP}")
        self.add("B-NORMAL", NORMAL_BUDGET + LANE_CAPS["repair_reserve"] == HARD_CAP,
                 "常规 120 + 预留 24 == 144")
        if not isinstance(budget, dict):
            return
        declared = budget.get("posts_by_lane") or {}
        self.add("B-DECLARED-CAPS", dict(declared) == LANE_CAPS,
                 f"声明 {declared} vs 独立表 {LANE_CAPS}")
        calls = [r for r in rows if r.get("event", "call") == "call"]
        selfcheck = [r for r in rows if r.get("event") == "selfcheck"]
        self.add("B-HARD-CAP", len(calls) <= HARD_CAP,
                 f"实呼 {len(calls)} / 硬帽 {HARD_CAP}")
        self.add("B-SELFCHECK-SINGLE", len(selfcheck) <= 1,
                 f"开账行 {len(selfcheck)} 条")
        cap_by_task = {task: LANE_CAPS[lane] for lane, task in LANE_TASK_ID.items()}
        by_task = {}
        for row in calls:
            task = int(row.get("task") if row.get("task") is not None else -1)
            by_task[task] = by_task.get(task, 0) + 1
        over = {task: n for task, n in by_task.items()
                if n > cap_by_task.get(task, 0)}
        self.add("B-TASK-CAPS", not over, f"越帽: {over} 账 {by_task}")
        seqs = [int(r["seq"]) for r in rows if isinstance(r.get("seq"), int)]
        self.add("B-SEQ-CONTIGUOUS", seqs == list(range(1, len(seqs) + 1)),
                 f"seq 1..{len(seqs)} 期望连续")
        bad_ts = [r.get("ts_iso") for r in rows
                  if not str(r.get("ts_iso", "")).endswith("Z")]
        self.add("B-TS-UTC", not bad_ts, f"非严格 Z: {bad_ts[:3]}")

    def check_manifest(self, study: str, manifest) -> "set[str] | None":
        if not isinstance(manifest, dict):
            return None
        plan_sha = manifest.get("plan_sha256")
        assignments = manifest.get("assignments") or []
        if not isinstance(plan_sha, str) or len(plan_sha) != 64:
            self.add(f"S-{study}-PLAN-SHA", False, repr(plan_sha))
            return None
        if manifest.get("study_id") != study:
            self.add(f"S-{study}-STUDY", False, str(manifest.get("study_id")))
        by_cond, seen = {}, set()
        expected = set()
        for a in assignments:
            cid, bid = a.get("condition_id"), a.get("block_id")
            key = (cid, bid)
            if key in seen:
                self.add(f"S-{study}-DUP", False, str(key))
            seen.add(key)
            by_cond.setdefault(cid, set()).add(bid)
            expected.add(run_id_for(plan_sha, study, str(cid), str(bid)))
        want = {f"b{i:02d}" for i in range(1, CONFIRM_BLOCKS + 1)}
        self.add(f"S-{study}-BLOCKS",
                 bool(by_cond) and all(b == want for b in by_cond.values()),
                 f"条件 {sorted(by_cond)} 各自 block 集合必须恰为 b01..b32")
        self.add(f"S-{study}-DENOMINATOR",
                 len(assignments) == len(by_cond) * CONFIRM_BLOCKS,
                 f"{len(assignments)} 条 / {len(by_cond)}×32")
        return expected

    def check_judge(self, study: str, report, expected) -> "dict | None":
        if not isinstance(report, dict):
            return None
        layers = report.get("layers") or {}
        verdict = report.get("verdict")
        recomputed_ok = (bool(layers.get("schema_accepted"))
                         and bool(layers.get("evidence_valid"))
                         and layers.get("claim_status") in ("MATCH",))
        self.add(f"J-{study}-VERDICT-CONSISTENT",
                 (verdict == "JUDGE_OK") == recomputed_ok,
                 f"verdict={verdict} 三层={recomputed_ok}")
        statuses = {}
        for row in layers.get("scientific") or []:
            cid = str(row.get("id"))
            n = int(row.get("n_blocks") or 0)
            self.add(f"J-{study}-N-{cid}", n == CONFIRM_BLOCKS,
                     f"n_blocks={n} != {CONFIRM_BLOCKS}")
            if n != CONFIRM_BLOCKS:
                continue
            lower, upper, effect = (row.get("lower"), row.get("upper"),
                                    row.get("effect"))
            if lower is None or upper is None:
                self.add(f"J-{study}-CI-{cid}", False, "缺区间")
                continue
            self.add(f"J-{study}-ORDER-{cid}",
                     Decimal(str(lower)) <= Decimal(str(upper)),
                     f"L={lower} U={upper}")
            if effect is not None:
                self.add(f"J-{study}-POINT-{cid}",
                         Decimal(str(lower)) <= Decimal(str(effect))
                         <= Decimal(str(upper)),
                         f"effect={effect} 不在 [{lower},{upper}]")
            recomputed = classify(str(row.get("proposition")),
                                  row.get("delta"), lower, upper)
            self.add(f"J-{study}-STATUS-{cid}",
                     recomputed == row.get("status"),
                     f"重算 {recomputed} vs 报告 {row.get('status')}")
            statuses[cid] = row.get("status")
        if expected is not None:
            self.add(f"J-{study}-CONTRASTS", bool(statuses),
                     f"科学层 {sorted(statuses)}")
        return statuses

    def check_replay(self, study: str, comparison, expected) -> bool:
        if not isinstance(comparison, dict):
            return False
        runs = comparison.get("runs") or []
        ids = [r.get("run_id") for r in runs]
        self.add(f"R-{study}-UNIQUE", len(ids) == len(set(ids)),
                 f"{len(ids)} 条 / 唯一 {len(set(ids))}")
        if expected is not None:
            self.add(f"R-{study}-COVERAGE", set(ids) == expected,
                     f"缺 {sorted(expected - set(ids))[:3]} "
                     f"多 {sorted(set(ids) - expected)[:3]}")
        self.add(f"R-{study}-OK", comparison.get("ok") is True,
                 f"ok={comparison.get('ok')} problems={comparison.get('problems')}")
        failed = [r.get("run_id") for r in runs if not r.get("ok")]
        self.add(f"R-{study}-RUNS", not failed, f"失败 run: {failed[:3]}")
        missing_ind = [r.get("run_id") for r in runs
                       if not isinstance(r.get("independence"), dict)]
        self.add(f"R-{study}-INDEPENDENCE", not missing_ind,
                 f"缺独立性证据: {missing_ind[:3]}")
        return comparison.get("ok") is True

    def check_deliverable(self, doc, judge_ok: dict, replay_ok: dict) -> None:
        if not isinstance(doc, dict):
            return
        declared = doc.get("studies") or {}
        for study in STUDIES:
            entry = declared.get(study) or {}
            claim = entry.get("claim")
            support = bool(judge_ok.get(study)) and bool(replay_ok.get(study))
            if claim in ("COMPLETE", "VERIFIED"):
                self.add(f"D-{study}-SUPPORTED", support,
                         f"声明 {claim} 但 judge_ok={judge_ok.get(study)} "
                         f"replay_ok={replay_ok.get(study)}")
            else:
                self.add(f"D-{study}-SUPPORTED", True, f"声明 {claim}（不作完成）")

    def verify_pack(self, pack: Path) -> None:
        manifest_path = pack / "evidence_manifest.json"
        if not manifest_path.exists():
            self.add("P-MANIFEST", False, f"缺 {manifest_path}")
            return
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            self.add("P-MANIFEST", False, f"不可读: {exc}")
            return
        files = manifest.get("files") or {}
        self.add("P-MANIFEST-NONEMPTY", bool(files), f"{len(files)} 件")
        bad, missing, listed = [], [], set()
        for rel, want_sha in sorted(files.items()):
            path = pack / rel
            listed.add(rel)
            if path.is_symlink():
                bad.append(f"{rel}:symlink")
                continue
            if not path.is_file():
                missing.append(rel)
                continue
            got = hashlib.sha256(path.read_bytes()).hexdigest()
            if got != want_sha:
                bad.append(f"{rel}:sha")
        present = set()
        for dirpath, _dirnames, filenames in os.walk(pack):
            for name in filenames:
                rel = os.path.relpath(os.path.join(dirpath, name), pack)
                if rel == "evidence_manifest.json":
                    continue
                present.add(rel)
        self.add("P-HASH", not bad, f"hash 不符/链接: {bad[:5]}")
        self.add("P-MISSING", not missing, f"缺件: {missing[:5]}")
        self.add("P-EXTRA", present <= listed,
                 f"包内未登记: {sorted(present - listed)[:5]}")

    def run(self, *, pack: "Path | None" = None) -> dict:
        budget = self.load("contract/budget.json")
        rows = self.ledger_rows()
        self.check_budget(budget, rows)
        judge_ok, replay_ok, statuses = {}, {}, {}
        for study in STUDIES:
            manifest = self.load(f"confirm/manifest_{study}.json")
            expected = self.check_manifest(study, manifest)
            report = self.load(f"judge/report_{study}.json")
            statuses[study] = self.check_judge(study, report, expected)
            layers = (report or {}).get("layers") or {}
            judge_ok[study] = ((report or {}).get("verdict") == "JUDGE_OK"
                               and bool(layers.get("evidence_valid")))
            comparison = self.load(f"replay/comparison_{study}.json")
            replay_ok[study] = self.check_replay(study, comparison, expected)
        deliverable_path = self.root / "deliverable" / "closure.json"
        if deliverable_path.exists():
            self.check_deliverable(self.load("deliverable/closure.json",
                                             required=False), judge_ok, replay_ok)
        if pack is not None:
            self.verify_pack(Path(pack))
        verdict = "PASS" if not self.problems else "FAIL"
        return {"schema": SCHEMA, "schema_version": 1,
                "root": str(self.root.resolve()),
                "validator_source_sha256": hashlib.sha256(
                    Path(__file__).read_bytes()).hexdigest(),
                "run_at_utc": datetime.now(timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"),
                "checks": self.checks, "problems": self.problems,
                "verdict": verdict}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="RSI-R1 独立闭合复算")
    parser.add_argument("--root", required=True, help="campaign 根")
    parser.add_argument("--verify", nargs="?", const="", default=None,
                        metavar="PACK", help="同时核对证据包（缺省为 <root>/evidence_pack）")
    parser.add_argument("--report", default=None, help="判决件输出路径")
    args = parser.parse_args(argv)
    root = Path(args.root)
    if not root.is_dir():
        print(f"ERR 根不存在: {root}", file=sys.stderr)
        return 2
    pack = None
    if args.verify is not None:
        pack = Path(args.verify) if args.verify else root / "evidence_pack"
    result = Checker(root).run(pack=pack)
    blob = json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if args.report:
        out = Path(args.report)
        out.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = out.with_suffix(out.suffix + ".tmp")
        tmp.write_text(blob, encoding="utf-8")
        tmp.chmod(0o600)
        tmp.replace(out)
    print(blob, end="")
    print(f"RESULT {result['verdict']} problems={len(result['problems'])}",
          file=sys.stderr)
    return 0 if result["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
