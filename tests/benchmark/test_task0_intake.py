"""Phase 2.8.0 Task 0 机判：AC-T0-1..9（P0-0 仓库准入与基线冻结）。

九条 AC 的**全部输入**都是 ``<bench_root>/intake/`` 下由
``scripts/benchmark/preflight.py`` 从真实命令输出生成的 JSON——测试只做
「重算一遍再比对」，不读日志人眼核对，也不接受手写数字（T0.1 不变量 2）。
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import stat
from pathlib import Path

import pytest

from orchestrator.benchmark import intake, paths
from orchestrator.common.runtime_paths import (
    phase1_root,
    phase2_root,
    phase25_root,
    phase26_root,
    phase27_root,
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_HEX40_RE = re.compile(r"^[0-9a-f]{40}$")
_SUMMARY_RE = re.compile(
    r"(\d+) passed,\s*(\d+) skipped,\s*(\d+) deselected.*?in ([\d.]+)s")
_EMPTY = "(空)"


@pytest.fixture
def bench(monkeypatch) -> Path:
    """真数据根（不受 conftest 的 phase2 hermetic 重定向影响）。"""
    monkeypatch.delenv("SIMPRAXIS_PHASE2_ROOT", raising=False)
    root = paths.bench_root()
    assert root.is_dir(), f"数据根不在场：{root}"
    return root


def _load(bench: Path, name: str) -> dict:
    return json.loads((bench / "intake" / name).read_text(encoding="utf-8"))


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------------------
# AC-T0-1 数据根与 12 个子目录：0700 / 无符号链接 / 不在五本账根内
# --------------------------------------------------------------------------

def test_ac_p0_0_1_root_permissions(bench: Path):
    assert stat.S_IMODE(bench.stat().st_mode) == 0o700, bench
    subdirs = paths.all_subdirs()
    assert len(subdirs) == 12, subdirs
    for name in subdirs:
        d = bench / name
        assert d.is_dir(), f"缺子目录：{d}"
        assert not d.is_symlink(), f"子目录不得是符号链接：{d}"
        assert stat.S_IMODE(d.stat().st_mode) == 0o700, d
    private = bench / "private"
    for name in ("hidden", "references", "evaluator_keys"):
        d = private / name
        assert d.is_dir() and stat.S_IMODE(d.stat().st_mode) == 0o700, d
    # 无符号链接（含 cmd_logs）
    links = [str(p) for p in bench.rglob("*") if p.is_symlink()]
    assert links == [], links
    # 与五个相位根两两互异且互不嵌套
    rp = bench.resolve()
    for phase_root in (phase1_root(), phase2_root(), phase25_root(),
                       phase26_root(), phase27_root()):
        other = Path(phase_root).resolve(strict=False)
        assert rp != other
        for a, b in ((rp, other), (other, rp)):
            with pytest.raises(ValueError):
                a.relative_to(b)


# --------------------------------------------------------------------------
# AC-T0-2 / AC-T0-3 仓库与 KB 状态快照
# --------------------------------------------------------------------------

def test_ac_p0_0_2_repository_state(bench: Path):
    doc = _load(bench, "repository_state.json")
    assert _HEX40_RE.match(doc["head"]), doc["head"]
    assert _HEX40_RE.match(doc["origin_main"]), doc["origin_main"]
    # 计划 §0.3 锚点 ahead==1；2026-11-05 用户裁决「修陈旧断言后重冻」新增一条
    # 修复提交（dc22f88），故本阶段起点实测 ahead==2（= 26b45d2 文档提交 + 该修复）。
    # 这里不放宽成 >=：**精确等值 2**，并另钉 origin_main 未被推走。
    assert doc["origin_main"] == "4b5d134a447b2069b050887d63f3ddd4e687f123"
    assert doc["ahead"] == 2, doc["ahead"]
    assert doc["status_porcelain"] == "", doc["status_porcelain"]
    assert doc["generated_by"] == "scripts/benchmark/preflight.py"
    # 剔除项只允许是本阶段自己新增的三处路径（多一条都算脏）
    for line in doc["status_porcelain_excluded"]:
        assert line[:2].strip() in ("??", "A"), line
        assert any(line[3:].startswith(p)
                   for p in intake.PHASE28_NEW_PATHS), line


def test_ac_p0_0_3_kb_state(bench: Path):
    doc = _load(bench, "kb_state.json")
    # 硬断言（AC-T0-3）：不符即 STOP-D/B，报用户裁决；不得就地漂移登记
    assert doc["head"] == "5f0bb17e91c9215536863b40f191a07b8f47ef35", doc["head"]
    assert doc["status_porcelain"] == "", doc["status_porcelain"]
    assert doc["diff_stat"] == "", doc["diff_stat"]


# --------------------------------------------------------------------------
# AC-T0-4 测试基线只认 raw log，且逐数字相符
# --------------------------------------------------------------------------

def test_ac_p0_0_4_test_baseline(bench: Path):
    doc = _load(bench, "test_baseline.json")
    raw_log = Path(doc["raw_log"])
    assert raw_log.is_file(), raw_log
    text = raw_log.read_text(encoding="utf-8", errors="replace")
    matches = _SUMMARY_RE.findall(text)
    assert matches, "raw log 缺 pytest 摘要行"
    passed, skipped, deselected, duration_s = matches[-1]
    assert doc["passed"] == int(passed)
    assert doc["skipped"] == int(skipped)
    assert doc["deselected"] == int(deselected)
    assert doc["duration_s"] == float(duration_s)
    assert doc["rc"] == 0, doc["rc"]
    import sys

    assert doc["cmd"] == intake.TWO_EXCLUSION_CMD.format(py=sys.executable)
    # 冻结基线本身（Task 7 精确增量的起点）
    assert doc["passed"] == 2304, doc["passed"]
    assert doc["skipped"] == 7, doc["skipped"]
    assert doc["deselected"] == 66, doc["deselected"]


# --------------------------------------------------------------------------
# AC-T0-5 环境锚点
# --------------------------------------------------------------------------

def test_ac_p0_0_5_environment(bench: Path):
    doc = _load(bench, "environment_baseline.json")
    assert doc["python"] == "3.10.12", doc["python"]
    assert doc["packages"].get("mujoco"), doc["packages"]
    assert doc["docker_version"].startswith("29.8.1"), doc["docker_version"]
    assert "A800" in doc["gpu_name"], doc["gpu_name"]
    assert doc["gpu_total_mib"] == 40960, doc["gpu_total_mib"]
    assert doc["df_mnt_available_gib"] >= 100, doc["df_mnt_available_gib"]


# --------------------------------------------------------------------------
# AC-T0-6 五本账：行数 / sha256 / 只读
# --------------------------------------------------------------------------

def test_ac_p0_0_6_ledgers(bench: Path):
    doc = _load(bench, "sealed_ledger_sha256.json")
    expected_rows = {"phase1": 29, "phase2": 455, "phase25": 56,
                     "phase26": 49, "phase27": 68}
    assert set(doc["ledgers"]) == set(expected_rows)
    for phase, rows in expected_rows.items():
        entry = doc["ledgers"][phase]
        assert set(entry) >= {"path", "rows", "sha256"}
        assert entry["rows"] == rows, (phase, entry["rows"])
        assert _SHA256_RE.match(entry["sha256"]), entry["sha256"]
        # 只读实证：此刻重算字节 sha256 必须与记录逐字相同（期间无人写入）
        p = Path(entry["path"])
        assert p.is_file(), p
        assert _sha256_file(p) == entry["sha256"], phase


# --------------------------------------------------------------------------
# AC-T0-7 四张活表行数
# --------------------------------------------------------------------------

def test_ac_p0_0_7_table_counts(bench: Path):
    doc = _load(bench, "table_counts.json")
    expected = {"kb_chunks": 1542, "phase1_learnings": 44,
                "solution_chunks": 106, "phase2_learnings": 1}
    for name, rows in expected.items():
        assert doc["tables"][name] == rows, (name, doc["tables"][name])
    assert doc["drift_note"] == "", doc["drift_note"]


# --------------------------------------------------------------------------
# AC-T0-8 intake_report.md 与 JSON 交叉核对（不靠人眼）
# --------------------------------------------------------------------------

def _flatten(value) -> "set[str]":
    out: "set[str]" = set()
    if isinstance(value, dict):
        for v in value.values():
            out |= _flatten(v)
    elif isinstance(value, list):
        for v in value:
            out |= _flatten(v)
    elif isinstance(value, bool):
        out.add("true" if value else "false")
    elif value is None:
        out.add("(null)")
    else:
        out.add(str(value))
    return out


def test_ac_p0_0_8_report_crosscheck(bench: Path):
    report = (bench / "intake" / "intake_report.md").read_text(encoding="utf-8")
    rows, orphans = [], []
    for line in report.splitlines():
        if not line.startswith("| "):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) != 3:
            # 带 .json 来源却切不出三列的行，说明表格被撑破了 → 不许静默跳过
            if ".json" in line:
                orphans.append(line)
            continue
        if cells[0] in ("项", "---") or set(cells[0]) <= {"-"}:
            continue
        rows.append(cells)
    assert not orphans, f"表格行被撑破（多行值？）：{orphans}"
    assert rows, "报告里没有可交叉核对的表格行"
    checked = 0
    for key, value, source in rows:
        if not source.endswith(".json"):
            continue
        doc = _load(bench, source)
        flat = _flatten(doc)
        if value == _EMPTY:
            assert "" in flat, f"{source}::{key} 报告写(空)但 JSON 无空串"
        else:
            assert value in flat, f"{source}::{key} 报告值 {value!r} 不在 JSON 中"
        checked += 1
    # 报告行数 ≥ 40（七节全在），防止「删到只剩几行」也能绿
    assert checked >= 40, checked


# --------------------------------------------------------------------------
# AC-T0-9 不变量 before 基线可复算
# --------------------------------------------------------------------------

def test_ac_p0_0_9_invariants_before(bench: Path):
    doc = _load(bench, "invariants_before.json")

    stage_models = Path(doc["stage_models_yaml"])
    assert stage_models.is_file(), stage_models
    assert _sha256_file(stage_models) == doc["stage_models_sha256"]
    assert _SHA256_RE.match(doc["stage_models_sha256"])

    assert _SHA256_RE.match(doc["phase27_pack_sha256"])
    assert _SHA256_RE.match(doc["phase27_manifest_sha256"])
    pack = Path(doc["phase27_pack"])
    assert (pack / "manifest.json").is_file(), pack
    assert doc["phase27_pack_files_missing"] == [], \
        doc["phase27_pack_files_missing"]
    assert doc["phase27_pack_n_files"] > 0

    # 独立复算：自己走一遍 AST，不调 intake 的辅助函数
    loop = intake.REPO_ROOT / "orchestrator" / "agentic" / "loop.py"
    source = loop.read_text(encoding="utf-8")
    segment = None
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef) and node.name == "_entrypoint_ready":
            segment = ast.get_source_segment(source, node)
            break
    assert segment, "loop.py 里找不到 _entrypoint_ready"
    assert hashlib.sha256(segment.encode("utf-8")).hexdigest() == \
        doc["entrypoint_ready_source_sha256"]
