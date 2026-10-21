#!/usr/bin/env python3
"""Phase 2 Task0 零付费验收（Phase_2_Plan_detailed.md v1.2 §Task0 + 附录 A）。

测试名与附录 A 表逐项对应；另含 §3.2 附录 B 分流项 S-1/S-2/S-7/S-10 机判。
全部 monkeypatch / 临时库 / 断网 env，0 付费、0 触网。fixture 字串刻意保持
「明显是夹具」形状，不含任何真实凭据。
"""
from __future__ import annotations

import ast
import importlib.util
import json
import os
import subprocess
import sys
import types
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml
from litellm.exceptions import ServiceUnavailableError

import orchestrator.gateway.router as R
from orchestrator.agent_runner import AgentRunner, RunSpec
from orchestrator.common.callcache import request_fingerprint
from orchestrator.common.clock import (
    parse_ts_iso_lenient,
    parse_ts_iso_strict,
    utc_now_iso_z,
)
from orchestrator.common.secrets import contains_secret, redact_text, scan_text
from orchestrator.direction.inputs import parse_track1, parse_track2
from orchestrator.feishu_gateway.cards import md_escape
from orchestrator.feishu_gateway.store import PendingStore
from orchestrator.gateway import quota_ledger as ql
from orchestrator.gateway.quota_ledger import (
    QUOTA_PAUSE_SCHEMA_V2_KEYS,
    QuotaLedger,
    billing_context,
)
# Task0b：S-7 analyze_q2 load_frame 断言随实现迁入 KB 内容层
# （solutions/track2_questions/02_lidar_weather/tests/test_analyze_q2_contract.py
# ::test_s7_load_frame_missing_file_and_key），引擎仓不再 import 已删的
# sandbox.track2 包。
from tests.milestone1.test_agent_runner import (
    CONVERGING_SCRIPT,
    FakeVector,
    _write_workdir,
)

REPO = Path(__file__).resolve().parents[2]

# ---------- 共享网关夹具（合成传输，0 触网） ----------

DEFAULT_IDS = {
    "macro_ideation": "deepseek-v4.1-flash",
    "coding_debug": "glm-5.3-flash",
    "visual_rollout": "ark-code-latest",
}


@pytest.fixture()
def bill_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMPRAXIS_LLM_TOKEN", "task0-billing-fixture-key-0001")
    monkeypatch.setenv("SIMPRAXIS_LLM_BASE_URL",
                       "https://ark.cn-beijing.volces.com/api/coding")
    monkeypatch.setenv("SIMPRAXIS_PHASE1_ROOT", str(tmp_path / "phase1"))
    monkeypatch.setenv("SIMPRAXIS_PHASE2_ROOT", str(tmp_path / "phase2"))
    monkeypatch.setenv("SIMPRAXIS_GATEWAY_STATE", str(tmp_path / "gwstate"))
    oq = tmp_path / "open-questions.md"
    oq.write_text("# Open Questions\n\n- 夹具占位\n", encoding="utf-8")
    monkeypatch.setenv("SIMPRAXIS_OPEN_QUESTIONS", str(oq))
    map_path = tmp_path / "stage_models.yaml"
    sm = {s: {"model_id": DEFAULT_IDS[s],
              "capabilities": ["text", "vision"] if s == R.VISION_STAGE else ["text"],
              "purpose": "t"} for s in R.STAGES}
    map_path.write_text(yaml.safe_dump({
        "meta": {"schema_version": "1.0", "endpoint": "anthropic-messages",
                 "base_url_env": "ANTHROPIC_BASE_URL"},
        "stage_models": sm}, allow_unicode=True, width=4096), encoding="utf-8")
    return {"tmp": tmp_path, "map": map_path}


def _gateway(env, transport=None, raw_transport=None, **kw):
    return R.ModelGateway(stage_models_path=env["map"], sleep=lambda s: None,
                          transport=transport, raw_transport=raw_transport,
                          ledger=QuotaLedger(), **kw)


def _litellm_resp(content="ok", *, in_tok=7, out_tok=3, cache_read=None,
                  cache_create=None, model="glm-5.3-flash"):
    return types.SimpleNamespace(
        choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=content))],
        model=model,
        usage=types.SimpleNamespace(
            input_tokens=in_tok, output_tokens=out_tok,
            cache_read_input_tokens=cache_read,
            cache_creation_input_tokens=cache_create))


def _rows(tmp):
    p = tmp / "phase2/quota/ledger.jsonl"
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


def _int_rows(tmp):
    return [r for r in _rows(tmp) if isinstance(r.get("seq"), int)]


def _blocked(tmp):
    return [r for r in _rows(tmp) if r.get("event") == "preflight_blocked"]


# ---------- 附录 A：clock ----------

def test_clock_z_format_and_parse():
    z_re = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"
    now = utc_now_iso_z()
    assert __import__("re").match(z_re, now), now

    strict = parse_ts_iso_strict("2026-10-12T08:00:00Z")
    assert strict.tzinfo is timezone.utc
    for bad in ("2026-10-12T08:00:00+0800", "2026-10-12T08:00:00+00:00",
                "2026-10-12 08:00:00Z", "2026-10-12T08:00:00.0Z", ""):
        with pytest.raises(ValueError):
            parse_ts_iso_strict(bad)

    # 同一 UTC 瞬时三态：Z / +0000 / +0800（含冒号形式）
    ref = datetime(2026, 10, 12, 8, 30, 0, tzinfo=timezone.utc)
    s_z = parse_ts_iso_lenient("2026-10-12T08:30:00Z")
    s_0 = parse_ts_iso_lenient("2026-10-12T08:30:00+0000")
    s_8 = parse_ts_iso_lenient("2026-10-12T16:30:00+0800")
    s_8c = parse_ts_iso_lenient("2026-10-12T16:30:00+08:00")
    assert {d.astimezone(timezone.utc) for d in (s_z, s_0, s_8, s_8c)} == {ref}
    with pytest.raises(ValueError):
        parse_ts_iso_lenient("2026-10-12T08:30:00")


# ---------- 附录 A：ledger tz 归一迁移 ----------

def _load_normalize_module():
    path = REPO / "scripts" / "milestone2" / "normalize_ledger_tz.py"
    spec = importlib.util.spec_from_file_location("task0_normalize_ledger_tz", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _synthetic_ledger_29_rows() -> list[dict]:
    """29 行脱敏合成账：24 整数 seq + 5 行 seq=null；18 +0800 / 11 +0000。"""
    tz8 = timezone(timedelta(hours=8))
    rows: list[dict] = []
    n_0800 = n_0000 = 0
    for i in range(1, 30):
        # 前 18 行 +0800，后 11 行 +0000
        if i <= 18:
            dt = datetime(2026, 10, 10, 8 + (i % 12), 5, i % 60, tzinfo=tz8)
            ts = dt.strftime("%Y-%m-%dT%H:%M:%S%z")
            n_0800 += 1
        else:
            dt = datetime(2026, 10, 11, (i - 18) % 12, 7, i % 60,
                          tzinfo=timezone.utc)
            ts = dt.strftime("%Y-%m-%dT%H:%M:%S%z")
            n_0000 += 1
        blocked = i > 24  # 最后 5 行模拟 preflight_blocked（seq=null）
        rows.append({
            "ts_iso": ts, "event": "preflight_blocked" if blocked else "call",
            "stage": "coding_debug", "model": "fixture-model",
            "thread_id": "", "task": 4, "batch_id": "T0-FIXTURE",
            "seq": None if blocked else i - 0,  # 1..24 连续整数
            "global_used_after": (i - 5) if not blocked else 24,
            "ok": True, "err": None,
        })
    assert (n_0800, n_0000) == (18, 11)
    return rows


def test_normalize_ledger_tz_idempotent_and_preserves(tmp_path):
    mod = _load_normalize_module()
    ledger = tmp_path / "quota" / "ledger.jsonl"
    ledger.parent.mkdir(parents=True)
    before = _synthetic_ledger_29_rows()
    ledger.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in before),
        encoding="utf-8")
    raw_before = ledger.read_bytes()

    # 1) dry-run：统计 29 行待改，文件字节不动
    stats = mod.normalize(ledger, apply=False)
    assert (stats["total"], stats["plus_0800"], stats["plus_0000"],
            stats["already_z"], stats["changed"]) == (29, 18, 11, 0, 29)
    assert ledger.read_bytes() == raw_before
    assert list(tmp_path.rglob("*.bak-tz-*")) == []

    # 2) apply：备份存在（0600）；行数/seq 多重集/其余字段全等；同一 UTC 瞬时
    stats2 = mod.normalize(ledger, apply=True)
    assert stats2["changed"] == 29 and stats2["backup"]
    backup = Path(stats2["backup"])
    assert backup.is_file()
    assert (backup.stat().st_mode & 0o777) == 0o600

    after = [json.loads(l) for l in ledger.read_text().splitlines()]
    assert len(after) == 29
    seq_before = Counter(json.dumps(r.get("seq"), sort_keys=True) for r in before)
    seq_after = Counter(json.dumps(r.get("seq"), sort_keys=True) for r in after)
    assert seq_after == seq_before
    for old, new in zip(before, after):
        assert {k: v for k, v in old.items() if k != "ts_iso"} == \
               {k: v for k, v in new.items() if k != "ts_iso"}
        assert new["ts_iso"].endswith("Z")
        assert (parse_ts_iso_lenient(old["ts_iso"]).astimezone(timezone.utc)
                == parse_ts_iso_strict(new["ts_iso"]))
    # 抽样：+0800 16:xx 换算为同日 UTC 08:xx（同盘可对账）
    sample = after[0]
    assert sample["ts_iso"][11:13] == f"{int(before[0]['ts_iso'][11:13]) - 8:02d}"

    # 3) 幂等：再跑零变化、零新增备份
    stats3 = mod.normalize(ledger, apply=True)
    assert stats3["changed"] == 0 and stats3["backup"] is None
    assert list(tmp_path.rglob("ledger.jsonl.bak-tz-*")) == [backup]


# ---------- 附录 A：非 root compose/Dockerfile 配置 ----------

def test_nonroot_compose_config():
    compose = yaml.safe_load((REPO / "sandbox/docker-compose.yml").read_text())
    svc = compose["services"]["sim-sandbox"]
    # compose 显式 user（删此行即回退 Phase 1 root 行为）
    assert svc["user"] == "${SIMPRAXIS_UID:-1000}:${SIMPRAXIS_GID:-1000}"
    env = svc["environment"]
    assert env["HF_HOME"] == "/home/agent/.cache/huggingface"
    assert env["SIMPRAXIS_RUN_DIR"] == "/home/agent/runs"
    # 环境值里不得再有 /root
    for k, v in env.items():
        assert "/root" not in str(v), f"{k}={v} 仍指向 /root"
    targets = {m.split(":")[-1] for m in svc["volumes"]}
    assert "/home/agent/runs" in targets
    assert not any(t.startswith("/root") for t in targets), targets

    for name in ("Dockerfile", "Dockerfile.task8"):
        df = (REPO / "sandbox/docker" / name).read_text()
        assert "ARG UID=1000" in df and "ARG GID=1000" in df
        assert "useradd" in df and "agent" in df
        assert "/home/agent/.cache/huggingface" in df
        # 回退性：镜像默认 USER 仍是 root（不允许出现生效的 USER 指令）
        active_user = [ln.strip() for ln in df.splitlines()
                       if ln.strip().startswith("USER ")]
        assert active_user == [], f"{name} 不得切默认 USER：{active_user}"

    ep = (REPO / "sandbox/docker/entrypoint.sh").read_text()
    assert 'id -u' in ep                       # 存在 root/非 root 分支
    assert "[ -O " in ep                      # 非 root 只清自己属主的锁
    # 非 root 分支不得无条件 rm X99 锁；1777 锁目录由 Dockerfile 构建期准备
    assert "chmod 1777 /tmp/.X11-unix" in (
        REPO / "sandbox/docker/Dockerfile").read_text()


# ---------- 附录 A：P3 归档 + S-1 AST 白名单 ----------

PENDING_WRITE_ALLOWLIST: dict[str, set[int]] = {
    # 唯一合法生产发送闸门（H-5 claim + 重发 upsert）
    # Task0 后 Task3 前的位移：871→877（upsert）、949→955（标准 Gate2 claim）；
    # Task3 新增 1107（双媒体 Diff Gate2 的 H-4 claim，同一占位协议第二发送闸门）。
    # QA-2（2026-10-17）插入观察窗常量/方法与两请求器三态裁决，纯位移：
    # 877→947（upsert）、955→1031（标准 Gate2 claim）、1107→1197（Diff claim）；
    # _claim_loss_action 内只有 get_pending 读，无新增写闸门（AST 全仓扫描对账）。
    # R-LOW-2（2026-10-19，commit 1c6a2af）双活裁定窗状态翻转即放弃接管 +
    # 发送前 fresh 重读，纯位移：947→959（upsert）、1031→1043（标准 Gate2 claim）、
    # 1197→1220（Diff Gate2 claim）；无新增写闸门——AST 全仓扫描
    # （grep -rn '\.claim_pending(\|\.upsert_pending(' orchestrator scripts
    # memory_flywheel，排除 tests/）仍只有 client.py 三处 +
    # probes/probe_c_form.py:115 + scripts/milestone1/q2_human_gate.py:484。
    # R-LOW-3（2026-10-19）发送序列内 fresh 复查 + attach 0 行孤儿告警：
    # 新增 _attach_ids_or_warn/_fresh_pending_or_abort 两私有方法（attach 走
    # UPDATE 不进本白名单），纯位移：959→1003（upsert）、1043→1089
    # （标准 Gate2 claim）、1220→1299（Diff Gate2 claim）；grep 对账同上，
    # 无新增 .claim_pending/.upsert_pending 调用。
    # Task6（2026-10-19，同一原子提交）：ascent 接缝新增两个合法发送闸门——
    # request_gate1_ascent_approval 的 upsert（1438）与
    # request_gate2_diff4_approval 的 claim（1508，H-4 占位协议的第三发送
    # 闸门：四媒体严格顺序 + 一张 diff4 卡）；import 加行使老三处纯位移：
    # 1003→1009（标准 Gate1 upsert）、1089→1095（标准 Gate2 claim）、
    # 1299→1305（Task3 Diff claim）。attach 仍走 UPDATE 不进本白名单。
    # Task6 M2（2026-10-20，同一原子提交）：__init__ 加 require_approval_notes
    # 关键字（+2 行）、import 加 APPROVAL_EMPTY_TOAST（+1）、_handle_card 在
    # 打回空意见分支后插入批准空意见拦截分支（注释+分支共 +19 行），五处写
    # 闸门全部纯位移 +22：1009→1031（标准 Gate1 upsert）、1095→1117（标准
    # Gate2 claim）、1305→1327（Task3 Diff claim）、1438→1460（Task6 ascent
    # Gate1 upsert）、1508→1530（Task6 diff4 Gate2 claim）。新分支只 toast
    # 不落库（mark_decided 不可达），grep 对账无新增写闸门。
    "orchestrator/feishu_gateway/client.py": {
        1031, 1117, 1327, 1460, 1530},
    # 表单探针（人工取证工具，probes/ 在契约豁免内，仍钉死行号）
    "orchestrator/feishu_gateway/probes/probe_c_form.py": {115},
    # Phase 1 封存的人工 HITL 操作脚本（既有调用点冻结，禁止漂移）
    # Task0a 在 run() 前插入 _offgraph_learning_writer，449 → 467（纯位移，
    # 无新增 claim/upsert；diff 已人工核对）
    # Task0b optics_model 改经 resolver stage（stage_optics_model 插入），
    # 467 → 484（纯位移，无新增 claim/upsert；diff 已人工核对）
    "scripts/milestone1/q2_human_gate.py": {484},
}


def _calls_by_attr(root_rel: str):
    """yield (relpath, lineno, attr) for every .upsert_pending(/.claim_pending( 调用。"""
    for py in REPO.rglob("*.py"):
        rel = py.relative_to(REPO).as_posix()
        if rel.startswith((".git/", "docs/")) or "__pycache__" in rel:
            continue
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and node.func.attr in ("upsert_pending", "claim_pending"):
                yield rel, node.lineno, node.func.attr


def test_s1_pending_write_ast_allowlist():
    offenders = []
    for rel, lineno, attr in _calls_by_attr(str(REPO)):
        if rel.startswith("tests/"):
            continue  # 测试自身任意使用
        allowed = PENDING_WRITE_ALLOWLIST.get(rel, set())
        if lineno not in allowed:
            offenders.append(f"{rel}:{lineno} .{attr}(")
    assert not offenders, (
        "pending_gates 写入旁路：除白名单 (path,lineno) 外不得出现 upsert/claim 调用：\n"
        + "\n".join(offenders))
    # 白名单自身必须真实存在（防文件变动后白名单变空头支票）
    for rel, lines in PENDING_WRITE_ALLOWLIST.items():
        found = {ln for r, ln, _ in _calls_by_attr(str(REPO)) if r == rel}
        assert lines <= found, f"{rel} 白名单行 {lines - found} 已不存在"


def test_p3_archive_chokepoint(tmp_path):
    store = PendingStore(tmp_path / "pending.db")
    t, g = "t0-reject-rounds", 1

    # claim 占位闸门：首次 True，并发第二次 False
    assert store.claim_pending(t, g, task_title="Q?") is True
    assert store.claim_pending(t, g) is False
    # 占位行补卡消息（生产由发送成功后回填）
    store.attach_message_ids(t, g, card_message_id="om-round1")

    # 第一轮真人 reject
    assert store.mark_decided(
        t, g, decision="reject_with_comments", source="card_form",
        human_notes="意见一：必须加域随机化边界") is True
    assert store.decision_exists(t, g, "reject_with_comments") is True
    assert store.decision_exists(t, g, "approve") is False

    # 归档：DECIDED 行带旧 card_message_id 整体进 history、主表消失
    assert store.archive_decided(t, g) is True
    assert store.get_pending(t, g) is None
    hist = store.list_history(t, g)
    assert len(hist) == 1
    assert hist[0]["status"] == "DECIDED"
    assert hist[0]["card_message_id"] == "om-round1"
    assert "意见一" in hist[0]["human_notes"]
    # H-5：归档后 UNION 并查仍认该终态（防二次推送绕过）
    assert store.decision_exists(t, g, "reject_with_comments") is True
    # PENDING 不存在，新一轮 claim 放行
    assert store.claim_pending(t, g) is True
    store.attach_message_ids(t, g, card_message_id="om-round2")
    assert store.mark_decided(
        t, g, decision="reject_with_comments", source="p2p_text",
        human_notes="意见二：补充接触阻尼区间") is True
    assert store.archive_decided(t, g) is True

    hist2 = store.list_history(t, g)
    assert [h["card_message_id"] for h in hist2] == ["om-round1", "om-round2"]
    assert "意见一" in hist2[0]["human_notes"]
    assert "意见二" in hist2[1]["human_notes"]
    assert {h["status"] for h in hist2} == {"DECIDED"}
    assert store.get_pending(t, g) is None
    store.close()


# ---------- 附录 A：tokens 列 + seq 语义 ----------

def test_ledger_tokens_columns_and_seq_semantics(bill_env, monkeypatch):
    tmp = bill_env["tmp"]
    ql.write_approval(batch_id="T0-CACHE", task=4, stage="coding_debug",
                      approved=12, beyond_normal=False, note="Task0 夹具批次")

    # (a) cache 两列入账（取不到才是 None；此处显式给值必须落账）
    def transport_cache(**kw):
        return _litellm_resp("补丁", in_tok=10, out_tok=4,
                             cache_read=7, cache_create=1)

    gw = _gateway(bill_env, transport=transport_cache)
    with billing_context(task=4, batch_id="T0-CACHE", thread_id="t0-tok"):
        gw.call("coding_debug", [{"role": "user", "content": "fix"}])
    rows = _int_rows(tmp)
    assert len(rows) == 1
    for col in ("tokens_cache_read", "tokens_cache_create"):
        assert col in rows[0]
    assert rows[0]["tokens_cache_read"] == 7
    assert rows[0]["tokens_cache_create"] == 1
    assert (rows[0]["tokens_in"], rows[0]["tokens_out"]) == (10, 4)
    assert rows[0]["ts_iso"].endswith("Z")  # P2：新行严格 UTC Z

    # (b) 内部 HTTP 重试 3 次仍只 1 整数行
    attempts = {"n": 0}

    def transport_retry(**kw):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise ServiceUnavailableError(
                "503 unavailable", "anthropic", "glm-5.3-flash", None)
        return _litellm_resp()

    gw2 = _gateway(bill_env, transport=transport_retry)
    with billing_context(task=4, batch_id="T0-CACHE"):
        gw2.call("coding_debug", [{"role": "user", "content": "fix2"}])
    assert attempts["n"] == 3
    assert len(_int_rows(tmp)) == 2

    # (c) preflight 拦截（无批次凭据）：seq=null，不占整数行
    gw3 = _gateway(bill_env, transport=lambda **kw: pytest.fail("不得触网"))
    with billing_context(task=4, batch_id="T0-NO-SUCH"):
        with pytest.raises(R.GatewayPausedError):
            gw3.call("coding_debug", [{"role": "user", "content": "x"}])
    assert len(_int_rows(tmp)) == 2
    blocked = _blocked(tmp)
    assert len(blocked) == 1 and blocked[0]["seq"] is None

    # (d) 发网前失败（缺网关凭据）：同样 seq=null
    monkeypatch.delenv("SIMPRAXIS_LLM_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    gw4 = _gateway(bill_env, transport=lambda **kw: pytest.fail("不得触网"))
    with billing_context(task=4, batch_id="T0-CACHE", thread_id="t0-nocred"):
        with pytest.raises(R.MissingGatewayCredentialError):
            gw4.call("coding_debug", [{"role": "user", "content": "y"}])
    assert len(_int_rows(tmp)) == 2
    blocked2 = _blocked(tmp)
    assert len(blocked2) == 2 and all(r["seq"] is None for r in blocked2)

    # (e) callcache 命中：0 账行（真网关 + 真 runner，miss 一次后 kill-9 重放）
    # 传输全替身不触网；(d) 的 delenv 只验缺凭据路径，这里恢复夹具 token。
    monkeypatch.setenv("SIMPRAXIS_LLM_TOKEN", "task0-billing-fixture-key-0001")
    _runner_cache_hit_zero_rows(tmp, bill_env["map"])
    assert len(_int_rows(tmp)) == 3  # 只有 miss 那一轮新增 1 行


def _runner_cache_hit_zero_rows(tmp, map_path):
    """miss 实呼 1 账行；同 spec run_idx 重放命中缓存：0 新增账行。"""
    from orchestrator.common.callcache import CallReplay

    thread = "t0-cache-hit"
    wd = _write_workdir(tmp / "wd1", CONVERGING_SCRIPT)
    patch = json.dumps({"patches": [{"path": "fix.txt", "old": "NEEDS_FIX",
                                     "new": "PATCHED"}]})
    gw = R.ModelGateway(
        stage_models_path=map_path, sleep=lambda s: None,
        transport=lambda **kw: _litellm_resp(patch, in_tok=11, out_tok=5),
        ledger=QuotaLedger())
    cache = CallReplay(tmp / "replay_cache")

    def spec():
        return RunSpec(thread_id=thread, workdir=wd,
                       cmd=[sys.executable, str(wd / "script.py")],
                       telemetry_relpath="telemetry.json",
                       batch_id="T0-CACHE", billing_task=4, run_idx=1)

    r1 = AgentRunner(gw, FakeVector(), evidence_root=tmp / "ev",
                     call_cache=cache)
    rec = r1.run(spec())
    assert rec["status"] == "ok" and rec["coding_round"] == 1
    before_rows = len(_int_rows(tmp))
    assert before_rows == 3  # 前置两成功账行 + 本次 miss 一行

    # kill-9 形态：撤掉补丁效果与成功产物，缓存与证据保留
    (wd / "telemetry.json").unlink()
    (wd / "fix.txt").write_text("NEEDS_FIX", encoding="utf-8")
    r2 = AgentRunner(gw, FakeVector(), evidence_root=tmp / "ev",
                     call_cache=cache)
    rec2 = r2.run(spec())
    assert rec2["status"] == "ok" and rec2["coding_round"] == 1
    assert len(_int_rows(tmp)) == before_rows  # 命中：0 新增账行
    assert (wd / "fix.txt").read_text() == "PATCHED"


# ---------- 附录 A：callcache 指纹 ----------

def test_callcache_fingerprint_includes_stage_model():
    fp = request_fingerprint("同一句 prompt", None, {"max_tokens": 64},
                             stage="coding_debug", model="m-a")
    assert fp == request_fingerprint(
        "同一句 prompt", None, {"max_tokens": 64},
        stage="coding_debug", model="m-a")
    other_stage = request_fingerprint("同一句 prompt", None, {"max_tokens": 64},
                                      stage="macro_ideation", model="m-a")
    other_model = request_fingerprint("同一句 prompt", None, {"max_tokens": 64},
                                      stage="coding_debug", model="m-b")
    other_extra = request_fingerprint("同一句 prompt", None, {"max_tokens": 96},
                                      stage="coding_debug", model="m-a")
    assert len({fp, other_stage, other_model, other_extra}) == 4
    # 缺 stage/model（旧调用形态）也不得与带身份的指纹碰撞
    legacy = request_fingerprint("同一句 prompt", None, {"max_tokens": 64})
    assert legacy != fp


# ---------- 附录 A：冻结输入扩编 ----------

def test_freeze_inputs_extended():
    t1 = parse_track1()
    t2 = parse_track2()
    # 锁内适配（Task0）：计划 v1.2 写「7/6 条」，~/文档 原件实测两份均为 6 条，
    # 按逐字保真原则固化 6/6（不自造第 7 条），偏差已在 inputs.py 与 Task0 报告登记。
    assert len(t1) == 6 and len(t2) == 6
    assert [e["index"] for e in t1] == list(range(1, 7))
    assert [e["index"] for e in t2] == list(range(1, 7))
    assert [e["name"] for e in t1] == [
        "ManiSkill", "mujoco", "Gazebo", "LIBERO", "RoboCasa", "IsaacLab-Arena"]
    for e in t2:
        assert len(e["question"]) >= 20

    # 前 2 条与 git HEAD 版本字节一致：旧文件去掉行尾空白行后必须是新文件的前缀
    for rel in ("inputs/tracks/track_1_samples.txt",
                "inputs/tracks/track_2_samples.txt"):
        old = subprocess.run(["git", "show", f"HEAD:{rel}"], cwd=REPO,
                             capture_output=True, check=True).stdout.rstrip(b"\n")
        new = (REPO / rel).read_bytes()
        assert old, f"HEAD:{rel} 为空，前缀比较无意义"
        assert new.startswith(old), f"{rel} 前 2 条与 HEAD 版本出现字节差异"
    # 首行标题保留
    assert (REPO / "inputs/tracks/track_1_samples.txt").read_text().splitlines()[0] \
        == "Track 1: tools"


# ---------- 附录 A：md_escape 行首枚举 ----------

def test_md_escape_line_start_enum_and_pipe():
    assert md_escape("1. x").startswith("1\\. ")
    assert "\\." in md_escape("12. y")
    assert md_escape("- x").startswith("\\- ")
    assert md_escape("+ x").startswith("\\+ ")
    assert md_escape("| a |").startswith("\\|")
    # 换行后的行首同样转义
    out = md_escape("上文\n- b\n+ c\n| d |\n10. e")
    assert "\n\\- b" in out and "\n\\+ c" in out
    assert "\n\\| d " in out and "\n10\\. e" in out
    # 非行首结构字符不被过杀
    assert md_escape("a-b+c|d") == "a-b+c|d"
    assert md_escape("版本1. 说明") == "版本1. 说明"  # 不在行首的数字点不转义


# ---------- 附录 A：Phase 2 独立新账 ----------

def test_quota_phase2_independent_ledger(tmp_path, monkeypatch):
    p1 = tmp_path / "phase1"
    p2 = tmp_path / "phase2"
    (p1 / "quota").mkdir(parents=True)
    phase1_ledger = p1 / "quota/ledger.jsonl"
    sentinel = json.dumps({"seq": 99, "ts_iso": "2026-10-16T00:00:00+0000"})
    phase1_ledger.write_text(sentinel + "\n", encoding="utf-8")
    # 只读绊网：Phase 1 封存账及其目录在测试期不可写
    os.chmod(phase1_ledger, 0o400)
    os.chmod(p1 / "quota", 0o500)
    try:
        monkeypatch.setenv("SIMPRAXIS_PHASE1_ROOT", str(p1))
        monkeypatch.setenv("SIMPRAXIS_PHASE2_ROOT", str(p2))
        ledger = QuotaLedger()
        row1 = ledger.record("coding_debug", task=4, batch_id="T0-INDEP",
                              ok=True, model="fixture-model",
                              tokens_in=1, tokens_out=1)
        assert row1["seq"] == 1 and row1["ts_iso"].endswith("Z")
        QuotaLedger().record("coding_debug", task=4, batch_id="T0-INDEP",
                             ok=False, model="fixture-model",
                             err="fixture failure")
        new_path = p2 / "quota/ledger.jsonl"
        assert new_path.is_file()
        new_rows = [json.loads(l) for l in new_path.read_text().splitlines()]
        assert [r["seq"] for r in new_rows] == [1, 2]
        # Phase 1 封存账字节不动（新代码从未以写方式打开）
        assert phase1_ledger.read_text() == sentinel + "\n"
        assert not (p1 / "quota/counters.json").exists()
        assert not (p1 / "quota/approvals").exists()
    finally:
        os.chmod(p1 / "quota", 0o700)
        os.chmod(phase1_ledger, 0o600)


# ---------- S-2：密钥正则单一源 10+10 样本 ----------

S2_POSITIVES = [
    "hf_" + "a" * 20,
    "ghp_" + "b" * 20,
    "nvapi-" + "c" * 20,
    "ark-a" + "-" * 20,
    "sk-" + "d" * 24,
    "AKIA" + "0" * 16,
    "xoxb-" + "e" * 10,
    "-----BEGIN RSA PRIVATE KEY-----\n夹具\n-----END RSA PRIVATE KEY-----",
    # S-2 新堵盲区：JSON/env dump 形态（name 后带收尾引号）
    '{"access_token" : "' + "g" * 30 + '"}',
    "api_key='" + "f" * 30 + "'",
    "cli_" + "a" * 16,
]

S2_NEGATIVES = [
    "api_key='short'",
    'api_key="' + "z" * 23 + '"',                 # 23 字符，差 1
    'api_key="' + "z" * 30 + "'",                 # 左右引号不一致（\5 反向引用拒绝）
    'password = "' + "z" * 40 + '"',              # 名字不在密钥名集
    "hf_" + "a" * 19,                             # 差 1 字符
    "ghx_" + "b" * 20,                            # gh 后字母非法
    "sk-" + "d" * 23,                             # 差 1
    "AKIA" + "0" * 15,                            # 差 1 hex
    "xoxx-" + "e" * 10,                           # baprs 之外字母
    '{"token": "' + "g" * 40 + '"}',              # 裸 token 名不匹配
]


def test_s2_secret_patterns_positive_negative_samples():
    for i, sample in enumerate(S2_POSITIVES):
        hits = scan_text(sample)
        assert hits, f"正例 {i} 漏报：{sample[:40]}"
        assert contains_secret(sample)
    for i, sample in enumerate(S2_NEGATIVES):
        assert not scan_text(sample), f"负例 {i} 误报：{sample[:40]}"
        assert not contains_secret(sample)
    # redact 宽模式保留键名前缀（含新 JSON 形态的收尾引号与冒号空白）
    red = redact_text('{"access_token" : "' + "g" * 30 + '"}')
    assert red.startswith('{"access_token" : "') and "g" * 30 not in red
    # 严格口径与 shell 扫描同源（scan_secrets.sh 第 9 ERE 注释已声明同源）
    shell = (REPO / "scripts/verification/scan_secrets.sh").read_text()
    assert "access[_-]?token" in shell


# ---------- S-7：Task0b 起随 analyze_q2.py 迁入 KB 内容层 ----------
# 同名测试 test_s7_load_frame_missing_file_and_key 现位于
# solutions/track2_questions/02_lidar_weather/tests/test_analyze_q2_contract.py
# （在 KB 仓内随内容层 CI/手动 pytest 运行，引擎仓默认套件不再覆盖已删实现）。


# ---------- S-10：未分类重试等待预算 / 文本块信任 / .call_cache 落点枚举 ----------

def test_s10_unclassified_retry_uses_shared_wait_budget(bill_env):
    tmp = bill_env["tmp"]
    ql.write_approval(batch_id="T0-UNC", task=4, stage="coding_debug",
                      approved=4, beyond_normal=False, note="Task0 夹具")

    def boom(**kw):
        raise RuntimeError("未分类故障（夹具）")

    # max_total_wait=0：未分类错误的唯一一次重试也必须先过共享等待预算
    gw = _gateway(bill_env, transport=boom, max_attempts=50, max_total_wait=0.0)
    with billing_context(task=4, batch_id="T0-UNC"):
        with pytest.raises(R.GatewayError, match="等待预算"):
            gw.call("coding_debug", [{"role": "user", "content": "z"}])
    rows = _int_rows(tmp)
    assert len(rows) == 1 and rows[0]["ok"] is False  # 预算熔断按重试耗尽记一行
    assert not (tmp / "gwstate/quota_pause.json").exists()  # 上游故障不写配额暂停


def test_s10_text_block_trust_only_typed_text_blocks():
    body = {"content": [
        {"type": "text", "text": "A"},
        {"type": "thinking", "thinking": "不得当答案"},
        {"type": "tool_use", "id": "x", "text": "SHOULD_NOT_APPEAR"},
        "裸字符串块也不算",
        {"type": "text", "text": "B"},
    ]}
    assert R._message_text(body) == "AB"
    assert "SHOULD_NOT_APPEAR" not in R._message_text(body)

    resp = types.SimpleNamespace(choices=[types.SimpleNamespace(
        message=types.SimpleNamespace(content=[
            {"type": "text", "text": "C"},
            {"type": "thinking", "thinking": "x"},
        ]))])
    assert R._litellm_text(resp) == "C"
    assert R._message_text({"content": "纯字符串原样返回"}) == "纯字符串原样返回"
    assert R._message_text({"content": None}) == ""


CALL_CACHE_ALLOWLIST = {
    # Phase 2 Task2：RunSpec.max_coding_rounds 字段+注释纯位移（417→421），
    # 无新增 CallReplay 落点。
    # Task6 拆块出生（seq42-47 锁内适配，用户 2026-10-20 裁决）：ChunkSpec/
    # ChunkPromptContext/RunSpec 新字段 + 逐块校验/渲染模块级代码插入后纯位移
    # （421→632）；拆块路径复用同一 _replay_cache 构造点，无新增落点。
    # seq48 生产首验块0 冗长截断：CHUNK_MAX_CODE_CHARS 常量+机器校验+合同
    # 提示注入（+7），复审 NIT 超长 raise 折行（再 +1）后纯位移（632→641，
    # +8 行全在构造点之前），仍无新增落点。
    # 2026-10-21 守卫回提 20000→26000：值行同行改数字不增行，溯源注释 2 行
    # 扩为 7 行（净 +5）全在构造点之前，纯位移（641→646）；拆块路径仍复用
    # 唯一 _replay_cache 构造点，无新增落点。
    # 2026-10-21 run4 sentinel 围栏接缝：围栏常量/正则/扫描器/_extract_chunk
    # 模块级代码插入（含 docstring/注释，净 +169 行）全在构造点之前，纯位移
    # （646→815）；拆块路径仍复用唯一 _replay_cache 构造点，无新增落点。
    # 2026-10-21 三签修复：MAJOR-1 重名不回显（+3 注释/折行）+ LOW-1 围栏行
    # 尾类收紧注释（+3）全在构造点之前，纯位移（815→821）；LOW-4 注释改述
    # 刻意保持等行数，无新增落点。
    ("orchestrator/agent_runner/runner.py", 821),
    # Task0b：ENGINE_SCENARIOS template→solution_id + resolver.stage 插入后
    # 纯位移（task7 330→331、task8 424→428），无新增 CallReplay 落点。
    # Task4：frames.contact_window_plan 并行接缝注入纯位移
    # （task7 331→340、task8 428→431），无新增 CallReplay 落点。
    # Task6：make_evaluate_seam 加参 + active-only 视觉闸插入，skip 分支不
    # 构造 CallReplay，落点仍为唯一一处（431→475 纯位移），无第五落点。
    ("orchestrator/graph/task7.py", 340),
    ("orchestrator/graph/task8.py", 475),
    # Phase 2 Task1：S-5 有意关闭——macro 方向步加调用级重放
    # （key macro_{thread}_r{round}_a{attempt}，同一 phase1 .call_cache）。
    # Phase 2 Task2：ascent_claims 校验+批判段注入纯位移（422→506），落点不变。
    ("orchestrator/direction/setter.py", 506),
}


def test_s10_call_cache_landing_sites_enumerated():
    found = {}
    for py in REPO.rglob("*.py"):
        rel = py.relative_to(REPO).as_posix()
        if rel.startswith("tests/") or "__pycache__" in rel:
            continue
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                    and node.func.id == "CallReplay":
                found[(rel, node.lineno)] = ast.unparse(node.args[0])
    assert set(found) == CALL_CACHE_ALLOWLIST, (
        "生产 CallReplay 落点只允许这四处："
        f"{sorted(set(found) ^ CALL_CACHE_ALLOWLIST)}")
    # 四处必须显式落在 Phase 1 封存的 phase1_root()/.call_cache（禁新开第二落点）
    for arg_src in found.values():
        assert "phase1_root()" in arg_src and ".call_cache" in arg_src, arg_src


# ---------- quota_pause v2 schema 冻结（12 键 + 写件断言） ----------

def test_quota_pause_v2_schema_freeze(tmp_path, monkeypatch):
    assert len(QUOTA_PAUSE_SCHEMA_V2_KEYS) == 12
    assert len(set(QUOTA_PAUSE_SCHEMA_V2_KEYS)) == 12  # 无重复键

    tpl = json.loads((REPO / "scripts/milestone2"
                      / "quota_pause_schema_v2.template.json").read_text())
    assert tpl["schema_version"] == 2
    assert tuple(tpl["keys"]) == QUOTA_PAUSE_SCHEMA_V2_KEYS  # 键序也锁
    assert "QUOTA_PAUSE_SCHEMA_V2_KEYS" in tpl["freeze_rule"]
    assert "0600" in tpl["install"]
    # 仓库内模板必须保持空白签收（真签收件在仓外 phase2/quota，绝不入 git）
    assert tpl["user_signoff"]["signed_by"] == ""
    assert tpl["user_signoff"]["decision"] == ""

    # 写件路径实测：写出的 quota_pause.json 键集与键序恰为 12 键常量
    monkeypatch.setenv("SIMPRAXIS_GATEWAY_STATE", str(tmp_path / "gwstate"))
    state = ql._write_budget_pause(
        "task_hard_cap", {"task": 4, "task_used": 24, "task_hard_cap": 24},
        stage="coding_debug", scope="task", affected_stages=["coding_debug"])
    assert tuple(state) == QUOTA_PAUSE_SCHEMA_V2_KEYS
    written = json.loads((tmp_path / "gwstate/quota_pause.json").read_text())
    assert tuple(written) == QUOTA_PAUSE_SCHEMA_V2_KEYS
    assert written["schema_version"] == 2 and written["scope"] == "task"


# ---------- Task6 单任务硬顶自批提顶（§6.3，2026-10-16 用户自主授权） ----------

def test_task6_cap_80_to_86_and_hard_cap_104_to_112_phase2():
    """Task6 局部硬顶六次自批提顶（授权依据：2026-10-16 用户预算自主授权 +
    Phase_2_Plan_detailed.md §6.3）：16→24（thinking 诊断 seq32-40 触顶，
    commit df1c0a9），24→40（2026-10-20 晚拆块出生生产首验 seq48 后再触顶，
    commit 7a03a05，task_used=24/24），40→48（2026-10-21 凌晨 32k 拆块出生
    seq61-64 后再触顶：seq64 round2 chunk1 成功实呼 14005 out，round2 chunk2
    写 req 后 preflight 拦停于 task_used=40/40，拦停时全局 64/80；重放信封
    最坏 7 seq（seq62 r1 chunk3 网关成功但被长度机器拒绝、未入缓存须重付 1，
    r2 块2/3 重付 2，r3 四块 4；控制票 T6-B5 8 额）：task6≤47、全局≤71，
    commit f918239），48→56（2026-10-21 晚：seq65 run1 出生成功仅重付 1，
    seq66/67 三轮修复后轮次耗尽 human_failure，用户裁决①重挂 run_idx=2；
    run2 最坏信封 12 seq——checkpoint 倒带后 run_idx 重算为 1（键名仍是
    coding_<thread>_r1_round*_chunk*），但 callcache 指纹含消息体，合同第 7
    条+骨架两注使全部键 miss（安全方向），出生 4 块全付；最坏三轮拆块出生
    全败 3×4=12（现实：出生 4+至多 2 轮单补丁修复=6）；task6 已用 43 →
    ≤55/56，全局 67 → ≤79/80；控制票 T6-B6 12 额 beyond_normal，
    commit 2835189），56→80（2026-10-21 晚第五次：run2 将 T6-B6 12 seq
    用满（seq68-79），三轮拆块出生 chunk3 全败于 20000 字符守卫——code
    21256/24514/22293>20000，跨 run1/run2 该块 5 次尝试仅 1 次过守
    （seq65=18009），out token 从未触 32768，守卫是唯一约束；用户裁决
    守卫 20000→26000（runner.CHUNK_MAX_CODE_CHARS）、task6 56→80、
    HARD_CAP 80→104（全局硬顶史上首次）并重挂 run3；run3 信封 ≤14 seq
    （出生最坏 3×4=12+出生后至多 2 轮单补丁修复）→ task6 55→≤69/80、
    全局 79→≤93/104；NORMAL_BUDGET 60 不动；控制票 T6-B7 14 额，LEAD 自批），
    80→86（2026-10-21 晚第六次：run4 sentinel 围栏纯文本定界接缝上线，
    n=4→6 工厂规格；重挂信封 21 seq=出生最坏 3×6=18+出生后至多 2 轮单补丁
    修复+kill-9 重放 1；task6 已用 65 → ≤86/86 恰顶，全局 89 → ≤110/112；
    同步守卫 CHUNK_MAX_CODE_CHARS 26000→18000（n=6 各块推算 ≤15k 给约
    20% 余量，字符口径）；HARD_CAP 104→112（全局硬顶第二次提）；
    模型 ID/32768/600/NORMAL_BUDGET 60/4·7·8 任务顶均不动；
    控制票 T6-B9-run4-birth 21 额 beyond_normal，LEAD 自批）。
    本用例钉死当前提顶值、Phase 2 覆盖 Phase 1 的并集语义，以及全局双门：
    前四次提顶均不动全局门，第五次（2026-10-21）首次动 HARD_CAP（80→104），
    第六次（2026-10-21 run4）再动 HARD_CAP（104→112），常态 60 门始终未动。
    0 联网。
    """
    from orchestrator.agent_runner.runner import CHUNK_MAX_CODE_CHARS
    from orchestrator.gateway.quota_ledger import (
        HARD_CAP,
        NORMAL_BUDGET,
        PHASE2_TASK_CAPS,
        TASK_HARD_CAPS,
    )

    # 单块机器窗钉位（run4 26000→18000，字符口径），防值漂移；与 32768
    # 输出顶的余量由生产实测覆盖（见提交说明）。
    assert CHUNK_MAX_CODE_CHARS == 18000
    assert CHUNK_MAX_CODE_CHARS != 26000       # run3 旧窗随 n=6 接缝下线
    assert PHASE2_TASK_CAPS == {4: 24, 6: 86, 7: 16, 8: 16}
    # Phase 2 字典覆盖 Phase 1 并集语义：task=6 当前顶 86（第六次自批）；
    # task=4 仍为 24（覆盖 Phase 1 的 8）；7/8 不动。
    assert TASK_HARD_CAPS[6] == 86
    assert TASK_HARD_CAPS[4] == 24
    assert TASK_HARD_CAPS[7] == 16 and TASK_HARD_CAPS[8] == 16
    # 顺带钉死全局双门：常态 60 始终未动；硬顶 112 是第六次自批（run4，
    # 104→112）后的值——前四次提顶均不动全局门，第五次首次动 HARD_CAP。
    assert NORMAL_BUDGET == 60
    assert HARD_CAP == 112
    assert HARD_CAP != 104                     # run3 旧全局顶随 run4 提顶下线
