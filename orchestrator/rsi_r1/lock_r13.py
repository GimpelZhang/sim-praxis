"""R1.3 validation LOCK 与 T5→paid 单向绑定（轻量 typed 两件套）。

R1.2 的 validation LOCK 绑定了离线六套（suite spec、回归测试字节、消费者
源码等），这些是 R1.2 专有仪器面；R1.3 的 T5 用同一生产分派器 + 真实容器
跑两留出任务的维护参考链（见 ``r13_validation``），不重跑 R1.2 六套。
因此这里给 R1.3 一套**最小但可核**的 validation LOCK：

* 绑定：任务集/资源计划字节、T5 报告字节、R1.3 闭包、镜像 pin；
* 三签：executor/reviewer/verifier 各一份，**必须三名不同 actor**；
* paid LOCK 通过 ``validation_binding`` 单向引用本 LOCK 与 T5 报告
  （T5 报告不引用 paid LOCK，无环）；任何字节漂移即拒绝。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from orchestrator.rsi_r1 import lock as LK

VALIDATION_LOCK_REL = "contract/validation_lock.json"
VALIDATION_SHA_REL = "contract/validation_lock.sha256"
VALIDATION_SIGN_DIR = "contract/validation_signatures"
VALIDATION_T5_REL = "contract/t5_summary.json"
VALIDATION_ROLES = ("executor", "reviewer", "verifier")
LOCK_SCHEMA_V13 = "simpraxis.rsi_r13.validation_lock.v1"
BINDING_SCHEMA_V13 = "simpraxis.rsi_r13.validation_binding.v1"
SIG_SCHEMA = "simpraxis.rsi_r13.validation_signature.v1"


class Lock13Error(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"[{code}] {detail}" if detail else code)


def _sha_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sha_doc(doc: Any) -> str:
    return hashlib.sha256(json.dumps(
        doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")).hexdigest()


def build_validation_lock(ctx: Any, *, image_pin: str,
                          purpose_note: str = "") -> "dict[str, Any]":
    if ctx.exists(VALIDATION_LOCK_REL):
        raise Lock13Error("LOCK_EXISTS", "validation LOCK 已冻结")
    t5_path = ctx.relative(VALIDATION_T5_REL)
    if not t5_path.is_file():
        raise Lock13Error("T5_MISSING", VALIDATION_T5_REL)
    t5 = json.loads(t5_path.read_text(encoding="utf-8"))
    if not bool(t5.get("all_ok")):
        raise Lock13Error("T5_NOT_PASSED", "T5 报告 all_ok != true")
    taskset = ctx.relative("contract/taskset.json")
    resource = ctx.relative("contract/resource_plan.json")
    for p in (taskset, resource):
        if not p.is_file():
            raise Lock13Error("PREREG_MISSING", str(p))
    closure = LK.build_closure(LK.REPO_ROOT, image_pin=image_pin,
                               groups_key="r13")
    doc = {
        "schema": LOCK_SCHEMA_V13,
        "schema_version": 1,
        "envelope_id": ctx.envelope_id,
        "purpose": "validation",
        "image": closure["image"],
        "closure": closure,
        "closure_sha256": closure["closure_sha256"],
        "taskset_sha256": _sha_file(taskset),
        "resource_plan_sha256": _sha_file(resource),
        "t5_summary_sha256": _sha_file(t5_path),
        "t5_all_ok": True,
        "roles": list(VALIDATION_ROLES),
        "note": purpose_note,
    }
    lock_sha = _sha_doc(doc)
    from orchestrator.common.fsutil import atomic_write_text
    atomic_write_text(ctx.relative(VALIDATION_LOCK_REL),
                      json.dumps(doc, indent=1, sort_keys=True,
                                 ensure_ascii=False) + "\n", mode=0o600)
    atomic_write_text(ctx.relative(VALIDATION_SHA_REL), lock_sha + "\n",
                      mode=0o600)
    return {"doc": doc, "sha256": lock_sha}


def sign_validation(ctx: Any, *, role: str, actor: str, decision: str,
                    rationale: str) -> "dict[str, Any]":
    if role not in VALIDATION_ROLES:
        raise Lock13Error("ROLE_UNKNOWN", role)
    if decision not in ("APPROVE", "REJECT"):
        raise Lock13Error("DECISION_INVALID", decision)
    lock_sha = read_validation_sha(ctx)
    if any(ctx.exists(f"{VALIDATION_SIGN_DIR}/{r}.json") for r in VALIDATION_ROLES):
        # 允许逐角色顺序签；已有**其他**角色的 actor 必须不同（在 verify 里判）
        pass
    from orchestrator.common.clock import utc_now_iso_z
    doc = {"schema": SIG_SCHEMA, "schema_version": 1, "role": role,
           "actor": actor, "decision": decision, "rationale": rationale,
           "validation_lock_sha256": lock_sha,
           "signed_at_utc": utc_now_iso_z()}
    from orchestrator.common.fsutil import atomic_write_text
    ctx.relative(VALIDATION_SIGN_DIR).mkdir(parents=True, exist_ok=True,
                                            mode=0o700)
    atomic_write_text(ctx.relative(f"{VALIDATION_SIGN_DIR}/{role}.json"),
                      json.dumps(doc, indent=1, sort_keys=True,
                                 ensure_ascii=False) + "\n", mode=0o600)
    return doc


def read_validation_sha(ctx: Any) -> str:
    path = ctx.relative(VALIDATION_SHA_REL)
    if not path.is_file():
        raise Lock13Error("LOCK_MISSING", VALIDATION_SHA_REL)
    text = path.read_text(encoding="utf-8").strip()
    if len(text) != 64:
        raise Lock13Error("LOCK_TAMPERED", "validation sha 非 64 hex")
    doc = json.loads(ctx.relative(VALIDATION_LOCK_REL).read_text(encoding="utf-8"))
    if _sha_doc(doc) != text:
        raise Lock13Error("LOCK_TAMPERED", "validation LOCK 字节与 sha 不符")
    return text


def verify_validation(ctx: Any, *, repo_root: "Path | str | None" = None
                      ) -> "dict[str, Any]":
    lock_sha = read_validation_sha(ctx)
    doc = json.loads(ctx.relative(VALIDATION_LOCK_REL).read_text(encoding="utf-8"))
    if doc.get("schema") != LOCK_SCHEMA_V13:
        raise Lock13Error("SCHEMA", str(doc.get("schema")))
    if doc.get("envelope_id") != ctx.envelope_id:
        raise Lock13Error("ENVELOPE", "validation LOCK envelope 不符")
    if _sha_file(ctx.relative(VALIDATION_T5_REL)) != doc.get("t5_summary_sha256"):
        raise Lock13Error("T5_DRIFT", "T5 报告字节漂移")
    if _sha_file(ctx.relative("contract/taskset.json")) != doc.get(
            "taskset_sha256"):
        raise Lock13Error("TASKSET_DRIFT", "任务集漂移")
    if _sha_file(ctx.relative("contract/resource_plan.json")) != doc.get(
            "resource_plan_sha256"):
        raise Lock13Error("RESOURCE_DRIFT", "资源计划漂移")
    LK.verify_closure(doc["closure"], repo_root or LK.REPO_ROOT)
    if doc["closure"].get("closure_sha256") != doc.get("closure_sha256"):
        raise Lock13Error("CLOSURE_DRIFT", "闭包 sha 不符")
    actors: "dict[str, str]" = {}
    for role in VALIDATION_ROLES:
        path = ctx.relative(f"{VALIDATION_SIGN_DIR}/{role}.json")
        if not path.is_file():
            raise Lock13Error("SIGNATURE_MISSING", role)
        sig = json.loads(path.read_text(encoding="utf-8"))
        if sig.get("schema") != SIG_SCHEMA or sig.get("role") != role:
            raise Lock13Error("SIGNATURE_INVALID", role)
        if sig.get("decision") != "APPROVE":
            raise Lock13Error("SIGNATURE_REJECTED", role)
        if sig.get("validation_lock_sha256") != lock_sha:
            raise Lock13Error("SIGNATURE_STALE", role)
        if not str(sig.get("rationale") or "").strip():
            raise Lock13Error("SIGNATURE_REASON", role)
        if sig.get("actor") in actors.values():
            raise Lock13Error("SIGNATURE_ACTOR_REUSED", role)
        actors[role] = str(sig.get("actor"))
    return {"ok": True, "lock_sha256": lock_sha, "actors": actors}


def build_binding(validation_ctx: Any) -> "dict[str, Any]":
    verdict = verify_validation(validation_ctx)
    binding = {
        "schema": BINDING_SCHEMA_V13,
        "schema_version": 1,
        "validation_root": str(validation_ctx.root),
        "validation_lock_sha256": _sha_file(
            validation_ctx.relative(VALIDATION_LOCK_REL)),
        "validation_lock_doc_sha256": verdict["lock_sha256"],
        "t5_summary_sha256": _sha_file(
            validation_ctx.relative(VALIDATION_T5_REL)),
        "taskset_sha256": _sha_file(
            validation_ctx.relative("contract/taskset.json")),
        "resource_plan_sha256": _sha_file(
            validation_ctx.relative("contract/resource_plan.json")),
        "signatures": {role: _sha_file(validation_ctx.relative(
            f"{VALIDATION_SIGN_DIR}/{role}.json"))
            for role in VALIDATION_ROLES},
    }
    return binding


def verify_binding(binding: Mapping[str, Any]) -> None:
    if not isinstance(binding, Mapping) or binding.get("schema") != BINDING_SCHEMA_V13:
        raise Lock13Error("BINDING_INVALID", "binding schema")
    root = Path(str(binding.get("validation_root") or ""))
    if not root.is_dir():
        raise Lock13Error("BINDING_ROOT", str(root))
    checks = {
        "validation_lock.json": binding.get("validation_lock_sha256"),
        "t5_summary.json": binding.get("t5_summary_sha256"),
        "taskset.json": binding.get("taskset_sha256"),
        "resource_plan.json": binding.get("resource_plan_sha256"),
    }
    rel_dirs = {"validation_lock.json": "contract",
                "t5_summary.json": "contract",
                "taskset.json": "contract",
                "resource_plan.json": "contract"}
    for name, want in checks.items():
        path = root / rel_dirs[name] / name
        if not path.is_file() or _sha_file(path) != want:
            raise Lock13Error("BINDING_DRIFT", name)
    sigs = binding.get("signatures") or {}
    for role in VALIDATION_ROLES:
        path = root / VALIDATION_SIGN_DIR / f"{role}.json"
        if not path.is_file() or _sha_file(path) != sigs.get(role):
            raise Lock13Error("BINDING_SIGNATURE", role)
