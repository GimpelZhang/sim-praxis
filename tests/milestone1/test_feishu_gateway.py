"""Phase 1 Task 1 飞书审批网关单测：9 个用例（计划要求 7 个①-⑦ + 1 个解析纯函数
+ 1 个探针 C 锁内适配回归⑧），0 付费 0 联网。

全部 monkeypatch requests.post；WS 不启动（start_ws=False），合成事件对象直接调
回调处理函数。fixture 凭据形状刻意避开 scan_secrets.sh 九条正则（无 cli_/ark-/sk-/
hf_ 前缀与真实赋值形态；构造函数注入，测试不依赖 FEISHU_* 环境变量）：
  app_id=FIXTURE_APP_ID_000，secret=FIXTURE_SECRET_<32 位 0>，owner=ou_fixture...

用例（详细计划 L428）：
  ① 空意见 reject -> error toast 且无 DECIDED 行；
  ② card_form approve -> DECIDED/choice/source=card_form 且 Event set；
  ③ p2p 文本：窗内合并 human_notes、窗外忽略、已 DECIDED 仅留痕；
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
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.feishu_gateway import client as client_mod
from orchestrator.feishu_gateway.cards import parse_card_event, parse_message_event
from orchestrator.feishu_gateway.client import FeishuAPIError, FeishuGateway
from orchestrator.feishu_gateway.store import now_ms
from orchestrator.feishu_gateway.video import HARD_LIMIT, MB, select_ladder_step

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
               create_ms=None):
    return SimpleNamespace(event=SimpleNamespace(
        message=SimpleNamespace(
            chat_id=chat_id, chat_type="p2p", message_type="text",
            content=json.dumps({"text": text}, ensure_ascii=False),
            message_id=message_id,
            create_time=create_ms if create_ms is not None else now_ms()),
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


# ---------- ③ P2P 文本：窗内合并 / 窗外忽略 / 已决定仅留痕 ----------

def test_03_p2p_text_window_merge_outside_ignore_decided_append(gw):
    # 窗内：两条文本合并进同一 PENDING 行，各自有 [文本 HH:MM:SS] 前缀，且有回执
    gw.store.upsert_pending("t3a", 1, card_message_id="om_card_3a",
                            open_chat_id=CHAT)
    gw._handle_message(text_event("第一条自由意见", chat_id=CHAT,
                                  message_id="om_t1"))
    gw._handle_message(text_event("第二条自由意见", chat_id=CHAT,
                                  message_id="om_t2"))
    notes = gw.store.get_pending("t3a", 1)["human_notes"]
    lines = notes.splitlines()
    assert len(lines) == 2
    assert lines[0].startswith("[文本 ") and lines[0].endswith("第一条自由意见")
    assert "第二条自由意见" in lines[1]
    replies = _text_calls(gw)
    assert any("已记录意见 7 字" in r for r in replies)
    assert any("已记录意见 7 字" in r for r in replies)

    # 窗外：created_at 早于 10 分钟会话窗 -> 忽略，不追加、不回执
    chat_b = "oc_fixture_outside_window_00000000000000000000"
    gw.store.upsert_pending(
        "t3b", 1, card_message_id="om_card_3b", open_chat_id=chat_b,
        created_at_ms=now_ms() - 11 * 60 * 1000)
    before = len(_text_calls(gw, chat_b))
    gw._handle_message(text_event("窗外的意见", chat_id=chat_b,
                                  message_id="om_t3"))
    row_b = gw.store.get_pending("t3b", 1)
    assert row_b["human_notes"] == "" and row_b["status"] == "PENDING"
    assert len(_text_calls(gw, chat_b)) == before

    # 已 DECIDED：文本仅追加留痕，决策/choice/source 不变
    gw.store.upsert_pending("t3c", 1, card_message_id="om_card_3c",
                            open_chat_id=CHAT + "c")
    assert gw.store.mark_decided("t3c", 1, decision="approve",
                                 source="card_form", choice=0)
    gw._handle_message(text_event("决定后补充说明", chat_id=CHAT + "c",
                                  message_id="om_t4"))
    row_c = gw.store.get_pending("t3c", 1)
    assert row_c["status"] == "DECIDED" and row_c["decision"] == "approve"
    assert row_c["source"] == "card_form" and row_c["choice"] == 0
    assert "决定后补充说明" in row_c["human_notes"]

    # 非文本/非 p2p/非 owner 的消息一律不关联
    gw.store.upsert_pending("t3d", 1, card_message_id="om_card_3d",
                            open_chat_id=CHAT + "d")
    gw._handle_message(text_event("别人说的", sender=OTHER, chat_id=CHAT + "d"))
    assert gw.store.get_pending("t3d", 1)["human_notes"] == ""


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
    # 纯函数边界（28MB 直传；30MB 接口硬上限；粗分档决定起始级）
    assert select_ladder_step(28 * MB) == 1
    assert select_ladder_step(28 * MB + 1) == 2
    assert select_ladder_step(31 * MB) == 2       # 用例锚点：31MB 从 step2 起
    assert select_ladder_step(100 * MB) == 3
    assert select_ladder_step(200 * MB) == 4
    assert select_ladder_step(300 * MB) == 5
    assert HARD_LIMIT == 30 * MB

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

    def fake_compress(host_video, step, _out=tmp_path / "runs"):
        compress_steps.append(step)
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
    assert compress_steps == [2, 3, 4]
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
    def fake_compress(host_video, step):
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
