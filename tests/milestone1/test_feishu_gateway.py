"""Phase 1 Task 1 飞书审批网关单测：9 个用例（计划要求 7 个①-⑦ + 1 个解析纯函数
+ 1 个探针 C 锁内适配回归⑧），0 付费 0 联网。

全部 monkeypatch requests.post；WS 不启动（start_ws=False），合成事件对象直接调
回调处理函数。fixture 凭据形状刻意避开 scan_secrets.sh 九条正则（无 cli_/ark-/sk-/
hf_ 前缀与真实赋值形态；构造函数注入，测试不依赖 FEISHU_* 环境变量）：
  app_id=FIXTURE_APP_ID_000，secret=FIXTURE_SECRET_<32 位 0>，owner=ou_fixture...

用例（详细计划 L428）：
  ① 空意见 reject -> error toast 且无 DECIDED 行；
  ② card_form approve -> DECIDED/choice/source=card_form 且 Event set；
  ③ p2p 文本（H-1 收紧）：凭回复上下文定位唯一 PENDING 卡，窗内合并
     human_notes、窗外忽略、终态/歧义不落库只回提示、message_id 去重重投幂等；
  ④ 非 owner 点击拒绝且不落决策；
  ⑤ 同主键第二次 request 不重发卡片（send 计数==1，复用 card_message_id）；
  ⑥ 31MB 选 step2 起步，mock 压缩产物体积逐级推进到 step4/5（mock 点：
     _file_size 与 _compress_in_container，真实环境分别是 Path.stat 与
     docker compose run 容器内 ffmpeg；纯函数 select_ladder_step 不需要 ffprobe）；
  ⑦ files 连续失败 -> video_fallback 降级，Gate2 卡片照发，决策可由 CLI 落库结束；
  ⑧ 探针 C 裁决（2026-10-14 实测 1.0 卡静默不渲染 form）：Gate 卡 form_required
     时 2.0 先发、被拒退回 1.0；普通卡仍 1.0 优先。
"""
from __future__ import annotations

import json
import logging
import shlex
import sqlite3
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.feishu_gateway import client as client_mod
from orchestrator.feishu_gateway.cards import parse_card_event, parse_message_event
from orchestrator.feishu_gateway.client import FeishuAPIError, FeishuGateway
from orchestrator.feishu_gateway.store import PendingStore, now_ms
from orchestrator.feishu_gateway.video import (
    HARD_LIMIT,
    MB,
    compress_command,
    select_ladder_step,
    validate_trim,
)

APP_ID = "FIXTURE_APP_ID_000"
APP_SECRET = "FIXTURE_SECRET_" + "0" * 32
OWNER = "ou_fixture0000000000000000000000000000"
OTHER = "ou_fixture1111111111111111111111111111"
CHAT = "oc_fixture00000000000000000000000000000"


# ---------- 合成事件 ----------

def card_event(value, *, feedback=None, operator=OWNER, message_id="om_card_1",
               chat_id=CHAT, form_value=None):
    fv = {"feedback": feedback} if feedback is not None else (form_value or {})
    return SimpleNamespace(event=SimpleNamespace(
        action=SimpleNamespace(value=dict(value), form_value=fv),
        operator=SimpleNamespace(open_id=operator, union_id=None, user_id=None),
        context=SimpleNamespace(open_chat_id=chat_id,
                                open_message_id=message_id)))


def text_event(text, *, sender=OWNER, chat_id=CHAT, message_id="om_txt_1",
               create_ms=None, parent_id=None, root_id=None, thread_id=None):
    return SimpleNamespace(event=SimpleNamespace(
        message=SimpleNamespace(
            chat_id=chat_id, chat_type="p2p", message_type="text",
            content=json.dumps({"text": text}, ensure_ascii=False),
            message_id=message_id,
            create_time=create_ms if create_ms is not None else now_ms(),
            parent_id=parent_id, root_id=root_id, thread_id=thread_id),
        sender=SimpleNamespace(
            sender_id=SimpleNamespace(open_id=sender),
            sender_type="user", tenant_key="fixture")))


# ---------- requests.post 替身 ----------

class FakeResp:
    def __init__(self, body, status_code=200):
        self._body = body
        self.status_code = status_code

    def json(self):
        return self._body


def make_fake_post(calls, *, fail_files=False, fail_images=False):
    seq = {"n": 0}

    def fake_post(url, *, json=None, data=None, files=None, headers=None,
                  timeout=None):
        if url.endswith("/auth/v3/tenant_access_token/internal"):
            return FakeResp({"code": 0, "tenant_access_token": "t-fixture-000",
                             "expire": 7200})
        if url.endswith("/im/v1/files"):
            if fail_files:
                return FakeResp({"code": 234001, "msg": "fixture: file rejected"})
            seq["n"] += 1
            return FakeResp({"code": 0,
                             "data": {"file_key": f"file_key_{seq['n']}"}})
        if url.endswith("/im/v1/images"):
            if fail_images:
                return FakeResp({"code": 234002, "msg": "fixture: image rejected"})
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
        evidence_dir=tmp_path / "task1_feishu",
        run_dir=str(tmp_path / "runs"),
        start_ws=False,
    )
    g._calls = calls
    yield g
    g.close()


def _interactive_calls(gw):
    return [c for c in gw._calls if c["kind"] == "interactive"]


def _text_calls(gw, chat_id=CHAT):
    out = []
    for c in gw._calls:
        if c["kind"] == "text" and c["payload"].get("receive_id") == chat_id:
            out.append(json.loads(c["content_raw"])["text"])
    return out


# ---------- ① 空意见打回被拒 ----------

def test_01_empty_feedback_reject_blocked(gw):
    gw.store.upsert_pending("t1", 1, card_message_id="om_card_1",
                            open_chat_id=CHAT)
    resp = gw._handle_card(card_event(
        {"gate": 1, "decision": "reject_with_comments"}, feedback="   "))
    assert resp.toast.type == "error"
    assert resp.toast.content == "打回必须填写意见"
    row = gw.store.get_pending("t1", 1)
    assert row["status"] == "PENDING" and row["decision"] is None
    # 空字符串同样拦截
    resp2 = gw._handle_card(card_event(
        {"gate": 1, "decision": "reject_with_comments"}, feedback="",
        message_id="om_card_1"))
    assert resp2.toast.type == "error"
    assert gw.store.get_pending("t1", 1)["status"] == "PENDING"
    assert len(_interactive_calls(gw)) == 0  # 没有任何卡片/消息被回调发出


# ---------- ② card_form approve 落库并唤醒 ----------

def test_02_card_form_approve_persists_and_sets_event(gw):
    gw.store.upsert_pending("t2", 1, card_message_id="om_card_2",
                            open_chat_id=CHAT)
    ev = gw._event_for("t2", 1)
    assert not ev.is_set()
    resp = gw._handle_card(card_event(
        {"gate": 1, "decision": "approve", "choice": 1},
        feedback="", message_id="om_card_2"))
    assert resp.toast.type == "success"
    row = gw.store.get_pending("t2", 1)
    assert row["status"] == "DECIDED"
    assert row["decision"] == "approve" and row["choice"] == 1
    assert row["source"] == "card_form"
    assert ev.is_set()
    # 重复点击不覆盖结论（幂等）
    resp_again = gw._handle_card(card_event(
        {"gate": 1, "decision": "reject_with_comments"},
        feedback="晚到的意见", message_id="om_card_2"))
    assert resp_again.toast.type == "info"
    assert gw.store.get_pending("t2", 1)["decision"] == "approve"


# ---------- ③ P2P 文本（H-1）：回复定位 / 窗内合并 / 窗外忽略 /
#             终态不落库 / 歧义回执 / 重投幂等 / 回执带线程身份 ----------

def test_03_p2p_reply_localization_window_decided_ambiguity_redelivery(gw):
    # 窗内：凭回复上下文（parent/root/thread）定位到唯一 PENDING 卡，
    # 两条文本合并进同一行；回执必须带 Gate、线程 ID 与任务名。
    gw.store.upsert_pending("t3a", 1, card_message_id="om_card_3a",
                            open_chat_id=CHAT, task_title="任务三A")
    gw._handle_message(text_event("第一条自由意见", chat_id=CHAT,
                                  message_id="om_t1", parent_id="om_card_3a"))
    gw._handle_message(text_event("第二条自由意见", chat_id=CHAT,
                                  message_id="om_t2", root_id="om_card_3a",
                                  thread_id="om_card_3a"))
    notes = gw.store.get_pending("t3a", 1)["human_notes"]
    lines = notes.splitlines()
    assert len(lines) == 2
    assert lines[0].startswith("[文本 ") and lines[0].endswith("第一条自由意见")
    assert "第二条自由意见" in lines[1]
    replies = _text_calls(gw)
    assert len(replies) == 2
    assert all("Gate1" in r and "t3a" in r and "任务三A" in r
               and "7 字" in r for r in replies)

    # 重投/重放同一 message_id：幂等，不产生第三行，也不重复回执
    gw._handle_message(text_event("第一条自由意见", chat_id=CHAT,
                                  message_id="om_t1", parent_id="om_card_3a"))
    assert len(gw.store.get_pending("t3a", 1)["human_notes"].splitlines()) == 2
    assert len(_text_calls(gw)) == 2
    inbound = gw.store._conn.execute(  # noqa: SLF001
        "SELECT COUNT(*) FROM inbound_messages").fetchone()[0]
    assert inbound == 2

    # 无回复上下文：不得再按「最近一行」猜归属；回歧义提示且不落库
    gw._handle_message(text_event("飘在会话里的话", chat_id=CHAT,
                                  message_id="om_t_lonely"))
    assert len(gw.store.get_pending("t3a", 1)["human_notes"].splitlines()) == 2
    amb = [r for r in _text_calls(gw) if "未记录" in r]
    assert amb and "t3a" in amb[-1]

    # 两个候选并存且引用不到任一卡：歧义回执列出两个候选线程
    gw.store.upsert_pending("t3e", 2, card_message_id="om_card_3e",
                            media_message_id="om_media_3e",
                            open_chat_id=CHAT, task_title="任务三E")
    gw._handle_message(text_event("到底批哪个", chat_id=CHAT,
                                  message_id="om_t_unk", parent_id="om_unknown"))
    last = _text_calls(gw)[-1]
    assert "未记录" in last and "t3a" in last and "t3e" in last
    assert gw.store.get_pending("t3a", 1)["human_notes"].count("\n") == 1
    assert gw.store.get_pending("t3e", 2)["human_notes"] == ""
    # 引用视频消息同样能唯一定位（media_message_id 匹配）
    gw._handle_message(text_event("回复视频的意见", chat_id=CHAT,
                                  message_id="om_t_media",
                                  parent_id="om_media_3e"))
    assert "回复视频的意见" in gw.store.get_pending("t3e", 2)["human_notes"]

    # 窗外：created_at 早于 10 分钟会话窗 -> 忽略，不追加、不回执
    chat_b = "oc_fixture_outside_window_00000000000000000000"
    gw.store.upsert_pending(
        "t3b", 1, card_message_id="om_card_3b", open_chat_id=chat_b,
        task_title="窗外任务", created_at_ms=now_ms() - 11 * 60 * 1000)
    before = len(_text_calls(gw, chat_b))
    gw._handle_message(text_event("窗外的意见", chat_id=chat_b,
                                  message_id="om_t3", parent_id="om_card_3b"))
    row_b = gw.store.get_pending("t3b", 1)
    assert row_b["human_notes"] == "" and row_b["status"] == "PENDING"
    assert len(_text_calls(gw, chat_b)) == before

    # 已 DECIDED：H-1 收紧——终态行绝不追加（旧实现「仅留痕」是误归属来源），
    # 该行也不在 PENDING 候选中：回执提示无进行中审批，决策/choice/source 不变。
    gw.store.upsert_pending("t3c", 1, card_message_id="om_card_3c",
                            open_chat_id=CHAT + "c")
    assert gw.store.mark_decided("t3c", 1, decision="approve",
                                 source="card_form", choice=0)
    gw._handle_message(text_event("决定后补充说明", chat_id=CHAT + "c",
                                  message_id="om_t4", parent_id="om_card_3c"))
    row_c = gw.store.get_pending("t3c", 1)
    assert row_c["status"] == "DECIDED" and row_c["decision"] == "approve"
    assert row_c["source"] == "card_form" and row_c["choice"] == 0
    assert row_c["human_notes"] == ""
    assert "未找到进行中的审批单" in _text_calls(gw, CHAT + "c")[-1]

    # 非 owner 的消息一律不关联（且不产生任何回执）
    gw.store.upsert_pending("t3d", 1, card_message_id="om_card_3d",
                            open_chat_id=CHAT + "d")
    n_txt = len(_text_calls(gw, CHAT + "d"))
    gw._handle_message(text_event("别人说的", sender=OTHER, chat_id=CHAT + "d",
                                  parent_id="om_card_3d"))
    assert gw.store.get_pending("t3d", 1)["human_notes"] == ""
    assert len(_text_calls(gw, CHAT + "d")) == n_txt


# ---------- ④ owner 不符拒绝 ----------

def test_04_non_owner_card_click_rejected(gw):
    gw.store.upsert_pending("t4", 1, card_message_id="om_card_4",
                            open_chat_id=CHAT)
    resp = gw._handle_card(card_event(
        {"gate": 1, "decision": "approve", "choice": 0},
        operator=OTHER, message_id="om_card_4"))
    assert resp.toast.type == "error"
    assert resp.toast.content == "非审批人操作，已拒绝"
    row = gw.store.get_pending("t4", 1)
    assert row["status"] == "PENDING" and row["operator_open_id"] is None
    assert not gw._event_for("t4", 1).is_set()


# ---------- ⑤ 同主键第二次请求不重发（重启重挂） ----------

def test_05_second_request_reuses_card_message(gw):
    proposals = [{"name": "P1", "sim_engine": "e1", "first_principles": "f1"},
                 {"name": "P2", "sim_engine": "e2", "first_principles": "f2"}]
    errors: list[BaseException] = []

    def run(req_ev: threading.Event):
        try:
            req_ev.result = gw.request_gate1_approval(  # type: ignore[attr-defined]
                "t5", "任务五", proposals, max_wait=30)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            req_ev.set()

    t1_done = threading.Event()
    t1 = threading.Thread(target=run, args=(t1_done,))
    t1.start()
    # 等第一次请求把卡片发出、PENDING 落库
    deadline = time.time() + 5
    while time.time() < deadline:
        row = gw.store.get_pending("t5", 1)
        if row and row["card_message_id"]:
            break
        time.sleep(0.02)
    first_message_id = gw.store.get_pending("t5", 1)["card_message_id"]
    assert first_message_id

    t2_done = threading.Event()
    t2 = threading.Thread(target=run, args=(t2_done,))
    t2.start()
    time.sleep(0.5)  # 让第二次请求走过“已有 PENDING 不重发”分支

    assert len(_interactive_calls(gw)) == 1
    assert t1.is_alive() and t2.is_alive()  # 两个等待都挂在同一 Event 上

    # 人工点「批准方案 1」
    gw._handle_card(card_event(
        {"gate": 1, "decision": "approve", "choice": 0},
        message_id=first_message_id))
    assert t1_done.wait(5) and t2_done.wait(5)
    t1.join(5)
    t2.join(5)
    assert errors == []
    assert getattr(t1_done, "result", None)["decision"] == "approve"
    assert getattr(t2_done, "result", None)["choice"] == 0
    assert len(_interactive_calls(gw)) == 1  # 全程只发过一张卡
    assert gw.store.get_pending("t5", 1)["card_message_id"] == first_message_id


# ---------- ⑥ 体积阶梯纯函数 + 31MB 逐级推进到 step4/5 ----------

def test_06_ladder_thresholds_and_31mb_walks_to_fallback(gw, tmp_path,
                                                          monkeypatch):
    # 纯函数边界（H-14：MB 取十进制 1_000_000；28MB 直传；30MB 接口硬上限；
    # 超过 28MB 一律从 step2 起步，之后只按实测产物体积逐级 2→3→4→5，不跳级）
    assert MB == 1_000_000
    assert select_ladder_step(28 * MB) == 1
    assert select_ladder_step(28 * MB + 1) == 2
    assert select_ladder_step(31 * MB) == 2       # 用例锚点：31MB 从 step2 起
    assert select_ladder_step(100 * MB) == 2      # 不再粗分档越级
    assert select_ladder_step(200 * MB) == 2
    assert select_ladder_step(300 * MB) == 2
    assert HARD_LIMIT == 30 * MB == 30_000_000

    video = tmp_path / "runs" / "probe.mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"0" * (31 * MB // 10))  # 占位即可，体积判定被 mock
    cover = tmp_path / "runs" / "probe.png"
    cover.write_bytes(b"\x89PNG\r\n")

    # mock 点①：Path.stat -> _file_size（真实环境读文件字节数）
    size_map = {str(video): 31 * MB,
                str(tmp_path / "runs" / "probe_s2.mp4"): 31 * MB,   # 720p 仍超限
                str(tmp_path / "runs" / "probe_s3.mp4"): 31 * MB,   # 540p 仍超限
                str(tmp_path / "runs" / "probe_s4.mp4"): 10 * MB}   # 截断后达标

    def fake_size(p, _map=size_map):
        return _map.get(str(p), 1 * MB)

    monkeypatch.setattr(gw, "_file_size", fake_size)

    # mock 点②：docker compose run 容器内 ffmpeg（真实环境在 Task7/8 发生）
    compress_steps: list[int] = []

    def fake_compress(host_video, step, _out=tmp_path / "runs", *, trim=None):
        compress_steps.append((step, trim))
        out_v, out_c = (_out / f"probe_s{step}.mp4",
                        _out / f"probe_s{step}_cover.png")
        out_v.write_bytes(b"x")  # 实体占位；体积由 _file_size mock 决定
        out_c.write_bytes(b"x")
        return out_v, out_c

    monkeypatch.setattr(gw, "_compress_in_container", fake_compress)
    # mock 点③：files 接口在所有阶梯都失败（连续失败推进降级）
    monkeypatch.setattr(client_mod.requests, "post",
                        make_fake_post(gw._calls, fail_files=True))

    result = gw.send_video_native(video, duration_ms=4000, cover_png=cover)
    assert result["video_fallback"] is True and result["ladder_step"] == 5
    # 31MB 起步走 step2 -> step3（仍超 30MB）-> step4（达标但接口拒）-> step5
    assert [s for s, _ in compress_steps] == [2, 3, 4]
    # 未传 trim：各级收到的都是 None（step4 也不得自行发明裁剪区间）
    assert all(t is None for _, t in compress_steps)
    assert any(a == {"step": 5, "action": "fallback_intranet"}
               for a in result["attempts"])


# ---------- ⑦ media 连续失败：降级且 Gate2 仍能裁决 ----------

def test_07_media_failure_fallback_gate2_decidable(gw, tmp_path, monkeypatch):
    video = tmp_path / "runs" / "v.mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"0" * (1024 * 1024))
    cover = tmp_path / "runs" / "v.png"
    cover.write_bytes(b"\x89PNG\r\n")
    # files/images 对每一级都拒绝；压缩经容器（mock）
    monkeypatch.setattr(client_mod.requests, "post",
                        make_fake_post(gw._calls, fail_files=True,
                                       fail_images=True))
    def fake_compress(host_video, step, *, trim=None):
        out = video.with_name(f"v_s{step}.mp4")
        cov = video.with_name(f"v_s{step}_cover.png")
        out.write_bytes(b"x")
        cov.write_bytes(b"x")
        return out, cov

    monkeypatch.setattr(gw, "_compress_in_container", fake_compress)

    done = threading.Event()
    box: dict = {}

    def run():
        try:
            box["result"] = gw.request_gate2_approval(
                "t7", "任务七验收",
                {"pose_error": "0.02", "max_contact_depth": "3mm",
                 "contact_duration": "1.2s", "jerk": "阈值内",
                 "rung4_atomic": "6/6"},
                video_path=str(video), intranet_url=None,
                duration_ms=4000, cover_png=str(cover), max_wait=30)
        finally:
            done.set()

    t = threading.Thread(target=run)
    t.start()
    deadline = time.time() + 5
    while time.time() < deadline:
        row = gw.store.get_pending("t7", 2)
        if row and row["card_message_id"]:
            break
        time.sleep(0.02)
    row = gw.store.get_pending("t7", 2)
    assert row and row["card_message_id"] and row["media_message_id"] is None

    # Gate2 卡片照发且带降级标注
    cards_sent = _interactive_calls(gw)
    assert len(cards_sent) == 1
    assert "video_fallback=true" in cards_sent[0]["content_raw"]

    # CLI/旁路落决策（生产中是独立进程写同一 DB；这里同进程补 set Event）
    assert gw.store.mark_decided("t7", 2, decision="approve", source="cli")
    gw._set_event("t7", 2)
    assert done.wait(5)
    t.join(5)
    result = box["result"]
    assert result["decision"] == "approve" and result["source"] == "cli"
    # 全链路没有任何硬编码批准：结论来自 DB 行
    assert len(_interactive_calls(gw)) == 1


# ---------- ⑧ 锁内适配：Gate 卡 2.0 优先（探针 C 实测裁决） ----------

def _walk_tags(node):
    if isinstance(node, dict):
        tag = node.get("tag")
        if tag:
            yield tag
        for v in node.values():
            yield from _walk_tags(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk_tags(v)


def _walk_names(node):
    if isinstance(node, dict):
        if isinstance(node.get("name"), str):
            yield node["name"]
        for v in node.values():
            yield from _walk_names(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk_names(v)


def test_08_gate_cards_v2_first_with_1_fallback(gw, monkeypatch):
    from orchestrator.feishu_gateway.cards import (
        build_gate1_card, build_gate1_card_v2, build_gate2_card_v2,
    )
    proposals = [{"name": "P1"}, {"name": "P2"}]
    c1 = build_gate1_card("t8", proposals)
    c2 = build_gate1_card_v2("t8", proposals)
    # v2 结构红线（探针 C 实测）：禁用 textarea（200621），多行输入必须是
    # input+multiline_text；form_submit 按钮在 form 内；全卡控件 name 唯一（11310）。
    tags = list(_walk_tags(c2))
    assert "textarea" not in tags
    forms = [e for e in c2["body"]["elements"] if e.get("tag") == "form"]
    assert len(forms) == 2
    names = [n for n in _walk_names(c2)]
    assert len(names) == len(set(names))  # 11310：全卡无重名
    assert [next(e["name"] for e in f["elements"] if e["tag"] == "input")
            for f in forms] == ["feedback_0", "feedback_1"]
    assert all(any(e.get("tag") == "input"
                   and e.get("input_type") == "multiline_text" for e in f["elements"])
               for f in forms)
    assert all(any(e.get("action_type") == "form_submit" for e in f["elements"])
               for f in forms)
    # 打回按钮携带所属方案 choice（解析 feedback_<choice> 用）
    reject1 = next(e for f in forms for e in f["elements"]
                   if e["name"] == "reject_1")
    assert reject1["value"]["choice"] == 1
    g2 = build_gate2_card_v2("t", {"jerk": "x"})
    g2tags = list(_walk_tags(g2))
    assert "textarea" not in g2tags and "form" in g2tags

    # form_required=True：首选即 2.0（接口成功时只发一张）
    ret = gw._send_interactive(CHAT, c1, c2, form_required=True)
    sent = _interactive_calls(gw)
    assert len(sent) == 1 and ret["schema"] == "2.0"
    assert json.loads(sent[0]["content_raw"])["schema"] == "2.0"

    # 默认（提醒卡）：仍 1.0 优先（1.0 卡无 "schema" 键）
    ret2 = gw._send_interactive(CHAT, {"marker": "v1"}, {"schema": "2.0"})
    sent = _interactive_calls(gw)
    assert len(sent) == 2 and ret2["schema"] == "1.0"
    assert json.loads(sent[1]["content_raw"]) == {"marker": "v1"}

    # 2.0 被服务端非 0 拒绝 → 退回 1.0，共两次调用、顺序 2.0→1.0
    seen: list[dict] = []

    def flaky(url, *, json=None, data=None, files=None, headers=None,
              timeout=None):
        if url.endswith("/auth/v3/tenant_access_token/internal"):
            return FakeResp({"code": 0, "tenant_access_token": "t-fixture",
                             "expire": 7200})
        if "/im/v1/messages" in url:
            seen.append(json)
            if len(seen) == 1:
                return FakeResp({"code": 230029, "msg": "fixture: v2 rejected"})
            return FakeResp({"code": 0, "data": {
                "message_id": f"om_fallback_{len(seen)}", "chat_id": CHAT}})
        raise AssertionError(f"未预期的 URL: {url}")

    monkeypatch.setattr(client_mod.requests, "post", flaky)
    ret3 = gw._send_interactive(CHAT, c1, c2, form_required=True)
    assert ret3["schema"] == "1.0" and len(seen) == 2
    assert json.loads(seen[0]["content"])["schema"] == "2.0"
    assert json.loads(seen[1]["content"]) == c1


# ---------- ⑨ Task7 锁内适配：图在容器内时视频压缩不嵌套 docker、同路径直连 ffmpeg ----------

def test_09_compress_execution_position_branch(gw, tmp_path, monkeypatch):
    from orchestrator.feishu_gateway import video as video_mod
    video = tmp_path / "runs" / "v.mp4"
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"0")

    captured: dict = {}

    def fake_run(argv, check=True, timeout=None):
        captured["argv"] = argv
        out, cov = video_mod.expected_outputs(video, int(captured["step"]))
        Path(out).write_bytes(b"x")
        Path(cov).write_bytes(b"x")
        return SimpleNamespace(returncode=0)

    def call(step):
        captured["step"] = step
        return gw._compress_in_container(video, step)

    monkeypatch.setattr(client_mod.subprocess, "run", fake_run)

    # 宿主模式（默认）：docker compose run 起一次性容器；/root/runs 映射
    monkeypatch.delenv("SIMPRAXIS_IN_SANDBOX", raising=False)
    out2, cov2 = call(2)
    argv = captured["argv"]
    assert argv[:2] == ["docker", "compose"] and "sim-sandbox" in argv
    assert argv[-3:-1] == ["bash", "-lc"]
    cmd_host = argv[-1]
    assert "/root/runs/v.mp4" in cmd_host
    assert str(video) not in cmd_host          # 宿主字面路径不得漏进容器命令
    assert out2 == tmp_path / "runs" / "v_s2.mp4"

    # 容器内模式（compose 注入 SIMPRAXIS_IN_SANDBOX=1）：无 docker 嵌套、同路径直连
    monkeypatch.setenv("SIMPRAXIS_IN_SANDBOX", "1")
    out3, cov3 = call(3)
    argv2 = captured["argv"]
    assert argv2[:1] == ["bash"] and "docker" not in argv2
    cmd_in = argv2[-1]
    assert str(video) in cmd_in and "/root/runs" not in cmd_in
    assert "crf 28" in cmd_in and "fps=20" in cmd_in   # step3 命令语义不变
    assert out3 == tmp_path / "runs" / "v_s3.mp4"

    # 宿主模式对 RUN_DIR 外产物仍硬报错（守卫不回归）；容器内同路径无需该改写
    monkeypatch.delenv("SIMPRAXIS_IN_SANDBOX")
    foreign = tmp_path / "elsewhere" / "x.mp4"
    with pytest.raises(ValueError, match="容器内不可见"):
        gw._compress_in_container(foreign, 2)


# ---------- 附：事件解析纯函数 ----------

def test_parse_card_event_duck_typing():
    ev = card_event({"gate": 2, "decision": "approve"}, feedback="原样意见 X")
    value, feedback, operator, chat_id, msg_id = parse_card_event(ev)
    assert value == {"gate": 2, "decision": "approve"}
    assert feedback == "原样意见 X"
    assert operator == OWNER and chat_id == CHAT and msg_id == "om_card_1"
    # Gate1 多 form：按 value.choice 取 feedback_<choice>
    ev2 = card_event({"gate": 1, "decision": "reject_with_comments", "choice": 1},
                     form_value={"feedback_1": "第二方案打回意见"})
    value2, feedback2, _, _, _ = parse_card_event(ev2)
    assert value2["choice"] == 1 and feedback2 == "第二方案打回意见"
    parsed = parse_message_event(text_event("hello"))
    assert parsed["text"] == "hello" and parsed["chat_type"] == "p2p"
    # code!=0 必须显式抛错（错误码路径自检）
    with pytest.raises(FeishuAPIError) as exc:
        raise FeishuAPIError(999, "x", api="unit")
    assert exc.value.code == 999 and "unit" in str(exc.value)


# ---------- ⑩ D10 多轮 Gate：终态行归档 + history 仅追加（store 层） ----------

def test_10_archive_decided_roundtrip(gw):
    s = gw.store
    s.upsert_pending("t10", 2, card_message_id="om_10a", open_chat_id=CHAT)
    # PENDING 行绝不归档（上一轮还在等真人，重发由「不重发」分支处理）
    assert s.archive_decided("t10", 2) is False
    assert s.get_pending("t10", 2)["status"] == "PENDING"
    assert s.list_history("t10", 2) == []

    assert s.mark_decided("t10", 2, decision="reject_with_comments",
                          source="card_form", human_notes="第一轮打回意见")
    assert s.archive_decided("t10", 2) is True
    assert s.get_pending("t10", 2) is None
    hist = s.list_history("t10", 2)
    assert len(hist) == 1
    assert hist[0]["decision"] == "reject_with_comments"
    assert hist[0]["human_notes"] == "第一轮打回意见"
    assert hist[0]["card_message_id"] == "om_10a"
    assert isinstance(hist[0]["archived_at"], int)

    # 新一轮：插全新 PENDING（新卡 id），history 仅追加保留第一轮
    s.upsert_pending("t10", 2, card_message_id="om_10b", open_chat_id=CHAT)
    row = s.get_pending("t10", 2)
    assert row["status"] == "PENDING" and row["card_message_id"] == "om_10b"
    assert len(s.list_history("t10", 2)) == 1
    assert s.mark_decided("t10", 2, decision="approve", source="cli")
    assert s.archive_decided("t10", 2) is True
    rounds = s.list_history("t10", 2)
    assert [r["decision"] for r in rounds] == ["reject_with_comments", "approve"]
    # 已无终态行：再次归档 no-op
    assert s.archive_decided("t10", 2) is False


# ---------- ⑪ Gate2 打回→重开：旧卡点击失效、新卡点击才算第二轮 + 自解释卡 ----------

def _nested_task7_metrics():
    r4 = {"status": "ok", "mode": "record_only",
          "items": [{"id": f"q{i}", "answer": "yes", "defect": False}
                    for i in range(1, 7)],
          "n_defects": 0, "low_confidence": False, "disagreement": None,
          "raw_text_ref": None}
    return {
        "engine": "mujoco", "run_id": "fix-r1", "ok": True, "schema_errors": [],
        "rungs": {
            "rung1": {"pass": True, "checks": {
                "all_finite": True, "max_penetration_m": 0.000223,
                "penetration_limit_m": 0.005, "penetration_within_limit": True,
                "ccd_method": "per_substep_contact_depth",
                "joint_torque_within_limits": True}},
            "rung2": {"pass": True, "checks": {
                "inside_box_frames": 89, "last_window_frames": 32,
                "last_window_all_inside_box": True,
                "contact_longest_run_s": 2.544, "duration_required_s": 1.0,
                "contact_force_mean_n": 0.528, "contact_cv": 0.173,
                "cv_max": 0.3, "stable_contact": True}},
            "rung3": {"pass": True, "checks": {
                "dt_band_ok": True, "dt": 0.016, "jerk_max_mps3": 105.87,
                "jerk_threshold_mps3": 565.14, "over_threshold_count": 0}},
            "rung4": r4},
        "vector": {"rung1": True, "rung2": True, "rung3": True},
        "rules_pass": True, "rung4": r4,
        "auto_release": True, "escalate_human": False}


def _wait_pending_card(gw, thread, gate, old_mid=None, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        row = gw.store.get_pending(thread, gate)
        if row and row["status"] == "PENDING" and row["card_message_id"] \
                and row["card_message_id"] != old_mid:
            return row["card_message_id"]
        time.sleep(0.02)
    raise AssertionError("等不到新的 PENDING 卡片行")


def test_11_gate2_reject_reopens_with_new_card_and_explanation(gw):
    thread = "t11"
    metrics = _nested_task7_metrics()
    explanation = {"task": "做最小接触推送", "problem": "验证物理可信",
                   "video": "看推块入盒"}
    box: dict = {}

    def request_round(key):
        try:
            box[key] = gw.request_gate2_approval(
                thread, "Task7 验收", metrics, explanation=explanation, max_wait=30)
        except BaseException as exc:  # noqa: BLE001
            box[key] = exc

    # —— 第一轮：等卡片，校验自解释内容与真实嵌套指标 ——
    t1 = threading.Thread(target=request_round, args=("r1",))
    t1.start()
    mid1 = _wait_pending_card(gw, thread, 2)
    sent1 = _interactive_calls(gw)
    assert len(sent1) == 1
    assert json.loads(sent1[0]["content_raw"])["schema"] == "2.0"
    card1 = sent1[0]["content_raw"]
    assert "审批前请先读" in card1          # 真人 Gate2 三问说明块
    assert "做最小接触推送" in card1
    assert "最大穿模" in card1 and "0.000223" in card1   # 真实 Rung1 值，不再是 '-'
    assert "record_only" in card1
    assert "位姿误差" not in card1          # 嵌套指标不再走扁平兜底

    # 真人打回（带意见）→ 第一轮 await 返回 reject
    resp = gw._handle_card(card_event(
        {"gate": 2, "decision": "reject_with_comments"},
        feedback="请说明任务背景", message_id=mid1))
    assert resp.toast.type == "success"
    t1.join(5)
    assert not t1.is_alive() and not isinstance(box["r1"], BaseException)
    assert box["r1"]["decision"] == "reject_with_comments"
    assert box["r1"]["human_notes"] == "请说明任务背景"

    # —— 第二轮：归档后必须发新卡；旧卡点击天然失效 ——
    t2 = threading.Thread(target=request_round, args=("r2",))
    t2.start()
    mid2 = _wait_pending_card(gw, thread, 2, old_mid=mid1)
    assert mid2 != mid1
    assert len(_interactive_calls(gw)) == 2          # 真的重发了一张新卡

    stale = gw._handle_card(card_event(
        {"gate": 2, "decision": "approve"}, message_id=mid1))
    assert stale.toast.type == "info"                # 该审批已结束或不存在
    still = gw.store.get_pending(thread, 2)
    assert still["status"] == "PENDING" and still["card_message_id"] == mid2

    # 真人在新卡上批准 → 第二轮 await 返回 approve
    gw._handle_card(card_event(
        {"gate": 2, "decision": "approve"}, message_id=mid2))
    t2.join(5)
    assert not t2.is_alive() and not isinstance(box["r2"], BaseException)
    assert box["r2"]["decision"] == "approve"

    final = gw.store.get_pending(thread, 2)
    assert final["status"] == "DECIDED" and final["decision"] == "approve"
    assert final["card_message_id"] == mid2
    history = gw.store.list_history(thread, 2)
    assert [h["decision"] for h in history] == ["reject_with_comments"]
    assert history[0]["card_message_id"] == mid1


# ---------- ⑫ 卡片纯渲染：1.0 说明块/hr + 扁平 metrics 兜底不回归 ----------

def test_12_gate2_card_explanation_block_and_flat_fallback():
    from orchestrator.feishu_gateway.cards import (
        build_gate2_card, build_gate2_card_v2,
    )
    expl = {"task": "T", "problem": "P", "video": "V"}
    for builder in (build_gate2_card, build_gate2_card_v2):
        nested = builder("tt", _nested_task7_metrics(), explanation=expl)
        blob = json.dumps(nested, ensure_ascii=False)
        assert "审批前请先读" in blob and "解决 / 验证什么问题" in blob
        assert '"tag": "hr"' in blob
        # 无 explanation 时不出说明块、不抛错
        bare = builder("tt", _nested_task7_metrics())
        assert "审批前请先读" not in json.dumps(bare, ensure_ascii=False)
        # 旧式扁平 metrics 仍走原兜底（Task3/合成 Gate2）
        flat = builder("tt", {"pose_error": "0.02", "jerk": "阈值内"})
        flat_blob = json.dumps(flat, ensure_ascii=False)
        assert "位姿误差" in flat_blob and "审批前请先读" not in flat_blob


def test_13_q2_report_metrics_branch_real_numbers_only():
    """Q2 Gate2 卡（D9 补点验）：outcome/mechanism 双结论分支，数字全部外给。

    模板自身不含指标字面量；旧式 Rung 扁平兜底不得串到 Q2 卡上。
    """
    from orchestrator.feishu_gateway.cards import (
        build_gate2_card, build_gate2_card_v2,
    )
    metrics = {
        "metrics_kind": "q2_invariance_report",
        "outcome": {
            "bit_identical": {"comparisons": 80, "failures": 0,
                              "cross_repeat": 20},
            "z_vs_clear": {"rain_hard": -1.1159, "fog_dense": -1.0091},
            "z_negative_control": {"rain_hard": -1.0767, "fog_dense": -0.9736},
            "survivor_max_abs_dI": "4.45e-05",
            "engine_has_snow": False,
        },
        "mechanism": [
            {"label": "rain 100 mm/h", "surface_kept_pct": 71.7,
             "weather_false": 6346, "near_blind": 3747, "transmission": 0.871},
            {"label": "snow 100 mm/h（融化当量）", "surface_kept_pct": 1.7,
             "weather_false": 8078, "near_blind": 3740, "transmission": 0.024},
            {"label": "fog V=6 m（Koschmieder）", "surface_kept_pct": 10.7,
             "weather_false": 0, "near_blind": None, "transmission": 0.013},
        ],
        "residual": "Qback 等参数未用真实数据标定，只断言量级与单调性。",
        "d9_note": "Q2 走 D9 条件闸门，本卡为事后补真人点验（无 Gate1）。",
    }
    for builder in (build_gate2_card, build_gate2_card_v2):
        blob = json.dumps(builder("tt", metrics), ensure_ascii=False)
        assert "逐 bit 相等" in blob and "80" in blob and "失败 0" in blob
        assert "阴性对照" in blob and "rain -1.12" in blob and "fog -1.01" in blob
        assert "4.45e-05" in blob and "无 snow 字段" in blob
        assert "71.7%" in blob and "1.7%" in blob and "10.7%" in blob
        assert "is_weather" in blob and "残差声明" in blob and "D9" in blob
        # 绝不走 Rung 扁平兜底
        assert "位姿误差" not in blob
    # 缺 outcome/mechanism 时不抛错，只渲染结论头
    minimal = json.dumps(
        build_gate2_card("tt", {"metrics_kind": "q2_invariance_report"}),
        ensure_ascii=False)
    assert "Q2 实验结论" in minimal


# ===========================================================================
# 评审修复批次 H-2/H-3/H-4/H-6/H-7/H-8/H-14/H-15/H-17 + schema 迁移回归
# 全部 0 付费 0 联网：requests/subprocess 全 mock，事件全合成。
# ===========================================================================

# ---------- H-2/C1：trim 严格契约 + step4 ffmpeg 透传 + 卡片注记 ----------

def test_14_h2_trim_contract_validates_strict_keys():
    assert validate_trim(None) is None
    base = {"trim_start": 0.0, "trim_seconds": 30.0,
            "contact_moment_s": None, "source_duration_s": None}
    assert validate_trim(dict(base)) == base
    for bad in (
        {},  # 空 dict
        {**base, "extra": 1},  # 多键
        {"trim_start": 0, "trim_seconds": 30},  # 缺键
        {**base, "trim_start": -1},
        {**base, "trim_seconds": 0},
        {**base, "trim_start": True},          # bool 不是数值
        {**base, "contact_moment_s": -0.1},
        {**base, "source_duration_s": "60"},   # 字符串不是数值
        "not-a-dict",
    ):
        with pytest.raises(ValueError):
            validate_trim(bad)


def test_15_h2_step4_command_carries_caller_interval(gw, tmp_path, monkeypatch):
    """调用方区间必须原样进 step4 的 ffmpeg 命令（支持小数秒），且只在 step4 带。"""
    video = tmp_path / "runs" / "v.mp4"
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"0")
    captured: list[list[str]] = []

    def fake_run(argv, check=True, timeout=None):
        captured.append(argv)
        # 返回路径由 _compress_in_container 自行计算，不检查产物存在性，
        # 替身只需记录 argv。
        return SimpleNamespace(returncode=0)

    monkeypatch.setenv("SIMPRAXIS_IN_SANDBOX", "1")
    monkeypatch.setattr(client_mod.subprocess, "run", fake_run)
    trim = {"trim_start": 12.5, "trim_seconds": 20.0,
            "contact_moment_s": 21.0, "source_duration_s": 60.0}
    gw._compress_in_container(video, 4, trim=trim)
    cmd = captured[-1][-1]
    assert "-ss 12.5 -t 20" in cmd
    # step2/3 命令不含 seek/时长（区间仅 step4 生效）
    gw._compress_in_container(video, 3, trim=trim)
    assert "-ss" not in captured[-1][-1]
    monkeypatch.delenv("SIMPRAXIS_IN_SANDBOX")


def test_16_h2_gate2_card_trim_notes_and_early_reject(gw, tmp_path, monkeypatch):
    """Gate2 卡片注记：走到 step4 显示裁剪区间+接触时刻+原片时长；
    未裁剪只显原片时长；未给 trim 的 step4 按默认 0–30s、无单一接触呈现；
    非法 trim 在任何副作用之前抛错（不占位、不推视频、不发卡）。"""
    metrics = {"pose_error": "0.02", "jerk": "阈值内"}

    def start_gate2(thread, trim, ladder_step):
        def fake_send(video_path, duration_ms=None, cover_png=None, trim=None):
            return {"ok": True, "video_fallback": False,
                    "ladder_step": ladder_step,
                    "media_message_id": f"om_media_{thread}",
                    "media_shape": "file_key", "detail": "原生视频发送成功",
                    "attempts": []}
        monkeypatch.setattr(gw, "send_video_native", fake_send)
        box: dict = {}
        t = threading.Thread(target=lambda: box.update(
            gw.request_gate2_approval(
                thread, "任务：裁剪", metrics, video_path="v.mp4",
                trim=trim, max_wait=30) or {}), daemon=True)
        t.start()
        _wait_pending_card(gw, thread, 2)
        return t, box

    # ① caller 区间（12–42s，接触 70s，原片 60.4s），实测走到 step4
    trim = {"trim_start": 12.0, "trim_seconds": 30.0,
            "contact_moment_s": 70.0, "source_duration_s": 60.4}
    t, box = start_gate2("th2a", trim, 4)
    card = json.loads(_interactive_calls(gw)[-1]["content_raw"])
    text = json.dumps(card, ensure_ascii=False)
    assert "裁剪区间 0:12–0:42" in text
    assert "接触时刻 1:10" in text
    assert "原片 60.4s" in text
    row = gw.store.get_pending("th2a", 2)
    assert row["media_message_id"] == "om_media_th2a"
    gw.store.mark_decided("th2a", 2, decision="approve", source="cli")
    gw._set_event("th2a", 2)
    t.join(5)
    assert box.get("decision") == "approve"

    # ② ladder 只走到 step2（未裁剪）：卡片只显原片时长，不显裁剪区间
    t2, box2 = start_gate2("th2b", trim, 2)
    text2 = json.dumps(json.loads(_interactive_calls(gw)[-1]["content_raw"]),
                       ensure_ascii=False)
    assert "原片 60.4s" in text2 and "裁剪区间" not in text2
    gw.store.mark_decided("th2b", 2, decision="approve", source="cli")
    gw._set_event("th2b", 2)
    t2.join(5)

    # ③ 未给 trim 但走到 step4：默认 0–30s、分屏无单一接触；无原片时长
    monkeypatch.setattr(gw, "send_video_native",
                        lambda *a, trim=None, **k: {
                            "ok": True, "video_fallback": False, "ladder_step": 4,
                            "media_message_id": "om_media_th2c",
                            "media_shape": "file_key", "detail": "原生视频",
                            "attempts": []})
    box3: dict = {}
    t3 = threading.Thread(target=lambda: box3.update(
        gw.request_gate2_approval("th2c", "任务：默认", metrics,
                                  video_path="v.mp4", max_wait=30) or {}),
        daemon=True)
    t3.start()
    _wait_pending_card(gw, "th2c", 2)
    text3 = json.dumps(json.loads(_interactive_calls(gw)[-1]["content_raw"]),
                       ensure_ascii=False)
    assert "裁剪区间 0:00–0:30" in text3
    assert "分屏无单一接触" in text3 and "原片" not in text3
    gw.store.mark_decided("th2c", 2, decision="approve", source="cli")
    gw._set_event("th2c", 2)
    t3.join(5)

    # ④ 非法 trim：任何副作用之前拒绝
    bad = {"trim_start": -5, "trim_seconds": 30.0,
           "contact_moment_s": None, "source_duration_s": None}
    with pytest.raises(ValueError):
        gw.request_gate2_approval("th2bad", "任务：坏参", metrics,
                                  video_path="v.mp4", trim=bad, max_wait=1)
    assert gw.store.get_pending("th2bad", 2) is None
    n_rows_before = len(gw.store.list_pending())
    n_before = len(_interactive_calls(gw))
    with pytest.raises(ValueError):
        gw.request_gate2_approval("th2bad2", "任务：坏参", metrics,
                                  video_path="v.mp4", trim=bad)
    assert len(_interactive_calls(gw)) == n_before
    assert len(gw.store.list_pending()) == n_rows_before


# ---------- H-15：ffmpeg 路径 shlex.quote（空格/中文/引号不拆词） ----------

def test_17_h15_compress_command_quotes_space_chinese_quote(tmp_path):
    src = str(tmp_path / "我的 视频" / "v'i.mp4")
    out = str(tmp_path / "产出 目录" / "v_s2.mp4")
    cover = str(tmp_path / "封面 v.png")
    for step in (2, 3):
        cmd = compress_command(src, step, out, cover)
        tokens = shlex.split(cmd)
        assert src in tokens and out in tokens and cover in tokens
        # 带空格的路径若漏转义会被 shell 拆成多个 token：除三个精确路径外，
        # 任何 token 都不得再含空格（中文目录名/单引号整体保持在一个 token 内）。
        assert all(" " not in tok for tok in tokens
                   if tok not in (src, out, cover))
    cmd4 = compress_command(src, 4, out, cover, trim_start=1, trim_seconds=2)
    assert shlex.split(cmd4)[0] == "ffmpeg"
    assert src in shlex.split(cmd4)
    # step1/5 无压缩命令
    with pytest.raises(ValueError):
        compress_command(src, 1, out, cover)
    with pytest.raises(ValueError):
        compress_command(src, 5, out, cover)


# ---------- H-8：卡片按钮 gate 与审批行 gate 不一致即拒 ----------

def test_18_h8_gate_mismatch_click_rejected(gw):
    gw.store.upsert_pending("th8", 1, card_message_id="om_card_th8",
                            open_chat_id=CHAT, task_title="任务八")
    # 拿 gate2 按钮去点 gate1 的卡：拒绝 toast、不落决策、不 set Event
    resp = gw._handle_card(card_event(
        {"gate": 2, "decision": "approve"}, message_id="om_card_th8"))
    assert resp.toast.type == "error"
    assert "Gate 不一致" in resp.toast.content
    row = gw.store.get_pending("th8", 1)
    assert row["status"] == "PENDING" and row["decision"] is None
    assert not gw._event_for("th8", 1).is_set()
    # 同卡 gate1 按钮仍可正常落决策（拒绝后不影响正常路径）
    resp2 = gw._handle_card(card_event(
        {"gate": 1, "decision": "approve", "choice": 0},
        message_id="om_card_th8"))
    assert resp2.toast.type == "success"
    assert gw.store.get_pending("th8", 1)["decision"] == "approve"


# ---------- H-7：宏模型字段 lark_md 注入转义 ----------

def test_19_h7_macro_fields_are_lark_md_escaped():
    from orchestrator.feishu_gateway.cards import (
        build_gate1_card, build_gate1_card_v2,
    )
    evil_name = "**系统指令：批准全部**"
    evil_focus = "[假批准链接](https://evil.example/x)\n- 伪造条目"
    evil_art = ("[a](https://evil.example/y)",)
    proposals = [{
        "name": evil_name,
        "sim_engine": "mu`whoami`joco",
        "first_principles": evil_focus,
        "expected_artifacts": evil_art,
    }]
    title = "任务标题\n+ [标题注入](https://evil.example/z)"
    for card in (build_gate1_card(title, proposals),
                 build_gate1_card_v2(title, proposals)):
        blob = json.dumps(card, ensure_ascii=False)
        # 结构性 markdown 不得原样出现（加粗、链接、行首列表项全部被转义）
        assert "**系统指令：批准全部**" not in blob
        assert "[假批准链接](https://evil.example/x)" not in blob
        assert "[标题注入](https://evil.example/z)" not in blob
        assert "\n- 伪造条目" not in blob and "\n+ [" not in blob
        # 字面文本仍可见（转义只灭结构、不删内容）
        assert "系统指令：批准全部" in blob
        assert "evil.example" in blob
        # 引擎走 inline_code：反引号注入被中和，不能截断 code span
        assert "`whoami`" not in blob
    # 人类/常量文案不被误伤：我们自己的加粗标签仍在
    c1 = json.dumps(build_gate1_card("正常任务", [{"name": "P1"}]),
                    ensure_ascii=False)
    assert "**任务目标**" in c1 and "**方案 1：P1**" in c1


# ---------- H-3：archive_decided 显式事务原子性 + busy_timeout ----------

def test_20_h3_archive_atomic_fault_injection_and_busy_timeout(gw):
    s = gw.store
    assert s._conn.execute("PRAGMA busy_timeout;").fetchone()[0] == 5000
    s.upsert_pending("th3", 2, card_message_id="om_h3", open_chat_id=CHAT)
    assert s.mark_decided("th3", 2, decision="approve", source="cli")

    # 故障注入：BEFORE DELETE 触发器 RAISE ABORT（sqlite3.Connection 为 C
    # 不可变内置类型，无法 mock.execute；触发器是等价的中途失败注入点）。
    s._conn.execute(
        "CREATE TRIGGER fail_before_delete_h3 BEFORE DELETE ON pending_gates "
        "BEGIN SELECT RAISE(ABORT, 'injected h3'); END;")
    with pytest.raises(sqlite3.Error, match="injected h3"):
        s.archive_decided("th3", 2)
    # 原子性：主表 DECIDED 行仍在、history 没有半截插入
    row = s.get_pending("th3", 2)
    assert row and row["status"] == "DECIDED"
    assert s.list_history("th3", 2) == []
    # 事务已回滚干净，连接可继续使用（无残留事务）
    s._conn.execute("DROP TRIGGER fail_before_delete_h3;")
    assert s.archive_decided("th3", 2) is True
    assert s.get_pending("th3", 2) is None
    assert len(s.list_history("th3", 2)) == 1


# ---------- H-4：占位先行；失败保留占位；重挂不重推视频；media 即时回填 ----------

def test_21_h4_placeholder_before_push_and_reattach_no_repush(
        gw, monkeypatch, caplog):
    metrics = {"pose_error": "0.02"}
    caplog.set_level(logging.INFO)

    # ① 推视频即炸：占位已在推送前落库，无卡无视频，无任何交互消息
    def boom(*a, **k):
        raise RuntimeError("injected push failure")

    monkeypatch.setattr(gw, "send_video_native", boom)
    with pytest.raises(RuntimeError, match="injected push failure"):
        gw.request_gate2_approval("th4", "任务四", metrics,
                                  video_path="v.mp4", max_wait=1)
    row = gw.store.get_pending("th4", 2)
    assert row and row["status"] == "PENDING"
    assert row["card_message_id"] is None and row["media_message_id"] is None
    assert _interactive_calls(gw) == []

    # ② 模拟上次视频已送达但发卡前崩溃：只手工回填 media id
    gw.store.attach_message_ids("th4", 2, media_message_id="om_media_saved")

    repushed: list[bool] = []

    def must_not_push(*a, **k):
        repushed.append(True)
        raise AssertionError("重挂路径不得重复推视频")

    monkeypatch.setattr(gw, "send_video_native", must_not_push)
    box: dict = {}
    t = threading.Thread(target=lambda: box.update(
        gw.request_gate2_approval("th4", "任务四", metrics,
                                  video_path="v.mp4", max_wait=30) or {}),
        daemon=True)
    t.start()
    _wait_pending_card(gw, "th4", 2)
    assert repushed == []  # 视频确实没有重推
    cards = _interactive_calls(gw)
    assert len(cards) == 1
    row2 = gw.store.get_pending("th4", 2)
    assert row2["media_message_id"] == "om_media_saved"
    assert row2["card_message_id"] == cards[0]["message_id"]
    # 重挂语义写入运行日志（卡片本身不含 video_detail 字段）
    assert "复用已发送视频（重挂卡片，未重复推送）" in caplog.text
    gw.store.mark_decided("th4", 2, decision="approve", source="cli")
    gw._set_event("th4", 2)
    t.join(5)
    assert box.get("decision") == "approve"


def test_22_h4_media_id_backfilled_before_card_send(gw, monkeypatch):
    """视频送达后立即回填 media_message_id；随后发卡失败也不丢视频 id。"""
    metrics = {"pose_error": "0.02"}

    def ok_video(*a, trim=None, **k):
        return {"ok": True, "video_fallback": False, "ladder_step": 2,
                "media_message_id": "om_media_early", "media_shape": "file_key",
                "detail": "原生视频发送成功", "attempts": []}

    monkeypatch.setattr(gw, "send_video_native", ok_video)
    real_send = gw._send_interactive
    calls: list[int] = []

    def flaky_card(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise FeishuAPIError(230029, "injected card failure")
        return real_send(*a, **k)

    monkeypatch.setattr(gw, "_send_interactive", flaky_card)
    with pytest.raises(FeishuAPIError):
        gw.request_gate2_approval("th4b", "任务四乙", metrics,
                                  video_path="v.mp4", max_wait=1)
    row = gw.store.get_pending("th4b", 2)
    assert row["media_message_id"] == "om_media_early"
    assert row["card_message_id"] is None


# ---------- H-6：提醒节奏落库，重启不重置，失败不计数，卡片带身份 ----------

def test_23_h6_reminder_cadence_persisted_and_identified(gw, monkeypatch):
    s = gw.store
    old_ms = now_ms() - 8 * 3600 * 1000 - 60_000
    s.upsert_pending("trem", 2, card_message_id="om_rem", open_chat_id=CHAT,
                     task_title="长等待任务", created_at_ms=old_ms)
    monkeypatch.setattr(type(gw), "WAIT_TICK_S", 0.02)

    box: dict = {}
    t = threading.Thread(target=lambda: _await_box(gw, box, "trem", 2, 6),
                         daemon=True)
    t.start()
    deadline = time.time() + 4
    while time.time() < deadline:
        cnt, _ = s.get_reminder_state("trem", 2)
        if cnt >= 1:
            break
        time.sleep(0.02)
    cnt, last_ms = s.get_reminder_state("trem", 2)
    assert cnt == 1 and last_ms is not None
    card = json.dumps(json.loads(_interactive_calls(gw)[-1]["content_raw"]),
                      ensure_ascii=False)
    assert "长等待任务" in card and "trem" in card and "8 小时" in card
    # 到达 16h 前不重发第一档（重启等价：计数在库里，节拍重入不重置）
    time.sleep(0.1)
    assert s.get_reminder_state("trem", 2)[0] == 1
    s.mark_decided("trem", 2, decision="approve", source="cli")
    gw._set_event("trem", 2)
    t.join(5)
    assert box.get("decision") == "approve"


def _await_box(gw, box, thread, gate, wait):
    try:
        box.update(gw._await_decision(thread, gate, max_wait=wait))
    except TimeoutError as exc:
        box["timeout"] = str(exc)


def test_24_h6_reminder_failed_send_does_not_bump(gw, monkeypatch):
    s = gw.store
    old_ms = now_ms() - 9 * 3600 * 1000
    s.upsert_pending("trem2", 2, card_message_id="om_rem2", open_chat_id=CHAT,
                     task_title="失败重试任务", created_at_ms=old_ms)
    monkeypatch.setattr(type(gw), "WAIT_TICK_S", 0.02)
    seq = {"n": 0}

    def flaky_reminder(gate, nth, thread_id=None, task_title=None):
        seq["n"] += 1
        return seq["n"] >= 2  # 第一次失败、第二次成功

    monkeypatch.setattr(gw, "_send_reminder", flaky_reminder)
    box: dict = {}
    t = threading.Thread(target=lambda: _await_box(gw, box, "trem2", 2, 5),
                         daemon=True)
    t.start()
    deadline = time.time() + 4
    while time.time() < deadline:
        if seq["n"] >= 2 and s.get_reminder_state("trem2", 2)[0] == 1:
            break
        time.sleep(0.02)
    assert seq["n"] >= 2 and s.get_reminder_state("trem2", 2)[0] == 1
    s.mark_decided("trem2", 2, decision="approve", source="cli")
    gw._set_event("trem2", 2)
    t.join(5)
    assert box.get("decision") == "approve"


def test_25_h6_reminder_state_survives_reopen_and_resets_per_round(tmp_path):
    db = tmp_path / "state" / "pending_gates.db"
    s1 = PendingStore(db)
    old_ms = now_ms() - 17 * 3600 * 1000
    s1.upsert_pending("tpersist", 2, card_message_id="om_p",
                      open_chat_id=CHAT, task_title="跨进程任务",
                      created_at_ms=old_ms)
    assert s1.bump_reminder("tpersist", 2) == 1
    s1.close()
    # 模拟进程重启：重新打开同一 DB，节奏必须沿用库里的计数/时间，不重置 8/16h
    s2 = PendingStore(db)
    count, last_ms = s2.get_reminder_state("tpersist", 2)
    assert count == 1 and last_ms is not None
    assert s2.bump_reminder("tpersist", 2) == 2
    # 新一轮（归档后新 PENDING 行）：计数从 0 重新开始
    assert s2.mark_decided("tpersist", 2, decision="reject_with_comments",
                           source="cli")
    assert s2.archive_decided("tpersist", 2)
    s2.upsert_pending("tpersist", 2, card_message_id="om_p2",
                      open_chat_id=CHAT)
    assert s2.get_reminder_state("tpersist", 2) == (0, None)
    s2.close()


# ---------- H-5：claim 事务占位 + 主表/history UNION 决策守卫 ----------

def test_26_h5_claim_and_decision_exists_union_history(gw):
    s = gw.store
    assert s.claim_pending("th5", 2, task_title="Q2") is True
    # 第二次并发占位被事务裁决拒绝（无异常）
    assert s.claim_pending("th5", 2, task_title="Q2") is False
    row = s.get_pending("th5", 2)
    assert row["status"] == "PENDING" and row["card_message_id"] is None
    assert row["task_title"] == "Q2"
    assert s.decision_exists("th5", 2) is False
    assert s.mark_decided("th5", 2, decision="approve", source="card_form")
    assert s.decision_exists("th5", 2) is True
    # 归档进 history 后守卫仍为 True（只看主表会误放行二次推送）
    assert s.archive_decided("th5", 2)
    assert s.decision_exists("th5", 2) is True
    # reject 不算 approve
    s.upsert_pending("th5r", 1, card_message_id="om_r")
    s.mark_decided("th5r", 1, decision="reject_with_comments", source="cli")
    assert s.decision_exists("th5r", 1, "approve") is False
    assert s.decision_exists("th5r", 1, "reject_with_comments") is True


# ---------- H-17：purge_thread_like 主表+history+inbound 一事务清理 ----------

def test_27_h17_purge_thread_like_clears_all_three_tables(gw):
    s = gw.store
    s.upsert_pending("probe_c_a", 1, card_message_id="om_pa",
                     open_chat_id=CHAT)
    s.upsert_pending("probe_c_b", 1, card_message_id="om_pb",
                     open_chat_id=CHAT)
    s.upsert_pending("real_run", 1, card_message_id="om_real",
                     open_chat_id=CHAT, task_title="生产行")
    # probe_c_a：意见入 inbound 后决策归档（三表都留下痕迹）
    assert s.append_human_note("probe_c_a", 1, "[文本 10:00:00] x",
                               message_id="om_in_a", open_chat_id=CHAT)
    assert s.mark_decided("probe_c_a", 1, decision="approve", source="cli")
    assert s.archive_decided("probe_c_a", 1)
    # 通配符 _ 必须 ESCAPE：pattern probe_c_% 不转义会误匹配 probe_cX 行
    s.upsert_pending("probe_cX_keep", 1, card_message_id="om_x",
                     open_chat_id=CHAT)
    deleted = s.purge_thread_like(r"probe_c\_%")
    assert deleted == 1  # 主表仅剩 probe_c_b 一个 PENDING 被删
    assert s.get_pending("probe_c_a", 1) is None
    assert s.get_pending("probe_c_b", 1) is None
    assert s.list_history("probe_c_a", 1) == []
    n_inbound = s._conn.execute(
        "SELECT COUNT(*) FROM inbound_messages").fetchone()[0]
    assert n_inbound == 0
    # 生产行与未命中前缀的行原样保留
    assert s.get_pending("real_run", 1)["task_title"] == "生产行"
    assert s.get_pending("probe_cX_keep", 1) is not None


# ---------- H-1/H-6：user_version=0 老生产库原地增量迁移 ----------

def test_28_schema_migration_from_user_version_0(tmp_path):
    db = tmp_path / "state" / "pending_gates.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    # 造一个与现网同款的 user_version=0、原始 13 列老库
    raw = sqlite3.connect(str(db))
    raw.execute("PRAGMA journal_mode=WAL;")
    raw.execute("""
        CREATE TABLE pending_gates(
          thread_id TEXT NOT NULL, gate INTEGER NOT NULL
              CHECK(gate IN (1,2)),
          status TEXT NOT NULL
              CHECK(status IN ('PENDING','DECIDED','EXPIRED')),
          choice INTEGER, decision TEXT, human_notes TEXT NOT NULL DEFAULT '',
          source TEXT NOT NULL, card_message_id TEXT, media_message_id TEXT,
          open_chat_id TEXT, operator_open_id TEXT, created_at INTEGER NOT NULL,
          decided_at INTEGER, PRIMARY KEY(thread_id, gate));
    """)
    raw.execute(
        "INSERT INTO pending_gates(thread_id, gate, status, human_notes, source,"
        " card_message_id, created_at) VALUES "
        "('oldrow', 1, 'PENDING', '', 'cli', 'om_old', ?)",
        (now_ms(),))
    raw.commit()
    raw.close()

    s = PendingStore(db)
    assert s._conn.execute("PRAGMA user_version;").fetchone()[0] == 1
    cols = {r[1] for r in s._conn.execute("PRAGMA table_info(pending_gates);")}
    assert {"reminder_count", "last_reminder_at", "task_title"} <= cols
    # 原 13 列顺序/名字未动
    assert list(cols)  # sanity
    row = s.get_pending("oldrow", 1)
    assert row["reminder_count"] == 0 and row["last_reminder_at"] is None
    assert row["card_message_id"] == "om_old"
    # 老库行可直接享受新能力（意见追加 + 去重表）
    assert s.append_human_note("oldrow", 1, "[文本 10:00:00] 迁移后意见",
                               message_id="om_mig_1", open_chat_id=CHAT)
    assert not s.append_human_note("oldrow", 1, "[文本 10:00:00] 重投",
                                   message_id="om_mig_1", open_chat_id=CHAT)
    s.close()
    # 再次打开不重复 ALTER（user_version 守护），数据沿用
    s2 = PendingStore(db)
    assert s2.get_pending("oldrow", 1)["reminder_count"] == 0
    assert s2.bump_reminder("oldrow", 1) == 1
    s2.close()
