#!/usr/bin/env python3
"""Phase 2.8.1 冻结采集 CLI（P281-0）。

把「开工当天的事实」固化成 `<root>/intake/` 下十件可核对的快照：

    repository_state.json     HEAD / origin / 分支 / 树状态 / 领先数
    ledger_state.json         五本封存账的 rows + sha256（只读字节读）
    phase281_ledger_seed.json phase281 账的「不在场」事实（不存在或 0 行）
    kb_state.json             KB HEAD、树状态
    models_state.json         stage_models.yaml 的逐 stage model_id + 文件 sha256
                              + ANTHROPIC_BASE_URL 的 **host[:port]**（不含 path）
    criteria_lock.json        三任务 criteria_lock + manifest criteria_lock
    environment_baseline.json 解释器/包版本/GPU/容器/磁盘
    test_baseline.json        冷跑摘要（passed/skipped/deselected/rc）
    table_counts.json         KB 四表行数（只读）
    intake_report.md          人读摘要 + 每项复核命令

两种模式：

* ``--emit``  采集并写盘（全部 0600、目录 0700；不写任何账本行）
* ``--check`` 只核对不落盘：把现算结果与既有 json 逐键比对，输出
  ``[{key, before, now, equal}]``，``rc=0`` iff 全部可比键相等，否则 ``rc=1``。

**为什么复用 intake 的采集器而不是重写**：这七件产物的口径必须与 Phase 2.8.0
逐字相同，重写一遍只会制造第二套口径。intake 的采集函数会把命令日志写到
``orchestrator.benchmark.paths.cmd_logs_dir()``（Phase 2.8.0 数据根）——把日志
写进已结案阶段的数据根是污染，故本脚本在调用期间**把 intake 的日志路径重定向**
到本阶段的 ``<root>/intake/cmd_logs/``（见 ``_intake_logs_redirected``）。
除日志落点外，采集逻辑一行未改。

**凭据纪律**：``ANTHROPIC_BASE_URL`` 只记 ``urlsplit(...).netloc``（host[:port]），
不记 path、不记 query、不记任何 header、不记 token。AC-P281-0-12 会扫全文。

用法::

    python scripts/benchmark/phase281_freeze.py --root /mnt/artifacts/simpraxis/phase281 --emit
    python scripts/benchmark/phase281_freeze.py --root … --check
    python scripts/benchmark/phase281_freeze.py --root … --emit --test-log /tmp/p281_baseline.log
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:  # 允许 `python scripts/...` 裸跑
    sys.path.insert(0, str(REPO_ROOT))

from orchestrator.common.fsutil import (  # noqa: E402
    atomic_write_text,
    ensure_private_dir,
)

GENERATED_BY = "scripts/benchmark/phase281_freeze.py"

#: 十件产物的名字与顺序（AC-P281-0-09 逐件列举，不按数量概括）
ARTIFACT_JSON = (
    "repository_state.json",
    "ledger_state.json",
    "phase281_ledger_seed.json",
    "kb_state.json",
    "models_state.json",
    "criteria_lock.json",
    "environment_baseline.json",
    "test_baseline.json",
    "table_counts.json",
)
ARTIFACT_REPORT = "intake_report.md"
ARTIFACT_NAMES = ARTIFACT_JSON + (ARTIFACT_REPORT,)

#: 每件产物的复核命令（写进 ``intake_report.md`` 的「产物清单」节，AC-P281-0-09）
_RECHECK_CMD = {
    "repository_state.json":
        "`git status --short --branch` + `git rev-parse HEAD origin/main`",
    "ledger_state.json":
        "`wc -l` + `sha256sum` 五本 `…/phaseNN/quota/ledger.jsonl`",
    "phase281_ledger_seed.json":
        "`ls -l /mnt/artifacts/simpraxis/phase281/quota/ledger.jsonl`（应不存在）",
    "kb_state.json":
        "`git -C $KB_ROOT rev-parse HEAD && git -C $KB_ROOT status --porcelain`",
    "models_state.json":
        "`sha256sum orchestrator/gateway/stage_models.yaml`（凭据只记 host[:port]）",
    "criteria_lock.json":
        "`/mnt/venvs/simpraxis/bin/python -m orchestrator.benchmark.cli validate-specs`",
    "environment_baseline.json": "`nvidia-smi --query-gpu=name,memory.used --format=csv`",
    "test_baseline.json":
        "`pytest tests -q -k 'not test_04' -m 'not paid and not kb_integration'` 的摘要行",
    "table_counts.json":
        "`lancedb` 四表 `count_rows()`（本实验不写任何表）",
    "intake_report.md": "本文件（人读摘要）",
}

#: 每件产物里**逐字可比**的键之外，明确登记的易变键（比对时标记为
#: ``equal: null`` 且不计入 rc）——时间戳、命令留痕、GPU/磁盘实时占用、
#: 以及提交前后必然变化的 HEAD 与树状态。
_VOLATILE_COMMON = ("generated_by", "git_head", "ts_iso", "commands")
_VOLATILE_BY_ARTIFACT = {
    "repository_state.json": (
        # 锁内适配（Phase 2.8.1 T0）：「开工时点的 HEAD」是**历史事实**，写进
        # 产物是为了留档（AC-02/F1 用它做锚）；本阶段十个 Task 各提交一笔，
        # HEAD 必然逐 Task 前移，把它留在逐字可比集里会让 `--check` 从第二个
        # Task 起恒 rc=1。HEAD 只许**前进**这一条真正的判据由
        # `test_ac_p281_0_01`（merge-base --is-ancestor）承担，不靠这里。
        "head",
        # *锁内适配*（Phase 2.8.1 P281-9，2026-11-07 推送窗口）：``origin_main``
        # 开工时点记的是 ``82a1427``（2026-11-06 窗口落点）。用户在 P281-7 复核
        # 裁决里逐字指示「四：推」，T7 评审修复批已推送至 origin/main（``065231b``），
        # 这一键必然逐字不等。与同处 ``head`` 的处理**同一条理由**：开工快照是
        # 历史事实，留档即可；「origin/main 只许沿人工推送窗口前进」这条真判据由
        # ``test_ac_p281_0_01`` 承担（它按 ``AUTHORIZED_PUSH_HEADS`` 精确集合放行，
        # 集合外仍红），不靠这里。
        "origin_main",
        "status_short_branch", "status_porcelain", "status_porcelain_raw",
        "status_porcelain_excluded", "status_porcelain_excluded_count",
        "ahead", "diff_stat",
    ),
    "environment_baseline.json": (
        "python_V", "gpu_used_mib", "df_mnt_available_gib", "df_mnt_raw",
    ),
    "phase281_ledger_seed.json": (
        # 锁内适配（Phase 2.8.1 T0）：本条路径是 ``--root`` 派生的恒等式
        # （``<root>/quota/ledger.jsonl``），换一个根核对时必然逐字不等
        # （AC-P281-0-10 把整份 intake 拷到 tmp 根再跑 ``--check``），
        # 它不是事实而是写法；真正的事实 ``exists`` / ``rows`` / ``bytes`` /
        # ``sha256`` 仍逐字可比。
        "phase281_ledger.path",
    ),
    "test_baseline.json": (
        "raw_log", "raw_log_sha256", "duration_s", "summary_line",
    ),
    "kb_state.json": ("status_short_branch", "status_porcelain", "diff_stat"),
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: "str | Path") -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _git_head() -> str:
    try:
        proc = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(REPO_ROOT),
                              capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):  # pragma: no cover - 防御性
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _envelope(payload: dict, commands: "list[dict]") -> dict:
    return {
        "generated_by": GENERATED_BY,
        "git_head": _git_head(),
        "ts_iso": _now(),
        **payload,
        "commands": commands,
    }


@contextmanager
def _intake_logs_redirected(log_dir: Path):
    """把 intake 的命令日志落点临时改指本阶段 ``intake/cmd_logs/``。

    intake 的七个采集器把每条命令的完整输出写进
    ``orchestrator.benchmark.paths.cmd_logs_dir()``——那是 **Phase 2.8.0 的
    数据根**。在已结案阶段的数据根里追加日志属于污染（其证据包不动点判据
    恰好只校验包内件，不会因此变红，所以这是一种「不会报错但确实越界」的
    写入，必须主动避免）。重定向只改**落点**，不改任何采集逻辑；退出时
    无条件还原，异常路径也不会把 intake 留在被改状态。
    """
    from orchestrator.benchmark import intake

    ensure_private_dir(log_dir)
    original = intake._log_path
    intake._log_path = lambda name: log_dir / name  # type: ignore[assignment]
    try:
        yield
    finally:
        intake._log_path = original  # type: ignore[assignment]


# --------------------------------------------------------------------------
# 采集（七件复用 intake；三件本阶段自算）
# --------------------------------------------------------------------------

def _self_phase281_ledger_state() -> dict:
    """phase281 账的「不在场」事实——P281-0 结束时必须是「不存在或 0 行」。

    这个判断**不能**用 ``QuotaLedger.phase281()``：构造器会
    ``ensure_private_dir`` 出 quota 目录，把「未开场」变成「空目录已存在」。
    这里只做只读探测。
    """
    from orchestrator.common import runtime_paths as rp

    ledger = rp.phase281_root() / "quota" / "ledger.jsonl"
    if not ledger.is_file():
        return {"path": str(ledger), "exists": False, "rows": 0,
                "bytes": 0, "sha256": None}
    data = ledger.read_bytes()
    return {
        "path": str(ledger),
        "exists": True,
        "rows": data.count(b"\n"),
        "bytes": len(data),
        "sha256": _sha256_bytes(data),
    }


def _models_state() -> dict:
    """stage_models.yaml 的三个 stage + 文件 sha256 + 端点主机名（不含 path）。"""
    import yaml

    cfg = REPO_ROOT / "orchestrator" / "gateway" / "stage_models.yaml"
    doc = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
    stages = doc.get("stage_models") or {}
    models = {}
    for stage in ("macro_ideation", "coding_debug", "visual_rollout"):
        item = stages.get(stage) or {}
        models[stage] = {
            "model_id": item.get("model_id"),
            "capabilities": list(item.get("capabilities") or []),
        }
    base_env = (doc.get("meta") or {}).get("base_url_env") or "ANTHROPIC_BASE_URL"
    netloc = ""
    raw_url = os.environ.get(base_env)
    if raw_url:
        # 只取 host[:port]；path/query/凭据一律不落盘（AC-P281-0-12）
        netloc = urlsplit(raw_url).netloc
    return _envelope({
        "stage_models_file": str(cfg),
        "stage_models_sha256": _sha256_file(cfg),
        "base_url_env": base_env,
        "base_url_host": netloc,
        "models": models,
        "paid_calls_made_by_this_script": 0,
    }, [])


def _criteria_lock_state() -> dict:
    """三任务 criteria_lock + manifest criteria_lock（**只读**）。

    锁内适配（Phase 2.8.1 T0）：计划 §4.5.3 写作「`validate-specs` 输出解析」，
    但 `cmd_validate_specs` 会把 ``validation_report.json`` /
    ``registry_snapshot.json`` / ``schemas_sha256.json`` 三件**写进
    Phase 2.8.0 的数据根**（``paths.specs_dir()``）。冻结采集器在 `--check`
    模式下必须「不写任何文件」，在 `--emit` 模式下也没有理由为一个只读快照
    去改写已结案阶段的数据根。故此处：
    1. 用 ``BenchmarkRegistry`` 直接算锁值（与 validate-specs 同源同算法，
       ``_specs_report()`` 用的就是这两个方法）；
    2. **只读解析**已存在的 ``specs/validation_report.json``，把它的
       ``ok`` / ``problems`` / ``criteria_lock`` 逐字记下来供人工对账。
    """
    from orchestrator.benchmark import paths as p280
    from orchestrator.benchmark.registry import load_registry

    reg = load_registry()
    locks = reg.criteria_locks()
    payload = {
        "criteria_dir": str(REPO_ROOT / "benchmarks" / "research_engineering_v1"
                            / "criteria"),
        "task_criteria_locks": dict(sorted(locks.items())),
        "manifest_criteria_lock": reg.criteria_aggregate_sha256(),
        "manifest_sha256": reg.manifest_sha256(),
    }
    try:
        # read_only=True：``specs_dir()`` 会 mkdir，本采集器只许读
        report_path = Path(p280.bench_root(read_only=True)) / "specs" \
            / "validation_report.json"
    except Exception:  # noqa: BLE001 - 2.8.0 数据根不在场即「无件可读」
        report_path = None
    if report_path is not None and report_path.is_file():
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:  # pragma: no cover - 防御性
            payload["validate_specs"] = {"path": str(report_path),
                                         "parse_error": str(exc)}
        else:
            payload["validate_specs"] = {
                "path": str(report_path),
                "ok": bool(report.get("ok")),
                "problems": list(report.get("problems") or []),
                "criteria_lock": report.get("criteria_lock"),
                "manifest_sha256": report.get("manifest_sha256"),
                "task_ids": [row.get("task_id")
                             for row in (report.get("tasks") or [])],
            }
    else:
        payload["validate_specs"] = {
            "path": str(report_path) if report_path is not None else None,
            "present": False}
    return _envelope(payload, [])


def collect(root: Path, *, test_log: "str | Path | None",
            log_dir: Path) -> "dict[str, dict]":
    """采集十件产物（不写盘）；返回 ``{文件名: payload}``。

    ``log_dir`` 是 intake 命令日志的落点：``--emit`` 传
    ``<root>/intake/cmd_logs``（留痕），``--check`` 传临时目录（**不写任何
    持久文件**，见计划「--check 不写任何文件」）。
    """
    from orchestrator.benchmark import intake

    artifacts: dict[str, dict] = {}
    with _intake_logs_redirected(log_dir):
        artifacts["repository_state.json"] = intake.repository_state()
        artifacts["ledger_state.json"] = intake.sealed_ledger_sha256()
        artifacts["kb_state.json"] = intake.kb_state()
        artifacts["environment_baseline.json"] = intake.environment_baseline()
        artifacts["table_counts.json"] = intake.table_counts()

    artifacts["phase281_ledger_seed.json"] = _envelope(
        {"phase281_ledger": _self_phase281_ledger_state(),
         "note": "P281-0 结束时 phase281 账必须是「不存在或 0 行」"}, [])
    artifacts["models_state.json"] = _models_state()
    artifacts["criteria_lock.json"] = _criteria_lock_state()

    if test_log is None:
        artifacts["test_baseline.json"] = _envelope(
            {"deferred": True,
             "note": "本次未传 --test-log；--check 会把本件标为不可比"},
            [])
    else:
        raw = intake.test_baseline(test_log)
        artifacts["test_baseline.json"] = _envelope(
            {k: v for k, v in raw.items()
             if k not in ("generated_by", "git_head", "ts_iso", "commands")},
            raw.get("commands") or [])
    return artifacts


# --------------------------------------------------------------------------
# 报告
# --------------------------------------------------------------------------

def render_report(artifacts: "dict[str, dict]", root: Path) -> str:
    import platform

    repo = artifacts.get("repository_state.json", {})
    led = artifacts.get("ledger_state.json", {}).get("ledgers", {})
    seed = artifacts.get("phase281_ledger_seed.json", {}).get(
        "phase281_ledger", {})
    kb = artifacts.get("kb_state.json", {})
    models = artifacts.get("models_state.json", {})
    crit = artifacts.get("criteria_lock.json", {})
    env = artifacts.get("environment_baseline.json", {})
    tbl = artifacts.get("table_counts.json", {}).get("tables", {})
    tb = artifacts.get("test_baseline.json", {})

    lines = [
        "# Phase 2.8.1 冻结采集报告（P281-0）",
        "",
        f"- 数据根：`{root}`",
        f"- git HEAD：`{repo.get('head', '')}`",
        f"- origin/main：`{repo.get('origin_main', '')}`",
        f"- 采集时刻：{artifacts.get('repository_state.json', {}).get('ts_iso', '')}",
        f"- 平台：{platform.platform()}",
        "",
        # AC-P281-0-09 要求报告**点名**每一件产物（不许按数量概括），且
        # 计划 §4.5.3 要求「人读摘要 + 每项复核命令」——本节即产物清单。
        "## 0. 产物清单（十件）与复核命令",
        "",
        "| 件 | 复核 |",
        "|---|---|",
    ]
    for name in ARTIFACT_NAMES:
        lines.append(f"| `{name}` | {_RECHECK_CMD.get(name, '（见下方各节）')} |")
    lines += [
        "",
        "## 1. 五本封存账（只读）",
        "",
        "| 期 | 行数 | sha256 |",
        "|---|---:|---|",
    ]
    for phase in ("phase1", "phase2", "phase25", "phase26", "phase27"):
        item = led.get(phase) or {}
        lines.append(f"| {phase} | {item.get('rows', '?')} | `{item.get('sha256', '?')}` |")
    lines += [
        "",
        "复核：`wc -l /mnt/artifacts/simpraxis/phaseNN/quota/ledger.jsonl` + `sha256sum`",
        "",
        "## 2. phase281 新账（开账前）",
        "",
        f"- 路径：`{seed.get('path', '')}`",
        f"- 是否存在：**{seed.get('exists')}**；行数：{seed.get('rows')}",
        "",
        "P281-0 结束时的硬性要求：**不存在或 0 行**。出现任何 `event=\"call\"` 行即 STOP。",
        "",
        "## 3. KB 仓（只读）",
        "",
        f"- 根：`{kb.get('kb_root', '')}`",
        f"- HEAD：`{kb.get('head', '')}`",
        "",
        "## 4. 模型（冻结，硬规则 1）",
        "",
        f"- `stage_models.yaml` sha256：`{models.get('stage_models_sha256', '')}`",
        f"- 端点主机：`{models.get('base_url_host', '')}`（仅 host[:port]）",
        "",
        "| stage | model_id |",
        "|---|---|",
    ]
    for stage, item in (models.get("models") or {}).items():
        lines.append(f"| {stage} | `{item.get('model_id')}` |")
    lines += [
        "",
        "## 5. Criteria Lock",
        "",
        "| task | criteria_lock |",
        "|---|---|",
    ]
    for tid, lock in (crit.get("task_criteria_locks") or {}).items():
        lines.append(f"| {tid} | `{lock}` |")
    lines += [
        "",
        f"- manifest criteria_lock：`{crit.get('manifest_criteria_lock', '')}`",
        f"- manifest_sha256：`{crit.get('manifest_sha256', '')}`",
        "",
        "复核：`/mnt/venvs/simpraxis/bin/python -m orchestrator.benchmark.cli validate-specs`",
        "",
        "## 6. 环境",
        "",
        f"- Python：`{env.get('python_executable', '')}`（{env.get('python', '')}）",
        f"- Docker：{env.get('docker_version', '')}",
        f"- GPU：{env.get('gpu_name', '')}（total {env.get('gpu_total_mib')} MiB）",
        f"- /mnt 可用：{env.get('df_mnt_available_gib', '?')} GiB",
        "",
        "## 7. KB 四表（只读冻结；口径争议见 OI-6）",
        "",
        "| 表 | 行数 |",
        "|---|---:|",
    ]
    for name, count in (tbl or {}).items():
        lines.append(f"| {name} | {count} |")
    lines += [
        "",
        "## 8. 测试基线",
        "",
        f"- passed={tb.get('passed')} skipped={tb.get('skipped')} "
        f"deselected={tb.get('deselected')} rc={tb.get('rc')}",
        "",
        "复核：" + (tb.get("cmd") or "（本次未采集，见 test_baseline.json 的 deferred 标记）"),
        "",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 写盘 / 核对
# --------------------------------------------------------------------------

def emit(root: Path, artifacts: "dict[str, dict]") -> Path:
    intake_dir = ensure_private_dir(root / "intake")
    for name in ARTIFACT_JSON:
        payload = artifacts[name]
        atomic_write_text(intake_dir / name,
                          json.dumps(payload, ensure_ascii=False, indent=2,
                                     sort_keys=True) + "\n",
                          mode=0o600)
    atomic_write_text(intake_dir / ARTIFACT_REPORT,
                      render_report(artifacts, root), mode=0o600)
    return intake_dir


def _flatten(obj: Any, prefix: str = "") -> "dict[str, Any]":
    out: dict[str, Any] = {}
    if isinstance(obj, dict):
        for key, value in obj.items():
            out.update(_flatten(value, f"{prefix}.{key}" if prefix else str(key)))
    elif isinstance(obj, list):
        for idx, value in enumerate(obj):
            out.update(_flatten(value, f"{prefix}[{idx}]"))
    else:
        out[prefix] = obj
    return out


def _volatile_keys(name: str) -> "set[str]":
    keys = set(_VOLATILE_COMMON)
    keys.update(_VOLATILE_BY_ARTIFACT.get(name, ()))
    return keys


def check(root: Path, artifacts: "dict[str, dict]") -> "tuple[int, list[dict]]":
    """现算结果 vs 既有 json 逐键比对；**不写任何文件**。"""
    intake_dir = root / "intake"
    diffs: "list[dict]" = []
    if not intake_dir.is_dir():
        # 首跑语义（计划 §4.6 步骤 1）：rc=1，且差异清单**逐件**指名十个产物
        # 都不在场——不用「intake/ 缺」一句话概括，否则操作者看不出到底缺几件。
        for name in ARTIFACT_NAMES:
            diffs.append({"key": name, "before": None, "now": "missing",
                          "equal": False})
        return 1, diffs
    for name in ARTIFACT_JSON:
        path = intake_dir / name
        if not path.is_file():
            diffs.append({"key": name, "before": None, "now": "missing",
                          "equal": False})
            continue
        try:
            before_obj = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            diffs.append({"key": name, "before": f"<unparseable: {exc}>",
                          "now": "parse-ok", "equal": False})
            continue
        before = _flatten(before_obj)
        now = _flatten(artifacts[name])
        volatile = _volatile_keys(name)
        # 锁内适配（Phase 2.8.1 T0）：``test_baseline.json`` 是唯一不能每次现算的
        # 产物（它记录的是**那一次**冷跑的计数）。任一侧带 ``deferred`` 标记即
        # 说明本件本次未采集，全件不可比（``equal: null``，不计入 rc）——否则
        # 不带 ``--test-log`` 的 ``--check`` 会因循环依赖恒 rc=1。
        if before_obj.get("deferred") or artifacts[name].get("deferred"):
            diffs.append({"key": f"{name}:<deferred>",
                          "before": before_obj.get("note"),
                          "now": artifacts[name].get("note"), "equal": None})
            continue
        for key in sorted(set(before) | set(now)):
            leaf = key.rsplit(".", 1)[-1]
            # 锁内适配（Phase 2.8.1 T0）：``_VOLATILE_COMMON`` 里登记的
            # ``commands`` 是**整棵子树**（``commands[3].stdout_tail`` 这类键名
            # 既不等于 ``commands``、叶名也不是 ``commands``）。按**路径段**
            # 匹配才真正兑现「命令留痕不可比」的意图——``df -h`` 的可用块、
            # 提交前后的 git 状态都会逐字变，而结构化事实各自单列、仍逐字可比。
            segments = {seg.split("[", 1)[0] for seg in key.split(".")}
            if leaf in volatile or key in volatile or (volatile & segments):
                diffs.append({"key": f"{name}:{key}", "before": before.get(key),
                              "now": now.get(key), "equal": None})
                continue
            diffs.append({"key": f"{name}:{key}", "before": before.get(key),
                          "now": now.get(key),
                          "equal": before.get(key) == now.get(key)})
    if not (intake_dir / ARTIFACT_REPORT).is_file():
        diffs.append({"key": ARTIFACT_REPORT, "before": None, "now": "missing",
                      "equal": False})
    bad = [d for d in diffs if d["equal"] is False]
    return (0 if not bad else 1), diffs


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description="Phase 2.8.1 冻结采集（P281-0）")
    ap.add_argument("--root", required=True,
                    help="phase281 数据根（如 /mnt/artifacts/simpraxis/phase281）")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--emit", action="store_true", help="采集并写盘")
    mode.add_argument("--check", action="store_true",
                      help="只核对，不写；rc=0 iff 全部可比键相等")
    ap.add_argument("--test-log", default=None,
                    help="冷跑 raw log 路径（两排除全套的输出）")
    args = ap.parse_args(argv)

    root = Path(args.root)
    # 数据根由 --root 指定 = 本进程的数据根；其余模块一律经本 env 解析。
    os.environ["SIMPRAXIS_PHASE281_ROOT"] = str(root)

    from orchestrator.benchmark.phase281 import paths as p281

    # 三条隔离纪律（含 SIMPRAXIS_BENCHMARK_ROOT 必须未设置）；--check 同样要过，
    # 否则一次指错的 --check 会在「别人的根」上读出看似正常的差异清单。
    p281.assert_isolated()

    tmp_logs: "str | None" = None
    if args.emit:
        ensure_private_dir(root)
        log_dir = ensure_private_dir(root / "intake" / "cmd_logs")
    else:
        # --check 不写任何持久文件：intake 的命令日志落到临时目录并在退出时删除
        tmp_logs = tempfile.mkdtemp(prefix="p281_freeze_check_")
        log_dir = Path(tmp_logs)
    try:
        artifacts = collect(root, test_log=args.test_log, log_dir=log_dir)
    finally:
        if tmp_logs is not None:
            shutil.rmtree(tmp_logs, ignore_errors=True)

    if args.emit:
        intake_dir = emit(root, artifacts)
        print(f"[phase281_freeze] emitted 10 artifacts -> {intake_dir}")
        return 0

    rc, diffs = check(root, artifacts)
    # 差异清单只打到 stdout：``--check`` 的契约是「不写任何文件」，连一份
    # diff 落盘都不行（调用方用重定向留证）。
    print(json.dumps(diffs, ensure_ascii=False, indent=2, sort_keys=True))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
