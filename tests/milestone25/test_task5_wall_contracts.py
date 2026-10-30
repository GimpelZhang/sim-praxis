"""Phase 2.5 Task 5 历史墙契约测试（AC-T5-1/2/3/4/5/6/7；全部 0 付费）。

三层：
1. 判据纯函数（verdict.py）——half-up 子帽/双半值 tie/储备不借调/
   三态判决必绿必红变异/B-cap 独立轴/deny  harvest，合成输入反证；
2. 冻结闸件——九件 sha256 钉死、GATE 内嵌哈希自证、白名单围栏逐字、
   SEQ_SPLIT 39 呼逐项连续、闸件零密钥字面量；
3. 运行件反作弊——模型容器 argv 永不挂 /opt/inst 与冻结 OBJ、
   裸跑 exit 2、冻结清单不符 fail-closed、LOCK 哈希不符拒跑。

依赖 /mnt 冻结根的对账在冻结根缺席时 skip（CI 裸机 hermetic）。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from orchestrator.agentic.wiring import parse_grant_strings
from orchestrator.common.secrets import scan_text
from scripts.phase25.walls import freeze_common as fc
from scripts.phase25.walls import run_wall, spike_backend, verdict as V

REPO = Path(__file__).resolve().parents[2]
GATES = REPO / "scripts" / "phase25" / "walls" / "gates"

# 首呼冻结时刻九件闸哈希（freeze_manifest.json 同源；改动任一字节必红）。
PINNED_GATE_HASHES = {
    "common": {
        "VERDICT_CRITERIA.md":
            "73abfc7b0af92aa75031817b29bdbfc041848ecc2a03d27fdf18c70333f8f126",
    },
    "wall_a": {
        "TASK.md":
            "c4fd5348b0e48dcefea7816293f24a9c6a2b5112dc72e049419251552a35a4ac",
        "GATE.md":
            "5d0b4474f4153352e05b397c1c1c7c41eb8a8c8525b709438af14bbd849d7845",
        "wall_a_runner.py":
            "21507dedd634d00359693c5f4a0f6b1fa62ce76366bc6e4323e1c304b0b8ec2d",
        "expected_vectors.json":
            "b6a7ac6319bc2cfb1cbf5134a4d35cc74dd90bca6ed2cb33b419c52fea59bbe2",
    },
    "wall_b": {
        "TASK.md":
            "5725313ee73d3c89085ba4937ced74746d603ea1c7942bd2862c5fdb0276546c",
        "GATE.md":
            "91ececd2f9cd2f72e2fa564c38a20d6804438da982155b223afcd6a4407339e6",
        "wall_b_checker.py":
            "5f178625d004ee8d3c239c1d967cb1610ad3502ffee4ffe0888e615caffd26c7",
        "SEQ_SPLIT.json":
            "861f5e77e378161d9f0337b97b76e1cecc91ea161e317fb0e1fde4096a57ee9c",
    },
}

FROZEN_GRANTS = (
    "read_file", "list_dir", "grep", "write:",
    "run:python", "run:ls", "run:cat", "run:grep", "run:find",
)


def _freeze_root() -> Path | None:
    explicit = os.environ.get("SIMPRAXIS_T5_FREEZE_ROOT")
    if explicit:
        p = Path(explicit)
        return p if (p / "manifests" / "freeze_manifest.json").is_file() else None
    base = os.environ.get("SIMPRAXIS_PHASE25_ROOT")
    if not base:
        return None
    p = Path(base) / "walls" / "t5_freeze"
    return p if (p / "manifests" / "freeze_manifest.json").is_file() else None


# ======================================================================
# 1. 判据纯函数：子帽算账（AC-T5-2）
# ======================================================================

def test_half_up_never_bankers():
    # 0.5 进位；Python 内建 round(0.5)==0 是反例，必须显式 half-up。
    assert V.half_up_quotient(1, 2) == 1
    assert V.half_up_quotient(3, 2) == 2
    assert V.half_up_quotient(25 * 60, 90) == 17   # 16.666… → 17（raw）
    assert V.half_up_quotient(25 * 30, 90) == 8    # 8.333… half-up=8（raw）
    with pytest.raises(TypeError):
        V.half_up_quotient(1.0, 2)
    with pytest.raises(ValueError):
        V.half_up_quotient(-1, 2)


def test_subcaps_frozen_60_30():
    r = V.compute_subcaps(60, 30)
    assert r["subcaps"] == {V.WALL_A: 16, V.WALL_B: 9}   # 17→clamp16, 8→clamp9
    assert r["sum"] == 25
    assert r["reserve"] == 3
    assert r["pool_total"] == 28
    assert r["double_half_tie"] is False
    # 50% 成立线。
    assert V.calls_to_rc0_threshold(60) == 30
    assert V.calls_to_rc0_threshold(30) == 15


def test_subcaps_clamp_extremes():
    r = V.compute_subcaps(1000, 1)
    assert r["subcaps"] == {V.WALL_A: 16, V.WALL_B: 9}
    r = V.compute_subcaps(1, 1000)
    assert r["subcaps"] == {V.WALL_A: 9, V.WALL_B: 16}
    # 任何极端组合帽和恒 25、储备恒 3（clamp 不变量）。
    assert r["sum"] == 25 and r["reserve"] == 3


def test_double_half_tie_both_orders():
    # sA==sB 时两商恰为 12.5/12.5——唯一双半值退化点。
    r_ab = V.compute_subcaps(7, 7, first_run=V.WALL_A)
    assert r_ab["double_half_tie"] is True
    assert r_ab["subcaps"] == {V.WALL_A: 12, V.WALL_B: 13}  # 先 floor 后 ceil
    assert r_ab["sum"] == 25 and r_ab["reserve"] == 3
    r_ba = V.compute_subcaps(7, 7, first_run=V.WALL_B)
    assert r_ba["subcaps"] == {V.WALL_A: 13, V.WALL_B: 12}
    # tie-break 留痕可审。
    assert r_ab["tie_break"]["rule"] == \
        "first_run_half_down_second_run_half_up"


def test_rerun_claim_rules():
    sub = V.compute_subcaps(60, 30)["subcaps"]
    # 零申请恒合法。
    ok, why = V.RerunClaim(V.WALL_A, False, 5).validate(sub)
    assert ok and why == "no_rerun"
    # 非污染不许重跑（到帽即熔断）。
    ok, why = V.RerunClaim(V.WALL_A, False, 16, rerun_calls=1,
                           reserve_taken=1).validate(sub)
    assert not ok and "非供应侧污染" in why
    # 先花本墙余额：剩 2，重跑 3 呼必须 reserve 1；申请 reserve 0 非法。
    ok, why = V.RerunClaim(V.WALL_A, True, 14, rerun_calls=3,
                           reserve_taken=0).validate(sub)
    assert not ok and "余额" in why
    ok, _ = V.RerunClaim(V.WALL_A, True, 14, rerun_calls=3,
                         reserve_taken=1).validate(sub)
    assert ok
    # 单墙重跑 ≤4 呼封顶。
    ok, why = V.RerunClaim(V.WALL_A, True, 14, rerun_calls=5,
                           reserve_taken=3).validate(sub)
    assert not ok and "4 呼封顶" in why
    # 储备池只有 3。
    ok, why = V.RerunClaim(V.WALL_B, True, 9, rerun_calls=4,
                           reserve_taken=4).validate(sub)
    assert not ok and "共享储备 3" in why
    # 跨墙借调形态：B 花光后申请 4 呼全靠储备——储备上限 3 直接拒。
    ok, _ = V.RerunClaim(V.WALL_B, True, 9, rerun_calls=3,
                         reserve_taken=3).validate(sub)
    assert ok


def test_settle_pool_first_come_reserve_and_total_cap():
    sub = V.compute_subcaps(60, 30)["subcaps"]
    # A 先到先得拿走 3 储备，B 再要储备即拒（不补不借）。
    claims = {
        V.WALL_A: V.RerunClaim(V.WALL_A, True, 14, rerun_calls=3,
                               reserve_taken=1),
        V.WALL_B: V.RerunClaim(V.WALL_B, True, 9, rerun_calls=3,
                               reserve_taken=3),
    }
    got = V.settle_pool(claims, sub)
    assert got["ok"] is False and "储备池仅剩 2" in got["reason"]
    # 合法总账：16+9 首跑 + 1 储备重跑 = 26 ≤ 28。
    claims[V.WALL_B] = V.RerunClaim(V.WALL_B, True, 9, rerun_calls=2,
                                    reserve_taken=2)
    got = V.settle_pool(claims, sub)
    assert got["ok"] is True
    # A 14+3=17、B 9+2=11 → 28（首跑 23 + 重跑 5），储备池取空。
    assert got["grand_total"] == 28 and got["reserve_left"] == 0
    # 两墙零申请：25。
    got0 = V.settle_pool({
        V.WALL_A: V.RerunClaim(V.WALL_A, False, 16),
        V.WALL_B: V.RerunClaim(V.WALL_B, False, 9)}, sub)
    assert got0["ok"] is True and got0["grand_total"] == 25
    # 缺一份申请 fail-closed。
    with pytest.raises(ValueError):
        V.settle_pool({V.WALL_A: claims[V.WALL_A]}, sub)


# ======================================================================
# 2. 三态判决纯函数（AC-T5-4；必绿/必红变异）
# ======================================================================

def _wr(wall, passed, calls=None, **kw):
    return V.WallResult(wall=wall, passed=passed, calls_to_rc0=calls, **kw)


def test_verdict_clean_double_pass_green():
    out = V.engineering_verdict(
        _wr(V.WALL_A, True, 16), _wr(V.WALL_B, True, 9),
        baselines={V.WALL_A: 60, V.WALL_B: 30})
    assert out["verdict"] == V.V_ESTABLISHED
    assert out["per_wall"][V.WALL_A]["within_50pct"] is True


def test_verdict_must_red_cap_exhausted():
    out = V.engineering_verdict(
        _wr(V.WALL_A, True, 10),
        _wr(V.WALL_B, False, cap_exhausted=True))
    assert out["verdict"] == V.V_NOT_ESTABLISHED
    assert any("到子帽" in r for r in out["reasons"])


def test_verdict_must_red_over_50pct():
    # 闸过但 calls=31 > 30：改善未达预注册线，禁止改界翻案。
    out = V.engineering_verdict(
        _wr(V.WALL_A, True, 31), _wr(V.WALL_B, True, 9))
    assert out["verdict"] == V.V_NOT_ESTABLISHED
    assert any("50%" in r for r in out["reasons"])


def test_verdict_must_red_escalation_or_human():
    for kw in ({"escalated": True}, {"human": True}):
        out = V.engineering_verdict(
            _wr(V.WALL_A, True, 1, **kw), _wr(V.WALL_B, True, 1))
        assert out["verdict"] == V.V_NOT_ESTABLISHED


def test_verdict_inconclusive_one_clean_fail_under_cap():
    out = V.engineering_verdict(
        _wr(V.WALL_A, True, 8),
        _wr(V.WALL_B, False))  # 模型自终局 fail，5 呼未到 9 帽
    assert out["verdict"] == V.V_INCONCLUSIVE


def test_verdict_pending_rerun_is_inconclusive():
    out = V.engineering_verdict(
        _wr(V.WALL_A, False, pending_rerun=True),
        _wr(V.WALL_B, True, 4))
    assert out["verdict"] == V.V_INCONCLUSIVE


def test_verdict_contaminated_undecidable_guilty():
    # 疑罪从有：重跑后仍不可判 → 不成立（不得再开口）。
    out = V.engineering_verdict(
        _wr(V.WALL_A, True, 4),
        _wr(V.WALL_B, False, contaminated_undecidable=True))
    assert out["verdict"] == V.V_NOT_ESTABLISHED
    assert any("疑罪从有" in r for r in out["reasons"])


def test_verdict_passed_requires_positive_calls():
    with pytest.raises(ValueError):
        V.engineering_verdict(_wr(V.WALL_A, True, None),
                              _wr(V.WALL_B, True, 1))


def test_b_cap_axis_three_branches():
    both = V.b_cap_axis(_wr(V.WALL_B, True, 4, b_cap_passed=True))
    assert both["label"] == V.BC_BOTH and both["d4_stop"] is False
    cap_wall = V.b_cap_axis(_wr(V.WALL_B, True, 4, b_cap_passed=False))
    assert cap_wall["label"] == V.BC_CAP_WALL_REPRODUCED
    assert cap_wall["d4_stop"] is False       # 独立轴，不触发 D-4
    eng_fail = V.b_cap_axis(_wr(V.WALL_B, False, b_cap_passed=None))
    assert eng_fail["label"] == V.BC_ENG_FAIL_D4
    assert eng_fail["d4_stop"] is True
    with pytest.raises(ValueError):
        V.b_cap_axis(_wr(V.WALL_A, True, 1))


def test_deny_records_harvest():
    events = [
        {"tool": "run_cmd", "policy": {"decision": "allow"}},
        {"tool": "run_cmd", "policy": {"decision": "deny"}},
        {"tool": "write_file", "policy": {"decision": "deny"}},
        {"tool": "finish", "policy": {"decision": "allow"}},
        "garbage",
    ]
    out = V.deny_records_from_trace(events)
    assert out["deny_count"] == 2
    assert out["deny_tools"] == ["run_cmd", "write_file"]
    assert V.deny_records_from_trace([]) == {"deny_count": 0,
                                            "deny_tools": []}


# ======================================================================
# 3. 冻结闸件：哈希钉死/围栏逐字/SEQ_SPLIT/零密钥
# ======================================================================

def test_gate_hashes_pinned_byte_for_byte():
    assert fc.gate_hashes() == PINNED_GATE_HASHES


def test_gate_embedded_hashes_truthful():
    # 两份 GATE.md 内嵌的 sha256 留痕必须与实件逐字一致。
    fc.verify_embedded_hashes()


def test_grant_fences_verbatim_in_both_gates():
    for wall in (V.WALL_A, V.WALL_B):
        assert run_wall.grants_from_gate(wall) == FROZEN_GRANTS
    gt = parse_grant_strings(FROZEN_GRANTS)
    assert gt.tools == frozenset(
        {"read_file", "list_dir", "grep", "write_file",
         "apply_patch", "run_cmd"})
    assert ("python",) in gt.run_argv
    assert {("ls",), ("cat",), ("grep",), ("find",)} <= set(gt.run_argv)
    assert gt.write_roots == ("",)          # 整个 /work
    assert gt.probes == frozenset() and gt.network_probes == frozenset()


def test_grant_parsing_rejects_smuggles():
    with pytest.raises(Exception):
        parse_grant_strings(("run:",))       # 空 argv
    with pytest.raises(Exception):
        parse_grant_strings(("run::rm",))    # 空 argv 段
    with pytest.raises(Exception):
        parse_grant_strings(("",))           # 空授予串
    with pytest.raises(Exception):
        parse_grant_strings(("write:../etc",))
    # 未知名（run_probe/recall/网络）一律不解析进表。
    gt = parse_grant_strings(FROZEN_GRANTS)
    assert "run_probe" not in gt.tools and "recall" not in gt.tools


def test_seq_split_frozen():
    doc = json.loads((GATES / "wall_b" / "SEQ_SPLIT.json").read_text("utf-8"))
    assert doc["schema"] == "simpraxis.phase25.wall_b_seq_split.v1"
    assert doc["seq_range"] == [398, 436]
    assert doc["sB_eng"] == 30 and doc["sB_cap"] == 9
    c = doc["counts"]
    assert (c["total"], c["B_eng"], c["B_cap"]) == (39, 30, 9)
    assert (c["birth"], c["repair"]) == (18, 21)
    seqs = [e["seq"] for e in doc["entries"]]
    assert seqs == list(range(398, 437))     # 39 呼逐项连续无缺
    walls = [e["wall"] for e in doc["entries"]]
    assert walls.count("B-eng") == 30 and walls.count("B-cap") == 9
    assert {e["evidence_level"] for e in doc["entries"]} == {"byte_confirmed"}
    by = c["by_batch"]
    assert (by["T8-C9-stage1"]["B_eng"],
            by["T8-C9-stage1"]["B_cap"]) == (13, 0)
    assert (by["T8-C10-stage1"]["B_eng"],
            by["T8-C10-stage1"]["B_cap"]) == (10, 3)
    assert (by["T8-C11-stage1"]["B_eng"],
            by["T8-C11-stage1"]["B_cap"]) == (7, 6)


def test_gate_files_contain_no_secret_literals():
    for section, names in fc.GATE_FILES.items():
        base = GATES if section == "common" else GATES / section
        for name in names:
            hits = scan_text((base / name).read_text(encoding="utf-8"))
            assert hits == [], f"{section}/{name} 命中密钥模式：{hits}"


def test_gate_tree_whitelist_no_stray_files():
    # 闸目录只许白名单件（.omc 工具垃圾曾误入；首呼后一字不改）。
    for section, names in fc.GATE_FILES.items():
        base = GATES if section == "common" else GATES / section
        actual = sorted(p.name for p in base.iterdir() if p.is_file())
        assert actual == sorted(names), section


# ======================================================================
# 4. 运行件反作弊 + 双守卫
# ======================================================================

def test_model_container_argv_never_mounts_scorer(tmp_path):
    # wallB 无冻结根依赖，恒可测；wallA 需要 gate_env 物化物。
    backend_b = spike_backend.SpikeWallBackend(V.WALL_B, freeze_dir=tmp_path)
    argv_b = backend_b.create_argv(
        ["/opt/venv/bin/python", "-c", "print(1)"],
        cwd=tmp_path, env=None, label="t5-unit")
    joined_b = " ".join(argv_b)
    assert "/opt/inst" not in joined_b             # 评分器不入模型容器
    assert "pitcher_open_watertight" not in joined_b
    assert "libero_tour" not in joined_b           # 不挂 tour/gate
    assert "/opt/gate" not in joined_b
    assert "--network=none" in argv_b
    assert "--cap-drop" in argv_b and "ALL" in argv_b

    root = _freeze_root()
    if root is not None:
        backend_a = spike_backend.SpikeWallBackend(V.WALL_A, freeze_dir=root)
        argv_a = backend_a.create_argv(
            ["/opt/venv/bin/python", "-c", "print(1)"],
            cwd=tmp_path, env=None, label="t5-unit")
        joined_a = " ".join(argv_a)
        assert "/opt/inst" not in joined_a
        assert "pitcher_open_watertight" not in joined_a
        assert "libero_tour" in joined_a           # wallA 才挂 tour+gate
        assert "/opt/gate" in joined_a
        assert "--network=none" in argv_a


def test_score_container_does_mount_scorer(tmp_path):
    # 评分容器才挂 /opt/inst（构造 argv 面，不启容器）。
    cmd = [
        "docker", "create", "--rm", "-i", "--network=none",
        "-v", f"{tmp_path}:/work:rw",
        "-v", f"{tmp_path}:/opt/inst:ro",
        spike_backend.IMAGE, "--", "/opt/venv/bin/python",
        "/opt/inst/wall_a_runner.py"]
    assert any("/opt/inst" in tok for tok in cmd)


def test_bare_run_exits_2(monkeypatch):
    monkeypatch.delenv("SIMPRAXIS_ALLOW_PAID", raising=False)
    # cmd_run 首行守卫 return 2（__main__ 包成 SystemExit(2)），不触网。
    assert run_wall.main(["--wall", V.WALL_A]) == 2


def test_run_wall_rejects_tampered_grants(monkeypatch, tmp_path):
    monkeypatch.setenv("SIMPRAXIS_ALLOW_PAID", "1")
    monkeypatch.setattr(run_wall, "verify_freeze", lambda root: {})
    monkeypatch.setattr(run_wall, "grants_from_gate",
                        lambda wall: ("read_file",))   # 与冻结串不符
    with pytest.raises(SystemExit, match="白名单"):
        run_wall.cmd_run(V.WALL_A)


def test_lock_hash_mismatch_refuses(tmp_path):
    root = tmp_path
    (root / "manifests").mkdir(parents=True)
    lock = root / "manifests" / "LOCK.json"
    fc.write_json(lock, {"gate_hashes": {"tampered": True}})
    with pytest.raises(SystemExit, match="LOCK 闸哈希"):
        run_wall.write_lock(root, V.WALL_A, "P25-B2", "t")


def test_verify_freeze_missing_manifest(tmp_path):
    with pytest.raises(SystemExit, match="冻结清单不存在"):
        run_wall.verify_freeze(tmp_path)


# ======================================================================
# 5. 冻结根对账（有 /mnt 冻结物才跑）
# ======================================================================

@pytest.mark.skipif(_freeze_root() is None,
                    reason="冻结根缺席（需 SIMPRAXIS_PHASE25_ROOT 物化物）")
def test_freeze_manifest_matches_live_hashes():
    root = _freeze_root()
    manifest = json.loads((root / "manifests" /
                           "freeze_manifest.json").read_text("utf-8"))
    assert manifest["gate_hashes"] == PINNED_GATE_HASHES
    a = manifest["sections"]["wall_a"]
    assert a["validation_assertions"] == {
        "green_rc0": True, "green_verdict_pass": True,
        "green_12_of_12": True, "red_rc1": True,
        "red_verdict_fail": True, "red_egl_error": True}
    assert manifest["sections"]["wall_b"]["validation_all_ok"] is True
    ev = manifest["sections"]["wall_b"]["frozen_evidence"]
    assert ev["sha256"].startswith("ab30905766eff130")
    assert ev["size"] == 48527
    g = ev["geometry"]
    assert (g["faces"], g["vertices"]) == (1536, 770)
    assert g["is_watertight"] is True
