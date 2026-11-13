"""RSI-R1.2 T0（AC04–AC07）：预算 v2、族账 160、历史 31 冻结、占额与许可。

覆盖：

* BudgetSpecV2 的形状/漂移拒绝、dispatch、单真值四源对账；
* 族账首次登记导入历史 31（重算 vs 冻结常量），carryover 不重复计；
* 第二成员/超额/预留漂移/许可篡改/族文档漂移 → 拒；
* 历史源漂移（行内容被改）→ ``HISTORY_DRIFT``，不静默接受；
* 已用量单调、≤预留；
* CLI init/check 全绿且零 POST（ledger 行数 0）。
"""

from __future__ import annotations

import json
import shutil
from decimal import Decimal
from pathlib import Path

import pytest

from orchestrator.gateway import quota_ledger as QL
from orchestrator.rsi_r1 import cli_r12
from orchestrator.rsi_r1 import contracts as C
from orchestrator.rsi_r1 import family as FAM
from orchestrator.rsi_r1 import profiles as P


# ---------------------------------------------------------------------------
# budget v2
# ---------------------------------------------------------------------------

def test_budget_v2_shape_and_numbers():
    budget = C.default_budget_v2("env_a")
    assert budget.total_posts == 124
    assert budget.normal_posts == 124
    assert budget.posts_by_lane == {"probe": 12, "practice": 16,
                                    "study_A": 48, "study_B": 48}
    assert budget.max_runs_by_lane == {"explore": 64, "confirm": 192,
                                       "replay": 192}
    assert budget.max_total_runs == 896
    # 锁内适配（2026-11-12 T5 独立评审 MAJOR-6）：这两个值**不许**跟着
    # ``profiles.P.*`` 走——旧写法 ``== P.MAX_TOTAL_STEPS`` 让测试自动跟随常量，
    # 帽一改测试照样绿。字面量 = T5 重注册的离线子信封之和（Complete §6 A1–A3）；
    # **正式**帽（124 POST / 896 run / 7,168,000 study step）未动，
    # 由 ``test_formal_caps_not_raised_by_offline_envelope`` 逐值钉死。
    assert budget.max_steps == 12_688_000
    assert budget.aux_total_steps == 5_520_000
    assert budget.max_usd == Decimal("100")
    slots = budget.slots()
    assert len(slots) == 124
    assert [s["slot"] for s in slots] == list(range(1, 125))
    assert all(s["reserve"] is False for s in slots)
    assert slots[0]["lane"] == "probe" and slots[-1]["lane"] == "study_B"


def test_budget_v2_roundtrip_and_dispatch():
    budget = C.default_budget_v2("env_a")
    parsed = C.budget_from_dict(budget.to_dict())
    assert isinstance(parsed, C.BudgetSpecV2)
    assert parsed.sha256 == budget.sha256


def test_budget_v2_rejects_drift():
    budget = C.default_budget_v2("env_a")
    doc = budget.to_dict()
    bad = json.loads(json.dumps(doc))
    bad["posts_by_lane"]["probe"] = 13
    with pytest.raises(C.ContractError) as exc:
        C.BudgetSpecV2.from_dict(bad)
    assert exc.value.code == "BUDGET_DRIFT"
    bad2 = json.loads(json.dumps(doc))
    bad2["aux_subenvelopes"]["v2_calibration"] = 1
    with pytest.raises(C.ContractError):
        C.BudgetSpecV2.from_dict(bad2)
    bad3 = json.loads(json.dumps(doc))
    bad3["profile_id"] = "rsi-r1-legacy"
    with pytest.raises(C.ContractError) as exc:
        C.BudgetSpecV2.from_dict(bad3)
    assert exc.value.code == "PROFILE_MISMATCH"


def test_budget_dispatch_unknown_schema():
    with pytest.raises(C.ContractError) as exc:
        C.budget_from_dict({"schema": "simpraxis.other.budget.v1"})
    assert exc.value.code == "SCHEMA_MISMATCH"


def test_single_truth_v2_against_ledger_namespace():
    budget = C.default_budget_v2("env_a")
    C.assert_single_truth_v2(
        budget, ledger_task_caps=QL.RSI_R12_TASK_CAPS,
        ledger_hard_cap=QL.RSI_R12_HARD_CAP,
        ledger_normal_budget=QL.RSI_R12_NORMAL_BUDGET,
        family_cap=P.FAMILY_CAP)
    with pytest.raises(C.ContractError) as exc:
        C.assert_single_truth_v2(
            budget, ledger_task_caps={0: 8, 1: 16, 2: 48, 3: 48},
            ledger_hard_cap=124, ledger_normal_budget=124,
            family_cap=P.FAMILY_CAP)
    assert exc.value.code == "BUDGET_DRIFT"
    with pytest.raises(C.ContractError):
        C.assert_single_truth_v2(
            budget, ledger_task_caps=QL.RSI_R12_TASK_CAPS,
            ledger_hard_cap=124, ledger_normal_budget=124, family_cap=144)


# ---------------------------------------------------------------------------
# 族账（真实历史账本 + 复制账本改行漂移）
# ---------------------------------------------------------------------------

@pytest.fixture()
def fam_root(tmp_path):
    root = tmp_path / "familyroot"
    root.mkdir()
    return root


@pytest.fixture()
def copied_sources(tmp_path):
    """把两本历史账复制到 tmp，返回 (sources, paths)。"""
    sources = []
    paths = []
    for i, source in enumerate(P.HISTORY_SOURCES):
        src = Path(source["root"]) / source["ledger_relpath"]
        if not src.is_file():
            pytest.fail(f"历史源账缺失（T0 环境前置）：{src}")
        dst = tmp_path / f"ledger_{i}.jsonl"
        shutil.copyfile(src, dst)
        dst.chmod(0o600)
        new_source = dict(source)
        new_source["root"] = str(tmp_path / f"root_{i}")
        sources.append(new_source)
        paths.append(dst)
    return sources, paths


def _relayout_sources(tmp_path, sources, paths):
    """把 copied_sources 的 ledger 放到 <root>/quota/ledger.jsonl 结构下。"""
    fixed = []
    for i, (source, path) in enumerate(zip(sources, paths)):
        root = tmp_path / f"root_{i}"
        qdir = root / "quota"
        qdir.mkdir(parents=True, exist_ok=True)
        target = qdir / "ledger.jsonl"
        shutil.move(str(path), target)
        target.chmod(0o600)
        fixed.append(dict(source))
    return fixed


def test_family_imports_history_once(fam_root, tmp_path, copied_sources):
    sources, paths = copied_sources
    sources = _relayout_sources(tmp_path, sources, paths)
    r12 = P.PROFILE_R12_DEF
    permit = FAM.register(fam_root, r12, "env1", history_sources=sources)
    assert permit["history_posts"] == 31
    assert permit["posts_reserved"] == 124
    doc = FAM.load_family_doc(fam_root)
    assert doc["history"]["total_call_rows"] == 31
    assert doc["history"]["total_ok_rows"] == 30
    assert doc["history"]["total_fail_rows"] == 1
    assert sum(int(m["posts_reserved"]) for m in doc["members"]) == 124
    usage = FAM.usage(fam_root)
    assert usage["reserved_total"] == 155
    # 族帽 160 = 历史 31 + 已关闭轮 5 + 本轮 124（2026-11-13 用户裁决）
    assert usage["remaining"] == 89
    # 幂等：再次登记不加第二次 carryover
    again = FAM.register(fam_root, r12, "env1", history_sources=sources)
    assert again["posts_reserved"] == 124
    doc2 = FAM.load_family_doc(fam_root)
    assert doc2["history"] == doc["history"]
    assert len(doc2["members"]) == 1
    # 第二轮被拒（预算已满，且本轮未关闭）
    with pytest.raises(FAM.FamilyError) as exc:
        FAM.register(fam_root, r12, "env2", history_sources=sources)
    assert exc.value.code == "FAMILY_ROUND_OPEN"


def test_family_history_drift_detected(fam_root, tmp_path, copied_sources):
    sources, paths = copied_sources
    sources = _relayout_sources(tmp_path, sources, paths)
    r12 = P.PROFILE_R12_DEF
    FAM.register(fam_root, r12, "env1", history_sources=sources)
    # 改动第一本账的一行（模型名），整账字节与行集哈希都变
    target = Path(sources[0]["root"]) / "quota" / "ledger.jsonl"
    lines = target.read_text(encoding="utf-8").splitlines()
    row = json.loads(lines[0])
    row["model"] = "tampered-model"
    lines[0] = json.dumps(row, sort_keys=True)
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(FAM.FamilyError) as exc:
        FAM.register(fam_root, r12, "env1", history_sources=sources)
    assert exc.value.code == "HISTORY_DRIFT"


def test_family_permit_binding(fam_root):
    r12 = P.PROFILE_R12_DEF
    permit = FAM.register(fam_root, r12, "env1")
    FAM.verify_permit(fam_root, permit, r12, "env1")
    tampered = dict(permit)
    tampered["posts_reserved"] = 120
    with pytest.raises(FAM.FamilyError) as exc:
        FAM.verify_permit(fam_root, tampered, r12, "env1")
    assert exc.value.code == "FAMILY_PERMIT_INVALID"
    # 族文档变化（登记已用量）后旧许可失效
    FAM.record_usage(fam_root, "env1", 3)
    with pytest.raises(FAM.FamilyError) as exc:
        FAM.verify_permit(fam_root, permit, r12, "env1")
    assert exc.value.code == "FAMILY_PERMIT_INVALID"


def test_family_usage_monotonic(fam_root):
    r12 = P.PROFILE_R12_DEF
    FAM.register(fam_root, r12, "env1")
    FAM.record_usage(fam_root, "env1", 12)
    assert FAM.usage(fam_root)["members"][0]["posts_used"] == 12
    with pytest.raises(FAM.FamilyError) as exc:
        FAM.record_usage(fam_root, "env1", 11)
    assert exc.value.code == "FAMILY_USAGE_REGRESSION"
    with pytest.raises(FAM.FamilyError) as exc:
        FAM.record_usage(fam_root, "env1", 125)
    assert exc.value.code == "FAMILY_CAP_EXCEEDED"
    FAM.record_usage(fam_root, "env1", 124)
    assert FAM.usage(fam_root)["members"][0]["state"] == "exhausted"


def test_family_requires_r12_profile(fam_root):
    legacy = P.PROFILE_LEGACY_DEF
    with pytest.raises(FAM.FamilyError) as exc:
        FAM.register(fam_root, legacy, "env1")
    assert exc.value.code == "PROFILE_NOT_ELIGIBLE"


def test_family_doc_permissions(fam_root):
    import os

    FAM.register(fam_root, P.PROFILE_R12_DEF, "env1")
    doc = fam_root / FAM.FAMILY_DIRNAME / FAM.FAMILY_DOC_NAME
    assert doc.is_file()
    assert os.stat(doc).st_mode & 0o777 == 0o600
    assert os.stat(doc.parent).st_mode & 0o777 == 0o700


# ---------------------------------------------------------------------------
# CLI init/check（零 POST）
# ---------------------------------------------------------------------------

def test_cli_init_check_zero_post(tmp_path, monkeypatch, capsys):
    root = tmp_path / "rsi_r12"
    root.mkdir()
    monkeypatch.setenv("SIMPRAXIS_RSI_R12_ROOT", str(root))
    plan = tmp_path / "plan.md"
    plan.write_text("plan bytes", encoding="utf-8")
    rc = cli_r12.main(["init", "--envelope", "env_cli", "--plan", str(plan)])
    assert rc == 0
    envelope = json.loads((root / "env_cli" / "contract" / "envelope.json")
                          .read_text(encoding="utf-8"))
    C.validate_envelope_v2(envelope, envelope_id="env_cli")
    assert envelope["ledger_namespace"] == 2904
    assert envelope["hard_cap"] == 124
    assert envelope["family_cap"] == 244
    assert envelope["mode"] == "offline_validation"
    assert envelope["cost_gate"]["decision"] == "BLOCKED"
    assert envelope["cost_gate"]["paid_authorized"] is False
    rc = cli_r12.main(["check", "--envelope", "env_cli"])
    assert rc == 0
    out = capsys.readouterr().out
    payload = json.loads([line for line in out.splitlines()
                          if line.startswith("{")][-1])
    assert payload["ok"] is True
    assert payload["ledger_rows"] == 0
    assert payload["family"]["reserved_total"] == 155
    # 重复 init 不许覆盖
    rc = cli_r12.main(["init", "--envelope", "env_cli", "--plan", str(plan)])
    assert rc == 2


def test_cli_paid_mode_starts_blocked_until_authorize(tmp_path, monkeypatch):
    """T6（锁内适配，Plan §6.1 顺序）：``init --mode paid`` 先建根、费用门 BLOCKED。

    旧测试钉的是「paid 无 consent 直接拒建根」——那是 T0–T5 阶段权宜，与 Plan
    §6.1「init(paid) → preprint → authorize --consent」的顺序冲突。现在的判据：
    根建成、envelope.cost_gate 保持未授权，付费命令仍被 ``_paid_gate`` 拦。
    """
    root = tmp_path / "rsi_r12"
    root.mkdir()
    monkeypatch.setenv("SIMPRAXIS_RSI_R12_ROOT", str(root))
    monkeypatch.setenv("SIMPRAXIS_RSI_R12_CAMPAIGN", "env_paid")
    monkeypatch.delenv("SIMPRAXIS_ALLOW_PAID", raising=False)
    plan = tmp_path / "plan.md"
    plan.write_text("plan bytes", encoding="utf-8")
    rc = cli_r12.main(["init", "--envelope", "env_paid", "--mode", "paid",
                       "--plan", str(plan)])
    assert rc == 0
    envelope = json.loads(
        (root / "env_paid" / "contract" / "envelope.json").read_text(
            encoding="utf-8"))
    assert envelope["mode"] == "paid"
    assert envelope["cost_gate"]["paid_authorized"] is False
    assert envelope["cost_gate"]["consent_sha256"] is None
    assert not (root / "env_paid" / "contract" / "consent.json").exists()
    # 未 authorize：付费命令必须被环境闸/费用门挡住（这里连环境闸都过不去）。
    rc = cli_r12.main(["probe", "--envelope", "env_paid",
                       "--batch-id", "b0", "--tokenizer", "/nonexistent",
                       "--tokenizer-sha256", "0" * 64])
    assert rc == 2


def test_cli_check_detects_tampered_budget(tmp_path, monkeypatch):
    root = tmp_path / "rsi_r12"
    root.mkdir()
    monkeypatch.setenv("SIMPRAXIS_RSI_R12_ROOT", str(root))
    plan = tmp_path / "plan.md"
    plan.write_text("plan bytes", encoding="utf-8")
    assert cli_r12.main(["init", "--envelope", "env_t", "--plan", str(plan)]) == 0
    budget_path = root / "env_t" / "contract" / "budget.json"
    doc = json.loads(budget_path.read_text(encoding="utf-8"))
    doc["max_steps"] = 1
    budget_path.write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n",
                           encoding="utf-8")
    assert cli_r12.main(["check", "--envelope", "env_t"]) == 2


# ---------------------------------------------------------------------------
# AC06/AC07 账本层：sent/failed/unknown 占槽、preflight 阻断 0 POST、不借 lane
# ---------------------------------------------------------------------------

@pytest.fixture()
def ledger_env(tmp_path, monkeypatch):
    root = tmp_path / "rsi_r12"
    root.mkdir()
    monkeypatch.setenv("SIMPRAXIS_RSI_R12_ROOT", str(root))
    monkeypatch.setenv("SIMPRAXIS_RSI_R12_CAMPAIGN", "cam-L")
    return root


def test_ledger_r12_namespace_and_caps(ledger_env):
    ledger = QL.QuotaLedger.rsi_r12()
    assert ledger.ns.phase == 2904
    assert dict(ledger.ns.task_caps) == {0: 12, 1: 16, 2: 48, 3: 48}
    assert ledger.ns.hard_cap == 124
    assert ledger.ns.normal_budget == 124


def test_sent_failed_unknown_occupy_slots(ledger_env):
    ledger = QL.QuotaLedger.rsi_r12()
    QL.write_approval(batch_id="b-probe", task=0, stage="probe_r12",
                      approved=12, beyond_normal=False, note="T0 账本层",
                      namespace=QL.RSI_R12_NAMESPACE)
    ledger.preflight("probe_r12", task=0, batch_id="b-probe")
    outcomes = [(True, None)] * 10 + [(False, "HTTP 500")]
    for ok, err in outcomes:
        ledger.record("probe_r12", task=0, batch_id="b-probe", ok=ok, err=err)
    # 未知发送（SEND_UNKNOWN）同样落 call 行并占槽
    ledger.record("probe_r12", task=0, batch_id="b-probe", ok=False,
                  err="SEND_UNKNOWN")
    rows = ledger._read_rows()
    calls = [r for r in rows if r.get("event") == "call"]
    assert len(calls) == 12
    assert sum(1 for r in calls if not r["ok"]) == 2
    # 第 13 呼：preflight 拒，且只留 seq=null blocked 行（POST=0）
    with pytest.raises(QL.GatewayPausedError):
        ledger.preflight("probe_r12", task=0, batch_id="b-probe")
    rows = ledger._read_rows()
    calls = [r for r in rows if r.get("event") == "call"]
    assert len(calls) == 12
    blocked = [r for r in rows if r.get("seq") is None]
    assert blocked and all(str(r.get("err", "")).startswith("preflight_blocked")
                           or "preflight_blocked" in str(r.get("reason", ""))
                           for r in blocked)


def test_no_lane_borrowing_and_global_gate(ledger_env):
    ledger = QL.QuotaLedger.rsi_r12()
    QL.write_approval(batch_id="b-p", task=0, stage="probe_r12", approved=12,
                      beyond_normal=False, note="x",
                      namespace=QL.RSI_R12_NAMESPACE)
    QL.write_approval(batch_id="b-q", task=1, stage="practice_r12", approved=16,
                      beyond_normal=False, note="x",
                      namespace=QL.RSI_R12_NAMESPACE)
    for _ in range(12):
        ledger.record("probe_r12", task=0, batch_id="b-p", ok=True)
    # probe 耗尽不回流：practice 仍只有自己的 16，study 不受影响
    ledger.preflight("practice_r12", task=1, batch_id="b-q")
    for i in range(16):
        ledger.record("practice_r12", task=1, batch_id="b-q", ok=(i < 15),
                      err=None if i < 15 else "TRANSPORT_TIMEOUT")
    with pytest.raises(QL.GatewayPausedError):
        ledger.preflight("practice_r12", task=1, batch_id="b-q")
    calls = [r for r in ledger._read_rows() if r.get("event") == "call"]
    assert len(calls) == 28
    # 全局门仍是 124（probe+practice 不侵占 study 的 96）
    assert ledger.ns.hard_cap == 124
    assert ledger.ns.task_caps[2] == 48 and ledger.ns.task_caps[3] == 48


def test_family_no_double_carryover_across_restart_and_concurrency(fam_root):
    import threading

    errors = []
    permits = []

    def _register():
        try:
            permits.append(FAM.register(fam_root, P.PROFILE_R12_DEF, "env1"))
        except FAM.FamilyError as exc:  # 竞争输家只允许 FAMILY_ROUND_OPEN
            errors.append(exc.code)

    threads = [threading.Thread(target=_register) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # 重启语义：不读进程内状态，全部从磁盘重读
    usage = FAM.usage(fam_root)
    assert len(usage["members"]) == 1
    assert usage["reserved_total"] == 155  # 31 只计一次
    assert len(permits) >= 1
    assert all(code == "FAMILY_ROUND_OPEN" for code in errors)
    # 「新 root」= 新 envelope 名，不能借余额
    with pytest.raises(FAM.FamilyError) as exc:
        FAM.register(fam_root, P.PROFILE_R12_DEF, "env2-new-root")
    assert exc.value.code == "FAMILY_ROUND_OPEN"


def test_formal_caps_not_raised_by_offline_envelope():
    """MAJOR-6 的第二半：离线子信封上抬**不得**带动正式帽。

    Plan L148 允许「修订离线分配并重新注册离线 spec」，但同一句写明「仍不得抬
    计算帽」。这里把**正式**帽逐值钉成字面量（不随常量走），任何连带上调即红：
    124 POST（12/16/48/48）、每 study 探索 64 / 确认 192 / 复跑 192、总 896 run、
    每 run 8000 step、正式总 7,168,000 step。
    """
    b = C.default_budget_v2("env_a")
    assert b.total_posts == 124
    assert dict(b.posts_by_lane) == {"probe": 12, "practice": 16,
                                     "study_A": 48, "study_B": 48}
    assert dict(b.max_runs_by_lane) == {"explore": 64, "confirm": 192,
                                        "replay": 192}
    assert b.max_total_runs == 896
    assert P.MAX_STEPS_PER_RUN == 8000
    assert P.MAX_STUDY_STEPS == 7_168_000
    # aux/整轮是 T5 重注册值，但必须**恰等于**子信封之和（自洽，不是随手数）。
    assert sum(P.AUX_SUBENVELOPES.values()) == P.AUX_SUBENVELOPE_TOTAL == 5_520_000
    assert P.MAX_TOTAL_STEPS == P.MAX_STUDY_STEPS + P.AUX_SUBENVELOPE_TOTAL
