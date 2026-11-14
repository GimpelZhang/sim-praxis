"""RSI 研究族账：跨轮 475 POST 硬帽的登记、冻结与许可。

RSI-R1.2 Plan §1.1 的落地模块。事实基础：

* RSI-R1（``rsi_r1_repair1/r1_20261111``）9 次实呼全 ok；
* RSI-R1.1（``rsi_r1_repair2/r1b_20261111``）22 次实呼（21 ok / 1 fail）；
* 两本账合计 **31**，逐行冻结哈希见 ``profiles.HISTORY_SOURCES``；
* 已关闭轮 ``r12-paid-20261113a``/``b`` 各实花 **5**，C 6、D 7、E 12、F 54、G 24、H 25、I 28、J 2、K 50、L 30、M 72（合计 320）（仪器缺陷轮，花掉的不归还）；
* 本轮 124 → 族帽 **475 = 31 + 320 + 124**（2026-11-13 用户裁决由 155 抬升）。
  carryover **不重复计**：历史块只在首次登记时导入一次，此后任何一轮都不能
  重新加一次、也不能改历史行。

设计约束（Plan §1.1 / §2.3 / AC05–AC07）：

* 单文件 ``family/family.json``（0600）+ ``family/family.lock``（flock 独占）；
* 写入是 tmp+fsync+replace 原子替换（``common.fsutil``），并发登记不会互踩；
* 历史块在**每次**登记/许可核验时从两本源账**重新逐行重算**并与冻结常量比对；
  源账变化（漂移）→ ``HISTORY_DRIFT``，不静默接受新值；
* 成员已登记则幂等返回同一许可；族帽余额不足 → ``FAMILY_CAP_EXCEEDED``；
* 同一 profile 只允许一个活动成员（本轮 124 + 历史 31 + 已关闭轮 320 = 475 无余额，
  第二轮在本轮关闭前必须被拒，而不是等付费时才发现没预算）；
  **关闭** = ``close_member``：只对已冻结且验证 PASS 的离线根执行，把成员
  标 ``closed`` 并释放其**未用**预留（``posts_used`` 仍占额，花掉的不归还），
  之后同一 profile 才能登记新一轮成员
  （T5 验证根关闭 → T6 付费根登记，Plan §5.2/§6.1 的既定换轮路径）。
* 许可（permit）是 T6 付费门的必要件：绑定族文档字节哈希 + 成员预留数；
  族文档漂移 → ``FAMILY_PERMIT_INVALID``。

本模块只做文件/哈希/登记，不触网、不读写账本命名空间、不构造任何 sender。
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from orchestrator.common.fsutil import atomic_write_text, ensure_private_dir
from orchestrator.rsi_r1.profiles import (
    FAMILY_CAP,
    FAMILY_HISTORY_POSTS,
    HISTORY_SOURCES,
    PROFILE_R12,
    CampaignProfile,
)

FAMILY_SCHEMA = "simpraxis.rsi_r12.family.v1"
FAMILY_PERMIT_SCHEMA = "simpraxis.rsi_r12.family_permit.v1"
FAMILY_SCHEMA_VERSION = 1

FAMILY_DIRNAME = "family"
FAMILY_DOC_NAME = "family.json"
FAMILY_LOCK_NAME = "family.lock"

_ENVELOPE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# 成员状态：``registered``（活动）/ ``exhausted``（用满预留）/ ``closed``
# （本轮关闭，预留已释放；只有它能解除「同 profile 单活动成员」）。
MEMBER_REGISTERED = "registered"
MEMBER_EXHAUSTED = "exhausted"
MEMBER_CLOSED = "closed"


class FamilyError(RuntimeError):
    """族账拒绝（fail-closed）；``code`` 为机判可引用常量。"""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"[{code}] {detail}")


# ---------------------------------------------------------------------------
# 规范化哈希（定义在本文档，导入与重算共用）
# ---------------------------------------------------------------------------

def canonical_row_sha256(row: Mapping[str, Any]) -> str:
    """单行规范哈希：sort_keys + 紧凑分隔符 + UTF-8。"""
    blob = json.dumps(row, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def canonical_rows_sha256(rows: Iterable[Mapping[str, Any]]) -> str:
    """整账行集哈希 = sha256(逐行 canonical_row_sha256 + "\\n" 的拼接)。

    定义写明在案：逐行哈希按文件顺序拼接、每行以换行结尾。任何一行的键序/
    数值/顺序变化都会改变它；行序变化同样会改变它。
    """
    joined = "".join(canonical_row_sha256(row) + "\n" for row in rows)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 历史源账导入（逐行重算 vs 冻结常量）
# ---------------------------------------------------------------------------

def _read_rows(path: Path) -> "list[dict[str, Any]]":
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raise FamilyError("HISTORY_UNAVAILABLE",
                          f"历史源账不存在：{path}") from None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise FamilyError("HISTORY_DRIFT", f"历史源账非 UTF-8：{path}") from None
    rows: "list[dict[str, Any]]" = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            raise FamilyError("HISTORY_DRIFT",
                              f"{path}:{lineno} 行不可解析") from None
        if not isinstance(row, dict):
            raise FamilyError("HISTORY_DRIFT",
                              f"{path}:{lineno} 行不是 JSON 对象")
        rows.append(row)
    if not rows:
        raise FamilyError("HISTORY_DRIFT", f"历史源账为空：{path}")
    return rows


def import_history(sources: "Iterable[Mapping[str, Any]]" = HISTORY_SOURCES,
                   *, now: "datetime | None" = None) -> "dict[str, Any]":
    """重算两本源账并与冻结常量逐项比对；返回可写入族文档的历史块。

    比对项：文件字节 sha256、行集规范化 sha256、call 行数、ok/fail 行数、
    seq 区间。任何一项不符 → ``HISTORY_DRIFT``（不会写入新值）。
    """
    imported: "list[dict[str, Any]]" = []
    total_calls = 0
    total_ok = 0
    total_fail = 0
    for source in sources:
        ledger = Path(str(source["root"])) / str(source["ledger_relpath"])
        raw_sha = hashlib.sha256(ledger.read_bytes()).hexdigest() \
            if ledger.is_file() else None
        if raw_sha is None:
            raise FamilyError("HISTORY_UNAVAILABLE", f"历史源账不存在：{ledger}")
        if raw_sha != source["ledger_sha256"]:
            raise FamilyError(
                "HISTORY_DRIFT",
                f"{ledger} 字节 sha256={raw_sha} 与冻结值 {source['ledger_sha256']} 不符")
        rows = _read_rows(ledger)
        rows_sha = canonical_rows_sha256(rows)
        if rows_sha != source["rows_sha256"]:
            raise FamilyError(
                "HISTORY_DRIFT",
                f"{ledger} 行集 sha256={rows_sha} 与冻结值 {source['rows_sha256']} 不符")
        non_call = [r for r in rows if r.get("event") != "call"]
        if non_call:
            raise FamilyError(
                "HISTORY_DRIFT",
                f"{ledger} 含非 call 行 {len(non_call)} 条（历史源账应全为 call）")
        calls = rows
        ok_rows = sum(1 for r in calls if r.get("ok") is True)
        fail_rows = len(calls) - ok_rows
        seqs = [int(r["seq"]) for r in calls]
        if seqs != list(range(seqs[0], seqs[0] + len(seqs))):
            raise FamilyError("HISTORY_DRIFT", f"{ledger} seq 不连续：{seqs}")
        if len(calls) != int(source["call_rows"]):
            raise FamilyError(
                "HISTORY_DRIFT",
                f"{ledger} call 行数 {len(calls)} != 冻结 {source['call_rows']}")
        if ok_rows != int(source["ok_rows"]) or fail_rows != int(source["fail_rows"]):
            raise FamilyError(
                "HISTORY_DRIFT",
                f"{ledger} ok/fail={ok_rows}/{fail_rows} != 冻结 "
                f"{source['ok_rows']}/{source['fail_rows']}")
        if [seqs[0], seqs[-1]] != list(source["seq_range"]):
            raise FamilyError(
                "HISTORY_DRIFT",
                f"{ledger} seq 区间 {[seqs[0], seqs[-1]]} != 冻结 "
                f"{list(source['seq_range'])}")
        # 账本权限：不允许组/他人可读写（0600 口径）。
        mode = os.stat(ledger).st_mode & 0o777
        if mode & 0o077:
            raise FamilyError(
                "HISTORY_PERMISSIONS",
                f"{ledger} 权限 {oct(mode)} 过宽（应为 0600）")
        imported.append({
            "envelope_id": str(source["envelope_id"]),
            "root": str(source["root"]),
            "ledger_relpath": str(source["ledger_relpath"]),
            "ledger_sha256": raw_sha,
            "rows_sha256": rows_sha,
            "call_rows": len(calls),
            "ok_rows": ok_rows,
            "fail_rows": fail_rows,
            "seq_range": [seqs[0], seqs[-1]],
        })
        total_calls += len(calls)
        total_ok += ok_rows
        total_fail += fail_rows
    if total_calls != FAMILY_HISTORY_POSTS:
        raise FamilyError(
            "HISTORY_DRIFT",
            f"历史 call 合计 {total_calls} != 冻结 {FAMILY_HISTORY_POSTS}")
    return {
        "imported_at_utc": _utc_now(now),
        "total_call_rows": total_calls,
        "total_ok_rows": total_ok,
        "total_fail_rows": total_fail,
        "sources": imported,
        "note": ("历史 31 = R1 9 + R1.1 22，两本独立 envelope 的最终根实呼；"
                 "carryover 不重复计，导入后不再累加"),
    }


def _utc_now(now: "datetime | None" = None) -> str:
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        raise FamilyError("CLOCK_INVALID", "时间必须带时区")
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# 路径与锁
# ---------------------------------------------------------------------------

@contextmanager
def _family_lock(family_root: Path):
    """独占 flock；锁文件在私有目录内（0700 / 0600）。"""
    ensure_private_dir(family_root)
    ensure_private_dir(family_root / FAMILY_DIRNAME)
    lock_path = family_root / FAMILY_DIRNAME / FAMILY_LOCK_NAME
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def family_doc_path(family_root: Path) -> Path:
    return family_root / FAMILY_DIRNAME / FAMILY_DOC_NAME


def _read_doc(path: Path) -> "dict[str, Any]":
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise FamilyError("FAMILY_ABSENT", f"族账不存在：{path}") from None
    try:
        doc = json.loads(text)
    except json.JSONDecodeError:
        raise FamilyError("FAMILY_CORRUPT", f"族账不可解析：{path}") from None
    if not isinstance(doc, dict):
        raise FamilyError("FAMILY_CORRUPT", f"族账不是对象：{path}")
    if doc.get("schema") != FAMILY_SCHEMA:
        raise FamilyError("FAMILY_CORRUPT",
                          f"族账 schema={doc.get('schema')!r} != {FAMILY_SCHEMA}")
    return doc


def _doc_sha256(doc: Mapping[str, Any]) -> str:
    blob = json.dumps(doc, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _write_doc(family_root: Path, doc: Mapping[str, Any]) -> None:
    path = family_doc_path(family_root)
    text = json.dumps(doc, indent=1, sort_keys=True, ensure_ascii=False) + "\n"
    atomic_write_text(path, text, mode=0o600)


def load_family_doc(family_root: Path) -> "dict[str, Any] | None":
    path = family_doc_path(family_root)
    if not path.is_file():
        return None
    return _read_doc(path)


# ---------------------------------------------------------------------------
# 登记 / 许可
# ---------------------------------------------------------------------------

def _assert_profile(profile: CampaignProfile) -> None:
    if profile.profile_id != PROFILE_R12:
        raise FamilyError("PROFILE_NOT_ELIGIBLE",
                          f"族账只服务 {PROFILE_R12}，实为 {profile.profile_id}")
    if profile.family_cap != FAMILY_CAP:
        raise FamilyError("FAMILY_CAP_MISMATCH",
                          f"profile 族帽 {profile.family_cap} != {FAMILY_CAP}")


def _assert_envelope_id(envelope_id: str) -> str:
    if not isinstance(envelope_id, str) or not _ENVELOPE_ID_RE.match(envelope_id):
        raise FamilyError("ENVELOPE_ID_INVALID",
                          f"envelope_id={envelope_id!r} 不合法")
    return envelope_id


def _reserved_total(doc: Mapping[str, Any]) -> int:
    """族帽占用 = 历史块 + 活动成员预留 + **已关闭成员实花**。

    锁内适配（2026-11-13，用户裁决 155→160）：旧版「关闭即释放全部预留」会把
    已真实花掉的 POST 从族账里抹掉（``r12-paid-20261113a`` 花 5 却被释放 124），
    新一轮再按 124 预留就会出现 31+5+124 实际 160 而账面只算 155 的窟窿。
    现口径：关闭成员按 ``posts_used`` 继续占额，未用部分才归还。
    """
    total = int(doc["history"]["total_call_rows"])
    for m in doc["members"]:
        if m.get("state") == MEMBER_CLOSED:
            total += int(m.get("posts_used") or 0)
        else:
            total += int(m["posts_reserved"])
    return total


def _build_permit(doc: Mapping[str, Any], member: Mapping[str, Any],
                  *, now: "datetime | None") -> "dict[str, Any]":
    doc_sha = _doc_sha256(doc)
    permit = {
        "schema": FAMILY_PERMIT_SCHEMA,
        "schema_version": FAMILY_SCHEMA_VERSION,
        "envelope_id": member["envelope_id"],
        "profile_id": member["profile_id"],
        "family_doc_sha256": doc_sha,
        "family_cap": int(doc["family_cap"]),
        "history_posts": int(doc["history"]["total_call_rows"]),
        "posts_reserved": int(member["posts_reserved"]),
        "posts_used": int(member["posts_used"]),
        "issued_at_utc": _utc_now(now),
    }
    blob = json.dumps(permit, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")
    permit["permit_sha256"] = hashlib.sha256(blob).hexdigest()
    return permit


def register(family_root: Path, profile: CampaignProfile, envelope_id: str,
             *, posts_reserved: "int | None" = None,
             history_sources: "Iterable[Mapping[str, Any]]" = HISTORY_SOURCES,
             now: "datetime | None" = None) -> "dict[str, Any]":
    """登记（或幂等取回）本 envelope 的族许可。

    首次登记：导入并冻结历史 31，写入成员行（posts_reserved 默认 124）。
    幂等：同 envelope_id 且同 profile/预留数 → 返回同一许可内容（族文档不变，
    除 permit 的 issued_at/permit_sha 外；族文档本身不重写）。
    拒绝：族帽不足、同 profile 已有其他成员、历史漂移、profile 不符。
    """
    _assert_profile(profile)
    envelope_id = _assert_envelope_id(envelope_id)
    reserved = int(profile.hard_cap if posts_reserved is None else posts_reserved)
    if reserved <= 0:
        raise FamilyError("FAMILY_CAP_EXCEEDED", f"posts_reserved={reserved} 非法")
    if reserved > profile.hard_cap:
        raise FamilyError("FAMILY_CAP_EXCEEDED",
                          f"posts_reserved={reserved} > 本轮帽 {profile.hard_cap}")

    with _family_lock(family_root):
        doc = load_family_doc(family_root)
        fresh_history = import_history(history_sources, now=now)
        if doc is None:
            doc = {
                "schema": FAMILY_SCHEMA,
                "schema_version": FAMILY_SCHEMA_VERSION,
                "family_cap": FAMILY_CAP,
                "profile_id": profile.profile_id,
                "history": fresh_history,
                "members": [],
                "created_at_utc": _utc_now(now),
            }
        else:
            _assert_history_unchanged(doc, fresh_history)
            if int(doc["family_cap"]) != FAMILY_CAP:
                raise FamilyError("FAMILY_CAP_MISMATCH",
                                  f"族账族帽 {doc['family_cap']} != {FAMILY_CAP}")
            if doc.get("profile_id") != profile.profile_id:
                raise FamilyError("FAMILY_PROFILE_MISMATCH",
                                  f"族账 profile={doc.get('profile_id')!r}")

        members = list(doc["members"])
        existing = [m for m in members if m["envelope_id"] == envelope_id]
        if existing:
            member = existing[0]
            if member["profile_id"] != profile.profile_id:
                raise FamilyError("FAMILY_PROFILE_MISMATCH",
                                  f"{envelope_id} 已以 {member['profile_id']} 登记")
            if int(member["posts_reserved"]) != reserved:
                raise FamilyError(
                    "FAMILY_RESERVATION_MISMATCH",
                    f"{envelope_id} 已预留 {member['posts_reserved']} != {reserved}")
            return _build_permit(doc, member, now=now)

        others = [m for m in members
                  if m["profile_id"] == profile.profile_id
                  and m.get("state") != MEMBER_CLOSED]
        if others:
            raise FamilyError(
                "FAMILY_ROUND_OPEN",
                (f"profile {profile.profile_id} 已有活动成员 "
                 f"{[m['envelope_id'] for m in others]}；本轮关闭前不得开第二轮"))
        reserved_now = _reserved_total(doc)
        if reserved_now + reserved > FAMILY_CAP:
            raise FamilyError(
                "FAMILY_CAP_EXCEEDED",
                f"已预留 {reserved_now} + 本轮 {reserved} > 族帽 {FAMILY_CAP}")

        member = {
            "envelope_id": envelope_id,
            "profile_id": profile.profile_id,
            "posts_reserved": reserved,
            "posts_used": 0,
            "state": "registered",
            # 该成员登记时的族帽（迁移后旧成员仍保留其历史帽值，判据按轮取用）
            "family_cap_at_registration": int(doc["family_cap"]),
            "registered_at_utc": _utc_now(now),
        }
        doc = dict(doc)
        doc["members"] = members + [member]
        doc["updated_at_utc"] = _utc_now(now)
        _write_doc(family_root, doc)
        return _build_permit(doc, member, now=now)


def close_member(family_root: Path, envelope_id: str, *, reason: str,
                 evidence: Mapping[str, Any] | None = None,
                 now: "datetime | None" = None) -> "dict[str, Any]":
    """关闭本轮成员并释放其预留（幂等；只有 ``closed`` 解除单成员约束）。

    只登记事实，不判根状态：调用方（``cli_r12 family close``）必须先在
    **离线验证根**上核完 LOCK/报告/三签并把根推到 ``CLOSED``，再把证据
    传进来落账。关闭后 ``_reserved_total`` 不再计该成员预留，同 profile
    可登记下一轮成员（Plan §5.2：T5 验证轮关闭 → T6 付费轮登记）。
    """
    envelope_id = _assert_envelope_id(envelope_id)
    if not isinstance(reason, str) or not reason.strip():
        raise FamilyError("FAMILY_CLOSE_REASON_REQUIRED", "关闭必须有 reason")
    with _family_lock(family_root):
        doc = load_family_doc(family_root)
        if doc is None:
            raise FamilyError("FAMILY_ABSENT", "族账不存在")
        members = list(doc["members"])
        hit = [m for m in members if m["envelope_id"] == envelope_id]
        if not hit:
            raise FamilyError("FAMILY_ABSENT", f"族账无成员 {envelope_id}")
        member = hit[0]
        if member.get("state") == MEMBER_CLOSED:
            return doc
        new_member = dict(member)
        new_member["state"] = MEMBER_CLOSED
        new_member["closed_at_utc"] = _utc_now(now)
        new_member["close_reason"] = str(reason)
        new_member["close_evidence"] = dict(evidence or {})
        new_doc = dict(doc)
        new_doc["members"] = [new_member if m["envelope_id"] == envelope_id
                              else m for m in members]
        new_doc["updated_at_utc"] = _utc_now(now)
        _write_doc(family_root, new_doc)
        return new_doc


def migrate_cap(family_root: Path, *, reason: str,
                evidence: Mapping[str, Any] | None = None,
                now: "datetime | None" = None) -> "dict[str, Any]":
    """把族文档的族帽抬到当前常量 ``FAMILY_CAP``（只增不减、幂等、留痕）。

    锁内适配（2026-11-13，用户裁决 155→160）。只做三件事：
    1. 给缺 ``family_cap_at_registration`` 的旧成员回填**迁移前**的帽值
       （旧轮判据仍按当时的帽复算，不被新帽回溯改写）；
    2. ``doc["family_cap"]`` 改为当前常量；
    3. 追加 ``cap_history`` 一条（from/to/reason/evidence/at_utc）。

    不是「悄悄改历史」：迁移件本身是族文档的一部分，随证据包逐字节留档；
    只增不减（``FAMILY_CAP_REGRESSION``），目标帽必须等于模块常量
    （``FAMILY_CAP_MISMATCH``），重复调用幂等。
    """
    if not isinstance(reason, str) or not reason.strip():
        raise FamilyError("FAMILY_CAP_REASON_REQUIRED", "族帽迁移必须有 reason")
    target = int(FAMILY_CAP)
    with _family_lock(family_root):
        doc = load_family_doc(family_root)
        if doc is None:
            raise FamilyError("FAMILY_ABSENT", "族账不存在")
        current = int(doc["family_cap"])
        if current > target:
            raise FamilyError(
                "FAMILY_CAP_REGRESSION",
                f"族帽回退不允许：{current} → {target}")
        members = []
        changed = current != target
        for m in doc["members"]:
            row = dict(m)
            if "family_cap_at_registration" not in row:
                row["family_cap_at_registration"] = current
                changed = True
            members.append(row)
        if not changed:
            return doc
        history = list(doc.get("cap_history") or [])
        if current != target:
            history.append({
                "from_cap": current,
                "to_cap": target,
                "reason": str(reason),
                "evidence": dict(evidence or {}),
                "at_utc": _utc_now(now),
            })
        new_doc = dict(doc)
        new_doc["members"] = members
        new_doc["family_cap"] = target
        new_doc["cap_history"] = history
        new_doc["updated_at_utc"] = _utc_now(now)
        _write_doc(family_root, new_doc)
        return new_doc


def _assert_history_unchanged(doc: Mapping[str, Any],
                              fresh: Mapping[str, Any]) -> None:
    stored = doc.get("history") or {}
    for key in ("total_call_rows", "total_ok_rows", "total_fail_rows"):
        if int(stored.get(key, -1)) != int(fresh[key]):
            raise FamilyError(
                "HISTORY_DRIFT",
                f"族账历史 {key}={stored.get(key)!r} != 重算 {fresh[key]}")
    stored_sources = {s["envelope_id"]: s for s in stored.get("sources", [])}
    fresh_sources = {s["envelope_id"]: s for s in fresh["sources"]}
    if set(stored_sources) != set(fresh_sources):
        raise FamilyError("HISTORY_DRIFT",
                          "族账历史来源集合与重算不一致")
    for envelope_id, source in fresh_sources.items():
        for key in ("ledger_sha256", "rows_sha256", "call_rows", "ok_rows",
                    "fail_rows", "seq_range"):
            if stored_sources[envelope_id].get(key) != source[key]:
                raise FamilyError(
                    "HISTORY_DRIFT",
                    f"{envelope_id}.{key} 与重算不一致")


def verify_permit(family_root: Path, permit: Mapping[str, Any],
                  profile: CampaignProfile, envelope_id: str, *,
                  history_sources: "Iterable[Mapping[str, Any]]" = HISTORY_SOURCES
                  ) -> "dict[str, Any]":
    """付费前核许可：族文档字节未漂移、成员预留未变、余额仍成立。

    任何不符 → ``FAMILY_PERMIT_INVALID``；历史源漂移 → ``HISTORY_DRIFT``。
    """
    _assert_profile(profile)
    envelope_id = _assert_envelope_id(envelope_id)
    if not isinstance(permit, Mapping):
        raise FamilyError("FAMILY_PERMIT_INVALID", "permit 不是对象")
    if permit.get("schema") != FAMILY_PERMIT_SCHEMA:
        raise FamilyError("FAMILY_PERMIT_INVALID",
                          f"permit.schema={permit.get('schema')!r}")
    claimed = dict(permit)
    claimed.pop("permit_sha256", None)
    blob = json.dumps(claimed, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")
    if hashlib.sha256(blob).hexdigest() != permit.get("permit_sha256"):
        raise FamilyError("FAMILY_PERMIT_INVALID", "permit 自哈希不符（被改动）")
    if permit.get("profile_id") != profile.profile_id:
        raise FamilyError("FAMILY_PERMIT_INVALID", "permit.profile_id 不符")
    if permit.get("envelope_id") != envelope_id:
        raise FamilyError("FAMILY_PERMIT_INVALID", "permit.envelope_id 不符")
    if int(permit.get("posts_reserved", -1)) != profile.hard_cap:
        raise FamilyError("FAMILY_PERMIT_INVALID",
                          "permit.posts_reserved 不等于本轮帽")
    doc = load_family_doc(family_root)
    if doc is None:
        raise FamilyError("FAMILY_PERMIT_INVALID", "族账不存在")
    _assert_history_unchanged(doc, import_history(history_sources))
    if _doc_sha256(doc) != permit.get("family_doc_sha256"):
        raise FamilyError("FAMILY_PERMIT_INVALID",
                          "族文档自 permit 签发后已变化")
    members = [m for m in doc["members"] if m["envelope_id"] == envelope_id]
    if not members:
        raise FamilyError("FAMILY_PERMIT_INVALID", "族账无该 envelope 成员")
    member = members[0]
    if int(member["posts_reserved"]) != int(permit["posts_reserved"]):
        raise FamilyError("FAMILY_PERMIT_INVALID", "成员预留数与 permit 不符")
    if int(member["posts_used"]) != int(permit["posts_used"]):
        raise FamilyError("FAMILY_PERMIT_INVALID", "成员已用量与 permit 不符")
    if _reserved_total(doc) > FAMILY_CAP:
        raise FamilyError("FAMILY_CAP_EXCEEDED", "族账预留总量已越过族帽")
    return doc


def record_usage(family_root: Path, envelope_id: str, posts_used: int, *,
                 now: "datetime | None" = None) -> "dict[str, Any]":
    """登记成员已用 POST 数（单调不减、≤ 预留、总量 ≤ 族帽）。

    由 4b/T9 收尾调用；只写 ``posts_used``，不触碰历史块。
    """
    envelope_id = _assert_envelope_id(envelope_id)
    used = int(posts_used)
    if used < 0:
        raise FamilyError("FAMILY_USAGE_INVALID", f"posts_used={used} 非法")
    with _family_lock(family_root):
        doc = load_family_doc(family_root)
        if doc is None:
            raise FamilyError("FAMILY_ABSENT", "族账不存在")
        members = [m for m in doc["members"] if m["envelope_id"] == envelope_id]
        if not members:
            raise FamilyError("FAMILY_ABSENT", f"族账无成员 {envelope_id}")
        member = members[0]
        previous = int(member["posts_used"])
        if used < previous:
            raise FamilyError(
                "FAMILY_USAGE_REGRESSION",
                f"posts_used {previous} → {used} 不允许回退")
        if used > int(member["posts_reserved"]):
            raise FamilyError(
                "FAMILY_CAP_EXCEEDED",
                f"posts_used={used} > 预留 {member['posts_reserved']}")
        new_member = dict(member)
        new_member["posts_used"] = used
        if used >= int(member["posts_reserved"]):
            new_member["state"] = "exhausted"
        new_members = [new_member if m["envelope_id"] == envelope_id else m
                       for m in doc["members"]]
        new_doc = dict(doc)
        new_doc["members"] = new_members
        new_doc["updated_at_utc"] = _utc_now(now)
        _write_doc(family_root, new_doc)
        return new_doc


def usage(family_root: Path) -> "dict[str, Any]":
    """只读汇总：历史、各成员预留/已用、族余额。"""
    doc = load_family_doc(family_root)
    if doc is None:
        return {
            "family_cap": FAMILY_CAP,
            "history_posts": None,
            "members": [],
            "reserved_total": None,
            "remaining": None,
            "family_doc_sha256": None,
        }
    reserved = _reserved_total(doc)
    return {
        "family_cap": int(doc["family_cap"]),
        "history_posts": int(doc["history"]["total_call_rows"]),
        "members": [
            {
                "envelope_id": m["envelope_id"],
                "profile_id": m["profile_id"],
                "posts_reserved": int(m["posts_reserved"]),
                "posts_used": int(m["posts_used"]),
                "state": m["state"],
                "family_cap_at_registration": int(
                    m.get("family_cap_at_registration") or doc["family_cap"]),
            }
            for m in doc["members"]
        ],
        "reserved_total": reserved,
        "remaining": int(doc["family_cap"]) - reserved,
        "family_doc_sha256": _doc_sha256(doc),
    }
