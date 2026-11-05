"""Phase 2.8.0 Task 7 — 收尾/证据包机判（AC-T7-1..9 + 不变量 I1..I12）。

两段式（D7-7）：

- ``test_ac_p0_7_X_<logic>`` —— 默认两排除全套跑；合成 fixture / ``tmp_path``，
  **不读活证据**；
- ``test_acc_p0_7_X_live``   —— ``milestone_closeout`` marker，打包后的验收车道跑，
  只断言 ①冷跑 ②打包 ③报告 ④账/KB/不变量 已产出的件（三签与终判件不在其列）。

例外：AC-T7-4 / AC-T7-5 的 live 语义由 ``check_closure --require-acceptance``
（链路末端的终判）承担，本文件只留 logic 半，避免自指。
"""
from __future__ import annotations

import ast
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
for extra in (str(REPO), str(REPO / "scripts" / "benchmark")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

import build_evidence_pack as packer  # noqa: E402
import check_closure as judge  # noqa: E402
from orchestrator.agent_runner.runner import RunSpec  # noqa: E402
from orchestrator.agentic.config import AgenticConfig  # noqa: E402
from orchestrator.benchmark import intake as bench_intake  # noqa: E402
from orchestrator.benchmark import paths, report  # noqa: E402

MARK = pytest.mark.milestone_closeout
CORE_CHECKS = {"intake", "specs", "fingerprints", "birth", "evaluator",
               "controls", "evidence", "cold_run"}
#: 名称即计划 §1.3 表内所列（AC-T7-8：`test_inv_i1..test_inv_i12` 各一具名函数）
INVARIANTS = [
    "test_inv_i1_agentic_default",
    "test_inv_i2_agentic_config_defaults",
    "test_inv_i3_graph_untouched",
    "test_inv_i4_stage_models_unchanged",
    "test_inv_i5_kb_readonly",
    "test_inv_i6_history_evidence_unchanged",
    "test_inv_i7_entrypoint_ready_untouched",
    "test_inv_i8_phase27_scripts_untouched",
    "test_inv_i9_whitelist_only",
    "test_inv_i10_no_push",
    "test_inv_i11_clean_target_guard",
    "test_inv_i12_environment_closeout",
]
JUDGMENT_NAMES = ("closure_judge.json", "closure_judge.final.json",
                  "acceptance_matrix.json", "acceptance_matrix.r4.json")


def _sha(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _json(path: Path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _root() -> Path:
    return paths.bench_root()


def _write(path: Path, payload: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")
    return path


def _fixture_root(tmp_path: Path) -> Path:
    root = tmp_path / "bench"
    _write(root / "intake" / "baseline.json", '{"ok": true}\n')
    _write(root / "intake" / "notes.txt", "hello\n")
    _write(root / "reports" / "summary.json", '{"verdict": "Established"}\n')
    _write(root / "reports" / "signs" / "executor.json", '{"role": "x"}\n')
    for name in JUDGMENT_NAMES:
        _write(root / "reports" / name, '{"judged": true}\n')
    return root


def _build(root: Path, clean: bool = False) -> dict:
    return packer.build(root=root, pack=root / "evidence_pack", clean=clean,
                        bench_dirs=("intake", "reports"), repo_paths=(),
                        include_runs_root=False, repo=root)


def _entry_set(manifest: dict) -> "set[tuple[str, str]]":
    return {(row["relpath"], row["sha256"]) for row in manifest["files"]}


def _expected_entries(root: Path) -> "set[tuple[str, str]]":
    """按打包器同一逻辑根重算当前源树的 (relpath, sha256)（虚拟重建）。"""
    expected = set()
    for name in packer.BENCH_DIRS:
        base = root / name
        if not base.exists():
            continue
        for source in packer._iter_files(base, exclude={root / "evidence_pack"}):
            rel = source.relative_to(root).as_posix()
            if packer.is_judgment_relpath(rel):
                continue
            expected.add((rel, _sha(source)))
    for rel_base in packer.REPO_PATHS:
        base = REPO / rel_base
        if not base.exists():
            continue
        for source in packer._iter_files(base):
            rel = f"repo/{source.relative_to(REPO).as_posix()}"
            if packer.is_judgment_relpath(rel):
                continue
            expected.add((rel, _sha(source)))
    return expected


# --------------------------------------------------------------------------
# AC-T7-1 证据包 manifest
# --------------------------------------------------------------------------

def test_ac_p0_7_1_pack_manifest_logic(tmp_path: Path):
    root = _fixture_root(tmp_path)
    result = _build(root)
    manifest = _json(Path(result["manifest"]))
    assert manifest["schema"] == packer.SCHEMA
    assert manifest["n_files"] == len(manifest["files"])
    assert manifest["total_bytes"] == sum(r["size"] for r in manifest["files"])
    for row in manifest["files"]:
        path = root / "evidence_pack" / row["relpath"]
        assert _sha(path) == row["sha256"], row["relpath"]
        assert path.stat().st_size == row["size"]
        assert oct(path.stat().st_mode & 0o777) == "0o600", row["relpath"]
        assert row["size"] <= packer.MAX_FILE_BYTES
    pack = root / "evidence_pack"
    assert oct(pack.stat().st_mode & 0o777) == "0o700"
    assert not any(p.is_symlink() for p in pack.rglob("*"))


@MARK
def test_acc_p0_7_1_live():
    root = _root()
    manifest = _json(root / "evidence_pack" / "manifest.json")
    assert manifest["schema"] == "simpraxis.benchmark.evidence_pack.v1"
    assert manifest["git_commit"] == subprocess.run(
        ["git", "-C", str(REPO), "rev-parse", "HEAD"], capture_output=True,
        text=True, check=True).stdout.strip()
    assert manifest["n_files"] == len(manifest["files"])
    assert manifest["total_bytes"] == sum(r["size"] for r in manifest["files"])
    for row in manifest["files"]:
        path = root / "evidence_pack" / row["relpath"]
        assert path.is_file() and not path.is_symlink(), row["relpath"]
        assert _sha(path) == row["sha256"], row["relpath"]
        assert oct(path.stat().st_mode & 0o777) == "0o600", row["relpath"]
    assert _sha(root / "evidence_pack" / "manifest.json") == (
        root / "evidence_pack" / "pack.sha256").read_text().strip()


# --------------------------------------------------------------------------
# AC-T7-2 判决件与包自身不入包
# --------------------------------------------------------------------------

def test_ac_p0_7_2_judgments_excluded_logic(tmp_path: Path):
    root = _fixture_root(tmp_path)
    assert any(p.is_file() for p in (root / "reports" / "signs").iterdir())
    _build(root)
    manifest = _json(root / "evidence_pack" / "manifest.json")
    rels = [row["relpath"] for row in manifest["files"]]
    assert not [rel for rel in rels if packer.is_judgment_relpath(rel)]
    assert not [rel for rel in rels if rel.split("/")[0] == "evidence_pack"]
    assert "intake/baseline.json" in rels          # 正常件必须仍在
    assert "reports/summary.json" in rels


@MARK
def test_acc_p0_7_2_live():
    root = _root()
    manifest = _json(root / "evidence_pack" / "manifest.json")
    rels = [row["relpath"] for row in manifest["files"]]
    assert not [rel for rel in rels if judge.is_judgment_relpath(rel)]
    assert not [rel for rel in rels if rel.startswith("closeout/")]
    assert not [rel for rel in rels if rel.startswith("evidence_pack/")]


# --------------------------------------------------------------------------
# AC-T7-3 --clean 重建不动点
# --------------------------------------------------------------------------

def test_ac_p0_7_3_pack_fixpoint_logic(tmp_path: Path):
    root = _fixture_root(tmp_path)
    first = _entry_set(_json(Path(_build(root, clean=True)["manifest"])))
    second = _entry_set(_json(Path(_build(root, clean=True)["manifest"])))
    assert first and first == second
    # 惯例：上一版包被排除，重建不吞自己
    assert not [rel for rel, _digest in second if rel.startswith("evidence_pack")]


@MARK
def test_acc_p0_7_3_live():
    root = _root()
    manifest = _json(root / "evidence_pack" / "manifest.json")
    assert _entry_set(manifest) == _expected_entries(root)


# --------------------------------------------------------------------------
# AC-T7-4 八类检查（live 语义由终判承担 → 只留 logic）
# --------------------------------------------------------------------------

def test_ac_p0_7_4_closure_judge_logic(tmp_path: Path):
    assert CORE_CHECKS <= set(judge.CHECKS)
    baseline = {"passed": 10, "skipped": 1, "deselected": 2}
    nodeids = ["tests/benchmark/test_a.py::test_x"]
    good = {
        "rc": 0, "passed": 11, "skipped": 1, "deselected": 4,
        "new_collected": {"total": 1, "container_deselected": 1,
                          "milestone_deselected": 1,
                          "nodeids": nodeids,
                          "nodeids_sha256": hashlib.sha256(
                              "\n".join(nodeids).encode()).hexdigest()},
        "delta_ok": True,
    }
    log = "11 passed, 1 skipped, 4 deselected, 7 warnings in 9.5s\n"
    assert judge.verify_cold_run(good, log, baseline) == []
    bad = json.loads(json.dumps(good))
    bad["passed"] = 12                      # 放水 / 数字对不上
    assert judge.verify_cold_run(bad, log, baseline)
    bad2 = json.loads(json.dumps(good))
    bad2["skipped"] = 2                    # 新增测试用 skip
    assert judge.verify_cold_run(bad2, log, baseline)
    bad3 = json.loads(json.dumps(good))
    bad3["new_collected"]["nodeids"] = ["tests/other/test_y.py::test_y"]
    assert judge.verify_cold_run(bad3, log, baseline)


# --------------------------------------------------------------------------
# AC-T7-5 三签（live 语义由终判承担 → 只留 logic）
# --------------------------------------------------------------------------

def _sign(role: str, **over) -> dict:
    doc = {"schema": "simpraxis.benchmark.sign.v1", "role": role,
           "verdict": "APPROVE", "commit": "a" * 40, "pack_sha256": "b" * 64,
           "ts_iso": "2026-11-05T12:00:00Z", "reviewer_notes": "逐条复核"}
    doc.update(over)
    return doc


def test_ac_p0_7_5_three_signs_logic():
    pack_sha = "b" * 64
    signs = [_sign(role) for role in judge.ROLES]
    assert judge.validate_signs(signs, pack_sha) == []
    assert judge.validate_signs(signs[:2], pack_sha)          # 缺角色
    assert judge.validate_signs([_sign("executor"),
                                 _sign("reviewer", commit="c" * 40),
                                 _sign("verifier")], pack_sha)  # commit 不一致
    assert judge.validate_signs([_sign("executor"), _sign("reviewer"),
                                 _sign("verifier", verdict="REJECT")],
                                pack_sha)
    assert judge.validate_signs(signs, "d" * 64)              # 与 pack 不符


# --------------------------------------------------------------------------
# AC-T7-6 五本账冻结
# --------------------------------------------------------------------------

def _freeze_doc(equal: bool = True) -> dict:
    return {"schema": "simpraxis.benchmark.ledger_freeze.v1",
            "sealed_phases": ["phase1", "phase2", "phase25", "phase26"],
            "active_phase": "phase27",
            "ledgers": [{"phase": phase, "sha256_before": "e" * 64,
                         "sha256_after": "e" * 64 if equal else "f" * 64,
                         "equal": equal}
                        for phase in ("phase1", "phase2", "phase25",
                                      "phase26", "phase27")],
            "verdict": "FROZEN" if equal else "DRIFT"}


def test_ac_p0_7_6_ledgers_unchanged_logic():
    assert judge.validate_ledger_freeze(_freeze_doc()) == []
    assert judge.validate_ledger_freeze(_freeze_doc(equal=False))
    broken = _freeze_doc()
    broken["active_phase"] = "phase26"
    assert judge.validate_ledger_freeze(broken)


@MARK
def test_acc_p0_7_6_live():
    root = _root()
    problems, detail = judge.check_ledgers(root)
    assert problems == [], problems
    freeze = _json(root / "closeout" / "ledger_freeze.json")
    assert freeze["verdict"] == "FROZEN"
    for row in freeze["ledgers"]:
        assert row["sha256_before"] == row["sha256_after"] == _sha(
            Path(row["path"])), row["phase"]
    assert detail["phases"] == {phase: True for phase in
                                ("phase1", "phase2", "phase25", "phase26",
                                 "phase27")}


# --------------------------------------------------------------------------
# AC-T7-7 KB 完整性
# --------------------------------------------------------------------------

def _kb_doc(ok: bool = True) -> dict:
    return {"schema": "simpraxis.benchmark.kb_integrity.v1",
            "head_before": "1" * 40, "head_after": "1" * 40 if ok else "2" * 40,
            "porcelain_before": "", "porcelain_after": "",
            "tables": {"kb_chunks": [1542, 1542], "phase1_learnings": [44, 44],
                       "phase2_learnings": [1, 1],
                       "solution_chunks": [106, 106]},
            "verdict": "UNCHANGED" if ok else "CHANGED"}


def test_ac_p0_7_7_kb_integrity_logic():
    assert judge.validate_kb_integrity(_kb_doc()) == []
    assert judge.validate_kb_integrity(_kb_doc(ok=False))
    dirty = _kb_doc()
    dirty["porcelain_after"] = " M knowledge/x.md"
    assert judge.validate_kb_integrity(dirty)


@MARK
def test_acc_p0_7_7_live():
    problems, detail = judge.check_kb(_root())
    assert problems == [], problems
    baseline = _json(_root() / "intake" / "table_counts.json")["tables"]
    assert detail["tables"]["after"] == baseline


# --------------------------------------------------------------------------
# AC-T7-8 不变量 I1..I12
# --------------------------------------------------------------------------

def test_ac_p0_7_8_invariants_i1_i12_logic():
    source = Path(__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    defined = {node.name for node in tree.body
               if isinstance(node, ast.FunctionDef)}
    missing = [name for name in INVARIANTS if name not in defined]
    assert not missing, f"缺不变量函数：{missing}"
    for name in INVARIANTS:
        node = next(n for n in tree.body
                    if isinstance(n, ast.FunctionDef) and n.name == name)
        decorators = [ast.unparse(d) for d in node.decorator_list]
        joined = " ".join(decorators)
        assert "milestone_closeout" in joined or "MARK" in decorators, (
            f"{name} 必须打 milestone_closeout marker（活证据不在默认半）")
    assert len(INVARIANTS) == 12


@MARK
def test_acc_p0_7_8_invariants_i1_i12_live():
    module = sys.modules[__name__]
    for name in INVARIANTS:
        getattr(module, name)()


@MARK
def test_inv_i1_agentic_default():
    assert RunSpec.__dataclass_fields__["agentic"].default is False


@MARK
def test_inv_i2_agentic_config_defaults():
    config = AgenticConfig()
    booleans = {"verify_repair_tail", "run_expr", "tolerant_patch",
                "scratchpad", "env_spec", "render_preview",
                "privileged_state_inject", "visual_critic"}
    for name in booleans:
        assert getattr(config, name) is False, name
    assert config.run_mode == "R"
    assert config.elastic_round_cap == 0


def _diff(pathspec: str) -> str:
    base = _json(_root() / "intake" / "repository_state.json")["head"]
    return subprocess.run(
        ["git", "-C", str(REPO), "diff", "--name-status", f"{base}..HEAD",
         "--", pathspec], capture_output=True, text=True,
        check=True).stdout.strip()


@MARK
def test_inv_i3_graph_untouched():
    assert _diff("orchestrator/graph/") == ""


@MARK
def test_inv_i4_stage_models_unchanged():
    before = _json(_root() / "intake" / "invariants_before.json")
    assert _sha(Path(before["stage_models_yaml"])) == \
        before["stage_models_sha256"]
    live = _json(_root() / "closeout" / "invariants.json")
    row = next(r for r in live["invariants"] if r["id"] == "I4")
    assert row["ok"] is True


@MARK
def test_inv_i5_kb_readonly():
    problems, _detail = judge.check_kb(_root())
    assert problems == [], problems


@MARK
def test_inv_i6_history_evidence_unchanged():
    root = _root()
    freeze = _json(root / "closeout" / "ledger_freeze.json")
    assert all(row["equal"] for row in freeze["ledgers"])
    before = _json(root / "intake" / "invariants_before.json")
    manifest = Path(before["phase27_pack"]) / "manifest.json"
    # phase27_pack_sha256 是「file 清单规范化摘要」，manifest 文件字节 sha 是
    # 另一个字段（phase27_manifest_sha256）；两者不可互比。
    digest = bench_intake.phase27_pack_sha256(Path(before["phase27_pack"]))
    assert digest["phase27_pack_sha256"] == before["phase27_pack_sha256"]
    assert digest["phase27_manifest_sha256"] == before["phase27_manifest_sha256"]
    assert int(digest["phase27_pack_n_files"]) == int(
        before["phase27_pack_n_files"])
    assert _sha(manifest) == before["phase27_manifest_sha256"]


@MARK
def test_inv_i7_entrypoint_ready_untouched():
    assert _diff("orchestrator/agentic/loop.py") == ""


@MARK
def test_inv_i8_phase27_scripts_untouched():
    assert _diff("scripts/phase27/") == ""
    porcelain = subprocess.run(
        ["git", "-C", str(REPO), "status", "--porcelain", "--",
         "scripts/phase27/"], capture_output=True, text=True,
        check=True).stdout.strip()
    assert porcelain == ""


@MARK
def test_inv_i9_whitelist_only():
    base = _json(_root() / "intake" / "repository_state.json")["head"]
    out = subprocess.run(
        ["git", "-C", str(REPO), "diff", "--name-status", f"{base}..HEAD",
         "--", ".", ":!benchmarks", ":!orchestrator/benchmark",
         ":!scripts/benchmark", ":!tests/benchmark",
         ":!tests/fixtures/benchmark", ":!CLAUDE.md"],
        capture_output=True, text=True, check=True).stdout.strip()
    assert out == "", out


@MARK
def test_inv_i10_no_push():
    head = subprocess.run(["git", "-C", str(REPO), "rev-parse", "origin/main"],
                          capture_output=True, text=True, check=True
                          ).stdout.strip()
    assert head == judge.ORIGIN_MAIN


@MARK
def test_inv_i11_clean_target_guard():
    holder = Path(tempfile.mkdtemp(prefix="p28_i11_"))
    root = holder / "bench"
    pack = root / "evidence_pack"
    pack.mkdir(parents=True)
    packer._check_clean_target(pack, root)            # 正例
    for bad, why in ((root, "数据根本身"),
                     (root / "sibling" / "evidence_pack", "兄弟目录"),
                     (Path("/"), "根目录"),
                     (root / ".." / "evidence_pack", "上跳"),
                     (Path("/tmp/../") / "evidence_pack", "绝对上跳")):
        with pytest.raises(packer.PackError):
            packer._check_clean_target(bad, root)
    escape = holder / "escape"
    escape.mkdir()
    link = root / "evidence_pack_link"
    link.symlink_to(escape)
    with pytest.raises(packer.PackError):
        packer._check_clean_target(link, root)


@MARK
def test_inv_i12_environment_closeout():
    root = _root()
    text = (root / "closeout" / "environment_closeout.txt").read_text(
        encoding="utf-8")
    assert "docker ps -a" in text and "nvidia-smi" in text
    live = _json(root / "closeout" / "invariants.json")
    row = next(r for r in live["invariants"] if r["id"] == "I12")
    assert row["ok"] is True


# --------------------------------------------------------------------------
# AC-T7-9 结论二值
# --------------------------------------------------------------------------

def test_ac_p0_7_9_verdict_binary_logic(tmp_path: Path):
    assert report.assert_report_text("结论：`Established`") == \
        "结论：`Established`"
    for phrase in report.FORBIDDEN_PHRASES:
        with pytest.raises(report.ReportError):
            report.assert_report_text(f"……{phrase}……")
    summary = {"verdict": "Established"}
    assert summary["verdict"] in ("Established", "Not Established")


@MARK
def test_acc_p0_7_9_live():
    root = _root()
    doc = _json(root / "reports" / "benchmark_summary.json")
    assert doc["verdict"] in ("Established", "Not Established")
    text = (root / "reports" / report.REPORT_NAME).read_text(encoding="utf-8")
    assert doc["verdict"] in text
    report.assert_report_text(text)


# --------------------------------------------------------------------------
# 逻辑半的附加：AC-T7-2 的包自身排除单测（族匹配与路径前缀）
# --------------------------------------------------------------------------

def test_ac_p0_7_2_pack_self_not_swallowed(tmp_path: Path):
    root = _fixture_root(tmp_path)
    _build(root, clean=True)
    _write(root / "evidence_pack" / "stray.txt", "previous pack\n")
    result = _build(root, clean=True)
    manifest = _json(Path(result["manifest"]))
    assert "evidence_pack/stray.txt" not in {r["relpath"]
                                              for r in manifest["files"]}
    assert not (root / "evidence_pack" / "stray.txt").exists()
