"""RSI-R1.2 T5：离线生产准入、公开消费者运输与验证 LOCK 支撑（Plan §4）。

三条纪律的代码落点：

1. **正式管线，不另造捷径**：离线跑走同一 ``cli``/准入/装配/状态/dispatcher/
   ``DockerRunner``/真 MuJoCo/分析沙盒/replay/closeout；只有外部模型 transport
   换成 :class:`OfflineConsumerTransport`（独立进程、stdin/stdout、无宿主 import）。
2. **``mode=offline_validation`` 是一等模式**：envelope 声明离线 ⇒
   ``SpendContract`` 用 ``MODE_OFFLINE_VALIDATION``（拒绝 consent、账本恒零实呼），
   并且此模式**不能**构造生产 gateway（``GT.build_gateway`` 不在此路径上）。
3. **消费者只吃公开事实**：每次调用把正式最终 request 原样写进 stdin；子进程
   ``python -I``、临时 cwd、环境只留 ``PATH``；逐回合证据（request/reply sha、
   pid、rc、耗时、cwd、env 键、canary 扫描）追加到
   ``contract/offline_validation/turns.jsonl``，任何 canary 出现即拒绝。

验证 LOCK（``lock.py`` 的 ``purpose=validation``）与付费 LOCK 共用闭包/依赖/
镜像钉死机制，但签名角色是 executor/reviewer/verifier 三人，且签名同时绑定
``validation_lock_sha256``、``report_sha256``、``implementation_sha256``、
``profile_sha256``——任何实现改动或报告重写都会让签名失效（单向，无哈希环）。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from orchestrator.common.clock import utc_now_iso_z
from orchestrator.common import research_transport as T
from orchestrator.rsi_r1 import contracts as C
from orchestrator.rsi_r1 import loop as L
from orchestrator.rsi_r1 import profiles as P
from orchestrator.rsi_r1 import state as S


AUTH_RELPATH = "contract/offline_authorization.json"
AUTH_SCHEMA = "simpraxis.rsi_r1.offline_authorization.v1"
SPEC_RELPATH = "contract/offline_suite_spec.json"
SPEC_SCHEMA = "simpraxis.rsi_r1.offline_suite_spec.v1"
REPORT_RELPATH = "contract/offline_validation_report.json"
REPORT_SCHEMA = "simpraxis.rsi_r1.offline_validation_report.v1"
TURN_LOG_RELPATH = "contract/offline_validation/turns.jsonl"
TURN_SCHEMA = "simpraxis.rsi_r1.offline_turn.v1"

CONSUMER_RELPATH = "orchestrator/rsi_r1/offline_consumer.py"

#: 消费者源码里出现即拒绝的宿主依赖（静态审：不得 import 宿主、不得起子进程/网络）。
FORBIDDEN_SOURCE_TOKENS = (
    "orchestrator",
    "import socket",
    "import subprocess",
    "from subprocess",
    "urllib",
    "http.client",
    "requests",
    "os.environ",
    "expanduser",
    "SIMPRAXIS",
)

#: MINOR-5 的三条源码级判据（见 ``verify_consumer_source``）。
_ABS_PATH_LITERAL = re.compile(r"""(["'])(?:~?/[^"'\n]*)\1""")
_DYNAMIC_OPEN = re.compile(r"""\bopen\(\s*(?!["'])[^)\s]""")
_DYNAMIC_EXEC_TOKENS = ("__import__", "importlib", "eval(", "exec(", "compile(")

#: 固定套登记（Plan §4 六套；**先注册预期，再跑**）。``suite`` 编号与 Plan 表一致。
SUITE_SPEC: "Mapping[str, Any]" = {
    "suite1_single_chains": {
        "what": "probe/practice 各单链 + A/B 两机制完整链（32 block 确认 + 物理 replay + closeout）",
        "expected": "PASS",
        "posts": 0,
    },
    "suite2_over_cap_self_repair": {
        "what": "同 attempt 先交 65/96 超帽矩阵被拒且零物理 run，再重写合法设计完成；"
                "8<decisions<=12/16 仍 PASS，first_pass=false",
        "expected": "PASS_WITH_RECOVERY",
        "posts": 0,
    },
    "suite3_boundaries": {
        "what": "63/64/65、累计 40+25>64、沙盒 32、confirm 余量、未知引用、重复 cell；"
                "8 batch 上限、第 9 批拒绝",
        "expected": "BOUNDARY_MATRIX_PASS",
        "posts": 0,
    },
    "suite4_transport_faults": {
        "what": "空回复/网关异常/未知发送/非 GatewayError/SIGTERM/SIGKILL/未知 intent/重发布",
        "expected": "EACH_HAS_REPORT_NO_DOUBLE_EXEC",
        "posts": 0,
    },
    "suite5_state_isolation": {
        "what": "旧 pause/旧 heartbeat/正常退出/真实死亡/跨 study 污染/重复 submit/"
                "未分析提交",
        "expected": "REJECT_OR_ISOLATE",
        "posts": 0,
    },
    "suite6_human_and_pack": {
        "what": "HumanEvent begin/end/interrupt/assisted/UNKNOWN + 最终 pack 与 "
                "attempt/ledger/compute 独立对账",
        "expected": "UNKNOWN_PRESERVED_AND_RECONCILED",
        "posts": 0,
    },
}

#: aux 子信封登记（与 ``P.AUX_SUBENVELOPES`` 同名；执行前先占额）。
AUX_PLAN: "Mapping[str, tuple[str, int]]" = {
    # 锁内适配（2026-11-12 T5 实测）：A 的 dt 由宿主固定 0.0005 ⇒ 每 run 8000
    # step，Plan L148 的 2000-step 假设与冻结仪器不符；子信封随实测成本上抬，
    # 见 ``profiles.AUX_SUBENVELOPES`` 的同一段注释（第二笔：toy 链三次确认
    # 2,448,000，因为它必须满足冻结的 32 block 确认形状）。
    "suite1_ab_confirm_replay": ("ab_instrument_confirm_replay", 2_160_000),
    "suite1_offline_probe_practice": ("offline_probe_practice_recovery", 2_448_000),
    "suite2_3_explore_boundary": ("ab_explore_boundary_fault", 240_000),
}


class OfflineValidationError(RuntimeError):
    """离线准入/消费者/证据失败（``code`` 供报告与测试引用）。"""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = str(code)
        self.detail = str(detail)
        super().__init__(f"[{self.code}] {self.detail}")


# ---------------------------------------------------------------------------
# 消费者身份与静态审
# ---------------------------------------------------------------------------

#: 引擎仓根（与 ``lock.REPO_ROOT`` 同一语义，但避免 import 环）。
REPO_ROOT = Path(__file__).resolve().parents[2]


def consumer_source(repo_root: "Path | str | None" = None) -> Path:
    root = Path(repo_root) if repo_root is not None else REPO_ROOT
    path = root / CONSUMER_RELPATH
    if not path.is_file():
        raise OfflineValidationError("CONSUMER_MISSING", str(path))
    return path


def consumer_sha256(path: "Path | str") -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def ledger_call_rows(ctx: Any, ledger_path: "Path | str | None", *,
                     require_exists: bool = False) -> int:
    """账本 ``event=="call"`` 行数（公开别名；报告与机判同一读法）。

    ``require_exists=True``（演练/报告面）时账本缺席 ⇒ ``LEDGER_MISSING``，
    杜绝「删掉账本 ⇒ 0 行 ⇒ 零 POST 真空成立」。
    """
    return _paid_rows(ctx, ledger_path, require_exists=require_exists)


def verify_consumer_source(path: "Path | str") -> "dict[str, Any]":
    """静态审：无宿主 import / 无子进程 / 无网络 / 无环境探测 / 无绝对路径。

    锁内适配（2026-11-12 T5 独立评审 MINOR-5）：仅靠 ``python -I`` + 临时 cwd
    + 子串禁用表挡不住**拼接出来的绝对路径**（``Path("/etc")/"passwd"``）与
    ``open(<变量>)``。这里再加三条源码级判据：

    ① 任何字符串字面量不得以 ``/`` 或 ``~/`` 开头（禁硬编码宿主绝对路径）；
    ② ``open(...)`` 的第一实参必须是字面量（禁动态打开任意路径）；
    ③ 禁 ``__import__``/``importlib``/``eval``/``exec``/``compile`` 动态执行。

    残余面（``sys.argv`` 交给模板的路径、Path 运算）明示为**人审 + 沙盒视图目录**
    兜底，不声称已被静态审完全封闭；报告里以
    ``consumer_isolation=static_audit_plus_sandbox`` 如实登记。
    """
    text = Path(path).read_text(encoding="utf-8")
    hits = sorted({tok for tok in FORBIDDEN_SOURCE_TOKENS if tok in text})
    if hits:
        raise OfflineValidationError(
            "CONSUMER_HOST_DEPENDENCY", f"消费者源码含禁用记号：{hits}")
    abs_literals = sorted(set(_ABS_PATH_LITERAL.findall(text)))
    if abs_literals:
        raise OfflineValidationError(
            "CONSUMER_ABSOLUTE_PATH",
            f"消费者源码含绝对路径字面量：{abs_literals[:4]}")
    dyn_opens = sorted(set(_DYNAMIC_OPEN.findall(text)))
    if dyn_opens:
        raise OfflineValidationError(
            "CONSUMER_DYNAMIC_OPEN",
            f"消费者 open() 实参非字面量：{dyn_opens[:4]}")
    dyn_tokens = sorted({tok for tok in _DYNAMIC_EXEC_TOKENS if tok in text})
    if dyn_tokens:
        raise OfflineValidationError(
            "CONSUMER_DYNAMIC_EXEC", f"消费者源码含动态执行记号：{dyn_tokens}")
    return {"path": str(path), "sha256": hashlib.sha256(
        Path(path).read_bytes()).hexdigest(), "n_bytes": len(text.encode("utf-8")),
        "isolation": "static_audit_plus_sandbox",
        "checks": ["forbidden_tokens", "absolute_path_literals",
                   "dynamic_open", "dynamic_exec"]}


# ---------------------------------------------------------------------------
# 授权（registered → offline_authorized）
# ---------------------------------------------------------------------------

def _load_envelope(ctx: Any) -> "dict[str, Any]":
    doc = ctx.read_json("contract/envelope.json")
    doc = C.validate_envelope_v2(doc, envelope_id=ctx.envelope_id)
    return doc


def _paid_rows(ctx: Any, ledger_path: "Path | str | None", *,
               require_exists: bool = False) -> int:
    """账本实呼行数。``require_exists=True`` 时**账本缺席即拒**。

    锁内适配（2026-11-12 T5 独立评审 MAJOR：零 POST 的最强证据源不能在文件被删时
    「真空成立」）。授权期账本可能尚未建（paid 根）故默认宽松；演练/报告/结案
    三处一律 ``require_exists=True``。
    """
    if ledger_path is None:
        raise OfflineValidationError("LEDGER_REQUIRED", "必须显式给 ledger_path")
    path = Path(ledger_path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        if require_exists:
            raise OfflineValidationError(
                "LEDGER_MISSING", f"账本不存在：{path}（零 POST 证据不得缺席）"
            ) from None
        return 0
    except OSError as exc:
        raise OfflineValidationError("LEDGER_UNREADABLE", str(exc)) from None
    n = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            raise OfflineValidationError(
                "LEDGER_CORRUPT", "账本含非 JSON 行") from None
        if row.get("event", "call") == "call":
            n += 1
    return n


def authorize_offline(ctx: Any, *, repo_root: "Path | str | None" = None,
                      ledger_path: "Path | str | None" = None,
                      consumer_path: "Path | str | None" = None,
                      now: Callable[[], str] = utc_now_iso_z) -> "dict[str, Any]":
    """写不可变离线授权件，并把 root 从 INIT 推到 AUTHORIZED。

    fail-closed 前置：envelope 必须声明 ``offline_validation``、profile 必须是
    rsi-r1.2、账本零实呼、消费者源码通过静态审。
    """
    env_doc = _load_envelope(ctx)
    if env_doc.get("mode") != "offline_validation":
        raise OfflineValidationError(
            "OFFLINE_MODE_REQUIRED",
            f"envelope.mode={env_doc.get('mode')!r} 不是 offline_validation")
    profile = P.profile_by_id(P.PROFILE_R12)
    consumer = Path(consumer_path) if consumer_path is not None else \
        consumer_source(repo_root)
    info = verify_consumer_source(consumer)
    if ledger_path is None:
        raise OfflineValidationError("LEDGER_REQUIRED", "authorize-offline 需 --ledger")
    calls = _paid_rows(ctx, ledger_path)
    if calls:
        raise OfflineValidationError(
            "NOT_ZERO_POST", f"当前 root 账本已有 {calls} 条实呼行")
    budget = C.budget_from_dict(ctx.read_json("contract/budget.json"))
    doc = {
        "schema": AUTH_SCHEMA, "schema_version": 1,
        "envelope_id": ctx.envelope_id,
        "profile_id": profile.profile_id,
        "profile_sha256": C.canonical_sha256(profile.to_dict()),
        "mode": "offline_validation",
        "mode_note": "离线演练：禁用生产 gateway；模型 transport 换成本地公开消费者",
        "consumer": {"relpath": CONSUMER_RELPATH, "sha256": info["sha256"],
                     "n_bytes": info["n_bytes"]},
        "lanes": {lane: int(profile.lane_post_caps[lane])
                  for lane in profile.lane_order},
        "aux_subenvelopes": dict(P.AUX_SUBENVELOPES),
        "ledger_path": str(ledger_path),
        "ledger_call_rows_at_issue": 0,
        "issued_at_utc": now(),
    }
    rel = ctx.relative(AUTH_RELPATH)
    if rel.is_file():
        prev = ctx.read_json(AUTH_RELPATH)
        if {k: v for k, v in prev.items() if k != "issued_at_utc"} == \
                {k: v for k, v in doc.items() if k != "issued_at_utc"}:
            _advance_authorized(ctx)
            return prev
        raise OfflineValidationError(
            "OFFLINE_AUTH_EXISTS", "离线授权已存在且身份不同（不可变）")
    ctx.write_json(AUTH_RELPATH, doc)
    _advance_authorized(ctx)
    return doc


def _advance_authorized(ctx: Any) -> None:
    state = S.ResearchState(ctx)
    if state.root_state() == S.ROOT_INIT:
        state.advance_root(S.ROOT_AUTHORIZED,
                           reason="离线授权件落盘（零 POST，禁用 sender）")


def load_authorization(ctx: Any) -> "dict[str, Any]":
    if not ctx.exists(AUTH_RELPATH):
        raise OfflineValidationError("OFFLINE_AUTH_MISSING", AUTH_RELPATH)
    return ctx.read_json(AUTH_RELPATH)


def assert_authorized(ctx: Any, *, repo_root: "Path | str | None" = None,
                      ledger_path: "Path | str | None" = None) -> "dict[str, Any]":
    """每次离线运行前的复核：授权件在场、消费者字节未漂移、账本仍零实呼。"""
    doc = load_authorization(ctx)
    if doc.get("schema") != AUTH_SCHEMA:
        raise OfflineValidationError("OFFLINE_AUTH_SCHEMA", repr(doc.get("schema")))
    if doc.get("envelope_id") != ctx.envelope_id:
        raise OfflineValidationError("OFFLINE_AUTH_STALE",
                                     "授权件 envelope 与当前根不符")
    env_doc = _load_envelope(ctx)
    if env_doc.get("mode") != "offline_validation":
        raise OfflineValidationError("OFFLINE_MODE_REQUIRED", "envelope 不是离线模式")
    consumer = consumer_source(repo_root)
    if consumer_sha256(consumer) != (doc.get("consumer") or {}).get("sha256"):
        raise OfflineValidationError(
            "CONSUMER_DRIFT", "消费者源码与授权件 sha256 不符")
    ledger = ledger_path if ledger_path is not None else Path(doc["ledger_path"])
    # 独立评审 R3-m2：调用面传进来的账本路径必须就是授权件登记的那一本——否则
    # 指到一个新建空账本即可在真实账本已有付费行时「证明」零 POST。
    declared = str(doc.get("ledger_path") or "")
    if declared and Path(ledger).resolve() != Path(declared).resolve():
        raise OfflineValidationError(
            "LEDGER_PATH_MISMATCH",
            f"账本 {Path(ledger).resolve()} ≠ 授权件 {Path(declared).resolve()}")
    # 演练期账本必在场（offline init 预建 0600 空账本）；缺席 ⇒ 证据面缺失，拒。
    calls = _paid_rows(ctx, ledger, require_exists=True)
    if calls:
        raise OfflineValidationError("NOT_ZERO_POST", f"账本出现 {calls} 条实呼行")
    return doc


def suite_spec_document(ctx: Any, *, authorization_sha256: str,
                        now: Callable[[], str] = utc_now_iso_z) -> "dict[str, Any]":
    doc = {
        "schema": SPEC_SCHEMA, "schema_version": 1,
        "envelope_id": ctx.envelope_id,
        "authorization_sha256": authorization_sha256,
        "suites": {k: dict(v) for k, v in SUITE_SPEC.items()},
        "aux_subenvelopes": dict(P.AUX_SUBENVELOPES),
        "aux_plan": {k: {"envelope": env, "steps": steps}
                     for k, (env, steps) in AUX_PLAN.items()},
        "registered_at_utc": now(),
        "rule": "夹具与预期先注册；跑后不得改预期来凑绿",
    }
    return doc


# ---------------------------------------------------------------------------
# canary 与逐回合证据
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CanarySet:
    """禁出现在消费者输入/输出里的字节串（私有盘/他 study/他 attempt）。"""

    values: "tuple[bytes, ...]" = ()

    def check(self, blob: bytes, *, where: str) -> None:
        for value in self.values:
            if value and value in blob:
                # 不回显 canary 原文，只报序号与来源（防止证据件自己泄露）
                idx = self.values.index(value)
                raise OfflineValidationError(
                    "CANARY_LEAK", f"{where} 出现第 {idx} 号 canary（{len(value)} 字节）")


class TurnLog:
    """逐回合证据（append-only；写入即 fsync；含 canary 扫描结果）。"""

    def __init__(self, ctx: Any, *, canaries: CanarySet = CanarySet()) -> None:
        self.ctx = ctx
        self.canaries = canaries
        self.seq = 0
        self.entries: "list[dict[str, Any]]" = []

    def append(self, entry: Mapping[str, Any]) -> "dict[str, Any]":
        doc = {"schema": TURN_SCHEMA, "schema_version": 1,
               "seq": self.seq + 1, **dict(entry)}
        self.canaries.check(
            json.dumps(doc, ensure_ascii=False, sort_keys=True).encode("utf-8"),
            where="turn-log")
        target = self.ctx.relative(TURN_LOG_RELPATH)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(target, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
        try:
            os.write(fd, (json.dumps(doc, ensure_ascii=False, sort_keys=True,
                                     allow_nan=False) + "\n").encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        self.seq += 1
        self.entries.append(doc)
        return doc


# ---------------------------------------------------------------------------
# 公开消费者运输
# ---------------------------------------------------------------------------

@dataclass
class OfflineConsumerTransport:
    """``Callable[[T.Request], L.Reply]``：独立进程跑公开消费者。

    ``evidence_root`` 是每次调用的私有临时目录（0700，用后回收）；消费者在其中
    只看到一个空目录：没有 repo、没有 campaign root、没有 env（只 ``PATH``）。
    """

    consumer: "Path | str"
    log: TurnLog
    python: str = field(default_factory=lambda: sys.executable)
    timeout_s: float = 120.0
    clock: Callable[[], float] = time.monotonic
    evidence_root: "Path | None" = None
    calls: int = 0

    def __post_init__(self) -> None:
        self.consumer = Path(self.consumer)
        if not self.consumer.is_file():
            raise OfflineValidationError("CONSUMER_MISSING", str(self.consumer))

    def __call__(self, request: T.Request) -> L.Reply:
        payload = request.serialized
        raw = payload.encode("utf-8")
        self.log.canaries.check(raw, where="request")
        cwd = Path(tempfile.mkdtemp(prefix="rsi-r12-consumer-"))
        os.chmod(cwd, 0o700)
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
               "PYTHONHASHSEED": "0"}
        started = self.clock()
        try:
            proc = subprocess.run(
                [self.python, "-I", str(self.consumer)],
                input=raw, capture_output=True, cwd=str(cwd), env=env,
                timeout=float(self.timeout_s), check=False)
        except subprocess.TimeoutExpired:
            self._record(request, b"", b"", cwd, started, rc=-1,
                         note="timeout")
            raise OfflineValidationError("CONSUMER_TIMEOUT",
                                         f"{self.timeout_s}s 未返回") from None
        elapsed_ms = int((self.clock() - started) * 1000)
        out, err = proc.stdout, proc.stderr
        self.log.canaries.check(out + err, where="consumer-output")
        if proc.returncode != 0:
            self._record(request, out, err, cwd, started, rc=proc.returncode,
                         note="nonzero")
            raise OfflineValidationError(
                "CONSUMER_EXIT",
                f"rc={proc.returncode} stderr={err[-300:].decode('utf-8', 'replace')}")
        try:
            text = out.decode("utf-8").strip()
            doc = json.loads(text)
        except (UnicodeDecodeError, ValueError):
            self._record(request, out, err, cwd, started, rc=0, note="bad-json")
            raise OfflineValidationError("CONSUMER_REPLY", "输出不是单行 JSON") from None
        if not isinstance(doc, dict) or doc.get("action") not in L.ACTION_NAMES:
            self._record(request, out, err, cwd, started, rc=0, note="bad-action")
            raise OfflineValidationError(
                "CONSUMER_ACTION", f"action={doc.get('action') if isinstance(doc, dict) else None!r}")
        reply_text = json.dumps(doc, ensure_ascii=False, sort_keys=True)
        entry = self._record(request, out, err, cwd, started, rc=0, note="ok")
        return L.Reply(text=reply_text,
                       response_id=_response_id(entry),
                       # ``subprocess.run`` 返回 CompletedProcess，**没有** pid 属性
                       # （2026-11-12 T5 全量演练实测：写 proc.pid ⇒ AttributeError
                       # 打断 suite1 practice 链）。子进程身份已由 ``_record`` 的
                       # argv/cwd/rc/耗时逐呼留证，这里不再引用 pid。
                       usage={"offline": True},
                       attempts_http=1)

    def _record(self, request: T.Request, out: bytes, err: bytes, cwd: Path,
                started: float, *, rc: int, note: str) -> "dict[str, Any]":
        self.calls += 1
        elapsed_ms = int((self.clock() - started) * 1000)
        entry = {
            "call": self.calls,
            "request_sha256": request.sha256,
            "request_bytes": request.n_bytes,
            "reply_sha256": hashlib.sha256(out).hexdigest(),
            "reply_bytes": len(out),
            "rc": int(rc), "note": note, "elapsed_ms": elapsed_ms,
            "cwd": str(cwd), "cwd_is_temp": True,
            "env_keys": ["PATH", "PYTHONHASHSEED"],
            "argv": [Path(self.python).name, "-I", self.consumer.name],
            "stderr_tail": err[-500:].decode("utf-8", "replace"),
        }
        try:
            return self.log.append(entry)
        finally:
            _rmtree(cwd)


@dataclass
class ScriptedTransport:
    """``Callable[[T.Request], L.Reply]``：**预注册脚本回合**（非 POST）。

    Plan §4 明确允许"scripted turns"：它们不是模型调用，但占用与真实发送**同一个
    lane 决策槽**（走同一 sender/admission/提前量门），因此不会因为零 POST 而
    无限循环。每次回合照旧进 ``turns.jsonl``（``transport="scripted"``），
    与子进程消费者回合同表可对账。
    """

    decide: Callable[[list], Mapping[str, Any]]
    log: TurnLog
    calls: int = 0

    def __call__(self, request: T.Request) -> L.Reply:
        payload = json.loads(request.serialized)
        messages = list(payload.get("messages") or [])
        doc = dict(self.decide(messages))
        text = json.dumps(doc, ensure_ascii=False, sort_keys=True)
        self.calls += 1
        entry = self.log.append({
            "call": self.calls,
            "transport": "scripted",
            "request_sha256": request.sha256,
            "request_bytes": request.n_bytes,
            "reply_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "reply_bytes": len(text.encode("utf-8")),
            "action": doc.get("action"),
            "rc": 0, "note": "scripted",
        })
        return L.Reply(text=text, response_id=_response_id(entry),
                       usage={"offline": True, "scripted": True},
                       attempts_http=1)


def _response_id(entry: Mapping[str, Any]) -> str:
    """离线回合身份 = 回合序号 + 请求 sha + 回复 sha（**全局唯一**，不是内容 sha）。

    2026-11-12 T5 实测两轮：
    1. 初版只取 ``reply_sha256``：消费者在第 8 呼复读第 6 呼的 ``run_batch``
       字节 ⇒ 身份撞车 ⇒ ``record_action`` 的幂等去重吞掉新动作事件 ⇒
       ``_binding`` 判 ``STATE_UNKNOWN``，整条链被一个「重复回复」静默处死。
    2. 加了 ``call`` 前缀仍不够：**每个 study/lane 各造一个 transport**，序号从
       0001 重新数，A 与 B 的首呼请求不同、回复却逐字相同（同一份 design 模板）
       ⇒ 仍是同一个 id ⇒ ``record_action`` 把 B 的动作当成 A 的重复吞掉，
       `_binding` 只见 A 的动作事件（study 过滤后不可见）⇒ ``STATE_UNKNOWN``。
    所以身份必须同时绑定**请求**与**回复**两侧字节：同一次请求+同一份回复=同一
    身份（重放幂等），任何一侧变化都是新回合。生产网关的 response_id 逐呼唯一，
    离线运输保持同一口径。
    """
    return (f"{int(entry['call']):04d}-{str(entry['request_sha256'])[:12]}"
            f"-{str(entry['reply_sha256'])[:12]}")


def _rmtree(path: Path) -> None:
    import shutil

    shutil.rmtree(path, ignore_errors=True)


# ---------------------------------------------------------------------------
# 验证 LOCK 的实现身份（lock.py 与签名共用同一个纯函数，避免两侧各算一套）
# ---------------------------------------------------------------------------

def implementation_sha256(lock_doc: Mapping[str, Any], *,
                          consumer_sha256_hex: str) -> str:
    """实现身份 = 闭包 + 依赖 + 镜像 pin + 消费者字节（不含报告/签名）。"""
    body = {
        "schema": "simpraxis.rsi_r1.validation_implementation.v1",
        "closure_sha256": (lock_doc.get("closure") or {}).get("closure_sha256"),
        "dependencies": dict(lock_doc.get("dependencies") or {}),
        "image": (lock_doc.get("image") or {}).get("pin"),
        "consumer_sha256": str(consumer_sha256_hex),
        "profile_sha256": ((lock_doc.get("validation") or {}).get("profile_sha256")),
    }
    return C.canonical_sha256(body)


def report_document(*, ctx: Any, authorization: Mapping[str, Any],
                    lock_sha256: str, suites: Mapping[str, Any],
                    turns: Sequence[Mapping[str, Any]],
                    verdict: str, reasons: Sequence[str],
                    ledger_call_rows: int,
                    suites_required: "Sequence[str] | None" = None,
                    suites_run: "Sequence[str] | None" = None,
                    complete: "bool | None" = None,
                    profile_sha256: "str | None" = None,
                    image: "str | None" = None,
                    failure_matrix: "Mapping[str, Any] | None" = None,
                    suite_spec_sha256: str = "",
                    now: Callable[[], str] = utc_now_iso_z
                    ) -> "dict[str, Any]":
    doc = {
        "schema": REPORT_SCHEMA, "schema_version": 1,
        "envelope_id": ctx.envelope_id,
        "mode": "offline_validation",
        "authorization_sha256": C.canonical_sha256(dict(authorization)),
        "consumer_sha256": (authorization.get("consumer") or {}).get("sha256"),
        "validation_lock_sha256": lock_sha256,
        # 锁内适配（2026-11-12 T5 独立评审 MAJOR-2/3/4）：报告必须自证「六套全跑、
        # 跑的是一份什么画像/镜像」，而不是只抄 LOCK——签名绑定读取这些字段。
        "suites_required": list(suites_required or []),
        "suites_run": list(suites_run or list(dict(suites))),
        "complete": bool(complete) if complete is not None else False,
        "profile_sha256": str(profile_sha256 or ""),
        "image": {"pin": str(image or "")},
        # MINOR-7：注册件（夹具与预期）的规范化 SHA 入报告并由三签绑定；
        # MAJOR-7：固定失败矩阵逐行覆盖登记（``complete`` 由纯函数机判）。
        "suite_spec_sha256": str(suite_spec_sha256 or ""),
        "failure_matrix": dict(failure_matrix or {}),
        "consumer_isolation": "static_audit_plus_sandbox",
        "suites": dict(suites),
        "turns": {"n": len(turns),
                  "request_sha256": [t.get("request_sha256") for t in turns],
                  "reply_sha256": [t.get("reply_sha256") for t in turns]},
        "ledger_call_rows": int(ledger_call_rows),
        "verdict": verdict, "reasons": list(reasons),
        "generated_at_utc": now(),
        "note": ("离线演练只证明生产管线可用、零 POST、消费者不可见私有面；"
                 "不是正式模型成果，也不产生科学结论。"),
    }
    return doc


def write_report(ctx: Any, doc: Mapping[str, Any]) -> str:
    ctx.write_json(REPORT_RELPATH, dict(doc))
    rel = ctx.relative(REPORT_RELPATH)
    return hashlib.sha256(rel.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# 回归面取证（失败矩阵的 ``regression:`` 行不得硬编码 ok=True）
# ---------------------------------------------------------------------------

REGRESSION_EVIDENCE_RELPATH = "contract/offline_validation/regression_evidence.json"
REGRESSION_SCHEMA = "simpraxis.rsi_r1.regression_evidence.v1"
#: 失败矩阵里登记为 T2/T3 回归面的测试文件（逐文件必须有真跑过的通过数）。
#:
#: 独立评审 R4-M1（2026-11-13）：`worker_sigterm_sigkill` 行原先登记
#: `tests/test_rsi_r12_attempts.py`，但该文件**没有任何** SIGTERM/SIGKILL 用例
#: （实测只有 10 条与信号无关的用例全绿就把这一行「证明」成通过）；真正覆盖在
#: `tests/test_rsi_r12_lifecycle.py`（SIGTERM 受控收尾 / 无握手死亡两条）。故
#: 把 lifecycle 纳入回归面，并让失败矩阵逐行钉**具体用例名**。
REGRESSION_TESTS = ("tests/test_rsi_r12_attempts.py",
                    "tests/test_rsi_r12_lifecycle.py",
                    "tests/test_rsi_r12_recovery.py")


def regression_tests_sha256(repo_root: "Path | str | None" = None
                            ) -> "dict[str, str]":
    """登记回归测试文件的逐件 sha256（LOCK 冻结值 / 取证件 / 签前现场三处共用）。

    独立评审 R3-M1：取证件若只绑实现闭包，freeze 后把测试文件改成 ``assert True``
    再跑 attest，矩阵三行照样变绿。这里把**测试字节本身**变成被冻结、被复核的锚点。
    """
    root = Path(repo_root) if repo_root is not None else REPO_ROOT
    out: "dict[str, str]" = {}
    for rel in REGRESSION_TESTS:
        path = root / rel
        if not path.is_file():
            raise OfflineValidationError("REGRESSION_TEST_MISSING", rel)
        out[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def _parse_junit(path: "Path | str") -> "dict[str, dict[str, Any]]":
    """junit xml → ``{测试文件: {passed, failed, skipped, cases}}``（按 classname 归并）。

    ``cases`` 是 ``{用例名: "passed"|"failed"|"skipped"}``（参数化实例保留
    ``[param]`` 后缀）。独立评审 R4-M1：失败矩阵的回归行必须能钉**具体用例**，
    否则同一文件里一批无关用例全绿就能把一行「证明」成通过——已实测发生过。
    """
    import xml.etree.ElementTree as ET

    out: "dict[str, dict[str, Any]]" = {}
    root = ET.parse(str(path)).getroot()
    for case in root.iter("testcase"):
        cls = str(case.get("classname") or "")
        rel = cls.replace(".", "/") + ".py" if cls else str(case.get("file") or "")
        rec = out.setdefault(rel, {"passed": 0, "failed": 0, "skipped": 0,
                                   "cases": {}})
        if case.find("failure") is not None or case.find("error") is not None:
            status = "failed"
        elif case.find("skipped") is not None:
            status = "skipped"
        else:
            status = "passed"
        rec[status] += 1
        name = str(case.get("name") or "")
        if name:
            # T5-M2 修复（2026-11-13）：同名用例**状态冲突**不许后写覆盖前写。
            # pytest 本身保证文件内用例名唯一，此分支只在 junit 被伪造/拼接时
            # 触发；状态相同则幂等保留。
            previous = rec["cases"].get(name)
            if previous is not None and previous != status:
                raise OfflineValidationError(
                    "REGRESSION_CASE_CONFLICT",
                    f"{rel}::{name} 出现冲突状态 {previous!r} vs {status!r}")
            rec["cases"][name] = status
    return out


def run_regression_attestation(ctx: Any, *, closure_sha256: str,
                               repo_root: "Path | str | None" = None,
                               python: "str | None" = None,
                               now: Callable[[], str] = utc_now_iso_z
                               ) -> "dict[str, Any]":
    """真跑失败矩阵登记的回归测试，落可复核取证件（绑定当前实现闭包）。

    锁内适配（2026-11-12 T5 独立评审 MAJOR：「回归行硬编码 ok=True」）。旧实现
    直接把三行回归标成通过——没有任何证据。这里用 ``pytest --junitxml`` 真跑
    登记文件，把逐文件通过/失败数 + 实现闭包 sha 写进 root 的取证件；演练面
    只有在 ``closure_sha256`` 与冻结 LOCK 逐字相等、``returncode==0``、逐文件
    ``passed>0 且 failed==0`` 时才承认这些行。
    """
    import subprocess
    import tempfile

    root = Path(repo_root) if repo_root is not None else REPO_ROOT
    interp = str(python or sys.executable)
    # R3-M1：跑前/跑后各算一次测试字节 sha，既进取证件也须与 LOCK 冻结值相等。
    tests_before = regression_tests_sha256(root)
    with tempfile.TemporaryDirectory(prefix="r12-regression-") as tmp:
        junit = Path(tmp) / "junit.xml"
        cmd = [interp, "-m", "pytest", *REGRESSION_TESTS, "-q", "-p", "no:cacheprovider",
               "-m", "not paid and not kb_integration",
               f"--junitxml={junit}"]
        proc = subprocess.run(cmd, cwd=str(root), capture_output=True, text=True)
        files = _parse_junit(junit) if junit.is_file() else {}
    if regression_tests_sha256(root) != tests_before:
        raise OfflineValidationError(
            "REGRESSION_TESTS_CHANGED", "回归测试文件在取证过程中被改动")
    doc = {
        "schema": REGRESSION_SCHEMA, "schema_version": 1,
        "closure_sha256": str(closure_sha256),
        "test_files": tests_before,
        "repo_root": str(root),
        "command": cmd,
        "returncode": int(proc.returncode),
        "files": files,
        "tail": (proc.stdout or "")[-2000:],
        "generated_at_utc": now(),
    }
    ctx.write_json(REGRESSION_EVIDENCE_RELPATH, doc)
    return doc


def load_regression_evidence(ctx: Any) -> "dict[str, Any] | None":
    if not ctx.exists(REGRESSION_EVIDENCE_RELPATH):
        return None
    return ctx.read_json(REGRESSION_EVIDENCE_RELPATH)


__all__ = [
    "AUTH_RELPATH", "AUTH_SCHEMA", "CONSUMER_RELPATH", "CanarySet",
    "OfflineConsumerTransport", "OfflineValidationError", "REGRESSION_EVIDENCE_RELPATH",
    "REGRESSION_SCHEMA", "REGRESSION_TESTS", "ScriptedTransport",
    "REPORT_RELPATH", "SPEC_RELPATH", "SUITE_SPEC", "TurnLog", "assert_authorized",
    "authorize_offline", "consumer_sha256", "consumer_source",
    "implementation_sha256", "load_authorization", "load_regression_evidence",
    "report_document", "regression_tests_sha256", "run_regression_attestation",
    "suite_spec_document", "verify_consumer_source", "write_report",
]
