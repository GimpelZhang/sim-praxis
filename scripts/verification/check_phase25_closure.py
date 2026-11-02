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
4. **科学结论**（AC-T10-7 三态段）：T6 Stage B 判决件**只认包内副本**
   （``verdict=RETAIN_R4`` 与 ``retain=true``、五条件全 true、点预测逐案相符；
   副本与包外活件逐字节不一致即 problem——包外兜底会让缺科学证据的包判绿）；
   Stage A 回滚后嵌入配置为 mpnet/768；T7/T8/T9 状态为 ``stopped_by_user``
   （用户裁决停，不虚报完成）；
5. **证据包**（AC-T10-4）：``manifest.json`` 存在、``git_commit`` 非空、
   ``evidence_roots`` 注册 ``@runs/phase25/<thread>/stage_<id>/`` 标签、
   逐件 sha256 复算一致、无 >10MB 文件、密钥双扫 0 命中、包内无符号链接、
   无非普通文件（FIFO/socket/设备）；
6. **交叉互证**：看板 ``git.commit`` 必须等于 ``manifest.git_commit``（同一
   代码状态出证）；看板 ``tests`` 段与包内冻结日志逐字段相等（命令口径/规模/
   rc/摘要），且两侧都要求恰 1 条 ``# cmd:`` 与恰 1 条 ``rc=``；三签的 verifier
   签必须带 ``recomputed`` 块，其 AC 数/账数/四表行数/批准总数与机判自身重算
   逐项相等。

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


def _is_num(x: object) -> bool:
    """数值判定（评审 LOW-2：bool 是 int 子类，True 不得冒充 1.0 过闸）。"""
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _stage_b_condition_problems(conds: object) -> list[str]:
    """五条件必须恰 5 键、每条为对象且 ok 为 true，预注册数值逐项相符。"""
    if not isinstance(conds, dict) or set(conds) != _STAGE_B_CONDITIONS:
        return [f"T6 Stage B 条件键不符（应恰 5 条）：{conds!r}"[:200]]
    # 非对象形态的条件无法做数值钉死：先整体判负再返回，绝不 .get 崩或跳过
    non_dict = sorted(k for k, c in conds.items() if not isinstance(c, dict))
    if non_dict:
        return [f"T6 Stage B 条件 {non_dict} 不是对象（不可判定）"]
    problems = []
    for k in sorted(conds):
        if conds[k].get("ok") is not True:
            problems.append(f"T6 Stage B 条件 {k} 未过（ok 非 true）")
    t1 = conds["1_thresholds"]
    if t1.get("all_pass") is not True:
        problems.append("T6 Stage B 条件 1 all_pass 非 true")
    detail = t1.get("detail")
    if not isinstance(detail, dict):
        problems.append("T6 Stage B 条件 1 缺 detail 对象（三条路由不可审）")
        detail = {}
    for route, (hits, total, thr) in _STAGE_B_ROUTES.items():
        d = detail.get(route)
        if not isinstance(d, dict):
            problems.append(f"T6 Stage B {route} 明细缺失（不可判定）")
            continue
        if (d.get("hits"), d.get("total")) != (hits, total):
            problems.append(
                f"T6 Stage B {route} 命中 {d.get('hits')}/{d.get('total')}"
                f" ≠ 预注册 {hits}/{total}")
        got = d.get("gold_at_8")
        if d.get("pass") is not True or not _is_num(got) or got < thr:
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
    rec_list = t3.get("recovered")
    if not isinstance(rec_list, list) \
            or len(rec_list) != _STAGE_B_RESIDUAL[1]:
        problems.append(
            f"T6 Stage B 回收清单 {rec_list!r} 非 {_STAGE_B_RESIDUAL[1]} 条列表")
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
    # T6 Stage B 判决件：**只认包内副本**（证据自足；R3 口径 (b)：包外活件
    # 兜底会让「包里缺科学证据」的包判绿）。副本与活件并存时逐字节比对，
    # 不一致＝问题（无法判断哪份是当次结论），不是缺口。
    verdict_path = None
    if pack_dir is None:
        problems.append("未指定证据包，无法核对 T6 Stage B 判决件")
    else:
        cand = (pack_dir / "task6_stage_b" / "window"
                / _STAGE_B_VERDICT.parent.name / "task6_r4_verdict.json")
        if not cand.is_file():
            problems.append(f"包内缺 T6 Stage B 判决件副本：{cand}")
        else:
            verdict_path = cand
            if _STAGE_B_VERDICT.is_file() and \
                    cand.read_bytes() != _STAGE_B_VERDICT.read_bytes():
                problems.append(
                    "T6 Stage B 判决件：包内副本与包外活件逐字节不一致"
                    "（无法判断哪份是当次科学结论）")
    if verdict_path is not None:
        v = json.loads(verdict_path.read_text(encoding="utf-8"))
        if v.get("verdict") != "RETAIN_R4" or v.get("retain") is not True:
            problems.append(f"T6 Stage B 判决非 RETAIN_R4：{v.get('verdict')}")
        problems += _stage_b_condition_problems(v.get("conditions") or {})
        ppm = v.get("point_prediction_match") or {}
        if not ppm.get("ok"):
            problems.append("T6 Stage B 点预测闸未过")
        if not isinstance(ppm.get("mismatches"), list):
            problems.append("T6 Stage B 点预测缺 mismatches 清单（不可判空）")
        elif ppm.get("mismatches") or ppm.get("n_cases") != 47:
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


# AC-T10-1：收尾全套必须是「两排除」命令，且规模不得塌到子集。
SUITE_FILTERS = ("not test_04", "not paid and not kb_integration")
# 规模下限是「跑成子集」的兜底 tripwire（R3 口径 (a)）：Task 10 收尾实测
# 2041 passed / 50 deselected（2026-11-02）；每次里程碑由人签上调，**不从日志
# 反推下限**（反推＝循环论证）。真正的硬约束是下面 parse_suite_log 与看板
# tests 段的逐字段互证。
MIN_FULL_SUITE_PASSED = 2000
MIN_FULL_SUITE_DESELECTED = 40


def parse_suite_log(text: str) -> "tuple[dict | None, list[str]]":
    """解析冻结冷跑日志（机判对日志的唯一口径，看板另有自己的一套实现）。

    要求：恰 1 条 ``# cmd:``（含两排除项）、恰 1 条 ``^rc=(\\d+)$``、至少一条
    pytest 摘要（取最后一条）、rc=0、passed/deselected 有下限。
    """
    problems: list[str] = []
    cmds = re.findall(r"^# cmd: (.+)$", text, re.M)
    if len(cmds) != 1:
        problems.append(f"冷跑日志 `# cmd:` 行应恰 1 条，实得 {len(cmds)}")
    cmd = cmds[0].strip() if len(cmds) == 1 else ""
    if cmd:
        miss = [f for f in SUITE_FILTERS if f not in cmd]
        if miss:
            problems.append(f"冷跑日志命令未含收尾两排除项 {miss}")
    rcs = re.findall(r"^rc=(\d+)$", text, re.M)
    if rcs != ["0"]:
        problems.append(f"冷跑日志 rc 行应恰为 rc=0：{rcs}")
    lines = [ln.strip() for ln in text.splitlines()
             if re.match(r"^\d+ passed", ln.strip())]
    if not lines:
        problems.append("冷跑日志缺 pytest 摘要行（不可判规模与去选数）")
        return None, problems
    summary = lines[-1]
    m = re.match(r"^(\d+) passed(?:, (\d+) skipped)?"
                 r"(?:, (\d+) deselected)?", summary)
    assert m is not None                       # 由过滤正则保证
    got = {"command": cmd, "passed": int(m.group(1)),
           "skipped": int(m.group(2) or 0),
           "deselected": int(m.group(3) or 0),
           "rc": int(rcs[0]) if rcs == ["0"] else None,
           "summary": summary}
    if got["passed"] < MIN_FULL_SUITE_PASSED:
        problems.append(
            f"冷跑日志 passed={got['passed']} < 全套下限 "
            f"{MIN_FULL_SUITE_PASSED}")
    if got["deselected"] < MIN_FULL_SUITE_DESELECTED:
        problems.append(
            f"冷跑日志 deselected={got['deselected']} < 收尾下限 "
            f"{MIN_FULL_SUITE_DESELECTED}（-k 单排除只有个位数）")
    return got, problems


# AC-T10-1 另一条腿：kb_integration 活库套必须显式跑绿（不能靠「默认 skip」
# 冒充）。下限同样是里程碑人签 tripwire，不从日志反推。
MIN_KBINT_PASSED = 30
KBINT_TOKENS = ("SIMPRAXIS_KB_INTEGRATION=1", "-m kb_integration")


def parse_kbint_log(text: str) -> "tuple[dict | None, list[str]]":
    """解析收尾 kbint 活库套日志（与冷跑日志同口径，命令特征不同）。"""
    problems: list[str] = []
    cmds = re.findall(r"^# cmd: (.+)$", text, re.M)
    if len(cmds) != 1:
        problems.append(f"kbint 日志 `# cmd:` 行应恰 1 条，实得 {len(cmds)}")
    cmd = cmds[0].strip() if len(cmds) == 1 else ""
    if cmd:
        miss = [t for t in KBINT_TOKENS if t not in cmd]
        if miss:
            problems.append(f"kbint 日志命令未含活库开关/选择器 {miss}")
    rcs = re.findall(r"^rc=(\d+)$", text, re.M)
    if rcs != ["0"]:
        problems.append(f"kbint 日志 rc 行应恰为 rc=0：{rcs}")
    lines = [ln.strip() for ln in text.splitlines()
             if re.match(r"^\d+ passed", ln.strip())]
    if not lines:
        problems.append("kbint 日志缺 pytest 摘要行（不可判规模）")
        return None, problems
    summary = lines[-1]
    m = re.match(r"^(\d+) passed", summary)
    assert m is not None                       # 由过滤正则保证
    got = {"command": cmd, "passed": int(m.group(1)),
           "rc": int(rcs[0]) if rcs == ["0"] else None, "summary": summary}
    if got["passed"] < MIN_KBINT_PASSED:
        problems.append(
            f"kbint 日志 passed={got['passed']} < 活库套下限 "
            f"{MIN_KBINT_PASSED}")
    return got, problems


def check_proc7(pack_dir: Path) -> list[str]:
    """P11/PROC-7：kbint 是重盖章器，必须用同口径快照差异自证「只动戳记」。

    包内缺件、差异件自称不 ok、或四表行数崩塌，一律判负——这条不许用
    「kbint 跑绿了」口头替代（跑绿恰恰会重盖章，必须看差异）。
    """
    path = pack_dir / "closeout" / "proc7_restamp_diff.json"
    if not path.is_file():
        return ["包内缺 PROC-7 重盖章差异件 closeout/proc7_restamp_diff.json"]
    try:
        diff = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return ["PROC-7 差异件不可解析"]
    problems: list[str] = []
    if diff.get("ok") is not True:
        problems.append(f"PROC-7 差异件未自证 ok：problems={diff.get('problems')!r}")
    if diff.get("problems") not in (None, []):
        problems.append(f"PROC-7 差异件挂问题：{diff.get('problems')!r}")
    if diff.get("q1q2q3_recall_equal") is not True:
        problems.append("PROC-7 差异件未自证召回快照恒等")
    tabs = diff.get("tables") or {}
    for name, want in EXPECTED_ROWS.items():
        rows = (tabs.get(name) or {}).get("rows_after")
        if rows != want:
            problems.append(f"PROC-7 差异件 {name}.rows_after={rows!r} ≠ {want}")
    return problems


def check_tests_cross(pack_dir: Path, dash: dict) -> list[str]:
    """R3 口径 (a)：两个独立读者（看板 tests 段 vs 包内冻结日志）逐字段互证。

    只看板、只包内日志各自成立还不够——看板被喂了另一份日志时，两边的
    passed/命令口径会分叉，这一层专抓该情形。
    """
    log_path = pack_dir / "task10_closeout" / "task10_fullsuite.log"
    t = dash.get("tests")
    if not log_path.is_file() or not isinstance(t, dict):
        return []              # 缺日志/看板 tests=null 已由各自检查记问题
    fields, _problems = parse_suite_log(
        log_path.read_text(encoding="utf-8", errors="replace"))
    if fields is None:
        return []
    return [f"看板 tests.{k}={t.get(k)!r} ≠ 包内冻结日志 {fields[k]!r}"
            for k in ("command", "passed", "skipped", "deselected", "rc",
                      "summary")
            if t.get(k) != fields[k]]


def check_dashboard(dash: dict, rec: dict) -> list[str]:
    problems = []
    if dash.get("schema") != "simpraxis.rsi_dashboard_phase25.v1":
        problems.append(f"看板 schema 不符：{dash.get('schema')}")
    t = dash.get("tests")
    if t is None:
        problems.append("看板 tests 为 null（未喂入全套实跑结果）")
    else:
        cmd = str(t.get("command") or "")
        missing = [f for f in SUITE_FILTERS if f not in cmd]
        if missing:
            problems.append(
                f"看板 tests.command 未含收尾两排除项 {missing}：{cmd!r}")
        if int(t.get("deselected") or 0) < MIN_FULL_SUITE_DESELECTED:
            problems.append(
                f"看板 tests.deselected={t.get('deselected')} < 收尾下限 "
                f"{MIN_FULL_SUITE_DESELECTED}（-k 单排除只有个位数）")
        if int(t.get("passed") or 0) < MIN_FULL_SUITE_PASSED:
            problems.append(
                f"看板 tests.passed={t.get('passed')} < 全套下限 "
                f"{MIN_FULL_SUITE_PASSED}")
        if t.get("rc") != 0:
            problems.append(f"看板 tests rc={t.get('rc')} 非 0")
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
    root_mode = stat.S_IMODE(pack_dir.lstat().st_mode)
    if root_mode != 0o700:
        problems.append(f"包根目录权限 {oct(root_mode)} ≠ 0700")
    for p in pack_dir.rglob("*"):
        if p.is_symlink():
            # 目录符号链接在 manifest 遍历里可能只表现为一个"文件"，这里显式拦截
            problems.append(f"包内符号链接：{p.relative_to(pack_dir)}")
            continue
        st = p.lstat()
        # 评审 LOW-3：FIFO/socket/设备既不在 is_file() 的 actual 集合里、
        # 也不进密钥扫描，必须单独判负（否则可被用来绕过全覆盖与扫描）
        if not (stat.S_ISDIR(st.st_mode) or stat.S_ISREG(st.st_mode)):
            problems.append(f"包内非普通文件：{p.relative_to(pack_dir)}")
            continue
        mode = stat.S_IMODE(st.st_mode)
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
    # AC-T10-1：包内冻结的冷跑日志必须自证「两排除 + rc=0 + 规模下限」，
    # 与看板 tests 段是两次独立读取（看板可被喂错日志，包内冻结件不行）。
    log_path = pack_dir / "task10_closeout" / "task10_fullsuite.log"
    if not log_path.is_file():
        problems.append("包内缺收尾冷跑日志 task10_closeout/task10_fullsuite.log")
    else:
        _fields, log_problems = parse_suite_log(
            log_path.read_text(encoding="utf-8", errors="replace"))
        problems += log_problems
        if _fields is not None and _fields["deselected"] == 0:
            problems.append("冷跑日志摘要缺 deselected 计数")
    # AC-T10-1 第二条腿：kbint 活库套必须显式跑绿并随包（否则「默认 skip」
    # 与「跑了绿」在包里分不出来）。
    kbint_path = pack_dir / "task10_closeout" / "task10_kbint.log"
    if not kbint_path.is_file():
        problems.append("包内缺收尾 kbint 活库套日志 "
                        "task10_closeout/task10_kbint.log")
    else:
        _kb, kb_problems = parse_kbint_log(
            kbint_path.read_text(encoding="utf-8", errors="replace"))
        problems += kb_problems
    # AC-T10-1 第三条腿：kbint 会重盖章 solution_chunks（P11/PROC-7），
    # 差异件必须随包证明「只动戳记」。
    problems += check_proc7(pack_dir)
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
    signs_marker = pack_dir / "task10_closeout" / "signs_MISSING.json"
    have_signs = signs_dir.is_dir() and any(signs_dir.glob("*.json"))
    if signs_marker.is_file() and have_signs:
        # 两态并存 = 状态自相矛盾，不许拿 MISSING 挂牌把实签件（含 REJECT）盖掉
        problems.append("三签件同时存在 signs_MISSING.json 与实际签件")
    if not have_signs:
        if signs_marker.is_file():
            gaps.append({"what": "Task 10 三签件", "path": str(signs_dir),
                         "why": "机判时点三签尚未落件（构建顺序：机判→三签→终建包）"})
        else:
            problems.append(f"缺三签件目录且无 signs_MISSING 挂牌：{signs_dir}")
    else:
        # 三签件齐备口径：executor/reviewer/verifier 三角色各一份、内容自带 role
        # 且与文件名一致、verdict 必须 APPROVE、commit 必须等于包冻结的
        # manifest.git_commit（旧提交的签不能给新包作数）。
        found: dict[str, dict] = {}
        for p in sorted(signs_dir.glob("*.json")) if signs_dir.is_dir() else []:
            try:
                s = json.loads(p.read_text(encoding="utf-8"))
            except ValueError:
                problems.append(f"三签件不可解析：{p.name}")
                continue
            role = s.get("role")
            if role != p.stem:
                # role 必须自证：缺 role 时不得用文件名顶替
                problems.append(
                    f"三签件 {p.name} 的 role={role!r} 与文件名不符")
                continue
            found[role] = s
        missing = {"executor", "reviewer", "verifier"} - set(found)
        if missing:
            problems.append(f"三签件缺角色：{sorted(missing)}")
        extra = set(found) - {"executor", "reviewer", "verifier"}
        if extra:
            problems.append(f"三签件出现未知角色：{sorted(extra)}")
        for role, s in found.items():
            if s.get("schema") != "simpraxis.phase25.sign.v1":
                problems.append(f"三签件 {role} schema 不符")
            if s.get("verdict") != "APPROVE":
                problems.append(
                    f"三签件 {role} 未 APPROVE：{s.get('verdict')}")
            if s.get("commit") != man.get("git_commit"):
                problems.append(
                    f"三签件 {role} 绑定 commit {s.get('commit')} ≠ "
                    f"manifest.git_commit {man.get('git_commit')}")
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
    # Complete 文档关键段（仓外）。评审 MINOR-10：终态文档缺段是**问题**
    # 而不是缺口——缺口不计入通过，等于把"文档没写"洗成"可以没有"。
    if COMPLETE.is_file():
        text = COMPLETE.read_text(encoding="utf-8")
        for needle in ("Task 6 Stage B", "RETAIN_R4", "T10"):
            if needle not in text:
                problems.append(f"Complete 文档缺 {needle} 段：{COMPLETE}")
    else:
        problems.append(f"缺 Complete 文档：{COMPLETE}")
    return problems, gaps


def check_commit_binding(dash: dict, pack_dir: Path) -> list[str]:
    """AC-T10-1 证据层：看板与证据包必须冻结在同一提交（评审 MINOR-9）。

    绑定对象是 ``manifest.git_commit``（包在建包时自证的提交），不是活 HEAD——
    终建包之后允许出现文档批，活 HEAD 前移不构成"包已过期"。但看板若与包
    不是同一提交，则 tests/对账/四表三段证据来自两个不同代码状态，不许判绿。
    """
    man_path = pack_dir / "manifest.json"
    if not man_path.is_file():
        return []           # 缺 manifest 已由 check_pack 记问题
    man = json.loads(man_path.read_text(encoding="utf-8"))
    commit = man.get("git_commit")
    got = (dash.get("git") or {}).get("commit")
    if not got:
        return ["看板 git.commit 缺失（AC-T10-1 证据层不可审）"]
    if got != commit:
        return [f"看板 git.commit={got} ≠ manifest.git_commit={commit}"]
    return []


# 评审 MINOR-7：verifier 签的 recomputed 块必须与机判自身重算的数逐项相等，
# 否则"三签 APPROVE"可建立在与机判不同的账/表/AC 事实上。
_VERIFIER_RECOMPUTED_PINS = (
    ("ac_total", ("ac", "total")),
    ("ac_per_task", ("ac", "per_task")),
    ("ac_ids_sha256", ("ac", "ids_sha256")),
)


def check_verifier_recomputed(pack_dir: Path, ac: dict, rec: "dict | None") -> list[str]:
    """把 verifier 签自报的 recomputed 数与机判自己的重算结果逐项对撞。"""
    sign_path = pack_dir / "task10_closeout" / "signs" / "verifier.json"
    if not sign_path.is_file():
        return []           # 签件缺位/齐备性由 check_pack 判（含三态）
    try:
        sign = json.loads(sign_path.read_text(encoding="utf-8"))
    except ValueError:
        return []           # 不可解析已由 check_pack 记问题
    rc = sign.get("recomputed")
    if not isinstance(rc, dict):
        return ["verifier 签缺 recomputed 块（三签不可与机判交叉互证）"]
    problems = []
    for key, (src, field) in _VERIFIER_RECOMPUTED_PINS:
        got, want = rc.get(key), ac[field]
        if got != want:
            problems.append(
                f"verifier 签 {key}={got!r} ≠ 机判重算 {want!r}")
    led = rc.get("ledger")
    if not isinstance(led, dict):
        problems.append("verifier 签缺 ledger 重算块")
    elif rec is not None:
        for field in ("n_calls", "ok", "fail"):
            if led.get(field) != rec["ledger"].get(field):
                problems.append(
                    f"verifier 签 ledger.{field}={led.get(field)!r} ≠ "
                    f"对账件 {rec['ledger'].get(field)!r}")
        if led.get("ledger_sha256") != rec.get("ledger_sha256"):
            problems.append("verifier 签 ledger_sha256 与对账件不符")
    if rec is not None:
        tabs = rc.get("tables")
        if not isinstance(tabs, dict):
            problems.append("verifier 签缺 tables 重算块")
        else:
            for name, want in EXPECTED_ROWS.items():
                got = (tabs.get(name) or {}).get("rows")
                if got != want:
                    problems.append(
                        f"verifier 签 {name}.rows={got!r} ≠ 收尾活值 {want}")
        appr = rc.get("approvals")
        if not isinstance(appr, dict) or \
                appr.get("total_approved") != rec["approvals"].get(
                    "total_approved"):
            problems.append(
                "verifier 签 approvals.total_approved 与对账件不符")
    return problems


def check_pack_vs_live(pack_dir: Path, rec: dict) -> list[str]:
    """包内冻结件与对账件的活值互证：账副本 sha 必须等于 ledger_sha256。

    check_ledger 复算活账、check_pack 复算包副本，两边都对却互相不一致时，
    只有这一层能抓到（否则「账与包各自自洽」即可绿）。
    """
    man_path = pack_dir / "manifest.json"
    if not man_path.is_file():
        return []          # 缺 manifest 已由 check_pack 记问题
    man = json.loads(man_path.read_text(encoding="utf-8"))
    files = {r["path"]: r for r in (man.get("files") or [])}
    entry = files.get("quota/ledger.jsonl")
    if entry is None:
        return ["包内缺 quota/ledger.jsonl（账不可审）"]
    if entry.get("sha256") != rec.get("ledger_sha256"):
        return ["包内账副本 sha256 与对账件 ledger_sha256 不符"]
    return []


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
    if rec is not None:
        problems += check_pack_vs_live(pack, rec)
        problems += check_verifier_recomputed(pack, ac, rec)
    if dash is not None:
        problems += check_commit_binding(dash, pack)
        problems += check_tests_cross(pack, dash)
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
