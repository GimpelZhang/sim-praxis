"""Task 0 的七类基线快照采集（P0-0）。

纪律（D0-3/D0-4/D0-6）：
- 每个采集函数只返回**从真实命令输出解析出来的**字典，禁止手写数字；
- 每个字典都带 ``generated_by`` / ``git_head`` / ``ts_iso``（严格 UTC ``Z``）
  与 ``commands`` 列表（每条命令的 ``cmd`` / ``rc`` / ``ts_iso`` / ``stdout_tail``）；
- 每条命令的完整 stdout/stderr 另落 ``intake/cmd_logs/NN_<name>.log``（0600，
  追加写，重跑留痕而不覆盖）；
- 五本账**只读**：只做 ``open("rb")`` 读字节算 sha256，永不写打开。

七个采集函数与产物一一对应：

===================================  ==========================
函数                                 产物
===================================  ==========================
``repository_state()``               ``intake/repository_state.json``
``kb_state()``                       ``intake/kb_state.json``
``test_baseline(raw_log)``           ``intake/test_baseline.json``
``environment_baseline()``           ``intake/environment_baseline.json``
``sealed_ledger_sha256()``           ``intake/sealed_ledger_sha256.json``
``table_counts()``                   ``intake/table_counts.json``
``invariants_before()``              ``intake/invariants_before.json``
===================================  ==========================
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from orchestrator.common.runtime_paths import (
    phase1_root,
    phase2_root,
    phase25_root,
    phase26_root,
    phase27_root,
)

#: Task 0 intake 信封的生成入口（**只**用于 intake 采集器自身）
GENERATED_BY = "scripts/benchmark/preflight.py"
#: Task 1 规格证据的生成入口（``python -m orchestrator.benchmark.cli validate-specs``）
GENERATED_BY_VALIDATE = "orchestrator/benchmark/cli.py"


def entry_label(argv: "list[str] | None" = None) -> str:
    """按**实际调用入口**给出 ``generated_by``（评审 W1-06）。

    早先 ``validation_report.json`` / ``schemas_sha256.json`` 无论由谁生成都
    盖 ``scripts/benchmark/preflight.py`` 的章，而 Complete 记录的再生成命令
    是 ``python -m orchestrator.benchmark.cli validate-specs``——证据件自述了
    一条没发生过的命令。
    """
    args = list(sys.argv if argv is None else argv)
    if not args:
        return GENERATED_BY_VALIDATE
    script = Path(args[0]).name
    rest = " ".join(a for a in args[1:] if a)
    if script == "preflight.py":
        base = GENERATED_BY
    elif script.startswith("cli"):
        base = GENERATED_BY_VALIDATE
    else:
        base = script
    return f"{base} {rest}".strip()

REPO_ROOT = Path(__file__).resolve().parents[2]

#: 两排除全套命令（逐字；AC-T0-4 用它与 ``test_baseline.json.cmd`` 比对）
TWO_EXCLUSION_CMD = (
    '{py} -m pytest tests -q -k "not test_04" '
    '-m "not paid and not kb_integration"'
)

#: five sealed/live ledgers，顺序固定（供报告与 AC 使用）
LEDGER_PHASES = ("phase1", "phase2", "phase25", "phase26", "phase27")

#: 四张活表（只读行数）
TABLES = ("kb_chunks", "phase1_learnings", "solution_chunks", "phase2_learnings")

_SUMMARY_RE = re.compile(
    r"(\d+) passed,\s*(\d+) skipped,\s*(\d+) deselected.*?in ([\d.]+)s")
_RC_RE = re.compile(r"^rc=(\d+)$", re.MULTILINE)

#: 本阶段**自己新增**的路径前缀。intake 在提交之前采集，这些文件必然出现在
#: ``git status`` 里；``status_porcelain`` 只把「逐字等于本前缀的新增行」剔出去，
#: 其余任何一行都保留——AC-T0-2 的「工作树干净」因此不因自身文件而变红，也不
#: 给非本阶段改动开口子（剔除清单逐条记进 ``status_porcelain_excluded``）。
PHASE28_NEW_PATHS = (
    "orchestrator/benchmark/",
    "scripts/benchmark/",
    "tests/benchmark/",
    # 评审 W1-03：I9 明确放行、本阶段法定新增的两处，早先漏在名单外
    "benchmarks/",
    "tests/fixtures/benchmark/",
)


def _in_phase28(path: str) -> bool:
    return any(path == p.rstrip("/") or path.startswith(p)
               for p in PHASE28_NEW_PATHS)


def _rename_ends(path: str) -> "list[str]":
    """把 porcelain 的路径字段拆成「实际涉及的每一端」（评审 W1-03）。

    改名行形如 ``old -> new``（git 同目录改名只回显裸文件名，也可能是
    ``dir/{a.py -> b.py}`` 的括号形式）。**两端都要在合法前缀内**才允许剔除：
    否则一次 ``git mv orchestrator/benchmark/x.py orchestrator/graph/x.py``
    会被整行剔出去，STOP-A 的工作树闸就看不见这次越过边界的改名。
    """
    if " -> " not in path:
        return [path]
    old, new = (piece.strip() for piece in path.split(" -> ", 1))
    old = old.rstrip("{").rstrip()
    new = new.rstrip("}").rstrip()
    if "/" not in new and "/" in old:
        new = old.rsplit("/", 1)[0] + "/" + new
    return [old, new]


def split_status(raw: str) -> "tuple[str, list[str]]":
    """``git status --porcelain`` 行分离：本阶段新增路径 vs 其余改动。"""
    kept, excluded = [], []
    for line in raw.splitlines():
        if not line.strip():
            continue
        path = line[3:] if len(line) > 3 else line
        if all(_in_phase28(part) for part in _rename_ends(path)):
            excluded.append(line)
        else:
            kept.append(line)
    return "\n".join(kept), excluded


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: "str | Path") -> str:
    """只读算 sha256（``open("rb")``；对封存账亦然）。"""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _tail(text: str, limit: int = 4000) -> str:
    text = text or ""
    return text if len(text) <= limit else "…" + text[-limit:]


def _log_path(name: str) -> Path:
    from orchestrator.benchmark import paths

    return paths.cmd_logs_dir() / name


def _run(cmd: "list[str]", *, cwd: "Path | None" = None, timeout: int = 120,
         log: "str | Path | None" = None, check: bool = False) -> dict:
    """执行只读命令并留痕；返回可入 JSON 的摘要（stdout 只留尾部）。"""
    started = _now()
    try:
        proc = subprocess.run(
            cmd, cwd=str(cwd) if cwd else None, capture_output=True,
            text=True, timeout=timeout, env=os.environ.copy())
        rc, out, err = proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as exc:
        rc = 124
        out = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        err = f"timeout after {timeout}s"
    if log is not None:
        log_path = Path(log)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(f"\n=== {started} $ {' '.join(cmd)}\n")
            fh.write(out)
            if err:
                fh.write("\n[stderr]\n" + err)
            fh.write(f"\n[rc={rc}]\n")
        os.chmod(log_path, 0o600)
    if check and rc != 0:
        raise RuntimeError(f"命令失败 rc={rc}: {' '.join(cmd)}\n{_tail(err)}")
    return {
        "cmd": " ".join(cmd),
        "argv": list(cmd),
        "rc": rc,
        "ts_iso": started,
        "stdout_tail": _tail(out),
    }


def _git(args: "list[str]", *, cwd: "Path" = REPO_ROOT, timeout: int = 60) -> dict:
    return _run(["git", *args], cwd=cwd, timeout=timeout)


#: CLI 侧使用的公开别名（同一实现；下划线名保留给库内调用）
run_command = _run


def _git_head() -> str:
    res = _git(["rev-parse", "HEAD"], timeout=30)
    return res["stdout_tail"].strip() if res["rc"] == 0 else ""


def _envelope(payload: dict, commands: "list[dict]") -> dict:
    head = _git_head()
    return {
        "generated_by": GENERATED_BY,
        "git_head": head,
        "ts_iso": _now(),
        **payload,
        "commands": commands,
    }


# --------------------------------------------------------------------------
# 1. 引擎仓状态
# --------------------------------------------------------------------------

def repository_state() -> dict:
    log = _log_path("01_repository_state.log")
    cmds = [
        _run(["git", "status", "--short", "--branch"], log=log),
        _run(["git", "rev-parse", "HEAD"], log=log),
        _run(["git", "rev-parse", "origin/main"], log=log),
        _run(["git", "rev-list", "--count", "origin/main..HEAD"], log=log),
        _run(["git", "status", "--porcelain"], log=log),
        _run(["git", "diff", "--stat"], log=log),
    ]
    status_short_branch, head, origin_main, ahead, porcelain, diff_stat = cmds
    raw_status = porcelain["stdout_tail"].rstrip("\n")
    kept_status, excluded = split_status(raw_status)
    payload = {
        "repo": str(REPO_ROOT),
        "status_short_branch": status_short_branch["stdout_tail"].strip(),
        "head": head["stdout_tail"].strip(),
        "origin_main": origin_main["stdout_tail"].strip(),
        "ahead": int(ahead["stdout_tail"].strip() or -1)
        if ahead["rc"] == 0 else -1,
        # AC-T0-2 口径：剔除本阶段自己新增的三处路径后的工作树状态
        "status_porcelain": kept_status,
        "status_porcelain_raw": raw_status,
        "status_porcelain_excluded": excluded,
        "status_porcelain_excluded_count": len(excluded),
        "diff_stat": diff_stat["stdout_tail"].strip(),
    }
    return _envelope(payload, cmds)


# --------------------------------------------------------------------------
# 2. KB 仓状态（只读；硬断言基线的采集侧）
# --------------------------------------------------------------------------

def kb_root() -> Path:
    raw = os.environ.get("KB_ROOT")
    if raw:
        return Path(raw)
    return Path.home() / "embodied-ai-sim-knowledge-base"


def kb_state() -> dict:
    kb = kb_root()
    log = _log_path("02_kb_state.log")
    cmds = [
        _run(["git", "-C", str(kb), "status", "--short", "--branch"], log=log),
        _run(["git", "-C", str(kb), "rev-parse", "HEAD"], log=log),
        _run(["git", "-C", str(kb), "status", "--porcelain"], log=log),
        _run(["git", "-C", str(kb), "diff", "--stat"], log=log),
        _run(["git", "-C", str(kb), "rev-parse", "origin/main"], log=log),
    ]
    status_short_branch, head, porcelain, diff_stat, origin_main = cmds
    payload = {
        "kb_root": str(kb),
        "status_short_branch": status_short_branch["stdout_tail"].strip(),
        "head": head["stdout_tail"].strip(),
        "origin_main": origin_main["stdout_tail"].strip(),
        "status_porcelain": porcelain["stdout_tail"].rstrip("\n"),
        "diff_stat": diff_stat["stdout_tail"].strip(),
    }
    return _envelope(payload, cmds)


# --------------------------------------------------------------------------
# 3. 测试基线（只认 raw log；D0-4）
# --------------------------------------------------------------------------

def test_baseline(raw_log: "str | Path") -> dict:
    """从两排除全套的 raw log 解析基线；``raw_log`` 必须已存在。"""
    log_path = Path(raw_log)
    if not log_path.is_file():
        raise FileNotFoundError(
            f"raw log 不在场：{log_path}（先用两排除命令重定向生成，再跑 preflight）")
    text = log_path.read_text(encoding="utf-8", errors="replace")
    matches = _SUMMARY_RE.findall(text)
    if not matches:
        raise ValueError(f"raw log 里找不到 pytest 摘要行：{log_path}")
    passed, skipped, deselected, duration_s = matches[-1]
    rc_match = _RC_RE.findall(text)
    payload = {
        "raw_log": str(log_path),
        "raw_log_sha256": _sha256_file(log_path),
        "cmd": TWO_EXCLUSION_CMD.format(py=sys.executable),
        "passed": int(passed),
        "skipped": int(skipped),
        "deselected": int(deselected),
        "duration_s": float(duration_s),
        "rc": int(rc_match[-1]) if rc_match else None,
        "summary_line": text.strip().splitlines()[-2 if rc_match else -1]
        if text.strip() else "",
    }
    return _envelope(payload, [{
        "cmd": payload["cmd"],
        "argv": shlex.split(payload["cmd"]),
        "rc": payload["rc"] if payload["rc"] is not None else -1,
        "ts_iso": _now(),
        "stdout_tail": payload["summary_line"],
    }])


# --------------------------------------------------------------------------
# 4. 环境锚点
# --------------------------------------------------------------------------

def _pkg_version(name: str) -> "str | None":
    try:
        from importlib.metadata import version

        return version(name)
    except Exception:  # noqa: BLE001 - 缺包/元数据损坏都算「版本未知」
        return None


def environment_baseline() -> dict:
    log = _log_path("08_environment_baseline.log")
    py = _run([sys.executable, "-V"], log=log, timeout=30)
    docker = _run(["docker", "--version"], log=log, timeout=30)
    gpu = _run(["nvidia-smi", "--query-gpu=name,memory.total,memory.used",
                "--format=csv,noheader,nounits"], log=log, timeout=30)
    df = _run(["df", "-k", "/mnt"], log=log, timeout=30)

    gpu_name, gpu_total_mib, gpu_used_mib = "", -1, -1
    if gpu["rc"] == 0 and gpu["stdout_tail"].strip():
        parts = [p.strip() for p in gpu["stdout_tail"].strip().splitlines()[0]
                 .split(",")]
        if len(parts) >= 3:
            gpu_name = parts[0]
            gpu_total_mib = int(float(parts[1]))
            gpu_used_mib = int(float(parts[2]))

    df_avail_gib = -1
    if df["rc"] == 0:
        lines = [ln for ln in df["stdout_tail"].splitlines() if ln.strip()]
        if len(lines) >= 2:
            cols = lines[-1].split()
            if len(cols) >= 4:
                df_avail_gib = round(int(cols[3]) / (1024 * 1024), 2)

    docker_version = ""
    if docker["rc"] == 0:
        m = re.search(r"Docker version\s+([0-9][^\s,]*)", docker["stdout_tail"])
        if m:
            docker_version = m.group(1)
        else:
            m = re.search(r"([0-9]+\.[0-9]+\.[0-9]+)", docker["stdout_tail"])
            docker_version = m.group(1) if m else ""

    payload = {
        "python": "{}.{}.{}".format(*sys.version_info[:3]),
        "python_executable": sys.executable,
        "python_V": py["stdout_tail"].strip(),
        "packages": {
            name: _pkg_version(name)
            for name in ("mujoco", "numpy", "torch", "lancedb", "langgraph",
                         "lark-oapi", "mani_skill", "sapien")
        },
        "docker_version": docker_version,
        "gpu_name": gpu_name,
        "gpu_total_mib": gpu_total_mib,
        "gpu_used_mib": gpu_used_mib,
        "df_mnt_available_gib": df_avail_gib,
        "df_mnt_raw": df["stdout_tail"].strip(),
    }
    return _envelope(payload, [py, docker, gpu, df])


# --------------------------------------------------------------------------
# 5. 五本账行数与 SHA256（只读）
# --------------------------------------------------------------------------

_LEDGER_ROOTS = {
    "phase1": phase1_root,
    "phase2": phase2_root,
    "phase25": phase25_root,
    "phase26": phase26_root,
    "phase27": phase27_root,
}


def sealed_ledger_sha256() -> dict:
    log = _log_path("03_sealed_ledgers.log")
    entries = {}
    for phase in LEDGER_PHASES:
        path = _LEDGER_ROOTS[phase]() / "quota" / "ledger.jsonl"
        if not path.is_file():
            raise FileNotFoundError(f"账本不在场：{path}")
        # 只 read，不 open("w")/("a")：封存账任何写打开都是事故
        data = path.read_bytes()
        rows = data.count(b"\n")
        entries[phase] = {
            "path": str(path),
            "rows": rows,
            "bytes": len(data),
            "sha256": _sha256_bytes(data),
        }
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(f"\n=== {_now()} {phase} rows={rows} "
                     f"sha256={entries[phase]['sha256']}\n")
        os.chmod(log, 0o600)
    return _envelope({"ledgers": entries}, [])


# --------------------------------------------------------------------------
# 6. 四张活表行数（只读）
# --------------------------------------------------------------------------

def table_counts(*, drift_note: str = "",
                 drift_signoff_ref: str = "") -> dict:
    """四张活表只读行数。

    评审 W1-14：计划 AC-T0-7 要求「若漂移则必须带 ``drift_note`` 且用户签收」，
    但采集函数早先永远写空串，这条流程只能靠手改一个规定「必须机器生成」的
    JSON 才能走通——等于没有机器通路。现在由 CLI 传入，且**两项必须成对**：
    只有说明没有签收凭据，等于自说自话。
    """
    if bool(drift_note.strip()) != bool(drift_signoff_ref.strip()):
        raise ValueError("drift_note 与 drift_signoff_ref 必须同时给出或同时为空")
    import lancedb

    uri = kb_root() / "data" / "lancedb"
    if not uri.exists():
        raise FileNotFoundError(f"LanceDB 不在场：{uri}")
    db = lancedb.connect(str(uri))
    counts = {}
    for name in TABLES:
        counts[name] = int(db.open_table(name).count_rows())
    payload = {
        "lancedb_uri": str(uri),
        "tables": counts,
        # 漂移时由执行者填「漂移说明 + 用户签收」，默认空串（不预置借口）
        "drift_note": drift_note.strip(),
        "drift_signoff_ref": drift_signoff_ref.strip(),
    }
    return _envelope(payload, [])


# --------------------------------------------------------------------------
# 7. 不变量 before 基线（I4 / I6 / AC-T3-8）
# --------------------------------------------------------------------------

def entrypoint_ready_source(loop_path: "str | Path | None" = None) -> str:
    """从 ``loop.py`` 的 AST 取 ``_entrypoint_ready`` 的逐字源码段。"""
    path = Path(loop_path) if loop_path else (
        REPO_ROOT / "orchestrator" / "agentic" / "loop.py")
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and \
                node.name == "_entrypoint_ready":
            segment = ast.get_source_segment(source, node)
            if segment is None:
                raise ValueError("ast.get_source_segment 返回 None")
            return segment
    raise LookupError(f"{path} 中找不到 _entrypoint_ready")


def phase27_pack_sha256(pack: "str | Path | None" = None) -> dict:
    """Phase 2.7 证据包的确定性摘要（manifest 文件清单的规范化哈希）。

    口径：``manifest.json`` 的 ``files`` 按 ``path`` 排序后逐条
    ``path\\0sha256\\0size\\n`` 拼接再 sha256；``manifest.json`` 自身不入清单
    （manifest 不含自己，n_files == len(files)）。另记 manifest 自身字节 sha256。
    """
    pack_dir = Path(pack) if pack else (
        phase27_root() / "task7_milestone" / "evidence_pack")
    manifest_path = pack_dir / "manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"Phase 2.7 证据包 manifest 不在场：{manifest_path}")
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes.decode("utf-8"))
    files = sorted(manifest.get("files", []), key=lambda e: e["path"])
    h = hashlib.sha256()
    missing = []
    #: 评审 W1-08（I6 假阴性修复）：只把 manifest 里登记的三元组拼起来算 sha，
    #: 包内文件被改而 manifest 不动时摘要完全不变——「证据没变」证明不了。
    #: 这里对每件**重算字节 sha256**，不符即记 ``..._files_bad``（951 件几十 MB，
    #: 成本可接受）。
    bad = []
    for entry in files:
        h.update(f"{entry['path']}\0{entry['sha256']}\0{entry['size']}\n"
                 .encode("utf-8"))
        target = pack_dir / entry["path"]
        if not target.is_file():
            missing.append(entry["path"])
            continue
        actual = _sha256_file(target)
        if actual != entry["sha256"]:
            bad.append({"path": entry["path"],
                        "manifest_sha256": entry["sha256"],
                        "actual_sha256": actual})
    return {
        "phase27_pack": str(pack_dir),
        "phase27_pack_sha256": h.hexdigest(),
        "phase27_manifest_sha256": _sha256_bytes(manifest_bytes),
        "phase27_pack_n_files": len(files),
        "phase27_pack_total_bytes": int(manifest.get("total_bytes", 0)),
        "phase27_pack_files_missing": missing,
        "phase27_pack_files_bad": bad,
    }


def invariants_before() -> dict:
    from orchestrator.benchmark import paths

    log = _log_path("09_invariants_before.log")
    stage_models = REPO_ROOT / "orchestrator" / "gateway" / "stage_models.yaml"
    cmd_sm = _run(["sha256sum", str(stage_models)], log=log, timeout=30)
    payload = {
        "stage_models_yaml": str(stage_models),
        "stage_models_sha256": _sha256_file(stage_models),
        **phase27_pack_sha256(),
        "entrypoint_ready_source_sha256": _sha256_bytes(
            entrypoint_ready_source().encode("utf-8")),
        "entrypoint_ready_source": str(
            REPO_ROOT / "orchestrator" / "agentic" / "loop.py"),
        "bench_root": str(paths.bench_root()),
    }
    return _envelope(payload, [cmd_sm])
