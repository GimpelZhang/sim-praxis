"""RSI-R1 T5：不可变闭包 LOCK + 双签（Plan §5.5/§6.5，AC27）。

AC27 要求「同 SHA 完整 LOCK 与独立判据**先**存在」——即付费/容器动作开始前，
以下八组闭包必须逐文件定死：

``runner``（沙盒执行器/批执行）、``shared``（运行时/路径/合同/量表/观测/指标）、
``scenes``（两场景公开合同）、``schema``（状态机/循环/人工事件/监护）、
``judge``（独立判据 + 公开统计）、``replay``（复跑独立性/比对）、
``checker``（纯标准库独立复算器）、``image``（容器镜像 digest 钉死）。

纪律：

* **不可变**：``contract/lock.json`` 一旦写出，再次 ``freeze`` 直接拒绝；
  内容篡改由 ``lock.sha256``（只覆盖 5 个绑定键，与 ``loop.LockDocument`` 同口径）
  与闭包/依赖逐文件重算双层发现。
* **同 SHA 双签**：``reviewer`` 与 ``verifier`` 两角色各一份签名，必须
  ``decision=APPROVE``、绑定同一 ``lock_sha256``，且 **actor 互不相同**。
* **独立判据先存在**：checker 源码本身在 ``checker`` 组内被 hash；LOCK 里还登记
  它的 relpath/sha，结案复算必须用同一份。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, Mapping

from orchestrator.common.clock import utc_now_iso_z
from orchestrator.rsi_r1 import contracts as C
from orchestrator.rsi_r1 import loop as L

LOCK_RELPATH = "contract/lock.json"
LOCK_SHA_RELPATH = "contract/lock.sha256"
SIG_DIR = "contract/signatures"
CLOSURE_SCHEMA = "simpraxis.rsi_r1.closure_lock.v1"
SIG_SCHEMA = "simpraxis.rsi_r1.lock_signature.v1"

STAGE = "study"
PHASE = "study"

ROLES = ("reviewer", "verifier")
DECISIONS = ("APPROVE", "REJECT")

REPO_ROOT = Path(__file__).resolve().parents[2]

#: 八组闭包（文件名相对仓库根；顺序即写入顺序，不影响 digest）。
CLOSURE_GROUPS: "dict[str, tuple[str, ...]]" = {
    "runner": ("orchestrator/rsi_r1/sandbox.py",
               "orchestrator/rsi_r1/experiments.py",
               "orchestrator/rsi_r1/backend.py"),
    "shared": ("orchestrator/rsi_r1/runtime.py",
               "orchestrator/common/runtime_paths.py",
               "orchestrator/rsi_r1/contracts.py",
               "orchestrator/rsi_r1/lanes.py",
               "orchestrator/rsi_r1/rubric.py",
               "orchestrator/rsi_r1/observations.py",
               "orchestrator/rsi_r1/metrics.py"),
    "scenes": ("orchestrator/rsi_r1/scenes.py",),
    "schema": ("orchestrator/rsi_r1/state.py",
               "orchestrator/rsi_r1/loop.py",
               "orchestrator/rsi_r1/assembly.py",
               "orchestrator/rsi_r1/protocol.py",
               "orchestrator/rsi_r1/gateway_transport.py",
               "orchestrator/rsi_r1/closure.py",
               "orchestrator/rsi_r1/repair.py",
               "orchestrator/rsi_r1/lock.py",
               "orchestrator/rsi_r1/human_events.py",
               "orchestrator/rsi_r1/watchdog.py"),
    "judge": ("orchestrator/rsi_r1/judge.py",
              "orchestrator/rsi_r1/statistics.py"),
    "replay": ("orchestrator/rsi_r1/replay.py",),
    "checker": ("scripts/verification/check_rsi_r1_closure.py",),
}

CALIBRATION_REPORTS = ("contract/calibration_report_v2.json",
                       "contract/calibration_report.json")
INSTRUMENT_PREREG = "contract/instrument_prereg.json"


class LockError(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


def _sha256_file(path: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _write_locked(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


# ---------------------------------------------------------------------------
# 闭包构造与复核
# ---------------------------------------------------------------------------

def _parse_image_pin(pin: Any, *, where: str) -> "dict[str, str]":
    if not isinstance(pin, str) or pin.count("@sha256:") != 1:
        raise LockError("IMAGE_PIN_INVALID", f"{where}={pin!r} 不是 name@sha256:<64>")
    ref, digest = pin.split("@sha256:", 1)
    if not ref or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise LockError("IMAGE_PIN_INVALID", f"{where}={pin!r} digest 非 64 hex")
    return {"ref": ref, "digest": f"sha256:{digest}",
            "pin": f"{ref}@sha256:{digest}"}


def build_closure(repo_root: Path, *, image_pin: str) -> "dict[str, Any]":
    groups: "dict[str, Any]" = {}
    for group in sorted(CLOSURE_GROUPS):
        files: "list[dict[str, Any]]" = []
        for rel in CLOSURE_GROUPS[group]:
            p = Path(repo_root) / rel
            if not p.is_file():
                raise LockError("CLOSURE_FILE_MISSING", f"{group}:{rel}")
            files.append({"path": rel, "sha256": _sha256_file(p)})
        groups[group] = {
            "files": files,
            "group_sha256": C.canonical_sha256({"group": group, "files": files}),
            "checker_present": group == "checker",
        }
    image = _parse_image_pin(image_pin, where="container_image")
    image["signed"] = True
    body = {"schema": CLOSURE_SCHEMA, "schema_version": 1,
            "groups": groups, "image": image}
    body["closure_sha256"] = C.canonical_sha256(body)
    return body


def verify_closure(closure: Mapping[str, Any], repo_root: Path) -> None:
    if closure.get("schema") != CLOSURE_SCHEMA:
        raise LockError("CLOSURE_SCHEMA", repr(closure.get("schema")))
    groups = closure.get("groups") or {}
    if set(groups) != set(CLOSURE_GROUPS):
        raise LockError("CLOSURE_GROUPS",
                        f"缺组 {sorted(set(CLOSURE_GROUPS) - set(groups))} / "
                        f"多组 {sorted(set(groups) - set(CLOSURE_GROUPS))}")
    for group in sorted(CLOSURE_GROUPS):
        entry = groups[group]
        files = entry.get("files") or []
        want_paths = list(CLOSURE_GROUPS[group])
        if [f.get("path") for f in files] != want_paths:
            raise LockError("CLOSURE_FILES", f"{group} 文件集与定义不符")
        for f in files:
            p = Path(repo_root) / str(f["path"])
            if not p.is_file():
                raise LockError("CLOSURE_DRIFT", f"{group}:{f['path']} 缺失")
            got = _sha256_file(p)
            if got != f.get("sha256"):
                raise LockError("CLOSURE_DRIFT",
                                f"{group}:{f['path']} {f.get('sha256')} → {got}")
        if entry.get("group_sha256") != C.canonical_sha256(
                {"group": group, "files": files}):
            raise LockError("CLOSURE_DRIFT", f"{group} group_sha256 不符")
    image = closure.get("image") or {}
    if image.get("pin") != f"{image.get('ref')}@{(image.get('digest') or '')}":
        raise LockError("IMAGE_PIN_INVALID", "closure.image 自洽性失败")
    _parse_image_pin(image.get("pin"), where="closure.image.pin")
    body = {k: closure[k] for k in ("schema", "schema_version", "groups", "image")}
    if closure.get("closure_sha256") != C.canonical_sha256(body):
        raise LockError("CLOSURE_DRIFT", "closure_sha256 不符")


# ---------------------------------------------------------------------------
# 冻结前的合同面
# ---------------------------------------------------------------------------

def regenerate_refusals(ctx: Any) -> "dict[str, Any]":
    """按 ``contracts.REFUSAL_CLAUSES`` 重生成 refusals（T3 尾债，冻结前必做）。"""
    doc = {"schema": f"{C.SCHEMA_PREFIX}.refusals.v1",
           "schema_version": C.SCHEMA_VERSION,
           "clauses": dict(C.REFUSAL_CLAUSES)}
    ctx.write_json("contract/refusals.json", doc)
    return doc


def _read_calibration(ctx: Any) -> "tuple[str, dict[str, Any]]":
    for rel in CALIBRATION_REPORTS:
        if ctx.exists(rel):
            doc = ctx.read_json(rel)
            if doc.get("verdict") != "CALIBRATION_PASS":
                raise LockError("CALIBRATION_NOT_READY",
                                f"{rel} verdict={doc.get('verdict')!r}")
            return rel, doc
    raise LockError("CALIBRATION_NOT_READY", "缺校准报告")


def _dependencies(repo_root: Path) -> "dict[str, str]":
    from orchestrator.rsi_r1 import cli  # 延迟导入，避免 cli↔lock 循环
    deps: "dict[str, str]" = {}
    for rel in cli.SEAM_FILES:
        p = Path(repo_root) / rel
        if not p.is_file():
            raise LockError("DEPENDENCY_MISSING", rel)
        deps[rel] = _sha256_file(p)
    return deps


# ---------------------------------------------------------------------------
# freeze / sign / verify
# ---------------------------------------------------------------------------

def freeze_lock(ctx: Any, *, repo_root: "Path | str | None" = None,
                image_pin: "str | None" = None,
                now: "Callable[[], str] | None" = None) -> "dict[str, Any]":
    """写不可变 LOCK（已存在即拒绝）。返回 ``{doc, sha256, lock}``。"""
    root = Path(repo_root) if repo_root is not None else REPO_ROOT
    if ctx.exists(LOCK_RELPATH) or ctx.exists(LOCK_SHA_RELPATH):
        raise LockError("LOCK_EXISTS", "LOCK 已冻结，不可重建（不可变）")
    envelope = ctx.read_json("contract/envelope.json")
    budget = ctx.read_json("contract/budget.json")
    envelope_id = C.require_safe_id(envelope.get("envelope_id"), where="envelope")
    budget_sha = C.BudgetSpec.from_dict(budget).sha256
    if envelope.get("budget_sha256") != budget_sha:
        raise LockError("BUDGET_DRIFT",
                        f"envelope.budget_sha256={envelope.get('budget_sha256')!r} "
                        f"!= budget.sha256={budget_sha}")
    report_rel, report = _read_calibration(ctx)
    if not ctx.exists(INSTRUMENT_PREREG):
        raise LockError("PREREG_MISSING", INSTRUMENT_PREREG)
    regenerate_refusals(ctx)
    pin = image_pin or report.get("container_image")
    if not isinstance(pin, str) or not pin:
        raise LockError("IMAGE_PIN_INVALID", "校准报告未携带 container_image")
    closure = build_closure(root, image_pin=pin)
    doc: "dict[str, Any]" = {
        "schema": L.LOCK_SCHEMA,
        "schema_version": 1,
        "envelope_id": envelope_id,
        "budget_sha256": budget_sha,
        "stage": STAGE,
        "phase": PHASE,
        "dependencies": _dependencies(root),
        "closure": closure,
        "closure_sha256": closure["closure_sha256"],
        "image": closure["image"],
        "instrument_prereg": {
            "path": INSTRUMENT_PREREG,
            "sha256": _sha256_file(ctx.relative(INSTRUMENT_PREREG))},
        "calibration": {
            "path": report_rel,
            "sha256": _sha256_file(ctx.relative(report_rel)),
            "verdict": report.get("verdict"),
            "design_version": report.get("design_version")},
        "refusals_sha256": _sha256_file(ctx.relative("contract/refusals.json")),
        "created_at_utc": (now or utc_now_iso_z)(),
    }
    lock = L.LockDocument.from_doc(doc)  # 与运行期同一解析口径（fail-closed）
    _write_locked(ctx.relative(LOCK_RELPATH),
                  (C.canonical_json(doc) + "\n").encode("utf-8"))
    _write_locked(ctx.relative(LOCK_SHA_RELPATH),
                  (lock.sha256 + "\n").encode("utf-8"))
    return {"doc": doc, "sha256": lock.sha256, "lock": lock}


def read_lock_sha(ctx: Any) -> str:
    if not ctx.exists(LOCK_SHA_RELPATH):
        raise LockError("LOCK_MISSING", LOCK_SHA_RELPATH)
    text = ctx.relative(LOCK_SHA_RELPATH).read_text(encoding="utf-8").strip()
    if len(text) != 64:
        raise LockError("LOCK_TAMPERED", "lock.sha256 非 64 hex")
    return text


def load_frozen(ctx: Any) -> "L.LockDocument":
    if not ctx.exists(LOCK_RELPATH):
        raise LockError("LOCK_MISSING", LOCK_RELPATH)
    doc = ctx.read_json(LOCK_RELPATH)
    lock = L.LockDocument.from_doc(doc)
    if read_lock_sha(ctx) != lock.sha256:
        raise LockError("LOCK_TAMPERED", "lock.sha256 与 lock.json 不符")
    return lock


def lock_reader(ctx: Any) -> "Callable[[], Mapping[str, Any]]":
    """给 ``build_production_sender`` 的 reader：任何读失败都抛（→ LOCK_MISSING）。"""
    def _read() -> Mapping[str, Any]:
        if not ctx.exists(LOCK_RELPATH):
            raise FileNotFoundError(LOCK_RELPATH)
        return ctx.read_json(LOCK_RELPATH)
    return _read


def sign_lock(ctx: Any, *, role: str, actor: str, decision: str,
              rationale: str = "", now: "Callable[[], str] | None" = None
              ) -> "dict[str, Any]":
    if role not in ROLES:
        raise LockError("SIGNATURE_ROLE", repr(role))
    if decision not in DECISIONS:
        raise LockError("SIGNATURE_DECISION", repr(decision))
    if not isinstance(actor, str) or not actor.strip():
        raise LockError("SIGNATURE_ACTOR", "actor 不得为空")
    lock = load_frozen(ctx)
    other = ROLES[1] if role == ROLES[0] else ROLES[0]
    prev = ctx.exists(f"{SIG_DIR}/{other}.json")
    if prev:
        other_doc = ctx.read_json(f"{SIG_DIR}/{other}.json")
        if other_doc.get("actor") == actor:
            raise LockError("SIGNATURE_ACTOR_NOT_DISTINCT",
                            f"{role} 与 {other} 同一 actor={actor}")
        if other_doc.get("lock_sha256") != lock.sha256:
            raise LockError("SIGNATURE_STALE",
                            f"{other} 签在 {other_doc.get('lock_sha256')}，"
                            f"LOCK={lock.sha256}")
    doc = {"schema": SIG_SCHEMA, "schema_version": 1, "role": role,
           "actor": actor, "decision": decision, "rationale": rationale,
           "lock_sha256": lock.sha256, "signed_at_utc": (now or utc_now_iso_z)()}
    _write_locked(ctx.relative(f"{SIG_DIR}/{role}.json"),
                  (C.canonical_json(doc) + "\n").encode("utf-8"))
    return doc


def signatures(ctx: Any) -> "dict[str, dict[str, Any]]":
    out: "dict[str, dict[str, Any]]" = {}
    for role in ROLES:
        rel = f"{SIG_DIR}/{role}.json"
        if ctx.exists(rel):
            out[role] = ctx.read_json(rel)
    return out


def verify_lock(ctx: Any, *, repo_root: "Path | str | None" = None
                ) -> "dict[str, Any]":
    """全量复核：LOCK 自洽 + 闭包零漂移 + 依赖零漂移 + 双签同 SHA 互异。"""
    root = Path(repo_root) if repo_root is not None else REPO_ROOT
    lock = load_frozen(ctx)
    doc = ctx.read_json(LOCK_RELPATH)
    closure = doc.get("closure") or {}
    verify_closure(closure, root)
    if doc.get("closure_sha256") != closure.get("closure_sha256"):
        raise LockError("CLOSURE_DRIFT", "LOCK.closure_sha256 不符")
    if doc.get("image") != closure.get("image"):
        raise LockError("CLOSURE_DRIFT", "LOCK.image 与 closure.image 不符")
    want_deps = _dependencies(root)
    if dict(lock.dependencies) != want_deps:
        diff = sorted(set(lock.dependencies) ^ set(want_deps)) or [
            p for p in want_deps if lock.dependencies.get(p) != want_deps[p]]
        raise LockError("DEPENDENCY_DRIFT", f"{diff[:5]}")
    report_rel = (doc.get("calibration") or {}).get("path")
    if report_rel:
        got = _sha256_file(ctx.relative(report_rel))
        if got != (doc.get("calibration") or {}).get("sha256"):
            raise LockError("CALIBRATION_DRIFT", f"{report_rel} {got}")
    sigs = signatures(ctx)
    missing = [r for r in ROLES if r not in sigs]
    if missing:
        raise LockError("SIGNATURES_INCOMPLETE", f"缺 {missing}")
    for role in ROLES:
        sig = sigs[role]
        if sig.get("schema") != SIG_SCHEMA:
            raise LockError("SIGNATURE_SCHEMA", role)
        if sig.get("lock_sha256") != lock.sha256:
            raise LockError("SIGNATURE_STALE",
                            f"{role}→{sig.get('lock_sha256')} LOCK={lock.sha256}")
        if sig.get("decision") != "APPROVE":
            raise LockError("SIGNATURE_REJECTED", f"{role}={sig.get('decision')}")
    if sigs["reviewer"].get("actor") == sigs["verifier"].get("actor"):
        raise LockError("SIGNATURE_ACTOR_NOT_DISTINCT",
                        f"actor={sigs['reviewer'].get('actor')}")
    return {"ok": True, "lock_sha256": lock.sha256, "stage": lock.stage,
            "phase": lock.phase, "closure_sha256": closure.get("closure_sha256"),
            "signatures": {r: {"actor": sigs[r].get("actor"),
                               "decision": sigs[r].get("decision"),
                               "signed_at_utc": sigs[r].get("signed_at_utc")}
                           for r in ROLES}}


def require_frozen(ctx: Any, *, repo_root: "Path | str | None" = None
                   ) -> "tuple[L.LockDocument, dict[str, Any]]":
    """冻结门：任何一项不满足即 ``LockError``（装配层不吞异常）。"""
    result = verify_lock(ctx, repo_root=repo_root)
    return load_frozen(ctx), result


def closure_file_list(doc: Mapping[str, Any]) -> "list[str]":
    """闭包覆盖的全部 relpath（供证据包/审计枚举）。"""
    out: "list[str]" = []
    for group in sorted((doc.get("closure") or {}).get("groups") or {}):
        for f in doc["closure"]["groups"][group]["files"]:
            out.append(str(f["path"]))
    return out
