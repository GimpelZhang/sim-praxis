#!/usr/bin/env python3
"""RSI-G2.1 独立闭合复核（Plan §4.1；**只标准库**，不 import 任何 orchestrator 模块）。

本脚本不采信桥自述的布尔值：它自己从落盘原件重算

* 冻结/合同快照的文件 sha 与实现闭包；
* 状态导入映射（源 journal 事件 vs 新 journal 事件）；
* trace 链（seq/parent/artifact sha）与「envelope 逐字在 request 中」；
* 五面（reply/action/report/rendered/claim）从原字节重算并核对互不混用；
* raw checker 的独立实现身份、每 study 复算结论与预登记变异矩阵；
* 容量矩阵 expected↔observed、正常形状必须成功；
* 隔离探针（uid/mount/errno/网络/负控）；
* closeout 轴与上述复算结果一致（缺项/UNKNOWN 不 vacuous pass）。

用法::

    python scripts/verification/check_rsi_g21_closure.py \
        --root /mnt/artifacts/simpraxis/rsi_g21/<id> [--repo .]

退出码 0 = ``problems == []``；1 = 有不一致。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

SCHEMA = "simpraxis.rsi_g21.closure_judge.v1"

CONTEXT_HEADERS = (
    "### 活动结果视图（result_context；g2_atomic_v1，不可裁剪）\n",
    "### 结果视图（g2_atomic_v1 最小交付单元，不可裁剪）\n",
)
MUTATIONS = {
    "run_value": ("NUMERIC_MISMATCH",),
    "condition_swap": ("NUMERIC_MISMATCH",),
    "wrong_unit": ("NUMERIC_MISMATCH",),
    "missing_block": ("COVERAGE_MISMATCH",),
    "duplicate_block": ("COVERAGE_MISMATCH",),
    "all_equal": ("SCIENCE_STATUS_MISMATCH",),
    "irrelevant_field": (),
    "unregistered_metric": ("METRIC_UNREGISTERED",),
    "delta_drift": ("DELTA_DRIFT",),
    "contrast_unknown": ("CONTRAST_UNREGISTERED",),
    "scope_drift": ("SCOPE_DRIFT",),
    "schema_drift": ("SCHEMA_DRIFT",),
    "meta_spec_hash": ("BINDING_MISMATCH",),
    "required_contrast_removed": ("REQUIRED_CONTRAST_MISSING",),
}
TRACE_KINDS = ("request_serialized", "response_raw", "action_parsed",
               "dispatch_return", "clipped_view", "claim_persisted")
FORBIDDEN_IMPORTS = ("orchestrator", "g2_contract", "g2_validation",
                     "metrics", "statistics", "contrasts")


def sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha_file(path: Path) -> str:
    return sha_bytes(path.read_bytes())


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def content_sha(doc: Mapping[str, Any], exclude: Sequence[str] = ()) -> str:
    body = {k: v for k, v in doc.items() if k not in set(exclude)}
    return sha_bytes(canonical(body))


def jload(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _journal(path: Path) -> "list[dict[str, Any]]":
    events: "list[dict[str, Any]]" = []
    if not path.is_file():
        return events
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            events.append(json.loads(line))
    return events


def _event_sha(doc: Mapping[str, Any], prev: "str | None") -> str:
    body = {"schema": doc.get("schema"), "seq": doc.get("seq"),
            "ts_iso": doc.get("ts_iso"), "event": doc.get("event"),
            "payload": doc.get("payload") or {}, "prev_sha256": prev}
    return sha_bytes(canonical(body))


class Judge:
    def __init__(self, root: Path, repo: Path) -> None:
        self.root = root
        self.repo = repo
        self.problems: "list[str]" = []
        self.counts: "dict[str, int]" = {}

    def bump(self, key: str, n: int = 1) -> None:
        self.counts[key] = self.counts.get(key, 0) + n

    def fail(self, message: str) -> None:
        self.problems.append(message)

    # -- 1. 冻结 -----------------------------------------------------------
    def check_freeze(self) -> None:
        path = self.root / "freeze" / "source_closure.json"
        if not path.is_file():
            self.fail("freeze.source_closure 缺失")
            return
        doc = jload(path)
        if doc.get("closure_sha256") != sha_bytes(canonical(doc.get("entries"))):
            self.fail("freeze.closure_sha256 不符")
        for entry in doc.get("entries") or []:
            target = Path(str(entry.get("path")))
            role = entry.get("role")
            if entry.get("kind") == "dir":
                if not target.is_dir():
                    self.fail(f"freeze dir 消失 {role}")
                    continue
                digest = hashlib.sha256()
                files = sorted(p for p in target.rglob("*") if p.is_file())
                for item in files:
                    digest.update(item.relative_to(target).as_posix().encode())
                    digest.update(b"\0")
                    digest.update(item.read_bytes())
                if digest.hexdigest() != entry.get("sha256"):
                    self.fail(f"freeze dir 漂移 {role}")
                self.bump("freeze_dir_files", len(files))
            else:
                if not target.is_file():
                    self.fail(f"freeze file 消失 {role}")
                    continue
                if sha_file(target) != entry.get("sha256"):
                    self.fail(f"freeze file 漂移 {role}")
                self.bump("freeze_files")

    # -- 2. 合同快照 -------------------------------------------------------
    def check_snapshot(self) -> None:
        path = self.root / "contract" / "g21_contract.json"
        if not path.is_file():
            self.fail("contract.g21_contract 缺失")
            return
        doc = jload(path)
        if doc.get("snapshot_sha256") != content_sha(doc, ("snapshot_sha256",)):
            self.fail("snapshot_sha256 不符")
        closure = doc.get("implementation_closure") or {}
        if closure.get("closure_sha256") != sha_bytes(canonical(
                closure.get("files"))):
            self.fail("implementation closure sha 不符")
        for item in closure.get("files") or []:
            target = self.repo / str(item.get("path"))
            if not target.is_file():
                self.fail(f"实现闭包文件缺失 {item.get('path')}")
                continue
            if sha_file(target) != item.get("sha256"):
                self.fail(f"实现闭包漂移 {item.get('path')}")
            self.bump("implementation_files")

    # -- 3. 状态导入 -------------------------------------------------------
    def check_import(self) -> None:
        manifest_path = self.root / "freeze" / "replay_manifest.json"
        if not manifest_path.is_file():
            self.fail("freeze.replay_manifest 缺失")
            return
        manifest = jload(manifest_path)
        journal = _journal(self.root / "state" / "journal.jsonl")
        prev = None
        for index, event in enumerate(journal, 1):
            if event.get("seq") != index:
                self.fail(f"新 journal seq 不连续 @{index}")
                break
            if event.get("prev_sha256") != prev:
                self.fail(f"新 journal 断链 @{index}")
                break
            want = _event_sha(event, prev)
            if event.get("sha256") != want:
                self.fail(f"新 journal sha 不符 @{index}")
                break
            prev = event.get("sha256")
        for study in ("A", "B"):
            path = self.root / "import" / study / "replay_import.json"
            if not path.is_file():
                if self._source_blocked(study):
                    self.bump("import_skipped_source_blocked")
                    continue
                self.fail(f"import.{study} 缺失")
                continue
            doc = jload(path)
            source = _journal(Path(manifest["studies"][study]["journal"]))
            for row in doc.get("mapping") or []:
                seq = int(row.get("new_seq"))
                if not (1 <= seq <= len(journal)):
                    self.fail(f"import.{study} new_seq 越界 {seq}")
                    continue
                if journal[seq - 1].get("event") != row.get("event"):
                    self.fail(f"import.{study} 事件名不符 @{seq}")
                src_seq = int(row.get("source_seq"))
                if not (1 <= src_seq <= len(source)):
                    self.fail(f"import.{study} source_seq 越界")
                    continue
                if sha_bytes(canonical(source[src_seq - 1].get("payload"))) != \
                        sha_bytes(canonical(journal[seq - 1].get("payload"))):
                    self.fail(f"import.{study} payload 不符 @{seq}")
                if sha_file(Path(manifest["studies"][study]["journal"])) != \
                        doc.get("source_journal_sha256"):
                    self.fail(f"import.{study} 源 journal sha 不符")
                self.bump("import_events")
            # 导入前缀里不得出现**本 study** 的 claim/conclusion（那是 cutoff 之后的面）
            for row in doc.get("mapping") or []:
                src = source[int(row.get("source_seq")) - 1]
                if src.get("event") in ("claim", "study_conclusion") and \
                        (src.get("payload") or {}).get("study") == study:
                    self.fail(f"import.{study} 导入了本 study 的 {src.get('event')}")
                    break

    # -- 4. trace ----------------------------------------------------------
    def check_trace(self) -> None:
        for study in ("A", "B"):
            path = self.root / "trace" / study / "events.jsonl"
            if not path.is_file():
                if self._source_blocked(study):
                    self.bump("trace_skipped_source_blocked")
                    continue
                self.fail(f"trace.{study} 缺失")
                continue
            rows = [json.loads(line) for line in
                    path.read_text(encoding="utf-8").splitlines() if line.strip()]
            if not rows:
                self.fail(f"trace.{study} 为空")
                continue
            prev_sha = None
            for index, row in enumerate(rows, 1):
                if row.get("seq") != index:
                    self.fail(f"trace.{study} seq 不连续 @{index}")
                    break
                if index > 1 and row.get("parent_event_sha256") != prev_sha:
                    self.fail(f"trace.{study} parent 断链 @{index}")
                    break
                prev_sha = row.get("event_sha256")
                ref = row.get("artifact_ref")
                if row.get("visibility") == "observed" and ref:
                    target = self.root / str(ref)
                    if not target.is_file():
                        self.fail(f"trace.{study} artifact 缺 {ref}")
                        continue
                    if sha_file(target) != row.get("bytes_sha256"):
                        self.fail(f"trace.{study} artifact sha 不符 {ref}")
                    self.bump("trace_artifacts")
            kinds = {str(row.get("event_kind")) for row in rows}
            for kind in TRACE_KINDS:
                if kind not in kinds:
                    self.fail(f"trace.{study} 缺事件 {kind}")
            # envelope 逐字在最终 request 中（原件重算）
            requests = [row for row in rows
                        if row.get("event_kind") == "request_serialized"]
            if requests:
                data = (self.root / str(requests[-1]["artifact_ref"])).read_text(
                    encoding="utf-8")
                self._check_envelope_in_request(study, data)
            self.bump("trace_events", len(rows))

    def _check_envelope_in_request(self, study: str, request_text: str) -> None:
        envelope = None
        for header in CONTEXT_HEADERS:
            index = request_text.find(header)
            if index >= 0:
                tail = request_text[index + len(header):]
                depth = 0
                end = None
                for offset, char in enumerate(tail):
                    if char == "{":
                        depth += 1
                    elif char == "}":
                        depth -= 1
                        if depth == 0:
                            end = offset + 1
                            break
                if end is None:
                    continue
                try:
                    envelope = json.loads(tail[:end])
                except json.JSONDecodeError:
                    envelope = None
                break
        # 消息里可能有转义引号：退回按行解析公开请求 JSON
        if envelope is None:
            try:
                payload = json.loads(request_text)
            except json.JSONDecodeError:
                self.fail(f"trace.{study} request 不是 JSON")
                return
            for message in payload.get("messages") or []:
                content = message.get("content") or ""
                for header in CONTEXT_HEADERS:
                    index = content.find(header)
                    if index < 0:
                        continue
                    try:
                        envelope = json.loads(content[index + len(header):])
                    except json.JSONDecodeError:
                        continue
        if envelope is None:
            self.fail(f"trace.{study} 最终 request 中找不到 envelope")
            return
        if envelope.get("envelope_sha256") != content_sha(
                envelope, ("envelope_sha256",)):
            self.fail(f"trace.{study} envelope_sha256 不符")
        text = canonical(envelope).decode("utf-8")
        try:
            parsed = json.loads(request_text)
            joined = "\n".join(str(message.get("content") or "")
                               for message in parsed.get("messages") or [])
        except json.JSONDecodeError:
            joined = request_text
        if text not in joined:
            self.fail(f"trace.{study} envelope 未逐字在 request 中")
        self.bump("envelope_in_request")

    # -- 5. 五面 -----------------------------------------------------------
    def check_submit(self) -> None:
        journal = _journal(self.root / "state" / "journal.jsonl")
        for study in ("A", "B"):
            path = self.root / "submit" / study / "five_faces.json"
            if not path.is_file():
                self.bump("submit_missing")
                continue
            doc = jload(path)
            faces = doc.get("faces") or {}
            action_events = [ev for ev in journal
                             if ev.get("event") == "action"
                             and (ev.get("payload") or {}).get("name")
                             == "submit_claim"
                             and (ev.get("payload") or {}).get("study")
                             in (None, study)]
            if not action_events:
                self.fail(f"submit.{study} 无 submit_claim 动作事件")
                continue
            action_ev = action_events[-1]["payload"]
            action_sha = sha_bytes(canonical(action_ev.get("payload")))
            reply_rel = str((faces.get("raw_reply") or {}).get("relpath"))
            reply_path = self.root / reply_rel
            reply_sha = sha_file(reply_path) if reply_path.is_file() else ""
            report_path = self.root / "submit" / study / "report.json"
            report_sha = (sha_bytes(canonical(jload(report_path)))
                          if report_path.is_file() else "")
            rendered_path = self.root / "submit" / study / "rendered.txt"
            rendered_sha = (sha_file(rendered_path)
                            if rendered_path.is_file() else "")
            claim_path = self.root / "submit" / study / "claim.json"
            claim_sha = (sha_bytes(canonical(jload(claim_path).get("claims")))
                         if claim_path.is_file() else "")
            expected = {"raw_reply": reply_sha, "action": action_sha,
                        "report_doc": report_sha, "rendered": rendered_sha,
                        "claim": claim_sha}
            for name, want in expected.items():
                got = str((faces.get(name) or {}).get("sha256") or "")
                if got != want:
                    self.fail(f"submit.{study} 面 {name} 重算不符")
            if not all(doc.get("checks", {}).values()):
                self.fail(f"submit.{study} 内嵌 checks 未全过")
            if len(set(expected.values())) != len(expected):
                self.fail(f"submit.{study} 五面 sha 有重复（混用）")
            self.bump("five_faces")

    # -- 6. raw checker ----------------------------------------------------
    def check_raw_check(self) -> None:
        path = self.root / "raw_check" / "report.json"
        if not path.is_file():
            self.fail("raw_check.report 缺失")
            return
        doc = jload(path)
        checker = self.repo / "orchestrator/rsi_r1/g21_raw_checker.py"
        if not checker.is_file():
            self.fail("raw checker 源文件缺失")
            return
        text = checker.read_text(encoding="utf-8")
        hits = [name for name in FORBIDDEN_IMPORTS
                if re.search(rf"^\s*(from|import)\s+{re.escape(name)}\b",
                             text, re.MULTILINE)]
        if hits:
            self.fail(f"raw checker 含禁用 import {hits}")
        if doc.get("checker_sha256") != sha_bytes(text.encode("utf-8")):
            self.fail("raw checker 源 sha 与报告不符")
        if not doc.get("independent"):
            self.fail("raw checker 未标独立")
        for study, report in (doc.get("studies") or {}).items():
            rc = report.get("checker_rc")
            problems = list(report.get("problems") or [])
            blocked_ok = (self._source_blocked(study) and rc == 1
                          and problems == ["REQUIRED_CONTRAST_MISSING"])
            if rc != 0 and not blocked_ok:
                self.fail(f"raw_check.{study} rc={rc} problems={problems}")
            if not report.get("raw_ok"):
                self.fail(f"raw_check.{study} raw_ok=false")
            if not report.get("axes"):
                self.fail(f"raw_check.{study} 缺轴")
            raw = report.get("raw") or {}
            self.bump("raw_check_studies")
            self._check_raw_inputs(study, raw, report)
        for name, report in (doc.get("mutations") or {}).items():
            expected = MUTATIONS.get(name)
            if expected is None:
                self.fail(f"变异 {name} 未登记")
                continue
            if list(report.get("expected") or []) != list(expected):
                self.fail(f"变异 {name} expected 与登记不符")
            observed = [str(x) for x in report.get("problems") or []]
            if not set(expected) <= set(observed):
                self.fail(f"变异 {name} 缺预期问题 {expected}（observed={observed}）")
            if not expected and observed:
                self.fail(f"变异 {name} 合法变化被误判 {observed}")
            if not report.get("ok"):
                self.fail(f"变异 {name} 未按预期判定")
            self.bump("mutations")

    def _check_raw_inputs(self, study: str, raw: Mapping[str, Any],
                          report: Mapping[str, Any]) -> None:
        manifest = jload(self.root / "freeze" / "replay_manifest.json")
        batch = Path(manifest["studies"][study]["observations_batch"])
        local = self.root / "observations" / batch.name
        manifest_path = (local if (local / "manifest.json").is_file()
                         else batch) / "manifest.json"
        if not manifest_path.is_file():
            self.fail(f"raw_check.{study} 找不到 manifest.json")
            return
        if raw.get("manifest_sha256") != sha_file(manifest_path):
            self.fail(f"raw_check.{study} manifest sha 不符")

    # -- 7. 容量 -----------------------------------------------------------
    def check_capacity(self) -> None:
        matrix_path = self.root / "freeze" / "capacity_matrix.json"
        report_path = self.root / "capacity" / "report.json"
        if not matrix_path.is_file() or not report_path.is_file():
            self.fail("capacity 矩阵/报告缺失")
            return
        matrix = {row["fixture_id"]: row for row in
                  jload(matrix_path).get("entries") or []}
        report = jload(report_path)
        results = report.get("results") or []
        if not results:
            self.fail("capacity 无结果")
            return
        normal_success = 0
        for row in results:
            fid = row.get("fixture_id")
            entry = matrix.get(fid)
            if entry is None:
                self.fail(f"capacity 未登记 fixture {fid}")
                continue
            if row.get("expected") != entry.get("expected_outcome"):
                self.fail(f"capacity {fid} expected 漂移")
            expected = entry.get("expected_outcome")
            observed = row.get("outcome")
            if expected == "SOURCE_OR_SUCCESS":
                if observed not in ("NORMAL_SUCCESS", "BLOCKED_SOURCE"):
                    self.fail(f"capacity {fid} observed={observed}")
            elif observed != expected:
                self.fail(f"capacity {fid} observed={observed} != {expected}")
            if expected == "NORMAL_SUCCESS":
                if observed == "NORMAL_SUCCESS":
                    normal_success += 1
                else:
                    self.fail(f"capacity {fid} 正常形状被阻断")
            self.bump("capacity_fixtures")
        if normal_success == 0:
            self.fail("capacity 没有任何 NORMAL_SUCCESS（全阻断不算通过）")
        tok = self.root / "freeze" / "tokenizer_contract.json"
        if not tok.is_file():
            self.fail("tokenizer_contract 缺失")
        else:
            doc = jload(tok)
            if doc.get("status") != "TOKENIZER_READY":
                self.fail(f"tokenizer 状态 {doc.get('status')}")
            path = Path(str(doc.get("path")))
            if not path.is_file() or sha_file(path) != doc.get("sha256"):
                self.fail("tokenizer artifact sha 不符")

    # -- 8. 隔离 -----------------------------------------------------------
    def check_isolation(self) -> None:
        path = self.root / "isolation" / "report.json"
        if not path.is_file():
            self.fail("isolation.report 缺失")
            return
        doc = jload(path)
        all_ok = True
        for row in doc.get("principals") or []:
            name = row.get("principal")
            container = self.root / "isolation" / name
            canary = (container / "view" / "canary.txt")
            probe = row.get("probe") or {}
            checks = row.get("checks") or {}
            if not canary.is_file():
                self.fail(f"isolation.{name} canary 缺失")
                continue
            want = sha_bytes(canary.read_text(encoding="utf-8").encode("utf-8"))
            if (probe.get("allowed_read") or {}).get("value") != want:
                self.fail(f"isolation.{name} allowed_read 不符")
            if int(probe.get("uid") or -1) != int(row.get("uid") or -2):
                self.fail(f"isolation.{name} uid 不符")
            log = self.root / "isolation" / f"{name}.log"
            if not log.is_file() or sha_file(log) != row.get("log_sha256"):
                self.fail(f"isolation.{name} 日志 sha 不符")
            if not all(checks.values()):
                all_ok = False
                self.fail(f"isolation.{name} 探针未全过")
            self.bump("isolation_principals")
        neg = doc.get("negative_control") or {}
        if not neg.get("readable_private"):
            all_ok = False
            self.fail("isolation 负控未被检出（私件可读必须 FAIL）")
        want_status = "ISOLATION_OK" if all_ok else "ISOLATION_UNVERIFIED"
        if doc.get("status") != want_status:
            self.fail(f"isolation.status={doc.get('status')} != {want_status}")
        self.bump("isolation_neg_control")

    def _source_blocked(self, study: str) -> bool:
        path = self.root / "reports" / f"{study}-bridge.json"
        if not path.is_file():
            return False
        doc = jload(path)
        return (doc.get("source") or {}).get("status") == \
            "SOURCE_EVIDENCE_INCOMPLETE"

    def recomputed_axes(self) -> "dict[str, str]":
        """按 closeout 同名词表从**原件复核结果**重算轴。"""
        def bad(prefixes) -> bool:
            return any(p.startswith(prefixes) for p in self.problems)

        isolation = "UNKNOWN"
        path = self.root / "isolation" / "report.json"
        if path.is_file():
            isolation = jload(path).get("status", "UNKNOWN")
        raw_ok = not bad(("raw_check", "变异", "raw checker"))
        return {
            "content_integrity": "FAIL" if bad(("freeze", "snapshot",
                                                "contract")) else "PASS",
            "visibility_observability": "FAIL" if bad(("trace",)) else "PASS",
            "submit_e2e": "PASS" if self.counts.get("five_faces") else "UNKNOWN",
            "raw_checker": "PASS" if raw_ok else "FAIL",
            "tokenizer_capacity": "FAIL" if bad(("capacity", "tokenizer")) else "PASS",
            "isolation": isolation,
        }

    # -- 9. closeout -------------------------------------------------------
    def check_closeout(self, recomputed: Mapping[str, str]) -> "dict[str, Any]":
        path = self.root / "decision" / "g21_closeout.json"
        if not path.is_file():
            self.fail("decision.g21_closeout 缺失")
            return {}
        doc = jload(path)
        axes = doc.get("axes") or {}
        for name, want in recomputed.items():
            got = axes.get(name)
            if got != want:
                self.fail(f"closeout 轴 {name}={got} 与独立复算 {want} 不符")
        if any(value != "PASS" for value in axes.values()) and \
                doc.get("overall") == "G2.1_ENGINEERING_READY_FOR_NEW_DESIGN":
            self.fail("READY 但存在非 PASS 轴（vacuous pass）")
        if doc.get("scientific_claim") != "NONE（只出工程轴；不晋升 R1/R2）":
            self.fail("closeout 科学主张字段被改写")
        signature = doc.get("signature") or {}
        paths = {"implementation_manifest_sha": "contract/g21_contract.json",
                 "input_manifest_sha": "freeze/source_closure.json",
                 "tokenizer_contract_sha": "freeze/tokenizer_contract.json",
                 "test_reports_sha": "reports/ac_report.json",
                 "raw_check_report_sha": "raw_check/report.json",
                 "capacity_report_sha": "capacity/report.json",
                 "isolation_report_sha": "isolation/report.json"}
        for key, rel in paths.items():
            recorded = str(signature.get(key) or "")
            path_ = self.root / rel
            if not recorded:
                self.fail(f"closeout 签名缺 {key}")
            elif not path_.is_file() or sha_file(path_) != recorded:
                self.fail(f"closeout 签名 {key} 与原件不符")
            else:
                self.bump("signature_faces")
        self.bump("closeout_axes", len(axes))
        return doc


def main(argv: "Sequence[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(prog="check_rsi_g21_closure")
    parser.add_argument("--root", required=True)
    parser.add_argument("--repo", default=str(Path(__file__).resolve().parents[2]))
    args = parser.parse_args(argv)
    root = Path(args.root)
    judge = Judge(root=root, repo=Path(args.repo))
    judge.check_freeze()
    judge.check_snapshot()
    judge.check_import()
    judge.check_trace()
    judge.check_submit()
    judge.check_raw_check()
    judge.check_capacity()
    judge.check_isolation()
    # 独立复算轴（只在本脚本自己验过的原件上得出）
    recomputed = dict(judge.recomputed_axes())
    closeout = judge.check_closeout(recomputed)
    report = {"schema": SCHEMA, "schema_version": 1,
              "ok": not judge.problems,
              "consistent": not judge.problems,
              "problems": judge.problems,
              "counts": dict(sorted(judge.counts.items())),
              "recomputed_axes": recomputed,
              "closeout_overall": (closeout or {}).get("overall"),
              "closeout_gaps": (closeout or {}).get("gaps"),
              "judge_source_sha256": sha_file(Path(__file__).resolve())}
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if not judge.problems else 1


if __name__ == "__main__":
    sys.exit(main())
