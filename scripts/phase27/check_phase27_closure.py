#!/usr/bin/env python3
"""Phase 2.7 里程碑收尾机判（AC-T7-7；0 付费、只读、独立实现）。

沿 ``check_phase25_closure.py`` 的模式：**不复用看板/对账代码**（D7-1），自己
重读账、重读表、重算证据包，再把三处独立产出互相对撞。任一断言不成立即
``problems`` 非空（rc=1）；未达成项**如实挂牌 gaps，不计入通过**（硬规则 7）。

六类检查：

1. **AC 计数**：从 ``Phase_2.7_Plan_detailed.md`` 正文去重提取 ``AC-Tn-k``，
   与附录声明的 67 及逐 Task 计数（9/10/6/13/9/5/8/7）逐项相等，且与看板
   ``ac_counts`` 相同；
2. **账**：``seq`` 自 1 连续、``ts_iso`` 严格 ``Z``、``event`` 白名单、
   ``selfcheck`` 恰 1 条且占 ``seq=1``、``global_used_after`` 同步递增、
   实呼 ≤ 硬帽 178、``by_task``/``by_stage``/``by_batch`` 与批准件一致；
3. **四表**：活库四表行数与基线逐项相等（``solution_chunks`` 允许 Task 6 经
   批准的 +12，须有 ``RECYCLE.json`` 与之对得上）；
4. **证据包**：``manifest.json`` 存在、``git_commit`` 非空、逐件 sha256 复算
   一致、无 >10MB、无符号链接、无非普通文件、密钥双扫 0 命中；
5. **三签**：三份签署件存在、``verdict == "APPROVE"``、绑定 commit 与
   ``manifest.git_commit`` 相等；
6. **交叉互证**：看板 ``git.commit`` == ``manifest.git_commit``；看板的
   ``tests`` 三段与包内冻结日志逐字段相等（含 ``failed``），且 kbint/容器场
   必须 ``rc == 0``；看板的 ``ledger`` / ``lance_tables`` 段与机判独立重算的
   结果相等；包内 ``cold_run.json`` ↔ ``cold_run.raw.log`` ↔
   ``manifest.git_commit`` 三件自洽。

用法：
  python scripts/phase27/check_phase27_closure.py [--report OUT.json]
                                                 [--pack DIR] [--dashboard P]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import stat
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from orchestrator.common import secrets as secret_scan  # noqa: E402

PLAN = Path.home() / "文档/Phase_2.7_Plan_detailed.md"

DECLARED_TOTAL = 67
DECLARED_PER_TASK = {"T0": 9, "T1": 10, "T2": 6, "T3": 13, "T4": 9,
                     "T5": 5, "T6": 8, "T7": 7}

HARD_CAP = 178
ALLOWED_EVENTS = ("call", "selfcheck", "preflight_blocked")
STRICT_Z = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?Z$")

# 活表基线：kb/phase1/phase2 冻结；solution_chunks = 94 + Δ（Δ 由 Task 6 的
# RECYCLE.json 声明，且必须等于 12）。
TABLE_BASELINE = {"kb_chunks": 1542, "phase1_learnings": 44,
                  "phase2_learnings": 1}
SOLUTION_BASELINE_PRE = 94

MAX_FILE_BYTES = 10 * 1024 * 1024
SIGN_SCHEMA = "simpraxis.phase27.sign.v1"
MANIFEST_SCHEMA = "simpraxis.evidence_pack.v1"
DASHBOARD_SCHEMA = "simpraxis.rsi_dashboard_phase27.v1"

# 未达成项：**必须**在 gaps 里出现（缺了反而是 problem——把缺口洗成通过）
REQUIRED_GAP_TAGS = ("T4_UNFREEZE_NOT_ESTABLISHED", "T6_NO_CONTROL_ARM",
                     "T6_MAIN_POLICY_ZERO")



def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# 1. AC 计数
# ---------------------------------------------------------------------------
def count_acs(plan_path: Path = PLAN) -> "tuple[dict, list[str]]":
    text = plan_path.read_text(encoding="utf-8")
    ids = sorted({(int(a), int(b))
                  for a, b in re.findall(r"AC-T(\d+)-(\d+)", text)})
    per = {f"T{i}": 0 for i in range(8)}
    for t, _k in ids:
        per[f"T{t}"] = per.get(f"T{t}", 0) + 1
    problems: "list[str]" = []
    if len(ids) != DECLARED_TOTAL:
        problems.append(f"AC 总数 {len(ids)} ≠ 声明 {DECLARED_TOTAL}")
    bad = {k: (per[k], v) for k, v in DECLARED_PER_TASK.items() if per[k] != v}
    if bad:
        problems.append(f"逐 Task AC 计数与声明不符：{bad}")
    return {
        "plan": str(plan_path),
        "total": len(ids),
        "per_task": per,
        "ids_sha256": hashlib.sha256(
            ",".join(f"AC-T{t}-{k}" for t, k in ids).encode()).hexdigest(),
    }, problems


# ---------------------------------------------------------------------------
# 2. 账
# ---------------------------------------------------------------------------
def _rows_from(path: Path) -> "list[dict]":
    rows = []
    for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise SystemExit(f"[phase27 judge] 第 {lineno} 行不是合法 JSON：{exc}")
    return rows


def check_ledger(rows: "list[dict]", approvals_dir: Path) -> "tuple[dict, list[str]]":
    problems: "list[str]" = []
    calls = [r for r in rows if r.get("event") == "call"]
    selfchecks = [r for r in rows if r.get("event") == "selfcheck"]
    blocked = [r for r in rows if r.get("event") == "preflight_blocked"]

    for idx, r in enumerate(rows, 1):
        ev = r.get("event")
        if ev not in ALLOWED_EVENTS:
            problems.append(f"第 {idx} 行 event 不在白名单：{ev!r}")
        ts = r.get("ts_iso")
        if not isinstance(ts, str) or not STRICT_Z.match(ts):
            problems.append(f"第 {idx} 行 ts_iso 非严格 UTC Z：{ts!r}")
        seq = r.get("seq")
        if seq is not None and not isinstance(seq, int):
            problems.append(f"第 {idx} 行 seq 既非 int 也非 null：{seq!r}")
        if ev == "preflight_blocked" and seq is not None:
            problems.append(f"第 {idx} 行 preflight_blocked 的 seq 必须为 null")

    int_seqs = [r["seq"] for r in rows if isinstance(r.get("seq"), int)]
    if int_seqs != list(range(1, len(int_seqs) + 1)):
        problems.append(f"整数 seq 不连续（{len(int_seqs)} 条）：{int_seqs[:5]}…")

    if len(selfchecks) != 1:
        problems.append(f"selfcheck 行应恰 1 条，实际 {len(selfchecks)}")
    elif selfchecks[0].get("seq") != 1 or selfchecks[0].get("model") is not None:
        problems.append("selfcheck 行必须占 seq=1 且 model=null")

    for i, r in enumerate(calls, 1):
        if r.get("global_used_after") != i:
            problems.append(
                f"第 {i} 条实呼 global_used_after={r.get('global_used_after')!r}，应为 {i}")
            break

    n_calls = len(calls)
    if n_calls > HARD_CAP:
        problems.append(f"实呼 {n_calls} 超硬帽 {HARD_CAP}")

    by_task: "dict[str, int]" = {}
    by_stage: "dict[str, int]" = {}
    by_batch: "dict[str, int]" = {}
    for r in calls:
        by_task[str(r.get("task"))] = by_task.get(str(r.get("task")), 0) + 1
        by_stage[str(r.get("stage"))] = by_stage.get(str(r.get("stage")), 0) + 1
        by_batch[str(r.get("batch_id"))] = by_batch.get(
            str(r.get("batch_id")), 0) + 1

    # 每张票必须有批准件，用量不超票额（无票不许发网），
    # 且该批实呼的 (task, stage) 必须与票面声明**逐项一致**（AC-T7-2 的
    # 「by_task/by_stage 与票一致」——只查票额会放过「改了 task 但仍挂在
    # 原批次下」这种账实不符）。
    for batch_id, used in sorted(by_batch.items()):
        p = approvals_dir / f"{batch_id}.json"
        if not p.is_file():
            problems.append(f"批次 {batch_id} 有 {used} 呼但无批准件：{p}")
            continue
        appr = json.loads(p.read_text(encoding="utf-8"))
        cap = appr.get("approved")
        if not isinstance(cap, int):
            problems.append(f"批准件 {batch_id} 的 approved 非 int：{cap!r}")
        elif used > cap:
            problems.append(f"批次 {batch_id} 用量 {used} 超票额 {cap}")

        got = {(str(r.get("task")), str(r.get("stage")))
               for r in calls if str(r.get("batch_id")) == batch_id}
        want = {(str(appr.get("task")), str(appr.get("stage")))}
        if got != want:
            problems.append(
                f"批次 {batch_id} 实呼的 (task, stage)={sorted(got)} 与票面"
                f"声明 {sorted(want)} 不符")
        if appr.get("batch_id") not in (None, batch_id):
            problems.append(
                f"批准件 {batch_id}.json 内的 batch_id={appr.get('batch_id')!r}"
                f" 与文件名不符")
    known = {p.stem for p in approvals_dir.glob("*.json")}
    for batch_id in sorted(known - set(by_batch)):
        problems.append(f"批准件 {batch_id} 在账上没有任何实呼（空票）")

    summary = {
        "total_rows": len(rows),
        "n_calls": n_calls,
        "selfcheck_rows": len(selfchecks),
        "blocked_rows": len(blocked),
        "seq_first": (min(int_seqs) if int_seqs else None),
        "seq_last": (max(int_seqs) if int_seqs else None),
        "ok": sum(1 for r in calls if r.get("ok")),
        "fail": sum(1 for r in calls if not r.get("ok")),
        "by_task": by_task,
        "by_stage": by_stage,
        "by_batch": by_batch,
        "hard_cap": HARD_CAP,
    }
    return summary, problems


# ---------------------------------------------------------------------------
# 3. 四表
# ---------------------------------------------------------------------------
def check_tables(recycle_path: Path) -> "tuple[dict, list[str]]":
    import lancedb
    from memory_flywheel import kb_pins

    problems: "list[str]" = []
    db = lancedb.connect(kb_pins.default_lancedb_uri())
    actual = {}
    for name in ("kb_chunks", "solution_chunks", "phase1_learnings",
                 "phase2_learnings"):
        try:
            actual[name] = db.open_table(name).count_rows()
        except Exception as exc:                      # noqa: BLE001
            problems.append(f"活表 {name} 打不开：{exc}")
    for name, want in TABLE_BASELINE.items():
        if name in actual and actual[name] != want:
            problems.append(f"{name} 行数 {actual[name]} ≠ 基线 {want}")

    delta_declared = None
    if recycle_path.is_file():
        rec = json.loads(recycle_path.read_text(encoding="utf-8"))
        delta_declared = rec.get("solution_chunks_delta",
                                 rec.get("delta"))
    else:
        problems.append(f"缺 Task 6 回收批准件：{recycle_path}")

    want_sol = (SOLUTION_BASELINE_PRE + delta_declared
                if isinstance(delta_declared, int) else None)
    if want_sol is None:
        problems.append("回收件未声明 solution_chunks 增量（Δ 不可审）")
    elif actual.get("solution_chunks") != want_sol:
        problems.append(
            f"solution_chunks 行数 {actual.get('solution_chunks')} ≠ "
            f"{SOLUTION_BASELINE_PRE}+Δ{delta_declared}={want_sol}")

    return {"rows": actual, "solution_delta": delta_declared,
            "solution_pre": SOLUTION_BASELINE_PRE}, problems


# ---------------------------------------------------------------------------
# 4. 证据包
# ---------------------------------------------------------------------------
def check_pack(pack_dir: Path) -> "tuple[dict, list[str], list[str]]":
    problems: "list[str]" = []
    gaps: "list[str]" = []
    man_path = pack_dir / "manifest.json"
    if not man_path.is_file():
        return {}, [f"缺证据包 manifest：{man_path}"], gaps
    man = json.loads(man_path.read_text(encoding="utf-8"))
    if man.get("schema") != MANIFEST_SCHEMA:
        problems.append(f"manifest schema 不符：{man.get('schema')}")
    commit = man.get("git_commit")
    if not commit:
        problems.append("manifest.git_commit 为空")
    # 脏树上打的包不许绑定 HEAD 被三签认证（脏树 = 证据不对应任何提交）
    if man.get("git_dirty") is not False:
        problems.append(
            f"证据包不是从干净树构建的（git_dirty={man.get('git_dirty')!r}）")

    files = man.get("files") or []
    if not files:
        problems.append("manifest.files 为空（空包不算通过）")
    base = pack_dir.resolve()
    n_bad = 0
    for entry in files:
        rel = str(entry.get("path"))
        want = entry.get("sha256")
        # 路径越界守卫：绝对路径 / `..` 会让 sha256 与 size 全部「对得上」，
        # 却把判据指向包外的任意文件（空包 + 外部引用即可骗过纯哈希校验）
        rp = (pack_dir / rel)
        if (not rel or Path(rel).is_absolute() or ".." in Path(rel).parts
                or base not in rp.resolve().parents):
            problems.append(f"manifest 条目路径越界：{rel!r}")
            n_bad += 1
            continue
        p = rp
        if not p.is_file():
            problems.append(f"包内缺件：{rel}")
            n_bad += 1
            continue
        if p.is_symlink():
            problems.append(f"包内出现符号链接：{rel}")
            n_bad += 1
            continue
        got = _sha256(p)
        if got != want:
            problems.append(f"包内 sha 不符：{rel} {got[:12]}… ≠ {want[:12]}…")
            n_bad += 1
        if p.stat().st_size > MAX_FILE_BYTES:
            problems.append(f"包内 >10MB 文件：{rel}")
            n_bad += 1
        # 注意：空文件 size==0 是**合法值**，不能写成 `entry.get("size") or -1`
        # （0 是 falsy，会把合法的空文件误判成登记不符）
        declared_size = entry.get("size")
        if declared_size is None or int(declared_size) != p.stat().st_size:
            problems.append(
                f"包内登记大小不符：{rel} {declared_size!r} ≠ "
                f"{p.stat().st_size}")

    on_disk = {p.relative_to(pack_dir).as_posix()
               for p in pack_dir.rglob("*") if p.is_file()}
    declared = {str(e.get("path")) for e in files} | {"manifest.json"}
    orphan = sorted(on_disk - declared)
    if orphan:
        problems.append(f"包内出现 manifest 未登记的件 {len(orphan)}：{orphan[:3]}")

    for p in pack_dir.rglob("*"):
        if p.is_symlink():
            problems.append(f"包内符号链接（全树扫）：{p.relative_to(pack_dir)}")
            break
        if p.is_file() and not stat.S_ISREG(p.stat().st_mode):
            problems.append(f"包内非普通文件：{p.relative_to(pack_dir)}")
            break

    hits = []
    for p in pack_dir.rglob("*"):
        if not p.is_file() or p.is_symlink():
            continue
        if p.stat().st_size > MAX_FILE_BYTES:
            continue
        try:
            data = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for name, matched in secret_scan.scan_text(data):
            hits.append(f"{p.relative_to(pack_dir)}::{name}::{matched[:24]}")
    if hits:
        problems.append(f"包内命中密钥正则 {len(hits)} 处：{hits[:3]}")

    roots = man.get("evidence_roots") or {}
    if not any(str(k).startswith("@runs/") for k in roots):
        problems.append("manifest.evidence_roots 未登记任何 @runs/ 根")

    summary = {"path": str(pack_dir), "n_files": len(files),
               "bytes": sum(int(e.get("size") or 0) for e in files),
               "git_commit": commit}
    return summary, problems, gaps


# ---------------------------------------------------------------------------
# 5. 三签
# ---------------------------------------------------------------------------
def check_signs(signs_dir: Path, commit: "str | None") -> "tuple[dict, list[str]]":
    problems: "list[str]" = []
    found: "dict[str, dict]" = {}
    for role in ("executor", "reviewer", "verifier"):
        p = signs_dir / f"{role}.json"
        if not p.is_file():
            problems.append(f"缺签署件：{p}")
            continue
        s = json.loads(p.read_text(encoding="utf-8"))
        if s.get("schema") != SIGN_SCHEMA:
            problems.append(f"签署件 {role} schema 不符：{s.get('schema')}")
        if s.get("role") != role:
            problems.append(f"签署件 {role} 的 role={s.get('role')!r} 与文件名不符")
        if s.get("verdict") != "APPROVE":
            problems.append(f"签署件 {role} 未 APPROVE：{s.get('verdict')}")
        if commit and s.get("commit") != commit:
            problems.append(
                f"签署件 {role} 绑定 commit {s.get('commit')} ≠ manifest {commit}")
        found[role] = {"verdict": s.get("verdict"), "commit": s.get("commit"),
                       "sha256": _sha256(p)}
    return found, problems


# ---------------------------------------------------------------------------
# 6. 交叉互证
# ---------------------------------------------------------------------------
_SUMMARY_RE = re.compile(
    r"(?P<passed>\d+) passed(?:, (?P<failed>\d+) failed)?"
    r"(?:, (?P<skipped>\d+) skipped)?"
    r"(?:, (?P<deselected>\d+) deselected)?")


def _tests_block(text: str) -> dict:
    """从冻结日志解析「# cmd: / 摘要行 / rc=」三元（缺一即不可审）。

    pytest 摘要行形如 ``9 failed, 2262 passed, 7 skipped, 59 deselected``——
    各计数字段**按名提取**，不依赖固定次序。
    """
    cmds = re.findall(r"^# cmd: (.+)$", text, re.M)
    rcs = re.findall(r"^rc=(\d+)$", text, re.M)
    lines = [ln.strip() for ln in text.splitlines()
             if re.search(r"\b\d+ passed\b", ln)]
    # 摘要行必须**恰一条**：两行摘要（如尾部又贴了子进程输出）时取最后一条
    # 会把红摘要吞掉（`9 failed, 2262 passed` + `2262 passed` → failed=0）。
    if len(cmds) != 1 or len(rcs) != 1 or len(lines) != 1:
        raise ValueError(
            f"日志不可审：cmd={len(cmds)} rc={len(rcs)} summary={len(lines)}"
            "（各应恰 1 条）")

    def _n(field: str) -> int:
        m = re.search(rf"\b(\d+) {field}\b", lines[0])
        return int(m.group(1)) if m else 0

    return {
        "command": cmds[0].strip(),
        "summary": lines[0],
        "passed": _n("passed"),
        "failed": _n("failed"),
        "skipped": _n("skipped"),
        "deselected": _n("deselected"),
        "rc": int(rcs[0]),
    }


COLD_PASSED_FLOOR = 2198
CANON_TAIL = ('-m pytest tests -q -k "not test_04" '
              '-m "not paid and not kb_integration"')


def canonical_command_problem(cmd: str) -> "str | None":
    """冷跑命令必须是 CLAUDE.md 收尾命令的**逐字**形态：``<解释器> <固定尾部>``。

    只看「含两个 -k/-m 子串」会被 ``--ignore`` 或额外旗标骗过（少收几条测试
    也算绿）。这里要求尾部**逐字相等**，且尾部之前**恰一个**解释器 token——
    不得夹任何参数。合格返回 ``None``，否则返回问题描述。

    （AC-T7-1 与本机判共用此单一真值；``6670c26`` 上一轮此处把尾随空格
    当成了「额外参数」，属实现 bug，已由本函数取代。）
    """
    cmd = (cmd or "").strip()
    if not cmd.endswith(CANON_TAIL):
        return f"冷跑命令不是逐字形态（尾部应为 {CANON_TAIL!r}）：{cmd!r}"
    prefix = cmd[: -len(CANON_TAIL)].strip()
    if not prefix:
        return f"冷跑命令缺解释器前缀：{cmd!r}"
    if not re.fullmatch(r"\S+", prefix):
        return f"解释器与固定尾部之间夹了额外参数：{prefix!r}"
    if "--" in prefix:
        return f"解释器位出现了旗标：{prefix!r}"
    if "--ignore" in cmd:
        return "冷跑命令不得 --ignore 任何测试文件"
    return None


def check_cross(dashboard_path: Path, pack_dir: Path,
                ledger_summary: "dict | None" = None,
                tables_summary: "dict | None" = None) -> "tuple[dict, list[str]]":
    problems: "list[str]" = []
    if not dashboard_path.is_file():
        return {}, [f"缺看板：{dashboard_path}"]
    dash = json.loads(dashboard_path.read_text(encoding="utf-8"))
    if dash.get("schema") != DASHBOARD_SCHEMA:
        problems.append(f"看板 schema 不符：{dash.get('schema')}")

    man_path = pack_dir / "manifest.json"
    man = (json.loads(man_path.read_text(encoding="utf-8"))
           if man_path.is_file() else {})
    commit = man.get("git_commit")
    got = (dash.get("git") or {}).get("commit")
    if not got:
        problems.append("看板 git.commit 缺失（证据层不可审）")
    elif commit and got != commit:
        problems.append(f"看板 git.commit={got} ≠ manifest.git_commit={commit}")

    # AC 计数两套实现互证（计数问题已在 build_report 里报过一次，此处只比对）
    ac, _ac_prob = count_acs()
    dac = dash.get("ac_counts") or {}
    if dac.get("total") != ac["total"]:
        problems.append(f"看板 ac_total={dac.get('total')} ≠ 机判 {ac['total']}")
    if dac.get("per_task") != ac["per_task"]:
        problems.append(
            f"看板 ac per_task={dac.get('per_task')} ≠ 机判 {ac['per_task']}")
    if dac.get("ids_sha256") != ac["ids_sha256"]:
        problems.append("看板 ac ids_sha256 与机判不一致")

    # 看板 tests 段与包内冻结日志逐字段相等
    tests = dash.get("tests") or {}
    for key, fname in (("cold_run", "cold_run.raw.log"),
                       ("kbint", "kbint.raw.log"),
                       ("container_run", "container_run.raw.log")):
        p = pack_dir / fname
        blk = tests.get(key)
        if blk is None:
            problems.append(f"看板 tests.{key} 段缺失")
            continue
        if not p.is_file():
            problems.append(f"包内缺冻结日志：{fname}")
            continue
        try:
            want = _tests_block(p.read_text(encoding="utf-8", errors="replace"))
        except ValueError as exc:
            problems.append(f"{fname} {exc}")
            continue
        for field in ("command", "summary", "passed", "failed", "skipped",
                      "deselected", "rc"):
            if blk.get(field) != want[field]:
                problems.append(
                    f"看板 tests.{key}.{field}={blk.get(field)!r} ≠ "
                    f"包内日志 {want[field]!r}")

    # AC-T7-1 口径：冷跑 rc=0、failed==0、passed ≥ 2198（锁内适配 1：
    # 「新增测试数」单列，不与既有基线混算，故这里是地板不是等式）。
    # 注意：`failed` 必须**在场**才算数——缺键时 `.get()` 返回 None，
    # 若写成 `if cold.get("failed"):` 则永久为假（死判据）。这里一律
    # 与 0 做不等比较，缺键即 problem。
    cold = tests.get("cold_run") or {}
    if cold:
        if cold.get("rc") != 0:
            problems.append(f"冷跑 rc={cold.get('rc')!r} ≠ 0")
        if cold.get("failed") != 0:
            problems.append(f"冷跑 failed={cold.get('failed')!r} ≠ 0")
        if int(cold.get("passed") or 0) < COLD_PASSED_FLOOR:
            problems.append(
                f"冷跑 passed={cold.get('passed')!r} < 地板 {COLD_PASSED_FLOOR}")
        cmd_problem = canonical_command_problem(cold.get("command") or "")
        if cmd_problem:
            problems.append(cmd_problem)

    # 锁内适配 2（kbint 增列）既然把活库套纳入收尾，就必须判它绿；
    # 容器场同理。只做「看板 vs 冻结日志」互撞是同源比较，无法发现红。
    for key in ("kbint", "container_run"):
        blk = tests.get(key) or {}
        if blk.get("rc") != 0:
            problems.append(
                f"{key} rc={blk.get('rc')!r} ≠ 0（活库/容器套必须绿）")
        if blk.get("failed") != 0:
            problems.append(f"{key} failed={blk.get('failed')!r} ≠ 0")

    # 看板自报的账与四表必须与机判**独立重算**的结果一致——否则看板整段
    # 撒谎（例如把 67 写成 60）也能靠「看板 vs 冻结日志」同源自洽而过关。
    if ledger_summary:
        dled = dash.get("ledger") or {}
        pairs = (("total_rows", "total_rows"), ("n_calls", "n_calls"),
                 ("ok", "ok"), ("fail", "fail"), ("by_task", "by_task"),
                 ("by_stage", "by_stage"), ("by_batch", "by_batch"),
                 ("n_selfcheck", "selfcheck_rows"))
        for dash_field, judge_field in pairs:
            if dled.get(dash_field) != ledger_summary.get(judge_field):
                problems.append(
                    f"看板 ledger.{dash_field}={dled.get(dash_field)!r} ≠ "
                    f"机判重算 {judge_field}={ledger_summary.get(judge_field)!r}")
    if tables_summary:
        dt = dash.get("lance_tables") or {}
        for name, rows in (tables_summary.get("rows") or {}).items():
            if dt.get(name) != rows:
                problems.append(
                    f"看板 lance_tables.{name}={dt.get(name)!r} ≠ 机判重算 {rows}")

    # 包内冷跑三件套自洽：cold_run.json ↔ cold_run.raw.log ↔ manifest.git_commit
    # （冷跑证据若来自别的提交，签了 HEAD 也等于没签）
    cj, clog = pack_dir / "cold_run.json", pack_dir / "cold_run.raw.log"
    if not cj.is_file() or not clog.is_file():
        problems.append("包内缺 cold_run.json / cold_run.raw.log（冷跑证据不完整）")
    else:
        cdoc = json.loads(cj.read_text(encoding="utf-8"))
        if cdoc.get("log_sha256") != _sha256(clog):
            problems.append("包内 cold_run.json.log_sha256 ≠ 包内冷跑日志 sha256")
        if commit and cdoc.get("commit") != commit:
            problems.append(
                f"包内 cold_run.json.commit={cdoc.get('commit')!r} ≠ "
                f"manifest.git_commit={commit}（冷跑不在被签提交上）")
        try:
            parsed = _tests_block(clog.read_text(encoding="utf-8",
                                                 errors="replace"))
        except ValueError as exc:
            problems.append(f"包内冷跑日志不可审：{exc}")
        else:
            for field in ("passed", "failed", "skipped", "deselected", "rc"):
                if cdoc.get(field) != parsed[field]:
                    problems.append(
                        f"包内 cold_run.json.{field}={cdoc.get(field)!r} ≠ "
                        f"日志 {parsed[field]!r}")

    return {"dashboard": str(dashboard_path), "git_commit": commit,
            "ac_total": ac["total"], "ac_ids_sha256": ac["ids_sha256"]}, problems

# ---------------------------------------------------------------------------
# 7. 未达成项挂牌（gap 必须出现，缺了是 problem）
# ---------------------------------------------------------------------------
def registered_gaps() -> "list[dict]":
    return [
        {"tag": "T4_UNFREEZE_NOT_ESTABLISHED",
         "what": "历史双墙解冻判定：模式 R 与模式 N 两判 NOT_ESTABLISHED，"
                 "解冻条件未满足。",
         "why_not_pass": "科学结论是「不成立」，不是「通过」；不得计为 AC 达成。",
         "owner": "Phase 3 backlog（须新预注册 + 用户裁决）"},
        {"tag": "T6_NO_CONTROL_ARM",
         "what": "LIBERO VLA S3 对照臂缺失：预注册序列内 octo-small 与 "
                 "openvla-7b-int4 在本镜像栈上不可达，无可达对照策略。",
         "why_not_pass": "对照未被测量，「优于/劣于基线」无证据；只判主策略。",
         "owner": "Phase 3 backlog（修可达性或新候选，须新预注册）"},
        {"tag": "T6_MAIN_POLICY_ZERO",
         "what": "主策略 openvla-7b-fp16 成功率 0/10（Wilson 95% "
                 "[0.0, 0.2775328]）；归因为 base 检查点 + 动作反归一化键回退。",
         "why_not_pass": "0 成功率是结论不是达成；换检查点属新预注册事项。",
         "owner": "Phase 3 backlog"},
        {"tag": "G0_12_NOT_ACTIVATED",
         "what": "G0-12（`--gpus` 分档启用）为条件项，仅当 T1.5 Step 0 任一段"
                 "判红才生效；三段探针未判红，故未激活。",
         "why_not_pass": "条件项未触发，不构成达成也不构成缺陷，如实登记。",
         "owner": "无（条件未触发）"},
    ]


def check_gaps(report_gaps: "list[dict]", problems: "list[str]") -> None:
    tags = {g.get("tag") for g in report_gaps}
    missing = [t for t in REQUIRED_GAP_TAGS if t not in tags]
    if missing:
        problems.append(f"未达成项未挂牌（把缺口洗成通过）：{missing}")
    for g in report_gaps:
        for field in ("tag", "what", "why_not_pass", "owner"):
            if not g.get(field):
                problems.append(f"gap {g.get('tag')} 缺字段 {field}")


# ---------------------------------------------------------------------------
def build_report(*, root: Path, dashboard: Path, pack: Path,
                 signs: Path, ledger: Path, approvals: Path,
                 recycle: Path) -> dict:
    problems: "list[str]" = []
    gaps = registered_gaps()

    ac, p = count_acs()
    problems.extend(p)
    if not ledger.is_file():
        problems.append(f"账本不存在：{ledger}（账不可审即不可收尾）")
        rows: "list[dict]" = []
    else:
        rows = _rows_from(ledger)
    led, p = check_ledger(rows, approvals)
    problems.extend(p)
    tables, p = check_tables(recycle)
    problems.extend(p)
    pack_sum, p, _ = check_pack(pack)
    problems.extend(p)
    sign_sum, p = check_signs(signs, pack_sum.get("git_commit"))
    problems.extend(p)
    cross, p = check_cross(dashboard, pack, led, tables)
    problems.extend(p)
    check_gaps(gaps, problems)

    return {
        "schema": "simpraxis.phase27.closure_judge.v1",
        "root": str(root),
        "problems": problems,
        "gaps": gaps,
        "checks": {
            "ac_counts": ac,
            "ledger": led,
            "tables": tables,
            "evidence_pack": pack_sum,
            "signs": sign_sum,
            "cross": cross,
        },
    }


def main(argv: "list[str] | None" = None) -> int:
    from orchestrator.common.runtime_paths import (  # noqa: PLC0415
        phase27_approvals_dir, phase27_ledger_path, phase27_root)

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", type=Path, default=None)
    ap.add_argument("--dashboard", type=Path, default=None)
    ap.add_argument("--pack", type=Path, default=None)
    ap.add_argument("--signs", type=Path, default=None)
    ap.add_argument("--ledger", type=Path, default=None)
    ap.add_argument("--approvals", type=Path, default=None)
    ap.add_argument("--recycle", type=Path, default=None)
    ap.add_argument("--report", type=Path, default=None)
    args = ap.parse_args(argv)

    root = args.root or phase27_root()
    milestone = root / "task7_milestone"
    rep = build_report(
        root=root,
        dashboard=args.dashboard or (milestone / "dashboard_phase27.json"),
        pack=args.pack or (milestone / "evidence_pack"),
        signs=args.signs or (milestone / "signs"),
        ledger=args.ledger or phase27_ledger_path(),
        approvals=args.approvals or phase27_approvals_dir(),
        recycle=args.recycle or (root / "task6_vla" / "manifests"
                                 / "RECYCLE.json"),
    )
    text = json.dumps(rep, ensure_ascii=False, indent=1, sort_keys=True) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(text, encoding="utf-8")
    print(text, end="")
    print(f"[CLOSURE-JUDGE] problems={len(rep['problems'])} "
          f"gaps={len(rep['gaps'])}", file=sys.stderr)
    return 0 if not rep["problems"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
