#!/usr/bin/env python3
"""Phase 2.5 收尾机判（AC-T10-7 / AC-T10-4 取证核验；0 付费、只读）。

沿 ``check_recall_closure.py`` 的模式：**证据逐字节互证**，任一断言不成立即
rc≠0；证据缺口如实挂牌 ``provenance_limited`` 并**不计入通过**。本脚本不
自己算新事实，只把三处独立产出（收尾对账件、看板、证据包 manifest）互相
对撞，并把 Plan 正文的 AC 编号重新数一遍：

1. **AC 计数**（AC-T10-7 的 M5 条款）：从 ``Phase_2.5_Plan_detailed.md`` 正文
   去重提取 ``AC-Tn-k``，与 §9 追溯矩阵声明的总数 102 及逐 Task 计数
   （7/7/8/8/18/7/8/8/9/12/10）**逐项相等**，且与看板 ``ac_counts`` 相同；
2. **账与工程**（AC-T10-2/5/6）：seq 自 1 连续、ts 严格 Z、ok+fail=n、
   批准件逐批齐备且用量不超批、phase25 窗口内缓存条目 0、容器存量 0、
   显存 ≤200 MiB 且有合法读数；
3. **数据层**（AC-T10-3）：四表存在且行数/维度与对账件一致，kb 表 1542 不动；
4. **科学结论**（AC-T10-7 三态段）：T6 Stage B 判决件 ``verdict=RETAIN_R4``
   与 ``retain=true``、五条件全 true、点预测逐案相符；Stage A 回滚后嵌入配置
   为 mpnet/768；T7/T8/T9 状态为 ``stopped_by_user``（用户裁决停，不虚报完成）；
5. **证据包**（AC-T10-4）：``manifest.json`` 存在、``git_commit`` 非空、
   ``evidence_roots`` 注册 ``@runs/phase25/<thread>/stage_<id>/`` 标签、
   逐件 sha256 复算一致、无 >10MB 文件、密钥双扫 0 命中、包内无符号链接。

用法：
  python scripts/verification/check_phase25_closure.py [--report OUT.json]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import stat
import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))

from orchestrator.common import secrets as secret_scan  # noqa: E402

PLAN = Path.home() / "文档/Phase_2.5_Plan_detailed.md"
COMPLETE = Path.home() / "文档/Phase_2.5_Plan_Complete.md"
DECLARED_TOTAL = 102
DECLARED_PER_TASK = {"T0": 7, "T1": 7, "T2": 8, "T3": 8, "T4": 18, "T5": 7,
                     "T6": 8, "T7": 8, "T8": 9, "T9": 12, "T10": 10}
MAX_FILE_BYTES = 10 * 1024 * 1024
# AC-T10-4：证据根登记键的前缀（模板形如
# ``@runs/phase25/<thread>/stage_<id>/``，故按键前缀匹配而非全等）。
REQUIRED_EVIDENCE_ROOT_TAG = "@runs/phase25/"
_STAGE_B_VERDICT = Path("/mnt/artifacts/simpraxis/phase25/task6_r4_window"
                        "/2026-11-02T031952Z/task6_r4_verdict.json")


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def count_acs(plan_path: Path = PLAN) -> "tuple[dict, list[str]]":
    """独立重数 AC 编号（不复用看板函数，避免同一实现自我印证）。"""
    text = plan_path.read_text(encoding="utf-8")
    ids = sorted({(int(a), int(b))
                  for a, b in re.findall(r"AC-T(\d+)-(\d+)", text)})
    per = {f"T{i}": 0 for i in range(11)}
    for t, _k in ids:
        per[f"T{t}"] += 1
    problems = []
    if len(ids) != DECLARED_TOTAL:
        problems.append(f"AC 总数 {len(ids)} ≠ 声明 {DECLARED_TOTAL}")
    bad = {k: (per[k], v) for k, v in DECLARED_PER_TASK.items()
           if per[k] != v}
    if bad:
        problems.append(f"逐 Task AC 计数与声明不符：{bad}")
    return {"total": len(ids), "per_task": per,
            "ids_sha256": hashlib.sha256(
                ",".join(f"AC-T{t}-{k}" for t, k in ids).encode()).hexdigest()}, problems


def check_ledger(rec: dict) -> list[str]:
    led = rec["ledger"]
    problems = []
    if not led["seq_contiguous_from_1"]:
        problems.append("账 seq 不连续")
    if not led["ts_all_strict_z"]:
        problems.append(f"非 Z 后缀 ts：{led['ts_non_z_seqs']}")
    if led["ok"] + led["fail"] != led["n_calls"]:
        problems.append("ok+fail ≠ n_calls")
    if len(led["fail_rows"]) != led["fail"]:
        problems.append("fail_rows 条数与 fail 计数不符")
    # 账文件 sha256 复算（不信任对账件自报值）
    lp = Path(rec.get("ledger_path") or "")
    if not lp.is_file():
        problems.append(f"账文件不存在：{lp}")
    elif _sha256(lp) != rec.get("ledger_sha256"):
        problems.append("账文件 sha256 复算与对账件不符")
    ap = rec["approvals"]
    if not ap["ok"] or ap["problems"]:
        problems.append(f"批准件不符：{ap['problems']}")
    if ap["total_approved"] != led["n_calls"]:
        problems.append(
            f"批准总 {ap['total_approved']} ≠ 实呼 {led['n_calls']}")
    return problems


# AC-T10-3：四表收尾活值（Plan 引用时点值；只增不改的是 kb 表，其余随里程碑
# 新增行——本里程碑 T9 未执行故 solution_chunks 停在 94、学习表停在 44/1）。
EXPECTED_ROWS = {"kb_chunks": 1542, "solution_chunks": 94,
                 "phase1_learnings": 44, "phase2_learnings": 1}
_STAGE_B_CONDITIONS = {"1_thresholds", "2_h3_subset_h4", "3_residual_recovery",
                       "4_data_identity", "5_engineering"}
# 预注册 §7 的数值承诺（判决件每条条件的 ok 之外，逐项钉死）：
# 路由 = (hits, total, 阈值)；H3/H4 = 38/45 且零翻失；9 残留回收 7、gold_id 直中 6。
_STAGE_B_ROUTES = {"overall": (45, 47, 0.80), "solution_route": (18, 18, 0.75),
                   "learning_route": (11, 12, 0.75)}
_STAGE_B_H3H4 = (38, 45)
_STAGE_B_RESIDUAL = (9, 7, 6)     # n_residual, n_recovered, n_gold_id_direct


def _stage_b_condition_problems(conds: dict) -> list[str]:
    """五条件必须恰 5 键、每条 ok 为 true，且预注册数值逐项相符。"""
    if set(conds) != _STAGE_B_CONDITIONS:
        return [f"T6 Stage B 条件键不符（应恰 5 条）：{sorted(conds)}"]
    problems = []
    for k, c in conds.items():
        if not isinstance(c, dict) or c.get("ok") is not True:
            problems.append(f"T6 Stage B 条件 {k} 未过（ok 非 true）")
    t1 = conds["1_thresholds"]
    if t1.get("all_pass") is not True:
        problems.append("T6 Stage B 条件 1 all_pass 非 true")
    detail = t1.get("detail") or {}
    for route, (hits, total, thr) in _STAGE_B_ROUTES.items():
        d = detail.get(route) or {}
        if (d.get("hits"), d.get("total")) != (hits, total):
            problems.append(
                f"T6 Stage B {route} 命中 {d.get('hits')}/{d.get('total')}"
                f" ≠ 预注册 {hits}/{total}")
        got = d.get("gold_at_8")
        if d.get("pass") is not True or not isinstance(got, (int, float)) \
                or got < thr:
            problems.append(
                f"T6 Stage B {route} 闸未过（gold_at_8={got} < {thr}）")
    t2 = conds["2_h3_subset_h4"]
    if (t2.get("n_h3"), t2.get("n_h4")) != _STAGE_B_H3H4:
        problems.append(
            f"T6 Stage B H3/H4 = {t2.get('n_h3')}/{t2.get('n_h4')} ≠ "
            f"{_STAGE_B_H3H4[0]}/{_STAGE_B_H3H4[1]}")
    if t2.get("flip_loss") != []:
        problems.append(f"T6 Stage B 翻失非空：{t2.get('flip_loss')}")
    t3 = conds["3_residual_recovery"]
    got3 = (t3.get("n_residual"), t3.get("n_recovered"),
            t3.get("n_gold_id_direct"))
    if got3 != _STAGE_B_RESIDUAL:
        problems.append(
            f"T6 Stage B 残留/回收/直中 = {got3} ≠ {_STAGE_B_RESIDUAL}")
    if len(t3.get("recovered") or []) != _STAGE_B_RESIDUAL[1]:
        problems.append(
            f"T6 Stage B 回收清单 {len(t3.get('recovered') or [])} 条 ≠ "
            f"{_STAGE_B_RESIDUAL[1]}")
    for k in ("4_data_identity", "5_engineering"):
        if conds[k].get("problems"):
            problems.append(
                f"T6 Stage B 条件 {k} 残留问题：{conds[k]['problems']}")
    return problems


def check_tables(rec: dict, pre_vector_identity: "Path | None" = None) -> list[str]:
    tabs = rec["tables"]
    problems = []
    for name, want in EXPECTED_ROWS.items():
        t = tabs.get(name)
        if not t:
            problems.append(f"缺表 {name}")
            continue
        if t.get("dim") != 768:
            problems.append(f"{name} 维度 {t.get('dim')} ≠ 768")
        if t.get("rows") != want:
            problems.append(f"{name} 行数 {t.get('rows')} ≠ 收尾活值 {want}")
    # 向量身份与窗口前基线逐表比对（不只看行数）
    if pre_vector_identity is None or not pre_vector_identity.is_file():
        problems.append(f"缺窗前向量身份基线：{pre_vector_identity}")
    else:
        base = json.loads(pre_vector_identity.read_text(encoding="utf-8"))
        for name in EXPECTED_ROWS:
            want = (base.get("tables", {}).get(name) or {}).get(
                "vector_set_sha256")
            got = (tabs.get(name) or {}).get("vector_set_sha256")
            if not want:
                problems.append(f"基线缺 {name} 的 vector_set_sha256")
            elif got != want:
                problems.append(f"{name} 向量集 sha 与窗前基线不符")
    return problems


def check_env(rec: dict) -> list[str]:
    env = rec["env"]
    problems = []
    if env["containers_present"] != 0:
        problems.append(f"残留容器 {env['container_ids']}")
    if env["vram_mib"] is None:
        problems.append("显存无可解析读数（空输出按未测处理）")
    elif env["vram_mib"] > 200:
        problems.append(f"显存 {env['vram_mib']} MiB > 200")
    if not env.get("mnt"):
        problems.append("df /mnt 无读数")
    return problems


def check_science(rec: dict,
                  pack_dir: "Path | None" = None) -> "tuple[list[str], list[dict]]":
    problems: list[str] = []
    gaps: list[dict] = []
    method = rec["methodology"]["per_task"]
    for task, v in method.items():
        if v.get("methodology_failure"):
            problems.append(f"{task} 实际超 P90（方法学失败结论须入 Complete）")
    # T6 Stage B 判决件：优先读包内已入 sha256 台账的副本（证据自足），
    # 包外活件只作缺口兜底——不能只凭包外活件判绿。
    verdict_path = None
    if pack_dir is not None:
        cand = (pack_dir / "task6_stage_b" / "window"
                / _STAGE_B_VERDICT.parent.name / "task6_r4_verdict.json")
        if cand.is_file():
            verdict_path = cand
    if verdict_path is None:
        if not _STAGE_B_VERDICT.is_file():
            # 科学结论无任何可读判决件 → 不得判绿（缺证 = 问题，不是缺口）
            problems.append(f"T6 Stage B 判决件缺失：{_STAGE_B_VERDICT}")
        else:
            verdict_path = _STAGE_B_VERDICT
            gaps.append({"what": "T6 Stage B 判决件（包外活件）",
                         "path": str(_STAGE_B_VERDICT),
                         "why": "包内未提供副本，按包外活件判定"})
    if verdict_path is not None:
        v = json.loads(verdict_path.read_text(encoding="utf-8"))
        if v.get("verdict") != "RETAIN_R4" or v.get("retain") is not True:
            problems.append(f"T6 Stage B 判决非 RETAIN_R4：{v.get('verdict')}")
        problems += _stage_b_condition_problems(v.get("conditions") or {})
        ppm = v.get("point_prediction_match") or {}
        if not ppm.get("ok"):
            problems.append("T6 Stage B 点预测闸未过")
        if ppm.get("mismatches") or ppm.get("n_cases") != 47:
            problems.append(
                f"T6 Stage B 点预测逐案不符：n={ppm.get('n_cases')} "
                f"mismatches={ppm.get('mismatches')}")
        want = "9a704e0081c62e1d938bd023bddc572eafac3768450f08327c7a8ad09fb59e16"
        if v.get("iter5_sha256") != want:
            problems.append("T6 Stage B iter5 报告 sha 与登记不符")
    # Stage A 回滚后嵌入配置 = mpnet/768
    cfg = json.loads((_REPO / "memory_flywheel/embedding_config.json")
                     .read_text(encoding="utf-8"))
    name = json.dumps(cfg)
    if "mpnet" not in name or int(cfg.get("dim", 0)) != 768:
        problems.append(f"嵌入配置未回滚到 mpnet/768：{name[:120]}")
    return problems, gaps


def check_dashboard(dash: dict, rec: dict) -> list[str]:
    problems = []
    if dash.get("schema") != "simpraxis.rsi_dashboard_phase25.v1":
        problems.append(f"看板 schema 不符：{dash.get('schema')}")
    t = dash.get("tests")
    if t is None:
        problems.append("看板 tests 为 null（未喂入全套实跑结果）")
    elif t.get("rc") != 0:
        problems.append(f"看板 tests rc={t.get('rc')} 非 0")
    elif int(t.get("passed") or 0) <= 0:
        problems.append("看板 tests passed=0")
    pr = dash["paid_reconciliation"]
    if pr["ledger"]["n_calls"] != rec["ledger"]["n_calls"]:
        problems.append("看板账行数与对账件不符")
    if pr.get("ledger_sha256") != rec.get("ledger_sha256"):
        problems.append("看板 ledger_sha256 与对账件不符")
    for name in ("kb_chunks", "solution_chunks",
                 "phase1_learnings", "phase2_learnings"):
        if dash["lance_tables"].get(name, {}).get("rows") != \
                rec["tables"].get(name, {}).get("rows"):
            problems.append(f"看板 {name} 行数与对账件不符")
    if dash["ac_counts"]["total"] != DECLARED_TOTAL:
        problems.append("看板 AC 总数与声明不符")
    stopped = {t["task"] for t in dash.get("tasks", [])
               if t.get("status") == "stopped_by_user"}
    if stopped != {"T7", "T8", "T9"}:
        problems.append(f"T7/T8/T9 状态应为 stopped_by_user：{stopped}")
    return problems


def check_pack(pack_dir: Path) -> "tuple[list[str], list[dict]]":
    problems: list[str] = []
    gaps: list[dict] = []
    manifest_path = pack_dir / "manifest.json"
    if not manifest_path.is_file():
        return [f"缺证据包 manifest：{manifest_path}"], gaps
    man = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not man.get("git_commit"):
        problems.append("manifest.git_commit 为空")
    roots = man.get("evidence_roots") or {}
    hit = next((k for k in roots
                if str(k).startswith(REQUIRED_EVIDENCE_ROOT_TAG)), None)
    if hit is None:
        problems.append(
            f"manifest.evidence_roots 未注册 {REQUIRED_EVIDENCE_ROOT_TAG}"
            "…/<thread>/stage_<id>/ 模板")
    else:
        entry = roots[hit] or {}
        if entry.get("tag") != "@runs/" or entry.get("subdir") != "phase25":
            problems.append(
                f"证据根登记 {hit} 的解析基准不符（tag={entry.get('tag')} "
                f"subdir={entry.get('subdir')}，应为 @runs/ + phase25）")
        if not isinstance(entry.get("rows_using"), int):
            problems.append(f"证据根登记 {hit} 缺 rows_using 整数）")
    entries = man.get("files") or []
    if not entries:
        problems.append("manifest.files 为空")
    listed = {r["path"] for r in entries}
    # 全覆盖：包内实有文件集合 == manifest 登记集合 + manifest.json 自身
    actual = {str(p.relative_to(pack_dir)) for p in pack_dir.rglob("*")
              if p.is_file()}
    unlisted = actual - listed - {"manifest.json"}
    if unlisted:
        problems.append(f"包内未登记文件 {len(unlisted)} 个：{sorted(unlisted)[:3]}")
    ghost = listed - actual
    if ghost:
        problems.append(f"manifest 列件缺失 {len(ghost)} 个：{sorted(ghost)[:3]}")
    bad_modes = []
    for p in pack_dir.rglob("*"):
        if p.name == "manifest.json":
            continue
        mode = stat.S_IMODE(p.stat().st_mode)
        want = 0o700 if p.is_dir() else 0o600
        if mode != want:
            bad_modes.append(f"{p.relative_to(pack_dir)}={oct(mode)}")
    if bad_modes:
        problems.append(f"包内权限不符 0700/0600：{bad_modes[:3]}")
    for rec in entries:
        p = pack_dir / rec["path"]
        if not p.is_file():
            problems.append(f"manifest 列件缺失：{rec['path']}")
            continue
        if p.is_symlink():
            problems.append(f"包内符号链接：{rec['path']}")
        if p.stat().st_size > MAX_FILE_BYTES:
            problems.append(f"包内 >10MB：{rec['path']}")
        if _sha256(p) != rec.get("sha256"):
            problems.append(f"sha256 不符：{rec['path']}")
    # T7/T8/T9 用户停做：包内挂牌 NOT_EXECUTED，机判按证据缺口登记
    # （provenance_limited，不计入通过）；缺失挂牌本身才算问题。
    for slug in ("task7_stages", "task8_drill", "task9_vla"):
        marker = pack_dir / slug / "NOT_EXECUTED.json"
        if not marker.is_file():
            problems.append(f"缺 {slug}/NOT_EXECUTED.json 或该任务证据")
            continue
        rec = json.loads(marker.read_text(encoding="utf-8"))
        if rec.get("status") != "stopped_by_user":
            problems.append(f"{slug}/NOT_EXECUTED.json status 非 stopped_by_user")
        gaps.append({"what": rec.get("task") or slug,
                     "path": str(marker),
                     "why": f"{rec.get('status')}：{rec.get('adjudication')}"})
    signs_dir = pack_dir / "task10_closeout" / "signs"
    if (pack_dir / "task10_closeout/signs_MISSING.json").is_file():
        gaps.append({"what": "Task 10 三签件",
                     "path": str(signs_dir),
                     "why": "机判时点三签尚未落件（构建顺序：机判→三签→终建包）"})
    else:
        # 三签件齐备口径：executor/reviewer/verifier 三角色各一份且 verdict 可读
        found = {}
        for p in sorted(signs_dir.glob("*.json")) if signs_dir.is_dir() else []:
            try:
                s = json.loads(p.read_text(encoding="utf-8"))
            except ValueError:
                problems.append(f"三签件不可解析：{p.name}")
                continue
            found[s.get("role") or p.stem] = s
        missing = {"executor", "reviewer", "verifier"} - set(found)
        if missing:
            problems.append(f"三签件缺角色：{sorted(missing)}")
        for role, s in found.items():
            if s.get("schema") != "simpraxis.phase25.sign.v1":
                problems.append(f"三签件 {role} schema 不符")
            if s.get("verdict") not in ("APPROVE", "REJECT"):
                problems.append(f"三签件 {role} verdict 非法：{s.get('verdict')}")
    # 密钥双扫（包内文本）
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
    # Complete 文档关键段（仓外）
    if COMPLETE.is_file():
        text = COMPLETE.read_text(encoding="utf-8")
        for needle in ("Task 6 Stage B", "RETAIN_R4", "T10"):
            if needle not in text:
                gaps.append({"what": f"Complete 缺 {needle} 段",
                             "path": str(COMPLETE), "why": "收尾文档待补"})
    else:
        problems.append(f"缺 Complete 文档：{COMPLETE}")
    return problems, gaps


def check_git_author() -> list[str]:
    out = subprocess.run(["bash", "scripts/verification/check_git_author.sh"],
                         cwd=_REPO, capture_output=True, text=True)
    if out.returncode != 0:
        return [f"作者校验失败：{out.stdout.strip() or out.stderr.strip()}"]
    return []


def build_report(*, reconcile: Path, dashboard: Path, pack: Path,
                 plan: Path = PLAN) -> dict:
    problems: list[str] = []
    gaps: list[dict] = []
    ac, ac_problems = count_acs(plan)
    problems += ac_problems
    if not reconcile.is_file():
        problems.append(f"缺收尾对账件：{reconcile}")
        rec = None
    else:
        rec = json.loads(reconcile.read_text(encoding="utf-8"))
        problems += check_ledger(rec)
        problems += check_tables(
            rec, pack / "task6_stage_b" / "window"
            / _STAGE_B_VERDICT.parent.name / "pre_vector_identity.json")
        problems += check_env(rec)
        sp, sg = check_science(rec, pack)
        problems += sp
        gaps += sg
    dash = None
    if not dashboard.is_file():
        problems.append(f"缺看板：{dashboard}")
    else:
        dash = json.loads(dashboard.read_text(encoding="utf-8"))
        if rec is not None:
            problems += check_dashboard(dash, rec)
        if dash.get("ac_counts", {}).get("total") != ac["total"]:
            problems.append("看板 AC 总数与独立重数不符")
        if dash.get("ac_counts", {}).get("per_task") != ac["per_task"]:
            problems.append("看板逐 Task AC 计数与独立重数不符")
    pp, pg = check_pack(pack)
    problems += pp
    gaps += pg
    problems += check_git_author()
    return {
        "schema": "simpraxis.phase25.closure_judge.v1",
        "reconcile": str(reconcile),
        "dashboard": str(dashboard),
        "pack": str(pack),
        "ac_counts": ac,
        "problems": problems,
        "provenance_limited": gaps,
        "ok": not problems,
    }


def main(argv: "list[str] | None" = None) -> int:
    from orchestrator.common.runtime_paths import phase25_root
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--reconcile", type=Path, default=None)
    ap.add_argument("--dashboard", type=Path, default=None)
    ap.add_argument("--pack", type=Path, default=None)
    ap.add_argument("--plan", type=Path, default=PLAN)
    ap.add_argument("--report", type=Path, default=None)
    args = ap.parse_args(argv)
    root = phase25_root() / "task10_milestone"
    recon = args.reconcile or (root / "closeout_reconcile.json")
    dash = args.dashboard or (root / "dashboard_phase25.json")
    pack = args.pack or (root / "evidence_pack")
    report = build_report(reconcile=recon, dashboard=dash, pack=pack,
                          plan=args.plan)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps(report, ensure_ascii=False, indent=1) + "\n",
            encoding="utf-8")
    for p in report["problems"]:
        print(f"[FAIL] {p}", file=sys.stderr)
    for g in report["provenance_limited"]:
        print(f"[GAP/provenance_limited] {g['what']}：{g['why']}", file=sys.stderr)
    print(f"ac_total={report['ac_counts']['total']} "
          f"problems={len(report['problems'])} "
          f"gaps={len(report['provenance_limited'])} "
          f"ok={report['ok']}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
