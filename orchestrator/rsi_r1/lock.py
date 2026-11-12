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

import json
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
#: 验证 LOCK（``purpose=validation``，T5 离线生产演练）的三签角色：实现者
#: 之外的独立审查者与独立验收者，三人 actor 互异（见 ``sign_lock``）。
VALIDATION_ROLES = ("executor", "reviewer", "verifier")
VALIDATION_SIG_DIR = "contract/validation_signatures"
VALIDATION_REPORT_RELPATH = "contract/offline_validation_report.json"
VALIDATION_AUTH_RELPATH = "contract/offline_authorization.json"
#: RSI-R1.2 T6：付费 LOCK 对 T5 验证件的**单向绑定**（Plan L212/L247）。
#: 绑定只存在于付费 LOCK 文档里；T5 根不反向引用付费根（T5 不依赖尚不存在的
#: 付费 LOCK）。付费 LOCK 带该键时摘要覆盖整份文档（见 loop.LockDocument）。
PAID_BINDING_SCHEMA = "simpraxis.rsi_r1.paid_validation_binding.v1"
PURPOSES = ("paid", "validation")
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
               "orchestrator/rsi_r1/watchdog.py",
               "orchestrator/rsi_r1/offline_validation.py",
               "orchestrator/rsi_r1/offline_consumer.py",
               # 离线演练的夹具/驱动本身就是产生验证报告的仪器：报告被三签绑定，
               # 若这份驱动程序不在闭包里，改它就能让 LOCK 失效而无人察觉
               # （T5 实测踩到：模板转义坏掉时 LOCK 仍判零漂移）。
               "orchestrator/rsi_r1/offline_suite.py",
               # 最终请求/回包截断层决定**候选实际看到什么字节**（历史原稿截断、
               # 回包白名单）——与 gateway_transport 同属仪器面：报告按它给
               # 候选的证据面判定科学结论，改它而 LOCK 判零漂移就是仪器漏洞
               # （T5 实测：run_confirm 的 candidate_output 在白名单外）。
               "orchestrator/common/research_transport.py",
               # R1.2 三层科学闸的凭据面：receipt 的字段/有效性判定与输出规范化
               # （parse_output）直接决定判据拿到的数值与缺口码；它此前不在任何
               # 闭包组 = 改它不改 LOCK（T5 实测：生产者词汇错配在 build_receipt
               # 才炸，演练前无人拦下）。
               "orchestrator/rsi_r1/analysis_receipt.py",
               # 有界驱动器的恢复策略表（INVALIDATE_FROM、里程碑游标）决定
               # 「恢复轨迹」这一 T5 验收观测本身；改它而 LOCK 判零漂移，就等于
               # 换一条恢复轨迹却仍宣称用冻结的仪器跑过。
               "orchestrator/rsi_r1/recovery.py"),
    "judge": ("orchestrator/rsi_r1/judge.py",
              "orchestrator/rsi_r1/statistics.py"),
    "replay": ("orchestrator/rsi_r1/replay.py",),
    "checker": ("scripts/verification/check_rsi_r1_closure.py",),
}

# 锁内适配（2026-11-12 T5 独立评审 MAJOR-5）：R1.2 的闭包必须覆盖**决定证据面**
# 的全部仪器代码——配额执行（resources）、研究驱动（study）、复跑分析
# （replay_driver）、结案判轴（closeout）、CLI 入口（cli/cli_r12）、画像与帽
# （profiles）、attempt 生命周期（attempts）、校准（calibration）、预注册
# （prereg）、容器内科学服务（experiment_service/physics）、请求审计
# （public_request）、双审（review_rubric）、族账（family）。这些文件此前不在
# 任何闭包组，改它们而 LOCK 判零漂移，等于「续用冻结仪器」的声明可以被绕过。
#
# 旧 R1 根的历史闭包**保持原样**：`CLOSURE_GROUPS` 仍是 legacy 七组（旧 LOCK
# 逐字节可复核），R1.2 用独立表 + closure 内的 ``groups_key`` 声明口径。
CLOSURE_GROUPS_R12: "dict[str, tuple[str, ...]]" = {
    **CLOSURE_GROUPS,
    "instrument": ("orchestrator/rsi_r1/attempts.py",
                   "orchestrator/rsi_r1/calibration.py",
                   "orchestrator/rsi_r1/closeout.py",
                   "orchestrator/rsi_r1/experiment_service.py",
                   "orchestrator/rsi_r1/family.py",
                   "orchestrator/rsi_r1/physics.py",
                   "orchestrator/rsi_r1/prereg.py",
                   "orchestrator/rsi_r1/profiles.py",
                   "orchestrator/rsi_r1/public_request.py",
                   "orchestrator/rsi_r1/replay_driver.py",
                   "orchestrator/rsi_r1/resources.py",
                   "orchestrator/rsi_r1/review_rubric.py",
                   "orchestrator/rsi_r1/study.py"),
    "entry": ("orchestrator/rsi_r1/cli.py",
              "orchestrator/rsi_r1/cli_r12.py"),
    # 验证仪器自身：六套的判据、锁校验与报告面都在这里。不进闭包的话，
    # 「三签绑定冻结实现」只是绑定了一份不含判据代码的清单——演练跑完再改
    # offline_suite.py 也能通过 verify（消费者源码另有 consumer_sha256 绑定）。
    "offline": ("orchestrator/rsi_r1/offline_suite.py",
                "orchestrator/rsi_r1/offline_validation.py"),
}

CLOSURE_GROUPS_BY_KEY: "dict[str, dict[str, tuple[str, ...]]]" = {
    "legacy": CLOSURE_GROUPS,
    "r12": CLOSURE_GROUPS_R12,
}


def closure_groups(key: str = "legacy") -> "dict[str, tuple[str, ...]]":
    table = CLOSURE_GROUPS_BY_KEY.get(str(key))
    if table is None:
        raise LockError("CLOSURE_GROUPS_KEY", repr(key))
    return table

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


def build_closure(repo_root: Path, *, image_pin: str,
                  groups_key: str = "legacy") -> "dict[str, Any]":
    table = closure_groups(groups_key)
    groups: "dict[str, Any]" = {}
    for group in sorted(table):
        files: "list[dict[str, Any]]" = []
        for rel in table[group]:
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
    if str(groups_key) != "legacy":
        # legacy 闭包的字节口径不变（旧 LOCK 的 closure_sha256 仍可复算）；
        # 新口径必须把「用的是哪张组表」写进被哈希的 body 里，否则验方无从知道。
        body["groups_key"] = str(groups_key)
    body["closure_sha256"] = C.canonical_sha256(body)
    return body


def verify_closure(closure: Mapping[str, Any], repo_root: Path,
                   *, groups_key: "str | None" = None) -> None:
    if closure.get("schema") != CLOSURE_SCHEMA:
        raise LockError("CLOSURE_SCHEMA", repr(closure.get("schema")))
    declared = closure.get("groups_key")
    key = str(groups_key) if groups_key is not None else str(declared or "legacy")
    if declared is not None and str(declared) != key:
        raise LockError("CLOSURE_GROUPS_KEY",
                        f"closure.groups_key={declared!r} != {key!r}")
    if key == "legacy" and declared is not None:
        raise LockError("CLOSURE_GROUPS_KEY", "legacy 闭包不得声明 groups_key")
    table = closure_groups(key)
    groups = closure.get("groups") or {}
    if set(groups) != set(table):
        raise LockError("CLOSURE_GROUPS",
                        f"缺组 {sorted(set(table) - set(groups))} / "
                        f"多组 {sorted(set(groups) - set(table))}")
    for group in sorted(table):
        entry = groups[group]
        files = entry.get("files") or []
        want_paths = list(table[group])
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
    if declared is not None:
        body["groups_key"] = declared
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

# ---------------------------------------------------------------------------
# T6：付费 LOCK 的 T5 单向绑定
# ---------------------------------------------------------------------------

class _RootView:
    """锁库内部的**只读**根视图（核验 T5 根用；任何写一律拒绝）。

    只实现 ``verify_lock`` 实际消费的面。付费冻结绝不回头改 T5 根——T5 的
    三签绑定对象是它自己的报告，改一个字节就会让 T5 三签 stale。
    """

    readonly = True

    def __init__(self, root: "Path | str") -> None:
        self.root = Path(root)
        env_path = self.root / "contract/envelope.json"
        try:
            envelope = json.loads(env_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise LockError("VALIDATION_BINDING_MISSING",
                            f"T5 根缺 envelope：{env_path}") from None
        except ValueError as exc:
            raise LockError("VALIDATION_BINDING_MISSING",
                            f"T5 根 envelope 坏 JSON：{exc}") from None
        self.envelope_id = str(envelope.get("envelope_id") or "")

    def relative(self, relpath: "str | Path") -> Path:
        rel = Path(relpath)
        if rel.is_absolute() or ".." in rel.parts:
            raise LockError("VALIDATION_BINDING_PATH", f"只接受根内相对路径：{rel}")
        return self.root / rel

    def exists(self, relpath: "str | Path") -> bool:
        return self.relative(relpath).is_file()

    def read_json(self, relpath: "str | Path") -> Any:
        return json.loads(self.relative(relpath).read_text(encoding="utf-8"))

    def write_text(self, *_a: Any, **_k: Any) -> Path:
        raise LockError("VALIDATION_BINDING_READONLY",
                        "T5 根在付费绑定核验中只读")

    def write_json(self, *_a: Any, **_k: Any) -> Path:
        raise LockError("VALIDATION_BINDING_READONLY",
                        "T5 根在付费绑定核验中只读")


def _binding_signature_files(ctx: Any) -> "dict[str, dict[str, Any]]":
    out: "dict[str, dict[str, Any]]" = {}
    for role in VALIDATION_ROLES:
        rel = f"{VALIDATION_SIG_DIR}/{role}.json"
        if not ctx.exists(rel):
            raise LockError("VALIDATION_BINDING_SIGNATURES",
                            f"T5 根缺 {role} 签名")
        doc = ctx.read_json(rel)
        out[role] = {
            "actor": doc.get("actor"),
            "decision": doc.get("decision"),
            "signed_at_utc": doc.get("signed_at_utc"),
            "sha256": _sha256_file(ctx.relative(rel)),
        }
    return out


def build_validation_binding(validation_root: "Path | str", *,
                             now: "Callable[[], str] | None" = None
                             ) -> "dict[str, Any]":
    """核验 T5 根（闭包/依赖/报告/三签对**当前源码**）并生成长效绑定件。

    绑定是**单向**的：付费 LOCK 记下 T5 的 LOCK/报告/三签/实现身份，T5 根
    不因付费冻结改动一个字节。任何一步不过即 ``LockError``（不降级）。
    """
    from orchestrator.common import runtime_paths as _RP

    root = Path(validation_root)
    ctx = _RootView(root)
    if not ctx.exists(LOCK_RELPATH):
        raise LockError("VALIDATION_BINDING_MISSING",
                        f"{root} 不是已冻结的验证根（缺 {LOCK_RELPATH}）")
    t5_doc = ctx.read_json(LOCK_RELPATH)
    if lock_purpose(t5_doc) != "validation":
        raise LockError("VALIDATION_BINDING_PURPOSE",
                        "T5 根 LOCK 不是 purpose=validation")
    # 全量复核：闭包与依赖对**当前源码**重算、报告 verdict/矩阵重算、三签
    # 绑定逐项复核。源码在 T5 之后改过一个字节，这里就红。
    result = verify_lock(ctx, repo_root=REPO_ROOT)
    if not ctx.exists(VALIDATION_REPORT_RELPATH):
        raise LockError("VALIDATION_REPORT_MISSING", VALIDATION_REPORT_RELPATH)
    family = Path(_RP.rsi_r12_root()).resolve(strict=False)
    t5_root = root.resolve(strict=False)
    if t5_root == family or family not in t5_root.parents:
        raise LockError("VALIDATION_BINDING_ROOT",
                        f"T5 根 {t5_root} 不在 R1.2 家族根 {family} 之内")
    return {
        "schema": PAID_BINDING_SCHEMA,
        "schema_version": 1,
        "validation_root": t5_root.relative_to(family).as_posix(),
        "validation_envelope_id": ctx.envelope_id,
        "lock_sha256": result["lock_sha256"],
        "closure_sha256": result["closure_sha256"],
        "image_pin": str((t5_doc.get("image") or {}).get("pin") or ""),
        "report_relpath": VALIDATION_REPORT_RELPATH,
        "report_sha256": _sha256_file(ctx.relative(VALIDATION_REPORT_RELPATH)),
        "implementation_sha256":
            _signature_bindings(ctx, t5_doc)["implementation_sha256"],
        "profile_sha256":
            str((t5_doc.get("validation") or {}).get("profile_sha256") or ""),
        "signatures": _binding_signature_files(ctx),
        "bound_at_utc": (now or utc_now_iso_z)(),
    }


def verify_paid_validation_binding(ctx: Any, doc: Mapping[str, Any], *,
                                   repo_root: "Path | str | None" = None) -> None:
    """付费 LOCK 的 T5 绑定逐项复核（冻结后每次 ``verify``/准入都会走）。

    只信**现场重算**：T5 根重新全量验签、报告/签名文件重新算 sha，再与
    付费 LOCK 里冻结的绑定值逐键比对；任何一项不等即拒。
    """
    from orchestrator.common import runtime_paths as _RP

    binding = doc.get("validation_binding")
    if not isinstance(binding, Mapping) or not binding:
        raise LockError("PAID_BINDING_MISSING",
                        "付费 LOCK 缺 validation_binding（T6 冻结须绑定 T5 报告与三签）")
    if binding.get("schema") != PAID_BINDING_SCHEMA:
        raise LockError("PAID_BINDING_SCHEMA", repr(binding.get("schema")))
    family = Path(_RP.rsi_r12_root()).resolve(strict=False)
    rel_root = str(binding.get("validation_root") or "")
    t5_root = (family / rel_root).resolve(strict=False) if rel_root else family
    if not rel_root or t5_root == family or family not in t5_root.parents:
        raise LockError("PAID_BINDING_ROOT",
                        f"validation_root={rel_root!r} 不在家族根之内")
    if t5_root == Path(ctx.root).resolve(strict=False):
        raise LockError("PAID_BINDING_ROOT", "T5 根不得是付费根自身")
    t5 = _RootView(t5_root)
    if not t5.exists(LOCK_RELPATH):
        raise LockError("VALIDATION_BINDING_MISSING",
                        f"T5 根 {t5_root} 的 LOCK 不在场")
    t5_doc = t5.read_json(LOCK_RELPATH)
    if lock_purpose(t5_doc) != "validation":
        raise LockError("VALIDATION_BINDING_PURPOSE",
                        "绑定的 T5 LOCK 不是 purpose=validation")
    result = verify_lock(t5, repo_root=repo_root)
    expect = {
        "lock_sha256": result["lock_sha256"],
        "closure_sha256": result["closure_sha256"],
        "image_pin": str((t5_doc.get("image") or {}).get("pin") or ""),
        "report_sha256": _sha256_file(t5.relative(VALIDATION_REPORT_RELPATH)),
        "implementation_sha256":
            _signature_bindings(t5, t5_doc)["implementation_sha256"],
        "profile_sha256":
            str((t5_doc.get("validation") or {}).get("profile_sha256") or ""),
        "validation_envelope_id": t5.envelope_id,
    }
    for key, want in expect.items():
        if binding.get(key) != want:
            raise LockError("PAID_BINDING_DRIFT",
                            f"binding.{key}={binding.get(key)!r} ≠ 现场 {want!r}")
    if dict(binding.get("signatures") or {}) != _binding_signature_files(t5):
        raise LockError("PAID_BINDING_DRIFT", "binding.signatures 与 T5 现场签名不符")
    paid_image = str((doc.get("image") or {}).get("pin") or "")
    if paid_image != expect["image_pin"]:
        raise LockError("PAID_BINDING_IMAGE",
                        f"付费 LOCK 镜像 {paid_image!r} ≠ T5 冻结镜像 "
                        f"{expect['image_pin']!r}")
    if ctx.exists("contract/envelope.json"):
        paid_profile = str(ctx.read_json("contract/envelope.json")
                           .get("profile_sha256") or "")
        if paid_profile != expect["profile_sha256"]:
            raise LockError("PAID_BINDING_PROFILE",
                            f"付费根画像 {paid_profile!r} ≠ T5 画像 "
                            f"{expect['profile_sha256']!r}")


def build_calibration_import(validation_root: "Path | str", *,
                             report_path: "Path | str | None" = None,
                             now: "Callable[[], str] | None" = None
                             ) -> "tuple[dict[str, Any], bytes]":
    """核验 T5 根的 v2 校准件并生成**导入取证件**（Plan L212/AC55）。

    返回 ``(provenance, report_bytes)``。来源根必须是已三签的验证 LOCK 根，
    且报告必须是该 LOCK ``calibration`` 字段绑定的那一件——付费根只导入
    「同一源码/镜像/场景/校准 spec 哈希」下的**原字节**，不冒称自己重跑过。
    """
    binding = build_validation_binding(validation_root, now=now)
    root = Path(validation_root)
    ctx = _RootView(root)
    t5_doc = ctx.read_json(LOCK_RELPATH)
    cal = t5_doc.get("calibration") or {}
    rel = str(cal.get("path") or "")
    if not ctx.exists(rel):
        raise LockError("CALIBRATION_IMPORT_MISSING",
                        f"T5 LOCK 登记的校准报告 {rel!r} 不在场")
    if report_path is not None:
        want = ctx.relative(rel).resolve(strict=False)
        got = Path(report_path).resolve(strict=False)
        if got != want:
            raise LockError(
                "CALIBRATION_IMPORT_PATH",
                f"--import-verified 指向 {got}，而 T5 LOCK 绑定的是 {want}")
    sha = _sha256_file(ctx.relative(rel))
    if sha != cal.get("sha256"):
        raise LockError("CALIBRATION_IMPORT_DRIFT",
                        f"{rel} sha256={sha[:16]}… ≠ T5 LOCK {str(cal.get('sha256'))[:16]}…")
    provenance = {
        "schema": "simpraxis.rsi_r1.calibration_import.v1",
        "schema_version": 1,
        "validation_root": binding["validation_root"],
        "validation_envelope_id": binding["validation_envelope_id"],
        "validation_lock_sha256": binding["lock_sha256"],
        "closure_sha256": binding["closure_sha256"],
        "image_pin": binding["image_pin"],
        "profile_sha256": binding["profile_sha256"],
        "report_relpath": rel,
        "report_sha256": sha,
        "report_verdict": cal.get("verdict"),
        "imported_at_utc": (now or utc_now_iso_z)(),
        "note": ("离线校准件按同闭包/同镜像/同画像验签导入（未在付费根重跑）；"
                 "来源为已三签的验证 LOCK 根"),
    }
    return provenance, ctx.relative(rel).read_bytes()


def freeze_lock(ctx: Any, *, repo_root: "Path | str | None" = None,
                image_pin: "str | None" = None,
                purpose: "str | None" = None,
                validation_binding: "Mapping[str, Any] | None" = None,
                now: "Callable[[], str] | None" = None,
                ) -> "dict[str, Any]":
    """写不可变 LOCK（已存在即拒绝）。返回 ``{doc, sha256, lock}``。"""
    root = Path(repo_root) if repo_root is not None else REPO_ROOT
    if ctx.exists(LOCK_RELPATH) or ctx.exists(LOCK_SHA_RELPATH):
        raise LockError("LOCK_EXISTS", "LOCK 已冻结，不可重建（不可变）")
    envelope = ctx.read_json("contract/envelope.json")
    budget = ctx.read_json("contract/budget.json")
    envelope_id = C.require_safe_id(envelope.get("envelope_id"), where="envelope")
    budget_sha = C.budget_from_dict(budget).sha256
    if envelope.get("budget_sha256") != budget_sha:
        raise LockError("BUDGET_DRIFT",
                        f"envelope.budget_sha256={envelope.get('budget_sha256')!r} "
                        f"!= budget.sha256={budget_sha}")
    derived_purpose = ("validation"
                       if envelope.get("mode") == "offline_validation" else "paid")
    if purpose is not None and purpose != derived_purpose:
        raise LockError(
            "LOCK_PURPOSE",
            f"purpose={purpose!r} 与 envelope.mode 推导的 {derived_purpose!r} 不符")
    purpose = derived_purpose
    auth_doc: "dict[str, Any] | None" = None
    if purpose == "validation":
        if not ctx.exists(VALIDATION_AUTH_RELPATH):
            raise LockError("OFFLINE_AUTH_MISSING", VALIDATION_AUTH_RELPATH)
        auth_doc = ctx.read_json(VALIDATION_AUTH_RELPATH)
        if auth_doc.get("envelope_id") != envelope_id:
            raise LockError("OFFLINE_AUTH_STALE",
                            "离线授权件 envelope 与当前根不符")
    report_rel, report = _read_calibration(ctx)
    if not ctx.exists(INSTRUMENT_PREREG):
        raise LockError("PREREG_MISSING", INSTRUMENT_PREREG)
    regenerate_refusals(ctx)
    pin = image_pin or report.get("container_image")
    if not isinstance(pin, str) or not pin:
        raise LockError("IMAGE_PIN_INVALID", "校准报告未携带 container_image")
    # 组表口径取 profile 的 ``closure_groups_key``（单一真值，T0 已入画像）：
    # R1.2 = r12 扩展组表，旧 R1 = legacy 七组（历史 LOCK 的 closure_sha256
    # 逐字节可复算）。延迟导入 profiles，避免 cli↔lock 循环。
    from orchestrator.rsi_r1 import profiles as _P
    profile_id = str(envelope.get("profile_id") or _P.PROFILE_LEGACY)
    groups_key = _P.profile_by_id(profile_id).closure_groups_key
    closure = build_closure(root, image_pin=pin, groups_key=groups_key)
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
    if purpose == "validation":
        # 只在验证 LOCK 里出现三个键；付费 LOCK 文档逐字节与旧版相同（AC02）。
        doc["purpose"] = "validation"
        # MINOR-7：冻结前写下的注册件（夹具与预期）也进 LOCK——否则「先注册
        # 再跑」只是约定；报告与三签另绑同一 SHA（见 ``_signature_bindings``）。
        from orchestrator.rsi_r1 import offline_validation as _OV
        doc["offline_suite_spec"] = {
            "path": _OV.SPEC_RELPATH,
            "sha256": C.canonical_sha256(ctx.read_json(_OV.SPEC_RELPATH))}
        # 独立评审 R3-M1：失败矩阵的回归行由「真跑回归测试」证明，因此**测试文件
        # 字节**也必须是被冻结的东西——否则 freeze 后把测试改成 `assert True` 再取证，
        # 三行照样绿。这里逐件登记 sha256，签前与取证件、现场三方互核。
        # 独立评审 LOW-4：缺件时原实现抛的是 ``OfflineValidationError``，不属 LOCK
        # 语汇；冻面上归一为 ``VALIDATION_REGRESSION_MISSING``。
        try:
            doc["regression_tests"] = _OV.regression_tests_sha256(root)
        except _OV.OfflineValidationError as exc:
            raise LockError("VALIDATION_REGRESSION_MISSING",
                            f"回归测试文件缺失/不可读：{exc}") from None
        doc["validation"] = {
            "authorization_sha256": C.canonical_sha256(auth_doc or {}),
            "consumer_sha256": (auth_doc or {}).get("consumer", {}).get("sha256"),
            "profile_sha256": (auth_doc or {}).get("profile_sha256"),
            "roles": list(VALIDATION_ROLES),
            "report_relpath": VALIDATION_REPORT_RELPATH,
        }
    elif str(envelope.get("profile_id") or "") == _P.PROFILE_R12:
        # R1.2 付费 LOCK **必须**绑定 T5 验证件（Plan L247/L333）：不绑定的
        # 付费冻结等于绕开生产准入。冻结前按现场重算一次（T5 根全量验签 +
        # 报告 sha + 实现身份），通过后才写盘；旧 R1 付费根不经过这一支，
        # 逐字节行为不变。
        if not isinstance(validation_binding, Mapping) or not validation_binding:
            raise LockError(
                "PAID_BINDING_MISSING",
                "R1.2 付费冻结必须给 --validation-report（绑定 T5 报告与三签）")
        doc["validation_binding"] = dict(validation_binding)
        verify_paid_validation_binding(ctx, doc, repo_root=root)
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


def lock_purpose(doc: Mapping[str, Any]) -> str:
    purpose = doc.get("purpose", "paid")
    if purpose not in PURPOSES:
        raise LockError("LOCK_PURPOSE", repr(purpose))
    return purpose


def _signature_bindings(ctx: Any, doc: Mapping[str, Any]) -> "dict[str, str]":
    """验证 LOCK 签名的四项绑定值（付费 LOCK 为空 dict）。"""
    if lock_purpose(doc) != "validation":
        return {}
    from orchestrator.rsi_r1 import offline_validation as OV

    rel = VALIDATION_REPORT_RELPATH
    if not ctx.exists(rel):
        raise LockError("VALIDATION_REPORT_MISSING", rel)
    # 锁内适配（2026-11-12 T5 独立评审 MAJOR-2/3/4）：签名绑定不能只看报告字节
    # 哈希——报告本身必须先证明「六套全跑全 PASS、跑的是冻结画像/镜像、跑的是本
    # LOCK」。旧实现允许 `--only` 子集报告 verdict=PASS 并被三签，等于没跑全链也能
    # 结案；也让「profile hash 绑定」退化为抄写。以下任何一条不满足即拒签。
    report = ctx.read_json(rel)
    from orchestrator.rsi_r1 import offline_suite as _OS
    # 独立评审 R3-m5：报告必须属于**本根**、绑**本根授权件**——跨根/跨授权搬报告
    # 不能只靠 LOCK sha 间接挡（报告作者可是先复制一份改字段）。
    if str(report.get("envelope_id") or "") != ctx.envelope_id:
        raise LockError("VALIDATION_REPORT_ENVELOPE",
                        f"report.envelope_id={report.get('envelope_id')!r} "
                        f"≠ {ctx.envelope_id!r}")
    if str(report.get("authorization_sha256") or "") != C.canonical_sha256(
            OV.load_authorization(ctx)):
        raise LockError("VALIDATION_AUTHORIZATION_MISMATCH",
                        "报告绑定的授权件不是本根当前授权件")
    # 独立评审 MINOR：签名面必须校验报告**自述 schema**，否则任何 JSON 都能冒充。
    if report.get("schema") != OV.REPORT_SCHEMA:
        raise LockError("VALIDATION_REPORT_SCHEMA",
                        f"report.schema={report.get('schema')!r}")
    # 逐回合证据：n 必须正整数且与两份 sha 列表长度一致（空演练不得签署）。
    turns = report.get("turns") or {}
    n_turns = turns.get("n")
    if isinstance(n_turns, bool) or not isinstance(n_turns, int) or n_turns <= 0:
        raise LockError("VALIDATION_REPORT_INCOMPLETE",
                        f"turns.n={n_turns!r}")
    if not (len(turns.get("request_sha256") or []) == n_turns
            == len(turns.get("reply_sha256") or [])):
        raise LockError("VALIDATION_REPORT_INCOMPLETE",
                        "turns 数与 sha 列表长度不一致")
    # 独立评审 R3-m3：回合证据必须与盘上 append-only 日志逐条对账——报告里的
    # ``turns`` 是自述，写一份手搓报告也能凑出「跑过 n 回合」。
    disk_turns: "list[dict[str, Any]]" = []
    turn_path = ctx.relative(OV.TURN_LOG_RELPATH)
    if turn_path.is_file():
        disk_turns = [json.loads(line) for line in
                      turn_path.read_text(encoding="utf-8").splitlines()
                      if line.strip()]
    if (len(disk_turns) != n_turns
            or [t.get("request_sha256") for t in disk_turns]
            != list(turns.get("request_sha256") or [])
            or [t.get("reply_sha256") for t in disk_turns]
            != list(turns.get("reply_sha256") or [])):
        raise LockError("VALIDATION_TURNS_DRIFT",
                        f"报告 turns({n_turns}) 与盘上回合日志"
                        f"({len(disk_turns)}) 不一致")
    required = list(_OS.SUITE_ORDER)
    if not required:
        raise LockError("VALIDATION_SUITE_SPEC", "六套清单为空")
    if report.get("verdict") != "PASS" or report.get("complete") is not True:
        raise LockError("VALIDATION_REPORT_INCOMPLETE",
                        f"verdict={report.get('verdict')!r} "
                        f"complete={report.get('complete')!r}")
    if set(report.get("suites_required") or []) != set(required):
        raise LockError("VALIDATION_SUITE_SPEC",
                        f"报告声明的必跑套件 {report.get('suites_required')!r}")
    if set(report.get("suites_run") or []) != set(required):
        raise LockError("VALIDATION_REPORT_INCOMPLETE",
                        f"suites_run={report.get('suites_run')!r} 不等于六套")
    for suite in required:
        status = ((report.get("suites") or {}).get(suite) or {}).get("status")
        if status != "PASS":
            raise LockError("VALIDATION_REPORT_INCOMPLETE",
                            f"{suite} status={status!r}")
    frozen_profile = str((doc.get("validation") or {}).get("profile_sha256") or "")
    if str(report.get("profile_sha256") or "") != frozen_profile:
        raise LockError("VALIDATION_PROFILE_MISMATCH",
                        f"报告画像 {report.get('profile_sha256')!r} ≠ "
                        f"LOCK {frozen_profile!r}")
    frozen_image = str((doc.get("image") or {}).get("pin") or "")
    if str((report.get("image") or {}).get("pin") or "") != frozen_image:
        raise LockError("VALIDATION_IMAGE_MISMATCH",
                        f"报告镜像 {report.get('image')!r} ≠ LOCK {frozen_image!r}")
    if str(report.get("validation_lock_sha256") or "") != read_lock_sha(ctx):
        raise LockError("VALIDATION_LOCK_MISMATCH",
                        "报告绑定的 LOCK 不是本 LOCK")
    # MINOR-3：零 POST 是报告**自证**字段（生成时实测账本），签前必须为 0。
    # 注意 `x or -1` 会把实测的 0 变成 -1——这里显式判类型，缺键/None/bool 一律拒。
    _rows = report.get("ledger_call_rows")
    if isinstance(_rows, bool) or not isinstance(_rows, int) or _rows != 0:
        raise LockError("VALIDATION_REPORT_INCOMPLETE",
                        f"ledger_call_rows={_rows!r}")
    # MAJOR-7：固定失败矩阵必须逐行有覆盖证据，且必须由**纯函数在签前重算**
    # 与报告声明逐行相符——报告里的 rows 只能是 recompute 的结果，不能是手写。
    matrix = report.get("failure_matrix") or {}
    if matrix.get("complete") is not True:
        raise LockError("VALIDATION_REPORT_INCOMPLETE",
                        "failure_matrix 不完整："
                        f"{matrix.get('missing')!r}")
    # R3-M1：回归行须由绑**冻结测试字节**的真跑取证件证明——三方核对：LOCK 冻结值
    # == 取证件自述 == 现场文件当前字节（freeze 后改测试 ⇒ 这里当场红）。
    frozen_tests = doc.get("regression_tests")
    if not isinstance(frozen_tests, Mapping) or not frozen_tests:
        raise LockError("VALIDATION_REGRESSION_UNFROZEN",
                        "验证 LOCK 未登记 regression_tests（旧根需重冻）")
    # 独立评审 LOW-4：取证件坏 JSON 时原实现让 ``ValueError`` 直接串出签名流程，
    # 不在 LOCK 错误语汇里；这里归一（缺件分支仍在下面判 Mapping）。
    try:
        evidence = OV.load_regression_evidence(ctx)
    except (ValueError, OV.OfflineValidationError) as exc:
        raise LockError("VALIDATION_REGRESSION_MISSING",
                        f"取证件不可读：{exc}") from None
    if not isinstance(evidence, Mapping):
        raise LockError("VALIDATION_REGRESSION_MISSING",
                        "缺少 regression-attest 取证件")
    if dict(evidence.get("test_files") or {}) != dict(frozen_tests):
        raise LockError("VALIDATION_REGRESSION_TESTS_DRIFT",
                        "取证件绑定的测试字节 ≠ LOCK 冻结值")
    # R4-m2：现场测试文件缺失时 ``regression_tests_sha256`` 抛的是
    # ``OfflineValidationError``——签面必须落本域错误码，否则调用方会看到
    # 一个不属于 LOCK 语汇的异常串出签名流程。
    try:
        live_tests = OV.regression_tests_sha256(REPO_ROOT)
    except OV.OfflineValidationError as exc:
        raise LockError("VALIDATION_REGRESSION_MISSING",
                        f"回归测试文件缺失：{exc}") from None
    if live_tests != dict(frozen_tests):
        raise LockError("VALIDATION_REGRESSION_TESTS_DRIFT",
                        "现场测试文件已改动（≠ LOCK 冻结值）")
    recomputed = _OS.failure_matrix(
        report.get("suites") or {},
        regression_evidence=evidence,
        closure_sha256=str(((doc.get("closure") or {}).get("closure_sha256")) or ""),
        regression_tests=dict(frozen_tests))
    if recomputed.get("complete") is not True:
        raise LockError("VALIDATION_REPORT_INCOMPLETE",
                        f"矩阵重算不完整：{recomputed.get('missing')!r}")

    def _proj(rows: Any) -> "list[tuple[Any, Any, Any]]":
        return [(r.get("row"), r.get("ok"), r.get("covered_by"))
                for r in (rows or [])]

    if _proj(matrix.get("rows")) != _proj(recomputed.get("rows")):
        raise LockError("VALIDATION_MATRIX_DRIFT",
                        "报告 failure_matrix.rows 与签前重算不一致")
    # MINOR-7：报告必须绑定**当前**注册件（夹具与预期）字节，换注册件即拒签。
    spec = ctx.read_json(OV.SPEC_RELPATH)
    if str(report.get("suite_spec_sha256") or "") != C.canonical_sha256(spec):
        raise LockError("VALIDATION_SUITE_SPEC",
                        "报告未绑定当前离线注册件（suite_spec_sha256 不符）")
    consumer = str((doc.get("validation") or {}).get("consumer_sha256") or "")
    return {
        "report_sha256": _sha256_file(ctx.relative(rel)),
        "implementation_sha256": OV.implementation_sha256(
            doc, consumer_sha256_hex=consumer),
        "profile_sha256": str((doc.get("validation") or {}).get("profile_sha256")),
        # R3-M1 补强：回归取证件字节本身也进签名绑定——签后换证（哪怕 test_files
        # 与冻结值相同、只把 counts 改绿）会与三签记录失配（SIGNATURE_BINDING）。
        "regression_evidence_sha256": _sha256_file(
            ctx.relative(OV.REGRESSION_EVIDENCE_RELPATH)),
    }


def sign_lock(ctx: Any, *, role: str, actor: str, decision: str,
              rationale: str = "", now: "Callable[[], str] | None" = None
              ) -> "dict[str, Any]":
    doc_raw = ctx.read_json(LOCK_RELPATH) if ctx.exists(LOCK_RELPATH) else {}
    purpose = lock_purpose(doc_raw)
    roles = VALIDATION_ROLES if purpose == "validation" else ROLES
    sig_dir = VALIDATION_SIG_DIR if purpose == "validation" else SIG_DIR
    if role not in roles:
        raise LockError("SIGNATURE_ROLE", f"{purpose}: {role!r}")
    if decision not in DECISIONS:
        raise LockError("SIGNATURE_DECISION", repr(decision))
    if not isinstance(actor, str) or not actor.strip():
        raise LockError("SIGNATURE_ACTOR", "actor 不得为空")
    lock = load_frozen(ctx)
    mine = [r for r in roles if r != role]
    actors = {}
    for other in mine:
        rel = f"{sig_dir}/{other}.json"
        if not ctx.exists(rel):
            continue
        other_doc = ctx.read_json(rel)
        actors[other] = other_doc.get("actor")
        if other_doc.get("actor") == actor:
            raise LockError("SIGNATURE_ACTOR_NOT_DISTINCT",
                            f"{role} 与 {other} 同一 actor={actor}")
        if other_doc.get("lock_sha256") != lock.sha256:
            raise LockError("SIGNATURE_STALE",
                            f"{other} 签在 {other_doc.get('lock_sha256')}，"
                            f"LOCK={lock.sha256}")
    doc = {"schema": SIG_SCHEMA, "schema_version": 1, "role": role,
           "actor": actor, "decision": decision, "rationale": rationale,
           "lock_sha256": lock.sha256,
           "signed_at_utc": (now or utc_now_iso_z)()}
    if purpose == "validation":
        # 一次性绑定：报告/实现/画像。之后任何实现或报告改动都会让签名 stale。
        doc.update(_signature_bindings(ctx, doc_raw))
    _write_locked(ctx.relative(f"{sig_dir}/{role}.json"),
                  (C.canonical_json(doc) + "\n").encode("utf-8"))
    return doc


def signatures(ctx: Any, *, purpose: str = "paid") -> "dict[str, dict[str, Any]]":
    roles, sig_dir = ((VALIDATION_ROLES, VALIDATION_SIG_DIR)
                      if purpose == "validation" else (ROLES, SIG_DIR))
    out: "dict[str, dict[str, Any]]" = {}
    for role in roles:
        rel = f"{sig_dir}/{role}.json"
        if ctx.exists(rel):
            out[role] = ctx.read_json(rel)
    return out


def verify_lock(ctx: Any, *, repo_root: "Path | str | None" = None,
                require_signatures: bool = True) -> "dict[str, Any]":
    """全量复核：LOCK 自洽 + 闭包零漂移 + 依赖零漂移 + 双签同 SHA 互异。

    ``require_signatures=False`` 只给**离线验证 LOCK 的引导期**用：验证报告正是
    三签绑定对象之一（``_signature_bindings``），报告未生成前签名必然缺失；
    此时仍全量校验闭包/依赖/校准零漂移，签名与绑定由 ``verify`` 终局门强制
    （见 ``require_frozen`` 的同名例外与报告里的 ``signatures_required`` 字段）。
    """
    root = Path(repo_root) if repo_root is not None else REPO_ROOT
    if not ctx.exists(LOCK_RELPATH):
        raise LockError("LOCK_MISSING", LOCK_RELPATH)
    doc = ctx.read_json(LOCK_RELPATH)
    purpose = lock_purpose(doc)
    if not require_signatures and purpose != "validation":
        # 引导期免签只属于验证 LOCK；付费 LOCK 在解析前就拒，绝不进入此口径。
        raise LockError("SIGNATURES_SKIPPED",
                        "只有验证 LOCK 允许引导期免签")
    lock = load_frozen(ctx)
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
    # MINOR-4：账本预算件冻结后必须零漂移（旧实现只在 freeze 与装配入口查，
    # ``verify`` 不复算 ⇒ 改 contract/budget.json 后 verify 仍 ok）。
    live_budget = C.budget_from_dict(ctx.read_json("contract/budget.json")).sha256
    if live_budget != doc.get("budget_sha256"):
        raise LockError("BUDGET_DRIFT",
                        f"budget.sha256 {live_budget[:16]}… ≠ "
                        f"LOCK {str(doc.get('budget_sha256'))[:16]}…")
    # MINOR-7：注册件（夹具与预期）冻结后同样零漂移。
    if purpose == "validation":
        spec_rel = (doc.get("offline_suite_spec") or {}).get("path")
        if not spec_rel:
            raise LockError("SUITE_SPEC_MISSING", "验证 LOCK 未登记注册件")
        live_spec = C.canonical_sha256(ctx.read_json(spec_rel))
        if live_spec != (doc.get("offline_suite_spec") or {}).get("sha256"):
            raise LockError("SUITE_SPEC_DRIFT",
                            f"{spec_rel} {live_spec[:16]}… 已漂移")
    if not require_signatures:
        # 锁内适配（Plan T5 + T9 引导期）：验证 LOCK 的签名绑定对象之一是
        # 离线验证报告本身，报告须先由生产链路跑出来；此期间仍全量校验闭包/
        # 依赖/校准零漂移，只是不要求三签。付费 LOCK 在上面的 purpose 闸已拒。
        return {"ok": True, "lock_sha256": lock.sha256, "stage": lock.stage,
                "phase": lock.phase, "purpose": purpose,
                "closure_sha256": closure.get("closure_sha256"),
                "signatures_required": False, "signatures": {}}
    roles = VALIDATION_ROLES if purpose == "validation" else ROLES
    sigs = signatures(ctx, purpose=purpose)
    missing = [r for r in roles if r not in sigs]
    if missing:
        raise LockError("SIGNATURES_INCOMPLETE", f"缺 {missing}")
    for role in roles:
        sig = sigs[role]
        if sig.get("schema") != SIG_SCHEMA:
            raise LockError("SIGNATURE_SCHEMA", role)
        # 角色名必须与文件名一致：把 reviewer 的签名复制成 verifier.json 不得蒙混。
        if sig.get("role") != role:
            raise LockError("SIGNATURE_ROLE",
                            f"{role}.json 自称 role={sig.get('role')!r}")
        if sig.get("lock_sha256") != lock.sha256:
            raise LockError("SIGNATURE_STALE",
                            f"{role}→{sig.get('lock_sha256')} LOCK={lock.sha256}")
        if sig.get("decision") != "APPROVE":
            raise LockError("SIGNATURE_REJECTED", f"{role}={sig.get('decision')}")
    if len({sigs[r].get("actor") for r in roles}) != len(roles):
        raise LockError("SIGNATURE_ACTOR_NOT_DISTINCT",
                        f"actors={[sigs[r].get('actor') for r in roles]}")
    if purpose == "validation":
        want = _signature_bindings(ctx, doc)
        for role in roles:
            for key, value in want.items():
                if sigs[role].get(key) != value:
                    raise LockError(
                        "SIGNATURE_BINDING",
                        f"{role}.{key} 与当前实现/报告不符（实现改了必须重签）")
    else:
        # T6：R1.2 付费 LOCK 必须携带 T5 绑定；未带绑定的 R1.2 付费 LOCK 按
        # 「缺生产准入」拒（旧 R1 付费 LOCK 无 profile 键，不走这一支）。
        is_r12 = False
        if ctx.exists("contract/envelope.json"):
            from orchestrator.rsi_r1 import profiles as _P2
            try:
                is_r12 = (str(ctx.read_json("contract/envelope.json")
                              .get("profile_id") or "") == _P2.PROFILE_R12)
            except (OSError, ValueError):   # 坏 envelope 交给下游 fail-closed
                is_r12 = False
        if is_r12 or "validation_binding" in doc:
            verify_paid_validation_binding(ctx, doc, repo_root=root)
    return {"ok": True, "lock_sha256": lock.sha256, "stage": lock.stage,
            "phase": lock.phase, "purpose": purpose,
            "closure_sha256": closure.get("closure_sha256"),
            "signatures_required": True,
            "signatures": {r: {"actor": sigs[r].get("actor"),
                               "decision": sigs[r].get("decision"),
                               "signed_at_utc": sigs[r].get("signed_at_utc")}
                           for r in roles}}


def require_frozen(ctx: Any, *, repo_root: "Path | str | None" = None
                   ) -> "tuple[L.LockDocument, dict[str, Any]]":
    """冻结门：任何一项不满足即 ``LockError``（装配层不吞异常）。

    唯一例外是**离线验证引导期**：envelope 为 ``offline_validation`` 时三签的
    绑定对象（验证报告）尚未生成，此时门只到「闭包/依赖/校准零漂移」；终局
    三签与绑定由 ``verify`` 子命令（``verify_lock`` 默认口径）强制，报告内
    ``signatures_required`` 字段如实记录本门当时的口径。
    """
    mode = ""
    if ctx.exists("contract/envelope.json"):
        try:
            mode = str(ctx.read_json("contract/envelope.json").get("mode") or "")
        except (OSError, ValueError):    # 坏 envelope 交给下游 fail-closed
            mode = ""
    result = verify_lock(ctx, repo_root=repo_root,
                         require_signatures=(mode != "offline_validation"))
    return load_frozen(ctx), result


def closure_file_list(doc: Mapping[str, Any]) -> "list[str]":
    """闭包覆盖的全部 relpath（供证据包/审计枚举）。"""
    out: "list[str]" = []
    for group in sorted((doc.get("closure") or {}).get("groups") or {}):
        for f in doc["closure"]["groups"][group]["files"]:
            out.append(str(f["path"]))
    return out
