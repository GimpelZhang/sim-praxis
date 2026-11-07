"""Phase 2.8.1 P281-9 终判机判 —— AC-P281-9-08..17（计划 §13.6）。

两层：

* **合成层**（默认两排除套就跑）：在 tmp 根上**造一个全绿的收尾现场**
  （终局报告 → 报告文本 → 证据包 → 冷跑 → 验收车道 → 三签 → 矩阵 → 终判），
  然后逐条把它改坏一次，断言对应检查**恰好**判红。只有活库行数与五本旧账
  这两项用 monkeypatch 顶替（它们读的是仓外实体，合成根里没有）。
* **活产物层**（``kb_integration`` 标记）：对真根跑一遍全量 ``run_judge``，
  断言 ``problems == []``、``gaps`` 只含两条预注册不做项、不动点两等式成立。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
for _p in (str(_REPO), str(_REPO / "scripts" / "verification"),
           str(_REPO / "scripts" / "benchmark")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import build_evidence_pack as packer  # noqa: E402
import check_phase281_closure as judge  # noqa: E402
import phase281_closeout as closeout  # noqa: E402
from orchestrator.benchmark.phase281 import analysis  # noqa: E402
from tests.benchmark import phase281_closeout_fixtures as fx  # noqa: E402
from tests.kb_support import ENV_FLAG as KB_INTEGRATION_FLAG  # noqa: E402

live = pytest.mark.skipif(
    not os.environ.get(KB_INTEGRATION_FLAG),
    reason=f"{KB_INTEGRATION_FLAG} 未开（活产物车道只在收尾显式开闸时跑）")

FROZEN_TABLES = {"kb_chunks": 1542, "phase1_learnings": 44,
                 "phase2_learnings": 1, "solution_chunks": 106}
COLD_SUMMARY = "195 passed, 7 skipped, 50 deselected in 351.95s"


def _write_final_report(root: Path) -> dict:
    report = analysis.build_final_report(root)
    path = root / "analysis" / "final_report.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2,
                               sort_keys=True) + "\n", encoding="utf-8")
    return report


def _write_old_ledgers(root: Path) -> "list[Path]":
    made = []
    for name in ("phase1", "phase2"):
        path = root / "old" / f"{name}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'{{"phase": "{name}", "seq": 1}}\n', encoding="utf-8")
        made.append(path)
    fx.write_json(root / "intake" / "ledger_state.json", {
        "generated_by": "synthetic",
        "ledgers": {p.stem: {"path": str(p), "sha256": judge._sha256(p),
                             "rows": 1} for p in made},
    })
    return made


def build_green_root(tmp_path: Path, monkeypatch) -> Path:
    """造一个**全绿**的收尾现场（顺序即 §13.5 的执行顺序）。"""
    root = fx.make_root(tmp_path / "root", reps=(0,))
    _write_old_ledgers(root)
    monkeypatch.setattr(judge, "_table_counts",
                        lambda uri, frozen=dict(FROZEN_TABLES): dict(frozen))

    report = _write_final_report(root)
    (root / "reports").mkdir(parents=True, exist_ok=True)
    (root / "reports" / "final_report.md").write_text(
        analysis.render_report_markdown(report), encoding="utf-8")

    packer.build(root=root, pack=root / "evidence_pack", clean=True,
                 bench_dirs=closeout.BENCH_DIRS_281, repo_paths=())

    log = tmp_path / "cold.log"
    log.write_text(f"$ pytest -q\n{COLD_SUMMARY}\n", encoding="utf-8")
    closeout.cmd_cold_run(root, log, 0)

    closeout._write_json(root / "closeout" / "acceptance_lane.json", {
        "schema": "simpraxis.phase281.acceptance_lane.v1",
        "rc": 0, "passed": 42, "failed": 0, "skipped": 0, "deselected": 0,
        "log": str(log), "log_sha256": judge._sha256(log),
    })
    closeout._write_json(root / "closeout" / "environment.json", {
        "schema": "simpraxis.phase281.environment.v1",
        "containers": 0, "compose_rc": 0, "gpu_mib": 14,
        "df_mnt": "/dev/sda1 500G 42G 458G 9% /mnt", "probe_error": None,
    })

    head = judge._git("rev-parse", "HEAD")
    pack_sha = judge.pack_sha256(root)
    assert pack_sha, "证据包必须已产出 pack.sha256"
    for role in judge.SIGN_ROLES:
        closeout._write_json(root / "closeout" / "signs" / f"{role}.json", {
            "role": role, "verdict": "APPROVE", "git_commit": head,
            "pack_sha256": pack_sha, "signed_at": "2026-11-07T00:00:00Z",
            "notes": "synthetic",
        })

    assert closeout.cmd_matrix(root)["summary"]["failed"] == 0
    judge.write_judge(root)
    return root


@pytest.fixture
def green(tmp_path, monkeypatch):
    return build_green_root(tmp_path, monkeypatch)


def _problems(root: Path, check: str) -> "list[str]":
    fn = judge._CHECKS[check]
    problems, _detail = fn(root)
    return problems


# ---------------------------------------------------------------------------
# 全绿基线
# ---------------------------------------------------------------------------

def test_green_root_passes_every_check(green):
    for name in judge.CHECKS:
        problems, _detail = (judge.collect_gaps(green)
                             if name == "gaps" else judge._CHECKS[name](green))
        assert problems == [], f"{name}: {problems}"
    doc = json.loads((green / judge.JUDGE_RELPATH).read_text(encoding="utf-8"))
    assert doc["verdict"] == "PASS"
    assert doc["problems"] == []
    assert doc["gaps"] == ["CONFIRMATORY_NOT_RUN_PILOT_HOLD",
                           "SEMANTIC_SCORES_ABSENT_NO_CONFIRMATORY"]


# ---------------------------------------------------------------------------
# AC-P281-9-08：证据包卫生
# ---------------------------------------------------------------------------

def test_ac_p281_9_08_pack_hygiene(green):
    problems, detail = judge.check_pack(green)
    assert problems == [], problems
    assert detail["pack_sha256"] == judge.pack_sha256(green)
    assert detail["secrets_hits"] == 0
    assert detail["n_files"] > 0


def test_ac_p281_9_08_negative_tampered_file(green):
    target = next((green / "evidence_pack" / "intake").glob("*.json"))
    target.write_text(target.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    problems, _ = judge.check_pack(green)
    assert any("sha256 与 manifest 不符" in p for p in problems), problems


def test_ac_p281_9_08_negative_bad_mode(green):
    target = next((green / "evidence_pack" / "intake").glob("*.json"))
    os.chmod(target, 0o644)
    problems, _ = judge.check_pack(green)
    assert any("模式非 0600" in p for p in problems), problems


def test_ac_p281_9_08_negative_missing_file_and_pack_digest(green):
    target = next((green / "evidence_pack" / "intake").glob("*.json"))
    target.unlink()
    problems, _ = judge.check_pack(green)
    assert any("包内缺件" in p for p in problems), problems

    (green / "evidence_pack" / "pack.sha256").write_text("0" * 64 + "\n",
                                                         encoding="utf-8")
    problems, _ = judge.check_pack(green)
    assert any("pack.sha256 与 manifest.json 的实际 sha256 不符" in p
               for p in problems), problems


# ---------------------------------------------------------------------------
# AC-P281-9-09：判决件族不入包
# ---------------------------------------------------------------------------

def test_ac_p281_9_09_verdicts_not_packed(green):
    problems, detail = judge.check_judgment_not_packed(green)
    assert problems == [], problems
    assert detail["offenders"] == []


def test_ac_p281_9_09_negative_judge_inside_pack(green):
    inner = green / "evidence_pack" / "analysis" / "closure_judge_x.json"
    inner.write_text("{}\n", encoding="utf-8")
    problems, detail = judge.check_judgment_not_packed(green)
    assert problems and "closure_judge_x.json" in problems[0], detail
    # 打包器的同一个判据必须同判（两处各写一份就会被这条钉住）
    assert packer.is_judgment_relpath("analysis/closure_judge_x.json")
    assert judge._is_judgment_relpath("analysis/closure_judge_x.json")


def test_ac_p281_9_09_signs_are_judgment_family():
    assert packer.is_judgment_relpath("closeout/signs/executor.json")
    assert judge._is_judgment_relpath("closeout/signs/executor.json")
    assert not judge._is_judgment_relpath("analysis/final_report.json")


# ---------------------------------------------------------------------------
# AC-P281-9-10：不动点两等式
# ---------------------------------------------------------------------------

def test_ac_p281_9_10_fixed_point(green):
    problems, detail = judge.verify_fixed_point(green)
    assert problems == [], problems
    assert detail["deterministic"] is True
    assert detail["pack_sha256_recorded"] == detail["pack_sha256_actual"]
    assert detail["judgment_family_excluded"] is True


def test_ac_p281_9_10_negative_pack_changed_after_judge(green):
    """包件在判决之后被改：① 仍成立（manifest 没变），② 立刻破。

    两条等式是**两条**：改包件破的是「可复算」，改 ``pack.sha256`` 破的是
    「判决件记的那个包」。合起来看，任何一次判决后的包改动都跑不掉。
    """
    target = next((green / "evidence_pack" / "intake").glob("*.json"))
    target.write_text("{}\n", encoding="utf-8")
    problems, detail = judge.verify_fixed_point(green)
    assert detail["pack_sha256_recorded"] == detail["pack_sha256_actual"]  # ① 未破
    assert detail["deterministic"] is False                                # ② 破了
    assert any("不逐字节相同" in p for p in problems), problems

    # 再改 pack.sha256 本身 ⇒ 等式①也破
    (green / "evidence_pack" / "pack.sha256").write_text("1" * 64 + "\n",
                                                         encoding="utf-8")
    problems, detail = judge.verify_fixed_point(green)
    assert any("与 evidence_pack/pack.sha256 的实际内容不符" in p
               for p in problems), problems


def test_ac_p281_9_10_negative_judge_tampered(green):
    path = green / judge.JUDGE_RELPATH
    path.write_text(path.read_text(encoding="utf-8").replace(
        '"verdict": "PASS"', '"verdict": "FAIL"'), encoding="utf-8")
    problems, detail = judge.verify_fixed_point(green)
    assert detail["deterministic"] is False
    assert any("不逐字节相同" in p for p in problems), problems


# ---------------------------------------------------------------------------
# AC-P281-9-11：三签
# ---------------------------------------------------------------------------

def test_ac_p281_9_11_signs_bound(green):
    problems, detail = judge.check_signs(green)
    assert problems == [], problems
    assert set(detail["roles"]) == set(judge.SIGN_ROLES)
    for role in judge.SIGN_ROLES:
        assert detail["roles"][role]["pack_sha256"] == judge.pack_sha256(green)


def test_ac_p281_9_11_negative_missing_and_reject(green):
    (green / "closeout" / "signs" / "verifier.json").unlink()
    problems, _ = judge.check_signs(green)
    assert any("缺 verifier 签" in p for p in problems), problems

    closeout._write_json(green / "closeout" / "signs" / "verifier.json", {
        "role": "verifier", "verdict": "REJECT",
        "git_commit": judge._git("rev-parse", "HEAD"),
        "pack_sha256": judge.pack_sha256(green)})
    problems, _ = judge.check_signs(green)
    assert any("不是 APPROVE" in p for p in problems), problems


def test_ac_p281_9_11_negative_not_ancestor_and_wrong_pack(green):
    closeout._write_json(green / "closeout" / "signs" / "executor.json", {
        "role": "executor", "verdict": "APPROVE",
        "git_commit": "0" * 40, "pack_sha256": judge.pack_sha256(green)})
    problems, _ = judge.check_signs(green)
    assert any("不是 HEAD" in p for p in problems), problems

    closeout._write_json(green / "closeout" / "signs" / "executor.json", {
        "role": "executor", "verdict": "APPROVE",
        "git_commit": judge._git("rev-parse", "HEAD"), "pack_sha256": "2" * 64})
    problems, _ = judge.check_signs(green)
    assert any("pack_sha256 与" in p for p in problems), problems


# ---------------------------------------------------------------------------
# AC-P281-9-12：终判干净 / gaps 只许预注册不做项
# ---------------------------------------------------------------------------

def test_ac_p281_9_12_judge_clean(green):
    doc = judge.run_judge(green)
    assert doc["problems"] == []
    assert doc["verdict"] == "PASS"
    assert set(doc["gaps"]) <= judge.ALLOWED_GAPS


def test_ac_p281_9_12_negative_unregistered_gap(green, monkeypatch):
    monkeypatch.setattr(judge, "ALLOWED_GAPS", frozenset())
    problems, gaps = judge.collect_gaps(green)
    assert gaps and problems, (problems, gaps)


def test_ac_p281_9_12_negative_any_problem_is_red(green):
    (green / "closeout" / "environment.json").unlink()
    doc = judge.run_judge(green)
    assert doc["verdict"] == "FAIL"
    assert any(p.startswith("[environment]") for p in doc["problems"])


# ---------------------------------------------------------------------------
# AC-P281-9-13：冷跑
# ---------------------------------------------------------------------------

def test_ac_p281_9_13_cold_run(green):
    problems, detail = judge.check_cold_run(green)
    assert problems == [], problems
    assert detail["rc"] == 0
    assert detail["summary"] == {"passed": 195, "skipped": 7,
                                 "deselected": 50, "duration_s": 351.95}


def test_ac_p281_9_13_negative_rc_and_unparsable(green):
    doc = json.loads((green / "closeout" / "cold_run.json").read_text(encoding="utf-8"))
    doc["rc"] = 1
    (green / "closeout" / "cold_run.json").write_text(
        json.dumps(doc, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
        encoding="utf-8")
    problems, _ = judge.check_cold_run(green)
    assert any("冷跑 rc=1" in p for p in problems), problems

    doc["rc"] = 0
    (green / "closeout" / "cold_run.json").write_text(
        json.dumps(doc, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
        encoding="utf-8")
    Path(doc["log"]).write_text("no summary here\n", encoding="utf-8")
    problems, _ = judge.check_cold_run(green)
    assert any("解析不出 pytest 摘要" in p for p in problems), problems


# ---------------------------------------------------------------------------
# AC-P281-9-14：终态账本
# ---------------------------------------------------------------------------

def test_ac_p281_9_14_ledger_final(green):
    problems, detail = judge.check_ledger(green)
    assert problems == [], problems
    assert detail["calls"] == 6
    assert set(detail["by_task"]) == {"1", "2", "3"}
    assert set(detail["by_stage"]) == {"coding_debug"}


def test_ac_p281_9_14_negative_gap_and_cap(green):
    path = green / "quota" / "ledger.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    rows[1]["seq"] = 5                       # 断号 + 重号
    path.write_text("".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n"
                            for r in rows), encoding="utf-8")
    problems, _ = judge.check_ledger(green)
    assert any("seq 断号" in p for p in problems), problems


def test_ac_p281_9_14_negative_over_hard_cap(green, monkeypatch):
    from orchestrator.benchmark.phase281 import constants

    monkeypatch.setattr(constants, "HARD_CAP", 3)
    problems, _ = judge.check_ledger(green)
    assert any("越过硬帽" in p for p in problems), problems


# ---------------------------------------------------------------------------
# AC-P281-9-15：四表未动
# ---------------------------------------------------------------------------

def test_ac_p281_9_15_tables_untouched(green):
    problems, detail = judge.check_tables(green)
    assert problems == [], problems
    assert detail["live"] == detail["frozen"] == FROZEN_TABLES


def test_ac_p281_9_15_negative_row_changed(green, monkeypatch):
    monkeypatch.setattr(judge, "_table_counts", lambda uri: {
        **FROZEN_TABLES, "solution_chunks": 94})     # 迟滞的旧数字
    problems, _ = judge.check_tables(green)
    assert any("solution_chunks 行数 94" in p for p in problems), problems


def test_ac_p281_9_15_frozen_values_are_not_literal_baseline(tmp_path, green):
    """断言的对象是冻结件，不是某段字面：改冻结件 ⇒ 期望值随之变。"""
    frozen = green / "intake" / "table_counts.json"
    doc = json.loads(frozen.read_text(encoding="utf-8"))
    doc["tables"]["kb_chunks"] = 9999
    frozen.write_text(json.dumps(doc, ensure_ascii=False, indent=1,
                                 sort_keys=True) + "\n", encoding="utf-8")
    problems, _ = judge.check_tables(green)
    assert any("kb_chunks 行数 1542 ≠ 冻结件 9999" in p for p in problems), problems


# ---------------------------------------------------------------------------
# AC-P281-9-16：五本旧账封存
# ---------------------------------------------------------------------------

def test_ac_p281_9_16_old_ledgers_sealed(green):
    problems, detail = judge.check_old_ledgers(green)
    assert problems == [], problems
    assert set(detail["ledgers"]) == {"phase1", "phase2"}


def test_ac_p281_9_16_negative_tampered(green):
    path = green / "old" / "phase2.jsonl"
    path.write_text('{"phase": "phase2", "seq": 2}\n', encoding="utf-8")
    problems, _ = judge.check_old_ledgers(green)
    assert any("phase2 的 sha256 变化" in p for p in problems), problems


# ---------------------------------------------------------------------------
# AC-P281-9-17：环境
# ---------------------------------------------------------------------------

def test_ac_p281_9_17_environment(green):
    problems, detail = judge.check_environment(green)
    assert problems == [], problems
    assert detail["containers"] == 0 and detail["gpu_mib"] <= 200


def test_ac_p281_9_17_negative_residue(green):
    path = green / "closeout" / "environment.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc.update({"containers": 2, "gpu_mib": 512, "compose_rc": 1})
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=1,
                               sort_keys=True) + "\n", encoding="utf-8")
    problems, _ = judge.check_environment(green)
    assert any("容器残留" in p for p in problems), problems
    assert any("显存 512" in p for p in problems), problems
    assert any("compose 探测 rc=1" in p for p in problems), problems


# ---------------------------------------------------------------------------
# 验收车道（不是 AC，但红会经 AC-9-12 兜住）
# ---------------------------------------------------------------------------

def test_acceptance_lane_must_actually_run(green):
    problems, _ = judge.check_acceptance(green)
    assert problems == []
    (green / "closeout" / "acceptance_lane.json").unlink()
    problems, _ = judge.check_acceptance(green)
    assert any("缺验收车道记录" in p for p in problems), problems


def test_acceptance_lane_zero_passed_is_red(green):
    closeout._write_json(green / "closeout" / "acceptance_lane.json", {
        "rc": 0, "passed": 0, "failed": 0, "skipped": 0, "deselected": 37})
    problems, _ = judge.check_acceptance(green)
    assert any("什么都没验" in p for p in problems), problems


# ---------------------------------------------------------------------------
# AC 索引覆盖
# ---------------------------------------------------------------------------

def test_ac_index_covers_all_seventeen(green):
    problems, detail = judge.check_ac_index(green)
    assert problems == [], problems
    assert detail["rows"] == 17
    assert set(judge.AC_IDS) == {f"AC-P281-9-{i:02d}" for i in range(1, 18)}


def test_ac_index_negative_missing_row(green):
    path = green / "closeout" / "acceptance_matrix.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["rows"] = [r for r in doc["rows"] if r["ac"] != "AC-P281-9-17"]
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=1,
                               sort_keys=True) + "\n", encoding="utf-8")
    problems, _ = judge.check_ac_index(green)
    assert any("缺 AC" in p for p in problems), problems


# ---------------------------------------------------------------------------
# 活产物层
# ---------------------------------------------------------------------------

@live
def test_live_closure_judge_is_green():
    root = Path("/mnt/artifacts/simpraxis/phase281")
    if not (root / judge.JUDGE_RELPATH).is_file():
        pytest.fail(f"缺判决件：{root / judge.JUDGE_RELPATH}")
    doc = judge.run_judge(root)
    assert doc["problems"] == [], doc["problems"]
    assert doc["verdict"] == "PASS"
    assert set(doc["gaps"]) <= judge.ALLOWED_GAPS
    problems, detail = judge.verify_fixed_point(root)
    assert problems == [], problems
    assert detail["deterministic"] is True


@live
def test_live_pack_and_signs():
    root = Path("/mnt/artifacts/simpraxis/phase281")
    problems, detail = judge.check_pack(root)
    assert problems == [], problems
    assert detail["secrets_hits"] == 0
    assert detail["n_files"] > 0
    problems, _ = judge.check_judgment_not_packed(root)
    assert problems == [], problems
    problems, _ = judge.check_signs(root)
    assert problems == [], problems
