"""Phase 2.5 Task 1 — hindcast 数据集/呼量方法学 v2/burn 规则测试。

全部用合成 ledger/req 夹具，hermetic、0 付费、0 触网。活账核对挂自包含
``ledger_integration`` marker（已在 tests/conftest.py 注册，与 paid /
kb_integration 同构）：默认 deselect，需 SIMPRAXIS_LEDGER_INTEGRATION=1
才跑。
"""
from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts" / "phase25"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


hb = _load("t1_hindcast_build", "hindcast_build.py")
ec = _load("t1_estimate_calls", "estimate_calls.py")
bc = _load("t1_burn_check", "burn_check.py")

# conftest autouse 夹具把 SIMPRAXIS_PHASE2_ROOT 重定向到临时目录；模块导入
# 早于夹具，此处快照 shell 真实账根，供活账核对/返修数字门还原。
_SHELL_PHASE2_ROOT = os.environ.get("SIMPRAXIS_PHASE2_ROOT")
_SHELL_PHASE25_ROOT = os.environ.get("SIMPRAXIS_PHASE25_ROOT")
_SHELL_PHASE1_ROOT = os.environ.get("SIMPRAXIS_PHASE1_ROOT")


# ------------------------------------------------------------ 合成夹具

def _row(seq, batch_id, task, stage="coding_debug", ok=True,
         thread_id=None, evidence=None):
    return {"seq": seq, "batch_id": batch_id, "task": task,
            "stage": stage, "ok": ok, "thread_id": thread_id,
            "evidence": evidence}


def _write_ledger(path: Path, rows: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
        encoding="utf-8")


def _req(traceback_body: str, injected: str = "") -> dict:
    """合成 runner req：单一 user text，含合同注入段、traceback 段、后续段。"""
    text = (
        "## 本轮合同\nfull contract injected every call\n"
        + injected
        + "## traceback / 日志尾部\n"
        + traceback_body + "\n"
        + "## 下一步\n继续修复\n")
    return {"messages": [{"role": "user", "content": text}]}


ENG_BODY = "RuntimeError: mesh empty or not watertight at precheck"
CAP_BODY = ('metrics: {"nonmanifold_edges": 0, "is_watertight": true}; '
            "contract 3 pitcher/cup clearance 0.008 < 0.012")
KB_INJECTED = (
    "## 知识库召回（troubleshooting）\n"
    "历史失败短语回灌：mesh empty or not watertight；锚 0 命中\n"
    "## 避坑 learnings\n曾见 AttributeError 与 binding_utils 断言\n")


# ------------------------------------------------------------ AC-T1-1 读账/切分

def test_split_calls_and_blocked(tmp_path):
    rows = [_row(1, "B", 6), _row(None, "B", 6), _row(2, "B", 6, ok=False)]
    p = tmp_path / "quota" / "ledger.jsonl"
    _write_ledger(p, rows)
    loaded = hb.read_ledger(p)
    calls, blocked = hb.split_calls_blocked(loaded)
    assert len(loaded) == 3
    assert [r["seq"] for r in calls] == [1, 2]
    assert len(blocked) == 1 and blocked[0]["seq"] is None


def test_round_chunk_labels():
    assert hb.round_chunk("d/coding_round1.chunk0.req.json")[2] == "birth_chunk"
    assert hb.round_chunk("d/coding_round1.chunk5.req.json")[2] == "birth_chunk"
    assert hb.round_chunk("d/coding_round1.req.json")[2] == "birth_simple"
    assert hb.round_chunk("d/coding_round3.req.json")[2] == "repair"
    assert hb.round_chunk("d/macro_round1.req.json")[2] == "macro"
    assert hb.round_chunk(None)[2] == "unknown"
    assert hb.round_chunk("d/other.json")[2] == "unknown"


def test_batch_slice_birth_and_repair_histogram():
    rows = [
        _row(10, "T7-C2-stage1", 7, evidence="d/coding_round1.chunk0.req.json"),
        _row(11, "T7-C2-stage1", 7, evidence="d/coding_round1.chunk1.req.json",
             ok=False),
        _row(12, "T7-C2-stage1", 7, evidence="d/coding_round1.chunk2.req.json"),
        _row(13, "T7-C2-stage1", 7, evidence="d/coding_round2.req.json"),
        _row(14, "T7-C2-stage1", 7, evidence="d/coding_round5.req.json"),
        _row(15, "T7-C2-stage1", 7, stage="macro_ideation",
             evidence="d/macro_round1.req.json"),
    ]
    batches = hb.batch_slices(rows)
    b = batches["T7-C2-stage1"]
    assert b["calls"] == 6 and b["ok"] == 5 and b["fail"] == 1
    assert b["birth_chunk_rows"] == 3
    assert b["repair_rows"] == 2
    assert b["macro_rows"] == 1
    assert b["birth_chunks"] == {0, 1, 2}
    assert b["repair_rounds"] == {2, 5}
    assert dict(b["round_hist"]) == {"round1": 3, "round2": 1, "round5": 1}


# ------------------------------------------------------------ AC-T1-2 墙 B 分类器

def test_classify_driving_failure_eng_and_cap():
    wall, _ = hb.classify_driving_failure(ENG_BODY)
    assert wall == "B-eng"
    wall, _ = hb.classify_driving_failure(CAP_BODY)
    assert wall == "B-cap"


def test_metrics_dict_topology_pass_is_not_eng():
    # 见证量字典 nonmanifold_edges=0 / watertight=true 是拓扑已过；无合同违约
    # 短语时 fail-closed，绝不因泛词 watertight 误判 B-eng。
    with pytest.raises(ValueError):
        hb.classify_driving_failure(
            'metrics: {"nonmanifold_edges": 0, "is_watertight": true, '
            '"faces": 1536, "boundary_edges": 0}')


def test_driving_section_missing_raises():
    req = {"messages": [{"role": "user", "content": "## 合同\n无 traceback 段"}]}
    with pytest.raises(ValueError):
        hb.driving_failure_section(req)


def test_injected_kb_section_cannot_double_match():
    # KB/避坑注入段逐字带历史失败短语；当轮真正 driving failure 是 cap。
    req = _req(CAP_BODY, injected=KB_INJECTED)
    section = hb.driving_failure_section(req)
    # 注入段的 eng 短语不计入 driving failure（is_watertight 见证量键名允许在场）
    assert "empty or not watertight" not in section
    non_inj = hb.non_injected_text(req)
    assert "锚 0 命中" not in non_inj and "binding_utils" not in non_inj
    wall, _ = hb.classify_driving_failure(section)
    assert wall == "B-cap"


def _make_wall_b_pkg(root: Path, pkg_dir: str, birth_kw: str,
                     repair_bodies: list[str]):
    ev = root / pkg_dir / "runner_evidence"
    ev.mkdir(parents=True)
    for chunk in range(6):
        body = birth_kw if chunk == 0 else "birth shard"
        (ev / f"coding_round1.chunk{chunk}.req.json").write_text(
            json.dumps(_req(body)), encoding="utf-8")
    for rnd, body in zip(range(2, 9), repair_bodies):
        (ev / f"coding_round{rnd}.req.json").write_text(
            json.dumps(_req(body)), encoding="utf-8")


def test_build_wall_b_synthetic_39_split(tmp_path):
    # 与活账同形：C9 七轮全 eng；C10 四 eng 三 cap；C11 一 eng 六 cap。
    _make_wall_b_pkg(
        tmp_path, "evidence-run5-a", "watertight", [ENG_BODY] * 7)
    _make_wall_b_pkg(
        tmp_path, "evidence-run5-b", "边界边 boundary edge",
        [ENG_BODY] * 4 + [CAP_BODY] * 3)
    _make_wall_b_pkg(
        tmp_path, "evidence-run5-c", "哨兵 -1 禁；数值导数峰值",
        [ENG_BODY] + [CAP_BODY] * 6)
    # 包目录名与 RUN5_BATCHES 不同名：直接复用其逻辑需对齐常量，这里 monkeypatch。
    orig = hb.RUN5_BATCHES
    hb.RUN5_BATCHES = [
        ("T8-C9-stage1", 398, "evidence-run5-a", ("watertight",)),
        ("T8-C10-stage1", 411, "evidence-run5-b", ("边界边", "boundary edge")),
        ("T8-C11-stage1", 424, "evidence-run5-c", ("哨兵", "数值导数")),
    ]
    try:
        split = hb.build_wall_b(tmp_path)
    finally:
        hb.RUN5_BATCHES = orig
    assert [e["seq"] for e in split["entries"]] == list(range(398, 437))
    assert split["sB_eng"] == 30 and split["sB_cap"] == 9
    assert split["counts"]["birth"] == 18 and split["counts"]["repair"] == 21
    by = split["counts"]["by_batch"]
    assert by["T8-C9-stage1"] == {"B_eng": 13, "B_cap": 0}
    assert by["T8-C10-stage1"] == {"B_eng": 10, "B_cap": 3}
    assert by["T8-C11-stage1"] == {"B_eng": 7, "B_cap": 6}
    assert all(e["evidence_level"] == "byte_confirmed"
               for e in split["entries"])


def test_build_wall_b_failclosed_on_missing_keyword(tmp_path, monkeypatch):
    _make_wall_b_pkg(tmp_path, "evidence-run5-a", "watertight",
                     [ENG_BODY] * 7)
    monkeypatch.setattr(hb, "RUN5_BATCHES", [
        ("T8-C9-stage1", 398, "evidence-run5-a", ("不存在的增量词",))])
    with pytest.raises(ValueError):
        hb.build_wall_b(tmp_path)


# ------------------------------------- AC-T1-2 墙 A 三级取证（REVISE M1/M2）

def _runner_req(path: Path, body: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_req(body)), encoding="utf-8")


def _sections_req(sections: dict[str, str]) -> dict:
    """按 ``## 标题`` 段拼合成 user req；末段保证 traceback 段有 ## 收尾。"""
    text = "".join(f"## {head}\n{body}\n" for head, body in sections.items())
    return {"messages": [{"role": "user", "content": text + "## 结束\n"}]}


def _wa_ev(rel: str) -> str:
    """runner_root 相对路径 → ledger evidence 字面量。"""
    return "phase1/task4_runner/" + rel


def _put_req(root: Path, rel: str, sections: dict[str, str]) -> str:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(_sections_req(sections), ensure_ascii=False),
                 encoding="utf-8")
    return _wa_ev(rel)


DRIVING_HEAD = "traceback / 日志尾部"
DEP_EASY = ("Traceback (most recent call last):\n"
            'RuntimeError: cannot import OffScreenRenderEnv from '
            'libero.libero.envs: ModuleNotFoundError("No module named '
            '\'easydict\'")')
BENCH_SELF = ("Traceback (most recent call last):\n"
              "TypeError: Benchmark.get_task_names() missing 1 required "
              "positional argument: 'self'")
LAUNCH_TAIL = ("agent/runs/phase2/venvs/libero/bin/python: can't open file "
               "'/home/agent/runs/phase2/task7_libero/stage1_spatial/"
               "stage1_minimal.py': [Errno 2] No such file or directory")


def _wa_rows(seq_path: dict[int, str], thread: str,
             batch: str = "T7-C2-stage1") -> list[dict]:
    return [_row(seq, batch, 7, thread_id=thread, evidence=ev)
            for seq, ev in seq_path.items()]


def test_wa_stage0_section_family_words_isolated(tmp_path):
    # ① 三个 G-9 族词只出现在 Stage0 段；干净 traceback（DEP 导入失败）
    # driving 段不得命中任何族 → confirmed 为空，行落 unclassified RULE-DEP。
    dep_sections = {
        "Stage0 探针（sph_ok/出生前）": (
            "历史回灌：binding_utils 断言 jnt_type 7 维末位；"
            "base_controller d.qM 已改名 M；replace 围栏 锚 0 命中 案例"),
        DRIVING_HEAD: DEP_EASY,
        "下一步": "继续装依赖",
    }
    bench_sections = {DRIVING_HEAD: BENCH_SELF, "下一步": "x"}
    ev_dep = _put_req(tmp_path, "t7-b/r2/coding_round1.req.json", dep_sections)
    ev_bench = _put_req(tmp_path, "t7-b/r3/coding_round1.req.json",
                        bench_sections)
    rows = _wa_rows({252: ev_dep, 262: ev_bench}, "t7-b")
    t = hb._compute_wall_a_tiers(rows, tmp_path)
    assert t["byte_confirmed"] == []
    assert {e["seq"]: e["rule"] for e in t["byte_unclassified"]} == {
        252: hb.RULE_DEP, 262: hb.RULE_BENCH}
    # 直接断言：driving 段字节里族词不可达
    req = json.loads((tmp_path / "t7-b/r2/coding_round1.req.json").read_text())
    section = hb.driving_failure_section(req)
    assert "binding_utils" not in section and "qM" not in section
    assert "锚 0 命中" not in section


def test_wa_machine_note_and_source_section_isolated(tmp_path):
    # ② 机器核验段的「补丁已被整体拒绝」与源码段 stage1_minimal.py:: 的
    # KeyError/can't-open-file 都在 driving 段外，不得决定规则码；driving
    # 段内末行机器注记（JSONDecodeError）不得污染 tail 摘录。
    sections = {
        "机器核验与补丁围栏": (
            "机器判定：补丁已被整体拒绝；old 块命中 18912 次（需恰好 1 次）"),
        "源码 stage1_minimal.py::run": (
            "KeyError: 'fake-key-in-source'\n"
            "# python: can't open file （源码注释里的字样）"),
        DRIVING_HEAD: (
            BENCH_SELF + "\n"
            "机器核验原因：未产出可应用补丁 "
            "(JSONDecodeError: Expecting ',' delimiter: line 1 column 2)"),
        "下一步": "x",
    }
    ev = _put_req(tmp_path, "t7-d/r3/coding_round1.req.json", sections)
    rows = _wa_rows({263: ev}, "t7-d")
    t = hb._compute_wall_a_tiers(rows, tmp_path)
    assert {e["seq"]: e["rule"] for e in t["byte_unclassified"]} == {
        263: hb.RULE_BENCH}
    entry = t["byte_unclassified"][0]
    assert "补丁已被整体拒绝" not in entry["tail_excerpt"]
    assert "JSONDecodeError" not in entry["tail_excerpt"]
    assert "get_task_names" in entry["tail_excerpt"]


def test_wa_golden_254_308_byte_confirmed(tmp_path):
    # ④ 254/308 合成 golden：driving 段字节命中 G-9 族才 byte_confirmed；
    # 308 只记 env_api，不許带 binding_utils/qM；254 仅锚点族。
    b254 = ("replace 围栏结果：锚 0 命中（需恰好 1 次，实际 0 次）\n"
            "Traceback (most recent call last):\n"
            'RuntimeError: cannot import OffScreenRenderEnv from '
            'libero.libero.envs: ModuleNotFoundError("No module named '
            '\'easydict\'")')
    b308 = ("Traceback (most recent call last):\n"
            "KeyError: 'main_table_between_plate_ramekin_region_pos'")
    ev254 = _put_req(tmp_path, "t7-e/r3/coding_round1.req.json",
                     {DRIVING_HEAD: b254, "下一步": "x"})
    ev308 = _put_req(tmp_path, "t7-e/r2/coding_round1.req.json",
                     {DRIVING_HEAD: b308, "下一步": "x"})
    rows = _wa_rows({250: ev254, 254: ev254, 303: ev308, 308: ev308},
                    "t7-e")
    t = hb._compute_wall_a_tiers(rows, tmp_path)
    conf = {e["seq"]: e for e in t["byte_confirmed"]}
    assert sorted(conf) == [254, 308]
    assert conf[254]["g9_families"] == ["anchor"]
    assert conf[308]["g9_families"] == ["env_api"]
    assert conf[254]["overwritten_seqs"] == [250]
    assert conf[308]["overwritten_seqs"] == [303]
    assert "easydict" in conf[254]["tail_excerpt"]
    assert sorted(e["seq"] for e in t["seq_level_overwritten"]) == [250, 303]


def test_wa_sole_ref_unclassified_and_mechanical_overwrite_reason(tmp_path):
    # ⑤ sole-ref 存活 → unclassified（理由含存活事实+tail）；真覆盖者理由
    # 机械含 refs 与存活 seq，不硬写。
    ev_multi = _put_req(tmp_path, "t7-b/r1/coding_round1.req.json",
                        {DRIVING_HEAD: DEP_EASY, "下一步": "x"})
    ev_sole = _put_req(tmp_path, "t7-b/r5/coding_round1.req.json",
                       {DRIVING_HEAD: DEP_EASY, "下一步": "x"})
    ev_reach = _put_req(tmp_path, "t7-b/r3/coding_round1.req.json",
                        {DRIVING_HEAD: BENCH_SELF, "下一步": "x"})
    rows = _wa_rows({244: ev_multi, 245: ev_multi, 246: ev_multi,
                     247: ev_multi, 248: ev_multi, 252: ev_multi,
                     256: ev_sole, 262: ev_reach}, "t7-b")
    t = hb._compute_wall_a_tiers(rows, tmp_path)
    uncl = {e["seq"]: e for e in t["byte_unclassified"]}
    assert set(uncl) == {252, 256, 262}
    sole = uncl[256]
    assert sole["rule"] == hb.RULE_DEP
    assert sole["refs_to_same_path"] == [256]
    assert "存活字节在位" in sole["reason"] and "easydict" in sole["tail_excerpt"]
    overw = {e["seq"]: e for e in t["seq_level_overwritten"]}
    assert sorted(overw) == [244, 245, 246, 247, 248]
    e244 = overw[244]
    assert e244["rule"] == "UNRECOVERABLE"
    assert e244["survivor_seq"] == 252
    assert e244["refs"] == [244, 245, 246, 247, 248, 252]
    assert "存活字节属 seq 252" in e244["reason"]
    assert "机械派生" in e244["reason"]
    assert t["sA"] == 8


def test_wa_launch_in_window_still_counts_sa(tmp_path):
    # ⑥ RULE-LAUNCH 窗内非修复轮：落 unclassified、标 LAUNCH，仍计 sA。
    ev_l1 = _put_req(tmp_path, "t7-e/r1/coding_round1.req.json",
                     {DRIVING_HEAD: LAUNCH_TAIL, "下一步": "x"})
    ev_l2 = _put_req(tmp_path, "t7-e/rd/coding_round1.req.json",
                     {DRIVING_HEAD: LAUNCH_TAIL, "下一步": "x"})
    ev_reach = _put_req(tmp_path, "t7-e/r3/coding_round1.req.json",
                        {DRIVING_HEAD: BENCH_SELF, "下一步": "x"})
    rows = _wa_rows({300: ev_l1, 307: ev_l1, 261: ev_l2, 262: ev_reach},
                    "t7-e")
    t = hb._compute_wall_a_tiers(rows, tmp_path)
    uncl = {e["seq"]: e for e in t["byte_unclassified"]}
    assert uncl[307]["rule"] == hb.RULE_LAUNCH
    assert uncl[261]["rule"] == hb.RULE_LAUNCH
    assert uncl[307]["is_repair_round"] is False
    assert "非修复轮" in uncl[307]["rule_reason"]
    # sA 含 LAUNCH 呼：0 confirmed + 3 unclassified + 1 overwritten = 4
    assert t["sA"] == 4
    assert len(t["byte_confirmed"]) + len(t["byte_unclassified"]) \
        + len(t["seq_level_overwritten"]) == t["sA"]


def test_wa_same_tail_symmetric_rule_across_threads(tmp_path):
    # ⑦ 同一 driving tail 注入窗内/窗外两线程：规则码必须相同，桶仅由
    # 窗口成员身份决定（unclassified vs excluded_per_seq）。
    ev_in = _put_req(tmp_path, "t7-e/r1/coding_round1.req.json",
                     {DRIVING_HEAD: LAUNCH_TAIL, "下一步": "x"})
    ev_reach = _put_req(tmp_path, "t7-e/r3/coding_round1.req.json",
                        {DRIVING_HEAD: BENCH_SELF, "下一步": "x"})
    ev_out = _put_req(tmp_path, "t7-a/r1/coding_round1.req.json",
                      {DRIVING_HEAD: LAUNCH_TAIL, "下一步": "x"})
    rows = _wa_rows({307: ev_in, 262: ev_reach}, "t7-e")
    rows += _wa_rows({237: ev_out}, "t7-a", batch="T7-C1-stage1")
    t = hb._compute_wall_a_tiers(rows, tmp_path)
    inside = next(e for e in t["byte_unclassified"] if e["seq"] == 307)
    outside = next(e for e in t["excluded_per_seq"] if e["seq"] == 237)
    assert inside["rule"] == outside["rule"] == hb.RULE_LAUNCH
    assert inside["tail_excerpt"] == outside["tail_excerpt"] == LAUNCH_TAIL
    assert t["window_threads"] == ["t7-e"]
    assert t["sA"] == 2  # 窗内仅 e 两呼；a 不计


def test_wa_surviving_req_missing_traceback_raises(tmp_path):
    # ⑧ 存活 coding req 缺 traceback 段 → fail-closed（禁止静默继续）。
    p = tmp_path / "t7-b/r1/coding_round1.req.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(
        {"messages": [{"role": "user", "content": "## 合同\n无错误段"}]}),
        encoding="utf-8")
    rows = _wa_rows({252: _wa_ev("t7-b/r1/coding_round1.req.json")}, "t7-b")
    with pytest.raises(ValueError):
        hb._compute_wall_a_tiers(rows, tmp_path)


def test_wa_tail_only_machine_notes_failclosed(tmp_path):
    # MINOR-2：driving 段存在但 tail 候选全为机器注释行（一条注释引用规则码
    # "DEP"，关键词匹配救不了归类）→ fail-closed，消息含 seq/path；正常真实
    # traceback 末行（含错误行在注释行之前）不受影响。
    note_only = ("机器核验原因：建议归 RULE-DEP（机器注释，非 traceback 行）\n"
                 "机器判定原因：围栏机器拒收，但响应非 JSON，无错误行")
    with pytest.raises(ValueError, match="tail 仅含机器注释行，无法机械归类") \
            as ei:
        hb._tail_error_line(
            note_only, seq=298, path="/x/coding_round8.req.json")
    msg = str(ei.value)
    assert "seq=298" in msg and "coding_round8.req.json" in msg

    normal = ("机器核验原因：未产出可应用补丁 (JSONDecodeError: x)\n"
              "RuntimeError: cannot read action bounds from env")
    tail = hb._tail_error_line(normal)
    assert "cannot read action bounds" in tail

    # 真错误行在前、注释行在末：倒序跳过注释仍取真错误行（256/263 同形）
    mixed = ("TypeError: Benchmark.get_task_names() missing 1 required "
             "positional argument: 'self'\n"
             "机器判定原因：未产出可应用补丁 (JSONDecodeError: x)")
    tail2 = hb._tail_error_line(mixed)
    assert "get_task_names" in tail2 and "JSONDecodeError" not in tail2


def test_wa_no_survivors_sa_zero():
    # 无任何存活墙 req：sA=0；失败呼只走 C_supply，不冒充墙呼量。
    rows = [_row(235, "T7-C1-stage1", 7, thread_id="t7-a",
                 evidence=None, ok=False)]
    t = hb._compute_wall_a_tiers(rows, Path("/nonexistent-runner-root"))
    assert t["sA"] == 0
    assert t["byte_confirmed"] == [] and t["byte_unclassified"] == []
    assert t["seq_level_overwritten"] == [] and t["excluded_per_seq"] == []


def test_failure_tax_bins():
    # failure_tax_bins 固定遍历四个代际箱，合成账须给足四任务行（生产恒满足）。
    rows = []
    rows += [_row(1, "b4", 4, stage="visual_rollout", ok=False)] * 3
    rows += [_row(2, "b4", 4, stage="visual_rollout", ok=True)]
    rows += [_row(25, "b6", 6) for _ in range(10)]
    rows += [_row(35, "b7", 7, ok=True)]
    rows += [_row(i, "b", 8, ok=(i < 405)) for i in range(309, 437)]
    bins = {b["task"]: b for b in hb.failure_tax_bins(rows)}
    assert bins[8]["ok"] == 96 and bins[8]["fail"] == 32
    assert bins[8]["fail_rate"] == 0.25
    assert bins[4]["ok"] == 1 and bins[4]["fail"] == 3


# ------------------------------------------------------------ AC-T1-3/4 区间算术

def test_simulate_deterministic_and_cap():
    sc, _, _ = ec.scenario_t3()
    r1, r2 = ec.simulate(sc), ec.simulate(sc)
    assert r1 == r2
    assert r1["max"] <= 6 and r1["p90"] <= 6
    sc10, _, _ = ec.scenario_t10()
    r10 = ec.simulate(sc10)
    assert r10["min"] >= 0 and r10["max"] <= 2


def test_quantile_nearest_rank():
    s = list(range(1, 11))
    assert ec.quantile(s, 0.5) == 5
    assert ec.quantile(s, 0.9) == 9
    with pytest.raises(ValueError):
        ec.quantile([], 0.5)


def test_distribution_validation():
    with pytest.raises(ValueError):
        ec.uniform(3, 2)
    with pytest.raises(ValueError):
        ec.mixture([(ec.constant(1), 0.5)])  # 权重和≠1
    with pytest.raises(ValueError):
        ec.empirical([])


def test_t9_cache_prior_zero_and_cap():
    sc, _, _ = ec.scenario_t9()
    assert sc["cache_hit_prior"] == 0.0
    r = ec.simulate_t9()
    assert r["max"] <= 160 and r["p50"] <= r["p90"]


def test_t5_subcaps_half_up_and_tie():
    normal = ec.half_up_subcaps(60, 30)
    assert normal["subcaps"] == {"wallA": 16, "wallB": 9}
    assert normal["sum"] == 25 and normal["reserve"] == 3
    assert normal["double_half_tie"] is False
    # sA=sB：两商同时恰 .5 → 双半值退化点，先跑墙 half-down
    tie = ec.half_up_subcaps(2, 2)
    assert tie["raw_half_up"] == {"wallA": 13, "wallB": 13}
    assert tie["double_half_tie"] is True
    assert tie["subcaps"] == {"wallA": 12, "wallB": 13}
    assert tie["sum"] == 25 and tie["reserve"] == 3
    with pytest.raises(ValueError):
        ec.half_up_subcaps(0, 0)


def test_t5_simulation_within_envelope():
    r = ec.simulate_t5()
    assert r["min"] >= 0 and r["max"] <= 28 and r["p90"] <= 28


def test_backtest_actuals_within_p50_p90():
    cases = [
        (195, 15, 195, 210),   # t6
        (51, 23, 51, 74),      # t7（reject 终止链声明不影响落位）
        (123, 5, 123, 128),    # t8
        (26, 3, 26, 29),       # phase1 混合代际
        (1, 23, 1, 24),        # t4 视觉空答
    ]
    for ok, fail, roots, actual in cases:
        r = ec.backtest_supply_tax(ok, fail, roots, actual)
        assert r["actual"] == actual
        assert r["p50"] <= actual <= r["p90"]
        assert r["within_p50_p90"] is True
        assert "样本内" in r["insample_disclaimer"]


def test_v1_distortion_table_pins_5_to_35x_floor():
    ratios = {x["task"]: x["ratio_vs_upper"] for x in ec.V1_DISTORTION}
    assert ratios == {"t6_genesis_q1": 34.5, "t7_libero_stage1": 11.0,
                      "t8_fluid_stage1": 20.8}
    assert min(ratios.values()) >= 5


# ------------------------------------------------------------ AC-T1-5 burn

def test_utilization_bands():
    assert bc.utilization_band(0, 28)["band"] == bc.BAND_UNDER
    assert bc.utilization_band(14, 28)["band"] == bc.BAND_50
    assert bc.utilization_band(23, 28)["band"] == bc.BAND_80
    with pytest.raises(ValueError):
        bc.utilization_band(1, 0)


def test_evaluate_burn_all_triggers():
    state = {"task": "t5", "cap": 28, "spent": 15,
             "birth_actual": 13, "birth_prior_p90": 12,
             "walls_crossed": 1, "gate_rejects": 0,
             "supply_bin": "t7_mimo_generation",
             "supply_ok": 10, "supply_fail": 9}
    r = bc.evaluate_burn(state)
    ids = {a["alert_id"] for a in r["alerts"]}
    assert "batch_50" in ids
    assert "first_birth_beyond_prior_p90" in ids
    assert "error_wall_crossed" in ids
    assert "supply_failure_rate_above_prior_p90" in ids
    assert "gate_reject" not in ids
    assert r["rebaseline_required"] is True


def test_evaluate_burn_quiet_and_edge_dedup_counters():
    state = {"task": "t5", "cap": 28, "spent": 4,
             "last_walls_crossed": 1, "walls_crossed": 1,
             "first_birth_seen_before": True,
             "supply_bin": "t8_glm_macro_kimi_coding",
             "supply_ok": 50, "supply_fail": 1}
    r = bc.evaluate_burn(state)
    assert r["alerts"] == [] and r["rebaseline_required"] is False
    # 新增 1 次驳回才发；供应 2% 不超 kimi 后验 P90
    state.update({"spent": 23, "last_band": bc.BAND_50,
                  "gate_rejects": 1})
    r2 = bc.evaluate_burn(state)
    ids = {a["alert_id"] for a in r2["alerts"]}
    assert ids == {"batch_80", "gate_reject"}


def test_birth_within_prior_no_rebaseline():
    state = {"task": "t3", "cap": 6, "spent": 2,
             "birth_actual": 4, "birth_prior_p90": 6}
    r = bc.evaluate_burn(state)
    assert all(a["alert_id"] != "first_birth_beyond_prior_p90"
               for a in r["alerts"])


def test_alert_dedup_and_rebaseline_artifacts(tmp_path):
    state = {"task": "t5", "cap": 28, "spent": 15,
             "birth_actual": 13, "birth_prior_p90": 12,
             "walls_crossed": 1,
             "supply_bin": "t7_mimo_generation",
             "supply_ok": 10, "supply_fail": 9}
    r1 = bc.evaluate_burn(state)
    keys = bc._dedup_keys(state)
    bc.append_alerts(tmp_path, "t5", r1["alerts"], dedup_keys=keys)
    log = tmp_path / "estimates" / "burn_alerts.jsonl"
    n1 = len(log.read_text().splitlines())
    bc.append_alerts(tmp_path, "t5", r1["alerts"], dedup_keys=keys)
    assert len(log.read_text().splitlines()) == n1  # 同事件不重发
    rb = bc.write_rebaseline(tmp_path, "20261029T1000Z", state,
                             r1["trigger_evaluation"])
    manifest = json.loads(Path(rb["manifest"]).read_text())
    assert manifest["scientific_caps_frozen"]
    assert "n、通过阈值、判据" in manifest["scientific_caps_frozen"][0]
    reb_est = Path(rb["manifest"]).parent / "estimates"
    assert (reb_est / "interval_t5.json").is_file()
    # 0600/0700 权限收敛
    assert stat.S_IMODE(Path(rb["manifest"]).stat().st_mode) == 0o600
    assert stat.S_IMODE(log.stat().st_mode) == 0o600


def test_alert_log_tolerates_only_trailing_half_line(tmp_path):
    # MINOR-9：末尾被 kill -9 截断的半行不致命；愈合后告警通道继续可用、
    # 去重历史不丢、文件不残留拼接物理行。
    log = tmp_path / "estimates" / "burn_alerts.jsonl"
    log.parent.mkdir(parents=True)
    good = json.dumps({"task": "t5", "alert_id": "old", "dedup_key": ""},
                      ensure_ascii=False)
    log.write_text(good + "\n" + '{"task": "t5", "alert_id": "brok',
                   encoding="utf-8")
    alerts = [{"alert_id": "new1", "action": "force_rebaseline",
               "message": "m"}]
    written = bc.append_alerts(tmp_path, "t5", alerts)
    assert len(written) == 1
    lines = log.read_text().splitlines()
    assert len(lines) == 2  # 半行被愈合丢弃，非拼接到新行
    assert json.loads(lines[-1])["alert_id"] == "new1"
    # 第二轮：半行已愈，不抛「中间坏行」，去重对旧行仍生效
    assert bc.append_alerts(
        tmp_path, "t5",
        [{"alert_id": "old", "action": "x", "message": ""}]) == []
    assert len(bc.append_alerts(
        tmp_path, "t5",
        [{"alert_id": "new2", "action": "x", "message": ""}])) == 1
    # 全部物理行可解析（无空行/拼接）
    for line in log.read_text().splitlines():
        json.loads(line)
    assert stat.S_IMODE(log.stat().st_mode) == 0o600


def test_alert_log_middle_bad_line_raises(tmp_path):
    # MINOR-9：中间坏行＝日志损坏，必须显式失败，不得静默跳过。
    log = tmp_path / "estimates" / "burn_alerts.jsonl"
    log.parent.mkdir(parents=True)
    good = json.dumps({"task": "t5", "alert_id": "a"}, ensure_ascii=False)
    log.write_text(good + "\n" + "{broken-middle\n" + good + "\n",
                   encoding="utf-8")
    with pytest.raises(ValueError, match="非末尾行"):
        bc.append_alerts(
            tmp_path, "t5",
            [{"alert_id": "z", "action": "x", "message": ""}])


def test_burn_rules_payload_immutability():
    rules = bc.default_rules()
    assert [x["fraction"] for x in rules["batch_utilization_lines"]] == [
        0.5, 0.8]
    ids = {t["id"] for t in rules["rebaseline_triggers"]}
    assert ids == {"first_birth_beyond_prior_p90", "error_wall_crossed",
                   "gate_reject",
                   "supply_failure_rate_above_prior_p90"}
    frozen = " ".join(rules["rebaseline_scope"]["never_changes"])
    assert "科学帽" in frozen and "阈值" in frozen


# ------------------------------------------------------------ AC-T1-7 T10 对账

def test_t10_closeout_verdicts(tmp_path):
    sc, _, _ = ec.scenario_t10()
    res = ec.simulate(sc)
    interval = {"estimate": {"p50": res["p50"], "p90": res["p90"]}}
    within = bc.t10_closeout(res["p50"], interval)
    assert within["verdict"] == "within_p50_p90"
    over = bc.t10_closeout(res["p90"] + 1, interval)
    assert over["verdict"] == "methodology_failure"
    assert over["reversal_forbidden"] is True
    under = bc.t10_closeout(0, interval)
    assert under["verdict"] in {"below_p50", "within_p50_p90"}
    with pytest.raises(ValueError):
        bc.t10_closeout(-1, interval)


# ------------------------------------------------------------ 落件幂等/权限

def test_write_outputs_idempotent_bytes_and_perms(tmp_path):
    ds = {"schema": hb.SCHEMA, "x": 1}
    split = {"schema": "simpraxis.phase25.wall_b_seq_split.v1", "y": 2}
    p1 = hb.write_outputs(tmp_path, ds, split)
    b1 = Path(p1["dataset"]).read_bytes()
    s1 = Path(p1["seq_split"]).read_bytes()
    p2 = hb.write_outputs(tmp_path, ds, split)
    assert Path(p2["dataset"]).read_bytes() == b1
    assert Path(p2["seq_split"]).read_bytes() == s1
    assert stat.S_IMODE(Path(p1["dataset"]).stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "walls" / "wall_b").stat().st_mode) == 0o700


# ---------------------------------------- 返修数字门（⑨；钉死 step-0 快照）

def test_revise_number_gate_against_prefix_snapshot(monkeypatch):
    if not _SHELL_PHASE25_ROOT:
        pytest.skip("需要 shell 真实 SIMPRAXIS_PHASE25_ROOT（source 00_env.sh）")
    monkeypatch.setenv("SIMPRAXIS_PHASE25_ROOT", _SHELL_PHASE25_ROOT)
    import hashlib
    from orchestrator.common import runtime_paths as rp
    root = rp.phase25_root()
    snap = json.loads((root / "estimates" / "rebaselines"
                       / "pre_fix_sha256.json").read_text())
    pre = {f["rel"]: f for f in snap["files"]}

    def cur_sha(rel: str) -> str:
        return hashlib.sha256((root / rel).read_bytes()).hexdigest()

    # (a) 返修前后逐字节相等：t3/t9/t10/burn_rules/backtest/SEQ_SPLIT + T0 四件
    byte_identical = [
        "estimates/interval_t3.json", "estimates/interval_t9.json",
        "estimates/interval_t10.json", "estimates/burn_rules.json",
        "estimates/hindcast_backtest.json",
        "walls/wall_b/SEQ_SPLIT.json",
        "estimates/t0_baseline_pytest.raw.log",
        "estimates/t0_dependency_verdict.json",
        "estimates/t0_full_v_rs.log",
        "estimates/t0_test_baseline.json",
    ]
    for rel in byte_identical:
        assert cur_sha(rel) == pre[rel]["sha256"], f"数字漂移：{rel}"

    # (b) interval_t5 全部数字字段逐值相等（provenance 字符串允许诚实变化）
    t5 = json.loads((root / "estimates" / "interval_t5.json").read_text())
    est = t5["estimate"]
    assert (est["p50"], est["p90"], est["mean"]) == (22, 28, 21.31)
    assert est["range_seen"] == [2, 28]
    assert est["sim_n"] == 40000 and est["seed"] == 20261029
    assert t5["plan_envelope"] == [12, 28] and t5["scientific_cap"] == 28
    sc = t5["t5_subcaps"]
    assert (sc["sA"], sc["sB"]) == (60, 30)
    assert sc["raw_half_up"] == {"wallA": 17, "wallB": 8}
    assert sc["subcaps"] == {"wallA": 16, "wallB": 9}
    assert sc["reserve"] == 3

    # (c) 允许变化的仅 dataset/methodology（t5 须数字全等）；其余全字节相等
    allowed_changed = {"estimates/hindcast_dataset.json",
                       "estimates/methodology_v2.md",
                       "estimates/interval_t5.json"}
    for rel, f in pre.items():
        if rel in allowed_changed:
            continue
        assert cur_sha(rel) == f["sha256"], f"未授权变化：{rel}"
    md = (root / "estimates" / "methodology_v2.md").read_text()
    assert "呼量计费构造" in md and "byte_unclassified 13" in md
    # 旧口径不得作为现行定义出现；允许在对账句里历史性引用一次。
    assert "字节确认 9" not in md
    assert md.count("seq 级 51") == 1 and "原 seq 级 51 中" in md

    # (d) 墙 B 39 条一条不动（counts + SEQ_SPLIT 字节相等已在 (a) 对账）
    dataset = json.loads(
        (root / "estimates" / "hindcast_dataset.json").read_text())
    wb = dataset["phase2"]["wall_b"]
    assert wb["counts"]["total"] == 39
    assert (wb["sB_eng"], wb["sB_cap"]) == (30, 9)
    wa = dataset["phase2"]["wall_a"]
    assert (wa["byte_confirmed_count"], wa["byte_unclassified_count"],
            wa["seq_level_overwritten_count"]) == (2, 13, 45)


# ------------------------------------------------------------ 活账核对（默认 skip）

INTEGRATION_MARK = pytest.mark.ledger_integration


@INTEGRATION_MARK
def test_live_ledger_anchors_and_walls(monkeypatch):
    if os.environ.get("SIMPRAXIS_LEDGER_INTEGRATION") != "1":
        pytest.skip("活账核对：仅在 SIMPRAXIS_LEDGER_INTEGRATION=1 时运行")
    assert _SHELL_PHASE2_ROOT and _SHELL_PHASE25_ROOT and _SHELL_PHASE1_ROOT, \
        "shell 必须 source scripts/00_env.sh 导出真实三根"
    monkeypatch.setenv("SIMPRAXIS_PHASE2_ROOT", _SHELL_PHASE2_ROOT)
    monkeypatch.setenv("SIMPRAXIS_PHASE25_ROOT", _SHELL_PHASE25_ROOT)
    monkeypatch.setenv("SIMPRAXIS_PHASE1_ROOT", _SHELL_PHASE1_ROOT)
    import hashlib
    from orchestrator.common import runtime_paths as rp
    assert str(rp.phase2_root()) == _SHELL_PHASE2_ROOT

    # 三本账 sha+mtime 前后守卫：build_dataset 对封存账只读、phase25 账零触写。
    ledgers = [rp.phase1_root() / "quota" / "ledger.jsonl",
               rp.phase2_root() / "quota" / "ledger.jsonl",
               rp.phase25_ledger_path()]
    guard = {}
    for p in ledgers:
        assert p.is_file(), f"账件缺件：{p}"
        st = p.stat()
        guard[str(p)] = (hashlib.sha256(p.read_bytes()).hexdigest(),
                         st.st_mtime_ns, st.st_size)

    dataset, seq_split = hb.build_dataset(rp.phase1_root(), rp.phase2_root())
    for p in ledgers:
        st = p.stat()
        got = (hashlib.sha256(p.read_bytes()).hexdigest(),
               st.st_mtime_ns, st.st_size)
        assert got == guard[str(p)], f"封存账被写动：{p}"

    a = dataset["anchors"]
    assert a["phase2_calls"] == 436 and a["phase2_blocked"] == 19
    assert a["by_task"]["6"] == [210, 195, 15, 25, 234]
    assert a["by_task"]["7"] == [74, 51, 23, 235, 308]
    assert a["by_task"]["8"] == [128, 123, 5, 309, 436]
    assert a["by_stage"] == {"macro_ideation": 14, "coding_debug": 398,
                             "visual_rollout": 24}
    assert a["blocked_by_task"] == {"6": 10, "7": 7, "8": 2}

    # 墙 A：三级 2/13/45，confirmed 恰 {254,308}
    wa = dataset["phase2"]["wall_a"]
    assert wa["sA"] == 60
    assert wa["sA_breakdown"]["total"] == 60
    assert (wa["byte_confirmed_count"], wa["byte_unclassified_count"],
            wa["seq_level_overwritten_count"]) == (2, 13, 45)
    assert sorted(e["seq"] for e in wa["byte_confirmed"]) == [254, 308]
    expected_unclassified = {
        252: hb.RULE_DEP, 253: hb.RULE_DEP, 255: hb.RULE_DEP,
        256: hb.RULE_DEP, 261: hb.RULE_LAUNCH, 262: hb.RULE_BENCH,
        263: hb.RULE_BENCH, 264: hb.RULE_BENCH, 265: hb.RULE_ENV_ADJ,
        304: hb.RULE_BDDL, 305: hb.RULE_BDDL, 306: hb.RULE_PATCH,
        307: hb.RULE_LAUNCH}
    assert {e["seq"]: e["rule"]
            for e in wa["byte_unclassified"]} == expected_unclassified
    for e in wa["byte_unclassified"]:
        assert e["evidence"] and e["tail_excerpt"] and e["rule_reason"]
        assert e["refs_to_same_path"][-1] == e["seq"]
    # 45 覆盖：逐条机械可解释（refs→survivor=max，本呼非末写者）
    survivors = {e["seq"] for e in wa["byte_confirmed"]} \
        | {e["seq"] for e in wa["byte_unclassified"]}
    overw = wa["seq_level_overwritten"]
    assert len(overw) == 45
    reconstructed = set()
    for e in overw:
        assert e["rule"] == "UNRECOVERABLE"
        assert e["evidence_level"] == "provenance_limited"
        assert e["survivor_seq"] == max(e["refs"])
        assert e["seq"] in e["refs"] and e["seq"] != e["survivor_seq"]
        assert e["survivor_seq"] in survivors
        assert f"存活字节属 seq {e['survivor_seq']}" in e["reason"]
        reconstructed.update(set(e["refs"]) - {e["survivor_seq"]})
    assert reconstructed == {e["seq"] for e in overw}
    assert not (reconstructed & survivors)
    # a/c 六呼结构排除 + 对称规则码
    excluded = {e["seq"]: e["rule"] for e in wa["excluded_per_seq"]}
    assert excluded == {237: hb.RULE_LAUNCH, 238: hb.RULE_DEP,
                        239: hb.RULE_DEP, 240: hb.RULE_DEP,
                        241: hb.RULE_DEP, 259: hb.RULE_LAUNCH}

    # 墙 B（C-1 断言原样保留）：30/9、39 条、冻结件与 build 一致
    assert seq_split["sB_eng"] == 30 and seq_split["sB_cap"] == 9
    assert len(seq_split["entries"]) == 39
    frozen = json.loads((rp.phase25_root() / "walls" / "wall_b"
                         / "SEQ_SPLIT.json").read_text())
    assert frozen["entries"] == seq_split["entries"]

    # phase25 新账必须仍为 0 行（mtime 守卫之外再查内容）
    ledger = rp.phase25_ledger_path()
    assert ledger.read_text().strip() == ""
