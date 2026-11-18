#!/usr/bin/env python3
"""S0 结案机判：四出口 + 依赖审计 + 账套对账（只读，problems=[] 才 rc=0）。

用法：
  python scripts/verification/check_s0_closure.py \
      --probe /mnt/artifacts/simpraxis/s0_probe/<stamp> \
      --audit runs/inventory/s0_baseline/physicalrsi_audit.json \
      --report <out>.json

判据（模式照 check_phase25_closure.py）：
  1. probe report schema/时间齐全，E1–E4 全绿；
  2. E1 五级阶梯（A1 有效响应/A2 落盘/A3 真实加载/A4 物理步进/A5 功能目标）+ G-CLAIM
     形状（自报位移 == host 重算）；
  3. E2 本批行数 == 逻辑调用数 ≤2、seq 从 1 连续、ts 严格 Z；
  4. E3 产者标记（session_id+model）与工具轨迹可重建、Write 重放零不符；
  5. E4 隔离负控（尝试过读且未泄漏哨兵）+ 门剧本（五剧本）；
  6. 依赖审计报告 pass=true。
gaps 如实挂牌（不阻断 rc=0）：headless 会话供应商消耗不在我方 ledger（E2 口径照计划
「PhysicalRSI 侧调用」；roadmap §6.3 的 headless 记账面缺口登记为 S1 立项项）。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

PROBLEMS: list[str] = []
GAPS: list[str] = []


def _load(path: Path, label: str):
    if not path.is_file():
        PROBLEMS.append(f"{label} 缺失：{path}")
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        PROBLEMS.append(f"{label} 非法 JSON：{exc}")
        return None


def check_probe(root: Path) -> None:
    report = _load(root / "report.json", "probe report")
    if report is None:
        return
    if report.get("schema") != "simpraxis.s0.probe_report.v1":
        PROBLEMS.append(f"report schema 非法：{report.get('schema')!r}")
    exits = report.get("exits") or {}
    for name in ("E1", "E2", "E3", "E4"):
        if exits.get(name) is not True:
            PROBLEMS.append(f"{name} 未通过（report.exits={exits.get(name)!r}）")

    e1 = _load(root / "e1_host_judgement.json", "E1 host judgement") or {}
    ladder = e1.get("ladder") or {}
    for level in ("A1_effective_response", "A2_artifacts_written", "A3_real_load",
                  "A4_physics_stepping", "A5_functional_target"):
        if ladder.get(level) is not True:
            PROBLEMS.append(f"E1 阶梯缺 {level}")
    if e1.get("claim_matches_host") is not True:
        PROBLEMS.append("E1 自报位移与 host 重算不符（G-CLAIM 形状）")
    displacement = e1.get("host_displacement_m")
    if not isinstance(displacement, (int, float)) or displacement < 0.05:
        PROBLEMS.append(f"E1 host 重算位移 {displacement!r} < 0.05 m")

    e2 = _load(root / "e2_ledger_reconciliation.json", "E2 reconciliation") or {}
    if e2.get("pass") is not True:
        PROBLEMS.append("E2 对账未通过")
    results = e2.get("results") or []
    if len(results) > 2:
        PROBLEMS.append(f"E2 小批次超过 2 呼：{len(results)}")
    # 不信任探针自述：直接重读账本复算（评审 HIGH 修复）
    from orchestrator.common.runtime_paths import rsi_s0_root

    declared = Path(str(e2.get("ledger_path") or ""))
    # 只算路径不建目录（判件必须只读）
    canonical = (rsi_s0_root() / "quota" / "ledger.jsonl").resolve(strict=False)
    if declared.resolve(strict=False) != canonical:
        PROBLEMS.append(f"E2 账本路径非 2908 规范路径：{declared} != {canonical}")
    ledger_path = declared
    if not ledger_path.is_file():
        PROBLEMS.append(f"E2 账本不可读：{ledger_path}")
    else:
        rows = []
        try:
            for line in ledger_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                row = json.loads(line)
                if not isinstance(row, dict):
                    PROBLEMS.append(f"账本行不是对象：{line[:80]!r}")
                    continue
                rows.append(row)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            PROBLEMS.append(f"账本存在不可解析行：{exc}")
        calls = [row for row in rows
                 if row.get("event") == "call" and isinstance(row.get("seq"), int)]
        seqs = [row["seq"] for row in calls]
        ts_ok = all(str(row.get("ts_iso", "")).endswith("Z") for row in calls)
        new_rows = [row for row in calls
                    if row["seq"] > int(e2.get("before_seq") or 0)]
        batch_new = [row for row in new_rows
                     if row.get("batch_id") == e2.get("batch_id")]
        if seqs != list(range(1, len(seqs) + 1)):
            PROBLEMS.append(f"账本 seq 不连续：{seqs}")
        if not ts_ok:
            PROBLEMS.append("账本 ts_iso 非严格 Z")
        if len(new_rows) != len(results):
            PROBLEMS.append(f"本轮新增账行 {len(new_rows)} != 逻辑调用 {len(results)}")
        if [bool(r.get("ok")) for r in new_rows] != [bool(x.get("ok")) for x in results]:
            PROBLEMS.append("本轮账行 ok 状态与调用结果不符")
        if len(batch_new) != len(new_rows):
            PROBLEMS.append(f"本轮新增中本批行 {len(batch_new)} != 全部新增 {len(new_rows)}")
        # 批准件必须存在且覆盖本轮调用数
        approvals = Path(str(e2.get("ledger_path"))).parent / "approvals"
        approval = approvals / f"{e2.get('batch_id')}.json"
        if not approval.is_file():
            PROBLEMS.append(f"缺批次批准件：{approval}")
        else:
            try:
                approve_doc = json.loads(approval.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                PROBLEMS.append(f"批次批准件不可解析：{approval}（{exc}）")
                approve_doc = {}
            if not isinstance(approve_doc, dict):
                PROBLEMS.append(f"批次批准件顶层不是对象：{approval}")
                approve_doc = {}
            if int(approve_doc.get("approved") or 0) < len(results):
                PROBLEMS.append("批准件次数小于本轮逻辑调用数")
        if e2.get("new_call_rows") != len(new_rows):
            PROBLEMS.append("探针自报新增行数与账本复算不符")
        # 未入账的失败/拦截 POST：degraded 告警件是唯一留痕，如实挂 gaps
        degraded = sorted(ledger_path.parent.glob("ledger.degraded.*.json"))
        unledgered = []
        for path in degraded:
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            meta = doc.get("call_meta") or {}
            if meta.get("batch_id") == e2.get("batch_id"):
                unledgered.append({"file": path.name,
                                   "audit_event": meta.get("audit_event"),
                                   "ok": meta.get("ok"),
                                   "err": str(meta.get("err"))[:160]})
        if unledgered:
            GAPS.append(
                f"{len(unledgered)} 次失败/拦截 POST 未入 ledger（2908 未登记写入路径期，"
                f"仅 ledger.degraded 留痕）：{[u['file'] for u in unledgered]}；"
                "不补记、不改时间戳")
        if e2.get("seqs") != seqs:
            PROBLEMS.append("探针自报 seqs 与账本复算不符")
    if (e1.get("transcript") or {}).get("session_id"):
        GAPS.append("headless 会话的供应商消耗不在我方 ledger（E2 口径照计划仅覆盖 "
                    "PhysicalRSI 侧调用；roadmap §6.3 的 headless 记账面缺口转 S1 立项，"
                    "价格记 UNKNOWN）")

    e3 = _load(root / "e3_reconstruction.json", "E3 reconstruction") or {}
    if not e3.get("session_id") or not e3.get("model"):
        PROBLEMS.append("E3 缺产者标记（session_id/model）")
    if e3.get("write_replay_mismatches"):
        PROBLEMS.append(f"E3 Write 重放不符：{e3['write_replay_mismatches'][:3]}")
    if not e3.get("tool_calls"):
        PROBLEMS.append("E3 工具轨迹为空，不能证明可重建")

    e4 = _load(root / "e4_isolation.json", "E4 isolation") or {}
    if e4.get("pass") is not True:
        PROBLEMS.append("E4 隔离负控未成立（未尝试或发生泄漏）")
    gates = _load(root / "e4_gate_rehearsal.json", "E4 gate rehearsal") or {}
    if gates.get("pass") is not True:
        PROBLEMS.append("E4 门剧本未逐项命中")


def check_audit(path: Path) -> None:
    audit = _load(path, "physicalrsi audit")
    if audit is None:
        return
    if audit.get("pass") is not True:
        PROBLEMS.append("依赖审计未通过")
    for check in audit.get("checks", []):
        if not check.get("pass"):
            PROBLEMS.append(f"审计项失败：{check.get('id')}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--probe", required=True, type=Path)
    parser.add_argument("--audit", required=True, type=Path)
    parser.add_argument("--report", type=Path, default=None)
    args = parser.parse_args()

    check_probe(args.probe)
    check_audit(args.audit)
    verdict = {
        "schema": "simpraxis.s0.closure_judgement.v1",
        "judged_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "probe_root": str(args.probe),
        "audit": str(args.audit),
        "problems": PROBLEMS,
        "gaps": GAPS,
        "pass": not PROBLEMS,
    }
    text = json.dumps(verdict, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(text, encoding="utf-8")
    print(json.dumps({"pass": verdict["pass"], "problems": len(PROBLEMS),
                      "gaps": len(GAPS)}, ensure_ascii=False))
    return 0 if verdict["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
