"""Phase 2.8.1 P281-0 基座机判（AC-P281-0-01..13、16；0 付费、0 联网）。

覆盖：

* AC-01：基线 HEAD / origin-main / 工作树三项与计划 F1–F3 相符；
* AC-02：`82a1427…` 进 `AUTHORIZED_PUSH_HEADS`，且 I10 在「HEAD 已前进」的
  合成输入下仍放行；
* AC-03/04：`sealed_ledger_paths()` 恰五本、精确集合；phase281 可写、其余必拒；
* AC-05：预算常量为**独立字面量**（不用 `sum()` 推）；
* AC-06：`QuotaLedger.phase281()` 在 tmp 根下落行 `seq=1` / 严格 `Z`；
* AC-07/08：phase281 根与五个前序根、引擎仓、KB 仓三条隔离纪律（五个反例）；
* AC-09：`intake/` 十件产物逐件在场（9 json + 1 md）、0600/0700、四键信封；
* AC-10：`--check` 在场时 rc=0，改一个数字后 rc=1 且差异清单指名该键；
* AC-11：Phase 2.8.0 终判链在当前代码上仍 `Established` 且**一次写都不发生**；
* AC-12：`models_state.json` 只记 host[:port]，全文无凭据字面；
* AC-13：`SIMPRAXIS_BENCHMARK_ROOT` 已设置时入口断言必抛；
* AC-16：账本 CLI 完整接线（`_namespace_for_cli(281)` / `choices` / 批准件落位
  / `status` 对开账 selfcheck 行不报问题）。

纪律：除 AC-09/10/11/12 必须读**生产** phase281 / phase0 数据根（它们是本 Task
的证据本体）外，一律 monkeypatch 到 tmp 根，绝不写 /mnt 活账；本文件全程 0 实呼。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:  # 允许 `python tests/...` 裸跑
    sys.path.insert(0, str(REPO_ROOT))

from orchestrator.common import runtime_paths as rp  # noqa: E402
from orchestrator.gateway import quota_ledger as ql  # noqa: E402

#: F1/F2：P281-0 的基线提交（2026-11-06 推送窗口后的 origin/main）
BASELINE_HEAD = "82a142710127367b0a771d818192321b53e2e951"
#: 既有放行头（2026-11-05 窗口），追加不得替换
PUSH_HEAD_2026_11_05 = "5895fe02e8cbda2a6962f8aa9a178268e7e5d991"
STRICT_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")
STAGE = "coding_debug"
BATCH = "P281-T-FIXTURE"

#: 本 Task 自身在 §16.1/§16.2/§16.3 里登记的路径——工作树脏检查的**唯一**豁免集。
#: 这不放水：集合是冻结字面量，且对照用例（`_dirty_outside_own`）证明任何集合外
#: 的改动仍红。提交之后这些路径全部干净，豁免集自动空转。
OWN_PATHS = (
    "orchestrator/benchmark/phase281/",              # §16.1（三件）
    "scripts/benchmark/phase281_freeze.py",          # §16.1
    "tests/benchmark/test_phase281_foundation.py",   # §16.1
    "tests/benchmark/test_phase281_legacy_flip.py",  # §16.1
    "orchestrator/common/runtime_paths.py",          # §16.2
    "orchestrator/gateway/quota_ledger.py",          # §16.2
    "orchestrator/agentic/wiring.py",                # §16.2
    "scripts/benchmark/check_closure.py",            # §16.2
    "tests/milestone25/test_task0_foundation.py",    # §16.3
    "tests/milestone25/test_task4_forensics_cache.py",
    "tests/milestone26/test_task0_foundation.py",
    "tests/milestone27/test_task0_foundation.py",
    "tests/milestone27/test_task3_elastic_loop.py",
    # 锁内适配（T0）：`tests/milestone27/test_task0_foundation.py::test_ac_t0_3_*`
    # 子进程跑本脚本的 `--selftest`，而它的端到端段会真写账——phase27 已封存，
    # 只能在脚本内部把该段改指 phase281（analyze 判据与期号无关）。计划 §16.2
    # 未登记此件，故额外列在这里并在 commit body 留痕。
    "scripts/phase27/check_ledger_phase27.py",
)


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(REPO_ROOT), *args],
                          text=True, capture_output=True, check=False)


def _dirty_outside_own(lines: "list[str]") -> "list[str]":
    """从 ``git status --porcelain`` 行里剔除本 Task 自有的路径。"""
    out: "list[str]" = []
    for line in lines:
        if not line.strip():
            continue
        path = line[3:].strip()
        if " -> " in path:                    # 重命名：取新路径
            path = path.split(" -> ", 1)[1]
        if path.startswith('"') and path.endswith('"'):
            path = path[1:-1]
        if any(path == own or path.startswith(own) for own in OWN_PATHS):
            continue
        out.append(line)
    return out


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


#: F2 的放行集合 = 基线 ∪ ``check_closure.AUTHORIZED_PUSH_HEADS``（精确集合，
#: 与 P281-0 修 F13 时同一份名单，不另立字面量）。
def _origin_allowlist() -> "tuple[str, ...]":
    sys.path.insert(0, str(REPO_ROOT / "scripts" / "benchmark"))
    import check_closure  # noqa: PLC0415

    return tuple(dict.fromkeys((BASELINE_HEAD,) + tuple(
        check_closure.AUTHORIZED_PUSH_HEADS)))


def _origin_problems(origin: str, *, allow: "tuple[str, ...] | None" = None,
                     base: str = BASELINE_HEAD) -> "list[str]":
    """F2 的判据本体（纯函数，便于负例对照）。

    ① 必须是 40 hex；② 必须落在放行集合里；③ 基线必须是它的祖先（只许前进）。
    """
    allow = _origin_allowlist() if allow is None else tuple(allow)
    problems: "list[str]" = []
    if len(origin) != 40 or any(c not in "0123456789abcdef" for c in origin):
        problems.append(f"origin/main 非 40 hex：{origin!r}")
    if origin not in allow:
        problems.append(f"origin/main {origin} 不在放行集合（基线∪已登记推送头）里")
    if _git("merge-base", "--is-ancestor", base, origin).returncode != 0:
        problems.append(f"基线 {base} 不是 origin/main {origin} 的祖先（历史被改写？）")
    return problems


@pytest.fixture()
def p281_roots(tmp_path, monkeypatch):
    """六根全部指 tmp（conftest 只接管 phase2；这里补全其余五根）。"""
    for env, name in (("SIMPRAXIS_PHASE1_ROOT", "phase1"),
                      ("SIMPRAXIS_PHASE2_ROOT", "phase2"),
                      ("SIMPRAXIS_PHASE25_ROOT", "phase25"),
                      ("SIMPRAXIS_PHASE26_ROOT", "phase26"),
                      ("SIMPRAXIS_PHASE27_ROOT", "phase27"),
                      ("SIMPRAXIS_PHASE281_ROOT", "phase281"),
                      ("SIMPRAXIS_GATEWAY_STATE", "gwstate")):
        monkeypatch.setenv(env, str(tmp_path / name))
    monkeypatch.delenv("SIMPRAXIS_BENCHMARK_ROOT", raising=False)
    return tmp_path


# ---------------------------------------------------------------------------
# AC-P281-0-01：基线三项
# ---------------------------------------------------------------------------

def test_ac_p281_0_01_repo_matches_baseline():
    # F2：``origin/main`` 只许停在「基线」或**经人工 STOP 门记录在案**的推送头
    # 上，且基线必须是它的祖先（只许前进，不许改写已推历史）。
    #
    # *锁内适配*（2026-11-07 推送窗口）：F2 的原文是「本阶段不推送，故它不许
    # 移动」。用户在 P281-7 复核四项裁决里逐字指示「四：推」，T7 评审修复批
    # 16 提交（``82a1427..065231b``）已随人工 STOP 门推送到 origin/main——
    # 前提被**有据地**打破，而不是漂移。放行机制与 P281-0 自己修 F13 时用的
    # 是同一份精确集合（``check_closure.AUTHORIZED_PUSH_HEADS``，计划 §16.2
    # 允许的追加项），集合外的 sha 仍判红（见下方负例）。
    origin = _git("rev-parse", "origin/main").stdout.strip()
    assert _origin_problems(origin) == [], _origin_problems(origin)
    # 负例（不放水对照）：集合外的 sha 与「往回退到基线之前」都必须判红。
    assert _origin_problems("0" * 40) != []
    assert _origin_problems("4b5d134a447b2069b050887d63f3ddd4e687f123") != []

    head = _git("rev-parse", "HEAD").stdout.strip()
    # F1：HEAD 是基线本身（提交前）或它的后代（提交后）——已推历史永不改写。
    assert _git("merge-base", "--is-ancestor", BASELINE_HEAD, "HEAD").returncode == 0, \
        f"HEAD={head} 不是基线 {BASELINE_HEAD} 的后代（历史被改写？）"

    # F3：工作树干净——除本 Task 在 §16.1/16.2/16.3 登记的路径外，一个都不许脏。
    status = _git("status", "--porcelain").stdout.splitlines()
    rogue = _dirty_outside_own(status)
    assert rogue == [], f"本 Task 之外的未提交内容：\n" + "\n".join(rogue)

    # 不放水对照：豁免判定只对 OWN_PATHS 生效，任意外来件仍红。
    assert _dirty_outside_own([" M docs/rogue.md", "?? newfile.txt"]) == [
        " M docs/rogue.md", "?? newfile.txt"]
    assert _dirty_outside_own([" M orchestrator/common/runtime_paths.py"]) == []


# ---------------------------------------------------------------------------
# AC-P281-0-02：I10 放行头登记
# ---------------------------------------------------------------------------

def test_ac_p281_0_02_push_anchor_registered():
    sys.path.insert(0, str(REPO_ROOT / "scripts" / "benchmark"))
    import check_closure  # noqa: PLC0415

    heads = check_closure.AUTHORIZED_PUSH_HEADS
    assert BASELINE_HEAD in heads, "82a1427 未登记进 AUTHORIZED_PUSH_HEADS（F13）"
    assert PUSH_HEAD_2026_11_05 in heads, "旧放行头被替换而非追加"
    assert len(tuple(dict.fromkeys(heads))) == len(heads), "放行头名单有重复项"

    # 假设 HEAD 已前进一个提交：origin/main 停在 82a1427 时必须仍放行。
    synthetic_head = "deadbeef" * 5
    assert check_closure.verify_origin_anchor(
        BASELINE_HEAD, synthetic_head, ancestor=True) == []

    # 负例：名单外的第三方 sha 仍判红（名单是精确集合，不是「>= 基线」放水）。
    assert check_closure.verify_origin_anchor(
        "0" * 40, synthetic_head) != []


# ---------------------------------------------------------------------------
# AC-P281-0-03 / 04：封存集 4→5 与唯一可写账
# ---------------------------------------------------------------------------

def test_ac_p281_0_03_sealed_set_has_five(p281_roots):
    sealed = rp.sealed_ledger_paths()
    expected = frozenset({
        rp.phase1_ledger_path().resolve(strict=False),
        rp.phase2_ledger_path().resolve(strict=False),
        rp.phase25_ledger_path().resolve(strict=False),
        rp.phase26_ledger_path().resolve(strict=False),
        rp.phase27_ledger_path().resolve(strict=False),
    })
    assert len(sealed) == 5, f"封存集长度应为 5，实得 {len(sealed)}"
    assert sealed == expected, "封存集不是精确的五本账路径（判据是 ==，不是 ⊆）"


def test_ac_p281_0_04_phase281_writable_others_raise(p281_roots):
    sealed = rp.sealed_ledger_paths()
    writable = rp.phase281_ledger_path().resolve(strict=False)
    assert writable not in sealed, "唯一可写账（phase281）不得在封存集内"
    for path in sorted(sealed):
        with pytest.raises(rp.SealedLedgerError):
            rp.open_ledger_append(path)
    # 缺省目标仍是 phase26（D0-3 不改缺省）：因 phase26 已封存，缺省调用必抛。
    with pytest.raises(rp.SealedLedgerError):
        rp.open_ledger_append()
    # phase281 可写，且 0 字节不写即关也不报错（负例表最后一条）。
    with rp.open_ledger_append(rp.phase281_ledger_path()):
        pass
    ledger = rp.phase281_ledger_path()
    assert ledger.exists() and ledger.stat().st_size == 0
    assert _mode(ledger) == 0o600


# ---------------------------------------------------------------------------
# AC-P281-0-05：预算常量为独立字面量
# ---------------------------------------------------------------------------

def test_ac_p281_0_05_budget_constants():
    from orchestrator.benchmark.phase281 import constants  # noqa: PLC0415

    assert dict(ql.PHASE281_TASK_CAPS) == {1: 60, 2: 60, 3: 60}
    assert ql.PHASE281_HARD_CAP == 180
    assert ql.PHASE281_NORMAL_BUDGET == 180
    assert ql.LEDGER_PHASE281 == 281
    # 独立字面量：三者相等是**事实**，不构成实现方式；常量模块里的同源值也须一致。
    assert sum(ql.PHASE281_TASK_CAPS.values()) == 180
    assert constants.HARD_CAP == 180
    assert constants.TASK_CAP == 60
    assert constants.PILOT_BUDGET == 60 and constants.CONFIRMATORY_BUDGET == 120
    assert constants.REPS_TOTAL == 3
    # 命名空间本身挂的就是这套常量（不是借用旧期的）。
    ns = ql.QuotaLedger.phase281().ns
    assert ns.phase == 281
    assert ns.hard_cap == 180 and ns.normal_budget == 180
    assert dict(ns.task_caps) == {1: 60, 2: 60, 3: 60}


# ---------------------------------------------------------------------------
# AC-P281-0-06：phase281 落行
# ---------------------------------------------------------------------------

def test_ac_p281_0_06_phase281_record_lands(p281_roots):
    prod = Path(rp.DEFAULT_DATA_ROOT) / "phase281" / "quota" / "ledger.jsonl"
    before = (prod.stat().st_size if prod.exists() else 0)
    led = ql.QuotaLedger.phase281()
    row = led.record(STAGE, task=1, batch_id=BATCH, ok=True,
                     model="fixture-model", thread_id="t-p281-1")
    assert row["seq"] == 1 and row["event"] == "call"
    assert STRICT_Z.match(row["ts_iso"]), row["ts_iso"]
    from orchestrator.common.clock import parse_ts_iso_strict  # noqa: PLC0415
    parse_ts_iso_strict(row["ts_iso"])
    tmp_ledger = p281_roots / "phase281" / "quota" / "ledger.jsonl"
    assert tmp_ledger.exists(), "账未落 tmp 根"
    rows = [json.loads(x) for x in tmp_ledger.read_text(encoding="utf-8").splitlines()
            if x.strip()]
    assert [r["seq"] for r in rows] == [1]
    # 生产根一个字节都没多（本用例全程 tmp）。
    after = (prod.stat().st_size if prod.exists() else 0)
    assert after == before, f"生产账被写：{prod} {before}B → {after}B"


# ---------------------------------------------------------------------------
# AC-P281-0-07 / 08：三条隔离纪律
# ---------------------------------------------------------------------------

def test_ac_p281_0_07_roots_isolated(tmp_path, monkeypatch):
    for env, name in (("SIMPRAXIS_PHASE1_ROOT", "phase1"),
                      ("SIMPRAXIS_PHASE2_ROOT", "phase2"),
                      ("SIMPRAXIS_PHASE25_ROOT", "phase25"),
                      ("SIMPRAXIS_PHASE26_ROOT", "phase26"),
                      ("SIMPRAXIS_PHASE27_ROOT", "phase27")):
        monkeypatch.setenv(env, str(tmp_path / name))
    monkeypatch.delenv("SIMPRAXIS_BENCHMARK_ROOT", raising=False)

    # 正例：兄弟目录互异 → 不抛。
    monkeypatch.setenv("SIMPRAXIS_PHASE281_ROOT", str(tmp_path / "phase281"))
    rp.assert_phase281_roots_isolated()

    # 五个反例：同体 ×3、嵌套 ×2，全部必须 fail-fast。
    bad = {
        "同体 phase1": tmp_path / "phase1",
        "同体 phase25": tmp_path / "phase25",
        "同体 phase27": tmp_path / "phase27",
        "嵌套于 phase1": tmp_path / "phase1" / "phase281",
        "嵌套于 phase2": tmp_path / "phase2" / "nested" / "phase281",
    }
    for label, target in bad.items():
        monkeypatch.setenv("SIMPRAXIS_PHASE281_ROOT", str(target))
        with pytest.raises(rp.PhaseRootAliasedError):
            rp.assert_phase281_roots_isolated()


def test_ac_p281_0_08_repo_kb_not_nested(tmp_path, monkeypatch):
    from orchestrator.benchmark import intake  # noqa: PLC0415
    from orchestrator.benchmark.phase281 import paths as p281  # noqa: PLC0415

    for env, name in (("SIMPRAXIS_PHASE1_ROOT", "phase1"),
                      ("SIMPRAXIS_PHASE2_ROOT", "phase2"),
                      ("SIMPRAXIS_PHASE25_ROOT", "phase25"),
                      ("SIMPRAXIS_PHASE26_ROOT", "phase26"),
                      ("SIMPRAXIS_PHASE27_ROOT", "phase27")):
        monkeypatch.setenv(env, str(tmp_path / name))
    monkeypatch.delenv("SIMPRAXIS_BENCHMARK_ROOT", raising=False)

    for label, target in (
            ("引擎仓内", REPO_ROOT / "_p281_probe"),
            ("KB 仓内", intake.kb_root() / "_p281_probe"),
            ("与引擎仓同体", REPO_ROOT),
            ("与 KB 仓同体", intake.kb_root())):
        monkeypatch.setenv("SIMPRAXIS_PHASE281_ROOT", str(target))
        with pytest.raises(p281.Phase281PathError):
            p281.assert_isolated()

    # 正例：tmp 兄弟目录过三条纪律。
    monkeypatch.setenv("SIMPRAXIS_PHASE281_ROOT", str(tmp_path / "phase281"))
    assert p281.assert_isolated() == (tmp_path / "phase281").resolve(strict=False)


# ---------------------------------------------------------------------------
# AC-P281-0-09：十件产物在场
# ---------------------------------------------------------------------------

ARTIFACT_JSON = (
    "repository_state.json",
    "ledger_state.json",
    "phase281_ledger_seed.json",
    "kb_state.json",
    "models_state.json",
    "criteria_lock.json",
    "environment_baseline.json",
    "test_baseline.json",
    "table_counts.json",
)
ARTIFACT_NAMES = ARTIFACT_JSON + ("intake_report.md",)


def _prod_root() -> Path:
    """生产 phase281 根的导入期快照语义：本文件不 monkeypatch 时才用它。"""
    root = rp.phase281_root()
    assert root.parent == Path(rp.DEFAULT_DATA_ROOT), (
        f"生产根不在 {rp.DEFAULT_DATA_ROOT} 下：{root}（环境被改写？）")
    return root


def test_ac_p281_0_09_intake_artifacts():
    root = _prod_root()
    intake = root / "intake"
    assert intake.is_dir(), f"intake/ 不在场：{intake}（先跑 --emit）"
    assert _mode(intake) == 0o700, f"intake/ 必须 0700，实际 {_mode(intake):o}"
    present = sorted(p.name for p in intake.iterdir() if p.is_file())
    assert present == sorted(ARTIFACT_NAMES), (
        f"十件产物集合不符：{present}")
    for name in ARTIFACT_JSON:
        path = intake / name
        assert _mode(path) == 0o600, f"{name} 必须 0600，实际 {_mode(path):o}"
        doc = json.loads(path.read_text(encoding="utf-8"))
        for key in ("generated_by", "git_head", "ts_iso", "commands"):
            assert key in doc, f"{name} 缺信封键 {key}"
        assert STRICT_Z.match(doc["ts_iso"]), f"{name} 的 ts_iso 非严格 Z"
    report = (intake / "intake_report.md").read_text(encoding="utf-8")
    assert _mode(intake / "intake_report.md") == 0o600
    for name in ARTIFACT_NAMES:
        if name.endswith(".json"):
            assert name.split(".")[0] in report or name in report, \
                f"报告未提及 {name}"


# ---------------------------------------------------------------------------
# AC-P281-0-10：--check 的核对语义
# ---------------------------------------------------------------------------

def test_ac_p281_0_10_freeze_check_detects_drift(tmp_path, monkeypatch, capsys):
    sys.path.insert(0, str(REPO_ROOT / "scripts" / "benchmark"))
    import phase281_freeze as freeze  # noqa: PLC0415

    prod_intake = _prod_root() / "intake"
    assert prod_intake.is_dir(), "生产 intake/ 不在场（先跑 --emit）"
    # 只有 phase281 根改指 tmp：`collect()` 现算的其余九件读的是**真实**的
    # 五本封存账 / KB / 活表 / stage_models——把前序根也改指空 tmp 会让
    # `sealed_ledger_sha256()` 直接 FileNotFoundError，比对也就无从谈起。
    # 这些读取全是只读（`read_bytes` + 0600 日志落 tmp 的 log_dir）。
    # 同理必须**撤销 conftest 的 hermetic phase2 根**：`tests/conftest.py` 的
    # autouse 夹具把 ``SIMPRAXIS_PHASE2_ROOT`` 指到 ``tmp_path/_phase2_hermetic``，
    # 那条路径下没有账本，`intake.sealed_ledgers()` 会 FileNotFoundError。
    # delenv 是按缺省回退语义生效（夹具自己也这么写），回退目标是真实账根。
    monkeypatch.delenv("SIMPRAXIS_BENCHMARK_ROOT", raising=False)
    monkeypatch.delenv("SIMPRAXIS_PHASE2_ROOT", raising=False)

    tmp_root = tmp_path / "phase281"
    monkeypatch.setenv("SIMPRAXIS_PHASE281_ROOT", str(tmp_root))
    shutil.copytree(prod_intake, tmp_root / "intake")

    # (1) 产物原样在场 → rc=0（真实 CLI 路径：env 解析 + 隔离断言 + collect）。
    rc = freeze.main(["--root", str(tmp_root), "--check"])
    out = capsys.readouterr().out
    if rc != 0:
        bad = [d for d in json.loads(out) if d.get("equal") is False]
        pytest.fail(f"--check 首跑应 rc=0，实得 {rc}；差异：{bad[:10]}")
    diffs = json.loads(out)
    assert all(d["equal"] is not False for d in diffs)

    # (2) 人为改一个数字 → rc=1 且差异清单**指名该键**。
    target = tmp_root / "intake" / "ledger_state.json"
    doc = json.loads(target.read_text(encoding="utf-8"))
    doc["ledgers"]["phase1"]["rows"] = int(doc["ledgers"]["phase1"]["rows"]) + 1
    target.write_text(json.dumps(doc, ensure_ascii=False, indent=2,
                                 sort_keys=True) + "\n", encoding="utf-8")

    log_dir = Path(tempfile.mkdtemp(prefix="p281_check_"))
    try:
        artifacts = freeze.collect(tmp_root, test_log=None, log_dir=log_dir)
    finally:
        shutil.rmtree(log_dir, ignore_errors=True)
    rc2, diffs2 = freeze.check(tmp_root, artifacts)
    assert rc2 == 1, "改了一个数字却没判红"
    named = [d for d in diffs2 if d.get("equal") is False]
    assert named, "差异清单为空"
    assert any("ledger_state.json" in d["key"]
               and "phase1" in d["key"] and "rows" in d["key"]
               for d in named), f"差异清单未指名被改的键：{named}"

    # (3) --check 不许写盘：tmp 根下除了我们复制进来的 intake/ 之外无新增件。
    extra = sorted(p.name for p in tmp_root.iterdir() if p.name != "intake")
    assert extra == [], f"--check 写了持久文件：{extra}"


# ---------------------------------------------------------------------------
# AC-P281-0-11：Phase 2.8.0 终判链不受影响（且一次写都不发生）
# ---------------------------------------------------------------------------

def test_ac_p281_0_11_p280_judge_unaffected():
    sys.path.insert(0, str(REPO_ROOT / "scripts" / "benchmark"))
    import check_closure  # noqa: PLC0415
    from orchestrator.benchmark import paths as p280  # noqa: PLC0415

    root = p280.bench_root(read_only=True)
    judge_path = root / "closeout" / "closure_judge.json"
    pack_path = root / "evidence_pack" / "pack.sha256"
    assert judge_path.is_file() and pack_path.is_file(), "F5/F6 的判决件不在场"

    import hashlib  # noqa: PLC0415

    def _fingerprint(path: Path) -> "tuple[str, int, int]":
        data = path.read_bytes()
        st = path.stat()
        return hashlib.sha256(data).hexdigest(), st.st_mtime_ns, len(data)

    before = {p.name: _fingerprint(p) for p in (judge_path, pack_path)}
    # 锁内适配（Phase 2.8.1 T0）：2.8.0 的判决链有两条「pack/invariants 的
    # commit == **活 HEAD**」的等式，而本 Task 一提交，HEAD 就前进了——
    # 「在当前代码上重跑」与「HEAD 相等」在定义上互斥。故显式声明
    # ``head_view`` = 冻结包里登记的那个 commit（**唯一合法取值**，写错一个
    # 字符仍不等、仍红）；判据本身一字未改（精确相等）。CLI 不暴露该参数，
    # 2.8.0 自己的生产判定永远看活 HEAD。
    manifest = json.loads((root / "evidence_pack" / "manifest.json")
                          .read_text(encoding="utf-8"))
    pack_commit = manifest["git_commit"]
    assert pack_commit == BASELINE_HEAD, (
        f"冻结包登记的 commit={pack_commit} 不是本阶段基线 {BASELINE_HEAD}")
    doc = check_closure.run_judge(root, require_acceptance=True,
                                  head_view=pack_commit)
    after = {p.name: _fingerprint(p) for p in (judge_path, pack_path)}

    assert before == after, "run_judge 落盘了（判决件/证据包指纹变了）"
    assert doc["problems"] == [], f"2.8.0 终判在当前代码上变红：{doc['problems'][:5]}"
    assert doc["verdict"] == "Established", doc.get("verdict")
    # LEDGER_PHASES 未被误扩（扩了就会报「active_phase 必须是 phase27」）。
    assert check_closure.LEDGER_PHASES == (
        "phase1", "phase2", "phase25", "phase26", "phase27")


# ---------------------------------------------------------------------------
# AC-P281-0-12：models_state 的凭据纪律
# ---------------------------------------------------------------------------

def test_ac_p281_0_12_models_state_no_secret():
    path = _prod_root() / "intake" / "models_state.json"
    assert path.is_file(), f"models_state.json 不在场：{path}"
    text = path.read_text(encoding="utf-8")
    for needle in ("sk-", "Bearer", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
                   "authorization", "x-api-key"):
        assert needle not in text, f"凭证字面泄漏：{needle}"
    doc = json.loads(text)
    host = doc["base_url_host"]
    # 只允许 host[:port]：不得出现 scheme、path、query、userinfo。
    assert "://" not in host and "/" not in host and "@" not in host, host
    assert host and re.fullmatch(r"[A-Za-z0-9.\-]+(:\d+)?", host), host
    models = doc["models"]
    assert set(models) == {"macro_ideation", "coding_debug", "visual_rollout"}
    for stage, item in models.items():
        assert item["model_id"], f"{stage} 的 model_id 为空（硬规则 1 冻结件）"
    assert doc["paid_calls_made_by_this_script"] == 0


# ---------------------------------------------------------------------------
# AC-P281-0-13：SIMPRAXIS_BENCHMARK_ROOT 必须未设置
# ---------------------------------------------------------------------------

def test_ac_p281_0_13_benchmark_root_env_rejected(tmp_path, monkeypatch):
    from orchestrator.benchmark.phase281 import paths as p281  # noqa: PLC0415

    monkeypatch.setenv("SIMPRAXIS_PHASE281_ROOT", str(tmp_path / "phase281"))
    monkeypatch.setenv(p281.BENCHMARK_ROOT_ENV, str(tmp_path / "bench"))
    with pytest.raises(p281.Phase281PathError):
        p281.assert_no_benchmark_root_env()
    with pytest.raises(p281.Phase281PathError):
        p281.assert_isolated()
    # 空串不触发（等价于未设置），但只要非空就必抛——不静默清理别人的 env。
    monkeypatch.setenv(p281.BENCHMARK_ROOT_ENV, "")
    p281.assert_no_benchmark_root_env()


# ---------------------------------------------------------------------------
# AC-P281-0-16：账本 CLI 完整接线
# ---------------------------------------------------------------------------

def test_ac_p281_0_16_ledger_cli_registered(p281_roots, capsys, monkeypatch):
    assert ql._namespace_for_cli(281) is ql.PHASE281_NAMESPACE, \
        "_namespace_for_cli(281) 未路由到 phase281（会写进旧期目录）"

    # approve 落 tmp 根：approvals/<batch>.json。
    rc = ql.main(["--ledger-phase", "281", "approve", "--batch-id", BATCH,
                  "--task", "1", "--stage", STAGE, "--approved", "1",
                  "--note", "fixture（AC-16，零付费）"])
    assert rc == 0
    approval = (p281_roots / "phase281" / "quota" / "approvals" / f"{BATCH}.json")
    assert approval.is_file(), f"批准件未落 phase281：{approval}"
    assert _mode(approval) == 0o600
    capsys.readouterr()                       # 清掉 approve 的输出

    # 开账 selfcheck 行：严格机判放行 selfcheck，status 不报问题。
    ledger = p281_roots / "phase281" / "quota" / "ledger.jsonl"
    from orchestrator.gateway.quota_ledger import _now_iso  # noqa: PLC0415

    selfcheck = {"ts_iso": _now_iso(), "event": "selfcheck", "seq": 1,
                 "stage": None, "model": None, "thread_id": "", "task": None,
                 "batch_id": None, "global_used_after": 0, "ok": True}
    with rp.open_ledger_append(ledger) as fh:
        fh.write(json.dumps(selfcheck, ensure_ascii=False) + "\n")
    rows = [json.loads(x) for x in ledger.read_text(encoding="utf-8").splitlines()
            if x.strip()]
    # 判据本体：严格校验 + selfcheck 放行（不是靠 validate=False 空转）。
    ql.validate_ledger_rows(rows, allow_events=("selfcheck",))
    summary = ql.ledger_summary(rows, allow_events=("selfcheck",))
    assert summary["selfcheck_rows"] == 1 and summary["call_rows"] == 0

    # `choices` 是唯一真值来源：截获 `add_argument` 的参数（**不替换**
    # `ArgumentParser` 类本体——那会破坏 argparse 内部 `super()` 的全局查找）。
    import argparse  # noqa: PLC0415

    seen: dict = {}
    real_add = argparse.ArgumentParser.add_argument

    def _spy_add(self, *names, **kwargs):
        if "--ledger-phase" in names:
            seen["choices"] = tuple(kwargs.get("choices") or ())
        return real_add(self, *names, **kwargs)

    monkeypatch.setattr(argparse.ArgumentParser, "add_argument", _spy_add)
    assert ql.main(["--ledger-phase", "281", "status"]) == 0
    monkeypatch.undo()

    choices = seen["choices"]
    # 旧四个期号逐字仍在（用 (281,) 替换会让旧期 CLI 全废）。
    for old in (2, 25, 26, 27):
        assert old in choices, f"--ledger-phase 丢了旧期号 {old}"
    assert 281 in choices

    out = json.loads(capsys.readouterr().out)
    assert out["ledger_phase"] == 281
    assert Path(out["ledger_path"]) == ledger
    assert out["hard_cap"] == 180 and out["task_caps"] == {"1": 60, "2": 60, "3": 60}
    assert out["paused"] in (False, None) or isinstance(out["paused"], dict)


# ---------------------------------------------------------------------------
# __all__ 之外的卫生：phase281 包不导出路径（路径只能经 paths 模块）
# ---------------------------------------------------------------------------

def test_phase281_package_surface():
    import orchestrator.benchmark.phase281 as p  # noqa: PLC0415

    assert not hasattr(p, "phase281_root"), \
        "路径解析不得从包根导出（唯一入口是 .paths）"
    assert set(p.__all__) >= {"ARMS", "ARM_IDS", "EXPERIMENT_ID", "trial_id"}
