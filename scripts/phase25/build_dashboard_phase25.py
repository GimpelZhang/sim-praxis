#!/usr/bin/env python3
"""Task10：Phase 2.5 RSI 看板 `dashboard_phase25.json`（AC-T10-7，0 付费）。

schema ``simpraxis.rsi_dashboard_phase25.v1``。只读活件 + 吃两份外部输入
（收尾对账件、全套测试结果），不调用 ModelGateway、不联网、不读凭据内容。

必含段：
- ``version/git/generated_at``：commit、分支、攒批未推送的 ahead 计数
  （只读 ``git rev-list --count``；推送与否永远是人工 STOP 门）；
- ``tests``：全套实跑摘要（passed/skipped/deselected/rc + 原始摘要行），
  未喂入时如实为 ``null``，绝不把收集清单冒充全绿；
- ``lance_tables``：四表行数/维度/向量集 sha（取自收尾对账件，同一口径）；
- ``tasks``：Task 0–10 状态与**三态科学结论**（P0–P5 探针、墙 A/B-eng/
  B-cap、gold@8 Stage A 负结果 / Stage B 保留 r4、T7/T8/T9 用户裁决停）；
- ``paid_reconciliation``：phase25 独立账（seq/ok/fail/by_task/by_stage/
  by_batch）、批准件、缓存窗口内新增 0；
- ``methodology``：方法学 v2 区间件 P50/P90 vs 实际（AC-T1-7）；
- ``ac_counts``：从 Plan 正文去重提取的 AC-Tn-k 计数（逐 Task 与总数），
  与 §9 追溯矩阵声明值对账；
- ``closeout``：容器存量/显存//mnt（gate_static 六步、作者校验、密钥双扫
  由机判 ``check_phase25_closure.py`` 承担并在其报告里落证）。

``--strict``：任一段缺失或与对账件不符即 exit 2（收尾期默认开）。
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))

from orchestrator.common.clock import utc_now_iso_z  # noqa: E402

SCHEMA = "simpraxis.rsi_dashboard_phase25.v1"
PLAN = Path.home() / "文档/Phase_2.5_Plan_detailed.md"

# §9 追溯矩阵声明值（v1.2；AC-T10-7 机判口径）。
DECLARED_AC_TOTAL = 102
DECLARED_AC_PER_TASK = {"T0": 7, "T1": 7, "T2": 8, "T3": 8, "T4": 18,
                        "T5": 7, "T6": 8, "T7": 8, "T8": 9, "T9": 12,
                        "T10": 10}

# Task 状态与三态科学结论（结论性事实；证据指针在 evidence_pack/manifest）
TASK_RESULTS = [
    {"task": "T0", "status": "closed", "batch": "108", "paid": 0,
     "result": "开工基座成立：phase25 独立账/封存守卫/依赖裁决 smolagents "
               "1.26.0 零额外传递依赖；基线黄金快照冻结。"},
    {"task": "T1", "status": "closed", "batch": "110", "paid": 0,
     "result": "方法学 v2 交付（错误墙为报价单位、失败税分箱、cache 两套"
               "口径、区间件 P50/P90）；样本内回代五箱全在 P50–P90。"},
    {"task": "T2", "status": "closed", "batch": "109", "paid": 0,
     "result": "看门狗 SOP 成立：双签名/PDEATHSIG/元看门狗/故障注入"
               "（含静默检测），drill 与误报反证齐备。"},
    {"task": "T3", "status": "closed", "batch": "111", "paid": 6,
     "result": "Stage0 传输探针 6/6 帽 ok=T,T,F,F,T,T：R2 smolagents "
               "CodeAgent 主路线成立（P1 parse 100%）；P3/P4 判否并留证。"},
    {"task": "T4", "status": "closed", "batch": "112", "paid": 0,
     "result": "agentic 内环产品化：五轮对抗 C1/C2 CRITICAL + C3/C4 MAJOR "
               "+ M1–M5 全闭；RunSpec.agentic 保持 opt-in。"},
    {"task": "T5", "status": "closed_not_established", "batch": "113/114",
     "paid": 25,
     "result": "历史墙两态实证：两墙干净触帽 16/16 + 9/9 共 25 呼全 ok，"
               "工程墙不成立；B-cap 空虚满足不产生独立结论。"},
    {"task": "T5b", "status": "closed_not_established", "batch": "118",
     "paid": 25,
     "result": "协议加固后同冻结墙重测：工程不成立 × H1 支持（空轮 "
               "9/25→0/25；墙B M1 真 watertight rc0；墙A 自探正确导入未进"
               "终稿到帽）。矩阵建议维持 opt-in/冻结。"},
    {"task": "T6", "status": "closed_retain_r4", "batch": "121–134",
     "paid": 0,
     "result": "Gate1 四路召回：r3 成立（38/47=0.8085）；Stage A 换嵌入"
               "（Qwen3-0.6B/1024）五条件 2/4 FAIL → 诚实负结果回滚 mpnet/"
               "768；Stage B r4（稠密 top-8 ⊕ BM25 top-8，RRF k=60）判 "
               "RETAIN_R4：45/47=0.9574、零翻失、9 残留回收 7（gold_id 直中"
               " 6）、点预测 47/47 逐字相符。"},
    {"task": "T7", "status": "stopped_by_user", "batch": "—", "paid": 0,
     "result": "stages[] 多阶段状态机：用户 2026-11-02 裁决不再进行。"},
    {"task": "T8", "status": "stopped_by_user", "batch": "—", "paid": 0,
     "result": "飞书富卡+真人 drill：用户 2026-11-02 裁决不再进行。"},
    {"task": "T9", "status": "stopped_by_user", "batch": "—", "paid": 0,
     "result": "VLA LIBERO 多阶段战役：用户 2026-11-02 裁决不再进行；"
               "其区间件 interval_t9.json 原样保留不回收。"},
    {"task": "T10", "status": "closed", "batch": "134+", "paid": 0,
     "result": "里程碑收尾：对账/证据包/看板/机判/Complete/三签。"},
]


class DashboardError(RuntimeError):
    """看板缺段或与对账件不符；CLI exit 2，不产出"半张绿"看板。"""


def _run(cmd: list[str]) -> str:
    return subprocess.run(cmd, cwd=_REPO, capture_output=True,
                          text=True).stdout.strip()


def git_state() -> dict:
    return {
        "commit": _run(["git", "rev-parse", "HEAD"]),
        "branch": _run(["git", "rev-parse", "--abbrev-ref", "HEAD"]),
        "ahead_origin_main": int(_run(
            ["git", "rev-list", "--count", "origin/main..HEAD"]) or 0),
        "push_policy": "本地攒批未推送；推送仅经用户显式窗口指示（人工 STOP 门）",
    }


_SUMMARY_RE = re.compile(
    r"^(?P<line>.*?(?P<passed>\d+) passed(?:, (?P<skipped>\d+) skipped)?"
    r"(?:, (?P<deselected>\d+) deselected)?.+?)$", re.M)


def tests_state(result_path: "Path | None") -> "dict | None":
    """吃全套实跑日志：末条 pytest 摘要 + 尾部 rc 行。"""
    if result_path is None:
        return None
    text = result_path.read_text(encoding="utf-8", errors="replace")
    matches = list(_SUMMARY_RE.finditer(text))
    if not matches:
        raise DashboardError(f"测试结果里找不到 pytest 摘要行：{result_path}")
    m = matches[-1]
    rcs = re.findall(r"^rc=(\d+)$", text, re.M)
    if len(rcs) != 1:
        # 多段日志的 rc 与"最后一条摘要"会错配（前段 0 / 后段 1）；
        # 恰好一条 rc 才允许配对，否则 fail-closed。
        raise DashboardError(
            f"测试结果里 rc= 行应恰 1 条，实得 {len(rcs)} 条：{result_path}")
    cmd = re.search(r"^# cmd: (.+)$", text, re.M)
    if cmd is None:
        # 无 `# cmd:` 头的日志无法证明跑的是哪套 → 不许拿规范命令串顶替
        raise DashboardError(
            f"测试结果里找不到 `# cmd:` 行（命令口径不可审）：{result_path}")
    return {
        "source": str(result_path),
        "summary": m.group("line").strip(),
        "passed": int(m.group("passed")),
        "skipped": int(m.group("skipped") or 0),
        "deselected": int(m.group("deselected") or 0),
        "rc": int(rcs[0]),
        "command": cmd.group(1).strip(),
    }


def ac_counts(plan_path: Path = PLAN) -> dict:
    """AC-T10-7 机判口径：从 Plan 正文去重提取 AC-Tn-k。"""
    text = plan_path.read_text(encoding="utf-8")
    ids = sorted(set(re.findall(r"AC-T(\d+)-(\d+)", text)),
                 key=lambda t: (int(t[0]), int(t[1])))
    per: dict[str, int] = {}
    for task, _k in ids:
        key = f"T{int(task)}"
        per[key] = per.get(key, 0) + 1
    return {
        "plan": str(plan_path),
        "total": len(ids),
        "per_task": {f"T{i}": per.get(f"T{i}", 0) for i in range(11)},
        "declared_total": DECLARED_AC_TOTAL,
        "declared_per_task": DECLARED_AC_PER_TASK,
        "matches_declared": (len(ids) == DECLARED_AC_TOTAL
                             and {f"T{i}": per.get(f"T{i}", 0)
                                  for i in range(11)}
                             == DECLARED_AC_PER_TASK),
    }


def build(*, reconcile_path: Path, tests_result: "Path | None",
          plan_path: Path = PLAN) -> dict:
    if not reconcile_path.is_file():
        raise DashboardError(f"缺收尾对账件：{reconcile_path}")
    rec = json.loads(reconcile_path.read_text(encoding="utf-8"))
    if rec.get("schema") != "simpraxis.phase25.closeout_reconcile.v1":
        raise DashboardError(f"对账件 schema 不符：{rec.get('schema')}")
    led = rec["ledger"]
    appr = rec["approvals"]
    cache = rec["call_cache"]
    env = rec["env"]
    counts = ac_counts(plan_path)
    if not counts["matches_declared"]:
        raise DashboardError(
            f"AC 计数与 §9 声明不符：total={counts['total']}"
            f"（声明 {DECLARED_AC_TOTAL}），per_task={counts['per_task']}")
    return {
        "schema": SCHEMA,
        "version": "1.0",
        "generated_at": utc_now_iso_z(),
        "git": git_state(),
        "tests": tests_state(tests_result),
        "lance_tables": {k: v for k, v in rec["tables"].items()
                         if not k.startswith("_")},
        "tasks": TASK_RESULTS,
        "paid_reconciliation": {
            "ledger": {k: led[k] for k in
                       ("n_calls", "seq_first", "seq_last",
                        "seq_contiguous_from_1", "ts_all_strict_z",
                        "ok", "fail", "by_task", "by_stage", "by_batch",
                        "tokens")},
            "fail_rows": led["fail_rows"],
            "approvals": appr,
            "call_cache": {
                "n_entries_total": cache.get("n_entries_total"),
                "n_entries_in_window": cache.get("n_entries_in_window_count"),
                "coding_hits_structurally_unobservable":
                    cache.get("coding_hits_structurally_unobservable"),
            },
            "ledger_sha256": rec.get("ledger_sha256"),
        },
        "methodology": rec["methodology"],
        "ac_counts": counts,
        "closeout": {
            "containers_present": env["containers_present"],
            "vram_mib": env["vram_mib"],
            "vram_ok_le_200": env["vram_ok_le_200"],
            "mnt": env["mnt"],
            "audited_from": str(reconcile_path),
        },
    }


def main(argv: "list[str] | None" = None) -> int:
    from orchestrator.common.runtime_paths import phase25_root
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--reconcile", type=Path, default=None)
    ap.add_argument("--tests-result", type=Path, default=None,
                    help="提交后两排除全套实跑日志（含 rc= 行）")
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--plan", type=Path, default=PLAN)
    args = ap.parse_args(argv)
    root = phase25_root()
    recon = args.reconcile or (root / "task10_milestone"
                               / "closeout_reconcile.json")
    out_path = args.out or (root / "task10_milestone"
                            / "dashboard_phase25.json")
    try:
        dash = build(reconcile_path=recon, tests_result=args.tests_result,
                     plan_path=args.plan)
    except DashboardError as exc:
        print(f"[DASHBOARD-FAIL] {exc}", file=sys.stderr)
        return 2
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(dash, ensure_ascii=False, indent=1) + "\n",
                        encoding="utf-8")
    t = dash["tests"]
    tests_desc = "n/a" if t is None else f"{t['passed']} passed rc={t['rc']}"
    print(f"dashboard {out_path} ac_total={dash['ac_counts']['total']} "
          f"tests={tests_desc} "
          f"ledger={dash['paid_reconciliation']['ledger']['n_calls']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
