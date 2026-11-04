"""Phase 2.7 Task 4（D4-1..D4-8b）：双墙破局实证机判 —— AC-T4-1..9。

对应计划 ``~/文档/Phase_2.7_Plan_detailed.md`` §T4.6 的九条验收准则，逐条一个
用例（名字与计划机判命令逐字一致）：

``test_ac_t4_1_criteria_locked`` / ``test_ac_t4_2_wall_a_verdict`` /
``test_ac_t4_3_wall_b_verdict`` / ``test_ac_t4_4_effective_ratio`` /
``test_ac_t4_5_ledger_reconcile`` / ``test_ac_t4_6_report_no_cross_ruler_claim`` /
``test_ac_t4_7_no_auto_unfreeze`` / ``test_ac_t4_8_unfreeze_triplet`` /
``test_ac_t4_9_early_stop_honest``。

纪律（硬规则 2/7）：

- **0 实呼、0 联网**：本文件只读已在册证据件（``$SIMPRAXIS_PHASE27_ROOT/walls/
  rebreakthrough/<ts>/``）与账本，不调用网关、不触网；
- **断言不放水**：阈值/子帽/票号一律从 ``scripts.phase27.run_wall_rebreakthrough``
  的常量与账本读，不抄字面量；判决为负（``NOT_ESTABLISHED``）时 AC 照样通过——
  计划 §4.2 明写这 4 条（AC-T4-2/3/7/8）是**过程型**，机判只断言「证据齐备且
  自洽」，**不能**断言结论为正面（这是硬规则 7「矛盾即结论」的直接后果）；
- **不 skip**：证据件缺失即红（``assert``），不写 ``pytest.skip``/``xfail``。
- AC-T4-1 的「模式 R 与冻结 V3 逐条等价」用**独立实现**复核：本文件自己剥标记块、
  自己做三处替换，再要求与冻结 V3 **逐字节相等**——不复用被测模块的同一函数，
  否则等于让被考核者给自己判卷。
"""
from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:            # 允许 `pytest tests/...` 直接跑
    sys.path.insert(0, str(REPO))

from orchestrator.agent_runner.runner import RunSpec   # noqa: E402
from orchestrator.gateway.quota_ledger import (        # noqa: E402
    PHASE27_HARD_CAP,
    PHASE27_TASK_CAPS,
    QuotaLedger,
    call_lines,
    read_lines,
)
import scripts.phase27.run_wall_rebreakthrough as p27       # noqa: E402

TASK_ID = 4
BATCHES = ("P27-B1", "P27-B1b")
ENVELOPE = {"P27-B1": p27.MODE_R["total"], "P27-B1b": p27.MODE_N["total"]}

# 报告黑名单（AC-T4-6 / AC-T4-7，计划 §T4.6 与 §10.3 的同一份清单）
CROSS_RULER_BLACKLIST = ("优于 V4", "优于V4", "打破历史记录", "比 V4 好", "比V4好")
UNFREEZE_BLACKLIST = ("已具备解冻条件", "可以解冻")


# --------------------------------------------------------------- 路径与读取

def phase27_root() -> Path:
    env = os.environ.get("SIMPRAXIS_PHASE27_ROOT")
    if env:
        return Path(env)
    raise AssertionError(
        "SIMPRAXIS_PHASE27_ROOT 未设置（先 source scripts/00_env.sh）")


def phase26_root() -> Path:
    env = os.environ.get("SIMPRAXIS_PHASE26_ROOT")
    if env:
        return Path(env)
    return phase27_root().parent / "phase26"


def rebreak_root() -> Path:
    return phase27_root() / "walls" / "rebreakthrough"


def session_dir() -> Path:
    ptr = rebreak_root() / "CURRENT"
    assert ptr.is_file(), f"本次战役未初始化：{ptr} 缺失"
    d = rebreak_root() / ptr.read_text(encoding="utf-8").strip()
    assert d.is_dir(), f"CURRENT 指向的会话目录不存在：{d}"
    return d


def criteria_file(mode: str) -> Path:
    return session_dir() / f"VERDICT_CRITERIA_PHASE27_{mode}.md"


def lock_file(mode: str = "R") -> Path:
    """模式 R 的 LOCK 件名沿用 phase26 的 ``LOCK.json``；模式 N 用 ``LOCK_N.json``。

    锁内适配 #10（2026-11-04，登记于 §5.0b）：R 的 LOCK 已封 0444，N 必须另有落点，
    否则「首呼前必封判据」对 N 无件可证。
    """
    name = "LOCK.json" if mode == "R" else f"LOCK_{mode}.json"
    return session_dir() / "manifests" / name


def wall_result(wall: str, mode: str = "R") -> dict:
    """取**指定标尺**下该墙最新一次尝试的判决件。

    锁内适配 #11（2026-11-04，登记于 Phase_2.7_Plan_Complete.md §5.0b）：两模式
    共用一个会话根、按墙分目录，``*/wall_result.json`` 的「最新一个」在模式 N
    跑起来之后就不再是 R 的那一份。AC-T4-2 点名「Wall A（**模式 R**）」，故默认
    按 R 取；AC-T4-3 分模式，显式传 mode。按记录自带的 ``mode`` 字段过滤，不靠
    目录顺序猜。
    """
    base = session_dir() / wall
    assert base.is_dir(), f"{wall} 无运行目录（墙未跑）：{base}"
    recs = [json.loads(p.read_text(encoding="utf-8"))
            for p in sorted(base.glob("*/wall_result.json"))]
    assert recs, f"{wall} 缺 wall_result.json（AC-T4-2/3 过程型：判决件必须存在）"
    mine = [r for r in recs if r.get("mode") == mode]
    assert mine, (f"{wall} 没有模式 {mode} 的 wall_result.json（现有模式："
                  f"{sorted({r.get('mode') for r in recs})}）")
    return mine[-1]


def ledger_rows() -> list[dict]:
    return read_lines(QuotaLedger.phase27().ns.ledger_path_fn())


def _strip_marked(text: str, begin: str, end: str) -> str:
    """删掉标记行**及其间全部内容**（行首标记；独立实现，不复用被测模块）。"""
    out, skip = [], False
    for line in text.splitlines(keepends=True):
        tag = line.strip()
        if tag == begin:
            assert not skip, f"标记嵌套：{begin}"
            skip = True
            continue
        if tag == end:
            assert skip, f"未闭合的结束标记：{end}"
            skip = False
            continue
        if not skip:
            out.append(line)
    assert not skip, f"标记未闭合：{begin}"
    return "".join(out)


def _apply(text: str, subs) -> str:
    for old, new, want in subs:
        got = text.count(old)
        assert got == want, (
            f"替换锚点漂移（命中 {got}，期望 {want}）：{old[:60]!r}")
        text = text.replace(old, new)
    return text


# ------------------------------------------------------------------ AC-T4-1

def test_ac_t4_1_criteria_locked():
    """预注册件首呼前落盘 + LOCK；模式 R 与冻结 V3 逐条等价（新增条目 0）。"""
    p = criteria_file("R")
    assert p.is_file(), f"缺模式 R 预注册件：{p}"
    lock = lock_file()
    assert lock.is_file(), "缺 LOCK.json（判据未预注册，AC-T4-1 红）"
    rec = json.loads(lock.read_text(encoding="utf-8"))
    assert rec["mode"] == "R", f"LOCK 属于模式 {rec['mode']!r}"
    assert rec["batch_id"] == "P27-B1", f"LOCK 票号 {rec['batch_id']!r}"

    # 封存三重取证：sha256 / mtime / mode=0444（跑后一字未改）
    st = p.stat()
    assert p27.fc.sha256_file(p) == rec["criteria_sha256"], "判据 sha256 与 LOCK 不符"
    assert int(st.st_mtime) == int(rec["criteria_mtime"]), (
        f"判据 mtime 与 LOCK 不符（{int(st.st_mtime)} vs "
        f"{rec['criteria_mtime']}）——首呼后改动即作废")
    assert oct(st.st_mode & 0o777) == "0o444", (
        f"判据文件 mode 应为 0444，实为 {oct(st.st_mode & 0o777)}")

    # 首呼前：判据 LOCK 的时间必须早于该批任何占批行
    call_rows = [r for r in ledger_rows() if r.get("batch_id") == "P27-B1"]
    if call_rows:
        first_ts = min(r["ts_iso"] for r in call_rows)
        assert rec["locked_iso"] <= first_ts, (
            f"LOCK 晚于首呼（{rec['locked_iso']} > {first_ts}）——预注册失效")

    # —— 与冻结 V3 逐条等价（独立复核）——
    v3p = phase26_root() / "walls" / "retest" / "VERDICT_CRITERIA_V3.md"
    assert v3p.is_file(), f"缺 AC-T4-1 判据源（冻结 V3）：{v3p}"
    v3 = v3p.read_text(encoding="utf-8")

    # 三处已登记替换必须在被测模块里恰好是这三条，且各命中一次
    subs = list(p27._P27_R_SUBS)
    assert len(subs) == 3, f"模式 R 只允许三处已登记替换，实有 {len(subs)} 处"
    assert subs[0][:2] == (".entrypoint.py", "agent_solution.py"), (
        "第一处必须是 D26-8d 出生件名替换")
    assert all(want == 1 for _, _, want in subs), "每处替换各须恰好命中一次"

    text = p.read_text(encoding="utf-8")
    stripped = _strip_marked(
        _strip_marked(text, p27._HEADER_BEGIN, p27._HEADER_END),
        p27._EARLY_BEGIN, p27._EARLY_END)
    norm = _apply(v3, subs)
    norm = norm.partition("\n")[2].lstrip("\n")     # 去掉 V3 原文标题行
    assert stripped == norm, (
        "模式 R 判据 ≠ 冻结 V3 + 三处已登记替换（AC-T4-1「新增条目数 == 0」"
        "以逐字节相等机判；差异即新增/删除/改动了判据条目）")

    # 判据永不弱化：四个硬阈值逐字在场（读 V3 侧，不抄字面量）
    for needle in ("rounds_to_first_birth <= 8", "calls_to_rc0 <= 15",
                   "sentinel_count == 0", "negative_physical == 0"):
        assert needle in v3 and needle in stripped, f"判据条目缺失：{needle}"

    # 计划 §T4.5 第 4 步要求预注册件带模型面与生成时刻（硬规则 1 可审计）
    header = text.split(p27._HEADER_END, 1)[0]
    assert "coding_debug" in header and p27.t26.coding_debug_model_id() in header, (
        "预注册件缺模型面（硬规则 1：判据可比性依赖模型 ID）")
    assert re.search(r"ts_iso = `\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z`", header), (
        "预注册件缺 ts_iso 生成时刻")


def test_ac_t4_1b_mode_n_criteria_locked():
    """模式 N 的预注册件同样首呼前 LOCK（锁内适配 #10：各模式各封各的）。

    计划 §T4.5 步骤 8 要求模式 N「须先 `--write-criteria --mode N` 并 LOCK」；
    只验 R 的 LOCK 会让 N 的「首呼前封判据」无件可证。本用例把同一套封存三重
    取证（sha256 / mtime / 0444）+ 首呼前时限，施加在 N 上。
    """
    p = criteria_file("N")
    assert p.is_file(), f"缺模式 N 预注册件：{p}"
    lock = lock_file("N")
    assert lock.is_file(), "缺 LOCK_N.json（模式 N 判据未预注册，AC-T4-1 红）"
    rec = json.loads(lock.read_text(encoding="utf-8"))
    assert rec["mode"] == "N", f"LOCK_N 属于模式 {rec['mode']!r}"
    assert rec["batch_id"] == "P27-B1b", f"LOCK_N 票号 {rec['batch_id']!r}"

    st = p.stat()
    assert p27.fc.sha256_file(p) == rec["criteria_sha256"], "N 判据 sha256 与 LOCK_N 不符"
    assert int(st.st_mtime) == int(rec["criteria_mtime"]), (
        f"N 判据 mtime 与 LOCK_N 不符（{int(st.st_mtime)} vs "
        f"{rec['criteria_mtime']}）——首呼后改动即作废")
    assert oct(st.st_mode & 0o777) == "0o444", (
        f"N 判据文件 mode 应为 0444，实为 {oct(st.st_mode & 0o777)}")

    call_rows = [r for r in ledger_rows() if r.get("batch_id") == "P27-B1b"]
    assert call_rows, "模式 N 尚无实呼行"
    first_ts = min(r["ts_iso"] for r in call_rows)
    assert rec["locked_iso"] <= first_ts, (
        f"LOCK_N 晚于 N 的首呼（{rec['locked_iso']} > {first_ts}）——预注册失效")

    # N 判据的已登记替换：出生轮上限 8→5（新标尺）、票号/子额度、两处「28→35」
    # 信封字样（§5.0 末尾已登记）；逐条各命中一次，替换后件内不再自相矛盾。
    text = p.read_text(encoding="utf-8")
    assert len(p27._P27_N_SUBS) == 4, (
        f"模式 N 只允许四处已登记替换，实有 {len(p27._P27_N_SUBS)} 处")
    assert all(want == 1 for _, _, want in p27._P27_N_SUBS), "每处替换各须恰好命中一次"
    assert "硬包络 28" not in text and "硬包络 35" in text
    assert "rounds_to_first_birth <= 5" in text
    assert p27.MODE_N["total"] == 35


# ------------------------------------------------------------------ AC-T4-2

WALL_A_CHECKS = 12


def test_ac_t4_2_wall_a_verdict():
    """墙 A 判决件存在且自洽：三态之一、可复算、失败原因已登记。"""
    rec = wall_result("wallA")
    assert rec["mode"] == "R" and rec["batch_id"] == "P27-B1"
    assert rec["passed"] in (True, False)

    # rounds_to_first_birth 可从 rounds.jsonl 复算
    rounds_p = Path(rec["run_dir"]) / "rounds.jsonl"
    assert rounds_p.is_file(), f"缺 rounds.jsonl：{rounds_p}"
    rounds = [json.loads(x) for x in rounds_p.read_text(encoding="utf-8").splitlines()
              if x.strip()]
    assert rounds, "rounds.jsonl 为空——轮记录缺失无法复算"
    # 轮数 / 有效轮数从 rounds.jsonl 逐行复算（判据 §3 的两择一实现见
    # phase26 `_productive_rounds`；这里只做**独立复算**，不引用同一函数）
    assert rec["rounds"] == len(rounds), (
        f"rounds={rec['rounds']} 与 rounds.jsonl 行数 {len(rounds)} 不符")
    prod = sum(1 for r in rounds if r.get("productive") is True)
    assert rec["productive_rounds"] == prod, (
        f"productive_rounds={rec['productive_rounds']} 与复算 {prod} 不符")
    # 出生轮：轨迹里标记为 birth_round 的轮必须与判决件一致；
    # 反向不强制（出生信息也可能来自 outcome.extra，此时该轮按工具计产出）。
    idxs = [r["round_idx"] for r in rounds]
    birth_marks = [r["round_idx"] for r in rounds
                   if r.get("productive_reason") == "birth_round"]
    born = rec["rounds_to_first_birth"]
    if born is not None:
        assert born in idxs, f"出生轮 {born} 不在 rounds.jsonl 轮号集合内"
    if birth_marks:
        assert born == birth_marks[0], (
            f"birth_round 标记 {birth_marks[0]} 与判决件 {born} 不符")

    # final_rc 与评分件一致
    score_p = Path(rec["run_dir"]) / "score" / "stdout.log"
    assert score_p.is_file(), f"缺评分 stdout：{score_p}"
    assert isinstance(rec["final_rc"], int)
    sf = rec.get("score_file") or ""
    if sf and Path(sf).is_file():
        sc = json.loads(Path(sf).read_text(encoding="utf-8"))
        assert len(sc.get("checks") or []) == rec["checks_n"], (
            f"checks_n={rec['checks_n']} 与评分件 {len(sc.get('checks') or [])} 不符")

    # 12 项检查名逐条在场（失败时由 wall_result 的 checks_n 与原因佐证）
    assert rec["checks_n"] <= WALL_A_CHECKS
    if rec["passed"]:
        assert rec["checks_n"] == WALL_A_CHECKS, (
            f"passed=True 但 checks_n={rec['checks_n']} ≠ {WALL_A_CHECKS}")
        assert rec["final_rc"] == 0
        assert rec["fail_reasons"] == []
    else:
        assert rec["fail_reasons"], (
            "失败但未登记 fail_reasons——AC-T4-2 要求失败路径可审计")
        assert rec["birth_round_limit"] == p27.BIRTH_ROUND_LIMIT_R

    # 触帽解读口径字段必填（计划 §T4.9 / v1.3）
    note = rec.get("amortization_note") or {}
    for key in ("rounds_used", "cap_exhausted", "last_round_tools", "faea_anchor"):
        assert key in note, f"缺 amortization_note.{key}（缺字段即 AC-T4-2 红）"
    assert "37.3" in note["faea_anchor"], "FAEA 锚点缺失（Spatial 37.3 turn/task）"


# ------------------------------------------------------------------ AC-T4-3

# 12 项见证的**规范名**取自冻结评分器的成功路径（计划 §T4.6 写作「M2_*×3 / K1–K6」，
# 并明写 `solution_import` / `M2_topology_instrument` / `K5K6_harness` 是**兜底名、
# 不计入**这 12 项）。下面的字面量由 `_wall_b_canonical_names()` 对冻结 V4 评分件
# 逐名核验——不是手抄的「理想名」，抄错即红。
WALL_B_CHECKS = (
    "S0_no_sentinel_literals", "S1_no_test_hardcoding", "M1_vessel",
    "M2_open_shell_768_2304", "M2_nonmanifold_fan", "M2_historical_1536",
    "K1_all_pass", "K2_static", "K3_too_fast",
    "K4_penetrating", "K5_nonfinite_raises", "K6_degenerate_raises",
)


def _wall_b_canonical_names() -> set[str]:
    """冻结评分器成功路径的 12 个检查名（单一真值 = 冻结 V4 评分件）。

    取 phase26 `retest_v4` 的墙 B 评分件：它是**冻结评分器**在真实工作目录上
    跑出来的 12 项完整名表（`checks` 长度 12），与本次模式 N 的评分件逐名相同。
    用它做单一真值，可防止手抄名字漂移（首版曾按计划里的「M2_mesh_watertight」
    之类理想名手抄，跑出真件后逐名比对才发现并不存在这些名字）。
    """
    p = (phase26_root() / "walls" / "retest_v4" / "runs" / "wallB" /
         "20261103T050900Z" / "work" / ".score" / "wall_b-20261103T051357.json")
    assert p.is_file(), f"缺冻结 V4 墙 B 评分件（12 名单一真值）：{p}"
    rec = json.loads(p.read_text(encoding="utf-8"))
    names = [c["check"] for c in rec.get("checks") or []]
    assert len(names) == 12, f"冻结评分器应有 12 项检查，实有 {len(names)}：{names}"
    return set(names)


@pytest.mark.parametrize("mode", ["R", "N"])
def test_ac_t4_3_wall_b_verdict(mode):
    """墙 B 判决件存在且 12 项见证逐条在场、证据可复算（分模式，计划 §T4.6）。

    锁内适配 #9（2026-11-04，登记于 Phase_2.7_Plan_Complete.md §5.0）：计划
    假设失败路径下评分器仍会写出判件（``load_solution`` 失败会落
    ``solution_import`` 兜底名）。实测另一条更早的失败路径：**模型从未写出
    入口件**时，冻结评分器在 ``main()`` 第 172 行 ``SOLUTION_PATH.read_text()``
    就直接抛 ``FileNotFoundError``，早于任何 ``check()``，判件根本不存在。
    冻结评分器不得改（计划 T4.4 第 4 条），故此处按「判件在场」与「判件缺席」
    两分支判：缺席分支只接受三联互证（``score_artifact_absent`` +
    ``loop_kind=birth_absent`` + stderr 逐字指向 ``agent_solution.py``），并
    反向断言入口件确实不在——**不降低**任何一条正面路径的断言。
    """
    rec = wall_result("wallB", mode)
    assert set(WALL_B_CHECKS) == _wall_b_canonical_names(), (
        "本文件的 12 项见证名与冻结评分器成功路径不一致（手抄漂移即红）："
        f"多 {sorted(set(WALL_B_CHECKS) - _wall_b_canonical_names())} / "
        f"少 {sorted(_wall_b_canonical_names() - set(WALL_B_CHECKS))}")
    assert rec["mode"] == mode and rec["batch_id"] == p27.BATCH_BY_MODE[mode]
    assert rec["passed"] in (True, False)
    if rec["passed"]:
        w12 = rec["witness_12"]
        assert isinstance(w12, dict)
        assert set(w12) == set(WALL_B_CHECKS), (
            "12 项见证名与冻结判据不符："
            f"多 {sorted(set(w12) - set(WALL_B_CHECKS))} / "
            f"少 {sorted(set(WALL_B_CHECKS) - set(w12))}")
        for name, ok in w12.items():
            assert isinstance(ok, bool), f"见证 {name} 不是布尔值"
        assert all(w12.values()), f"passed=True 但有见证未过：{w12}"
        assert rec["sentinel_count"] == 0 and rec["negative_physical"] == 0
        assert rec["scorer_rc"] == 0
        assert rec["paid_calls"] <= p27.CALLS_TO_RC0_LIMIT_B
    else:
        assert rec["fail_reasons"], "失败但未登记 fail_reasons"
        if rec.get("score_file"):
            w12 = rec["witness_12"]
            assert set(w12) == set(WALL_B_CHECKS), (
                "判件在场但 12 项见证名不全："
                f"缺 {sorted(set(WALL_B_CHECKS) - set(w12))}")
        else:
            assert rec["score_file_reason"] == "score_artifact_absent"
            assert rec["loop_kind"] == "birth_absent", (
                "判件缺席只因入口件未出生；其他缺席原因不得放过")
            assert rec["rounds_to_first_birth"] is None
            entry = Path(rec["run_dir"]) / "work" / "agent_solution.py"
            assert not entry.exists(), (
                "入口件在场却无判件——评分器异常，不得以缺判件放过")
            err = (Path(rec["run_dir"]) / "score" / "stderr.log").read_text(
                encoding="utf-8", errors="replace")
            assert "FileNotFoundError" in err and "agent_solution.py" in err, (
                "缺判件的根因必须能在 stderr 里逐字复算")
    # 评分器证据件在场（可复算）
    for rel in ("score/stdout.log", "score/stderr.log", "outcome.json"):
        assert (Path(rec["run_dir"]) / rel).is_file(), f"缺证据件 {rel}"

    if mode == "N":
        # 计划 §T4.6 AC-T4-3 的「模式 N 额外」三条：四 API 经 AST 可核、技能库
        # import 探针 rc=0、工具层拒绝计数如实登记（deny>0 不判失败）。
        assert "policy_denies" in rec, (
            "模式 N 必须如实登记工具层拒绝计数（cad_guard/policy deny）")
        probe_p = session_dir() / "skills_import_probe.json"
        assert probe_p.is_file(), "缺技能库 import 探针件（模式 N 预检必落盘）"
        probe = json.loads(probe_p.read_text(encoding="utf-8"))
        assert probe.get("rc") == 0, f"技能库 import 探针 rc≠0：{probe}"
        assert "SKILLS_IMPORT_OK" in str(probe.get("stdout") or ""), (
            f"import 探针未打印成功标记：{probe}")
        assert int(probe.get("files") or 0) > 0, "探针未记录装配文件数"
        work = Path(rec["run_dir"]) / "work"
        lib = work / "simpraxis_skills" / "wall_b" / "instruments.py"
        assert lib.is_file(), "模式 N 的 workdir 必须带已装配的技能库"
        lib_tree = ast.parse(lib.read_text(encoding="utf-8"))
        apis = {n.name for n in lib_tree.body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and not n.name.startswith("_")}
        assert len(apis) >= 4, f"技能库公开入口不足四件：{sorted(apis)}"
        entry = work / "agent_solution.py"
        if entry.is_file():
            tree = ast.parse(entry.read_text(encoding="utf-8"))
            provided: set[str] = set()
            star = False
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    provided.add(node.name)
                elif isinstance(node, ast.ImportFrom):
                    names = [a.name for a in node.names]
                    if names == ["*"]:
                        star = True
                    provided.update(names)
                elif isinstance(node, ast.Assign):
                    for tgt in node.targets:
                        if isinstance(tgt, ast.Name):
                            provided.add(tgt.id)
            missing = set() if star else (apis - provided)
            assert not missing, (
                f"入口件未提供/转发四 API：缺 {sorted(missing)}")
        else:
            assert rec["loop_kind"] == "birth_absent", (
                "入口件缺席只能因出生失败，不得以其它原因解释")


# ------------------------------------------------------------------ AC-T4-4

@pytest.mark.parametrize("mode", ["R", "N"])
def test_ac_t4_4_effective_ratio(mode):
    """有效步数占比 ≥ 0.80（定义复用 phase26 预注册，逐字；分模式复核）。"""
    import scripts.phase26.run_wall_retest as t26m

    # 同一标尺：阈值常量与合并算法都必须是 phase26 的实现（不是抄一份）
    assert p27.EFFECTIVE_RATIO_FLOOR == t26m.EFFECTIVE_RATIO_FLOOR, (
        "占比阈值与 phase26 预注册不一致（判据不许改界）")
    assert p27.EFFECTIVE_RATIO_FLOOR == 0.80

    a, b = wall_result("wallA", mode), wall_result("wallB", mode)
    for name, rec in (("wallA", a), ("wallB", b)):
        r, pr = int(rec["rounds"]), int(rec["productive_rounds"])
        assert 0 <= pr <= r, f"{name}（{mode}）productive_rounds 越界"
    # 用 phase26 的 `_pooled_ratio` 复算：同一实现、同一口径
    ratio = t26m._pooled_ratio(a, b)
    assert ratio is not None, "两墙合计 0 轮——无有效证据"

    verdict = json.loads(
        (session_dir() / f"verdict_phase27_{mode}.json").read_text(
            encoding="utf-8"))
    assert abs(verdict["effective_step_ratio"] - ratio) < 1e-9, (
        f"判决件占比 {verdict['effective_step_ratio']} 与 phase26 口径复算 "
        f"{ratio} 不符")
    assert verdict["ratio_floor"] == p27.EFFECTIVE_RATIO_FLOOR
    # 占比本身不设「必须 ≥ 0.80」的假门槛：它是判决输入，负结论时低于阈值才是
    # 诚实结果（硬规则 7）。此处只断言**判决与占比自洽**。
    if verdict["verdict"] == p27.V_ESTABLISHED:
        assert ratio >= p27.EFFECTIVE_RATIO_FLOOR, (
            "判 ESTABLISHED 但占比低于预注册下限——判决与判据不符")


# ------------------------------------------------------------------ AC-T4-5

def test_ac_t4_5_ledger_reconcile():
    """账：各票实呼 ≤ 信封、任务 ≤ 63、全局 ≤ 178、seq 连续、票据四字段齐备。"""
    rows = ledger_rows()
    assert rows, "phase27 账为空（至少应有 selfcheck 开账行）"
    seqs = [r["seq"] for r in rows if isinstance(r.get("seq"), int)]
    assert seqs == list(range(1, len(seqs) + 1)), (
        f"seq 不连续：{seqs[:5]}…（首行必须是 selfcheck 占 seq 不计实呼）")
    assert rows[0]["event"] == "selfcheck" and rows[0]["seq"] == 1, (
        "开账行必须是 event=selfcheck / seq=1（不得删除以保 seq 连续）")
    assert rows[0]["model"] is None and rows[0]["ok"] is True

    paid = [r for r in rows if r.get("event") == "call"]
    by_batch: dict[str, int] = {}
    for r in paid:
        b = r.get("batch_id") or ""
        by_batch[b] = by_batch.get(b, 0) + 1
    for batch, cap in ENVELOPE.items():
        assert by_batch.get(batch, 0) <= cap, (
            f"票据 {batch} 实呼 {by_batch.get(batch, 0)} > 信封 {cap}")

    task_used = sum(1 for r in call_lines(rows) if r.get("task") == TASK_ID)
    assert task_used <= PHASE27_TASK_CAPS[TASK_ID] == 63, (
        f"任务 4 累计 {task_used} > 帽 {PHASE27_TASK_CAPS[TASK_ID]}")
    global_used = max(int(r.get("global_used_after") or 0) for r in rows)
    assert global_used <= PHASE27_HARD_CAP == 178, (
        f"全局 {global_used} > 硬帽 {PHASE27_HARD_CAP}")

    # 批准件四字段与账本闸门一致（plan_check 如实落盘）
    approvals = QuotaLedger.phase27().ns.approvals_dir_fn()
    for batch, cap in ENVELOPE.items():
        p = approvals / f"{batch}.json"
        assert p.is_file(), f"缺批准件：{p}"
        rec = json.loads(p.read_text(encoding="utf-8"))
        assert rec["batch_id"] == batch
        assert rec["task"] == TASK_ID
        assert rec["stage"] == "coding_debug"
        assert rec["approved"] == cap, f"{batch} approved={rec['approved']} ≠ {cap}"
        assert rec["beyond_normal"] is False
        assert rec["ledger_phase"] == 27, "票据必须绑定 phase27 账"
    extra = sorted({r.get("batch_id") for r in paid} - set(BATCHES) - {""})
    assert not extra, f"出现计划外批次（D4-8b 禁止第三张票）：{extra}"

    plan = json.loads((session_dir() / "plan_check.json").read_text(encoding="utf-8"))
    assert plan["ok"] is True, f"plan_check 未通过：{plan}"
    assert plan["envelope_sum"] == p27.MODE_R["total"] + p27.MODE_N["total"] == 63
    assert plan["task_cap"] == 63 and plan["hard_cap"] == 178


# ------------------------------------------------------------------ AC-T4-6

def test_ac_t4_6_report_no_cross_ruler_claim():
    """报告含可比性声明（R=可比 / N=不可比），且无跨标尺措辞。"""
    rep = session_dir() / "rebreakthrough_report_R.md"
    assert rep.is_file(), f"缺模式 R 报告：{rep}"
    text = rep.read_text(encoding="utf-8")
    assert "可比" in text, "报告缺「与 V3/V4 可比」声明（AC-T4-6）"
    assert "不可比" in text, "报告须注明模式 N 不可比"
    for bad in CROSS_RULER_BLACKLIST:
        assert bad not in text, f"报告出现跨标尺措辞「{bad}」（AC-T4-6 黑名单）"

    verdict = json.loads(
        (session_dir() / "verdict_phase27_R.json").read_text(encoding="utf-8"))
    assert verdict["comparable_to_v3_v4"]["comparable"] is True, (
        "模式 R 必须声明可比（与 V3 逐字同条件）")
    assert "P27-B1b" in text, (
        "报告须点名另一模式的票据号（`P27-B1b`），否则「N 不可比」无从落地")


# ------------------------------------------------------------------ AC-T4-7

def test_ac_t4_7_no_auto_unfreeze():
    """解冻建议件存在，且默认值未被修改、无「可以解冻」类表述。"""
    rec_p = session_dir() / "unfreeze_recommendation_R.md"
    assert rec_p.is_file(), f"缺解冻建议件：{rec_p}"
    text = rec_p.read_text(encoding="utf-8")
    assert "不解冻" in text, "建议件必须明确「不解冻」"
    for bad in UNFREEZE_BLACKLIST:
        assert bad not in text, f"建议件出现「{bad}」（AC-T4-7 黑名单）"

    # 默认值未被本 Task 触碰（运行期真值，比 diff 更强）
    assert RunSpec.agentic is False, "RunSpec.agentic 默认值被改写（BR-2 禁）"
    assert RunSpec.agentic_config is None, "RunSpec.agentic_config 默认值被改写"
    assert RunSpec.tool_grants == (), "RunSpec.tool_grants 默认值被改写"

    verdict = json.loads(
        (session_dir() / "verdict_phase27_R.json").read_text(encoding="utf-8"))
    assert verdict["unfreeze"]["recommended"] is False, (
        "判决件 unfreeze.recommended 必须为 False")


# ------------------------------------------------------------------ AC-T4-8

def _agentic_default_history(limit: int = 400) -> list[tuple[str, str]]:
    """逐提交读 runner.py 里 ``agentic: bool = …`` 的实际行。"""
    shas = subprocess.run(
        ["git", "-C", str(REPO), "log", f"-{limit}", "--format=%H", "--",
         "orchestrator/agent_runner/runner.py"],
        check=True, stdout=subprocess.PIPE).stdout.decode().split()
    out = []
    for sha in shas:
        blob = subprocess.run(
            ["git", "-C", str(REPO), "show",
             f"{sha}:orchestrator/agent_runner/runner.py"],
            stdout=subprocess.PIPE)
        if blob.returncode != 0:
            continue
        for line in blob.stdout.decode("utf-8", "replace").splitlines():
            if line.strip().startswith("agentic: bool ="):
                out.append((sha, line.strip()))
                break
    return out


def test_ac_t4_8_unfreeze_triplet():
    """三件套不齐备 ⇒ 全历史不得有改 RunSpec.agentic 默认值的提交（反绕过闸）。

    **无条件执行**：BR-2 被拒**不**使本 AC 变成 N/A（硬规则 7 禁 skip）。
    """
    triplet = {
        "prereg_phase27_unfreeze": phase27_root() / "unfreeze" /
        "prereg_phase27_unfreeze.md",
        "gate1_dual_lane": phase27_root() / "unfreeze" / "gate1_dual_lane.json",
        "t5_t5b_rerun": phase27_root() / "unfreeze" / "t5_t5b_rerun_verdict.json",
        "t4_contract_rerun": phase27_root() / "unfreeze" /
        "t4_contract_rerun_verdict.json",
    }
    complete = all(p.is_file() for p in triplet.values())
    history = _agentic_default_history()
    assert history, "读不到 RunSpec.agentic 的历史默认值行"
    if not complete:
        bad = [(sha, line) for sha, line in history if line != "agentic: bool = False"]
        assert not bad, (
            "三件套未齐备，但历史上有提交改了 RunSpec.agentic 默认值："
            + repr(bad[:3]))


# ------------------------------------------------------------------ AC-T4-9

def test_ac_t4_9_early_stop_honest():
    """早停合法性与诚实标注（D4-8/8b）。"""
    text = criteria_file("R").read_text(encoding="utf-8")
    rule_declared = p27.EARLY_STOP_RULE_TEXT in text
    assert rule_declared, (
        "早停规则未逐字出现在首呼前 LOCK 的预注册件里（AC-T4-9①）")

    verdict = json.loads(
        (session_dir() / "verdict_phase27_R.json").read_text(encoding="utf-8"))
    block = verdict["early_stop"]
    assert set(block) >= {"occurred", "rounds_used", "early_stop_reason",
                          "transferred_to", "transfer_amount"}, (
        f"early_stop 块字段不全：{sorted(block)}")
    assert block["occurred"] is False or rule_declared, (
        "occurred=True 但预注册里没有早停规则——AC-T4-9① 红")

    if block["occurred"]:
        wa = wall_result("wallA")
        assert block["rounds_used"] == 8, f"早停轮数 {block['rounds_used']} ≠ 8"
        assert block["rounds_used"] == wa["rounds"], "早停判决件与墙结果轮数不符"
        assert block["early_stop_reason"] == "birth_absent_at_round_8"
        assert verdict["verdict"] == p27.V_NOT_ESTABLISHED, (
            "早停不得改判：判决必须仍为 NOT_ESTABLISHED（D4-8）")
        assert block["transferred_to"] == "N"
        assert block["transfer_amount"] == p27.MODE_R["total"] - block["rounds_used"], (
            "转移额必须等于 28 − rounds_used（AC-T4-9③）")
        assert verdict["comparable_to_v3_v4"]["comparable_rounds"] == 8
        rep = (session_dir() / "rebreakthrough_report_R.md").read_text(
            encoding="utf-8")
        assert "comparable_rounds=8" in rep, "报告缺 comparable_rounds=8 诚实标注"
        assert "完整重打 16 轮" not in rep, "报告出现「完整重打 16 轮」（禁止）"
    else:
        assert block["transfer_amount"] == 0
        assert block["transferred_to"] is None

    # 账上批准件恒为两张（D4-8b：不得为转移另开第三张票）
    approvals = QuotaLedger.phase27().ns.approvals_dir_fn()
    names = sorted(p.stem for p in approvals.glob("*.json"))
    assert set(names) <= set(BATCHES), f"出现第三张票：{names}"
    rows = ledger_rows()
    task_used = sum(1 for r in call_lines(rows) if r.get("task") == TASK_ID)
    assert task_used <= PHASE27_TASK_CAPS[TASK_ID] == 63
    assert max(int(r.get("global_used_after") or 0) for r in rows) <= PHASE27_HARD_CAP
