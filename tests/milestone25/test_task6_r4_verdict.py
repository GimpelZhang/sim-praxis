#!/usr/bin/env python3
"""Phase 2.5 Task6 Stage B（r4）— 保留判据机判器单元测试（hermetic）。

``scripts/phase25/task6_r4_verdict.py`` 是预注册 §7.2 的机判件：它读 iter3/
iter5/点预测/工程证据四份只读 JSON，输出 `retain`。它写错就等于「判据放水」，
故本文件专门压它的**fail-closed 面**（GATE1 规格 lane MAJOR-2）：

- 工程证据缺键/为 null 必须判**负**（原实现 `not in (0, None)` 把缺数据当通过）；
- `--evidence` 必须按 schema 校验，形状不符是 exit 2（输入不可用），不是判据；
- 五条件与点预测闸的合取逻辑本身。

全 hermetic：不连库、不跑 docker/nvidia-smi、不联网、0 付费。
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

vd = importlib.import_module("scripts.phase25.task6_r4_verdict")


# ---------------------------------------------------------------- 构造件

def _case(cid, *, hit, rank=None, matched_by=None):
    return {"id": cid, "track": "track2", "tier": "probe", "route": "kb",
            "hit": hit, "rank": rank, "matched_by": matched_by,
            "top8_ids": [cid] if hit else []}


def _metrics(*, all_pass=True, fail=()):
    def _m(route, hits, total):
        bad = route in fail
        return {"hits": hits, "total": total, "gold_at_8": hits / total,
                "threshold": 0.75, "pass": (not bad) and all_pass}

    return {"overall": {"hits": 44, "total": 47, "gold_at_8": 44 / 47,
                        "threshold": 0.80,
                        "pass": all_pass and "overall" not in fail},
            "solution_route": _m("solution_route", 17, 18),
            "learning_route": _m("learning_route", 12, 12),
            "kb_route_observed": _m("kb_route_observed", 15, 17),
            "all_pass": all_pass}


def _iter3(*, n_hit=38, n_miss=9):
    """r3 基准：38 命中 + 9 残留（总 47，顺序无关）。"""
    cases = [_case(f"h{i}", hit=True, rank=1, matched_by="gold_id")
             for i in range(n_hit)]
    cases += [_case(f"m{i}", hit=False) for i in range(n_miss)]
    return {"schema": vd.EVAL_SCHEMA, "iteration": 3, "rules_version": "r3",
            "metrics": _metrics(), "cases": cases}


def _iter5(*, recovered_gold_id=5, recovered_gold_text=1, miss_total=9,
           flip_ids=(), onward_hits=38):
    """r4 绑定报告：残留 9 案里回收 N（直中/兄弟块）、其余仍失；可注入翻失。"""
    still = miss_total - recovered_gold_id - recovered_gold_text
    assert still >= 0
    cases = []
    for i in range(onward_hits):
        if f"h{i}" in flip_ids:
            cases.append(_case(f"h{i}", hit=False))
        else:
            cases.append(_case(f"h{i}", hit=True, rank=1,
                               matched_by="gold_id"))
    for i in range(recovered_gold_id):
        cases.append(_case(f"m{i}", hit=True, rank=2, matched_by="gold_id"))
    for i in range(recovered_gold_id, recovered_gold_id + recovered_gold_text):
        cases.append(_case(f"m{i}", hit=True, rank=3,
                           matched_by="gold_text"))
    for i in range(recovered_gold_id + recovered_gold_text,
                   recovered_gold_id + recovered_gold_text + still):
        cases.append(_case(f"m{i}", hit=False))
    out = {"schema": vd.EVAL_SCHEMA, "iteration": 5, "rules_version": "r4",
           "metrics": _metrics(), "cases": cases}
    pred = {"schema": vd.PRED_SCHEMA,
            "cases": [{"id": c["id"],
                       "fused_ids": [str(x) for x in c["top8_ids"]]}
                      for c in cases]}
    return out, pred


def _evidence(**over):
    ev = {
        "schema": vd.EVIDENCE_SCHEMA,
        "four_tables": {
            "rows": dict(vd.EXPECTED_ROWS),
            "dims": {k: vd.EXPECTED_DIM for k in vd.EXPECTED_ROWS},
            "id_sha8": dict(vd.EXPECTED_ID_SHA8)},
        "q1q2q3": {"ok": True},
        "live_writes_except_gate_restamp": 0,
        "write_surface": {"unexpected": [], "gate_restamp_rows": 0,
                          "gate_restamp_ingested_at_only_rows": 0,
                          "gate_restamp_other_rows": 0},
        "vector_identity": {"observed": True, "unchanged": True,
                            "mismatches": [], "tables": {}},
        "ledger_rows": vd.EXPECTED_LEDGER_ROWS,
        "hermetic": {"rc": 0, "ok": True},
        "kbint": {"ok": True, "rc": 0},
        "gate_static": {"ok": True},
        "containers_present": 0,
        "vram_mib": 14,
        "provenance_restore": True,
    }
    ev.update(over)
    return ev


# ------------------------------------------------- E1 工程条件 fail-closed

def test_engineering_gate_declarations_are_not_defaulted_true():
    """工程条件只读「执行方声明」与显存/容器；缺声明不得默认通过。"""
    assert vd.check_engineering(_evidence())["ok"] is True
    ev = _evidence()
    for key in ("hermetic", "kbint", "gate_static", "provenance_restore"):
        ev.pop(key)
    assert vd.check_engineering(ev)["ok"] is False


def test_engineering_requires_containers_observed_as_zero():
    for bad in (None, 1, "0"):
        got = vd.check_engineering(_evidence(containers_present=bad))
        assert got["ok"] is False, bad
    ev = _evidence()
    ev.pop("containers_present")
    assert vd.check_engineering(ev)["ok"] is False


def test_engineering_rejects_legacy_containers_running_key():
    """旧键名 `containers_running`（裸 docker ps 口径）不再被当作已观测。"""
    ev = _evidence()
    ev.pop("containers_present")
    ev["containers_running"] = 0
    assert vd.check_engineering(ev)["ok"] is False


def test_engineering_requires_every_gate_declared():
    for key, bad in (("hermetic", {"rc": 0, "ok": False}),
                     ("kbint", {}),
                     ("gate_static", {"ok": False}),
                     ("provenance_restore", False)):
        assert vd.check_engineering(_evidence(**{key: bad}))["ok"] is False


def test_engineering_requires_kbint_rc_not_bare_ok():
    """kbint 门必须绑实跑 rc（A10-M1）：只有 ok 布尔、缺 rc 一律判负。"""
    ev = _evidence()
    ev["kbint"] = {"ok": True}
    got = vd.check_engineering(ev)
    assert got["ok"] is False and any("kbint" in p for p in got["problems"])
    # rc=1 即使 ok 写成 true 也判负
    assert vd.check_engineering(
        _evidence(kbint={"ok": True, "rc": 1}))["ok"] is False


def test_engineering_rejects_negative_vram():
    """负值天然落在上限内，旧实现会放过（A10-L2）。"""
    assert vd.check_engineering(_evidence(vram_mib=-1))["ok"] is False
    assert vd.check_engineering(_evidence(vram_mib=0))["ok"] is True


def test_engineering_ledger_and_vram_bounds():
    assert vd.check_engineering(_evidence(ledger_rows=55))["ok"] is False
    assert vd.check_engineering(_evidence(vram_mib=None))["ok"] is False
    assert vd.check_engineering(_evidence(vram_mib=201))["ok"] is False
    assert vd.check_engineering(_evidence(vram_mib=200))["ok"] is True


# ------------------------------------------------- E2 数据身份 fail-closed

def test_data_identity_requires_writes_observed_as_zero():
    assert vd.check_data_identity(_evidence())["ok"] is True
    for bad in (None, 1):
        got = vd.check_data_identity(_evidence(
            live_writes_except_gate_restamp=bad))
        assert got["ok"] is False, bad


def test_data_identity_pins_authorized_restamp_scope():
    """§7.3 例外的范围要单独钉死：非授权戳记变化计数必须为 0（laneA C3b）。"""
    ev = _evidence()
    ev["write_surface"]["gate_restamp_other_rows"] = 1
    got = vd.check_data_identity(ev)
    assert got["ok"] is False
    assert any("非授权戳记" in p for p in got["problems"])
    # 缺 write_surface = 观测缺口，同样判负
    ev = _evidence()
    ev.pop("write_surface")
    assert vd.check_data_identity(ev)["ok"] is False
    # 键在但为 None 也不得放行
    ev = _evidence()
    ev["write_surface"]["gate_restamp_other_rows"] = None
    assert vd.check_data_identity(ev)["ok"] is False
    # 只有 solution_chunks.ingested_at 重戳（other=0）才允许
    ev = _evidence()
    ev["write_surface"]["gate_restamp_rows"] = 94
    ev["write_surface"]["gate_restamp_ingested_at_only_rows"] = 94
    assert vd.check_data_identity(ev)["ok"] is True


def test_data_identity_requires_observed_vector_identity():
    """向量轴必须被观测且不变（GATE1-LANE-A C-1）。

    快照逐行哈希不含 ``vector`` 列，所以"文本/id 全等"不能推出"嵌入没被重算"。
    缺观测、未观测、或哈希变了，都是条件 4 的失败——不是可选项。
    """
    assert vd.check_data_identity(_evidence())["ok"] is True

    ev = _evidence()
    ev.pop("vector_identity")
    got = vd.check_data_identity(ev)
    assert got["ok"] is False and any("vector_identity" in p
                                      for p in got["problems"])

    ev = _evidence()
    ev["vector_identity"] = {"observed": False}
    assert vd.check_data_identity(ev)["ok"] is False

    ev = _evidence()
    ev["vector_identity"] = {"observed": True, "unchanged": False,
                             "mismatches": [{"table": "solution_chunks"}]}
    got = vd.check_data_identity(ev)
    assert got["ok"] is False and any("向量集合哈希" in p
                                      for p in got["problems"])


def test_data_identity_pins_rows_dims_and_id_sha():
    ev = _evidence()
    ev["four_tables"]["rows"]["kb_chunks"] = 1543
    assert vd.check_data_identity(ev)["ok"] is False
    ev = _evidence()
    ev["four_tables"]["dims"]["solution_chunks"] = 1024
    assert vd.check_data_identity(ev)["ok"] is False
    ev = _evidence()
    ev["four_tables"]["id_sha8"]["phase2_learnings"] = "deadbeef"
    assert vd.check_data_identity(ev)["ok"] is False
    ev = _evidence()
    ev["q1q2q3"] = {"ok": False}
    assert vd.check_data_identity(ev)["ok"] is False


# ------------------------------------------------- E3 科学条件

def test_thresholds_all_pass_gate():
    assert vd.check_thresholds(_iter5()[0])["ok"] is True
    bad = _iter5()[0]
    bad["metrics"] = _metrics(all_pass=False, fail=("solution_route",))
    assert vd.check_thresholds(bad)["ok"] is False


def test_h3_subset_h4_detects_flip_loss():
    i3 = _iter3()
    i5, _ = _iter5()
    assert vd.check_h3_subset_h4(i3, i5)["ok"] is True
    i5_flip, _ = _iter5(flip_ids=("h0",))
    got = vd.check_h3_subset_h4(i3, i5_flip)
    assert got["ok"] is False and got["flip_loss"] == ["h0"]


def test_residual_recovery_needs_six_total_and_five_gold_id():
    i3 = _iter3()
    assert vd.check_residual_recovery(i3, _iter5()[0])["ok"] is True
    # 6 回收但直中只有 4（另 2 走兄弟块）→ 不达标
    got = vd.check_residual_recovery(
        i3, _iter5(recovered_gold_id=4, recovered_gold_text=2)[0])
    assert got["ok"] is False and got["n_gold_id_direct"] == 4
    # 5 回收 → 数量不达标（即使全部直中）
    got = vd.check_residual_recovery(
        i3, _iter5(recovered_gold_id=5, recovered_gold_text=0)[0])
    assert got["ok"] is False and got["n_recovered"] == 5


def test_point_prediction_mismatch_is_detected():
    i5, pred = _iter5()
    assert vd.check_point_prediction(i5, pred)["ok"] is True
    pred["cases"][0]["fused_ids"] = ["other"]
    got = vd.check_point_prediction(i5, pred)
    assert got["ok"] is False and got["mismatches"]


# ------------------------------------------------- E4 输入件契约

def _write(tmp_path, **files):
    out = {}
    for name, payload in files.items():
        p = tmp_path / f"{name}.json"
        p.write_text(json.dumps(payload), encoding="utf-8")
        out[name] = p
    return out


def test_load_inputs_pins_sha_and_schema(tmp_path, monkeypatch):
    paths = _write(tmp_path, iter3=_iter3(), iter5=_iter5()[0],
                   pred=_iter5()[1], ev=_evidence())
    monkeypatch.setattr(vd, "_sha256", lambda p: {
        str(paths["iter3"]): vd.ITER3_SHA256,
        str(paths["pred"]): vd.PREDICTIONS_SHA256}.get(str(p), "0" * 64))
    args = type("A", (), {"iter3": str(paths["iter3"]),
                          "iter5": str(paths["iter5"]),
                          "predictions": str(paths["pred"]),
                          "evidence": str(paths["ev"])})()
    assert vd.load_inputs(args)[1]["iteration"] == 5

    ev_path = paths["ev"]
    ev_path.write_text(json.dumps(_evidence(schema="other.v1")),
                       encoding="utf-8")
    with pytest.raises(vd.InputError, match="schema"):
        vd.load_inputs(args)


def test_load_inputs_rejects_unpinned_iter3_and_pred(tmp_path, monkeypatch):
    paths = _write(tmp_path, iter3=_iter3(), iter5=_iter5()[0],
                   pred=_iter5()[1], ev=_evidence())
    monkeypatch.setattr(vd, "_sha256", lambda p: "0" * 64)
    args = type("A", (), {"iter3": str(paths["iter3"]),
                          "iter5": str(paths["iter5"]),
                          "predictions": str(paths["pred"]),
                          "evidence": str(paths["ev"])})()
    with pytest.raises(vd.InputError):
        vd.load_inputs(args)


def test_load_inputs_pins_iter5_label(tmp_path, monkeypatch):
    i5 = _iter5()[0]
    i5["iteration"] = 4
    paths = _write(tmp_path, iter3=_iter3(), iter5=i5,
                   pred=_iter5()[1], ev=_evidence())
    monkeypatch.setattr(vd, "_sha256", lambda p: {
        str(paths["iter3"]): vd.ITER3_SHA256,
        str(paths["pred"]): vd.PREDICTIONS_SHA256}.get(str(p), "0" * 64))
    args = type("A", (), {"iter3": str(paths["iter3"]),
                          "iter5": str(paths["iter5"]),
                          "predictions": str(paths["pred"]),
                          "evidence": str(paths["ev"])})()
    with pytest.raises(vd.InputError, match="iteration"):
        vd.load_inputs(args)


# ------------------------------------------------- E5 端到端 retain

def _cli_args(tmp_path, **overrides):
    i5, pred = _iter5()
    payloads = dict(iter3=_iter3(), iter5=i5, pred=pred, ev=_evidence())
    payloads.update(overrides)
    paths = _write(tmp_path, **payloads)
    return type("A", (), {"iter3": str(paths["iter3"]),
                          "iter5": str(paths["iter5"]),
                          "predictions": str(paths["pred"]),
                          "evidence": str(paths["ev"]),
                          "out": None})()


def _pin(monkeypatch, args):
    monkeypatch.setattr(vd, "_sha256", lambda p: {
        args.iter3: vd.ITER3_SHA256,
        args.predictions: vd.PREDICTIONS_SHA256}.get(str(p), "0" * 64))


def test_verdict_retains_only_when_all_conditions_and_prediction_hold(
        tmp_path, monkeypatch):
    args = _cli_args(tmp_path)
    _pin(monkeypatch, args)
    out = vd.verdict(args)
    assert out["retain"] is True and out["verdict"] == "RETAIN_R4"
    assert out["point_prediction_match"]["ok"] is True
    assert set(out["conditions"]) == {
        "1_thresholds", "2_h3_subset_h4", "3_residual_recovery",
        "4_data_identity", "5_engineering"}


def test_verdict_negative_when_point_prediction_mismatches(
        tmp_path, monkeypatch):
    """点预测不一致 = 实现没复现冻结规格 → 不得保留（第六道闸）。"""
    i5 = _iter5()[0]
    pred = _iter5()[1]
    pred["cases"][0]["fused_ids"] = ["nope"]
    args = _cli_args(tmp_path, iter5=i5, pred=pred)
    _pin(monkeypatch, args)
    out = vd.verdict(args)
    assert out["retain"] is False
    assert out["verdict"] == "NEGATIVE_ROLLBACK_R3"
    assert all(c["ok"] for c in out["conditions"].values())


def test_verdict_negative_when_engineering_evidence_missing_writes(
        tmp_path, monkeypatch):
    ev = _evidence()
    ev["live_writes_except_gate_restamp"] = None
    args = _cli_args(tmp_path, ev=ev)
    _pin(monkeypatch, args)
    out = vd.verdict(args)
    assert out["retain"] is False
    # 活库写入量属 §8.4 数据身份条件（观测不到即判负，不得当成 0）
    assert out["conditions"]["4_data_identity"]["ok"] is False
    assert out["conditions"]["5_engineering"]["ok"] is True


def test_main_exit_codes(tmp_path, monkeypatch, capsys):
    args = _cli_args(tmp_path)
    _pin(monkeypatch, args)
    argv = ["--iter3", args.iter3, "--iter5", args.iter5,
            "--predictions", args.predictions, "--evidence", args.evidence]
    assert vd.main(argv) == 0
    i5 = _iter5()[0]
    pred = _iter5()[1]
    pred["cases"][0]["fused_ids"] = ["nope"]
    (tmp_path / "b").mkdir()
    args2 = _cli_args(tmp_path / "b", iter5=i5, pred=pred)
    _pin(monkeypatch, args2)
    assert vd.main(["--iter3", args2.iter3, "--iter5", args2.iter5,
                    "--predictions", args2.predictions,
                    "--evidence", args2.evidence]) == 3
    rc = vd.main(["--iter3", str(tmp_path / "nope.json"),
                  "--iter5", args.iter5, "--predictions", args.predictions,
                  "--evidence", args.evidence])
    assert rc == 2
    assert "[T6R4-VERDICT][STOP]" in capsys.readouterr().err
