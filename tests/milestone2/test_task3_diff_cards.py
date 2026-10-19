"""Phase 2 Task3 — 飞书 Diff View 双媒体看板（0 付费 0 联网）。

六组主测试 + 分流项（H-1 固化、S-9 前导横线、v0/v1→v2 迁移、纯选择器）：

  1. test_ascent_card_contains_two_columns_and_claims
     Gate1 升阶假设卡：V1 基线列 vs V2 假设列两列对照 + claims 表格
     （metric/方向/target/epsilon/task），按钮协议与标准 Gate1 完全一致。
  2. test_standard_path_card_unchanged
     build_gate1_card_v2 / build_gate2_card_v2 对快照文件字节级不回归。
  3. test_diff_gate2_sends_exactly_two_media_then_card
     假网关全局时序：claim 占位 → text① → video V1 → text② → video V2 →
     interactive 卡，多一少一皆 fail；V1 列 id 在 V2 推送前已回填（H-4 分列）。
  4. test_reattach_never_repushes_either_video（LOW-6 三场景）
     A 推完 v1 崩溃→B 只补 v2+卡（只发②标签）；两列都在→只发卡；
     卡已被他方发出→只等待（零发送）。每列视频全程至多 1 次推送。
  5. test_card_md_injection_escaped_on_all_llm_fields
     题面/方案名/focus/artifacts/claims.task 的 markdown 注入全部失效。
  6. H-1：无上下文 P2P 文本即便只有一张 PENDING 也不自动绑卡；
     回复双媒体任一 id（v1/v2 列新通路）可唯一定位。
  4b. QA-2：全空占位行存活观察窗三态（alive 零发送纯等待 / stale 窗内零变化
     接管 / gone 行消失 fail-loud），diff 与标准 Gate2 各三测；部分回填的崩溃
     重挂两测 spy 计数为 0（立即接管，零观察等待）。
  分流：S-9 compress_command 前导横线 ./ 前缀（实现 Task0 已有，此处补测）；
  v0→v2、v1→v2 原地迁移；select_gate1_card_view/select_gate2_requestor 纯选择器。

全部 monkeypatch requests.post / send_video_native；WS 不启动，无真实网络。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.feishu_gateway import client as client_mod
from orchestrator.feishu_gateway.cards import (
    _METRIC_LABELS,
    build_gate1_card_ascent,
    build_gate1_card_v2,
    build_gate2_card_diff_v2,
    build_gate2_card_v2,
    parse_card_event,
)
from orchestrator.feishu_gateway.client import (
    RUN_MODE_ASCENT,
    RUN_MODE_STANDARD,
    FeishuGateway,
    select_gate1_card_view,
    select_gate2_requestor,
)
from orchestrator.feishu_gateway.store import PendingStore, now_ms
from orchestrator.feishu_gateway.video import compress_command
from orchestrator.graph.ascent import ASCENT, STANDARD

APP_ID = "FIXTURE_APP_ID_000"
APP_SECRET = "FIXTURE_SECRET_" + "0" * 32
OWNER = "ou_fixture0000000000000000000000000000"
CHAT = "oc_fixture00000000000000000000000000000"

SNAPSHOT = Path(__file__).parent / "snapshots" / "task3_standard_cards.json"

# ---------- 合成事件 / 假 requests.post（与 milestone1 同一副夹具形状） ----------


class FakeResp:
    def __init__(self, body):
        self.status_code = 200
        self._body = body

    def json(self):
        return self._body


def make_fake_post(calls):
    seq = {"n": 0}

    def fake_post(url, *, json=None, data=None, files=None, headers=None,
                  timeout=None):
        if url.endswith("/auth/v3/tenant_access_token/internal"):
            return FakeResp({"code": 0, "tenant_access_token": "t-fixture-000",
                             "expire": 7200})
        if url.endswith("/im/v1/files"):
            seq["n"] += 1
            return FakeResp({"code": 0,
                             "data": {"file_key": f"file_key_{seq['n']}"}})
        if url.endswith("/im/v1/images"):
            seq["n"] += 1
            return FakeResp({"code": 0,
                             "data": {"image_key": f"img_key_{seq['n']}"}})
        if "/im/v1/messages" in url:
            msg_type = json["msg_type"]
            seq["n"] += 1
            mid = f"om_{msg_type}_{seq['n']}"
            calls.append({"kind": msg_type, "message_id": mid,
                          "payload": json, "content_raw": json.get("content")})
            return FakeResp({"code": 0, "msg": "success",
                             "data": {"message_id": mid, "chat_id": CHAT,
                                      "create_time": str(now_ms())}})
        raise AssertionError(f"未预期的 URL: {url}")

    return fake_post


@pytest.fixture()
def gw(tmp_path, monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(client_mod.requests, "post", make_fake_post(calls))
    g = FeishuGateway(
        APP_ID, APP_SECRET, OWNER,
        db_path=tmp_path / "state" / "pending_gates.db",
        evidence_dir=tmp_path / "task3_feishu",
        run_dir=str(tmp_path / "runs"),
        start_ws=False,
    )
    g._calls = calls
    yield g
    g.close()


def _wait_pending_card(g, thread, gate=2, timeout=5.0):
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        row = g.store.get_pending(thread, gate)
        if row and row["status"] == "PENDING" and row["card_message_id"]:
            return row["card_message_id"]
        time.sleep(0.02)
    raise AssertionError("等不到 PENDING 卡片行")


def _run_in_thread(target):
    box: dict = {}
    t = threading.Thread(target=lambda: box.update(target() or {}), daemon=True)
    t.start()
    return t, box


def _finish(gw, t, box, thread, gate=2):
    _wait_pending_card(gw, thread, gate)
    gw.store.mark_decided(thread, gate, decision="approve", source="cli")
    gw._set_event(thread, gate)
    t.join(5)
    assert box.get("decision") == "approve"


def _kinds(calls):
    return [c["kind"] for c in calls]


def _texts(calls):
    return [json.loads(c["content_raw"])["text"]
            for c in calls if c["kind"] == "text"]


def _install_video(gw, pushes, calls, *, fallback_paths=()):
    """装一个把推送也写进全局 calls 时序的假 send_video_native。"""

    def fake_video(video_path, *, duration_ms=None, cover_png=None, trim=None):
        mid = f"om_media_{video_path}"
        # 与真实 HTTP 同列表追加：calls 的全局顺序即真实发送顺序。
        calls.append({"kind": "video", "message_id": mid,
                      "payload": {"receive_id": OWNER, "video_path": video_path},
                      "content_raw": None})
        pushes.append(video_path)
        return {"ok": True,
                "video_fallback": video_path in fallback_paths,
                "ladder_step": 2, "media_message_id": mid,
                "media_shape": "file_key",
                "detail": f"原生视频发送成功:{video_path}", "attempts": []}

    gw.send_video_native = fake_video


def _diff_rows():
    # 六指标齐备（Pareto 指标集），V2 在成功率上严格超越、其余在 5% 容差内。
    return [
        {"metric": "success_rate", "v1": 0.8, "v2": 0.9},
        {"metric": "slip_dist_mean_m", "v1": 0.10, "v2": 0.099},
        {"metric": "postpush_vmax_mean_mps", "v1": 1.0, "v2": 1.01},
        {"metric": "tilt_max_deg_max", "v1": 10.0, "v2": 9.8},
        {"metric": "solve_wallclock_s", "v1": 100.0, "v2": 102.0},
        {"metric": "vram_peak_gb", "v1": 20.0, "v2": 19.5},
    ]


# ---------- 1. Gate1 升阶假设卡 ----------

def test_ascent_card_contains_two_columns_and_claims():
    proposals = [{
        "name": "p1", "sim_engine": "mujoco",
        "first_principles": "降低滑移",
        "expected_artifacts": ["a.mp4"],
        "ascent_claims": [
            {"metric": "success_rate", "direction": 1, "task": "ice_dr",
             "target": 0.9, "epsilon": 0.0},
            {"metric": "slip_dist_mean_m", "direction": -1, "task": "normal",
             "target": 0.01, "epsilon": 0.0005},
        ],
    }]
    card = build_gate1_card_ascent("推块入盒", proposals,
                                   v1_summary="V1 基线：成功率 0.8")
    blob = json.dumps(card, ensure_ascii=False)
    md_text = _card_md(card)
    assert card["schema"] == "2.0"
    # 两列对照 + claims 五要素（task 文本过 md_escape：下划线转义后形态）
    assert "V1 指标（冻结档案基线）" in md_text
    assert "V2 升阶假设（本候选承诺）" in md_text
    for needle in ("success_rate", "slip_dist_mean_m", "ice\\_dr", "normal",
                   "0.9", "0.01", "0.0005", "↑ 增大", "↓ 减小",
                   "V1 基线：成功率 0.8"):
        assert needle in md_text, needle
    assert "success_rate" in blob
    # 表单协议与标准 Gate1 完全一致：gate=1/decision/choice，parse 无需改协议
    form = card["body"]["elements"][-1]
    assert form["tag"] == "form" and form["name"] == "gate1_form_0"
    vals = [b["value"] for b in form["elements"] if b.get("action_type")]
    assert {"gate": 1, "decision": "approve", "choice": 0} in vals
    assert any(v["gate"] == 1 and v["decision"] == "reject_with_comments"
               and v["choice"] == 0 for v in vals)
    value, feedback, _, _, _ = parse_card_event(_form_event(vals[0]))
    assert value["decision"] == "approve"
    assert value["choice"] == 0 and value["gate"] == 1
    assert feedback == ""  # 未填意见
    # 无 claims 的候选必须显式提示，不许静默伪装
    card_no_claims = build_gate1_card_ascent("t", [{"name": "n"}])
    assert "未给出可量化超越假设" in json.dumps(card_no_claims, ensure_ascii=False)
    # fail-closed：方向反了/指标不存在/epsilon 负/bool 冒充 int（True==1 陷阱，
    # reviewer LOW-3：旧口径对 success_rate(+1)+True 会误放行）
    bad_cases = [
        {"metric": "success_rate", "direction": -1, "task": "x",
         "target": 1, "epsilon": 0},
        {"metric": "nope", "direction": 1, "task": "x",
         "target": 1, "epsilon": 0},
        {"metric": "success_rate", "direction": 1, "task": "x",
         "target": 1, "epsilon": -0.1},
        {"metric": "success_rate", "direction": True, "task": "x",
         "target": 1, "epsilon": 0},
        {"metric": "success_rate", "direction": False, "task": "x",
         "target": 1, "epsilon": 0},
    ]
    for claim in bad_cases:
        with pytest.raises(ValueError):
            build_gate1_card_ascent(
                "t", [{"name": "n", "ascent_claims": [claim]}])


def _form_event(value, *, feedback=None, operator=OWNER, message_id="om_c",
                form_value=None):
    fv = form_value if form_value is not None else (
        {"feedback_0": feedback} if feedback is not None else {})
    return SimpleNamespace(event=SimpleNamespace(
        action=SimpleNamespace(value=dict(value), form_value=fv),
        operator=SimpleNamespace(open_id=operator, union_id=None, user_id=None),
        context=SimpleNamespace(open_chat_id=CHAT, open_message_id=message_id)))


# ---------- 2. 标准卡字节级快照不回归 ----------

def test_standard_path_card_unchanged():
    proposals = [
        {"name": "MuJoCo 方案 A", "sim_engine": "mujoco315",
         "first_principles": "最小接触推送，先做摩擦标定再发力控",
         "expected_artifacts": ["runs/a/rollout.mp4", "runs/a/metrics.json"]},
        {"name": "Genesis 方案 B", "sim_engine": "genesis133",
         "first_principles": "刚体接触基线对照",
         "expected_artifacts": ["runs/b/rollout.mp4"]},
    ]
    title = "快照任务：推块入盒（标准路径不回归）"
    metrics = {"pose_error": "0.018", "max_contact_depth": "0.004",
               "contact_duration": "1.2s", "jerk": "中"}
    blob = {
        "gate1_v2": build_gate1_card_v2(title, proposals),
        "gate2_v2": build_gate2_card_v2(
            title, metrics, "http://headless.local/run/x",
            video_fallback=False,
            explanation={"task": "推块入盒", "problem": "验证最小接触",
                         "video": "俯视滚出"},
            trim_note="原片 42.0s，裁剪区间 0.0–30.0s"),
    }
    canonical = json.dumps(blob, ensure_ascii=False, sort_keys=True)
    snap = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    snapshot_canonical = json.dumps(snap, ensure_ascii=False, sort_keys=True)
    assert canonical == snapshot_canonical, (
        "标准 Gate1/Gate2 卡结构发生漂移：如为有意修改，请用 Task3 快照生成"
        "脚本重生成并人工对 diff；否则即不回归破坏。")


# ---------- 3. Diff Gate2：严格两媒体 + 卡片全局时序 ----------

def test_diff_gate2_sends_exactly_two_media_then_card(gw):
    pushes: list[str] = []
    _install_video(gw, pushes, gw._calls)
    rows = _diff_rows()

    def call():
        return gw.request_gate2_diff_approval(
            "th-diff-1", "任务：冰面推盒", "v1.mp4", "v2.mp4",
            rows, "v2_dominates", max_wait=30)

    t, box = _run_in_thread(call)
    _wait_pending_card(gw, "th-diff-1", 2)

    kinds = _kinds(gw._calls)
    # 严格顺序：标签① → 视频① → 标签② → 视频② → 卡片（多一少一皆 fail）
    assert kinds == ["text", "video", "text", "video", "interactive"], kinds
    texts = _texts(gw._calls)
    assert texts == [gw._DIFF_LABEL_V1, gw._DIFF_LABEL_V2]
    assert pushes == ["v1.mp4", "v2.mp4"]
    row = gw.store.get_pending("th-diff-1", 2)
    assert row["media_message_id_v1"] == "om_media_v1.mp4"
    assert row["media_message_id_v2"] == "om_media_v2.mp4"
    # 老单列在双媒体路径上保持 NULL（两条路径互不写对方的列）
    assert row["media_message_id"] is None
    card_call = gw._calls[-1]
    card = json.loads(card_call["content_raw"])
    card_blob = json.dumps(card, ensure_ascii=False)
    for needle in ("V1（冻结档案）", "V2（本次）", "机判结论", "人类最终裁决",
                   "V2 支配 V1", "成功率", "显存峰值"):
        assert needle in card_blob, needle
    # Δ 列带符号、方向列含改善箭头
    assert "+0.1" in card_blob and "↑ 改善" in card_blob
    gw.store.mark_decided("th-diff-1", 2, decision="approve", source="cli")
    gw._set_event("th-diff-1", 2)
    t.join(5)
    assert box.get("decision") == "approve"


def test_diff_columns_backfill_order_and_parse_protocol(gw):
    """H-4 分列：V1 必须在 V2 推送前回填；Diff 卡按钮走既有 gate=2 解析。"""
    pushes: list[str] = []
    observations: list[tuple[str, object, object]] = []

    def fake_video(video_path, *, duration_ms=None, cover_png=None, trim=None):
        row = gw.store.get_pending("th-diff-o", 2)
        observations.append((video_path,
                             row["media_message_id_v1"],
                             row["media_message_id_v2"]))
        mid = f"om_media_{video_path}"
        gw._calls.append({"kind": "video", "message_id": mid,
                          "payload": {"video_path": video_path},
                          "content_raw": None})
        pushes.append(video_path)
        return {"ok": True, "video_fallback": False, "ladder_step": 2,
                "media_message_id": mid, "media_shape": "file_key",
                "detail": "ok", "attempts": []}

    gw.send_video_native = fake_video
    t, box = _run_in_thread(lambda: gw.request_gate2_diff_approval(
        "th-diff-o", "t", "a.mp4", "b.mp4", _diff_rows(),
        {"verdict": "non_dominated"}, max_wait=30))
    _wait_pending_card(gw, "th-diff-o", 2)
    # 推 V2 那一刻：v1 列已落 id、v2 列仍 NULL（崩溃接管只补缺件的前提）
    assert observations == [("a.mp4", None, None),
                            ("b.mp4", "om_media_a.mp4", None)]
    gw.store.mark_decided("th-diff-o", 2, decision="approve", source="cli")
    gw._set_event("th-diff-o", 2)
    t.join(5)
    assert box.get("decision") == "approve"
    # 卡按钮 value 协议不变：parse_card_event 直接解析 gate=2
    card = json.loads(gw._calls[-1]["content_raw"])
    forms = [e for e in _walk_elements(card) if e.get("tag") == "form"]
    assert forms and forms[0]["name"] == "gate2_form"
    btn = next(b for e in forms[0]["elements"]
               if (b := e).get("action_type") == "form_submit"
               and b["value"]["decision"] == "approve")
    value, feedback, _, _, _ = parse_card_event(_form_event(btn["value"]))
    assert value["gate"] == 2 and value["decision"] == "approve"


def _card_md(card):
    """拼接 schema 2.0 卡全部 div 的 lark_md 文本（避免 JSON 双重转义干扰）。"""
    parts = []
    for e in card.get("body", {}).get("elements", []):
        if e.get("tag") == "div":
            parts.append(e["text"]["content"])
        elif e.get("tag") == "markdown":
            parts.append(e["content"])
    return "\n".join(parts)


def _walk_elements(card):
    if isinstance(card, dict):
        if card.get("tag") in {"form", "button", "input"}:
            yield card
        for v in card.values():
            yield from _walk_elements(v)
    elif isinstance(card, list):
        for v in card:
            yield from _walk_elements(v)


def test_diff_gate2_validates_before_any_side_effect(gw):
    """非法 verdict/Δ 行在占位与任何推送之前 fail-closed。"""
    pushes: list[str] = []
    _install_video(gw, pushes, gw._calls)
    with pytest.raises(ValueError):
        gw.request_gate2_diff_approval(
            "th-bad", "t", "v1.mp4", "v2.mp4", _diff_rows(), "wat", max_wait=1)
    with pytest.raises(ValueError):
        gw.request_gate2_diff_approval(
            "th-bad", "t", "v1.mp4", "v2.mp4",
            [{"metric": "success_rate", "v1": 0.5, "v2": float("nan")}],
            "v2_dominates", max_wait=1)
    assert gw._calls == [] and pushes == []
    assert gw.store.get_pending("th-bad", 2) is None


# ---------- 4. LOW-6：崩溃接管，两列各自不重推 ----------

def _claim_placeholder(gw, thread, title="接管任务", **cols):
    """构造他方进程遗留的占位行（claim + 若干列回填，模拟崩溃现场）。"""
    assert gw.store.claim_pending(thread, 2, task_title=title)
    if cols:
        gw.store.attach_message_ids(thread, 2, **cols)


def test_reattach_after_crash_pushes_only_missing_column(gw):
    # A 推完 v1（已回填 v1 列）后崩溃：无 v2、无卡片。
    _claim_placeholder(gw, "th-crash",
                       media_message_id_v1="om_media_v1_saved")
    pushes: list[str] = []
    _install_video(gw, pushes, gw._calls)

    def boom(*a, **k):
        raise AssertionError("V1 已送达，接管路径不得重推 V1")

    # 接管方：v1 列已存在 → 不允许 v1.mp4 再被推；用包装在路由层钉死。
    orig = gw.send_video_native

    def routed(video_path, **k):
        assert video_path != "v1.mp4", "V1 被重复推送"
        return orig(video_path, **k)

    gw.send_video_native = routed
    # QA-2：部分回填的崩溃现场必须零等待——根本不进存活观察窗
    observer = _spy_observer(gw)
    t, box = _run_in_thread(lambda: gw.request_gate2_diff_approval(
        "th-crash", "接管任务", "v1.mp4", "v2.mp4",
        _diff_rows(), "non_dominated", max_wait=30))
    _finish(gw, t, box, "th-crash")
    kinds = _kinds(gw._calls)
    # 只发②标签 → v2 → 卡；①标签不重发
    assert kinds == ["text", "video", "interactive"], kinds
    assert _texts(gw._calls) == [gw._DIFF_LABEL_V2]
    assert pushes == ["v2.mp4"]
    assert observer == []  # 观察窗 spy 计数为 0（立即接管）
    row = gw.store.get_pending("th-crash", 2)
    assert row["media_message_id_v1"] == "om_media_v1_saved"
    assert row["media_message_id_v2"] == "om_media_v2.mp4"


def test_reattach_both_media_present_sends_card_only(gw):
    # 两列均已送达、仅卡片未发出（崩在发卡前）：零标签零视频，只补卡。
    _claim_placeholder(
        gw, "th-both",
        media_message_id_v1="om_media_v1_saved",
        media_message_id_v2="om_media_v2_saved")
    pushes: list[str] = []

    def must_not_push(*a, **k):
        raise AssertionError("两列均已送达，不得再推任何视频")

    gw.send_video_native = must_not_push
    # QA-2：两列都在的崩溃现场同样立即接管，不进观察窗、零等待
    observer = _spy_observer(gw)
    t, box = _run_in_thread(lambda: gw.request_gate2_diff_approval(
        "th-both", "接管任务", "v1.mp4", "v2.mp4",
        _diff_rows(), "v2_dominates", max_wait=30))
    _finish(gw, t, box, "th-both")
    assert _kinds(gw._calls) == ["interactive"]
    assert _texts(gw._calls) == [] and pushes == []
    assert observer == []  # 观察窗 spy 计数为 0（立即接管）
    row = gw.store.get_pending("th-both", 2)
    assert row["media_message_id_v1"] == "om_media_v1_saved"
    assert row["media_message_id_v2"] == "om_media_v2_saved"


def test_card_already_sent_by_other_party_waits_only(gw):
    # 他方已完成全部推送并发卡：本次调用零发送，纯等待同一裁决。
    gw.store.upsert_pending(
        "th-held", 2, card_message_id="om_card_held", open_chat_id=CHAT,
        task_title="他方任务")
    gw.store.attach_message_ids(
        "th-held", 2,
        media_message_id_v1="om_media_v1_x",
        media_message_id_v2="om_media_v2_x")
    pushes: list[str] = []
    _install_video(gw, pushes, gw._calls)
    t, box = _run_in_thread(lambda: gw.request_gate2_diff_approval(
        "th-held", "他方任务", "v1.mp4", "v2.mp4",
        _diff_rows(), "v2_dominates", max_wait=30))
    _finish(gw, t, box, "th-held")
    # 连卡片都不重发（fast path：PENDING + card_message_id）
    assert gw._calls == [] and pushes == []


def test_missing_column_without_path_fails_closed(gw):
    """缺列且调用方不给路径：禁止用单媒体冒充双媒体看板。"""
    _claim_placeholder(gw, "th-missing", media_message_id_v1="om_mv1")
    pushes: list[str] = []
    _install_video(gw, pushes, gw._calls)
    with pytest.raises(ValueError, match="v2"):
        gw.request_gate2_diff_approval(
            "th-missing", "t", "v1.mp4", None,
            _diff_rows(), "non_dominated", max_wait=1)
    assert pushes == []
    # 卡未发出、v1 未重推
    assert _kinds(gw._calls) == []


# ---------- 4b. QA-2：全空占位行存活观察窗（alive/stale/gone） ----------
# 双活是真实场景：Task6 runbook 含 kill-9 resume，同一 (thread, gate) 可能有
# 两个存活调用方；claim 失败读到全空行时无法区分「他方崩溃遗留」与「他方存活
# 处于秒级上传窗」。观察窗纯读四消息列，三态裁决，测试零真实 sleep/网络。

def _spy_observer(gw):
    """包住 _observe_claimed_row 记录进入次数（部分回填现场必须一次不进）。"""
    entered: list[tuple] = []
    orig = gw._observe_claimed_row

    def wrapped(*a, **k):
        entered.append((a, k))
        return orig(*a, **k)

    gw._observe_claimed_row = wrapped
    return entered


def _patch_claim_observe(monkeypatch, on_tick):
    """观察窗钉成单 tick、零真实 sleep；每 tick 前跑 on_tick() 模拟他方动作。

    client 全模块只有观察窗一处 time.sleep（QA-2 设计），补丁随 monkeypatch
    teardown 自动回收，不污染其他测试。
    """
    monkeypatch.setattr(client_mod, "CLAIM_OBSERVE_WINDOW_S", 0.0)
    monkeypatch.setattr(client_mod, "CLAIM_OBSERVE_TICK_S", 0.0)

    def fake_sleep(_seconds):
        on_tick()

    monkeypatch.setattr(client_mod.time, "sleep", fake_sleep)


def _watch_decide_when_waiting(gw, thread, gate=2, *, decision="approve"):
    """他方裁决：等本次调用登记等待事件（进入 _await_decision）后立刻裁决。

    用 Event.wait 轮询而非 time.sleep——观察窗测试把 time.sleep 钉成了假的。
    """
    key = (thread, gate)
    tick = threading.Event()

    def watcher():
        for _ in range(1000):
            if key in gw._events:
                gw.store.mark_decided(thread, gate, decision=decision,
                                      source="cli")
                gw._set_event(thread, gate)
                return
            tick.wait(0.002)
        raise AssertionError("等不到本次调用进入 _await_decision")

    return threading.Thread(target=watcher, daemon=True)


def test_dual_live_other_grows_column_sends_nothing(gw, monkeypatch):
    thread = "th-dualive"
    # 他方 claim 成功、四消息列全空（秒级上传窗刚开始的现场）
    assert gw.store.claim_pending(thread, 2, task_title="他方任务")
    pushes: list[str] = []
    _install_video(gw, pushes, gw._calls)

    def on_tick():
        # 他方在观察窗内完成 V1 列回填 → 四列之一增长，证明他方存活
        gw.store.attach_message_ids(thread, 2, media_message_id_v1="om_other_v1")

    _patch_claim_observe(monkeypatch, on_tick)
    watcher = _watch_decide_when_waiting(gw, thread)
    watcher.start()
    t, box = _run_in_thread(lambda: gw.request_gate2_diff_approval(
        thread, "他方任务", "v1.mp4", "v2.mp4",
        _diff_rows(), "v2_dominates", max_wait=30))
    t.join(5)
    watcher.join(2)
    assert not t.is_alive() and not watcher.is_alive()
    assert box.get("decision") == "approve"
    # 双活零重复推送：本次调用零标签/零视频/零卡片
    assert gw._calls == [] and pushes == []
    # 他方回填的列原样保留，本次未 COALESCE 任何自己的消息 id
    row = gw.store.get_pending(thread, 2)
    assert row["status"] == "DECIDED"
    assert row["media_message_id_v1"] == "om_other_v1"
    assert row["media_message_id_v2"] is None and row["card_message_id"] is None


def test_bare_placeholder_stale_after_window_takes_over(gw, monkeypatch):
    thread = "th-stale"
    assert gw.store.claim_pending(thread, 2, task_title="崩溃方")
    pushes: list[str] = []
    _install_video(gw, pushes, gw._calls)
    # 观察窗内行列值毫无变化（他方已 kill -9）→ 判陈旧，本次接管补发
    _patch_claim_observe(monkeypatch, lambda: None)
    t, box = _run_in_thread(lambda: gw.request_gate2_diff_approval(
        thread, "接管任务", "v1.mp4", "v2.mp4",
        _diff_rows(), "non_dominated", max_wait=30))
    _finish(gw, t, box, thread)
    # 接管序列仍是严格 ①text→①video→②text→②video→card，一件不多一件不少
    assert _kinds(gw._calls) == [
        "text", "video", "text", "video", "interactive"]
    assert _texts(gw._calls) == [gw._DIFF_LABEL_V1, gw._DIFF_LABEL_V2]
    assert pushes == ["v1.mp4", "v2.mp4"]


def test_placeholder_row_vanishes_fail_closed(gw, monkeypatch):
    thread = "th-gone"
    assert gw.store.claim_pending(thread, 2, task_title="他方任务")
    pushes: list[str] = []
    _install_video(gw, pushes, gw._calls)

    def on_tick():
        # 极端窗口：观察期间他方收裁决并归档，PENDING 行消失
        gw.store.mark_decided(thread, 2, decision="approve", source="cli")
        assert gw.store.archive_decided(thread, 2)

    _patch_claim_observe(monkeypatch, on_tick)
    # fail-loud：明确 RuntimeError，零标签/零视频/零卡片，不新建占位（LOW-4）
    with pytest.raises(RuntimeError, match="占位行在 claim 后消失"):
        gw.request_gate2_diff_approval(
            thread, "他方任务", "v1.mp4", "v2.mp4",
            _diff_rows(), "v2_dominates", max_wait=1)
    assert gw._calls == [] and pushes == []
    assert gw.store.get_pending(thread, 2) is None


def test_standard_gate2_dual_live_other_grows_column_sends_nothing(
        gw, monkeypatch):
    thread = "th-stdalive"
    assert gw.store.claim_pending(thread, 2, task_title="他方任务")
    pushes: list[str] = []
    _install_video(gw, pushes, gw._calls)

    def on_tick():
        # 标准 Gate2 列在观察窗内回填 → 他方存活
        gw.store.attach_message_ids(thread, 2, media_message_id="om_other_m")

    _patch_claim_observe(monkeypatch, on_tick)
    watcher = _watch_decide_when_waiting(gw, thread)
    watcher.start()
    t, box = _run_in_thread(lambda: gw.request_gate2_approval(
        thread, "他方任务", {"pose_error": "0.02"}, "std.mp4",
        max_wait=30))
    t.join(5)
    watcher.join(2)
    assert not t.is_alive() and not watcher.is_alive()
    assert box.get("decision") == "approve"
    # 标准路径双活同样零视频零卡片
    assert gw._calls == [] and pushes == []
    row = gw.store.get_pending(thread, 2)
    assert row["media_message_id"] == "om_other_m"
    assert row["card_message_id"] is None


def test_standard_gate2_bare_placeholder_stale_takes_over(gw, monkeypatch):
    thread = "th-stdstall"
    assert gw.store.claim_pending(thread, 2, task_title="崩溃方")
    pushes: list[str] = []
    _install_video(gw, pushes, gw._calls)
    _patch_claim_observe(monkeypatch, lambda: None)
    t, box = _run_in_thread(lambda: gw.request_gate2_approval(
        thread, "接管任务", {"pose_error": "0.02"}, "std.mp4",
        max_wait=30))
    _finish(gw, t, box, thread)
    # 标准接管序列：视频 → 卡片，恰两件
    assert _kinds(gw._calls) == ["video", "interactive"]
    assert pushes == ["std.mp4"]
    row = gw.store.get_pending(thread, 2)
    assert row["media_message_id"] == "om_media_std.mp4"
    assert row["card_message_id"]


def test_standard_gate2_placeholder_vanishes_fail_closed(gw, monkeypatch):
    thread = "th-stdgone"
    assert gw.store.claim_pending(thread, 2, task_title="他方任务")
    pushes: list[str] = []
    _install_video(gw, pushes, gw._calls)

    def on_tick():
        gw.store.mark_decided(thread, 2, decision="approve", source="cli")
        assert gw.store.archive_decided(thread, 2)

    _patch_claim_observe(monkeypatch, on_tick)
    with pytest.raises(RuntimeError, match="占位行在 claim 后消失"):
        gw.request_gate2_approval(
            thread, "他方任务", {"pose_error": "0.02"}, "std.mp4",
            max_wait=1)
    assert gw._calls == [] and pushes == []
    assert gw.store.get_pending(thread, 2) is None


# ---------- 4c. R-LOW-2：观察窗/发送前状态翻转即放弃接管（零孤儿消息） ----------
# 洞：双活竞争同一占位行时，对手可不经任何消息列（CLI/P2P 文本通道）把行裁定
# 为 DECIDED/EXPIRED。旧观察只比对四个消息列，会把「四列全空但已终结」误判为
# stale 接管补发，产生孤儿媒体/孤儿卡。修法：观察窗内状态翻转优先判 gone；
# standard/diff 两请求方在首个发送副作用前再 fresh 重读一次（TOCTOU）。


def test_rlow2_observe_status_flip_without_column_change_aborts_standard(
        gw, monkeypatch):
    # (a) 观察窗内四消息列完全不变、仅 status PENDING→DECIDED：观察判 gone，
    # 标准 Gate2 零飞书发送，抛现有孤儿 RuntimeError。
    thread = "th-rlow2-flip"
    assert gw.store.claim_pending(thread, 2, task_title="他方任务")
    pushes: list[str] = []
    _install_video(gw, pushes, gw._calls)

    def on_tick():
        # 他方在窗内经 CLI 通道收裁决：不动任何消息列，只翻 status
        assert gw.store.mark_decided(
            thread, 2, decision="approve", source="cli")

    _patch_claim_observe(monkeypatch, on_tick)
    verdicts: list[str] = []
    orig_observe = gw._observe_claimed_row

    def wrapped_observe(*a, **k):
        verdict = orig_observe(*a, **k)
        verdicts.append(verdict)
        return verdict

    gw._observe_claimed_row = wrapped_observe
    with pytest.raises(RuntimeError, match="拒绝发送孤儿媒体/孤儿卡"):
        gw.request_gate2_approval(
            thread, "他方任务", {"pose_error": "0.02"}, "std.mp4",
            max_wait=1)
    # 观察窗本身必须判 gone（而非 stale 接管、再被发送前闸拦下）
    assert verdicts == ["gone"]
    # 零飞书副作用：无视频、无卡片
    assert gw._calls == [] and pushes == []
    row = gw.store.get_pending(thread, 2)
    assert row["status"] == "DECIDED"
    assert row["card_message_id"] is None
    assert row["media_message_id"] is None


@pytest.mark.parametrize("path,terminal_status", [
    ("standard", "DECIDED"),
    ("standard", "EXPIRED"),
    ("diff", "DECIDED"),
    ("diff", "EXPIRED"),
])
def test_rlow2_fresh_reread_before_first_send_aborts_both_requestors(
        gw, path, terminal_status):
    # (b) claim 成功后、首个发送副作用之前行被他方终结：standard/diff 两请求方
    # 都在 fresh 重读处放弃——零卡片/零标签/零 v1/v2 视频，四个附件列保持 NULL。
    thread = f"th-rlow2-fresh-{path}-{terminal_status.lower()}"
    pushes: list[str] = []
    _install_video(gw, pushes, gw._calls)

    real_claim = gw.store.claim_pending

    def claim_then_other_party_terminates(*a, **k):
        ok = real_claim(*a, **k)
        assert ok is True  # 本次调用自己占位成功，翻转发生在其后的 TOCTOU 窗口
        if terminal_status == "DECIDED":
            assert gw.store.mark_decided(
                thread, 2, decision="approve", source="cli")
        else:
            assert gw.store.mark_expired(thread, 2)
        return ok

    gw.store.claim_pending = claim_then_other_party_terminates
    with pytest.raises(RuntimeError, match="拒绝发送孤儿媒体/孤儿卡"):
        if path == "standard":
            gw.request_gate2_approval(
                thread, "任务", {"pose_error": "0.02"}, "std.mp4", max_wait=1)
        else:
            gw.request_gate2_diff_approval(
                thread, "任务", "v1.mp4", "v2.mp4",
                _diff_rows(), "non_dominated", max_wait=1)
    assert gw._calls == [] and pushes == []
    row = gw.store.get_pending(thread, 2)
    assert row["status"] == terminal_status
    assert row["card_message_id"] is None
    assert row["media_message_id"] is None
    assert row["media_message_id_v1"] is None
    assert row["media_message_id_v2"] is None


# ---------- 4d. R-LOW-3：发送序列内状态复查 + attach 0 行孤儿留痕 ----------
# R-LOW-2 的 fresh 闸只闸住首个副作用前；对手在秒级上传窗内翻转时，已拿到
# Feishu 消息 id 的媒体/卡片会变孤儿。修法（best-effort recall，不保证强
# 一致）：序列内每个剩余副作用前再 fresh 复查，未发的不发；attach_message_ids
# 返回影响行数，0 行（占位行已终结）经 _attach_ids_or_warn 打孤儿告警，
# 卡片后的最后一次回填 0 行直接 fail-loud。logger propagate=False，caplog
# 在 call 阶段对 fixture 期已存在的非传播 logger 挂 handler（pytest 9
# catching_logs 行为），故所有断言经 gw fixture 捕获。


def _flip_cli(gw, thread, gate=2):
    return gw.store.mark_decided(thread, gate, decision="approve", source="cli")


def _flip_inside_video(gw, pushes, thread, *, flip_paths):
    """假 send_video_native：正常记账推送，flip_paths 中的路径在返回前翻转状态。"""

    def fake_video(video_path, *, duration_ms=None, cover_png=None, trim=None):
        mid = f"om_media_{video_path}"
        gw._calls.append({"kind": "video", "message_id": mid,
                          "payload": {"video_path": video_path},
                          "content_raw": None})
        pushes.append(video_path)
        if video_path in flip_paths:
            # 模拟秒级上传窗：Feishu 已发、id 已拿到，他方同期 CLI 落裁决
            assert _flip_cli(gw, thread)
        return {"ok": True, "video_fallback": False, "ladder_step": 2,
                "media_message_id": mid, "media_shape": "file_key",
                "detail": f"原生视频发送成功:{video_path}", "attempts": []}

    gw.send_video_native = fake_video


def _install_flip_card(gw, thread, *, card_id="om_card_flip"):
    """假 _send_interactive：翻转状态但不记账 interactive 推送（卡未实际落飞书）。

    生产随后 attach 0 行检测到行已终结，打卡片孤儿告警并 fail-loud。
    """

    def flip_then_no_push(receive_id, card1, card2, *, form_required=False):
        assert _flip_cli(gw, thread)
        return {"message_id": card_id, "chat_id": CHAT, "schema": "2.0"}

    gw._send_interactive = flip_then_no_push


def test_rlow3_standard_flip_during_video_upload_orphans_video(gw, caplog):
    # (a) 标准 Gate2：翻转落在视频上传窗内——视频恰发 1 次、卡片 0、抛错；
    # 视频 attach 0 行留孤儿告警，media 列保持 NULL（孤儿语义必须断言）。
    thread = "th-rlow3-vup"
    caplog.set_level(logging.WARNING)
    pushes: list[str] = []
    _flip_inside_video(gw, pushes, thread, flip_paths={"std.mp4"})
    with pytest.raises(RuntimeError, match="孤儿"):
        gw.request_gate2_approval(
            thread, "任务", {"pose_error": "0.02"}, "std.mp4", max_wait=1)
    assert pushes == ["std.mp4"]
    assert _kinds(gw._calls) == ["video"]  # 卡片未发
    row = gw.store.get_pending(thread, 2)
    assert row["status"] == "DECIDED" and row["source"] == "cli"
    # 0 行回填：刚发出的视频 id 无处落账，四列如实保持 NULL
    assert row["media_message_id"] is None
    assert row["card_message_id"] is None and row["open_chat_id"] is None
    # 孤儿告警必须带 thread/gate 与所发 id，供运维召回
    assert "孤儿，回填 0 行" in caplog.text
    assert thread in caplog.text and "om_media_std.mp4" in caplog.text
    assert "Gate2 视频" in caplog.text


def test_rlow3_standard_flip_after_video_attach_before_card(gw, caplog):
    # (b) 标准 Gate2：视频正常返回且 attach 成功后、卡片发送前翻转——卡 0、
    # raise；media 列有值（非孤儿），只允许出现卡片孤儿告警。
    # 时序注记：翻转点在假 _send_interactive 入口（生产卡片前复查之后）。
    # 若翻转提前到视频 attach 之前（见上一测），media 列会是 NULL 且出现
    # 「Gate2 视频」孤儿告警；两种时序由 attach 与翻转的先后决定。
    thread = "th-rlow3-vok"
    caplog.set_level(logging.WARNING)
    pushes: list[str] = []
    _install_video(gw, pushes, gw._calls)
    _install_flip_card(gw, thread, card_id="om_card_flip_std")
    with pytest.raises(RuntimeError, match="孤儿卡片"):
        gw.request_gate2_approval(
            thread, "任务", {"pose_error": "0.02"}, "std.mp4", max_wait=1)
    assert pushes == ["std.mp4"]
    assert _kinds(gw._calls) == ["video"]  # 假卡不记账，卡片实际 0
    row = gw.store.get_pending(thread, 2)
    assert row["status"] == "DECIDED"
    assert row["media_message_id"] == "om_media_std.mp4"  # 翻转前已回填
    assert row["card_message_id"] is None
    assert "Gate2 卡片" in caplog.text and "om_card_flip_std" in caplog.text
    assert "Gate2 视频" not in caplog.text  # 视频不是孤儿


def test_rlow3_diff_flip_between_v1_and_v2(gw, caplog):
    # (c) Diff Gate2：翻转落在 V1 上传窗（V1/V2 之间被复查发现）——
    # V1 视频 1、其标签 1；V2 标签/视频 0、卡 0；V1 attach 0 行留痕，
    # 三个未发件相关列（v2/card）与 v1 列本身全部 NULL。
    thread = "th-rlow3-dv1"
    caplog.set_level(logging.WARNING)
    pushes: list[str] = []
    _flip_inside_video(gw, pushes, thread, flip_paths={"v1.mp4"})
    with pytest.raises(RuntimeError, match="V2 媒体发送前已终结"):
        gw.request_gate2_diff_approval(
            thread, "任务", "v1.mp4", "v2.mp4",
            _diff_rows(), "non_dominated", max_wait=1)
    assert pushes == ["v1.mp4"]
    # ①标签 + ①视频已发；②标签与②视频、卡片均未发
    assert _kinds(gw._calls) == ["text", "video"]
    assert _texts(gw._calls) == [gw._DIFF_LABEL_V1]
    row = gw.store.get_pending(thread, 2)
    assert row["status"] == "DECIDED" and row["source"] == "cli"
    assert row["media_message_id_v1"] is None  # attach 0 行，V1 是孤儿
    assert row["media_message_id_v2"] is None and row["card_message_id"] is None
    assert row["media_message_id"] is None  # 老单列恒不被 diff 路径写
    assert "Gate2(diff) V1 列视频" in caplog.text
    assert "om_media_v1.mp4" in caplog.text and thread in caplog.text


def test_rlow3_diff_flip_after_v2_before_card(gw, caplog):
    # (d) Diff Gate2：两列视频都正常送达回填后、卡片前翻转——两视频各 1、
    # 卡 0、raise；v1/v2 列有值（非孤儿），只有卡片孤儿告警。
    thread = "th-rlow3-dv2"
    caplog.set_level(logging.WARNING)
    pushes: list[str] = []
    _install_video(gw, pushes, gw._calls)
    _install_flip_card(gw, thread, card_id="om_card_flip_diff")
    with pytest.raises(RuntimeError, match="孤儿卡片"):
        gw.request_gate2_diff_approval(
            thread, "任务", "v1.mp4", "v2.mp4",
            _diff_rows(), "non_dominated", max_wait=1)
    assert pushes == ["v1.mp4", "v2.mp4"]
    assert _kinds(gw._calls) == ["text", "video", "text", "video"]
    assert _texts(gw._calls) == [gw._DIFF_LABEL_V1, gw._DIFF_LABEL_V2]
    row = gw.store.get_pending(thread, 2)
    assert row["status"] == "DECIDED"
    assert row["media_message_id_v1"] == "om_media_v1.mp4"
    assert row["media_message_id_v2"] == "om_media_v2.mp4"
    assert row["card_message_id"] is None
    assert "Gate2(diff) 卡片" in caplog.text
    assert "V1 列视频" not in caplog.text and "V2 列视频" not in caplog.text


def test_rlow3_store_attach_rowcount_semantics(tmp_path):
    # store 层：1=PENDING 正常回填（含纯 COALESCE no-op 仍算 1）；
    # 0=行已终结（不抛、不写，刚发出的即孤儿，由调用方告警）；
    # None=无填充列，本次未尝试回填。
    store = PendingStore(tmp_path / "state" / "pending.db")
    t, g = "th-rlow3-store", 2
    assert store.claim_pending(t, g, task_title="t") is True
    n = store.attach_message_ids(t, g, media_message_id="om_m1")
    assert n == 1
    assert store.get_pending(t, g)["media_message_id"] == "om_m1"
    # 行存活时重复 COALESCE 回填（全 no-op）仍是 1，不得误报孤儿
    assert store.attach_message_ids(t, g, media_message_id="om_m1") == 1
    assert store.mark_decided(t, g, decision="approve", source="cli") is True
    assert store.attach_message_ids(t, g, card_message_id="om_late") == 0
    row = store.get_pending(t, g)
    assert row["status"] == "DECIDED" and row["card_message_id"] is None
    assert store.attach_message_ids(t, g) is None
    store.close()


# ---------- 5. LLM 字段 markdown 注入 ----------

def test_card_md_injection_escaped_on_all_llm_fields():
    evil_name = "x\n[批准](https://evil.example)"
    evil_focus = "**加粗伪系统**\n- 伪系统行"
    evil_art = ["a.mp4", "b.mp4"]
    evil_task = "t|t`code`"
    proposals = [{
        "name": evil_name, "sim_engine": "mujoco",
        "first_principles": evil_focus,
        "expected_artifacts": evil_art,
        "ascent_claims": [
            {"metric": "success_rate", "direction": 1, "task": evil_task,
             "target": 1.0, "epsilon": 0.0},
        ],
    }]
    card = build_gate1_card_ascent("题面\n1. 伪列表", proposals)
    md_text = _card_md(card)
    # 注入原形态不得作为有效 markdown 出现（结构字符已反斜杠化）
    assert "[批准](https://evil.example)" not in md_text
    assert "**加粗伪系统**" not in md_text
    # 表格单元格内的行内竖线必须被转义，列结构不可被 task 截断
    assert "t\\|t" in md_text
    assert "\\- 伪系统行" in md_text and "\n- 伪系统行" not in md_text
    assert "1\\. 伪列表" in md_text
    # 反引号不可截断行内 code
    assert "`code`" not in md_text
    # Diff 卡题面同口径（题面不在表格内：行首/非行内竖线沿用 md_escape）
    diff = build_gate2_card_diff_v2("题面|x\n# 伪标题", _diff_rows(),
                                    "non_dominated")
    dmd = _card_md(diff)
    assert "\\# 伪标题" in dmd
    assert "题面\\|x" not in dmd and "题面|x" in dmd


def test_diff_card_three_verdict_copies_and_arrows():
    rows = _diff_rows()
    blobs = {
        v: json.dumps(build_gate2_card_diff_v2("t", rows, v),
                      ensure_ascii=False)
        for v in ("v2_dominates", "non_dominated", "dominated_by_v1")}
    assert "V2 支配 V1" in blobs["v2_dominates"]
    assert "non_dominated" in blobs["non_dominated"]
    assert "V2 被 V1 支配" in blobs["dominated_by_v1"]
    for b in blobs.values():
        assert "机判仅作参考，绝不代决" in b
        assert "人类最终裁决" in b
    # dict 形态 verdict（state.pareto_verdict）同效
    assert build_gate2_card_diff_v2("t", rows, {"verdict": "v2_dominates"})
    # ε 带内 = ≈ 持平：vram 20→19.5 为 2.5% 改善（5% 带内）
    assert "≈ 持平" in blobs["non_dominated"]
    # 成功率 ε=0：0.8→0.9 严格改善
    assert "↑ 改善" in blobs["non_dominated"]


def test_diff_rows_must_be_exactly_six_pareto_metrics():
    # Δ 面板是 Pareto 机判证据：缺指标/多指标/重复/非有限值一律 fail-closed。
    base = _diff_rows()
    with pytest.raises(ValueError):
        build_gate2_card_diff_v2("t", [], "non_dominated")
    with pytest.raises(ValueError):
        build_gate2_card_diff_v2("t", base[:5], "non_dominated")  # 缺一指标
    dup = base + [{"metric": "success_rate", "v1": 0.8, "v2": 0.9}]
    with pytest.raises(ValueError):
        build_gate2_card_diff_v2("t", dup, "non_dominated")
    bad = [dict(r) for r in base]
    bad[0]["v2"] = float("nan")
    with pytest.raises(ValueError):
        build_gate2_card_diff_v2("t", bad, "non_dominated")
    # 六行齐备但顺序打乱：渲染按规范顺序，不报错
    shuffled = [base[i] for i in (5, 0, 4, 1, 3, 2)]
    md_text = _card_md(build_gate2_card_diff_v2("t", shuffled, "non_dominated"))
    names = [_METRIC_LABELS[m] for m in
             ("success_rate", "slip_dist_mean_m", "postpush_vmax_mean_mps",
              "tilt_max_deg_max", "solve_wallclock_s", "vram_peak_gb")]
    positions = [md_text.index(n) for n in names]
    assert positions == sorted(positions)


# ---------- 6. H-1：无上下文不自动绑卡；双媒体 id 回复可定位 ----------

def _text_event(text, *, message_id="om_txt_h1", parent_id=None,
                root_id=None, thread_id=None):
    return SimpleNamespace(event=SimpleNamespace(
        message=SimpleNamespace(
            chat_id=CHAT, chat_type="p2p", message_type="text",
            content=json.dumps({"text": text}, ensure_ascii=False),
            message_id=message_id, create_time=now_ms(),
            parent_id=parent_id, root_id=root_id, thread_id=thread_id),
        sender=SimpleNamespace(
            sender_id=SimpleNamespace(open_id=OWNER),
            sender_type="user", tenant_key="fixture")))


def test_h1_contextless_text_never_binds_even_with_single_pending(gw):
    gw.store.upsert_pending("th-h1", 2, card_message_id="om_card_h1",
                            open_chat_id=CHAT, task_title="唯一在途审批")
    gw.store.attach_message_ids(
        "th-h1", 2,
        media_message_id_v1="om_mv1_h1", media_message_id_v2="om_mv2_h1")
    # 恰好一张 PENDING，但无任何回复上下文——必须落歧义回执而非自动绑卡。
    gw._handle_message(_text_event("直接批准吧"))
    row = gw.store.get_pending("th-h1", 2)
    assert row["human_notes"] in (None, "")
    reply = _texts(gw._calls)
    assert len(reply) == 1 and "未记录" in reply[0]


def test_h1_reply_to_either_diff_media_binds_uniquely(gw):
    gw.store.upsert_pending("th-h1b", 2, card_message_id="om_card_h1b",
                            open_chat_id=CHAT, task_title="双媒体审批")
    gw.store.attach_message_ids(
        "th-h1b", 2,
        media_message_id_v1="om_mv1_h1b", media_message_id_v2="om_mv2_h1b")
    # 回复 V1 视频消息：新 v1 列参与定位，意见落库
    gw._handle_message(_text_event(
        "V1 基线看着没问题", message_id="om_txt_r1",
        parent_id="om_mv1_h1b"))
    row = gw.store.get_pending("th-h1b", 2)
    assert "V1 基线看着没问题" in (row["human_notes"] or "")
    # 再来一条回复 V2 视频：v2 列同样可定位
    gw._handle_message(_text_event(
        "V2 有抖动", message_id="om_txt_r2", parent_id="om_mv2_h1b"))
    row = gw.store.get_pending("th-h1b", 2)
    assert "V2 有抖动" in (row["human_notes"] or "")


# ---------- 分流：S-9 前导横线（实现 Task0 已在 video.py，补回归测试） ----------

def test_s9_compress_command_dash_filename_prefix(tmp_path):
    cmd = compress_command("-dash-in.mp4", 2, "-dash-out.mp4",
                           "-cover.png")
    # 三个路径在 shlex.quote 之前加 ./ 前缀（无特殊字符时 quote 不再加引号），
    # ffmpeg 不会把它们当成命令行选项
    assert " -i ./-dash-in.mp4 " in cmd
    assert "aac ./-dash-out.mp4 " in cmd
    assert " -an ./-cover.png" in cmd
    assert " -i -dash-in.mp4 " not in cmd
    # 不带横线的普通路径不被加前缀
    normal = compress_command("clip.mp4", 2, "out.mp4", "cover.png")
    assert "./clip.mp4" not in normal and " -i clip.mp4 " in normal
    # step4 裁剪参数仍在（防 ./ 改动顺手破坏命令拼接）
    cmd4 = compress_command("-x.mp4", 4, "-o.mp4", "-c.png",
                            trim_start=1.5, trim_seconds=20)
    assert "-ss 1.5 -t 20" in cmd4 and " -i ./-x.mp4 " in cmd4


# ---------- 迁移：v0→v2、v1→v2 原地 ALTER ----------

_RAW_V0_DDL = """
CREATE TABLE pending_gates(
  thread_id TEXT NOT NULL, gate INTEGER NOT NULL
      CHECK(gate IN (1,2)),
  status TEXT NOT NULL
      CHECK(status IN ('PENDING','DECIDED','EXPIRED')),
  choice INTEGER, decision TEXT, human_notes TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL, card_message_id TEXT, media_message_id TEXT,
  open_chat_id TEXT, operator_open_id TEXT, created_at INTEGER NOT NULL,
  decided_at INTEGER, PRIMARY KEY(thread_id, gate));
"""
_RAW_HISTORY_V0 = """
CREATE TABLE pending_gates_history(
  thread_id TEXT NOT NULL, gate INTEGER NOT NULL,
  status TEXT NOT NULL, choice INTEGER, decision TEXT,
  human_notes TEXT NOT NULL DEFAULT '', source TEXT NOT NULL,
  card_message_id TEXT, media_message_id TEXT, open_chat_id TEXT,
  operator_open_id TEXT, created_at INTEGER NOT NULL, decided_at INTEGER,
  archived_at INTEGER NOT NULL, PRIMARY KEY(thread_id, gate, archived_at));
"""


def _cols(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table});")}


def test_migration_v0_db_upgrades_to_v2(tmp_path):
    db = tmp_path / "state" / "pending_gates.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    raw = sqlite3.connect(str(db))
    raw.execute("PRAGMA journal_mode=WAL;")
    raw.execute(_RAW_V0_DDL)
    raw.execute(_RAW_HISTORY_V0)
    raw.execute(
        "INSERT INTO pending_gates(thread_id, gate, status, human_notes, source,"
        " card_message_id, media_message_id, created_at) VALUES "
        "('v0row', 2, 'PENDING', '', 'cli', 'om_v0card', 'om_v0media', ?)",
        (now_ms(),))
    raw.commit()
    raw.close()

    s = PendingStore(db)
    # Task6：v0 老库一次性增量到 user_version=3（v1 提醒三列 + Task3 双媒体两列
    # + Task6 四媒体四列）；Task3 原断言 ==2 随 bump 同提交对账为 ==3（非放水：
    # 下列两列/四列子集断言全部保留并外扩）。
    assert s._conn.execute("PRAGMA user_version;").fetchone()[0] == 3
    cols = _cols(s._conn, "pending_gates")
    assert {"media_message_id_v1", "media_message_id_v2"} <= cols
    assert {"media_message_id_v1", "media_message_id_v2"} <= _cols(
        s._conn, "pending_gates_history")
    assert {"media_message_id_v1a", "media_message_id_v1b",
            "media_message_id_v2a", "media_message_id_v2b"} <= cols
    assert {"media_message_id_v1a", "media_message_id_v1b",
            "media_message_id_v2a", "media_message_id_v2b"} <= _cols(
        s._conn, "pending_gates_history")
    row = s.get_pending("v0row", 2)
    # 老行新两列 NULL；老单列 media_message_id 原样保留
    assert row["media_message_id_v1"] is None
    assert row["media_message_id_v2"] is None
    assert row["media_message_id"] == "om_v0media"
    assert row["card_message_id"] == "om_v0card"
    # 双列回填在升级后的老库上可用
    s.attach_message_ids("v0row", 2, media_message_id_v1="om_new_v1")
    assert s.get_pending("v0row", 2)["media_message_id_v1"] == "om_new_v1"
    s.close()


def test_migration_v1_db_upgrades_to_v2(tmp_path):
    db = tmp_path / "state" / "pending_gates.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    raw = sqlite3.connect(str(db))
    raw.execute("PRAGMA journal_mode=WAL;")
    # v1 = 原 13 列 + 提醒三列；history 仍 13 列；inbound 表已建
    raw.execute(_RAW_V0_DDL.replace(
        "PRIMARY KEY(thread_id, gate));",
        "reminder_count INTEGER NOT NULL DEFAULT 0,"
        "last_reminder_at INTEGER, task_title TEXT,"
        "PRIMARY KEY(thread_id, gate));"))
    raw.execute(_RAW_HISTORY_V0)
    raw.execute("""
        CREATE TABLE inbound_messages(
          message_id TEXT PRIMARY KEY,
          thread_id TEXT NOT NULL, gate INTEGER NOT NULL,
          open_chat_id TEXT, note_line TEXT NOT NULL,
          received_at_ms INTEGER NOT NULL);
    """)
    raw.execute(
        "INSERT INTO pending_gates(thread_id, gate, status, human_notes, source,"
        " card_message_id, created_at, reminder_count, task_title) VALUES "
        "('v1row', 1, 'PENDING', '', 'cli', 'om_v1card', ?, 1, 'v1 任务')",
        (now_ms(),))
    raw.execute("PRAGMA user_version = 1;")
    raw.commit()
    raw.close()

    s = PendingStore(db)
    # Task6：v1 老库增量到 3（原 ==2 随 schema bump 同提交对账）。
    assert s._conn.execute("PRAGMA user_version;").fetchone()[0] == 3
    row = s.get_pending("v1row", 1)
    assert row["reminder_count"] == 1 and row["task_title"] == "v1 任务"
    assert row["media_message_id_v1"] is None
    assert row["media_message_id_v2"] is None
    assert {"media_message_id_v1", "media_message_id_v2"} <= _cols(
        s._conn, "pending_gates_history")
    # 归档 INSERT...SELECT 携带新两列（NULL 不炸、列单对齐）
    assert s.mark_decided("v1row", 1, decision="approve", source="cli")
    assert s.archive_decided("v1row", 1)
    hist = s._conn.execute(
        "SELECT media_message_id_v1, media_message_id_v2, card_message_id "
        "FROM pending_gates_history WHERE thread_id='v1row' AND gate=1"
    ).fetchone()
    assert hist[0] is None and hist[1] is None
    assert hist[2] == "om_v1card"
    s.close()
    # 再次打开停在 v3，不重复 ALTER
    s2 = PendingStore(db)
    assert s2._conn.execute("PRAGMA user_version;").fetchone()[0] == 3
    s2.close()


def test_fresh_db_at_version_two_with_columns(tmp_path):
    s = PendingStore(tmp_path / "fresh.db")
    # Task6：新库直接 user_version=3，Task3 两列与 Task6 四列同生（原名保留）。
    assert s._conn.execute("PRAGMA user_version;").fetchone()[0] == 3
    assert {"media_message_id_v1", "media_message_id_v2"} <= _cols(
        s._conn, "pending_gates")
    assert {"media_message_id_v1a", "media_message_id_v1b",
            "media_message_id_v2a", "media_message_id_v2b"} <= _cols(
        s._conn, "pending_gates")
    assert {"media_message_id_v1a", "media_message_id_v1b",
            "media_message_id_v2a", "media_message_id_v2b"} <= _cols(
        s._conn, "pending_gates_history")
    s.close()


# ---------- 纯选择器（图缝 Task6 才接，此处只测分支） ----------

def test_run_mode_selectors_and_constant_parity():
    # 与 graph.ascent 的字面量同源（client 不得反向 import graph）
    assert RUN_MODE_STANDARD == STANDARD == "standard"
    assert RUN_MODE_ASCENT == ASCENT == "ascent"
    assert select_gate1_card_view({"run_mode": "ascent"}) == "ascent"
    assert select_gate2_requestor({"run_mode": "ascent"}) == "diff"
    assert select_gate1_card_view({"run_mode": "standard"}) == "standard"
    assert select_gate2_requestor({"run_mode": "standard"}) == "standard"
    # 缺省/None/非映射全部安全回落到标准路径
    assert select_gate1_card_view({}) == "standard"
    assert select_gate2_requestor(None) == "standard"
    assert select_gate2_requestor({"run_mode": None}) == "standard"
    assert select_gate1_card_view({"run_mode": "weird"}) == "standard"
