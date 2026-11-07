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

``--require-acceptance`` 额外读验收车道日志、三签、``acceptance_matrix.json``
（20 条 AC 独立复算）与 ``invariants.json``（I1..I12 逐条），并校验与
``pack.sha256`` / ``manifest.git_commit`` 的绑定。输出
``<root>/closeout/closure_judge.json``。

评审 W4-03：``verdict=Established`` 只在**全集**（11 类核心 + 验收四类）跑过
且无问题时给出；子集运行如实记 ``Incomplete``，不得产出 Established 判决。
"""
from __future__ import annotations

import argparse
import ast
import contextvars
import fnmatch
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

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
#: Task 0 基线 HEAD（``intake/repository_state.json`` 登记值）。I10 不再要求
#: 实时 ``origin/main`` 恒等于它（评审 W5-01：合法授权推送会把该断言变活红），
#: 而要求实时 ``origin/main`` ∈ {基线, HEAD} 且是 HEAD 的祖先（见
#: ``verify_origin_anchor``）。
ORIGIN_MAIN = "4b5d134a447b2069b050887d63f3ddd4e687f123"
#: 人工 STOP 门**放行过**的推送头（除基线 ``ORIGIN_MAIN`` 外）——每一条都必须
#: 在 Complete 里有对应的推送窗口记录。``origin/main`` 只允许停在这些值之一，
#: 或等于当前 ``HEAD``（=本阶段即将/已经经门推送）。名单是**精确集合**，不是
#: ``>=`` 式放水：任何未经记录的新 sha 仍判红。
#: - ``5895fe0``：2026-11-05 19:41–19:43 CST 推送窗口（Complete §8，用户逐字指示；
#:   11 条 phase28 提交，含 Task 7 收尾件）。
AUTHORIZED_PUSH_HEADS = (
    "5895fe02e8cbda2a6962f8aa9a178268e7e5d991",
    # Phase 2.8.1 P281-0（2026-11-06 推送窗口）：Phase 2.8.0 的 6 条重建提交
    # 经人工 STOP 门推送至 origin/main，远端基线由此前移。不加这一项，
    # 本阶段第一笔提交之后 test_inv_i10_no_push 会**按设计**恒红（计划 F13）。
    "82a142710127367b0a771d818192321b53e2e951",
    # *锁内适配*（Phase 2.8.1 P281-9，2026-11-07 推送窗口）：用户在 P281-7
    # 复核四项裁决里逐字指示「四：推」，T7 评审修复批 16 提交
    # （``82a1427..065231b``）经人工 STOP 门推送至 origin/main，远端再次前移。
    # 计划 §16.2 / L2603 把「``AUTHORIZED_PUSH_HEADS`` 追加一个经门推送头（附
    # 推送窗口注释）」明确列为允许的追加项，是同 ``5895fe0``/``82a1427`` 的先例。
    # 不加这一项，P281-9 的第一笔提交之后 ``test_inv_i10_no_push`` 与
    # ``P281-0 AC-01 F2``（origin/main 精确集合）都会按设计恒红。
    "065231bfdab2a37d70bbf7d9516e3c92e43d78a1",
)
LEDGER_PHASES = ("phase1", "phase2", "phase25", "phase26", "phase27")
ROLES = ("executor", "reviewer", "verifier")
#: Task 5 预注册控制组规模（§3 Task 5）：不钉死则「缩水也算过」（W4-15）。
EXPECTED_CONTROLS = {"golden": 3, "negative": 20, "mutation": 10}
#: 验收组附加检查（``--require-acceptance`` 才有输入，故不进默认 ``CHECKS``）。
ACCEPTANCE_CHECKS = ("acceptance", "matrix", "invariants", "gaps")
SIGN_TS_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z")
_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
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


RUN_IDENTITY = "run_identity.json"


def _latest_run(root: Path, task_id: str) -> Path:
    """被判 run 的身份来源（评审 W4-11）。

    优先读 ``<root>/intake/run_identity.json`` 的钉死登记；缺该件时才退回
    「目录唯一」判据——**恰好一个** ``runs/<task_id>-*`` 才接受，多个即
    ``JudgeError``（禁止「谁最后写入谁被审」）。钉死后诊断 run 不会被误判。
    """
    from orchestrator.benchmark import paths as bench_paths

    try:
        return bench_paths.pinned_run(root, task_id)
    except bench_paths.BenchmarkPathError as exc:
        raise JudgeError(f"{task_id}: {exc}") from exc


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


def verify_origin_anchor(origin_main: str, head: str, *,
                         base: str = ORIGIN_MAIN,
                         authorized: "tuple[str, ...]" = AUTHORIZED_PUSH_HEADS,
                         ancestor: "bool | None" = None) -> "list[str]":
    """I10（评审 W5-01 **改向不放水**）：推送是人工 STOP 门，不是「永不推送」。

    本阶段允许的实时 ``origin/main`` 是**有限集合**：Task 0 基线 ``base``、
    每条经人工 STOP 门记录在案的推送头 ``authorized``（见
    ``AUTHORIZED_PUSH_HEADS``），或本阶段 ``HEAD``（＝本次放行后的推送结果）；
    且 ``origin/main`` 必须是 ``HEAD`` 的祖先（改写已推历史即违规）。任何名单
    外的第三方 sha、或非祖先关系都判红——推过几次就有几个值，新增一个就必须
    先更新名单，不存在「只要不是基线就放行」的缝。
    """
    problems = []
    if not _COMMIT_RE.fullmatch(str(origin_main or "")):
        problems.append(f"origin/main 非 40 hex：{origin_main!r}")
        return problems
    allowed = tuple(dict.fromkeys((base, *authorized, head)))
    if origin_main not in allowed:
        problems.append(
            f"origin/main={origin_main} 不在 {len(allowed)} 个放行值内"
            f"（基线 {base} / 已记录推送头 {list(authorized)} / HEAD {head}）"
            f"——未经审的推送或改写？")
    if ancestor is False:
        problems.append("HEAD 不是 origin/main 的祖先（已推送历史被改写）")
    return problems


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
        # 评审 W5-07：§4.1 明文要求「nodeid **排序后** sha256」，两侧同口径。
        joined = "\n".join(sorted(nodeids))
        if hashlib.sha256(joined.encode()).hexdigest() != new.get("nodeids_sha256"):
            problems.append("nodeids_sha256 与排序后 nodeids 复算不符")
        bad = [n for n in nodeids if not n.startswith("tests/benchmark/")]
        if bad:
            problems.append(f"存在非 tests/benchmark/ 的 nodeid：{bad[:3]}")
        if len(nodeids) != int(new.get("total", -1)):
            problems.append("nodeids 条数 ≠ new_collected.total")
    if doc.get("delta_ok") is not True or problems:
        problems.append("delta_ok 必须为 true 且上面无问题")
    return problems


def validate_signs(signs: "list[dict]", pack_sha256: str, *,
                   expected_commit: "str | None" = None) -> "list[str]":
    """三签校验（评审 W4-07 契约补绑一半 + W4-16 字段名二择一）。

    ``expected_commit`` 给出时（＝``manifest.git_commit``），三签 commit 必须
    与之相同——只绑 pack 不绑 commit 会让旧提交上的三签被复用。
    """
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
        if not _COMMIT_RE.fullmatch(str(sign.get("commit", ""))):
            problems.append(f"{sign.get('role')}: commit 非 40 hex")
        if not _SHA256_RE.fullmatch(str(sign.get("pack_sha256", ""))):
            problems.append(f"{sign.get('role')}: pack_sha256 非 64 hex")
        # 计划 §4.1 写 notes、实件写 reviewer_notes：二者皆收（W4-16），
        # 但至少一个非空。
        notes = sign.get("reviewer_notes") or sign.get("notes")
        if not str(notes or "").strip():
            problems.append(f"{sign.get('role')}: reviewer_notes/notes 为空")
        stamp = str(sign.get("ts_iso", ""))
        if not SIGN_TS_RE.fullmatch(stamp):
            problems.append(f"{sign.get('role')}: ts_iso 非 RFC3339-Z：{stamp!r}")
    commits = {sign.get("commit") for sign in signs}
    packs = {sign.get("pack_sha256") for sign in signs}
    if len(commits) != 1:
        problems.append(f"三签 commit 不一致：{sorted(commits)}")
    elif expected_commit and next(iter(commits)) != expected_commit:
        problems.append(f"三签 commit={next(iter(commits))} ≠ "
                        f"manifest.git_commit={expected_commit}")
    if len(packs) != 1:
        problems.append(f"三签 pack_sha256 不一致：{sorted(packs)}")
    elif next(iter(packs)) != pack_sha256:
        problems.append("三签 pack_sha256 ≠ pack.sha256")
    return problems


def pack_manifest_commit(root: Path) -> "tuple[str | None, dict]":
    """证据包 manifest 的 ``git_commit``（三签/矩阵的绑定锚）。"""
    path = Path(root) / "evidence_pack" / "manifest.json"
    if not path.is_file():
        return None, {}
    manifest = _json(path)
    return manifest.get("git_commit"), manifest


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
    detail["origin_main_baseline"] = state["origin_main"]
    if state["origin_main"] != ORIGIN_MAIN:
        problems.append(f"Task 0 基线 origin/main={state['origin_main']} ≠ "
                        f"{ORIGIN_MAIN}")
    # 评审 W5-01：I10 的实时判据（基线或本阶段 HEAD，且为 HEAD 祖先）。
    live_origin = _git("rev-parse", "origin/main")
    live_head = _git("rev-parse", "HEAD")
    ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", live_origin, live_head],
        cwd=str(_REPO), capture_output=True, text=True, timeout=120
    ).returncode == 0
    detail["origin_main_live"] = live_origin
    detail["head"] = live_head
    detail["origin_is_ancestor"] = ancestor
    problems.extend(verify_origin_anchor(live_origin, live_head,
                                         ancestor=ancestor))
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
                   "all_passed": bool(golden["all_passed"]),
                   "expected": EXPECTED_CONTROLS["golden"]},
        "negative": {"count": int(negative["count"]),
                     "rejected": int(negative["rejected"]),
                     "all_passed": bool(negative["all_passed"]),
                     "expected": EXPECTED_CONTROLS["negative"]},
        "mutation": {"count": int(mutation["count"]),
                     "rejected": int(mutation["rejected"]),
                     "all_passed": bool(mutation["all_passed"]),
                     "expected": EXPECTED_CONTROLS["mutation"]},
    }
    detail.update(rows)
    # 评审 W4-15：规模先钉死（缩水不得算过），再判「全过/全拒」。
    for name, expected in EXPECTED_CONTROLS.items():
        if rows[name]["count"] != expected:
            problems.append(f"{name} 控制组规模 {rows[name]['count']} ≠ "
                            f"预注册 {expected}")
    if not rows["golden"]["all_passed"]:
        problems.append("golden 控制组未全通过")
    if rows["negative"]["rejected"] != rows["negative"]["count"]:
        problems.append("负例未全拒")
    if rows["mutation"]["rejected"] != rows["mutation"]["count"]:
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
    seen_relpaths: "dict[str, int]" = {}
    for row in files:
        rel = row["relpath"]
        # 评审 W4-09：relpath 必须是包内相对路径、无 ``..``、且唯一。
        seen_relpaths[rel] = seen_relpaths.get(rel, 0) + 1
        pure = PurePosixPath(rel)
        if pure.is_absolute() or rel.startswith("/"):
            problems.append(f"manifest relpath 为绝对路径：{rel}")
            continue
        if ".." in pure.parts:
            problems.append(f"manifest relpath 含 '..'：{rel}")
            continue
        path = pack / rel
        # 评审 W4-17：原来那条「同时是自己的父目录和自身」恒假；改成真正的
        # 越界判定——解析后必须仍在 pack 之内。
        resolved = path.resolve(strict=False)
        if resolved != pack_resolved and pack_resolved not in resolved.parents:
            problems.append(f"relpath 越出包目录：{rel}")
            continue
        if is_judgment_relpath(rel):
            problems.append(f"判决件混入包：{rel}")
        if rel.split("/", 1)[0] in ("closeout", "evidence_pack"):
            problems.append(f"判决目录/包自身混入包：{rel}")
        if rel in ("manifest.json", "pack.sha256"):
            problems.append(f"清单件混入自身条目：{rel}")
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
    dupes = sorted(rel for rel, count in seen_relpaths.items() if count > 1)
    if dupes:
        problems.append(f"manifest relpath 重复：{dupes[:5]}")
    # 评审 W4-01：超集校验——包树实际文件必须**恰好**等于
    # 清单 ∪ {manifest.json, pack.sha256}；后置混入的判决件/密钥/超限件
    # 不会改变 pack.sha256（它只覆盖 manifest 字节），必须由这里兜住。
    actual = {p.relative_to(pack).as_posix() for p in pack.rglob("*")
              if p.is_file() or p.is_symlink()}
    expected = set(seen_relpaths) | {"manifest.json", "pack.sha256"}
    unregistered = sorted(actual - expected)
    detail["n_unregistered"] = len(unregistered)
    if unregistered:
        problems.append(f"包内存在未登记文件（差集非空）：{unregistered[:5]}")
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
#: 自测 nodeid 的方括号参数（评审 W4-08）：规则①只豁免**参数内**的命中，
#: 不再整行豁免——同一行里出现真实凭据仍会被抓到。终判侧的解析独立实现
#: （逐字符找配对方括号），不复用打包侧的行内切分。
_SELFTEST_HEAD = "test_scan_detects_all_strict_forms"


def _selftest_params(line: str) -> "list[str]":
    """返回该行所有自测 nodeid 的方括号参数文本（判官侧独立解析）。"""
    params, cursor = [], 0
    while True:
        hit = line.find(_SELFTEST_HEAD, cursor)
        if hit < 0:
            return params
        cursor = hit + len(_SELFTEST_HEAD)
        if cursor >= len(line) or line[cursor] != "[":
            continue
        depth, start = 0, cursor
        while cursor < len(line):
            char = line[cursor]
            if char == "[":
                depth += 1
            elif char == "]":
                depth -= 1
                if depth == 0:
                    params.append(line[start + 1:cursor])
                    cursor += 1
                    break
            cursor += 1


def _in_selftest_param(line: str, matched: str) -> bool:
    """规则①收紧后的判据：命中值必须逐字出现在自测 nodeid 的参数内。"""
    return any(matched in param for param in _selftest_params(line))


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
    known = {row["relpath"] for row in manifest.get("files") or []}
    # 评审 W4-01/W4-09：扫描源改为**实际包树**（含未登记件），不再只信清单。
    targets = [(rel, pack / rel) for rel in sorted(known)]
    targets += [(p.relative_to(pack).as_posix(), p)
                for p in sorted(pack.rglob("*"))
                if (p.is_file() or p.is_symlink())
                and p.relative_to(pack).as_posix() not in known]
    for rel, path in targets:
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for lineno, line in enumerate(text.splitlines(), 1):
            for name, matched in secret_scan.scan_text(line):
                key = f"{rel}::{name}::L{lineno}"
                if _in_selftest_param(line, matched):
                    if key not in allowed:
                        problems.append(f"合成哨兵未在 manifest 登记：{key}")
                elif _synthetic_token(matched):
                    if key not in allowed:
                        problems.append(f"合成哨兵未在 manifest 登记：{key}")
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


BENCHMARK_CODE_DIRS = ("orchestrator/benchmark", "scripts/benchmark",
                       "tests/benchmark")


def _imports_gateway(path: Path) -> bool:
    """AST 判定（评审 W5-19）；语法不可解析时 fail-closed 视为引用。"""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
    except SyntaxError:
        return True
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "orchestrator.gateway" or \
                        alias.name.startswith("orchestrator.gateway."):
                    return True
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            module = node.module or ""
            if module == "orchestrator.gateway" or \
                    module.startswith("orchestrator.gateway."):
                return True
            if module == "orchestrator" and \
                    any(alias.name == "gateway" for alias in node.names):
                return True
    return False


def baseline_benchmark_files(
        ref: str, bases: "tuple[str, ...]" = BENCHMARK_CODE_DIRS) -> "set[str]":
    """``ref`` 时点的 benchmark 代码相对路径集（``git ls-tree``，fail-closed）。

    锁内适配（Phase 2.8.1 T0，计划 §16.2 只放了 `AUTHORIZED_PUSH_HEADS` 追加）：
    本检查是 **Phase 2.8.0 的独立复算**，扫描面必须钉死在 2.8.0 冻结包所对应的
    那个提交上。Phase 2.8.1 按计划在 ``tests/benchmark/`` 与
    ``orchestrator/benchmark/phase281/`` 下新增代码，它们合法引用
    ``orchestrator.gateway``（那是**本阶段**自己的账本）；沿用活树 ``rglob``
    会把后续相位的代码算进 2.8.0 的复算，让 2.8.0 终判因**跨相位误伤**变红。
    改成按冻结提交的文件清单逐件复算后：2.8.0 的每一件旧代码仍被逐件 AST 判
    （判据强度不变，新增文件也无法稀释它），只是不再把别人的代码记成 2.8.0 的。

    空集合**不是**「全过」而是判据失效（评审 W5-19 的 fail-closed 口径）：
    ``git ls-tree`` 指错提交会得到 0 件，若静默返回 0 命中就是假绿。
    """
    out = _git("ls-tree", "-r", "--name-only", ref, "--", *bases)
    files = {line.strip() for line in out.splitlines()
             if line.strip().endswith(".py")}
    if not files:
        raise JudgeError(
            f"提交 {ref} 在 benchmark 目录下没有任何 .py——Gateway 引用复算面为空，"
            "判据失效（指错提交？）")
    return files


def gateway_scan_scope(root: Path) -> "set[str] | None":
    """Gateway 引用复算的扫描面。

    * 冻结包在场 → 包 ``manifest.json`` 登记的 ``git_commit``（2.8.0 六条重建
      提交后的 ``82a1427``），按该提交的文件清单逐件复算——与活工作树解耦；
    * 冻结包未建（打包/冻结前的 ``closeout matrix``）→ ``None``，沿用原口径
      全扫活树。
    """
    manifest = Path(root) / "evidence_pack" / "manifest.json"
    if not manifest.is_file():
        return None
    doc = _json(manifest)
    return baseline_benchmark_files(str(doc["git_commit"]))


def count_gateway_imports(
        bases: "tuple[str, ...]" = BENCHMARK_CODE_DIRS,
        *, only: "set[str] | None" = None) -> "tuple[int, list[str]]":
    """统计基准代码里的 Gateway 引用；``only`` 给定时只算该相对路径集
    （见 :func:`baseline_benchmark_files` 的锁内适配说明）。"""
    hits = []
    for base in bases:
        for path in sorted((_REPO / base).rglob("*.py")):
            if only is not None and path.relative_to(_REPO).as_posix() not in only:
                continue
            if _imports_gateway(path):
                hits.append(str(path))
    return len(hits), hits


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
    # 评审 W5-19：按**真实 import 图**（AST）判，不再用行级正则——正则漏
    # 多行 import、`from orchestrator import gateway`、括号换行等形态。
    scope = gateway_scan_scope(root)
    count, hits = count_gateway_imports(BENCHMARK_CODE_DIRS, only=scope)
    detail["gateway_imports_in_benchmark"] = count
    detail["gateway_scan_scope"] = "live" if scope is None else f"commit:{len(scope)}件"
    for hit in hits:
        problems.append(f"benchmark 代码引用 Gateway：{hit}")
    return problems, detail


def _baseline_head(root: Path) -> str:
    """Task 0 登记的 HEAD（``intake/repository_state.json``）——矩阵/invariants
    的 ``baseline_head`` 锚。"""
    return _json(Path(root) / "intake" / "repository_state.json")["head"]


#: 锁内适配（Phase 2.8.1 T0）：**封闭阶段判定链的 HEAD 视图**。
#:
#: Phase 2.8.0 的 ``check_matrix`` / ``check_invariants`` 各有一条
#: 「pack/invariants 的 commit == 活 HEAD」的等式。它比对的是**收尾提交当时**
#: 的 HEAD；任何后续提交（哪怕只改文档）都会让这两条不等。Phase 2.8.1 必须
#: 证明「新代码没有破坏 2.8.0 的判据链」（AC-P281-0-11），而新代码本身就意味着
#: HEAD 已经前进——于是「在当前代码上重跑仍 Established」与「HEAD 必须相等」
#: 在定义上互斥。
#:
#: 处置：允许调用方**显式**声明「以哪个提交为 HEAD 视图」（``head_override``），
#: 缺省为 ``None`` = 活 HEAD，**判据本身一字不改**（仍是精确相等，不是前缀/
#: 正则/包含）。这不是放水：它把「提交身份」这一项从「活 HEAD」换成「调用方
#: 声明并留痕的提交」，而 pack 自身登记的那个 sha 正是唯一合法的取值——写错
#: 一个字符就仍然不等、仍然红。CLI 侧不暴露该参数（生产判定永远看活 HEAD）。
_JUDGE_HEAD_VIEW: "contextvars.ContextVar[str | None]" = contextvars.ContextVar(
    "judge_head_view", default=None)


def _judge_head() -> str:
    """判定链看到的 HEAD（有显式视图用视图，否则查活 HEAD）。"""
    view = _JUDGE_HEAD_VIEW.get()
    return view if view is not None else _git("rev-parse", "HEAD")


def _collect_signs(root: Path) -> "tuple[list[str], list[dict], dict]":
    """三签读入 + 与 pack/commit 绑定的**单一实现**（评审 W4-18 去重）。

    ``check_signs`` 与 ``check_acceptance`` 都走这里，字段口径只有一份。
    """
    problems = []
    signs_dir = Path(root) / "closeout" / "signs"
    signs = []
    for role in ROLES:
        path = signs_dir / f"{role}.json"
        if not path.is_file():
            problems.append(f"缺三签：{role}.json")
            continue
        signs.append(_json(path))
    pack_path = Path(root) / "evidence_pack" / "pack.sha256"
    if not pack_path.is_file():
        problems.append("缺 evidence_pack/pack.sha256")
        return problems, signs, {"pack_sha256": None}
    pack_sha = pack_path.read_text(encoding="utf-8").strip()
    expected_commit, _manifest = pack_manifest_commit(root)
    if expected_commit is None:
        problems.append("缺 evidence_pack/manifest.json 的 git_commit")
    if signs:
        problems.extend(validate_signs(signs, pack_sha,
                                       expected_commit=expected_commit))
    return problems, signs, {"pack_sha256": pack_sha,
                             "git_commit": expected_commit,
                             "roles": [s.get("role") for s in signs]}


def check_signs(root: Path) -> "tuple[list[str], dict]":
    """P0-AC-20：三签逐件校验并与 `pack.sha256`/`git_commit` 绑定。"""
    problems, _signs, detail = _collect_signs(root)
    return problems, detail


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


#: 评审 W5-17：container 车道的下限条数。低于此值即视为「标记没生效/被整套
#: deselect」，而不是「容器车道通过」——防止空绿。
CONTAINER_LANE_MIN = 2
#: 里程碑车道同理（评审修复批次自伤回归：5083b52 把
#: ``SIMPRAXIS_MILESTONE_CLOSEOUT=1`` 丢在重构缝里，车道 0 条全 deselect、
#: rc=5，而旧判据只要求 rc=0/passed≥1，等于「什么都没验也算跑过」）。
#: 下限取 10 而不是 1：车道当前常态采集 19 条，若哪天被削到 1 条，
#: ``passed == selected`` 仍会成立、下限 1 也仍成立——那正是空绿的近亲
#: （不足但不为零）。10 是「里程碑主体仍在」的保守地板，且不随新增用例漂移
#: （19→20 之类不会误红）；真删到 10 条以下属实质性删减，必须红。
MILESTONE_LANE_MIN = 10


def _check_lane(detail: dict, doc: dict, log: Path, label: str) -> "list[str]":
    problems = []
    if not log.is_file():
        return [f"缺{label}日志：{log.name}"]
    if doc.get("rc") != 0:
        problems.append(f"{label} rc={doc.get('rc')}")
    if int(doc.get("failed", -1)) != 0:
        problems.append(f"{label} failed={doc.get('failed')}")
    if int(doc.get("passed", 0)) < 1:
        problems.append(f"{label} 0 passed")
    if _sha256(log) != doc.get("log_sha256"):
        problems.append(f"{label}日志 sha256 与记录不符")
    summary = parse_pytest_summary(log.read_text(encoding="utf-8",
                                                 errors="ignore"))
    if summary is None or summary["passed"] != int(doc.get("passed", -1)):
        problems.append(f"{label}日志摘要与 json 不符")
    detail[f"{label}.passed"] = doc.get("passed")
    detail[f"{label}.failed"] = doc.get("failed")
    return problems


def check_acceptance(root: Path) -> "tuple[list[str], dict]":
    problems, detail = [], {}
    lane_json = root / "closeout" / "acceptance_lane.json"
    lane_log = root / "closeout" / "acceptance_lane.log"
    container_log = root / "closeout" / "container_lane.log"
    if not lane_json.is_file() or not lane_log.is_file():
        return ["缺验收车道产物（acceptance_lane.json/.log）"], {}
    lane = _json(lane_json)
    detail.update({k: lane.get(k) for k in ("cmd", "rc", "passed", "failed",
                                           "summary")})
    problems.extend(_check_lane(detail, lane, lane_log, "验收车道"))
    #: 里程碑车道同样不许「标记没开所以 0 条」的空绿：采集条数与实跑条数
    #: 必须相等且不低于下限（``closeout.acceptance_envs()`` 负责开闸，
    #: ``test_ac_p0_7_9_acceptance_lane_env_logic`` 把开关钉成机判）。
    ml_selected = int(lane.get("selected", 0))
    detail["验收车道.selected"] = ml_selected
    if ml_selected < MILESTONE_LANE_MIN:
        problems.append(
            f"验收车道采集 {ml_selected} 条 < 下限 {MILESTONE_LANE_MIN}"
            f"（标记未生效或测试被删）")
    elif int(lane.get("passed", 0)) != ml_selected:
        problems.append(
            f"验收车道 passed={lane.get('passed')} ≠ 采集 {ml_selected}")

    # 评审 W5-17：容器车道是验收的第二段，必须真跑且记录采集条数——只认
    # passed ≥ 下限，不认「标记没开所以 0 条」的空绿。
    container = lane.get("container_lane") or {}
    if not container:
        problems.append("验收车道缺 container_lane 段（W5-17）")
    else:
        problems.extend(_check_lane(detail, container, container_log,
                                    "容器车道"))
        selected = int(container.get("selected", 0))
        passed = int(container.get("passed", 0))
        detail["容器车道.selected"] = selected
        if selected < CONTAINER_LANE_MIN:
            problems.append(
                f"容器车道采集 {selected} 条 < 下限 {CONTAINER_LANE_MIN}"
                f"（标记未生效或测试被删）")
        if passed != selected:
            problems.append(f"容器车道 passed={passed} ≠ 采集 {selected}")

    # 评审 W4-18：与 `--check signs` 共用同一实现，不再内联第二份。
    sign_problems, _signs, sign_detail = _collect_signs(root)
    problems.extend(sign_problems)
    detail.update(sign_detail)
    return problems, detail


AC_EXPECTED = tuple(f"P0-AC-{i:02d}" for i in range(1, 21))
T7_EXPECTED = tuple(f"AC-T7-{i}" for i in range(1, 10))


def check_matrix(root: Path) -> "tuple[list[str], dict]":
    """验收矩阵的机判消费者（评审 W4-02 / W5-05）。

    不采信矩阵自述：20 条 AC 逐条复算（能独立复算的类别直接调对应
    ``check_*``），commit/pack 三方绑定（矩阵 == 包清单 == HEAD）。
    """
    problems, detail = [], {}
    path = Path(root) / "closeout" / "acceptance_matrix.json"
    if not path.is_file():
        return ["缺 closeout/acceptance_matrix.json"], {}
    matrix = _json(path)
    if matrix.get("schema") != "simpraxis.benchmark.acceptance_matrix.v1":
        problems.append(f"acceptance_matrix schema={matrix.get('schema')}")
    acs = matrix.get("acs") or []
    rows = {row.get("ac"): row for row in acs}
    detail["n_acs"] = len(acs)
    if sorted(rows) != list(AC_EXPECTED):
        problems.append(f"矩阵 AC 集不符：{sorted(rows)}")
    for ac_id in AC_EXPECTED:
        row = rows.get(ac_id)
        if row is None:
            problems.append(f"矩阵缺 {ac_id}")
            continue
        if row.get("result") != "PASS":
            problems.append(f"{ac_id}: result={row.get('result')}")
        if not str(row.get("evidence") or "").strip():
            problems.append(f"{ac_id}: 无 evidence")
    summary = matrix.get("summary") or {}
    detail["summary"] = {k: summary.get(k) for k in
                         ("total", "passed", "failed", "gaps")}
    if int(summary.get("total", -1)) != 20:
        problems.append(f"summary.total={summary.get('total')} ≠ 20")
    if int(summary.get("failed", -1)) != 0:
        problems.append(f"summary.failed={summary.get('failed')} ≠ 0")
    if int(summary.get("passed", -1)) != 20:
        problems.append(f"summary.passed={summary.get('passed')} ≠ 20")
    t7 = {row.get("ac") for row in matrix.get("task7_acs") or []}
    if not set(T7_EXPECTED) <= t7:
        problems.append(f"task7_acs 未覆盖全部九条：缺 "
                        f"{sorted(set(T7_EXPECTED) - t7)}")
    # ---- 三方绑定：矩阵 commit/pack == 包清单 == HEAD ----
    manifest_commit, _manifest = pack_manifest_commit(root)
    head = _judge_head()
    pack_path = Path(root) / "evidence_pack" / "pack.sha256"
    pack_sha = (pack_path.read_text(encoding="utf-8").strip()
                if pack_path.is_file() else None)
    detail.update({"commit": matrix.get("commit"),
                   "manifest_git_commit": manifest_commit,
                   "head": head,
                   "pack_sha256": matrix.get("pack_sha256"),
                   "pack_file_sha256": pack_sha})
    if matrix.get("commit") != manifest_commit:
        problems.append(f"矩阵 commit={matrix.get('commit')} ≠ "
                        f"manifest.git_commit={manifest_commit}")
    if manifest_commit != head:
        problems.append(f"manifest.git_commit={manifest_commit} ≠ HEAD={head}")
    if pack_sha is None:
        problems.append("缺 evidence_pack/pack.sha256")
    elif matrix.get("pack_sha256") != pack_sha:
        problems.append("矩阵 pack_sha256 ≠ pack.sha256")
    baseline_head = _baseline_head(root)
    if matrix.get("baseline_head") != baseline_head:
        problems.append(f"矩阵 baseline_head={matrix.get('baseline_head')} ≠ "
                        f"Task 0 HEAD {baseline_head}")
    # ---- 逐条独立复算（W5-05：矩阵行不得只靠自述）----
    recompute = {}
    try:
        recompute["P0-AC-16"] = not check_paid(root)[0]
        recompute["P0-AC-17"] = not check_kb(root)[0]
        recompute["P0-AC-18"] = not check_ledgers(root)[0]
        recompute["P0-AC-19"] = not check_cold_run(root)[0]
        recompute["P0-AC-20"] = not _collect_signs(root)[0]
    except (JudgeError, OSError, ValueError, KeyError) as exc:
        problems.append(f"矩阵逐条复算失败：{type(exc).__name__}: {exc}")
    detail["recomputed"] = recompute
    for ac_id, ok in recompute.items():
        row = rows.get(ac_id) or {}
        if row.get("result") != ("PASS" if ok else "FAIL"):
            problems.append(f"{ac_id}: 矩阵 result={row.get('result')} 与独立"
                            f"复算 {'PASS' if ok else 'FAIL'} 不符")
        if row.get("strength") not in (None, "recomputed"):
            problems.append(f"{ac_id}: strength={row.get('strength')} 应为 "
                            f"recomputed")
    return problems, detail


INVARIANT_IDS = tuple(f"I{i}" for i in range(1, 13))


def check_invariants(root: Path) -> "tuple[list[str], dict]":
    """I1..I12 的终判复核（评审 W5-06）：`invariants.json` 逐条 + 绑定。"""
    problems, detail = [], {}
    path = Path(root) / "closeout" / "invariants.json"
    if not path.is_file():
        return ["缺 closeout/invariants.json（先跑 closeout matrix）"], {}
    doc = _json(path)
    if doc.get("schema") != "simpraxis.benchmark.invariants.v1":
        problems.append(f"invariants schema={doc.get('schema')}")
    listed = [row.get("id") for row in doc.get("invariants") or []]
    rows = {iid: row for iid, row in zip(listed, doc.get("invariants") or [])}
    detail["n_rows"] = len(listed)
    # 集齐判：`sorted(rows)` 是字典序（I1,I10,I11,I12,I2,…），与数值序的
    # INVARIANT_IDS 恒不相等，于是 12 条全 ok 的完美件也会被点名「集不齐」——
    # 终判从此永不可能 Established（Phase 2.8.0 验证车道现场抓出；5083b52
    # 引入，原测试只断言「I7 在 problems 里」，覆盖不到「完美件零问题」）。
    # 改为「条数相等 + 集合相等」，并因此比旧写法更严：重复 id 原先会被 dict
    # 静默吞掉，现在按条数不符报红。
    if len(listed) != len(INVARIANT_IDS) or set(listed) != set(INVARIANT_IDS):
        problems.append(f"不变量集不齐：{sorted(map(str, listed))}"
                        f"（应为 I1..I12 各恰好一次）")
    for iid in INVARIANT_IDS:
        row = rows.get(iid)
        if row is None:
            continue
        if row.get("ok") is not True:
            problems.append(f"{iid} ok={row.get('ok')}："
                            f"{str(row.get('evidence'))[:200]}")
    head = _judge_head()
    detail.update({"verdict": doc.get("verdict"), "commit": doc.get("commit"),
                   "baseline_head": doc.get("baseline_head"), "head": head})
    if doc.get("verdict") != "OK":
        problems.append(f"invariants.verdict={doc.get('verdict')} 非 OK")
    if doc.get("commit") != head:
        problems.append(f"invariants.commit={doc.get('commit')} ≠ HEAD={head}")
    baseline_head = _baseline_head(root)
    if doc.get("baseline_head") != baseline_head:
        problems.append(f"invariants.baseline_head={doc.get('baseline_head')} "
                        f"≠ Task 0 HEAD {baseline_head}")
    return problems, detail


def check_gaps(root: Path) -> "tuple[list[str], dict]":
    """acceptance_matrix 的 gaps 逐条必须有归属（owner/reason/evidence）。

    detail 与其他检查同形（dict），``run_judge`` 用 ``detail.get("gaps")`` 取
    结论件顶层的 gaps 列表；早期版本这里直接返回 list 会让
    ``--require-acceptance`` 抛 ``AttributeError``（不是判定失败，是崩溃）。
    """
    path = root / "closeout" / "acceptance_matrix.json"
    if not path.is_file():
        return ["缺 closeout/acceptance_matrix.json"], {"gaps": []}
    matrix = _json(path)
    gaps = matrix.get("gaps") or []
    problems = []
    for gap in gaps:
        for key in ("id", "owner", "reason", "evidence"):
            if not str(gap.get(key) or "").strip():
                problems.append(f"gap {gap.get('id')}: 缺 {key}（无归属）")
    return problems, {"gaps": gaps, "count": len(gaps)}


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
    "acceptance": check_acceptance,
    "matrix": check_matrix,
    "invariants": check_invariants,
    "gaps": check_gaps,
}


def run_judge(root: Path, *, checks: "tuple[str, ...]" = CHECKS,
              require_acceptance: bool = False,
              pack_sha256: "str | None" = None,
              head_view: "str | None" = None) -> dict:
    """跑判定链；返回判决件 dict。

    ``head_view``（锁内适配，Phase 2.8.1 T0）：显式声明「以哪个提交为 HEAD
    视图」，供**封闭阶段**在后续提交上复核判定链时使用（见 ``_JUDGE_HEAD_VIEW``
    的注释）。缺省 ``None`` = 活 HEAD；判据仍是精确相等。本参数不暴露给 CLI。
    """
    root = Path(root)
    token = _JUDGE_HEAD_VIEW.set(head_view)
    try:
        return _run_judge_inner(root, checks=checks,
                                require_acceptance=require_acceptance,
                                pack_sha256=pack_sha256)
    finally:
        _JUDGE_HEAD_VIEW.reset(token)


def _run_judge_inner(root: Path, *, checks: "tuple[str, ...]",
                     require_acceptance: bool,
                     pack_sha256: "str | None") -> dict:
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
        for name in ACCEPTANCE_CHECKS:
            try:
                probs, detail = CHECK_FUNCS[name](root)
            except (JudgeError, OSError, ValueError, KeyError) as exc:
                probs, detail = [f"{type(exc).__name__}: {exc}"], {}
            results[name] = {"ok": not probs, "problems": probs,
                             "detail": detail}
            problems.extend(f"{name}: {p}" for p in probs)
            if name == "gaps":
                gaps = detail.get("gaps") or []
        acceptance = {"required": True,
                      "ok": all(results[name]["ok"]
                                for name in ACCEPTANCE_CHECKS)}
    # 评审 W4-03：只有全集（11 类核心 + 验收四类）跑过才可能 Established；
    # 子集运行如实记 Incomplete，避免 ``--check signs`` 伪造终判。
    complete = (set(checks) == set(CHECKS) and require_acceptance)
    if problems:
        verdict = "Not Established"
    elif not complete:
        verdict = "Incomplete"
    else:
        verdict = "Established"
    doc = {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%SZ"),
        "root": str(root),
        "commit": _git("rev-parse", "HEAD"),
        # 锁内适配（Phase 2.8.1 T0）：仅在显式声明视图时留痕，缺省判决件
        # 形状与 Phase 2.8.0 逐字相同（不新增键）。
        **({"commit_view": _JUDGE_HEAD_VIEW.get()}
           if _JUDGE_HEAD_VIEW.get() is not None else {}),
        "pack_sha256": pack_sha256 or (root / "evidence_pack" /
                                       "pack.sha256").read_text(
                                           encoding="utf-8").strip()
        if (root / "evidence_pack" / "pack.sha256").is_file() else None,
        "checks_run": list(checks),
        "checks_required": list(CHECKS) + list(ACCEPTANCE_CHECKS),
        "checks": results,
        "acceptance": acceptance,
        "problems": problems,
        "gaps": gaps,
        "verdict": verdict,
    }
    return doc


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _resolve_rc(root: Path, rc: "int | None") -> int:
    """冷跑 rc 的来源（评审 W4-10）：显式参数 → ``closeout/cold_run.rc`` 文件
    → fail-closed。**禁止**从日志末行文本推断（`1 error`/崩溃/被信号杀死时
    末行不含 ``failed``，会被记成 rc=0）。"""
    if rc is not None:
        return int(rc)
    rc_file = Path(root) / "closeout" / "cold_run.rc"
    if rc_file.is_file():
        text = rc_file.read_text(encoding="utf-8").strip()
        match = re.fullmatch(r"(?:rc=)?(\d+)", text)
        if not match:
            raise JudgeError(f"cold_run.rc 不可解析：{text!r}")
        return int(match.group(1))
    raise JudgeError(
        "冷跑 rc 未知：请传 --rc，或先写 closeout/cold_run.rc（形如 `rc=0`）")


def record_cold_run(root: Path, raw_log: Path, rc: "int | None" = None) -> dict:
    """解析冷跑日志 + 测量 ``N_new``，写 ``closeout/cold_run.json``（§4.1）。"""
    import os

    resolved_rc = _resolve_rc(root, rc)
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
        # 评审 W5-07：§4.1 要求**排序后** sha256（与 verify_cold_run 同口径）。
        "nodeids_sha256": hashlib.sha256(
            "\n".join(sorted(nodeids)).encode()).hexdigest(),
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
        "rc": resolved_rc,
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
    # 评审 W4-10：rc 同时落独立侧车（下一轮缺 --rc 时可复读，不再从日志推断）。
    rc_side = root / "closeout" / "cold_run.rc"
    rc_side.write_text(f"rc={resolved_rc}\n", encoding="utf-8")
    rc_side.chmod(0o600)
    return doc


def main(argv: "list[str] | None" = None) -> int:
    from orchestrator.benchmark import paths

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=None)
    parser.add_argument("--check", choices=tuple(CHECK_FUNCS), action="append",
                        help="只跑某类检查（可重复；缺省全跑）")
    parser.add_argument("--report", default=None,
                        help="判决件路径（缺省 <root>/closeout/closure_judge.json）")
    parser.add_argument("--require-acceptance", action="store_true",
                        help="追加验收车道/三签/矩阵/不变量四类检查（终判必须带）")
    parser.add_argument("--record-cold-run", default=None,
                        metavar="RAW_LOG", help="解析冷跑日志并写 cold_run.json")
    parser.add_argument("--rc", type=int, default=None,
                        help="冷跑进程退出码；缺省读 closeout/cold_run.rc，"
                             "缺失即 fail-closed（评审 W4-10）")
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
    # 评审 W5-18：子集运行（--check）不得顺手覆盖终判件——只打印；要落盘
    # 必须显式给 --report（且判决必为 Incomplete，见 run_judge）。
    if args.check and args.report is None:
        print(json.dumps({"verdict": doc["verdict"],
                          "problems": doc["problems"],
                          "checks": {k: v["ok"]
                                     for k, v in doc["checks"].items()},
                          "report_written": None},
                         ensure_ascii=False, indent=1, sort_keys=True))
        return 0 if doc["verdict"] == "Established" else 1
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=1,
                              sort_keys=True) + "\n", encoding="utf-8")
    out.chmod(0o600)
    print(json.dumps({"verdict": doc["verdict"],
                      "problems": doc["problems"],
                      "checks": {k: v["ok"] for k, v in doc["checks"].items()}},
                     ensure_ascii=False, indent=1, sort_keys=True))
    # 评审 W4-03/W4-06：rc 跟随判决——只有 Established 才是 0，Incomplete/
    # Not Established 一律非 0，自动化不得把「没跑全集」当绿灯。
    return 0 if doc["verdict"] == "Established" else 1


if __name__ == "__main__":
    raise SystemExit(main())
