#!/usr/bin/env python3
"""Phase 2.5 Task6 Stage B（r4）— 窗口工程证据机产器单元测试（hermetic）。

``scripts/phase25/gen_task6_r4_window_evidence.py`` 是预注册 §7.1 白名单里的
**门 2 取证脚本**：它产出的 json 直接被 ``task6_r4_verdict.py`` 读作 §8 条件
4/5 的判据，所以它自己必须被测——尤其 ``_write_surface`` 的「零写入」判别，
写错就会把写入污染报成干净。

本文件全 hermetic：不连活库、不跑 docker/nvidia-smi、不联网、0 付费。
活库真值由 ``test_task6_r4_live.py``（kb_integration）与窗口实跑覆盖。
"""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

ev_mod = importlib.import_module(
    "scripts.phase25.gen_task6_r4_window_evidence")

TABLES = ("kb_chunks", "solution_chunks", "phase1_learnings",
          "phase2_learnings")


def _row(rid: str, *, text_sha="T", meta="M", noprov="N",
         ingested="IA", source_commit="SC") -> dict:
    """快照逐行节点（形状对齐 `gen_prewindow_snapshots.table_snapshot`）。

    `ingested_at_sha256`/`source_commit_sha256` 是分列戳记哈希：口径管委会
    （GATE1 规格 lane MAJOR-1）只有「ingested_at 变、source_commit 不变」才算
    kbint 门授权例外，缺这两个键就无法区分，故与本行节点同形给全。
    """
    return {"id": rid, "text_sha256": text_sha, "meta_sha256": meta,
            "meta_noprov_sha256": noprov,
            "ingested_at_sha256": ingested,
            "source_commit_sha256": source_commit}


def _snap(tables_payload: dict, recalls: dict, schema=None) -> dict:
    tables = {}
    for name, per_row in tables_payload.items():
        tables[name] = {
            "rows": len(per_row),
            "id_set_sha256": "ids-" + name,
            "prov_cols_present": (["ingested_at", "source_commit"]
                                  if name == "solution_chunks" else []),
            "per_row": per_row,
            "missing": False,
        }
    return {"schema": schema or "simpraxis.phase25.embedswap_prewindow.v1",
            "header": {"uri": "x", "config_dim": 768},
            "tables": tables,
            "q1q2q3_mpnet_recall": recalls}


def _baseline() -> dict:
    return _snap({name: [_row(f"{name}-1"), _row(f"{name}-2")]
                  for name in TABLES},
                 {"Q1": {"ids": ["a", "b"], "filters": {}},
                  "Q2": {"ids": ["c"], "filters": {}},
                  "Q3": {"ids": ["d"], "filters": {}}})


# --- A. _write_surface：零写入判别 ----------------------------------------- #

def test_identical_snapshots_report_no_writes():
    pre = _baseline()
    post = json.loads(json.dumps(pre))
    got = ev_mod._write_surface(pre, post)
    assert got["unexpected"] == []
    assert got["gate_restamp_rows"] == 0
    assert got["gate_restamp_ingested_at_only_rows"] == 0
    assert got["gate_restamp_other_rows"] == 0


def test_ingested_at_only_restamp_is_gate_exception_not_write():
    """kbint 门幂等重灌只重戳 solution_chunks.ingested_at（§7.3 唯一例外）。"""
    pre = _baseline()
    post = json.loads(json.dumps(pre))
    row = post["tables"]["solution_chunks"]["per_row"][0]
    row["meta_sha256"] = "M2"
    row["ingested_at_sha256"] = "IA2"          # 只有 ingested_at 前进
    got = ev_mod._write_surface(pre, post)
    assert got["unexpected"] == []
    assert got["gate_restamp_rows"] == 1
    assert got["gate_restamp_ingested_at_only_rows"] == 1
    assert got["gate_restamp_other_rows"] == 0


def test_restamp_outside_solution_chunks_is_a_write():
    """别的表的任何戳记变化都不是 §7.3 例外（规格 lane MAJOR-1）。"""
    pre = _baseline()
    post = json.loads(json.dumps(pre))
    row = post["tables"]["kb_chunks"]["per_row"][0]
    row["meta_sha256"] = "M2"
    row["ingested_at_sha256"] = "IA2"
    got = ev_mod._write_surface(pre, post)
    assert len(got["unexpected"]) == 1
    assert got["unexpected"][0]["table"] == "kb_chunks"
    assert got["gate_restamp_rows"] == 0
    assert got["gate_restamp_ingested_at_only_rows"] == 0
    assert got["gate_restamp_other_rows"] == 1


def test_solution_source_commit_change_is_a_write():
    """solution_chunks 的 source_commit 变化 = 写入（§7.3 只授权 ingested_at）。"""
    pre = _baseline()
    post = json.loads(json.dumps(pre))
    row = post["tables"]["solution_chunks"]["per_row"][0]
    row["meta_sha256"] = "M2"
    row["source_commit_sha256"] = "SC2"
    got = ev_mod._write_surface(pre, post)
    assert len(got["unexpected"]) == 1
    assert "非授权戳记变化" in got["unexpected"][0]["reason"]
    assert got["gate_restamp_rows"] == 0


def test_solution_restamp_without_split_hashes_is_a_write():
    """分列戳记哈希缺失 → 无法证明是 ingested_at，fail-closed 按写入算。"""
    pre = _baseline()
    post = json.loads(json.dumps(pre))
    for snap in (pre, post):
        snap["tables"]["solution_chunks"]["per_row"][0].pop(
            "source_commit_sha256")
    post["tables"]["solution_chunks"]["per_row"][0]["meta_sha256"] = "M2"
    got = ev_mod._write_surface(pre, post)
    assert len(got["unexpected"]) == 1
    assert got["gate_restamp_rows"] == 0


def test_text_change_is_a_write_even_without_provenance_shift():
    pre = _baseline()
    post = json.loads(json.dumps(pre))
    post["tables"]["kb_chunks"]["per_row"][0]["text_sha256"] = "T2"
    got = ev_mod._write_surface(pre, post)
    assert len(got["unexpected"]) == 1
    assert got["unexpected"][0]["table"] == "kb_chunks"
    assert got["unexpected"][0]["id"] == "kb_chunks-1"


def test_non_provenance_metadata_change_is_a_write():
    pre = _baseline()
    post = json.loads(json.dumps(pre))
    post["tables"]["phase1_learnings"]["per_row"][1]["meta_noprov_sha256"] = "N2"
    got = ev_mod._write_surface(pre, post)
    assert len(got["unexpected"]) == 1
    assert "非戳记元数据" in got["unexpected"][0]["reason"]


def test_row_count_or_id_set_change_is_a_write():
    pre = _baseline()
    post = json.loads(json.dumps(pre))
    node = post["tables"]["phase2_learnings"]
    node["per_row"].append(_row("phase2_learnings-3"))
    node["rows"] = 3
    node["id_set_sha256"] = "ids-phase2_learnings-3"
    got = ev_mod._write_surface(pre, post)
    assert len(got["unexpected"]) == 1
    assert got["unexpected"][0]["reason"] == "行数或 id 集变化"

    post2 = json.loads(json.dumps(pre))
    post2["tables"]["kb_chunks"]["id_set_sha256"] = "other"
    assert len(ev_mod._write_surface(pre, post2)["unexpected"]) == 1


def test_per_row_only_drift_without_count_change_is_still_a_write():
    """只多出一行却不改 rows/id_set_sha —— 一样判污染，不许蒙混。"""
    pre = _baseline()
    post = json.loads(json.dumps(pre))
    post["tables"]["phase2_learnings"]["per_row"].append(
        _row("phase2_learnings-3"))
    got = ev_mod._write_surface(pre, post)
    assert len(got["unexpected"]) == 1
    assert got["unexpected"][0]["reason"] == "per_row id 集不齐"


def test_missing_table_in_post_is_an_evidence_error():
    """任一侧缺表 = 观测缺口，不是"没写入"（GATE1-LANE-A M6）。"""
    pre = _baseline()
    post = _baseline()
    del post["tables"]["solution_chunks"]
    with pytest.raises(ev_mod.EvidenceError, match="solution_chunks"):
        ev_mod._write_surface(pre, post)


def test_table_missing_on_both_sides_is_an_evidence_error():
    """两侧都缺表时旧实现判"干净"——那是把观测缺口当零写入，必须报错。"""
    pre = _baseline()
    post = _baseline()
    for snap in (pre, post):
        snap["tables"]["phase2_learnings"] = {
            "rows": None, "id_set_sha256": None, "missing": True}
    with pytest.raises(ev_mod.EvidenceError, match="phase2_learnings"):
        ev_mod._write_surface(pre, post)


def test_table_node_without_per_row_is_an_evidence_error():
    """节点残缺（无 per_row）同属观测缺口——空 per_row 集合恒等不是证据。"""
    pre = _baseline()
    post = _baseline()
    pre["tables"]["kb_chunks"].pop("per_row")
    post["tables"]["kb_chunks"].pop("per_row")
    with pytest.raises(ev_mod.EvidenceError, match="kb_chunks"):
        ev_mod._write_surface(pre, post)


def test_table_node_with_none_values_is_an_evidence_error():
    """键在而值为 None 的节点曾让 `None == None` / `set()==set()` 恒成立。

    生成器缺表时给 `{rows: None, id_set_sha256: None, missing: True}`——靠
    `missing` 能拦；但残缺节点（例如被截断写出）可能只留 None 值，那时旧实现
    会把「没观测到」判成「没写入」（GATE1-LANE-A M6/D10）。只认完整观测。
    """
    pre = _baseline()
    post = _baseline()
    for snap in (pre, post):
        snap["tables"]["phase1_learnings"] = {
            "rows": None, "id_set_sha256": None, "per_row": []}
    with pytest.raises(ev_mod.EvidenceError, match="phase1_learnings"):
        ev_mod._write_surface(pre, post)


def test_table_node_with_empty_id_set_sha_is_an_evidence_error():
    """空串 id 集 sha 不是身份（等同于未观测）。"""
    pre = _baseline()
    post = _baseline()
    pre["tables"]["kb_chunks"]["id_set_sha256"] = ""
    post["tables"]["kb_chunks"]["id_set_sha256"] = ""
    with pytest.raises(ev_mod.EvidenceError, match="kb_chunks"):
        ev_mod._write_surface(pre, post)


# --- B. _q1q2q3：有序 13 id 集恒等 ----------------------------------------- #

def test_q1q2q3_ok_when_live_matches_prewindow(monkeypatch):
    pre = _baseline()
    monkeypatch.setattr(
        "scripts.kb.gen_prewindow_snapshots.recall_snapshot",
        lambda uri: {k: {"ids": list(v["ids"])}
                     for k, v in pre["q1q2q3_mpnet_recall"].items()})
    got = ev_mod._q1q2q3("x", pre)
    assert got["ok"] is True
    assert got["n_queries"] == 3
    assert got["mismatches"] == []


def test_q1q2q3_detects_order_change_and_extra_query(monkeypatch):
    pre = _baseline()

    def _live(uri):
        return {"Q1": {"ids": ["b", "a"]},          # 顺序变了
                "Q2": {"ids": ["c"]},
                "Q3": {"ids": ["d"]},
                "Q4": {"ids": ["e"]}}               # 多出一条查询

    monkeypatch.setattr("scripts.kb.gen_prewindow_snapshots.recall_snapshot",
                        _live)
    got = ev_mod._q1q2q3("x", pre)
    assert got["ok"] is False
    assert [m["query"] for m in got["mismatches"]] == ["Q1"]
    assert got["unexpected_queries"] == ["Q4"]


def test_q1q2q3_requires_prewindow_recall_block():
    pre = _baseline()
    pre["q1q2q3_mpnet_recall"] = {}
    with pytest.raises(ev_mod.EvidenceError):
        ev_mod._q1q2q3("x", pre)


def test_q1q2q3_rejects_empty_expected_ids(monkeypatch):
    """`[] == []` 恒等不算身份证据（laneA L8）：窗口前召回为空即报错。"""
    pre = _baseline()
    pre["q1q2q3_mpnet_recall"]["Q2"]["ids"] = []
    monkeypatch.setattr(
        "scripts.kb.gen_prewindow_snapshots.recall_snapshot",
        lambda uri: {"Q1": {"ids": ["a", "b"]}, "Q2": {"ids": []},
                     "Q3": {"ids": ["d"]}})
    with pytest.raises(ev_mod.EvidenceError, match="Q2"):
        ev_mod._q1q2q3("x", pre)


# --- C. ledger / build 装配 ----------------------------------------------- #

def test_ledger_rows_counts_only_non_empty_lines(monkeypatch, tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text('{"seq": 1}\n\n{"seq": 2}\n   \n', encoding="utf-8")
    monkeypatch.setattr(
        "orchestrator.common.runtime_paths.phase25_ledger_path",
        lambda: ledger)
    assert ev_mod._ledger_rows() == 2


def test_ledger_rows_missing_file_is_evidence_error(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "orchestrator.common.runtime_paths.phase25_ledger_path",
        lambda: tmp_path / "nope.jsonl")
    with pytest.raises(ev_mod.EvidenceError):
        ev_mod._ledger_rows()


def _stub_live(monkeypatch, *, ledger=56, containers=0, vram=14):
    rows = {"kb_chunks": 1542, "solution_chunks": 94,
            "phase1_learnings": 44, "phase2_learnings": 1}
    monkeypatch.setattr(ev_mod, "_live_tables", lambda uri: {
        "rows": dict(rows),
        "dims": {k: 768 for k in rows},
        "id_sha8": {k: "deadbeef" for k in rows}})
    monkeypatch.setattr(ev_mod, "_q1q2q3", lambda uri, pre: {"ok": True})
    monkeypatch.setattr(ev_mod, "_ledger_rows", lambda: ledger)
    monkeypatch.setattr(ev_mod, "_containers_present", lambda: containers)
    monkeypatch.setattr(ev_mod, "_vram_mib", lambda: vram)
    monkeypatch.setattr(ev_mod, "_vector_identity", lambda path, uri: {
        "observed": True, "unchanged": True, "mismatches": [],
        "tables": {}})


def _write_snaps(tmp_path, pre, post):
    a, b = tmp_path / "pre.json", tmp_path / "post.json"
    a.write_text(json.dumps(pre), encoding="utf-8")
    b.write_text(json.dumps(post), encoding="utf-8")
    return a, b


_KBINT_OK_LOG = "1234 passed, 7 skipped, 50 deselected in 11.2s\n"
_KBINT_BAD_LOG = "1 failed, 1233 passed in 11.2s\n"
_GATE_OK_LOG = "== [gate 1/6] ... ==\n[STATIC GATE OK]\n"


def _write_log(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return str(p)


def _args(tmp_path, pre, post, **kw):
    import argparse
    a, b = _write_snaps(tmp_path, pre, post)
    return argparse.Namespace(
        prewindow=str(a), postwindow=str(b), out=None,
        pre_vector=kw.get("pre_vector"),
        hermetic_rc=kw.get("hermetic_rc", 0),
        kbint_log=kw.get("kbint_log",
                         _write_log(tmp_path, "kbint.log", _KBINT_OK_LOG)),
        gate_static_log=kw.get("gate_static_log",
                               _write_log(tmp_path, "gate.log", _GATE_OK_LOG)),
        provenance_restore=kw.get("provenance_restore", True))


def test_build_emits_exactly_the_keys_the_verdict_reads(monkeypatch, tmp_path):
    """键名必须与 task6_r4_verdict.check_data_identity/check_engineering 同形。"""
    _stub_live(monkeypatch)
    out = ev_mod.build(_args(tmp_path, _baseline(), _baseline()))
    for key in ("four_tables", "q1q2q3", "live_writes_except_gate_restamp",
                "ledger_rows", "hermetic", "kbint", "gate_static",
                "containers_present", "vram_mib", "provenance_restore",
                "write_surface", "vector_identity"):
        assert key in out, key
    assert out["live_writes_except_gate_restamp"] == 0
    assert out["ledger_rows"] == 56
    assert out["hermetic"] == {"rc": 0, "ok": True}
    # 门禁绑实跑工件（A10-M1）：kbint 带解析出的 rc 与日志 sha，gate_static 带标记
    assert out["kbint"]["rc"] == 0 and out["kbint"]["ok"] is True
    assert len(out["kbint"]["log_sha256"]) == 64
    assert "passed" in out["kbint"]["summary"]
    assert out["gate_static"]["ok"] is True
    assert len(out["gate_static"]["log_sha256"]) == 64
    assert out["provenance_restore"] is True
    # 只有 provenance_restore 仍是声明项（其余三项已绑工件）
    assert out["declared_by_executor"] == ["provenance_restore"]


def test_build_rejects_foreign_schema_before_touching_live_db(
        monkeypatch, tmp_path):
    def _boom(uri):
        raise AssertionError("schema 不符时不得连活库")

    monkeypatch.setattr(ev_mod, "_live_tables", _boom)
    pre = _baseline()
    post = _baseline()
    post["schema"] = "simpraxis.something_else.v1"
    with pytest.raises(ev_mod.EvidenceError):
        ev_mod.build(_args(tmp_path, pre, post))


def test_gate_flags_are_read_from_artifacts_not_defaulted_true(monkeypatch,
                                                               tmp_path):
    """门禁不认任何"声明通过"：rc/日志说什么就是什么，缺省也不得为真。"""
    _stub_live(monkeypatch)
    out = ev_mod.build(_args(
        tmp_path, _baseline(), _baseline(),
        hermetic_rc=1,
        kbint_log=_write_log(tmp_path, "bad.log", _KBINT_BAD_LOG),
        gate_static_log=_write_log(tmp_path, "g.log", "no marker here\n"),
        provenance_restore=False))
    assert out["hermetic"]["ok"] is False        # 由 rc==1 推出
    assert out["kbint"]["ok"] is False           # 由摘要里的 failed 推出
    assert out["gate_static"]["ok"] is False     # 缺标记
    assert out["provenance_restore"] is False    # 仍是声明项


# --- D. CLI 契约 ----------------------------------------------------------- #

def test_cli_requires_hermetic_rc():
    with pytest.raises(SystemExit) as exc:
        ev_mod.main(["--prewindow", "a", "--postwindow", "b"])
    assert exc.value.code == 2


def test_cli_exit_2_on_bad_input(tmp_path, capsys):
    pre = tmp_path / "pre.json"
    pre.write_text("{not json", encoding="utf-8")
    post = tmp_path / "post.json"
    post.write_text(json.dumps(_baseline()), encoding="utf-8")
    rc = ev_mod.main(["--prewindow", str(pre), "--postwindow", str(post),
                      "--hermetic-rc", "0",
                      "--kbint-log", _write_log(tmp_path, "k.log", _KBINT_OK_LOG),
                      "--gate-static-log",
                      _write_log(tmp_path, "g.log", _GATE_OK_LOG)])
    assert rc == 2
    assert "[T6R4-EVIDENCE][STOP]" in capsys.readouterr().err


def test_cli_writes_deterministic_json(monkeypatch, tmp_path):
    _stub_live(monkeypatch)
    args = _args(tmp_path, _baseline(), _baseline())
    out1, out2 = tmp_path / "e1.json", tmp_path / "e2.json"
    for path in (out1, out2):
        rc = ev_mod.main(["--prewindow", args.prewindow,
                          "--postwindow", args.postwindow,
                          "--hermetic-rc", "0",
                          "--kbint-log", args.kbint_log,
                          "--gate-static-log", args.gate_static_log,
                          "--pre-vector", "ignored-because-stubbed",
                          "--provenance-restore", "--out", str(path)])
        assert rc == 0
    assert out1.read_bytes() == out2.read_bytes()
    text = out1.read_text(encoding="utf-8")
    assert text.endswith("}\n") and not text.endswith("}\n\n")
    data = json.loads(text)
    assert data["schema"] == ev_mod.SCHEMA


# ---------- E：容器计数（GATE1-LANE-A C2——真实过滤器不得恒零） ----------

def test_simpraxis_container_filter_matches_real_compose_name():
    """真实容器名是 `sim_praxis_sandbox`（下划线），旧子串法对它恒 False。

    `sandbox/docker-compose.yml` 明写 `container_name: sim_praxis_sandbox`；
    旧实现 `"simpraxis" in name.lower()` 在有容器在跑时也返回 0，等于把
    "零容器收尾"这条证据变成永远成立的空断言（硬规则 7）。
    """
    raw = "sim_praxis_sandbox\n"
    assert ev_mod._simpraxis_container_names(raw) == ["sim_praxis_sandbox"]


@pytest.mark.parametrize("name", [
    "sim_praxis_sandbox",          # compose 实名（下划线）
    "simpraxis-sandbox",           # 连字符变体
    "SIM.PRAXIS.sandbox",          # 大小写 + 点号变体
    "my_simpraxis_worker",         # 前后缀
])
def test_simpraxis_container_filter_accepts_all_separators(name):
    assert ev_mod._simpraxis_container_names(f"{name}\n") == [name]


@pytest.mark.parametrize("name", [
    "alpasim_agent",               # 他方容器（/mnt/venvs 同族，不得误伤）
    "run_a15_batch30",             # 他方批处理
    "postgres",
])
def test_simpraxis_container_filter_rejects_foreign(name):
    assert ev_mod._simpraxis_container_names(f"{name}\n") == []


def test_simpraxis_container_filter_ignores_blank_lines():
    assert ev_mod._simpraxis_container_names("\n  \nsim_praxis_sandbox\n\n") \
        == ["sim_praxis_sandbox"]


def test_containers_present_counts_through_real_docker_output(monkeypatch):
    """端到端：只 monkeypatch `subprocess.run`，走真实过滤器与计数。"""
    class _Done:
        stdout = "sim_praxis_sandbox\nalpasim_agent\n\n"

    def _fake_run(cmd, **kw):
        # 必须带 -a：已退出/created 的容器同样算残留（硬规则 5）
        assert cmd[:3] == ["docker", "ps", "-a"], cmd
        return _Done()

    monkeypatch.setattr(ev_mod.subprocess, "run", _fake_run)
    assert ev_mod._containers_present() == 1


def test_containers_present_counts_exited_containers(monkeypatch):
    """已退出容器（`docker ps -a` 可见、裸 `ps` 不可见）必须计入残留。"""
    class _Done:
        stdout = "sim_praxis_sandbox\n"

    monkeypatch.setattr(ev_mod.subprocess, "run", lambda cmd, **kw: _Done())
    assert ev_mod._containers_present() == 1


def test_containers_present_zero_when_only_foreign_containers(monkeypatch):
    class _Done:
        stdout = "alpasim_agent\nrun_a15_batch30\n"

    monkeypatch.setattr(ev_mod.subprocess, "run", lambda cmd, **kw: _Done())
    assert ev_mod._containers_present() == 0


def test_containers_present_docker_failure_is_evidence_error(monkeypatch):
    def _boom(cmd, **kw):
        raise OSError("no docker")

    monkeypatch.setattr(ev_mod.subprocess, "run", _boom)
    with pytest.raises(ev_mod.EvidenceError):
        ev_mod._containers_present()


# ---------- F：显存读数（GATE1-LANE-A L10——空输出不得记 0） ----------

def test_vram_sums_valid_gpu_lines(monkeypatch):
    class _Done:
        stdout = "14\n0\n\n"

    monkeypatch.setattr(ev_mod.subprocess, "run", lambda cmd, **kw: _Done())
    assert ev_mod._vram_mib() == 14


def test_vram_empty_output_is_an_evidence_error(monkeypatch):
    """空 stdout 时 `sum([]) == 0` 曾让显存条件恒过——必须报错。"""
    class _Done:
        stdout = "\n  \n"

    monkeypatch.setattr(ev_mod.subprocess, "run", lambda cmd, **kw: _Done())
    with pytest.raises(ev_mod.EvidenceError, match="为空"):
        ev_mod._vram_mib()


def test_vram_non_numeric_output_is_an_evidence_error(monkeypatch):
    class _Done:
        stdout = "N/A\n"

    monkeypatch.setattr(ev_mod.subprocess, "run", lambda cmd, **kw: _Done())
    with pytest.raises(ev_mod.EvidenceError, match="非数值"):
        ev_mod._vram_mib()


def test_vram_negative_reading_is_an_evidence_error(monkeypatch):
    """负值是异常读数（`-5` ≤200 会让判据恒过），按未测处理。"""
    class _Done:
        stdout = "-5\n"

    monkeypatch.setattr(ev_mod.subprocess, "run", lambda cmd, **kw: _Done())
    with pytest.raises(ev_mod.EvidenceError, match="负值"):
        ev_mod._vram_mib()


def test_vram_command_failure_is_an_evidence_error(monkeypatch):
    def _boom(cmd, **kw):
        raise OSError("no nvidia-smi")

    monkeypatch.setattr(ev_mod.subprocess, "run", _boom)
    with pytest.raises(ev_mod.EvidenceError):
        ev_mod._vram_mib()


# ---------- G：向量身份（GATE1-LANE-A C-1——同维重嵌入不得隐身） ----------

VEC_TABLES = ("kb_chunks", "solution_chunks", "phase1_learnings",
              "phase2_learnings")


def _vec_baseline(tmp_path, hashes=None):
    mod = importlib.import_module("scripts.phase25.gen_task6_r4_vector_identity")
    hashes = hashes or {n: f"sha-{n}" for n in VEC_TABLES}
    path = tmp_path / "pre_vector.json"
    path.write_text(json.dumps({
        "schema": mod.SCHEMA,
        "tables": {n: {"rows": 1, "dim": 768, "vector_set_sha256": h}
                   for n, h in hashes.items()},
        "total_rows": 4}), encoding="utf-8")
    return str(path)


def _stub_vec_live(monkeypatch, hashes=None):
    mod = importlib.import_module("scripts.phase25.gen_task6_r4_vector_identity")
    hashes = hashes or {n: f"sha-{n}" for n in VEC_TABLES}
    monkeypatch.setattr(mod, "build", lambda uri=None: {
        "schema": mod.SCHEMA,
        "tables": {n: {"rows": 1, "dim": 768, "vector_set_sha256": h}
                   for n, h in hashes.items()},
        "total_rows": 4})


def test_vector_identity_ok_when_hashes_match(monkeypatch, tmp_path):
    _stub_vec_live(monkeypatch)
    got = ev_mod._vector_identity(_vec_baseline(tmp_path), "x")
    assert got["observed"] is True
    assert got["unchanged"] is True
    assert got["mismatches"] == []


def test_vector_identity_detects_same_dim_reembed(monkeypatch, tmp_path):
    """只换向量（行数/id/维度全同）也必须被抓到——这正是 C-1 的场景。"""
    changed = {n: f"sha-{n}" for n in VEC_TABLES}
    changed["solution_chunks"] = "other-sha"
    _stub_vec_live(monkeypatch, changed)
    got = ev_mod._vector_identity(_vec_baseline(tmp_path), "x")
    assert got["observed"] is True
    assert got["unchanged"] is False
    assert [m["table"] for m in got["mismatches"]] == ["solution_chunks"]


def test_vector_identity_requires_baseline_path():
    with pytest.raises(ev_mod.EvidenceError, match="pre-vector"):
        ev_mod._vector_identity(None, "x")


def test_vector_identity_rejects_foreign_schema(monkeypatch, tmp_path):
    mod = importlib.import_module("scripts.phase25.gen_task6_r4_vector_identity")
    path = tmp_path / "v.json"
    path.write_text(json.dumps({"schema": "nope", "tables": {}}),
                    encoding="utf-8")
    with pytest.raises(ev_mod.EvidenceError, match=mod.SCHEMA):
        ev_mod._vector_identity(str(path), "x")


def test_vector_identity_rejects_baseline_missing_a_table_hash(tmp_path):
    baseline = _vec_baseline(tmp_path)
    data = json.loads(open(baseline, encoding="utf-8").read())
    data["tables"]["phase2_learnings"]["vector_set_sha256"] = None
    open(baseline, "w", encoding="utf-8").write(json.dumps(data))
    with pytest.raises(ev_mod.EvidenceError, match="phase2_learnings"):
        ev_mod._vector_identity(baseline, "x")


# ---------- H：门禁绑实跑工件（GATE1-LANE-A A10-M1） ----------

def test_kbint_log_with_failure_parses_rc_1(monkeypatch, tmp_path):
    """日志里有 failed → rc=1；判分器据此判负（不再有裸 ok 布尔可传）。"""
    _stub_live(monkeypatch)
    args = _args(tmp_path, _baseline(), _baseline(),
                 kbint_log=_write_log(tmp_path, "bad.log", _KBINT_BAD_LOG))
    out = ev_mod.build(args)
    assert out["kbint"]["rc"] == 1 and out["kbint"]["ok"] is False


def test_kbint_log_without_pytest_summary_is_evidence_error(monkeypatch,
                                                            tmp_path):
    """日志里没有摘要行 = 没跑过（或被截断），不得写成"跑过了"。"""
    args = _args(tmp_path, _baseline(), _baseline(),
                 kbint_log=_write_log(tmp_path, "empty.log", "collected 0 items\n"))
    with pytest.raises(ev_mod.EvidenceError, match="摘要行"):
        ev_mod.build(args)


def test_kbint_log_missing_file_is_evidence_error(tmp_path):
    args = _args(tmp_path, _baseline(), _baseline(),
                 kbint_log=str(tmp_path / "nope.log"))
    with pytest.raises(ev_mod.EvidenceError, match="读不了"):
        ev_mod.build(args)


def test_gate_static_log_without_ok_marker_is_not_ok(monkeypatch, tmp_path):
    _stub_live(monkeypatch)
    args = _args(tmp_path, _baseline(), _baseline(),
                 gate_static_log=_write_log(tmp_path, "g.log", "[STATIC GATE FAIL]\n"))
    out = ev_mod.build(args)
    assert out["gate_static"]["ok"] is False


def test_pytest_summary_pick_ignores_noise_lines():
    """摘要行只认「结果词 + 耗时」结尾的那行；告警里的 passed 不算。"""
    text = ("tests/x.py::test_a PASSED\n"
            "WARNING: something passed through\n"
            "3 passed, 1 skipped in 2.5s (0:00:02)\n"
            "\n")
    got = ev_mod._pytest_summary_rc(text, "t")
    assert got["rc"] == 0
    assert got["summary"].endswith("(0:00:02)")


# ---------- I：ledger seq 连续性（A10-L3） ----------

def _write_ledger(tmp_path, seqs):
    p = tmp_path / "ledger.jsonl"
    p.write_text("".join(json.dumps({"seq": s, "ok": True}) + "\n" for s in seqs),
                 encoding="utf-8")
    return p


def test_ledger_rows_accepts_contiguous_seq(monkeypatch, tmp_path):
    p = _write_ledger(tmp_path, range(1, 57))
    monkeypatch.setattr(
        "orchestrator.common.runtime_paths.phase25_ledger_path", lambda: p)
    assert ev_mod._ledger_rows() == 56


def test_ledger_rows_rejects_deleted_row_with_same_count(monkeypatch, tmp_path):
    """删一行再补一行：行数仍 56，但 seq 断档——这正是旧实现的盲区。"""
    seqs = list(range(1, 57))
    seqs[10] = 999
    p = _write_ledger(tmp_path, seqs)
    monkeypatch.setattr(
        "orchestrator.common.runtime_paths.phase25_ledger_path", lambda: p)
    with pytest.raises(ev_mod.EvidenceError, match="连续"):
        ev_mod._ledger_rows()


def test_ledger_rows_rejects_row_without_seq(monkeypatch, tmp_path):
    p = tmp_path / "ledger.jsonl"
    p.write_text(json.dumps({"ok": True}) + "\n", encoding="utf-8")
    monkeypatch.setattr(
        "orchestrator.common.runtime_paths.phase25_ledger_path", lambda: p)
    with pytest.raises(ev_mod.EvidenceError, match="seq"):
        ev_mod._ledger_rows()


# ---------- J：per_row 行形状（A10-L4） ----------

def test_per_row_row_without_id_is_evidence_error():
    """旧实现直取 r["id"] → KeyError（rc=1 traceback），不是输入层错误。"""
    pre = _baseline()
    post = _baseline()
    pre["tables"]["kb_chunks"]["per_row"].append({"text_sha256": "x"})
    post["tables"]["kb_chunks"]["per_row"].append({"text_sha256": "x"})
    with pytest.raises(ev_mod.EvidenceError, match="per_row"):
        ev_mod._write_surface(pre, post)


def test_per_row_non_object_row_is_evidence_error():
    pre = _baseline()
    post = _baseline()
    pre["tables"]["solution_chunks"]["per_row"].append("not-a-row")
    post["tables"]["solution_chunks"]["per_row"].append("not-a-row")
    with pytest.raises(ev_mod.EvidenceError, match="per_row"):
        ev_mod._write_surface(pre, post)
