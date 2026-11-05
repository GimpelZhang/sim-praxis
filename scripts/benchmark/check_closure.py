#!/usr/bin/env python3
"""Phase 2.8.0 Task 7 终判（D7-4：独立于证据包重新计算）。

十一类检查（八类核心 + 三本账/付费/KB 侧查）：

1. ``intake``      —— Task 0 基线件齐备且自洽（含 I10 的 origin/main 锚）
2. ``specs``       —— 三任务规格/判据/权重与 Task 0 校验报告逐字相同
3. ``fingerprints``—— 运行身份件与运行副本逐 sha 相同
4. ``birth``       —— 出生验证 BORN、problems 空、sha 绑定
5. ``evaluator``   —— 独立评测 PASS、硬闸空、criteria_lock 与运行绑定
6. ``controls``    —— golden 全过、负例/变异全拒
7. ``evidence``    —— 证据包逐件 sha 复算、模式、软链、超限、判决件排除、
                       Phase 2.7 包 sha 前后不变（I6）
8. ``cold_run``    —— P0-AC-19 精确增量等式（禁止 ``>=`` 放水）
9. ``paid``        —— 五本账零新增（ZERO_PAID）；benchmark 代码零 Gateway 导入
10. ``kb``         —— KB HEAD/工作树/四表行数与 Task 0 相同
11. ``ledgers``    —— 五本账 SHA256 与 Task 0 快照逐字相同（FROZEN）

``--require-acceptance`` 额外读验收车道日志与三签，并校验三者与
``pack.sha256`` 的绑定。输出 ``<root>/closeout/closure_judge.json``。
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

SCHEMA = "simpraxis.benchmark.closure_judge.v1"
TASKS = ("mujoco_contact_push_v1", "libero_env_adapter_v1", "icy_ground_dr_v1")
#: 与打包器各自独立实现（D7-4）：机判重新算，不信打包器自述。
MAX_FILE_BYTES = 10 * 1024 * 1024
EXPECTED_MODE = {"mujoco_contact_push_v1": ("host", None),
                 "libero_env_adapter_v1": ("docker",
                                           "sha256:83304574517980a2b5f94d8e45b5c3f361182d0fa40fdd27f6c1713040eca624"),
                 "icy_ground_dr_v1": ("host", None)}
ORIGIN_MAIN = "4b5d134a447b2069b050887d63f3ddd4e687f123"
LEDGER_PHASES = ("phase1", "phase2", "phase25", "phase26", "phase27")
ROLES = ("executor", "reviewer", "verifier")
SUMMARY_RE = re.compile(
    r"(\d+) passed(?:, (\d+) skipped)?(?:, (\d+) deselected)?"
    r"(?:, \d+ warnings)? in ([\d.]+)s")

CHECKS = ("intake", "specs", "fingerprints", "birth", "evaluator", "controls",
          "evidence", "cold_run", "paid", "kb", "ledgers")


class JudgeError(RuntimeError):
    """输入缺失或自相矛盾（fail-closed，不产出 PASS）。"""


def is_judgment_relpath(relpath: str) -> bool:
    """判决件族（独立实现：只看 relpath 的族形状）。"""
    parts = Path(relpath).parts
    name = parts[-1] if parts else ""
    if fnmatch.fnmatch(name, "closure_judge*.json"):
        return True
    if fnmatch.fnmatch(name, "acceptance_matrix*.json"):
        return True
    return "signs" in parts[:-1] and name.endswith(".json")


def _json(path: Path):
    if not Path(path).is_file():
        raise JudgeError(f"缺文件：{path}")
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _git(*args: str, cwd: "Path | None" = None) -> str:
    proc = subprocess.run(["git", *args], cwd=str(cwd or _REPO),
                          capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise JudgeError(f"git {' '.join(args)} 失败：{proc.stderr.strip()}")
    return proc.stdout.strip()


def _latest_run(root: Path, task_id: str) -> Path:
    runs = sorted(p for p in (root / "runs").glob(f"{task_id}-*") if p.is_dir())
    if not runs:
        raise JudgeError(f"{task_id}: 缺 runs/{task_id}-*")
    return runs[-1]


# --------------------------------------------------------------------------
# 纯函数（供单测与机判共用；不读活证据）
# --------------------------------------------------------------------------

def parse_pytest_summary(text: str) -> "dict | None":
    """从 raw 日志里取 pytest 末尾摘要（取最后一个匹配行）。"""
    for line in reversed(text.splitlines()):
        match = SUMMARY_RE.search(line)
        if match:
            return {"passed": int(match.group(1)),
                    "skipped": int(match.group(2) or 0),
                    "deselected": int(match.group(3) or 0),
                    "duration_s": float(match.group(4))}
    return None


def verify_cold_run(doc: dict, log_text: str, baseline: dict) -> "list[str]":
    """P0-AC-19 精确等式（§4.1）；任何一条不成立即返回问题文本。"""
    problems = []
    summary = parse_pytest_summary(log_text)
    if summary is None:
        return ["raw 日志里找不到 pytest 摘要行"]
    if doc.get("rc") != 0:
        problems.append(f"冷跑 rc={doc.get('rc')} 非 0")
    for key in ("passed", "skipped", "deselected"):
        if int(doc.get(key, -1)) != summary[key]:
            problems.append(f"cold_run.{key}={doc.get(key)} 与日志 "
                            f"{summary[key]} 不符")
    new = doc.get("new_collected") or {}
    base_passed = int(baseline["passed"])
    base_skipped = int(baseline["skipped"])
    base_deselected = int(baseline["deselected"])
    if int(doc.get("passed", -1)) != base_passed + int(new.get("total", -1)):
        problems.append(
            f"passed ≠ {base_passed}+{new.get('total')}（精确增量等式不成立）")
    if int(doc.get("skipped", -1)) != base_skipped:
        problems.append(f"skipped ≠ {base_skipped}（新增测试不许用 skip）")
    expected_deselect = (base_deselected + int(new.get("container_deselected", -1))
                         + int(new.get("milestone_deselected", -1)))
    if int(doc.get("deselected", -1)) != expected_deselect:
        problems.append(f"deselected ≠ {base_deselected}+"
                        f"{new.get('container_deselected')}+"
                        f"{new.get('milestone_deselected')}")
    nodeids = list(new.get("nodeids") or [])
    if not nodeids:
        problems.append("new_collected.nodeids 为空（增量无法独立复验）")
    else:
        joined = "\n".join(nodeids)
        if hashlib.sha256(joined.encode()).hexdigest() != new.get("nodeids_sha256"):
            problems.append("nodeids_sha256 与 nodeids 复算不符")
        bad = [n for n in nodeids if not n.startswith("tests/benchmark/")]
        if bad:
            problems.append(f"存在非 tests/benchmark/ 的 nodeid：{bad[:3]}")
        if len(nodeids) != int(new.get("total", -1)):
            problems.append("nodeids 条数 ≠ new_collected.total")
    if doc.get("delta_ok") is not True or problems:
        problems.append("delta_ok 必须为 true 且上面无问题")
    return problems


def validate_signs(signs: "list[dict]", pack_sha256: str) -> "list[str]":
    problems = []
    roles = sorted(sign.get("role") for sign in signs)
    if roles != sorted(ROLES):
        problems.append(f"三签角色必须是 {ROLES}，实得 {roles}")
    for sign in signs:
        if sign.get("schema") != "simpraxis.benchmark.sign.v1":
            problems.append(f"{sign.get('role')}: schema 不符")
        if sign.get("verdict") not in ("APPROVE", "REJECT"):
            problems.append(f"{sign.get('role')}: verdict 非 APPROVE/REJECT")
        elif sign.get("verdict") != "APPROVE":
            problems.append(f"{sign.get('role')}: verdict={sign.get('verdict')}")
        if not re.fullmatch(r"[0-9a-f]{40}", str(sign.get("commit", ""))):
            problems.append(f"{sign.get('role')}: commit 非 40 hex")
        if not re.fullmatch(r"[0-9a-f]{64}", str(sign.get("pack_sha256", ""))):
            problems.append(f"{sign.get('role')}: pack_sha256 非 64 hex")
        if not str(sign.get("ts_iso", "")).endswith("Z"):
            problems.append(f"{sign.get('role')}: ts_iso 非严格 Z")
        if not str(sign.get("reviewer_notes") or "").strip():
            problems.append(f"{sign.get('role')}: reviewer_notes 为空")
    commits = {sign.get("commit") for sign in signs}
    packs = {sign.get("pack_sha256") for sign in signs}
    if len(commits) != 1:
        problems.append(f"三签 commit 不一致：{sorted(commits)}")
    if len(packs) != 1:
        problems.append(f"三签 pack_sha256 不一致：{sorted(packs)}")
    elif next(iter(packs)) != pack_sha256:
        problems.append("三签 pack_sha256 ≠ pack.sha256")
    return problems


def validate_ledger_freeze(doc: dict) -> "list[str]":
    problems = []
    if doc.get("schema") != "simpraxis.benchmark.ledger_freeze.v1":
        problems.append("ledger_freeze schema 不符")
    if doc.get("active_phase") != "phase27":
        problems.append("active_phase 必须是 phase27")
    rows = doc.get("ledgers") or []
    phases = [row.get("phase") for row in rows]
    if sorted(phases) != sorted(LEDGER_PHASES):
        problems.append(f"账本相位不齐：{phases}")
    for row in rows:
        if row.get("equal") is not True:
            problems.append(f"{row.get('phase')}: before≠after")
        if row.get("sha256_before") != row.get("sha256_after"):
            problems.append(f"{row.get('phase')}: sha 记录不一致")
        mt_before, mt_after = row.get("mtime_before"), row.get("mtime_after")
        if mt_before is not None and mt_after is not None \
                and mt_before != mt_after:
            problems.append(f"{row.get('phase')}: mtime 变化")
    if doc.get("verdict") != "FROZEN":
        problems.append("verdict 必须是 FROZEN")
    return problems


def validate_kb_integrity(doc: dict) -> "list[str]":
    problems = []
    if doc.get("schema") != "simpraxis.benchmark.kb_integrity.v1":
        problems.append("kb_integrity schema 不符")
    if doc.get("head_before") != doc.get("head_after"):
        problems.append("KB HEAD 变化")
    if (doc.get("porcelain_before") or "").strip() != "":
        problems.append("KB 工作树 before 非空")
    if (doc.get("porcelain_after") or "").strip() != "":
        problems.append("KB 工作树 after 非空")
    tables = doc.get("tables") or {}
    for name, pair in sorted(tables.items()):
        if not (isinstance(pair, list) and len(pair) == 2 and pair[0] == pair[1]):
            problems.append(f"{name}: 行数前后不等 {pair}")
    if doc.get("verdict") != "UNCHANGED":
        problems.append("verdict 必须是 UNCHANGED")
    return problems


# --------------------------------------------------------------------------
# 十一类检查
# --------------------------------------------------------------------------

def check_intake(root: Path) -> "tuple[list[str], dict]":
    problems, detail = [], {}
    required = ("test_baseline.json", "repository_state.json", "kb_state.json",
                "table_counts.json", "invariants_before.json",
                "sealed_ledger_sha256.json", "environment_baseline.json")
    for name in required:
        detail[name] = (root / "intake" / name).is_file()
        if not detail[name]:
            problems.append(f"缺 intake/{name}")
    if problems:
        return problems, detail
    baseline = _json(root / "intake" / "test_baseline.json")
    raw = Path(baseline["raw_log"])
    detail["test_baseline"] = {k: baseline[k] for k in
                               ("passed", "skipped", "deselected", "rc")}
    if baseline["rc"] != 0:
        problems.append("基线 rc 非 0")
    if not raw.is_file():
        problems.append(f"基线 raw log 不存在：{raw}")
    elif _sha256(raw) != baseline["raw_log_sha256"]:
        problems.append("基线 raw log sha256 与登记不符")
    state = _json(root / "intake" / "repository_state.json")
    detail["origin_main"] = state["origin_main"]
    if state["origin_main"] != ORIGIN_MAIN:
        problems.append(f"origin/main={state['origin_main']} ≠ {ORIGIN_MAIN}")
    kb = _json(root / "intake" / "kb_state.json")
    detail["kb_head"] = kb["head"]
    if not re.fullmatch(r"[0-9a-f]{40}", kb["head"]):
        problems.append("kb_state.head 非 40 hex")
    tables = _json(root / "intake" / "table_counts.json")["tables"]
    detail["tables"] = tables
    if sorted(tables) != ["kb_chunks", "phase1_learnings", "phase2_learnings",
                          "solution_chunks"]:
        problems.append(f"四表名不符：{sorted(tables)}")
    invariants = _json(root / "intake" / "invariants_before.json")
    detail["stage_models_sha256"] = invariants["stage_models_sha256"]
    detail["phase27_pack_sha256"] = invariants["phase27_pack_sha256"]
    return problems, detail


def check_specs(root: Path) -> "tuple[list[str], dict]":
    problems, detail = [], {}
    report = _json(root / "specs" / "validation_report.json")
    detail["validation_ok"] = bool(report.get("ok"))
    detail["problems"] = report.get("problems")
    if not report.get("ok") or report.get("problems"):
        problems.append(f"Task 0 规格校验报告不通过：{report.get('problems')}")
    bench = Path(report["benchmark_root"])
    rows = {row["task_id"]: row for row in report["tasks"]}
    if sorted(rows) != sorted(TASKS):
        problems.append(f"规格任务集不符：{sorted(rows)}")
    detail["tasks"] = {}
    for task_id, row in sorted(rows.items()):
        for key, sha_key in (("criteria_file", "criteria_sha256"),
                             ("task_file", "task_file_sha256")):
            path = bench / row[key]
            actual = _sha256(path) if path.is_file() else None
            expected = row[sha_key]
            if actual != expected:
                problems.append(f"{task_id}.{key}: sha {actual} ≠ {expected}")
        if abs(float(row["weights_sum"]) - 1.0) > 1e-9:
            problems.append(f"{task_id}: weights_sum={row['weights_sum']}")
        detail["tasks"][task_id] = {
            "criteria_lock_sha256": row["criteria_lock_sha256"],
            "hard_gates": row["hard_gates"],
        }
    return problems, detail


def check_fingerprints(root: Path) -> "tuple[list[str], dict]":
    problems, detail = [], {}
    for task_id in TASKS:
        run = _latest_run(root, task_id)
        doc_path = run / "runtime_fingerprint.json"
        doc = _json(doc_path)
        sha_path = run / "runtime_fingerprint.sha256"
        if sha_path.is_file():
            recorded = sha_path.read_text(encoding="utf-8").split()[0]
            if recorded != doc["fingerprint_sha256"]:
                problems.append(f"{task_id}: 运行内指纹 sha 记录不符")
        manifest = _json(run / "run_manifest.json")
        # run_manifest 记的是指纹**文件字节**的 sha256；doc.fingerprint_sha256
        # 是内容规范化摘要——两者口径不同，不能互比（Task 2 契约）。
        if manifest["runtime_fingerprint_sha256"] != _sha256(doc_path):
            problems.append(f"{task_id}: run_manifest 指纹文件 sha 不符")
        mode, digest = EXPECTED_MODE[task_id]
        container = doc.get("container") or {}
        if mode == "docker":
            if container.get("backend") != "docker":
                problems.append(f"{task_id}: backend={container.get('backend')}")
            if container.get("image_digest") != digest:
                problems.append(f"{task_id}: image_digest 不符")
        elif container.get("backend") != "host":
            problems.append(f"{task_id}: backend={container.get('backend')} 非 host")
        detail[task_id] = {"fingerprint_sha256": doc["fingerprint_sha256"],
                           "backend": container.get("backend")}
        pinned = root / "fingerprints" / task_id / "runtime_fingerprint.json"
        if task_id != "icy_ground_dr_v1":
            # 冻结指纹（Task 2）与参考运行指纹（Task 6）是两次独立捕获：
            # argv/engine_head/criteria 随 Task 4/5 合法演进，哈希不可能相等。
            # 只验冻结件自身一致性（其 sha 侧车 == doc 声明的规范化摘要）。
            if not pinned.is_file():
                problems.append(f"{task_id}: 缺 fingerprints/{task_id}/")
            else:
                side = pinned.parent / "runtime_fingerprint.sha256"
                pinned_doc = _json(pinned)
                if side.is_file():
                    recorded_side = side.read_text(encoding="utf-8").split()[0]
                    if recorded_side != pinned_doc.get("fingerprint_sha256"):
                        problems.append(f"{task_id}: 冻结指纹侧车 sha 不一致")
                detail[task_id]["pinned_sha256"] = pinned_doc.get(
                    "fingerprint_sha256")
    return problems, detail


def check_birth(root: Path) -> "tuple[list[str], dict]":
    problems, detail = [], {}
    for task_id in TASKS:
        run = _latest_run(root, task_id)
        birth_path = run / "birth" / "functional_birth.json"
        birth = _json(birth_path)
        detail[task_id] = {"birth_status": birth["birth_status"],
                           "problems": birth.get("problems")}
        if birth["birth_status"] != "BORN":
            problems.append(f"{task_id}: birth_status={birth['birth_status']}")
        if birth.get("problems"):
            problems.append(f"{task_id}: birth problems={birth['problems']}")
        manifest = _json(run / "run_manifest.json")
        if manifest["birth_result_sha256"] != _sha256(birth_path):
            problems.append(f"{task_id}: 出生件 sha 与 run_manifest 不符")
    return problems, detail


def check_evaluator(root: Path) -> "tuple[list[str], dict]":
    problems, detail = [], {}
    for task_id in TASKS:
        run = _latest_run(root, task_id)
        result = _json(root / "evaluator_results" / run.name
                       / "evaluation_result.json")
        manifest = _json(run / "run_manifest.json")
        detail[task_id] = {"status": result["status"],
                           "weighted_score": result["weighted_score"],
                           "hard_gates": result["hard_gates"]}
        if result["status"] != "PASS":
            problems.append(f"{task_id}: evaluator status={result['status']}")
        if result["hard_gates"]:
            problems.append(f"{task_id}: 硬闸失败 {result['hard_gates']}")
        if result["criteria_lock_sha256"] != manifest["criteria_lock_sha256"]:
            problems.append(f"{task_id}: criteria_lock 与运行不符")
        if int(result["evaluator_version"]) != int(manifest["evaluator_version"]):
            problems.append(f"{task_id}: evaluator_version 与运行不符")
        score = float(result["weighted_score"])
        if not 0.0 <= score <= 1.0:
            problems.append(f"{task_id}: weighted_score={score} 越界")
        for name, dim in result["dimensions"].items():
            if not 0.0 <= float(dim["score"]) <= 1.0:
                problems.append(f"{task_id}.{name}: 维度分越界")
    return problems, detail


def check_controls(root: Path) -> "tuple[list[str], dict]":
    problems, detail = [], {}
    base = root / "negative_controls"
    golden = _json(base / "golden_smoke.json")
    negative = _json(base / "negative_control_results.json")
    mutation = _json(base / "mutation_results.json")
    rows = {
        "golden": {"count": int(golden["count"]),
                   "all_passed": bool(golden["all_passed"])},
        "negative": {"count": int(negative["count"]),
                     "rejected": int(negative["rejected"]),
                     "all_passed": bool(negative["all_passed"])},
        "mutation": {"count": int(mutation["count"]),
                     "rejected": int(mutation["rejected"]),
                     "all_passed": bool(mutation["all_passed"])},
    }
    detail.update(rows)
    if rows["golden"]["count"] < 1 or not rows["golden"]["all_passed"]:
        problems.append("golden 控制组未全通过")
    if rows["negative"]["count"] < 1 or \
            rows["negative"]["rejected"] != rows["negative"]["count"]:
        problems.append("负例未全拒")
    if rows["mutation"]["count"] < 1 or \
            rows["mutation"]["rejected"] != rows["mutation"]["count"]:
        problems.append("变异未全拒")
    return problems, detail


def check_evidence(root: Path) -> "tuple[list[str], dict]":
    problems, detail = [], {}
    pack = root / "evidence_pack"
    manifest_path = pack / "manifest.json"
    if not manifest_path.is_file():
        return ["缺 evidence_pack/manifest.json（先跑 build_evidence_pack.py）"], {}
    manifest = _json(manifest_path)
    if manifest.get("schema") != "simpraxis.benchmark.evidence_pack.v1":
        problems.append(f"manifest schema={manifest.get('schema')}")
    if manifest.get("git_dirty") is not False:
        problems.append(f"manifest.git_dirty={manifest.get('git_dirty')}"
                        "（证据包必须从干净树构建）")
    files = manifest.get("files") or []
    detail["n_files"] = len(files)
    detail["total_bytes"] = int(manifest.get("total_bytes", -1))
    if int(manifest.get("n_files", -1)) != len(files):
        problems.append("manifest.n_files 与 files 条数不符")
    if detail["total_bytes"] != sum(int(row["size"]) for row in files):
        problems.append("manifest.total_bytes 与逐件字节和不符")
    pack_resolved = pack.resolve()
    for row in files:
        rel = row["relpath"]
        path = pack / rel
        if is_judgment_relpath(rel):
            problems.append(f"判决件混入包：{rel}")
        if rel.split("/", 1)[0] in ("closeout", "evidence_pack"):
            problems.append(f"判决目录/包自身混入包：{rel}")
        if pack_resolved in path.resolve(strict=False).parents and \
                path.resolve(strict=False) == pack_resolved:
            problems.append(f"包自身混入包：{rel}")
        if not path.is_file():
            problems.append(f"缺件：{rel}")
            continue
        if path.is_symlink():
            problems.append(f"符号链接入包：{rel}")
        if _sha256(path) != row["sha256"]:
            problems.append(f"sha 不符：{rel}")
        if path.stat().st_size != int(row["size"]):
            problems.append(f"字节数不符：{rel}")
        if path.stat().st_size > MAX_FILE_BYTES:
            problems.append(f"超 10 MiB：{rel}")
        if oct(path.stat().st_mode & 0o777) != "0o600":
            problems.append(f"文件模式非 0600：{rel}")
    sha_file = pack / "pack.sha256"
    if not sha_file.is_file():
        problems.append("缺 pack.sha256")
    else:
        recorded = sha_file.read_text(encoding="utf-8").strip()
        actual = _sha256(manifest_path)
        detail["pack_sha256"] = recorded
        if recorded != actual:
            problems.append("pack.sha256 ≠ sha256(manifest.json)")
    dirs_bad = [str(p) for p in pack.rglob("*")
                if p.is_dir() and oct(p.stat().st_mode & 0o777) != "0o700"]
    if dirs_bad:
        problems.append(f"目录模式非 0700：{dirs_bad[:3]}")
    problems.extend(_audit_pack_secrets(pack, manifest, detail))
    # I6：Phase 2.7 证据包前后不变（before 取 Task 0 采集）
    before = _json(root / "intake" / "invariants_before.json")
    old_pack = Path(before["phase27_pack"])
    old_manifest = old_pack / "manifest.json"
    from orchestrator.benchmark import intake as bench_intake

    now_digest = bench_intake.phase27_pack_sha256(old_pack)
    detail["phase27_pack"] = {
        "n_files": now_digest["phase27_pack_n_files"],
        "total_bytes": now_digest["phase27_pack_total_bytes"],
        "sha256": now_digest["phase27_pack_sha256"],
        "manifest_sha256": now_digest["phase27_manifest_sha256"]}
    if now_digest["phase27_pack_sha256"] != before["phase27_pack_sha256"]:
        problems.append("Phase 2.7 证据包规范化 sha 变化")
    if now_digest["phase27_manifest_sha256"] != before["phase27_manifest_sha256"]:
        problems.append("Phase 2.7 证据包 manifest sha 变化")
    if int(now_digest["phase27_pack_n_files"]) != int(
            before["phase27_pack_n_files"]):
        problems.append("Phase 2.7 证据包件数变化")
    if int(now_digest["phase27_pack_total_bytes"]) != int(
            before["phase27_pack_total_bytes"]):
        problems.append("Phase 2.7 证据包字节数变化")
    return problems, detail


_TOKEN_PREFIXES = ("hf_", "ghp_", "gho_", "ghu_", "ghs_", "ghr_", "nvapi-",
                   "ark-", "sk-", "cli_", "xoxb-", "xoxp-", "xoxa-", "xoxr-",
                   "xoxs-")


FIXTURE_SELFTEST = "test_scan_detects_all_strict_forms["


def _synthetic_token(matched: str) -> bool:
    """与 pack 端同一口径的**独立复算**：去前缀后整段同一字符且 ≥20 位。"""
    body = str(matched)
    for prefix in _TOKEN_PREFIXES:
        if body.startswith(prefix):
            body = body[len(prefix):]
            break
    core = body.strip("\"'")
    return len(core) >= 20 and len(set(core)) == 1


def _audit_pack_secrets(pack: Path, manifest: dict, detail: dict) -> "list[str]":
    """独立密钥审计：严格命中必须为 0；被豁免的合成哨兵逐件复算仍是哨兵。"""
    problems = []
    policy = (manifest.get("policy") or {}).get("secrets") or {}
    detail["secrets"] = {"strict_hits": policy.get("strict_hits"),
                         "synthetic_fixture_skipped":
                             policy.get("synthetic_fixture_skipped")}
    if int(policy.get("strict_hits", -1)) != 0:
        problems.append("manifest 声明 strict_hits 非 0")
    try:
        from orchestrator.common import secrets as secret_scan
    except Exception as exc:  # pragma: no cover
        return [f"无法加载密钥扫描器：{exc}"]
    allowed = set(policy.get("synthetic_entries") or [])
    for row in manifest.get("files") or []:
        path = pack / row["relpath"]
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        rel = row["relpath"]
        for lineno, line in enumerate(text.splitlines(), 1):
            found = secret_scan.scan_text(line)
            if not found:
                continue
            if FIXTURE_SELFTEST in line:
                for name, _matched in found:
                    if f"{rel}::{name}::L{lineno}" not in allowed:
                        problems.append(f"合成哨兵未在 manifest 登记："
                                        f"{rel}::{name}::L{lineno}")
                continue
            for name, matched in found:
                if _synthetic_token(matched):
                    if f"{rel}::{name}::L{lineno}" not in allowed:
                        problems.append(f"合成哨兵未在 manifest 登记："
                                        f"{rel}::{name}::L{lineno}")
                else:
                    problems.append(f"包内真实密钥形态：{rel}::{name}")
    return problems


def check_cold_run(root: Path) -> "tuple[list[str], dict]":
    doc = _json(root / "closeout" / "cold_run.json")
    raw = Path(doc["raw_log"])
    if not raw.is_file():
        return [f"缺 cold_run.raw.log：{raw}"], {"raw_log": str(raw)}
    if _sha256(raw) != doc.get("raw_log_sha256"):
        return ["cold_run.raw.log sha256 与 cold_run.json 不符"], {}
    baseline = _json(root / "intake" / "test_baseline.json")
    problems = verify_cold_run(doc, raw.read_text(encoding="utf-8",
                                                  errors="ignore"), baseline)
    detail = {key: doc.get(key) for key in
              ("rc", "passed", "skipped", "deselected", "delta_ok", "verdict")}
    return problems, detail


def check_paid(root: Path) -> "tuple[list[str], dict]":
    """P0-AC-16：先验判决件 `closeout/paid_calls.json`，再独立复算账本与
    Gateway 引用（判决件自述不作为唯一依据）。"""
    problems, detail = [], {"gateway_imports_in_benchmark": 0}
    doc_path = root / "closeout" / "paid_calls.json"
    if not doc_path.is_file():
        return [f"缺 closeout/paid_calls.json：{doc_path}"], {}
    doc = _json(doc_path)
    detail["doc_verdict"] = doc.get("verdict")
    if doc.get("schema") != "simpraxis.benchmark.paid_calls.v1":
        problems.append(f"paid_calls schema={doc.get('schema')}")
    if doc.get("verdict") != "ZERO_PAID":
        problems.append(f"paid_calls verdict={doc.get('verdict')}")
    if int(doc.get("paid_call_rows_added", -1)) != 0:
        problems.append("paid_calls.paid_call_rows_added 非 0")
    if int(doc.get("gateway_imports_in_benchmark", -1)) != 0:
        problems.append("paid_calls.gateway_imports_in_benchmark 非 0")
    doc_rows = {row.get("phase"): row for row in doc.get("ledgers") or []}
    snapshot = _json(root / "intake" / "sealed_ledger_sha256.json")["ledgers"]
    for phase, row in sorted(snapshot.items()):
        path = Path(row["path"])
        actual = _sha256(path) if path.is_file() else None
        if actual != row["sha256"]:
            problems.append(f"{phase}: 账本 sha 变化（实付调用？）")
        recorded = doc_rows.get(phase) or {}
        if recorded.get("sha256_after") != actual:
            problems.append(f"{phase}: paid_calls 记录与现算 sha 不符")
        if recorded.get("equal") is not True:
            problems.append(f"{phase}: paid_calls equal 非 true")
    needle = re.compile(r"^\s*(?:from\s+orchestrator\.gateway|"
                        r"import\s+orchestrator\.gateway)", re.MULTILINE)
    for base in (_REPO / "orchestrator" / "benchmark",
                 _REPO / "scripts" / "benchmark",
                 _REPO / "tests" / "benchmark"):
        for path in sorted(base.rglob("*.py")):
            if needle.search(path.read_text(encoding="utf-8", errors="ignore")):
                detail["gateway_imports_in_benchmark"] += 1
                problems.append(f"benchmark 代码引用 Gateway：{path}")
    return problems, detail


def check_signs(root: Path) -> "tuple[list[str], dict]":
    """P0-AC-20：三签逐件校验并与 `pack.sha256` 绑定（`--check signs`）。"""
    problems = []
    signs_dir = root / "closeout" / "signs"
    signs = []
    for role in ROLES:
        path = signs_dir / f"{role}.json"
        if not path.is_file():
            problems.append(f"缺三签：{role}.json")
            continue
        signs.append(_json(path))
    pack_path = root / "evidence_pack" / "pack.sha256"
    if not pack_path.is_file():
        problems.append("缺 evidence_pack/pack.sha256")
        return problems, {}
    pack_sha = pack_path.read_text(encoding="utf-8").strip()
    if signs:
        problems.extend(validate_signs(signs, pack_sha))
    return problems, {"roles": [s.get("role") for s in signs],
                      "pack_sha256": pack_sha}


def check_kb(root: Path) -> "tuple[list[str], dict]":
    problems, detail = [], {}
    before = _json(root / "intake" / "kb_state.json")
    repo = Path(before["kb_root"])
    head = _git("rev-parse", "HEAD", cwd=repo)
    porcelain = _git("status", "--porcelain", cwd=repo)
    detail["head_before"] = before["head"]
    detail["head_after"] = head
    detail["porcelain"] = porcelain
    if head != before["head"]:
        problems.append("KB HEAD 变化")
    if porcelain:
        problems.append("KB 工作树非空")
    baseline = _json(root / "intake" / "table_counts.json")
    uri = baseline["lancedb_uri"]
    try:
        import lancedb

        conn = lancedb.connect(uri)
        names = set(conn.table_names())
        current = {}
        for name, expected in baseline["tables"].items():
            if name not in names:
                problems.append(f"KB 表缺失：{name}")
                continue
            current[name] = int(conn.open_table(name).count_rows())
            if current[name] != int(expected):
                problems.append(f"{name}: {current[name]} ≠ {expected}")
        detail["tables"] = {"before": baseline["tables"], "after": current}
    except Exception as exc:  # pragma: no cover - 环境缺失时如实记问题
        problems.append(f"读 LanceDB 失败：{exc}")
    return problems, detail


def check_ledgers(root: Path) -> "tuple[list[str], dict]":
    problems, detail = [], {}
    freeze = _json(root / "closeout" / "ledger_freeze.json")
    problems.extend(validate_ledger_freeze(freeze))
    snapshot = _json(root / "intake" / "sealed_ledger_sha256.json")["ledgers"]
    for row in freeze["ledgers"]:
        phase = row["phase"]
        if phase not in snapshot:
            problems.append(f"{phase}: 不在 Task 0 快照")
            continue
        if row["sha256_before"] != snapshot[phase]["sha256"]:
            problems.append(f"{phase}: before 与 Task 0 快照不符")
        path = Path(row["path"])
        actual = _sha256(path) if path.is_file() else None
        if actual != row["sha256_after"]:
            problems.append(f"{phase}: 现算 sha 与 after 不符")
        if int(row["rows_before"]) != int(snapshot[phase]["rows"]):
            problems.append(f"{phase}: rows_before 与快照不符")
    detail["phases"] = {row["phase"]: row["equal"] for row in freeze["ledgers"]}
    return problems, detail


CHECK_FUNCS = {
    "intake": check_intake,
    "specs": check_specs,
    "fingerprints": check_fingerprints,
    "birth": check_birth,
    "evaluator": check_evaluator,
    "controls": check_controls,
    "evidence": check_evidence,
    "cold_run": check_cold_run,
    "paid": check_paid,
    "signs": check_signs,
    "kb": check_kb,
    "ledgers": check_ledgers,
}


def check_acceptance(root: Path) -> "tuple[list[str], dict]":
    problems, detail = [], {}
    lane_json = root / "closeout" / "acceptance_lane.json"
    lane_log = root / "closeout" / "acceptance_lane.log"
    if not lane_json.is_file() or not lane_log.is_file():
        return ["缺验收车道产物（acceptance_lane.json/.log）"], {}
    lane = _json(lane_json)
    detail.update({k: lane.get(k) for k in ("cmd", "rc", "passed", "failed",
                                           "summary")})
    if lane.get("rc") != 0:
        problems.append(f"验收车道 rc={lane.get('rc')}")
    if int(lane.get("failed", -1)) != 0:
        problems.append(f"验收车道 failed={lane.get('failed')}")
    if int(lane.get("passed", 0)) < 1:
        problems.append("验收车道 0 passed")
    if _sha256(lane_log) != lane.get("log_sha256"):
        problems.append("acceptance_lane.log sha256 与记录不符")
    summary = parse_pytest_summary(
        lane_log.read_text(encoding="utf-8", errors="ignore"))
    if summary is None or summary["passed"] != int(lane.get("passed", -1)):
        problems.append("验收车道日志摘要与 acceptance_lane.json 不符")
    signs_dir = root / "closeout" / "signs"
    signs = []
    for role in ROLES:
        path = signs_dir / f"{role}.json"
        if not path.is_file():
            problems.append(f"缺三签：{role}.json")
            continue
        signs.append(_json(path))
    pack_sha = (root / "evidence_pack" / "pack.sha256").read_text(
        encoding="utf-8").strip()
    problems.extend(validate_signs(signs, pack_sha))
    detail["roles"] = [sign.get("role") for sign in signs]
    detail["pack_sha256"] = pack_sha
    return problems, detail


def check_gaps(root: Path) -> "tuple[list[str], list[dict]]":
    """acceptance_matrix 的 gaps 逐条必须有归属（owner/reason/evidence）。"""
    path = root / "closeout" / "acceptance_matrix.json"
    if not path.is_file():
        return ["缺 closeout/acceptance_matrix.json"], []
    matrix = _json(path)
    gaps = matrix.get("gaps") or []
    problems = []
    for gap in gaps:
        for key in ("id", "owner", "reason", "evidence"):
            if not str(gap.get(key) or "").strip():
                problems.append(f"gap {gap.get('id')}: 缺 {key}（无归属）")
    return problems, gaps


def run_judge(root: Path, *, checks: "tuple[str, ...]" = CHECKS,
              require_acceptance: bool = False,
              pack_sha256: "str | None" = None) -> dict:
    root = Path(root)
    results = {}
    problems = []
    for name in checks:
        try:
            probs, detail = CHECK_FUNCS[name](root)
        except (JudgeError, OSError, ValueError, KeyError) as exc:
            probs, detail = [f"{type(exc).__name__}: {exc}"], {}
        results[name] = {"ok": not probs, "problems": probs, "detail": detail}
        problems.extend(f"{name}: {p}" for p in probs)
    gaps: list[dict] = []
    acceptance = None
    if require_acceptance:
        try:
            probs, detail = check_acceptance(root)
        except (JudgeError, OSError, ValueError, KeyError) as exc:
            probs, detail = [f"{type(exc).__name__}: {exc}"], {}
        results["acceptance"] = {"ok": not probs, "problems": probs,
                                 "detail": detail}
        problems.extend(f"acceptance: {p}" for p in probs)
        gap_probs, gaps = check_gaps(root)
        results["gaps"] = {"ok": not gap_probs, "problems": gap_probs,
                           "detail": {"gaps": gaps}}
        problems.extend(f"gaps: {p}" for p in gap_probs)
        acceptance = {"required": True, "ok": not probs}
    doc = {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%SZ"),
        "root": str(root),
        "commit": _git("rev-parse", "HEAD"),
        "pack_sha256": pack_sha256 or (root / "evidence_pack" /
                                       "pack.sha256").read_text(
                                           encoding="utf-8").strip()
        if (root / "evidence_pack" / "pack.sha256").is_file() else None,
        "checks_run": list(checks),
        "checks": results,
        "acceptance": acceptance,
        "problems": problems,
        "gaps": gaps,
        "verdict": "Established" if not problems else "Not Established",
    }
    return doc


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def record_cold_run(root: Path, raw_log: Path, rc: "int | None" = None) -> dict:
    """解析冷跑日志 + 测量 ``N_new``，写 ``closeout/cold_run.json``（§4.1）。"""
    import os

    text = Path(raw_log).read_text(encoding="utf-8", errors="ignore")
    summary = parse_pytest_summary(text)
    if summary is None:
        raise JudgeError(f"{raw_log}: 找不到 pytest 摘要行")
    baseline = _json(root / "intake" / "test_baseline.json")
    python = "/mnt/venvs/simpraxis/bin/python"
    clean_env = {k: v for k, v in os.environ.items()
                 if k not in ("SIMPRAXIS_CONTAINER",
                              "SIMPRAXIS_MILESTONE_CLOSEOUT")}
    cmd = [python, "-m", "pytest", "tests/benchmark", "--collect-only", "-q"]
    collect = subprocess.run(cmd, cwd=str(_REPO), env=clean_env,
                             capture_output=True, text=True, timeout=600)
    nodeids = [line.strip() for line in collect.stdout.splitlines()
               if line.strip().startswith("tests/benchmark/")]
    if collect.returncode != 0:
        raise JudgeError(f"collect-only 失败：{collect.stdout[-500:]}")

    def _marked(marker: str) -> int:
        env = dict(clean_env, SIMPRAXIS_CONTAINER="1",
                   SIMPRAXIS_MILESTONE_CLOSEOUT="1")
        proc = subprocess.run(cmd + ["-m", marker], cwd=str(_REPO), env=env,
                              capture_output=True, text=True, timeout=600)
        if proc.returncode != 0:
            raise JudgeError(f"collect -m {marker} 失败")
        return sum(1 for line in proc.stdout.splitlines()
                   if line.strip().startswith("tests/benchmark/"))

    new = {
        "total": len(nodeids),
        "container_deselected": _marked("container"),
        "milestone_deselected": _marked("milestone_closeout"),
        "nodeids": nodeids,
        "nodeids_sha256": hashlib.sha256(
            "\n".join(nodeids).encode()).hexdigest(),
    }
    expected = {"passed": int(baseline["passed"]) + new["total"],
                "skipped": int(baseline["skipped"]),
                "deselected": int(baseline["deselected"])
                + new["container_deselected"] + new["milestone_deselected"]}
    delta_ok = (summary["passed"] == expected["passed"]
                and summary["skipped"] == expected["skipped"]
                and summary["deselected"] == expected["deselected"])
    doc = {
        "schema": "simpraxis.benchmark.cold_run.v1",
        "cmd": baseline["cmd"],
        "raw_log": str(Path(raw_log).resolve()),
        "raw_log_sha256": _sha256(Path(raw_log)),
        "rc": int(rc) if rc is not None else (0 if "failed" not in
        text.splitlines()[-1] else 1),
        "passed": summary["passed"],
        "skipped": summary["skipped"],
        "deselected": summary["deselected"],
        "duration_s": summary["duration_s"],
        "baseline": {"passed": int(baseline["passed"]),
                     "skipped": int(baseline["skipped"]),
                     "deselected": int(baseline["deselected"]),
                     "source": "intake/test_baseline.json"},
        "new_collected": new,
        "delta_ok": delta_ok,
        "verdict": "GREEN" if delta_ok else "RED",
    }
    out = root / "closeout" / "cold_run.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=1,
                              sort_keys=True) + "\n", encoding="utf-8")
    out.chmod(0o600)
    return doc


def main(argv: "list[str] | None" = None) -> int:
    from orchestrator.benchmark import paths

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=None)
    parser.add_argument("--check", choices=tuple(CHECK_FUNCS), action="append",
                        help="只跑某类检查（可重复；缺省全跑）")
    parser.add_argument("--report", default=None, help="判决件路径")
    parser.add_argument("--require-acceptance", action="store_true")
    parser.add_argument("--record-cold-run", default=None,
                        metavar="RAW_LOG", help="解析冷跑日志并写 cold_run.json")
    parser.add_argument("--rc", type=int, default=None,
                        help="冷跑进程退出码（缺省按日志末行推断）")
    args = parser.parse_args(argv)

    root = Path(args.root) if args.root else paths.bench_root()
    if args.record_cold_run:
        doc = record_cold_run(root, Path(args.record_cold_run), args.rc)
        print(json.dumps({k: doc[k] for k in
                          ("passed", "skipped", "deselected", "delta_ok",
                           "verdict")}, ensure_ascii=False))
        return 0 if doc["delta_ok"] else 1

    checks = tuple(args.check) if args.check else CHECKS
    doc = run_judge(root, checks=checks,
                    require_acceptance=args.require_acceptance)
    out = Path(args.report) if args.report else root / "closeout" / \
        "closure_judge.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=1,
                              sort_keys=True) + "\n", encoding="utf-8")
    out.chmod(0o600)
    print(json.dumps({"verdict": doc["verdict"],
                      "problems": doc["problems"],
                      "checks": {k: v["ok"] for k, v in doc["checks"].items()}},
                     ensure_ascii=False, indent=1, sort_keys=True))
    return 0 if not doc["problems"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
