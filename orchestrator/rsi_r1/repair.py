"""RSI-R1 仪器修复储备（Plan §8.1「repair_reserve」）的唯一裁决面。

计划原文（§8.1，逐字）：

    储备不是第二次科学实验：仅 probe/practice 中的已证仪器缺陷，在任一正式
    study 首 POST 前，经具体新 root 裁决后重做 probe8+practice16，最多一次，
    继承整个 envelope 既往消耗、价格与未知预留。模型有效失败不自动解锁储备。
    正式 study 开始后不能本轮换 root 重跑。

本模块把这句话落成四件可机判的事实：

1. ``state/repair_authorization.json``：**一次且仅一次**的授权件。写前核
   「旧 root 存在、同 envelope、无正式 study 实呼、无既往 repair 占用」，
   并把用户裁决正文（字节 sha）与缺陷证据（逐件 sha）钉进去；文件存在后
   不可覆盖（单次使用）。
2. ``quota/carryover.json`` + 旧 root 账行**逐字节**搬入新 root 的
   ``quota/ledger.jsonl``：AC34「整轮新 root 不归零」——seq 在整轮内连续，
   旧消耗在原位可见，旧账一行不改（只读复制）。
3. 修复运行的 lane 归属：probe/practice 的重做**全部记 repair_reserve**
   （task 4，帽 24），不得回填 probe/practice 槽，也不得借给正式 study。
4. 门：授权件存在 + root 在 {FROZEN, PROBED} 且与协议配对（probe→FROZEN、
   practice→PROBED）+ 每个协议最多一次尝试。

R1 T6 的适用事实（2026-11-11）：probe 首跑 3/8 停于 DESIGN_FAILED，根因是
``sandbox._container_argv`` 漏传 ``/data /out``（仪器缺陷，非模型失败）；
用户逐字裁决「授权重开」后按本节重做。
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Callable, Mapping

from orchestrator.common.clock import utc_now_iso_z
from orchestrator.common.fsutil import ensure_private_dir
from orchestrator.rsi_r1 import closure as CL
from orchestrator.rsi_r1 import lanes as LANES
from orchestrator.rsi_r1 import state as S

AUTH_SCHEMA = "simpraxis.rsi_r1.repair_authorization.v1"
CARRYOVER_SCHEMA = "simpraxis.rsi_r1.carryover.v1"
SCHEMA_VERSION = 1

LANE = "repair_reserve"
AUTH_RELPATH = "state/repair_authorization.json"
CARRYOVER_RELPATH = "quota/carryover.json"

#: 授权覆盖的协议与各自 POST 数（§8.1「重做 probe8+practice16」）。
POSTS_BY_PROTOCOL: Mapping[str, int] = {"probe": 8, "practice": 16}
#: 协议 → 开始该协议时必须已到达的 root（与 ``protocol.ROOT_GATE`` 同义，
#: 这里独立列出是为了让本模块自证，不依赖 protocol 的导入图）。
REPAIR_ROOT_GATE: Mapping[str, str] = {"probe": S.ROOT_FROZEN,
                                       "practice": S.ROOT_PROBED}
#: repair_reserve 只在这两个门位可用（正式 study 首 POST 之前）。
ALLOWED_ROOTS = tuple(REPAIR_ROOT_GATE.values())
#: 正式 study 的 ledger task id：旧 root 若已出现过它们，储备即失效。
STUDY_TASK_IDS = (LANES.LANE_TASK_ID["study_A"], LANES.LANE_TASK_ID["study_B"])
#: 单次授权最多搬入的旧实呼数（旧 probe 8 + 旧 practice 16 之外的余量不需要）。
MAX_CARRY_CALLS = 24


class RepairError(RuntimeError):
    """修复储备的配置/授权错误（不是模型结论）。``code`` 公开。"""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def canonical_sha(doc: Any) -> str:
    """与 assembly/protocol 同口径的文档 sha（sorted keys、紧凑分隔）。"""
    return _sha256_bytes(json.dumps(
        doc, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8"))


def rows_sha256(rows: "list[dict[str, Any]]") -> str:
    """搬入行的摘要：结案机判按同一口径复核「前 N 行未被改写」（AC34）。"""
    return _sha256_bytes(json.dumps(
        rows, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8"))


def repair_cap() -> int:
    return int(LANES.LANE_POST_CAPS[LANE])


# ---------------------------------------------------------------------------
# 读取与门
# ---------------------------------------------------------------------------

def load_authorization(ctx: Any) -> "dict[str, Any] | None":
    """读授权件并复核自锚 sha；缺失返回 None，被改写即 ``AUTH_TAMPERED``。"""
    if not ctx.exists(AUTH_RELPATH):
        return None
    doc = ctx.read_json(AUTH_RELPATH)
    if not isinstance(doc, dict):
        raise RepairError("AUTH_TAMPERED", "授权件不是对象")
    want = doc.get("document_sha256")
    body = {k: v for k, v in doc.items() if k != "document_sha256"}
    if not isinstance(want, str) or canonical_sha(body) != want:
        raise RepairError("AUTH_TAMPERED",
                          "授权件 document_sha256 与内容不符")
    if doc.get("envelope_id") != ctx.envelope_id:
        raise RepairError("AUTH_TAMPERED",
                          f"授权件 envelope={doc.get('envelope_id')!r} "
                          f"!= {ctx.envelope_id!r}")
    return doc


def require_repair_gate(ctx: Any, *, root: str,
                        protocol: "str | None" = None) -> "dict[str, Any]":
    """repair_reserve 的准入面：授权件 + root 门位（+ 协议配对）。"""
    doc = load_authorization(ctx)
    if doc is None:
        raise RepairError(
            "REPAIR_NOT_AUTHORIZED",
            "repair_reserve 需要 state/repair_authorization.json（先 cli repair-authorize）")
    if protocol is not None:
        if protocol not in POSTS_BY_PROTOCOL:
            raise RepairError("PROTOCOL_UNKNOWN", repr(protocol))
        want = REPAIR_ROOT_GATE[protocol]
        if root != want:
            raise RepairError("ROOT_GATE",
                              f"修复 {protocol} 需要 root={want}，现为 {root}")
        if protocol not in (doc.get("protocols") or []):
            raise RepairError("REPAIR_NOT_AUTHORIZED",
                              f"授权件不含协议 {protocol!r}")
    elif root not in ALLOWED_ROOTS:
        raise RepairError(
            "ROOT_GATE",
            f"repair_reserve 只能在修复门位 {ALLOWED_ROOTS} 使用，现为 {root}")
    return doc


def require_protocol_sequence(ctx: Any, *, protocol: str) -> None:
    """修复协议的单次顺序：probe 只能 attempt 1；practice 需 probe PASS 在先。"""
    if protocol == "practice":
        rel = "protocol/report_probe.json"
        if not ctx.exists(rel):
            raise RepairError("REPAIR_SEQUENCE",
                              "修复 practice 前缺 protocol/report_probe.json")
        doc = ctx.read_json(rel)
        if doc.get("verdict") != "PASS" or doc.get("billing_lane") != LANE:
            raise RepairError(
                "REPAIR_SEQUENCE",
                f"修复 practice 需要同 lane 的 probe PASS，实为 "
                f"{doc.get('verdict')!r}/{doc.get('billing_lane')!r}")


# ---------------------------------------------------------------------------
# 授权 + 消耗继承（整轮新 root 不归零）
# ---------------------------------------------------------------------------

def _read_ledger_rows(path: Path) -> "list[dict[str, Any]]":
    rows: "list[dict[str, Any]]" = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RepairError("PRIOR_LEDGER_UNREADABLE",
                              f"第 {lineno} 行：{exc}") from None
        if not isinstance(row, dict):
            raise RepairError("PRIOR_LEDGER_UNREADABLE", f"第 {lineno} 行不是对象")
        rows.append(row)
    seqs = [r.get("seq") for r in rows if isinstance(r.get("seq"), int)]
    if seqs != list(range(1, len(seqs) + 1)):
        raise RepairError("PRIOR_LEDGER_UNREADABLE", "旧账整数 seq 不连续")
    return rows


def _require_prior_root(ctx: Any, prior_root: "str | Path") -> Path:
    prior = Path(prior_root).expanduser()
    if not prior.is_dir():
        raise RepairError("PRIOR_ROOT_MISSING", f"{prior} 不是目录")
    prior = prior.resolve()
    cur = ctx.root.resolve()
    if prior == cur:
        raise RepairError("PRIOR_ROOT_SAME", "旧 root 与当前 root 相同（新 root 裁决要求互异）")
    for a, b, label in ((prior, cur, "旧"), (cur, prior, "当前")):
        try:
            a.relative_to(b)
        except ValueError:
            continue
        raise RepairError("PRIOR_ROOT_NESTED",
                          f"{label} root 嵌套在对方内：{a} ⊂ {b}")
    env_path = prior / "contract" / "envelope.json"
    if not env_path.is_file():
        raise RepairError("PRIOR_ROOT_INVALID", f"缺 {env_path}")
    env_doc = json.loads(env_path.read_text(encoding="utf-8"))
    if env_doc.get("envelope_id") != ctx.envelope_id:
        raise RepairError(
            "ENVELOPE_MISMATCH",
            f"旧 root envelope={env_doc.get('envelope_id')!r} "
            f"!= {ctx.envelope_id!r}")
    return prior


def _prior_facts(prior: Path) -> "dict[str, Any]":
    ledger = prior / "quota" / "ledger.jsonl"
    if not ledger.is_file():
        raise RepairError("PRIOR_LEDGER_MISSING", f"缺 {ledger}")
    raw = ledger.read_bytes()
    rows = _read_ledger_rows(ledger)
    calls = [r for r in rows if r.get("event", "call") == "call"]
    by_task: "dict[int, int]" = {}
    for row in calls:
        task = row.get("task")
        key = int(task) if task is not None else -1
        by_task[key] = by_task.get(key, 0) + 1
    if any(n for t, n in by_task.items() if t in STUDY_TASK_IDS):
        raise RepairError(
            "STUDY_ALREADY_STARTED",
            f"旧 root 已有正式 study 实呼（by_task={by_task}）：§8.1 禁止本轮换 root 重跑")
    repair_task = LANES.LANE_TASK_ID[LANE]
    if by_task.get(repair_task):
        raise RepairError(
            "REPAIR_ALREADY_USED",
            f"旧 root 已用过 repair_reserve（{by_task[repair_task]} 呼）：最多一次")
    probe_calls = by_task.get(LANES.LANE_TASK_ID["probe"], 0)
    practice_calls = by_task.get(LANES.LANE_TASK_ID["practice"], 0)
    if probe_calls + practice_calls == 0:
        raise RepairError(
            "NOTHING_TO_REPAIR",
            "旧 root 无 probe/practice 实呼：储备不适用于未经协议试跑的设计")
    if len(calls) > MAX_CARRY_CALLS:
        raise RepairError("CARRY_TOO_LARGE",
                          f"旧账实呼 {len(calls)} > {MAX_CARRY_CALLS}")
    reservations = prior / "quota" / f"reservations_{LANE}.jsonl"
    if reservations.is_file() and reservations.read_text(encoding="utf-8").strip():
        raise RepairError("REPAIR_ALREADY_USED",
                          f"旧 root 已有 {LANE} 槽预留：最多一次")
    return {"ledger_path": ledger, "raw": raw, "rows": rows, "calls": len(calls),
            "calls_by_task": {str(k): v for k, v in sorted(by_task.items())}}


def _write_carryover(ctx: Any, prior: Path, facts: Mapping[str, Any],
                     *, clock: Callable[[], str]) -> "dict[str, Any]":
    target = ctx.assert_write_allowed(ctx.relative(CL.LEDGER_RELPATH))
    if target.exists() and target.read_text(encoding="utf-8").strip():
        raise RepairError("CARRYOVER_TARGET_NOT_EMPTY",
                          f"{target} 已有账行；新 root 只在空账上继承")
    ensure_private_dir(target.parent)
    # 逐字节搬运：seq 在整轮内连续，旧行在任何字段上都不被重写。
    fd = os.open(target, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        os.write(fd, facts["raw"])
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(target, 0o600)
    copied = target.read_bytes()
    if copied != facts["raw"]:
        raise RepairError("CARRYOVER_MISMATCH", "搬入字节与旧账不一致")
    manifest = {
        "schema": CARRYOVER_SCHEMA, "schema_version": SCHEMA_VERSION,
        "envelope_id": ctx.envelope_id, "created_at_utc": clock(),
        "source_root": str(prior),
        "source_ledger_sha256": _sha256_bytes(facts["raw"]),
        "carried_bytes": len(facts["raw"]), "carried_rows": len(facts["rows"]),
        "carried_rows_sha256": rows_sha256(facts["rows"]),
        "carried_calls": int(facts["calls"]),
        "calls_by_task": dict(facts["calls_by_task"]),
        "target_relpath": CL.LEDGER_RELPATH,
        "target_ledger_sha256": _sha256_bytes(copied),
        "reason": ("AC34 整轮新 root 不归零：旧 root 实呼进入本 root 账本原位，"
                   "seq 整轮连续；旧账文件本身未被修改（只读复制）"),
    }
    ctx.write_json(CARRYOVER_RELPATH, manifest)
    return manifest


def authorize(ctx: Any, *, prior_root: "str | Path", decision_text: str,
              evidence: Mapping[str, "str | Path"] | None = None,
              note: str = "",
              clock: Callable[[], str] = utc_now_iso_z) -> "dict[str, Any]":
    """写一次性的 repair 授权件（连同旧消耗搬入）。不可重复、不可覆盖。"""
    if ctx.exists(AUTH_RELPATH):
        raise RepairError("REPAIR_ALREADY_AUTHORIZED",
                          f"{AUTH_RELPATH} 已存在：储备最多一次，不允许重写")
    if S.ResearchState(ctx).root_state() != S.ROOT_INIT:
        raise RepairError(
            "ROOT_NOT_INIT",
            f"授权必须在新 root 的 INIT 位（现为 {S.ResearchState(ctx).root_state()}）")
    if ctx.exists(CARRYOVER_RELPATH):
        raise RepairError("REPAIR_ALREADY_AUTHORIZED", "carryover 已存在")
    text = str(decision_text)
    if not text.strip():
        raise RepairError("DECISION_TEXT_EMPTY", "用户裁决正文不得为空")
    raw_text = text.encode("utf-8")
    if len(raw_text) > 64 * 1024:
        raise RepairError("DECISION_TEXT_TOO_LARGE", f"{len(raw_text)} B > 64 KiB")
    ev: "dict[str, dict[str, Any]]" = {}
    for name, path in dict(evidence or {}).items():
        p = Path(path).expanduser()
        if not p.is_file():
            raise RepairError("EVIDENCE_MISSING", f"{name}: {p}")
        ev[str(name)] = {"path": str(p.resolve()),
                         "sha256": _sha256_file(p), "bytes": p.stat().st_size}
    if not ev:
        raise RepairError("EVIDENCE_MISSING", "至少给一件缺陷证据（--evidence NAME=PATH）")

    prior = _require_prior_root(ctx, prior_root)
    facts = _prior_facts(prior)
    carry = _write_carryover(ctx, prior, facts, clock=clock)

    doc = {
        "schema": AUTH_SCHEMA, "schema_version": SCHEMA_VERSION,
        "envelope_id": ctx.envelope_id, "created_at_utc": clock(),
        "lane": LANE, "lane_task_id": int(LANES.LANE_TASK_ID[LANE]),
        "cap_posts": repair_cap(),
        "protocols": sorted(POSTS_BY_PROTOCOL),
        "posts_by_protocol": dict(POSTS_BY_PROTOCOL),
        "single_use": True,
        "prior_root": str(prior),
        "prior_ledger_sha256": carry["source_ledger_sha256"],
        "prior_calls": int(facts["calls"]),
        "prior_calls_by_task": dict(facts["calls_by_task"]),
        "carryover_relpath": CARRYOVER_RELPATH,
        "decision_text": text,
        "decision_text_sha256": _sha256_bytes(raw_text),
        "evidence": ev,
        "note": note,
    }
    doc["document_sha256"] = canonical_sha(doc)
    ctx.write_json(AUTH_RELPATH, doc)
    return doc


__all__ = ["ALLOWED_ROOTS", "AUTH_RELPATH", "AUTH_SCHEMA", "CARRYOVER_RELPATH",
           "CARRYOVER_SCHEMA", "LANE", "POSTS_BY_PROTOCOL", "REPAIR_ROOT_GATE",
           "RepairError", "authorize", "canonical_sha", "load_authorization",
           "repair_cap", "require_protocol_sequence", "require_repair_gate",
           "rows_sha256"]
