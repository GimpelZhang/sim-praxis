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
import inspect
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
for extra in (str(REPO), str(REPO / "scripts" / "benchmark")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

import build_evidence_pack as packer  # noqa: E402
import check_closure as judge  # noqa: E402
import closeout  # noqa: E402
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
    # 评审 W5-08：只读解析，数据根缺席时 fail-fast 而不是被 mkdir 掩盖。
    return paths.bench_root(read_only=True)


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
# AC-T7-9 验收车道必须真开闸（评审修复批次自伤回归）
# --------------------------------------------------------------------------

def test_ac_p0_7_9_acceptance_lane_env_logic(tmp_path: Path):
    """两条验收车道各自带自己的开关，缺一即「空车道」。

    回归背景：5083b52 的重构把 ``SIMPRAXIS_MILESTONE_CLOSEOUT="1"`` 从
    里程碑车道环境里丢掉了，车道在未开闸环境里收集期 **全部 deselect**
    （实测 37 deselected / 0 passed / rc=5）——判据只看 rc/passed 时这不是
    「红」而是「什么都没验」。本用例把两个开关与两条采集目标钉成机判。
    """
    lane_env, container_env = closeout.acceptance_envs(tmp_path)
    assert lane_env["SIMPRAXIS_MILESTONE_CLOSEOUT"] == "1"
    assert "SIMPRAXIS_CONTAINER" not in lane_env          # 两开关不互相顶替
    assert container_env["SIMPRAXIS_CONTAINER"] == "1"
    assert "SIMPRAXIS_MILESTONE_CLOSEOUT" not in container_env
    # 两条车道都读同一个根（评审 W4-05）
    assert lane_env["SIMPRAXIS_BENCHMARK_ROOT"] == str(tmp_path.resolve())
    assert container_env["SIMPRAXIS_BENCHMARK_ROOT"] == str(tmp_path.resolve())
    # 采集目标必须与车道命令指向同一路径，否则「采集数」与「实跑数」脱钩
    assert closeout.MILESTONE_LANE_TARGET == \
        "tests/benchmark/test_task7_closeout.py"
    assert closeout.CONTAINER_LANE_TARGET == "tests/benchmark"
    # 判据侧下限：两条车道都不许 0 条（空绿）
    # 下限不是「≥1 即可」：1 条也能通过的话，「被削到只剩一条」与空绿只差一步。
    assert judge.MILESTONE_LANE_MIN >= 10
    assert judge.CONTAINER_LANE_MIN >= 2


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
def test_acc_p0_7_8_invariants_i1_i12_live(tmp_path: Path):
    """12 条不变量活车道必须逐个真跑。

    ``I11`` 是唯一带 ``tmp_path`` 形参的不变量；早先这里无参调用，一旦里程碑
    车道真的开闸就抛 ``TypeError: missing 1 required positional argument``。
    改为按签名注入（有 ``tmp_path`` 就给一个独立子目录），既修崩溃又不让
    将来新增带参不变量再次「调用即崩」。
    """
    module = sys.modules[__name__]
    for name in INVARIANTS:
        func = getattr(module, name)
        if "tmp_path" in inspect.signature(func).parameters:
            func(tmp_path / name)
        else:
            func()


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
    """I10：推送只经人工 STOP 门（评审 W5-01/W5-04 修法后的口径）。

    原文「origin/main 恒等于 ``judge.ORIGIN_MAIN``」在推送窗口之后必然恒红：
    推送本身是被允许的动作，恒红只会训练出「忽略这条」的习惯。改为与
    ``closeout.write_invariants`` 同一实现的二值 + 祖先谓词：
    ``origin/main ∈ {Task-0 基线, HEAD}`` 且 ``origin/main`` 是 HEAD 的祖先
    （后者排除「已推送历史被改写」）。
    """
    def _git(*argv: str) -> str:
        return subprocess.run(["git", "-C", str(REPO), *argv],
                              capture_output=True, text=True,
                              check=True).stdout.strip()

    origin = _git("rev-parse", "origin/main")
    head = _git("rev-parse", "HEAD")
    ancestor = subprocess.run(
        ["git", "-C", str(REPO), "merge-base", "--is-ancestor", origin, head],
        capture_output=True, text=True).returncode == 0
    assert judge.verify_origin_anchor(origin, head, ancestor=ancestor) == []
    # 基线不是 HEAD 时，origin/main 必须停在基线或某个**已记录**的推送头
    # （每一条都对应 Complete 里的一次人工 STOP 门放行）；是 HEAD 时则两者
    # 必须相等——三种合法态都被上面一条覆盖，这里再钉住「不得名单外取值」。
    assert origin in judge.AUTHORIZED_PUSH_HEADS + (judge.ORIGIN_MAIN, head), \
        origin


@MARK
def test_inv_i11_clean_target_guard(tmp_path: Path):
    holder = tmp_path / "p28_i11"
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
    # 评审 W5-20：对自造 dict 的「in 二元组」断言恒真、证明力为零；改为对
    # report 的真实结论常量的产生点断言（有问题的输入必须落到另一值）。
    assert report.VERDICTS == ("Established", "Not Established")
    assert report.verdict_for(()) == "Established"
    assert report.verdict_for(["x"]) == "Not Established"


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


# --------------------------------------------------------------------------
# 机判侧新增逻辑的护栏（评审 W4-01/04/08/09/10/12/15、W5-05/06/17）
# --------------------------------------------------------------------------

def _evidence_root(tmp_path: Path) -> Path:
    """可跑 ``check_evidence`` 的最小合成根（自洽相位 2.7 包，避免 I6 假红）。"""
    from orchestrator.benchmark import intake as bench_intake

    root = _fixture_root(tmp_path)
    phase27 = root / "phase27_pack"
    _write(phase27 / "a.txt", "phase27\n")
    _write(phase27 / "manifest.json", json.dumps(
        {"files": [{"path": "a.txt",
                    "sha256": _sha(phase27 / "a.txt"),
                    "size": (phase27 / "a.txt").stat().st_size}]}))
    digest = bench_intake.phase27_pack_sha256(phase27)
    _write(root / "intake" / "invariants_before.json", json.dumps({
        "phase27_pack": str(phase27),
        "phase27_pack_sha256": digest["phase27_pack_sha256"],
        "phase27_manifest_sha256": digest["phase27_manifest_sha256"],
        "phase27_pack_n_files": digest["phase27_pack_n_files"],
        "phase27_pack_total_bytes": digest["phase27_pack_total_bytes"]}))
    _build(root)
    return root


def _seal_pack(root: Path) -> Path:
    """把合成包伪装成「从干净树构建」：改写 git_dirty 并重算 pack.sha256。

    合成根不是 git 仓，打包器如实写 ``git_dirty=True``；本文件其余逻辑与此
    无关，故只在校验超集/越界两条规则时把它改成干净态。
    """
    pack = root / "evidence_pack"
    manifest = _json(pack / "manifest.json")
    manifest["git_dirty"] = False
    (pack / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
        encoding="utf-8")
    (pack / "pack.sha256").write_text(_sha(pack / "manifest.json") + "\n",
                                      encoding="utf-8")
    return pack


def test_judge_evidence_superset_and_relpath(tmp_path: Path):
    """W4-01/W4-09：包内多出的未登记件、绝对/上跳 relpath 都必须被抓。"""
    root = _evidence_root(tmp_path)
    _seal_pack(root)
    problems, _detail = judge.check_evidence(root)
    assert problems == [], problems

    # ① 超集：后置混入一件未登记文件 → 必须报「未登记」
    _write(root / "evidence_pack" / "intake" / "smuggled.txt", "x\n")
    problems, detail = judge.check_evidence(root)
    assert any("未登记" in p for p in problems), problems
    assert detail["n_unregistered"] == 1

    # ② relpath 越界：manifest 里写 `../` 与绝对路径都必须被拒
    (root / "evidence_pack" / "intake" / "smuggled.txt").unlink()
    manifest_path = root / "evidence_pack" / "manifest.json"
    manifest = _json(manifest_path)
    manifest["files"].append({"relpath": "../escape.txt", "sha256": "0" * 64,
                              "size": 1})
    manifest["files"].append({"relpath": "/abs.txt", "sha256": "0" * 64,
                              "size": 1})
    manifest["n_files"] = len(manifest["files"])
    manifest["total_bytes"] = sum(r["size"] for r in manifest["files"])
    manifest_path.write_text(json.dumps(manifest, sort_keys=True) + "\n",
                             encoding="utf-8")
    problems, _detail = judge.check_evidence(root)
    assert any("含 '..'" in p for p in problems), problems
    assert any("绝对路径" in p for p in problems), problems


def test_judge_secret_exemption_only_inside_selftest_param():
    """W4-08：规则①只豁免**自测 nodeid 方括号参数内**的命中。"""
    fake = "sk-" + "e" * 20
    same_line = f"tests/x.py::test_scan_detects_all_strict_forms[{fake}]"
    assert judge._in_selftest_param(same_line, fake) is True
    nested = "tests/x.py::test_scan_detects_all_strict_forms[" \
             f"('{fake}', 'ok')]"
    assert judge._in_selftest_param(nested, fake) is True
    # 关键收紧：**同一行**上、参数之外的另一个真凭据不再被顺带豁免
    other = "ghp_" + "z" * 24
    line = f'{other} tests/x.py::test_scan_detects_all_strict_forms[{fake}]'
    assert judge._in_selftest_param(line, fake) is True
    assert judge._in_selftest_param(line, other) is False
    # 没有自测 nodeid 的行一律不豁免
    assert judge._in_selftest_param(f"{fake} plain line", fake) is False


def test_judge_origin_anchor_two_valued():
    """W5-01/W4-15：origin/main ∈ {基线 ∪ 已记录推送头, HEAD} 且为 HEAD 祖先。"""
    base, other = "a" * 40, "b" * 40
    assert judge.verify_origin_anchor(base, base, base=base) == []
    assert judge.verify_origin_anchor(base, other, base=base) == []
    assert judge.verify_origin_anchor(other, other, base=base) == []
    problems = judge.verify_origin_anchor(other, base, base=base)
    assert any("不在" in p and "放行值" in p for p in problems), problems
    # 名单是封闭集合：把已记录推送头放进 allowed 才放行，随便给个 sha 仍然红
    pushed = judge.AUTHORIZED_PUSH_HEADS[0]
    assert judge.verify_origin_anchor(
        pushed, other, base=base, authorized=(pushed,)) == []
    assert judge.verify_origin_anchor(
        "c" * 40, other, base=base, authorized=(pushed,))
    assert any("改写" in p for p in
               judge.verify_origin_anchor(base, other, base=base,
                                          ancestor=False))
    assert any("40 hex" in p for p in
               judge.verify_origin_anchor("xyz", base))


def test_judge_verdict_requires_full_set(tmp_path: Path, monkeypatch):
    """W4-03：子集运行只能是 Incomplete，永不 Established。"""
    monkeypatch.setitem(judge.CHECK_FUNCS, "stub", lambda root: ([], {}))
    doc = judge.run_judge(tmp_path / "bench", checks=("stub",))
    assert doc["verdict"] == "Incomplete", doc["verdict"]
    assert doc["acceptance"] is None
    assert doc["checks_run"] == ["stub"]
    assert doc["checks_required"] == list(judge.CHECKS) + list(
        judge.ACCEPTANCE_CHECKS)


def test_judge_cold_run_rc_provenance(tmp_path: Path):
    """W4-10：rc 只认显式参数与侧车，末行文本一律不采信。"""
    root = tmp_path / "bench"
    (root / "closeout").mkdir(parents=True)
    log = root / "cold.raw.log"
    log.write_text("1 passed in 0.10s\n", encoding="utf-8")
    with pytest.raises(judge.JudgeError):
        judge._resolve_rc(root, None)
    (root / "closeout" / "cold_run.rc").write_text("rc=3\n", encoding="utf-8")
    assert judge._resolve_rc(root, None) == 3
    assert judge._resolve_rc(root, 0) == 0
    (root / "closeout" / "cold_run.rc").write_text("boom\n", encoding="utf-8")
    with pytest.raises(judge.JudgeError):
        judge._resolve_rc(root, None)


def test_judge_matrix_and_invariants_gate(tmp_path: Path):
    """W5-05/W5-06：矩阵与不变量都要自称 + 被机判复核，缺件即红。"""
    root = tmp_path / "bench"
    (root / "closeout").mkdir(parents=True)
    (root / "intake").mkdir(parents=True)
    _write(root / "intake" / "repository_state.json",
           json.dumps({"head": "0" * 40, "origin_main": "0" * 40}))
    problems, _ = judge.check_matrix(root)
    assert any("acceptance_matrix" in p for p in problems), problems
    problems, _ = judge.check_invariants(root)
    assert any("invariants" in p for p in problems), problems

    acs = [{"ac": f"P0-AC-{i:02d}", "result": "PASS", "evidence": "x"}
           for i in range(1, 20)]                 # 缺 P0-AC-20
    _write(root / "closeout" / "acceptance_matrix.json", json.dumps({
        "schema": "simpraxis.benchmark.acceptance_matrix.v1", "acs": acs,
        "summary": {"total": 19, "passed": 19, "failed": 0},
        "commit": "0" * 40, "pack_sha256": "0" * 64,
        "baseline_head": "0" * 40}))
    problems, _ = judge.check_matrix(root)
    assert any("AC 集不符" in p for p in problems), problems
    assert any("矩阵缺 P0-AC-20" in p for p in problems), problems

    _write(root / "closeout" / "invariants.json", json.dumps({
        "schema": "simpraxis.benchmark.invariants.v1",
        "verdict": "OK", "commit": "0" * 40, "baseline_head": "0" * 40,
        "invariants": [{"id": f"I{i}", "ok": i != 7, "evidence": "x"}
                       for i in range(1, 13)]}))
    problems, _ = judge.check_invariants(root)
    assert any("I7" in p for p in problems), problems


def test_judge_invariant_set_check_is_order_agnostic(tmp_path: Path):
    """集齐判必须与顺序无关（Phase 2.8.0 验证车道抓出的恒红）。

    旧写法 `sorted(rows) != list(INVARIANT_IDS)` 拿字典序（I1,I10,I11,I12,
    I2,…）比数值序（I1..I12），12 条全 ok 的完美件也会被点名「集不齐」，
    终判于是永不可能 Established；上一条测试只断言「I7 在 problems 里」，
    覆盖不到「完美件零问题」。这里正反两面都钉死：条数相等 + 集合相等，
    重复 id 也要红（旧 dict 构造会把重复静默吞掉）。
    """
    root = tmp_path / "bench"
    (root / "closeout").mkdir(parents=True)
    (root / "intake").mkdir(parents=True)
    _write(root / "intake" / "repository_state.json",
           json.dumps({"head": "0" * 40, "origin_main": "0" * 40}))

    def _doc(ids):
        return {"schema": "simpraxis.benchmark.invariants.v1", "verdict": "OK",
                "commit": "0" * 40, "baseline_head": "0" * 40,
                "invariants": [{"id": iid, "ok": True, "evidence": "x"}
                               for iid in ids]}

    ids = [f"I{i}" for i in range(1, 13)]
    _write(root / "closeout" / "invariants.json", json.dumps(_doc(ids)))
    problems, detail = judge.check_invariants(root)
    assert not any("集不齐" in p for p in problems), problems
    assert detail["n_rows"] == 12

    _write(root / "closeout" / "invariants.json", json.dumps(_doc(ids[:-1])))
    problems, _ = judge.check_invariants(root)
    assert any("集不齐" in p for p in problems), problems

    _write(root / "closeout" / "invariants.json",
           json.dumps(_doc(ids + ["I1"])))
    problems, _ = judge.check_invariants(root)
    assert any("集不齐" in p for p in problems), problems


def test_judge_acceptance_requires_container_lane(tmp_path: Path):
    """W5-17：验收车道缺 container_lane 段 / 采集条数不足都必须红。"""
    root = tmp_path / "bench"
    lane_log = _write(root / "closeout" / "acceptance_lane.log",
                      "$ pytest\n(rc=0)\n12 passed, 3 deselected in 1.00s\n")
    _write(root / "closeout" / "acceptance_lane.json", json.dumps({
        "schema": "simpraxis.benchmark.acceptance_lane.v1",
        "cmd": "pytest", "rc": 0, "passed": 12, "failed": 0,
        "deselected": 3, "summary": "12 passed",
        "log": str(lane_log), "log_sha256": _sha(lane_log)}))
    problems, _ = judge.check_acceptance(root)
    assert any("container_lane" in p for p in problems), problems

    container_log = _write(root / "closeout" / "container_lane.log",
                           "$ pytest\n(rc=0)\n2 passed, 30 deselected in 9.0s\n")
    doc = _json(root / "closeout" / "acceptance_lane.json")
    doc["container_lane"] = {
        "cmd": "pytest -m container", "rc": 0, "passed": 2, "failed": 0,
        "deselected": 30, "summary": "2 passed", "selected": 2,
        "log": str(container_log), "log_sha256": _sha(container_log)}
    _write(root / "closeout" / "acceptance_lane.json", json.dumps(doc))
    problems, detail = judge.check_acceptance(root)
    assert not any("container_lane" in p or "容器车道" in p for p in problems), \
        problems
    assert detail["容器车道.selected"] == 2

    doc["container_lane"]["selected"] = 0          # 标记没生效的空绿
    _write(root / "closeout" / "acceptance_lane.json", json.dumps(doc))
    problems, _ = judge.check_acceptance(root)
    assert any("下限" in p for p in problems), problems


def test_record_input_manifest_fail_closed(tmp_path: Path, monkeypatch):
    """W4-12：既有清单坏了必须抛错，不得静默重置为空（会丢别任务登记）。"""
    from orchestrator.benchmark import reference as ref

    monkeypatch.setenv("SIMPRAXIS_BENCHMARK_ROOT", str(tmp_path / "bench"))
    rows = [{"kind": "stage_root", "stage_root": "x", "sha256": "0" * 64}]
    first = ref.record_input_manifest("task_a", rows)
    assert "task_a" in first["tasks"]

    path = ref.input_manifest_path()
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ref.ReferenceError):
        ref.record_input_manifest("task_b", rows)
    assert path.read_text(encoding="utf-8") == "{not json"

    path.write_text('{"schema": "other"}', encoding="utf-8")
    with pytest.raises(ref.ReferenceError):
        ref.record_input_manifest("task_b", rows)
