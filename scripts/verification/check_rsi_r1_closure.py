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

#: Plan §11.1 四轴 + R1 结论的判定面（独立字面量，不从生产代码 import）。
AXIS_LEGAL = {
    "process": ("COMPLETE", "CLOSEOUT_INCOMPLETE"),
    "measurement": ("VALID", "INVALID_INSTRUMENT", "CONTAMINATED", "INCOMPLETE"),
    "study_execution": ("NOT_STARTED", "COMPLETED", "VALID_FAIL", "BLOCKED",
                        "ASSISTED"),
    "claim": ("NONE", "SUPPORTED", "REFUTED", "INCONCLUSIVE", "CLAIM_INVALID"),
    "r1": ("NOT_MEASURED", "INCOMPLETE_COVERAGE", "NOT_ESTABLISHED",
           "MINIMAL_RESEARCH_LOOP_ESTABLISHED"),
}
STUDY_ARTIFACT_RELS = ("confirm/manifest_{s}.json", "judge/report_{s}.json",
                       "replay/comparison_{s}.json")


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

    def check_carryover(self, rows) -> None:
        """AC34「整轮新 root 不归零」：搬入行必须在现账原位且未被改写。

        ``quota/carryover.json`` 只在 §8.1 仪器修复重开时存在；普通 campaign
        没有它，本条不产生任何断言。摘要在 authorize 时由 ``repair.py`` 按同一
        口径计算，机判独立重算，不信自述。
        """
        path = self.root / "quota" / "carryover.json"
        if not path.exists():
            return
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            self.add("C-READABLE", False, f"不可读: {exc}")
            return
        self.add("C-READABLE", True, "")
        n = int(doc.get("carried_rows") or 0)
        head = rows[:n]
        self.add("C-ROWS-PRESENT", n > 0 and len(head) == n,
                 f"carried_rows={n} 现账行数={len(rows)}")
        got = hashlib.sha256(json.dumps(
            head, ensure_ascii=False, sort_keys=True,
            separators=(",", ":")).encode("utf-8")).hexdigest()
        want = doc.get("carried_rows_sha256")
        self.add("C-ROWS-INTACT", isinstance(want, str) and got == want,
                 f"前 {n} 行摘要 {got[:16]}… vs 登记 {str(want)[:16]}…")
        calls = sum(1 for r in head if r.get("event", "call") == "call")
        self.add("C-CALLS-COUNT", calls == int(doc.get("carried_calls") or -1),
                 f"前 {n} 行实呼 {calls} vs 登记 {doc.get('carried_calls')}")
        self.add("C-REASON-AC34", "AC34" in str(doc.get("reason") or ""),
                 "carryover.reason 必须引用 AC34")

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

    # -- T9 closeout（AC34–36）-------------------------------------------
    def study_started_independently(self) -> bool:
        """盘面清点（不读任何 readout 声明）：任一研究产物存在即算启动。"""
        for study in STUDIES:
            for rel in STUDY_ARTIFACT_RELS:
                if (self.root / rel.format(s=study)).is_file():
                    return True
        return (self.root / "protocol" / "report_practice.json").is_file()

    def check_closeout(self, rows) -> "dict | None":
        readout = self.load("closeout/readout.json", required=False)
        if not isinstance(readout, dict):
            return None
        body = dict(readout)
        anchor = body.pop("document_sha256", None)
        got = sha256_text(canonical_json(body))
        self.add("X-ANCHOR", isinstance(anchor, str) and got == anchor,
                 f"独立重算 {got[:16]}… vs 登记 {str(anchor)[:16]}…")

        axes = readout.get("axes") or {}
        self.add("X-AXES-LEGAL",
                 all(axes.get(k) in AXIS_LEGAL[k] for k in AXIS_LEGAL),
                 f"axes={axes}")
        reasons = readout.get("process_reasons") or []
        self.add("X-PROCESS",
                 (axes.get("process") == "COMPLETE") == (len(reasons) == 0),
                 f"process={axes.get('process')} 但 reasons={len(reasons)} 条")

        # §11.1 优先级：研究未启动 ⇒ 其余三轴必须是最弱值；反之不得声称未启动。
        execu, meas = axes.get("study_execution"), axes.get("measurement")
        claim, r1 = axes.get("claim"), axes.get("r1")
        if execu == "NOT_STARTED":
            self.add("X-AXES-PRECEDENCE",
                     meas == "INCOMPLETE" and claim == "NONE"
                     and r1 == "NOT_MEASURED",
                     f"NOT_STARTED 要求 (INCOMPLETE, NONE, NOT_MEASURED)，"
                     f"实为 ({meas}, {claim}, {r1})")
        else:
            self.add("X-AXES-PRECEDENCE",
                     meas != "INCOMPLETE" or r1 != "MINIMAL_RESEARCH_LOOP_ESTABLISHED",
                     f"measurement=INCOMPLETE 不得配 {r1}")
            self.add("X-AXES-STRONG",
                     not (meas == "INCOMPLETE"
                          and r1 in ("NOT_ESTABLISHED",
                                     "MINIMAL_RESEARCH_LOOP_ESTABLISHED")),
                     f"measurement=INCOMPLETE 不得给出 r1={r1}")
        self.add("X-AXES-R1-STRONGEST",
                 not (r1 == "MINIMAL_RESEARCH_LOOP_ESTABLISHED"
                      and (execu != "COMPLETED" or meas != "VALID")),
                 f"最强结论要求 COMPLETED+VALID，实为 ({execu}, {meas})")
        self.add("X-AXES-R1-COMPLETE",
                 not (r1 == "NOT_ESTABLISHED" and execu != "COMPLETED"),
                 f"NOT_ESTABLISHED 要求 study_execution=COMPLETED，实为 {execu}")
        self.add("X-AXES-COVERAGE",
                 not (r1 == "INCOMPLETE_COVERAGE" and execu == "NOT_STARTED"),
                 "研究未启动只能记 NOT_MEASURED")

        # 独立重算账本事实（不信 readout.ledger）
        calls = [r for r in rows if r.get("event", "call") == "call"]
        facts = {
            "rows": len(rows),
            "calls": len(calls),
            "ok": sum(1 for r in calls if r.get("ok") is True),
            "failed": sum(1 for r in calls if r.get("ok") is False),
            "events": len(rows) - len(calls),
            "tokens_in": sum(int(r.get("tokens_in") or 0) for r in calls),
            "tokens_out": sum(int(r.get("tokens_out") or 0) for r in calls),
        }
        claimed = readout.get("ledger") or {}
        for key in ("rows", "calls", "ok", "failed", "events"):
            self.add(f"X-LEDGER-{key}", int(claimed.get(key, -1)) == facts[key],
                     f"读out={claimed.get(key)} 独立={facts[key]}")
        tok = claimed.get("tokens") or {}
        self.add("X-LEDGER-TOKENS",
                 int(tok.get("in", -1)) == facts["tokens_in"]
                 and int(tok.get("out", -1)) == facts["tokens_out"],
                 f"tokens={tok} 独立 in/out={facts['tokens_in']}/{facts['tokens_out']}")

        # 人工时长：独立数事件行，不许把「未记录」写成 0 完整。
        human = readout.get("human") or {}
        ev_path = self.root / "human_events" / "events.jsonl"
        n_ev = (sum(1 for ln in ev_path.read_text(encoding="utf-8").splitlines()
                    if ln.strip()) if ev_path.is_file() else 0)
        self.add("X-HUMAN-N", int(human.get("n_events", -1)) == n_ev,
                 f"读out={human.get('n_events')} 独立={n_ev}")
        self.add("X-HUMAN-COMPLETE",
                 not (human.get("complete") is True
                      and human.get("unknown_events")),
                 "complete=true 不得同时有未闭合事件")
        oob = human.get("out_of_band") or {}
        self.add("X-HUMAN-OUT-OF-BAND",
                 isinstance(oob, dict)
                 and oob.get("machine_timed") == (n_ev > 0)
                 and isinstance(oob.get("decisions_on_record"), dict),
                 "盘外裁决必须单列且不得冒充机器计时")

        # 旧根 hash 保护（AC36）：按盘上字节独立重算。
        old = readout.get("old_root") or {}
        if old.get("present"):
            src = Path(str(old.get("source_root") or ""))
            ledger = src / "quota" / "ledger.jsonl"
            if not ledger.is_file():
                self.add("X-OLD-ROOT-READ", False, f"旧根账本不可读：{ledger}")
            else:
                got_sha = hashlib.sha256(ledger.read_bytes()).hexdigest()
                self.add("X-OLD-ROOT-UNCHANGED",
                         got_sha == old.get("declared_source_sha256"),
                         f"现值 {got_sha[:16]}… vs 登记 "
                         f"{str(old.get('declared_source_sha256'))[:16]}…")
                self.add("X-OLD-ROOT-MIRROR",
                         got_sha == old.get("observed_sha256")
                         and old.get("unchanged") is True,
                         "readout 的 unchanged/observed 与独立重算不一致")

        # 启动与否只认盘面事实，与声明矛盾即 FAIL。
        started = self.study_started_independently()
        self.add("X-INVENTORY", started == (execu != "NOT_STARTED"),
                 f"盘面 started={started} 声明 study_execution={execu}")
        for study in STUDIES:
            present = [rel.format(s=study) for rel in STUDY_ARTIFACT_RELS
                       if (self.root / rel.format(s=study)).is_file()]
            if not started:
                self.add(f"X-STUDY-ARTIFACTS-{study}", not present,
                         f"研究未启动但 {study} 有产物: {present}")
            # 已启动但某 study 无产物是**部分覆盖**（BLOCKED/INCOMPLETE_COVERAGE），
            # 不是伪造；完成声明由 D-*-SUPPORTED 管，这里不重复判死。

        for study in STUDIES:
            doc = self.load(f"research_packets/{study}/status.json",
                            required=False)
            want = "STARTED" if started else "NOT_STARTED"
            self.add(f"X-PACKET-{study}",
                     isinstance(doc, dict) and doc.get("status") == want,
                     f"status={None if not isinstance(doc, dict) else doc.get('status')}"
                     f" 期望 {want}")

        deliverable = self.load("deliverable/closure.json", required=False)
        if not isinstance(deliverable, dict):
            self.add("X-DELIVERABLE", False, "缺 deliverable/closure.json")
        elif not started:
            for study in STUDIES:
                got_claim = ((deliverable.get("studies") or {}).get(study)
                             or {}).get("claim")
                self.add(f"X-DELIVERABLE-{study}",
                         got_claim in ("BLOCKED", "VALID_FAIL", "NOT_STARTED",
                                       "NONE"),
                         f"研究未启动却声明 {got_claim}")

        # 失败谱系：登记项的证据必须在盘上；协议失败必须有对应条目。
        failure_map = self.load("closeout/failure_map.json", required=False)
        entries = (failure_map or {}).get("entries") if isinstance(failure_map,
                                                                  dict) else None
        self.add("X-FAILURE-MAP", isinstance(entries, list),
                 "缺/不可读 closeout/failure_map.json")
        missing, stages = [], set()
        for entry in entries or []:
            stages.add(str(entry.get("stage")))
            for rel in entry.get("evidence") or []:
                p = Path(str(rel))
                if not p.is_absolute():
                    p = self.root / p
                if not p.exists():
                    missing.append(str(rel))
        self.add("X-FAILURE-EVIDENCE", not missing, f"缺件: {missing[:5]}")
        for name in ("probe", "practice"):
            report = self.root / "protocol" / f"report_{name}.json"
            if not report.is_file():
                continue
            try:
                verdict = json.loads(report.read_text(encoding="utf-8")).get(
                    "verdict")
            except (OSError, json.JSONDecodeError):
                verdict = None
            if verdict and verdict != "PASS":
                self.add(f"X-FAILURE-REGISTERED-{name}",
                         f"protocol/{name}" in stages,
                         f"报告 verdict={verdict} 但 failure_map 无该阶段条目")

        self.add("X-SHADOW-PRESENT",
                 (self.root / "closeout" / "shadow_proposals.jsonl").is_file(),
                 "closeout/shadow_proposals.jsonl 必须存在（可为空）")
        self.add("X-R2-READINESS",
                 (self.root / "closeout" / "r2_readiness.json").is_file()
                 and isinstance(readout.get("r2_readiness_relpath"), str),
                 "缺 closeout/r2_readiness.json 或其登记")
        return {"started": started}

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
        self.check_carryover(rows)
        # T9：有 closeout/readout.json 就按「结案口径」判——研究未启动时
        # 研究产物**应当缺席**，缺件不是错误；矛盾声明才是。
        close = self.check_closeout(rows) if (
            self.root / "closeout" / "readout.json").exists() else None
        started = bool(close and close.get("started"))
        judge_ok, replay_ok, statuses = {}, {}, {}
        if close is None or started:
            for study in STUDIES:
                artifacts = [rel.format(s=study) for rel in STUDY_ARTIFACT_RELS
                             if (self.root / rel.format(s=study)).is_file()]
                if close is not None and not artifacts:
                    continue  # 该 study 未产出：按部分覆盖结案，不按缺件判死
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
                "closeout_present": close is not None,
                "studies_started": started,
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
