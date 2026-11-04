"""Phase 2.7 Task 6：LIBERO VLA 多 Stage 战役 —— AC-T6-1..8 机判。

对应用例名与计划 §T6.6 的机判命令逐字一致：

``test_ac_t6_1_candidates_schema`` / ``test_ac_t6_2_gate1_human_record`` /
``test_ac_t6_3_baseline_video`` / ``test_ac_t6_4_success_matrix`` /
``test_ac_t6_5_gate2_and_recycle_order`` / ``test_ac_t6_6_degradation_disclosure`` /
``test_ac_t6_7_main_venv_untouched`` / ``test_ac_t6_8_vram_budget_and_order``。

纪律（硬规则 2/7）：

- **0 付费 / 0 联网**：只读战役落盘的证据件，不构网关、不连飞书、不下载权重；
- **断言不放水**：缺件即红，不 ``skip``、不 ``xfail``、不用默认值兜底；
  Wilson 端点用**独立重算**逐位比对（< 1e-6）；
- AC-T6-3 按计划打 ``container`` 标记（真容器产物）——裸跑收集期 deselect，
  需 ``SIMPRAXIS_CONTAINER=1`` 显式跑；
- AC-T6-5 依赖活 KB 仓（`check_solutions` 与四表对账），按计划打
  ``kb_integration`` 标记，默认 deselect，收尾显式跑。

证据根：``$SIMPRAXIS_PHASE27_ROOT/task6_vla/``（计划 §T6.7）。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PHASE_ROOT = Path(
    os.environ.get("SIMPRAXIS_PHASE27_ROOT") or "/mnt/artifacts/simpraxis/phase27")
TASK_ROOT = PHASE_ROOT / "task6_vla"
S0 = TASK_ROOT / "s0"
RUN = TASK_ROOT / "runs" / "p27-t6-vla-001"

# 预注册 §5：策略选择顺序（钉死，不得开跑后改序）。
STRATEGY_ORDER = ["octo-small", "openvla-7b-int4", "openvla-7b-fp16"]
STAGE_IDS = ("s1_baseline", "s2_main_policy", "s3_control_policy")
Z = 1.959963984540054


def _load(path: Path):
    assert path.is_file(), f"证据件缺失：{path}"
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _wilson(k: int, n: int) -> dict[str, float]:
    """独立实现（不与生产同源），用于端点互证。"""
    p = k / n
    denom = 1.0 + Z * Z / n
    center = (p + Z * Z / (2 * n)) / denom
    half = (Z / denom) * ((p * (1 - p) / n + Z * Z / (4 * n * n)) ** 0.5)
    return {"lower": max(0.0, center - half), "upper": min(1.0, center + half)}


# --------------------------------------------------------------------- AC-T6-1

def test_ac_t6_1_candidates_schema():
    """每个候选含 repo_commit(40hex|null)/weights_sha256(64hex|null)/license/requires；
    null 必须配 ``unverified`` 标记。"""
    doc = _load(S0 / "vla_candidates.json")
    assert doc["schema"] == "simpraxis.vla_candidates.v1"
    assert doc["paid_calls"] == 0, "S0 不得产生付费调用"
    cands = doc["candidates"]
    assert [c["name"] for c in cands] == ["octo-small", "openvla-7b"], \
        "候选集与 S0 清单不符（跑后不得增删）"
    hex40 = re.compile(r"^[0-9a-f]{40}$")
    hex64 = re.compile(r"^[0-9a-f]{64}$")
    for c in cands:
        rc = c.get("repo_commit")
        assert rc is None or hex40.match(rc), f"{c['name']}: repo_commit 非法 {rc!r}"
        ws = c.get("weights_sha256")
        assert ws is None or hex64.match(ws), f"{c['name']}: weights_sha256 非法 {ws!r}"
        assert c.get("license"), f"{c['name']}: license 缺失"
        assert c.get("requires"), f"{c['name']}: requires 缺失"
        unverified = " ".join(c.get("unverified") or [])
        if rc is None:
            assert "repo_commit" in unverified, f"{c['name']}: null 未标 unverified"
        if ws is None:
            assert "weights_sha256" in unverified, \
                f"{c['name']}: weights_sha256 为 null 却未标 unverified"
    # 分片模型不得伪造单一全等哈希（openvla 三片 15.08 GB）
    ov = next(c for c in cands if c["name"] == "openvla-7b")
    assert ov["weights_sha256"] is None
    assert "sharded" in (ov.get("weights_sha256_scope") or "")
    assert len(ov["weights_files"]) == 3


# --------------------------------------------------------------------- AC-T6-2

def test_ac_t6_2_gate1_human_record():
    """Gate 1 真人核准记录存在（message_id + 时间戳 + 点击值），且路线图与 S0 候选一致。"""
    rec = _load(RUN / "gates" / "gate1_roadmap_record.json")
    assert rec["gate"] == 1
    assert rec["message_id"], "缺飞书 message_id"
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", rec["sent_at_utc"])
    assert rec.get("decided_at_utc"), "缺回调/决策时间戳"
    assert rec["decision"] == "approve", \
        f"Gate 1 未获真人核准：{rec.get('decision')!r}"
    # 修正（2026-11-04）：pending_gates.status 在库里是**大写枚举**
    # （`PENDING`/`DECIDED`，见 orchestrator/gateway 的 pending 表），记录件
    # 逐字落库值不改写；这里按大小写无关比较——判的是「终态」这件事，
    # 不是枚举字面量的大小写。不是放宽：非终态（PENDING/BLOCKED）仍然 FAIL。
    assert str(rec.get("pending_status") or "").lower() in (
        "approved", "decided", "resolved"), \
        f"pending 行终态异常：{rec.get('pending_status')!r}"

    # 卡片快照里必须写明与 S0 一致的候选与选择顺序（路线图 ↔ S0 一致性）
    snaps = sorted((RUN / "gates" / "card_snapshots").glob("gate1_roadmap_*.json"))
    assert snaps, "缺 Gate 1 卡片快照"
    card_text = snaps[-1].read_text(encoding="utf-8")
    s0 = _load(S0 / "vla_candidates.json")
    for name in [c["name"] for c in s0["candidates"]]:
        assert name in card_text, f"Gate 1 卡片未列 S0 候选 {name}"
    order = s0["strategy_order"]
    assert order == STRATEGY_ORDER, f"S0 选择顺序被改动：{order}"

    # 检查点侧互证：gate1_approved 事件在场
    camp = _load(RUN / "campaign_record.json")
    assert "gate1_approved" in (camp.get("events") or []), \
        f"检查点里没有 gate1_approved：{camp.get('events')}"


# --------------------------------------------------------------------- AC-T6-3

@pytest.mark.container
def test_ac_t6_3_baseline_video():
    """S1：MP4 可解码 1920×1080 帧数>0；telemetry 行>0；Rung 判定按预注册 §7.1。"""
    res = _load(RUN / "s1" / "stage_result.json")
    man = res["video_manifest"]
    assert man["count"] == 10, f"S1 应有 10 条视频，实得 {man['count']}"
    for v in man["videos"]:
        assert v.get("decodable") is True, f"{v['name']}: ffprobe 解码失败 {v.get('ffprobe_error')}"
        assert (v.get("width"), v.get("height")) == (1920, 1080), \
            f"{v['name']}: 分辨率 {(v.get('width'), v.get('height'))} != 1920×1080"
        assert (v.get("nb_frames") or 0) > 0, f"{v['name']}: 帧数为 0"
        assert v["bytes"] > 0

    judged = res["judge"]
    eps = judged["episodes"]
    assert len(eps) == 10 and len(res["judge"]["episodes"]) == 10
    for e in eps:
        assert (e.get("steps") or 0) > 0, f"{e['run_id']}: telemetry 行数为 0"
        assert e.get("error") is None, f"{e['run_id']}: 容器侧异常 {e['error']}"
    # 预注册 §7.1 ③：Rung1 全绿 + Rung2 接触子项全绿 + Rung3 出读数（带外 not_applicable）
    assert judged["rung1_all_pass"] is True, json.dumps(
        [(e["run_id"], e["rung1"].get("checks")) for e in eps
         if not e["rung1"].get("pass")], ensure_ascii=False)[:2000]
    assert judged["rung2_contact_all_pass"] is True, json.dumps(
        [(e["run_id"], e["rung2"]) for e in eps if not e["rung2"]["pass"]],
        ensure_ascii=False)[:2000]
    for e in eps:
        r3 = e["rung3"]
        assert r3["detail"] is not None, f"{e['run_id']}: Rung3 无读数"
        # LIBERO 20 Hz 出带 ⇒ 必须如实记 not_applicable，且不得声称通过
        assert r3["not_applicable"] is True, \
            f"{e['run_id']}: dt={r3['dt']} 竟落在冻结带内？判据/控制率需复核"
        assert r3["pass"] is None, "not_applicable 不得记成 pass"
        assert (r3["detail"] or {}).get("dt_band_ok") is False
        # 预注册 §8：带外也要逐 episode 报 jerk 原始值（报数不计分）
        assert r3["jerk_max_mps3_reported"] is not None, f"{e['run_id']}: 缺 jerk 读数"
        assert r3["jerk_reported_scored"] is False, "带外 jerk 不得计分"


# --------------------------------------------------------------------- AC-T6-4

def test_ac_t6_4_success_matrix():
    """对预注册枚举的每个策略：要么 10 episode 完整记录 + Wilson（端点 <1e-6），
    要么 degradations 含该策略；策略集为空或某策略两样都没有 ⇒ FAIL。"""
    camp = _load(RUN / "campaign_record.json")
    s2 = _load(RUN / "s2" / "stage_result.json")
    s3 = _load(RUN / "s3" / "stage_result.json")
    with_metrics = {s2.get("picked"): s2, s3.get("picked"): s3}
    deg_policies = {d["policy"] for d in (camp.get("degradations") or [])
                    if d.get("kind") == "policy_unreachable"}
    assert deg_policies | {p for p in with_metrics if p}, "策略集为空 ⇒ FAIL"

    for name in STRATEGY_ORDER:
        if name in deg_policies:
            continue                      # 有降级件，按 AC 不算 vacuous
        assert name in with_metrics, \
            f"{name} 既无指标件也无降级件 ⇒ FAIL（不得 vacuous pass）"
        stage = with_metrics[name]
        eps = stage["judge"]["episodes"]
        assert len(eps) == 10, f"{name}: episode 数 {len(eps)} != 10"
        for e in eps:
            for key in ("seed", "success", "steps", "vram_mib"):
                assert key in e, f"{name}/{e.get('run_id')}: 缺字段 {key}"
        k = sum(1 for e in eps if e["success"])
        n = len(eps)
        indep = _wilson(k, n)
        got = stage["wilson"]
        assert got["k"] == k and got["n"] == n
        assert abs(got["lower"] - indep["lower"]) < 1e-6, \
            f"{name}: Wilson 下界 {got['lower']} != 重算 {indep['lower']}"
        assert abs(got["upper"] - indep["upper"]) < 1e-6, \
            f"{name}: Wilson 上界 {got['upper']} != 重算 {indep['upper']}"
        # ③ 至少 1 次成功，或给出明确失败归因（付费归因件必须在场）
        if k == 0:
            attr = RUN / "paid" / f"{stage['stage']}_attribution.json"
            assert attr.is_file(), f"{name}: 0 成功却没有失败归因件"
            body = _load(attr)["response"]
            assert len(body.strip()) >= 80, "失败归因内容过短（疑似敷衍）"
        # 预注册 §7.2：0/10 是合法结论（§11 不翻案），不在这里要求非零成功率
        assert stage["pass"] is True, f"{name}: Stage 未通过（steps/error 见证据）"


# --------------------------------------------------------------------- AC-T6-5

@pytest.mark.kb_integration
def test_ac_t6_5_gate2_and_recycle_order():
    """Gate 2 真人 approve（message_id + 回调时间戳 + 检查点 gate2 决策三件互证）；
    回收动作只在 approve 之后发生；回收后 check_solutions rc=0 且四表口径不变。"""
    camp = _load(RUN / "campaign_record.json")
    gates2 = [g for g in camp["gates"] if g["gate"] == 2]
    assert len(gates2) == 3, f"Gate 2 应有三张终审卡，实得 {len(gates2)}"
    for g in gates2:
        assert g["message_id"] and g["sent_at_utc"] and g.get("decided_at_utc")
        assert g["decision"] == "approve", f"{g['label']}: 未获真人批准 {g['decision']!r}"
    status = camp.get("stage_status") or {}
    for sid in STAGE_IDS:
        assert status.get(sid) == "approved", f"{sid}: stage_status={status.get(sid)!r}"

    recycle = _load(TASK_ROOT / "manifests" / "RECYCLE.json")
    assert recycle["gate2_approved_at_utc"] <= recycle["recycled_at_utc"], \
        "回收时刻早于 Gate 2 批准 ⇒ 顺序违规"
    approved_ts = max(g["decided_at_utc"] for g in gates2)
    assert recycle["gate2_approved_at_utc"] >= approved_ts, \
        "RECYCLE 记录的批准时刻早于最后一张 Gate 2 卡"

    kb = Path(os.environ.get("KB_ROOT") or (Path.home() / "embodied-ai-sim-knowledge-base"))
    sol = kb / "solutions" / "track1_tools" / "04_libero_vla"
    assert sol.is_dir(), f"未回收方案目录：{sol}"
    proc = subprocess.run(
        [sys.executable, "scripts/kb/check_solutions.py"],
        cwd=str(REPO_ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        timeout=300, check=False)
    out = proc.stdout.decode()
    assert proc.returncode == 0, out[-2000:]
    oks = [ln for ln in out.splitlines() if ln.startswith("[solutions OK]")]
    assert len(oks) == 6, f"方案数应为 6（新增 04_libero_vla），实得 {len(oks)}：{oks}"

    counts = _load(TASK_ROOT / "manifests" / "table_counts_after_recycle.json")
    assert counts["kb_chunks"] == 1542, counts
    assert counts["phase1_learnings"] == 44, counts
    assert counts["solution_chunks"] == counts["solution_chunks_before"] + \
        recycle["solution_chunks_delta"], counts


# --------------------------------------------------------------------- AC-T6-6

def test_ac_t6_6_degradation_disclosure():
    """降级可见：4-bit 或 Octo 替代必须 `degradations` 非空且逐条说明；未降级为空列表。"""
    camp = _load(RUN / "campaign_record.json")
    degs = camp.get("degradations")
    assert isinstance(degs, list), "campaign_record 缺 degradations 列表"
    for d in degs:
        assert d.get("kind") in ("policy_unreachable", "4bit", "octo_substitute",
                                 "no_weights", "other"), d.get("kind")
        assert d.get("reason"), f"降级条目缺 reason：{d}"
        assert d.get("evidence"), f"降级条目缺 evidence：{d}"

    s2 = _load(RUN / "s2" / "stage_result.json")
    s3 = _load(RUN / "s3" / "stage_result.json")
    for stage in (s2, s3):
        picked = stage.get("picked") or ""
        if picked.endswith("-int4"):
            assert any(d["kind"] == "4bit" or "4-bit" in (d.get("reason") or "")
                       for d in degs), f"{picked}: 量化态未披露"

    report = (TASK_ROOT / "vla_report.md").read_text(encoding="utf-8")
    assert "degradations" in report, "报告缺 degradations 段"
    m = re.search(r"## 2\. 降级披露.*?```json\n(.*?)\n```", report, re.S)
    assert m, "报告里的 degradations 段格式不符"
    in_report = json.loads(m.group(1))
    assert [d.get("policy") for d in in_report] == \
        [d.get("policy") for d in degs], "报告与机读件的降级列表不一致"


# --------------------------------------------------------------------- AC-T6-7

def test_ac_t6_7_main_venv_untouched():
    """主 venv 物理隔离：前后 freeze 逐字节相等 + torch/numpy 版本 + 主 freeze 不含
    VLA 依赖；VLA 依赖只出现在 VLA venv/镜像的 freeze 里。"""
    before = S0 / "pip_freeze_before.txt"
    after = S0 / "pip_freeze_after.txt"
    assert before.is_file() and after.is_file()
    assert _sha256(before) == _sha256(after), "主 venv pip freeze 前后不相等（AC-T6-7 破防）"
    text = before.read_text(encoding="utf-8")

    iso = _load(S0 / "isolation_check.json")
    assert iso["ok"] is True, iso
    assert iso["byte_identical"] is True, "iso 记录自己承认前后不等"
    assert iso["main_venv_torch"].startswith("2.4.1+cu124"), iso["main_venv_torch"]
    assert iso["main_venv_numpy"] == "1.26.4", iso["main_venv_numpy"]
    assert iso["pip_freeze_before"]["sha256"] == _sha256(before), "iso 里的 before 哈希与文件不符"
    assert iso["pip_freeze_after"]["sha256"] == _sha256(after), "iso 里的 after 哈希与文件不符"

    for bad in ("flash-attn", "openvla", "flax"):
        assert bad not in text.lower(), f"主 venv 混入 VLA 依赖：{bad}"
    assert not re.search(r"^octo", text, re.M | re.I), "主 venv 混入 octo 包"

    vla = S0 / "vla_venv_pip_freeze.txt"
    assert vla.is_file(), "缺 VLA venv 的 freeze（隔离证据不全）"
    vtext = vla.read_text(encoding="utf-8")
    assert "transformers==" in vtext and "timm==" in vtext
    assert _sha256(vla) != _sha256(before), "两份 freeze 竟然相同 ⇒ 隔离未发生"


# --------------------------------------------------------------------- AC-T6-8

def test_ac_t6_8_vram_budget_and_order():
    """显存预算表逐候选给峰值与判定；vram 字段非空或 null+unverified；
    选择顺序恰为预注册三元组，且实际执行的首个成功策略不在序中更晚位置。"""
    budget = (S0 / "vram_budget.md").read_text(encoding="utf-8")
    assert "A800-40GB" in budget or "A800" in budget
    doc = _load(S0 / "vla_candidates.json")
    assert doc["strategy_order"] == STRATEGY_ORDER
    for c in doc["candidates"]:
        assert f"**{c['name']}**" in budget, f"预算表缺 {c['name']} 的权重口径"
        assert re.search(rf"\| {re.escape(c['name'])} \|", budget), \
            f"预算表缺 {c['name']} 的行"
        for key in ("vram_fp16", "vram_int4"):
            v = c.get(key)
            assert (v is not None and v > 0) or (
                v is None and any(key in u for u in (c.get("unverified") or []))
            ), f"{c['name']}.{key} 既无值也无 unverified 标记"
    # 判定列必须给出「可跑 / 4-bit / 改用更小模型」之一
    assert re.search(r"fp16 可跑|默认 4-bit|改用更小模型", budget)

    # 序列单调：实际首个成功策略 = 顺序里第一个不在「不可达」降级清单中的候选
    camp = _load(RUN / "campaign_record.json")
    unreachable = {d["policy"] for d in (camp.get("degradations") or [])
                   if d.get("kind") == "policy_unreachable"
                   and d.get("stage") == "s2"}
    first_reachable = next((p for p in STRATEGY_ORDER if p not in unreachable), None)
    assert first_reachable is not None, "三候选全不可达 ⇒ FAIL"
    assert camp["chosen"].get("s2") == first_reachable, \
        f"实际主策略 {camp['chosen'].get('s2')!r} 晚于序中最前可达者 {first_reachable!r}"

    # 修正（2026-11-04）：原先写的是「S3 必须选出对照策略」，这比计划更严：
    # 计划 D6-2 明写「单策略降级时 AC-T6-4 判『对照缺失』为**降级已知**
    # （需 degradations 非空），不判通过也不得空真」，AC-T6-8 正文只要求
    # 「实际执行的首个成功策略不在序中更晚位置（序列单调）」。
    # 这里按计划口径写：S3 要么选出一个与 S2 不同形态的对照，要么**逐个**
    # 列出其它候选的降级件——空真（既没对照也没降级说明）仍然 FAIL。
    s3_choice = camp["chosen"].get("s3")
    if s3_choice:
        assert s3_choice != camp["chosen"].get("s2"), \
            f"S3 对照与 S2 同形态（预注册 §5 要求换形态）"
    else:
        alternatives = [p for p in STRATEGY_ORDER if p != camp["chosen"].get("s2")]
        disclosed = {d["policy"] for d in (camp.get("degradations") or [])
                     if d.get("kind") in ("policy_unreachable", "4bit",
                                          "octo_substitute", "no_weights")}
        missing = [p for p in alternatives if p not in disclosed]
        assert not missing, (
            f"S3 未选出对照，且以下候选既无对照指标件也无降级件：{missing}"
            "（D6-2：对照缺失只能以逐条降级披露存在，不得空真）")
