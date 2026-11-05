"""Task 3 Functional Birth Validator：B0→B4 五层出生验证（Plan T3.5）。

**Birth ≠ Task Success**：本模块只回答「交付物是否达到可执行的最低程度」，
``task_success`` 在结果里**恒为 None**；任务成功由 Task 4 的独立评测器判定。

五层语义（D3-1，固定）：

===========  ============================================================  ==============
层           检查什么                                                      必需性来源
===========  ============================================================  ==============
B0 静态      入口存在 / 普通文件 / 非符号链接 / 不越出 workdir / 体积下限 /  恒必需
             ``ast.parse``（.py）或 YAML 解析（.yaml）
B1 真实      按 ``task.yaml.entrypoint.kind`` 真加载候选（``python_script``  ``birth_contract.import_required``
import       → ``runpy.run_path``；``module`` → ``importlib``）；文档包不适用
B2 构造      调契约的构造入口（``build_scene`` / ``build_env``），对象非 None   ``construction_required``
B3 契约      契约结构检查（动作维度/观测键/可调用性；文档包查四件产物）       ``contract_required``
B4 最小步进  一次真实 reset（若契约要求）+ 一次 step：必须推进且全 finite    ``minimal_step_required``
===========  ============================================================  ==============

隔离纪律（T3.1 不变量 2 / D3-2 / D3-4 / D3-5 / D3-6）：

- **每层一个独立子进程**（``python -I -c``），层间不共享解释器状态；候选调用
  ``os._exit()`` 只杀子进程，``sys.modules`` 污染不跨层；
- ``preexec_fn`` 设 ``RLIMIT_CPU`` / ``RLIMIT_AS``；``subprocess`` 超时另设上限；
  两个上限哪个先到就按 ``timeout`` / ``memory`` 归类；
- 路径 jail：入口 ``resolve()`` 后必须仍在 ``workdir`` 内，符号链接入口与软链
  逃逸一律 ``path_escape``（D3-5，``is_relative_to`` 而非字符串前缀）；
- 断网**分级**：容器路径 ``--network none`` 记 ``enforced``；宿主路径无特权断网
  能力，记 ``audited_only`` 并附 AST 静态扫描命中（D3-6，不假装断网）。

**结果通道（Phase 2.8.0 评审 W2-01 修复）**：运行器的层结果**不再**写 stdout，
而是写到父进程指定的 ``result_path``（一次性 nonce 文件名，``os.replace`` 原子落盘），
记录内含 ``level`` 与 ``nonce`` 双字段，父进程逐项校验。stdout/stderr 只当日志；
二者中**出现任何** ``RUNNER_MARKER`` 行即判候选伪造出生标记 → 该层 ``FAIL``
（``contract_error``，detail 前缀「候选伪造出生标记行」）。

**挑战通道（同一条 W2-01 的二次加固）**：``nonce`` 与 ``result_path`` **不进**
``payload.json``、也不进 argv（候选可读 ``payload.json``、``/proc/self/cmdline``），
而是父进程在候选代码执行**之前**经 stdin 下发一行 JSON 挑战；运行器先读走该行
再导入候选，父进程随即关闭写端（候选读 stdin 只得 EOF）。候选即使读遍
``payload.json`` 与自身 argv 也拿不到 nonce / 结果路径，写出的任何记录都会因
nonce 校验失败而不被采信（fail-closed 落 ``_GENERIC_REASON``）。

残余威胁模型（如实登记，不假装解决）：「恶意候选」在**同一进程**内执行，仍可在
运行器读走 stdin 之后从 ``sys.modules['__main__']`` 的全局变量（``NONCE`` /
``RESULT_PATH``）或调用栈帧里取出这两个值，并写出一条合法记录后 ``os._exit(0)``。
协议层无法在进程内自证——这是同一进程模型的固有上限，非本通道遗漏。真正的
进程外隔离（OS 沙箱 / 独立 harness 进程 + 独立通信通道）属 Phase 3 backlog。
本仓消除的是**共享通道**（候选打印标记行即被采信）与**可枚举通道**
（payload/argv 直给 nonce），并把 stdout 上的任何伪造尝试变成可检测的篡改证据。

失败 ``reason`` 取值固定（D3-7）：``missing / too_small / symlink / ast_error /
compile_error / import_error / construction_error / contract_error / step_error /
non_finite / timeout / memory / path_escape / blocked_infra``。
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from orchestrator.benchmark import contracts as C
from orchestrator.benchmark import task_spec as ts
from orchestrator.benchmark.runtime_fingerprint import sanitized_env
from orchestrator.common.fsutil import atomic_write_text, ensure_private_dir

__all__ = [
    "BIRTH_SCHEMA", "LEVELS", "LEVEL_STATUSES", "FAILURE_REASONS",
    "CONTRACT_BY_TASK", "BirthError", "BirthSpec", "Limits", "LevelResult",
    "BirthResult", "BirthValidator", "RUNNER_MARKER", "read_runner_result",
    "detect_forged_marker",
]

BIRTH_SCHEMA = "simpraxis.functional_birth.v1"
BIRTH_NAME = "functional_birth.json"
BIRTH_SHA_NAME = "birth.sha256"
LEVELS = ("B0", "B1", "B2", "B3", "B4")
LEVEL_STATUSES = ("PASS", "FAIL", "NOT_APPLICABLE", "BLOCKED_INFRA")
FAILURE_REASONS = ("missing", "too_small", "symlink", "ast_error",
                   "compile_error", "import_error", "construction_error",
                   "contract_error", "step_error", "non_finite", "timeout",
                   "memory", "path_escape", "blocked_infra")
RUNNER_MARKER = "__SIMPRAXIS_BIRTH__"
#: D3-4：stdout/stderr 各截断 64 KiB 落盘（meta 里仍记全量字节数）
MAX_CAPTURE_BYTES = 64 * 1024
#: 超时后整组击杀，再给管道至多这么多秒排空；仍在拖则不再等待（W2-05）。
_KILL_GRACE_S = 5.0


def _kill_process_group(proc) -> None:
    """杀掉以 ``proc`` 为首的整个进程组（评审 W2-05）。

    ``start_new_session=True`` 让子进程自成会话/进程组（pgid == pid），
    因此候选派生的孙进程默认仍在组内；只 ``proc.kill()`` 会留下持有
    stdout 管道的孙进程，``communicate()`` 随即无上界阻塞。
    """
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass

#: 任务 → 契约名（Task 4/6 可直接复用；未知任务必须显式给 contract）
CONTRACT_BY_TASK = {
    "mujoco_contact_push_v1": "mujoco_push",
    "libero_env_adapter_v1": "libero_env_adapter",
    "icy_ground_dr_v1": "research_doc",
}

#: 每层的「兜底 reason」（子进程没吐出标记行时用；仍是固定枚举，不是自由文本）
_GENERIC_REASON = {
    "B0": "compile_error",
    "B1": "import_error",
    "B2": "construction_error",
    "B3": "contract_error",
    "B4": "step_error",
}

#: 断网审计的 AST 扫描目标（D3-6 的 audited 部分）
_NETWORK_MODULES = ("socket", "requests", "urllib", "urllib3", "http",
                    "ftplib", "telnetlib", "aiohttp", "httpx")


class BirthError(RuntimeError):
    """出生验证自身不可继续（配置错误 / 输出目录不合规）。

    注意：**候选**的问题一律落成 FAIL/BLOCKED_INFRA 的结果件，不抛本异常。
    """


# --------------------------------------------------------------------------
# 候选子进程内的运行器（单文件、无本地 import，避免被候选目录劫持）
# --------------------------------------------------------------------------

_RUNNER_SRC = r'''
import ast
import json
import math
import os
import sys
import traceback

#: 只用于**父进程**检测伪造：运行器自己不再往 stdout 写任何结果。
MARKER = "__SIMPRAXIS_BIRTH__"


def emit(status, reason, detail="", data=None):
    record = {"level": LEVEL, "nonce": NONCE, "status": status,
              "reason": reason, "detail": str(detail)[:4000],
              "data": data or {}}
    body = json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n"
    path = RESULT_PATH
    tmp = path + ".tmp.%d" % os.getpid()
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, body.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    #: 容器形态（L1）以镜像默认用户 root 落盘，宿主 uid 读不回 0600 的 root 文件。
    #: 载荷里带 ``chown_uid/chown_gid`` 时把结果件交回调用者：root 才能 chown，
    #: 宿主路径不带这两个键，语义不变（非 root 也无从 chown）。
    if cfg.get("chown_uid") is not None:
        try:
            os.chown(path, int(cfg["chown_uid"]), int(cfg["chown_gid"]))
        except OSError:
            pass
    sys.exit(0)


cfg = json.loads(open(sys.argv[1], encoding="utf-8").read())

#: 评审 W2-01 二次加固：一次性 nonce 与结果路径**不在**候选可读的 ``payload.json``
#: 里，也不进 argv（argv 候选可从 /proc/self/cmdline 读），而是父进程在导入候选
#: **之前**经 stdin 下发一行挑战。候选代码无论读 payload.json、argv 还是 /proc/self/cmdline
#: 都拿不到 nonce / 结果路径；父进程写完即关写端，管道里没有任何残留可读内容。
_CHALLENGE_LINE = sys.stdin.readline()
try:
    _CHALLENGE = json.loads(_CHALLENGE_LINE)
except Exception:
    _CHALLENGE = None
if (not isinstance(_CHALLENGE, dict)
        or not isinstance(_CHALLENGE.get("nonce"), str)
        or not _CHALLENGE.get("nonce")
        or not isinstance(_CHALLENGE.get("result_path"), str)
        or not _CHALLENGE.get("result_path")):
    sys.stderr.write("runner: 缺少一次性挑战行（stdin），拒绝执行\n")
    sys.exit(3)
NONCE = _CHALLENGE["nonce"]
RESULT_PATH = _CHALLENGE["result_path"]
#: 不关 fd 0：父进程写完即关闭管道写端，候选读 stdin 自然得到 EOF（``input()``
#: 抛 EOFError 而不是 ValueError），且管道里再无任何可读内容。
del _CHALLENGE_LINE, _CHALLENGE

LEVEL = cfg["level"]
WORKDIR = cfg["workdir"]
ENTRY_KIND = cfg["entry"]["kind"]
ENTRY_PATH = cfg["entry"]["path"]
sys.path.insert(0, cfg["repo_root"])
from orchestrator.benchmark import contracts as C  # noqa: E402


def _fail(reason, detail):
    emit("FAIL", reason, detail)


def _jail(rel):
    from pathlib import Path
    wd = Path(WORKDIR).resolve(strict=True)
    p = wd / rel
    if p.is_symlink():
        target = p.resolve(strict=False)
        if target != wd and wd not in target.parents:
            _fail("path_escape", "%s 是符号链接且逃出 workdir（-> %s）"
                  % (rel, target))
        _fail("symlink", "%s 是符号链接（出生验证拒绝链接入口）" % rel)
    if not p.exists():
        _fail("missing", "%s 不存在" % rel)
    # T3.5-3 明确要求 B0 判 ``is_file``（评审 W2-02）：入口是目录时若放行，
    # 不带 .py/.yaml 后缀的目录会以 st_size≈4096 直接 PASS B0。D3-7 是固定
    # 枚举，用 ``missing`` 归因最稳（detail 写明真实形态）。
    if not p.is_file():
        _fail("missing", "%s 不是常规文件（is_file=False）" % rel)
    rp = p.resolve(strict=True)
    if rp != wd and wd not in rp.parents:
        _fail("path_escape", "%s 解析到 %s，越过 workdir %s" % (rel, rp, wd))
    return wd, rp


def _load():
    wd, rp = _jail(ENTRY_PATH)
    sys.path.insert(0, str(wd))
    try:
        if ENTRY_KIND == "module":
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                "simpraxis_candidate", str(rp))
            if spec is None or spec.loader is None:
                raise ImportError("无法为 %s 构造 import spec" % rp)
            mod = importlib.util.module_from_spec(spec)
            sys.modules["simpraxis_candidate"] = mod
            spec.loader.exec_module(mod)
            return mod
        import runpy
        return runpy.run_path(str(rp), run_name="__simpraxis_candidate__")
    finally:
        if sys.path and sys.path[0] == str(wd):
            sys.path.pop(0)


def _load_or_fail():
    try:
        return _load()
    except SystemExit:
        raise
    except SyntaxError as exc:
        _fail("ast_error", "%s: %s" % (type(exc).__name__, exc))
    except ImportError as exc:
        _fail("import_error", "%s: %s" % (type(exc).__name__, exc))
    except MemoryError as exc:
        _fail("memory", "加载候选时内存超限（RLIMIT_AS）：%s" % (exc,))
    except BaseException as exc:
        _fail(cfg["generic_reason"],
              "%s: %s\n%s" % (type(exc).__name__, exc,
                              traceback.format_exc()[-4096:]))


def _contract():
    try:
        return C.contract_by_name(cfg["contract"])
    except BaseException as exc:
        emit("BLOCKED_INFRA", "blocked_infra", "契约不可用：%s" % exc)


def level_b0():
    wd, rp = _jail(ENTRY_PATH)
    size = rp.stat().st_size
    if size < int(cfg["min_bytes"]):
        _fail("too_small", "%s 只有 %d 字节（下限 %d）"
              % (ENTRY_PATH, size, int(cfg["min_bytes"])))
    if rp.suffix == ".py":
        # 评审 W2-02：候选自己的脏源码（null 字节 → ValueError、
        # 非 UTF-8 → UnicodeDecodeError）必须归 ``ast_error``，不能穿透到
        # runner 顶层 ``except BaseException`` 记成 BLOCKED_INFRA——那是把
        # 候选的错算到基础设施头上，违背 T3.1 不变量 4（模型问题不得记 infra）。
        try:
            ast.parse(rp.read_text(encoding="utf-8"))
        except (SyntaxError, ValueError, UnicodeDecodeError) as exc:
            _fail("ast_error", "%s: %s" % (type(exc).__name__, exc))
    elif rp.suffix in (".yaml", ".yml"):
        try:
            import yaml
            doc = yaml.safe_load(rp.read_text(encoding="utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            _fail("compile_error", "YAML 读取失败：%s: %s"
                  % (type(exc).__name__, exc))
        except BaseException as exc:
            _fail("compile_error", "YAML 解析失败：%s: %s"
                  % (type(exc).__name__, exc))
        if not isinstance(doc, (dict, list)):
            _fail("compile_error", "YAML 顶层必须是 mapping/sequence")
    emit("PASS", None, "", {"bytes": size, "suffix": rp.suffix,
                            "resolved": str(rp)})


def level_b1():
    if ENTRY_KIND == "document_bundle":
        emit("NOT_APPLICABLE", None, "document_bundle 无 import 语义")
    mod = _load_or_fail()
    contract = _contract()
    data = {}
    if contract.builder:
        data["builder_present"] = C.module_get(mod, contract.builder) is not None
    emit("PASS", None, "", data)


def level_b2():
    if ENTRY_KIND == "document_bundle":
        emit("NOT_APPLICABLE", None, "document_bundle 无构造语义")
    contract = _contract()
    mod = _load_or_fail()
    try:
        obj = contract.build(mod)
    except SystemExit:
        raise
    except MemoryError as exc:
        _fail("memory", "构造时内存超限（RLIMIT_AS）：%s" % (exc,))
    except BaseException as exc:
        _fail("construction_error", "%s: %s" % (type(exc).__name__, exc))
    keys = getattr(obj, "observation_keys", None)
    try:
        keys = list(keys) if keys is not None else None
    except BaseException:
        keys = repr(keys)
    emit("PASS", None, "", {"object_type": type(obj).__name__,
                            "action_dim": getattr(obj, "action_dim", None),
                            "observation_keys": keys})


def level_b3():
    contract = _contract()
    obj = None
    if contract.needs_object:
        mod = _load_or_fail()
        try:
            obj = contract.build(mod)
        except SystemExit:
            raise
        except BaseException as exc:
            _fail("construction_error", "%s: %s" % (type(exc).__name__, exc))
    try:
        violations = contract.check(obj, cfg.get("ctx") or {})
    except SystemExit:
        raise
    except BaseException as exc:
        _fail("contract_error", "契约检查抛异常：%s: %s"
              % (type(exc).__name__, exc))
    if violations:
        _fail("contract_error",
              "; ".join("%s: %s" % (v.code, v.detail) for v in violations))
    emit("PASS", None, "", {"violations": 0})


def level_b4():
    if ENTRY_KIND == "document_bundle":
        emit("NOT_APPLICABLE", None, "document_bundle 无步进语义")
    contract = _contract()
    ctx = cfg.get("ctx") or {}
    mod = _load_or_fail()
    try:
        obj = contract.build(mod)
    except SystemExit:
        raise
    except MemoryError as exc:
        _fail("memory", "构造时内存超限（RLIMIT_AS）：%s" % (exc,))
    except BaseException as exc:
        _fail("construction_error", "%s: %s" % (type(exc).__name__, exc))
    if contract.reset_required:
        try:
            contract.reset(obj, ctx)
        except SystemExit:
            raise
        except MemoryError as exc:
            _fail("memory", "reset 时内存超限（RLIMIT_AS）：%s" % (exc,))
        except BaseException as exc:
            _fail("step_error", "reset 失败：%s: %s"
                  % (type(exc).__name__, exc))
    try:
        action = contract.action(obj, ctx)
    except SystemExit:
        raise
    except BaseException as exc:
        _fail("step_error", "最小动作构造失败：%s: %s"
              % (type(exc).__name__, exc))
    try:
        result = contract.step(obj, action, ctx)
    except SystemExit:
        raise
    except MemoryError as exc:
        _fail("memory", "step 时内存超限（RLIMIT_AS）：%s" % (exc,))
    except BaseException as exc:
        _fail("step_error", "step 失败：%s: %s"
              % (type(exc).__name__, exc))
    numbers = C.flatten_numbers(result)
    if not numbers:
        _fail("step_error", "step 未返回任何数值状态：%r" % (result,))
    bad = sorted(key for key, value in numbers.items() if not math.isfinite(value))
    if bad:
        _fail("non_finite", "步进后存在非有限值：%s" % (bad[:8],))
    if not any(value != 0.0 for value in numbers.values()):
        _fail("step_error", "步进后状态毫无变化（未推进）")
    sample = dict(sorted(numbers.items())[:8])
    emit("PASS", None, "", {"advanced": True, "n_values": len(numbers),
                            "sample": sample})


LEVELS_IMPL = {"B0": level_b0, "B1": level_b1, "B2": level_b2,
               "B3": level_b3, "B4": level_b4}

if __name__ == "__main__":
    fn = LEVELS_IMPL.get(LEVEL)
    if fn is None:
        emit("BLOCKED_INFRA", "blocked_infra", "未知层 %r" % (LEVEL,))
    try:
        fn()
    except SystemExit:
        raise
    except BaseException as exc:
        emit("BLOCKED_INFRA", "blocked_infra",
             "运行器异常：%s: %s\n%s" % (type(exc).__name__, exc,
                                        traceback.format_exc()[-2048:]))
'''


# --------------------------------------------------------------------------
# 描述与结果
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class BirthSpec:
    """出生验证所需的**最小**任务视图（不依赖 registry，测试可直接构造）。"""

    task_id: str
    entry_kind: str
    entry_path: str
    birth_contract: Mapping[str, bool]
    contract: str
    outputs: "tuple[str, ...]" = ()
    min_bytes: int = 16

    @classmethod
    def from_task_spec(cls, spec: ts.TaskSpec) -> "BirthSpec":
        try:
            contract = CONTRACT_BY_TASK[spec.task_id]
        except KeyError:
            raise BirthError(
                f"{spec.task_id} 没有登记契约；请在 CONTRACT_BY_TASK 显式登记"
            ) from None
        entry = dict(spec.entrypoint)
        return cls(
            task_id=spec.task_id,
            entry_kind=str(entry.get("kind")),
            entry_path=str(entry.get("path")),
            birth_contract=dict(spec.birth_contract),
            contract=contract,
            outputs=tuple(spec.outputs),
        )

    def required_levels(self) -> "dict[str, bool]":
        bc = dict(self.birth_contract)
        return {
            "B0": True,
            "B1": bool(bc.get("import_required")),
            "B2": bool(bc.get("construction_required")),
            "B3": bool(bc.get("contract_required")),
            "B4": bool(bc.get("minimal_step_required")),
        }

    def to_json(self) -> dict:
        return {
            "task_id": self.task_id,
            "entry": {"kind": self.entry_kind, "path": self.entry_path},
            "birth_contract": dict(self.birth_contract),
            "contract": self.contract,
            "outputs": list(self.outputs),
            "min_bytes": self.min_bytes,
            "required": self.required_levels(),
        }


@dataclass(frozen=True)
class Limits:
    """子进程资源上限（D3-4）；改值必须在 Complete 登记（阈值属判据）。"""

    timeout_s: float = 120.0
    cpu_s: int = 180
    mem_bytes: int = 4 * 1024 ** 3

    def to_json(self) -> dict:
        return {"timeout_s": self.timeout_s, "cpu_s": self.cpu_s,
                "mem_bytes": self.mem_bytes}


@dataclass
class LevelResult:
    level: str
    status: str
    reason: "str | None" = None
    detail: str = ""
    duration_s: float = 0.0
    exit_code: "int | None" = None
    pid: "int | None" = None
    data: dict = field(default_factory=dict)
    stdout_bytes: int = 0
    stderr_bytes: int = 0

    def __post_init__(self) -> None:
        if self.status not in LEVEL_STATUSES:
            raise BirthError(f"{self.level}: 非法状态 {self.status!r}")
        if self.reason is not None and self.reason not in FAILURE_REASONS:
            raise BirthError(f"{self.level}: 非法 reason {self.reason!r}")

    def to_json(self) -> dict:
        return {
            "status": self.status,
            "reason": self.reason,
            "detail": self.detail,
            "duration_s": round(self.duration_s, 6),
            "exit_code": self.exit_code,
            "pid": self.pid,
            "data": dict(self.data),
            "stdout_bytes": self.stdout_bytes,
            "stderr_bytes": self.stderr_bytes,
            "evidence": {
                "stdout": f"levels/{self.level}/stdout.txt",
                "stderr": f"levels/{self.level}/stderr.txt",
                "meta": f"levels/{self.level}/meta.json",
            },
        }


@dataclass
class BirthResult:
    spec: BirthSpec
    levels: "dict[str, LevelResult]"
    birth_status: str
    problems: "list[str]"
    limits: Limits
    network_audit: dict
    workdir: str
    ts_iso: str
    schema: str = BIRTH_SCHEMA
    task_success: "None" = None

    def to_json(self) -> dict:
        return {
            "schema": self.schema,
            "task_id": self.spec.task_id,
            "task_success": None,
            "birth_status": self.birth_status,
            "entry": {"kind": self.spec.entry_kind,
                      "path": self.spec.entry_path},
            "contract": C.contract_by_name(self.spec.contract).describe(),
            "required": self.spec.required_levels(),
            "levels": {level: self.levels[level].to_json() for level in LEVELS},
            "problems": list(self.problems),
            "limits": self.limits.to_json(),
            "network_audit": dict(self.network_audit),
            "workdir": self.workdir,
            "ts_iso": self.ts_iso,
        }

    def canonical_json(self) -> str:
        return ts.canonical_json(self.to_json())


# --------------------------------------------------------------------------
# 运行器输出解析
# --------------------------------------------------------------------------

def read_runner_result(path: "str | Path", *, level: str, nonce: str) -> "dict | None":
    """读运行器写出的层结果（``result_path``）；任何不符一律 ``None``。

    拒绝条件（全部 fail-closed）：文件不存在/不可读、记录不止一条（多个写入者）、
    非单一 JSON 对象、``level`` 或 ``nonce`` 不符、``status`` 不在枚举内。
    """
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError:
        return None
    lines = [line for line in raw.splitlines() if line.strip()]
    if len(lines) != 1:
        return None
    try:
        doc = json.loads(lines[0])
    except json.JSONDecodeError:
        return None
    if not isinstance(doc, dict):
        return None
    if doc.get("level") != level or doc.get("nonce") != nonce:
        return None
    if doc.get("status") not in LEVEL_STATUSES:
        return None
    return doc


def detect_forged_marker(*texts: "str | None") -> "str | None":
    """stdout/stderr 中出现 ``RUNNER_MARKER`` 行 ⇒ 候选伪造出生标记（返回该行）。"""
    for text in texts:
        for line in (text or "").splitlines():
            if line.startswith(RUNNER_MARKER):
                return line[:200]
    return None


def _scan_network(path: Path) -> "list[dict]":
    """AST 扫候选入口的 import（D3-6 audited 部分；解析失败返回空并由 B0 报）。"""
    hits: "list[dict]" = []
    import ast
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:                                       # noqa: BLE001
        return hits
    for node in ast.walk(tree):
        names: "list[str]" = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        for name in names:
            root = name.split(".")[0]
            if root in _NETWORK_MODULES:
                hits.append({"module": name, "lineno": getattr(node, "lineno", 0)})
    return hits


#: 树扫描上限（防御候选写一万个 .py 把审计拖死）；触顶如实登记
_NET_SCAN_MAX_FILES = 200


def _scan_network_tree(root: Path) -> "tuple[list[dict], int]":
    """扫 ``workdir`` 下**全部** ``.py``（评审 W2-11）。

    只扫入口文件等于给了绕过口子：候选把 ``import requests`` 放进
    ``util.py`` 再 import，审计就 0 命中。这里按文件名排序、上限
    ``_NET_SCAN_MAX_FILES`` 件，命中项带相对路径 ``file``。
    """
    if not root.is_dir():
        return [], 0
    files = [p for p in sorted(root.rglob("*.py"))
             if p.is_file() and not p.is_symlink()][:_NET_SCAN_MAX_FILES]
    hits: "list[dict]" = []
    for path in files:
        rel = path.relative_to(root).as_posix()
        for hit in _scan_network(path):
            hits.append({"file": rel, "module": hit["module"],
                         "lineno": hit["lineno"]})
    return hits, len(files)


# --------------------------------------------------------------------------
# 验证器
# --------------------------------------------------------------------------

class BirthValidator:
    """在一个 ``workdir`` 上跑完 B0→B4，落盘五层原始输出与聚合结果。"""

    def __init__(self, spec: BirthSpec, *, workdir: "str | Path",
                 python: "str | None" = None,
                 limits: "Limits | None" = None,
                 network_mode: str = "audited_only",
                 ctx: "Mapping[str, Any] | None" = None) -> None:
        self.spec = spec
        self.workdir = Path(workdir).resolve()
        if not self.workdir.is_dir():
            raise BirthError(f"workdir 不存在或不是目录：{self.workdir}")
        self.python = python or sys.executable
        self.limits = limits or Limits()
        if network_mode not in ("none", "audited_only"):
            raise BirthError(f"network_mode 非法：{network_mode!r}")
        self.network_mode = network_mode
        self.repo_root = Path(__file__).resolve().parents[2]
        self.ctx = dict(ctx or {})
        self.ctx.setdefault("workdir", str(self.workdir))

    # -- 子进程 -------------------------------------------------------------
    def _preexec(self):                                     # pragma: no cover - 子进程内生效
        import resource
        resource.setrlimit(resource.RLIMIT_CPU,
                           (self.limits.cpu_s, self.limits.cpu_s + 1))
        resource.setrlimit(resource.RLIMIT_AS,
                           (self.limits.mem_bytes, self.limits.mem_bytes))

    def _payload(self, level: str) -> dict:
        return {
            "level": level,
            "workdir": str(self.workdir),
            "entry": {"kind": self.spec.entry_kind,
                      "path": self.spec.entry_path},
            "contract": self.spec.contract,
            "repo_root": str(self.repo_root),
            "min_bytes": self.spec.min_bytes,
            "generic_reason": _GENERIC_REASON[level],
            "ctx": dict(self.ctx),
        }

    def _not_applicable(self, level: str, level_dir: Path) -> LevelResult:
        """``birth_contract`` 未要求的层：直接记 NOT_APPLICABLE，不起子进程。

        D3-3：四个布尔决定哪些层必需；不适用层**不得算 PASS**。证据三件照写
        （空 stdout/stderr + meta.json），保证五层证据结构一致（T3.7）。
        """
        result = LevelResult(
            level=level, status="NOT_APPLICABLE",
            detail="birth_contract 未要求该层（不适用层不计入 PASS）")
        atomic_write_text(level_dir / "stdout.txt", "", 0o600)
        atomic_write_text(level_dir / "stderr.txt", "", 0o600)
        atomic_write_text(level_dir / "meta.json",
                          json.dumps(result.to_json(), ensure_ascii=False,
                                     indent=2, sort_keys=True) + "\n", 0o600)
        return result

    def _run_restricted(self, level: str, level_dir: Path) -> LevelResult:
        if not self.spec.required_levels()[level]:
            return self._not_applicable(level, level_dir)
        nonce = secrets.token_hex(16)
        result_path = level_dir / f"runner_result.{nonce}.json"
        #: 评审 W2-01 二次加固：payload.json 与 argv 都**不含** nonce / 结果路径
        #: （二者候选可读），挑战行只经 stdin 在导入候选前下发。
        payload = self._payload(level)
        challenge = json.dumps(
            {"nonce": nonce, "result_path": str(result_path)},
            ensure_ascii=False) + "\n"
        payload_path = level_dir / "payload.json"
        atomic_write_text(payload_path,
                          json.dumps(payload, ensure_ascii=False,
                                     indent=2, sort_keys=True) + "\n", 0o600)
        started = time.monotonic()
        timed_out = False
        stdout = stderr = ""
        rc: "int | None" = None
        pid: "int | None" = None
        try:
            proc = subprocess.Popen(
                [self.python, "-I", "-c", _RUNNER_SRC, str(payload_path)],
                cwd=str(self.workdir), env=sanitized_env(),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                # 评审 W2-05：独立进程组，超时可整组击杀（孙进程不再拖住管道）。
                start_new_session=True,
                preexec_fn=self._preexec)
        except (OSError, subprocess.SubprocessError) as exc:
            # 计划 T3.9：setrlimit 不可用必须显式 BLOCKED_INFRA，不得静默跳过限制
            result = LevelResult(
                level=level, status="BLOCKED_INFRA", reason="blocked_infra",
                detail=f"受限子进程无法启动（setrlimit/preexec 失败）：{exc}")
            atomic_write_text(level_dir / "stdout.txt", "", 0o600)
            atomic_write_text(level_dir / "stderr.txt", "", 0o600)
            atomic_write_text(level_dir / "meta.json",
                              json.dumps(result.to_json(), ensure_ascii=False,
                                         indent=2, sort_keys=True) + "\n", 0o600)
            return result
        pid = proc.pid
        #: 挑战行必须**先于**候选代码执行写入：运行器在 import 候选之前读走整行。
        #: 写完立刻关闭写端（子进程随之在 stdin 上得到 EOF），再交给 communicate
        #: 收尾；把句柄置 None 避免 communicate 重复关闭已关的管道。
        try:
            proc.stdin.write(challenge)
            proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            pass
        finally:
            try:
                if proc.stdin is not None:
                    proc.stdin.close()
            except (BrokenPipeError, OSError, ValueError):
                pass
            proc.stdin = None
        try:
            stdout, stderr = proc.communicate(timeout=self.limits.timeout_s)
            rc = proc.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_process_group(proc)
            try:
                stdout, stderr = proc.communicate(timeout=_KILL_GRACE_S)
            except subprocess.TimeoutExpired:
                # 孙进程自建会话仍持有管道：不再等待（否则超时无上界），
                # 按已读缓冲收尾——证据件缺读入内容本身也是事实。
                for pipe in (proc.stdout, proc.stderr):
                    try:
                        pipe.close()
                    except OSError:
                        pass
                stdout = stderr = ""
            rc = proc.returncode
        duration = time.monotonic() - started

        atomic_write_text(level_dir / "stdout.txt",
                          _truncate_capture(stdout or ""), 0o600)
        atomic_write_text(level_dir / "stderr.txt",
                          _truncate_capture(stderr or ""), 0o600)

        parsed = read_runner_result(result_path, level=level, nonce=nonce)
        forged = detect_forged_marker(stdout, stderr)
        if timed_out:
            status, reason, detail, data = "FAIL", "timeout", (
                f"超过 {self.limits.timeout_s}s 上限"), {}
        elif forged is not None:
            # 运行器已不再写 stdout：任何标记行都只能是候选自己伪造的
            status, reason, detail, data = "FAIL", "contract_error", (
                f"候选伪造出生标记行（已拒绝采信）：{forged}"), {}
        elif parsed is not None:
            status = parsed.get("status")
            reason = parsed.get("reason")
            detail = parsed.get("detail") or ""
            data = parsed.get("data") or {}
        else:
            status = "FAIL"
            reason = _GENERIC_REASON[level]
            if rc in (-9, -11):
                reason = "memory"
            elif rc in (-24, -25):
                reason = "timeout"
            detail = (f"子进程未产出层结果（rc={rc}）："
                      f"{stderr.strip().splitlines()[-1] if stderr.strip() else '无 stderr'}")
            data = {}

        result = LevelResult(
            level=level, status=status, reason=reason, detail=detail,
            duration_s=duration, exit_code=rc, pid=pid, data=dict(data),
            stdout_bytes=len(stdout.encode("utf-8")),
            stderr_bytes=len(stderr.encode("utf-8")))
        atomic_write_text(level_dir / "meta.json",
                          json.dumps(result.to_json(), ensure_ascii=False,
                                     indent=2, sort_keys=True) + "\n", 0o600)
        return result

    # -- 聚合 ---------------------------------------------------------------
    def _aggregate(self, levels: "dict[str, LevelResult]") -> "tuple[str, list[str]]":
        required = self.spec.required_levels()
        problems: "list[str]" = []
        failing = [L for L in LEVELS if required[L] and levels[L].status == "FAIL"]
        not_applicable = [L for L in LEVELS
                          if required[L] and levels[L].status == "NOT_APPLICABLE"]
        infra = [L for L in LEVELS if levels[L].status == "BLOCKED_INFRA"]
        for level in LEVELS:
            if not required[level] and levels[level].status == "FAIL":
                problems.append(
                    f"{level} 非必需层却 FAIL（{levels[level].reason}）——不影响结论，需人看")
        if failing:
            return "NOT_BORN", problems + [f"必需层失败：{failing}"]
        if not_applicable:
            return "NOT_BORN", problems + [
                f"必需层被标 NOT_APPLICABLE（契约与 birth_contract 自相矛盾）：{not_applicable}"]
        if infra:
            return "BLOCKED_INFRA", problems + [f"基础设施阻塞层：{infra}"]
        return "BORN", problems

    # -- 入口 ---------------------------------------------------------------
    def validate(self, out_dir: "str | Path | None" = None) -> BirthResult:
        target = Path(out_dir) if out_dir is not None else (
            Path(tempfile.mkdtemp(prefix="simpraxis-birth-")))
        ensure_private_dir(target)
        levels_dir = ensure_private_dir(target / "levels")
        levels: "dict[str, LevelResult]" = {}
        for level in LEVELS:
            level_dir = ensure_private_dir(levels_dir / level)
            levels[level] = self._run_restricted(level, level_dir)

        hits, scanned = _scan_network_tree(self.workdir)
        audit = {
            "mode": "enforced" if self.network_mode == "none" else "audited_only",
            "network_mode": self.network_mode,
            "static_import_hits": hits,
            "scanned_files": scanned,
            "note": ("容器 --network none：断网被强制执行"
                     if self.network_mode == "none"
                     else "宿主无特权断网：仅做 AST 静态审计（扫 workdir 下全部 "
                          ".py，上限 %d 件），如实标注不假装断网"
                          % _NET_SCAN_MAX_FILES),
        }
        status, problems = self._aggregate(levels)
        result = BirthResult(
            spec=self.spec, levels=levels, birth_status=status,
            problems=problems, limits=self.limits, network_audit=audit,
            workdir=str(self.workdir), ts_iso=_now())
        return result


def _truncate_capture(text: str) -> str:
    """按 UTF-8 字节截到 64 KiB（末尾留一行截断标记）；全量字节数记在 meta。"""
    raw = text.encode("utf-8")
    if len(raw) <= MAX_CAPTURE_BYTES:
        return text
    head = raw[:MAX_CAPTURE_BYTES].decode("utf-8", errors="replace")
    return head + f"\n[simpraxis] 输出已截断：全量 {len(raw)} 字节\n"


def _now() -> str:
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def write_birth(out_dir: "str | Path", result: BirthResult) -> dict:
    """原子写 ``functional_birth.json`` + ``birth.sha256``（0600）。"""
    directory = ensure_private_dir(Path(out_dir))
    body = json.dumps(result.to_json(), ensure_ascii=False, indent=2,
                      sort_keys=True) + "\n"
    path = atomic_write_text(directory / BIRTH_NAME, body, 0o600)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    sha_path = atomic_write_text(directory / BIRTH_SHA_NAME,
                                 f"{digest}  {BIRTH_NAME}\n", 0o600)
    return {"birth_path": str(path), "sha256_path": str(sha_path),
            "birth_sha256": digest, "birth_status": result.birth_status}


def load_birth(path: "str | Path") -> dict:
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    if doc.get("schema") != BIRTH_SCHEMA:
        raise BirthError(f"{path}: schema={doc.get('schema')!r} 非 {BIRTH_SCHEMA}")
    if doc.get("task_success") is not None:
        raise BirthError(f"{path}: task_success 必须恒为 None（Birth ≠ Task Success）")
    if doc.get("birth_status") not in ("BORN", "NOT_BORN", "BLOCKED_INFRA"):
        raise BirthError(f"{path}: 非法 birth_status {doc.get('birth_status')!r}")
    for level in LEVELS:
        entry = doc.get("levels", {}).get(level)
        if not isinstance(entry, dict):
            raise BirthError(f"{path}: 缺层 {level}")
        if entry.get("status") not in LEVEL_STATUSES:
            raise BirthError(f"{path}: {level} 非法状态 {entry.get('status')!r}")
    return doc
