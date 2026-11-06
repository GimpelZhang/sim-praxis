"""Phase 2.8.1 T1：CandidateEvaluator —— 独立判决模块（**永不执行候选代码**）。

一句话纪律：**评测器只读文件、只调纯函数，不 import 候选模块、不跑候选脚本**。
M1/L1/R1 判分所需的事实在候选运行期就已落盘（``delivered/`` 冻结副本 +
``telemetry.jsonl`` 运行器遥测 + 账本），评测器只对这些字节重算。这条把
「评测期执行不可信代码」的攻击面缩到零（计划 §5.5 的设计裁定 / 风险 R8）。

判决优先级（计划 §5.4.2，代码里是显式 if/elif，不靠字典顺序）：

1. 锚不符 / 判据摘要不符 / 运行时不符 → ``BLOCKED_INFRA``；
2. manifest 缺失/不符 / 交付件缺件、超限、符号链接逃逸 / 运行器遥测缺 → ``INVALID_ARTIFACT``；
3. ``claimed.*`` 与账本或运行器观测不一致 → ``INVALID_ARTIFACT``（**不允许以候选为准**）；
4. 出生未过（``birth.json`` 的 ``birth_status``/``status`` != ``"BORN"``）→ ``FAIL``，
   且判据全部 ``passed=false``（不逐条给分）；
5. 判据逐条全过 → ``PASS``，否则 ``FAIL``。

**运行器遥测契约**（T1 定文字契约，T2 的 runner 实现）：``trial_dir/telemetry.jsonl``
每行一个 JSON 对象；其中恰好一条（通常最后一条）是
``{"event": "summary", "coding_rounds": <int>, "tool_steps": <int>,
"first_birth_round": <int|null>, "trial_id": <str>}``。缺该 summary 行即无法核验
自报的这三个字段 ⇒ ``INVALID_ARTIFACT``（不放行「无观测即默认自报为真」）。

**判据轴 = 评分维度 + 任务级硬闸判据**：每个评分维度一条 ``DIM_<维度名>``；
Phase 2.8.0 里「会直接判 FAIL 的领域硬闸」在本阶段同样只能落 ``FAIL``（不能落
``INVALID_ARTIFACT``，那会让负控 parity 的期望判决漂移），因此它们进判据轴、
``weight=0``（布尔、不计权）。M1 的 ``CRIT_metrics_consistency`` 就是 ``HG-M1-7``：
``metrics.json`` 自报与 telemetry 重算对账不一致 ⇒ 判 FAIL。
"""
from __future__ import annotations

import json
import os
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import yaml

from orchestrator.benchmark import scoring
from orchestrator.benchmark import runtime_fingerprint as rf
from orchestrator.benchmark.phase281 import anchor as anchor_mod
from orchestrator.benchmark.phase281 import candidate_manifest as cm
from orchestrator.benchmark.phase281 import constants

__all__ = (
    "CandidateEvaluator",
    "EvaluationError",
    "GATE_ANCHOR",
    "GATE_ARTIFACT_PARSE",
    "GATE_BIRTH",
    "GATE_DELIVERABLES",
    "GATE_MANIFEST",
    "GATE_RUNTIME_IDENTITY",
    "GATE_RUNNER_TELEMETRY",
    "GATE_SELF_REPORT",
    "MAX_FILE_BYTES",
    "MAX_TOTAL_BYTES",
    "RUNNER_SUMMARY_EVENT",
)

#: 单件交付件上限 / 交付件总量上限（评测器自己的护栏；超限 ⇒ INVALID_ARTIFACT）
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 512 * 1024 * 1024

RUNNER_SUMMARY_EVENT = "summary"

GATE_ANCHOR = "GATE_ANCHOR"
GATE_MANIFEST = "GATE_MANIFEST"
GATE_DELIVERABLES = "GATE_DELIVERABLES"
GATE_ARTIFACT_PARSE = "GATE_ARTIFACT_PARSE"
GATE_RUNTIME_IDENTITY = "GATE_RUNTIME_IDENTITY"
GATE_RUNNER_TELEMETRY = "GATE_RUNNER_TELEMETRY"
GATE_BIRTH = "GATE_BIRTH"
GATE_SELF_REPORT = "GATE_SELF_REPORT"

#: 判据 id 前缀：评分维度
DIM_PREFIX = "DIM_"

#: 任务级硬闸判据（权重 0 的布尔判据；见模块 docstring）
CRIT_METRICS = "CRIT_metrics_consistency"

#: 只有这几个任务有「自报 metrics vs 重算」的对象（M1 的 metrics.json）
_METRICS_TASKS = ("mujoco_contact_push_v1",)


class EvaluationError(RuntimeError):
    """评测器自身用法错误（不是候选问题）；候选问题一律落 verdict。"""


def _now_z() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _process_argv() -> "list[str]":
    """进程**真实的**命令行（读 ``/proc/self/cmdline``），失败才退回 ``sys.argv``。

    为什么要读 ``/proc``：评测子进程以 ``<python> -I -c <bootstrap> …`` 起，
    ``sys.argv`` 只剩 bootstrap 之后的位置参数，看不见 ``-I``；用它判断「是否
    隔离」会把真隔离记成未隔离。退回分支只可能在非 Linux 上触发——那里本实验
    不跑，且一致性检查（``evaluator.isolated`` ⇔ argv 含 ``-I``）会当场报错，
    不会悄悄产出错误留痕。
    """
    try:
        raw = Path("/proc/self/cmdline").read_bytes()
    except OSError:
        return [sys.executable, *sys.argv]
    parts = [chunk.decode("utf-8", "replace") for chunk in raw.split(b"\0")]
    argv = [part for part in parts if part]
    return argv or [sys.executable, *sys.argv]


def _sha256_bytes(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def _read_json(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _parse_artifact(rel: str, raw: bytes) -> Any:
    """与 Phase 2.8.0 ``evaluator.parse_artifact`` 同一口径（只读、不执行）。"""
    text = raw.decode("utf-8")
    suffix = Path(rel).suffix
    if suffix == ".json":
        return json.loads(text)
    if suffix == ".jsonl":
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    if suffix in (".yaml", ".yml"):
        return yaml.safe_load(text)
    return text


def _strip_delivered(rel: str) -> str:
    prefix = cm.OUTPUT_PREFIX
    return rel[len(prefix):] if rel.startswith(prefix) else rel


def _as_int(value: Any) -> "int | None":
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


class CandidateEvaluator:
    """单个 Trial 的独立判决器。

    ``anchor`` 为 ``None`` 时（锚文件缺失/不可解析）``self_check()`` 非空，
    ``evaluate()`` 一律 ``BLOCKED_INFRA``——**禁止**在锚不可信时产出 PASS/FAIL。
    """

    def __init__(self, *, anchor: "anchor_mod.AnchorManifest | None",
                 root: "str | Path",
                 anchor_error: "str | None" = None,
                 scoring_source: "str | Path | None" = None) -> None:
        self.anchor = anchor
        self.root = Path(root)
        self.anchor_error = anchor_error
        #: 只为测试留缝（AC-P281-1-05 的「被改过的 scoring.py 副本」）；
        #: 生产调用恒为 ``None``，校验面永远是仓内真件。
        self.scoring_source = Path(scoring_source) if scoring_source else None

    # ---- 自检 ---------------------------------------------------------

    def self_check(self) -> "list[str]":
        """锚重算 + 判据摘要 + scoring 摘要 + 目录权限。空列表 = 可评。"""
        problems: "list[str]" = []
        if self.anchor is None:
            problems.append(self.anchor_error or "锚未加载（仓外信任锚不在场）")
            return problems
        path = anchor_mod.anchor_path(self.root)
        problems.extend(anchor_mod.verify_anchor(
            path, scoring_source=self.scoring_source))
        try:
            on_disk = anchor_mod.load_anchor(path)
        except anchor_mod.AnchorError as exc:
            problems.append(str(exc))
        else:
            if on_disk.manifest_sha256 != self.anchor.manifest_sha256:
                problems.append(
                    "内存锚与盘上锚不一致："
                    f"{self.anchor.manifest_sha256} ≠ {on_disk.manifest_sha256}")
        problems.extend(self._permission_problems())
        return problems

    def _permission_problems(self) -> "list[str]":
        problems: "list[str]" = []
        for label, path, want in (
            ("数据根", self.root, 0o700),
            ("private", self.root / "private", 0o700),
        ):
            if not path.is_dir():
                problems.append(f"{label} 目录不在场：{path}")
                continue
            mode = stat.S_IMODE(path.stat().st_mode)
            if mode != want:
                problems.append(f"{label} 权限 {oct(mode)} ≠ {oct(want)}：{path}")
        return problems

    # ---- 评测 ---------------------------------------------------------

    def evaluate(self, trial_dir: "str | Path") -> dict:
        """评测一个 Trial，返回 ``candidate_evaluation_result.v1`` 字典（不落盘）。"""
        base = Path(trial_dir)
        if not base.is_dir():
            raise EvaluationError(f"trial 目录不在场：{base}")

        peek_id, peek_task = self._peek_identity(base)
        self_problems = self.self_check()
        if self_problems:
            return self._result(
                trial_id=peek_id, task_id=peek_task,
                verdict="BLOCKED_INFRA", criteria=[], gates=[], facts={
                    "infra_problems": list(self_problems),
                    "trial_id_source": "manifest/目录反推/占位 三者中最先成立者"},
                ledger={"calls": 0, "seqs": [], "tokens_in": 0, "tokens_out": 0},
                checks=[], anchor_block=self._anchor_block(self_problems))

        manifest, manifest_problems, manifest_gate = self._load_manifest(base)
        trial_id = (manifest or {}).get("trial_id") or peek_id
        task_id = (manifest or {}).get("task_id") or peek_task

        if manifest_problems:
            return self._result(
                trial_id=trial_id, task_id=task_id,
                verdict="INVALID_ARTIFACT", criteria=[], gates=[manifest_gate],
                facts={"manifest_problems": manifest_problems},
                ledger=self._empty_ledger(), checks=[],
                anchor_block=self._anchor_block([]))

        assert manifest is not None and task_id is not None

        ledger_block, ledger_rows = self._ledger_block(base, trial_id)

        # --- 交付件完整性（存在、非符号链接、sha/bytes 相符、上限） ---
        artefacts, deliverable_gate, deliverable_problems, delivered_shas = \
            self._load_deliverables(base, manifest)
        if deliverable_problems:
            return self._result(
                trial_id=trial_id, task_id=task_id,
                verdict="INVALID_ARTIFACT", criteria=[],
                gates=[manifest_gate, deliverable_gate],
                facts={"deliverable_problems": deliverable_problems},
                ledger=ledger_block, checks=[], anchor_block=self._anchor_block([]))

        # --- 运行时身份（不符 ⇒ BLOCKED_INFRA：环境不符不是能力失败） ---
        runtime_gate, runtime_problems = self._runtime_gate(task_id, artefacts)
        if runtime_problems:
            return self._result(
                trial_id=trial_id, task_id=task_id,
                verdict="BLOCKED_INFRA", criteria=[],
                gates=[], facts={"runtime_problems": runtime_problems},
                ledger=ledger_block, checks=[], anchor_block=self._anchor_block([]))

        # --- 运行器遥测（缺 summary ⇒ 无法核验自报） ---
        summary, telemetry_gate = self._runner_summary(base, trial_id)

        # --- 出生 ---
        birth_status, birth_doc = self._birth(base)
        birth_gate_passed = birth_status == "BORN"
        birth_gate = {
            "id": GATE_BIRTH,
            "passed": birth_gate_passed,
            "detail": (f"birth_status={birth_status!r}"
                       if birth_doc else "缺 birth.json（无法证明出生）"),
            "source": "runner_telemetry",
        }

        # --- 自述 vs 观测 ---
        checks, self_report_gate = self._self_report(
            manifest, summary, birth_doc, ledger_block, delivered_shas)

        gates = [manifest_gate, deliverable_gate, runtime_gate,
                 telemetry_gate, birth_gate, self_report_gate]

        if summary is None:
            return self._result(
                trial_id=trial_id, task_id=task_id,
                verdict="INVALID_ARTIFACT", criteria=[], gates=gates,
                facts={"runner_telemetry_problems":
                       ["缺 telemetry.jsonl 的 summary 行，自报三字段无法核验"]},
                ledger=ledger_block, checks=checks,
                anchor_block=self._anchor_block([]))

        try:
            criteria_doc = self._criteria(task_id, artefacts)
        except EvaluationError as exc:
            return self._result(
                trial_id=trial_id, task_id=task_id,
                verdict="BLOCKED_INFRA", criteria=[], gates=[],
                facts={"infra_problems": [str(exc)]},
                ledger=ledger_block, checks=checks,
                anchor_block=self._anchor_block([str(exc)]))
        parse_gate = self._parse_gate(artefacts)
        gates.insert(0, parse_gate)

        facts = {
            "criteria_source": "scoring.score_task（仓外锚登记摘要的同一份源码）",
            "criteria_sha256": self._criteria_sha256(task_id),
            "anchor_manifest_sha256": self._anchor_sha(),
            "birth_status": birth_status,
            "runner_summary": summary,
        }
        facts.update(criteria_doc["facts"])

        # 优先级 3：claimed 与观测不一致 ⇒ INVALID_ARTIFACT（不许以候选为准）
        if any(not entry["equal"] for entry in checks):
            return self._result(
                trial_id=trial_id, task_id=task_id,
                verdict="INVALID_ARTIFACT", criteria=[], gates=gates, facts=facts,
                ledger=ledger_block, checks=checks,
                anchor_block=self._anchor_block([]))

        # 产物无法按判据解析/评分 ⇒ INVALID_ARTIFACT（缺件本身就属于产物问题）
        if criteria_doc["error"] or not parse_gate["passed"]:
            return self._result(
                trial_id=trial_id, task_id=task_id,
                verdict="INVALID_ARTIFACT", criteria=[], gates=gates, facts=facts,
                ledger=ledger_block, checks=checks,
                anchor_block=self._anchor_block([]))

        # 优先级 4：出生未过 ⇒ FAIL，判据全部 passed=false（不逐条给分）
        if not birth_gate_passed:
            for criterion in criteria_doc["criteria"]:
                criterion["passed"] = False
                criterion["fact"] = {"not_scored": True,
                                     "reason": f"出生未过（{birth_status!r}），不逐条给分"}
            return self._result(
                trial_id=trial_id, task_id=task_id, verdict="FAIL",
                criteria=criteria_doc["criteria"], gates=gates, facts=facts,
                ledger=ledger_block, checks=checks,
                anchor_block=self._anchor_block([]))

        verdict = ("PASS" if all(c["passed"] for c in criteria_doc["criteria"])
                   else "FAIL")
        return self._result(
            trial_id=trial_id, task_id=task_id, verdict=verdict,
            criteria=criteria_doc["criteria"], gates=gates, facts=facts,
            ledger=ledger_block, checks=checks,
            anchor_block=self._anchor_block([]))

    def evaluate_and_write(self, trial_dir: "str | Path",
                           out: "str | Path") -> dict:
        """评测并原子写 0600 结果件（``candidate_evaluation_result.v1``）。"""
        from orchestrator.common.fsutil import atomic_write_text

        doc = self.evaluate(trial_dir)
        text = json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        atomic_write_text(Path(out), text, mode=0o600)
        return doc

    # ---- 内部：结果装配 ------------------------------------------------

    def _anchor_sha(self) -> "str | None":
        return None if self.anchor is None else self.anchor.manifest_sha256

    def _criteria_sha256(self, task_id: str) -> "dict[str, str]":
        if self.anchor is None:
            return {}
        entry = self.anchor.criteria.get(task_id) or {}
        sha = entry.get("sha256")
        return {task_id: str(sha)} if sha else {}

    def _anchor_block(self, problems: "list[str]") -> dict:
        """结果里的锚块——**只装** schema 允许的四键。

        锚的问题不进这一块（``_check_anchor`` 是精确键集，多一个键就是不合
        schema），改由 :meth:`_result` 归到 ``facts["anchor_problems"]``：
        诊断信息仍然留痕，但不污染契约字段。
        """
        if self.anchor is None:
            return {"manifest_sha256": None, "criteria_sha256": None,
                    "scoring_sha256": None, "checked": False,
                    "problems": list(problems)}
        return {
            "manifest_sha256": self.anchor.manifest_sha256,
            "criteria_sha256": {k: str(v.get("sha256"))
                                for k, v in self.anchor.criteria.items()
                                if v.get("sha256")},
            "scoring_sha256": self.anchor.scoring_sha256,
            "checked": not problems,
            "problems": list(problems),
        }

    @staticmethod
    def _empty_ledger() -> dict:
        return {"calls": 0, "seqs": [], "tokens_in": 0, "tokens_out": 0}

    def _result(self, *, trial_id: str, task_id: "str | None", verdict: str,
                criteria: "list[dict]", gates: "list[dict]", facts: dict,
                ledger: dict, checks: "list[dict]", anchor_block: dict) -> dict:
        # 锚的诊断问题移出 ``anchor``（精确键集）落到自由形态的 ``facts``；
        # 事实不丢，契约不脏。
        anchor_problems = anchor_block.pop("problems", [])
        facts = dict(facts)
        if anchor_problems:
            facts["anchor_problems"] = list(anchor_problems)
        # ``evaluator.isolated`` 与 ``argv`` 是否含 ``-I`` 必须等价（schema 不变量），
        # 因此两者只从同一处观测推导：argv 是**进程真实的**命令行，不伪造。
        # 读 ``sys.argv`` 不够——``-I -c <bootstrap>`` 形态下 ``-I`` 不在 ``sys.argv``
        # 里（它只剩 bootstrap 之后的位置参数），会把「真隔离」记成「未隔离」。
        argv = _process_argv()
        doc = {
            "schema": cm.RESULT_SCHEMA,
            "trial_id": trial_id,
            "task_id": task_id,
            "verdict": verdict,
            "criteria": criteria,
            "gates": gates,
            "facts": facts,
            "ledger": ledger,
            "manifest_checks": checks,
            "anchor": anchor_block,
            "evaluated_at": _now_z(),
            "evaluator": {
                "python": sys.executable,
                "isolated": "-I" in argv,
                "argv": argv,
                "pid": os.getpid(),
            },
        }
        problems = cm.result_problems(doc)
        if problems:
            raise EvaluationError(
                "评测器自身产出了不合 schema 的结果（这是代码缺陷，不是候选问题）："
                + "；".join(problems))
        return doc

    # ---- 内部：manifest / 交付件 ---------------------------------------

    def _peek_identity(self, base: Path) -> "tuple[str, str | None]":
        """尽力取 ``(trial_id, task_id)``——**只用于 BLOCKED_INFRA 留痕**。

        锚不可信时结果里仍要给一个 ``trial_id``（schema 要求形态合法）。优先用
        manifest 自述值（经 ``constants.trial_id`` 反向重建校验，不接受自由串），
        其次用目录布局 ``runs/<task>/<arm>/rep<k>`` 反推；两者都不成立时落显式
        占位 ``unknown__arm__rep0``，并在 ``facts`` 里标 ``trial_id_source``。
        """
        candidate = None
        task_candidate: "str | None" = None
        path = base / cm.MANIFEST_NAME
        if path.is_file():
            try:
                doc = _read_json(path)
            except (OSError, json.JSONDecodeError):
                doc = None
            if isinstance(doc, Mapping):
                raw = doc.get("trial_id")
                parts = str(raw).split("__") if isinstance(raw, str) else []
                if len(parts) == 3 and parts[2].startswith("rep"):
                    try:
                        rep = int(parts[2][3:])
                    except ValueError:
                        rep = -1
                    if rep >= 0 and parts[0] in constants.TASK_IDS \
                            and parts[1] in constants.ARMS \
                            and constants.trial_id(parts[0], parts[1], rep) == raw:
                        candidate = str(raw)
                        task_candidate = parts[0]
                raw_task = doc.get("task_id")
                if isinstance(raw_task, str) and raw_task in constants.TASK_IDS:
                    task_candidate = raw_task
        if candidate is None:
            parts = base.name, base.parent.name, base.parent.parent.name
            rep_raw, arm_id, task_id = parts
            if rep_raw.startswith("rep"):
                try:
                    rep = int(rep_raw[3:])
                except ValueError:
                    rep = -1
                if rep >= 0 and task_id in constants.TASK_IDS \
                        and arm_id in constants.ARMS:
                    candidate = constants.trial_id(task_id, arm_id, rep)
                    task_candidate = task_id
        return (candidate or "unknown__arm__rep0", task_candidate)

    def _load_manifest(self, base: Path) -> "tuple[dict | None, list[str], dict]":
        path = base / cm.MANIFEST_NAME
        if not path.is_file():
            gate = {"id": GATE_MANIFEST, "passed": False,
                    "detail": f"缺 {cm.MANIFEST_NAME}", "source": "file_sha256"}
            return None, [f"缺 {cm.MANIFEST_NAME}：{path}"], gate
        try:
            doc = _read_json(path)
        except (OSError, json.JSONDecodeError) as exc:
            gate = {"id": GATE_MANIFEST, "passed": False,
                    "detail": f"{cm.MANIFEST_NAME} 不可解析：{exc}",
                    "source": "file_sha256"}
            return None, [f"{cm.MANIFEST_NAME} 不可解析：{exc}"], gate
        problems = cm.manifest_problems(doc)
        if problems:
            gate = {"id": GATE_MANIFEST, "passed": False,
                    "detail": "manifest 不合 schema：" + "；".join(problems),
                    "source": "file_sha256"}
            trial_id = doc.get("trial_id") if isinstance(doc, Mapping) else None
            task_id = doc.get("task_id") if isinstance(doc, Mapping) else None
            manifest = ({"trial_id": trial_id, "task_id": task_id}
                        if isinstance(trial_id, str) and isinstance(task_id, str)
                        else None)
            return manifest, problems, gate
        gate = {"id": GATE_MANIFEST, "passed": True,
                "detail": f"{cm.MANIFEST_NAME} 通过校验",
                "source": "file_sha256"}
        return cm.validate_manifest(doc), [], gate

    def _load_deliverables(self, base: Path, manifest: Mapping
                           ) -> "tuple[dict[str, Any], dict, list[str], dict[str, str]]":
        artefacts: "dict[str, Any]" = {}
        shas: "dict[str, str]" = {}
        problems: "list[str]" = []
        total = 0
        for index, entry in enumerate(manifest["deliverables"]):
            rel = str(entry["relpath"])
            path = base / rel
            if path.is_symlink():
                problems.append(f"deliverables[{index}] 是符号链接：{rel}")
                continue
            if not path.is_file():
                problems.append(f"deliverables[{index}] 不在场：{rel}")
                continue
            size = path.stat().st_size
            if size > MAX_FILE_BYTES:
                problems.append(
                    f"deliverables[{index}] 超过单件上限 {MAX_FILE_BYTES}B：{rel}")
                continue
            total += size
            raw = path.read_bytes()
            sha = _sha256_bytes(raw)
            shas[rel] = sha
            if sha != str(entry["sha256"]):
                problems.append(
                    f"deliverables[{index}] sha256 不符：{rel} "
                    f"清单 {entry['sha256']} ≠ 实得 {sha}")
                continue
            if size != int(entry["bytes"]):
                problems.append(
                    f"deliverables[{index}] bytes 不符：{rel} "
                    f"清单 {entry['bytes']} ≠ 实得 {size}")
                continue
            try:
                artefacts[_strip_delivered(rel)] = _parse_artifact(rel, raw)
            except (UnicodeDecodeError, json.JSONDecodeError, yaml.YAMLError,
                    ValueError) as exc:
                problems.append(
                    f"deliverables[{index}] 解析失败：{rel} "
                    f"（{type(exc).__name__}: {exc}）")
        if total > MAX_TOTAL_BYTES:
            problems.append(f"交付件总量 {total}B 超过上限 {MAX_TOTAL_BYTES}B")
        gate = {
            "id": GATE_DELIVERABLES,
            "passed": not problems,
            "detail": ("全部交付件在场、非符号链接、sha256/bytes 相符且未超限"
                       if not problems else "；".join(problems)),
            "source": "file_sha256",
        }
        return artefacts, gate, problems, shas

    def _parse_gate(self, artefacts: Mapping[str, Any]) -> dict:
        present = sorted(artefacts)
        return {"id": GATE_ARTIFACT_PARSE, "passed": True,
                "detail": f"已读入产物 {len(present)} 件：{', '.join(present)}",
                "source": "file_sha256"}

    # ---- 内部：运行时 --------------------------------------------------

    def _runtime_gate(self, task_id: str,
                      artefacts: Mapping[str, Any]) -> "tuple[dict, list[str]]":
        requirement = self._runtime_requirement(task_id)
        if requirement is None:
            return ({"id": GATE_RUNTIME_IDENTITY, "passed": False,
                     "detail": f"锚里没有 {task_id} 的运行时要求快照",
                     "source": "file_sha256"},
                    [f"锚缺 {task_id} 的 runtime 快照（不得对运行时身份不做判断）"])
        fingerprint = artefacts.get("runtime_fingerprint.json")
        if not isinstance(fingerprint, Mapping):
            return ({"id": GATE_RUNTIME_IDENTITY, "passed": False,
                     "detail": "缺 delivered/runtime_fingerprint.json",
                     "source": "file_sha256"},
                    ["缺 delivered/runtime_fingerprint.json（无法判定运行时身份）"])
        mismatches = rf.compare(requirement, fingerprint)
        if mismatches:
            detail = "；".join(getattr(m, "detail", str(m)) for m in mismatches)
            return ({"id": GATE_RUNTIME_IDENTITY, "passed": False,
                     "detail": detail, "source": "recomputed"},
                    [f"运行时身份不符：{detail}"])
        # 指纹对得上要求还不够：**两件自述运行时身份的产物必须互相自洽**。
        # 这是 Phase 2.8.0 ``l1_n07_host_impersonation`` 的判定面——探针记
        # ``captured_in='host'``、指纹记 ``'container'``，两份件逐份都对得上
        # 任务要求里的那一半，只有交叉比对才看得出「宿主冒充容器」。
        # 这类矛盾属环境侧（BLOCKED_INFRA），不是能力失败（FAIL）。
        contradictions = self._identity_contradictions(
            fingerprint, artefacts.get("env_probe.json"))
        if contradictions:
            detail = "；".join(contradictions)
            return ({"id": GATE_RUNTIME_IDENTITY, "passed": False,
                     "detail": detail, "source": "recomputed"},
                    [f"运行时身份自相矛盾：{detail}"])
        return ({"id": GATE_RUNTIME_IDENTITY, "passed": True,
                 "detail": "运行时指纹与锚登记的任务要求相符，且探针与指纹自洽",
                 "source": "recomputed"}, [])

    @staticmethod
    def _identity_contradictions(fingerprint: Mapping,
                                 probe: Any) -> "list[str]":
        """``env_probe.json`` 与 ``runtime_fingerprint.json`` 的身份矛盾。"""
        if not isinstance(probe, Mapping):
            return []
        out: "list[str]" = []
        fp_captured = fingerprint.get("captured_in")
        probe_captured = probe.get("captured_in")
        if fp_captured is not None and probe_captured is not None \
                and str(fp_captured) != str(probe_captured):
            out.append(f"探针 captured_in={probe_captured!r} ≠ 指纹 "
                       f"captured_in={fp_captured!r}（宿主冒充容器）")
        fp_python = fingerprint.get("python") or {}
        probe_exe = probe.get("python_executable")
        fp_exe = fp_python.get("executable")
        if probe_exe is not None and fp_exe is not None \
                and str(probe_exe) != str(fp_exe):
            out.append(f"探针解释器 {probe_exe!r} ≠ 指纹解释器 {fp_exe!r}")
        probe_ver = probe.get("python_version")
        fp_ver = fp_python.get("version")
        if probe_ver is not None and fp_ver is not None:
            probe_ver, fp_ver = str(probe_ver), str(fp_ver)
            if not (probe_ver == fp_ver or fp_ver.startswith(f"{probe_ver}.")):
                out.append(f"探针 python {probe_ver} ≠ 指纹 python {fp_ver}")
        return out

    def _runtime_requirement(self, task_id: str) -> "dict | None":
        if self.anchor is None:
            return None
        entry = self.anchor.runtime.get(task_id) or {}
        path = entry.get("path")
        if not path or not Path(str(path)).is_file():
            return None
        doc = _read_json(Path(str(path)))
        return dict(doc) if isinstance(doc, Mapping) else None

    # ---- 内部：运行器遥测 / 出生 ---------------------------------------

    def _runner_summary(self, base: Path, trial_id: str
                        ) -> "tuple[dict | None, dict]":
        path = base / "telemetry.jsonl"
        if not path.is_file():
            return None, {"id": GATE_RUNNER_TELEMETRY, "passed": False,
                          "detail": "缺 telemetry.jsonl", "source": "runner_telemetry"}
        summary: "dict | None" = None
        problems: "list[str]" = []
        for number, line in enumerate(
                path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                problems.append(f"第 {number} 行不是 JSON：{exc}")
                continue
            if isinstance(row, Mapping) and row.get("event") == RUNNER_SUMMARY_EVENT:
                summary = dict(row)
        if summary is None:
            problems.append("没有 event=summary 的汇总行")
        elif summary.get("trial_id") not in (None, trial_id):
            problems.append(
                f"summary.trial_id={summary.get('trial_id')!r} ≠ manifest {trial_id!r}")
        gate = {"id": GATE_RUNNER_TELEMETRY, "passed": not problems,
                "detail": ("；".join(problems) if problems
                           else "运行器遥测 summary 行在场且 trial_id 相符"),
                "source": "runner_telemetry"}
        return (None if problems else summary), gate

    def _birth(self, base: Path) -> "tuple[str | None, dict]":
        path = base / "birth.json"
        if not path.is_file():
            return None, {}
        try:
            doc = _read_json(path)
        except (OSError, json.JSONDecodeError):
            return None, {}
        if not isinstance(doc, Mapping):
            return None, {}
        raw = doc.get("birth_status")
        if raw is None:
            raw = doc.get("status")
        return (str(raw) if raw is not None else None), dict(doc)

    # ---- 内部：自述 vs 观测 --------------------------------------------

    def _ledger_block(self, base: Path, trial_id: str
                      ) -> "tuple[dict, list[dict]]":
        path = self.root / "quota" / "ledger.jsonl"
        rows: "list[dict]" = []
        if not path.is_file():
            return self._empty_ledger(), rows
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, Mapping):
                continue
            if row.get("event") != "call":
                continue
            if row.get("thread_id") != trial_id and row.get("task") != trial_id:
                continue
            rows.append(dict(row))
        seqs = sorted(int(row["seq"]) for row in rows
                      if _as_int(row.get("seq")) is not None)
        block = {
            "calls": len(rows),
            "seqs": seqs,
            "tokens_in": sum(_as_int(row.get("tokens_in")) or 0 for row in rows),
            "tokens_out": sum(_as_int(row.get("tokens_out")) or 0 for row in rows),
        }
        return block, rows

    def _self_report(self, manifest: Mapping, summary: "dict | None",
                     birth: Mapping, ledger: Mapping,
                     delivered_shas: Mapping[str, str]
                     ) -> "tuple[list[dict], dict]":
        claimed = dict(manifest["claimed"])
        observed = {
            "paid_model_calls": int(ledger["calls"]),
            "tokens_in": int(ledger["tokens_in"]),
            "tokens_out": int(ledger["tokens_out"]),
            "coding_rounds": None if summary is None else summary.get("coding_rounds"),
            "tool_steps": None if summary is None else summary.get("tool_steps"),
            "first_birth_round": (None if summary is None
                                  else summary.get("first_birth_round")),
        }
        if not birth:
            observed["first_birth_round"] = None
        checks: "list[dict]" = []
        for field in cm.CLAIMED_KEYS:
            want = claimed.get(field)
            got = observed.get(field)
            equal = (got is not None and want == got)
            checks.append({"field": f"claimed.{field}", "claimed": want,
                           "observed": got, "equal": equal})
        # runtime 自述的是「冻结指纹文件的 sha256」；这里拿**实测字节**对账。
        # 指纹**内容**是否满足任务要求由 GATE_RUNTIME_IDENTITY 判，两件事分开。
        runtime_claimed = (manifest.get("runtime") or {}).get("fingerprint_sha256")
        runtime_observed = delivered_shas.get(cm.OUTPUT_PREFIX + "runtime_fingerprint.json")
        checks.append({"field": "runtime.fingerprint_sha256",
                       "claimed": runtime_claimed,
                       "observed": runtime_observed,
                       "equal": runtime_observed is not None
                                and runtime_claimed == runtime_observed})
        passed = all(entry["equal"] for entry in checks)
        detail = ("claimed/ runtime 自述与账本/运行器观测/交付件字节一致" if passed
                  else "；".join(f"{e['field']}: 自述 {e['claimed']!r} ≠ 观测 "
                                f"{e['observed']!r}" for e in checks
                                if not e["equal"]))
        gate = {"id": GATE_SELF_REPORT, "passed": passed, "detail": detail,
                "source": "ledger"}
        return checks, gate

    # ---- 内部：判据 -----------------------------------------------------

    def _criteria(self, task_id: str, artefacts: Mapping[str, Any]) -> dict:
        criteria_data = self._criteria_data(task_id)
        dims: "dict[str, Any]" = {}
        facts: dict = {}
        error: "str | None" = None
        try:
            facts = scoring.facts_task(task_id, artefacts, criteria_data)
            dims = scoring.score_task(task_id, artefacts, criteria_data)
        except scoring.ScoringError as exc:
            error = f"{type(exc).__name__}: {exc}"
        except (ValueError, TypeError, KeyError) as exc:
            error = f"{type(exc).__name__}: {exc}"
        if error is not None:
            return {"criteria": [], "facts": {"scoring_error": error},
                    "error": error}

        weights = criteria_data["weights"]
        out: "list[dict]" = []
        for name in scoring.TASK_DIMENSIONS[task_id]:
            dim = dims[name]
            out.append({
                "id": DIM_PREFIX + name,
                "weight": float(weights[name]),
                "passed": float(dim.score) == 1.0,
                "fact": {"score": float(dim.score), "detail": dim.detail},
                "source": "recomputed",
            })
        out.extend(self._hard_gate_criteria(task_id, facts))
        return {"criteria": out, "facts": facts, "error": None}

    def _hard_gate_criteria(self, task_id: str, facts: Mapping) -> "list[dict]":
        out: "list[dict]" = []
        if task_id in _METRICS_TASKS:
            consistent = facts.get("metrics_consistent")
            passed = consistent is not False
            detail = ("metrics.json 与 telemetry 重算一致"
                      if consistent is True else
                      "metrics.json 缺可对账键，未做一致性判定")
            if consistent is False:
                detail = "metrics.json 自报与 telemetry 重算不一致：" + "；".join(
                    str(x) for x in (facts.get("metrics_mismatches") or []))
            out.append({"id": CRIT_METRICS, "weight": 0.0, "passed": passed,
                        "fact": {"metrics_consistent": consistent,
                                 "mismatches": list(facts.get("metrics_mismatches") or [])},
                        "source": "recomputed"})
        return out

    def _criteria_data(self, task_id: str) -> dict:
        if self.anchor is None:
            raise EvaluationError("锚未加载，不能取判据")
        entry = self.anchor.criteria.get(task_id) or {}
        path = entry.get("path")
        if not path or not Path(str(path)).is_file():
            raise EvaluationError(f"锚缺 {task_id} 的判据快照")
        doc = _read_json(Path(str(path)))
        if not isinstance(doc, Mapping):
            raise EvaluationError(f"判据快照不是对象：{path}")
        return dict(doc)
