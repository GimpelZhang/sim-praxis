"""Phase 2.8.1 T5：预注册件的 LOCK 构造 / 校验 / 自锚（计划 §9）。

**为什么要有 LOCK**：预注册的全部意义在于「结果出来之后不能再改设计」。
一个 diff 只能证明「现在和某个提交不同」，证明不了「付费之前它就是这些字」。
LOCK 把付费前的那一刻钉成可复算的一枚 JSON：8 件预注册件逐件 sha256、
``trial_order.json`` 的 sha256、三任务 ``criteria_lock``、manifest 的聚合
锁、两臂 ``config_digest``、预算五数、以及写 LOCK 时的账本行数快照。
任一字节变动，:func:`verify_lock` 都能指名道姓地报出是哪一件（STOP-13）。

**单一真值（计划 §9.3 的裁定）**：8 件预注册件与 ``LOCK.json`` 的真值在**引擎仓**
``benchmarks/research_engineering_v1/experiments/roadmap_phase1/``（入 git：
可 review、可复算、历史不可改）。数据根 ``$ROOT/pre_registration/`` 是
``--write-lock`` 写盘时从仓内**逐字复制**出来的只读镜像（0600/0700），
用于运行期不受仓内后续改动影响。:func:`verify_lock` **先验镜像与仓内逐件
相等**，再验 LOCK 内记录的 sha256——两层任一不等即报错。

**写 LOCK 的前置（硬门）**：账本必须在 0 行 ``event=="call"`` 时写 LOCK。
先花一次钱再补 LOCK，预注册就变成了事后叙事；STOP-13 的整个意义依赖这个顺序
（:class:`LockRefused` 硬拒）。

**0 付费**：本模块只做本地文件哈希与 JSON 读写，**不 import** ``router`` /
``requests`` / ``httpx`` / ``urllib.request``，账本行数直接读 JSONL。计划 §9.9
的静态断言守的就是这条。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Mapping

import yaml

from orchestrator.benchmark import task_spec as ts
from orchestrator.common.fsutil import ensure_private_dir

from . import arm_adapter, constants

__all__ = (
    "LOCK_FILE",
    "LOCK_SCHEMA",
    "ORDER_FILE",
    "PREREG_FILES",
    "PREREG_RELPATH",
    "STOP_IDS",
    "ConsentError",
    "LockBroken",
    "LockRefused",
    "RegistrationError",
    "build_lock",
    "canonical_json",
    "collect_prereg_hashes",
    "compute_trial_order",
    "criteria_locks",
    "derive_requirements",
    "file_sha256",
    "head_commit",
    "ledger_counts",
    "lock_anchor",
    "main",
    "order_bytes",
    "order_sha256",
    "prereg_root",
    "verify_consent",
    "verify_lock",
    "write_consent",
    "write_lock",
    "write_order",
    "write_requirements",
)

#: 仓内预注册目录（相对引擎仓根；唯一真值所在）
PREREG_RELPATH = Path("benchmarks/research_engineering_v1/experiments/roadmap_phase1")
#: 8 件预注册件（顺序即人读顺序；集合即 AC-P281-5-01 的「齐备」判据）
PREREG_FILES = (
    "experiment.yaml",
    "arms.yaml",
    "tasks.yaml",
    "budget.yaml",
    "statistics.yaml",
    "stopping.yaml",
    "randomization.json",
    "runtime_requirement.yaml",
)
ORDER_FILE = "trial_order.json"
LOCK_FILE = "LOCK.json"
LOCK_SCHEMA = "simpraxis.phase281_lock.v1"
REQ_SCHEMA = "simpraxis.phase281_runtime_requirement.v1"
ORDER_SCHEMA_NOTE = "simpraxis.phase281_trial_order.v1"
#: §15.2 的 17 条 STOP（AC-P281-5-12 的精确集合）
STOP_IDS = tuple(f"STOP-{i:02d}" for i in range(1, 18))

_STRICT_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class RegistrationError(RuntimeError):
    """预注册面不可用（缺件 / 解析失败 / 环境不可复算）。"""


class LockRefused(RegistrationError):
    """写 LOCK 的前置不成立（账本已有实呼 / criteria 现算不符 / 缺件）。"""


class LockBroken(RegistrationError):
    """已存在的 LOCK 与现算不一致（STOP-13：本批作废，另立新实验）。"""


class ConsentError(RegistrationError):
    """LOCK 批准件不合规（缺失 / sha 不符 / approved_by 不是 user / 时间非严格 Z）。"""


# ---------------------------------------------------------------------------
# 路径与哈希
# ---------------------------------------------------------------------------

def prereg_root(root: "str | Path | None" = None) -> Path:
    """预注册目录（缺省 = 引擎仓内的唯一真值目录）。"""
    if root is not None:
        return Path(root)
    return _repo_root() / PREREG_RELPATH


def _repo_root() -> Path:
    from orchestrator.benchmark import intake as _intake

    return Path(_intake.REPO_ROOT)


def file_sha256(path: "str | Path") -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def canonical_json(doc: Any) -> str:
    """与 ``arm_adapter.canonical_json`` 同口径（键序无关、无多余空白）。"""
    return json.dumps(doc, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def _load_doc(path: Path) -> Any:
    if not path.is_file():
        raise RegistrationError(f"缺件：{path}")
    text = path.read_text(encoding="utf-8")
    try:
        if path.suffix in (".yaml", ".yml"):
            return yaml.safe_load(text)
        return json.loads(text)
    except (yaml.YAMLError, json.JSONDecodeError) as exc:
        raise RegistrationError(f"解析失败：{path}: {exc}") from exc


def collect_prereg_hashes(root: "str | Path | None" = None) -> "dict[str, str]":
    """8 件逐件 sha256（缺件即抛，不返回半张表）。"""
    base = prereg_root(root)
    out: "dict[str, str]" = {}
    missing: "list[str]" = []
    for name in PREREG_FILES:
        path = base / name
        if not path.is_file():
            missing.append(name)
            continue
        # 附加：8 件必须可解析（AC-P281-5-01 的第二半）
        _load_doc(path)
        out[name] = file_sha256(path)
    if missing:
        raise RegistrationError(f"预注册件缺件：{missing}（目录 {base}）")
    return out


# ---------------------------------------------------------------------------
# 判据锁与臂摘要（现算，唯一来源）
# ---------------------------------------------------------------------------

def _bench_root(root: "str | Path | None" = None) -> Path:
    """``benchmarks/research_engineering_v1``（``<bench>/experiments/<exp>`` 上两级）。"""
    return prereg_root(root).parents[1]


def _criteria_path(task_id: str, root: "str | Path | None") -> Path:
    return _bench_root(root) / "criteria" / f"{task_id}.criteria.json"


def criteria_locks(root: "str | Path | None" = None) -> "dict[str, str]":
    """三任务 ``criteria_lock_sha256``（仓内 ``criteria/*.json`` 现算）。"""
    out: "dict[str, str]" = {}
    for task_id in constants.TASK_IDS:
        doc = _load_doc(_criteria_path(task_id, root))
        out[task_id] = ts.criteria_lock_sha256(doc)
    return out


def manifest_criteria_lock(root: "str | Path | None" = None) -> str:
    """manifest 的聚合锁（``criteria_aggregate_sha256``，按 task_id 排序拼接）。"""
    return ts.criteria_aggregate_sha256(criteria_locks(root))


def declared_manifest_lock(root: "str | Path | None" = None) -> "str | None":
    """仓内 ``manifest.yaml`` 声明的 ``criteria_lock``（用于「现算 == 声明」）。"""
    doc = _load_doc(_bench_root(root) / "manifest.yaml")
    value = doc.get("criteria_lock") if isinstance(doc, dict) else None
    return value if isinstance(value, str) else None


def arm_digests() -> "dict[str, str]":
    """两臂 ``config_digest``（``ARMS`` 现算，别处不得另算一份）。"""
    return {arm: arm_adapter.arm_config_digest(arm) for arm in constants.ARMS}


# ---------------------------------------------------------------------------
# 随机化：可复算的 trial_order
# ---------------------------------------------------------------------------

def _seed(task_id: str, pair_index: int) -> str:
    """§9.4 逐字：``sha256(f"{EXPERIMENT_ID}|{task_id}|{pair_index}")``。"""
    raw = f"{constants.EXPERIMENT_ID}|{task_id}|{pair_index}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def pair_key(task_id: str, pair_index: int) -> str:
    """``<task_id>|rep<k>``（与 ``trial_id`` 的 rep 段同写法）。"""
    return f"{task_id}|rep{pair_index}"


def compute_trial_order() -> "dict[str, list[str]]":
    """由种子复算 9 对执行顺序（同输入同输出，逐字节确定）。"""
    order: "dict[str, list[str]]" = {}
    for task_id in constants.TASK_IDS:
        for pair_index in range(constants.REPS_TOTAL):
            seed = _seed(task_id, pair_index)
            arms = list(constants.ARMS)
            arms.sort(key=lambda arm: hashlib.sha256(
                f"{seed}|{arm}".encode("utf-8")).hexdigest())
            order[pair_key(task_id, pair_index)] = arms
    return order


def order_bytes(order: "Mapping[str, list[str]] | None" = None) -> bytes:
    """``trial_order.json`` 的**唯一序列化**（sha256 就吃这串字节）。"""
    payload = compute_trial_order() if order is None else dict(order)
    return (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
            + "\n").encode("utf-8")


def order_sha256(order: "Mapping[str, list[str]] | None" = None) -> str:
    return hashlib.sha256(order_bytes(order)).hexdigest()


def write_order(path: "str | Path | None" = None,
                root: "str | Path | None" = None) -> Path:
    """写 ``trial_order.json``（幂等：内容恒等于复算值）。"""
    target = Path(path) if path is not None else prereg_root(root) / ORDER_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    _write_bytes(target, order_bytes())
    return target


# ---------------------------------------------------------------------------
# 运行时要求（派生件，不手写）
# ---------------------------------------------------------------------------

def _plain(value: Any) -> Any:
    """``MappingProxyType`` / 元组 → 普通 dict / list（只为可 YAML 化，不改值）。"""
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def derive_requirements(root: "str | Path | None" = None) -> dict:
    """从 ``task.yaml.runtime`` 派生运行时要求（**逐键相等**，杜绝手写漂移）。

    Phase 2.8.0 的 W2-03 教训：手写的运行时期望表在 schema 演进后必然分叉，
    而分叉表现为「候选被判 BLOCKED_INFRA」，看着像能力问题。
    """
    from . import candidate_birth

    tasks: "dict[str, dict]" = {}
    for row in constants.TASK_ORDER:
        task_id = row["task_id"]
        spec = candidate_birth.load_task(task_id)
        tasks[task_id] = {
            "runtime": _plain(spec.runtime),
            "harness": {
                "task_no": row["task_no"],
                "short": row["short"],
                "form": row["runtime"],
                # ``spec.task_file`` 是相对 benchmark 根（``tasks/<dir>/task.yaml``），
                # 这里补上前缀写成仓根相对路径，便于人在仓内直接定位。
                "task_file": str(Path("benchmarks/research_engineering_v1")
                                 / Path(spec.task_file)),
                "probe_relpath": "analysis/run_form_probe.json",
            },
        }
    return {
        "schema": REQ_SCHEMA,
        "experiment_id": constants.EXPERIMENT_ID,
        "derived_by": "python -m orchestrator.benchmark.phase281.registration "
                      "--derive-requirements",
        "source": "benchmarks/research_engineering_v1/tasks/*/task.yaml#runtime",
        "note": "手写本件 = 伪造预注册；它必须是派生输出。",
        "tasks": tasks,
    }


def requirements_bytes(doc: "dict | None" = None) -> bytes:
    payload = derive_requirements() if doc is None else doc
    return yaml.safe_dump(payload, allow_unicode=True, sort_keys=False,
                          default_flow_style=False,
                          width=100).encode("utf-8")


def write_requirements(path: "str | Path | None" = None,
                       root: "str | Path | None" = None) -> Path:
    target = (Path(path) if path is not None
              else prereg_root(root) / "runtime_requirement.yaml")
    target.parent.mkdir(parents=True, exist_ok=True)
    _write_bytes(target, requirements_bytes())
    return target


# ---------------------------------------------------------------------------
# 账本快照（0 行 call 是写 LOCK 的硬前置）
# ---------------------------------------------------------------------------

def _ledger_path() -> Path:
    from . import paths

    return paths.ledger_path()


def ledger_counts(ledger: "str | Path | None" = None) -> "dict[str, int]":
    """``{"calls": n, "total": n}``——按 ``quota_ledger`` 的口径数 ``event=="call"``。

    直接读 JSONL 而不 import ``quota_ledger``：后者会拽上 ``router``（暂停态
    复用），而本模块必须保持「零网络面」可静态断言（计划 §9.9）。语义仍是
    同一口径：整数 seq 且 ``event=="call"`` 的行。
    """
    path = Path(ledger) if ledger is not None else _ledger_path()
    if not path.is_file():
        return {"calls": 0, "total": 0}
    calls = 0
    total = 0
    for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:  # fail-closed：账本坏了不许当空账
            raise RegistrationError(
                f"账本第 {lineno} 行不是合法 JSON：{path}: {exc}") from exc
        total += 1
        if isinstance(row, dict) and row.get("event") == "call":
            calls += 1
    return {"calls": calls, "total": total}


# ---------------------------------------------------------------------------
# LOCK 构造 / 自锚
# ---------------------------------------------------------------------------

def head_commit(repo: "str | Path | None" = None) -> str:
    """写 LOCK 时的 HEAD（40 hex）。子进程读 git，不写盘、不触网。"""
    cwd = str(repo) if repo is not None else str(_repo_root())
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=cwd,
                             capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RegistrationError(f"读不到 git HEAD：{exc}") from exc
    sha = (out.stdout or "").strip()
    if out.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise RegistrationError(f"git rev-parse HEAD 失败：{sha!r} "
                                f"{(out.stderr or '').strip()}")
    return sha


def lock_anchor(doc: Mapping[str, Any]) -> str:
    """自锚：``lock_sha256`` 置空 → 规范化 JSON → sha256（§5.5 同算法）。"""
    body = dict(doc)
    body["lock_sha256"] = ""
    return hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()


def build_lock(*, root: "str | Path | None" = None,
               git_commit: "str | None" = None,
               ledger: "str | Path | None" = None) -> dict:
    """构造 LOCK 文档（**不落盘**；落盘走 :func:`write_lock`）。"""
    prereg = collect_prereg_hashes(root)
    order_path = prereg_root(root) / ORDER_FILE
    if not order_path.is_file():
        raise LockRefused(
            f"缺 {order_path}：先跑 --write-order（trial_order 由种子复算，不手写）")
    on_disk_order = order_path.read_bytes()
    if on_disk_order != order_bytes():
        raise LockRefused(
            f"{order_path} 与复算值不一致：先重跑 --write-order，"
            "或（若确实要改随机化规则）另立新实验")
    criteria = criteria_locks(root)
    aggregate = ts.criteria_aggregate_sha256(criteria)
    declared = declared_manifest_lock(root)
    if declared != aggregate:
        raise LockRefused(
            f"criteria 现算不符：manifest.yaml 声明 {declared!r}，重算 {aggregate!r}")
    doc = {
        "schema": LOCK_SCHEMA,
        "experiment_id": constants.EXPERIMENT_ID,
        "git_commit": (git_commit if git_commit is not None
                       else head_commit()),
        "preregistration": prereg,
        "trial_order_sha256": hashlib.sha256(on_disk_order).hexdigest(),
        "criteria": criteria,
        "manifest_criteria_lock": aggregate,
        "arms": arm_digests(),
        "budget": {
            "pilot": constants.PILOT_BUDGET,
            "confirmatory": constants.CONFIRMATORY_BUDGET,
            "hard_cap": constants.HARD_CAP,
            "per_task_cap": constants.TASK_CAP,
            "call_cap_per_trial": constants.CALL_CAP_PER_TRIAL,
        },
        "ledger_rows_at_lock": ledger_counts(ledger),
        "lock_sha256": "",
    }
    doc["lock_sha256"] = lock_anchor(doc)
    return doc


def lock_bytes(doc: Mapping[str, Any]) -> bytes:
    """LOCK 的唯一序列化（仓内真值、数据根镜像、批准件 sha 都吃这串字节）。"""
    return (json.dumps(dict(doc), ensure_ascii=False, sort_keys=True, indent=2)
            + "\n").encode("utf-8")


def _write_bytes(path: Path, data: bytes) -> None:
    """原子写 0600（与 ``fsutil.write_private`` 同口径，但不改名换行）。"""
    tmp = path.with_name(path.name + ".tmp")
    if path.parent != tmp.parent:  # pragma: no cover - 防御性
        raise RegistrationError(f"临时件越目录：{tmp}")
    ensure_private_dir(path.parent)
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# 写 LOCK（含数据根只读镜像）
# ---------------------------------------------------------------------------

def mirror_files() -> "tuple[str, ...]":
    """镜像件族：8 件 + ``trial_order.json`` + ``LOCK.json``。"""
    return PREREG_FILES + (ORDER_FILE, LOCK_FILE)


def write_lock(out_path: "str | Path | None" = None, *,
               root: "str | Path | None" = None,
               mirror_dir: "str | Path | None" = None,
               git_commit: "str | None" = None,
               ledger: "str | Path | None" = None) -> dict:
    """写 LOCK：先过前置（0 行 call），再写仓内真值 + 数据根镜像。

    ``out_path`` 是计划 §9.5 命令行里给的那个路径（缺省＝数据根镜像位置）。
    §9.3 的单一真值裁定使**仓内** ``LOCK.json`` 为准，故这里无论 ``out_path``
    指哪，都会在仓内写一份同字节副本；``out_path`` 与仓内同名时不会写两次。
    """
    counts = ledger_counts(ledger)
    if counts["calls"] != 0:
        raise LockRefused(
            f"账本已有 {counts['calls']} 行 event=call（总行 {counts['total']}）："
            "预注册必须在第一次付费调用之前冻结。已花的钱不能靠补 LOCK 变成"
            "「一开始就这么设计的」（STOP-13）")
    doc = build_lock(root=root, git_commit=git_commit, ledger=ledger)
    data = lock_bytes(doc)
    repo_lock = prereg_root(root) / LOCK_FILE
    _write_bytes(repo_lock, data)
    if out_path is not None:
        out = Path(out_path)
        if out.resolve(strict=False) != repo_lock.resolve(strict=False):
            _write_bytes(out, data)
        else:
            mirror_dir = mirror_dir or out.parent
    if mirror_dir is not None:
        _write_mirror(Path(mirror_dir), data, root=root)
    return doc


def _write_mirror(target: Path, lock_data: bytes, *,
                  root: "str | Path | None" = None) -> None:
    """把仓内真值逐字复制到数据根（0600 文件 / 0700 目录）。"""
    base = prereg_root(root)
    ensure_private_dir(target)
    for name in PREREG_FILES + (ORDER_FILE,):
        _write_bytes(target / name, (base / name).read_bytes())
    _write_bytes(target / LOCK_FILE, lock_data)


# ---------------------------------------------------------------------------
# 校验（五条断言 + 镜像对账）
# ---------------------------------------------------------------------------

def verify_lock(lock_path: "str | Path | None" = None, *,
                root: "str | Path | None" = None,
                mirror_dir: "str | Path | None" = None) -> "list[str]":
    """五条断言 + 镜像对账；返回问题列表（空 = 通过）。

    AC-P281-5-03 要求「未改动件返回 ``[]``」——所以这里**不抛**，把每个问题
    写成一行可读文本（含件名），由调用方（:func:`verify_or_raise` / CLI）决定
    是抛 :class:`LockBroken` 还是打印。
    """
    base = prereg_root(root)
    path = Path(lock_path) if lock_path is not None else base / LOCK_FILE
    problems: "list[str]" = []

    if not path.is_file():
        return [f"LOCK 不在场：{path}"]
    try:
        doc = _load_doc(path)
    except RegistrationError as exc:
        return [str(exc)]
    if not isinstance(doc, dict):
        return [f"LOCK 顶层必须是对象：{path}"]
    if doc.get("schema") != LOCK_SCHEMA:
        problems.append(f"schema 不符：{doc.get('schema')!r} != {LOCK_SCHEMA!r}")
    if doc.get("experiment_id") != constants.EXPERIMENT_ID:
        problems.append(
            f"experiment_id 不符：{doc.get('experiment_id')!r} "
            f"!= {constants.EXPERIMENT_ID!r}")
    if not re.fullmatch(r"[0-9a-f]{40}",
                        str(doc.get("git_commit") or "")):
        problems.append(f"git_commit 不是 40 hex：{doc.get('git_commit')!r}")

    # ---- 单一真值：仓内 LOCK 必须与待验件逐字节相同 ----
    repo_lock = base / LOCK_FILE
    if repo_lock.is_file() and repo_lock.resolve(strict=False) != path.resolve(
            strict=False):
        if repo_lock.read_bytes() != path.read_bytes():
            problems.append(
                f"LOCK 与仓内真值不一致：{path} vs {repo_lock}")

    # ---- 镜像对账（§9.3 的第一层）----
    if mirror_dir is not None:
        mirror = Path(mirror_dir)
        if mirror.is_dir():
            for name in PREREG_FILES + (ORDER_FILE,):
                src = base / name
                dst = mirror / name
                if not dst.is_file():
                    problems.append(f"镜像缺件：{name}")
                    continue
                if not src.is_file():
                    problems.append(f"仓内缺件：{name}")
                    continue
                if file_sha256(dst) != file_sha256(src):
                    problems.append(f"镜像漂移：{name}")
            mirror_lock = mirror / LOCK_FILE
            if not mirror_lock.is_file():
                problems.append(f"镜像缺件：{LOCK_FILE}")
            elif mirror_lock.read_bytes() != path.read_bytes():
                problems.append(f"镜像漂移：{LOCK_FILE}")

    # ---- 断言 1：8 件逐件 sha256 ----
    recorded = doc.get("preregistration")
    if not isinstance(recorded, dict):
        problems.append("preregistration 缺失或不是对象")
        recorded = {}
    for name in PREREG_FILES:
        src = base / name
        if not src.is_file():
            problems.append(f"预注册件缺失：{name}")
            continue
        actual = file_sha256(src)
        if recorded.get(name) != actual:
            problems.append(
                f"预注册件被改：{name}（LOCK {recorded.get(name)!r}，现算 {actual}）")
    for name in sorted(set(recorded) - set(PREREG_FILES)):
        problems.append(f"LOCK 记录了未登记的件：{name}")

    # ---- 断言 2：trial_order_sha256 ----
    order_path = base / ORDER_FILE
    if not order_path.is_file():
        problems.append(f"trial_order 缺失：{ORDER_FILE}")
    else:
        actual_order = file_sha256(order_path)
        if doc.get("trial_order_sha256") != actual_order:
            problems.append(
                f"trial_order_sha256 不符：LOCK "
                f"{doc.get('trial_order_sha256')!r}，现算 {actual_order}")
        elif order_path.read_bytes() != order_bytes():
            problems.append(
                f"trial_order_sha256 与复算序列化不符：{ORDER_FILE}")

    # ---- 断言 3：三任务 criteria_lock + manifest 聚合锁 ----
    try:
        criteria = criteria_locks(root)
        aggregate = ts.criteria_aggregate_sha256(criteria)
    except RegistrationError as exc:
        problems.append(f"criteria 现算失败：{exc}")
        criteria, aggregate = {}, None
    recorded_criteria = doc.get("criteria")
    if not isinstance(recorded_criteria, dict):
        problems.append("criteria 缺失或不是对象")
        recorded_criteria = {}
    for task_id in constants.TASK_IDS:
        if recorded_criteria.get(task_id) != criteria.get(task_id):
            problems.append(
                f"criteria_lock 不符：{task_id}（LOCK "
                f"{recorded_criteria.get(task_id)!r}，现算 {criteria.get(task_id)!r}）")
    if aggregate is not None and doc.get("manifest_criteria_lock") != aggregate:
        problems.append(
            f"manifest_criteria_lock 不符：LOCK "
            f"{doc.get('manifest_criteria_lock')!r}，现算 {aggregate!r}")
    declared = declared_manifest_lock(root)
    if aggregate is not None and declared != aggregate:
        problems.append(
            f"manifest.yaml 声明锁与现算不符：{declared!r} vs {aggregate!r}")

    # ---- 断言 4：两臂 config_digest ----
    recorded_arms = doc.get("arms")
    if not isinstance(recorded_arms, dict):
        problems.append("arms 缺失或不是对象")
        recorded_arms = {}
    digests = arm_digests()
    for arm in constants.ARMS:
        if recorded_arms.get(arm) != digests[arm]:
            problems.append(
                f"arm config_digest 不符：{arm}（LOCK "
                f"{recorded_arms.get(arm)!r}，现算 {digests[arm]!r}）")

    # ---- 断言 5：lock_sha256 自锚 ----
    anchor = doc.get("lock_sha256")
    if not isinstance(anchor, str) or not _HEX64.fullmatch(anchor):
        problems.append(f"lock_sha256 不是 64 hex：{anchor!r}")
    else:
        recomputed = lock_anchor(doc)
        if recomputed != anchor:
            problems.append(
                f"lock_sha256 自锚不可复算：记录 {anchor}，重算 {recomputed}")

    # 去重但保序（同一条问题可能由镜像与断言两路命中）
    seen: "set[str]" = set()
    unique: "list[str]" = []
    for item in problems:
        if item not in seen:
            seen.add(item)
            unique.append(item)
    return unique


def verify_or_raise(lock_path: "str | Path | None" = None, *,
                    root: "str | Path | None" = None,
                    mirror_dir: "str | Path | None" = None) -> None:
    problems = verify_lock(lock_path, root=root, mirror_dir=mirror_dir)
    if problems:
        raise LockBroken("LOCK 校验失败（STOP-13）：" + "; ".join(problems))


# ---------------------------------------------------------------------------
# 用户批准件（G0-3 的机判落盘面，AC-P281-5-13）
# ---------------------------------------------------------------------------

def consent_sha_for(lock_path: "str | Path") -> str:
    return file_sha256(lock_path)


def write_consent(path: "str | Path", *, lock_path: "str | Path",
                  user_statement: str,
                  approved_at: "str | None" = None) -> dict:
    """写 LOCK 批准件（**只在用户明确批准之后调用**）。

    ``user_statement`` 必须是用户批准的**逐字原文**——这是「用户批准」这道门
    的唯一可审计载体；没有原件时写这个文件就是 No Fake Approval。
    """
    from orchestrator.common.clock import utc_now_iso_z

    if not isinstance(user_statement, str) or not user_statement.strip():
        raise ConsentError("user_statement 不能为空：批准件必须记录用户逐字原文")
    lock = Path(lock_path)
    if not lock.is_file():
        raise ConsentError(f"LOCK 不在场：{lock}")
    stamp = approved_at or utc_now_iso_z()
    if not _STRICT_Z.fullmatch(stamp):
        raise ConsentError(f"approved_at 必须是严格 Z 的 ISO 时间：{stamp!r}")
    doc = {
        "schema": "simpraxis.phase281_lock_consent.v1",
        "experiment_id": constants.EXPERIMENT_ID,
        "approved_by": "user",
        "approved_at": stamp,
        "lock_sha256": file_sha256(lock),
        "user_statement": user_statement,
    }
    target = Path(path)
    _write_bytes(target, (json.dumps(doc, ensure_ascii=False, sort_keys=True,
                                     indent=2) + "\n").encode("utf-8"))
    return doc


def verify_consent(consent_path: "str | Path",
                   lock_path: "str | Path") -> "list[str]":
    """批准件校验；返回问题列表（空 = 通过）。

    P281-9 的 ``phase281_closeout.py judge`` 必须调本函数：缺件、sha 不符、
    ``approved_by != "user"``、``approved_at`` 非严格 Z 一律拒绝出终判。
    """
    path = Path(consent_path)
    lock = Path(lock_path)
    problems: "list[str]" = []
    if not path.is_file():
        return [f"LOCK 批准件缺失：{path}（用户未批准 LOCK，不得出终判）"]
    if not lock.is_file():
        problems.append(f"LOCK 不在场：{lock}")
    mode = path.stat().st_mode & 0o777
    if mode != 0o600:
        problems.append(f"LOCK 批准件权限必须是 0600：实得 {oct(mode)}")
    try:
        doc = _load_doc(path)
    except RegistrationError as exc:
        return problems + [str(exc)]
    if not isinstance(doc, dict):
        return problems + [f"LOCK 批准件顶层必须是对象：{path}"]
    if doc.get("approved_by") != "user":
        problems.append(f"approved_by 必须是 user：{doc.get('approved_by')!r}")
    approved_at = doc.get("approved_at")
    if not isinstance(approved_at, str) or not _STRICT_Z.fullmatch(approved_at):
        problems.append(f"approved_at 必须是严格 Z 的 ISO 时间：{approved_at!r}")
    if not str(doc.get("user_statement") or "").strip():
        problems.append("user_statement 为空：批准件必须记录用户逐字原文")
    if lock.is_file():
        actual = file_sha256(lock)
        if doc.get("lock_sha256") != actual:
            problems.append(
                f"lock_sha256 不符：批准件 {doc.get('lock_sha256')!r}，"
                f"LOCK 现算 {actual}")
    return problems


# ---------------------------------------------------------------------------
# CLI（计划 §9.5 逐条对应；`python -m orchestrator.benchmark.phase281.registration`）
# ---------------------------------------------------------------------------

def _mirror_default(out_path: "str | None") -> "Path | None":
    """``--out`` 给了就用它的父目录当镜像目录；否则用数据根的 pre_registration/。"""
    from . import paths

    if out_path:
        return Path(out_path).parent
    return paths.pre_registration_dir()


def _print(title: str, payload: Any) -> None:
    print(f"[{title}] {json.dumps(payload, ensure_ascii=False, sort_keys=True)}")


def main(argv: "list[str] | None" = None) -> int:
    """预注册 CLI。**0 付费**：本函数不构造网关、不发网、不 import ``router``。"""
    import argparse
    import sys

    from . import paths

    parser = argparse.ArgumentParser(
        description="Phase 2.8.1 预注册 / LOCK（0 付费）")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--derive-requirements", action="store_true",
                        help="从 task.yaml.runtime 派生 runtime_requirement.yaml")
    action.add_argument("--write-order", action="store_true",
                        help="由种子复算并写 trial_order.json")
    action.add_argument("--write-lock", action="store_true",
                        help="写仓内 LOCK.json + 数据根只读镜像（要求账本 0 行 call）")
    action.add_argument("--verify", action="store_true",
                        help="五条断言 + 镜像对账（幂等，可重复跑）")
    action.add_argument("--verify-consent", action="store_true",
                        help="校验用户 LOCK 批准件")
    action.add_argument("--write-consent", action="store_true",
                        help="写用户 LOCK 批准件（**只在用户明确批准之后**）")
    parser.add_argument("--out", default=None,
                        help="输出路径（--derive-requirements / --write-order / --write-lock）")
    parser.add_argument("--lock", default=None, help="LOCK 路径（--verify 缺省 = 仓内）")
    parser.add_argument("--mirror", default=None,
                        help="数据根镜像目录（缺省 = 数据根 pre_registration/）")
    parser.add_argument("--no-mirror", action="store_true",
                        help="只写仓内真值，不写数据根镜像（测试/演练用）")
    parser.add_argument("--consent", default=None, help="批准件路径")
    parser.add_argument("--user-statement", default=None,
                        help="用户批准的逐字原文（--write-consent 必填）")
    parser.add_argument("--approved-at", default=None,
                        help="批准时刻（严格 Z；缺省 = 现在）")
    args = parser.parse_args(argv)

    # 入口断言（AC-P281-0-13）：Phase 2.8.0 的数据根 env 在本阶段不得设置
    paths.assert_no_benchmark_root_env()

    try:
        if args.derive_requirements:
            target = write_requirements(args.out)
            _print("derive-requirements", {
                "path": str(target),
                "sha256": file_sha256(target),
                "tasks": sorted(derive_requirements()["tasks"]),
            })
            return 0

        if args.write_order:
            target = write_order(args.out)
            _print("write-order", {
                "path": str(target),
                "sha256": file_sha256(target),
                "pairs": len(compute_trial_order()),
            })
            return 0

        if args.write_lock:
            mirror = None if args.no_mirror else (
                Path(args.mirror) if args.mirror else _mirror_default(args.out))
            doc = write_lock(args.out, mirror_dir=mirror)
            _print("write-lock", {
                "repo_lock": str(prereg_root() / LOCK_FILE),
                "out": args.out,
                "mirror": None if mirror is None else str(mirror),
                "lock_sha256": doc["lock_sha256"],
                "git_commit": doc["git_commit"],
                "ledger_rows_at_lock": doc["ledger_rows_at_lock"],
            })
            return 0

        if args.verify:
            problems = verify_lock(
                args.lock, mirror_dir=(None if args.no_mirror else (
                    Path(args.mirror) if args.mirror else None)))
            if problems:
                for item in problems:
                    print(f"[verify] PROBLEM {item}", file=sys.stderr)
                return 2
            _print("verify", {"ok": True, "problems": []})
            return 0

        if args.verify_consent:
            if not args.consent:
                raise SystemExit("--verify-consent 需要 --consent")
            lock = args.lock or str(prereg_root() / LOCK_FILE)
            problems = verify_consent(args.consent, lock)
            if problems:
                for item in problems:
                    print(f"[consent] PROBLEM {item}", file=sys.stderr)
                return 2
            _print("verify-consent", {"ok": True, "problems": []})
            return 0

        if args.write_consent:
            if not args.consent:
                raise SystemExit("--write-consent 需要 --consent")
            lock = args.lock or str(prereg_root() / LOCK_FILE)
            doc = write_consent(args.consent, lock_path=lock,
                                user_statement=args.user_statement or "",
                                approved_at=args.approved_at)
            _print("write-consent", {"path": args.consent,
                                     "lock_sha256": doc["lock_sha256"],
                                     "approved_at": doc["approved_at"]})
            return 0
    except (RegistrationError, SystemExit) as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":  # `python -m orchestrator.benchmark.phase281.registration`
    raise SystemExit(main())
